"""기상청·해양부이·조석 연동, Mock Fallback, 물때·피딩 계산."""

from __future__ import annotations

import asyncio
import math
import os
import random
from collections import OrderedDict
from datetime import date, datetime, timedelta, time
from functools import lru_cache
from typing import Any
from urllib.parse import urlencode
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo

import httpx

from points_data import POINTS, SEASON_KO, season_of, today_species

KST = ZoneInfo("Asia/Seoul")
CACHE_TTL_SECONDS = 30 * 60

POHANG_NX, POHANG_NY = 102, 94
POHANG_LAT, POHANG_LNG = 36.01, 129.37
KMA_BUOY_STN = "22104"
KHOA_POHANG_OBS = "DT_0007"

WIND_ALERT_MS = 7.0
WAVE_ALERT_M = 1.5
WIND_DANGER_MS = 10.0
WAVE_DANGER_M = 2.0

FEEDING_HALF = timedelta(hours=1.5)
TIDE_HALF = timedelta(hours=2)

KMA_FCST_URL = (
    "https://apis.data.go.kr/1360000/VilageFcstInfoService_2.0/getVilageFcst"
)
KMA_BUOY_URL = (
    "https://apis.data.go.kr/1360000/OceanBuoyInfoService/getOceanBuoyInfo"
)
KHOA_TIDE_URL = "https://www.khoa.go.kr/api/oceangrid/tideObsPreTab/search.do"

DIR16_KO = ["북", "북북동", "북동", "동북동", "동", "동남동", "남동", "남남동",
            "남", "남남서", "남서", "서남서", "서", "서북서", "북서", "북북서"]
SKY_KO = {"1": "맑음", "3": "구름많음", "4": "흐림"}
PTY_KO = {"0": "없음", "1": "비", "2": "비/눈", "3": "눈", "5": "빗방울",
          "6": "빗방울눈날림", "7": "눈날림"}

MONTH_TEMP = {
    1: 4.0, 2: 5.5, 3: 10.0, 4: 14.5, 5: 18.5, 6: 21.5,
    7: 24.5, 8: 25.5, 9: 22.0, 10: 17.0, 11: 11.5, 12: 6.0,
}
MONTH_SST = {
    1: 12.0, 2: 11.2, 3: 12.0, 4: 13.8, 5: 16.2, 6: 19.4,
    7: 22.6, 8: 24.8, 9: 22.4, 10: 20.1, 11: 16.8, 12: 14.0,
}


def env_key(*names: str) -> str:
    for name in names:
        val = os.getenv(name, "").strip()
        if val:
            return val
    return ""


def kma_api_key() -> str:
    return env_key("KMA_API_KEY", "KMA_SERVICE_KEY")


def khoa_api_key() -> str:
    return env_key("KHOA_API_KEY", "KHOA_SERVICE_KEY")


class LRUTTLCache:
    """30분 TTL + 용량 제한 LRU. Vercel 인스턴스 안에서 공공 API 호출을 줄입니다."""

    def __init__(self, ttl: int = CACHE_TTL_SECONDS, maxsize: int = 32) -> None:
        self.ttl = ttl
        self.maxsize = maxsize
        self._store: OrderedDict[str, tuple[float, Any]] = OrderedDict()
        self._lock = asyncio.Lock()

    async def get_or_set(self, key: str, factory) -> Any:
        now = datetime.now(KST).timestamp()
        async with self._lock:
            hit = self._store.get(key)
            if hit and now - hit[0] < self.ttl:
                self._store.move_to_end(key)
                return hit[1]
            if hit:
                self._store.pop(key, None)
        value = await factory()
        async with self._lock:
            self._store[key] = (datetime.now(KST).timestamp(), value)
            self._store.move_to_end(key)
            while len(self._store) > self.maxsize:
                self._store.popitem(last=False)
        return value


_cache = LRUTTLCache()
_http: httpx.AsyncClient | None = None


def _client() -> httpx.AsyncClient:
    global _http
    if _http is None or _http.is_closed:
        _http = httpx.AsyncClient(timeout=5.5, follow_redirects=True)
    return _http


def now_kst() -> datetime:
    return datetime.now(KST)


def cache_slot(dt: datetime | None = None) -> datetime:
    dt = dt or now_kst()
    minute = (dt.minute // 30) * 30
    return dt.replace(minute=minute, second=0, microsecond=0)


def deg_to_dir_ko(deg: float | None) -> str:
    if deg is None:
        return "-"
    idx = int((deg % 360) / 22.5 + 0.5) % 16
    return DIR16_KO[idx]


def _kma_base_for_fcst(dt: datetime) -> tuple[str, str]:
    """단기예보 발표: 02/05/08/11/14/17/20/23시 (+10분 이후 사용)."""
    times = [2, 5, 8, 11, 14, 17, 20, 23]
    hour = dt.hour
    base_date = dt.date()
    for t in reversed(times):
        if hour > t or (hour == t and dt.minute >= 10):
            return base_date.strftime("%Y%m%d"), f"{t:02d}00"
    prev = base_date - timedelta(days=1)
    return prev.strftime("%Y%m%d"), "2300"


def _as_list(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _to_float(value: Any) -> float | None:
    if value in (None, "", "-", "강수없음"):
        return None
    try:
        return float(str(value).replace("m", "").replace("℃", "").strip())
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# 음력 물때 (1~14물, 조금, 무시)
# ---------------------------------------------------------------------------
def _julian_date(day: date) -> float:
    a = (14 - day.month) // 12
    y = day.year + 4800 - a
    m = day.month + 12 * a - 3
    return day.day + (153 * m + 2) // 5 + 365 * y + y // 4 - y // 100 + y // 400 - 32045


@lru_cache(maxsize=128)
def lunar_day_number(day: date) -> int:
    """음력일(1~30). 평균삭망월 29.530588853일, 2000-01-06 삭 기준."""
    jd = _julian_date(day) + 0.125  # 03:00 KST ≈ 전날 18UT 보정
    ref_new_moon = 2451550.259722
    synodic = 29.530588853
    age = (jd - ref_new_moon) % synodic
    n = int(age) + 1
    return min(30, max(1, n))


@lru_cache(maxsize=128)
def mul_ttae(day: date) -> dict[str, Any]:
    """한국 바다낚시 물때. 반달 주기 1~14물, 7·8물 조금, 1·14물 무시(사리)."""
    lunar = lunar_day_number(day)
    raw = lunar if lunar <= 15 else lunar - 15
    num = 14 if raw == 15 else raw
    if num in (7, 8):
        phase = "조금"
        phase_note = "소조 · 조류가 약합니다"
    elif num in (1, 14):
        phase = "사리"
        phase_note = "대조(무시) · 조류가 강합니다"
    else:
        phase = "중조"
        phase_note = "중간 물때"
    label = f"{num}물"
    display = label if phase == "중조" else f"{phase} ({label})"
    return {
        "num": num,
        "label": label,
        "display": display,
        "badge": f"{label} · {phase}",
        "phase": phase,
        "phase_note": phase_note,
        "lunar_day": lunar,
    }


# ---------------------------------------------------------------------------
# Sunrise / sunset — 포항(36.01, 129.37) 기본, NOAA 근사
# ---------------------------------------------------------------------------
@lru_cache(maxsize=64)
def sun_times(lat: float, lon: float, day: date) -> dict[str, str]:
    n = datetime(day.year, day.month, day.day, tzinfo=KST).timetuple().tm_yday
    lat_r = math.radians(lat)

    def _event(sunrise: bool) -> datetime:
        zenith = math.radians(90.833)
        lng_hour = lon / 15.0
        t = n + ((6 if sunrise else 18) - lng_hour) / 24.0
        m = (0.9856 * t) - 3.289
        l = (m + 1.916 * math.sin(math.radians(m)) + 0.020 * math.sin(math.radians(2 * m)) + 282.634) % 360
        ra = math.degrees(math.atan(0.91764 * math.tan(math.radians(l)))) % 360
        l_quad = math.floor(l / 90) * 90
        ra_quad = math.floor(ra / 90) * 90
        ra = (ra + (l_quad - ra_quad)) / 15.0
        sin_dec = 0.39782 * math.sin(math.radians(l))
        cos_dec = math.cos(math.asin(sin_dec))
        cos_h = (math.cos(zenith) - sin_dec * math.sin(lat_r)) / (cos_dec * math.cos(lat_r))
        cos_h = max(-1.0, min(1.0, cos_h))
        h = math.degrees(math.acos(cos_h))
        if sunrise:
            h = 360 - h
        h /= 15.0
        t_local = h + ra - (0.06571 * t) - 6.622
        ut = (t_local - lng_hour) % 24
        kst_hours = (ut + 9) % 24
        hh = int(kst_hours)
        mm = int((kst_hours - hh) * 60)
        return datetime.combine(day, time(hh, mm), tzinfo=KST)

    rise, set_ = _event(True), _event(False)
    return {
        "sunrise": rise.strftime("%H:%M"),
        "sunset": set_.strftime("%H:%M"),
        "sunrise_iso": rise.isoformat(),
        "sunset_iso": set_.isoformat(),
    }


def _parse_hhmm(day: date, hhmm: str) -> datetime:
    hh, mm = hhmm.split(":")
    return datetime.combine(day, time(int(hh), int(mm)), tzinfo=KST)


# ---------------------------------------------------------------------------
# Mock
# ---------------------------------------------------------------------------
def _rng(seed: str) -> random.Random:
    return random.Random(seed)


def estimate_wave(wind_speed: float, exposure: float, rng: random.Random | None = None) -> float:
    jitter = rng.uniform(-0.08, 0.12) if rng else 0.0
    raw = (0.12 + wind_speed * 0.13) * exposure + jitter
    return round(max(0.15, min(3.5, raw)), 2)


def mock_weather(point: dict[str, Any], dt: datetime) -> dict[str, Any]:
    seed = f"{dt.date().isoformat()}-{dt.hour}-{dt.minute // 30}-{point['id']}"
    rng = _rng(seed)
    month, hour = dt.month, dt.hour
    temp = round(MONTH_TEMP[month] + 3.2 * math.sin((hour - 10) / 24 * 2 * math.pi) + rng.uniform(-1.2, 1.2), 1)
    if month in (12, 1, 2):
        wind_dir_deg, wind_base = rng.uniform(280, 350), rng.uniform(3.5, 7.5)
    elif month in (6, 7, 8):
        wind_dir_deg, wind_base = rng.uniform(160, 230), rng.uniform(1.8, 5.0)
    else:
        wind_dir_deg = rng.choice([rng.uniform(40, 90), rng.uniform(250, 320)])
        wind_base = rng.uniform(2.5, 6.5)
    wind_speed = round(max(0.4, wind_base * (0.85 + 0.3 * point["exposure"]) + rng.uniform(-0.6, 0.6)), 1)
    wave = estimate_wave(wind_speed, point["exposure"], rng)
    sst = round(MONTH_SST[month] + rng.uniform(-0.6, 0.6), 1)
    sky_roll = rng.random()
    if sky_roll < 0.45:
        sky, pty, pop = "맑음", "없음", rng.randint(0, 15)
    elif sky_roll < 0.75:
        sky, pty, pop = "구름많음", "없음", rng.randint(10, 35)
    elif sky_roll < 0.9:
        sky, pty, pop = "흐림", "없음", rng.randint(20, 50)
    else:
        sky = "흐림"
        pty = "비" if month not in (12, 1, 2) else "눈"
        pop = rng.randint(55, 85)
    return {
        "temp": temp,
        "wind_speed": wind_speed,
        "wind_dir": deg_to_dir_ko(wind_dir_deg),
        "wind_dir_deg": round(wind_dir_deg, 0),
        "wave_height": wave,
        "water_temp": sst,
        "pop": pop,
        "sky": sky,
        "pty": pty,
        "humidity": int(rng.uniform(48, 82)),
        "source": "mock",
    }


def mock_tide(day: date, point_id: str) -> list[dict[str, Any]]:
    rng = _rng(f"tide-{day.isoformat()}-{point_id}")
    amp, mean = rng.uniform(0.12, 0.22), 0.34
    lunar_age = ((day.toordinal() + 3) % 30) / 30.0
    offset_min = int(lunar_age * 12.42 * 60) + rng.randint(-35, 35)
    half = timedelta(hours=6, minutes=13)
    t = datetime.combine(day, time(0, 0), tzinfo=KST) + timedelta(
        minutes=offset_min % int(half.total_seconds() // 60)
    )
    high = rng.choice([True, False])
    events: list[dict[str, Any]] = []
    for _ in range(6):
        if t.date() == day:
            height_m = mean + (amp if high else -amp)
            height_m = max(0.08, height_m)
            cm = int(round(height_m * 100))
            events.append({
                "type": "고조" if high else "저조",
                "time": t.strftime("%H:%M"),
                "height": round(height_m, 2),
                "height_cm": cm,
            })
        t += half
        high = not high
        if t.date() > day:
            break
    return events[:4]


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------
async def _kma_get(url: str, extra: dict[str, str]) -> Any | None:
    key = kma_api_key()
    if not key:
        return None
    params = {"pageNo": "1", "numOfRows": "1000", "dataType": "JSON", **extra}
    urls = [url]
    if url.startswith("https://"):
        urls.append("http://" + url[len("https://"):])
    try:
        res = None
        for target in urls:
            if "%" in key:
                res = await _client().get(f"{target}?serviceKey={key}&{urlencode(params)}")
            else:
                res = await _client().get(target, params={**params, "serviceKey": key})
            if res.status_code < 400:
                break
        if res is None or res.status_code >= 400:
            return None
        text = res.text.lstrip()
        if text.startswith("<") or "xml" in (res.headers.get("content-type") or "").lower():
            return _parse_kma_xml(text)
        return res.json()
    except Exception:
        return None


def _parse_kma_xml(text: str) -> dict[str, Any]:
    root = ET.fromstring(text)
    items = []
    for node in root.iter():
        tag = node.tag.split("}")[-1].lower()
        if tag == "item":
            items.append({child.tag.split("}")[-1]: (child.text or "") for child in list(node)})
    return {"response": {"body": {"items": {"item": items}}}}


def _kma_items(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    body = payload.get("response", {}).get("body", {})
    header = payload.get("response", {}).get("header", {})
    code = str(header.get("resultCode", "00"))
    if code not in ("00", "0", ""):
        return []
    return _as_list(body.get("items", {}).get("item"))


# ---------------------------------------------------------------------------
# 기상청 단기예보 (포항 102, 94)
# ---------------------------------------------------------------------------
async def fetch_kma_vilage(dt: datetime) -> dict[str, Any] | None:
    base_date, base_time = _kma_base_for_fcst(dt)
    payload = await _kma_get(KMA_FCST_URL, {
        "base_date": base_date,
        "base_time": base_time,
        "nx": str(POHANG_NX),
        "ny": str(POHANG_NY),
    })
    items = _kma_items(payload)
    if not items:
        return None
    target_date = dt.strftime("%Y%m%d")
    slots: dict[tuple[str, str], dict[str, Any]] = {}
    for it in items:
        fd, ft = str(it.get("fcstDate") or ""), str(it.get("fcstTime") or "0000")
        slots.setdefault((fd, ft), {})[it.get("category")] = it.get("fcstValue")
    best_cats: dict[str, Any] | None = None
    best_score = 10**9
    for (fd, ft), cats in slots.items():
        if fd != target_date:
            continue
        try:
            hour = int(ft[:2]) % 24
        except ValueError:
            continue
        diff = abs(hour - dt.hour)
        complete = 0 if ("TMP" in cats and "WSD" in cats) else 1
        score = diff * 10 + complete
        if score < best_score:
            best_score = score
            best_cats = cats
    if not best_cats:
        return None
    temp = _to_float(best_cats.get("TMP") or best_cats.get("T1H"))
    wsd = _to_float(best_cats.get("WSD"))
    vec = _to_float(best_cats.get("VEC"))
    pop = _to_float(best_cats.get("POP"))
    wav = _to_float(best_cats.get("WAV"))
    if temp is None or wsd is None:
        return None
    out = {
        "temp": round(temp, 1),
        "wind_speed": round(wsd, 1),
        "wind_dir": deg_to_dir_ko(vec),
        "wind_dir_deg": round(vec, 0) if vec is not None else None,
        "pop": int(pop) if pop is not None else None,
        "sky": SKY_KO.get(str(best_cats.get("SKY", "1")), "맑음"),
        "pty": PTY_KO.get(str(best_cats.get("PTY", "0")), "없음"),
        "humidity": int(_to_float(best_cats.get("REH")) or 0) or None,
        "source": "kma",
    }
    if wav is not None and wav > 0:
        out["wave_height"] = round(wav, 2)
    return out


# ---------------------------------------------------------------------------
# 기상청 해양부이 22104 — 유의파고, 수온
# ---------------------------------------------------------------------------
def _parse_buoy_row(row: dict[str, Any]) -> dict[str, float] | None:
    keys = {str(k).lower(): v for k, v in row.items()}
    wave = None
    sst = None
    for name in ("wh", "whsv", "wave", "waveheight", "wav", "sig_wh", "wvht"):
        if keys.get(name) is not None:
            wave = _to_float(keys.get(name))
            if wave is not None:
                break
    for name in ("tw", "wtem", "wt", "sst", "watertemp", "tw_sea", "sea_temp"):
        if keys.get(name) is not None:
            sst = _to_float(keys.get(name))
            if sst is not None:
                break
    if wave is None and sst is None:
        return None
    out: dict[str, float] = {}
    if wave is not None and 0 <= wave <= 12:
        out["wave_height"] = round(wave, 2)
    if sst is not None and 0 <= sst <= 40:
        out["water_temp"] = round(sst, 1)
    return out or None


async def fetch_kma_buoy(_dt: datetime) -> dict[str, float] | None:
    key = kma_api_key()
    if not key:
        return None
    params = {"pageNo": "1", "numOfRows": "10", "dataType": "JSON", "stnId": KMA_BUOY_STN, "serviceKey": key}
    try:
        async with httpx.AsyncClient(timeout=2.5, follow_redirects=True) as client:
            res = await client.get(KMA_BUOY_URL, params=params)
            if res.status_code >= 400:
                return None
            text = res.text.lstrip()
            payload = _parse_kma_xml(text) if text.startswith("<") else res.json()
    except Exception:
        return None
    items = _kma_items(payload)
    for row in reversed(items):
        parsed = _parse_buoy_row(row)
        if parsed:
            return parsed
    return None


# ---------------------------------------------------------------------------
# 국립해양조사원 조석예보 DT_0007
# ---------------------------------------------------------------------------
async def fetch_khoa_tide(day: date) -> list[dict[str, Any]] | None:
    key = khoa_api_key()
    if not key:
        return None
    params = {
        "ServiceKey": key,
        "ObsCode": KHOA_POHANG_OBS,
        "Date": day.strftime("%Y%m%d"),
        "ResultType": "json",
    }
    try:
        if "%" in key:
            res = await _client().get(
                f"{KHOA_TIDE_URL}?ServiceKey={key}&{urlencode({k: v for k, v in params.items() if k != 'ServiceKey'})}"
            )
        else:
            res = await _client().get(KHOA_TIDE_URL, params=params)
        if res.status_code >= 400:
            return None
        payload = res.json()
        data = payload.get("result", {}).get("data") or payload.get("data") or []
        events: list[dict[str, Any]] = []
        for row in _as_list(data):
            raw_time = str(row.get("tide_time") or row.get("tph_time") or row.get("time") or "")
            hhmm = raw_time[-5:] if ":" in raw_time else ""
            if len(hhmm) != 5:
                continue
            code = str(row.get("hl_code") or row.get("tide_type") or row.get("type") or "")
            kind = "고조" if ("고" in code or code.upper() in ("HIGH", "H")) else "저조"
            raw_h = _to_float(row.get("tide_level") or row.get("tph_level") or row.get("level"))
            if raw_h is None:
                height_cm, height_m = None, None
            elif raw_h > 5:
                height_cm, height_m = int(round(raw_h)), round(raw_h / 100.0, 2)
            else:
                height_m, height_cm = round(raw_h, 2), int(round(raw_h * 100))
            events.append({
                "type": kind,
                "time": hhmm,
                "height": height_m,
                "height_cm": height_cm,
            })
        return events[:4] if events else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 추천 알고리즘
# ---------------------------------------------------------------------------
def tide_progress(events: list[dict[str, Any]], now: datetime | None = None) -> dict[str, Any]:
    """현재가 들물/날물인지, 다음 만·간조까지 남은 시간을 계산."""
    now = now or now_kst()
    day = now.date()
    if not events:
        return {
            "phase": "unknown",
            "phase_label": "-",
            "badge": "조석 정보 없음",
            "countdown": "-",
            "next_type": None,
            "next_time": None,
            "minutes_left": None,
            "progress": 0,
        }

    timed: list[dict[str, Any]] = []
    for ev in events:
        try:
            timed.append({**ev, "dt": _parse_hhmm(day, ev["time"])})
        except Exception:
            continue
    timed.sort(key=lambda x: x["dt"])
    if not timed:
        return {
            "phase": "unknown",
            "phase_label": "-",
            "badge": "조석 정보 없음",
            "countdown": "-",
            "next_type": None,
            "next_time": None,
            "minutes_left": None,
            "progress": 0,
        }

    prev = None
    nxt = None
    for ev in timed:
        if ev["dt"] <= now:
            prev = ev
        elif nxt is None:
            nxt = ev
    if prev is None:
        nxt = timed[0]
        prev_type = "저조" if nxt["type"] == "고조" else "고조"
        prev = {**timed[-1], "type": prev_type, "dt": timed[0]["dt"] - timedelta(hours=6, minutes=13)}
    if nxt is None:
        nxt_type = "저조" if prev["type"] == "고조" else "고조"
        nxt = {**timed[0], "type": nxt_type, "dt": timed[0]["dt"] + timedelta(days=1)}

    flood = prev["type"] == "저조"
    phase = "flood" if flood else "ebb"
    phase_label = "들물" if flood else "날물"
    badge = "🌊 들물 진행 중" if flood else "🌙 날물 진행 중"
    next_kind = "만조" if nxt["type"] == "고조" else "간조"
    mins = max(0, int((nxt["dt"] - now).total_seconds() // 60))
    hours, minutes = divmod(mins, 60)
    countdown = (
        f"{next_kind}까지 {hours}시간 {minutes}분 전" if hours else f"{next_kind}까지 {minutes}분 전"
    )
    span = (nxt["dt"] - prev["dt"]).total_seconds() or 1
    elapsed = (now - prev["dt"]).total_seconds()
    progress = max(0.0, min(1.0, elapsed / span))
    return {
        "phase": phase,
        "phase_label": phase_label,
        "badge": badge,
        "countdown": countdown,
        "next_type": nxt["type"],
        "next_kind": next_kind,
        "next_time": nxt["dt"].strftime("%H:%M"),
        "minutes_left": mins,
        "progress": round(progress, 3),
        "from_type": prev["type"],
        "from_time": prev["dt"].strftime("%H:%M"),
    }


def compute_alert(weather: dict[str, Any]) -> dict[str, str]:
    wind = float(weather.get("wind_speed") or 0)
    wave = float(weather.get("wave_height") or 0)
    if wind >= WIND_DANGER_MS or wave >= WAVE_DANGER_M:
        return {
            "level": "danger",
            "label": "출조 위험",
            "message": f"풍속 {wind:.1f}m/s · 유의파고 {wave:.1f}m. 방파제 출조를 미루는 것이 안전합니다.",
        }
    if wind >= WIND_ALERT_MS or wave >= WAVE_ALERT_M:
        return {
            "level": "caution",
            "label": "출조 주의",
            "message": f"풍속 {wind:.1f}m/s · 유의파고 {wave:.1f}m. 외항 끝단·갯바위는 피하고 내항 위주로 짧게.",
        }
    return {
        "level": "safe",
        "label": "출조 양호",
        "message": f"풍속 {wind:.1f}m/s · 유의파고 {wave:.1f}m. 피딩·물때 겹치는 시간대를 노려보세요.",
    }


def compute_windows(day: date, sun: dict[str, str], tides: list[dict[str, Any]]) -> list[dict[str, Any]]:
    sunrise = _parse_hhmm(day, sun["sunrise"])
    sunset = _parse_hhmm(day, sun["sunset"])
    windows: list[dict[str, Any]] = [
        {
            "kind": "sunrise",
            "label": "아침 피딩",
            "detail": "일출 전후 1.5시간",
            "start": (sunrise - FEEDING_HALF).strftime("%H:%M"),
            "end": (sunrise + FEEDING_HALF).strftime("%H:%M"),
            "anchor": sun["sunrise"],
            "golden": False,
        },
        {
            "kind": "sunset",
            "label": "저녁 피딩",
            "detail": "일몰 전후 1.5시간",
            "start": (sunset - FEEDING_HALF).strftime("%H:%M"),
            "end": (sunset + FEEDING_HALF).strftime("%H:%M"),
            "anchor": sun["sunset"],
            "golden": False,
        },
    ]
    for ev in tides:
        anchor = _parse_hhmm(day, ev["time"])
        cm = ev.get("height_cm")
        extra = f" · {cm}cm" if cm is not None else (f" · {ev.get('height')}m" if ev.get("height") is not None else "")
        windows.append({
            "kind": "tide",
            "label": f"{ev['type']} 물돌이",
            "detail": f"{ev['type']} 전후 2시간{extra}",
            "start": (anchor - TIDE_HALF).strftime("%H:%M"),
            "end": (anchor + TIDE_HALF).strftime("%H:%M"),
            "anchor": ev["time"],
            "golden": False,
        })

    def overlap_minutes(a: dict[str, Any], b: dict[str, Any]) -> int:
        a0, a1 = _parse_hhmm(day, a["start"]), _parse_hhmm(day, a["end"])
        b0, b1 = _parse_hhmm(day, b["start"]), _parse_hhmm(day, b["end"])
        return max(0, int((min(a1, b1) - max(a0, b0)).total_seconds() // 60))

    feeding = [w for w in windows if w["kind"] in ("sunrise", "sunset")]
    tide_ws = [w for w in windows if w["kind"] == "tide"]
    for f in feeding:
        for t in tide_ws:
            if overlap_minutes(f, t) >= 30:
                f["golden"] = True
                t["golden"] = True
                if "황금물때" not in f["label"]:
                    f["label"] = f["label"] + " · 황금물때"
                break
    windows.sort(key=lambda w: w["start"])
    return windows


async def _live_bundle(dt: datetime) -> dict[str, Any]:
    """단기예보는 필수, 부이·조석은 실패해도 예보를 지킵니다."""
    village = await fetch_kma_vilage(dt)
    buoy, tides = None, None
    try:
        buoy, tides = await asyncio.gather(
            fetch_kma_buoy(dt),
            fetch_khoa_tide(dt.date()),
        )
    except Exception:
        pass
    return {"village": village, "buoy": buoy, "tides": tides}


def _compose_weather(point: dict[str, Any], dt: datetime, village: dict | None, buoy: dict | None) -> dict[str, Any]:
    if not village:
        weather = mock_weather(point, dt)
        if buoy:
            if buoy.get("wave_height") is not None:
                weather["wave_height"] = round(buoy["wave_height"] * point["exposure"] / 1.0, 2)
            if buoy.get("water_temp") is not None:
                weather["water_temp"] = buoy["water_temp"]
            weather["source"] = "mock+buoy"
        return weather
    wave = (buoy or {}).get("wave_height") or village.get("wave_height")
    sst = (buoy or {}).get("water_temp")
    src = "kma"
    if buoy and (buoy.get("wave_height") is not None or buoy.get("water_temp") is not None):
        src = "kma+buoy"
    if wave is None:
        wave = estimate_wave(float(village["wind_speed"]), point["exposure"])
    else:
        wave = round(max(0.1, float(wave) * (0.75 + 0.35 * point["exposure"])), 2)
    if sst is None:
        sst = MONTH_SST.get(dt.month, 22.4)
    pop = village.get("pop")
    if pop is None:
        pop = 10
    return {
        **village,
        "wave_height": wave,
        "water_temp": round(float(sst), 1),
        "pop": int(pop),
        "source": src,
    }


async def build_recommendation(force_refresh: bool = False) -> dict[str, Any]:
    dt = now_kst()
    slot = cache_slot(dt)
    key = f"recommend:{slot.isoformat()}"

    async def _build() -> dict[str, Any]:
        day = dt.date()
        month = dt.month
        season = season_of(month)
        sun = sun_times(POHANG_LAT, POHANG_LNG, day)
        mul = mul_ttae(day)
        bundle = await _live_bundle(dt)
        village, buoy, live_tides = bundle["village"], bundle["buoy"], bundle["tides"]
        tide_src = "khoa" if live_tides else "mock"
        sources: set[str] = set()
        points_out: list[dict[str, Any]] = []

        for point in POINTS:
            weather = _compose_weather(point, dt, village, buoy)
            tides = live_tides if live_tides else mock_tide(day, point["id"])
            progress = tide_progress(tides, dt)
            wsrc = weather.get("source", "mock")
            sources.add(wsrc)
            sources.add(tide_src)
            points_out.append({
                "id": point["id"],
                "name": point["name"],
                "short_name": point["short_name"],
                "lat": point["lat"],
                "lng": point["lng"],
                "area": point["area"],
                "tip": point["tip"],
                "season": SEASON_KO[season],
                "weather": weather,
                "mul": mul,
                "mul_label": mul["display"],
                "tide": {
                    "events": tides,
                    "source": tide_src,
                    "station": KHOA_POHANG_OBS,
                    "mul": mul,
                    "progress": progress,
                    "state": progress,
                },
                "sun": {"sunrise": sun["sunrise"], "sunset": sun["sunset"]},
                "today_species": today_species(point, month, limit=4),
                "windows": compute_windows(day, sun, tides),
                "alert": compute_alert(weather),
                "data_source": {"weather": wsrc, "tide": tide_src, "buoy": "kma" if buoy else "mock"},
            })

        safe_n = sum(1 for p in points_out if p["alert"]["level"] == "safe")
        caution_n = sum(1 for p in points_out if p["alert"]["level"] != "safe")
        used_live = any(s not in ("mock",) for s in sources)
        return {
            "generated_at": dt.isoformat(),
            "cache_until": (slot + timedelta(seconds=CACHE_TTL_SECONDS)).isoformat(),
            "date": day.isoformat(),
            "month": month,
            "season": SEASON_KO[season],
            "mul": mul,
            "tide_progress": points_out[0]["tide"]["progress"] if points_out else None,
            "source": "live" if used_live else "mock",
            "sources": sorted(sources),
            "summary": {
                "safe": safe_n,
                "caution": caution_n,
                "headline": (
                    f"오늘 {SEASON_KO[season]} · {mul['badge']} · 양호 {safe_n}곳 / 주의 {caution_n}곳"
                ),
            },
            "points": points_out,
        }

    if force_refresh:
        payload = await _build()
        async with _cache._lock:
            _cache._store[key] = (datetime.now(KST).timestamp(), payload)
            _cache._store.move_to_end(key)
        return payload
    return await _cache.get_or_set(key, _build)
