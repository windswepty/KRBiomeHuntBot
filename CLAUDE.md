# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 프로젝트 규칙 (필수)

`.agents/AGENTS.md`에 정의된 규칙이며 예외 없이 적용합니다.

1. **모든 디스코드 응답은 `ephemeral=True`** — 슬래시 명령어, View/Select/Button 상호작용 전부. 신규 코드에도 예외 없이 적용합니다.
2. **작업 완료 시 자동 배포** — 코드 변경이 끝나면 `git add` → `commit` → `push`를 실행합니다. Render가 GitHub push를 감지해 자동 재배포하므로 별도 배포 작업은 없습니다. **커밋 메시지는 한국어**로 작성합니다.

## 명령어

```bash
# 실행 (봇 + keep_alive Flask 서버)
python main.py

# 문법 검사
python -c "import ast; [ast.parse(open(f,encoding='utf-8').read()) for f in ['main.py','utils/verify_db.py','utils/database.py']]"

# Cog 로드 검증 (디스코드 연결 없이 — 명령어 등록/영구 View/리스너 확인)
PYTHONIOENCODING=utf-8 python -c "
import asyncio, os, discord
from discord.ext import commands
bot = commands.Bot(command_prefix='!', intents=discord.Intents.default())
async def m():
    for f in sorted(os.listdir('cogs')):
        if f.endswith('.py') and not f.startswith('__'): await bot.load_extension(f'cogs.{f[:-3]}')
    print(len(bot.tree.get_commands()), 'commands /', len(bot.persistent_views), 'views')
    await bot.close()
asyncio.run(m())"
```

정식 테스트 스위트는 없습니다. 로직 검증은 **`MONGO_URI=` 를 비워 JSON 폴백으로 강제**하고 임시 디렉터리에서 실행하세요. 실 DB를 건드리지 않습니다.

```bash
PYTHONIOENCODING=utf-8 MONGO_URI= python -c "
import sys, os, tempfile; sys.path.insert(0, os.getcwd()); os.chdir(tempfile.mkdtemp())
import asyncio, utils.verify_db as vdb
# ...
"
```

**Windows 주의**: 콘솔이 cp949라 한글/이모지 출력 시 `UnicodeEncodeError`가 납니다. 항상 `PYTHONIOENCODING=utf-8`을 붙이세요.

**로컬에서 봇을 띄우지 마세요.** Render에 인스턴스가 상시 가동 중이라, 로컬 실행 시 같은 봇이 두 번 응답하고 임베드를 중복 삭제하려다 충돌합니다.

## 아키텍처

`main.py`가 `cogs/` 폴더를 스캔해 확장을 자동 로드하고 `bot.tree.sync()`로 슬래시 명령어를 동기화합니다. **cog 파일을 추가하면 자동 등록**되므로 `main.py` 수정이 불필요합니다.

### 서로 독립된 두 시스템

이 저장소에는 **의도적으로 분리된 두 개의 하위 시스템**이 있습니다. 이 경계를 깨뜨리지 마세요.

| | 포인트 이코노미 | 인증 / 링크 보호 |
| :--- | :--- | :--- |
| Cog | `checkin` `chat_reward` `shop` `gacha` `report` `blacklist` `logs` | `verify` `link_guard` |
| 데이터 계층 | `utils/database.py` | `utils/verify_db.py` |
| Mongo 컬렉션 | `users` `shop_items` `gacha_settings` `activity_logs` … | `verify_settings` `linkguard_settings` `protected_links` `link_clicks` `link_watermarks` `guarded_keys` `link_usage` `log_references` |

`verify.py` / `link_guard.py` / `verify_db.py`는 **`utils/database.py`를 절대 import 하지 않습니다.** 포인트를 지급·차감하지 않고 `/로그` 활동 기록에도 남기지 않습니다. 두 시스템이 공유하는 것은 **인증 역할 ID 하나뿐**입니다.

### 데이터 계층 패턴

두 DB 모듈 모두 동일한 형태입니다. 새 함수를 추가할 때 이 패턴을 따르세요.

```python
if some_col is not None:
    try:
        ...  # MongoDB 경로
        return
    except Exception as e:
        print(f"MongoDB ... error: {e}, using local file.", flush=True)
async with _some_lock:
    ...  # 로컬 JSON 폴백
```

- 모든 데이터는 **길드 단위로 격리**됩니다 (`_id = guild_id` 또는 `guild_id:user_id`).
- MongoDB 연결 실패 시 조용히 `data/*.json`으로 폴백해 봇이 죽지 않습니다.
- `data/`는 `.gitignore`에 있습니다 (유저 ID·워터마크 포함).

### 링크 보호 흐름 (`cogs/link_guard.py`)

```
웹훅이 임베드 게시 (webhook_id 있는 메시지만 대상)
  → 감시 대상 채널인가? (category_ids 우선, channel_ids 병행)
  → 링크 수집: 임베드 본문 · 메시지 본문 · 링크 버튼(컴포넌트)
  → roblox_only 필터 → 로블록스 링크가 없으면 원본 그대로 두고 종료
  → 원본 삭제 → 링크 제거한 임베드 + [개인서버 입장하기] 버튼으로 재게시
  → protected_links 에 저장 (키 = 재게시된 메시지 ID)
  → guarded_keys 색인 등록 (유출 감지용) + 로그 채널에 집계 임베드
```

버튼 클릭 시:

```
전원(enabled) 검사 → 오염 링크 검사 → 인증 역할 검사 → 워터마크 부착 → 링크 버튼 전달
                                                          ↓ 응답 후 백그라운드
                                              사용 기록 · 로그 임베드 집계 갱신
```

### 영구 View 규칙

봇 재시작 후에도 버튼이 동작해야 합니다. **custom_id는 반드시 고정값**을 쓰고 `setup()`에서 `bot.add_view()`로 등록하세요.

링크별로 다른 데이터가 필요할 때 custom_id에 값을 넣지 마세요. 대신 **버튼이 붙어있는 메시지 ID를 키로 DB를 조회**합니다 (`protected_links`, `log_references`가 이 방식). 이게 이 코드베이스의 핵심 설계입니다.

### 속도 제약

개인서버 링크는 수백 명이 동시에 클릭합니다. 버튼 콜백 경로에서는:

- 설정·링크 조회를 **메모리 캐시**로 처리합니다 (`_verify_cache` `_linkguard_cache` `_plink_cache`). 캐시는 `save_*()`가 즉시 갱신하므로 어긋나지 않습니다.
- 역할 검사는 이미 메모리에 있는 `interaction.user.roles`를 씁니다.
- **기록 저장은 응답을 보낸 뒤** `asyncio.create_task`로 처리합니다.
- 로그 임베드 갱신은 10초에 한 번으로 제한합니다 (`LOG_REFRESH_INTERVAL`).

**중계 리다이렉트를 도입하지 마세요.** Render 무료 플랜은 유휴 시 잠들어 첫 요청에 30~50초가 걸립니다.

## 이미 밟은 지뢰

- **매크로 홍보 링크 오인** — 매크로 임베드에는 제작자 사이트 링크가 섞여 있습니다. `roblox_only=True`(기본값)로 로블록스 링크만 보호하고, 나머지는 원문 그대로 둡니다. 이걸 끄면 버튼이 엉뚱한 주소로 연결됩니다.
- **매크로마다 링크 위치가 다름** — 평문 URL(maxstellar) / 마크다운 마스킹 링크(Coteab) / **링크 버튼 컴포넌트**(MultiScope). 세 곳을 모두 훑어야 합니다.
- **비공개 카테고리** — 봇이 카테고리를 못 보면 선택 목록에도 안 뜨고 메시지도 안 옵니다. 그래서 이름·ID로 추가하는 경로를 따로 두었습니다.
- **전원 스위치 의미론** — `/인증시스템` 정지 시 **역할 검사보다 전원 검사를 먼저** 해야 합니다. 그러지 않으면 정지 상태인데 유저가 링크를 못 받습니다.
- **`on_message` 리스너 2개 공존** — `ChatReward`는 `author.bot`으로, `LinkGuard`는 `webhook_id`로 걸러 대상이 배타적입니다.
- **임베드 이미지/썸네일 URL은 건드리지 마세요.** 보호 대상이 아니며 제거하면 임베드 모양이 깨집니다.

## 배포

Render 웹 서비스 (`render.yaml`). `keep_alive.py`가 Flask를 데몬 스레드로 띄워 `PORT` 환경 변수에 바인딩하며, 이게 없으면 502가 납니다.

환경 변수: `DISCORD_TOKEN`, `MONGO_URI`.

디스코드 개발자 포털에서 **MESSAGE CONTENT INTENT**와 **SERVER MEMBERS INTENT**가 켜져 있어야 부팅됩니다.
