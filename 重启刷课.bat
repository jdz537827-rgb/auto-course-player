@echo off
cd /d "%~dp0"
echo ============================================
echo   Chaoxing Auto Course Player (Clean Start)
echo   Step 1: kill stale Edge processes
echo   Step 2: launch fresh browser + pick course
echo ============================================
echo.
taskkill /F /IM msedge.exe >nul 2>&1
timeout /t 2 >nul
echo Stale Edge cleared. Starting player...
echo.
if exist "C:\Program Files\python\python.exe" (
    "C:\Program Files\python\python.exe" chaoxing_player.py --config chaoxing.json --pick
) else (
    python chaoxing_player.py --config chaoxing.json --pick
)
echo.
echo Done. Press any key to close...
pause >nul
