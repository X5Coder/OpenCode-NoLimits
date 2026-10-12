@echo off
REM Build single-file console EXE with PyInstaller
cd /d "%~dp0"
python -m pip install -r requirements.txt
python -m PyInstaller --onefile --console --name WarpSetup --icon icon.ico warp_setup.py
echo.
if exist "dist\WarpSetup.exe" (
  echo OK - EXE at: dist\WarpSetup.exe
) else (
  echo FAILED - dist\WarpSetup.exe not created, check errors above.
)
pause

