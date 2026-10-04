@echo off
REM Volume Scanner - Windows launcher (double-click to run)
cd /d "%~dp0"
set PYTHONUTF8=1
chcp 65001 >nul

if exist ".env.txt" if not exist ".env" ren ".env.txt" ".env"
if not exist ".env" (
    copy ".env.example" ".env" >nul
    echo Created .env - put your TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in it, then run start.bat again.
    notepad .env
    pause
    exit /b
)

if not exist ".venv\Scripts\python.exe" (
    echo First run: creating virtual environment...
    py -3 -m venv .venv || python -m venv .venv
    ".venv\Scripts\python.exe" -m pip install --upgrade pip -q
)
REM always make sure packages are there (fast when already installed, fixes half installs)
".venv\Scripts\python.exe" -m pip install -r requirements.txt -q --disable-pip-version-check
if errorlevel 1 (
    echo Package install failed - check your internet connection and run start.bat again.
    pause
    exit /b
)

:loop
echo Starting Volume Scanner... (close this window to stop)
".venv\Scripts\python.exe" -m scanner
echo Scanner stopped. Restarting in 30 seconds... (Ctrl+C to quit)
timeout /t 30 /nobreak >nul
goto loop
