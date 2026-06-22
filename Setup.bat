@echo off
setlocal EnableDelayedExpansion
title Claude Traffic Light — Windows Setup

rem ── Colours (requires Windows 10+ with VT support) ──────────────────────────
for /f %%A in ('echo prompt $E ^| cmd') do set "ESC=%%A"
set "GREEN=%ESC%[92m"
set "YELLOW=%ESC%[93m"
set "RED=%ESC%[91m"
set "CYAN=%ESC%[96m"
set "BOLD=%ESC%[1m"
set "DIM=%ESC%[2m"
set "RESET=%ESC%[0m"

cls
echo.
echo   %BOLD%%CYAN%🚦  Claude Traffic Light%RESET%
echo       Windows Setup Installer
echo.
echo   %DIM%This sets up the HUD overlay on this Windows PC.%RESET%
echo   %DIM%It also lets your Mac send sessions here over WiFi.%RESET%
echo.

rem ── Check Python ─────────────────────────────────────────────────────────────
echo   %BOLD%Step 1 of 4 — Checking Python 3%RESET%
echo   %DIM%────────────────────────────────────────────────────%RESET%
echo.

python --version >nul 2>&1
if errorlevel 1 (
    py -3 --version >nul 2>&1
    if errorlevel 1 (
        echo   %RED%✗%RESET%  Python 3 not found.
        echo.
        echo   %DIM%Download it from: https://www.python.org/downloads/%RESET%
        echo   %DIM%Make sure to check "Add Python to PATH" during install.%RESET%
        echo.
        pause
        exit /b 1
    )
    set "PYTHON=py -3"
) else (
    set "PYTHON=python"
)

for /f "tokens=*" %%V in ('!PYTHON! --version 2^>^&1') do set "PY_VER=%%V"
echo   %GREEN%✓%RESET%  Found: !PY_VER!
echo.

rem ── Install pystray + Pillow ─────────────────────────────────────────────────
echo   %BOLD%Step 2 of 4 — Installing dependencies%RESET%
echo   %DIM%────────────────────────────────────────────────────%RESET%
echo.
echo   %DIM%Installing pystray and Pillow (needed for the tray icon)...%RESET%

!PYTHON! -m pip install --quiet pystray pillow 2>nul
if errorlevel 1 (
    echo   %YELLOW%⚠%RESET%  pip install had warnings (may still work).
) else (
    echo   %GREEN%✓%RESET%  pystray + Pillow ready
)
echo.

rem ── Register Claude Code hooks ───────────────────────────────────────────────
echo   %BOLD%Step 3 of 4 — Registering Claude Code hooks%RESET%
echo   %DIM%────────────────────────────────────────────────────%RESET%
echo.

!PYTHON! "%~dp0bin\install.py"
if errorlevel 1 (
    echo   %RED%✗%RESET%  Hook installation failed.
    echo   %DIM%Check that Claude Code is installed: %USERPROFILE%\.claude\settings.json%RESET%
    pause
    exit /b 1
)
echo.

rem ── Create taskbar shortcut ──────────────────────────────────────────────────
echo   %BOLD%Step 4 of 4 — Creating taskbar shortcut%RESET%
echo   %DIM%────────────────────────────────────────────────────%RESET%
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0create-shortcut.ps1" >nul 2>&1
if errorlevel 1 (
    echo   %YELLOW%⚠%RESET%  Could not create shortcut automatically.
    echo   %DIM%You can run create-shortcut.ps1 manually later.%RESET%
) else (
    echo   %GREEN%✓%RESET%  "Claude Traffic Light" shortcut created on Desktop
    echo   %DIM%      Right-click it → "Pin to taskbar" for one-click access%RESET%
)
echo.

rem ── Your Windows IP ──────────────────────────────────────────────────────────
echo   %CYAN%Your Windows IP address (share this with your Mac):%RESET%
echo.
for /f "tokens=2 delims=:" %%I in ('ipconfig ^| findstr /C:"IPv4 Address"') do (
    set "IP=%%I"
    set "IP=!IP: =!"
    echo   %BOLD%    !IP!%RESET%
    goto :got_ip
)
:got_ip
echo.
echo   %DIM%Mac users will connect to: http://YOUR_IP:51790%RESET%
echo.

rem ── Open the firewall port ───────────────────────────────────────────────────
netsh advfirewall firewall show rule name="Claude HUD" >nul 2>&1
if errorlevel 1 (
    echo   %DIM%Adding Windows Firewall rule for port 51790...%RESET%
    netsh advfirewall firewall add rule name="Claude HUD" dir=in action=allow protocol=TCP localport=51790 >nul 2>&1
    if errorlevel 1 (
        echo   %YELLOW%⚠%RESET%  Could not add firewall rule (run Setup.bat as Administrator to fix).
    ) else (
        echo   %GREEN%✓%RESET%  Firewall: port 51790 open for incoming Mac connections
    )
) else (
    echo   %GREEN%✓%RESET%  Firewall rule already exists
)
echo.

rem ── Done ─────────────────────────────────────────────────────────────────────
echo   %GREEN%%BOLD%✅  Setup complete!%RESET%
echo.
echo   %DIM%What to do next:%RESET%
echo   %BOLD%1.%RESET%  Double-click "Claude Traffic Light" on your Desktop to open the launcher
echo   %BOLD%2.%RESET%  Click "Start HUD" — the floating overlay will appear
echo   %BOLD%3.%RESET%  (Optional) Check "Run on Startup" so it starts with Windows
echo   %BOLD%4.%RESET%  On your Mac, run install.command from the Mac release package
echo.

set /p "LAUNCH=   Start the HUD now? (Y/n): "
if /i not "!LAUNCH!"=="n" (
    start "" "%~dp0start-hud.cmd"
    echo   %GREEN%✓%RESET%  HUD started — look for the floating overlay on screen
    echo   %DIM%     Also check the system tray (bottom-right) for the traffic-light icon%RESET%
)
echo.
pause
