; 星芒（Starlight）离线全量版安装包（Inno Setup 6）
; 源 = build.ps1 产物（项目根 dist\Starlight）+ 本地 ASR 模型（打包进 {app}\models，断网可用）
; 模型目录可在命令行覆盖：ISCC.exe /DModelsDir="D:\path\to\model" setup_full.iss
; 注意：Inno Setup 把脚本内相对路径（SourceDir/Source/SetupIconFile）解释为
; 相对脚本所在目录（scripts/），故统一用 "..\" 回退到项目根。
#define MyAppName "星芒"
#define MyAppVersion "0.1.0"
#define MyAppExeName "Starlight.exe"
#define SourceDir "..\dist\Starlight"
#ifndef ModelsDir
; 模型目录：项目根 models/（相对 iss 所在目录 scripts/，用 ..\ 回退），换机可打包
#define ModelsDir "..\models"
#endif

[Setup]
AppId={{C8E4F3B2-5D6E-4F70-9BAC-1E2F304A5C6D}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher=星芒
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
; 管理员权限（连招/LCU 钩子需要）
PrivilegesRequired=admin
; 全量版：含 ASR 模型（Q8_0 ~767MB + mmproj-bf16 ~361MB），断网可用，无需下载
OutputDir=.
OutputBaseFilename=Starlight_Setup_Full
SetupIconFile=..\assets\icon\app.ico
Compression=lzma2/ultra
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\{#MyAppExeName}

[Languages]
Name: "chinesesimplified"; MessagesFile: "compiler:Default.isl"

[Files]
; 应用本体（全量复制 dist 产物）
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; ASR 模型（内置 → asr_bootstrap 检测到本地模型后跳过下载，直接拉起 llama-server）
Source: "{#ModelsDir}\Qwen3-ASR-0.6B-Q8_0.gguf"; DestDir: "{app}\models"; Flags: ignoreversion
Source: "{#ModelsDir}\mmproj-Qwen3-ASR-0.6B-bf16.gguf"; DestDir: "{app}\models"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务:"

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "立即运行 {#MyAppName}"; Flags: nowait postinstall skipifsilent
