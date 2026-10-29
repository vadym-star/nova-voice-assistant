"""Offline speech-to-text with faster-whisper."""

import logging
import os
import re
import site
import sys
from pathlib import Path

import numpy as np

from . import config

log = logging.getLogger(__name__)


def _add_cuda_dll_dirs() -> None:
    """Make pip-installed CUDA libraries (nvidia-cublas-cu12, nvidia-cudnn-cu12) loadable."""
    if sys.platform != "win32":
        return
    roots = site.getsitepackages() + [site.getusersitepackages()]
    for root in roots:
        nvidia = Path(root) / "nvidia"
        if not nvidia.is_dir():
            continue
        for bin_dir in nvidia.glob("*/bin"):
            os.add_dll_directory(str(bin_dir))
            os.environ["PATH"] = str(bin_dir) + os.pathsep + os.environ["PATH"]


def _cuda_ready() -> bool:
    """Can CUDA really run? Without the cuBLAS/cuDNN libraries CTranslate2 may kill the
    whole process instead of raising an error, so look for them first."""
    import ctypes

    for dll in ("cublas64_12.dll", "cudnn_ops64_9.dll"):
        try:
            ctypes.WinDLL(dll)
        except OSError:
            log.info("CUDA not used: %s not found", dll)
            return False
    return True


class SpeechToText:
    def __init__(self, model_size: str, device: str, language: str):
        from faster_whisper import WhisperModel

        _add_cuda_dll_dirs()
        # "auto": recognise whatever language is spoken.
        self.language = None if language == "auto" else language
        # Per-language prompts that bias whisper towards the assistant's name and
        # command words (and, for Chinese, towards simplified characters).
        self.hints: dict[str, str] = {}
        self.last_language = ""
        self.model_size = model_size
        # A small model that is already loaded (the name gate's): it spots the language
        # in ~20 ms instead of ~70 ms. Set by the app.
        self.detector = None
        # int8 weights: half the video memory of float16 (~360 vs ~760 MB for "small")
        # for ~30 ms more per phrase and the same words. float16 is for older GPUs.
        cuda = [("cuda", "int8_float16"), ("cuda", "float16")] if _cuda_ready() else []
        attempts = cuda + [("cpu", "int8")] if device == "auto" else (
            cuda if device == "cuda" else [(device, "int8")])
        last_error = None
        for dev, compute in attempts:
            try:
                self.model = self._load(WhisperModel, model_size, dev, compute)
                # Warm-up run: CUDA errors only surface on first inference, and
                # transcribe() is lazy, so the segments must actually be consumed.
                noise = (np.random.default_rng(0).standard_normal(16000) * 0.1).astype(np.float32)
                list(self.model.transcribe(noise, language=self.language)[0])
                self.device = dev
                log.info("STT: whisper %s on %s (%s)", model_size, dev, compute)
                return
            except Exception as e:  # noqa: BLE001 - any backend failure means "try the next one"
                last_error = e
                log.warning("STT on %s unavailable: %s", dev, e)
        raise RuntimeError(f"Не удалось загрузить распознавание речи: {last_error}")

    @staticmethod
    def _load(WhisperModel, model_size, device, compute):
        from faster_whisper.utils import download_model

        root = str(config.MODELS_DIR / "whisper")
        try:  # downloaded already? then never touch the network
            path = download_model(model_size, local_files_only=True, cache_dir=root)
        except Exception:  # noqa: BLE001 - not downloaded yet (first start)
            path = download_model(model_size, cache_dir=root)
        return WhisperModel(path, device=device, compute_type=compute)

    def _detect(self, audio: np.ndarray) -> str:
        if self.detector is not None:
            language, probability, _ = self.detector.detect_language(audio)
            if probability >= 0.8:
                return language
        # Unsure (close languages like Russian and Ukrainian): ask the bigger model.
        return self.model.detect_language(audio)[0]

    def transcribe(self, audio: np.ndarray) -> str:
        try:
            return self._transcribe(audio)
        except RuntimeError as e:
            if "cuda" not in str(e).lower() and "cublas" not in str(e).lower():
                raise
            log.warning("GPU failed (%s): reloading speech recognition", e)
            self._recover()
            return self._transcribe(audio)

    def _recover(self) -> None:
        """The CUDA context is gone (driver reset, sleep): a fresh model, CPU if the GPU won't come back."""
        from faster_whisper import WhisperModel

        for dev, compute in ((self.device, "int8_float16" if self.device == "cuda" else "int8"), ("cpu", "int8")):
            try:
                self.model = self._load(WhisperModel, self.model_size, dev, compute)
                self.device = dev
                return
            except Exception as e:  # noqa: BLE001
                log.warning("Reload on %s failed: %s", dev, e)

    def _transcribe(self, audio: np.ndarray) -> str:
        if audio.size < 16000 * 0.3:
            return ""
        language = self.language
        if language is None:
            # Detect first, then decode with that language's hint: a fixed hint
            # in one language would pull detection towards it.
            language = self._detect(audio)
        self.last_language = language
        if audio.size < 16000 * 1.5:
            # Whisper is unreliable on very short clips: pad with silence.
            audio = np.concatenate([audio, np.zeros(int(16000 * 1.5) - audio.size, dtype=np.float32)])
        segments, _ = self.model.transcribe(
            audio, language=language, beam_size=1, temperature=0.0,
            vad_filter=True, initial_prompt=self.hints.get(language) or None,
            condition_on_previous_text=False,
        )
        kept = []
        for s in segments:
            # Whisper "hears" words in silence and noise; drop what it isn't sure was speech.
            # Same rule as OpenAI's Whisper: silence only when it both looks like no speech AND
            # the model is unsure — either alone also throws away short real phrases ("Нова!").
            if (s.no_speech_prob > 0.6 and s.avg_logprob < -1.0) or s.compression_ratio > 2.4:
                log.info("Dropped unsure segment: %r (no_speech %.2f, logprob %.2f)",
                         s.text, s.no_speech_prob, s.avg_logprob)
                continue
            kept.append(s.text.strip())
        text = " ".join(kept).strip()
        if _is_phantom(text):
            log.info("Dropped phantom phrase: %r", text)
            return ""
        return text


_GATE_PROMPTS = {
    "ru": "{name}, открой. Эй, {name}. {name}, нажми.",
    "uk": "{name}, відкрий. Гей, {name}.",
    "en": "{name}, open. Hey {name}. {name}, click.",
    "zh": "{name}，打开。嘿，{name}。{name}，点击。",
}


class NameGate:
    """Cheap first look at a phrase in always-listen mode: is it speech, and does it
    start with the assistant's name? Whisper "tiny" on the first ~1.5 s costs ~50 ms on
    a GPU, about 7x less than the main model — so chatter, music and game sound never
    reach the big model, and Nova can react before the user has finished talking."""

    SECONDS = 1.5

    def __init__(self, device: str):
        from faster_whisper import WhisperModel
        from faster_whisper.vad import VadOptions

        self._vad_options = VadOptions(min_speech_duration_ms=250, threshold=0.5)
        self.model = None
        for compute in (("int8_float16", "float16") if device == "cuda" else ("int8",)):
            try:
                self.model = SpeechToText._load(WhisperModel, "tiny", device, compute)
                self.model.detect_language(np.zeros(16000, np.float32))  # CUDA errors show up here
                break
            except Exception as e:  # noqa: BLE001 - older GPUs lack int8: try float16
                log.warning("Name gate with %s unavailable: %s", compute, e)
        if self.model is None:
            raise RuntimeError("name gate model failed to load")
        log.info("Name gate: whisper tiny on %s", device)

    def has_speech(self, audio: np.ndarray) -> bool:
        from faster_whisper.vad import get_speech_timestamps

        return bool(get_speech_timestamps(audio, self._vad_options))  # ~10 ms

    def text(self, audio: np.ndarray, name: str, language: str | None) -> str:
        audio = audio[: int(16000 * self.SECONDS)]
        if audio.size < 16000:
            audio = np.concatenate([audio, np.zeros(16000 - audio.size, dtype=np.float32)])
        segments, _ = self.model.transcribe(
            audio, language=language, beam_size=1, without_timestamps=True, condition_on_previous_text=False,
            temperature=0.0, max_new_tokens=16,  # one quick pass: retries made it 250 ms instead of 50
            # The name in the prompt is what makes the tiny model spell it right ("Нава",
            # "Noga" without it). It also makes it "hear" the name in other speech now and
            # then — the main model checks every early wake before Nova reacts (app.py).
            initial_prompt=_GATE_PROMPTS.get(language or "", _GATE_PROMPTS["en"]).format(name=name))
        text = " ".join(s.text.strip() for s in segments).strip()
        log.debug("Name gate heard: %r", text)  # mostly other people's speech: not kept in the log
        return text


# What Whisper typically "transcribes" from silence: subtitle credits and outros.
_PHANTOMS = re.compile(
    r"^(?:продолжение следует|спасибо за просмотр|субтитры (?:сделал|создавал|подготовил).*|"
    r"редактор субтитров.*|подписывайтесь на канал|до новых встреч|thank you(?: for watching)?|"
    r"thanks for watching|please subscribe.*|subtitles by.*|you|bye|"
    r"谢谢观看|请不吝点赞.*|字幕.*|中文字幕.*)[.!…]*$",
    re.I,
)


def _is_phantom(text: str) -> bool:
    t = text.strip().strip(".!?…,").lower()
    return not t or bool(_PHANTOMS.match(t)) or bool(re.fullmatch(r"[\W\d_]+", t))
