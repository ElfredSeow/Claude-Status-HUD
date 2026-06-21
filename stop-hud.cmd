@echo off
rem Stop any running Status HUD daemon.
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe'\" | Where-Object { $_.CommandLine -like '*hud_daemon.pyw*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
