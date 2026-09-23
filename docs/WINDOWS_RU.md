# WhisperWin — порт на Windows

`whisper_win.py` — адаптация `whisper_mac.py` под Windows. Логика чанкинга,
детектор "зацикливания" Whisper, фильтр тишины и Tkinter-виджет не изменились
(они не были завязаны на macOS). Заменён только слой интеграции с ОС.

## Установка для обычных пользователей (.exe, без Python)

1. Скачай `WhisperWin-Setup.exe` из [Releases](https://github.com/msh-ship-it/whisperwin/releases) этого репозитория.
2. Запусти — установка не требует прав администратора, ставится в
   `%LOCALAPPDATA%\Programs\WhisperWin`. Можно отметить «Создать значок на
   рабочем столе» и «Запускать при включении компьютера».
3. При первом запуске появится окно с предложением ввести Groq API-ключ
   (бесплатный, console.groq.com/keys) — можно нажать «Пропустить» и работать
   полностью локально, ключ всегда можно добавить позже: правый клик по
   плашке-виджету ИЛИ иконка в системном трее → «Настроить Groq-ключ...».
4. Дальше: зажал `Right Ctrl` → диктуешь → отпустил → текст вставился в то
   окно, где стоял курсор.

Ключ хранится в `%APPDATA%\WhisperWin\config.json`, в исходники/установщик не
попадает.

## Что заменено

| Было (macOS)                                   | Стало (Windows)                              |
|-------------------------------------------------|-----------------------------------------------|
| `mlx_whisper` (Apple Metal/MLX)                  | `faster-whisper` (CTranslate2, CPU или CUDA)   |
| `ApplicationServices` (AXUIElement)              | не используется — только буфер обмена + Ctrl+V |
| `Quartz` (`CGEventPost` для `Cmd+V`)             | `SendInput` через `ctypes` (`Ctrl+V`)          |
| `AppKit` (`NSWorkspace`, `NSPasteboard`)         | `pywin32` (`win32gui`/`win32process`) + `win32clipboard` |
| `fcntl.flock` (единственный инстанс)             | лок на localhost TCP-сокете (`127.0.0.1:47563`) |
| `afconvert` (сжатие аудио для Groq)              | `ffmpeg`, если найден в PATH; иначе обычный WAV |
| Hold-to-talk: `Right Option`                     | Hold-to-talk: `Right Ctrl` (см. ниже, почему)   |

## Почему `Right Ctrl`, а не `Right Alt`

На русской раскладке (и большинстве европейских) правый Alt — это `AltGr`,
он участвует в наборе спецсимволов. Если повесить hold-to-talk на него,
консфликтует с обычным набором текста. `Right Ctrl` в этом смысле безопаснее.
Переключить можно: `$env:WHISPERMAC_HOLD_KEY = "right_alt"` или `"off"`.

## Установка из исходников (для разработки)

```powershell
powershell -ExecutionPolicy Bypass -File scripts\setup_windows.ps1
```

Ставит venv, зависимости из `requirements-windows.txt`, предзагружает
локальную модель `faster-whisper` (размер модели по умолчанию — `small` на
CPU или `large-v3`, если найдена CUDA-видеокарта).

## Запуск из исходников

```powershell
powershell -ExecutionPolicy Bypass -File scripts\launch_windows.ps1
```

## Сборка .exe и инсталлятора

```powershell
.\venv\Scripts\python.exe -m pip install pyinstaller
.\venv\Scripts\python.exe -m PyInstaller WhisperWin.spec --noconfirm
```

Результат: `dist\WhisperWin\WhisperWin.exe` (onedir-сборка). `ffmpeg.exe`
ищется автоматически (PATH, затем стандартный путь winget-пакета
`Gyan.FFmpeg`); переопределить — `$env:WHISPERWIN_FFMPEG = "путь\to\ffmpeg.exe"`
перед сборкой. Без ffmpeg тоже соберётся — просто длинные записи для Groq
пойдут несжатым WAV вместо m4a.

Инсталлятор (нужен [Inno Setup 6](https://jrsoftware.org/isinfo.php),
`ISCC.exe`):

```powershell
& "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe" installer.iss
```

Результат: `installer_output\WhisperWin-Setup.exe` — ставится без прав
администратора в `%LOCALAPPDATA%\Programs\WhisperWin`, с опциональными
значком на рабочем столе и автозапуском (см. `installer.iss`).

## Важно: Groq и Россия

По умолчанию движок — `WHISPERMAC_ENGINE=groq` (облачный, самый быстрый).
Groq API сейчас отдаёт HTTP 403 на запросы из России без VPN. Если работаешь
без VPN — сразу выставляй:

```powershell
$env:WHISPERMAC_ENGINE = "local"
```

Тогда распознавание идёт полностью локально через `faster-whisper`, без
сети (можно дополнительно включить `WHISPERMAC_STRICT_LOCAL=1` после того,
как модель закэширована).

## Известные ограничения

- **UAC / приложения "от администратора".** Windows (UIPI) не даёт
  непривилегированному процессу передавать фокус и синтетический ввод в окно,
  запущенное от администратора. Если диктуешь в такое окно — запусти
  WhisperWin тоже от администратора.
- **Нет аналога macOS Accessibility API.** На маке был прямой AX-инсерт текста
  в поле ввода в обход буфера обмена; на Windows устойчивого кроссплатформенного
  эквивалента нет (UI Automation работает не во всех приложениях одинаково),
  поэтому используется единственный путь: скопировать в буфер обмена → перевести
  фокус на целевое окно → отправить `Ctrl+V` через `SendInput`. Если фокус
  увести не удалось — текст всё равно остаётся в буфере обмена, вставь вручную.
- **`ctypes`-структура `INPUT` для `SendInput` обязана включать все три
  варианта union'а (`ki`/`mi`/`hi`).** Если оставить только `KEYBDINPUT`,
  `ctypes.sizeof()` посчитает структуру на 8 байт короче настоящего
  `sizeof(INPUT)` (40 байт на x64) — `SendInput` молча возвращает 0 с
  `GetLastError()=87` (`ERROR_INVALID_PARAMETER`), и вставка нигде не
  срабатывает без единого исключения в логе. См. `_InputUnion` в
  `whisper_win.py` — грабли уже наступлены и задокументированы в коде.
- **`print()` в собранном windowed-exe (`console=False`) может уронить фоновый
  поток.** При обычном запуске двойным кликом `sys.stdout` — `None` (консоли
  нет вообще); при отладке через перенаправление вывода кодировка потока не
  всегда UTF-8. Оба случая роняли бы `log()` на первом же нелатинском символе
  (кириллица могла пройти, но `→` в cp1251 — нет), а исключение в фоновом
  `_groq_worker`/`_streaming_worker` тихо убивало поток до вставки текста.
  Поэтому `print()` внутри `log()` обёрнут в `try/except` — надёжный канал
  логов теперь только файл `~/whisper_runtime.log` (всегда пишется в UTF-8).
- **`faster-whisper` на CPU медленнее MLX на Apple Silicon.** Если есть
  NVIDIA-видеокарта с CUDA — `WHISPERMAC_DEVICE=cuda` (или `auto`, по умолчанию)
  даст заметно лучшую скорость на модели `large-v3`.
- Модель качается через Hugging Face Hub при первом запуске (те же переменные
  `HF_HUB_*`, что в оригинале).

## Переменные окружения (специфичные для Windows-порта)

- `WHISPERMAC_MODEL` — размер/repo модели faster-whisper (`tiny`/`small`/
  `medium`/`large-v3`/путь к CTranslate2-модели). По умолчанию: `small` на CPU,
  `large-v3` на CUDA.
- `WHISPERMAC_DEVICE` — `auto` (по умолчанию) / `cpu` / `cuda`.
- `WHISPERMAC_COMPUTE_TYPE` — тип вычислений CTranslate2 (`int8` на CPU,
  `float16` на CUDA по умолчанию).
- `WHISPERMAC_HOLD_KEY` — `right_ctrl` (по умолчанию) / `right_alt` / `off`.
- `WHISPERMAC_INSTANCE_PORT` — порт для лока единственного инстанса
  (по умолчанию `47563`).

Остальные переменные (`WHISPERMAC_ENGINE`, `WHISPERMAC_CHUNK_SEC`,
`WHISPERMAC_SAVE_TRANSCRIPTS`, `GROQ_API_KEY` и т.д.) работают так же, как в
оригинальном README.
