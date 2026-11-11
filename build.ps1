$ErrorActionPreference = "Stop"

Set-Location $PSScriptRoot

Write-Host "Generating application icon..."
python tools\make_icon.py
if ($LASTEXITCODE -ne 0) { throw "Icon generation failed." }

Write-Host "Building PowerGuard.exe..."
python -m PyInstaller --noconfirm --clean --onefile --windowed --name "PowerGuard" --icon "assets\app.ico" --add-data "assets\app.ico;assets" --collect-all customtkinter --hidden-import pystray._win32 --version-file "version_info.txt" main.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed." }

Copy-Item "dist\PowerGuard.exe" "PowerGuard.exe" -Force
Write-Host ""
Write-Host "Done: $PSScriptRoot\PowerGuard.exe"
