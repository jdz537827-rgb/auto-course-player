@echo off
cd /d "%~dp0"
echo ============================================
echo   Chaoxing Auto Course Player
echo   First run: please scan QR code to login
echo ============================================
echo.
if exist "C:\Program Files\python\python.exe" (
    "C:\Program Files\python\python.exe" chaoxing_player.py --config chaoxing.json --pick
) else (
    python chaoxing_player.py --config chaoxing.json --pick
)
echo.
echo Done. Press any key to close...
pause >nul
