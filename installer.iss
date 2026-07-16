; ===========================================================================
; XCS2SVG Converter Pro - script de Inno Setup
;
; Genera un instalador estandar de Windows (XCS2SVG_Converter_Setup.exe) a partir
; del ejecutable ya compilado con PyInstaller (dist\XCS2SVG_Converter.exe).
;
; Requisitos:
;   1. Haber ejecutado build.bat antes (debe existir dist\XCS2SVG_Converter.exe)
;   2. Tener instalado Inno Setup Compiler (gratuito): https://jrsoftware.org/isinfo.php
;
; Uso:
;   Abre este archivo con Inno Setup Compiler y pulsa "Compile" (Ctrl+F9),
;   o ejecutalo por linea de comandos:
;       "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer.iss
;
; Resultado: Output\XCS2SVG_Converter_Setup.exe
; ===========================================================================

#define MyAppName "XCS2SVG Converter"
#define MyAppVersion "1.0.0"
#define MyAppPublisher "XCS2SVG"
#define MyAppExeName "XCS2SVG_Converter.exe"

[Setup]
AppId={{8F3A2B1C-6D4E-4F9A-9C2B-1A2B3C4D5E6F}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=Output
OutputBaseFilename=XCS2SVG_Converter_Setup
Compression=lzma
SolidCompression=yes
WizardStyle=modern
SetupIconFile=icon.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "spanish"; MessagesFile: "compiler:Languages\Spanish.isl"

[Tasks]
Name: "desktopicon"; Description: "Crear un acceso directo en el Escritorio"; GroupDescription: "Accesos directos:"

[Files]
Source: "dist\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Desinstalar {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Abrir {#MyAppName}"; Flags: nowait postinstall skipifsilent
