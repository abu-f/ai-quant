@echo off
REM ============================================================
REM  aquant / TSP local dev launcher (Windows)
REM  Double-click this file to start backend (3018) + frontend (3011).
REM  Close the window or press Ctrl-C to stop both.
REM ============================================================
setlocal
cd /d "%~dp0"

if not exist "backend\.venv\Scripts\python.exe" (
    echo [ERROR] backend venv not found. Run: cd backend ^&^& uv sync
    pause
    exit /b 1
)
if not exist "frontend\node_modules" (
    echo [ERROR] frontend deps not found. Run: cd frontend ^&^& pnpm install
    pause
    exit /b 1
)

echo Starting TSP ...
echo   backend  http://127.0.0.1:3018
echo   frontend http://127.0.0.1:3011
echo.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0dev.ps1"

echo.
echo Dev server stopped.
pause
