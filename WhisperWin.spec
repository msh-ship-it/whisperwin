# -*- mode: python ; coding: utf-8 -*-
#
# Сборка: python -m PyInstaller WhisperWin.spec
#
# ffmpeg.exe ищется автоматически (PATH, затем стандартный путь winget);
# можно переопределить переменной окружения WHISPERWIN_FFMPEG перед сборкой,
# если ffmpeg стоит в нестандартном месте. Без ffmpeg сборка тоже соберётся —
# просто отправка длинных записей в Groq не будет сжиматься (шлётся WAV).

import os
import shutil
from pathlib import Path

def _find_ffmpeg():
    override = os.environ.get('WHISPERWIN_FFMPEG', '').strip()
    if override and Path(override).exists():
        return override
    found = shutil.which('ffmpeg')
    if found:
        return found
    winget_glob = list(Path(os.environ.get('LOCALAPPDATA', '')).glob(
        'Microsoft/WinGet/Packages/Gyan.FFmpeg*/ffmpeg-*/bin/ffmpeg.exe'
    ))
    if winget_glob:
        return str(winget_glob[0])
    print('WARNING: ffmpeg.exe not found — building without it '
          '(Groq uploads will use uncompressed WAV).')
    return None

_ffmpeg_path = _find_ffmpeg()
_ffmpeg_binaries = [(_ffmpeg_path, '.')] if _ffmpeg_path else []

a = Analysis(
    ['whisper_win.py'],
    pathex=[],
    binaries=_ffmpeg_binaries,
    datas=[
        ('assets/mic.png', 'assets'),
        ('icon.ico', '.'),
        ('icon.png', '.'),
    ],
    hiddenimports=[
        'faster_whisper',
        'ctranslate2',
        'win32timezone',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='WhisperWin',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='icon.ico',
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='WhisperWin',
)
