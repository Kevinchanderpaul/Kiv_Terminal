@echo off
cd /d "%~dp0"
echo ============================================================
echo    THE KIV TERMINAL  ^|  EXE Builder
echo ============================================================
echo Working directory: %CD%
echo.

echo [1/2] Installing Python dependencies...
pip install pyinstaller yfinance pandas numpy flask requests
if %errorlevel% neq 0 (
    echo ERROR: pip install failed. Make sure Python is installed.
    pause & exit /b 1
)

echo.
echo [2/2] Building KivTerminal.exe...
if not exist "terminal.ico" (
    echo WARNING: terminal.ico not found in current directory!
    echo Building without custom icon...
    python -m PyInstaller --onefile --noconsole --name "KivTerminal" ^
      --add-data "static;static" ^
      --hidden-import=yfinance ^
      --hidden-import=pandas ^
      --hidden-import=numpy ^
      --hidden-import=flask ^
      --hidden-import=requests ^
      app.py
) else (
    echo Found terminal.ico - using as app icon
    python -m PyInstaller --onefile --noconsole --name "KivTerminal" ^
      --icon="%CD%\terminal.ico" ^
      --add-data "static;static" ^
      --hidden-import=yfinance ^
      --hidden-import=pandas ^
      --hidden-import=numpy ^
      --hidden-import=flask ^
      --hidden-import=requests ^
      app.py
)

if %errorlevel% neq 0 (
    echo ERROR: PyInstaller build failed. See output above.
    pause & exit /b 1
)

echo.
echo ============================================================
echo    SUCCESS!
echo    Your exe is ready at:  %CD%\dist\KivTerminal.exe
echo    Double-click it to launch the terminal.
echo ============================================================
pause
