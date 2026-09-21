"""Vercel Python Serverless 엔트리포인트.

`public/` 정적 파일은 Vercel CDN이 서빙하고, `/api/*` 만 이 ASGI 앱으로 들어옵니다.
로컬은 `uvicorn main:app` 과 동일한 FastAPI `app` 객체를 사용합니다.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from main import app  # noqa: E402, F401
