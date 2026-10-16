"""Recognize finished phrases while the microphone continues capturing.

One worker per recording, no inference backlog and no audio/text files. Cuts
are made only inside VAD-confirmed pauses; spoken samples never overlap.
"""
import queue
import threading
from math import gcd

import numpy as np

from dictate import clean_text, transcribe


def speech_regions(samples):
    from vad import speech_timestamps
    return speech_timestamps(samples, min_silence_ms=800, speech_pad_ms=0)


class StreamingDictation:
    def __init__(self, sample_rate=16000, recognize=transcribe, detect=speech_regions):
        self.rate = int(sample_rate)
        self.recognize = recognize
        self.detect = detect
        self._queue = queue.SimpleQueue()
        self._cancelled = threading.Event()
        self._flush = threading.Event()
        self._finished = threading.Event()
        self._sealed = False
        self._result = ''
        self._error = None
        self._received = 0
        self._inflight_covered = -1
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def feed(self, samples):
        # Called by PortAudio: copying/queuing only, never VAD or inference.
        if not self._sealed and not self._cancelled.is_set():
            block = np.asarray(samples, dtype=np.float32).reshape(-1).copy()
            self._received += len(block)
            self._queue.put(block)

    def finish(self):
        """Seal after stopping the microphone; wait in a background thread."""
        if not self._sealed:
            self._sealed = True
            # Don't finish an obsolete prefix then run another full decoder
            # pass on the tail: on CPU that can double post-stop waiting.
            if self._received != self._inflight_covered:
                self._flush.set()
            self._queue.put(None)
        self._finished.wait()
        if self._error is not None:
            raise self._error
        return self._result

    def cancel(self):
        self._cancelled.set()
        self._flush.set()
        self._queue.put(None)

    def _pcm(self, raw):
        if self.rate == 16000:
            return raw
        from scipy.signal import resample_poly
        factor = gcd(self.rate, 16000)
        return resample_poly(raw, 16000 // factor, self.rate // factor).astype(np.float32)

    def _run(self):
        raw = np.zeros(0, dtype=np.float32)
        parts = []
        scanned = 0
        consumed_total = 0
        try:
            final = False
            while not self._cancelled.is_set():
                item = self._queue.get()
                incoming = []
                while True:
                    if item is None:
                        final = True
                        break
                    incoming.append(item)
                    try:
                        item = self._queue.get_nowait()
                    except queue.Empty:
                        break
                if self._cancelled.is_set():
                    return
                if incoming:
                    raw = np.concatenate([raw, *incoming])
                if final:
                    if len(raw):
                        parts.append(self.recognize(self._pcm(raw), cancel=self._cancelled))
                    if not self._cancelled.is_set():
                        self._result = clean_text(' '.join(parts))
                    return
                # VAD at most four times per audio second. While inference is
                # busy, incoming audio is coalesced into the next single pass.
                if len(raw) - scanned < self.rate // 4:
                    continue
                scanned = len(raw)
                pcm = self._pcm(raw)
                regions = self.detect(pcm)
                # Keep 400 ms of silence on either side of a cut. Never cut
                # continuous speech or use a guessed fixed word boundary.
                ends = [region['end'] for i, region in enumerate(regions)
                        if (regions[i + 1]['start'] if i + 1 < len(regions) else len(pcm))
                        - region['end'] >= 12800]
                if not ends:
                    continue
                cut = ends[-1] + 6400
                self._inflight_covered = (consumed_total + len(raw)
                                          if cut >= regions[-1]['end'] else -1)
                try:
                    text = self.recognize(pcm[:cut], cancel=self._flush)
                except InterruptedError:
                    # Preserve the entire uncommitted audio prefix. Finish
                    # will combine it with incoming samples in one final pass.
                    continue
                parts.append(text)
                consumed = cut * self.rate // 16000
                consumed_total += consumed
                raw = raw[consumed:]
                scanned = 0
        except Exception as error:
            self._error = error
        finally:
            self._finished.set()
