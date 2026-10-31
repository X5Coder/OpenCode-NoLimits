@echo off
REM Build single-file console EXE with PyInstaller and UAC Administrator manifest
cd /d "%~dp0"
python -m pip install -r requirements.txt
python -m PyInstaller --onefile --console --uac-admin --name WarpSetup --icon icon.ico warp_setup.py
echo.
if exist "dist\WarpSetup.exe" (
  echo ========================================================
  echo SUCCESS: Built dist\WarpSetup.exe with Administrator manifest
  echo ========================================================
) else (
  echo FAILED: dist\WarpSetup.exe not created, check errors above.
)
pause
