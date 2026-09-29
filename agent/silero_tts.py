"""Local Silero TTS runtime used by Luma.

The module deliberately keeps PyTorch optional. Importing Luma never imports
Torch unless the Silero model is actually present and selected.
"""
from __future__ import annotations

import array
import importlib.util
import pathlib
import threading
import wave
from typing import Any

MODEL_ID = "v5_5_ru"
SPEAKER = "baya"
SAMPLE_RATE = 48000
MODEL_URL = "https://models.silero.ai/models/tts/ru/v5_5_ru.pt"

_MODEL_LOCK = threading.RLock()
_MODEL: Any = None
_MODEL_PATH = ""


def default_model_path() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parents[1] / "models" / "tts" / "silero_v5_5_ru.pt"


def torch_available() -> bool:
    return importlib.util.find_spec("torch") is not None


def available(model_path: str | pathlib.Path | None = None) -> bool:
    path = pathlib.Path(model_path or default_model_path()).expanduser()
    return torch_available() and path.is_file()


def _load_model(model_path: str | pathlib.Path | None = None) -> Any:
    global _MODEL, _MODEL_PATH
    path = pathlib.Path(model_path or default_model_path()).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Silero model not found: {path}")

    with _MODEL_LOCK:
        if _MODEL is not None and _MODEL_PATH == str(path):
            return _MODEL

        import torch

        torch.set_num_threads(max(1, min(4, int(torch.get_num_threads() or 1))))
        model = torch.package.PackageImporter(str(path)).load_pickle("tts_models", "model")
        model.to(torch.device("cpu"))
        if hasattr(model, "eval"):
            model.eval()
        _MODEL = model
        _MODEL_PATH = str(path)
        return model


def _write_pcm16_wav(audio: Any, output_path: str | pathlib.Path, sample_rate: int) -> None:
    values = audio.detach().cpu().flatten().tolist()
    pcm = array.array("h")
    pcm.extend(
        max(-32768, min(32767, int(float(sample) * 32767.0)))
        for sample in values
    )
    if pcm.itemsize != 2:
        raise RuntimeError("Unexpected PCM sample size")
    path = pathlib.Path(output_path)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(int(sample_rate))
        wav.writeframes(pcm.tobytes())


def synthesize_to_wav(
    text: str,
    output_path: str | pathlib.Path,
    *,
    model_path: str | pathlib.Path | None = None,
    speaker: str = SPEAKER,
    sample_rate: int = SAMPLE_RATE,
) -> dict[str, Any]:
    clean = " ".join(str(text or "").split())
    if not clean:
        raise ValueError("Empty Silero TTS text")

    model = _load_model(model_path)
    audio = model.apply_tts(
        text=clean,
        speaker=str(speaker or SPEAKER),
        sample_rate=int(sample_rate),
        put_accent=True,
        put_yo=True,
    )
    _write_pcm16_wav(audio, output_path, int(sample_rate))
    return {
        "model_id": MODEL_ID,
        "speaker": str(speaker or SPEAKER),
        "sample_rate": int(sample_rate),
        "model_path": str(pathlib.Path(model_path or default_model_path()).expanduser()),
    }
