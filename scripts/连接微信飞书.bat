@echo off
REM ============================================================
REM  HuaiEn - connect WeChat / Feishu to Hermes
REM
REM  What this does:
REM    Runs the Hermes gateway setup wizard so you can pick
REM    Weixin (personal WeChat, QR code scan) or Feishu.
REM
REM  Why a bat: hermes is not on the system PATH, so typing
REM  "hermes gateway setup" in cmd fails with 'not recognized'.
REM  This bat puts the right directory on PATH first.
REM
REM  ASCII only on purpose: cmd reads .bat as cp936.
REM ============================================================
setlocal enabledelayedexpansion
title HuaiEn - Connect WeChat / Feishu

set "HD=%LOCALAPPDATA%\AppData\Local\hermes"
set "PATH=%HD%\bin;%PATH%"

echo ============================================
echo    HuaiEn - connect WeChat / Feishu
echo ============================================
echo.
echo    In the menu pick:
echo      Weixin  = personal WeChat (scan a QR code with your phone)
echo      Feishu  = Feishu / Lark
echo.
echo    Notes:
echo      - Weixin works reliably for DM only, groups usually do not.
echo      - Feishu needs 2 extra permissions AND a new version
echo        published afterwards, otherwise it stays half-connected.
echo.

if not exist "%HD%\bin\hermes.exe" (
    echo [FAIL] hermes.exe not found under %HD%\bin
    echo.
    pause
    exit /b 1
)

hermes gateway setup
echo.
echo [EXIT] setup closed. Check with: hermes gateway status
pause
