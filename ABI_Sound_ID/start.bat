@echo off
chcp 65001 >nul
cd /d "%~dp0"

py -3.12 -c "import sys" >nul 2>nul
if errorlevel 1 (
  echo Python 3.12 not found. Install from https://www.python.org/downloads/
  echo Enable "Add python.exe to PATH" during setup.
  pause
  exit /b 1
)

if not exist "abi_gui.py" (
  echo Missing abi_gui.py
  pause
  exit /b 1
)
if not exist "abi_audio.py" (
  echo Missing abi_audio.py
  pause
  exit /b 1
)

echo Installing/updating dependencies...
py -3.12 -m pip install -q -r requirements.txt
if errorlevel 1 (
  echo pip install failed
  pause
  exit /b 1
)

echo Starting ABI Sound ID...
py -3.12 abi_gui.py
if errorlevel 1 pause
