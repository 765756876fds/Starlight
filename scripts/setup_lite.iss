; 星芒（Starlight）轻量版安装包（Inno Setup 6）
; 源 = build.ps1 产物（项目根 dist\Starlight，不含 1.2G ASR 模型；模型首次运行自动下载）
; 相对路径解释为相对脚本目录（scripts/），用 "..\" 回退到项目根。
#define MyAppName "星芒"
#include "version.iss"
#define MyAppExeName "Starlight.exe"
#define SourceDir "..\dist\Starlight"

[Setup]
AppId={{B7E3F2A1-4C5D-4E6F-8A9B-0D1E2F3A4B5C}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
VersionInfoVersion={#MyAppNumericVersion}
VersionInfoProductVersion={#MyAppNumericVersion}
VersionInfoTextVersion={#MyAppVersion}
VersionInfoProductTextVersion={#MyAppVersion}
AppPublisher=星芒
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
; 管理员权限（连招/LCU 钩子需要）
PrivilegesRequired=admin
; 轻量版：不含 ASR 模型，包体仅应用本体（~1.1GB 资源；模型首次运行从国内镜像下载）
OutputDir=.
OutputBaseFilename=Starlight_Setup_Lite
SetupIconFile=..\assets\icon\app.ico
Compression=lzma2/ultra
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\{#MyAppExeName}

[Languages]
Name: "chinesesimplified"; MessagesFile: "compiler:Default.isl"

[Files]
; 全量复制 dist 产物（绝不包含 models/——dist 中本就没有）
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务:"

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "立即运行 {#MyAppName}"; Flags: nowait postinstall skipifsilent
