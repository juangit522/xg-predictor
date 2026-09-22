"""
Logging estructurado para todo el proyecto.

Reemplaza los print() de diagnostico/error dispersos por un logger real:
niveles, timestamps y un archivo persistente ademas de consola, sin tocar
cada punto de llamada.

Los print() que forman parte de la SALIDA del programa (tablas, reportes
de backtest, resultado de una prediccion) se mantienen como print(): esa
es la interfaz del usuario, no un log. Lo que migra a logger son avisos,
errores de red, descartes de datos y diagnosticos.

Uso:
    from logging_setup import get_logger
    log = get_logger(__name__)
    log.warning("partido descartado: %s", motivo)
"""

import logging
import os
import sys

_CONFIGURADOS = set()

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")


class _Formato(logging.Formatter):
    def __init__(self):
        super().__init__(
            fmt="%(asctime)s  %(levelname)-7s  %(name)-16s  %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )


def get_logger(nombre, nivel=logging.INFO, a_archivo=True):
    """Logger listo para usar, con handler de consola (stderr) y de archivo.

    Idempotente: llamarlo varias veces con el mismo nombre no duplica
    handlers (sin esta guarda, cada import volveria a agregar handlers
    porque logging.getLogger cachea el objeto pero no evita addHandler
    repetidos).
    """
    logger = logging.getLogger(nombre)
    if nombre in _CONFIGURADOS:
        return logger

    logger.setLevel(nivel)
    logger.propagate = False

    consola = logging.StreamHandler(sys.stderr)
    consola.setFormatter(_Formato())
    logger.addHandler(consola)

    if a_archivo:
        try:
            os.makedirs(LOG_DIR, exist_ok=True)
            archivo = logging.FileHandler(
                os.path.join(LOG_DIR, "xg_predictor.log"), encoding="utf-8")
            archivo.setFormatter(_Formato())
            logger.addHandler(archivo)
        except OSError:
            # Sin permisos de escritura (ej. FS de solo lectura): seguimos
            # solo con consola en vez de romper el programa por esto.
            pass

    _CONFIGURADOS.add(nombre)
    return logger
