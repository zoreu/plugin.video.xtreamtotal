# -*- coding: utf-8 -*-
import sys
import os
import time
import json
import html
import calendar
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime

from proxy_http_scraper import ProxyScraper
import xbmc
import xbmcgui
import xbmcplugin
import xbmcaddon
import xbmcvfs
import requests
import proxy_http_scraper

from dns import customdns
customdns(cache_ttl=14400)  # Ativa DNS customizado com cache de 4 horas

# =========================
# Configurações do Addon
# =========================
ADDON = xbmcaddon.Addon()
ADDON_ID = ADDON.getAddonInfo('id')
ADDON_HANDLE = int(sys.argv[1])
BASE_URL = ADDON.getSetting('host') or ''
USERNAME = ADDON.getSetting('username') or ''
PASSWORD = ADDON.getSetting('password') or ''
RETRY = ADDON.getSetting('retry') or 'false'
PROXY_HTTP = ADDON.getSetting('proxy_http') or 'false'
ENABLE_EPG = ADDON.getSetting('enable_epg') or 'false'
USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36'
HEADERS = {'User-Agent': USER_AGENT}
HOME = ADDON.getAddonInfo('path')
addonIcon = xbmcvfs.translatePath(os.path.join(HOME, 'icon.png'))

PROFILE_DIR = xbmcvfs.translatePath(ADDON.getAddonInfo('profile'))
EPG_XML_PATH = os.path.join(PROFILE_DIR, 'epg.xml')
EPG_META_PATH = os.path.join(PROFILE_DIR, 'epg_meta.json')
EPG_TTL = 24 * 3600  # 24h

_EPG_PARSED = None

# =========================
# Utilitários
# =========================
def log(msg, level=xbmc.LOGDEBUG):
    xbmc.log(f"[{ADDON_ID}] {msg}", level)

def show_dialog(title, message):
    xbmcgui.Dialog().ok(title, message)

def build_url(**kwargs):
    return sys.argv[0] + '?' + urllib.parse.urlencode(kwargs)

def ensure_profile_dir():
    if not xbmcvfs.exists(PROFILE_DIR):
        xbmcvfs.mkdir(PROFILE_DIR)

def get_param_map():
    params = {}
    if len(sys.argv) > 2 and sys.argv[2]:
        parsed = urllib.parse.urlparse(sys.argv[2])
        q = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        for k, v in q.items():
            params[k] = v[0] if isinstance(v, list) else v
    return params

def get_param(name, default=""):
    return PARAMS.get(name, default)

def fingerprint():
    return f"{BASE_URL}|{USERNAME}|{PASSWORD}"

def safe_requests_get(url, **kw):
    kw.setdefault('headers', HEADERS)
    kw.setdefault('timeout', 30)

    # Se houver proxy definido, adiciona
    if PROXY_HTTP == 'true':
        scraper = proxy_http_scraper.ProxyScraper()
        proxy = scraper.get_proxy()
        if proxy:
            kw.setdefault('proxies', {
                "http": proxy,
                "https": proxy
            })

    r = requests.get(url, **kw)
    r.raise_for_status()
    return r

def get_json(endpoint):
    if not all([BASE_URL, USERNAME, PASSWORD]):
        log("Credenciais incompletas.", xbmc.LOGERROR)
        show_dialog("Erro", "Configure host, usuário e senha nas configurações.")
        return None
    url = f"{BASE_URL.rstrip('/')}/player_api.php?username={USERNAME}&password={PASSWORD}&{endpoint}"
    log(f"API: {url}")
    try:
        r = safe_requests_get(url)
        return r.json()
    except requests.RequestException as e:
        log(f"Erro na requisição: {e}", xbmc.LOGERROR)
        show_dialog("Erro", f"Falha na API: {e}")
    except ValueError:
        log(f"Resposta inválida (não-JSON) da API: {url}", xbmc.LOGERROR)
        show_dialog("Erro", "Resposta da API não é JSON válido.")
    return None

# =========================
# EPG (cache + parsing)
# =========================
def epg_meta_load():
    if os.path.exists(EPG_META_PATH):
        try:
            with open(EPG_META_PATH, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def epg_meta_save(meta):
    try:
        with open(EPG_META_PATH, 'w', encoding='utf-8') as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log(f"Falha ao salvar meta EPG: {e}", xbmc.LOGERROR)

def epg_should_refresh():
    meta = epg_meta_load()
    fp = fingerprint()
    meta_fp = meta.get('fingerprint')
    fetched_at = meta.get('fetched_at', 0)
    if meta_fp != fp:
        log("Fingerprint mudou (host/usuário/senha). Renovando EPG.")
        return True
    if not os.path.exists(EPG_XML_PATH):
        log("EPG não existe. Baixando.")
        return True
    if (time.time() - fetched_at) >= EPG_TTL:
        log("EPG expirado. Renovando.")
        return True
    return False

def epg_download():
    ensure_profile_dir()
    url = f"{BASE_URL.rstrip('/')}/xmltv.php?username={USERNAME}&password={PASSWORD}"
    log(f"Baixando EPG: {url}")
    r = safe_requests_get(url)
    with open(EPG_XML_PATH, 'w', encoding='utf-8') as f:
        f.write(r.text)
    epg_meta_save({'fingerprint': fingerprint(), 'fetched_at': time.time()})

def parse_xmltv_time(ts):
    """Converte timestamp XMLTV para UNIX timestamp. Retorna agora() se inválido."""
    if not ts:
        return int(time.time())
    ts = ts.strip()
    if len(ts) < 14 or not ts[:14].isdigit():
        log(f"Timestamp XMLTV inválido: {ts}")
        return int(time.time())
    
    base = ts[:14]
    try:
        dt = datetime.strptime(base, "%Y%m%d%H%M%S")
    except Exception:
        return int(time.time())
    
    offset_secs = 0
    rest = ts[14:].strip()
    if rest:
        rest = rest.replace(' ', '')
        if rest.startswith(('+', '-')) and len(rest) >= 5:
            try:
                sign = 1 if rest[0] == '+' else -1
                hh = int(rest[1:3])
                mm = int(rest[3:5])
                offset_secs = sign * (hh * 3600 + mm * 60)
            except Exception:
                offset_secs = 0

    epoch = calendar.timegm(dt.timetuple()) - offset_secs
    return epoch if epoch > 0 else int(time.time())

def normalize_epg_channel_id(cid):
    if not cid:
        return ''
    return cid.strip().lower().replace('&amp;', '&')

def epg_load_parsed():
    global _EPG_PARSED

    if epg_should_refresh():
        try:
            epg_download()
        except Exception as e:
            log(f"Falha ao baixar EPG: {e}", xbmc.LOGERROR)
            _EPG_PARSED = {'channels': {}, 'progs': {}}
            return _EPG_PARSED

    if not os.path.exists(EPG_XML_PATH):
        _EPG_PARSED = {'channels': {}, 'progs': {}}
        return _EPG_PARSED

    try:
        tree = ET.parse(EPG_XML_PATH)
        root = tree.getroot()

        channels = {}
        progs = {}

        for c in root.findall(".//channel"):
            cid = normalize_epg_channel_id(c.get('id'))
            dn = (c.findtext('display-name') or '').strip()
            channels[cid] = dn

        for p in root.findall(".//programme"):
            cid = normalize_epg_channel_id(p.get('channel'))

            # usa start_timestamp e stop_timestamp se existirem
            try:
                start = int(p.get('start_timestamp'))
            except (TypeError, ValueError):
                start = parse_xmltv_time(p.get('start'))

            try:
                stop = int(p.get('stop_timestamp') or p.get('end_timestamp'))
            except (TypeError, ValueError):
                stop = parse_xmltv_time(p.get('stop') or p.get('end'))

            # garante start < end
            if stop <= start:
                stop = start + 3600

            title = (p.findtext('title') or '').strip()
            desc = (p.findtext('desc') or '').strip()

            if cid not in progs:
                progs[cid] = []

            progs[cid].append({'start': start, 'end': stop, 'title': title, 'desc': desc})

        for cid, arr in progs.items():
            arr.sort(key=lambda x: x['start'])

        _EPG_PARSED = {'channels': channels, 'progs': progs}
        log(f"EPG carregado: canais={len(channels)}, programas={sum(len(v) for v in progs.values())}")
    except Exception as e:
        log(f"Erro parseando EPG: {e}", xbmc.LOGERROR)
        _EPG_PARSED = {'channels': {}, 'progs': {}}

    return _EPG_PARSED

def epg_lookup_current_next(epg_channel_id, epg):
    #epg = epg_load_parsed()
    cid = normalize_epg_channel_id(epg_channel_id)
    now = int(time.time())
    
    plist = epg['progs'].get(cid, [])
    # log(f'CID EPG: {cid}')
    # log(f'LISTA DO CANAL: {plist}')    
    current, nextp = None, None
    
    for i, pr in enumerate(plist):
        start = pr.get('start') or now
        end = pr.get('end') or (start + 3600)
        if end <= start:
            end = start + 3600
        pr['start'] = start
        pr['end'] = end
        
        if start <= now < end:
            current = pr
            if i + 1 < len(plist):
                nextp = plist[i + 1]
            break
        if start > now:
            nextp = pr
            if i - 1 >= 0 and plist[i-1]['end'] > now:
                current = plist[i-1]
            break

    return current, nextp

# =========================
# UI (menus)
# =========================
def build_menu(items, mode=None, is_playable=False):
    if not items:
        xbmcplugin.endOfDirectory(ADDON_HANDLE)
        return

    for item in items:
        label = item.get('title', '')
        li = xbmcgui.ListItem(label=label)
        icon = item.get('icon', addonIcon)
        if icon:
            li.setArt({'icon': icon, 'thumb': icon})
        else:
            li.setArt({'icon': addonIcon, 'thumb': addonIcon})

        plot = item.get('plot', '') or ''
        if plot:
            li.setInfo('video', {'title': label, 'plot': plot})
        else:
            li.setInfo('video', {'title': label})

        if 'url' in item and is_playable:
            play = item['url'] + '|User-Agent=' + USER_AGENT
            if 'filme:' in label.lower() or 'live' in label.lower():
                params = {'mode': 'play', 'url': play, 'title': label, 'icon': icon, 'normalplayer': 'true'}
                url = build_url(**params)
            else:
                li.setProperty('IsPlayable', 'true')                
                url = build_url(mode='play', url=play)
            xbmcplugin.addDirectoryItem(ADDON_HANDLE, url, li, False)
        else:
            url_params = {'mode': mode} if mode else {'mode': item.get('mode')}
            if 'params' in item:
                qs = urllib.parse.parse_qs(item['params'], keep_blank_values=True)
                for k, v in qs.items():
                    url_params[k] = v[0] if isinstance(v, list) else v
            url = build_url(**url_params)
            xbmcplugin.addDirectoryItem(ADDON_HANDLE, url, li, item.get('folder', True))

    xbmcplugin.endOfDirectory(ADDON_HANDLE)

# =========================
# Funções de dados
# =========================
def get_categories(endpoint):
    data = get_json(endpoint)
    if not data:
        return []

    if isinstance(data, list):
        cats = data
    elif isinstance(data, dict) and 'categories' in data:
        cats = data['categories']
    else:
        cats = list(data.values()) if isinstance(data, dict) else []

    items = []
    for cat in cats:
        name = html.unescape(cat.get('category_name', 'Sem nome'))
        if 'adult' in name.lower():
            continue
        items.append({
            'title': name,
            'params': f"category_id={cat.get('category_id', '')}"
        })
    return items

def ensure_epg_loaded():
    global _EPG_PARSED
    if _EPG_PARSED is None:
        _EPG_PARSED = epg_load_parsed()
    return _EPG_PARSED

def annotate_live_with_epg(items_from_api):
    epg = ensure_epg_loaded()
    out = []
    for s in items_from_api:
        name = s.get('title') or s.get('name') or 'Sem nome'
        epg_id = s.get('epg_channel_id')
        current, nextp = epg_lookup_current_next(epg_id, epg) if epg_id else (None, None)

        label = name
        plot = ''
        if current:
            label = f"{name} - {current.get('title', '').strip()}"
            plot += f"Agora: {current.get('title', '').strip()}\n{current.get('desc', '').strip()}\n\n"
        if nextp:
            plot += f"Próximo: {nextp.get('title', '').strip()}\n{nextp.get('desc', '').strip()}"

        s2 = dict(s)
        s2['title'] = label
        if plot.strip():
            s2['plot'] = plot.strip()
        out.append(s2)
    return out

def get_items(endpoint, category_id=None):
    params = f"&category_id={category_id}" if category_id else ""
    data = get_json(f"action={endpoint}{params}")
    if not data:
        return []

    items = []

    if endpoint in ['get_live_streams', 'get_vod_streams']:
        for s in data:
            url = s.get('stream_url', '')
            sid = s.get('stream_id')
            stype = s.get('stream_type', '')
            name = html.unescape(s.get('name', 'Sem nome'))
            icon = s.get('stream_icon', '')
            epg_channel_id = s.get('epg_channel_id') or None

            if sid:
                if endpoint == 'get_live_streams':
                    url = f'{BASE_URL.rstrip("/")}/live/{USERNAME}/{PASSWORD}/{sid}.m3u8'
                else:
                    ext = 'mp4'
                    url = f'{BASE_URL.rstrip("/")}/{stype}/{USERNAME}/{PASSWORD}/{sid}.{ext}'

            item = {'title': name, 'url': url, 'icon': icon}
            if epg_channel_id:
                item['epg_channel_id'] = epg_channel_id
            items.append(item)

        if endpoint == 'get_live_streams':
            if ENABLE_EPG.lower() == 'true':
                items = annotate_live_with_epg(items)

    elif endpoint == 'get_series':
        for s in data:
            # Pegando capa da série (cover_big ou movie_image)
            icon = (s.get('info', {}) or {}).get('cover_big') or (s.get('info', {}) or {}).get('movie_image', '')
            if not icon:
                icon = s.get('cover') if s.get('cover') else s.get('backdrop_path', [''])[0]
            items.append({
                'title': html.unescape(s.get('name', 'Sem nome')),
                'params': f"series_id={s.get('series_id', '')}",
                'icon': icon
            })

    return items


def get_seasons(series_id):
    data = get_json(f"action=get_series_info&series_id={series_id}")
    if not data:
        return []

    seasons = data.get('episodes', {})
    items = []
    for season_name, episodes in seasons.items():
        ep_list = []
        for index, ep in enumerate(episodes):
            index += 1
            title = html.unescape(ep.get('title', 'Sem título'))
            eid = ep.get('id') or ep.get('episode_id') or ep.get('stream_id')
            ext = (ep.get('info', {}) or {}).get('container_extension') or 'mp4'
            icon = (ep.get('info', {}) or {}).get('cover_big') or (ep.get('info', {}) or {}).get('movie_image', '')
            url = f"{BASE_URL.rstrip('/')}/series/{USERNAME}/{PASSWORD}/{eid}.{ext}" if eid else ep.get('direct_source', '')
            ep_list.append({'title': str(index) + ' - ' + title, 'url': url, 'icon': icon})

        payload = urllib.parse.quote(json.dumps(ep_list, ensure_ascii=False), safe='')
        items.append({
            'title': f"Temporada {season_name}",
            'params': f"episodes={payload}&series_id={series_id}"
        })
    return items

def search_global(query):
    results = []
    for endpoint, prefix, playable in [
        ('get_live_streams', 'Live: ', True),
        ('get_vod_streams', 'Filme: ', True),
        ('get_series', 'Série: ', False)
    ]:
        items = get_items(endpoint)
        for i in items:
            if query.lower() in i['title'].lower():
                entry = {'title': prefix + i['title'], 'icon': i.get('icon', '')}
                if playable:
                    entry['url'] = i['url']
                    entry['plot'] = i.get('plot', '')
                else:
                    entry['mode'] = 'seasons'
                    entry['params'] = i.get('params', '')
                results.append(entry)
    if not results:
        show_dialog("Aviso", "Nenhum resultado encontrado.")
    return results

def get_account_info():
    data = get_json("")  # Requisição sem parâmetros adicionais
    if not data:
        return None

    user_info = data.get('user_info', {})
    if not user_info:
        log("Nenhuma informação de usuário retornada pela API.", xbmc.LOGERROR)
        show_dialog("Erro", "Não foi possível obter informações da conta.")
        return None

    # Extrair informações
    username = user_info.get('username', 'Não informado')
    status = user_info.get('status', 'Não informado')
    max_connections = user_info.get('max_connections', 'Não informado')
    active_connections = user_info.get('active_cons', 'Não informado')

    # Processar datas
    created_at = user_info.get('created_at', 0)
    exp_date = user_info.get('exp_date', 0)

    # Converter timestamps para formato brasileiro (DD/MM/YYYY)
    try:
        created_at_str = datetime.fromtimestamp(int(created_at)).strftime('%d/%m/%Y') if created_at and str(created_at).isdigit() else 'Não informado'
    except Exception:
        created_at_str = 'Não informado'

    try:
        exp_date_str = datetime.fromtimestamp(int(exp_date)).strftime('%d/%m/%Y') if exp_date and str(exp_date).isdigit() else 'Não informado'
    except Exception:
        exp_date_str = 'Não informado'

    # Montar texto para exibição
    info_text = (
        f"Nome de Usuário: {username}\n"
        f"Status: {status}\n"
        f"Data de Criação: {created_at_str}\n"
        f"Data de Vencimento: {exp_date_str}\n"
        f"Conexões Máximas: {max_connections}\n"
        f"Conexões Ativas: {active_connections}"
    )

    return info_text

def play_item(url, title, icon, normalplayer):
    import proxy
    PORT = proxy.PORT
    if RETRY == 'true':
        try:
            url = url.split('|')[0]
        except:
            pass          
        if '.m3u8' in url:        
            url_proxy = "http://127.0.0.1:{}/hlsretry?url=".format(PORT)
        else:
            url_proxy = "http://127.0.0.1:{}/mp4proxy?url=".format(PORT)
        url = url_proxy + url
    else:
        try:
            url = url.split('|')[0]
        except:
            pass          
        if '.m3u8' in url:
            url = url.replace('live/', '').replace('.m3u8', '')
            url_proxy = "http://127.0.0.1:{}/tsdownloader?url=".format(PORT)
        else:
            url_proxy = "http://127.0.0.1:{}/mp4proxy?url=".format(PORT)
        url = url_proxy + url
    proxy.kodiproxy()
    if normalplayer == 'false': 
        li = xbmcgui.ListItem(path=url)
        xbmcplugin.setResolvedUrl(ADDON_HANDLE, True, li)
    else:
        li = xbmcgui.ListItem(label=title, path=url)
        li.setArt({'icon': icon, 'thumb': icon})
        li.setInfo('video', {'title': title})
        player = xbmc.Player()
        player.play(item=url, listitem=li)

# =========================
# Rotas
# =========================
PARAMS = get_param_map()
mode = get_param('mode', 'main')
log(f"Modo: {mode}")

if mode == 'main':
    items = [
        {'title': 'Entrar', 'mode': 'enter'}
    ]
    build_menu(items)

elif mode == 'enter':
    if not BASE_URL or not USERNAME or not PASSWORD:
        ADDON.openSettings()
    else:
        items = [
            {'title': 'Informações da Conta', 'mode': 'account_info'},
            {'title': 'Pesquisa Global', 'mode': 'search'},
            {'title': 'TV (Canais Ao Vivo)', 'mode': 'tv'},
            {'title': 'Filmes', 'mode': 'movies'},
            {'title': 'Séries', 'mode': 'series'},
            {'title': 'Configurações', 'mode': 'settings'}
        ]
        build_menu(items)

elif mode == 'account_info':
    info_text = get_account_info()
    if info_text:
        xbmcgui.Dialog().textviewer("Informações da Conta", info_text)
    else:
        show_dialog("Erro", "Não foi possível obter informações da conta.")

elif mode == 'settings':
    ADDON.openSettings()

elif mode == 'tv':
    build_menu(get_categories('action=get_live_categories'), 'live_items')

elif mode == 'live_items':
    cid = get_param('category_id')
    build_menu(get_items('get_live_streams', cid), is_playable=True)

elif mode == 'movies':
    build_menu(get_categories('action=get_vod_categories'), 'movie_items')

elif mode == 'movie_items':
    cid = get_param('category_id')
    build_menu(get_items('get_vod_streams', cid), is_playable=True)

elif mode == 'series':
    build_menu(get_categories('action=get_series_categories'), 'series_items')

elif mode == 'series_items':
    cid = get_param('category_id')
    build_menu(get_items('get_series', cid), 'seasons')

elif mode == 'seasons':
    sid = get_param('series_id')
    build_menu(get_seasons(sid), 'episodes')

elif mode == 'episodes':
    eps_json = urllib.parse.unquote(get_param('episodes', '[]'))
    eps = json.loads(eps_json)
    build_menu(eps, is_playable=True)

elif mode == 'search':
    kb = xbmc.Keyboard('', 'Digite o que deseja buscar')
    kb.doModal()
    if kb.isConfirmed():
        query = kb.getText()
        build_menu(search_global(query), is_playable=True)

elif mode == 'play':
    url = get_param('url')
    normalplayer = get_param('normalplayer', 'false')
    title = get_param('title', 'Reproduzindo')
    icon = get_param('icon', addonIcon)
    play_item(url, title, icon, normalplayer)

else:
    show_dialog("Erro", f"Modo desconhecido: {mode}")
