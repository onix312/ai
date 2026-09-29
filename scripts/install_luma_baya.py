"""Install the recommended local Luma voice: Silero v5_5_ru / baya.

Run from repository root:
    python scripts/install_luma_baya.py
"""
from __future__ import annotations

import argparse
import pathlib
import subprocess
import sys
import urllib.request

MODEL_URL = "https://models.silero.ai/models/tts/ru/v5_5_ru.pt"
ROOT = pathlib.Path(__file__).resolve().parents[1]
MODEL_PATH = ROOT / "models" / "tts" / "silero_v5_5_ru.pt"


def have_torch() -> bool:
    try:
        import torch  # noqa: F401
        return True
    except ImportError:
        return False


def install_torch() -> None:
    print("Installing CPU PyTorch for Silero...")
    subprocess.check_call([
        sys.executable, "-m", "pip", "install", "--upgrade",
        "torch", "--index-url", "https://download.pytorch.org/whl/cpu",
    ])


def download_model(force: bool = False) -> None:
    MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    if MODEL_PATH.is_file() and MODEL_PATH.stat().st_size > 1_000_000 and not force:
        print(f"Model already present: {MODEL_PATH}")
        return

    temp = MODEL_PATH.with_suffix(".pt.part")
    print(f"Downloading Silero v5_5_ru to {MODEL_PATH}...")
    try:
        with urllib.request.urlopen(MODEL_URL, timeout=60) as response, temp.open("wb") as out:
            total = int(response.headers.get("Content-Length") or 0)
            done = 0
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                out.write(chunk)
                done += len(chunk)
                if total:
                    print(f"  {done * 100 // total:3d}%", end="\r", flush=True)
        temp.replace(MODEL_PATH)
    finally:
        if temp.exists():
            temp.unlink(missing_ok=True)
    print("\nModel ready.")


def smoke_test() -> None:
    sys.path.insert(0, str(ROOT))
    from agent import silero_tts

    if not silero_tts.available(MODEL_PATH):
        raise RuntimeError("Silero Baya is not available after installation")
    print("Luma voice ready: Silero v5_5_ru · baya · 48 kHz")


def main() -> int:
    parser = argparse.ArgumentParser(description="Install Luma Silero Baya voice")
    parser.add_argument("--force-model", action="store_true", help="redownload the model")
    parser.add_argument("--skip-torch", action="store_true", help="do not install PyTorch")
    args = parser.parse_args()

    if not have_torch():
        if args.skip_torch:
            print("PyTorch is missing. Install torch>=2.0 and rerun.", file=sys.stderr)
            return 2
        install_torch()

    download_model(force=args.force_model)
    smoke_test()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
