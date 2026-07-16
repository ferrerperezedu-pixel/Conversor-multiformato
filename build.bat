@echo off
REM ===========================================================================
REM XCS2SVG Converter Pro - script de compilacion (ejecutar en Windows)
REM
REM Requisitos previos: Python 3.10+ instalado y en el PATH.
REM
REM Uso:
REM     1. Abre una terminal (cmd o PowerShell) en esta carpeta.
REM     2. Ejecuta:  build.bat
REM     3. El ejecutable resultante aparecera en dist\XCS2SVG_Converter.exe
REM ===========================================================================

echo.
echo === XCS2SVG Converter Pro - Compilacion ===
echo.

python -m pip install --upgrade pip
if errorlevel 1 goto :error

pip install -r requirements.txt
if errorlevel 1 goto :error

echo.
echo Compilando con PyInstaller...
echo.

pyinstaller --noconfirm --onefile --windowed ^
    --name "XCS2SVG_Converter" ^
    --icon "icon.ico" ^
    --collect-data tkinterdnd2 ^
    --hidden-import pypdf ^
    gui.py

if errorlevel 1 goto :error

echo.
echo ===========================================================
echo  Compilacion terminada. Ejecutable en: dist\XCS2SVG_Converter.exe
echo  Siguiente paso: compila installer.iss con Inno Setup Compiler
echo  para generar XCS2SVG_Converter_Setup.exe
echo ===========================================================
pause
exit /b 0

:error
echo.
echo *** Ocurrio un error durante la compilacion. Revisa el mensaje de arriba. ***
pause
exit /b 1
