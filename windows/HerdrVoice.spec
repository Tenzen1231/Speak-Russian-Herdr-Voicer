# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_data_files
from PyInstaller.utils.hooks import collect_all
from PyInstaller.utils.hooks import copy_metadata
from pathlib import Path

ROOT = Path(SPECPATH).parent
CORE = ROOT / 'core'
# GigaAM files only; the Hugging Face download cache is left out.
datas = [(str(p), 'gigaam') for p in (CORE / 'gigaam').iterdir() if p.is_file()]
datas += [(str(CORE / 'vad-model'), 'vad-model'), (str(ROOT / 'licenses'), 'licenses')]
binaries = []
hiddenimports = ['onnx_asr.models.gigaam']
datas += collect_data_files('onnx_asr') + copy_metadata('onnx-asr')
tmp_ret = collect_all('onnxruntime')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('sounddevice')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]


a = Analysis(
    [str(ROOT / 'windows/bubble.py')],
    pathex=[str(CORE)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Leftovers of the Whisper build and optional onnx-asr extras.
    excludes=['faster_whisper', 'ctranslate2', 'av', 'tokenizers', 'huggingface_hub', 'hf_xet',
              'scipy', 'onnx', 'tqdm', 'httpx2', 'httpcore2', 'anyio', 'fsspec', 'yaml'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='HerdrVoice',
    icon=str(ROOT / 'windows/voice.ico'),
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='HerdrVoice',
)

standalone = EXE(pyz, a.scripts, a.binaries, a.datas, [],
    name='HerdrVoice-Standalone', icon=str(ROOT / 'windows/voice.ico'), console=False, debug=False,
    strip=False, upx=False, disable_windowed_traceback=False)
