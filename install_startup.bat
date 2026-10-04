@echo off
cd /d "%~dp0"
echo ============================================
echo   添加开机自启 - 拓竹打印机服务
echo ============================================
echo.
python install_startup.py
echo.
echo   下次开机将自动启动打印机状态服务
echo   如需移除，运行: install_startup.bat remove
echo.
pause
