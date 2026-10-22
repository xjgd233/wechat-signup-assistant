@echo off
setlocal
cd /d "%~dp0"

echo === WeChat Signup Assistant - Windows build ===
echo.

where python >nul 2>nul
if %errorlevel% neq 0 (
  echo [ERROR] Python was not found on PATH.
  echo Install Python 3.12 from python.org and tick "Add python.exe to PATH",
  echo then double-click this file again.
  pause
  exit /b 1
)

echo [1/4] Creating virtual environment .venv-build
python -m venv .venv-build
if %errorlevel% neq 0 (
  echo [ERROR] Could not create the virtual environment.
  pause
  exit /b 1
)

call .venv-build\Scripts\activate.bat

echo [2/4] Upgrading pip
python -m pip install --upgrade pip

echo [3/4] Installing dependencies (first time needs internet, may take a while)
python -m pip install -r requirements.txt pyinstaller
if %errorlevel% neq 0 (
  echo [ERROR] Dependency install failed. Check your network and try again.
  pause
  exit /b 1
)

echo [4/4] Building
python -m PyInstaller --noconfirm --clean windows_build.spec
if %errorlevel% neq 0 (
  echo [ERROR] Build failed. Copy the error above and send it to me.
  pause
  exit /b 1
)

echo.
echo Done: dist\WeChatSignupAssistant\WeChatSignupAssistant.exe
echo.
echo This folder IS the portable copy: Python and every library live inside it.
echo Copy the WHOLE dist\WeChatSignupAssistant folder to another PC, then just
echo double-click WeChatSignupAssistant.exe there. No Python install needed.
echo.
copy /y README-PORTABLE.txt dist\WeChatSignupAssistant\ >nul
echo A portable readme was placed next to the exe: README-PORTABLE.txt
pause
