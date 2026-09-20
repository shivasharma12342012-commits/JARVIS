@echo off
rem ============================================================================
rem  J.A.R.V.I.S. Desktop Launcher (no virtual environment - uses your system Python)
rem
rem  This file does the real work but is meant to be started *hidden*, by
rem  JARVIS.vbs (double-click JARVIS.vbs, or the desktop shortcut created by
rem  create_shortcut.vbs) rather than by double-clicking this .bat directly.
rem  Run this .bat by itself only if you want to see the console output.
rem
rem  Everything printed here also goes to jarvis_launcher.log next to this
rem  file, so a failure can still be diagnosed when the console is hidden.
rem ============================================================================
setlocal enabledelayedexpansion

set "HERE=%~dp0"
cd /d "%HERE%"
set "LOG=%HERE%jarvis_launcher.log"

echo ============================================ > "%LOG%"
echo   J.A.R.V.I.S. Launcher >> "%LOG%"
echo ============================================ >> "%LOG%"

rem --- 1. Find a working Python interpreter -----------------------------------
rem "python" is tried first: on some setups the "py" launcher points at a
rem Python install that has since been moved or deleted.
set "PY="
python --version >nul 2>&1
if !errorlevel! equ 0 set "PY=python"

if not defined PY (
    py -3 --version >nul 2>&1
    if !errorlevel! equ 0 set "PY=py -3"
)

if not defined PY (
    echo [error] No working Python interpreter found. >> "%LOG%"
    echo         Install Python 3.11+ from python.org and make sure it is on PATH, >> "%LOG%"
    echo         or repair your "py" launcher registration. >> "%LOG%"
    exit /b 1
)

echo [check] Using: !PY! >> "%LOG%"

rem --- 2. Make sure a .env file exists ----------------------------------------
if not exist "%HERE%.env" (
    if exist "%HERE%.env.example" (
        echo [setup] Creating .env from .env.example ... >> "%LOG%"
        copy /y "%HERE%.env.example" "%HERE%.env" >nul
    )
)

rem --- 3. Check the Ollama daemon, and try to start it if it's not running ----
echo [check] Looking for Ollama on 127.0.0.1:11434 ... >> "%LOG%"
%PY% -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:11434/api/version', timeout=2)" >nul 2>&1
if errorlevel 1 (
    echo [warn]  Ollama does not appear to be running. >> "%LOG%"
    if exist "%LOCALAPPDATA%\Programs\Ollama\ollama.exe" (
        echo [setup] Starting Ollama in the background ... >> "%LOG%"
        start "Ollama" /min "%LOCALAPPDATA%\Programs\Ollama\ollama.exe" serve
        timeout /t 3 /nobreak >nul
    ) else (
        echo [warn]  Could not find ollama.exe automatically. >> "%LOG%"
        echo         Start it yourself with: ollama serve >> "%LOG%"
    )
) else (
    echo [check] Ollama is reachable. >> "%LOG%"
)

rem --- 4. Launch J.A.R.V.I.S. Desktop (the windowed app, not the terminal HUD) -
echo [run] Starting J.A.R.V.I.S. Desktop ... >> "%LOG%"
%PY% "%HERE%main.py" desktop %* >> "%LOG%" 2>&1
set "EXITCODE=%ERRORLEVEL%"

echo J.A.R.V.I.S. exited with code %EXITCODE%. >> "%LOG%"

endlocal
exit /b %EXITCODE%
