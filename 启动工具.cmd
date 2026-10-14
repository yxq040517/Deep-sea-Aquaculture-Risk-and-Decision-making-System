@echo off
setlocal
cd /d "%~dp0" || goto path_error
if exist ".venv\Scripts\python.exe" goto launch

set "UV="
where uv.exe >nul 2>nul && set "UV=uv.exe"
if not defined UV if exist "%USERPROFILE%\.local\bin\uv.exe" set "UV=%USERPROFILE%\.local\bin\uv.exe"
if not defined UV goto missing_uv
echo Project Python environment not found. Installing from uv.lock...
"%UV%" sync --locked --python 3.10
if errorlevel 1 goto install_error
if not exist ".venv\Scripts\python.exe" goto install_error

:launch
echo Starting one local platform; its address will be printed after startup.
".venv\Scripts\python.exe" main.py
if errorlevel 1 goto runtime_error
exit /b 0

:path_error
echo Cannot enter the project directory.
goto failed

:missing_uv
echo uv.exe was not found. Install uv from https://docs.astral.sh/uv/ then reopen this file.
goto failed

:install_error
echo Automatic dependency installation failed. Review the error above.
goto failed

:runtime_error
echo The application stopped with an error. Read the message above.

:failed
pause
exit /b 1
