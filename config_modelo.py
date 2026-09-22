"""
Versionado y metadata de los parametros calibrados del modelo.

El problema que resuelve: XI, K_SHRINK, RHO, etc. viven como constantes
hardcodeadas en poisson_model.py / data_loader.py / backtest.py, SIN
registro de cuando se calibraron ni con que datos. Si football-data
cambia de formato, o simplemente pasan varias temporadas, esos numeros
quedan desactualizados y nada lo hace evidente.

Este modulo:
  1. Persiste los parametros optimos en un JSON versionado junto con la
     metadata de la calibracion (fecha, ligas, temporadas, n de partidos,
     RPS logrado).
  2. Expone cargar_config(), que devuelve esos parametros y AVISA (log
     warning) si la calibracion tiene mas de ANTIGUEDAD_MAX_DIAS dias.
  3. backtest.py escribe aqui al terminar un barrido con --guardar-config.
     data_loader.py lee de aqui si el archivo existe; si no, cae a las
     constantes por defecto (comportamiento identico al actual).

Sin este archivo el sistema funciona exactamente igual que antes: es un
complemento opt-in, no un requisito.
"""

import json
import os
from datetime import datetime

from logging_setup import get_logger

RUTA_CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "modelo_config.json")

ANTIGUEDAD_MAX_DIAS = 90  # pasado esto, avisamos que toca recalibrar

_log = get_logger("config_modelo")


def _leer_crudo(ruta=RUTA_CONFIG):
    if not os.path.exists(ruta):
        return {}
    try:
        with open(ruta, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        _log.warning("no se pudo leer %s (%s); se ignora y se usan defaults", ruta, e)
        return {}


def guardar_config(bloque, parametros, metadata, ruta=RUTA_CONFIG):
    """Actualiza (o crea) el JSON de config con un bloque recien calibrado.

    bloque      -> "principal" | "disponibilidad" | "dixon_coles"
    parametros  -> dict de parametros optimos, ej {"xi": 0.002, "k": 2.0}
    metadata    -> dict libre: ligas, temporadas, n_partidos, rps, etc.
    """
    config = _leer_crudo(ruta)
    config[bloque] = {
        "parametros": parametros,
        "metadata": metadata,
        "calibrado_en": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    config["_version"] = config.get("_version", 0) + 1
    with open(ruta, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False, sort_keys=True)
    _log.info("config del modelo actualizada: bloque=%s version=%s -> %s",
              bloque, config["_version"], ruta)
    return config


def cargar_config(bloque, defaults, ruta=RUTA_CONFIG):
    """Devuelve los parametros calibrados para `bloque`, o `defaults` si no
    hay config guardada. Avisa si la calibracion esta vieja.

    defaults -> dict con los mismos nombres de clave que se guardarian.
    Devuelve (parametros_dict, info); info["fuente"] es "calibrado" o
    "default", y si es "calibrado" incluye "antiguedad_dias" y "metadata".
    """
    config = _leer_crudo(ruta)
    entrada = config.get(bloque)
    if not entrada:
        return dict(defaults), {"fuente": "default"}

    try:
        fecha = datetime.strptime(entrada["calibrado_en"], "%Y-%m-%d %H:%M:%S")
    except (KeyError, ValueError):
        return dict(defaults), {"fuente": "default"}

    antiguedad = (datetime.now() - fecha).days
    if antiguedad > ANTIGUEDAD_MAX_DIAS:
        _log.warning(
            "parametros de '%s' calibrados hace %s dias (> %s): considera "
            "relanzar backtest.py ... --guardar-config", bloque, antiguedad,
            ANTIGUEDAD_MAX_DIAS)

    parametros = dict(defaults)
    parametros.update(entrada.get("parametros", {}))
    return parametros, {"fuente": "calibrado", "antiguedad_dias": antiguedad,
                        "metadata": entrada.get("metadata", {})}


def resumen(ruta=RUTA_CONFIG):
    """Texto legible del estado de calibracion, para un CLI --info."""
    config = _leer_crudo(ruta)
    if not config:
        return ("  Sin config de calibracion guardada (modelo_config.json no existe).\n"
                "  Se estan usando los defaults hardcodeados del codigo.")

    lineas = ["  Estado de calibracion (modelo_config.json, version {}):".format(
        config.get("_version", "?"))]
    for bloque, entrada in sorted(config.items()):
        if bloque.startswith("_"):
            continue
        try:
            fecha = datetime.strptime(entrada["calibrado_en"], "%Y-%m-%d %H:%M:%S")
            antiguedad = (datetime.now() - fecha).days
        except (KeyError, ValueError):
            antiguedad = "?"
        aviso = ("  <-- RECALIBRAR"
                 if isinstance(antiguedad, int) and antiguedad > ANTIGUEDAD_MAX_DIAS
                 else "")
        lineas.append("    {:<16} calibrado {} (hace {} dias){}".format(
            bloque, entrada.get("calibrado_en", "?"), antiguedad, aviso))
        lineas.append("      parametros: {}".format(entrada.get("parametros")))
    return "\n".join(lineas)
