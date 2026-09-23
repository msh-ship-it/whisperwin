#!/usr/bin/env python3
"""
WhisperWin — голосовой ввод для Windows (streaming + real-time EQ)

Порт WhisperMac (https://github.com/ranlywood/whispermac-local-case) на Windows.
Что заменено относительно оригинала:
  - mlx_whisper (Apple Metal/MLX, только Apple Silicon) -> faster-whisper (CTranslate2, CPU/CUDA)
  - ApplicationServices/Quartz/AppKit (macOS Accessibility API)  -> pywin32 (win32gui/win32process) + pynput
  - fcntl-лок единственного инстанса                             -> лок на localhost TCP-сокете
  - afconvert (сжатие аудио для Groq)                             -> ffmpeg (опционально, если найден в PATH)
  - Cmd+V / AXUIElement вставка текста                            -> Ctrl+V через pynput в сфокусированное окно
  - hold-to-talk на Right Option                                  -> hold-to-talk на Right Ctrl (Right Alt на
    русской раскладке — это AltGr, конфликтует с набором спецсимволов, поэтому не используется по умолчанию)

Всё остальное (Tkinter UI, sounddevice, чанкинг, детектор "зацикливания" Whisper,
фильтр тишины/галлюцинаций) не завязано на ОС и перенесено как есть.
"""

import math
import os
import subprocess
import threading
import time
import sys
import atexit
import socket
import ctypes
import webbrowser
from pathlib import Path
import io
import wave
import shutil
import tempfile

import numpy as np
import sounddevice as sd
import tkinter as tk
from tkinter import messagebox

import user_config

try:
    import win32gui
    import win32process
    import win32con
    import win32api
except ImportError:
    print(
        "Не найден pywin32. Установи зависимости: pip install -r requirements-windows.txt",
        file=sys.stderr,
    )
    raise

try:
    import pystray
    from PIL import Image as _PILImage
except ImportError:
    pystray = None

try:
    from faster_whisper import WhisperModel
except ImportError:
    print(
        "Не найден faster-whisper. Установи зависимости: pip install -r requirements-windows.txt",
        file=sys.stderr,
    )
    raise


# ═══════════════════════════════════════════════════
def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


# Отключаем телеметрию Hugging Face по умолчанию (модель тянем через HF Hub).
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")

STRICT_LOCAL_MODE = _env_bool("WHISPERMAC_STRICT_LOCAL", False)
if STRICT_LOCAL_MODE:
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

# faster-whisper принимает: tiny/base/small/medium/large-v2/large-v3, либо repo/путь CTranslate2-модели.
# large-v3 на CPU — медленно. Если явно не задано, по умолчанию берём "small" на CPU и "large-v3" на CUDA.
MODEL_SIZE_ENV = os.getenv("WHISPERMAC_MODEL", "").strip()
LANGUAGE     = os.getenv("WHISPERMAC_LANGUAGE", "ru")
SAMPLE_RATE  = 16000
MIN_DURATION = 0.3
SAVE_TRANSCRIPTS = _env_bool("WHISPERMAC_SAVE_TRANSCRIPTS", True)
SAVE_PERF_LOG = _env_bool("WHISPERMAC_SAVE_PERF_LOG", True)

CHUNK_SEC    = max(5.0, _env_float("WHISPERMAC_CHUNK_SEC", 10.0))
WORKER_POLL_SEC = max(0.05, _env_float("WHISPERMAC_WORKER_POLL_SEC", 0.20))
FINAL_PASS_MIN_SEC = max(5.0, _env_float("WHISPERMAC_FINAL_PASS_MIN_SEC", 15.0))
FINAL_PASS_MAX_SEC = max(
    FINAL_PASS_MIN_SEC,
    _env_float("WHISPERMAC_FINAL_PASS_MAX_SEC", 95.0),
)
LOW_CONF_LOGPROB = _env_float("WHISPERMAC_LOW_CONF_LOGPROB", -1.15)
SILENCE_SKIP_NO_SPEECH = min(
    0.99,
    max(0.5, _env_float("WHISPERMAC_SILENCE_SKIP_NO_SPEECH", 0.83)),
)
SILENCE_SKIP_MAX_CHARS = int(max(8, _env_float("WHISPERMAC_SILENCE_SKIP_MAX_CHARS", 36)))
FINAL_TEMPERATURES = (0.0, 0.2, 0.4, 0.6)
HOTWORDS_PROMPT = "WhisperWin, Whisper Flow, Miro, Zoom, Claude Code, ChatGPT."

# ── Движок транскрипции ─────────────────────────────
# ENGINE=groq (по умолчанию) — быстрый облачный путь через Groq API,
# локальный faster-whisper остаётся фоллбэком при ошибке/отсутствии сети/ключа.
# ENGINE=local — только локальная модель.
#
# ВАЖНО (RU): Groq API сейчас блокирует запросы из России (HTTP 403) без VPN.
# Если ты в РФ без VPN — сразу ставь WHISPERMAC_ENGINE=local.
ENGINE       = os.getenv("WHISPERMAC_ENGINE", "groq").strip().lower()
GROQ_MODEL   = os.getenv("WHISPERMAC_GROQ_MODEL", "whisper-large-v3-turbo")
GROQ_TIMEOUT = max(3.0, _env_float("WHISPERMAC_GROQ_TIMEOUT", 120.0))
GROQ_CONNECT_TIMEOUT = max(3.0, _env_float("WHISPERMAC_GROQ_CONNECT_TIMEOUT", 10.0))
GROQ_API_URL = os.getenv(
    "WHISPERMAC_GROQ_URL",
    "https://api.groq.com/openai/v1/audio/transcriptions",
)


def _app_dir() -> Path:
    """Каталог с ассетами: рядом со скриптом в dev-режиме; внутри собранного
    PyInstaller-приложения — sys._MEIPASS (для onedir-сборок PyInstaller 6.x
    это папка _internal рядом с exe, а не сам exe-каталог)."""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
    return Path(__file__).resolve().parent


GROQ_KEYS_URL = "https://console.groq.com/keys"


def _load_groq_key() -> str:
    """Ключ Groq: сначала env, затем %APPDATA%\\WhisperWin\\config.json
    (тот же формат хранения, что у screen-recorder-win — задаётся через
    диалог настроек), затем legacy-файлы вне репозитория (для тех, кто
    ставил ключ до появления диалога настроек — при первом чтении
    переносится в config.json)."""
    key = os.getenv("GROQ_API_KEY", "").strip()
    if key:
        return key
    key = user_config.get("groq_api_key", "").strip()
    if key:
        return key
    for path in (Path.home() / ".whispermac_groq_key",
                 Path.home() / ".config" / "whispermac" / "groq_key"):
        try:
            if path.exists():
                legacy_key = path.read_text(encoding="utf-8").strip()
                if legacy_key:
                    user_config.set_value("groq_api_key", legacy_key)
                    return legacy_key
        except OSError:
            continue
    return ""


GROQ_API_KEY = _load_groq_key()


def set_groq_api_key(key: str) -> None:
    global GROQ_API_KEY
    GROQ_API_KEY = key.strip()
    user_config.set_value("groq_api_key", GROQ_API_KEY)


def _audio_to_wav_bytes(audio: np.ndarray, sample_rate: int = SAMPLE_RATE) -> bytes:
    """float32 [-1..1] → 16-bit PCM WAV в памяти (для отправки в Groq)."""
    clipped = np.clip(audio.astype(np.float32), -1.0, 1.0)
    pcm16 = (clipped * 32767.0).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm16.tobytes())
    return buf.getvalue()


def _encode_for_groq(audio: np.ndarray) -> tuple:
    """
    Готовит payload для Groq. Длинные записи в несжатом WAV грузятся долго
    и упираются в таймаут, поэтому сжимаем речь в AAC/m4a через ffmpeg —
    сначала пробуем bundled ffmpeg.exe (кладётся рядом при сборке
    PyInstaller, см. WhisperWin.spec), затем ffmpeg из PATH (на macOS для
    этого использовался afconvert — на Windows его нет, но обычный ffmpeg
    делает то же самое). При отсутствии ffmpeg или любой ошибке — шлём
    обычный WAV. Возвращает (filename, bytes, mime).
    """
    wav = _audio_to_wav_bytes(audio)
    bundled_ffmpeg = _app_dir() / "ffmpeg.exe"
    ffmpeg = str(bundled_ffmpeg) if bundled_ffmpeg.exists() else shutil.which("ffmpeg")
    if not ffmpeg:
        return "audio.wav", wav, "audio/wav"
    try:
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "in.wav"
            dst = Path(td) / "out.m4a"
            src.write_bytes(wav)
            proc = subprocess.run(
                [ffmpeg, "-y", "-i", str(src), "-c:a", "aac", "-b:a", "48k", str(dst)],
                capture_output=True, timeout=30,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            if proc.returncode == 0 and dst.exists() and dst.stat().st_size > 0:
                return "audio.m4a", dst.read_bytes(), "audio/mp4"
            log(f"[groq] ffmpeg rc={proc.returncode} — шлю WAV")
    except Exception as ex:  # noqa: BLE001
        log(f"[groq] сжатие не удалось ({ex}) — шлю WAV")
    return "audio.wav", wav, "audio/wav"


def groq_transcribe(audio: np.ndarray, *, prompt: str = "", api_key: str = "") -> str:
    """
    Одним запросом отправляет всю запись в Groq и возвращает текст.
    При любой ошибке возвращает "" — вызывающий код падает на локальный фоллбэк.
    """
    key = api_key or GROQ_API_KEY
    if not key:
        log("[groq] ключ не найден — фоллбэк на локальную модель")
        return ""
    try:
        import requests  # ленивый импорт: локальный режим не требует requests
    except Exception as ex:  # noqa: BLE001
        log(f"[groq] requests недоступен ({ex}) — фоллбэк")
        return ""
    try:
        fname, payload, mime = _encode_for_groq(audio)
        files = {"file": (fname, payload, mime)}
        data = {
            "model": GROQ_MODEL,
            "language": LANGUAGE,
            "response_format": "json",
            "temperature": "0",
        }
        if prompt:
            data["prompt"] = prompt
        started = time.perf_counter()
        resp = requests.post(
            GROQ_API_URL,
            headers={"Authorization": f"Bearer {key}"},
            files=files,
            data=data,
            timeout=(GROQ_CONNECT_TIMEOUT, GROQ_TIMEOUT),
        )
        elapsed = time.perf_counter() - started
        if resp.status_code != 200:
            log(f"[groq] HTTP {resp.status_code}: {resp.text[:160]} — фоллбэк")
            return ""
        text = (resp.json().get("text") or "").strip()
        audio_sec = len(audio) / SAMPLE_RATE
        kb = len(payload) / 1024
        log(f"[groq] {audio_sec:.1f}s аудио ({fname}, {kb:.0f}КБ) → {elapsed:.2f}s: {text}")
        return text
    except Exception as ex:  # noqa: BLE001
        log(f"[groq] ошибка запроса ({ex}) — фоллбэк на локальную модель")
        return ""


# ═══════════════════════════════════════════════════
# Локальный движок: faster-whisper (CTranslate2). Работает на CPU (int8) и
# на CUDA (float16), в отличие от mlx-whisper, который требует Apple Silicon.

_model = None
_model_lock = threading.Lock()


def _detect_device() -> str:
    try:
        import ctranslate2
        if ctranslate2.get_cuda_device_count() > 0:
            return "cuda"
    except Exception:
        pass
    return "cpu"


def _resolve_model_size(device: str) -> str:
    if MODEL_SIZE_ENV:
        return MODEL_SIZE_ENV
    return "large-v3" if device == "cuda" else "small"


def _get_model() -> WhisperModel:
    global _model
    if _model is not None:
        return _model
    with _model_lock:
        if _model is not None:
            return _model
        device = os.getenv("WHISPERMAC_DEVICE", "auto").strip().lower()
        if device == "auto":
            device = _detect_device()
        model_size = _resolve_model_size(device)
        compute_type = os.getenv(
            "WHISPERMAC_COMPUTE_TYPE",
            "float16" if device == "cuda" else "int8",
        ).strip()
        log(f"faster-whisper: model={model_size} device={device} compute_type={compute_type}")
        _model = WhisperModel(model_size, device=device, compute_type=compute_type)
        return _model


def _local_transcribe(
    audio: np.ndarray,
    *,
    prompt=None,
    final=False,
    condition_on_previous_text=True,
    temperature=None,
) -> dict:
    """Обёртка над faster-whisper, приводящая результат к виду словаря
    {"text": str, "segments": [{"avg_logprob":.., "no_speech_prob":..}, ...]}
    — том же формате, что был у mlx_whisper.transcribe(), чтобы вся логика
    качества/тишины/loop-детекции в App осталась без изменений."""
    model = _get_model()
    temp = temperature if temperature is not None else (list(FINAL_TEMPERATURES) if final else 0.0)
    segments_gen, _info = model.transcribe(
        audio,
        language=LANGUAGE,
        initial_prompt=prompt,
        condition_on_previous_text=condition_on_previous_text,
        temperature=temp,
        vad_filter=False,
    )
    texts = []
    segments = []
    for seg in segments_gen:
        texts.append(seg.text)
        segments.append({
            "avg_logprob": float(seg.avg_logprob),
            "no_speech_prob": float(seg.no_speech_prob),
        })
    return {"text": "".join(texts).strip(), "segments": segments}


# ═══════════════════════════════════════════════════

W, H   = 228, 52
RADIUS = H // 2
MIC_X = 22

BG         = "#0D0D0F"
PILL       = "#1C1C1E"
C_IDLE     = "#3A3A3C"
C_REC      = "#FF375F"
C_PROC     = "#FF9F0A"
C_MIC_BG     = "#2C2C2E"   # круг микрофона в покое
C_MIC_BG_ON  = "#FF375F"   # круг микрофона при записи
C_MIC_SYM    = "#D7DBE2"   # символ микрофона в покое
C_MIC_SYM_ON = "#FF375F"   # символ микрофона при записи
C_CLOSE_BG   = "#2C2C2E"
C_CLOSE_HV   = "#3A3A3C"
C_CLOSE_X    = "#8E8E93"
C_LOG_BG     = "#2C2C2E"
C_LOG_HV     = "#3A3A3C"
C_LOG_X      = "#8E8E93"

BAR_COUNT = 7
BAR_W     = 3
BAR_STEP  = 7
BARS_X    = 74
BAR_MIN   = 2.0
BAR_MAX   = 18.0

# EQ smoothing
EQ_ATTACK        = 0.86
EQ_DECAY         = 0.28
EQ_RMS_ALPHA     = 0.42
EQ_VISUAL_GAMMA  = 0.62
# Адаптивный шумовой пол: раньше "тишина" была захардкожена абсолютным
# уровнем громкости (EQ_RMS_THRESHOLD=0.0038), откалиброванным под конкретный
# микрофон/громкость исходного разработчика — на других микрофонах/дистанциях
# индикатор просто не реагировал (нужно было подносить микрофон к лицу).
# Вместо этого отслеживаем реальный уровень тишины в комнате и считаем
# порог/"полную громкость" относительно него — работает на любом микрофоне.
EQ_NOISE_FLOOR_MIN  = 0.0006  # абсолютный пол на случай выключенного/заглушённого мика
EQ_NOISE_ADAPT_DOWN = 0.06    # как быстро "пол тишины" опускается к тихим моментам
EQ_NOISE_ADAPT_UP   = 0.0006  # как быстро он ползёт вверх (медленно, не даёт голосу "стать тишиной")
EQ_GATE_MULT        = 2.2     # во сколько раз громче пола тишины = "есть голос"
EQ_FULL_MULT        = 16.0    # во сколько раз громче пола тишины = бары "полные"
EQ_WOBBLE_MAX    = 0.16

INSTANCE_PORT = int(os.getenv("WHISPERMAC_INSTANCE_PORT", "47563"))


def log(msg):
    line = f"  {msg}"
    try:
        # В собранном windowed-exe (console=False) sys.stdout при обычном
        # запуске двойным кликом — None (нет консоли вообще), а если и есть
        # (например, перенаправлен в файл при отладке), его кодировка не
        # всегда UTF-8 — оба случая иначе валили бы приложение на первом же
        # логе с не-ASCII символом (кириллица, "→" и т.п.). print() здесь —
        # best-effort для разработки; надёжный лог — файл ниже (всегда UTF-8).
        if sys.stdout is not None:
            print(line, flush=True)
    except Exception:
        pass
    if not _env_bool("WHISPERMAC_RUNTIME_LOG", True):
        return
    try:
        from datetime import datetime
        with open(Path.home() / "whisper_runtime.log", "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}\n")
    except Exception:
        pass


# ── Единственный инстанс: лок на localhost TCP-сокете ───────────
# Замена fcntl.flock (POSIX-only) — просто занимаем порт на 127.0.0.1.
# Если порт уже занят, значит приложение уже запущено.
_instance_socket = None


def acquire_instance_lock():
    global _instance_socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
    try:
        s.bind(("127.0.0.1", INSTANCE_PORT))
        s.listen(1)
    except OSError:
        s.close()
        return None
    _instance_socket = s
    return s


def _release_instance_lock():
    global _instance_socket
    if _instance_socket is not None:
        try:
            _instance_socket.close()
        except Exception:
            pass
        _instance_socket = None


# ── Фокус/окна (замена NSWorkspace/AppKit) ───────────────────────
def frontmost_hwnd():
    try:
        hwnd = win32gui.GetForegroundWindow()
        return hwnd or None
    except Exception:
        return None


def process_name_for_hwnd(hwnd):
    """Имя exe-файла процесса, которому принадлежит окно (напр. 'chrome.exe')."""
    if not hwnd:
        return None
    try:
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        handle = win32api.OpenProcess(
            win32con.PROCESS_QUERY_INFORMATION | win32con.PROCESS_VM_READ, False, pid
        )
        try:
            path = win32process.GetModuleFileNameEx(handle, 0)
            return os.path.basename(path).lower()
        finally:
            win32api.CloseHandle(handle)
    except Exception:
        return None


def force_foreground(hwnd) -> bool:
    """Пытается поставить окно на передний план и передать ему фокус.

    Windows по умолчанию блокирует SetForegroundWindow из фоновых процессов
    (защита от кражи фокуса). Обходим классическим трюком: эмулируем нажатие
    Alt перед вызовом — это снимает ограничение для текущего процесса.
    Не сработает для окон, запущенных с более высокими правами (UAC/admin),
    если сам WhisperWin запущен без повышения — это ограничение Windows
    (UIPI), не баг скрипта."""
    if not hwnd or not win32gui.IsWindow(hwnd):
        return False
    try:
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    except Exception:
        pass
    try:
        win32gui.SetForegroundWindow(hwnd)
        if win32gui.GetForegroundWindow() == hwnd:
            return True
    except Exception:
        pass
    try:
        user32 = ctypes.windll.user32
        user32.keybd_event(0x12, 0, 0, 0)  # ALT down
        win32gui.SetForegroundWindow(hwnd)
        user32.keybd_event(0x12, 0, 2, 0)  # ALT up
        return win32gui.GetForegroundWindow() == hwnd
    except Exception as ex:
        log(f"SetForegroundWindow workaround failed: {ex}")
        return False


# ── Синтетический Ctrl+V через WinAPI SendInput ──────────────────
# Прямой SendInput (а не pynput/keybd_event) — самый низкоуровневый и
# надёжный способ инъекции клавиш на Windows; именно его используют macro-
# и accessibility-инструменты, когда keybd_event или сторонние обёртки
# ненадёжны для конкретных приложений (Electron/Chromium, игры и т.п.).

_PUL = ctypes.POINTER(ctypes.c_ulong)


class _KeyBdInput(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.c_ushort),
        ("wScan", ctypes.c_ushort),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", _PUL),
    ]


class _MouseInput(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_long),
        ("dy", ctypes.c_long),
        ("mouseData", ctypes.c_ulong),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", _PUL),
    ]


class _HardwareInput(ctypes.Structure):
    _fields_ = [
        ("uMsg", ctypes.c_ulong),
        ("wParamL", ctypes.c_short),
        ("wParamH", ctypes.c_ushort),
    ]


class _InputUnion(ctypes.Union):
    # ВАЖНО: union должен содержать все три варианта (ki/mi/hi), а не только
    # ki. SendInput проверяет cbSize против настоящего sizeof(INPUT) из
    # user32.dll (40 байт на x64) — если union содержит только KEYBDINPUT,
    # ctypes.sizeof() посчитает структуру на 8 байт короче (32), cbSize не
    # совпадёт с ожиданием user32.dll, и вызов будет молча отклонён с
    # GetLastError()=87 (ERROR_INVALID_PARAMETER) — именно так и было здесь.
    _fields_ = [("ki", _KeyBdInput), ("mi", _MouseInput), ("hi", _HardwareInput)]


class _Input(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong), ("union", _InputUnion)]


_INPUT_KEYBOARD = 1
_KEYEVENTF_KEYUP = 0x0002
_VK_CONTROL = 0x11
_VK_V = 0x56


def _make_key_input(vk: int, key_up: bool = False) -> _Input:
    extra = ctypes.c_ulong(0)
    flags = _KEYEVENTF_KEYUP if key_up else 0
    ki = _KeyBdInput(vk, 0, flags, 0, ctypes.pointer(extra))
    return _Input(_INPUT_KEYBOARD, _InputUnion(ki=ki))


_user32 = ctypes.WinDLL("user32", use_last_error=True)


def send_ctrl_v() -> bool:
    """Синтетический Ctrl+V через SendInput (замена Quartz CGEventPost)."""
    try:
        sequence = (_Input * 4)(
            _make_key_input(_VK_CONTROL),
            _make_key_input(_VK_V),
            _make_key_input(_VK_V, key_up=True),
            _make_key_input(_VK_CONTROL, key_up=True),
        )
        ctypes.set_last_error(0)
        sent = _user32.SendInput(4, ctypes.byref(sequence), ctypes.sizeof(_Input))
        if sent != 4:
            err = ctypes.get_last_error()
            log(
                f"SendInput отправил {sent}/4 событий (ожидалось 4), "
                f"GetLastError={err} ({ctypes.FormatError(err)})"
            )
        return sent == 4
    except Exception as ex:
        log(f"SendInput Ctrl+V failed: {ex}")
        return False


def open_mic_privacy_settings():
    try:
        os.startfile("ms-settings:privacy-microphone")
    except Exception as ex:
        log(f"Не удалось открыть настройки микрофона: {ex}")


def _clean_chunk(text: str) -> str:
    """Убирает артефакты Whisper на границах чанков."""
    import re
    text = text.strip()
    text = re.sub(r'^[\s\.…]+', '', text)
    text = re.sub(r'[\s\.…]+$', '', text)
    return text.strip()


def _join_chunks(parts: list) -> str:
    cleaned = [_clean_chunk(p) for p in parts]
    cleaned = [p for p in cleaned if p]
    return " ".join(cleaned)


def _prompt_from_parts(parts: list) -> str:
    """Короткий prompt для смешанной русско-английской речи."""
    tail = _join_chunks(parts)[-180:] if parts else ""
    return f"{HOTWORDS_PROMPT}\n{tail}" if tail else HOTWORDS_PROMPT


def _is_repetition_loop(text: str) -> bool:
    """Детектирует типичный whisper-loop c многократным повтором одной фразы."""
    import re
    words = re.findall(r"[\w$]+", text.lower())
    if len(words) < 12:
        return False

    def max_consecutive_repeat(n: int) -> int:
        best = 1
        i = 0
        end = len(words) - 2 * n
        while i <= end:
            gram = words[i:i+n]
            j = i + n
            run = 1
            while j + n <= len(words) and words[j:j+n] == gram:
                run += 1
                j += n
            if run > best:
                best = run
            i = i + 1 if run == 1 else j
        return best

    for n in (2, 3):
        if max_consecutive_repeat(n) >= 5:
            return True

    def max_ngram_count(n: int) -> tuple:
        from collections import Counter
        grams = [tuple(words[i:i+n]) for i in range(len(words) - n + 1)]
        if not grams:
            return (), 0
        gram, count = Counter(grams).most_common(1)[0]
        return gram, count

    for n in (2, 3):
        gram, count = max_ngram_count(n)
        if count >= 10 and (count * n) / max(1, len(words)) >= 0.08:
            return True

    m = re.search(r"(\b[\w$]+(?:\s+[\w$]+){0,2}\b)(?:[\s,.;:!?-]+\1){6,}", text.lower())
    if m:
        return True

    zero_dollars = sum(1 for w in words if w == "$0" or w.endswith("$0"))
    if zero_dollars >= 6 and zero_dollars / len(words) >= 0.04:
        return True

    return False


def _collapse_repetition_loop(text: str) -> str:
    """Последняя защита: схлопывает подряд идущие повторы короткой фразы."""
    import re
    prev = None
    cur = text
    pattern = re.compile(
        r"(\b[\w$]+(?:\s+[\w$]+){0,2}\b)(?:[\s,.;:!?-]+\1){3,}",
        re.IGNORECASE,
    )
    while prev != cur:
        prev = cur
        cur = pattern.sub(r"\1", cur)
    return cur


def _segment_quality(result: dict) -> tuple:
    segments = result.get("segments") or []
    if not segments:
        return 0.0, 0.0
    avg_logprob = sum(float(s.get("avg_logprob", 0.0)) for s in segments) / len(segments)
    avg_no_speech = sum(float(s.get("no_speech_prob", 0.0)) for s in segments) / len(segments)
    return avg_logprob, avg_no_speech


def _likely_silence_hallucination(text: str, avg_no_speech: float) -> bool:
    if not text:
        return False
    return avg_no_speech >= SILENCE_SKIP_NO_SPEECH and len(text.strip()) <= SILENCE_SKIP_MAX_CHARS


def pill_points(x1, y1, x2, y2, r):
    return [
        x1+r, y1,   x2-r, y1,
        x2,   y1,   x2,   y1+r,
        x2,   y2-r, x2,   y2,
        x2-r, y2,   x1+r, y2,
        x1,   y2,   x1,   y2-r,
        x1,   y1+r, x1,   y1,
    ]


class App:
    EXCLUDED = {"whisperwin.exe", "python.exe", "pythonw.exe", "explorer.exe"}

    def __init__(self):
        self.ready      = False
        self.recording  = False
        self.processing = False
        self.chunks     = []
        self._chunks_lock = threading.Lock()
        self.stream     = None
        self.target_hwnd = None
        self._recording_started_at = None
        self._frame     = 0
        self._drag_ox   = 0
        self._drag_oy   = 0
        self._dragging  = False
        self._hold_key_down = False
        self._hold_started_recording = False
        self._keyboard_listener = None
        self._keyboard_mod = None
        self._hold_key_mode = self._normalize_hold_key_mode(
            os.getenv("WHISPERMAC_HOLD_KEY", "right_ctrl")
        )

        self._eq_levels = np.zeros(BAR_COUNT, dtype=np.float32)
        self._eq_smooth = np.zeros(BAR_COUNT, dtype=np.float32)
        self._rms_smooth = 0.0
        self._noise_floor = EQ_NOISE_FLOOR_MIN

        self._mic_photo_idle   = None
        self._mic_photo_active = None
        self._mic_item         = None
        self._logs_win         = None
        self._logs_list        = None
        self._logs_text        = None
        self._log_records      = []
        self._logs_bounds      = (0, 0, 0, 0)
        self._close_bounds     = (0, 0, 0, 0)
        self._suppress_next_toggle = False
        self._tray_icon        = None

        # ── Окно ────────────────────────────────────────────────
        self.root = tk.Tk()
        self.root.overrideredirect(True)
        self.root.protocol("WM_DELETE_WINDOW", self._quit)
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", 0.96)
        self.root.configure(bg=BG)
        self._load_mic_images()

        sx = self.root.winfo_screenwidth()  - W - 28
        sy = self.root.winfo_screenheight() - H - 96
        self.root.geometry(f"{W}x{H}+{sx}+{sy}")

        # ── Canvas ───────────────────────────────────────────────
        self.cv = tk.Canvas(self.root, width=W, height=H,
                            bg=BG, highlightthickness=0)
        self.cv.pack()

        self.cv.create_polygon(
            pill_points(0, 0, W, H, RADIUS),
            smooth=True, fill=PILL, outline=""
        )

        self._draw_mic(recording=False)

        self.spinner = self.cv.create_text(
            28, H // 2, text="◌", font=("Helvetica", 22), fill="#636366"
        )
        self.cv.itemconfig("mic", state="hidden")

        cy = H // 2
        self.bars = []
        for i in range(BAR_COUNT):
            x = BARS_X + i * BAR_STEP
            b = self.cv.create_rectangle(
                x, cy - BAR_MIN, x + BAR_W, cy + BAR_MIN,
                fill=C_IDLE, outline="", tags="bar"
            )
            self.bars.append((b, x))

        self._draw_close()
        self._draw_logs_button()

        # ── Биндинги ────────────────────────────────────────────
        self.cv.tag_bind("close", "<Button-1>", self._quit)
        self.cv.tag_bind("close", "<Enter>",
                         lambda e: self.cv.itemconfig(self._close_bg,
                                                      fill=C_CLOSE_HV))
        self.cv.tag_bind("close", "<Leave>",
                         lambda e: self.cv.itemconfig(self._close_bg,
                                                      fill=C_CLOSE_BG))
        self.cv.tag_bind("logs", "<Button-1>", self._toggle_logs_window)
        self.cv.tag_bind("logs", "<ButtonRelease-1>", lambda _e: "break")
        self.cv.tag_bind("logs", "<Enter>",
                         lambda e: self.cv.itemconfig(self._logs_bg, fill=C_LOG_HV))
        self.cv.tag_bind("logs", "<Leave>",
                         lambda e: self.cv.itemconfig(self._logs_bg, fill=C_LOG_BG))

        self.cv.bind("<ButtonPress-1>",  self._press)
        self.cv.bind("<B1-Motion>",       self._motion)
        self.cv.bind("<ButtonRelease-1>", self._release)
        self.cv.bind("<Button-3>", self._open_settings_dialog)
        self.root.bind_all("<Control-Shift-E>", self._toggle_logs_window)
        self.root.bind_all("<Control-Shift-H>", self._toggle_logs_window)

        self.root.bind("<Destroy>", self._on_destroy)

        self._setup_hold_key_listener()
        self._setup_tray_icon()
        self._track_app()
        self._tick()
        if not GROQ_API_KEY:
            self.root.after(400, self._prompt_groq_key_then_load)
        else:
            threading.Thread(target=self._load_model, daemon=True).start()

    def _normalize_hold_key_mode(self, raw: str) -> str:
        mode = (raw or "").strip().lower()
        if mode in {"right_ctrl", "ctrl_r", "control_r"}:
            return "right_ctrl"
        if mode in {"right_alt", "alt_r", "altgr"}:
            return "right_alt"
        return "off"

    def _is_excluded_process(self, process_name: str) -> bool:
        name = (process_name or "").lower()
        return not name or any(ex in name for ex in self.EXCLUDED)

    def _setup_hold_key_listener(self):
        if self._hold_key_mode == "off":
            log("Hold-to-talk: off")
            return
        try:
            from pynput import keyboard
        except Exception as ex:
            log(f"Hold-to-talk отключен: не удалось загрузить pynput ({ex})")
            self._hold_key_mode = "off"
            return

        self._keyboard_mod = keyboard
        self._keyboard_listener = keyboard.Listener(
            on_press=self._on_global_key_press,
            on_release=self._on_global_key_release,
        )
        self._keyboard_listener.daemon = True
        self._keyboard_listener.start()
        key_label = "right ctrl" if self._hold_key_mode == "right_ctrl" else "right alt"
        log(f"Hold-to-talk: {key_label} (нажал -> запись, отпустил -> вставка)")

    def _on_destroy(self, event):
        if event.widget is self.root:
            self._close_logs_window()
            try:
                if self._keyboard_listener:
                    self._keyboard_listener.stop()
            except Exception:
                pass

    def _quit(self, _event=None):
        self.recording = False
        self.processing = False
        try:
            if self.stream:
                self.stream.stop()
                self.stream.close()
                self.stream = None
        except Exception as ex:
            log(f"Stream close during quit failed: {ex}")
        try:
            if self._keyboard_listener:
                self._keyboard_listener.stop()
        except Exception as ex:
            log(f"Keyboard listener stop during quit failed: {ex}")
        try:
            if self._tray_icon:
                self._tray_icon.stop()
        except Exception as ex:
            log(f"Tray icon stop during quit failed: {ex}")
        self._close_logs_window()
        try:
            self.root.quit()
            self.root.destroy()
        except Exception:
            pass
        os._exit(0)
        return "break"

    # ── Трей-иконка и настройка Groq-ключа ───────────────────────
    def _setup_tray_icon(self):
        if pystray is None:
            log("pystray не найден — трей-иконка недоступна (доступен только виджет)")
            return
        try:
            icon_path = _app_dir() / "icon.ico"
            if not icon_path.exists():
                icon_path = _app_dir() / "icon.png"
            image = (
                _PILImage.open(icon_path)
                if icon_path.exists()
                else _PILImage.new("RGBA", (64, 64), (200, 40, 60, 255))
            )
        except Exception as ex:
            log(f"Не удалось загрузить иконку трея: {ex}")
            image = _PILImage.new("RGBA", (64, 64), (200, 40, 60, 255))

        menu = pystray.Menu(
            pystray.MenuItem(
                "Настроить Groq-ключ...",
                lambda icon, item: self.root.after(0, self._open_settings_dialog),
            ),
            pystray.MenuItem(
                "Открыть лог расшифровок",
                lambda icon, item: self.root.after(0, self._open_logs_file),
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "Показать/скрыть виджет",
                lambda icon, item: self.root.after(0, self._toggle_widget_visibility),
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Выход", lambda icon, item: self.root.after(0, self._quit)),
        )
        try:
            self._tray_icon = pystray.Icon("whisperwin", image, "WhisperWin", menu)
            threading.Thread(target=self._tray_icon.run, daemon=True).start()
        except Exception as ex:
            log(f"Не удалось запустить трей-иконку: {ex}")
            self._tray_icon = None

    def _toggle_widget_visibility(self):
        try:
            if self.root.state() == "withdrawn":
                self.root.deiconify()
            else:
                self.root.withdraw()
        except Exception as ex:
            log(f"Toggle widget visibility failed: {ex}")

    def _prompt_groq_key_then_load(self):
        self._open_settings_dialog(
            on_close=lambda: threading.Thread(target=self._load_model, daemon=True).start()
        )

    def _open_settings_dialog(self, _event=None, on_close=None):
        window = tk.Toplevel(self.root)
        window.title("Настройка Groq-ключа")
        window.resizable(False, False)
        window.attributes("-topmost", True)

        padding = {"padx": 16, "pady": 6}

        tk.Label(
            window,
            justify="left",
            wraplength=380,
            text=(
                "Чтобы распознавание речи работало быстро (облачный Groq), "
                "нужен бесплатный API-ключ:\n\n"
                "1. Открой console.groq.com/keys и войди (можно через Google).\n"
                "2. Нажми «Create API Key», скопируй ключ (начинается с gsk_...).\n"
                "3. Вставь его в поле ниже и нажми «Сохранить».\n\n"
                "Без ключа приложение всё равно работает: распознаёт голос "
                "полностью локально на этом компьютере, но медленнее."
            ),
        ).pack(**padding)

        tk.Button(
            window,
            text="Открыть console.groq.com/keys",
            command=lambda: webbrowser.open(GROQ_KEYS_URL),
        ).pack(**padding)

        key_var = tk.StringVar(value=GROQ_API_KEY)
        entry = tk.Entry(window, textvariable=key_var, width=50)
        entry.pack(**padding)

        def paste_into_entry(event=None):
            try:
                text = window.clipboard_get()
            except tk.TclError:
                return "break"
            try:
                entry.delete("sel.first", "sel.last")
            except tk.TclError:
                pass
            entry.insert("insert", text)
            return "break"

        context_menu = tk.Menu(entry, tearoff=0)
        context_menu.add_command(label="Вставить", command=paste_into_entry)
        context_menu.add_command(label="Копировать", command=lambda: entry.event_generate("<<Copy>>"))
        context_menu.add_command(label="Вырезать", command=lambda: entry.event_generate("<<Cut>>"))
        entry.bind("<Button-3>", lambda e: context_menu.tk_popup(e.x_root, e.y_root))

        # На Windows Ctrl+<буква> матчится по keysym, который зависит от активной
        # раскладки — с кириллической раскладкой Ctrl+V/A/C/X молча не срабатывают.
        # Матчим по физическому keycode (не зависит от раскладки).
        def handle_ctrl_key(event):
            if event.keycode == 86:  # 'V'
                return paste_into_entry()
            if event.keycode == 65:  # 'A' — выделить всё
                entry.select_range(0, tk.END)
                entry.icursor(tk.END)
                return "break"
            if event.keycode == 67:  # 'C'
                entry.event_generate("<<Copy>>")
                return "break"
            if event.keycode == 88:  # 'X'
                entry.event_generate("<<Cut>>")
                return "break"

        entry.bind("<Control-Key>", handle_ctrl_key)

        button_row = tk.Frame(window)
        button_row.pack(pady=(0, 12))

        def on_save():
            key = key_var.get().strip()
            if not key:
                messagebox.showwarning(
                    "Пустой ключ", "Вставь ключ или нажми «Пропустить».", parent=window
                )
                return
            set_groq_api_key(key)
            log("Groq-ключ сохранён")
            window.destroy()

        def on_skip():
            window.destroy()

        tk.Button(button_row, text="Сохранить", command=on_save, width=12).pack(side="left", padx=6)
        tk.Button(
            button_row, text="Пропустить (работать локально)", command=on_skip, width=28
        ).pack(side="left", padx=6)

        window.protocol("WM_DELETE_WINDOW", on_skip)

        closed = {"done": False}

        def _on_window_destroy(event):
            if event.widget is window and not closed["done"]:
                closed["done"] = True
                if on_close:
                    on_close()

        window.bind("<Destroy>", _on_window_destroy)

        window.lift()
        window.focus_force()
        window.grab_set()
        entry.focus_set()

    def _is_hold_key(self, key) -> bool:
        if self._hold_key_mode == "off" or self._keyboard_mod is None:
            return False
        Key = self._keyboard_mod.Key
        if self._hold_key_mode == "right_ctrl":
            return key == getattr(Key, "ctrl_r", None)
        if self._hold_key_mode == "right_alt":
            return key == getattr(Key, "alt_gr", None) or key == getattr(Key, "alt_r", None)
        return False

    def _on_global_key_press(self, key):
        if not self._is_hold_key(key):
            return
        if self._hold_key_down:
            return
        self._hold_key_down = True
        try:
            self.root.after(0, self._handle_hold_key_down)
        except Exception:
            pass

    def _on_global_key_release(self, key):
        if not self._is_hold_key(key):
            return
        if not self._hold_key_down:
            return
        self._hold_key_down = False
        try:
            self.root.after(0, self._handle_hold_key_up)
        except Exception:
            pass

    def _handle_hold_key_down(self):
        if not self.ready or self.processing or self.recording:
            self._hold_started_recording = False
            return
        self._hold_started_recording = True
        self._start_rec()

    def _handle_hold_key_up(self):
        if self._hold_started_recording and self.recording:
            self._stop_rec()
        self._hold_started_recording = False

    # ── Загрузка PNG-иконки ─────────────────────────────────────
    def _load_mic_images(self):
        try:
            from PIL import Image, ImageTk

            if not _env_bool("WHISPERMAC_USE_PNG_MIC_ICON", True):
                raise FileNotFoundError("PNG mic icon disabled by default")

            env_icon = os.getenv("WHISPERMAC_MIC_ICON")
            candidates = [
                Path(env_icon).expanduser() if env_icon else None,
                Path.home() / "Downloads" / "микро.png",
                Path.home() / "Downloads" / "micro.png",
                _app_dir() / "assets" / "mic.png",
            ]
            icon_path = None
            for path in candidates:
                if path and path.exists():
                    icon_path = path
                    break

            if icon_path is None:
                raise FileNotFoundError("No mic icon found")

            img  = Image.open(icon_path).convert("RGBA")
            img  = img.resize((28, 28), Image.LANCZOS)
            data = np.array(img, dtype=np.uint8)

            white = (data[:,:,0] > 200) & (data[:,:,1] > 200) & (data[:,:,2] > 200)
            data[white, 3] = 0
            mask = data[:,:,3] > 10

            d_idle = data.copy()
            d_idle[mask, 0] = 0xD7
            d_idle[mask, 1] = 0xDB
            d_idle[mask, 2] = 0xE2

            d_active = data.copy()

            self._mic_photo_idle   = ImageTk.PhotoImage(Image.fromarray(d_idle,   "RGBA"))
            self._mic_photo_active = ImageTk.PhotoImage(Image.fromarray(d_active, "RGBA"))
            log(f"Иконка микрофона загружена: {icon_path}")
        except Exception as ex:
            log(f"PNG-иконка недоступна, используем canvas: {ex}")

    # ── Рисование иконок ────────────────────────────────────────
    def _draw_mic(self, recording=False):
        x, cy = MIC_X, H // 2

        if self._mic_photo_idle and self._mic_photo_active:
            photo = self._mic_photo_active if recording else self._mic_photo_idle
            self._mic_item = self.cv.create_image(
                x, cy, image=photo, anchor="center", tags="mic"
            )
            return

        sym = C_MIC_SYM_ON if recording else C_MIC_SYM

        cw, ch, lw = 8, 11, 1.8
        cap_top = cy - 7
        cap_bot = cap_top + ch
        self.cv.create_arc(x-cw//2, cap_top, x+cw//2, cap_top+cw,
                           start=0, extent=180, style="arc",
                           outline=sym, width=lw, tags=("mic", "mic_sym"))
        self.cv.create_line(x-cw//2, cap_top+cw//2, x-cw//2, cap_bot,
                            fill=sym, width=lw, tags=("mic", "mic_sym"))
        self.cv.create_line(x+cw//2, cap_top+cw//2, x+cw//2, cap_bot,
                            fill=sym, width=lw, tags=("mic", "mic_sym"))
        self.cv.create_arc(x-cw//2, cap_bot-cw, x+cw//2, cap_bot,
                           start=180, extent=180, style="arc",
                           outline=sym, width=lw, tags=("mic", "mic_sym"))
        sr = 5
        self.cv.create_arc(x-sr, cap_bot, x+sr, cap_bot+sr*2,
                           start=0, extent=-180, style="arc",
                           outline=sym, width=lw, tags=("mic", "mic_sym"))
        self.cv.create_line(x-4, cap_bot+sr*2, x+4, cap_bot+sr*2,
                            fill=sym, width=lw, capstyle="round",
                            tags=("mic", "mic_sym"))

    def _set_mic_color(self, recording=False):
        if self._mic_item and self._mic_photo_idle and self._mic_photo_active:
            photo = self._mic_photo_active if recording else self._mic_photo_idle
            self.cv.itemconfig(self._mic_item, image=photo)
            self._current_mic_photo = photo
            return

        sym = C_MIC_SYM_ON if recording else C_MIC_SYM
        for item in self.cv.find_withtag("mic_sym"):
            t = self.cv.type(item)
            if t == "line":
                self.cv.itemconfig(item, fill=sym)
            elif t == "arc":
                self.cv.itemconfig(item, outline=sym)

    def _draw_close(self):
        cx = W - 22
        cy_c = H // 2
        self._close_bounds = (cx - 16, cy_c - 16, cx + 16, cy_c + 16)
        self._close_bg = self.cv.create_oval(
            cx - 13, cy_c - 13, cx + 13, cy_c + 13,
            fill=C_CLOSE_BG, outline="", tags="close"
        )
        d = 5
        lw = 1
        self.cv.create_line(cx-d, cy_c-d, cx+d, cy_c+d,
                             fill=C_CLOSE_X, width=lw,
                             capstyle="round", tags="close")
        self.cv.create_line(cx-d, cy_c+d, cx+d, cy_c-d,
                             fill=C_CLOSE_X, width=lw,
                             capstyle="round", tags="close")

    def _draw_logs_button(self):
        cx = W - 56
        cy_c = H // 2
        r = 13
        self._logs_bounds = (cx - 16, cy_c - 16, cx + 16, cy_c + 16)
        self._logs_bg = self.cv.create_oval(
            cx - r, cy_c - r, cx + r, cy_c + r,
            fill=C_LOG_BG, outline="", tags="logs"
        )
        self.cv.create_line(cx - 5, cy_c - 4, cx + 5, cy_c - 4,
                            fill=C_LOG_X, width=1.4, capstyle="round", tags="logs")
        self.cv.create_line(cx - 5, cy_c, cx + 5, cy_c,
                            fill=C_LOG_X, width=1.4, capstyle="round", tags="logs")
        self.cv.create_line(cx - 5, cy_c + 4, cx + 5, cy_c + 4,
                            fill=C_LOG_X, width=1.4, capstyle="round", tags="logs")

    # ── Логи (UI) ───────────────────────────────────────────────
    def _toggle_logs_window(self, _event=None):
        self._suppress_next_toggle = True
        if self._logs_win and self._logs_win.winfo_exists():
            self._close_logs_window()
        else:
            self._open_logs_window()
        return "break"

    def _open_logs_window(self):
        self._logs_win = tk.Toplevel(self.root)
        self._logs_win.title("WhisperWin: Логи")
        self._logs_win.geometry("860x520")
        self._logs_win.minsize(700, 380)
        self._logs_win.configure(bg=BG)
        self._logs_win.protocol("WM_DELETE_WINDOW", self._close_logs_window)

        root_frame = tk.Frame(self._logs_win, bg=BG)
        root_frame.pack(fill="both", expand=True, padx=10, pady=10)

        left = tk.Frame(root_frame, bg=BG)
        left.pack(side="left", fill="both", expand=False)
        right = tk.Frame(root_frame, bg=BG)
        right.pack(side="right", fill="both", expand=True, padx=(10, 0))

        self._logs_list = tk.Listbox(
            left,
            width=46,
            bg="#151518",
            fg="#E8E8EA",
            selectbackground="#2C2C31",
            selectforeground="#FFFFFF",
            activestyle="none",
            borderwidth=0,
            highlightthickness=0,
        )
        list_scroll = tk.Scrollbar(left, orient="vertical", command=self._logs_list.yview)
        self._logs_list.config(yscrollcommand=list_scroll.set)
        self._logs_list.pack(side="left", fill="both", expand=True)
        list_scroll.pack(side="right", fill="y")
        self._logs_list.bind("<<ListboxSelect>>", self._on_log_select)

        self._logs_text = tk.Text(
            right,
            wrap="word",
            bg="#151518",
            fg="#F1F1F4",
            insertbackground="#F1F1F4",
            borderwidth=0,
            highlightthickness=0,
            padx=10,
            pady=10,
        )
        text_scroll = tk.Scrollbar(right, orient="vertical", command=self._logs_text.yview)
        self._logs_text.config(yscrollcommand=text_scroll.set)
        self._logs_text.pack(side="left", fill="both", expand=True)
        text_scroll.pack(side="right", fill="y")

        controls = tk.Frame(self._logs_win, bg=BG)
        controls.pack(fill="x", padx=10, pady=(0, 10))
        tk.Button(controls, text="Обновить", command=self._refresh_logs).pack(side="left")
        tk.Button(controls, text="Копировать", command=self._copy_selected_log).pack(side="left", padx=(6, 0))
        tk.Button(controls, text="Открыть Файл", command=self._open_logs_file).pack(side="right", padx=(0, 6))

        self._refresh_logs()

    def _close_logs_window(self):
        if self._logs_win and self._logs_win.winfo_exists():
            self._logs_win.destroy()
        self._logs_win = None
        self._logs_list = None
        self._logs_text = None
        self._log_records = []

    def _log_file_path(self) -> Path:
        return Path.home() / "whisper_log.txt"

    def _read_log_records(self) -> list:
        path = self._log_file_path()
        if not path.exists():
            return []
        records = []
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    raw = line.strip()
                    if not raw:
                        continue
                    ts = ""
                    text = raw
                    if raw.startswith("[") and "] " in raw:
                        ts, text = raw[1:].split("] ", 1)
                    text = text.strip()
                    if not text:
                        continue
                    records.append({"ts": ts, "text": text})
        except Exception as ex:
            log(f"Не удалось прочитать лог: {ex}")
            return []
        return list(reversed(records[-1000:]))

    def _log_preview(self, text: str, max_len: int = 72) -> str:
        cleaned = " ".join(text.split())
        if len(cleaned) <= max_len:
            return cleaned
        return cleaned[: max_len - 1] + "…"

    def _refresh_logs(self):
        if not self._logs_list:
            return
        prev_idx = self._selected_log_index()
        self._log_records = self._read_log_records()
        self._logs_list.delete(0, tk.END)
        for item in self._log_records:
            ts = item["ts"] or "--"
            self._logs_list.insert(tk.END, f"{ts} · {self._log_preview(item['text'])}")
        if self._log_records:
            idx = prev_idx if prev_idx is not None and prev_idx < len(self._log_records) else 0
            self._logs_list.selection_set(idx)
            self._logs_list.activate(idx)
            self._on_log_select()
        elif self._logs_text:
            self._logs_text.delete("1.0", tk.END)

    def _selected_log_index(self):
        if not self._logs_list:
            return None
        cur = self._logs_list.curselection()
        if not cur:
            return None
        idx = int(cur[0])
        if idx < 0 or idx >= len(self._log_records):
            return None
        return idx

    def _on_log_select(self, _event=None):
        if not self._logs_text:
            return
        idx = self._selected_log_index()
        self._logs_text.delete("1.0", tk.END)
        if idx is None:
            return
        item = self._log_records[idx]
        header = f"{item['ts']}\n\n" if item["ts"] else ""
        self._logs_text.insert("1.0", header + item["text"])

    def _copy_selected_log(self):
        idx = self._selected_log_index()
        if idx is None:
            return
        item = self._log_records[idx]
        if self._copy_to_clipboard(item["text"]):
            log("Лог скопирован в буфер")

    def _open_logs_file(self):
        try:
            os.startfile(str(self._log_file_path()))
        except Exception as ex:
            log(f"Не удалось открыть файл лога: {ex}")

    # ── Drag ────────────────────────────────────────────────────
    def _point_in_bounds(self, x: int, y: int, bounds: tuple) -> bool:
        x1, y1, x2, y2 = bounds
        return x1 <= x <= x2 and y1 <= y <= y2

    def _is_control_hit(self, e) -> bool:
        return (
            self._point_in_bounds(e.x, e.y, self._close_bounds)
            or self._point_in_bounds(e.x, e.y, self._logs_bounds)
        )

    def _press(self, e):
        if self._is_control_hit(e):
            self._suppress_next_toggle = True
            return "break"
        self._drag_ox = e.x
        self._drag_oy = e.y
        self._dragging = False

    def _motion(self, e):
        if self._is_control_hit(e):
            return "break"
        if abs(e.x - self._drag_ox) + abs(e.y - self._drag_oy) > 4:
            self._dragging = True
        if self._dragging:
            self.root.geometry(
                f"+{self.root.winfo_x() + e.x - self._drag_ox}"
                f"+{self.root.winfo_y() + e.y - self._drag_oy}"
            )

    def _release(self, e):
        if self._suppress_next_toggle or self._is_control_hit(e):
            self._suppress_next_toggle = False
            self._dragging = False
            return "break"
        if not self._dragging:
            self._toggle()
        self._dragging = False

    # ── Запись ──────────────────────────────────────────────────
    def _toggle(self):
        if not self.ready or self.processing:
            return
        if not self.recording:
            self._start_rec()
        else:
            self._stop_rec()

    def _start_rec(self):
        with self._chunks_lock:
            self.chunks = []
        self._eq_levels[:] = 0
        self._eq_smooth[:] = 0
        self._rms_smooth = 0.0
        self._recording_started_at = time.perf_counter()
        hwnd = frontmost_hwnd()
        name = process_name_for_hwnd(hwnd)
        if hwnd and not self._is_excluded_process(name):
            self.target_hwnd = hwnd
        self.recording = True
        self._set_mic_color(recording=True)
        log(
            f"Конфиг: chunk={CHUNK_SEC:.1f}s, poll={WORKER_POLL_SEC:.2f}s, "
            f"final-pass={FINAL_PASS_MIN_SEC:.0f}-{FINAL_PASS_MAX_SEC:.0f}s"
        )
        log(
            f"Privacy: strict_local={'on' if STRICT_LOCAL_MODE else 'off'}, "
            f"save_transcripts={'on' if SAVE_TRANSCRIPTS else 'off'}, "
            f"save_perf={'on' if SAVE_PERF_LOG else 'off'}"
        )
        log(f"Запись... (target={name or '-'})")
        try:
            self.stream = sd.InputStream(
                samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                blocksize=1024, latency="low", callback=self._audio_cb
            )
            self.stream.start()
        except Exception as ex:
            log(f"Ошибка: {ex}")
            err = str(ex).lower()
            if any(k in err for k in ("permission", "not permitted", "unauthorized", "access", "denied")):
                open_mic_privacy_settings()
            self._reset()
            return
        if ENGINE == "groq" and GROQ_API_KEY:
            threading.Thread(target=self._groq_worker, daemon=True).start()
        else:
            threading.Thread(target=self._streaming_worker, daemon=True).start()

    def _stop_rec(self):
        self.recording = False
        hwnd = frontmost_hwnd()
        name = process_name_for_hwnd(hwnd)
        if hwnd and not self._is_excluded_process(name):
            self.target_hwnd = hwnd
        if self.stream:
            self.stream.stop()
            self.stream.close()
            self.stream = None
        self.processing = True
        self._set_mic_color(recording=False)

    # ── Аудио-коллбэк (real-time FFT для эквалайзера) ───────────
    def _audio_cb(self, indata, frames, time_info, status):
        frame = indata.flatten()
        with self._chunks_lock:
            self.chunks.append(indata.copy())

        if len(frame) < 64:
            return

        rms = float(np.sqrt(np.mean(frame ** 2)))
        self._rms_smooth = (
            (1.0 - EQ_RMS_ALPHA) * self._rms_smooth + EQ_RMS_ALPHA * rms
        )

        # Подстраиваем оценку "тишины в комнате" под реальный сигнал: быстро
        # опускаемся к тихим моментам, медленно ползём вверх — так голос не
        # успевает сам стать новой "тишиной", а разная чувствительность
        # микрофонов не требует ручной калибровки.
        if self._rms_smooth < self._noise_floor:
            self._noise_floor += (self._rms_smooth - self._noise_floor) * EQ_NOISE_ADAPT_DOWN
        else:
            self._noise_floor += (self._rms_smooth - self._noise_floor) * EQ_NOISE_ADAPT_UP

        gate = max(EQ_NOISE_FLOOR_MIN, self._noise_floor * EQ_GATE_MULT)
        full = max(gate + 1e-6, self._noise_floor * EQ_FULL_MULT)

        if self._rms_smooth <= gate:
            self._eq_levels[:] = 0
            return

        windowed = frame * np.hanning(len(frame))
        fft_vals  = np.abs(np.fft.rfft(windowed))
        n_bins    = len(fft_vals)

        levels = np.array([
            np.mean(fft_vals[n_bins * i // BAR_COUNT : n_bins * (i+1) // BAR_COUNT])
            for i in range(BAR_COUNT)
        ], dtype=np.float32)

        peak = levels.max()
        if peak > 1e-6:
            shape = levels / peak
            denom = max(1e-6, full - gate)
            amplitude = min(
                1.0,
                max(0.0, (self._rms_smooth - gate) / denom),
            )
            amplitude = amplitude ** 0.72
            self._eq_levels[:] = (shape * amplitude).astype(np.float32)
        else:
            self._eq_levels[:] = 0

    def _transcribe_audio(
        self,
        audio,
        *,
        prompt=None,
        final=False,
        condition_on_previous_text=True,
        temperature=None,
    ):
        return _local_transcribe(
            audio,
            prompt=prompt,
            final=final,
            condition_on_previous_text=condition_on_previous_text,
            temperature=temperature,
        )

    def _take_new_audio(self, chunk_idx: int) -> tuple:
        with self._chunks_lock:
            total = len(self.chunks)
            if chunk_idx >= total:
                return chunk_idx, None
            new_chunks = self.chunks[chunk_idx:total]
            chunk_idx = total
        if not new_chunks:
            return chunk_idx, None
        new_audio = np.concatenate([c.flatten() for c in new_chunks])
        return chunk_idx, new_audio

    def _decode_piece(self, audio: np.ndarray, parts: list, label: str) -> tuple:
        prompt = _prompt_from_parts(parts)
        started = time.perf_counter()
        result = self._transcribe_audio(audio, prompt=prompt, final=False)
        elapsed = time.perf_counter() - started
        text = result.get("text", "").strip()
        avg_logprob, avg_no_speech = _segment_quality(result)
        if _likely_silence_hallucination(text, avg_no_speech):
            log(
                f"[{label}] пропуск (тишина): no_speech={avg_no_speech:.2f}, "
                f"text='{text[:24]}'"
            )
            return "", elapsed, avg_logprob, avg_no_speech
        if text:
            log(f"[{label}] {text}")
        return text, elapsed, avg_logprob, avg_no_speech

    # ── Groq воркер (основной путь) ─────────────────────────────
    def _groq_worker(self):
        while self.recording:
            time.sleep(WORKER_POLL_SEC)

        with self._chunks_lock:
            all_audio = (
                np.concatenate([c.flatten() for c in self.chunks])
                if self.chunks else np.array([], dtype=np.float32)
            )

        audio_sec = len(all_audio) / SAMPLE_RATE
        amp = float(np.max(np.abs(all_audio))) if len(all_audio) else 0.0
        if audio_sec < MIN_DURATION or amp <= 0.001:
            log("[groq] слишком короткая/тихая запись — пропуск")
            self.root.after(0, self._reset)
            return

        full = groq_transcribe(all_audio, prompt=HOTWORDS_PROMPT)

        if not full:
            log("[groq] пустой результат — фоллбэк на локальную модель")
            full = self._local_full_transcribe(all_audio)

        if full and _is_repetition_loop(full):
            collapsed = _collapse_repetition_loop(full).strip()
            if collapsed and collapsed != full:
                log("[post] схлопнул повторяющийся loop-текст")
                full = collapsed

        log(f"→ {full}")
        if full:
            self._save(full)
            self.root.after(0, lambda t=full: self._paste_and_reset(t))
        else:
            self.root.after(0, self._reset)

    def _local_full_transcribe(self, all_audio: np.ndarray) -> str:
        if not len(all_audio):
            return ""
        try:
            res = self._transcribe_audio(
                all_audio,
                prompt=HOTWORDS_PROMPT,
                final=True,
                condition_on_previous_text=False,
            )
            text = res.get("text", "").strip()
            if text and _is_repetition_loop(text):
                safe = self._transcribe_audio(
                    all_audio, prompt=None, final=False,
                    condition_on_previous_text=False, temperature=0.0,
                )
                safe_text = safe.get("text", "").strip()
                if safe_text and not _is_repetition_loop(safe_text):
                    text = safe_text
            return text
        except Exception as ex:  # noqa: BLE001
            log(f"[local-fallback] ошибка: {ex}")
            return ""

    # ── Streaming воркер ────────────────────────────────────────
    def _streaming_worker(self):
        CHUNK      = int(CHUNK_SEC * SAMPLE_RATE)
        parts      = []
        pending    = np.array([], dtype=np.float32)
        chunk_idx  = 0
        decode_time_sec = 0.0
        processed_audio_sec = 0.0
        low_conf_chunks = 0
        decoded_chunks = 0

        while self.recording:
            time.sleep(WORKER_POLL_SEC)

            chunk_idx, new_audio = self._take_new_audio(chunk_idx)
            if new_audio is None:
                continue
            pending   = np.concatenate([pending, new_audio]) if len(pending) else new_audio

            while len(pending) >= CHUNK:
                segment = pending[:CHUNK]
                pending = pending[CHUNK:]

                text, elapsed, avg_logprob, _ = self._decode_piece(
                    segment, parts, "chunk"
                )
                decode_time_sec += elapsed
                processed_audio_sec += len(segment) / SAMPLE_RATE
                decoded_chunks += 1
                if avg_logprob <= LOW_CONF_LOGPROB:
                    low_conf_chunks += 1
                if text:
                    parts.append(text)

        chunk_idx, new_audio = self._take_new_audio(chunk_idx)
        if new_audio is not None:
            pending   = np.concatenate([pending, new_audio]) if len(pending) else new_audio

        while len(pending) >= CHUNK:
            segment = pending[:CHUNK]
            pending = pending[CHUNK:]
            text, elapsed, avg_logprob, _ = self._decode_piece(segment, parts, "flush")
            decode_time_sec += elapsed
            processed_audio_sec += len(segment) / SAMPLE_RATE
            decoded_chunks += 1
            if avg_logprob <= LOW_CONF_LOGPROB:
                low_conf_chunks += 1
            if text:
                parts.append(text)

        amp = float(np.max(np.abs(pending))) if len(pending) else 0
        if len(pending) / SAMPLE_RATE >= MIN_DURATION and amp > 0.001:
            text, elapsed, avg_logprob, _ = self._decode_piece(pending, parts, "tail")
            decode_time_sec += elapsed
            processed_audio_sec += len(pending) / SAMPLE_RATE
            decoded_chunks += 1
            if avg_logprob <= LOW_CONF_LOGPROB:
                low_conf_chunks += 1
            if text:
                parts.append(text)

        chunk_full = _join_chunks(parts)
        full = chunk_full

        with self._chunks_lock:
            all_audio = (
                np.concatenate([c.flatten() for c in self.chunks])
                if self.chunks else np.array([], dtype=np.float32)
            )
        if len(all_audio):
            audio_sec = len(all_audio) / SAMPLE_RATE
            low_conf_ratio = (
                (low_conf_chunks / decoded_chunks)
                if decoded_chunks else 0.0
            )
            need_final_pass = (
                FINAL_PASS_MIN_SEC <= audio_sec <= FINAL_PASS_MAX_SEC
                and (
                    _is_repetition_loop(chunk_full)
                    or not chunk_full
                    or low_conf_ratio >= 0.35
                )
            )
            if need_final_pass and audio_sec >= MIN_DURATION:
                try:
                    final_res = self._transcribe_audio(
                        all_audio,
                        prompt=HOTWORDS_PROMPT,
                        final=True,
                        condition_on_previous_text=False,
                    )
                    final_text = final_res.get("text", "").strip()
                    if final_text:
                        if _is_repetition_loop(final_text):
                            log("[final] обнаружен loop-повтор, пробую safe-pass")
                            safe_res = self._transcribe_audio(
                                all_audio,
                                prompt=None,
                                final=False,
                                condition_on_previous_text=False,
                                temperature=0.0,
                            )
                            safe_text = safe_res.get("text", "").strip()
                            if safe_text and not _is_repetition_loop(safe_text):
                                log(f"[final-safe] {safe_text}")
                                full = safe_text
                            else:
                                log("[final] loop остался, fallback на chunk-текст")
                                full = chunk_full
                        else:
                            log(f"[final] {final_text}")
                            full = final_text
                except Exception as ex:
                    log(f"[final] fallback на чанки: {ex}")
            elif audio_sec > FINAL_PASS_MAX_SEC:
                skip_line = (
                    f"[final] пропуск полного pass: запись {audio_sec:.1f}s > "
                    f"{FINAL_PASS_MAX_SEC:.0f}s"
                )
                log(skip_line)
                self._save_perf(skip_line)

        if full and _is_repetition_loop(full):
            collapsed = _collapse_repetition_loop(full).strip()
            if collapsed and collapsed != full:
                log("[post] схлопнул повторяющийся loop-текст")
                full = collapsed

        record_wall_sec = 0.0
        if self._recording_started_at is not None:
            record_wall_sec = max(0.0, time.perf_counter() - self._recording_started_at)
        if processed_audio_sec > 0:
            rtf = decode_time_sec / processed_audio_sec
            perf_line = (
                f"[perf] обработано {processed_audio_sec:.1f}s аудио за "
                f"{decode_time_sec:.2f}s (RTF={rtf:.2f}x), запись шла {record_wall_sec:.1f}s"
            )
            log(perf_line)
            self._save_perf(perf_line)

        log(f"→ {full}")
        if full:
            self._save(full)
            self.root.after(0, lambda t=full: self._paste_and_reset(t))
        else:
            self.root.after(0, self._reset)

    # ── Вспомогательные ─────────────────────────────────────────
    def _save(self, text):
        if not SAVE_TRANSCRIPTS:
            return
        from datetime import datetime
        with open(Path.home() / "whisper_log.txt", "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {text}\n")
        if self._logs_win and self._logs_win.winfo_exists():
            self.root.after(0, self._refresh_logs)

    def _save_perf(self, text):
        if not SAVE_PERF_LOG:
            return
        from datetime import datetime
        with open(Path.home() / "whisper_perf.log", "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {text}\n")

    def _copy_to_clipboard(self, text: str) -> bool:
        try:
            import win32clipboard
            win32clipboard.OpenClipboard()
            try:
                win32clipboard.EmptyClipboard()
                win32clipboard.SetClipboardText(text, win32clipboard.CF_UNICODETEXT)
            finally:
                win32clipboard.CloseClipboard()
            return True
        except Exception as ex:
            log(f"win32clipboard failed, fallback to Tk clipboard: {ex}")
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self.root.update_idletasks()
            return True
        except Exception as ex2:
            log(f"Tk clipboard fallback failed: {ex2}")
            return False

    def _paste_and_reset(self, text):
        if not self._copy_to_clipboard(text):
            log("Paste failed: не удалось скопировать текст в буфер обмена")
            self._reset()
            return

        target = self.target_hwnd
        if target and not win32gui.IsWindow(target):
            target = None
        front = frontmost_hwnd()
        front_name = process_name_for_hwnd(front)
        if not target and front and not self._is_excluded_process(front_name):
            target = front

        if not target:
            log("Paste skipped: no external target window; text is in clipboard (Ctrl+V вручную)")
            self._reset()
            return

        target_name = process_name_for_hwnd(target)
        target_title = ""
        try:
            target_title = win32gui.GetWindowText(target)
        except Exception:
            pass

        # Если целевое окно и так уже активно (самый частый случай — диктуем
        # туда же, откуда не уходили), пропускаем весь SetForegroundWindow/ALT
        # манёвр целиком: даже "успешный" вызов к уже-активному окну заставляет
        # Windows лишний раз пересчитать z-order/фокус, что на некоторых
        # системах видно глазом как короткое моргание экрана.
        already_foreground = frontmost_hwnd() == target
        if not already_foreground:
            if not force_foreground(target):
                log(
                    f"Не удалось перевести фокус на целевое окно "
                    f"(process={target_name or '-'} title='{target_title}' hwnd={target}); "
                    f"текст в буфере обмена, вставь вручную Ctrl+V"
                )
                self._reset()
                return
            time.sleep(0.08)
            now_foreground = frontmost_hwnd()
            if now_foreground != target:
                log(
                    f"Внимание: после force_foreground foreground-окно другое "
                    f"(ожидали hwnd={target}, реально hwnd={now_foreground}) — "
                    f"Ctrl+V может уйти не туда"
                )

        sent = send_ctrl_v()
        if not sent:
            log("Синтетический Ctrl+V не сработал; текст в буфере обмена, вставь вручную")
        else:
            log(
                f"Paste: Ctrl+V отправлен в process={target_name or '-'} "
                f"title='{target_title}' hwnd={target} "
                f"(окно {'уже было' if already_foreground else 'стало'} активным)"
            )
        self._reset()

    def _reset(self):
        self.processing  = False
        self._eq_levels[:] = 0
        self._set_mic_color(recording=False)

    # ── Анимация ────────────────────────────────────────────────
    def _tick(self):
        self._frame += 1
        t  = self._frame * 0.12
        cy = H // 2

        if self.recording:
            target = np.power(np.clip(self._eq_levels, 0.0, 1.0), EQ_VISUAL_GAMMA)
            rising = target > self._eq_smooth
            self._eq_smooth = np.where(
                rising,
                self._eq_smooth + (target - self._eq_smooth) * EQ_ATTACK,
                self._eq_smooth * (1.0 - EQ_DECAY)
            )
            energy = float(np.mean(target))
            for i, (b, bx) in enumerate(self.bars):
                wobble = 0.0
                if energy > 0.03:
                    wobble = (
                        0.5 + 0.5 * math.sin(t * (5.2 + i * 0.15) + i * 0.9)
                    ) * EQ_WOBBLE_MAX * energy
                level = min(1.0, self._eq_smooth[i] + wobble)
                h = BAR_MIN + level * BAR_MAX
                self.cv.coords(b, bx, cy - h, bx + BAR_W, cy + h)
                self.cv.itemconfig(b, fill=C_REC)

        elif self.processing:
            for i, (b, bx) in enumerate(self.bars):
                h = BAR_MIN + abs(math.sin(t * 4.0 + i * 0.5)) * BAR_MAX * 0.55
                self.cv.coords(b, bx, cy - h, bx + BAR_W, cy + h)
                self.cv.itemconfig(b, fill=C_PROC)

        else:
            for b, bx in self.bars:
                self.cv.coords(b, bx, cy - BAR_MIN, bx + BAR_W, cy + BAR_MIN)
                self.cv.itemconfig(b, fill=C_IDLE)

        if not self.ready:
            self.cv.itemconfig(
                self.spinner,
                text="◜◝◞◟"[self._frame // 4 % 4]
            )

        self.root.after(33, self._tick)

    def _track_app(self):
        try:
            if not self.recording and not self.processing:
                hwnd = frontmost_hwnd()
                name = process_name_for_hwnd(hwnd)
                if hwnd and not self._is_excluded_process(name):
                    self.target_hwnd = hwnd
        except Exception:
            pass
        self.root.after(300, self._track_app)

    def _load_model(self):
        if ENGINE == "groq" and GROQ_API_KEY:
            log(f"Движок: Groq ({GROQ_MODEL}), локальная модель — фоллбэк")
            log("Готово")
            self.root.after(0, self._on_ready)
            return
        if ENGINE == "groq" and not GROQ_API_KEY:
            log("[groq] ключ не найден — работаю только на локальной модели")
        log("Загружаю модель...")
        if STRICT_LOCAL_MODE:
            log("Strict local mode: offline-only")
        dummy = np.zeros(SAMPLE_RATE, dtype=np.float32)
        self._transcribe_audio(dummy, prompt=HOTWORDS_PROMPT, final=False)
        log("Готово")
        self.root.after(0, self._on_ready)

    def _on_ready(self):
        self.ready = True
        self.cv.itemconfig(self.spinner, state="hidden")
        self.cv.itemconfig("mic",        state="normal")

    def run(self):
        log("WhisperWin запущен")
        self.root.mainloop()


if __name__ == "__main__":
    _instance_lock = acquire_instance_lock()
    if _instance_lock is None:
        log("WhisperWin уже запущен, второй экземпляр остановлен")
        sys.exit(0)
    atexit.register(_release_instance_lock)
    App().run()
