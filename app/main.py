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
API_BASE_URL = os.getenv("API_BASE_URL", "http://localhost:8000")  # 외부 REST 서버(미사용 가능)
API_KEY = os.getenv("API_KEY", "")  # 필요 없다면 빈 값 유지

# 임시(외부) 데이터 서버
UPSTREAM_API_BASE = os.getenv("UPSTREAM_API_BASE", "https://shuttle-roid-894717980119.asia-northeast3.run.app/")
UPSTREAM_API_BASE2 = os.getenv("UPSTREAM_API_BASE", "http://zxcv.imagine.io.kr:9900")
TIMEOUT = float(os.getenv("UPSTREAM_TIMEOUT", "3.0"))

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


# ----------------------------
# 페이지 라우트 (SSR: HTML만)
# ----------------------------

# (1) 홈: 기관 선택
@app.get("/", response_class=HTMLResponse)
async def home(request: Request) -> HTMLResponse:
    # orgs는 이후 필요시 프록시 + 클라렌더 방식으로 확장 가능
    return templates.TemplateResponse("home.html", {"request": request})


# (2) 기관 내 노선 목록 페이지(HTML)
#    실제 데이터는 /routes-data 프록시를 통해 클라이언트 JS가 주입한다.
@app.get("/routes", response_class=HTMLResponse)
async def routes(request: Request, orgId: str = Query(...)) -> HTMLResponse:
    return templates.TemplateResponse(
        "route_list.html",
        {
            "request": request,
            "orgId": orgId,
        },
    )


# (3) 노선 상세(정류소 목록) 페이지(HTML)
@app.get("/{org}/{routeNo}", response_class=HTMLResponse)
async def route_detail(request: Request, org: str, routeNo: str) -> HTMLResponse:
    """
    HTML만 SSR. org/routeNo를 템플릿에 주입하여
    클라이언트 JS가 /meta, /stops, /vehicles 프록시를 호출하도록 한다.
    """
    return templates.TemplateResponse(
        "route_detail.html",
        {
            "request": request,
            "orgId": org,
            "routeId": routeNo,
            "apiBase": "",  # 동일 오리진 프록시 사용
        },
    )


# ----------------------------
# 프록시 엔드포인트
# ----------------------------

def _ensure_params(orgId: Optional[str], routeId: Optional[str]) -> None:
    if not orgId or not routeId:
        raise HTTPException(status_code=400, detail="orgId and routeId are required.")


# (A) 노선 메타
@app.get("/meta")
async def meta_proxy(orgId: str = Query(...), routeId: str = Query(...)):
    url = f"{UPSTREAM_API_BASE}/user/meta" if orgId == "1" else f"{UPSTREAM_API_BASE2}/meta"
    logger.info(f"[proxy] -> GET {url} params={{'orgID': '{orgId}', 'routeID': '{routeId}'}}")
    try:
        assert client is not None, "HTTP client is not initialized"
        r = await client.get(url, params={"orgID": orgId, "routeID": routeId}, timeout=TIMEOUT)
        logger.info(f"[proxy] <- {r.status_code} from {url}")
        r.raise_for_status()
        return JSONResponse(r.json(), headers={"Cache-Control": "no-store"})
    except httpx.HTTPError as e:
        logger.exception(f"[proxy] upstream error: {e}")
        raise HTTPException(status_code=502, detail=f"upstream error: {e}")


# (B) 정류소 목록
@app.get("/stops")
async def stops_proxy(orgId: str = Query(...), routeId: str = Query(...)):
    url = f"{UPSTREAM_API_BASE}/user/stops" if orgId == "1" else f"{UPSTREAM_API_BASE2}/stops"
    try:
        assert client is not None, "HTTP client is not initialized"
        r = await client.get(url, params={"orgID": orgId, "routeID": routeId}, timeout=TIMEOUT)
        r.raise_for_status()
        return JSONResponse(r.json(), headers={"Cache-Control": "no-store"})
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"upstream error: {e}")


# (C) 차량 목록
@app.get("/vehicles")
async def vehicles_proxy(orgId: str = Query(...), routeId: str = Query(...)):
    url = f"{UPSTREAM_API_BASE}/user/vehicles" if orgId == "1" else f"{UPSTREAM_API_BASE2}/vehicles"
    try:
        assert client is not None, "HTTP client is not initialized"
        r = await client.get(url, params={"orgID": orgId, "routeID": routeId}, timeout=TIMEOUT)
        r.raise_for_status()
        return JSONResponse(r.json(), headers={"Cache-Control": "no-store"})
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"upstream error: {e}")


# (D) 노선 목록  ← [신규] /routes 페이지용 프록시
#     외부 서버 규약: GET /routes?orgId=...
@app.get("/routes-data")
async def routes_data_proxy(orgId: str = Query(...)):
    url = f"{UPSTREAM_API_BASE}/user/route-list" if orgId == "1" else f"{UPSTREAM_API_BASE2}/routes"
    logger.info(f"[proxy] -> GET {url} params={{'orgID': '{orgId}'}}")
    try:
        assert client is not None, "HTTP client is not initialized"
        r = await client.get(url, params={"orgID": orgId}, timeout=TIMEOUT)
        logger.info(f"[proxy] <- {r.status_code} from {url}")
        r.raise_for_status()
        # 외부가 배열 혹은 {routes:[...]} 모두 수용
        data = r.json()
        if isinstance(data, dict) and "routes" in data:
            data = data["routes"]
        if not isinstance(data, list):
            raise HTTPException(status_code=502, detail="Invalid routes payload from upstream")
        return JSONResponse(data, headers={"Cache-Control": "no-store"})
    except httpx.HTTPError as e:
        logger.exception(f"[proxy] upstream error: {e}")
        raise HTTPException(status_code=502, detail=f"upstream error: {e}")


# (E) 기관 목록  ← [신규] / 루트(기관 선택) 페이지용 프록시
#     외부 서버 규약: GET /orgs  (옵션: ?q=검색어 등)
@app.get("/orgs-data")
async def orgs_data_proxy(q: Optional[str] = Query(None)):
    url = f"{UPSTREAM_API_BASE2}/user/orgs"
    params: Dict[str, Any] = {}
    if q:
        params["q"] = q

    logger.info(f"[proxy] -> GET {url} params={params or '{}'}")
    try:
        assert client is not None, "HTTP client is not initialized"
        r = await client.get(url, params=params, timeout=TIMEOUT)
        logger.info(f"[proxy] <- {r.status_code} from {url}")
        r.raise_for_status()

        # 외부가 배열 또는 {orgs:[...]} 모두 수용
        data = r.json()
        if isinstance(data, dict) and "orgs" in data:
            data = data["orgs"]
        if not isinstance(data, list):
            raise HTTPException(status_code=502, detail="Invalid orgs payload from upstream")

        # 그대로 프런트로 전달 (home.html의 JS가 렌더링)
        return JSONResponse(data, headers={"Cache-Control": "no-store"})
    except httpx.HTTPError as e:
        logger.exception(f"[proxy] upstream error: {e}")
        raise HTTPException(status_code=502, detail=f"upstream error: {e}")

# ----------------------------
# 개발 실행 안내 (uvicorn)
# ----------------------------
# uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
