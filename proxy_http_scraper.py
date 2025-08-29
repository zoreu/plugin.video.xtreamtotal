import requests
import random
import os
import sys
import json
import time
import logging

try:
    from kodi_six import xbmc, xbmcplugin, xbmcgui, xbmcaddon, xbmcvfs
except ImportError:
    import xbmc
    import xbmcplugin
    import xbmcgui
    import xbmcaddon
    import xbmcvfs
from dns import customdns

PY2 = sys.version_info[0] == 2
ADDON_ = xbmcaddon.Addon()
TRANSLATE_ = xbmc.translatePath if PY2 else xbmcvfs.translatePath
profile = TRANSLATE_(ADDON_.getAddonInfo('profile'))
if not os.path.exists(profile):
    os.makedirs(profile)

CACHE_FILE = os.path.join(profile, 'proxy_cache.json')
logging.basicConfig(level=logging.DEBUG)

customdns(cache_ttl=14400)  # Ativa DNS customizado com cache de 4 horas

# URL para pegar a lista de proxies
BASE_PROXIES_URL = 'https://raw.githubusercontent.com/TheSpeedX/SOCKS-List/master/http.txt'


class ProxyScraper:
    def __init__(self, cache_file=CACHE_FILE, cache_ttl=14400):
        self.cache_file = cache_file
        self.cache_ttl = cache_ttl
        self.cache = self._load_cache()

    def _load_cache(self):
        """Carrega o cache do arquivo JSON, removendo entradas expiradas."""
        try:
            if os.path.exists(self.cache_file):
                with open(self.cache_file, 'r') as f:
                    cache = json.load(f)
                current_time = time.time()
                # Mantém apenas entradas não expiradas
                valid_cache = {
                    key: data for key, data in cache.items()
                    if data['expires'] > current_time
                }
                return valid_cache
            return {}
        except Exception as e:
            logging.error(f"Erro ao carregar cache: {e}")
            return {}

    def _save_cache(self):
        """Salva o cache no arquivo JSON."""
        try:
            with open(self.cache_file, 'w') as f:
                json.dump(self.cache, f, indent=2)
        except Exception as e:
            logging.error(f"Erro ao salvar cache: {e}")

    def _fetch_new_proxy(self):
        """Busca um proxy novo da lista online."""
        try:
            response = requests.get(BASE_PROXIES_URL, headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36'
            }, timeout=10)
            if response.status_code == 200:
                proxies = response.text.splitlines()
                if proxies:
                    selected = random.choice(proxies)
                    proxy_auth = f"http://{selected}"
                    return proxy_auth
            logging.warning("Não foi possível obter proxies da lista online.")
        except Exception as e:
            logging.error(f"Erro ao buscar proxies: {e}")
        return None

    def get_proxy(self, key="default"):
        """Retorna um proxy do cache ou pega um novo se expirado."""
        current_time = time.time()
        # Verifica cache
        if key in self.cache:
            cached = self.cache[key]
            if cached['expires'] > current_time:
                logging.info(f"Usando proxy do cache: {cached['proxy']}")
                return cached['proxy']
            else:
                logging.info(f"Proxy expirado, removendo do cache: {cached['proxy']}")
                del self.cache[key]
                self._save_cache()

        # Busca novo proxy
        new_proxy = self._fetch_new_proxy()
        if new_proxy:
            self.cache[key] = {
                'proxy': new_proxy,
                'expires': current_time + self.cache_ttl
            }
            self._save_cache()
            logging.info(f"Novo proxy salvo no cache: {new_proxy}")
            return new_proxy

        logging.warning("Nenhum proxy disponível.")
        return None