@echo off
rem Build the Windows installer, dist\HomeFinance-Windows-Setup.exe, to share. Run on Windows.
rem Needs Inno Setup 6 (https://jrsoftware.org/isdl.php, or: winget install JRSoftware.InnoSetup).
setlocal
cd /d "%~dp0"

set "UV="
for /f "delims=" %%i in ('where uv 2^>nul') do if not defined UV set "UV=%%i"
if not defined UV if exist "%USERPROFILE%\.local\bin\uv.exe" set "UV=%USERPROFILE%\.local\bin\uv.exe"
if not defined UV (
  echo Installing uv ^(fetches Python for the build^)...
  set "UV_NO_MODIFY_PATH=1"
  powershell -NoProfile -ExecutionPolicy ByPass -Command "irm https://astral.sh/uv/install.ps1 | iex"
  set "UV=%USERPROFILE%\.local\bin\uv.exe"
)

set "ISCC="
for /f "delims=" %%i in ('where iscc 2^>nul') do if not defined ISCC set "ISCC=%%i"
for %%d in ("%ProgramFiles(x86)%" "%ProgramFiles%" "%LOCALAPPDATA%\Programs") do (
  if not defined ISCC if exist "%%~d\Inno Setup 6\ISCC.exe" set "ISCC=%%~d\Inno Setup 6\ISCC.exe"
)
if not defined ISCC (
  echo Inno Setup 6 isn't installed. Get it from https://jrsoftware.org/isdl.php
  echo or run: winget install JRSoftware.InnoSetup
  goto :fail
)

if not exist .build-venv\Scripts\python.exe "%UV%" venv --quiet --clear --python 3.13 .build-venv || goto :fail
"%UV%" pip install --quiet --python .build-venv\Scripts\python.exe -r requirements.txt -r requirements-desktop.txt || goto :fail
.build-venv\Scripts\pyinstaller.exe --noconfirm --clean --log-level WARN HomeFinance.spec || goto :fail
for /f "delims=" %%v in ('.build-venv\Scripts\python.exe -c "from app.version import VERSION; print(VERSION)"') do set "VER=%%v"
"%ISCC%" /Q /DAppVersion=%VER% packaging\windows-installer.iss || goto :fail
rem Version 1.1.0 can only update itself from this zip of the app folder, so releases still carry
rem it. Drop it once nobody is on 1.1.0.
powershell -NoProfile -Command "Compress-Archive -Force -Path 'dist\Home Finance','Read Me First.txt' -DestinationPath 'dist\HomeFinance-Windows.zip'" || goto :fail
echo Built "dist\HomeFinance-Windows-Setup.exe" (and "dist\HomeFinance-Windows.zip" for 1.1.0 updates)
exit /b 0

:fail
echo Build failed.
exit /b 1
