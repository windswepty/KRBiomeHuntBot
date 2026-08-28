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

**동시성·원자성을 검증할 때는 컬렉션 객체를 가짜로 갈아끼우세요.**
`db.users_col = FakeCollection()` 처럼 바꾸면 실 DB를 건드리지 않고
`asyncio.gather`로 진짜 동시 실행을 시험할 수 있습니다. 가짜 드라이버는
**문서 단위 락**과 실제로 쓰는 연산자(`$inc` `$set` `$setOnInsert` `$gte` `$lte`
`$ne` `$in` `$elemMatch`, 위치 연산자 `items.$.stock`)를 구현해야 합니다.
연산자 하나가 빠지면 조건이 통째로 무시되어 **테스트가 거짓 통과**합니다
(`$lte` 누락으로 상한 검증이 실제와 다르게 나온 적이 있습니다).

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
| Cog | `checkin` `chat_reward` `shop` `gacha` `report` `blacklist` `logs` `backup` | `verify` `link_guard` |
| 데이터 계층 | `utils/database.py` | `utils/verify_db.py` |
| Mongo 컬렉션 | `users` `shop_items` `gacha_settings` `activity_logs` `blacklist` `channel_boosts` `point_snapshots` | `verify_settings` `linkguard_settings` `protected_links` `link_clicks` `link_watermarks` `guarded_keys` `link_usage` `log_references` |

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
- MongoDB 연결 실패 시 `data/*.json`으로 폴백하되, **눈에 띄는 경고 배너**를 출력합니다.
  포인트가 임시 파일에 쌓이면 재시작 때 사라지므로 조용히 넘어가면 안 됩니다.
- `data/` 와 `backups/` 는 `.gitignore`에 있습니다 (유저 ID·포인트·워터마크 포함).

#### ⚠️ pymongo 호출은 반드시 `_db()` 로 감쌉니다

pymongo는 **동기 라이브러리**라 async 함수에서 그대로 부르면 그동안 이벤트 루프
전체가 멈춥니다. 개인서버 자리 경쟁(정원 20명에 80명 몰림) 상황에서 이 정지가
누적되면 **디스코드 상호작용 3초 제한을 넘겨 "상호작용 실패"** 가 납니다.

```python
doc = await _db(users_col.find_one, {"_id": key})     # O
doc = users_col.find_one({"_id": key})                # X — 루프가 멈춥니다
```

커서를 도는 코드는 **스레드 안에서 리스트로 만들어** 반환하세요.

```python
def _fetch():
    return list(col.find(q).sort("_id", -1).limit(n))
rows = await _db(_fetch)
```

#### ⚠️ 그래서 읽기→수정→쓰기는 전부 원자 연산이어야 합니다

`_db()`가 **await 중단점을 만들기 때문에**, 예전처럼 "읽고 계산해서 덮어쓰기"를
하면 동시 요청끼리 서로를 지웁니다. 조건을 **필터에 넣어** 서버 측에서 판정하세요.

```python
# O — 잔액이 충분할 때만, 원자적으로 차감
doc = await _db(users_col.find_one_and_update,
                {"_id": key, "points": {"$gte": amount}},
                {"$inc": {"points": -amount}}, return_document=_AFTER)

# X — 두 명이 동시에 사면 포인트 한 번만 빠지고 상품은 두 개 나갑니다
doc = await _db(users_col.find_one, {"_id": key})
await _db(users_col.update_one, {"_id": key}, {"$set": {"points": doc["points"] - amount}})
```

포인트를 **절대값으로 덮어쓰는 코드를 새로 만들지 마세요.** 전부 `$inc` 입니다.
유일한 예외가 `restore_point_snapshot()`(백업 복원)이고, 이건 의도된 덮어쓰기입니다.

### 링크 보호 흐름 (`cogs/link_guard.py`)

```
웹훅이 임베드 게시 (webhook_id 있는 메시지만 대상)
  → 감시 대상 채널인가? (category_ids 우선, channel_ids 병행)
  → 링크 수집: 임베드 본문 · 메시지 본문 · 링크 버튼(컴포넌트)
  → roblox_only 필터 → 로블록스 링크가 없으면 원본 그대로 두고 종료
  → 원본 삭제 → 링크 제거한 임베드 + [개인서버 입장하기] 버튼으로 재게시
  → protected_links 에 저장 (키 = 재게시된 메시지 ID)
  → guarded_keys 색인 등록 (유출 감지용)
  ※ 로그 임베드는 여기서 만들지 않습니다. 아무도 안 누른 링크가 98%라
     보호할 때마다 올리면 로그 채널이 '0명 사용'으로 도배됩니다.
```

버튼 클릭 시:

```
전원(enabled) 검사 → 오염 링크 검사 → 인증 역할 검사 → 워터마크 부착 → 링크 버튼 전달
                                                          ↓ 응답 후 백그라운드
                              사용 기록 → ensure_usage_log()
                                            ├ 첫 클릭이면 로그 임베드 생성
                                            └ 아니면 사용 인원만 갱신(10초 디바운스)
```

`ensure_usage_log()`는 메시지별 `asyncio.Lock`으로 보호됩니다. 수십 명이 동시에
누를 때 로그 임베드가 그 수만큼 생기는 것을 막습니다.

### 영구 View 규칙

봇 재시작 후에도 버튼이 동작해야 합니다. **custom_id는 반드시 고정값**을 쓰고 `setup()`에서 `bot.add_view()`로 등록하세요.

링크별로 다른 데이터가 필요할 때 custom_id에 값을 넣지 마세요. 대신 **버튼이 붙어있는 메시지 ID를 키로 DB를 조회**합니다 (`protected_links`, `log_references`가 이 방식). 이게 이 코드베이스의 핵심 설계입니다.

### 속도 제약

개인서버 링크는 수백 명이 동시에 클릭합니다. 버튼 콜백 경로에서는:

- 설정·링크 조회를 **메모리 캐시**로 처리합니다 (`_verify_cache` `_linkguard_cache` `_plink_cache`). 캐시는 `save_*()`가 즉시 갱신하므로 어긋나지 않습니다.
- 역할 검사는 이미 메모리에 있는 `interaction.user.roles`를 씁니다.
- **기록 저장은 응답을 보낸 뒤** `asyncio.create_task`로 처리합니다.
- 그 백그라운드 작업의 DB 쓰기도 **`_db()`로 스레드에 넘깁니다.** 같은 루프에서
  돌면 응답을 보낸 의미가 없어집니다. (80명 동시 클릭 기준 2,970ms → 155ms)
- 로그 임베드 갱신은 10초 디바운스이되, 창이 닫힌 뒤 **최신 값으로 한 번 더**
  갱신합니다(트레일링). 그러지 않으면 몰림의 마지막 클릭들이 반영되지 않습니다.
- 캐시는 전부 `_TTLCache`(시간 만료 + 크기 상한)입니다. 무한히 늘어나면 메모리를
  잠식하고, DB TTL이 지운 값을 계속 돌려주게 됩니다.

**중계 리다이렉트를 도입하지 마세요.** Render 무료 플랜은 유휴 시 잠들어 첫 요청에 30~50초가 걸립니다.

## 이미 밟은 지뢰

- **매크로 홍보 링크 오인** — 매크로 임베드에는 제작자 사이트 링크가 섞여 있습니다. `roblox_only=True`(기본값)로 로블록스 링크만 보호하고, 나머지는 원문 그대로 둡니다. 이걸 끄면 버튼이 엉뚱한 주소로 연결됩니다.
- **매크로마다 링크 위치가 다름** — 평문 URL(maxstellar) / 마크다운 마스킹 링크(Coteab) / **링크 버튼 컴포넌트**(MultiScope). 세 곳을 모두 훑어야 합니다.
- **비공개 카테고리** — 봇이 카테고리를 못 보면 선택 목록에도 안 뜨고 메시지도 안 옵니다. 그래서 이름·ID로 추가하는 경로를 따로 두었습니다.
- **전원 스위치 의미론** — `/인증시스템` 정지 시 **역할 검사보다 전원 검사를 먼저** 해야 합니다. 그러지 않으면 정지 상태인데 유저가 링크를 못 받습니다.
- **`on_message` 리스너 2개 공존** — `ChatReward`는 `author.bot`으로, `LinkGuard`는 `webhook_id`로 걸러 대상이 배타적입니다.
- **임베드 이미지/썸네일 URL은 건드리지 마세요.** 보호 대상이 아니며 제거하면 임베드 모양이 깨집니다.
- **`to_thread` 도입이 경합을 만듭니다.** 예전 동기 코드는 중단점이 없어서 "우연히"
  원자적이었습니다. 스레드로 넘기는 순간 읽기→수정→쓰기가 전부 깨집니다.
  실제로 사용 인원 집계가 80명 → 1명으로 뭉개졌고, 캐시를 로드한 뒤 **다시 확인**해
  하나의 객체를 공유하도록 고쳐서 잡았습니다.
- **상한(trim)이 JSON 경로에만 걸려 있던 버그가 반복됐습니다.** `link_clicks`와
  `activity_logs` 둘 다 Mongo 경로에는 상한이 없어 영구 누적되고 있었습니다.
  새 로그성 컬렉션을 만들 때 **양쪽 경로 모두** 상한을 거세요.
- **동시 완료 순서는 값의 크기 순이 아닙니다.** 디바운스에 "마지막에 도착한 값"을
  쓰면 20명이 눌렀는데 13명으로 남습니다. 단조 증가하는 값은 **최댓값**을 쓰세요.
- **레거시 유저 문서 191건** (`_id`가 user_id인 옛 형식)이 아직 있습니다.
  마이그레이션 경로를 지우지 마세요. 그리고 `replace_one`으로 옮기면 그 사이
  적립된 포인트를 덮어씁니다 — 반드시 `$setOnInsert`를 쓰세요.

## 배포

Render 웹 서비스 (`render.yaml`). `keep_alive.py`가 Flask를 데몬 스레드로 띄워 `PORT` 환경 변수에 바인딩하며, 이게 없으면 502가 납니다.

환경 변수: `DISCORD_TOKEN`, `MONGO_URI`. 둘 다 `render.yaml`에 선언돼 있습니다.

**`MONGO_URI`가 빠지면 봇은 죽지 않고 임시 파일로 폴백합니다.** Render 파일시스템은
휘발성이라 재시작마다 포인트가 사라지는데도 정상 동작처럼 보입니다. 기동 로그의
`Point storage:` 줄로 반드시 확인하세요.

### 포인트 데이터 보호

포인트는 이 봇의 존재 이유입니다.

- 매일 새벽 5시(KST) `point_snapshots`에 자동 스냅샷, 최근 30개 보관
- `/포인트백업`으로 수동 스냅샷 · 목록 · 복원 · **파일 내려받기**
- 복원 시 직전 상태를 자동 백업하므로 복원 자체도 되돌릴 수 있습니다
- 스냅샷은 **같은 클러스터 안**이라 클러스터·계정 사고는 막지 못합니다.
  파일 내려받기로 서버 밖에도 주기적으로 보관하세요 (M0에는 자동 백업이 없습니다).
- 봇 DB 계정은 `readWrite`만 주는 것이 안전합니다. `atlasAdmin`이면 `MONGO_URI`
  유출 시 전 데이터가 삭제될 수 있습니다.

디스코드 개발자 포털에서 **MESSAGE CONTENT INTENT**와 **SERVER MEMBERS INTENT**가 켜져 있어야 부팅됩니다.
