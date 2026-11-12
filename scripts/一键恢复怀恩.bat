@echo off
REM ============================================================
REM  HuaiEn - one-click health check and restore
REM
REM  Fixes, in order:
REM    1. Hermes gateway not running      -> start it
REM    2. HuaiEn desktop window missing   -> start it
REM    3. Phone web UI missing            -> start it
REM    4. Memory out of sync              -> sync it
REM    5. Then it verifies all four again
REM
REM  Add --reset-code to also roll the project code back to
REM  the sealed tag (uses git checkout, does NOT discard your
REM  uncommitted work).
REM
REM  ASCII only on purpose: cmd reads .bat as cp936.
REM ============================================================
setlocal enabledelayedexpansion
title HuaiEn - Restore

set "HD=%LOCALAPPDATA%\AppData\Local\hermes"
REM 项目根 = 本 bat 所在目录的上一级; 想换地方先 set PROJ=...
if not defined PROJ set "PROJ=%~dp0.."
set "PATH=%HD%\bin;%PATH%"

if not exist "%PROJ%\scripts\restore.py" (
    echo [FAIL] restore.py not found under %PROJ%\scripts
    echo.
    pause
    exit /b 1
)

set "PY="
if exist "%HD%\hermes-agent\venv\Scripts\python.exe" set "PY=%HD%\hermes-agent\venv\Scripts\python.exe"
if not defined PY (
    echo [FAIL] python not found
    echo.
    pause
    exit /b 1
)

"%PY%" "%PROJ%\scripts\restore.py" --fix %*
echo.
echo [EXIT] done. Log: %PROJ%\logs\restore.log
pause
