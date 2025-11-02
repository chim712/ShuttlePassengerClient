# app/main.py
import os
import logging
from typing import Any, Dict, Optional

from fastapi import FastAPI, HTTPException, Request, Query
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import httpx
from starlette.responses import JSONResponse

# ----------------------------
# 환경설정
# ----------------------------
API_BASE_URL = os.getenv("API_BASE_URL", "http://localhost:9000")  # 외부 REST 서버
API_KEY = os.getenv("API_KEY", "")  # 필요 없다면 빈 값 유지

app = FastAPI(title="ShuttlePassengerClient")
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")

# httpx AsyncClient 수명주기 관리
client: Optional[httpx.AsyncClient] = None

# log library
logger = logging.getLogger("uvicorn.error")


@app.on_event("startup")
async def _startup() -> None:
    global client
    client = httpx.AsyncClient(
        base_url=API_BASE_URL,
        timeout=httpx.Timeout(10.0, connect=5.0),
        headers={"Authorization": f"Bearer {API_KEY}"} if API_KEY else None,
    )


@app.on_event("shutdown")
async def _shutdown() -> None:
    global client
    if client:
        await client.aclose()
        client = None


# 공통 REST 호출 헬퍼
# async def fetch_json(path: str, params: Optional[Dict[str, Any]] = None) -> Any:
#     """
#     외부 REST 서버에서 JSON을 받아온다.
#     예외/상태코드 처리 포함. path는 '/orgs' 같은 상대 경로 기준.
#     """
#     assert client is not None, "HTTP client is not initialized"
#     try:
#         resp = await client.get(path, params=params)
#     except httpx.RequestError as e:
#         # 네트워크 오류
#         raise HTTPException(status_code=502, detail=f"Upstream request error: {e}") from e
#
#     if resp.status_code == 404:
#         raise HTTPException(status_code=404, detail="리소스를 찾을 수 없습니다.")
#     if resp.status_code >= 400:
#         raise HTTPException(status_code=502, detail=f"Upstream error: {resp.status_code}")
#
#     try:
#         return resp.json()
#     except ValueError:
#         raise HTTPException(status_code=502, detail="Invalid JSON from upstream")


# ----------------------------
# 페이지 라우트
# ----------------------------

# (1) 홈: 기관 선택
#   - 외부 REST 예시: GET /orgs  ->  [{ "id": "SCH", "name": "순천향대학교" }, ...]
@app.get("/", response_class=HTMLResponse)
async def home(request: Request) -> HTMLResponse:
    #orgs = await fetch_json("/orgs")
    # 템플릿은 셀렉트박스 렌더 기준으로 orgs: List[Dict] 가정
    return templates.TemplateResponse("home.html", {"request": request})


# (2) 기관 내 노선 목록: /{org}
#   - 외부 REST 예시: GET /orgs/{org}/routes
#   - 응답 예시: [{ "routeNo": "1501", "name": "저상 1501", "direction": "상행" }, ...]
@app.get("/route_list", response_class=HTMLResponse)
async def route_list(request: Request, org: int) -> HTMLResponse:
    #routes = await fetch_json(f"/orgs/{org}/routes")
    routes = [
        {"routeId": 102, "routeNumber": "1", "routeTitle": "학내순환(순환)", "routeType": "CIRCULATION"},
        {"routeId": 191, "routeNumber": "1-1", "routeTitle": "학내순환(미디어랩스)", "routeType": "ROUND_TRIP_DOWN"},
        {"routeId": 200, "routeNumber": "2", "routeTitle": "신창역 직행 (신창역행)", "routeType": "ROUND_TRIP_UP"},
        {"routeId": 201, "routeNumber": "2", "routeTitle": "신창역 직행 (후문행)", "routeType": "ROUND_TRIP_DOWN"},
        {"routeId": 282, "routeNumber": "2-2", "routeTitle": "학내순환 > 신창역", "routeType": "CIRCULATION"},
        {"routeId": 292, "routeNumber": "2-1", "routeTitle": "신창역 > 학내순환", "routeType": "CIRCULATION"},
        {"routeId": 302, "routeNumber": "3", "routeTitle": "신창역 휴일", "routeType": "CIRCULATION"},
        {"routeId": 900, "routeNumber": "900", "routeTitle": "아산터미널-천안터미널", "routeType": "ROUND_TRIP_DOWN"},
    ]
    jump_base = f"/{org}"
    return templates.TemplateResponse(
        "route_list.html",
        {"request": request, "routes": routes, "jump_base": jump_base},
    )


# (3) 노선 상세(정류소 목록): /{org}/{routeNo}
#   - 외부 REST 예시(둘 중 하나를 사용):
#       A) GET /orgs/{org}/routes/{routeNo}           -> { "routeNo": "...", "name": "...", ... }
#          GET /orgs/{org}/routes/{routeNo}/stops     -> [{ "stopId": 946, "name": "신동", "seq": 1 }, ...]
#       B) GET /orgs/{org}/routes/{routeNo}/detail    -> { "route": {...}, "stops": [...] }

UPSTREAM_API_BASE = os.getenv("UPSTREAM_API_BASE", "http://localhost:9900")  # 예: http://data-api:8001
TIMEOUT = float(os.getenv("UPSTREAM_TIMEOUT", "3.0"))


@app.get("/{org}/{routeNo}", response_class=HTMLResponse)
async def route_detail(request: Request, org: str, routeNo: str) -> HTMLResponse:
    """
    HTML만 SSR. org/routeNo를 템플릿에 주입하여
    클라이언트 JS가 querystring로 넘겨 사용하게 함.
    """
    return templates.TemplateResponse(
        "route_detail.html",
        {
            "request": request,
            "orgId": org,
            "routeId": routeNo,
            # 프록시 모드이므로 apiBase는 굳이 노출할 필요 없음 (동일 오리진 사용)
            "apiBase": "",
        },
    )

# --------- 프록시 엔드포인트 3종 ---------
def _ensure_params(orgId: str | None, routeId: str | None):
    if not orgId or not routeId:
        raise HTTPException(status_code=400, detail="orgId and routeId are required.")

def _build_upstream(path: str, orgId: str, routeId: str) -> str:
    # 임시 데이터 서버는 /meta?orgId=&routeId= 형태라고 가정
    # path: "/meta" | "/stops" | "/vehicles"
    return f"{UPSTREAM_API_BASE}{path}?orgId={httpx.QueryParams({'orgId': orgId, 'routeId': routeId})['orgId']}&routeId={routeId}"

@app.get("/meta")
async def meta_proxy(orgId: str = Query(...), routeId: str = Query(...)):
    _ensure_params(orgId, routeId)
    url = f"{UPSTREAM_API_BASE}/meta"
    logger.info(f"[proxy] -> GET {url} params={{'orgId': '{orgId}', 'routeId': '{routeId}'}}")
    try:
        r = await client.get(url, params={"orgId": orgId, "routeId": routeId})
        logger.info(f"[proxy] <- {r.status_code} from {url}")
        r.raise_for_status()
        return JSONResponse(r.json(), headers={"Cache-Control": "no-store"})
    except httpx.HTTPError as e:
        logger.exception(f"[proxy] upstream error: {e}")
        raise HTTPException(status_code=502, detail=f"upstream error: {e}")

@app.get("/stops")
async def stops_proxy(orgId: str = Query(...), routeId: str = Query(...)):
    _ensure_params(orgId, routeId)
    url = f"{UPSTREAM_API_BASE}/stops"
    try:
        r = await client.get(url, params={"orgId": orgId, "routeId": routeId})
        r.raise_for_status()
        return JSONResponse(r.json(), headers={"Cache-Control": "no-store"})
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"upstream error: {e}")

@app.get("/vehicles")
async def vehicles_proxy(orgId: str = Query(...), routeId: str = Query(...)):
    _ensure_params(orgId, routeId)
    url = f"{UPSTREAM_API_BASE}/vehicles"
    try:
        r = await client.get(url, params={"orgId": orgId, "routeId": routeId})
        r.raise_for_status()
        return JSONResponse(r.json(), headers={"Cache-Control": "no-store"})
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"upstream error: {e}")


# @app.get("/{org}/{routeNo}", response_class=HTMLResponse)
# async def route_detail(request: Request, org: str, routeNo: str) -> HTMLResponse:
#
#     # 템플릿은 route + stops 기반으로 렌더(정류소 목록만 표시)
#     return templates.TemplateResponse(
#         "route_detail.html",{"request": request}
#     )


# ----------------------------
# 개발 실행 안내 (uvicorn)
# ----------------------------
# uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
