# Retire IAMail (démarrage auto + exécutable). Conserve config et archive.
$ErrorActionPreference = "SilentlyContinue"
Get-Process iamail -ErrorAction SilentlyContinue | Stop-Process -Force
$startup = [Environment]::GetFolderPath("Startup")
$progs   = [Environment]::GetFolderPath("Programs")
Remove-Item (Join-Path $startup "IAMail.lnk") -Force
Remove-Item (Join-Path $progs "Cockpit IAMail.lnk") -Force
$base = Join-Path $env:LOCALAPPDATA "IAMail"
Remove-Item (Join-Path $base "iamail.exe") -Force
Remove-Item (Join-Path $base "demarrer-cache.vbs") -Force
Write-Host "✅ IAMail désinstallé. Config conservée dans $base."
