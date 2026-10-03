@echo off
title ModPackager Executable Compiler
echo ===================================================
echo   Minecraft Modpack Sorter - Auto Compiler
echo ===================================================
echo.

:: 1. Verify Python is installed and accessible in PATH
where python >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo [ERROR] Python is not installed or not added to system PATH.
    echo Please install Python and make sure to check "Add Python to PATH".
    echo.
    pause
    exit /b 1
)

:: 2. Check if required Python dependencies are installed
echo [1/3] Checking dependencies...
python -c "import textual, PyInstaller" >nul 2>&1
if %ERRORLEVEL% EQU 0 (
    echo [OK] All required dependencies are already installed.
    goto COMPILE
)

echo [!] Missing required dependencies (textual and/or PyInstaller).
echo.
set /p "install_deps=Would you like to install them now? [Y/N]: "

if /i "%install_deps%"=="Y" goto INSTALL
if /i "%install_deps%"=="YES" goto INSTALL

echo.
echo [CANCELLED] Cannot compile without required dependencies. Exiting...
pause
exit /b 1

:INSTALL
echo.
echo Installing dependencies via pip...
python -m pip install --upgrade textual PyInstaller
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo [ERROR] Failed to install required Python packages.
    pause
    exit /b %ERRORLEVEL%
)

:COMPILE
echo.
echo [2/3] Compiling source.py into ModPackager.exe...
PyInstaller --onefile --console --name="MCMPES" source.py

if %ERRORLEVEL% NEQ 0 (
    echo.
    echo [ERROR] PyInstaller compilation failed! Check error output above.
    pause
    exit /b %ERRORLEVEL%
)

echo.
:: 3. Clean up build temporary files
echo [3/3] Cleaning up build temporary files...
if exist "build" rd /s /q "build"
if exist "ModPackager.spec" del /q "ModPackager.spec"

echo.
echo ===================================================
echo SUCCESS! Your executable was generated at:
echo %CD%\dist\ModPackager.exe
echo ===================================================
echo.
pause