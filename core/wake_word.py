"""
Local wake-word detection for JARVIS ("Hey Jarvis").

Design goals:
  • ZERO cost when the feature is off — openwakeword is imported ONLY inside
    start()/install helpers, never at module load. If the user never enables
    wake word, none of this touches the app.
  • ZERO latency on the audio path — the microphone callback only ever does a
    cheap, non-blocking queue push (feed()); the actual model inference runs in
    this module's own background thread, so the real-time audio thread and the
    Gemini stream are never slowed.
  • Fully local & offline — audio fed here never leaves the machine; there is no
    network call except the one-time model download the user triggers from the UI.

openwakeword ships small ONNX models (a few MB each) and runs comfortably on a
CPU. Its pretrained set is a FIXED list of phrases — alexa, hey_mycroft,
hey_jarvis, hey_rhasspy, timer, weather — with no model for arbitrary words.

To use a different wake phrase (e.g. "Connect") you supply a custom model:
drop its `<name>.onnx` (or `.tflite`) into config/wake_models/ and set
wake_word_model = "<name>" in config. This module then loads that file instead
of a pretrained name, and the UI phrase follows wake_word_label. Without such a
model the default stays "hey_jarvis", so the interface never claims a phrase the
detector cannot actually hear.
"""
from __future__ import annotations

import queue
import subprocess
import sys
import threading
from pathlib import Path
from typing import Callable

# Default pretrained openwakeword model ("Hey Jarvis"). The ACTIVE model is read
# from config at start()/readiness time via _model_name(); this is only the
# fallback when nothing is configured.
WAKE_MODEL = "hey_jarvis"

# Custom (user-trained) models live here, one <name>.onnx/.tflite per phrase.
CUSTOM_DIR = Path(__file__).resolve().parent.parent / "config" / "wake_models"

# openwakeword's fixed pretrained set — anything else must be a custom file.
_PRETRAINED = {"alexa", "hey_mycroft", "hey_jarvis", "hey_rhasspy", "timer", "weather"}


def _model_name() -> str:
    """Configured wake model name, defaulting to WAKE_MODEL. Never raises."""
    try:
        from memory.config_manager import get_wake_word_model
        return get_wake_word_model() or WAKE_MODEL
    except Exception:
        return WAKE_MODEL


def wake_label() -> str:
    """The phrase to show the user ('Hey Jarvis', 'Connect', …). Never raises."""
    try:
        from memory.config_manager import get_wake_word_label
        return get_wake_word_label() or "Hey Jarvis"
    except Exception:
        return "Hey Jarvis"


def _custom_model_file(name: str) -> Path | None:
    """Path to a custom model file for `name`, if one is present on disk."""
    try:
        for ext in ("onnx", "tflite"):
            hit = sorted(CUSTOM_DIR.glob(f"{name}.{ext}"))
            if hit:
                return hit[0]
    except Exception:
        pass
    return None
# Score in [0,1]; above this counts as a detection. Tunable per environment.
DEFAULT_THRESHOLD = 0.5
# Mic frames arrive at 16 kHz int16; this is just the detector's input rate.
SAMPLE_RATE = 16000

# Porcupine built-in keywords (free, no .ppn file needed).
_PORCUPINE_BUILTIN = {
    "alexa", "americano", "blueberry", "bumblebee", "computer",
    "grapefruit", "grasshopper", "hey google", "hey siri",
    "jarvis", "ok google", "picovoice", "pineapple", "porcupine", "terminator",
}


def _custom_ppn_file(name: str) -> Optional[Path]:
    """Path to a Porcupine .ppn wake word file for `name`, if present."""
    try:
        for p in sorted(CUSTOM_DIR.glob(f"{name}*.ppn")):
            return p
    except Exception:
        pass
    return None


def _wake_engine() -> str:
    """'porcupine' or 'openwakeword'. Never raises."""
    try:
        from memory.config_manager import get_wake_engine
        return get_wake_engine()
    except Exception:
        return "openwakeword"


def is_installed() -> bool:
    """True if the active engine's package is importable (no model check)."""
    import importlib.util
    try:
        if _wake_engine() == "porcupine":
            return importlib.util.find_spec("pvporcupine") is not None
        return importlib.util.find_spec("openwakeword") is not None
    except Exception:
        return False


def is_ready() -> bool:
    """True when the active engine is installed and can start. Never raises."""
    try:
        if _wake_engine() == "porcupine":
            return _porcupine_ready()
        return _oww_ready()
    except Exception:
        return False


def _oww_ready() -> bool:
    """openwakeword file-existence check."""
    import importlib.util
    if not importlib.util.find_spec("openwakeword"):
        return False
    try:
        name = _model_name()
        import openwakeword
        models_dir = Path(openwakeword.__file__).resolve().parent / "resources" / "models"
        if not models_dir.is_dir():
            return False
        if name not in _PRETRAINED:
            has_wake = _custom_model_file(name) is not None
        else:
            has_wake = (any(models_dir.glob(f"{name}*.onnx"))
                        or any(models_dir.glob(f"{name}*.tflite")))
        has_mel = (any(models_dir.glob("melspectrogram*.onnx"))
                   or any(models_dir.glob("melspectrogram*.tflite")))
        has_emb = (any(models_dir.glob("embedding_model*.onnx"))
                   or any(models_dir.glob("embedding_model*.tflite")))
        return bool(has_wake and has_mel and has_emb)
    except Exception:
        return False


def _porcupine_ready() -> bool:
    """Porcupine is ready when pvporcupine is installed + AccessKey is set +
    either the keyword is built-in OR a .ppn file exists."""
    import importlib.util
    if not importlib.util.find_spec("pvporcupine"):
        return False
    try:
        from memory.config_manager import get_porcupine_key
        if not get_porcupine_key():
            return False
        name = _model_name()
        return name in _PORCUPINE_BUILTIN or _custom_ppn_file(name) is not None
    except Exception:
        return False


def install_and_download(logger: Callable[[str], None] = print,
                         notify: Callable[[str], None] | None = None) -> tuple[bool, str]:
    """
    One-click setup for the UI button: pip-install openwakeword if missing, then
    download the wake model. Returns (ok, message). Never raises — every failure
    is reported through the returned message and the logger.
    """
    _tell = notify or (lambda _msg: None)
    name  = _model_name()
    engine = _wake_engine()
    try:
        if engine == "porcupine":
            return _install_porcupine(name, logger, _tell)
        return _install_oww(name, logger, _tell)
    except Exception as e:
        return False, f"setup error: {e}"


def _install_porcupine(name: str, logger, tell) -> tuple[bool, str]:
    import importlib.util
    if not importlib.util.find_spec("pvporcupine"):
        logger("Wake word: installing pvporcupine (one-time)…")
        tell("Wake word: installing pvporcupine (one-time)…")
        r = subprocess.run(
            [sys.executable, "-m", "pip", "install", "pvporcupine"],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            tail = (r.stderr or r.stdout or "").strip().splitlines()[-1:] or [""]
            return False, f"pip install pvporcupine failed: {tail[0][:160]}"
    from memory.config_manager import get_porcupine_key
    if not get_porcupine_key():
        return False, ("No Porcupine AccessKey set. Get one free at "
                       "console.picovoice.ai and enter it via ⚙ → WAKE WORD ENGINE.")
    if name not in _PORCUPINE_BUILTIN and _custom_ppn_file(name) is None:
        return False, (f"No .ppn file for '{name}'. Download it from "
                       f"console.picovoice.ai and drop it in {CUSTOM_DIR}.")
    if not _porcupine_ready():
        return False, "Porcupine installed but could not validate."
    logger("Wake word (Porcupine): ready.")
    return True, "Porcupine wake word ready."


def _install_oww(name: str, logger, tell) -> tuple[bool, str]:
    import importlib.util
    if not importlib.util.find_spec("openwakeword"):
        logger("Wake word: installing openwakeword (one-time)…")
        tell("Wake word: installing openwakeword (one-time)…")
        r = subprocess.run(
            [sys.executable, "-m", "pip", "install", "openwakeword"],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            tail = (r.stderr or r.stdout or "").strip().splitlines()[-1:] or [""]
            return False, f"pip install failed: {tail[0][:160]}"
    if name not in _PRETRAINED and _custom_model_file(name) is None:
        return False, (f"No model for '{name}'. openwakeword has no pretrained "
                       f"model for it — drop a trained {name}.onnx into "
                       f"{CUSTOM_DIR} first.")
    logger("Wake word: downloading models…")
    tell("Wake word: downloading models…")
    try:
        import openwakeword.utils as _u
        try:
            _u.download_models([name] if name in _PRETRAINED else [])
        except TypeError:
            _u.download_models()
    except Exception as e:
        return False, f"model download failed: {e}"
    if not is_ready():
        return False, "installed, but the wake model could not be loaded."
    logger("Wake word: ready.")
    return True, "Wake word installed and ready."


class WakeWordDetector:
    """
    Runs the wake model in a dedicated thread. The mic thread calls feed() with
    raw int16 frames; detections invoke on_detect() (called from this thread —
    the callback must marshal to whatever loop/UI it needs).
    """

    def __init__(self, on_detect: Callable[[], None],
                 threshold: float = DEFAULT_THRESHOLD,
                 logger: Callable[[str], None] = print,
                 notify: Callable[[str], None] | None = None):
        self._on_detect = on_detect
        self._threshold = threshold
        self._logger    = logger
        self._notify    = notify or (lambda _msg: None)
        self._queue: queue.Queue = queue.Queue(maxsize=50)
        self._thread: threading.Thread | None = None
        self._running = False
        self._model   = None   # openwakeword Model OR pvporcupine handle
        self._ready   = False
        self._engine  = "openwakeword"
        self._wake_key = WAKE_MODEL.lower()
        self._ppn_buf: list = []   # sample buffer for porcupine frame alignment
        self._ppn_frame_len: int = 512

    def start(self) -> bool:
        """Load the model and spawn the inference thread. Returns True on success.
        Safe to call again — a no-op if already running. Never raises."""
        if self._running:
            return True
        self._engine = _wake_engine()
        if self._engine == "porcupine":
            return self._start_porcupine()
        return self._start_oww()

    def _start_oww(self) -> bool:
        name   = _model_name()
        custom = _custom_model_file(name)
        self._wake_key = (custom.stem if custom else name).lower()
        for attempt in range(2):
            try:
                from openwakeword.model import Model
                arg = str(custom) if custom else name
                self._model = Model(wakeword_models=[arg], inference_framework="onnx")
                break
            except Exception as e:
                if attempt == 0 and "circular import" in str(e).lower():
                    # openwakeword circular-import bug on first load — purge + retry
                    import sys as _sys
                    for k in list(_sys.modules):
                        if k.startswith("openwakeword"):
                            _sys.modules.pop(k, None)
                    continue
                self._logger(f"Wake word: could not load model '{name}' — {e}")
                self._notify("Wake word unavailable — use the WAKE NOW button.")
                self._model = None
                return False
        self._running = True
        self._ready   = True
        self._thread  = threading.Thread(target=self._loop, daemon=True, name="WakeWordThread")
        self._thread.start()
        self._logger(f"Wake word (openwakeword): listening for '{wake_label()}'.")
        return True

    def _start_porcupine(self) -> bool:
        name = _model_name()
        try:
            import pvporcupine
            from memory.config_manager import get_porcupine_key
            key = get_porcupine_key()
            if not key:
                self._logger("Wake word: Porcupine AccessKey not set.")
                self._notify("Set Porcupine AccessKey via ⚙ → WAKE WORD ENGINE.")
                return False
            ppn = _custom_ppn_file(name)
            if ppn:
                self._model = pvporcupine.create(access_key=key, keyword_paths=[str(ppn)])
                self._logger(f"Wake word (Porcupine): loaded custom model {ppn.name}.")
            elif name in _PORCUPINE_BUILTIN:
                self._model = pvporcupine.create(access_key=key, keywords=[name])
                self._logger(f"Wake word (Porcupine): using built-in keyword '{name}'.")
            else:
                self._logger(f"Wake word: no .ppn for '{name}' — "
                             f"download from console.picovoice.ai → {CUSTOM_DIR}")
                self._notify(f"Drop '{name}.ppn' in config/wake_models/ for Porcupine.")
                return False
            self._ppn_frame_len = self._model.frame_length
            self._ppn_buf = []
        except Exception as e:
            self._logger(f"Wake word (Porcupine): init failed — {e}")
            self._notify("Porcupine failed to start — check AccessKey.")
            self._model = None
            return False
        self._running = True
        self._ready   = True
        self._thread  = threading.Thread(target=self._loop, daemon=True, name="WakeWordThread")
        self._thread.start()
        self._logger(f"Wake word (Porcupine): listening for '{wake_label()}'.")
        return True

    def stop(self) -> None:
        self._running = False
        try:
            self._queue.put_nowait(None)
        except Exception:
            pass
        if self._engine == "porcupine" and self._model is not None:
            try:
                self._model.delete()
            except Exception:
                pass
        self._model = None
        self._ready = False
        self._ppn_buf = []

    @property
    def ready(self) -> bool:
        return self._ready

    def feed(self, frame_int16) -> None:
        """Called from the mic callback (real-time thread). Non-blocking."""
        if not self._running:
            return
        try:
            data = frame_int16[:, 0].copy() if getattr(frame_int16, "ndim", 1) > 1 else frame_int16.copy()
            if self._engine == "porcupine":
                # Buffer into fixed-size Porcupine frames then queue each one
                self._ppn_buf.extend(data.tolist())
                while len(self._ppn_buf) >= self._ppn_frame_len:
                    chunk = self._ppn_buf[:self._ppn_frame_len]
                    self._ppn_buf = self._ppn_buf[self._ppn_frame_len:]
                    try:
                        self._queue.put_nowait(chunk)
                    except queue.Full:
                        pass
            else:
                self._queue.put_nowait(data)
        except queue.Full:
            pass
        except Exception:
            pass

    def _loop(self) -> None:
        import numpy as np
        while self._running:
            try:
                frame = self._queue.get()
                if frame is None or not self._running:
                    break
                if self._engine == "porcupine":
                    result = self._model.process(frame)
                    if result >= 0:
                        self._drain()
                        try:
                            self._on_detect()
                        except Exception as e:
                            self._logger(f"Wake word: on_detect error — {e}")
                else:
                    scores = self._model.predict(np.asarray(frame, dtype=np.int16))
                    score = 0.0
                    if isinstance(scores, dict):
                        key = self._wake_key
                        for k, v in scores.items():
                            if key in k.lower():
                                score = max(score, float(v))
                        if score == 0.0 and scores:
                            score = max(float(v) for v in scores.values())
                    if score >= self._threshold:
                        self._drain()
                        try:
                            self._on_detect()
                        except Exception as e:
                            self._logger(f"Wake word: on_detect error — {e}")
            except Exception as e:
                self._logger(f"Wake word: inference error — {e}")

    def _drain(self) -> None:
        try:
            while True:
                self._queue.get_nowait()
        except Exception:
            pass
