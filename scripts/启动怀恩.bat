@echo off
setlocal enabledelayedexpansion
title HuaiEn

set "HD=%LOCALAPPDATA%\AppData\Local\hermes"
REM 项目根 = 本 bat 所在目录的上一级; 想换地方先 set PROJ=...
if not defined PROJ set "PROJ=%~dp0.."
set "PATH=%HD%\bin;%PATH%"

echo ============================================
echo    HuaiEn - start
echo ============================================
echo.

if not exist "%PROJ%\huaien\app.py" (
    echo [FAIL] Project not found: %PROJ%\huaien\app.py
    echo.
    pause
    exit /b 1
)

set "PY="
if exist "%HD%\hermes-agent\venv\Scripts\python.exe" set "PY=%HD%\hermes-agent\venv\Scripts\python.exe"
if not defined PY (
    for /f "usebackq delims=" %%p in (`dir /b /ad "%HD%\tools\python-3*" 2^>nul`) do (
        if exist "%%~fp\python.exe" (
            if not defined PY set "PY=%%~fp\python.exe"
        )
    )
)
if not defined PY (
    for /f "usebackq delims=" %%p in (`where python 2^>nul`) do (
        if not defined PY set "PY=%%p"
    )
)
if not defined PY (
    echo [FAIL] No Python interpreter found.
    echo.
    pause
    exit /b 1
)
echo [ OK ] Python: %PY%

"%PY%" -c "import tkinter" >nul 2>&1
if errorlevel 1 (
    echo [FAIL] This Python has no tkinter.
    echo.
    pause
    exit /b 1
)
echo [ OK ] tkinter available.

where hermes >nul 2>&1
if errorlevel 1 (
    echo [FAIL] hermes command not on PATH.
    echo.
    pause
    exit /b 1
)
echo [ OK ] hermes found.

echo [ .. ] Starting HuaiEn...
echo.
cd /d "%PROJ%"
"%PY%" -m huaien.app
if errorlevel 1 (
    echo.
    echo [FAIL] HuaiEn exited with an error. See the message above.
    pause
    exit /b 1
)
echo [ OK ] HuaiEn closed normally.
