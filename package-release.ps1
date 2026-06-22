# package-release.ps1
# Builds two GitHub Release zip assets:
#   Claude-Traffic-Light-Windows.zip  — full Windows install (double-click Setup.bat)
#   Claude-Traffic-Light-Mac.zip      — Mac install package (double-click install.command)
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File package-release.ps1
#   powershell -ExecutionPolicy Bypass -File package-release.ps1 -Version "1.2.0"

param(
    [string]$Version = "1.0.0"
)

$Root    = Split-Path -Parent $MyInvocation.MyCommand.Definition
$OutDir  = Join-Path $Root "release"

# Ensure output folder is clean
if (Test-Path $OutDir) { Remove-Item $OutDir -Recurse -Force }
New-Item -ItemType Directory -Path $OutDir | Out-Null

Write-Host ""
Write-Host "Building Claude Traffic Light v$Version release packages..." -ForegroundColor Cyan
Write-Host ""

# ── Windows package ──────────────────────────────────────────────────────────
Write-Host "  Building Windows package..." -ForegroundColor Yellow

$WinStage = Join-Path $OutDir "win-stage\Claude-Traffic-Light"
New-Item -ItemType Directory -Path $WinStage -Force | Out-Null
New-Item -ItemType Directory -Path "$WinStage\bin"   -Force | Out-Null
New-Item -ItemType Directory -Path "$WinStage\hooks" -Force | Out-Null

# Core files
$WinFiles = @(
    @{ Src = "Setup.bat";              Dst = "Setup.bat" },
    @{ Src = "start-hud.cmd";          Dst = "start-hud.cmd" },
    @{ Src = "stop-hud.cmd";           Dst = "stop-hud.cmd" },
    @{ Src = "create-shortcut.ps1";    Dst = "create-shortcut.ps1" },
    @{ Src = "setup-windows.html";     Dst = "setup-windows.html" },
    @{ Src = "bin\hud_daemon.pyw";     Dst = "bin\hud_daemon.pyw" },
    @{ Src = "bin\logi_led.py";        Dst = "bin\logi_led.py" },
    @{ Src = "bin\install.py";         Dst = "bin\install.py" },
    @{ Src = "bin\launcher.pyw";       Dst = "bin\launcher.pyw" },
    @{ Src = "bin\make_icon.py";       Dst = "bin\make_icon.py" },
    @{ Src = "bin\traffic_light.ico";  Dst = "bin\traffic_light.ico" },
    @{ Src = "hooks\hud_hook.py";      Dst = "hooks\hud_hook.py" },
    @{ Src = "hooks\remote_hook.py";   Dst = "hooks\remote_hook.py" }
)

# Include the Logitech DLL if present
$Dll = Join-Path $Root "bin\LogitechLedEnginesWrapper.dll"
if (Test-Path $Dll) {
    $WinFiles += @{ Src = "bin\LogitechLedEnginesWrapper.dll"; Dst = "bin\LogitechLedEnginesWrapper.dll" }
}

foreach ($f in $WinFiles) {
    $src = Join-Path $Root $f.Src
    $dst = Join-Path $WinStage $f.Dst
    if (Test-Path $src) {
        $dstDir = Split-Path $dst -Parent
        if (-not (Test-Path $dstDir)) { New-Item -ItemType Directory -Path $dstDir -Force | Out-Null }
        Copy-Item $src $dst -Force
    } else {
        Write-Warning "    Missing: $($f.Src)"
    }
}

$WinZip = Join-Path $OutDir "Claude-Traffic-Light-Windows-v$Version.zip"
Compress-Archive -Path (Join-Path $OutDir "win-stage\*") -DestinationPath $WinZip -Force
Remove-Item (Join-Path $OutDir "win-stage") -Recurse -Force

$WinSize = [math]::Round((Get-Item $WinZip).Length / 1KB, 1)
Write-Host "  ✓ Windows: Claude-Traffic-Light-Windows-v$Version.zip ($WinSize KB)" -ForegroundColor Green

# ── Mac package ──────────────────────────────────────────────────────────────
Write-Host "  Building Mac package..." -ForegroundColor Yellow

$MacStage = Join-Path $OutDir "mac-stage\Claude-Traffic-Light-Mac"
New-Item -ItemType Directory -Path $MacStage -Force | Out-Null

$MacFiles = @(
    @{ Src = "install.command";     Dst = "install.command" },
    @{ Src = "hooks\remote_hook.py"; Dst = "remote_hook.py" },
    @{ Src = "setup-mac.html";      Dst = "setup-mac.html" }
)

foreach ($f in $MacFiles) {
    $src = Join-Path $Root $f.Src
    $dst = Join-Path $MacStage $f.Dst
    if (Test-Path $src) {
        Copy-Item $src $dst -Force
    } else {
        Write-Warning "    Missing: $($f.Src)"
    }
}

# Write a plain-text quick-start so Finder users know what to do
@"
Claude Traffic Light — Mac
==========================

QUICK START
-----------
1. Extract this zip anywhere (e.g. your Desktop)
2. Double-click  install.command
   (If blocked: right-click → Open → Open)
3. Follow the on-screen steps — takes about 2 minutes
4. Start a Claude Code session on this Mac
5. Watch your Windows PC's HUD update in real time!

NEED HELP?
----------
Open setup-mac.html in your browser for the full guide.

"@ | Set-Content -Path (Join-Path $MacStage "README.txt") -Encoding UTF8

$MacZip = Join-Path $OutDir "Claude-Traffic-Light-Mac-v$Version.zip"
Compress-Archive -Path (Join-Path $OutDir "mac-stage\*") -DestinationPath $MacZip -Force
Remove-Item (Join-Path $OutDir "mac-stage") -Recurse -Force

$MacSize = [math]::Round((Get-Item $MacZip).Length / 1KB, 1)
Write-Host "  ✓ Mac:     Claude-Traffic-Light-Mac-v$Version.zip ($MacSize KB)" -ForegroundColor Green

# ── Summary ──────────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "Release assets ready in:  .\release\" -ForegroundColor Cyan
Write-Host ""
Write-Host "  Upload both zips to your GitHub Release:" -ForegroundColor White
Write-Host "    1. Go to your repo on GitHub"
Write-Host "    2. Click Releases → Draft a new release"
Write-Host "    3. Tag: v$Version"
Write-Host "    4. Drag both zip files into the Assets section"
Write-Host "    5. Publish"
Write-Host ""
Write-Host "  Mac users download:  Claude-Traffic-Light-Mac-v$Version.zip" -ForegroundColor DarkGray
Write-Host "  Extract → double-click install.command → done!" -ForegroundColor DarkGray
Write-Host ""
