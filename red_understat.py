"""
Capa de red robusta para Understat, enchufada debajo de soccerdata.

El problema que resuelve (ver _scrape_jug.log):

    [red] 0 TLS Client Error: failed to do request: Get "https://understat.com/g...

"0" es el status_code que devuelve tls_requests (el cliente HTTP de
soccerdata, un binario Go) cuando la peticion ni siquiera llega a tener
respuesta HTTP: Understat corto la conexion keep-alive o limito la rafaga.
Es transitorio -- la misma URL funciona segundos despues -- pero
soccerdata 1.9.1 lo maneja mal para Understat:

  1. Understat._request_api() hace UN solo intento, sin reintento.
  2. Tras el fallo sigue usando la MISMA sesion, cuya conexion ya esta
     rota, asi que el fallo arrastra a las peticiones siguientes.
     (_download_and_save, el camino de otras fuentes, si recrea la
     sesion; el de Understat no.)
  3. Un solo partido que falla tumba el lote entero de read_player_match_stats.

blindar(reader) reemplaza _request_api en la instancia (no toca la
libreria instalada) por una version que:

  - espacia las peticiones (pausa minima global entre requests),
  - reintenta con backoff exponencial + jitter solo errores transitorios
    (conexion caida, 429, 403 de Cloudflare, 5xx, respuesta no-JSON),
  - recrea la sesion (y sus cookies) tras cada fallo,
  - tras 2 fallos seguidos alterna de cliente: tls_requests <-> requests,
    porque cuando el binario TLS es el que falla (o no se pudo descargar
    en Linux) requests plano suele pasar,
  - si agota los intentos lanza ConnectionError, que soccerdata ya
    interpreta como "saltar este partido" en read_player_match_stats
    (y bajar_jugadores lo reintenta en la proxima corrida).

Depende de internos de soccerdata (_request_api, _session, _init_session),
por eso requirements.txt ancla soccerdata==1.9.1. Si una version futura
los cambia, blindar() avisa por log y deja el reader como estaba.
"""

import io
import os
import random
import threading
import time

from logging_setup import get_logger

logger = get_logger("red_understat")

UNDERSTAT_URL = "https://understat.com"
HEADERS_API = {"X-Requested-With": "XMLHttpRequest"}
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36")

INTENTOS = 6
ESPERA_BASE = 3.0     # segundos: 3, 6, 12, 24, 48 (+-30% de jitter)
ESPERA_MAX = 90.0
PAUSA_MIN = 0.8       # separacion minima entre dos requests cualquiera
TIMEOUT = 30
FALLOS_PARA_ALTERNAR = 2

# Compartido por todos los readers del proceso: la app puede tener el
# hilo de actualizacion y una carga del usuario scrapeando a la vez, y
# para Understat eso es una sola IP haciendo rafagas.
_lock_ritmo = threading.Lock()
_ultimo_request = 0.0


def _espaciar():
    global _ultimo_request
    with _lock_ritmo:
        espera = _ultimo_request + PAUSA_MIN + random.random() * 0.4 - time.monotonic()
        if espera > 0:
            time.sleep(espera)
        _ultimo_request = time.monotonic()


def _es_transitorio(status):
    return status == 0 or status in (403, 408, 425, 429) or status >= 500


class _ClienteUnderstat:
    def __init__(self, reader):
        self._reader = reader
        self._modo = "tls"
        self._fallos_seguidos = 0
        self._sesion = None

    # -- sesiones ------------------------------------------------------

    def _nueva_sesion(self):
        viejo = self._sesion
        if viejo is not None:
            try:
                viejo.close()
            except Exception:
                pass

        if self._modo == "tls":
            self._sesion = self._reader._init_session()
            # Que el resto de soccerdata (read_schedule, etc.) tambien use
            # la sesion sana en vez de la que se rompio.
            self._reader._session = self._sesion
        else:
            import requests
            self._sesion = requests.Session()
            self._sesion.headers["User-Agent"] = USER_AGENT

        # Understat pide las cookies de la home antes de su API JSON
        # (lo mismo que hace soccerdata en _ensure_cookies).
        try:
            self._get_crudo(UNDERSTAT_URL, headers=None)
        except Exception as e:
            logger.info("no se pudieron precargar cookies de Understat (%s)", str(e)[:100])

    def _get_crudo(self, url, headers):
        """(status, contenido, retry_after). Normaliza tls_requests/requests."""
        if self._modo == "tls":
            r = self._sesion.get(url, headers=headers)
        else:
            r = self._sesion.get(url, headers=headers, timeout=TIMEOUT)
        return r.status_code, r.content, r.headers.get("Retry-After")

    # -- API -----------------------------------------------------------

    def request_api(self, url, filepath=None, no_cache=False):
        """Mismo contrato que soccerdata.Understat._request_api."""
        r = self._reader
        if (filepath is not None and filepath.exists()
                and not no_cache and not r.no_cache):
            return filepath.open(mode="rb")

        payload = self._get_con_reintentos(url)

        if not r.no_store and filepath is not None:
            filepath.parent.mkdir(parents=True, exist_ok=True)
            # Escritura atomica: un corte a mitad no deja en el cache de
            # soccerdata un JSON truncado que despues se lea como valido.
            tmp = filepath.with_name(filepath.name + ".tmp")
            tmp.write_bytes(payload)
            os.replace(tmp, filepath)
        return io.BytesIO(payload)

    def _get_con_reintentos(self, url):
        if self._sesion is None:
            self._nueva_sesion()

        detalle = ""
        for intento in range(INTENTOS):
            _espaciar()
            retry_after = None
            try:
                status, contenido, retry_after = self._get_crudo(url, HEADERS_API)
            except Exception as e:  # timeout, conexion reseteada, DNS...
                status, contenido, detalle = 0, b"", "{}: {}".format(type(e).__name__, e)

            if 200 <= status < 300:
                if contenido.lstrip()[:1] in (b"{", b"["):
                    self._fallos_seguidos = 0
                    return contenido
                # 200 con HTML = pagina de desafio/bloqueo, no datos
                status, detalle = 0, "respuesta 200 que no es JSON"
            elif status:
                detalle = "HTTP {}".format(status)
            elif not detalle:
                detalle = "conexion fallida (status 0)"

            if not _es_transitorio(status):
                raise ConnectionError("Understat {} -> {}".format(url, detalle))

            self._fallos_seguidos += 1
            if intento == INTENTOS - 1:
                break

            if self._fallos_seguidos >= FALLOS_PARA_ALTERNAR:
                self._modo = "requests" if self._modo == "tls" else "tls"
                self._fallos_seguidos = 0
                logger.info("Understat: cambiando a cliente '%s'", self._modo)

            espera = min(ESPERA_MAX, ESPERA_BASE * (2 ** intento))
            espera *= random.uniform(0.7, 1.3)
            try:
                espera = max(espera, float(retry_after))
            except (TypeError, ValueError):
                pass
            logger.warning("Understat %s: %s | reintento %s/%s en %.0fs",
                           url.replace(UNDERSTAT_URL, ""), detalle[:120],
                           intento + 1, INTENTOS - 1, espera)
            time.sleep(espera)
            self._nueva_sesion()

        raise ConnectionError("Understat {} sin respuesta tras {} intentos ({})".format(
            url, INTENTOS, detalle[:200]))


def blindar(reader):
    """Hace robusto un soccerdata.Understat. Devuelve el mismo reader."""
    if not all(hasattr(reader, a) for a in
               ("_request_api", "_init_session", "no_cache", "no_store")):
        logger.warning("soccerdata cambio sus internos: Understat queda sin "
                       "blindar (reintentos de red limitados)")
        return reader

    cliente = _ClienteUnderstat(reader)
    reader._request_api = cliente.request_api
    # Las cookies ya las pide _nueva_sesion(); el _ensure_cookies original
    # hace un GET sin reintento que puede fallar con el mismo error TLS.
    reader._ensure_cookies = lambda: None
    return reader
