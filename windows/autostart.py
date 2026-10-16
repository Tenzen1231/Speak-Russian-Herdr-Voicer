"""Per-user Windows autostart for the actual executable, never its unpacked files."""
from pathlib import Path
import subprocess
import sys
import winreg

RUN_KEY = r'Software\Microsoft\Windows\CurrentVersion\Run'
VALUE_NAME = 'HerdrVoice'


def launch_command():
    if getattr(sys, 'frozen', False):
        args = [sys.executable]
    else:
        executable = Path(sys.executable).with_name('pythonw.exe')
        args = [str(executable), str(Path(__file__).with_name('bubble.py'))]
    return subprocess.list2cmdline(args)


def is_enabled():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            command, kind = winreg.QueryValueEx(key, VALUE_NAME)
        return kind == winreg.REG_SZ and bool(command)
    except FileNotFoundError:
        return False


def set_enabled(enabled):
    if enabled:
        command = launch_command()
        if len(command) > 260:
            raise OSError('Слишком длинный путь: переместите программу ближе к корню диска')
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, command)
    else:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, VALUE_NAME)
        except FileNotFoundError:
            pass
