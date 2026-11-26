@echo off
cd /d "%~dp0"
echo === DS-BOT setup ===
where py >nul 2>nul
if errorlevel 1 goto nopython

if not exist .venv\Scripts\python.exe (
  echo Creating virtual environment...
  py -3 -m venv .venv
)
if not exist .venv\Scripts\python.exe goto venvfail

.venv\Scripts\python.exe -m pip install -U pip
.venv\Scripts\python.exe -m pip install -U -r requirements.txt
if errorlevel 1 goto pipfail

where ffmpeg >nul 2>nul
if errorlevel 1 (
  echo Installing FFmpeg...
  winget install -e --id Gyan.FFmpeg --accept-source-agreements --accept-package-agreements
)
where deno >nul 2>nul
if errorlevel 1 (
  echo Installing Deno - needed by yt-dlp for YouTube...
  winget install -e --id DenoLand.Deno --accept-source-agreements --accept-package-agreements
)
if not exist .env copy .env.example .env >nul

echo.
echo DONE! Open the .env file, paste DISCORD_TOKEN and run start.bat
echo If FFmpeg or Deno were just installed - close this window first so PATH updates.
pause
exit /b 0

:nopython
echo Python not found. Install Python 3.13 from python.org and tick "Add python.exe to PATH".
pause
exit /b 1

:venvfail
echo Could not create .venv
pause
exit /b 1

:pipfail
echo.
echo Library install FAILED. Probably your Python version is too new.
echo Install Python 3.13, delete the .venv folder and run:
echo   py -3.13 -m venv .venv
echo   install.bat
pause
exit /b 1
