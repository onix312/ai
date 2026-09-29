# Luma Packaging 1.0

Packaging 1.0 turns the local assistant into a normal per-user Windows app.

## Result

The build produces `dist/LumaPackage` with:

- `app/Luma.exe` and PyInstaller runtime files
- `install.ps1`
- `README.txt`
- optional `models/` when built with `-WithVoice`

`Luma.exe` is the single desktop entry point. It reuses an already-running Luma backend when one exists, otherwise starts the speech and agent loopback servers in background threads, starts the PySide6 Native UI on the main thread, and shuts down the backend it owns when the UI exits.

There is no console window in the packaged build.

## Build

On Windows:

`powershell -ExecutionPolicy Bypass -File .\scripts\build_luma_windows.ps1 -Clean`

To also place the recommended `ru_RU-irina-medium` model in the package:

`powershell -ExecutionPolicy Bypass -File .\scripts\build_luma_windows.ps1 -Clean -WithVoice`

The standalone build toolchain is pinned to Python 3.11 because the current OCR dependency has compatible Windows wheels there. The build environment is isolated in `.luma-build-venv`.

## Install

From inside `dist\LumaPackage`:

`powershell -ExecutionPolicy Bypass -File .\install.ps1`

Default installation path is `%LOCALAPPDATA%\Luma`. The installer creates Start Menu, Desktop, and per-user Startup shortcuts.

Options: `-NoAutostart`, `-NoDesktopShortcut`, `-NoLaunch`, `-Uninstall`.

Uninstall removes application files and shortcuts but intentionally keeps user data and downloaded models.

## Data

Packaging does not move or delete Luma/PrintFlow user databases. Application binaries and mutable user state remain separate.

## Voice assets

The packaged host looks for `models\tts\luma.onnx` and `piper\piper.exe` next to the executable or in the parent installation asset directory. If HQ TTS is unavailable, the existing system voice fallback remains active.

## CI

`.github/workflows/luma-packaging.yml` verifies desktop-host compilation, packaging contracts, a real PyInstaller build on `windows-latest`, and the presence of `Luma.exe` plus `install.ps1` in the final package.
