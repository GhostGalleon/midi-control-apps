@echo off
set "HERE=%~dp0"
cd /d "%HERE%"
py "%HERE%python\td6_app.py"
if errorlevel 1 pause
