@echo off
REM ============================================================
REM  Activate the project-local conda env for yolo-label-tool
REM
REM  Usage:  init.bat
REM  Then :  python main.py
REM ============================================================

set "_PROJECT_ROOT=%~dp0"
if "%_PROJECT_ROOT:~-1%"=="\" set "_PROJECT_ROOT=%_PROJECT_ROOT:~0,-1%"
set "_ENV_PREFIX=%_PROJECT_ROOT%\.conda\envs\yolo-label-tool"

if not exist "%_ENV_PREFIX%\python.exe" (
    echo [init.bat] conda env not found: %_ENV_PREFIX%
    echo [init.bat] Create it first:
    echo     conda create -y -p "%_ENV_PREFIX%" python=3.11 pip
    echo     "%_ENV_PREFIX%\python.exe" -m pip install -r "%_PROJECT_ROOT%\requirements.txt"
    exit /b 1
)

REM Put the env dir first on PATH (no conda init required)
set "PATH=%_ENV_PREFIX%;%_ENV_PREFIX%\Scripts;%_ENV_PREFIX%\Library\bin;%PATH%"
set "CONDA_PREFIX=%_ENV_PREFIX%"
set "CONDA_DEFAULT_ENV=yolo-label-tool"

echo [init.bat] env activated: %_ENV_PREFIX%
"%_ENV_PREFIX%\python.exe" -V
exit /b 0
