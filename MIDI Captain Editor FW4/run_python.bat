@echo off
set "HERE=%~dp0"
cd /d "%HERE%"
py "%HERE%python\captain_fw4_app.py"
if errorlevel 1 pause
