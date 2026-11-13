# Exécutable autonome IAMail pour Windows, AVEC l'agent Outlook (COM via pywin32).
# À lancer SUR Windows (build non croisé) :
#   powershell -ExecutionPolicy Bypass -File empaquetage\construire-windows.ps1
# Prérequis : Python 3 ; les dépendances sont installées automatiquement ci-dessous.
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..\compagnon")

Write-Host "• Installation des dépendances (pyinstaller, pywin32)…"
python -m pip install --quiet --upgrade pyinstaller pywin32

python -m PyInstaller --noconfirm --onefile --name iamail `
  --add-data "..\moteur;moteur" `
  --hidden-import collecteur_outlook `
  --hidden-import win32com --hidden-import win32com.client `
  --hidden-import win32timezone --hidden-import pythoncom --hidden-import pywintypes `
  iamail_compagnon.py

Write-Host "`n✅ Exécutable : $(Resolve-Path .\dist\iamail.exe)"
Write-Host "   Agent Outlook inclus. Active-le dans la config : \"collecteur\": \"outlook\""
