# create-shortcut.ps1
# Creates a "Claude Traffic Light.lnk" shortcut on the Desktop pointing to
# the launcher app (launcher.pyw) with the traffic-light icon.
# After it runs, right-click the Desktop shortcut -> "Pin to taskbar".

$ScriptDir   = Split-Path -Parent $MyInvocation.MyCommand.Definition
$LauncherPyw = Join-Path $ScriptDir "bin\launcher.pyw"
$IconPath    = Join-Path $ScriptDir "bin\traffic_light.ico"
$ShortcutDst = [System.IO.Path]::Combine(
    [Environment]::GetFolderPath("Desktop"),
    "Claude Traffic Light.lnk"
)

# Find pythonw.exe so the shortcut runs without a console window.
$PythonDir = & py -3 -c "import sys,os; print(os.path.dirname(sys.executable))" 2>$null
$PythonW   = Join-Path $PythonDir "pythonw.exe"
if (-not (Test-Path $PythonW)) {
    $PythonW = (Get-Command pythonw.exe -ErrorAction SilentlyContinue)?.Source
}
if (-not $PythonW -or -not (Test-Path $PythonW)) {
    Write-Warning "pythonw.exe not found — falling back to python.exe (console will flash briefly)"
    $PythonW = (Get-Command python.exe -ErrorAction SilentlyContinue)?.Source
}
if (-not $PythonW) {
    Write-Error "Python not found in PATH. Install Python 3 and try again."
    exit 1
}

$WShell   = New-Object -ComObject WScript.Shell
$Shortcut = $WShell.CreateShortcut($ShortcutDst)
$Shortcut.TargetPath       = $PythonW
$Shortcut.Arguments        = "`"$LauncherPyw`""
$Shortcut.WorkingDirectory = $ScriptDir
$Shortcut.IconLocation     = $IconPath
$Shortcut.Description      = "Open the Claude Traffic Light HUD launcher"
$Shortcut.Save()

Write-Host ""
Write-Host "Shortcut created:" -ForegroundColor Green
Write-Host "  $ShortcutDst" -ForegroundColor Cyan
Write-Host ""
Write-Host "To pin it to the taskbar:" -ForegroundColor Yellow
Write-Host "  1. Right-click 'Claude Traffic Light' on your Desktop"
Write-Host "  2. Select 'Pin to taskbar'"
Write-Host ""
