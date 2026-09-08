@echo off
rem J.A.R.V.I.S. Desktop -- the windowed front end.
rem
rem   jarvis-desktop                  open the window
rem   jarvis-desktop --theme violet   open it in a colour
rem   jarvis-desktop --no-window      serve it, print the address, open it yourself
rem
rem Put this folder on your PATH and the command is simply `jarvis-desktop`.
setlocal
set "HERE=%~dp0"

rem Prefer a virtualenv sitting next to the source, then the py launcher.
if exist "%HERE%.venv\Scripts\python.exe" (
    set "PY=%HERE%.venv\Scripts\python.exe"
) else if exist "%HERE%venv\Scripts\python.exe" (
    set "PY=%HERE%venv\Scripts\python.exe"
) else (
    where py >nul 2>&1 && (set "PY=py -3") || (set "PY=python")
)

%PY% "%HERE%main.py" desktop %*
endlocal
