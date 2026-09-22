"""
Test de humo del filtro de confederaciones para partidos de selecciones.

Verifica la regla de negocio exacta: solo pasan UEFA-UEFA, CONMEBOL-
CONMEBOL, o el cruce UEFA-CONMEBOL (en cualquier orden). Todo lo demas
(Concacaf, CAF, AFC, OFC, o una desconocida) queda excluido, incluso si
el rival es UEFA o CONMEBOL.

Correr con:  python test_confederaciones.py
"""

from confederaciones import confederacion, es_partido_valido, filtrar_partidos_fifa

fallos = []


def check(nombre, condicion, detalle=""):
    estado = "OK  " if condicion else "FALLA"
    print("  [{}] {}{}".format(estado, nombre, "  " + detalle if detalle else ""))
    if not condicion:
        fallos.append(nombre)


print("=" * 66)
print("  TEST DE FILTRO DE CONFEDERACIONES")
print("=" * 66)

# ---------------------------------------------------------------
# 1. Clasificacion individual
# ---------------------------------------------------------------
check("Francia es UEFA", confederacion("France") == "UEFA")
check("Argentina es CONMEBOL", confederacion("Argentina") == "CONMEBOL")
check("Mexico (Concacaf) es OTRA", confederacion("Mexico") == "OTRA")
check("Nigeria (CAF) es OTRA", confederacion("Nigeria") == "OTRA")
check("Japon (AFC) es OTRA", confederacion("Japan") == "OTRA")
check("Nueva Zelanda (OFC) es OTRA", confederacion("New Zealand") == "OTRA")
check("las 55 selecciones UEFA estan cargadas",
      len(__import__("confederaciones").UEFA) == 55)
check("las 10 selecciones CONMEBOL estan cargadas",
      len(__import__("confederaciones").CONMEBOL) == 10)

# ---------------------------------------------------------------
# 2. Combinaciones permitidas
# ---------------------------------------------------------------
permitidos = [
    ("France", "Spain"), ("Argentina", "Brazil"),
    ("France", "Argentina"), ("Argentina", "France"),
    ("Germany", "Uruguay"), ("Chile", "Portugal"),
]
for local, visita in permitidos:
    check("{} vs {} -> permitido".format(local, visita),
          es_partido_valido(local, visita))

# ---------------------------------------------------------------
# 3. Combinaciones excluidas
# ---------------------------------------------------------------
excluidos = [
    ("France", "Mexico"),      # UEFA vs Concacaf
    ("Argentina", "USA"),      # CONMEBOL vs Concacaf
    ("France", "Japan"),       # UEFA vs AFC
    ("Argentina", "Nigeria"),  # CONMEBOL vs CAF
    ("Nigeria", "Egypt"),      # CAF-CAF
    ("Mexico", "USA"),         # Concacaf-Concacaf
    ("Japan", "Australia"),    # AFC vs OFC/AFC
]
for local, visita in excluidos:
    check("{} vs {} -> excluido".format(local, visita),
          not es_partido_valido(local, visita))

# ---------------------------------------------------------------
# 4. filtrar_partidos_fifa() sobre un lote mixto
# ---------------------------------------------------------------
lote = [
    {"local": "France", "visita": "Germany"},     # UEFA-UEFA: pasa
    {"local": "Brazil", "visita": "Peru"},         # CONMEBOL-CONMEBOL: pasa
    {"local": "Spain", "visita": "Colombia"},      # UEFA-CONMEBOL: pasa
    {"local": "Mexico", "visita": "USA"},          # Concacaf-Concacaf: fuera
    {"local": "France", "visita": "Senegal"},      # UEFA vs CAF: fuera
]
validos, descartados = filtrar_partidos_fifa(lote)
check("filtra el lote mixto correctamente (3 validos, 2 descartados)",
      len(validos) == 3 and descartados == 2,
      "validos={} descartados={}".format(len(validos), descartados))

print("=" * 66)
if fallos:
    print("  {} FALLOS: {}".format(len(fallos), ", ".join(fallos)))
    raise SystemExit(1)
print("  Todo OK")
