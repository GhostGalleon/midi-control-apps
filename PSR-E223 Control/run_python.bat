@echo off
set "HERE=%~dp0"
cd /d "%HERE%"
py "%HERE%python\psr_app.py"
if errorlevel 1 pause
