@echo off
rem Build "Home Finance.exe" (in dist\Home Finance\) and a zip to share. Run on Windows.
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

if not exist .build-venv\Scripts\python.exe "%UV%" venv --quiet --clear --python 3.13 .build-venv || goto :fail
"%UV%" pip install --quiet --python .build-venv\Scripts\python.exe -r requirements.txt -r requirements-desktop.txt || goto :fail
.build-venv\Scripts\pyinstaller.exe --noconfirm --clean --log-level WARN HomeFinance.spec || goto :fail
powershell -NoProfile -Command "Compress-Archive -Force -Path 'dist\Home Finance','Read Me First.txt' -DestinationPath 'dist\HomeFinance-Windows.zip'" || goto :fail
echo Built "dist\Home Finance\Home Finance.exe" and "dist\HomeFinance-Windows.zip"
exit /b 0

:fail
echo Build failed.
exit /b 1
