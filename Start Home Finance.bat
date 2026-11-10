@echo off
rem Double-click to start Home Finance on Windows. The first run downloads what it needs
rem (the "uv" tool and Python, about 50 MB) and can take a minute; later starts are quick.
setlocal
cd /d "%~dp0"
title Home Finance

set "UV="
for /f "delims=" %%i in ('where uv 2^>nul') do if not defined UV set "UV=%%i"
if not defined UV if exist "%USERPROFILE%\.local\bin\uv.exe" set "UV=%USERPROFILE%\.local\bin\uv.exe"
if not defined UV (
  echo First run: installing uv ^(it fetches Python for Home Finance^)...
  set "UV_NO_MODIFY_PATH=1"
  powershell -NoProfile -ExecutionPolicy ByPass -Command "irm https://astral.sh/uv/install.ps1 | iex"
  set "UV=%USERPROFILE%\.local\bin\uv.exe"
)
if not exist "%UV%" (
  echo.
  echo Couldn't download uv. Check your internet connection and try again.
  pause
  exit /b 1
)

"%UV%" run --quiet --no-project --python 3.13 --with-requirements requirements.txt python run.py %*
if errorlevel 1 (
  echo.
  echo Home Finance stopped with an error.
  pause
)
