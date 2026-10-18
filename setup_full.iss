; LoL_Music_Agent 离线全量版安装包（Inno Setup 6）
; 源 = build.ps1 产物 dist\LoL_Music_Agent + 本地 ASR 模型（打包进 {app}\models，断网可用）
; 模型目录可在命令行覆盖：ISCC.exe /DModelsDir="D:\path\to\model" setup_full.iss
#define MyAppName "LoL_Music_Agent"
#define MyAppVersion "0.1.0"
#define MyAppExeName "LoL_Music_Agent.exe"
#define SourceDir "dist\LoL_Music_Agent"
#ifndef ModelsDir
#define ModelsDir "D:\迅雷下载\ggml-org\Qwen3-ASR-0.6B-GGUF"
#endif

[Setup]
AppId={{C8E4F3B2-5D6E-4F70-9BAC-1E2F304A5C6D}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher=LoL Music Agent
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
; 管理员权限（连招/LCU 钩子需要）
PrivilegesRequired=admin
; 全量版：含 ASR 模型（Q8_0 ~767MB + mmproj-bf16 ~361MB），断网可用，无需下载
OutputDir=.
OutputBaseFilename=Setup_Full
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
