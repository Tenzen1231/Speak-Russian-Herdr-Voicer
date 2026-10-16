"""Silero VAD v6 on ONNX Runtime; a compact port of faster-whisper's helper."""
from functools import lru_cache

import numpy as np

from dictate import ROOT

WINDOW = 512
CONTEXT = 64


@lru_cache(maxsize=1)
def _session():
    import onnxruntime as rt
    options = rt.SessionOptions()
    options.inter_op_num_threads = options.intra_op_num_threads = 1
    options.enable_cpu_mem_arena = False
    options.log_severity_level = 4
    return rt.InferenceSession(str(ROOT / 'vad-model/silero_vad_v6.onnx'),
                               sess_options=options, providers=['CPUExecutionProvider'])


def probabilities(audio):
    audio = np.pad(audio, (0, WINDOW - len(audio) % WINDOW))
    windows = audio.reshape(-1, WINDOW)
    # Each window sees the last 64 samples of its predecessor.
    context = np.roll(windows[:, -CONTEXT:].copy(), 1, 0)
    context[0] = 0
    batch = np.ascontiguousarray(np.concatenate([context, windows], 1), dtype=np.float32)
    h = c = np.zeros((1, 1, 128), dtype=np.float32)
    outputs = []
    for start in range(0, len(batch), 10000):
        output, h, c = _session().run(None, {'input': batch[start:start + 10000], 'h': h, 'c': c})
        outputs.append(output.reshape(-1))
    return np.concatenate(outputs)


def speech_timestamps(audio, threshold=0.35, min_silence_ms=600, speech_pad_ms=400,
                      max_speech_s=float('inf'), min_speech_ms=250, rate=16000):
    """Return [{'start', 'end'}] sample ranges of speech, like faster-whisper."""
    negative = max(threshold - 0.15, 0.01)
    min_speech = rate * min_speech_ms / 1000
    pad = rate * speech_pad_ms / 1000
    max_speech = rate * max_speech_s - WINDOW - 2 * pad
    min_silence = rate * min_silence_ms / 1000
    min_silence_at_max = rate * 98 / 1000
    length = len(audio)
    triggered = False
    speeches, current = [], {}
    temp_end = prev_end = next_start = 0
    for i, probability in enumerate(probabilities(audio)):
        position = WINDOW * i
        if probability >= threshold and temp_end:
            temp_end = 0
            if next_start < prev_end:
                next_start = position
        if probability >= threshold and not triggered:
            triggered = True
            current['start'] = position
            continue
        if triggered and position - current['start'] > max_speech:
            # Too long: cut at the last short pause, or right here if none.
            if prev_end:
                current['end'] = prev_end
                speeches.append(current)
                current = {}
                if next_start < prev_end:
                    triggered = False
                else:
                    current['start'] = next_start
                prev_end = next_start = temp_end = 0
            else:
                current['end'] = position
                speeches.append(current)
                current = {}
                prev_end = next_start = temp_end = 0
                triggered = False
                continue
        if probability < negative and triggered:
            if not temp_end:
                temp_end = position
            if position - temp_end > min_silence_at_max:
                prev_end = temp_end
            if position - temp_end < min_silence:
                continue
            current['end'] = temp_end
            if current['end'] - current['start'] > min_speech:
                speeches.append(current)
            current = {}
            prev_end = next_start = temp_end = 0
            triggered = False
    if current and length - current['start'] > min_speech:
        current['end'] = length
        speeches.append(current)
    for i, speech in enumerate(speeches):
        if i == 0:
            speech['start'] = int(max(0, speech['start'] - pad))
        if i != len(speeches) - 1:
            silence = speeches[i + 1]['start'] - speech['end']
            if silence < 2 * pad:
                speech['end'] += int(silence // 2)
                speeches[i + 1]['start'] = int(max(0, speeches[i + 1]['start'] - silence // 2))
            else:
                speech['end'] = int(min(length, speech['end'] + pad))
                speeches[i + 1]['start'] = int(max(0, speeches[i + 1]['start'] - pad))
        else:
            speech['end'] = int(min(length, speech['end'] + pad))
    return speeches
