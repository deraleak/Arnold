@echo off
cd /d "c:\Python Projects\Arnold"

:: Start Ollama if not already running
tasklist /FI "IMAGENAME eq ollama.exe" 2>NUL | find /I "ollama.exe" >NUL
if %ERRORLEVEL% NEQ 0 (
    echo Starting Ollama...
    start "" ollama serve
    timeout /t 4 /nobreak >NUL
)

:: Start Arnold server in its own window
echo Starting Arnold...
start "Arnold" .venv\Scripts\python.exe script.py

:: Give the server a moment to bind, then open the browser
timeout /t 2 /nobreak >NUL
start http://localhost:8000
