@echo off
REM Volume Scanner - Windows launcher (double-click to run)
cd /d "%~dp0"
set PYTHONUTF8=1
chcp 65001 >nul

if not exist ".env" (
    copy ".env.example" ".env" >nul
    echo Created .env - put your TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in it, then run start.bat again.
    notepad .env
    pause
    exit /b
)

if not exist ".venv\Scripts\python.exe" (
    echo First run: creating virtual environment and installing packages...
    py -3 -m venv .venv || python -m venv .venv
    ".venv\Scripts\python.exe" -m pip install --upgrade pip -q
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt -q
)

:loop
echo Starting Volume Scanner... (close this window to stop)
".venv\Scripts\python.exe" -m scanner
echo Scanner stopped. Restarting in 30 seconds... (Ctrl+C to quit)
timeout /t 30 /nobreak >nul
goto loop
