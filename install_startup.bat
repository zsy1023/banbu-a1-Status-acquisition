@echo off
cd /d "%~dp0"
echo ============================================
echo   添加开机自启 - BambuLab Printer Service
echo ============================================
echo.
python install_startup.py
echo.
echo   Next boot will auto-start printer status service
echo   To remove: install_startup.bat remove
echo.
pause