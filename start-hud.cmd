@echo off
rem Start the Claude Code Status HUD daemon (no console window).
rem Finds pythonw.exe automatically — no hardcoded path required.

rem Try pythonw.exe via the Python launcher (py.exe ships with every Python 3.3+ installer on Windows)
for /f "usebackq delims=" %%P in (`py -3 -c "import sys,os; print(os.path.join(os.path.dirname(sys.executable),'pythonw.exe'))" 2^>nul`) do (
    if exist "%%P" (
        start "" "%%P" "%~dp0bin\hud_daemon.pyw"
        exit /b 0
    )
)

rem Fallback: look for pythonw.exe in PATH
where pythonw.exe >nul 2>&1
if not errorlevel 1 (
    start "" pythonw.exe "%~dp0bin\hud_daemon.pyw"
    exit /b 0
)

rem Last resort: use python.exe (console window appears briefly)
start /min "" python.exe "%~dp0bin\hud_daemon.pyw"
