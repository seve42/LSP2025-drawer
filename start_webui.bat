@echo off
chcp 65001 >nul
echo ====================================
echo    LSP2025 Drawer - WebUI
echo ====================================
echo.
echo 正在启动 WebUI (默认端口 8080)...
echo 如需使用其他端口，请手动运行: python main.py -port 端口号
echo.
python main.py -port 8080
pause
