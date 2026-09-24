"""
Test de humo del blindaje de red de Understat y del actualizador, sin red.

Simula el reader de soccerdata con respuestas fabricadas y verifica:
  1. Un "status 0" (el "TLS Client Error" de los logs) se reintenta con
     sesion nueva y termina devolviendo los datos.
  2. Un 404 no se reintenta: ConnectionError al primer intento.
  3. Agotar intentos -> ConnectionError (soccerdata salta ese partido).
  4. Un 200 con HTML (pagina de bloqueo) cuenta como fallo, no como dato.
  5. Tras 2 fallos seguidos alterna del cliente TLS a requests.
  6. El cache en disco de soccerdata se respeta y se escribe atomico.
  7. El Actualizador procesa la cola en su hilo sin bloquear, no duplica
     tareas y un SystemExit dentro de una tarea no mata el hilo.

Correr con:  python test_red_understat.py
"""

import os
import tempfile
import time
from pathlib import Path

import actualizador
import red_understat

fallos = []

# Sin esperas reales en los reintentos
red_understat.ESPERA_BASE = 0.0
red_understat.PAUSA_MIN = 0.0
red_understat.time.sleep = lambda s: None


def check(nombre, condicion, detalle=""):
    estado = "OK  " if condicion else "FALLA"
    print("  [{}] {}{}".format(estado, nombre, "  " + detalle if detalle else ""))
    if not condicion:
        fallos.append(nombre)


class Resp:
    def __init__(self, status, content=b'{"ok": 1}'):
        self.status_code = status
        self.content = content
        self.headers = {}


class SesionFalsa:
    """Devuelve las respuestas de `guion` en orden para la API; la home
    (precarga de cookies) siempre responde 200."""

    def __init__(self, guion, log):
        self.guion = guion
        self.log = log

    def get(self, url, headers=None, **kw):
        if url == red_understat.UNDERSTAT_URL:
            return Resp(200, b"<html>")
        self.log.append(url)
        return self.guion.pop(0)

    def close(self):
        pass


class ReaderFalso:
    no_cache = False
    no_store = False

    def __init__(self, guion):
        self.guion = guion
        self.pedidos = []
        self.sesiones_creadas = 0
        self._session = None

    def _init_session(self):
        self.sesiones_creadas += 1
        return SesionFalsa(self.guion, self.pedidos)

    def _request_api(self, url, filepath=None, no_cache=False):
        raise AssertionError("no deberia llamarse el original")


URL = red_understat.UNDERSTAT_URL + "/getMatchData/1"

print("=" * 66)
print("  BLINDAJE DE RED")
print("=" * 66)

# 1. status 0 dos veces... pero con alternancia a requests en el medio.
#    Para aislar el reintento TLS, un solo fallo y luego exito.
r = red_understat.blindar(ReaderFalso([Resp(0, b""), Resp(200)]))
datos = r._request_api(URL).read()
check("status 0 se reintenta y devuelve datos", datos == b'{"ok": 1}')
check("tras el fallo se recrea la sesion", r.sesiones_creadas == 2,
      "sesiones={}".format(r.sesiones_creadas))

# 2. 404 permanente
r = red_understat.blindar(ReaderFalso([Resp(404), Resp(200)]))
try:
    r._request_api(URL)
    check("404 lanza ConnectionError sin reintentar", False)
except ConnectionError:
    check("404 lanza ConnectionError sin reintentar", len(r.pedidos) == 1)

# 3 y 5. Siempre status 0: alterna a requests y al final ConnectionError
r = red_understat.blindar(ReaderFalso([Resp(0, b"")] * 20))
modos = []
original = red_understat._ClienteUnderstat._nueva_sesion


def espia(self):
    modos.append(self._modo)
    if self._modo == "requests":
        # requests "real" sustituido por la misma sesion falsa
        self._sesion = SesionFalsa(self._reader.guion, self._reader.pedidos)
    else:
        original(self)


red_understat._ClienteUnderstat._nueva_sesion = espia
try:
    r._request_api(URL)
    check("agotar intentos lanza ConnectionError", False)
except ConnectionError:
    check("agotar intentos lanza ConnectionError",
          len(r.pedidos) == red_understat.INTENTOS,
          "pedidos={}".format(len(r.pedidos)))
check("alterna a requests tras 2 fallos seguidos", "requests" in modos, str(modos))
red_understat._ClienteUnderstat._nueva_sesion = original

# 4. 200 con HTML no es un dato valido
r = red_understat.blindar(ReaderFalso([Resp(200, b"<html>bloqueado"), Resp(200)]))
check("200 con HTML se reintenta", r._request_api(URL).read() == b'{"ok": 1}'
      and len(r.pedidos) == 2)

# 6. cache de soccerdata
with tempfile.TemporaryDirectory() as d:
    ruta = Path(d) / "sub" / "match_1.json"
    r = red_understat.blindar(ReaderFalso([Resp(200)]))
    r._request_api(URL, ruta)
    check("escribe el cache de soccerdata", ruta.read_bytes() == b'{"ok": 1}')
    check("sin .tmp residual", not any(p.suffix == ".tmp" for p in ruta.parent.iterdir()))
    r._request_api(URL, ruta)  # guion vacio: si saliera a la red, reventaria
    check("segunda lectura sale del cache sin red", len(r.pedidos) == 1)

# blindar sobre algo que no parece un reader de soccerdata
cosa = object()
check("blindar ignora objetos desconocidos", red_understat.blindar(cosa) is cosa)

print("=" * 66)
print("  ACTUALIZADOR EN SEGUNDO PLANO")
print("=" * 66)

hechas = []


def ejecutar_falso(tarea, forzar=True):
    time.sleep(1.0)  # bastante mas que el umbral de "no bloquea"
    if tarea[0] == "rota":
        raise SystemExit("equipo sin mapear")
    hechas.append((tarea, forzar))


actualizador.ejecutar = ejecutar_falso
act = actualizador.Actualizador(automatico=False).iniciar()
t_ok = ("premier", ("2526",), "csv")
t_rota = ("rota", ("2526",), "csv")

inicio = time.time()
act.solicitar(t_ok)
act.solicitar(t_ok)       # duplicada: se ignora
act.solicitar(t_rota)
check("solicitar() no bloquea", time.time() - inicio < 0.5)
check("el estado refleja trabajo pendiente", act.estado()["ocupado"])

limite = time.time() + 10
while act.estado()["ocupado"] and time.time() < limite:
    time.sleep(0.05)

est = act.estado()
check("procesa la cola y termina", not est["ocupado"])
check("no duplica tareas", hechas == [(t_ok, True)], str(hechas))
check("registra el error de la tarea rota", t_rota in est["errores"])
check("ultima_ok apunta a la tarea buena", est["ultima_ok"] and est["ultima_ok"][0] == t_ok)

act.solicitar(t_ok, forzar=False)
limite = time.time() + 10
while act.estado()["ocupado"] and time.time() < limite:
    time.sleep(0.05)
check("el hilo sigue vivo tras un SystemExit", hechas[-1] == (t_ok, False))

# firma(): cambia cuando cambia el archivo
with tempfile.TemporaryDirectory() as d:
    actualizador.CACHE_DIR = d
    t = ("premier", ("2526",), "csv")
    check("firma con archivo faltante tiene None", actualizador.firma(*t) == (None,))
    ruta = os.path.join(d, "E0_2526.csv")
    open(ruta, "w").close()
    f1 = actualizador.firma(*t)
    os.utime(ruta, (1, 1))
    check("firma cambia al regenerarse el archivo", f1 != actualizador.firma(*t))

from datetime import datetime as dt
check("septiembre ya incluye la temporada en curso",
      actualizador.temporadas_recientes(3, dt(2026, 9, 24)) == ["2425", "2526", "2627"])
check("agosto todavia usa la temporada recien terminada",
      actualizador.temporadas_recientes(3, dt(2026, 8, 20)) == ["2324", "2425", "2526"])
check("enero sigue en la temporada que empezo en septiembre",
      actualizador.temporadas_recientes(1, dt(2027, 1, 15)) == ["2627"])
check("en_curso: combinacion con la temporada actual",
      actualizador.en_curso(("2526", "2627"), dt(2026, 9, 24)))
check("en_curso: temporadas viejas no",
      not actualizador.en_curso(("2425", "2526"), dt(2026, 9, 24)))
check("en_curso: en julio nada esta en curso",
      not actualizador.en_curso(("2627",), dt(2027, 7, 1)))

check("tareas_app cubre 4 ligas csv + 12 combinaciones xG",
      len(actualizador.tareas_app()) == 16, str(len(actualizador.tareas_app())))

print("=" * 66)
if fallos:
    print("  {} FALLOS: {}".format(len(fallos), ", ".join(fallos)))
    raise SystemExit(1)
print("  Todo OK")
