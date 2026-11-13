# Exécutable autonome IAMail pour Windows. À lancer SUR Windows (build non croisé) :
#   powershell -ExecutionPolicy Bypass -File empaquetage\construire-windows.ps1
# Prérequis : Python 3 + "pip install pyinstaller".
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..\compagnon")
python -m PyInstaller --noconfirm --onefile --name iamail `
  --add-data "..\moteur;moteur" `
  iamail_compagnon.py
Write-Host "`n✅ Exécutable : $(Resolve-Path .\dist\iamail.exe)"
