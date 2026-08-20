@echo off
setlocal
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo Python was not found on PATH.
    exit /b 1
)

python -c "import winpty, websocket, qrcode, PIL, sv_ttk" >nul 2>nul
if errorlevel 1 (
    echo Installing RelayTerm desktop dependencies...
    python -m pip install -r "%~dp0bridge\requirements.txt"
    if errorlevel 1 exit /b 1
)

for /f "delims=" %%P in ('python -c "import pathlib,sys; p=pathlib.Path(sys.executable); w=p.with_name('pythonw.exe'); print(w if w.exists() else p)"') do set "PYTHONW=%%P"
start "" /b "%PYTHONW%" -m pc.agent
echo RelayTerm launcher started. Press Ctrl+Alt+Shift+R to open projects.
exit /b 0
