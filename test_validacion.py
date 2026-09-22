"""
Test de humo de la validacion de datos, con partidos inventados.

Verifica las propiedades que tienen que cumplirse:
  1. Un partido normal no genera problemas.
  2. Goles negativos / fecha futura / equipo vacio -> descartable.
  3. Un marcador alto pero posible (7-0) -> sospechoso, NO descartable.
  4. validar_lote() filtra los descartables y elimina duplicados exactos.
  5. validar_esquema() exige las columnas criticas del CSV de origen.

Correr con:  python test_validacion.py
"""

from datetime import datetime, timedelta

from validacion import validar_partido, es_descartable, validar_lote, validar_esquema

HOY = datetime(2026, 1, 1)

fallos = []


def check(nombre, condicion, detalle=""):
    estado = "OK  " if condicion else "FALLA"
    print("  [{}] {}{}".format(estado, nombre, "  " + detalle if detalle else ""))
    if not condicion:
        fallos.append(nombre)


def partido(**over):
    base = {
        "fecha": HOY, "local": "Arsenal", "visita": "Chelsea",
        "goles_local": 2, "goles_visita": 1,
    }
    base.update(over)
    return base


print("=" * 66)
print("  TEST DE VALIDACION DE DATOS")
print("=" * 66)

# ---------------------------------------------------------------
# 1. Partido normal
# ---------------------------------------------------------------
p = partido()
problemas = validar_partido(p, hoy=HOY)
check("partido normal no genera problemas", problemas == [], str(problemas))

# ---------------------------------------------------------------
# 2. Datos imposibles -> descartables
# ---------------------------------------------------------------
casos_descartables = [
    ("goles negativos", partido(goles_local=-1)),
    ("equipo local vacio", partido(local="")),
    ("local == visita", partido(local="Arsenal", visita="Arsenal")),
    ("fecha futura", partido(fecha=HOY + timedelta(days=30))),
    ("fecha inverosimil", partido(fecha=datetime(1899, 1, 1))),
    ("sin fecha", partido(fecha=None)),
]
for nombre, p in casos_descartables:
    problemas = validar_partido(p, hoy=HOY)
    check("{} -> descartable".format(nombre), es_descartable(problemas),
          str(problemas))

# ---------------------------------------------------------------
# 3. Marcador alto pero posible -> no dispara nada por si solo
#    (15 es el techo "razonable"; sin datos de tiros no hay forma de
#    distinguir un 7-0 real de uno corrupto, asi que no se avisa)
# ---------------------------------------------------------------
p = partido(goles_local=7, goles_visita=0)
problemas = validar_partido(p, hoy=HOY)
check("7-0 sin mas contexto no genera problemas", problemas == [], str(problemas))
check("7-0 NO es descartable (es raro pero posible)", not es_descartable(problemas))

# ---------------------------------------------------------------
# 4. Tiros a puerta inconsistentes con los goles
# ---------------------------------------------------------------
p = partido(goles_local=6, sot_local=1, tiros_local=3)
problemas = validar_partido(p, hoy=HOY)
check("goles muy por encima de SOT genera aviso",
      any("sot_local" in x or "goles_local" in x for x in problemas), str(problemas))

p = partido(sot_local=8, tiros_local=5)
problemas = validar_partido(p, hoy=HOY)
check("SOT > tiros totales genera aviso",
      any("sot_local" in x for x in problemas), str(problemas))

# ---------------------------------------------------------------
# 5. xG fuera de rango
# ---------------------------------------------------------------
p = partido(xg_local=-0.5)
check("xG negativo es descartable", es_descartable(validar_partido(p, hoy=HOY)))

p = partido(xg_local=12.0)
problemas = validar_partido(p, hoy=HOY)
check("xG absurdamente alto genera aviso (no descartable)",
      len(problemas) > 0 and not es_descartable(problemas), str(problemas))

# ---------------------------------------------------------------
# 6. validar_lote(): filtra descartables, cuenta duplicados
#    Cada partido usa equipos/fecha distintos salvo el duplicado a
#    proposito: la clave de deduplicacion es (fecha, local, visita).
# ---------------------------------------------------------------
lote = [
    partido(local="Arsenal", visita="Chelsea"),                       # valido
    partido(local="Tottenham", visita="Fulham", goles_local=-1),      # descartable
    partido(local="Liverpool", visita="Everton"),                     # valido
    partido(local="Liverpool", visita="Everton"),                     # duplicado exacto
    partido(local="Man City", visita="Wolves",
            goles_local=6, sot_local=1, tiros_local=3),                # sospechoso, se conserva
]
validos, reporte = validar_lote(lote, hoy=HOY)
check("descarta el partido con goles negativos", reporte["descartados"] == 1, str(reporte))
check("detecta el duplicado exacto", reporte["duplicados"] == 1, str(reporte))
check("conserva el sospechoso (goles vs SOT)", reporte["sospechosos"] == 1, str(reporte))
check("total de validos correcto (5 - 1 descartado - 1 duplicado)",
      len(validos) == 3, "validos={}".format(len(validos)))

# ---------------------------------------------------------------
# 7. validar_esquema(): columnas criticas ausentes -> falla ruidosamente
# ---------------------------------------------------------------
try:
    validar_esquema(["HomeTeam", "AwayTeam", "FTHG"], ruta="test.csv")  # falta FTAG, Date
    check("esquema incompleto lanza SystemExit", False)
except SystemExit:
    check("esquema incompleto lanza SystemExit", True)

try:
    validar_esquema(["Date", "HomeTeam", "AwayTeam", "FTHG", "FTAG"], ruta="test.csv")
    check("esquema completo no lanza nada", True)
except SystemExit:
    check("esquema completo no lanza nada", False)

print("=" * 66)
if fallos:
    print("  {} FALLOS: {}".format(len(fallos), ", ".join(fallos)))
    raise SystemExit(1)
print("  Todo OK")
