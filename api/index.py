"""Vercel entrypoint.

Vercel serves every route through this function; server.py stays the app and
still runs standalone with `python3 server.py`.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from server import app  # noqa: E402,F401
