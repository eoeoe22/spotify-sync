"""Spotify '좋아요 표시한 곡'을 공개 플레이리스트로 동기화하는 Cloudflare Worker.

라우팅:
  GET  /          → 대시보드 HTML (Cloudflare Access 보호)
  GET  /login     → Spotify OAuth 시작 (공개)
  GET  /callback  → OAuth 콜백 처리 (공개)
  GET  /status    → 동기화 상태 JSON (공개)
  GET  /playlists → 사용자 플레이리스트 목록 JSON (Access 보호)
  POST /select    → 미러 플레이리스트 선택 (Access 보호)
  POST /create    → 새 미러 플레이리스트 생성 (Access 보호)
  POST /sync      → 수동 동기화 (Access 보호)
  scheduled       → 매일 cron 자동 동기화
"""

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse, parse_qs, urlencode

from js import console, fetch, Object
from pyodide.ffi import to_js as _to_js
from workers import WorkerEntrypoint, Response

# --- 상수 정의 -------------------------------------------------------------

SPOTIFY_AUTH_URL: str = "https://accounts.spotify.com/authorize"
SPOTIFY_TOKEN_URL: str = "https://accounts.spotify.com/api/token"
SPOTIFY_API_BASE: str = "https://api.spotify.com/v1"
SCOPE: str = (
    "user-library-read "
    "playlist-read-private "
    "playlist-read-collaborative "
    "playlist-modify-public "
    "playlist-modify-private"
)

# KV 키
KV_ACCESS: str = "spotify:access_token"
KV_REFRESH: str = "spotify:refresh_token"
KV_USER: str = "spotify:user_id"
KV_PLAYLIST: str = "spotify:playlist_id"
KV_LAST_TIME: str = "spotify:last_sync_time"
KV_LAST_COUNT: str = "spotify:last_sync_count"
KV_SCOPE: str = "spotify:scope"

# 한 번에 처리 가능한 트랙 수 (Spotify 제한)
CHUNK_SIZE: int = 100


def to_js_obj(obj: Dict[str, Any]):
    """파이썬 dict 를 JS 일반 객체(Object)로 변환하는 함수.

    pyodide 의 기본 변환은 dict 를 JS Map 으로 바꾸므로, fetch 옵션이나
    KV put 옵션처럼 일반 객체가 필요한 곳에서는 이 함수로 변환한다.
    """
    return _to_js(obj, dict_converter=Object.fromEntries)


# --- 저수준 HTTP 헬퍼 ------------------------------------------------------

async def http_request(
    method: str,
    url: str,
    token: Optional[str] = None,
    json_body: Optional[Dict[str, Any]] = None,
    form_body: Optional[Dict[str, str]] = None,
) -> Tuple[int, Dict[str, Any]]:
    """HTTP 요청을 보내고 (상태코드, 응답 dict) 튜플을 돌려주는 함수.

    json_body 가 있으면 application/json 으로, form_body 가 있으면
    application/x-www-form-urlencoded 로 본문을 인코딩한다.
    """
    headers: Dict[str, str] = {}
    if token:
        headers["Authorization"] = "Bearer " + token

    body: Optional[str] = None
    if json_body is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(json_body)
    elif form_body is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        body = urlencode(form_body)

    options: Dict[str, Any] = {"method": method, "headers": headers}
    if body is not None:
        options["body"] = body

    resp = await fetch(url, to_js_obj(options))
    status: int = int(resp.status)
    text: str = await resp.text()
    data: Dict[str, Any] = json.loads(text) if text else {}
    return status, data


# --- 토큰 관리 -------------------------------------------------------------

async def refresh_token(env) -> Optional[str]:
    """저장된 refresh_token 으로 새 access_token 을 발급받아 KV 에 저장하는 함수.

    성공하면 새 access_token(str), 실패하면 None 을 반환한다.
    """
    rt: Optional[str] = await env.SPOTIFY_KV.get(KV_REFRESH)
    if not rt:
        console.log("refresh_token 이 없습니다. /login 으로 재인증이 필요합니다.")
        return None

    form: Dict[str, str] = {
        "grant_type": "refresh_token",
        "refresh_token": rt,
        "client_id": str(env.SPOTIFY_CLIENT_ID),
        "client_secret": str(env.SPOTIFY_CLIENT_SECRET),
    }
    status, data = await http_request("POST", SPOTIFY_TOKEN_URL, form_body=form)
    if status != 200:
        console.log("토큰 갱신 실패 status=" + str(status) + " body=" + json.dumps(data))
        return None

    access: str = data.get("access_token")
    await env.SPOTIFY_KV.put(KV_ACCESS, access, to_js_obj({"expirationTtl": 3600}))

    # Spotify 가 새 refresh_token 을 함께 줄 수도 있으므로 갱신한다.
    new_rt: Optional[str] = data.get("refresh_token")
    if new_rt:
        await env.SPOTIFY_KV.put(KV_REFRESH, new_rt)

    console.log("access_token 갱신 완료")
    return access


async def get_token(env) -> Optional[str]:
    """유효한 access_token 을 반환하는 함수.

    KV 에 access_token 이 살아 있으면 그대로 쓰고, TTL 만료로 사라졌으면
    refresh_token() 을 호출해 자동 갱신한다. 둘 다 불가능하면 None.
    """
    access: Optional[str] = await env.SPOTIFY_KV.get(KV_ACCESS)
    if access:
        return access
    return await refresh_token(env)


async def spotify_api(
    env,
    method: str,
    path: str,
    json_body: Optional[Dict[str, Any]] = None,
) -> Tuple[int, Dict[str, Any]]:
    """access_token 을 붙여 Spotify API 를 호출하는 함수.

    path 가 전체 URL(http로 시작)이면 그대로 쓰고, 아니면 API 베이스에 붙인다.
    401 응답이 오면 토큰을 갱신한 뒤 한 번 재시도한다.
    """
    token: Optional[str] = await get_token(env)
    if not token:
        return 401, {}

    url: str = path if path.startswith("http") else SPOTIFY_API_BASE + path
    status, data = await http_request(method, url, token=token, json_body=json_body)

    if status == 401:
        token = await refresh_token(env)
        if not token:
            return 401, {}
        status, data = await http_request(method, url, token=token, json_body=json_body)

    return status, data


# --- Spotify 데이터 수집 ---------------------------------------------------

async def fetch_liked_songs(env) -> List[str]:
    """좋아요 표시한 곡 전체의 track URI 리스트를 페이지네이션으로 모으는 함수.

    GET /me/tracks 를 limit=50 으로 호출하고, 응답의 'next' 가 None 이 될 때까지
    while 반복문으로 다음 페이지를 계속 가져온다.
    """
    uris: List[str] = []
    url: Optional[str] = "/me/tracks?limit=50"

    while url:
        status, data = await spotify_api(env, "GET", url)
        if status != 200:
            console.log("좋아요 곡 조회 실패 status=" + str(status))
            break
        items: List[Dict[str, Any]] = data.get("items", [])
        for item in items:
            track: Optional[Dict[str, Any]] = item.get("track")
            if track and track.get("uri"):
                uris.append(track["uri"])
        url = data.get("next")  # 다음 페이지 전체 URL 또는 None

    console.log("좋아요 곡 " + str(len(uris)) + "개 수집 완료")
    return uris


async def fetch_playlist_uris(env, playlist_id: str) -> Set[str]:
    """대상 플레이리스트에 이미 들어 있는 트랙 URI 집합을 모으는 함수.

    2026 API 마이그레이션으로 /tracks -> /items, 각 항목의 track -> item 으로 바뀌어
    item.item 을 우선 보고 옛 응답(track)도 폴백으로 지원한다.
    """
    existing: Set[str] = set()
    url: Optional[str] = "/playlists/" + playlist_id + "/items?limit=100"

    while url:
        status, data = await spotify_api(env, "GET", url)
        if status != 200:
            console.log("플레이리스트 트랙 조회 실패 status=" + str(status))
            break
        for item in data.get("items", []):
            entry: Optional[Dict[str, Any]] = item.get("item") or item.get("track")
            if entry and entry.get("uri"):
                existing.add(entry["uri"])
        url = data.get("next")

    return existing


async def get_user_id(env) -> Optional[str]:
    """현재 토큰의 Spotify user_id 를 반환하는 함수.

    항상 /me 로 현재 토큰 주인의 id 를 조회해 KV 에 최신화한다. (재인증으로 계정이
    바뀌었을 때 KV 의 옛 user_id 를 그대로 쓰면 플레이리스트 생성이 403/404 로
    실패하므로, 신선한 값을 사용한다.) /me 가 실패하면 KV 의 마지막 값으로 폴백한다.
    """
    status, data = await spotify_api(env, "GET", "/me")
    if status == 200 and data.get("id"):
        user_id: str = data["id"]
        await env.SPOTIFY_KV.put(KV_USER, user_id)
        return user_id
    console.log("/me 조회 실패 status=" + str(status) + ", KV 값으로 폴백")
    return await env.SPOTIFY_KV.get(KV_USER)


async def list_playlists(env) -> Tuple[int, List[Dict[str, Any]]]:
    """현재 사용자의 플레이리스트 목록을 (상태코드, dict 리스트)로 반환하는 함수.

    Spotify 조회가 실패하면 그 상태코드를 함께 돌려주어, 호출 측이 '목록 없음'과
    '조회 실패(스코프 부족 등)'를 구분할 수 있게 한다.
    """
    user_id: Optional[str] = await get_user_id(env)
    result: List[Dict[str, Any]] = []
    url: Optional[str] = "/me/playlists?limit=50"

    while url:
        status, data = await spotify_api(env, "GET", url)
        if status != 200:
            console.log("플레이리스트 목록 조회 실패 status=" + str(status))
            return status, result
        for pl in data.get("items", []):
            owner_id: Optional[str] = (pl.get("owner") or {}).get("id")
            editable: bool = (owner_id == user_id) or bool(pl.get("collaborative"))
            result.append({
                "id": pl.get("id"),
                "name": pl.get("name"),
                "owner": owner_id,
                "editable": editable,
                "tracks": (pl.get("items") or pl.get("tracks") or {}).get("total", 0),
            })
        url = data.get("next")

    console.log("플레이리스트 " + str(len(result)) + "개 조회. user_id="
                + str(user_id) + " 샘플=" + json.dumps(result[:3]))
    return 200, result


async def create_playlist(env, name: str) -> Tuple[Optional[str], int, str]:
    """새 공개 플레이리스트를 만들고 (id, 상태코드, 오류메시지)를 반환하는 함수.

    성공 시 (playlist_id, 201, ""), 실패 시 (None, status, 사람이 읽을 오류).
    """
    user_id: Optional[str] = await get_user_id(env)
    if not user_id:
        return None, 0, "사용자 정보를 확인할 수 없습니다. /login 으로 다시 인증하세요."

    body: Dict[str, Any] = {
        "name": name,
        "public": True,
        "description": "좋아요 표시한 곡 자동 미러 (spotify-sync)",
    }
    status, data = await spotify_api(env, "POST", "/users/" + user_id + "/playlists", json_body=body)
    if status not in (200, 201):
        console.log("플레이리스트 생성 실패 status=" + str(status) + " body=" + json.dumps(data))
        msg: str = "Spotify 생성 실패 (status " + str(status) + ")"
        err = data.get("error") if isinstance(data, dict) else None
        if isinstance(err, dict) and err.get("message"):
            msg += ": " + str(err["message"])
        if status == 403:
            msg += " — 스코프 부족 또는 다른 계정의 사용자 id 입니다. /login 으로 다시 인증하세요."
        return None, status, msg

    return data.get("id"), status, ""


# --- 플레이리스트 쓰기 -----------------------------------------------------

async def replace_playlist(env, playlist_id: str, uris: List[str]) -> bool:
    """플레이리스트 전체를 좋아요 곡으로 교체하는 함수 (최초 동기화 전용).

    첫 100개는 PUT 으로 보내 기존 트랙을 전부 덮어쓰고(제거 효과), 나머지는
    100개씩 청크로 나눠 POST 로 이어 붙인다.
    """
    first: List[str] = uris[:CHUNK_SIZE]
    status, data = await spotify_api(
        env, "PUT", "/playlists/" + playlist_id + "/items", json_body={"uris": first}
    )
    if status not in (200, 201):
        console.log("전체 교체 PUT 실패 status=" + str(status) + " body=" + json.dumps(data))
        return False

    rest: List[str] = uris[CHUNK_SIZE:]
    for i in range(0, len(rest), CHUNK_SIZE):
        chunk: List[str] = rest[i:i + CHUNK_SIZE]
        status, data = await spotify_api(
            env, "POST", "/playlists/" + playlist_id + "/items", json_body={"uris": chunk}
        )
        if status not in (200, 201):
            console.log("전체 교체 POST 실패 status=" + str(status) + " body=" + json.dumps(data))
            return False
    return True


async def add_tracks(env, playlist_id: str, uris: List[str]) -> bool:
    """플레이리스트에 트랙을 100개씩 청크로 추가하는 함수 (증분 동기화 전용)."""
    for i in range(0, len(uris), CHUNK_SIZE):
        chunk: List[str] = uris[i:i + CHUNK_SIZE]
        status, data = await spotify_api(
            env, "POST", "/playlists/" + playlist_id + "/items", json_body={"uris": chunk}
        )
        if status not in (200, 201):
            console.log("트랙 추가 실패 status=" + str(status) + " body=" + json.dumps(data))
            return False
    return True


# --- 메인 동기화 -----------------------------------------------------------

async def sync_playlist(env) -> Dict[str, Any]:
    """좋아요 곡을 미러 플레이리스트에 동기화하는 메인 함수.

    - 최초 동기화(last_sync_time 없음): 플레이리스트 전체를 교체한다.
    - 이후 동기화: 플레이리스트에 아직 없는 곡만 새로 추가한다.
    동기화 후 KV 에 마지막 동기화 시각/곡 수를 저장한다.
    """
    playlist_id: Optional[str] = await env.SPOTIFY_KV.get(KV_PLAYLIST)
    if not playlist_id:
        msg = "미러 플레이리스트가 선택되지 않았습니다. 대시보드에서 먼저 선택하세요."
        console.log(msg)
        return {"ok": False, "error": msg}

    token: Optional[str] = await get_token(env)
    if not token:
        msg = "Spotify 인증 정보가 없습니다. /login 으로 다시 연동하세요."
        console.log(msg)
        return {"ok": False, "error": msg}

    liked: List[str] = await fetch_liked_songs(env)
    last_sync: Optional[str] = await env.SPOTIFY_KV.get(KV_LAST_TIME)

    if not last_sync:
        # 최초 동기화: 전체 교체
        console.log("최초 동기화: 전체 교체를 수행합니다.")
        ok: bool = await replace_playlist(env, playlist_id, liked)
        added: int = len(liked) if ok else 0
    else:
        # 증분 동기화: 플레이리스트에 없는 곡만 추가
        existing: Set[str] = await fetch_playlist_uris(env, playlist_id)
        new_uris: List[str] = [u for u in liked if u not in existing]
        console.log("증분 동기화: 신규 " + str(len(new_uris)) + "개 추가")
        ok = await add_tracks(env, playlist_id, new_uris)
        added = len(new_uris) if ok else 0

    if not ok:
        return {"ok": False, "error": "Spotify API 호출에 실패했습니다."}

    now: str = datetime.now(timezone.utc).isoformat()
    await env.SPOTIFY_KV.put(KV_LAST_TIME, now)
    await env.SPOTIFY_KV.put(KV_LAST_COUNT, str(len(liked)))
    console.log("동기화 완료 time=" + now + " total=" + str(len(liked)) + " added=" + str(added))

    return {"ok": True, "last_sync": now, "track_count": len(liked), "added": added}


# --- 응답 헬퍼 -------------------------------------------------------------

def json_response(obj: Dict[str, Any], status: int = 200) -> Response:
    """dict 를 JSON 본문으로 직렬화해 Response 를 만드는 함수."""
    return Response(
        json.dumps(obj),
        status=status,
        headers={"Content-Type": "application/json; charset=utf-8"},
    )


async def read_json(request) -> Dict[str, Any]:
    """요청 본문을 JSON dict 로 파싱하는 함수 (본문이 없거나 깨지면 빈 dict)."""
    try:
        text: str = await request.text()
        if not text:
            return {}
        return json.loads(text)
    except Exception as exc:  # noqa: BLE001
        console.log("요청 본문 파싱 실패: " + str(exc))
        return {}


# --- OAuth 핸들러 ----------------------------------------------------------

def _origin(request) -> str:
    """요청 URL 에서 'scheme://host' 형태의 오리진을 추출하는 함수."""
    parsed = urlparse(request.url)
    return parsed.scheme + "://" + parsed.netloc


async def handle_login(request, env) -> Response:
    """Spotify 인증 페이지로 리다이렉트하는 함수."""
    redirect_uri: str = _origin(request) + "/callback"
    params: Dict[str, str] = {
        "client_id": str(env.SPOTIFY_CLIENT_ID),
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": SCOPE,
        "show_dialog": "true",
    }
    auth_url: str = SPOTIFY_AUTH_URL + "?" + urlencode(params)
    return Response.redirect(auth_url, 302)


async def handle_callback(request, env) -> Response:
    """OAuth 콜백을 처리해 토큰을 발급받아 KV 에 저장하는 함수."""
    parsed = urlparse(request.url)
    qs = parse_qs(parsed.query)
    error: Optional[str] = qs.get("error", [None])[0]
    code: Optional[str] = qs.get("code", [None])[0]

    if error:
        return Response("인증이 거부되었습니다: " + error, status=400)
    if not code:
        return Response("code 파라미터가 없습니다.", status=400)

    redirect_uri: str = _origin(request) + "/callback"
    form: Dict[str, str] = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": str(env.SPOTIFY_CLIENT_ID),
        "client_secret": str(env.SPOTIFY_CLIENT_SECRET),
    }
    status, data = await http_request("POST", SPOTIFY_TOKEN_URL, form_body=form)
    if status != 200:
        return Response("토큰 발급 실패: " + json.dumps(data), status=400)

    access: str = data.get("access_token")
    refresh: Optional[str] = data.get("refresh_token")
    granted_scope: str = data.get("scope") or ""
    console.log("토큰 발급 완료. 부여된 scope=" + granted_scope)
    await env.SPOTIFY_KV.put(KV_ACCESS, access, to_js_obj({"expirationTtl": 3600}))
    await env.SPOTIFY_KV.put(KV_SCOPE, granted_scope)
    if refresh:
        await env.SPOTIFY_KV.put(KV_REFRESH, refresh)

    # 사용자 id 저장 (플레이리스트 생성/소유 판별에 사용)
    ustatus, udata = await http_request("GET", SPOTIFY_API_BASE + "/me", token=access)
    if ustatus == 200 and udata.get("id"):
        await env.SPOTIFY_KV.put(KV_USER, udata["id"])

    console.log("OAuth 연동 완료")
    return Response.redirect(_origin(request) + "/", 302)


async def handle_status(env) -> Response:
    """동기화 상태를 JSON 으로 반환하는 함수 (공개 엔드포인트)."""
    refresh: Optional[str] = await env.SPOTIFY_KV.get(KV_REFRESH)
    synced: bool = refresh is not None

    last_sync: str = (await env.SPOTIFY_KV.get(KV_LAST_TIME)) or ""
    count_str: Optional[str] = await env.SPOTIFY_KV.get(KV_LAST_COUNT)
    track_count: int = int(count_str) if count_str else 0

    playlist_id: Optional[str] = await env.SPOTIFY_KV.get(KV_PLAYLIST)
    playlist_url: str = ("https://open.spotify.com/playlist/" + playlist_id) if playlist_id else ""

    scope: str = (await env.SPOTIFY_KV.get(KV_SCOPE)) or ""

    payload: Dict[str, Any] = {
        "synced": synced,
        "last_sync": last_sync,
        "track_count": track_count,
        "playlist_url": playlist_url,
        "playlist_id": playlist_id or "",
        "scope": scope,
    }
    return json_response(payload)


# --- 라우팅 ----------------------------------------------------------------

async def handle_fetch(request, env) -> Response:
    """들어온 HTTP 요청을 경로/메서드에 따라 분기 처리하는 함수."""
    parsed = urlparse(request.url)
    path: str = parsed.path
    method: str = request.method

    # 공개 경로 (Cloudflare Access 보호 제외)
    if path == "/login":
        return await handle_login(request, env)
    if path == "/callback":
        return await handle_callback(request, env)
    if path == "/status":
        return await handle_status(env)
    if path == "/favicon.ico":
        return Response(None, status=204)

    # IP 화이트리스트 확인 — 허용된 IP 이면 JWT 인증 건너뜀
    client_ip: str = request.headers.get("CF-Connecting-IP") or ""
    raw_whitelist: str = getattr(env, "IP_WHITELIST", "") or ""
    allowed_ips = {ip.strip() for ip in raw_whitelist.split(",") if ip.strip()}
    ip_whitelisted: bool = bool(client_ip and client_ip in allowed_ips)

    # Cloudflare Access JWT 헤더 검증 (헤더 존재 여부만 확인)
    if not ip_whitelisted:
        jwt: Optional[str] = request.headers.get("CF-Access-Jwt-Assertion")
        if not jwt:
            return Response(
                "403 Forbidden: Cloudflare Access 인증이 필요한 경로입니다.",
                status=403,
            )

    if path == "/" and method == "GET":
        return Response(DASHBOARD_HTML, headers={"Content-Type": "text/html; charset=utf-8"})

    if path == "/playlists" and method == "GET":
        status, playlists = await list_playlists(env)
        if status != 200:
            detail = "Spotify 플레이리스트 조회 실패 (status " + str(status) + ")."
            if status == 403:
                detail += " 스코프가 부족합니다. /login 으로 다시 인증하세요."
            return json_response({"playlists": [], "error": detail}, status=status)
        return json_response({"playlists": playlists})

    if path == "/select" and method == "POST":
        body = await read_json(request)
        pid: Optional[str] = body.get("playlist_id")
        if not pid:
            return json_response({"ok": False, "error": "playlist_id 가 필요합니다."}, status=400)
        await env.SPOTIFY_KV.put(KV_PLAYLIST, pid)
        # 새 플레이리스트로 바꾸면 다음 동기화를 전체 교체로 다시 시작한다.
        await env.SPOTIFY_KV.delete(KV_LAST_TIME)
        await env.SPOTIFY_KV.delete(KV_LAST_COUNT)
        console.log("미러 플레이리스트 선택: " + pid)
        return json_response({"ok": True, "playlist_id": pid})

    if path == "/create" and method == "POST":
        body = await read_json(request)
        name: str = body.get("name") or "좋아요 미러"
        new_id, cstatus, cerr = await create_playlist(env, name)
        if not new_id:
            http_status = cstatus if cstatus in (401, 403, 404) else 400
            return json_response({"ok": False, "error": cerr or "플레이리스트 생성 실패"}, status=http_status)
        await env.SPOTIFY_KV.put(KV_PLAYLIST, new_id)
        await env.SPOTIFY_KV.delete(KV_LAST_TIME)
        await env.SPOTIFY_KV.delete(KV_LAST_COUNT)
        console.log("미러 플레이리스트 생성: " + new_id)
        return json_response({"ok": True, "playlist_id": new_id})

    if path == "/sync" and method == "POST":
        result = await sync_playlist(env)
        return json_response(result, status=200 if result.get("ok") else 400)

    return Response("404 Not Found", status=404)


# --- 대시보드 HTML ---------------------------------------------------------

DASHBOARD_HTML: str = """<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Spotify Sync</title>
  <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.8/dist/css/bootstrap.min.css" rel="stylesheet" integrity="sha384-sRIl4kxILFvY47J16cr9ZwB07vP4J8+LH7qKQnuqkuIAvNWLzeN8tE5YBujZqJLB" crossorigin="anonymous">
  <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.8/dist/js/bootstrap.bundle.min.js" integrity="sha384-FKyoEForCGlyvwx9Hj09JcYn3nv7wiPVlz7YYwJrWVcXK/BmnVDxM+D2scQbITxI" crossorigin="anonymous"></script>
</head>
<body class="bg-light">
  <div class="container py-5" style="max-width: 720px;">
    <h1 class="mb-4">Spotify 좋아요 동기화</h1>

    <div class="card mb-3">
      <div class="card-body">
        <h5 class="card-title">상태</h5>
        <p class="mb-2">연동 상태: <span id="conn" class="badge bg-secondary">확인 중...</span></p>
        <p class="mb-2">마지막 동기화: <span id="lastSync">-</span></p>
        <p class="mb-2">동기화된 트랙 수: <span id="trackCount">0</span></p>
        <p class="mb-0">미러 플레이리스트:
          <a id="playlistLink" href="#" target="_blank" style="display:none;"></a>
          <span id="noPlaylist" class="text-muted">선택되지 않음</span>
        </p>
      </div>
    </div>

    <div class="card mb-3" id="playlistCard" style="display:none;">
      <div class="card-body">
        <h5 class="card-title">미러 플레이리스트 선택</h5>
        <div class="d-flex gap-2 align-items-center flex-wrap">
          <select id="playlistSelect" class="form-select" style="max-width: 360px;"></select>
          <button class="btn btn-outline-primary" onclick="savePlaylist()">선택 저장</button>
          <button class="btn btn-outline-success" onclick="createPlaylist()">새 플레이리스트 생성</button>
        </div>
        <small id="playlistHint" class="text-muted d-block mt-2"></small>
      </div>
    </div>

    <div class="d-flex gap-2">
      <a class="btn btn-success" href="/login">Spotify 연동</a>
      <button id="syncBtn" class="btn btn-primary" onclick="doSync()">지금 동기화</button>
    </div>
  </div>

  <script>
    async function loadStatus() {
      const res = await fetch('/status');
      const s = await res.json();

      const conn = document.getElementById('conn');
      conn.textContent = s.synced ? '연동됨' : '연동 안됨';
      conn.className = s.synced ? 'badge bg-success' : 'badge bg-secondary';

      document.getElementById('lastSync').textContent = s.last_sync || '없음';
      document.getElementById('trackCount').textContent = s.track_count;

      const link = document.getElementById('playlistLink');
      const none = document.getElementById('noPlaylist');
      if (s.playlist_url) {
        link.href = s.playlist_url;
        link.textContent = s.playlist_url;
        link.style.display = 'inline';
        none.style.display = 'none';
      } else {
        link.style.display = 'none';
        none.style.display = 'inline';
      }

      document.getElementById('playlistCard').style.display = s.synced ? 'block' : 'none';
      if (s.synced) { loadPlaylists(s.playlist_id); }
    }

    async function loadPlaylists(current) {
      const sel = document.getElementById('playlistSelect');
      const hint = document.getElementById('playlistHint');
      hint.textContent = '';
      let res;
      try {
        res = await fetch('/playlists');
      } catch (e) {
        hint.textContent = '목록 요청 중 오류: ' + e;
        return;
      }
      let data = {};
      try { data = await res.json(); } catch (e) { /* 본문 없음 */ }
      if (!res.ok) {
        sel.innerHTML = '';
        hint.textContent = data.error
          || ('목록을 불러오지 못했습니다 (HTTP ' + res.status
              + '). Spotify 연동 상태와 Cloudflare Access 설정을 확인하세요.');
        return;
      }
      const lists = data.playlists || [];
      sel.innerHTML = '';
      if (lists.length === 0) {
        hint.textContent = '플레이리스트가 없습니다. "새 플레이리스트 생성"으로 만들어 주세요.';
        return;
      }
      lists.forEach(function (p) {
        const opt = document.createElement('option');
        opt.value = p.id;
        opt.textContent = p.name + ' (' + p.tracks + '곡)' + (p.editable ? '' : ' [편집불가]');
        if (!p.editable) { opt.disabled = true; }
        if (p.id === current) { opt.selected = true; }
        sel.appendChild(opt);
      });
    }

    async function postJson(url, payload) {
      const res = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });
      let body = {};
      try { body = await res.json(); } catch (e) { /* 본문 없음 */ }
      return { ok: res.ok, status: res.status, body: body };
    }

    async function savePlaylist() {
      const pid = document.getElementById('playlistSelect').value;
      if (!pid) { return; }
      const r = await postJson('/select', { playlist_id: pid });
      alert(r.ok ? '저장되었습니다.' : ('저장 실패: ' + (r.body.error || ('HTTP ' + r.status))));
      loadStatus();
    }

    async function createPlaylist() {
      const name = prompt('새 플레이리스트 이름을 입력하세요.', '좋아요 미러');
      if (!name) { return; }
      const r = await postJson('/create', { name: name });
      alert(r.ok ? '생성되었습니다.' : ('생성 실패: ' + (r.body.error || ('HTTP ' + r.status))));
      loadStatus();
    }

    async function doSync() {
      const btn = document.getElementById('syncBtn');
      btn.disabled = true;
      btn.textContent = '동기화 중...';
      try {
        const res = await fetch('/sync', { method: 'POST' });
        const r = await res.json();
        if (r.ok) {
          alert('동기화 완료: 총 ' + r.track_count + '곡 (신규 ' + r.added + '곡)');
        } else {
          alert('동기화 실패: ' + (r.error || '알 수 없는 오류'));
        }
      } finally {
        btn.disabled = false;
        btn.textContent = '지금 동기화';
        loadStatus();
      }
    }

    window.addEventListener('DOMContentLoaded', loadStatus);
  </script>
</body>
</html>
"""


# --- Worker 엔트리포인트 ---------------------------------------------------

class Default(WorkerEntrypoint):
    """Cloudflare Worker 진입점. fetch(HTTP)와 scheduled(cron)를 처리한다."""

    async def fetch(self, request) -> Response:
        """들어온 HTTP 요청을 처리하고, 예외는 500 응답으로 감싸는 핸들러."""
        try:
            return await handle_fetch(request, self.env)
        except Exception as exc:  # noqa: BLE001
            console.log("처리 중 오류: " + str(exc))
            return Response("500 Internal Server Error: " + str(exc), status=500)

    async def scheduled(self, controller, env=None, ctx=None) -> None:
        """매일 cron 트리거로 자동 동기화를 수행하는 핸들러."""
        console.log("cron 동기화 시작")
        result = await sync_playlist(env or self.env)
        console.log("cron 동기화 결과: " + json.dumps(result))
