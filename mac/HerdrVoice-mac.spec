# -*- mode: python ; coding: utf-8 -*-
# Built on the Mac itself by install.sh: python -m PyInstaller mac/HerdrVoice-mac.spec
import sys
from pathlib import Path
from PyInstaller.utils.hooks import collect_all, collect_data_files, copy_metadata

ROOT = Path(SPECPATH).parent
CORE = ROOT / 'core'
sys.path.insert(0, str(CORE))
from mascot import render

icon = Path(workpath) / 'icon.png'
icon.parent.mkdir(parents=True, exist_ok=True)
render('open', 1024).save(icon)

datas = [(str(p), 'gigaam') for p in (CORE / 'gigaam').iterdir() if p.is_file()]
datas += [(str(CORE / 'vad-model'), 'vad-model'), (str(ROOT / 'licenses'), 'licenses')]
datas += collect_data_files('onnx_asr') + copy_metadata('onnx-asr')
binaries = []
hiddenimports = ['onnx_asr.models.gigaam', 'AVFoundation', 'ApplicationServices', 'Quartz', 'AppKit', 'Foundation']
for package in ('onnxruntime', 'sounddevice'):
    tmp_ret = collect_all(package)
    datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]

a = Analysis(
    [str(ROOT / 'mac/mac_app.py')],
    pathex=[str(CORE)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=['tkinter', 'scipy', 'onnx', 'huggingface_hub', 'faster_whisper'],
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='HerdrVoice', console=False,
          argv_emulation=False, target_arch=None, codesign_identity=None, entitlements_file=None)
coll = COLLECT(exe, a.binaries, a.datas, name='HerdrVoice')
app = BUNDLE(
    coll,
    name='HerdrVoice.app',
    icon=str(icon),
    bundle_identifier='com.herdr.voice',
    info_plist={
        'CFBundleName': 'HerdrVoice',
        'CFBundleDisplayName': 'Herdr Voice',
        'CFBundleShortVersionString': '1.0.0',
        'LSUIElement': True,
        'LSMinimumSystemVersion': '12.0',
        'NSHighResolutionCapable': True,
        'NSMicrophoneUsageDescription':
            'Herdr Voice слушает микрофон только во время диктовки. Звук не покидает компьютер.',
    },
)
