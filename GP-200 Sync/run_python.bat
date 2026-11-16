@echo off
set "HERE=%~dp0"
cd /d "%HERE%"
py "%HERE%python\gp200_sync_app.py"
if errorlevel 1 pause
