"""
CSS de app.py. Solo presentacion: no calcula nada.

Complementa el tema de .streamlit/config.toml ("Pizarra de analista":
papel crema, tinta y un unico acento cuero). Es la misma base visual que
el NFL Predictor, para que ambas apps se vean como un solo sistema.
"""

import streamlit as st

CSS = """
<style>
:root {
  --xp-papel: #FAF9F6;
  --xp-superficie: #FFFFFF;
  --xp-borde: #E8E4DC;
  --xp-borde-suave: #EFEBE4;
  --xp-tinta: #1D1F22;
  --xp-tinta-2: #6E6A63;
  --xp-cuero: #8C4A2A;
  --xp-sombra: 0 1px 2px rgba(29,31,34,.04), 0 4px 14px rgba(29,31,34,.05);
}

/* --- Superficies del navegador -------------------------------------- */
::selection { background: #EBD8CC; color: var(--xp-tinta); }
* { scrollbar-color: #D6D0C5 transparent; scrollbar-width: thin; }
.stApp { caret-color: var(--xp-cuero); }

/* --- Lienzo ---------------------------------------------------------- */
.block-container { padding-top: 2.25rem; padding-bottom: 4rem; max-width: 1240px; }
h1, h2, h3, h4 { letter-spacing: -0.015em; }
[data-testid="stCaptionContainer"] { color: var(--xp-tinta-2); }
hr { border-color: var(--xp-borde) !important; }

/* Numeros alineados en metricas y tablas */
[data-testid="stMetricValue"], [data-testid="stDataFrame"] { font-variant-numeric: tabular-nums; }

/* --- Tabs ------------------------------------------------------------ */
.stTabs [data-baseweb="tab-list"] { gap: 1.5rem; border-bottom: 1px solid var(--xp-borde); }
.stTabs [data-baseweb="tab"] { padding-left: 0; padding-right: 0; font-weight: 600; }

/* --- Tarjetas, expander y dataframes --------------------------------- */
[data-testid="stExpander"] details { border-radius: 12px; border-color: var(--xp-borde); background: var(--xp-superficie); }
[data-testid="stDataFrame"] { border-radius: 10px; }
.stButton button { font-weight: 600; }

/* --- Celular --------------------------------------------------------- */
@media (max-width: 760px) {
  .block-container { padding-top: 3.25rem; padding-left: 1rem; padding-right: 1rem; }
}
</style>
"""


def aplicar_css():
    st.markdown(CSS, unsafe_allow_html=True)
