"""List and play through a chosen local output device."""

from __future__ import annotations

import sys
import wave


def output_devices() -> list[dict[str, str]]:
    try:
        import sounddevice as sd
        apis = sd.query_hostapis()
        return [
            {"id": f"{apis[device['hostapi']]['name']}:{device['name']}",
             "name": f"{device['name']} · {apis[device['hostapi']]['name']}"}
            for device in sd.query_devices()
            if device["max_output_channels"] > 0
        ]
    except Exception:
        return []


def resolve_output_device(device_id: str) -> int:
    import sounddevice as sd
    apis = sd.query_hostapis()
    for index, device in enumerate(sd.query_devices()):
        if (device["max_output_channels"] > 0
                and f"{apis[device['hostapi']]['name']}:{device['name']}" == device_id):
            return index
    raise ValueError("Выбранное устройство вывода больше не подключено")


def play_wav(path: str, device_id: str) -> None:
    import sounddevice as sd
    device = resolve_output_device(device_id)
    with wave.open(path, "rb") as wav:
        if wav.getcomptype() != "NONE" or wav.getsampwidth() != 2:
            raise ValueError("Неподдерживаемый формат WAV для выбранного устройства")
        with sd.RawOutputStream(
            samplerate=wav.getframerate(), channels=wav.getnchannels(),
            dtype="int16", device=device,
        ) as stream:
            while frames := wav.readframes(4096):
                stream.write(frames)


if __name__ == "__main__":
    try:
        play_wav(sys.argv[1], sys.argv[2])
    except Exception as exc:
        print(f"Аудиовывод: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
