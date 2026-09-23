# WhisperWin

Локальный голосовой ввод для Windows: зажал `Right Ctrl`, продиктовал, отпустил —
текст сам вставился в то окно, где стоял курсор.

Порт [WhisperMac](https://github.com/ranlywood/whispermac-local-case)
(автор оригинала: [t.me/ei_ai_channel](https://t.me/ei_ai_channel)) на Windows.

## Быстрый старт

1. Скачай `WhisperWin-Setup.exe` из [Releases](https://github.com/msh-ship-it/whisperwin/releases).
2. Запусти — установка не требует прав администратора.
3. При первом запуске появится окно с предложением ввести бесплатный
   Groq API-ключ ([console.groq.com/keys](https://console.groq.com/keys)) —
   можно нажать «Пропустить» и работать полностью локально; ключ всегда
   можно добавить позже (правый клик по виджету или иконка в трее →
   «Настроить Groq-ключ...»).
4. Зажми `Right Ctrl`, диктуй, отпусти — текст вставится в активное окно.

## Как это работает

- Голос пишется потоково (`sounddevice`), режется на куски и распознаётся
  по мере поступления — не нужно ждать окончания записи.
- Два движка распознавания:
  - **Groq API** (по умолчанию) — облачный, быстрый (`whisper-large-v3-turbo`).
  - **faster-whisper** — локальный фоллбэк на случай отсутствия ключа/сети
    (CPU или CUDA).
- Фильтры на тишину/галлюцинации и на характерное для Whisper "зацикливание"
  (повтор одной фразы по кругу).
- Текст вставляется через буфер обмена + синтетический `Ctrl+V` в окно,
  которое было активно перед началом записи.

Подробности архитектуры и известные ограничения — в [`docs/WINDOWS_RU.md`](docs/WINDOWS_RU.md).

## Приватность

- Ключи API нигде не хардкодятся — ключ Groq хранится локально в
  `%APPDATA%\WhisperWin\config.json`, вводится каждым пользователем сам.
- Без ключа Groq — работает полностью локально (`faster-whisper`), без сети.
- Логи (`~/whisper_log.txt`, `~/whisper_runtime.log`) можно отключить —
  см. [`docs/WINDOWS_RU.md`](docs/WINDOWS_RU.md).

Подробно: [`SECURITY.md`](SECURITY.md)

## Сборка из исходников

```powershell
powershell -ExecutionPolicy Bypass -File scripts\setup_windows.ps1   # venv + зависимости
powershell -ExecutionPolicy Bypass -File scripts\launch_windows.ps1  # запуск без сборки exe
```

Сборка `.exe` и установщика — см. [`docs/WINDOWS_RU.md`](docs/WINDOWS_RU.md#сборка-exe-и-инсталлятора).

## Платформы

- Windows: поддерживается (этот репозиторий).
- macOS: см. оригинал — [ranlywood/whispermac-local-case](https://github.com/ranlywood/whispermac-local-case).
- Linux: не поддерживается.
