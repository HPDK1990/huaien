@echo off
REM Restart the gateway so it picks up the new STT config and reconnects weixin.
REM Must run outside the gateway process tree (scheduled task), otherwise the
REM gateway kills the script before it can finish.
setlocal
set "HD=%LOCALAPPDATA%\AppData\Local\hermes"
set "PATH=%HD%\bin;%PATH%"
echo [%date% %time%] restarting gateway >> "%HD%\logs\gateway_restart.log"
hermes gateway stop --yes >> "%HD%\logs\gateway_restart.log" 2>&1
timeout /t 15 /nobreak >nul
hermes gateway start >> "%HD%\logs\gateway_restart.log" 2>&1
echo [%date% %time%] done >> "%HD%\logs\gateway_restart.log"
