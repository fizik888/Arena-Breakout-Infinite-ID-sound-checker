@echo off
chcp 65001 >nul
cd /d "%~dp0"

py -3.12 -c "import sys" >nul 2>nul
if errorlevel 1 (
  echo Python 3.12 not found.
  pause
  exit /b 1
)

py -3.12 -m pip install -q -r requirements.txt pyinstaller
py -3.12 -m PyInstaller --noconfirm --clean --onefile --windowed --name ABI_Sound_ID --collect-all sounddevice --collect-all soundfile --collect-all pyaudiowpatch --hidden-import _cffi_backend --hidden-import abi_audio --hidden-import abi_sound_id --hidden-import i18n abi_gui.py

echo.
echo Output: %~dp0dist\ABI_Sound_ID.exe
pause
