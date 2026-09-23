# Установка WhisperWin (порт WhisperMac на Windows)
#
# Использование:
#   powershell -ExecutionPolicy Bypass -File scripts\setup_windows.ps1
#
# Что делает:
#   - создаёт venv в корне проекта;
#   - ставит зависимости из requirements-windows.txt;
#   - предзагружает локальную модель faster-whisper (можно отключить:
#     $env:WHISPERMAC_PRELOAD_MODEL = "0" перед запуском).

$ErrorActionPreference = "Stop"

$RootDir = Split-Path -Parent $PSScriptRoot
Set-Location $RootDir

Write-Host "=== WhisperWin - установка ===" -ForegroundColor Cyan
Write-Host ""

$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
    Write-Host "Python не найден в PATH. Установи Python 3.10+ с python.org (отметь 'Add to PATH')." -ForegroundColor Red
    exit 1
}

Write-Host "[1/3] Создаю venv..."
if (Test-Path "venv") {
    Remove-Item -Recurse -Force "venv"
}
python -m venv venv

$venvPython = Join-Path $RootDir "venv\Scripts\python.exe"

Write-Host "[2/3] Устанавливаю Python-зависимости..."
& $venvPython -m pip install --upgrade pip
& $venvPython -m pip install -r requirements-windows.txt

if ($env:WHISPERMAC_PRELOAD_MODEL -ne "0") {
    Write-Host "[3/3] Предзагружаю локальную модель faster-whisper (может занять время при первом запуске)..."
    $pyScript = @'
import os
import numpy as np
from faster_whisper import WhisperModel

try:
    import ctranslate2
    device = "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
except Exception:
    device = "cpu"

model_size = os.getenv("WHISPERMAC_MODEL") or ("large-v3" if device == "cuda" else "small")
compute_type = os.getenv("WHISPERMAC_COMPUTE_TYPE") or ("float16" if device == "cuda" else "int8")
print(f"  model={model_size} device={device} compute_type={compute_type}")

model = WhisperModel(model_size, device=device, compute_type=compute_type)
dummy = np.zeros(16000, dtype=np.float32)
list(model.transcribe(dummy, language=os.getenv("WHISPERMAC_LANGUAGE", "ru"))[0])
print("  model cache: ready")
'@
    $tmpScript = Join-Path $env:TEMP "whisperwin_preload_$PID.py"
    Set-Content -Path $tmpScript -Value $pyScript -Encoding utf8
    try {
        & $venvPython $tmpScript
    } finally {
        Remove-Item -Force $tmpScript -ErrorAction SilentlyContinue
    }
} else {
    Write-Host "[3/3] Пропускаю предзагрузку модели (WHISPERMAC_PRELOAD_MODEL=0)"
}

Write-Host ""
Write-Host "=== Готово! ===" -ForegroundColor Green
Write-Host ""
Write-Host "Запуск:"
Write-Host "  powershell -ExecutionPolicy Bypass -File scripts\launch_windows.ps1"
Write-Host ""
Write-Host "ВАЖНО:"
Write-Host "  - Если Windows спросит про доступ к микрофону — разреши в Settings > Privacy > Microphone."
Write-Host "  - Если запись/вставка не работает в конкретном приложении, запущенном 'от администратора',"
Write-Host "    запусти WhisperWin тоже от администратора (иначе Windows (UIPI) блокирует фокус/ввод"
Write-Host "    из непривилегированного процесса в привилегированный)."
