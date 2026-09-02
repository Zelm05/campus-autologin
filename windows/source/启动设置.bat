@echo off
rem ============================================================
rem 启动设置服务（无窗口）并打开设置页
rem
rem v1.2 变更：端口改为「动态分配」，不再写死 8765。
rem   原因：固定端口会与用户本地部署的其他网站冲突，
rem         导致打开软件却显示成别的网站的内容。
rem   做法：启动后轮询等待 gui.url 出现，读取真实地址再打开，
rem         取代原来「固定等 1 秒」的写法（服务没就绪时会开到错误页）。
rem ============================================================

set "APPDIR=%~dp0"
set "URLFILE=%APPDIR%gui.url"
set "SETURL="
set "TRIES=0"

rem 启动无窗口的设置服务
where pyw >nul 2>nul
if %errorlevel%==0 (
    start "" pyw -3 "%APPDIR%gui.py"
) else (
    start "" pythonw "%APPDIR%gui.py"
)

rem 轮询等待服务就绪并写出地址文件（最多 25 秒）
:wait
if exist "%URLFILE%" set /p SETURL=<"%URLFILE%"
if defined SETURL goto opened
timeout /t 1 /nobreak >nul
set /a TRIES+=1
if %TRIES% GEQ 25 goto failed
goto wait

:opened
start "" "%SETURL%"
goto end

:failed
echo.
echo 设置服务启动超时，未能打开设置页。
echo 请确认 pythonw 可用，或直接运行：python "%APPDIR%gui.py"
echo.
pause

:end
