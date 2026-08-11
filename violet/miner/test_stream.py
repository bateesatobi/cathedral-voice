"""Deprecated shim — use tts_test_stream.py."""
from pathlib import Path
import runpy

if __name__ == "__main__":
    runpy.run_path(str(Path(__file__).with_name("tts_test_stream.py")), run_name="__main__")
