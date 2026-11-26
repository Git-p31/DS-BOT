@echo off
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe goto noinstall
if not exist .env goto noenv
echo Checking libraries and updating yt-dlp...
.venv\Scripts\python.exe -m pip install -q -r requirements.txt
.venv\Scripts\python.exe -m pip install -U -q "yt-dlp[default]"

:loop
.venv\Scripts\python.exe bot.py
if errorlevel 2 goto cfg
echo Bot stopped. Restarting in 5 seconds... Close this window to turn it off.
timeout /t 5 >nul
goto loop

:cfg
echo Fix the settings in the .env file and run start.bat again.
pause
exit /b 1

:noenv
copy .env.example .env >nul
echo Created .env - open it, paste DISCORD_TOKEN and run start.bat again.
pause
exit /b 1

:noinstall
echo Run install.bat first.
pause
exit /b 1
