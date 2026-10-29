# Nova installer: copies Nova to %LOCALAPPDATA%\Programs\Nova, sets up its own Python
# environment, adds Start-menu shortcuts and an entry in "Apps & features".
# No administrator rights needed. Run again to update.
#   powershell -ExecutionPolicy Bypass -File install.ps1   (or double-click "Install Nova.bat")

$ErrorActionPreference = "Stop"
$Source = $PSScriptRoot
$Target = Join-Path $env:LOCALAPPDATA "Programs\Nova"
$Venv = Join-Path $Target ".venv"
$StartMenu = Join-Path ([Environment]::GetFolderPath("Programs")) "Nova"
$Ru = (Get-Culture).TwoLetterISOLanguageName -in @("ru", "uk", "be", "kk")

function Say($ru, $en) { if ($Ru) { Write-Host $ru } else { Write-Host $en } }
function Step($ru, $en) { Write-Host ""; if ($Ru) { Write-Host "==> $ru" -ForegroundColor Cyan } else { Write-Host "==> $en" -ForegroundColor Cyan } }
function Ask($ru, $en) {
    $q = if ($Ru) { "$ru [Д/н]" } else { "$en [Y/n]" }
    $a = Read-Host $q
    return ($a -eq "" -or $a -match "^(y|yes|д|да)$")
}
function Fail($ru, $en) {
    Write-Host ""; if ($Ru) { Write-Host $ru -ForegroundColor Red } else { Write-Host $en -ForegroundColor Red }
    Read-Host "Enter"; exit 1
}

Write-Host ""
Write-Host "  Nova — voice assistant for Windows" -ForegroundColor Green
Say "  Установка в $Target" "  Installing to $Target"

# --- Python ------------------------------------------------------------------------------
Step "Ищу Python 3.11 или новее" "Looking for Python 3.11 or newer"
function Find-Python {
    foreach ($cmd in @("py -3", "python")) {
        try {
            $parts = $cmd.Split(" ")
            $v = & $parts[0] $parts[1..9] -c "import sys; print('%d.%d' % sys.version_info[:2]); print(sys.executable)" 2>$null
            if ($LASTEXITCODE -eq 0 -and $v) {
                $ver = [version]($v[0]); if ($ver -ge [version]"3.11") { return $v[1] }
            }
        } catch {}
    }
    return $null
}
$Python = Find-Python
if (-not $Python) {
    Say "Python не найден." "Python wasn't found."
    if ((Get-Command winget -ErrorAction SilentlyContinue) -and (Ask "Установить Python 3.12 через winget?" "Install Python 3.12 with winget?")) {
        winget install --id Python.Python.3.12 -e --scope user --accept-package-agreements --accept-source-agreements
        $env:Path = [Environment]::GetEnvironmentVariable("Path", "User") + ";" + [Environment]::GetEnvironmentVariable("Path", "Machine")
        $Python = Find-Python
    }
    if (-not $Python) { Fail "Установите Python 3.11+ с python.org (галочка «Add to PATH») и запустите установщик снова." "Install Python 3.11+ from python.org (tick 'Add to PATH') and run the installer again." }
}
Say "Python: $Python" "Python: $Python"

# --- WebView2 (the window engine) ------------------------------------------------------
$wv = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
$hasWebView = @("HKLM:\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\$wv", "HKCU:\Software\Microsoft\EdgeUpdate\Clients\$wv",
                "HKLM:\SOFTWARE\Microsoft\EdgeUpdate\Clients\$wv") | Where-Object { Test-Path $_ }
if (-not $hasWebView) {
    Step "Нужен Microsoft Edge WebView2 Runtime" "Microsoft Edge WebView2 Runtime is needed"
    if ((Get-Command winget -ErrorAction SilentlyContinue) -and (Ask "Установить через winget?" "Install it with winget?")) {
        winget install --id Microsoft.EdgeWebView2Runtime -e --accept-package-agreements --accept-source-agreements
    } else {
        Say "Скачайте: https://developer.microsoft.com/microsoft-edge/webview2/" "Download it from https://developer.microsoft.com/microsoft-edge/webview2/"
    }
}

# --- Stop a running Nova, copy the files ------------------------------------------------
Step "Копирую файлы" "Copying files"
Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" |
    Where-Object { $_.CommandLine -match "-m nova" -and $_.ExecutablePath -like "$Target*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
New-Item -ItemType Directory -Force -Path $Target | Out-Null
foreach ($item in @("nova", "requirements.txt", "LICENSE", "README.md", "README.ru.md", "uninstall.ps1")) {
    $from = Join-Path $Source $item
    if (-not (Test-Path $from)) { Fail "Не найден $item рядом с установщиком." "$item not found next to the installer." }
    $to = Join-Path $Target $item
    if (Test-Path $to) { Remove-Item -Recurse -Force $to }
    Copy-Item -Recurse -Force $from $to
}
Get-ChildItem -Path (Join-Path $Target "nova") -Recurse -Directory -Filter "__pycache__" | Remove-Item -Recurse -Force

# --- Its own Python environment -------------------------------------------------------
Step "Ставлю библиотеки (несколько минут при первой установке)" "Installing libraries (a few minutes the first time)"
if (-not (Test-Path (Join-Path $Venv "Scripts\python.exe"))) { & $Python -m venv $Venv }
$VPython = Join-Path $Venv "Scripts\python.exe"
& $VPython -m pip install --upgrade pip --quiet --disable-pip-version-check
& $VPython -m pip install -r (Join-Path $Target "requirements.txt") --disable-pip-version-check
if ($LASTEXITCODE -ne 0) { Fail "Не удалось установить библиотеки. Проверьте интернет и запустите снова." "Installing the libraries failed. Check the internet connection and run again." }
if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) {
    Say "Найдена видеокарта NVIDIA: ставлю ускорение распознавания." "NVIDIA graphics found: installing faster recognition."
    & $VPython -m pip install nvidia-cublas-cu12 "nvidia-cudnn-cu12==9.*" --disable-pip-version-check
}

# --- Shortcuts ---------------------------------------------------------------------------
Step "Создаю ярлыки" "Creating shortcuts"
$Shell = New-Object -ComObject WScript.Shell
$Icon = Join-Path $Target "nova\web\nova.ico"
$PythonW = Join-Path $Venv "Scripts\pythonw.exe"
function Make-Link($path, $arguments, $description) {
    $l = $Shell.CreateShortcut($path)
    $l.TargetPath = $PythonW; $l.Arguments = $arguments; $l.WorkingDirectory = $Target
    $l.IconLocation = "$Icon,0"; $l.Description = $description; $l.Save()
}
New-Item -ItemType Directory -Force -Path $StartMenu | Out-Null
Make-Link (Join-Path $StartMenu "Nova.lnk") "-m nova" "Nova voice assistant"
$u = $Shell.CreateShortcut((Join-Path $StartMenu "Uninstall Nova.lnk"))
$u.TargetPath = "powershell.exe"; $u.Arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$Target\uninstall.ps1`""
$u.IconLocation = "$Icon,0"; $u.Save()
if (Ask "Ярлык на рабочем столе?" "Desktop shortcut?") {
    Make-Link (Join-Path ([Environment]::GetFolderPath("Desktop")) "Nova.lnk") "-m nova" "Nova voice assistant"
}
$Startup = Join-Path ([Environment]::GetFolderPath("Startup")) "Nova.lnk"
if (Ask "Запускать Nova вместе с Windows?" "Start Nova with Windows?") {
    Make-Link $Startup "-m nova" "Nova voice assistant"
} elseif (Test-Path $Startup) { Remove-Item $Startup }

# --- "Apps & features" entry ------------------------------------------------------------
$Key = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\Nova"
$Version = (Select-String -Path (Join-Path $Target "nova\__init__.py") -Pattern '__version__ = "(.+)"').Matches[0].Groups[1].Value
New-Item -Force -Path $Key | Out-Null
$props = @{ DisplayName = "Nova"; DisplayVersion = $Version; Publisher = "VadymStar"; DisplayIcon = $Icon
            InstallLocation = $Target; NoModify = 1; NoRepair = 1; URLInfoAbout = "https://github.com/vadym-star/nova"
            UninstallString = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$Target\uninstall.ps1`"" }
foreach ($k in $props.Keys) { Set-ItemProperty -Path $Key -Name $k -Value $props[$k] }

Write-Host ""
Say "Готово! Nova установлена." "Done! Nova is installed."
if (Ask "Запустить Nova сейчас?" "Start Nova now?") { Start-Process $PythonW -ArgumentList "-m nova" -WorkingDirectory $Target }
Say "Nova появится в трее (у часов). Правый клик → Настройки." "Nova appears in the tray (next to the clock). Right-click → Settings."
Read-Host "Enter"
