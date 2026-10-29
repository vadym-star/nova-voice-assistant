"""Microphone capture: push-to-talk recording and an always-on utterance stream."""

import queue
import threading

import numpy as np
import sounddevice as sd

SAMPLE_RATE = 16000
BLOCK = 512  # 32 ms at 16 kHz


class Recorder:
    """Records from the default microphone between start() and stop()."""

    def __init__(self):
        self._chunks: list[np.ndarray] = []
        self._stream: sd.InputStream | None = None
        self.level = 0.0  # last RMS, for the UI

    def _callback(self, indata, frames, time, status):
        mono = indata[:, 0].copy()
        self._chunks.append(mono)
        self.level = float(np.sqrt(np.mean(mono**2)))

    def start(self) -> None:
        self._chunks = []
        self.level = 0.0
        self._stream = sd.InputStream(
            samplerate=SAMPLE_RATE, channels=1, dtype="float32",
            blocksize=BLOCK, callback=self._callback,
        )
        self._stream.start()

    def stop(self) -> np.ndarray:
        if self._stream:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        if not self._chunks:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(self._chunks)


class UtteranceListener:
    """Always-on listener. For each spoken phrase it yields ("early", first ~1.5 s)
    while the person is still talking — enough to hear the assistant's name — and
    then ("end", whole phrase).

    Uses a simple adaptive energy gate: speech starts when the level rises well
    above the background noise and ends after `silence_s` of quiet.
    """

    EARLY_S = 1.5

    def __init__(self, silence_s: float = 0.7, max_s: float = 40.0):
        self.silence_blocks = int(silence_s * SAMPLE_RATE / BLOCK)
        self.max_blocks = int(max_s * SAMPLE_RATE / BLOCK)
        self._q: queue.Queue[np.ndarray] = queue.Queue()
        self._stop = threading.Event()
        self.paused = threading.Event()

    def _callback(self, indata, frames, time, status):
        if not self.paused.is_set():
            self._q.put(indata[:, 0].copy())

    def stop(self) -> None:
        self._stop.set()

    def flush(self) -> None:
        """Drop what was heard while Nova was busy: it isn't a new command."""
        while True:
            try:
                self._q.get_nowait()
            except queue.Empty:
                return

    def utterances(self):
        early_blocks = int(self.EARLY_S * SAMPLE_RATE / BLOCK)
        noise = 0.005
        pre_roll: list[np.ndarray] = []
        speech: list[np.ndarray] = []
        quiet = 0
        with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                            blocksize=BLOCK, callback=self._callback):
            silent_for = 0.0
            zeros = 0
            while not self._stop.is_set():
                try:
                    block = self._q.get(timeout=0.2)
                    silent_for = 0.0
                except queue.Empty:
                    if not self.paused.is_set():
                        silent_for += 0.2
                        if silent_for > 3.0:  # no audio at all: the microphone is gone
                            raise OSError("microphone stopped delivering audio")
                    continue
                rms = float(np.sqrt(np.mean(block**2)))
                zeros = zeros + 1 if rms == 0.0 else 0
                if zeros * BLOCK / SAMPLE_RATE > 5.0:
                    # Exact digital silence for 5 s: Windows blocks the mic (privacy) or it's muted.
                    raise OSError("microphone delivers only silence")
                loud = rms > max(noise * 3.0, 0.01)
                if not speech:
                    if loud:
                        speech = pre_roll + [block]
                        quiet = 0
                    else:
                        noise = 0.95 * noise + 0.05 * rms
                        pre_roll = (pre_roll + [block])[-8:]
                    continue
                speech.append(block)
                quiet = 0 if loud else quiet + 1
                if len(speech) == early_blocks:
                    yield "early", np.concatenate(speech)
                if quiet >= self.silence_blocks or len(speech) >= self.max_blocks:
                    if len(speech) - quiet > 6:  # ignore clicks and coughs
                        yield "end", np.concatenate(speech)
                    speech, pre_roll, quiet = [], [], 0
