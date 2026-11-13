@echo off
REM ============================================================
REM  HuaiEn - clean up dangling gateway_routing rows
REM
REM  Why this needs a separate script:
REM    The gateway refuses to stop itself from inside the gateway
REM    process ("cannot restart or stop the gateway from inside the
REM    gateway process"). Without a restart the gateway keeps the
REM    stale session_id in memory and writes it back, so the order
REM    must be: stop -> clean -> start.
REM
REM  This bat is launched by a scheduled task, so it runs outside
REM  the gateway process tree and is not blocked.
REM
REM  ASCII only on purpose: cmd reads .bat as cp936, and a UTF-8
REM  Chinese comment would come out as mojibake.
REM ============================================================
setlocal enabledelayedexpansion

set "HD=%LOCALAPPDATA%\AppData\Local\hermes"
REM 项目根 = 本 bat 所在目录的上一级; 想换地方先 set PROJ=...
if not defined PROJ set "PROJ=%~dp0.."
set "PY=%HD%\hermes-agent\venv\Scripts\python.exe"
set "PATH=%HD%\bin;%PATH%"
set "LOG=%PROJ%\logs\cleanup_routing.log"

echo [%date% %time%] start >> "%LOG%"

if not exist "%HD%\state.db" (
    echo [%date% %time%] FAIL: state.db not found >> "%LOG%"
    exit /b 1
)
if not exist "%PY%" (
    echo [%date% %time%] FAIL: python not found >> "%LOG%"
    exit /b 1
)

REM  GATE FIRST. Without --yes, the gateway refuses to stop when a turn is
REM  running -- good -- but this script used to continue straight into the
REM  DELETE anyway, writing to a LIVE state.db. On 2026-10-30 13:50 it
REM  printed "Refusing to stop the gateway: 3 live session(s) would be
REM  interrupted" and then deleted 74 rows anyway. Abort instead.
echo [%date% %time%] preflight gate >> "%LOG%"
"%PY%" "%PROJ%\scripts\preflight.py" >> "%LOG%" 2>&1
if errorlevel 1 (
    echo [%date% %time%] PREFLIGHT BLOCKED - aborting, nothing was stopped >> "%LOG%"
    echo [FAIL] preflight blocked: a live session is running. Aborted.
    exit /b 2
)

echo [%date% %time%] stopping gateway ... >> "%LOG%"
hermes gateway stop >> "%LOG%" 2>&1
timeout /t 8 /nobreak >nul

REM  The gateway task has RestartOnFailure Count=999 / Interval=PT1M, so it
REM  comes BACK within a minute. Wait until it really stays down before
REM  touching state.db. On 2026-10-30 19:23 it was auto-revived as PID 32292
REM  and optimize-storage then refused to run.
echo [%date% %time%] waiting for gateway to really stop ... >> "%LOG%"
"%PY%" "%PROJ%\scripts\wait_gateway_stopped.py" --timeout 90 >> "%LOG%" 2>&1
if errorlevel 1 (
    echo [%date% %time%] GATEWAY STILL RUNNING - aborting, nothing was cleaned >> "%LOG%"
    echo [FAIL] gateway came back (RestartOnFailure). Aborted.
    exit /b 4
)

echo [%date% %time%] cleaning ... >> "%LOG%"
"%PY%" "%PROJ%\scripts\cleanup_routing.py" >> "%LOG%" 2>&1
if errorlevel 1 (
    echo [%date% %time%] CLEANUP FAILED >> "%LOG%"
    echo [FAIL] cleanup_routing.py failed
)

echo [%date% %time%] starting gateway ... >> "%LOG%"
hermes gateway start >> "%LOG%" 2>&1
timeout /t 12 /nobreak >nul

hermes gateway status >> "%LOG%" 2>&1
echo [%date% %time%] done >> "%LOG%"
exit /b 0
