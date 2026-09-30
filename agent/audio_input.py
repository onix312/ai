"""Enumerate and resolve local microphone devices for speech input."""

from __future__ import annotations


def input_devices() -> list[dict[str, str]]:
    try:
        import sounddevice as sd
        apis = sd.query_hostapis()
        return [
            {"id": f"{apis[device['hostapi']]['name']}:{device['name']}",
             "name": f"{device['name']} · {apis[device['hostapi']]['name']}"}
            for device in sd.query_devices()
            if device["max_input_channels"] > 0
        ]
    except Exception:
        return []


def resolve_input_device(device_id: str) -> int:
    import sounddevice as sd
    apis = sd.query_hostapis()
    for index, device in enumerate(sd.query_devices()):
        if (device["max_input_channels"] > 0
                and f"{apis[device['hostapi']]['name']}:{device['name']}" == device_id):
            return index
    raise ValueError("Выбранный микрофон больше не подключён")
