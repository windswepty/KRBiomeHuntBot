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

**cog 로드 검사만으로는 부족합니다.** `NameError` 는 그 줄이 **실행될 때만** 납니다.
클래스를 실수로 지워도 파일은 멀쩡히 로드되고, 유저가 메뉴를 눌러야 터집니다
(실제로 `BackupChannelView` 를 지운 채 배포해 "적시에 응답하지 않았어요"가 났습니다).
UI 를 건드렸으면 **콜백을 실제로 호출**해 보세요.

```bash
# Select 콜백을 가짜 Interaction 으로 전부 실행 (예외가 나면 그 자리에서 잡힙니다)
PYTHONIOENCODING=utf-8 MONGO_URI= python -c "
import asyncio, cogs.backup as B
class R:
    def __init__(s): s.n=0
    async def send_message(s,*a,**k): s.n+=1
    async def defer(s,*a,**k): s.n+=1
class I:
    def __init__(s): s.guild=G(); s.response=R(); s.followup=R(); s.user=type('U',(),{'id':1})()
class G:
    id=1; me=object()
    def get_channel(s,c): return None
async def m():
    for v in ('snap','list','file','restore','chan','sendnow','status'):
        sel=B.BackupMenu(); sel._values=[v]; await sel.callback(I())
        print('OK', v)
asyncio.run(m())"
```

## 아키텍처

`main.py`가 `cogs/` 폴더를 스캔해 확장을 자동 로드하고 `bot.tree.sync()`로 슬래시 명령어를 동기화합니다. **cog 파일을 추가하면 자동 등록**되므로 `main.py` 수정이 불필요합니다.

### 서로 독립된 두 시스템

이 저장소에는 **의도적으로 분리된 두 개의 하위 시스템**이 있습니다. 이 경계를 깨뜨리지 마세요.

| | 포인트 이코노미 | 인증 / 링크 보호 |
| :--- | :--- | :--- |
| Cog | `checkin` `chat_reward` `shop` `gacha` `report` `blacklist` `logs` `backup` `quest` | `verify` `link_guard` `roblox_link` |
| 데이터 계층 | `utils/database.py` | `utils/verify_db.py` |
| 보조 모듈 | `utils/health.py`(공용) | `utils/link_tokens.py` `utils/oauth_gate.py` `utils/roblox_oauth.py` |
| Mongo 컬렉션 | `users` `shop_items` `gacha_settings` `activity_logs` `blacklist` `channel_boosts` `point_snapshots` `backup_settings` `checkin_settings` `quest_settings` `quest_progress` `quest_state` `quest_events` | `verify_settings` `linkguard_settings` `protected_links` `link_clicks` `link_watermarks` `guarded_keys` `link_usage` `log_references` `roblox_links` |

`verify.py` / `link_guard.py` / `roblox_link.py` / `verify_db.py`는 **`utils/database.py`를 절대 import 하지 않습니다.** 포인트를 지급·차감하지 않고 `/로그` 활동 기록에도 남기지 않습니다. 두 시스템이 공유하는 것은 **인증 역할 ID 하나뿐**입니다.

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

**중계 리다이렉트는 결국 도입했습니다** (`relay_links`, 켜짐). 원래 이 문서는 도입하지
말라고 했었습니다 — Render 무료 플랜이 유휴 시 잠들어 첫 요청에 30~50초가 걸리기 때문입니다.
**그 위험은 지금도 그대로입니다.** 버튼에 로블록스 주소를 직접 넣으면 우클릭 한 번으로
링크가 새는데, 그걸 막으려면 중계가 필수라 감수하기로 한 것입니다.

속도 문제가 재발하면 `/링크보호설정`에서 `relay_links`를 끄면 즉시 예전 방식으로 돌아갑니다.
**끄는 경로를 제거하지 마세요.** 이게 유일한 탈출구입니다.

## 무인 운영 (관리자가 오래 자리를 비웁니다)

이 봇은 **사람이 손을 못 대는 기간**을 전제로 운영됩니다. 아래 장치들은
"사람이 알아채고 고친다"를 기대하지 않도록 만든 것입니다. 지우지 마세요.

- **`/health` 는 디스코드 연결 상태를 반영해야 합니다.** 무조건 200 을 돌려주면
  Flask 만 살고 봇이 죽은 **좀비 상태**를 외부 감시가 정상으로 봅니다. 죽는 것보다
  나쁩니다 — 죽으면 호스팅이 재시작이라도 하니까요.
- **`watchdog` 은 회복 불능일 때 `os._exit(1)` 로 끝냅니다.** 좀비는 프로세스가
  멀쩡해서 호스팅이 감지하지 못하므로 봇이 직접 죽어야 합니다.
  `sys.exit()` 은 예외라 이벤트 루프나 남의 `except` 에 먹힙니다.
  **종료 코드가 0 이면 호스팅이 '정상 종료'로 보고 다시 안 띄울 수 있습니다.**
- **`MONGO_URI` 가 있는데 접속 실패면 기동을 중단합니다.** 폴백으로 조용히 넘어가면
  휘발성 파일에 포인트가 쌓였다 사라지는데, 무인 운영에서는 며칠치가 날아가도
  아무도 모릅니다. **틀린 데이터로 도는 것보다 잠깐 멈추는 편이 낫습니다.**
- **상태 임베드는 메시지 하나를 계속 수정합니다.** 매번 새로 보내면 도배가 되어
  정작 중요한 신호가 묻힙니다. 메시지 ID 를 DB 에 두는 방식은 링크 보호와 같습니다.
- **경고는 진짜일 때만 떠야 합니다.** 늘 빨간 표시는 없는 것보다 나쁩니다 —
  익숙해져서 진짜 경고를 놓칩니다. 그래서 크래시 루프 판정에 커밋 해시를 씁니다
  (재배포는 커밋이 매번 바뀌므로 걸리지 않습니다).
- **분모 없는 개수는 쓸모가 없습니다.** `기능 11개` 로는 원래 12개였는지 알 수
  없습니다. `cogs/` 파일 수를 분모로 써서 `11/11` 로 보여줍니다.

## 백업은 세 곳에 있어야 합니다

| 위치 | 담기는 것 | 막아주는 사고 |
| :--- | :--- | :--- |
| Atlas 스냅샷 (`point_snapshots`) | 포인트만 | 로직 사고 · 실수 |
| 디스코드 채널 (매일 05:00) | **DB 전체** | 클러스터 · 계정 · PC 사고 |
| PC 파일 (`tools/full_backup.py`) | DB 전체 | 디스코드 사고 |

스냅샷은 **같은 클러스터 안**이라 클러스터·계정 사고를 못 막습니다. 설정(상점 구성 ·
인증 조건 · 링크 보호)은 아예 담기지도 않습니다. 채널 업로드가 그 구멍을 메웁니다.

**세 경로의 파일 형식은 같아야 합니다.** 채널에서 받은 파일을 그대로
`tools/full_backup.py restore` 에 넣을 수 있어야 백업으로서 의미가 있습니다.

## 퀘스트 · 연속 출석 (포인트 수급처)

채팅·출석만으로는 포인트가 잘 모이지 않아 넓힌 수급처입니다.
**수치는 전부 설정값**입니다. 운영진이 `/출석설정` `/퀘스트설정` 으로 직접
조정하므로 코드에 박으면 조정할 때마다 개발자를 불러야 합니다.

- **퀘스트 목록은 저장하지 않습니다.** 무작위 씨앗이 `길드:유저:날짜` 라
  같은 사람은 하루 동안 같은 목록을 보고, 사람마다 다른 조합을 받습니다.
  저장하면 1,245명분 문서를 매일 써야 하는데 계산이 더 쌉니다.
  **씨앗에서 user_id 를 빼지 마세요** — 전원이 같은 퀘스트를 받게 됩니다.
- **풀 크기가 바뀌면 뽑기 결과가 통째로 달라집니다.** 랜덤 채널 후보를 모두
  비우면 `channel_chat` 이 풀에서 빠지면서 그날 목록이 바뀝니다. 개수
  (`daily_count`) 변경은 안전합니다(같은 씨앗의 `random.sample` 은 앞부분이
  유지됩니다).
- **채널 퀘스트 진행도는 채널별로 나뉩니다** (`ch_<id>` / `wch_<id>`).
  사람마다 배정된 채널이 다르므로 하나의 카운터로는 셀 수 없습니다.
  필드 이름에 **점(`.`)을 쓰지 마세요** — Mongo 는 중첩 문서를 만들고 JSON
  폴백은 납작한 키를 만들어 서로 어긋납니다.
- **구버전 `channel_id`(단일)를 지우지 마세요.** `channel_ids` 가 비어 있으면
  그걸 한 개짜리 목록으로 읽어, 설정을 다시 하지 않아도 동작합니다.
- **진행도는 전부 `$inc`** 입니다. 읽고 계산해서 쓰면 채팅이 몰릴 때 집계가
  뭉개집니다. 보상 수령도 '아직 안 받음'을 필터에 넣어 서버 측에서 판정합니다.
- **선착순 등수는 `$inc` 의 반환값을 그대로** 씁니다. 읽고 더하면 동시에
  출석한 사람들이 같은 등수를 받습니다.
- **연속 출석 보상은 두 번의 `$inc`** 로 나갑니다. 첫 연산이 `last_checkin` 을
  오늘로 바꿔 동시 요청을 막으므로, 두 번째(보너스)가 중복될 수 없습니다.
- **주간 키는 일요일 시작**입니다. `isocalendar()` 는 월요일 시작이라 하루를
  더해 밀어냅니다. 이걸 되돌리면 주간 퀘스트가 월요일에 초기화됩니다.

### ⚠️ 모든 메시지 경로에 들어가는 코드는 캐시하세요

`get_quest_settings()` 는 `on_message` 에서 불립니다. 캐시 없이 두면
**채팅 한 줄마다 DB 를 읽습니다.** 블랙리스트를 캐시하는 것과 같은 이유입니다.
그리고 기능이 **꺼져 있으면 아예 쓰지 않도록** 순서를 잡으세요 —
꺼진 기능이 모든 메시지마다 DB 에 쓰고 있던 적이 있습니다.

- **이벤트 진행도는 기간이 깁니다.** `created` 를 `$setOnInsert` 로 고정하면
  TTL 이 이벤트 도중에 문서를 지웁니다. 이벤트 기간 키(`E…`)일 때는 활동할
  때마다 `created` 를 갱신해 **마지막 활동 기준**으로 만료되게 합니다.
- **역할 보상은 포인트 지급 뒤에 줍니다.** 역할 부여가 실패해도 되돌리지 않고
  무엇이 안 됐는지 알립니다. 되돌리려다 포인트까지 꼬이는 것이 더 나쁩니다.

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
- **설정 화면의 드롭다운 설명은 View 를 만들 때 값으로 굳습니다.** 값을 바꾼 뒤
  메시지를 다시 그리지 않으면 켰는데도 '꺼짐'으로 보입니다. 컴포넌트·모달
  상호작용은 `interaction.response.edit_message()` 로 자기를 띄운 메시지를
  고칠 수 있으니, 저장 후 패널을 다시 그리세요.
- **패널 임베드는 명령어와 갱신이 같은 함수를 써야 합니다.** 따로 두면 한쪽만
  고쳐져 다시 어긋납니다.
- **상수는 쓰는 곳보다 먼저 정의하세요.** `QUEST_PROGRESS_TTL_DAYS` 를 파일
  아래쪽에 뒀더니 인덱스 생성 시점에 `NameError` 가 나고, 그게 `try/except` 에
  삼켜져 **TTL 인덱스가 조용히 안 만들어질** 뻔했습니다.
- **`ObjectId` · `datetime` 을 문자열로 뭉개면 복원이 조용히 망가집니다.**
  TTL 인덱스는 BSON Date 를 요구하므로, 타입이 달라지면 데이터가 영영 안 지워집니다.
  백업은 `{"$oid":...}` `{"$date":...}` 형태로 타입을 보존하세요.
- **봇이 켜진 채로 복원하면 `delete_many` 와 `insert_many` 사이에 봇이 끼어듭니다.**
  `_id` 가 겹쳐 E11000 이 나고 **복원이 중간에서 통째로 멈춥니다**
  (users 1,236건 중 353건만 들어간 채 멈춘 적이 있습니다).
  `bulk_write(ReplaceOne, upsert=True)` 를 쓰면 몇 번을 돌려도 같은 결과가 됩니다.
- **`--wipe` 는 지우고 넣는 게 아니라 넣고 지우는 순서여야 합니다.**
  반대로 하면 중간에 실패했을 때 데이터가 빈 채로 남습니다.
- **`backups/` 에는 형식이 다른 파일이 섞입니다.** `/백업설정` 이 내려주는
  `users-*.json` 은 포인트 전용입니다. 이름순으로 최신 파일을 고르면 그걸 집습니다.
  수정 시각으로 고르고, 형식을 검사하세요.
- **디스코드 API는 기본 User-Agent를 막습니다.** `urllib` 의 `Python-urllib/3.x` 로
  토큰 교환을 시도하면 Cloudflare 가 **403 / error code 1010** 으로 끊습니다.
  OAuth 가 통째로 실패하는데 원인은 로그에 안 보입니다. UA 를 명시하세요.
- **`urllib` 예외 계층에 구멍이 있습니다.** 응답을 **읽는 중**에 나는
  `TimeoutError` `RemoteDisconnected` `IncompleteRead` `SSLError` 는 `URLError` 로
  감싸지지 않습니다. `except URLError` 만 두면 Flask 500 이 뜨고 진단 로그도 안 남습니다.
  `OSError` + `http.client.HTTPException` 까지 잡으세요.
- **Flask 는 GET 라우트에 HEAD 를 자동으로 붙입니다.** 디스코드·카카오톡의 링크
  미리보기가 HEAD 를 날려 **1회용 토큰을 먼저 소비해 버렸습니다.**
  유저가 누르면 이미 죽어 있습니다. HEAD 는 204 로 막으세요.
- **OAuth 왕복 예산(600초) > 토큰 수명(90초).** 신원 확인에 **성공하고도**
  "만료되었습니다"가 떴습니다. 게이트로 내보내기 직전에 `extend()` 로 늘립니다.
- **만료 판정은 정리(purge)보다 먼저 조회해야 합니다.** 순서를 바꾸면 방금 만료된
  토큰이 이미 지워져 "만료됨" 대신 "유효하지 않음"으로 안내됩니다.
- **`roblox_created_at` 은 유닉스 타임스탬프(int)** 입니다. ISO 문자열이 아닙니다.
  계정 나이 조건을 붙일 때 문자열로 파싱하면 안 됩니다.
- **로블록스 앱은 심사 전 10명 제한**입니다. 승인 전에 `require_roblox` 를 켜면
  11번째 유저부터 인증을 못 받습니다. OAuth 미설정 시 켜지지 않도록 막아 두었지만,
  **심사 여부까지는 코드가 알 수 없습니다.**
- **연동 명단 검색어는 반드시 `re.escape` 하세요.** 안 하면 `.*` 한 글자로
  명단이 통째로 덤프되고, ReDoS 도 열립니다.
- **레거시 유저 문서 191건** (`_id`가 user_id인 옛 형식)이 아직 있습니다.
  마이그레이션 경로를 지우지 마세요. 그리고 `replace_one`으로 옮기면 그 사이
  적립된 포인트를 덮어씁니다 — 반드시 `$setOnInsert`를 쓰세요.

## 배포

Render 웹 서비스 (`render.yaml`). `keep_alive.py`가 Flask를 데몬 스레드로 띄워 `PORT` 환경 변수에 바인딩하며, 이게 없으면 502가 납니다.

환경 변수는 7개입니다. `DISCORD_TOKEN` `MONGO_URI` 는 필수, 나머지는 해당 기능을
켤 때 필요합니다. 자세한 목록과 "없으면 생기는 일"은 README 를 보세요.

```
DISCORD_TOKEN  MONGO_URI                              (필수)
DISCORD_CLIENT_ID  DISCORD_CLIENT_SECRET  SECRET_KEY  (중계 신원 게이트)
ROBLOX_CLIENT_ID   ROBLOX_CLIENT_SECRET               (로블록스 연동)
```

### ⚠️ MongoDB 클러스터는 2026-08-29 에 옮겼습니다

**옛 클러스터(`cluster0.o4wytak`)를 보고 작업하지 마세요.** 거기엔 옛 데이터가
그대로 남아 있어 얼핏 정상으로 보이지만, **봇은 더 이상 그곳을 쓰지 않습니다.**

| | 옛 | 현재 |
| :--- | :--- | :--- |
| 클러스터 | `cluster0.o4wytak` | `krbiomehuntbot.jklg6pg` |
| 계정 | `windswept` / `atlasAdmin` | `krbiomehunt_bot` / `readWrite` |
| 범위 | 봇 3개 DB 전부 + 클러스터 삭제 | **이 DB 하나만** |

옮긴 이유: 한 계정으로 봇 3개가 물려 있어, 연결 문자열이 한 번 새면 셋 다
삭제될 수 있었습니다. 이전은 `tools/migrate_cluster.py` 로 했고 3,127건이
문서 단위로 일치함을 확인했습니다.

계정을 또 바꿀 일이 생기면 **바꾸기 전에** `tools/check_db_user.py` 를 돌리세요.
Render 에 반영한 뒤에 문제를 알면 늦습니다.

**`MONGO_URI`가 빠지면 봇은 죽지 않고 임시 파일로 폴백합니다.** Render 파일시스템은
휘발성이라 재시작마다 포인트가 사라지는데도 정상 동작처럼 보입니다. 기동 로그의
`Point storage:` 줄로 반드시 확인하세요.

### 포인트 데이터 보호

포인트는 이 봇의 존재 이유입니다.

- 매일 새벽 5시(KST) `point_snapshots`에 자동 스냅샷, 최근 30개 보관
- `/백업설정`으로 수동 스냅샷 · 목록 · 복원 · **파일 내려받기**
- 복원 시 직전 상태를 자동 백업하므로 복원 자체도 되돌릴 수 있습니다
- 스냅샷은 **같은 클러스터 안**이라 클러스터·계정 사고는 막지 못합니다.
  파일 내려받기로 서버 밖에도 주기적으로 보관하세요 (M0에는 자동 백업이 없습니다).
- 봇 DB 계정은 `readWrite`만 줍니다. `atlasAdmin`이면 `MONGO_URI` 유출 시
  전 데이터가 삭제될 수 있습니다. (2026-08-29 에 전용 계정으로 교체 완료)
- **매일 05:00 에 전체 백업이 디스코드 채널로 올라갑니다.** 스냅샷과 달리 설정까지
  담기고, 클러스터 밖에 남습니다. `/백업설정` → `📡 전체 백업 채널 설정`.
- 봇 상태는 `🔔 봇 상태 알림 채널` 의 임베드 하나가 1분마다 갱신하며 보여줍니다.
  **그 시각이 멈춰 있으면 봇이 멈춘 것입니다.**

디스코드 개발자 포털에서 **MESSAGE CONTENT INTENT**와 **SERVER MEMBERS INTENT**가 켜져 있어야 부팅됩니다.
