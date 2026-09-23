# Запуск WhisperWin
#
# Использование:
#   powershell -ExecutionPolicy Bypass -File scripts\launch_windows.ps1
#
# Полезные переменные (задать перед запуском через $env:ИМЯ = "значение"):
#   WHISPERMAC_ENGINE          groq (по умолчанию, нужен VPN из РФ) | local
#   WHISPERMAC_MODEL           размер модели faster-whisper (small/medium/large-v3/...)
#   WHISPERMAC_DEVICE          auto (по умолчанию) | cpu | cuda
#   WHISPERMAC_HOLD_KEY        right_ctrl (по умолчанию) | right_alt | off
#   GROQ_API_KEY                ключ Groq, если используешь облачный движок

$ErrorActionPreference = "Stop"

$RootDir = Split-Path -Parent $PSScriptRoot
Set-Location $RootDir

$venvPython = Join-Path $RootDir "venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    Write-Host "Не найден venv. Сначала запусти scripts\setup_windows.ps1" -ForegroundColor Red
    exit 1
}

if (-not $env:WHISPERMAC_SAVE_TRANSCRIPTS) { $env:WHISPERMAC_SAVE_TRANSCRIPTS = "1" }
if (-not $env:WHISPERMAC_SAVE_PERF_LOG)    { $env:WHISPERMAC_SAVE_PERF_LOG = "1" }
if (-not $env:WHISPERMAC_CHUNK_SEC)        { $env:WHISPERMAC_CHUNK_SEC = "10" }
if (-not $env:WHISPERMAC_FINAL_PASS_MIN_SEC) { $env:WHISPERMAC_FINAL_PASS_MIN_SEC = "15" }
if (-not $env:WHISPERMAC_FINAL_PASS_MAX_SEC) { $env:WHISPERMAC_FINAL_PASS_MAX_SEC = "95" }
if (-not $env:WHISPERMAC_HOLD_KEY)         { $env:WHISPERMAC_HOLD_KEY = "right_ctrl" }
if (-not $env:HF_HUB_DISABLE_TELEMETRY)    { $env:HF_HUB_DISABLE_TELEMETRY = "1" }

$engineLabel = if ($env:WHISPERMAC_ENGINE) { $env:WHISPERMAC_ENGINE } else { "groq" }
Write-Host "WhisperWin: engine=$engineLabel hold_key=$($env:WHISPERMAC_HOLD_KEY)"

& $venvPython "$RootDir\whisper_win.py"
