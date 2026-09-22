@echo off
setlocal
cd /d "%~dp0"

echo Verificando streamlit...
python -m pip show streamlit >nul 2>&1
if errorlevel 1 (
    echo Instalando streamlit, un momento...
    python -m pip install streamlit --quiet
)

rem Precarga la config de streamlit para que la pantalla de bienvenida
rem "Welcome to Streamlit / Email" no aparezca en cada arranque.
if not exist "%USERPROFILE%\.streamlit" mkdir "%USERPROFILE%\.streamlit" >nul 2>&1
if not exist "%USERPROFILE%\.streamlit\credentials.toml" (
    (
        echo [general]
        echo email = ""
    ) > "%USERPROFILE%\.streamlit\credentials.toml"
)
if not exist "%USERPROFILE%\.streamlit\config.toml" (
    (
        echo [browser]
        echo gatherUsageStats = false
    ) > "%USERPROFILE%\.streamlit\config.toml"
)

echo.
echo Abriendo xG Predictor en el navegador...
echo (para cerrar la app, cerra esta ventana)
echo.
python -m streamlit run "%~dp0app.py"

pause
