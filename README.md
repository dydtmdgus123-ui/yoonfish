# 윤피쉬 (Yunfish)

포항 지역 낚시인을 위한 실시간 포인트 추천 모바일 웹 / PWA입니다.

- 포항 주요 8개 방파제·갯바위 좌표
- 월별 대상 어종 3~4종
- 일출·일몰 피딩 타임 + 간조·만조 물돌이 시간대
- 풍속 7m/s 또는 파고 1.5m 이상이면 **출조 주의/위험** 배지
- 기상청·해양조사원 API가 없거나 실패해도 Mock Fallback으로 중단 없이 동작
- Vercel 무료 서버리스 배포 지원 (`public/` 정적 파일 + `api/index.py`)

## 1. 로컬 실행

Python 3.10 이상이 필요합니다.

```powershell
cd C:\Users\Yong\Desktop\Yunfish\yunfish
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
uvicorn main:app --host 0.0.0.0 --port 8000
```

브라우저에서 [http://127.0.0.1:8000](http://127.0.0.1:8000) 로 접속합니다.

## 2. Vercel 무료 배포

Hobby 플랜(서버리스)으로 올리면 PC를 켜 두지 않아도 폰에서 접속할 수 있습니다.

1. [https://vercel.com](https://vercel.com) 에 GitHub 또는 이메일로 가입
2. 이 폴더를 Git 저장소에 올린 뒤 Vercel에서 **Import**
3. Framework Preset 은 **Other** (비워 두어도 됨)
4. **Environment Variables** 에 아래를 추가 (Production / Preview / Development 모두)

| 이름 | 필수 | 설명 |
|------|------|------|
| `KAKAO_JS_KEY` | 지도용 필수 | 카카오 **JavaScript 키** |
| `KMA_API_KEY` | 선택 | 기상청 data.go.kr 일반 인증키(디코딩키). 없으면 Mock |
| `KHOA_API_KEY` | 선택 | 해양조사원 바다누리 키. 없으면 Mock 물때 |

5. Deploy 후 나온 주소 예: `https://yunfish.vercel.app`
6. 카카오 개발자 콘솔 → **앱 → 플랫폼 키 → JavaScript 키 → JavaScript SDK 도메인** 에 배포 주소를 추가

```
https://yunfish.vercel.app
https://yunfish-xxxx.vercel.app
```

7. 같은 콘솔에서 **카카오맵 → 사용 설정 ON**
8. 배포 사이트에서 Ctrl+F5

프론트(`public/index.html`)는 `/api/config.js` 와 `/api/config` 로 Vercel 환경변수의 `KAKAO_JS_KEY` 를 받아 지도를 켭니다.

CLI로 배포하려면:

```powershell
npm i -g vercel
vercel login
vercel
```

환경변수는 `vercel env add KAKAO_JS_KEY` 로 넣을 수 있습니다.

## 3. API 키 (로컬)

`.env` 파일을 열어 키를 넣습니다. **비워 두어도 서비스는 켜집니다.**

| 변수 | 용도 | 없어도 |
|------|------|--------|
| `KAKAO_JS_KEY` | 카카오 지도 JavaScript 키 | 목록 모드로 포인트 카드만 표시 |
| `KMA_API_KEY` | 기상청 단기예보·해양부이 | 포항 앞바다 Mock 기온·바람·파고·수온 |
| `KHOA_API_KEY` | 국립해양조사원 조석예보 | 반일주조 기반 Mock 물때 |

카카오 키는 [Kakao Developers](https://developers.kakao.com) 에서 **JavaScript 키**를 쓰고, **JavaScript SDK 도메인**에 `http://127.0.0.1:8000` 과 `http://localhost:8000` 을 등록하세요. **카카오맵 사용 설정은 ON** 이어야 합니다.

## 4. API

| 경로 | 설명 |
|------|------|
| `GET /` | 모바일 웹 (카카오맵 + 바텀시트) |
| `GET /api/recommend` | 포인트별 날씨·어종·추천 시간대. `?refresh=true` 이면 캐시 무시 |
| `GET /api/config` | 카카오 키 등 프론트 설정 JSON |
| `GET /api/config.js` | 같은 설정을 `window.__YUNFISH_CONFIG__` 로 주입 |
| `GET /api/health` | 서버 상태 |
| `GET /manifest.json` | PWA 매니페스트 |
| `GET /service-worker.js` | 정적 자원 캐시 |

## 5. 추천 로직

- **오늘의 어종**: 오늘 날짜의 월 기준으로 포인트별 3~4종
- **아침 피딩**: 일출 ± 1.5시간
- **저녁 피딩**: 일몰 ± 1.5시간
- **물돌이**: 간조·만조 ± 2시간
- 피딩과 물때가 30분 이상 겹치면 **황금물때** 표시
- **주의**: 풍속 ≥ 7m/s 또는 파고 ≥ 1.5m
- **위험**: 풍속 ≥ 10m/s 또는 파고 ≥ 2.0m

## 6. 프로젝트 구조

```
yunfish/
  api/index.py              Vercel Python 엔트리 (main.app 연결)
  main.py                   FastAPI 앱 (로컬 uvicorn + 서버리스 공용)
  weather_service.py
  points_data.py
  vercel.json
  requirements.txt
  public/                   Vercel이 그대로 서빙하는 정적 파일
    index.html
    manifest.json
    service-worker.js
    static/icons/
  static/                   로컬 호환용 복사본
```
