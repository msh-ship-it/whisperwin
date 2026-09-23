# WhisperWin

Локальный голосовой ввод для Windows: зажал `Right Ctrl`, продиктовал,
отпустил — текст сам вставился в то окно, где стоял курсор.

## Быстрый старт

1. Скачай `WhisperWin-Setup.exe` из [Releases](https://github.com/msh-ship-it/whisperwin/releases).
2. Запусти — установка не требует прав администратора.
3. Введи ключ Groq, если есть ([console.groq.com/keys](https://console.groq.com/keys)) —
   можно и пропустить, тогда распознавание будет работать локально, просто
   медленнее. Ключ всегда можно добавить позже: правый клик по виджету или
   иконка в трее → «Настроить Groq-ключ...».
4. Поставь курсор в поле ввода, зажми `Right Ctrl`, говори, отпусти — текст
   вставится сам.

## Как это работает

Голос пишется потоково и распознаётся движком Groq (быстрый, облачный) либо
`faster-whisper` (локальный фоллбэк без сети, если ключа нет). Подробности
архитектуры, известные ограничения и переменные окружения — в
[`docs/WINDOWS_RU.md`](docs/WINDOWS_RU.md). Приватность и что куда
отправляется — в [`SECURITY.md`](SECURITY.md).

## Сборка из исходников

```powershell
powershell -ExecutionPolicy Bypass -File scripts\setup_windows.ps1   # venv + зависимости
powershell -ExecutionPolicy Bypass -File scripts\launch_windows.ps1  # запуск без сборки exe
```

Сборка `.exe` и установщика — см. [`docs/WINDOWS_RU.md`](docs/WINDOWS_RU.md#сборка-exe-и-инсталлятора).

## Платформы

Только Windows. 
