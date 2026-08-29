@echo off
rem 启动设置服务（无窗口）并打开浏览器设置页
where pyw >nul 2>nul
if %errorlevel%==0 (
    start "" pyw -3 "%~dp0gui.py"
) else (
    start "" pythonw "%~dp0gui.py"
)
timeout /t 1 /nobreak >nul
start "" "http://127.0.0.1:8765/"
