@echo off
set NODE_EXE=C:\Users\Gabriel\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe
set APP_DIR=%~dp0
powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*comprador_gestaoclick_web.js*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
"%NODE_EXE%" "%APP_DIR%comprador_gestaoclick_web.js"
pause
