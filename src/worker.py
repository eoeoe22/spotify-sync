"""Spotify 좋아요 곡을 공개 플레이리스트로 동기화하는 Cloudflare Worker (Python).

Spotify Web API 공식 문서를 기준으로 처음부터 다시 작성한 구현이다.
2026년 2월/3월 마이그레이션 이후의 엔드포인트와 응답 스키마를 따른다.

라우팅:
  GET  /          → 대시보드 (Cloudflare Access 보호)
  GET  /login     → Spotify OAuth 시작 (공개)
  GET  /callback  → OAuth 콜백 (공개)
  GET  /status    → 동기화 상태 JSON (공개)
  GET  /playlists → 사용자 플레이리스트 목록 (Access 보호)
  POST /select    → 미러 플레이리스트 선택 (Access 보호)
  POST /create    → 새 미러 플레이리스트 생성 (Access 보호)
  POST /sync      → 수동 동기화 (Access 보호)
  scheduled cron  → 매일 자동 동기화

참고:
  - Authorization Code Flow:  https://developer.spotify.com/documentation/web-api/tutorials/code-flow
  - 2026-02 마이그레이션 가이드: https://developer.spotify.com/documentation/web-api/tutorials/february-2026-migration-guide
  - 플레이리스트 트랙 엔드포인트는 `/items` 로 변경되었고, 응답에서도
    items[].item 으로 키가 바뀌었다.
  - 플레이리스트 생성은 `POST /me/playlists` 만 지원된다.
    (`POST /users/{user_id}/playlists` 는 제거됨.)
"""

import json
import secrets
from datetime import datetime, timezone
from base64 import b64encode
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse, parse_qs, urlencode

from js import console, fetch, Object
from pyodide.ffi import to_js as _to_js
from workers import WorkerEntrypoint, Response


# --- 상수 ------------------------------------------------------------------

SPOTIFY_AUTH_URL: str = "https://accounts.spotify.com/authorize"
SPOTIFY_TOKEN_URL: str = "https://accounts.spotify.com/api/token"
SPOTIFY_API_BASE: str = "https://api.spotify.com/v1"

# 필요한 권한 (공식 문서 기준):
#   user-library-read            → GET /me/tracks (좋아요 곡 읽기)
#   playlist-read-private        → 비공개 플레이리스트 목록 조회
#   playlist-read-collaborative  → 협업 플레이리스트 목록 조회
#   playlist-modify-public       → 공개 플레이리스트 생성/수정
#   playlist-modify-private      → 비공개/협업 플레이리스트 수정
# 미러로 비공개·협업 플레이리스트를 선택해도 동작하도록 modify-private 도 요청한다.
SCOPES: List[str] = [
    "user-library-read",
    "playlist-read-private",
    "playlist-read-collaborative",
    "playlist-modify-public",
    "playlist-modify-private",
]
SCOPE_STR: str = " ".join(SCOPES)
REQUIRED_SCOPES: Set[str] = set(SCOPES)

# 한 번의 PUT/POST 에 보낼 수 있는 최대 트랙 수 (공식 문서: 100)
CHUNK_SIZE: int = 100

# KV 키
KV_ACCESS: str = "spotify:access_token"
KV_REFRESH: str = "spotify:refresh_token"
KV_USER: str = "spotify:user_id"
KV_PLAYLIST: str = "spotify:playlist_id"
KV_LAST_TIME: str = "spotify:last_sync_time"
KV_LAST_COUNT: str = "spotify:last_sync_count"
KV_SCOPE: str = "spotify:scope"

# OAuth state CSRF 토큰은 HttpOnly 쿠키로 보존한다.
# Cloudflare Workers KV 는 cross-edge 전파에 최대 60초 이상 걸릴 수 있어
# 짧은 수명의 nonce 에는 적합하지 않다.
#   https://developers.cloudflare.com/kv/concepts/how-kv-works/#consistency
OAUTH_STATE_COOKIE: str = "spotify_oauth_state"


# --- 유틸 ------------------------------------------------------------------

def to_js_obj(obj: Any) -> Any:
    """파이썬 dict 를 fetch/KV 옵션에 쓸 JS Object 로 변환."""
    return _to_js(obj, dict_converter=Object.fromEntries)


def origin_of(request) -> str:
    """요청 URL 에서 'scheme://host' 만 잘라낸다."""
    p = urlparse(request.url)
    return p.scheme + "://" + p.netloc


def json_response(obj: Any, status: int = 200) -> Response:
    return Response(
        json.dumps(obj),
        status=status,
        headers={"Content-Type": "application/json; charset=utf-8"},
    )


async def read_json(request) -> Dict[str, Any]:
    try:
        text: str = await request.text()
        return json.loads(text) if text else {}
    except (ValueError, TypeError) as exc:
        console.log("요청 본문 파싱 실패: " + str(exc))
        return {}


def read_cookie(request, name: str) -> Optional[str]:
    """요청의 Cookie 헤더에서 name 의 값을 꺼낸다 (없으면 None)."""
    raw = request.headers.get("Cookie") or request.headers.get("cookie") or ""
    for part in raw.split(";"):
        kv = part.strip().split("=", 1)
        if len(kv) == 2 and kv[0] == name:
            return kv[1]
    return None


# --- 저수준 HTTP -----------------------------------------------------------

async def http_request(
    method: str,
    url: str,
    *,
    headers: Optional[Dict[str, str]] = None,
    json_body: Optional[Any] = None,
    form_body: Optional[Dict[str, str]] = None,
) -> Tuple[int, Dict[str, Any], Dict[str, str]]:
    """HTTP 요청 → (status, parsed_body, response_headers).

    JSON 본문이면 application/json, form_body 면 x-www-form-urlencoded 로 보낸다.
    응답 본문이 JSON 이 아니면 {"_raw": <truncated text>} 로 감싸 반환한다.
    """
    h: Dict[str, str] = dict(headers or {})
    body: Optional[str] = None
    if json_body is not None:
        h.setdefault("Content-Type", "application/json")
        body = json.dumps(json_body)
    elif form_body is not None:
        h.setdefault("Content-Type", "application/x-www-form-urlencoded")
        body = urlencode(form_body)

    opts: Dict[str, Any] = {"method": method, "headers": h}
    if body is not None:
        opts["body"] = body

    resp = await fetch(url, to_js_obj(opts))
    status: int = int(resp.status)
    text: str = str(await resp.text() or "")

    parsed: Dict[str, Any] = {}
    if text.strip():
        try:
            obj = json.loads(text)
            parsed = obj if isinstance(obj, dict) else {"_raw": obj}
        except (ValueError, TypeError):
            parsed = {"_raw": text[:1000]}

    res_headers: Dict[str, str] = {}
    try:
        ra = resp.headers.get("Retry-After")
        if ra:
            res_headers["Retry-After"] = str(ra)
    except Exception:  # noqa: BLE001
        pass

    return status, parsed, res_headers


# --- OAuth & 토큰 ----------------------------------------------------------

def _basic_auth(env) -> str:
    """Spotify 토큰 엔드포인트용 Basic 인증 헤더 값.

    공식 문서: Authorization: Basic base64(client_id:client_secret)
    """
    raw = (str(env.SPOTIFY_CLIENT_ID) + ":" + str(env.SPOTIFY_CLIENT_SECRET)).encode("utf-8")
    return "Basic " + b64encode(raw).decode("ascii")


async def exchange_code(env, code: str, redirect_uri: str) -> Tuple[int, Dict[str, Any]]:
    """authorization_code 를 access/refresh 토큰으로 교환."""
    status, data, _ = await http_request(
        "POST", SPOTIFY_TOKEN_URL,
        headers={"Authorization": _basic_auth(env)},
        form_body={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
        },
    )
    return status, data


async def do_refresh(env) -> Optional[str]:
    """refresh_token 으로 새 access_token 발급하고 KV 에 저장."""
    rt: Optional[str] = await env.SPOTIFY_KV.get(KV_REFRESH)
    if not rt:
        console.log("refresh_token 없음 — /login 필요")
        return None

    status, data, _ = await http_request(
        "POST", SPOTIFY_TOKEN_URL,
        headers={"Authorization": _basic_auth(env)},
        form_body={"grant_type": "refresh_token", "refresh_token": rt},
    )
    if status != 200 or not data.get("access_token"):
        console.log("토큰 갱신 실패 status=" + str(status) + " body=" + json.dumps(data)[:300])
        return None

    access: str = data["access_token"]
    # 공식 문서상 expires_in 은 보통 3600. 안전하게 60초 여유.
    ttl: int = max(60, int(data.get("expires_in", 3600)) - 60)
    await env.SPOTIFY_KV.put(KV_ACCESS, access, to_js_obj({"expirationTtl": ttl}))

    new_rt: Optional[str] = data.get("refresh_token")
    if new_rt and new_rt != rt:
        await env.SPOTIFY_KV.put(KV_REFRESH, new_rt)

    if data.get("scope"):
        await env.SPOTIFY_KV.put(KV_SCOPE, data["scope"])

    return access


async def get_token(env) -> Optional[str]:
    """유효한 access_token 반환 (없으면 refresh 시도)."""
    access: Optional[str] = await env.SPOTIFY_KV.get(KV_ACCESS)
    if access:
        return access
    return await do_refresh(env)


# --- Spotify API 호출 ------------------------------------------------------

async def spotify_api(
    env,
    method: str,
    path: str,
    *,
    json_body: Optional[Any] = None,
) -> Tuple[int, Dict[str, Any]]:
    """access_token 을 붙여 Spotify API 호출. 401 이면 한 번 자동 재시도."""
    token: Optional[str] = await get_token(env)
    if not token:
        return 401, {"error": {"message": "no access token"}}

    url: str = path if path.startswith("http") else SPOTIFY_API_BASE + path
    headers: Dict[str, str] = {"Authorization": "Bearer " + token}
    status, data, _ = await http_request(method, url, headers=headers, json_body=json_body)

    if status == 401:
        token = await do_refresh(env)
        if not token:
            return 401, {"error": {"message": "refresh failed"}}
        headers["Authorization"] = "Bearer " + token
        status, data, _ = await http_request(method, url, headers=headers, json_body=json_body)

    return status, data


# --- Spotify 도메인 함수 ---------------------------------------------------

async def fetch_liked_uris(env) -> List[str]:
    """GET /me/tracks 를 페이지네이션으로 돌며 모든 좋아요 곡의 URI 수집.

    공식 문서: limit 최대 50, 응답의 next 가 null 이 될 때까지 따라간다.
    """
    uris: List[str] = []
    url: Optional[str] = "/me/tracks?limit=50"
    while url:
        status, data = await spotify_api(env, "GET", url)
        if status != 200:
            console.log("좋아요 곡 조회 실패 status=" + str(status))
            break
        for item in data.get("items", []) or []:
            track = item.get("track") if isinstance(item, dict) else None
            if isinstance(track, dict) and track.get("uri"):
                uris.append(track["uri"])
        url = data.get("next")
    console.log("좋아요 곡 " + str(len(uris)) + "개 수집")
    return uris


async def fetch_playlist_uris(env, playlist_id: str) -> Set[str]:
    """대상 플레이리스트의 모든 트랙 URI 집합.

    2026 마이그레이션:
      - 엔드포인트: /playlists/{id}/items
      - 응답: items[].item (이전: items[].track) — 구버전 응답도 폴백
    """
    existing: Set[str] = set()
    url: Optional[str] = "/playlists/" + playlist_id + "/items?limit=100"
    while url:
        status, data = await spotify_api(env, "GET", url)
        if status != 200:
            console.log("플레이리스트 트랙 조회 실패 status=" + str(status))
            break
        for it in data.get("items", []) or []:
            entry = it.get("item") or it.get("track") if isinstance(it, dict) else None
            if isinstance(entry, dict) and entry.get("uri"):
                existing.add(entry["uri"])
        url = data.get("next")
    return existing


async def fetch_me(env) -> Optional[Dict[str, Any]]:
    """현재 토큰 주인의 프로필 (/me)."""
    status, data = await spotify_api(env, "GET", "/me")
    if status == 200 and isinstance(data, dict) and data.get("id"):
        return data
    console.log("/me 조회 실패 status=" + str(status))
    return None


async def get_user_id(env) -> Optional[str]:
    """현재 토큰의 user id (필요하면 /me 로 신선한 값을 받아 KV 갱신)."""
    me = await fetch_me(env)
    if me and me.get("id"):
        uid = me["id"]
        await env.SPOTIFY_KV.put(KV_USER, uid)
        return uid
    return await env.SPOTIFY_KV.get(KV_USER)


async def list_my_playlists(env) -> Dict[str, Any]:
    """GET /me/playlists 페이지네이션 (편집 가능 여부 표시)."""
    user_id: Optional[str] = await get_user_id(env)
    items: List[Dict[str, Any]] = []
    url: Optional[str] = "/me/playlists?limit=50"
    total: int = 0
    first: bool = True
    spotify_err: str = ""
    last_status: int = 200

    while url:
        status, data = await spotify_api(env, "GET", url)
        last_status = status
        if first:
            first = False
            try:
                total = int(data.get("total") or 0)
            except (ValueError, TypeError):
                total = 0
        if status != 200:
            err = data.get("error") if isinstance(data, dict) else None
            if isinstance(err, dict):
                spotify_err = str(err.get("message") or "")
            elif isinstance(err, str):
                spotify_err = err
            console.log("/me/playlists 실패 status=" + str(status) + " err=" + spotify_err)
            return {
                "status": status, "playlists": items, "total": total,
                "user_id": user_id, "spotify_error": spotify_err,
            }
        for pl in data.get("items", []) or []:
            owner = (pl.get("owner") or {}).get("id")
            tracks = pl.get("tracks") or pl.get("items") or {}
            items.append({
                "id": pl.get("id"),
                "name": pl.get("name"),
                "owner": owner,
                "editable": (owner == user_id) or bool(pl.get("collaborative")),
                "tracks": int(tracks.get("total") or 0),
            })
        url = data.get("next")

    return {
        "status": last_status, "playlists": items, "total": total,
        "user_id": user_id, "spotify_error": "",
    }


async def create_my_playlist(env, name: str) -> Tuple[Optional[str], int, str]:
    """공개 플레이리스트를 새로 만든다 (POST /me/playlists).

    2026 마이그레이션 이후 POST /users/{id}/playlists 는 제거되었다.
    """
    body: Dict[str, Any] = {
        "name": name,
        "public": True,
        "description": "좋아요 표시한 곡 자동 미러 (spotify-sync)",
    }
    status, data = await spotify_api(env, "POST", "/me/playlists", json_body=body)
    if status not in (200, 201) or not (isinstance(data, dict) and data.get("id")):
        err_msg = ""
        if isinstance(data.get("error"), dict):
            err_msg = str(data["error"].get("message") or "")
        msg = "플레이리스트 생성 실패 (status " + str(status) + ")"
        if err_msg:
            msg += ": " + err_msg
        if status == 403:
            msg += " — playlist-modify-public scope 부족이거나 앱이 Development Mode 인지 확인하세요."
        console.log(msg)
        return None, status, msg
    return data["id"], status, ""


async def replace_playlist(env, playlist_id: str, uris: List[str]) -> bool:
    """플레이리스트 전체를 uris 로 교체 (최초 동기화).

    PUT /playlists/{id}/items 로 첫 100개를 보내 기존 트랙을 덮어쓰고
    (uris=[] 도 허용), 나머지는 POST 로 100개씩 이어 붙인다.
    """
    head = uris[:CHUNK_SIZE]
    status, data = await spotify_api(
        env, "PUT", "/playlists/" + playlist_id + "/items",
        json_body={"uris": head},
    )
    if status not in (200, 201):
        console.log("전체 교체 PUT 실패 status=" + str(status) + " body=" + json.dumps(data)[:300])
        return False

    for i in range(CHUNK_SIZE, len(uris), CHUNK_SIZE):
        chunk = uris[i:i + CHUNK_SIZE]
        status, data = await spotify_api(
            env, "POST", "/playlists/" + playlist_id + "/items",
            json_body={"uris": chunk},
        )
        if status not in (200, 201):
            console.log("전체 교체 POST 실패 status=" + str(status) + " body=" + json.dumps(data)[:300])
            return False
    return True


async def append_tracks(env, playlist_id: str, uris: List[str]) -> bool:
    """플레이리스트에 100개씩 청크로 트랙을 추가 (증분 동기화)."""
    for i in range(0, len(uris), CHUNK_SIZE):
        chunk = uris[i:i + CHUNK_SIZE]
        status, data = await spotify_api(
            env, "POST", "/playlists/" + playlist_id + "/items",
            json_body={"uris": chunk},
        )
        if status not in (200, 201):
            console.log("트랙 추가 실패 status=" + str(status) + " body=" + json.dumps(data)[:300])
            return False
    return True


async def remove_tracks(env, playlist_id: str, uris: List[str]) -> bool:
    """플레이리스트에서 100개씩 청크로 트랙을 제거 (증분 동기화)."""
    for i in range(0, len(uris), CHUNK_SIZE):
        chunk = uris[i:i + CHUNK_SIZE]
        body = {"tracks": [{"uri": uri} for uri in chunk]}
        status, data = await spotify_api(
            env, "DELETE", "/playlists/" + playlist_id + "/items",
            json_body=body,
        )
        if status not in (200, 201):
            console.log("트랙 제거 실패 status=" + str(status) + " body=" + json.dumps(data)[:300])
            return False
    return True


# --- 메인 동기화 -----------------------------------------------------------

async def sync_now(env) -> Dict[str, Any]:
    """좋아요 곡을 미러 플레이리스트에 동기화하고 결과 dict 반환."""
    playlist_id: Optional[str] = await env.SPOTIFY_KV.get(KV_PLAYLIST)
    if not playlist_id:
        return {"ok": False, "error": "미러 플레이리스트가 선택되지 않았습니다. 대시보드에서 먼저 선택하세요."}

    if not await get_token(env):
        return {"ok": False, "error": "Spotify 인증 정보가 없습니다. /login 으로 다시 연동하세요."}

    liked = await fetch_liked_uris(env)
    last_sync: Optional[str] = await env.SPOTIFY_KV.get(KV_LAST_TIME)

    if not last_sync:
        console.log("최초 동기화: 전체 교체")
        ok = await replace_playlist(env, playlist_id, liked)
        added = len(liked) if ok else 0
        removed = 0
    else:
        existing = await fetch_playlist_uris(env, playlist_id)
        new_uris = [u for u in liked if u not in existing]
        removed_uris = [u for u in existing if u not in liked]
        console.log("증분 동기화: 신규 " + str(len(new_uris)) + "개, 제거 " + str(len(removed_uris)) + "개")

        ok = True
        if removed_uris:
            ok_remove = await remove_tracks(env, playlist_id, removed_uris)
            ok = ok and ok_remove

        if new_uris:
            ok_add = await append_tracks(env, playlist_id, new_uris)
            ok = ok and ok_add

        added = len(new_uris) if ok else 0
        removed = len(removed_uris) if ok else 0

    if not ok:
        return {"ok": False, "error": "Spotify API 호출 실패"}

    now = datetime.now(timezone.utc).isoformat()
    await env.SPOTIFY_KV.put(KV_LAST_TIME, now)
    await env.SPOTIFY_KV.put(KV_LAST_COUNT, str(len(liked)))
    console.log("동기화 완료 time=" + now + " total=" + str(len(liked)) + " added=" + str(added) + " removed=" + str(removed))
    return {"ok": True, "last_sync": now, "track_count": len(liked), "added": added, "removed": removed}


# --- 라우트 핸들러 ---------------------------------------------------------

async def handle_login(request, env) -> Response:
    """Spotify Authorization Code Flow 시작.

    공식 문서: response_type=code, scope, redirect_uri, state (CSRF), show_dialog 옵션.
    state 는 HttpOnly·Secure·SameSite=Lax 쿠키로 보존해 콜백에서 검증한다.
    (KV 는 eventually consistent 라 cross-edge 콜백에서 못 읽을 수 있음.)
    """
    state = secrets.token_urlsafe(24)
    params = {
        "client_id": str(env.SPOTIFY_CLIENT_ID),
        "response_type": "code",
        "redirect_uri": origin_of(request) + "/callback",
        "scope": SCOPE_STR,
        "state": state,
        "show_dialog": "true",
    }
    cookie = (
        OAUTH_STATE_COOKIE + "=" + state
        + "; Path=/callback; Max-Age=600; HttpOnly; Secure; SameSite=Lax"
    )
    return Response(
        None,
        status=302,
        headers={
            "Location": SPOTIFY_AUTH_URL + "?" + urlencode(params),
            "Set-Cookie": cookie,
        },
    )


async def handle_callback(request, env) -> Response:
    """OAuth 콜백: code → 토큰 교환, KV 저장."""
    qs = parse_qs(urlparse(request.url).query)
    error = qs.get("error", [None])[0]
    code = qs.get("code", [None])[0]
    state = qs.get("state", [None])[0]

    if error:
        return Response("인증 거부: " + error, status=400)
    if not code:
        return Response("code 파라미터가 없습니다.", status=400)
    if not state:
        return Response("state 파라미터가 없습니다.", status=400)

    # CSRF 방어: 쿠키의 state 와 URL state 가 일치해야 한다.
    cookie_state = read_cookie(request, OAUTH_STATE_COOKIE)
    if not cookie_state or not secrets.compare_digest(cookie_state, state):
        return Response("state 가 유효하지 않거나 만료되었습니다.", status=400)

    redirect_uri = origin_of(request) + "/callback"
    status, data = await exchange_code(env, code, redirect_uri)
    if status != 200 or not data.get("access_token"):
        return Response(
            "토큰 발급 실패 (status " + str(status) + "): " + json.dumps(data)[:500],
            status=400,
        )

    access = data["access_token"]
    ttl = max(60, int(data.get("expires_in", 3600)) - 60)
    await env.SPOTIFY_KV.put(KV_ACCESS, access, to_js_obj({"expirationTtl": ttl}))
    if data.get("refresh_token"):
        await env.SPOTIFY_KV.put(KV_REFRESH, data["refresh_token"])
    if data.get("scope"):
        await env.SPOTIFY_KV.put(KV_SCOPE, data["scope"])

    # 사용자 id 도 즉시 저장
    _, me, _ = await http_request(
        "GET", SPOTIFY_API_BASE + "/me",
        headers={"Authorization": "Bearer " + access},
    )
    if isinstance(me, dict) and me.get("id"):
        await env.SPOTIFY_KV.put(KV_USER, me["id"])

    console.log("OAuth 연동 완료, scope=" + str(data.get("scope") or ""))
    # 쿠키 state 는 일회용 — 응답에서 즉시 만료시킨다.
    clear_cookie = (
        OAUTH_STATE_COOKIE + "=; Path=/callback; Max-Age=0; HttpOnly; Secure; SameSite=Lax"
    )
    return Response(
        None,
        status=302,
        headers={
            "Location": origin_of(request) + "/",
            "Set-Cookie": clear_cookie,
        },
    )


async def handle_status(env) -> Response:
    refresh: Optional[str] = await env.SPOTIFY_KV.get(KV_REFRESH)
    last_sync: str = (await env.SPOTIFY_KV.get(KV_LAST_TIME)) or ""
    count_s: Optional[str] = await env.SPOTIFY_KV.get(KV_LAST_COUNT)
    playlist_id: Optional[str] = await env.SPOTIFY_KV.get(KV_PLAYLIST)
    scope: str = (await env.SPOTIFY_KV.get(KV_SCOPE)) or ""
    return json_response({
        "synced": refresh is not None,
        "last_sync": last_sync,
        "track_count": int(count_s) if count_s else 0,
        "playlist_url": ("https://open.spotify.com/playlist/" + playlist_id) if playlist_id else "",
        "playlist_id": playlist_id or "",
        "scope": scope,
    })


async def handle_playlists(env) -> Response:
    res = await list_my_playlists(env)
    status = res["status"]
    if status != 200:
        granted: str = (await env.SPOTIFY_KV.get(KV_SCOPE)) or ""
        granted_set = set(granted.split()) if granted else set()
        missing = sorted(REQUIRED_SCOPES - granted_set)
        msg = "Spotify 플레이리스트 조회 실패 (status " + str(status) + ")"
        if res.get("spotify_error"):
            msg += ": " + res["spotify_error"]
        if missing:
            msg += " · 미부여 scope: " + ", ".join(missing) + ". /login 으로 다시 인증하세요."
        elif status == 403:
            msg += " · 앱이 Development Mode 일 가능성. Developer Dashboard 에서 본인 계정을 User Management 에 추가하세요."
        return json_response({
            "playlists": [], "total": res.get("total", 0),
            "user_id": res.get("user_id") or "",
            "error": msg, "missing_scope": missing,
            "granted_scope": granted,
            "spotify_error": res.get("spotify_error", ""),
        }, status=status)
    return json_response({
        "playlists": res["playlists"],
        "total": res.get("total", 0),
        "user_id": res.get("user_id") or "",
    })


async def handle_select(request, env) -> Response:
    body = await read_json(request)
    pid = body.get("playlist_id")
    if not pid:
        return json_response({"ok": False, "error": "playlist_id 가 필요합니다."}, status=400)
    await env.SPOTIFY_KV.put(KV_PLAYLIST, pid)
    # 다른 플레이리스트로 바꾸면 다음 동기화를 전체 교체로 다시 시작
    await env.SPOTIFY_KV.delete(KV_LAST_TIME)
    await env.SPOTIFY_KV.delete(KV_LAST_COUNT)
    console.log("미러 선택: " + pid)
    return json_response({"ok": True, "playlist_id": pid})


async def handle_create(request, env) -> Response:
    body = await read_json(request)
    name = (body.get("name") or "").strip() or "좋아요 미러"
    new_id, status, err = await create_my_playlist(env, name)
    if not new_id:
        http_status = status if status in (401, 403, 404) else 400
        return json_response({"ok": False, "error": err or "생성 실패"}, status=http_status)
    await env.SPOTIFY_KV.put(KV_PLAYLIST, new_id)
    await env.SPOTIFY_KV.delete(KV_LAST_TIME)
    await env.SPOTIFY_KV.delete(KV_LAST_COUNT)
    console.log("미러 생성: " + new_id)
    return json_response({"ok": True, "playlist_id": new_id})


async def handle_sync(env) -> Response:
    result = await sync_now(env)
    return json_response(result, status=200 if result.get("ok") else 400)


# --- 라우터 ----------------------------------------------------------------

async def handle_fetch(request, env) -> Response:
    path = urlparse(request.url).path
    method = request.method

    # 공개 경로
    if path == "/login":
        return await handle_login(request, env)
    if path == "/callback":
        return await handle_callback(request, env)
    if path == "/status":
        return await handle_status(env)
    if path == "/favicon.ico":
        return Response(None, status=204)

    # Cloudflare Access JWT 헤더 존재 확인 (서명 검증은 Access 가 처리)
    if not request.headers.get("CF-Access-Jwt-Assertion"):
        return Response(
            "403 Forbidden: Cloudflare Access 인증이 필요한 경로입니다.",
            status=403,
        )

    if path == "/" and method == "GET":
        return Response(DASHBOARD_HTML, headers={"Content-Type": "text/html; charset=utf-8"})
    if path == "/playlists" and method == "GET":
        return await handle_playlists(env)
    if path == "/select" and method == "POST":
        return await handle_select(request, env)
    if path == "/create" and method == "POST":
        return await handle_create(request, env)
    if path == "/sync" and method == "POST":
        return await handle_sync(env)

    return Response("404 Not Found", status=404)


# --- 대시보드 --------------------------------------------------------------

DASHBOARD_HTML: str = """<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Spotify Sync</title>
  <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.8/dist/css/bootstrap.min.css" rel="stylesheet" integrity="sha384-sRIl4kxILFvY47J16cr9ZwB07vP4J8+LH7qKQnuqkuIAvNWLzeN8tE5YBujZqJLB" crossorigin="anonymous">
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
          <a id="playlistLink" href="#" target="_blank" rel="noopener" style="display:none;"></a>
          <span id="noPlaylist" class="text-muted">선택되지 않음</span>
        </p>
      </div>
    </div>

    <div class="card mb-3" id="playlistCard" style="display:none;">
      <div class="card-body">
        <h5 class="card-title">미러 플레이리스트</h5>
        <div class="d-flex gap-2 align-items-center flex-wrap">
          <select id="playlistSelect" class="form-select" style="max-width: 360px;"></select>
          <button class="btn btn-outline-primary" onclick="savePlaylist()">선택 저장</button>
          <button class="btn btn-outline-success" onclick="createPlaylist()">새로 만들기</button>
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
      const s = await (await fetch('/status')).json();
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
      if (s.synced) loadPlaylists(s.playlist_id);
    }

    async function loadPlaylists(current) {
      const sel = document.getElementById('playlistSelect');
      const hint = document.getElementById('playlistHint');
      hint.textContent = '';
      let res;
      try { res = await fetch('/playlists'); }
      catch (e) { hint.textContent = '요청 오류: ' + e; return; }
      let data = {};
      try { data = await res.json(); } catch (e) {}
      if (!res.ok) {
        sel.innerHTML = '';
        hint.textContent = data.error || ('목록 로드 실패 (HTTP ' + res.status + ')');
        return;
      }
      const lists = data.playlists || [];
      sel.innerHTML = '';
      if (lists.length === 0) {
        hint.textContent = '플레이리스트가 없습니다 (user_id=' + (data.user_id || '') + ').';
        return;
      }
      lists.forEach(p => {
        const opt = document.createElement('option');
        opt.value = p.id;
        opt.textContent = p.name + ' (' + p.tracks + '곡)' + (p.editable ? '' : ' [편집불가]');
        if (!p.editable) opt.disabled = true;
        if (p.id === current) opt.selected = true;
        sel.appendChild(opt);
      });
    }

    async function postJson(url, payload) {
      const res = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      let body = {};
      try { body = await res.json(); } catch (e) {}
      return { ok: res.ok, status: res.status, body };
    }

    async function savePlaylist() {
      const pid = document.getElementById('playlistSelect').value;
      if (!pid) return;
      const r = await postJson('/select', { playlist_id: pid });
      alert(r.ok ? '저장되었습니다.' : ('저장 실패: ' + (r.body.error || ('HTTP ' + r.status))));
      loadStatus();
    }

    async function createPlaylist() {
      const name = prompt('새 플레이리스트 이름', '좋아요 미러');
      if (!name) return;
      const r = await postJson('/create', { name });
      alert(r.ok ? '생성되었습니다.' : ('생성 실패: ' + (r.body.error || ('HTTP ' + r.status))));
      loadStatus();
    }

    async function doSync() {
      const btn = document.getElementById('syncBtn');
      btn.disabled = true;
      btn.textContent = '동기화 중...';
      try {
        const r = await (await fetch('/sync', { method: 'POST' })).json();
        if (r.ok) alert('완료: 총 ' + r.track_count + '곡 (신규 ' + r.added + '곡, 제거 ' + (r.removed || 0) + '곡)');
        else alert('실패: ' + (r.error || '알 수 없는 오류'));
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
    """fetch (HTTP) 와 scheduled (cron) 진입점."""

    async def fetch(self, request) -> Response:
        try:
            return await handle_fetch(request, self.env)
        except Exception as exc:  # noqa: BLE001
            console.log("처리 중 오류: " + str(exc))
            return Response("500 Internal Server Error: " + str(exc), status=500)

    async def scheduled(self, controller, env=None, ctx=None) -> None:
        console.log("cron 동기화 시작")
        result = await sync_now(env or self.env)
        console.log("cron 결과: " + json.dumps(result))
