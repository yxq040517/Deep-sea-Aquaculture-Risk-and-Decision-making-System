@echo off
setlocal
cd /d "%~dp0" || goto path_error
if not exist ".venv\Scripts\python.exe" goto missing_environment
echo Starting one local platform; its address will be printed after startup.
".venv\Scripts\python.exe" main.py
if errorlevel 1 goto runtime_error
exit /b 0

:path_error
echo Cannot enter the project directory.
goto failed

:missing_environment
echo Project Python environment is missing.
echo From this directory, run: uv sync --python 3.10
goto failed

:runtime_error
echo The application stopped with an error. Read the message above.

:failed
pause
exit /b 1
