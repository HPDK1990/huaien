@echo off
setlocal enabledelayedexpansion
title HuaiEn Web (Phone)

set "HD=%LOCALAPPDATA%\AppData\Local\hermes"
REM 项目根 = 本 bat 所在目录的上一级; 想换地方先 set PROJ=...
if not defined PROJ set "PROJ=%~dp0.."
set "PATH=%HD%\bin;%PATH%"

echo ============================================
echo    HuaiEn Web - phone access
echo ============================================
echo.

if not exist "%PROJ%\huaien\web.py" (
    echo [FAIL] Project not found: %PROJ%\huaien\web.py
    echo.
    pause
    exit /b 1
)

set "PY="
if exist "%HD%\hermes-agent\venv\Scripts\python.exe" set "PY=%HD%\hermes-agent\venv\Scripts\python.exe"
if not defined PY (
    for /f "usebackq delims=" %%p in (`dir /b /a "%HD%\hermes-agent\venv\Scripts\python.exe" 2^>nul`) do if not defined PY set "PY=%%p"
)
if not defined PY (
    for /f "usebackq delims=" %%p in (`where python 2^>nul`) do if not defined PY set "PY=%%p"
)
if not defined PY (
    echo [FAIL] python not found
    echo.
    pause
    exit /b 1
)

cd /d "%PROJ%"
"%PY%" -m huaien.web %*
echo.
echo [EXIT] web stopped
pause
