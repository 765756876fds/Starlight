$projectDir = "C:\Users\1\Doubao\chats\2026-09-14\new-chat\lol-music-agent"
$vbs = Join-Path $projectDir "启动_无窗口.vbs"
if (-not (Test-Path $vbs)) { Write-Host "ERROR: $vbs not found"; exit 1 }
$desktopPaths = @(
  [Environment]::GetFolderPath("Desktop"),
  [Environment]::GetFolderPath("DesktopDirectory"),
  (Join-Path $env:USERPROFILE "Desktop"),
  "C:\Users\Public\Desktop"
) | Where-Object { $_ -and (Test-Path $_) } | Select-Object -Unique
$ws = New-Object -ComObject WScript.Shell
foreach ($desk in $desktopPaths) {
  $lnk = Join-Path $desk "LoL Music Agent.lnk"
  $s = $ws.CreateShortcut($lnk)
  $s.TargetPath = "wscript.exe"
  $s.Arguments = "`"$vbs`""
  $s.WorkingDirectory = $projectDir
  $s.Description = "LoL Music Agent"
  $s.IconLocation = "$env:SystemRoot\System32\shell32.dll,137"
  $s.Save()
}
Write-Host "=== done ==="
foreach ($desk in $desktopPaths) {
  $lnk = Join-Path $desk "LoL Music Agent.lnk"
  if (Test-Path $lnk) { Write-Host "OK  $lnk" } else { Write-Host "FAIL $lnk" }
}
