"""Resolve the folder that holds user data, for both source and frozen runs.

In a PyInstaller onefile build, __file__ points inside the temporary extraction
directory; user data must live next to the executable instead.
"""
import sys
from pathlib import Path


def app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent
