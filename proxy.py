import json
import requests
import binascii
import os
import re
import time
import logging
import threading
import socket
from urllib.parse import urljoin, unquote_plus, parse_qs, urlparse

import proxy_http_scraper
try:
    from kodi_six import xbmc, xbmcaddon
except ImportError:
    import xbmc
    import xbmcaddon
from dns import customdns
from requests.exceptions import ConnectionError, RequestException
try:
    from urllib3.exceptions import IncompleteRead
except ImportError:
    from urllib2 import HTTPError as IncompleteRead  # Python 2 fallback

# Configuration
PORT = 8097
DEFAULT_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"

# Global caches and state
IP_CACHE_TS = {}
IP_CACHE_MP4 = {}
AGENT_OF_CHAOS = {}
COUNT_CLEAR = {}
SHUTDOWN_EVENT = threading.Event()
customdns(cache_ttl=14400)  # Ativa DNS customizado com cache de 4 horas

# Logging setup
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s')

# Kodi Monitor to detect shutdown
def monitor_kodi_shutdown(server):
    """Monitor Kodi shutdown and stop the proxy server."""
    monitor = xbmc.Monitor()
    while not monitor.abortRequested():
        if monitor.waitForAbort(1):
            break
    SHUTDOWN_EVENT.set()
    if server:
        try:
            server.close()
        except Exception as e:
            logging.error("Error closing server: %s", e)
    logging.info("Proxy server stopped due to Kodi shutdown.")

def get_ip(headers, client_address):
    """Extract client IP from request headers or remote address."""
    forwarded_for = headers.get("X-Forwarded-For", "")
    real_ip = headers.get("X-Real-IP", "")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    elif real_ip:
        return real_ip
    return client_address[0]

def get_cache_key(client_ip, url):
    """Generate cache key from client IP and URL."""
    return "%s:%s" % (client_ip, url)

def rewrite_m3u8_urls(playlist_content, base_url, scheme, host):
    """Rewrite URLs in m3u8 playlist to proxy through the server."""
    def replace_url(match):
        segment_url = match.group(0).strip()
        if segment_url.startswith('#') or not segment_url or segment_url == '/':
            return segment_url
        try:
            absolute_url = urljoin(base_url + '/', segment_url)
            if not (absolute_url.endswith('.ts') or '/hl' in absolute_url.lower() or absolute_url.endswith('.m3u8')):
                logging.debug("[HLS Proxy] Ignoring invalid URL in m3u8: %s" % absolute_url)
                return segment_url
            try:
                from urllib import quote  # Python 2
            except ImportError:
                from urllib.parse import quote  # Python 3
            proxied_url = "%s://%s/hlsretry?url=%s" % (scheme, host, quote(absolute_url))
            return proxied_url
        except ValueError as e:
            logging.debug("[HLS Proxy] Error resolving URL %s: %s" % (segment_url, e))
            return segment_url
    return re.sub(r'^(?!#)\S+', replace_url, playlist_content, flags=re.MULTILINE)

def stream_response(response, client_ip, url, headers, sess):
    """Stream response chunks, caching for .mp4 and .ts files."""
    cache_key = get_cache_key(client_ip, url) if any(ext in url.lower() for ext in ['.mp4', '.m3u8']) else client_ip
    mode_ts = [False]  # Use list for Python 2 compatibility

    def generate_chunks():
        bytes_read = 0
        try:
            for chunk in response.iter_content(chunk_size=4096):
                if chunk:
                    bytes_read += len(chunk)
                    if '.mp4' in url.lower():
                        IP_CACHE_MP4.setdefault(cache_key, []).append(chunk)
                        if len(IP_CACHE_MP4[cache_key]) > 20:
                            IP_CACHE_MP4[cache_key].pop(0)
                    elif '.ts' in url.lower() or '/hl' in url.lower():
                        mode_ts[0] = True
                        IP_CACHE_TS.setdefault(cache_key, []).append(chunk)
                        if len(IP_CACHE_TS[cache_key]) > 20:
                            IP_CACHE_TS[cache_key].pop(0)
                    yield chunk
        except (IncompleteRead, ConnectionError) as e:
            logging.debug("[HLS Proxy] Error processing chunks (bytes read: %d): %s" % (bytes_read, e))
            cache = IP_CACHE_TS if mode_ts[0] else IP_CACHE_MP4
            for chunk in cache.get(cache_key, [])[-5:]:
                yield chunk
        finally:
            try:
                sess.close()
            except:
                pass
    return generate_chunks()

def stream_cache(client_ip, url):
    """Stream cached chunks for .mp4 or .ts files."""
    if url:
        cache_key = get_cache_key(client_ip, url) if any(ext in url.lower() for ext in ['.mp4', '.m3u8']) else client_ip
        cache = IP_CACHE_MP4 if '.mp4' in url.lower() else IP_CACHE_TS if ('.ts' in url.lower() or '/hl' in url.lower()) else None
        if cache:
            def generate_cached_chunks():
                if cache_key in cache:
                    for chunk in cache.get(cache_key, [])[-5:]:
                        yield chunk
                else:
                    logging.debug("[HLS Proxy] Cache empty for %s" % cache_key)
            return generate_cached_chunks()
    return None

def parse_headers(request):
    """Parse HTTP headers from raw request."""
    headers = {}
    for line in request.splitlines():
        if ': ' in line:
            key, value = line.split(': ', 1)
            headers[key] = value
    return headers

def handle_request(client_socket, client_address, server_socket):
    """Handle incoming HTTP request."""
    try:
        client_socket.settimeout(5)
        request_data = client_socket.recv(4096).decode('utf-8', errors='ignore')
        if not request_data:
            return

        # Parse request
        lines = request_data.splitlines()
        if not lines:
            return
        request_line = lines[0]
        method, path, _ = request_line.split(' ', 2)
        if method != 'GET':
            client_socket.sendall(b"HTTP/1.1 405 Method Not Allowed\r\n\r\n")
            return

        headers = parse_headers(request_data)
        parsed_path = urljoin('http://localhost' + path, path)  # Fake base for parsing
        parsed = urlparse(parsed_path)
        query_params = parse_qs(parsed.query)
        path = parsed.path

        if path == "/":
            response = json.dumps({"message": "ONEPLAY PROXY"})
            client_socket.sendall(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n" +
                response.encode('utf-8')
            )
        elif path == "/stop":
            response = json.dumps({"message": "Proxy shutting down"})
            client_socket.sendall(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n" +
                response.encode('utf-8')
            )
            SHUTDOWN_EVENT.set()
            server_socket.close()
        elif path == "/hlsretry":
            url = query_params.get('url', [None])[0]
            try:
                url = unquote_plus(url)
            except:
                pass
            client_ip = get_ip(headers, client_address)
            cache_key = get_cache_key(client_ip, url) if url and any(x in url.lower() for x in ['.mp4', '.m3u8']) else client_ip

            if not url:
                client_socket.sendall(b"HTTP/1.1 400 Bad Request\r\n\r\nNo URL provided")
                return

            session = requests.Session()          
            req_headers = dict((k, v) for k, v in headers.items() if k.lower() != 'host')
            req_headers.update({'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36'})
            timeout = 15
            _ADDON_1 = xbmcaddon.Addon()
            _PROXY_HTTP_1 = _ADDON_1.getSetting('proxy_http') or 'false'
            if _PROXY_HTTP_1 == 'true':
                scraper = proxy_http_scraper.ProxyScraper()
                proxy_selected = scraper.get_proxy()                
                proxies = proxy_selected
                if proxies:
                    proxies_ = {
                        "http": proxies,
                        "https": proxies
                    }
                    try:
                        url = requests.get(url, headers=req_headers, allow_redirects=True, proxies=proxies_, stream=True, timeout=10).url
                    except:
                        pass
            
            original_headers = req_headers.copy()
            max_retries = 7
            attempts = 0
            tried_without_range = [False]
            change_user_agent = [False]
            media_type = (
                'video/mp4' if '.mp4' in url.lower()
                else 'video/mp2t' if '.ts' in url.lower() or '/hl' in url.lower()
                else 'application/octet-stream'
            )
            response_headers = {}
            status = 200

            while attempts < max_retries:
                # if '/hl' in url.lower() and '_' in url.lower() and '.ts' in url.lower():
                #     try:
                #         seg_ = re.findall(r'_(.*?)\.ts', url)[0]
                #         url = url.replace('_%s.ts' % seg_, '_%s.ts' % (int(seg_) + 1))
                #     except:
                #         pass                
                try:
                    range_header = req_headers.get('Range')
                    if '.mp4' in url.lower() and range_header and tried_without_range[0]:
                        req_headers.pop('Range', None)

                    if AGENT_OF_CHAOS.get(cache_key) and not ('.ts' in url.lower() or '/hl' in url.lower()):
                        req_headers['User-Agent'] = AGENT_OF_CHAOS[cache_key] if change_user_agent[0] else original_headers.get('User-Agent', DEFAULT_USER_AGENT)
                    elif '.ts' in url.lower() or '/hl' in url.lower():
                        req_headers['User-Agent'] = binascii.b2a_hex(os.urandom(20))[:32] if change_user_agent[0] or not req_headers.get('User-Agent') else original_headers.get('User-Agent', DEFAULT_USER_AGENT)

                    response = session.get(url, headers=req_headers, allow_redirects=True, stream=True, timeout=timeout)
                    logging.debug("HLS PROXY: URL %s, attempt %s, status code %s" % (url, attempts, response.status_code))

                    if response.status_code in (200, 206):
                        if '.mp4' in url.lower() or '.m3u8' in url.lower():
                            url = response.url
                        change_user_agent[0] = False
                        if client_ip in COUNT_CLEAR and COUNT_CLEAR.get(client_ip, 0) > 4:
                            try:
                                AGENT_OF_CHAOS.pop(cache_key, None)
                                IP_CACHE_MP4.pop(cache_key, None)
                                IP_CACHE_TS.pop(cache_key, None)
                            except:
                                pass
                            COUNT_CLEAR[client_ip] = 0
                        else:
                            COUNT_CLEAR[client_ip] = COUNT_CLEAR.get(client_ip, 0) + 1

                        content_type = response.headers.get("content-type", "").lower()
                        if "mpegurl" in content_type or ".m3u8" in url.lower():
                            base_url = url.rsplit('/', 1)[0]
                            playlist_content = response.content.decode('utf-8', errors='ignore')
                            rewritten = rewrite_m3u8_urls(playlist_content, base_url, 'http', '127.0.0.1:%d' % PORT)
                            client_socket.sendall(
                                b"HTTP/1.1 200 OK\r\nContent-Type: application/x-mpegURL\r\n\r\n" +
                                rewritten.encode('utf-8')
                            )
                            return

                        # if '/hl' in url.lower() and '_' in url.lower() and '.ts' in url.lower():
                        #     try:
                        #         seg_ = re.findall(r'_(.*?)\.ts', url)[0]
                        #         url = url.replace('_%s.ts' % seg_, '_%s.ts' % (int(seg_) + 1))
                        #     except:
                        #         pass

                        media_type = (
                            'video/mp4' if '.mp4' in url.lower()
                            else 'video/mp2t' if '.ts' in url.lower() or '/hl' in url.lower()
                            else response.headers.get("content-type", "application/octet-stream")
                        )
                        response_headers = dict((k, v) for k, v in response.headers.items()
                                                if k.lower() in ['content-type', 'accept-ranges', 'content-range'])
                        status = 206 if response.status_code == 206 else 200

                        header_str = "HTTP/1.1 %d OK\r\n" % status
                        for k, v in response_headers.items():
                            header_str += "%s: %s\r\n" % (k, v)
                        header_str += "Content-Type: %s\r\n\r\n" % media_type
                        client_socket.sendall(header_str.encode('utf-8'))

                        for chunk in stream_response(response, client_ip, url, req_headers, session):
                            client_socket.sendall(chunk)
                        return

                    elif response.status_code == 416 and range_header and not tried_without_range[0]:
                        tried_without_range[0] = True
                        continue
                    else:
                        change_user_agent[0] = True
                        logging.debug("Error code %d, attempt %d" % (response.status_code, attempts))
                        AGENT_OF_CHAOS[cache_key] = binascii.b2a_hex(os.urandom(20))[:32]
                        time.sleep(3)
                        attempts += 1
                        if '.ts' in url.lower() or '/hl' in url.lower() or '.mp4' in url.lower():
                            header_str = "HTTP/1.1 %d OK\r\nContent-Type: %s\r\n" % (status, media_type)
                            for k, v in response_headers.items():
                                header_str += "%s: %s\r\n" % (k, v)
                            header_str += "\r\n"
                            client_socket.sendall(header_str.encode('utf-8'))
                            for chunk in stream_cache(client_ip, url) or []:
                                client_socket.sendall(chunk)
                            return
                except RequestException as e:
                    change_user_agent[0] = True
                    logging.debug("Unknown error: %s" % e)
                    AGENT_OF_CHAOS[cache_key] = binascii.b2a_hex(os.urandom(20))[:32]
                    time.sleep(3)
                    attempts += 1
                    if '.ts' in url.lower() or '/hl' in url.lower() or '.mp4' in url.lower():
                        header_str = "HTTP/1.1 %d OK\r\nContent-Type: %s\r\n" % (status, media_type)
                        for k, v in response_headers.items():
                            header_str += "%s: %s\r\n" % (k, v)
                        header_str += "\r\n"
                        client_socket.sendall(header_str.encode('utf-8'))
                        for chunk in stream_cache(client_ip, url) or []:
                            client_socket.sendall(chunk)
                        return

            client_socket.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\nFailed to connect after multiple attempts")
        # New route for MP4 proxy with partial content support
        elif path == "/mp4proxy":
            url = query_params.get('url', [None])[0]
            try:
                url = unquote_plus(url)
            except:
                pass
            client_ip = get_ip(headers, client_address)
            cache_key = get_cache_key(client_ip, url)

            if not url or '.mp4' not in url.lower():
                client_socket.sendall(b"HTTP/1.1 400 Bad Request\r\n\r\nInvalid or missing MP4 URL")
                return

            session = requests.Session()
            req_headers = dict((k, v) for k, v in headers.items() if k.lower() != 'host')
            req_headers.update({'User-Agent': DEFAULT_USER_AGENT})

            # Handle proxy settings
            _ADDON_2 = xbmcaddon.Addon()
            _PROXY_HTTP_2 = _ADDON_2.getSetting('proxy_http') or 'false'
            proxies_ = None
            timeout = 20
            if _PROXY_HTTP_2 == 'true':
                scraper = proxy_http_scraper.ProxyScraper()
                proxy_selected = scraper.get_proxy()
                if proxy_selected:
                    proxies_ = {"http": proxy_selected, "https": proxy_selected}
                    try:
                        url = requests.get(url, headers=req_headers, allow_redirects=True, proxies=proxies_, stream=True, timeout=10).url
                    except:
                        pass

            max_retries = 7
            attempts = 0
            tried_without_range = [False]
            change_user_agent = [False]
            media_type = 'video/mp4'
            response_headers = {}
            status = 200

            while attempts < max_retries:
                try:
                    range_header = req_headers.get('Range')
                    if range_header and tried_without_range[0]:
                        req_headers.pop('Range', None)

                    if AGENT_OF_CHAOS.get(cache_key):
                        req_headers['User-Agent'] = AGENT_OF_CHAOS[cache_key] if change_user_agent[0] else req_headers.get('User-Agent', DEFAULT_USER_AGENT)

                    response = session.get(url, headers=req_headers, allow_redirects=True, stream=True, timeout=timeout, proxies=proxies_)
                    logging.debug("MP4 PROXY: URL %s, attempt %s, status code %s" % (url, attempts, response.status_code))

                    if response.status_code in (200, 206):
                        url = response.url
                        change_user_agent[0] = False
                        if client_ip in COUNT_CLEAR and COUNT_CLEAR.get(client_ip, 0) > 4:
                            try:
                                AGENT_OF_CHAOS.pop(cache_key, None)
                                IP_CACHE_MP4.pop(cache_key, None)
                            except:
                                pass
                            COUNT_CLEAR[client_ip] = 0
                        else:
                            COUNT_CLEAR[client_ip] = COUNT_CLEAR.get(client_ip, 0) + 1

                        response_headers = dict((k, v) for k, v in response.headers.items()
                                                if k.lower() in ['content-type', 'accept-ranges', 'content-range', 'content-length'])
                        status = 206 if response.status_code == 206 else 200

                        header_str = f"HTTP/1.1 {status} OK\r\n"
                        for k, v in response_headers.items():
                            header_str += f"{k}: {v}\r\n"
                        if 'content-type' not in response_headers:
                            header_str += f"Content-Type: {media_type}\r\n"
                        header_str += "Accept-Ranges: bytes\r\n\r\n"
                        client_socket.sendall(header_str.encode('utf-8'))

                        for chunk in stream_response(response, client_ip, url, req_headers, session):
                            client_socket.sendall(chunk)
                        return

                    elif response.status_code == 416 and range_header and not tried_without_range[0]:
                        tried_without_range[0] = True
                        continue
                    else:
                        change_user_agent[0] = True
                        logging.debug("MP4 PROXY: Error code %d, attempt %d" % (response.status_code, attempts))
                        AGENT_OF_CHAOS[cache_key] = binascii.b2a_hex(os.urandom(20))[:32]
                        time.sleep(3)
                        attempts += 1
                        header_str = f"HTTP/1.1 {status} OK\r\nContent-Type: {media_type}\r\n\r\n"
                        client_socket.sendall(header_str.encode('utf-8'))
                        for chunk in stream_cache(client_ip, url) or []:
                            client_socket.sendall(chunk)
                        return
                except RequestException as e:
                    change_user_agent[0] = True
                    logging.debug("MP4 PROXY: Unknown error: %s" % e)
                    AGENT_OF_CHAOS[cache_key] = binascii.b2a_hex(os.urandom(20))[:32]
                    time.sleep(3)
                    attempts += 1
                    header_str = f"HTTP/1.1 {status} OK\r\nContent-Type: {media_type}\r\n\r\n"
                    client_socket.sendall(header_str.encode('utf-8'))
                    for chunk in stream_cache(client_ip, url) or []:
                        client_socket.sendall(chunk)
                    return

            client_socket.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\nFailed to connect after multiple attempts")
        elif path == "/tsdownloader":
            url = query_params.get('url', [None])[0]
            if not url:
                client_socket.sendall(b"HTTP/1.1 400 Bad Request\r\n\r\nMissing 'url' parameter")
                return
            try:
                url = unquote_plus(url)
            except:
                pass

            req_headers = dict((k, v) for k, v in headers.items() if k.lower() != 'host')
            stop_ts = [False]
            last_url = ['']

            session = requests.Session()
            req_headers.update({'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36'})
            session.headers.update(req_headers)

            _ADDON_3 = xbmcaddon.Addon()
            _PROXY_HTTP_3 = _ADDON_3.getSetting('proxy_http') or 'false'
            if _PROXY_HTTP_3 == 'true':
                scraper = proxy_http_scraper.ProxyScraper()
                proxy_selected = scraper.get_proxy()
                proxies = proxy_selected
                if proxies:
                    proxies_ = {
                        "http": proxies,
                        "https": proxies
                    }
                    try:
                        url = requests.get(url, headers=req_headers, allow_redirects=True, proxies=proxies_, stream=True, timeout=10).url
                    except:
                        pass
                    last_url[0] = url

            def generate_ts():
                while not stop_ts[0] and not SHUTDOWN_EVENT.is_set():
                    try:
                        if not last_url[0]:
                            last_url[0] = session.get(url, allow_redirects=True, stream=True, timeout=7).url

                        response = session.get(last_url[0], stream=True, timeout=15)
                        if response.status_code == 200:
                            for chunk in response.iter_content(chunk_size=4096):
                                if stop_ts[0] or SHUTDOWN_EVENT.is_set():
                                    logging.warning("[TS Downloader] Stream stopped by client or shutdown.")
                                    return
                                if chunk:
                                    yield chunk
                            response.close()
                        else:
                            logging.warning("[TS Downloader] HTTP response %d" % response.status_code)

                    except Exception as e:
                        logging.warning("[TS Downloader] Stream error: %s" % e)

                logging.warning("[TS Downloader] Stream terminated by client or shutdown")

            client_socket.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: video/mp2t\r\n\r\n")
            try:
                for chunk in generate_ts():
                    client_socket.sendall(chunk)
            except (socket.error, BrokenPipeError):
                logging.warning("[TS Downloader] Client disconnected")
                stop_ts[0] = True
    except Exception as e:
        logging.error("Error handling request: %s" % e)
    finally:
        try:
            client_socket.close()
        except:
            pass

def is_proxy_running():
    """Check if the proxy is already running by checking the port."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(1)
        s.connect(('127.0.0.1', PORT))
        s.close()
        return True
    except socket.error:
        return False

def start_proxy():
    """Start the proxy server, ensuring only one instance runs."""
    if is_proxy_running():
        xbmc.log("[Proxy] Proxy is already running on port %d" % PORT, level=xbmc.LOGINFO)
        return False

    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        server_socket.bind(('127.0.0.1', PORT))
        server_socket.listen(5)
    except socket.error as e:
        xbmc.log("[Proxy] Failed to bind to port %d: %s" % (PORT, e), level=xbmc.LOGERROR)
        server_socket.close()
        return False

    def run_server():
        try:
            xbmc.log("[Proxy] Starting proxy server on port %d" % PORT, level=xbmc.LOGINFO)
            while not SHUTDOWN_EVENT.is_set():
                try:
                    client_socket, client_address = server_socket.accept()
                    threading.Thread(target=handle_request, args=(client_socket, client_address, server_socket)).start()
                except socket.error:
                    if not SHUTDOWN_EVENT.is_set():
                        logging.error("Error accepting connection")
        except Exception as e:
            xbmc.log("[Proxy] Server error: %s" % e, level=xbmc.LOGERROR)
        finally:
            server_socket.close()
            xbmc.log("[Proxy] Proxy server stopped", level=xbmc.LOGINFO)

    threading.Thread(target=run_server).start()
    threading.Thread(target=monitor_kodi_shutdown, args=(server_socket,)).start()
    return True

def kodiproxy():
    """Start the Kodi proxy server."""
    if start_proxy():
        xbmc.log("[Addon] Proxy started successfully", level=xbmc.LOGINFO)
    else:
        xbmc.log("[Addon] Failed to start proxy (already running)", level=xbmc.LOGERROR)

# if __name__ == '__main__':
#     addon = xbmcaddon.Addon(ADDON_ID)
#     xbmc.log("[Addon %s] Initializing proxy" % ADDON_ID, level=xbmc.LOGINFO)
#     kodiproxy()