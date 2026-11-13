@echo off
REM Thin wrapper: the real restart logic lives in restart_gateway_safer.py.
REM This bat only exists so schtasks /TR has an ASCII path to call.
REM
REM Why a bat at all: schtasks /TR runs the command through cmd.exe, and a
REM .bat gives a stable handle to point a scheduled task at.
REM
REM The Python script does the whole job properly:
REM   stop -> WAIT and confirm it is really gone (RestartOnFailure PT1M
REM   brings it back within a minute) -> start -> scan for wedged
REM   sessions and discard them.
REM
REM NOTE: do NOT add a bulk kill by image name here. That kills every
REM Python process on the box -- including the agent hosting this gateway,
REM the cron jobs, and the HuaiEn frontend. The Python script kills by
REM explicit PID instead.
REM
REM ASCII only on purpose: cmd reads .bat as cp936.
setlocal
set "HD=%LOCALAPPDATA%\AppData\Local\hermes"
REM 项目根 = 本 bat 所在目录的上一级; 想换地方先 set PROJ=...
if not defined PROJ set "PROJ=%~dp0.."
set "PY=%HD%\hermes-agent\venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" "%PROJ%\scripts\restart_gateway_safer.py" %*
exit /b %ERRORLEVEL%
