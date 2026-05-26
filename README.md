# spotify-sync

Spotify **"좋아요 표시한 곡"** 목록을 공개 플레이리스트로 **매일 자동 동기화**하는 Cloudflare Workers (Python) 앱입니다.

- 도메인: `spotify.vialinks.xyz`
- 매일 `00:00 UTC` cron 으로 자동 동기화
- 최초 1회는 플레이리스트 전체 교체, 이후에는 **플레이리스트에 없는 곡만 새로 추가**
- 대시보드 / 수동 동기화는 **Cloudflare Access** 로 보호

---

## 파일 구조

```
spotify-sync/
├── wrangler.toml      # Worker 설정 (cron, KV, 도메인, 환경변수, observability)
├── requirements.txt   # Python 의존성 (표준 라이브러리만 사용)
├── README.md
└── src/
    └── worker.py      # 모든 로직 (라우팅, OAuth, 동기화, 대시보드)
```

## 엔드포인트

| 경로 | 메서드 | 보호 | 설명 |
|------|--------|------|------|
| `/` | GET | Access | 대시보드 HTML |
| `/login` | GET | 공개 | Spotify OAuth 시작 |
| `/callback` | GET | 공개 | OAuth 콜백 (토큰 발급·저장) |
| `/status` | GET | 공개 | 상태 JSON `{synced, last_sync, track_count, playlist_url}` |
| `/playlists` | GET | Access | 내 플레이리스트 목록 JSON |
| `/select` | POST | Access | 미러 플레이리스트 선택 `{playlist_id}` |
| `/create` | POST | Access | 새 미러 플레이리스트 생성 `{name}` |
| `/sync` | POST | Access | 수동 동기화 트리거 |
| (cron) | scheduled | - | 매일 자동 동기화 |

> Access 보호 경로는 `CF-Access-Jwt-Assertion` 헤더가 없으면 `403` 을 반환합니다.
> 실제 JWT 서명 검증은 앞단의 Cloudflare Access 가 처리합니다.

---

## 1. Spotify 개발자 앱 생성

1. <https://developer.spotify.com/dashboard> 에 로그인합니다.
2. **Create app** 클릭 후 아래와 같이 입력합니다.
   - **App name**: `spotify-sync` (자유롭게)
   - **App description**: `좋아요 표시한 곡을 공개 플레이리스트로 매일 동기화` (자유롭게)
   - **Redirect URIs**: 아래 두 개를 모두 등록하는 것을 권장합니다.
     - `https://spotify.vialinks.xyz/callback` (커스텀 도메인)
     - `https://spotify-sync.<계정명>.workers.dev/callback` (workers.dev 기본 도메인)
   - **Which API/SDKs are you planning to use?**: `Web API` 체크
3. 저장 후 앱 **Settings** 에서 **Client ID** 와 **Client secret** 을 확인합니다.
   - Client ID 는 이미 `wrangler.toml` 의 `SPOTIFY_CLIENT_ID` 에 들어 있습니다
     (`d87978f9aec64e5d9f45a7dc06ce98ca`). 다른 앱을 쓴다면 이 값을 교체하세요.
   - Client secret 은 비밀값이므로 Cloudflare secret 으로 주입합니다(아래 2번 참고).

> Redirect URI 는 등록한 값과 **정확히** 일치해야 합니다. 이 앱은 `/login` 에 접속한
> 도메인을 기준으로 콜백 URI 를 자동 구성하므로, 접속할 도메인의 `/callback` 을 반드시 등록하세요.

---

## 2. Cloudflare 배포 (GitHub 연동)

1. 이 코드를 GitHub 레포지토리(`spotify-sync`)에 push 합니다.
2. Cloudflare 대시보드 → **Workers & Pages** → **Create application** → **Workers** 탭
   → **Continue with GitHub** 를 선택합니다.
3. 방금 만든 `spotify-sync` 레포지토리와 배포 브랜치를 선택합니다.
   - 빌드 설정은 기본값으로 두면 `wrangler.toml` 을 읽어 배포합니다.
   - Python Workers 는 `compatibility_flags = ["python_workers"]` 가 필요하며 이미 설정돼 있습니다.
4. 배포가 끝나면 애플리케이션 **Settings → 변수 및 암호(Variables and Secrets)** 로 이동합니다.
5. **암호(Secret)** 로 `SPOTIFY_CLIENT_SECRET` 을 추가하고 Spotify 의 Client secret 값을 입력한 뒤 저장합니다.
   - `SPOTIFY_CLIENT_ID` 는 `wrangler.toml` 의 `[vars]` 에 평문으로 들어 있어 별도 입력이 필요 없습니다.
6. **KV 네임스페이스** `SPOTIFY_KV` (id `226fd06df76548a2aa8477fba7c06691`) 와
   **커스텀 도메인** `spotify.vialinks.xyz` 도 `wrangler.toml` 로 자동 구성됩니다.
   대시보드 **Settings → Domains & Routes** 에서 연결 상태를 확인하세요.

> 로컬에서 배포하려면 [`pywrangler`](https://github.com/cloudflare/workers-py) 를 사용합니다.
> ```bash
> npx wrangler kv key list --binding SPOTIFY_KV   # KV 확인 (옵션)
> npx pywrangler deploy
> npx wrangler secret put SPOTIFY_CLIENT_SECRET    # secret 주입
> ```

---

## 3. 최초 실행 (OAuth 인증)

1. 브라우저에서 `https://spotify.vialinks.xyz/login` 에 접속합니다.
   - (`/login`, `/callback` 은 Access 보호 대상이 아니므로 바로 접근됩니다.)
2. Spotify 인증 화면에서 권한(`user-library-read`, `playlist-modify-public`,
   `playlist-modify-private`)을 허용합니다.
3. 콜백이 처리되며 `access_token`·`refresh_token`·`user_id` 가 KV 에 저장되고
   대시보드(`/`)로 리다이렉트됩니다.
4. 대시보드에서 **미러 플레이리스트**를 지정합니다.
   - 기존 플레이리스트를 골라 **선택 저장**, 또는 **새 플레이리스트 생성** 으로 새로 만듭니다.
5. **지금 동기화** 버튼으로 첫 동기화를 실행합니다.
   - 첫 실행은 전체 교체, 이후(수동·cron)는 신규 곡만 추가됩니다.

> 미러 플레이리스트를 바꾸면 마지막 동기화 기록이 초기화되어 다음 동기화가 전체 교체로 다시 시작됩니다.

---

## 4. Cloudflare Access 설정

대시보드(`/`)와 수동 동기화(`/sync`) 등 관리 경로를 본인만 쓸 수 있도록 Access 로 보호합니다.

1. Cloudflare **Zero Trust** 대시보드 → **Access → Applications** → **Add an application**
   → **Self-hosted** 를 선택합니다.
2. **Application Configuration**
   - **Application name**: `spotify-sync`
   - **Session Duration**: 원하는 값 (예: 24h)
   - **Public hostname**: `spotify.vialinks.xyz` (Path 는 비워 전체 경로 보호)
3. **Policies** 에서 본인만 허용하는 정책을 추가합니다.
   - 예: **Action** `Allow`, **Include** → `Emails` → 본인 이메일(`eoe253326@gmail.com`).
4. 저장하면 보호된 경로 접속 시 Access 로그인 후, Cloudflare 가
   `CF-Access-Jwt-Assertion` 헤더를 Worker 로 주입합니다. Worker 는 이 헤더의 존재 여부를 확인합니다.

> `/login`, `/callback`, `/status` 는 Spotify OAuth 와 상태 조회를 위해 공개로 두어야 합니다.
> Access Application 의 Path 를 전체(`/*`)로 잡아도, 이 경로들은 Worker 내부에서 보호 대상에서 제외됩니다.
> 다만 Access 가 앞단에서 막지 않도록, 필요하다면 `/login`·`/callback`·`/status` 에 대해
> **Bypass** 정책을 별도로 추가하거나 Path 를 `/`·`/sync` 등 관리 경로로 한정하세요.

---

## 동작 요약

- `fetch_liked_songs()` — `GET /me/tracks` 를 `limit=50` 으로 페이지네이션(while)하며 전체 URI 수집
- `get_token()` / `refresh_token()` — access_token(TTL 3600초) 관리 및 자동 갱신
- `sync_playlist()` — 최초엔 `PUT`(전체 교체) + `POST`(100개씩 청크), 이후엔 신규 곡만 `POST` 추가
- 동기화 후 `spotify:last_sync_time`(ISO8601), `spotify:last_sync_count` 를 KV 에 저장
- `[observability]` 활성화로 `console.log` 가 전부 수집됩니다.
