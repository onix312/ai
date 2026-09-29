"""Packaged desktop entry point: python luma.py or PyInstaller Luma.exe."""
from agent.desktop import main

if __name__ == "__main__":
    raise SystemExit(main())
