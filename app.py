"""
Interfaz web local del predictor xG, con Streamlit.

No reimplementa nada del modelo: es una capa visual sobre data_loader.py /
poisson_model.py / config_modelo.py / elo.py, los mismos modulos que usan
los scripts de linea de comandos.

Uso:
    streamlit run app.py

O doble clic en iniciar_app.bat (Windows) -- ver ese archivo.
"""

import os

import pandas as pd
import streamlit as st

import actualizador
from actualizador import temporadas_recientes
from data_loader import LIGAS, RHO_LIGA, cargar
from poisson_model import (
    predecir, prob_a_cuota, K_SHRINK, K_SHRINK_XG, RHO,
    fuerza_ataque_local, fuerza_defensa_local,
    fuerza_ataque_visita, fuerza_defensa_visita,
)
from elo import elo_desde_partidos
import config_modelo

st.set_page_config(page_title="xG Predictor", page_icon="⚽", layout="wide")

NOMBRES_LIGA = {k: v[1] for k, v in LIGAS.items()}
UMBRAL_VALUE_BET = 0.02  # 2% de ventaja minima para marcar "value bet"


@st.cache_resource
def obtener_actualizador():
    """Un solo hilo de actualizacion por proceso, compartido por todas las
    sesiones (st.cache_resource lo crea una vez y lo reutiliza)."""
    return actualizador.Actualizador().iniciar()


# `firma` (mtimes de los archivos de cache, ver actualizador.firma) es parte
# de la clave del cache: cuando el hilo de fondo regenera los datos, la
# firma cambia y la proxima llamada recalcula sola. No lleva "_" delante
# a proposito: Streamlit excluye de la clave los argumentos con "_".
@st.cache_data(show_spinner="Calculando fuerzas de la liga...")
def cargar_datos(liga_key, temporadas, fuente, firma):
    return cargar(liga_key, list(temporadas), fuente=fuente)


@st.cache_data(show_spinner="Calculando ranking Elo...")
def cargar_elo(liga_key, temporadas, fuente, firma):
    from fuentes import obtener_fuente
    partidos = obtener_fuente(fuente).partidos(liga_key, list(temporadas))
    partidos.sort(key=lambda p: (p["fecha"], p["local"]))
    return elo_desde_partidos(partidos)


def estado_calibracion():
    """(estado, dias, metadata) para el bloque 'principal' de modelo_config.json.
    estado: 'sin_calibrar' | 'ok' | 'vencida'.
    """
    _, info = config_modelo.cargar_config("principal", {})
    if info["fuente"] == "default":
        return "sin_calibrar", None, None
    dias = info["antiguedad_dias"]
    estado = "ok" if dias <= config_modelo.ANTIGUEDAD_MAX_DIAS else "vencida"
    return estado, dias, info.get("metadata")


def parametros_prediccion(liga_key, fuente):
    """xi/k/w/rho a usar: calibrados si hay config guardada, si no los
    defaults de siempre (misma logica que data_loader.py --local/--visita).
    """
    con_xg = fuente == "understat"
    defaults = {
        "xi": 0.001, "k": (K_SHRINK_XG if con_xg else K_SHRINK),
        "w": (0.0 if con_xg else 1.0), "rho": RHO_LIGA.get(liga_key, RHO),
    }
    parametros, _ = config_modelo.cargar_config("principal", defaults)
    # rho no vive en el bloque "principal" (lo calibra --dixon-coles aparte)
    parametros["rho"] = defaults["rho"]
    return parametros


# ----------------------------------------------------------------------
# SIDEBAR: seleccion de datos + panel de diagnostico
# ----------------------------------------------------------------------
with st.sidebar:
    st.title("⚽ xG Predictor")
    st.caption("Modelo Poisson + xG + Dixon-Coles + disponibilidad")

    st.divider()
    st.subheader("Datos")
    liga_key = st.selectbox(
        "Competicion", options=list(LIGAS.keys()),
        format_func=lambda k: NOMBRES_LIGA[k])
    fuente = st.radio(
        "Fuente de datos", options=["understat", "csv"],
        format_func=lambda f: "xG real (Understat)" if f == "understat" else "Solo goles (football-data)",
        help="xG real es mas preciso (menos ruido) pero requiere que el "
             "partido este en el cache de Understat o poder descargarlo.")
    n_temporadas = st.select_slider("Temporadas a usar", options=[1, 2, 3], value=3)
    temporadas = tuple(temporadas_recientes(n_temporadas))
    st.caption("Temporadas: " + ", ".join(temporadas))

    st.divider()
    st.subheader("Estado del modelo")
    estado, dias, meta = estado_calibracion()
    if estado == "sin_calibrar":
        st.warning("Sin calibracion guardada.\nUsando los defaults del codigo.")
        st.caption("Corre `python backtest.py --guardar-config` para calibrar.")
    elif estado == "ok":
        st.success(f"Calibrado hace {dias} dia(s)")
        st.caption(f"limite antes de avisar: {config_modelo.ANTIGUEDAD_MAX_DIAS} dias")
        if meta:
            st.caption(f"RPS {meta.get('rps')} · {meta.get('n_partidos')} partidos · "
                      f"{', '.join(meta.get('ligas', []))}")
    else:
        st.error(f"Calibracion vencida: hace {dias} dias "
                f"(limite {config_modelo.ANTIGUEDAD_MAX_DIAS})")
        st.caption("Conviene relanzar backtest.py --guardar-config")

    st.divider()
    act = obtener_actualizador()
    tarea = (liga_key, temporadas, fuente)
    firma = actualizador.firma(*tarea)
    if None in firma and tarea not in act.estado()["errores"]:
        # Combinacion sin cache todavia (p.ej. arranco una temporada nueva):
        # se descarga en segundo plano en vez de congelar la pagina minutos.
        # Si ya fallo no se reencola sola (evita un bucle de reintentos);
        # el boton de abajo permite reintentar a mano.
        act.solicitar(tarea, forzar=False)
    if st.button("\U0001f504 Actualizar datos", width='stretch',
                disabled=act.ocupado_con(tarea),
                help="Vuelve a descargar en segundo plano los datos de la "
                     "competicion y fuente seleccionadas. La app sigue "
                     "usable mientras tanto y se refresca sola al terminar."):
        act.solicitar(tarea, forzar=True)
        st.toast("Actualizacion iniciada en segundo plano", icon="\U0001f504")

    def panel_actualizacion(firma_mostrada, ocupado_al_dibujar):
        est = act.estado()
        # Datos nuevos en disco, o el trabajo termino (bien o mal): rerun
        # completo para recargar y para apagar este sondeo.
        if (actualizador.firma(*tarea) != firma_mostrada
                or (ocupado_al_dibujar and not est["ocupado"])):
            st.rerun()
        if est["actual"]:
            st.caption("⏳ Actualizando " + actualizador.etiqueta(est["actual"])
                       + (f" · {est['pendientes']} en cola" if est["pendientes"] else ""))
        elif est["pendientes"]:
            st.caption(f"⏳ {est['pendientes']} actualizacion(es) en cola")
        elif est["ultima_ok"]:
            t, cuando = est["ultima_ok"]
            st.caption(f"Ultima actualizacion: {actualizador.etiqueta(t)}, "
                       f"{cuando:%d/%m %H:%M}")
        if tarea in est["errores"]:
            mensaje, cuando = est["errores"][tarea]
            st.warning(f"La ultima actualizacion de esta seleccion fallo "
                       f"({cuando:%d/%m %H:%M}); se muestran los datos anteriores.\n\n"
                       f"{mensaje[:300]}")

    # Solo sondea (cada 3s, sin recargar la pagina entera) mientras hay
    # trabajo en curso; en reposo el panel es estatico.
    ocupado = act.estado()["ocupado"]
    st.fragment(run_every=3 if ocupado else None)(panel_actualizacion)(firma, ocupado)


# ----------------------------------------------------------------------
# CARGA DE DATOS (comun a ambas pestanas)
# ----------------------------------------------------------------------
if None in firma:
    if tarea in act.estado()["errores"] and not act.ocupado_con(tarea):
        st.error(f"No se pudieron descargar los datos de {NOMBRES_LIGA[liga_key]}: "
                 f"{act.estado()['errores'][tarea][0]}")
    else:
        st.info(f"Descargando por primera vez los datos de {NOMBRES_LIGA[liga_key]} "
                f"({', '.join(temporadas)}) en segundo plano. La pagina se "
                f"actualiza sola cuando esten listos.")
    st.stop()

try:
    liga, equipos, crudos, n_partidos = cargar_datos(liga_key, temporadas, fuente, firma)
except SystemExit as e:
    st.error(f"No se pudieron cargar los datos de {NOMBRES_LIGA[liga_key]}: {e}")
    st.stop()

if len(equipos) < 2:
    st.error("Muy pocos equipos con historial suficiente todavia para esta "
             "seleccion. Prueba con mas temporadas.")
    st.stop()

parametros = parametros_prediccion(liga_key, fuente)

tab_prediccion, tab_diagnostico, tab_manual = st.tabs(
    ["\U0001f3af Predicciones", "\U0001f4ca Diagnostico", "\U0001f4d6 Manual"])


# ----------------------------------------------------------------------
# TAB 1: PREDICCIONES
# ----------------------------------------------------------------------
with tab_prediccion:
    st.subheader(f"{NOMBRES_LIGA[liga_key]} — {n_partidos} partidos cargados")

    nombres = sorted(equipos.keys())
    if st.session_state.get("sel_local") not in nombres:
        # Cambio de liga/fuente: la lista de equipos es otra y el valor
        # guardado (de la competicion anterior) ya no existe en ella.
        st.session_state.pop("sel_local", None)
    col_a, col_b = st.columns(2)
    with col_a:
        # Sin `index` explicito: dejamos que Streamlit gobierne el valor
        # via session_state (key="sel_local"). Pasar index=0 en cada rerun
        # pisaba la seleccion del usuario y el combo volvia siempre al
        # primero alfabetico.
        equipo_local = st.selectbox("\U0001f3e0 Equipo local", nombres, key="sel_local")
    with col_b:
        opciones_visita = [n for n in nombres if n != equipo_local]
        if st.session_state.get("sel_visita") not in opciones_visita:
            # O bien cambio de liga (otros equipos), o el que era visita
            # paso a ser el nuevo local: en ambos casos ya no es una opcion
            # valida. Liberamos la key para que el widget elija un default
            # valido en vez de lanzar una excepcion.
            st.session_state.pop("sel_visita", None)
        equipo_visita = st.selectbox("✈️ Equipo visitante", opciones_visita, key="sel_visita")

    with st.expander("Cuotas de la casa de apuestas (opcional, para detectar valor)"):
        st.caption("Si cargas las cuotas decimales que ofrece tu casa de apuestas, "
                  "se compara contra la probabilidad del modelo y se marca si hay ventaja.")
        oc1, oc2, oc3 = st.columns(3)
        cuota_local = oc1.number_input("Cuota 1 (local)", min_value=0.0, value=0.0, step=0.01, format="%.2f")
        cuota_empate = oc2.number_input("Cuota X (empate)", min_value=0.0, value=0.0, step=0.01, format="%.2f")
        cuota_visita = oc3.number_input("Cuota 2 (visita)", min_value=0.0, value=0.0, step=0.01, format="%.2f")

    predecir_click = st.button("\U0001f52e Predecir partido", type="primary", width='stretch')

    if predecir_click or "ultimo_resultado" in st.session_state:
        if predecir_click:
            local = equipos[equipo_local]
            visita = equipos[equipo_visita]
            resultado = predecir(local, visita, liga, k=parametros["k"], rho=parametros["rho"])
            st.session_state["ultimo_resultado"] = resultado
            st.session_state["ultimo_par"] = (equipo_local, equipo_visita)
        else:
            resultado = st.session_state["ultimo_resultado"]
            equipo_local, equipo_visita = st.session_state["ultimo_par"]

        mk = resultado["mercados"]
        st.divider()
        st.markdown(f"### {resultado['partido']}")
        st.caption(f"xi={parametros['xi']} · k={parametros['k']} · "
                  f"rho={parametros['rho']} · fuente={fuente}")

        st.markdown("##### Resultado 1X2")
        c1, c2, c3 = st.columns(3)
        for col, etiqueta, clave, emoji in (
            (c1, equipo_local, "local", "\U0001f3e0"),
            (c2, "Empate", "empate", "\U0001f91d"),
            (c3, equipo_visita, "visita", "✈️"),
        ):
            with col:
                with st.container(border=True):
                    st.metric(f"{emoji} {etiqueta}", f"{mk[clave]:.1%}")
                    st.caption(f"cuota justa: {prob_a_cuota(mk[clave])}")

        df_probs = pd.DataFrame({
            "Resultado": [equipo_local, "Empate", equipo_visita],
            "Probabilidad": [mk["local"], mk["empate"], mk["visita"]],
        }).set_index("Resultado")
        st.bar_chart(df_probs, height=200)

        st.markdown("##### Goles esperados (xG) y otros mercados")
        g1, g2, g3, g4 = st.columns(4)
        with g1:
            with st.container(border=True):
                st.metric(f"xG {equipo_local}", resultado["xg_local"])
        with g2:
            with st.container(border=True):
                st.metric(f"xG {equipo_visita}", resultado["xg_visita"])
        with g3:
            with st.container(border=True):
                st.metric("+2.5 goles", f"{mk['over_2_5']:.1%}")
        with g4:
            with st.container(border=True):
                st.metric("Ambos marcan", f"{mk['btts_si']:.1%}")

        cuotas_dadas = [
            ("local", equipo_local, cuota_local), ("empate", "Empate", cuota_empate),
            ("visita", equipo_visita, cuota_visita),
        ]
        if any(c > 0 for _, _, c in cuotas_dadas):
            st.markdown("##### Alerta de valor")
            for clave, etiqueta, cuota in cuotas_dadas:
                if cuota <= 0:
                    continue
                prob = mk[clave]
                edge = prob * cuota - 1
                if edge > UMBRAL_VALUE_BET:
                    st.success(f"**{etiqueta}**: ventaja de **{edge:+.1%}** "
                              f"(modelo {prob:.1%} vs implicita {1/cuota:.1%} de la cuota {cuota:.2f}) — posible value bet")
                elif edge < -UMBRAL_VALUE_BET:
                    st.warning(f"**{etiqueta}**: cuota {cuota:.2f} por debajo del valor del modelo "
                              f"({edge:+.1%})")
                else:
                    st.info(f"**{etiqueta}**: cuota {cuota:.2f} alineada con el modelo ({edge:+.1%})")
            st.caption("edge = probabilidad_modelo × cuota − 1. Positivo = el modelo cree que "
                      "la cuota paga mas de lo que deberia. Esto es analisis informativo, "
                      "no una recomendacion de apuesta.")

        with st.expander("Desglose de fuerzas (1.00 = promedio de la liga)"):
            local_obj, visita_obj = equipos[equipo_local], equipos[equipo_visita]
            df_fuerzas = pd.DataFrame({
                "Equipo": [equipo_local, equipo_visita],
                "Ataque casa": [fuerza_ataque_local(local_obj, liga, parametros["k"]),
                                fuerza_ataque_local(visita_obj, liga, parametros["k"])],
                "Defensa casa": [fuerza_defensa_local(local_obj, liga, parametros["k"]),
                                 fuerza_defensa_local(visita_obj, liga, parametros["k"])],
                "Ataque fuera": [fuerza_ataque_visita(local_obj, liga, parametros["k"]),
                                 fuerza_ataque_visita(visita_obj, liga, parametros["k"])],
                "Defensa fuera": [fuerza_defensa_visita(local_obj, liga, parametros["k"]),
                                  fuerza_defensa_visita(visita_obj, liga, parametros["k"])],
            }).set_index("Equipo").round(3)
            st.dataframe(df_fuerzas, width='stretch')
            st.caption("Ataque: mas alto = mejor. Defensa: mas bajo = mejor (encaja menos).")

        with st.expander("Marcadores mas probables"):
            df_marc = pd.DataFrame(resultado["top_marcadores"], columns=["Marcador", "Probabilidad"])
            df_marc["Probabilidad"] = df_marc["Probabilidad"].map(lambda p: f"{p:.2%}")
            st.dataframe(df_marc, hide_index=True, width='stretch')
    else:
        st.info("Elegi local, visita y toca **Predecir partido**.")


# ----------------------------------------------------------------------
# TAB 2: DIAGNOSTICO
# ----------------------------------------------------------------------
with tab_diagnostico:
    st.subheader("Estado de calibracion completo")
    st.code(config_modelo.resumen(), language=None)

    st.divider()
    st.subheader(f"Ranking Elo — {NOMBRES_LIGA[liga_key]}")
    st.caption("Fuerza general de cada equipo (ver elo.py). Complementa, no "
              "reemplaza, las fuerzas ataque/defensa del modelo Poisson.")
    try:
        elo = cargar_elo(liga_key, temporadas, fuente, firma)
        df_elo = pd.DataFrame(elo.tabla(), columns=["Equipo", "Elo"])
        df_elo.index = df_elo.index + 1
        df_elo["Elo"] = df_elo["Elo"].round(1)
        st.dataframe(df_elo, width='stretch')
    except SystemExit as e:
        st.error(f"No se pudo calcular Elo: {e}")

    st.divider()
    st.subheader("Tabla de fuerzas de la liga cargada")
    filas = []
    for e in sorted(equipos.values(),
                    key=lambda e: (e.gf_local + e.gf_visita) - (e.gc_local + e.gc_visita),
                    reverse=True):
        filas.append({
            "Equipo": e.nombre,
            "PJ (crudo)": sum(crudos[e.nombre]),
            "GF": round(e.gf_local + e.gf_visita, 1),
            "GC": round(e.gc_local + e.gc_visita, 1),
            "Ataque casa": round(fuerza_ataque_local(e, liga, parametros["k"]), 3),
            "Defensa casa": round(fuerza_defensa_local(e, liga, parametros["k"]), 3),
            "Ataque fuera": round(fuerza_ataque_visita(e, liga, parametros["k"]), 3),
            "Defensa fuera": round(fuerza_defensa_visita(e, liga, parametros["k"]), 3),
        })
    st.dataframe(pd.DataFrame(filas), hide_index=True, width='stretch')

    st.caption(f"Media de goles por partido — local: {liga.media_goles_local:.2f} · "
              f"visita: {liga.media_goles_visita:.2f}")


# ----------------------------------------------------------------------
# TAB 3: MANUAL DE USO
# ----------------------------------------------------------------------
with tab_manual:
    ruta_manual = os.path.join(os.path.dirname(os.path.abspath(__file__)), "MANUAL.md")
    if os.path.exists(ruta_manual):
        with open(ruta_manual, encoding="utf-8") as f:
            st.markdown(f.read())
    else:
        st.warning("No se encontro MANUAL.md junto a app.py.")
