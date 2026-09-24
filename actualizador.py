"""
Actualizacion de datos en segundo plano.

La web nunca espera a la red: encola trabajo aqui y sigue respondiendo
con lo que ya hay en cache/. Un unico hilo por proceso procesa la cola
de a una tarea por vez (Understat castiga las rafagas) y cada cierto
tiempo hace un barrido que rellena archivos faltantes o viejos.

Una "tarea" es (liga, temporadas, fuente), lo mismo que la app le pide
a data_loader.cargar(). Los datos quedan escritos en cache/ con escritura
atomica, asi que la app solo nota el cambio via firma(): los mtimes de
esos archivos, que app.py usa como clave de st.cache_data.

Uso desde la app: ver obtener_actualizador() en app.py.

Uso por linea de comandos (cron, GitHub Actions):
    python actualizador.py            # descarga lo que falte o este viejo
    python actualizador.py --forzar   # vuelve a descargar todo

Variables de entorno:
    XG_ACTUALIZACION_AUTO=0   desactiva el barrido periodico (la cola manual sigue)
    XG_INTERVALO_HORAS=6      cada cuanto se revisa el cache
    XG_EDAD_MAX_EN_CURSO_HORAS=12
                              antiguedad maxima de datos con la temporada en curso
    XG_EDAD_MAX_HORAS=168     antiguedad maxima de temporadas ya terminadas
"""

import argparse
import os
import sys
import threading
import time
from datetime import datetime

from data_loader import LIGAS, CACHE_DIR
from logging_setup import get_logger

logger = get_logger("actualizador")

FUENTES_APP = ("csv", "understat")  # csv primero: la union con xG lo necesita
MAX_TEMPORADAS = 3                   # el slider de la app va de 1 a 3
INTERVALO_S = float(os.environ.get("XG_INTERVALO_HORAS", "6")) * 3600
EDAD_MAX_S = float(os.environ.get("XG_EDAD_MAX_HORAS", "168")) * 3600
EDAD_MAX_EN_CURSO_S = float(os.environ.get("XG_EDAD_MAX_EN_CURSO_HORAS", "12")) * 3600
AUTOMATICO = os.environ.get("XG_ACTUALIZACION_AUTO", "1") != "0"
# Primer barrido un rato despues del arranque, para no competir por CPU
# con el primer render de la app recien desplegada.
ESPERA_INICIAL_S = 60


def temporadas_recientes(n=3, hoy=None):
    """Las n ultimas temporadas, INCLUIDA la que esta en curso, formato '2526'.

    Para predecir el proximo partido importa la forma actual: los
    partidos de la temporada en curso son los mas recientes y el
    decaimiento temporal (xi) ya les da mas peso.

    La temporada nueva cuenta desde SEPTIEMBRE, no desde agosto: las
    ligas arrancan a mitad de agosto y football-data/Understat tardan en
    publicar los primeros partidos, asi que en agosto la "ultima" sigue
    siendo la que acaba de terminar (evita una temporada vacia o un 404).

    Distinto a backtest.temporadas_por_defecto(), que usa solo temporadas
    COMPLETAS: un backtest necesita resultados ya conocidos.
    """
    hoy = hoy or datetime.now()
    ultimo = hoy.year if hoy.month >= 9 else hoy.year - 1
    return ["{:02d}{:02d}".format((ultimo - i) % 100, (ultimo - i + 1) % 100)
            for i in range(n - 1, -1, -1)]


def en_curso(temporadas, hoy=None):
    """True si la combinacion incluye la temporada que se esta jugando
    (sus archivos cambian cada jornada y hay que refrescarlos seguido)."""
    hoy = hoy or datetime.now()
    if 6 <= hoy.month <= 8:
        return False  # junio-agosto: la ultima temporada ya termino
    return temporadas_recientes(1, hoy)[0] in temporadas


def etiqueta(tarea):
    liga, temporadas, fuente = tarea
    return "{} {} ({})".format(LIGAS.get(liga, (None, liga))[1], "-".join(temporadas),
                               "xG" if fuente == "understat" else "goles")


def rutas_cache(liga, temporadas, fuente):
    if fuente == "understat":
        from xg_loader import ruta_cache
        return [ruta_cache(liga, temporadas)]
    codigo, _ = LIGAS[liga]
    return [os.path.join(CACHE_DIR, "{}_{}.csv".format(codigo, t)) for t in temporadas]


def firma(liga, temporadas, fuente):
    """mtimes de los archivos de cache de la tarea (None = falta).

    Cambia cada vez que algo -- este hilo, el CLI, un redeploy con cache
    nuevo -- regenera los datos. La app la pasa como argumento a sus
    funciones @st.cache_data para que se invaliden solas.
    """
    marcas = []
    for ruta in rutas_cache(liga, temporadas, fuente):
        try:
            marcas.append(os.path.getmtime(ruta))
        except OSError:
            marcas.append(None)
    return tuple(marcas)


def tareas_app():
    """Todas las combinaciones que la app puede pedir.

    Para csv basta la de MAX_TEMPORADAS: es un archivo por temporada, asi
    que ya cubre las de 1 y 2. Para xG cada combinacion es su propio
    archivo (xg_<liga>_<temporadas>.csv).
    """
    completas = tuple(temporadas_recientes(MAX_TEMPORADAS))
    tareas = [(liga, completas, "csv") for liga in LIGAS]
    for liga in LIGAS:
        for n in range(1, MAX_TEMPORADAS + 1):
            tareas.append((liga, tuple(temporadas_recientes(n)), "understat"))
    return tareas


def ejecutar(tarea, forzar=True):
    """Descarga y deja en cache/ los datos de una tarea. Bloqueante."""
    from fuentes import obtener_fuente
    liga, temporadas, fuente = tarea
    obtener_fuente(fuente).partidos(liga, list(temporadas), forzar=forzar)


def vencidas(edad_max_s=EDAD_MAX_S, edad_max_en_curso_s=EDAD_MAX_EN_CURSO_S):
    """[(tarea, forzar)] con archivos faltantes (forzar=False: solo construir
    lo que falta) o demasiado viejos (forzar=True). Las combinaciones con
    la temporada en curso vencen mucho antes: cambian cada jornada."""
    ahora = time.time()
    salida = []
    for tarea in tareas_app():
        marcas = firma(*tarea)
        limite = edad_max_en_curso_s if en_curso(tarea[1]) else edad_max_s
        if None in marcas:
            salida.append((tarea, False))
        elif ahora - min(marcas) > limite:
            salida.append((tarea, True))
    return salida


class Actualizador:
    """Cola + un hilo trabajador. Seguro para llamar desde cualquier sesion."""

    def __init__(self, automatico=AUTOMATICO, intervalo_s=INTERVALO_S):
        self._automatico = automatico
        self._intervalo_s = intervalo_s
        self._cv = threading.Condition()
        self._cola = []
        self._actual = None
        self._ultima_ok = None      # (tarea, datetime)
        self._errores = {}          # tarea -> (mensaje, datetime)
        self._hilo = None

    def iniciar(self):
        if self._hilo is None or not self._hilo.is_alive():
            self._hilo = threading.Thread(target=self._bucle, name="xg-actualizador",
                                          daemon=True)
            self._hilo.start()
            logger.info("actualizador en segundo plano iniciado (barrido automatico: %s)",
                        "cada {:.0f}h".format(self._intervalo_s / 3600)
                        if self._automatico else "no")
        return self

    def solicitar(self, tarea, forzar=True):
        """Encola una tarea (sin duplicar) y vuelve enseguida."""
        with self._cv:
            if tarea != self._actual and all(t != tarea for t, _ in self._cola):
                self._cola.append((tarea, forzar))
                self._cv.notify()

    def ocupado_con(self, tarea):
        with self._cv:
            return tarea == self._actual or any(t == tarea for t, _ in self._cola)

    def estado(self):
        with self._cv:
            return {
                "actual": self._actual,
                "pendientes": len(self._cola),
                "ocupado": self._actual is not None or bool(self._cola),
                "ultima_ok": self._ultima_ok,
                "errores": dict(self._errores),
            }

    # -- hilo trabajador -----------------------------------------------

    def _bucle(self):
        proximo_barrido = time.time() + ESPERA_INICIAL_S
        while True:
            with self._cv:
                while not self._cola:
                    if self._automatico and time.time() >= proximo_barrido:
                        proximo_barrido = time.time() + self._intervalo_s
                        for tarea, forzar in vencidas():
                            if tarea != self._actual and all(t != tarea for t, _ in self._cola):
                                self._cola.append((tarea, forzar))
                        if self._cola:
                            logger.info("barrido: %s tareas de actualizacion", len(self._cola))
                        continue
                    espera = (proximo_barrido - time.time()) if self._automatico else None
                    self._cv.wait(timeout=espera)
                tarea, forzar = self._cola.pop(0)
                self._actual = tarea
            try:
                self._correr(tarea, forzar)
            except Exception:
                # Ultima red: si este hilo muere, la app deja de actualizarse
                # en silencio hasta el proximo reinicio del servidor.
                logger.exception("error inesperado en el actualizador")
                with self._cv:
                    self._actual = None

    def _correr(self, tarea, forzar):
        inicio = time.time()
        error = None
        try:
            ejecutar(tarea, forzar)
        # SystemExit incluido: data_loader/xg_loader lo usan como "error
        # con mensaje para el usuario", y sin atraparlo mataria el hilo.
        except (Exception, SystemExit) as e:
            error = str(e).strip() or type(e).__name__
        with self._cv:
            self._actual = None
            if error:
                self._errores[tarea] = (error, datetime.now())
            else:
                self._errores.pop(tarea, None)
                self._ultima_ok = (tarea, datetime.now())
        if error:
            logger.error("fallo al actualizar %s: %s", etiqueta(tarea), error[:300])
        else:
            logger.info("actualizado %s en %.0fs", etiqueta(tarea), time.time() - inicio)


def main():
    ap = argparse.ArgumentParser(description="Actualiza el cache de datos de la app")
    ap.add_argument("--forzar", action="store_true",
                    help="re-descarga todo aunque el cache este al dia")
    args = ap.parse_args()

    trabajo = ([(t, True) for t in tareas_app()] if args.forzar else vencidas())
    if not trabajo:
        print("  [ok] cache al dia, nada que actualizar")
        return

    fallos = 0
    for tarea, forzar in trabajo:
        print("  -> {}".format(etiqueta(tarea)), flush=True)
        try:
            ejecutar(tarea, forzar)
        except (Exception, SystemExit) as e:
            fallos += 1
            logger.error("fallo al actualizar %s: %s", etiqueta(tarea), str(e)[:300])
    print("  {} de {} tareas OK".format(len(trabajo) - fallos, len(trabajo)))
    sys.exit(1 if fallos else 0)


if __name__ == "__main__":
    main()
