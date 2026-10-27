# build.ps1 - LoL_Music_Agent one-click packaging (onedir portable folder)
# Usage: powershell -ExecutionPolicy Bypass -File scripts\build.ps1
$ErrorActionPreference = 'Stop'
# 位于 scripts/ 下：两级向上回退到项目根（third_party/config/data 等相对路径基于项目根）
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $root
$py = 'C:\Program Files\Python311\python.exe'

Write-Host '[1/4] Prepare external tools (mpv / node) ...'
if (-not (Test-Path 'third_party\mpv\mpv.exe')) {
    $src = ''
    $m = Select-String -Path 'config\settings.yaml' -Pattern '^mpv_path:\s*(.+)$' -ErrorAction SilentlyContinue
    if ($m) { $src = $m.Matches[0].Groups[1].Value.Trim() }
    if ($src -and (Test-Path $src)) {
        New-Item -ItemType Directory -Force 'third_party\mpv' | Out-Null
        Copy-Item $src 'third_party\mpv\mpv.exe'
        $dll = Join-Path (Split-Path $src) 'd3dcompiler_43.dll'
        if (Test-Path $dll) { Copy-Item $dll 'third_party\mpv\' }
        Write-Host "    mpv copied from $src"
    } else {
        Write-Host '    [WARN] mpv.exe not found in settings.yaml; skip (music/movie mpv will fall back to PATH)'
    }
}
if (-not (Test-Path 'third_party\node\node.exe')) {
    $copied = $false
    foreach ($c in @('C:\Program Files\nodejs\node.exe', 'C:\Program Files (x86)\nodejs\node.exe')) {
        if (Test-Path $c) {
            New-Item -ItemType Directory -Force 'third_party\node' | Out-Null
            Copy-Item $c 'third_party\node\node.exe'
            Write-Host "    node copied from $c"
            $copied = $true
            break
        }
    }
    if (-not $copied) { Write-Host '    [WARN] node.exe not found; online music JS bridge will be unavailable' }
}
if (-not (Test-Path 'third_party\ffmpeg\ffmpeg.exe')) {
    $ff = (Get-Command ffmpeg -ErrorAction SilentlyContinue).Source
    if (-not $ff) { $ff = 'D:\ffmpeg\ffmpeg.exe' }
    if ($ff -and (Test-Path $ff)) {
        New-Item -ItemType Directory -Force 'third_party\ffmpeg' | Out-Null
        Copy-Item $ff 'third_party\ffmpeg\ffmpeg.exe'
        Write-Host "    ffmpeg copied from $ff"
    } else {
        Write-Host '    [WARN] ffmpeg.exe not found; videofetch m3u8 解析依赖的 ffmpeg 将缺失'
    }
}

Write-Host '[2/4] Running PyInstaller (build.spec) ...'
# Start-Process 重定向：避开 PS 5.1 对 native stderr 的 NativeCommandError 误报
try {
    $proc = Start-Process -FilePath $py `
        -ArgumentList @('-m', 'PyInstaller', 'build.spec', '--clean', '--noconfirm') `
        -WorkingDirectory $root `
        -RedirectStandardOutput 'pyi_log.txt' -RedirectStandardError 'pyi_err.txt' `
        -Wait -PassThru -NoNewWindow
    if ($proc.ExitCode -ne 0) { throw "PyInstaller build failed (see pyi_log.txt / pyi_err.txt), exit=$($proc.ExitCode)" }
} catch {
    if ($_.Exception.Message -match 'WinError 5|Access is denied|denied') {
        throw 'PyInstaller cannot delete old dist (files locked). Close running Starlight / mpv processes and retry.'
    }
    throw
}

Write-Host '[3/4] Post-process: move third_party/ and data/lol_data to exe dir (frozen PROJECT_ROOT = exe dir) ...'
$dist = Join-Path $root 'dist\Starlight'
$internal = Join-Path $dist '_internal'
if (-not (Test-Path (Join-Path $dist 'third_party'))) {
    Move-Item (Join-Path $internal 'third_party') (Join-Path $dist 'third_party') -Force
}
if (-not (Test-Path (Join-Path $dist 'data'))) {
    New-Item -ItemType Directory -Force (Join-Path $dist 'data') | Out-Null
}
if (-not (Test-Path (Join-Path $dist 'data\lol_data'))) {
    Move-Item (Join-Path $internal 'data\lol_data') (Join-Path $dist 'data\lol_data') -Force
}
# JS 音源插件（musicfree/ lx/ 的 .js）：PROJECT_ROOT/data/music_sources →
# 与 lol_data 同规则移到 exe 目录（frozen 态代码按 PROJECT_ROOT 定位）
if (-not (Test-Path (Join-Path $dist 'data\music_sources'))) {
    Move-Item (Join-Path $internal 'data\music_sources') (Join-Path $dist 'data\music_sources') -Force
}
# 外部可写配置：settings.example.yaml 空白模板 → exe 同级 config/（仅供人工参考）
# （真实 settings.yaml 含用户 Key，绝不进包；打包态首次启动由 config.py 在
#   USER_DATA_DIR/config（%LOCALAPPDATA%\Starlight\config）自动生成默认配置，
#   旧版 exe 目录配置/数据库由 migrate_legacy_user_data() 幂等迁移）
if (-not (Test-Path (Join-Path $dist 'config'))) {
    New-Item -ItemType Directory -Force (Join-Path $dist 'config') | Out-Null
}
if ((Test-Path (Join-Path $internal 'config\settings.example.yaml')) -and -not (Test-Path (Join-Path $dist 'config\settings.example.yaml'))) {
    Move-Item (Join-Path $internal 'config\settings.example.yaml') (Join-Path $dist 'config\settings.example.yaml') -Force
}
# tools/.qwen_key 空模板（用户在此填 Qwen Key；绝不复制开发目录真实 Key）
if (-not (Test-Path (Join-Path $dist 'tools'))) {
    New-Item -ItemType Directory -Force (Join-Path $dist 'tools') | Out-Null
}
$qwenTpl = Join-Path $dist 'tools\.qwen_key'
if (-not (Test-Path $qwenTpl)) {
    New-Item -ItemType File -Force $qwenTpl | Out-Null   # 空文件：strip 后为空 → 未配置
}

Write-Host '[4/4] Verify + integrity assertions ...'
# ===== 打包完整性断言（防呆）：缺失任何关键资源 → 抛异常中断，绝不生成安装包 =====
# 断言检查的是 exe 同级外层根目录（外部可读写），非 _internal
$missing = @()
$qss = Join-Path $internal 'app\ui\theme\dark.qss'
$iconsDir = Join-Path $internal 'app\ui\theme\icons'
$msDir = Join-Path $dist 'data\music_sources'
$lolDir = Join-Path $dist 'data\lol_data'
$cfgDir = Join-Path $dist 'config'
$cfgTpl = Join-Path $dist 'config\settings.example.yaml'
$appIco = Join-Path $internal 'assets\icon\app.ico'
$starlightExe = Join-Path $dist 'Starlight.exe'
if (-not (Test-Path $qss)) { $missing += "UI 主题 QSS: $qss" }
if (-not (Get-ChildItem $iconsDir -Filter *.svg -ErrorAction SilentlyContinue)) { $missing += "UI 图标 SVG: $iconsDir" }
if (-not (Get-ChildItem $msDir -Recurse -Filter *.js -ErrorAction SilentlyContinue)) { $missing += "JS 音源插件: $msDir" }
if (-not (Get-ChildItem $lolDir -Filter *.json -ErrorAction SilentlyContinue)) { $missing += "游戏静态数据: $lolDir" }
if (-not (Test-Path $cfgTpl)) { $missing += "配置模板 settings.example.yaml: $cfgTpl" }
if (-not (Test-Path $qwenTpl)) { $missing += "Qwen Key 空模板: $qwenTpl" }
if (-not (Test-Path $appIco)) { $missing += "应用图标 app.ico: $appIco" }
if (-not (Test-Path $starlightExe)) { $missing += "主程序 Starlight.exe: $starlightExe" }
if ($missing.Count -gt 0) {
    Write-Host '[BUILD FAILED] 打包完整性断言未通过：'
    foreach ($m in $missing) { Write-Host "  MISSING: $m" }
    throw '打包资源缺失，中断构建——不生成安装包（请检查 build.spec 的 datas 收集规则）'
}
Write-Host '    integrity: theme(QSS+SVG) / js-plugins / lol_data / config模板 / qwen_key模板 / app.ico / Starlight.exe 全部就位'

$exe = Join-Path $dist 'Starlight.exe'
if (-not (Test-Path $exe)) { throw "exe missing: $exe" }
$mpv = Join-Path $dist 'third_party\mpv\mpv.exe'
$node = Join-Path $dist 'third_party\node\node.exe'
$lold = Join-Path $dist 'data\lol_data\champions.json'
Write-Host "    exe:      $exe  $([math]::Round((Get-Item $exe).Length/1MB,1))MB"
Write-Host "    mpv:      $mpv  $(Test-Path $mpv)"
Write-Host "    node:     $node  $(Test-Path $node)"
Write-Host "    lol_data: $lold  $(Test-Path $lold)"
$sz = (Get-ChildItem $dist -Recurse -File | Measure-Object Length -Sum).Sum
Write-Host "    dist total: $([math]::Round($sz/1MB,1)) MB"
Write-Host "DONE: $dist"
