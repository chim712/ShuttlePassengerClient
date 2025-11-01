# app/main.py
import os
from typing import Any, Dict, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import httpx

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
async def fetch_json(path: str, params: Optional[Dict[str, Any]] = None) -> Any:
    """
    외부 REST 서버에서 JSON을 받아온다.
    예외/상태코드 처리 포함. path는 '/orgs' 같은 상대 경로 기준.
    """
    assert client is not None, "HTTP client is not initialized"
    try:
        resp = await client.get(path, params=params)
    except httpx.RequestError as e:
        # 네트워크 오류
        raise HTTPException(status_code=502, detail=f"Upstream request error: {e}") from e

    if resp.status_code == 404:
        raise HTTPException(status_code=404, detail="리소스를 찾을 수 없습니다.")
    if resp.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"Upstream error: {resp.status_code}")

    try:
        return resp.json()
    except ValueError:
        raise HTTPException(status_code=502, detail="Invalid JSON from upstream")


# ----------------------------
# 페이지 라우트
# ----------------------------

# (1) 홈: 기관 선택
#   - 외부 REST 예시: GET /orgs  ->  [{ "id": "SCH", "name": "순천향대학교" }, ...]
@app.get("/", response_class=HTMLResponse)
async def home(request: Request) -> HTMLResponse:
    orgs = await fetch_json("/orgs")
    # 템플릿은 셀렉트박스 렌더 기준으로 orgs: List[Dict] 가정
    return templates.TemplateResponse("home.html", {"request": request, "orgs": orgs})


# (2) 기관 내 노선 목록: /{org}
#   - 외부 REST 예시: GET /orgs/{org}/routes
#   - 응답 예시: [{ "routeNo": "1501", "name": "저상 1501", "direction": "상행" }, ...]
@app.get("/{org}", response_class=HTMLResponse)
async def route_list(request: Request, org: str) -> HTMLResponse:
    routes = await fetch_json(f"/orgs/{org}/routes")
    return templates.TemplateResponse(
        "route_list.html",
        {"request": request, "org": org, "routes": routes},
    )


# (3) 노선 상세(정류소 목록): /{org}/{routeNo}
#   - 외부 REST 예시(둘 중 하나를 사용):
#       A) GET /orgs/{org}/routes/{routeNo}           -> { "routeNo": "...", "name": "...", ... }
#          GET /orgs/{org}/routes/{routeNo}/stops     -> [{ "stopId": 946, "name": "신동", "seq": 1 }, ...]
#       B) GET /orgs/{org}/routes/{routeNo}/detail    -> { "route": {...}, "stops": [...] }
@app.get("/{org}/{routeNo}", response_class=HTMLResponse)
async def route_detail(request: Request, org: str, routeNo: str) -> HTMLResponse:
    # 우선 B형(detail) 엔드포인트를 먼저 시도하고, 없으면 A형으로 폴백
    try:
        detail = await fetch_json(f"/orgs/{org}/routes/{routeNo}/detail")
        route = detail.get("route", {})
        stops = detail.get("stops", [])
    except HTTPException as e:
        if e.status_code == 404:
            # A형으로 폴백
            route = await fetch_json(f"/orgs/{org}/routes/{routeNo}")
            stops = await fetch_json(f"/orgs/{org}/routes/{routeNo}/stops")
        else:
            raise

    # 템플릿은 route + stops 기반으로 렌더(정류소 목록만 표시)
    return templates.TemplateResponse(
        "route_detail.html",
        {
            "request": request,
            "org": org,
            "routeNo": routeNo,
            "route": route,
            "stops": stops,
        },
    )


# ----------------------------
# 개발 실행 안내 (uvicorn)
# ----------------------------
# uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
