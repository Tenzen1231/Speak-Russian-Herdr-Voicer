import sys, threading, unittest
from pathlib import Path
import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from streaming import StreamingDictation


def regions(audio):
    active = np.flatnonzero(audio)
    if not len(active):
        return []
    boundaries = np.flatnonzero(np.diff(active) > 1)
    starts = np.r_[active[0], active[boundaries + 1]]
    ends = np.r_[active[boundaries] + 1, active[-1] + 1]
    return [dict(start=int(a), end=int(b)) for a, b in zip(starts, ends)]


class StreamingTests(unittest.TestCase):
    def test_capture_continues_during_inference_and_preserves_all_words(self):
        entered, release = threading.Event(), threading.Event()
        calls = []
        def recognize(audio, cancel=None):
            values = audio[audio != 0]
            calls.append(values.copy())
            if len(calls) == 1:
                entered.set()
                self.assertTrue(release.wait(3))
            return 'первая' if values[0] == 1 else 'вторая'
        s = StreamingDictation(recognize=recognize, detect=regions)
        s.feed(np.r_[np.ones(16000), np.zeros(16000)])
        self.assertTrue(entered.wait(3))  # Must start before Stop.
        s.feed(np.full(16000, 2))        # Microphone doesn't wait for recognition.
        release.set()
        self.assertEqual(s.finish(), 'первая вторая')
        self.assertEqual([len(a) for a in calls], [16000, 16000])
        self.assertTrue(np.all(calls[0] == 1) and np.all(calls[1] == 2))

    def test_continuous_speech_is_not_cut_and_final_word_is_kept(self):
        calls = []
        def recognize(audio, cancel=None):
            calls.append(audio.copy())
            return 'полная фраза'
        s = StreamingDictation(recognize=recognize, detect=regions)
        for _ in range(5):
            s.feed(np.ones(8000))
        self.assertEqual(s.finish(), 'полная фраза')
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(calls[0]), 40000)

    def test_cancellation_discards_inflight_result(self):
        entered, release = threading.Event(), threading.Event()
        def recognize(audio, cancel=None):
            entered.set()
            self.assertTrue(release.wait(3))
            return 'старое сообщение'
        s = StreamingDictation(recognize=recognize, detect=regions)
        s.feed(np.r_[np.ones(16000), np.zeros(16000)])
        self.assertTrue(entered.wait(3))
        s.cancel()
        release.set()
        self.assertEqual(s.finish(), '')

    def test_failure_reaches_caller_without_partial_text(self):
        def recognize(audio, cancel=None):
            raise RuntimeError('decoder failed')
        s = StreamingDictation(recognize=recognize, detect=regions)
        s.feed(np.ones(100))
        with self.assertRaisesRegex(RuntimeError, 'decoder failed'):
            s.finish()

    def test_stop_aborts_obsolete_prefix_and_redecodes_all_uncommitted_audio(self):
        entered = threading.Event()
        calls = []
        def recognize(audio, cancel=None):
            calls.append(audio.copy())
            if len(calls) == 1:
                entered.set()
                self.assertTrue(cancel.wait(3))
                raise InterruptedError()
            return 'всё сообщение'
        s = StreamingDictation(recognize=recognize, detect=regions)
        s.feed(np.r_[np.ones(16000), np.zeros(16000)])
        self.assertTrue(entered.wait(3))
        s.feed(np.full(16000, 2))
        self.assertEqual(s.finish(), 'всё сообщение')
        self.assertEqual(len(calls), 2)
        self.assertEqual(np.count_nonzero(calls[-1]), 32000)

    def test_stop_reuses_prefix_when_it_already_covers_all_speech(self):
        entered = threading.Event()
        def recognize(audio, cancel=None):
            if np.any(audio):
                entered.set()
                self.assertFalse(cancel.wait(.1))
                return 'готово'
            return ''
        s = StreamingDictation(recognize=recognize, detect=regions)
        s.feed(np.r_[np.ones(16000), np.zeros(16000)])
        self.assertTrue(entered.wait(3))
        self.assertEqual(s.finish(), 'готово')

    def test_empty_recording(self):
        s = StreamingDictation(recognize=lambda a, cancel=None: self.fail('unexpected inference'), detect=regions)
        self.assertEqual(s.finish(), '')


if __name__ == '__main__':
    unittest.main()
