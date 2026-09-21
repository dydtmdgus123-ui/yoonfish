"""Vercel 엔트리: /api/config"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path[:0] = [str(HERE), str(ROOT)]

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from main import app  # noqa: E402, F401
