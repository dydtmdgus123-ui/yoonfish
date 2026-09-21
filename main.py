"""윤피쉬 — 포항 실시간 낚시 포인트 추천 API.

로컬: `uvicorn main:app --host 0.0.0.0 --port 8000`
Vercel: `api/index.py` 가 이 모듈의 `app` 을 서버리스로 실행합니다.
"""

from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

from weather_service import CACHE_TTL_SECONDS, _client, build_recommendation, env_key, now_kst

PUBLIC_DIR = BASE_DIR / "public"
STATIC_DIR = PUBLIC_DIR if PUBLIC_DIR.exists() else BASE_DIR / "static"
ICONS_DIR = next(
    (p for p in (PUBLIC_DIR / "static" / "icons", BASE_DIR / "static" / "icons") if p.exists()),
    BASE_DIR / "static" / "icons",
)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    yield
    client = _client()
    if client is not None and not client.is_closed:
        await client.aclose()


app = FastAPI(
    title="Yunfish",
    description="포항 지역 실시간 낚시 포인트 추천",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _app_config() -> dict:
    return {
        "app_name": "윤피쉬",
        "kakao_js_key": os.getenv("KAKAO_JS_KEY", "").strip(),
        "has_kma_key": bool(env_key("KMA_API_KEY", "KMA_SERVICE_KEY")),
        "has_khoa_key": bool(env_key("KHOA_API_KEY", "KHOA_SERVICE_KEY")),
        "cache_ttl_seconds": CACHE_TTL_SECONDS,
    }


async def health():
    return {"ok": True, "now": now_kst().isoformat(), "app": "yunfish"}


async def config():
    return _app_config()


async def config_js():
    """Vercel 환경변수를 프론트에 주입하기 위한 JS 스니펫."""
    body = f"window.__YUNFISH_CONFIG__={json.dumps(_app_config(), ensure_ascii=False)};"
    return Response(body, media_type="application/javascript; charset=utf-8")


async def recommend(refresh: bool = Query(False, description="캐시를 무시하고 다시 계산")):
    payload = await build_recommendation(force_refresh=refresh)
    headers = {
        "Cache-Control": f"public, max-age={CACHE_TTL_SECONDS}",
        "X-Yunfish-Source": payload.get("source", "mock"),
    }
    if refresh:
        headers["Cache-Control"] = "no-store"
    return JSONResponse(payload, headers=headers)


# Vercel이 /api 접두사를 유지하거나 제거하는 경우 모두 대응
for prefix in ("/api", ""):
    app.add_api_route(f"{prefix}/health", health, methods=["GET"])
    app.add_api_route(f"{prefix}/config", config, methods=["GET"])
    app.add_api_route(f"{prefix}/config.js", config_js, methods=["GET"])
    app.add_api_route(f"{prefix}/recommend", recommend, methods=["GET"])


def _api_name(raw: str) -> str:
    name = str(raw).split("?")[0].rstrip("/").split("/")[-1]
    return name.replace(".py", "")


async def dispatch_api(request: Request, p: str = "", refresh: bool = Query(False)):
    """rewrites 로 /api/index 에 들어온 요청을 원래 엔드포인트로 연결."""
    raw = p or request.headers.get("x-forwarded-uri") or request.headers.get("x-invoke-path") or request.url.path
    name = _api_name(raw)
    if name in ("recommend",):
        return await recommend(refresh)
    if name == "config.js":
        return await config_js()
    if name in ("config",):
        return await config()
    if name == "health":
        return await health()
    if name in ("api", "index", ""):
        return await health()
    return JSONResponse({"ok": False, "error": "not_found", "path": name}, status_code=404)


app.add_api_route("/api/index", dispatch_api, methods=["GET"])
app.add_api_route("/index", dispatch_api, methods=["GET"])


def _public_file(*parts: str) -> Path:
    return STATIC_DIR.joinpath(*parts)


@app.get("/")
async def index():
    return FileResponse(_public_file("index.html"), media_type="text/html; charset=utf-8")


@app.get("/manifest.json")
async def manifest():
    return FileResponse(
        _public_file("manifest.json"),
        media_type="application/manifest+json; charset=utf-8",
    )


@app.get("/service-worker.js")
async def service_worker():
    return FileResponse(
        _public_file("service-worker.js"),
        media_type="application/javascript; charset=utf-8",
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/favicon.svg")
async def favicon_svg():
    svg = PUBLIC_DIR / "favicon.svg"
    if not svg.exists():
        svg = BASE_DIR / "static" / "favicon.svg"
    return FileResponse(svg, media_type="image/svg+xml")


@app.get("/favicon.ico")
async def favicon():
    svg = PUBLIC_DIR / "favicon.svg"
    if svg.exists():
        return FileResponse(svg, media_type="image/svg+xml")
    png = ICONS_DIR / "icon-192.png"
    if png.exists():
        return FileResponse(png)
    return FileResponse(ICONS_DIR / "icon.svg", media_type="image/svg+xml")


@app.get("/icon-192.png")
async def icon_192():
    path = PUBLIC_DIR / "icon-192.png"
    if not path.exists():
        path = ICONS_DIR / "icon-192.png"
    return FileResponse(path, media_type="image/png")


@app.get("/icon-512.png")
async def icon_512():
    path = PUBLIC_DIR / "icon-512.png"
    if not path.exists():
        path = ICONS_DIR / "icon-512.png"
    return FileResponse(path, media_type="image/png")


_static_root = PUBLIC_DIR / "static" if (PUBLIC_DIR / "static").exists() else BASE_DIR / "static"
if _static_root.exists():
    app.mount("/static", StaticFiles(directory=_static_root), name="static")
