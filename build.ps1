# build.ps1 - LoL_Music_Agent one-click packaging (onedir portable folder)
# Usage: powershell -ExecutionPolicy Bypass -File build.ps1
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
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
        throw 'PyInstaller cannot delete old dist (files locked). Close running LoL_Music_Agent / mpv processes and retry.'
    }
    throw
}

Write-Host '[3/4] Post-process: move third_party/ and data/lol_data to exe dir (frozen PROJECT_ROOT = exe dir) ...'
$dist = Join-Path $root 'dist\LoL_Music_Agent'
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

Write-Host '[4/4] Verify ...'
$exe = Join-Path $dist 'LoL_Music_Agent.exe'
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
