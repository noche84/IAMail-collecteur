# Installe IAMail pour l'utilisateur courant sur Windows : exécutable + démarrage
# automatique CACHÉ (aucune fenêtre, aucun terminal) + raccourci cockpit.
# AUCUN droit administrateur requis.
#
#   powershell -ExecutionPolicy Bypass -File empaquetage\installer-windows.ps1 [chemin\iamail.exe]
#
# Sans argument, prend compagnon\dist\iamail.exe (sortie de construire-windows.ps1).
$ErrorActionPreference = "Stop"
$ici = $PSScriptRoot
$exe = if ($args.Count -ge 1) { $args[0] } else { Join-Path $ici "..\compagnon\dist\iamail.exe" }
if (-not (Test-Path $exe)) {
  Write-Error "Exécutable introuvable : $exe`nConstruis-le d'abord : construire-windows.ps1"; exit 1
}

$base = Join-Path $env:LOCALAPPDATA "IAMail"
New-Item -ItemType Directory -Force -Path $base | Out-Null
Copy-Item $exe (Join-Path $base "iamail.exe") -Force
Write-Host "• Exécutable installé : $base\iamail.exe"

# Config — créée une fois, à personnaliser (boîte, adresses, dossiers).
$cfg = Join-Path $base "config.json"
if (-not (Test-Path $cfg)) {
  Copy-Item (Join-Path $ici "..\compagnon\config.exemple.json") $cfg -Force
  Write-Host "• Config créée : $cfg  (à personnaliser)"
}

# Clé IA (facultative) : variable d'environnement utilisateur, persistante.
if ($env:IAMAIL_MISTRAL_KEY) {
  [Environment]::SetEnvironmentVariable("MISTRAL_API_KEY", $env:IAMAIL_MISTRAL_KEY, "User")
  Write-Host "• Clé Mistral enregistrée (variable utilisateur MISTRAL_API_KEY)"
}

# Lanceur CACHÉ : un .vbs qui démarre le service sans fenêtre.
$vbs = Join-Path $base "demarrer-cache.vbs"
@"
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = "$base"
sh.Run """$base\iamail.exe"" --config ""$cfg"" service", 0, False
"@ | Set-Content -Encoding ASCII $vbs

# Démarrage automatique à l'ouverture de session (dossier Démarrage, caché).
$startup = [Environment]::GetFolderPath("Startup")
$ws = New-Object -ComObject WScript.Shell
$lnk = $ws.CreateShortcut((Join-Path $startup "IAMail.lnk"))
$lnk.TargetPath = "wscript.exe"
$lnk.Arguments = """$vbs"""
$lnk.WindowStyle = 7                      # minimisé/caché
$lnk.Description = "IAMail — compagnon de tri des emails"
$lnk.Save()
Write-Host "• Démarrage automatique (caché) installé dans le dossier Démarrage"

# Raccourci « Cockpit IAMail » dans le menu Démarrer.
$progs = [Environment]::GetFolderPath("Programs")
$lnk2 = $ws.CreateShortcut((Join-Path $progs "Cockpit IAMail.lnk"))
$lnk2.TargetPath = "http://127.0.0.1:8740/"
$lnk2.Description = "Ouvrir le cockpit IAMail"
$lnk2.Save()

# Démarrer tout de suite, caché.
Start-Process wscript.exe -ArgumentList "`"$vbs`""
Write-Host "`n✅ Installé. Le compagnon tourne (caché) et démarrera à chaque ouverture de session."
Write-Host "   Cockpit : http://127.0.0.1:8740/  (menu Démarrer → « Cockpit IAMail »)"
Write-Host "   Note : sur Windows, la source des mails est aujourd'hui Thunderbird ;"
Write-Host "          pour Outlook classique, l'agent Outlook est le prochain jalon."
