@echo off
cd /d "%~dp0"
title Printer Status Service

echo ============================================
echo   BambuLab Printer Status Service
echo ============================================
echo.

REM ---- First run: interactive setup ----
if not exist "config.json" (
    echo [First Setup] Please enter the following:
    echo.
    set /p PHONE=Phone number: 
    set /p SERIAL=Printer serial: 
    set /p APIKEY=Agent API key: 
    set /p WEBPORT=Web port [18443]: 
    set /p AGENTPORT=Agent TCP port [19555]: 
    
    if "%WEBPORT%"=="" set WEBPORT=18443
    if "%AGENTPORT%"=="" set AGENTPORT=19555
    
    echo {"phone":"%PHONE%","serial":"%SERIAL%","api_keys":["%APIKEY%"],"web_port":%WEBPORT%,"agent_port":%AGENTPORT%,"refresh_interval":30,"mqtt_timeout":15,"mqtt_retries":2} > config.json
    echo.
    echo [OK] config.json saved.
    echo.
)

REM ---- Start service ----
echo [Starting] Server...
echo.
echo   Dashboard: https://localhost:18443
echo   Agent TCP: tcp://localhost:19555
echo.
echo   First time login:
echo     1. Open https://localhost:18443 in browser
echo     2. Click Send SMS, enter code to login
echo     3. Dashboard will show printer status
echo.
echo   Press Ctrl+C to stop
echo.

python server.py

if %ERRORLEVEL% NEQ 0 (
    echo.
    echo [ERROR] Server exited with code: %ERRORLEVEL%
    echo Make sure dependencies are installed: pip install aiohttp paho-mqtt requests cryptography
    pause
)
