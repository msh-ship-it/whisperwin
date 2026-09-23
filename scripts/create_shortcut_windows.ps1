# Создаёт ярлык WhisperWin на рабочем столе (с горячей клавишей запуска
# приложения — это НЕ hold-to-talk клавиша внутри приложения, это отдельная
# ОС-горячая клавиша "запустить программу", если она ещё не запущена).
#
# Использование:
#   powershell -ExecutionPolicy Bypass -File scripts\create_shortcut_windows.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\create_shortcut_windows.ps1 -AddToStartup
#
# -AddToStartup   дополнительно кладёт копию ярлыка в папку автозагрузки
#                 Windows — WhisperWin поднимется сам при каждом входе в
#                 систему (виджет появится в углу экрана, дальше работает
#                 hold-to-talk на Right Ctrl без какого-либо запуска руками).

param(
    [switch]$AddToStartup,
    [string]$HotkeyCombo = "CTRL+ALT+W"
)

$ErrorActionPreference = "Stop"
$RootDir = Split-Path -Parent $PSScriptRoot

$venvPythonw = Join-Path $RootDir "venv\Scripts\pythonw.exe"
if (-not (Test-Path $venvPythonw)) {
    Write-Host "Не найден venv (venv\Scripts\pythonw.exe). Сначала запусти scripts\setup_windows.ps1" -ForegroundColor Red
    exit 1
}

$iconPath = Join-Path $RootDir "icon.ico"
$scriptPath = Join-Path $RootDir "whisper_win.py"

function New-WhisperWinShortcut {
    param([string]$LinkPath)

    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($LinkPath)
    $shortcut.TargetPath = $venvPythonw
    $shortcut.Arguments = "`"$scriptPath`""
    $shortcut.WorkingDirectory = $RootDir
    if (Test-Path $iconPath) {
        $shortcut.IconLocation = $iconPath
    }
    $shortcut.Description = "WhisperWin — локальный голосовой ввод"
    $shortcut.Save()
}

$desktop = [Environment]::GetFolderPath("Desktop")
$desktopLink = Join-Path $desktop "WhisperWin.lnk"
New-WhisperWinShortcut -LinkPath $desktopLink

# Горячая клавиша запуска работает только у ярлыков на Рабочем столе или в
# Пуск — задаём её через COM-объект отдельно после сохранения.
try {
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($desktopLink)
    $shortcut.Hotkey = $HotkeyCombo
    $shortcut.Save()
    Write-Host "Ярлык на рабочем столе: $desktopLink (горячая клавиша: $HotkeyCombo)" -ForegroundColor Green
} catch {
    Write-Host "Ярлык создан ($desktopLink), но не удалось назначить горячую клавишу: $_" -ForegroundColor Yellow
}

if ($AddToStartup) {
    $startupDir = [Environment]::GetFolderPath("Startup")
    $startupLink = Join-Path $startupDir "WhisperWin.lnk"
    New-WhisperWinShortcut -LinkPath $startupLink
    Write-Host "Добавлено в автозагрузку: $startupLink" -ForegroundColor Green
    Write-Host "WhisperWin будет запускаться сам при каждом входе в Windows."
}

Write-Host ""
Write-Host "Готово. Дальше:"
Write-Host "  - Двойной клик по ярлыку на рабочем столе ИЛИ нажми $HotkeyCombo из любого места — запустится WhisperWin."
Write-Host "  - Внутри приложения: зажми Right Ctrl -> диктуй -> отпусти -> текст вставится."
Write-Host "  - Повторный запуск при уже работающем WhisperWin ничего не сломает (инстанс-лок молча завершит вторую копию)."
