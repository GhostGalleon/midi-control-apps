@echo off
set "HERE=%~dp0"
cd /d "%HERE%"
py "%HERE%python\captain_app.py"
if errorlevel 1 pause
