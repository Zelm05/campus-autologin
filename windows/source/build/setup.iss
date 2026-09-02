; ============================================================
; 校园网自动登录 v1.2.0 安装脚本
; 默认安装到 D:\校园网自动登录，用户可在安装向导自由更改
; 编译：D:\inno\ISCC.exe build\setup.iss
;
; 行为说明：
;  - AppId 固定 → 运行新安装包会“覆盖升级”已装版本（自动卸载旧版再装新版）
;  - 安装/卸载前自动停止后台守护（daemon.pid），避免文件被占用
;  - 清理“手动删过目录但注册表残留”导致的旧卸载记录，让覆盖安装顺利进行
;  - 不创建“选择附加任务”页与“开始菜单文件夹”页，默认仅创建桌面快捷方式
; ============================================================

#define MyAppName "校园网自动登录"
#define MyAppNameEn "CampusAutoLogin"
#define MyAppVersion "1.2.0"
#define MyAppPublisher "Zelm"
#define MyAppCopyright "Copyright (C) 2026 Zelm"

[Setup]
AppId={{2A8F6E1B-3C4D-4E5F-9A1B-7C8D9E0F1234}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppCopyright={#MyAppCopyright}
DefaultDirName=D:\校园网自动登录
; 不再创建开始菜单程序组，也不显示“开始菜单文件夹”选择页
DisableProgramGroupPage=yes
AllowNoIcons=yes
OutputDir=..\output
OutputBaseFilename=Setup_校园网自动登录_v{#MyAppVersion}
SetupIconFile=icon.ico
Compression=lzma2/ultra64
SolidCompression=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
UninstallDisplayIcon={app}\CampusLogin.exe
; 不启用 Inno 自带的 CloseApplications（过滤 *.exe 太宽泛，会误报无法关闭）。
; 改为在 [Code] 里手动停止本程序自身的进程，覆盖/卸载前确保文件不被占用。
RestartApplications=no
WizardStyle=modern

[Languages]
Name: "simplifiedchinese"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Files]
; onedir 部署：整个程序目录（CampusLogin.exe + _internal 依赖）一起打包
Source: "stage\app\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "stage\使用说明.txt"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
; 默认创建桌面快捷方式，不关联任何 Task，因此不出现“选择附加任务”页
Name: "{userdesktop}\{#MyAppName}"; Filename: "{app}\CampusLogin.exe"

; [Tasks] 段已移除：不再提供复选框，桌面快捷方式默认创建
; [Run] 段已移除：不再提供“安装完成后启动”选项

[Code]
// 在选择目录页：若默认盘符不存在则提示用户改选
function NextButtonClick(CurPageID: Integer): Boolean;
begin
  if CurPageID = wpSelectDir then
  begin
    if not DirExists(ExtractFileDrive(ExpandConstant('{app}'))) then
    begin
      MsgBox('提示：默认安装盘符（' + ExtractFileDrive(ExpandConstant('{app}')) +
             '）在您的电脑上不可用。' + #13#10 +
             '请在下一步选择其他盘符（推荐 D:\校园网自动登录 或 C:\校园网自动登录）。',
             mbInformation, MB_OK);
    end;
  end;
  Result := True;
end;

// 停止后台守护服务（卸载/升级前调用）
// 说明：daemon 与 GUI 是同一个 exe（校园网自动登录.exe），按镜像名 taskkill 即可
// 可靠地停掉后台守护；版本无关，不依赖 exe 内部逻辑。
// 注：若此时设置界面(GUI)正好开着也会被一并结束——卸载/升级场景下这是可接受的。
//
// 重要：taskkill 找不到进程时返回非零退出码。我们据此“早退”——
// 没有进程时就立刻返回，绝不做无谓的长时间 Sleep，避免安装界面被卡住、点×无响应。
procedure StopBackgroundService();
var
  TaskKill: string;
  RC1, RC2: Integer;
  i: Integer;
begin
  TaskKill := ExpandConstant('{sys}\taskkill.exe');
  for i := 0 to 5 do
  begin
    Exec(TaskKill, '/F /IM "校园网自动登录.exe" /T', '', SW_HIDE, ewWaitUntilTerminated, RC1);
    // 兼容旧版 exe 文件名 CampusLogin.exe
    Exec(TaskKill, '/F /IM "CampusLogin.exe" /T', '', SW_HIDE, ewWaitUntilTerminated, RC2);
    // 两个名字都找不到进程（taskkill 返回非 0）= 当前没有后台在跑，立即退出，不空等
    if (RC1 <> 0) and (RC2 <> 0) then
      Break;
    Sleep(500);
  end;
end;

// 清理「手动删除安装目录但注册表残留」导致的旧卸载记录，
// 否则覆盖安装时会误报“已安装”并要求先手动卸载。
procedure CleanStaleInstall();
var
  Key, S: string;
begin
  Key := 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{2A8F6E1B-3C4D-4E5F-9A1B-7C8D9E0F1234}_is1';
  if RegQueryStringValue(HKCU, Key, 'UninstallString', S) then
  begin
    if (S <> '') and (not FileExists(S)) then
      RegDeleteKeyIncludingSubkeys(HKCU, Key);
  end;
  if RegQueryStringValue(HKLM, Key, 'UninstallString', S) then
  begin
    if (S <> '') and (not FileExists(S)) then
      RegDeleteKeyIncludingSubkeys(HKLM, Key);
  end;
end;

// 卸载时一并清理开机自启项（指向已删除的 exe 会造成开机报错）
// 自启历史：早期为注册表 Run 键 → 后为计划任务（登录级 CampusAutoLogin /
// 开机级 CampusAutoLoginBoot）→ 现登录后自启用「启动文件夹」.cmd，极速启动用计划任务。
procedure RemoveAutostart();
var
  RC: Integer;
  StartupCmd: string;
begin
  // 旧版（<= v1.1.0）遗留的注册表项，兼容清理
  RegDeleteValue(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Run', 'CampusAutoLogin');
  // 旧版登录级计划任务 CampusAutoLogin（早期实现登录后自启）—— 兼容清理
  Exec(ExpandConstant('{sys}\schtasks.exe'), '/delete /tn "CampusAutoLogin" /f', '',
       SW_HIDE, ewWaitUntilTerminated, RC);
  // 开机级任务：以 SYSTEM 身份创建，删除需要管理员。
  // 卸载本身不要求管理员（PrivilegesRequired=lowest），此处仅尽力而为，失败不阻塞卸载。
  Exec(ExpandConstant('{sys}\schtasks.exe'), '/delete /tn "CampusAutoLoginBoot" /f', '',
       SW_HIDE, ewWaitUntilTerminated, RC);
  // 登录后自启的 .cmd 启动器（位于用户 Startup 文件夹）
  StartupCmd := ExpandConstant('{userappdata}\Microsoft\Windows\Start Menu\Programs\Startup\校园网自动登录.cmd');
  DeleteFile(StartupCmd);
end;

// 卸载时清理程序运行时生成的文件与日志。
// 这些文件由 exe 在 {app} 目录下创建，不在 [Files] 段里，Inno 默认不会删除，
// 不清除会导致卸载后安装目录残留 config.json、日志等。
//
// 注意：覆盖升级走的是“静默卸载”，此时应保留 config.json 和日志，避免用户重新填账号。
// 手动（交互式）卸载才彻底清除所有数据。
procedure CleanRuntimeFiles();
var
  AppDir: string;
  Silent: Boolean;
begin
  AppDir := ExpandConstant('{app}');
  Silent := UninstallSilent();

  // 临时/端口状态类文件：无论静默/交互都清除，升级后会重建
  DeleteFile(AppDir + '\daemon.pid');
  DeleteFile(AppDir + '\status.json');
  DeleteFile(AppDir + '\gui.port');
  DeleteFile(AppDir + '\gui.url');

  // 配置文件与日志：仅在用户手动卸载时彻底清除；覆盖升级时保留
  if not Silent then
  begin
    DeleteFile(AppDir + '\config.json');
    DeleteFile(AppDir + '\boot_task.json');  // 极速启动开关状态（卸载时任务也会被一并尝试删除）
    if DirExists(AppDir + '\logs') then
      DelTree(AppDir + '\logs', True, True, True);
  end;
end;

// 卸载时清理 C 盘产生的缓存：
// 1) WebView2 用户数据目录（程序启动时已指定到 %LOCALAPPDATA%\校园网自动登录_WebView2）
// 2) PyInstaller onefile 运行时在 %TEMP% 下残留的 _MEIxxxxxx 临时目录（旧版遗留）
//    只删除其中包含 "校园网自动登录.exe" 的目录，避免误删其他程序的 _MEI 缓存。
//    注意：v1.1.0 起已改为 onedir 部署，不再生成新的 _MEI；此处仅用于清理旧版残留。
procedure CleanCaches();
var
  LocalAppData, TempDir, WebView2Dir, FindPath: string;
  FindRec: TFindRec;
  MeiDir, ExeInMei: string;
begin
  LocalAppData := ExpandConstant('{localappdata}');
  WebView2Dir := LocalAppData + '\校园网自动登录_WebView2';
  if DirExists(WebView2Dir) then
    DelTree(WebView2Dir, True, True, True);

  TempDir := ExpandConstant('{tmp}');
  FindPath := TempDir + '\_MEI*';
  if FindFirst(FindPath, FindRec) then
  begin
    repeat
      if (FindRec.Attributes and FILE_ATTRIBUTE_DIRECTORY) <> 0 then
      begin
        MeiDir := TempDir + '\' + FindRec.Name;
        ExeInMei := MeiDir + '\校园网自动登录.exe';
        if FileExists(ExeInMei) then
          DelTree(MeiDir, True, True, True);
      end;
    until not FindNext(FindRec);
    FindClose(FindRec);
  end;
end;

function InitializeSetup(): Boolean;
begin
  Result := True;
  // 覆盖安装/升级前，先停掉后台守护，避免文件被占用导致更新失败
  StopBackgroundService();
  // 清理残留注册表（手动删过目录的情况），让覆盖安装顺利进行
  CleanStaleInstall();
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  // 在向导结束、真正复制文件之前，再强制停止一次后台守护，确保文件句柄已释放
  StopBackgroundService();
end;

function InitializeUninstall(): Boolean;
begin
  Result := True;
  // 卸载前先停掉后台守护服务，再进行删除操作
  StopBackgroundService();
  // 顺手清理开机自启项（仅在本程序自身的卸载流程里执行）
  RemoveAutostart();
  // 清理运行时生成的 config/logs/pid/status/port/url 等文件
  CleanRuntimeFiles();
  // 清理 C 盘 WebView2 / _MEI 等运行缓存
  CleanCaches();
end;

// 真正开始复制/替换文件时（ssInstall），最后再确认后台守护已完全退出，
// 避免旧 exe 仍被占用导致“无法自动关闭应用程序”提示或覆盖失败。
procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssInstall then
    StopBackgroundService();
end;
