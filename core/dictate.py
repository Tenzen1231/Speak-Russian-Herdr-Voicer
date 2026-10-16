"""Offline GigaAM v3 E2E (Russian with punctuation); PCM and text stay in memory."""
import os
# numpy's OpenBLAS otherwise commits ~350 MB of per-thread buffers at import.
# Must be set before numpy is first imported anywhere in the process.
os.environ.setdefault('OPENBLAS_NUM_THREADS', '1')
import sys
import threading
from functools import lru_cache
from pathlib import Path
ROOT = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))
_lock = threading.Lock()
RATE = 16000
# GigaAM is trained on utterances up to ~25 s; longer windows lose words.
CHUNK = 20 * RATE


def clean_text(text):
    return ' '.join(''.join(c for c in text if (c >= ' ' and c != '\x7f') or c in '\n\r\t').split())


@lru_cache(maxsize=1)
def model_instance():
    import onnx_asr
    import onnxruntime as rt
    options = rt.SessionOptions()
    options.enable_cpu_mem_arena = False
    options.log_severity_level = 3
    options.intra_op_num_threads = min(4, max(1, (os.cpu_count() or 2) // 2))
    return onnx_asr.load_model('gigaam-v3-e2e-rnnt', ROOT / 'gigaam', quantization='int8',
                               sess_options=options, providers=['CPUExecutionProvider'])


def chunks(regions):
    """Group VAD regions into windows of at most CHUNK samples, keeping pauses."""
    groups = []
    for region in regions:
        if groups and region['end'] - groups[-1][0] <= CHUNK:
            groups[-1][1] = region['end']
        else:
            groups.append([region['start'], region['end']])
    return groups


def warmup():
    import numpy as np
    from vad import speech_timestamps
    with _lock:
        model_instance().recognize(np.zeros(RATE, dtype=np.float32), sample_rate=RATE)
        speech_timestamps(np.zeros(RATE, dtype=np.float32))


def transcribe(audio, cancel=None):
    import numpy as np
    from vad import speech_timestamps
    audio = np.ascontiguousarray(audio, dtype=np.float32)
    with _lock:
        if cancel is not None and cancel.is_set():
            raise InterruptedError('Superseded recognition')
        regions = speech_timestamps(audio, max_speech_s=CHUNK / RATE)
        model = model_instance()
        parts = []
        for start, end in chunks(regions):
            if cancel is not None and cancel.is_set():
                raise InterruptedError('Superseded recognition')
            parts.append(model.recognize(audio[start:end], sample_rate=RATE))
        return clean_text(' '.join(parts))
