@echo off
rem ============================================================
rem 校园网自动登录 Windows 安装包构建脚本  (v1.2.0)
rem ------------------------------------------------------------
rem 用法：在文件资源管理器里双击本脚本，或在 build 目录执行 build.bat
rem 前提：
rem   1) 已安装 Python 且能找到 pyinstaller 命令（或自行修改下方 PYI）
rem   2) 已安装 Inno Setup，ISCC.exe 位于 D:\inno\ISCC.exe
rem      （若路径不同，修改下方 ISCC 变量即可）
rem
rem 产物：
rem   build\..\output\Setup_校园网自动登录_v1.2.0.exe
rem   ..\release\Autologin_v1.2.0_x64_setup.exe
rem ============================================================
setlocal

rem ---- 路径（基于本脚本所在目录，可随意移动） ----
set "BUILDIR=%~dp0"
set "SRCIR=%BUILDIR%.."
set "RELDIR=%SRCIR%\..\release"
set "APPVER=1.2.0"

rem ---- 1) 定位 PyInstaller ----
where pyinstaller >nul 2>nul
if %errorlevel%==0 (
  set "PYI=pyinstaller"
) else if exist "%SRCIR%\..\tools\venv\Scripts\pyinstaller.exe" (
  set "PYI=%SRCIR%\..\tools\venv\Scripts\pyinstaller.exe"
) else (
  echo [错误] 找不到 pyinstaller，请先安装，或在上方 PYI 变量指定其完整路径。
  pause & exit /b 1
)

rem ---- 2) 清理旧的构建产物，避免缓存干扰 ----
cd /d "%BUILDIR%"
if exist work rmdir /s /q work
if exist dist rmdir /s /q dist
if exist stage rmdir /s /q stage

rem ---- 3) PyInstaller 打包（onedir） ----
rem 必须在 build 目录执行，否则 spec 里的 ../app.py 等相对路径会解析错误
%PYI% --workpath work --distpath dist CampusLogin.spec
if errorlevel 1 (
  echo [错误] PyInstaller 打包失败，请检查上方输出。
  pause & exit /b 1
)

rem ---- 4) 暂存到 stage（Inno 从这里取文件） ----
mkdir stage\app
xcopy /e /i /y dist\CampusLogin\* stage\app\
copy /y "..\使用说明.txt" stage\

rem ---- 5) Inno Setup 生成安装包 ----
set "ISCC="
if exist "D:\inno\ISCC.exe" set "ISCC=D:\inno\ISCC.exe"
if not defined ISCC ( where ISCC >nul 2>nul && set "ISCC=ISCC" )
if not defined ISCC (
  echo [错误] 找不到 ISCC.exe，请安装 Inno Setup（默认在 D:\inno\ISCC.exe），
  echo        或在本脚本顶部 ISCC 变量指定其完整路径。
  pause & exit /b 1
)
"%ISCC%" setup.iss
if errorlevel 1 (
  echo [错误] Inno Setup 打包失败，请检查上方输出。
  pause & exit /b 1
)

rem ---- 6) 复制到 release 目录（统一命名） ----
if not exist "%RELDIR%" mkdir "%RELDIR%"
copy /y "..\output\Setup_校园网自动登录_v%APPVER%.exe" "%RELDIR%\Autologin_v%APPVER%_x64_setup.exe"

echo.
echo [完成] 安装包已生成：
echo   %BUILDIR%..\output\Setup_校园网自动登录_v%APPVER%.exe
echo   %RELDIR%\Autologin_v%APPVER%_x64_setup.exe
echo.
echo 之后请手动提交并推送（按你的习惯）：
echo   git add -A ^&^& git commit -m "release: Windows v%APPVER%" ^&^& git push
pause
