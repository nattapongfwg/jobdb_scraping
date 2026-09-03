@echo off
REM Keep-alive wrapper for webapp.py: restarts the server if it ever exits.
REM Do not run this by hand - use:  service.ps1 start | stop | restart | status | logs | deploy
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
if not exist logs mkdir logs
del /q logs\stop.flag 2>nul

:loop
if exist logs\webapp.prev.log del /q logs\webapp.prev.log
if exist logs\webapp.log move /y logs\webapp.log logs\webapp.prev.log >nul
echo [%date% %time%] starting webapp.py>>logs\service.log
".venv\Scripts\python.exe" webapp.py >>logs\webapp.log 2>&1
set rc=%errorlevel%
if exist logs\stop.flag (
  echo [%date% %time%] stopped by service.ps1>>logs\service.log
  exit /b 0
)
echo [%date% %time%] webapp.py exited with code %rc% - restarting in 5s>>logs\service.log
ping -n 6 127.0.0.1 >nul
goto loop
