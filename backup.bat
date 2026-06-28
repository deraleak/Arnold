@echo off
cd /d "c:\Python Projects\Arnold"
echo Running Arnold DB backup...
.venv\Scripts\python.exe backup.py
if %ERRORLEVEL% NEQ 0 (
    echo Backup failed. See error above.
    pause
)
