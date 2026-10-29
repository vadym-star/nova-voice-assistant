# Removes Nova installed by install.ps1. Asks before deleting settings, memory and models.

$ErrorActionPreference = "Continue"
$Target = Join-Path $env:LOCALAPPDATA "Programs\Nova"
$Data = Join-Path $env:APPDATA "Nova"
$Ru = (Get-Culture).TwoLetterISOLanguageName -in @("ru", "uk", "be", "kk")
function Say($ru, $en) { if ($Ru) { Write-Host $ru } else { Write-Host $en } }
function Ask($ru, $en) {
    $q = if ($Ru) { "$ru [д/Н]" } else { "$en [y/N]" }
    return ((Read-Host $q) -match "^(y|yes|д|да)$")
}

Write-Host ""
if (-not (Ask "Удалить Nova?" "Uninstall Nova?")) { exit 0 }

# Stop it first: files in use can't be deleted.
Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" |
    Where-Object { $_.CommandLine -match "-m nova" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 1

foreach ($link in @(
    (Join-Path ([Environment]::GetFolderPath("Programs")) "Nova"),
    (Join-Path ([Environment]::GetFolderPath("Desktop")) "Nova.lnk"),
    (Join-Path ([Environment]::GetFolderPath("Startup")) "Nova.lnk"))) {
    if (Test-Path $link) { Remove-Item -Recurse -Force $link }
}
Remove-Item -Force -Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\Nova" -ErrorAction SilentlyContinue

if (Test-Path $Data) {
    $size = [math]::Round(((Get-ChildItem $Data -Recurse -File -ErrorAction SilentlyContinue | Measure-Object Length -Sum).Sum) / 1MB)
    if (Ask "Удалить также настройки, память и модели речи ($size МБ, $Data)?" "Also delete settings, memory and speech models ($size MB, $Data)?") {
        Remove-Item -Recurse -Force $Data
    }
}

# The script itself lives in the folder being removed: delete it after this window closes.
Say "Nova удалена." "Nova has been uninstalled."
Start-Process cmd.exe -ArgumentList "/c timeout /t 2 >nul & rmdir /s /q `"$Target`"" -WindowStyle Hidden
Start-Sleep -Seconds 1
