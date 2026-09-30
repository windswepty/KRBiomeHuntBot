"""
경고 · 밴 (모더레이션) 전용 데이터 계층.

포인트(utils/database.py)와도, 인증·링크 보호(utils/verify_db.py)와도
**독립**입니다. 둘 다 import 하지 않습니다. 경고를 받았다고 포인트가
깎이거나, 링크 보호 기록에 섞이는 일이 없어야 합니다.

  mod_settings  길드별 설정 + 케이스 번호 카운터 (`case_seq`)
  mod_cases     처벌 기록 하나하나 (_id = "길드:케이스번호")
  mod_users     유저별 유효 경고 수 (_id = "길드:유저")
  channel_locks 채널 잠금 (_id = 길드) — 자동 잠금 설정 · 수동 잠금 · 잠그기
                전의 원래 권한 (/잠금 · /채널잠금설정)

**처벌 기록에는 TTL 을 걸지 않습니다.** 몇 달 전 경고가 사라지면 누적
처벌이 무의미해지고, 이의 제기 때 근거를 댈 수 없습니다.
"""

import os
import re
import json
import asyncio
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

load_dotenv()

DATA_DIR = "data"
MOD_SETTINGS_FILE = os.path.join(DATA_DIR, "mod_settings.json")
MOD_CASES_FILE = os.path.join(DATA_DIR, "mod_cases.json")
MOD_USERS_FILE = os.path.join(DATA_DIR, "mod_users.json")
LOCK_FILE = os.path.join(DATA_DIR, "channel_locks.json")
MONGO_URI = os.getenv("MONGO_URI")

KST = timezone(timedelta(hours=9))

_settings_lock = asyncio.Lock()
_cases_lock = asyncio.Lock()
_lockdoc_lock = asyncio.Lock()

# pymongo 의 ReturnDocument.AFTER. 연결 블록 안에서만 정의하면 폴백일 때
# NameError 가 나므로 밖에 둡니다.
_AFTER = None

mod_settings_col = None
mod_cases_col = None
mod_users_col = None
channel_lock_col = None

if MONGO_URI:
    try:
        from pymongo import MongoClient, ReturnDocument as _ReturnDocument
        _client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
        _client.admin.command('ping')
        _mdb = _client["krbiomehuntbot"]
        mod_settings_col = _mdb["mod_settings"]
        mod_cases_col = _mdb["mod_cases"]
        mod_users_col = _mdb["mod_users"]
        channel_lock_col = _mdb["channel_locks"]
        _AFTER = _ReturnDocument.AFTER
        try:
            # 유저 검색이 이 순서로 찾습니다 (없으면 풀스캔)
            mod_cases_col.create_index(
                [("guild_id", 1), ("user_id", 1), ("case", -1)],
                name="modcase_by_user")
        except Exception as e:
            print(f"MongoDB (mod) index setup warning: {e}", flush=True)
        print("Connected to MongoDB successfully for moderation storage!", flush=True)
    except Exception as e:
        print(f"MongoDB (mod) connection warning: {e}. Falling back to local JSON storage.",
              flush=True)
        mod_settings_col = None
        mod_cases_col = None
        mod_users_col = None
        channel_lock_col = None


async def _db(fn, *args, **kwargs):
    """동기 드라이버(pymongo) 호출을 워커 스레드에서 실행합니다.

    그냥 부르면 그동안 이벤트 루프 전체가 멈춥니다 (CLAUDE.md).
    """
    return await asyncio.to_thread(fn, *args, **kwargs)


def _read_json(path: str) -> dict:
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        # 조용히 비우면 경고 기록이 통째로 사라진 것처럼 보입니다
        print(f"[ModDB] {path} 읽기 실패: {type(e).__name__}: {e}", flush=True)
    return {}


def _write_json(path: str, payload: dict) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def _now_kst_str() -> str:
    return datetime.now(KST).strftime("%Y-%m-%d %H:%M")


# ── 설정 ──────────────────────────────────────────────────────────────

# 자동 처벌에 쓸 수 있는 것. 타임아웃은 디스코드 상한이 28일입니다.
AUTO_ACTIONS = ("timeout", "kick", "ban")
TIMEOUT_MAX_MINUTES = 28 * 24 * 60

DEFAULT_MOD_SETTINGS = {
    # 처벌 기록을 올릴 채널. 없으면 기록은 DB 에만 남습니다.
    "log_channel_id": None,
    # 밴 직전에 사유와 서포트 서버 링크를 DM 으로 보냅니다.
    # **밴 뒤에는 보낼 수 없습니다** — 서버를 같이 쓰지 않으면 DM 이 막힙니다.
    "ban_dm": True,
    "warn_dm": True,
    # 이의 제기용 서포트 서버 초대 링크. DM 의 링크 버튼이 됩니다.
    "support_link": "",
    # DM 에 덧붙일 문구 (예: "이의 제기는 서포트 서버에서 받습니다")
    "ban_dm_message": "",
    # 밴할 때 그 유저의 최근 며칠 치 메시지를 지울지 (0~7). **7일이 디스코드 상한**입니다
    # — 그보다 오래된 것은 밴 API 로 지울 수 없습니다(Dyno 도 같음).
    # /밴 의 `메시지삭제` 를 적으면 그 값이 우선합니다.
    "ban_delete_days": 7,
    # 경고 누적 자동 처벌 [{"count": 3, "action": "timeout", "minutes": 60}, ...]
    "auto_rules": [],
}

# !warn · !ban · !warnings 의 접두사. **셋 다 늘 받습니다.** 다른 봇이 `!` 를
# 써서 겹치면 운영진이 `ㅂ!` · `q!` 로 치면 됩니다 — 설정을 바꿀 필요가 없습니다.
# `?` 는 넣지 않습니다: Dyno 가 아직 서버에 있고 `?warn` 에 답합니다.
# `Q!` 는 휴대폰 키보드가 첫 글자를 대문자로 바꾸기 때문입니다.
# `ㅃ!` 은 한글 자판에서 Shift+q 입니다. 뒤의 명령어 이름을 한글 자판으로 친
# 경우(`ㅂ!ㅈㅁ구`)는 cogs/moderation.py 의 hangul_to_keys 가 되돌립니다.
PREFIXES = ("!", "ㅂ!", "ㅃ!", "q!", "Q!")
DEFAULT_PREFIX = "!"


def prefix_for(bot, message):
    """`commands.Bot(command_prefix=...)` 에 넘기는 함수 (main.py · 검증이 같이 씁니다).

    **모든 채팅마다** 불리므로 DB 를 읽지 않습니다.
    """
    return list(PREFIXES)


async def get_mod_settings(guild_id: int) -> dict:
    key = str(guild_id)
    doc = None
    if mod_settings_col is not None:
        try:
            doc = await _db(mod_settings_col.find_one, {"_id": key})
        except Exception as e:
            print(f"MongoDB get_mod_settings error: {e}, using local file.", flush=True)
            doc = None
            async with _settings_lock:
                doc = _read_json(MOD_SETTINGS_FILE).get(key)
    else:
        async with _settings_lock:
            doc = _read_json(MOD_SETTINGS_FILE).get(key)
    out = dict(DEFAULT_MOD_SETTINGS)
    out["auto_rules"] = []
    for k, v in (doc or {}).items():
        if k in DEFAULT_MOD_SETTINGS:
            out[k] = v
    return out


async def save_mod_settings(guild_id: int, changes: dict) -> None:
    """**바뀐 키만** `$set` 합니다.

    통째로 덮으면 그 사이 늘어난 `case_seq`(케이스 번호)가 되돌아가
    번호가 겹칩니다 (뽑기 재고가 되살아났던 것과 같은 사고입니다).
    """
    changes = {k: v for k, v in changes.items() if k in DEFAULT_MOD_SETTINGS}
    if not changes:
        return
    key = str(guild_id)
    if mod_settings_col is not None:
        try:
            await _db(mod_settings_col.update_one, {"_id": key},
                      {"$set": changes}, upsert=True)
            return
        except Exception as e:
            print(f"MongoDB save_mod_settings error: {e}, using local file.", flush=True)
    async with _settings_lock:
        all_rows = _read_json(MOD_SETTINGS_FILE)
        row = all_rows.get(key) or {}
        row.update(changes)
        all_rows[key] = row
        _write_json(MOD_SETTINGS_FILE, all_rows)


async def _next_case(guild_id: int) -> int:
    """길드별 케이스 번호. **`$inc` 의 반환값**을 씁니다.

    읽고 더하면 동시에 처리한 두 관리자가 같은 번호를 받습니다.
    """
    key = str(guild_id)
    if mod_settings_col is not None:
        try:
            doc = await _db(mod_settings_col.find_one_and_update,
                            {"_id": key}, {"$inc": {"case_seq": 1}},
                            upsert=True, return_document=_AFTER)
            return int(doc["case_seq"])
        except Exception as e:
            print(f"MongoDB _next_case error: {e}, using local file.", flush=True)
    async with _settings_lock:
        all_rows = _read_json(MOD_SETTINGS_FILE)
        row = all_rows.get(key) or {}
        row["case_seq"] = int(row.get("case_seq", 0)) + 1
        all_rows[key] = row
        _write_json(MOD_SETTINGS_FILE, all_rows)
        return row["case_seq"]


# ── 처벌 기록 ─────────────────────────────────────────────────────────

# kind: warn · unwarn · ban · unban · timeout · kick
async def add_case(guild_id: int, kind: str, user_id: int, user_name: str,
                   mod_id: int, mod_name: str, reason: str,
                   auto: bool = False, extra: dict | None = None) -> dict:
    """처벌 하나를 기록하고 기록된 문서를 돌려줍니다 (케이스 번호 포함)."""
    case = await _next_case(guild_id)
    doc = {
        "_id": f"{guild_id}:{case}",
        "guild_id": str(guild_id),
        "case": case,
        "kind": kind,
        "user_id": str(user_id),
        "user_name": (user_name or "")[:100],
        "mod_id": str(mod_id),
        "mod_name": (mod_name or "")[:100],
        "reason": (reason or "")[:1000],
        "auto": bool(auto),
        "created": _now_kst_str(),
        "created_at": datetime.now(timezone.utc),
    }
    if kind == "warn":
        doc["active"] = True
    if extra:
        doc.update(extra)

    if mod_cases_col is not None:
        try:
            await _db(mod_cases_col.insert_one, dict(doc))
            return doc
        except Exception as e:
            print(f"MongoDB add_case error: {e}, using local file.", flush=True)
    async with _cases_lock:
        all_rows = _read_json(MOD_CASES_FILE)
        row = dict(doc)
        row["created_at"] = doc["created_at"].isoformat()
        all_rows[doc["_id"]] = row
        _write_json(MOD_CASES_FILE, all_rows)
    return doc


async def _bump_active(guild_id: int, user_id: int, delta: int) -> int:
    """유효 경고 수를 `$inc` 하고 **바뀐 뒤의 값**을 돌려줍니다.

    자동 처벌은 이 반환값으로 판정합니다. 기록을 세어서 판정하면 두 관리자가
    동시에 경고했을 때 둘 다 '4회' 를 보고 **'3회 규칙' 을 통째로 건너뜁니다.**
    """
    key = f"{guild_id}:{user_id}"
    if mod_users_col is not None:
        try:
            doc = await _db(mod_users_col.find_one_and_update,
                            {"_id": key},
                            {"$inc": {"active_warns": int(delta)},
                             "$setOnInsert": {"guild_id": str(guild_id),
                                              "user_id": str(user_id)}},
                            upsert=True, return_document=_AFTER)
            return max(0, int(doc.get("active_warns", 0)))
        except Exception as e:
            print(f"MongoDB _bump_active error: {e}, using local file.", flush=True)
    async with _cases_lock:
        all_rows = _read_json(MOD_USERS_FILE)
        row = all_rows.get(key) or {"guild_id": str(guild_id), "user_id": str(user_id)}
        row["active_warns"] = int(row.get("active_warns", 0)) + int(delta)
        all_rows[key] = row
        _write_json(MOD_USERS_FILE, all_rows)
        return max(0, row["active_warns"])


async def add_warning(guild_id: int, user_id: int, user_name: str,
                      mod_id: int, mod_name: str, reason: str) -> tuple[dict, int]:
    """경고를 기록하고 (기록, 누적 유효 경고 수) 를 돌려줍니다."""
    doc = await add_case(guild_id, "warn", user_id, user_name,
                         mod_id, mod_name, reason)
    count = await _bump_active(guild_id, user_id, 1)
    return doc, count


def _sort_cases(rows: list) -> list:
    return sorted(rows, key=lambda r: int(r.get("case", 0)), reverse=True)


async def get_user_cases(guild_id: int, user_id: int, limit: int = 200) -> list:
    """한 유저의 모든 기록 (최신 순)."""
    q = {"guild_id": str(guild_id), "user_id": str(user_id)}
    if mod_cases_col is not None:
        try:
            def _fetch():
                return list(mod_cases_col.find(q).sort("case", -1).limit(limit))
            return await _db(_fetch)
        except Exception as e:
            print(f"MongoDB get_user_cases error: {e}, using local file.", flush=True)
    async with _cases_lock:
        all_rows = _read_json(MOD_CASES_FILE)
    rows = [r for r in all_rows.values()
            if r.get("guild_id") == q["guild_id"] and r.get("user_id") == q["user_id"]]
    return _sort_cases(rows)[:limit]


async def get_active_warnings(guild_id: int, user_id: int) -> list:
    """유효 경고 전부 (최신 순).

    `get_user_cases` 의 상한(200건)을 거치지 않고 **직접** 찾습니다. 기록이
    많은 사람은 오래된 유효 경고가 상한 밖으로 밀려 '전체 해제' 에서 빠집니다.
    """
    q = {"guild_id": str(guild_id), "user_id": str(user_id), "kind": "warn", "active": True}
    if mod_cases_col is not None:
        try:
            def _fetch():
                return list(mod_cases_col.find(q).sort("case", -1))
            return await _db(_fetch)
        except Exception as e:
            print(f"MongoDB get_active_warnings error: {e}, using local file.", flush=True)
    async with _cases_lock:
        all_rows = _read_json(MOD_CASES_FILE)
    return _sort_cases([r for r in all_rows.values()
                        if r.get("guild_id") == q["guild_id"] and r.get("user_id") == q["user_id"]
                        and r.get("kind") == "warn" and r.get("active")])


async def find_users_by_name(guild_id: int, query: str, limit: int = 25) -> list:
    """기록에 남은 이름으로 유저를 찾습니다 → [(user_id, 마지막 이름), ...]

    서버를 나간(또는 밴된) 사람은 멤버 목록에 없으므로 기록의 이름으로
    찾아야 합니다. **검색어는 반드시 `re.escape`** — 안 하면 `.*` 한 글자로
    기록이 통째로 나옵니다 (연동 명단에서 실제로 막았던 구멍입니다).
    """
    query = (query or "").strip()
    if not query:
        return []
    pat = re.escape(query)
    rows = []
    if mod_cases_col is not None:
        try:
            def _fetch():
                return list(mod_cases_col.find(
                    {"guild_id": str(guild_id),
                     "user_name": {"$regex": pat, "$options": "i"}},
                    {"user_id": 1, "user_name": 1, "case": 1}
                ).sort("case", -1).limit(500))
            rows = await _db(_fetch)
        except Exception as e:
            print(f"MongoDB find_users_by_name error: {e}, using local file.", flush=True)
            rows = None
    else:
        rows = None
    if rows is None:
        async with _cases_lock:
            all_rows = _read_json(MOD_CASES_FILE)
        rx = re.compile(pat, re.I)
        rows = _sort_cases([r for r in all_rows.values()
                            if r.get("guild_id") == str(guild_id)
                            and rx.search(r.get("user_name", ""))])
    seen = {}
    for r in rows:
        uid = str(r.get("user_id"))
        if uid not in seen:
            seen[uid] = r.get("user_name", "")
        if len(seen) >= limit:
            break
    return list(seen.items())


async def remove_warnings(guild_id: int, user_id: int, cases: list | None,
                          by_id: int, by_name: str, reason: str) -> list:
    """유효 경고를 해제하고 **실제로 해제된 케이스 번호**를 돌려줍니다.

    지우지 않고 `active: False` 로 남깁니다 — 누가 왜 풀었는지까지가 기록입니다.
    '아직 유효함' 을 **필터에 넣어** 서버가 판정하므로, 두 관리자가 같은
    경고를 동시에 풀어도 한 번만 빠집니다.
    """
    active = await get_active_warnings(guild_id, user_id)
    targets = [int(r["case"]) for r in active
               if cases is None or int(r["case"]) in {int(c) for c in cases}]
    if not targets:
        return []
    stamp = {"active": False, "removed_by": str(by_id),
             "removed_by_name": (by_name or "")[:100],
             "removed_at": _now_kst_str(),
             "removed_reason": (reason or "")[:500]}
    done = []
    if mod_cases_col is not None:
        try:
            def _apply():
                out = []
                for c in targets:
                    res = mod_cases_col.update_one(
                        {"_id": f"{guild_id}:{c}", "active": True},
                        {"$set": stamp})
                    if res.modified_count:
                        out.append(c)
                return out
            done = await _db(_apply)
        except Exception as e:
            print(f"MongoDB remove_warnings error: {e}, using local file.", flush=True)
            done = None
    else:
        done = None
    if done is None:
        async with _cases_lock:
            all_rows = _read_json(MOD_CASES_FILE)
            done = []
            for c in targets:
                row = all_rows.get(f"{guild_id}:{c}")
                if row and row.get("active"):
                    row.update(stamp)
                    done.append(c)
            _write_json(MOD_CASES_FILE, all_rows)
    if done:
        await _bump_active(guild_id, user_id, -len(done))
    return done


# ── 채널 잠금 (/잠금 · /잠금해제 · /채널잠금설정) ─────────────────────
#
# 길드마다 문서 하나입니다.
#
#   설정      enabled · start · end · channel_ids · announce   (자동 잠금)
#   locked    {채널: {saved, at, notice_id}}  실제로 잠가 둔 채널과 **원래 권한**
#   manual    {채널: {until, by, reason}}     /잠금 으로 잠근 것 (until 없으면 무기한)
#   open_until {채널: 시각}                   자동 잠금 시간 중 /잠금해제 한 것
#
# ⚠️ **원래 권한(`saved`)은 권한을 바꾸기 전에 저장합니다.** 바꾼 뒤 저장이
# 실패하면 무엇으로 되돌릴지 몰라 그 채널이 **영영 잠깁니다.** 반대로 먼저
# 저장하고 바꾸기가 실패하면, 다음 주기에 같은 `saved` 로 다시 시도합니다.
# 재시작해도 7시에 풀리는 것도 이게 DB 에 있기 때문입니다.

DEFAULT_LOCK_SETTINGS = {
    "enabled": False,
    "start": "00:00",       # KST
    "end": "07:00",
    "channel_ids": [],
    "announce": True,       # 잠글 때 채널에 안내를 올리고, 풀 때 지웁니다
}
LOCK_STATE_ROOTS = ("locked", "manual", "open_until")
MAX_LOCK_CHANNELS = 25


async def get_lock_doc(guild_id: int) -> dict:
    """설정 + 상태를 한 번에. 없는 키는 기본값으로 채웁니다."""
    key = str(guild_id)
    doc = None
    if channel_lock_col is not None:
        try:
            doc = await _db(channel_lock_col.find_one, {"_id": key})
        except Exception as e:
            print(f"MongoDB get_lock_doc error: {e}, using local file.", flush=True)
            async with _lockdoc_lock:
                doc = _read_json(LOCK_FILE).get(key)
    else:
        async with _lockdoc_lock:
            doc = _read_json(LOCK_FILE).get(key)
    doc = doc or {}
    out = {k: doc.get(k, v) for k, v in DEFAULT_LOCK_SETTINGS.items()}
    out["channel_ids"] = [int(c) for c in (out.get("channel_ids") or [])]
    for root in LOCK_STATE_ROOTS:
        out[root] = dict(doc.get(root) or {})
    return out


async def save_lock_settings(guild_id: int, changes: dict) -> None:
    """**바뀐 설정 키만** `$set`. 상태(locked 등)는 여기로 건드리지 않습니다 —
    통째로 덮으면 잠가 둔 채널의 원래 권한이 날아가 풀 수 없게 됩니다."""
    changes = {k: v for k, v in changes.items() if k in DEFAULT_LOCK_SETTINGS}
    if not changes:
        return
    await _lock_update(guild_id, set_=changes)


async def set_lock_state(guild_id: int, root: str, channel_id: int, value) -> None:
    """상태 한 칸을 바꿉니다(`value` 가 None 이면 지움). 다른 채널은 건드리지 않습니다."""
    if root not in LOCK_STATE_ROOTS:
        raise ValueError(f"모르는 상태 칸: {root}")
    path = f"{root}.{int(channel_id)}"
    if value is None:
        await _lock_update(guild_id, unset=[path])
    else:
        await _lock_update(guild_id, set_={path: value})


async def _lock_update(guild_id: int, set_: dict | None = None,
                       unset: list | None = None) -> None:
    key = str(guild_id)
    if channel_lock_col is not None:
        try:
            update = {}
            if set_:
                update["$set"] = set_
            if unset:
                update["$unset"] = {p: "" for p in unset}
            await _db(channel_lock_col.update_one, {"_id": key}, update, upsert=True)
            return
        except Exception as e:
            print(f"MongoDB channel lock update error: {e}, using local file.", flush=True)
    async with _lockdoc_lock:
        all_rows = _read_json(LOCK_FILE)
        row = all_rows.get(key) or {}
        for path, value in (set_ or {}).items():
            cur = row
            parts = path.split(".")
            for p in parts[:-1]:
                cur = cur.setdefault(p, {})
            cur[parts[-1]] = value
        for path in unset or []:
            cur = row
            parts = path.split(".")
            for p in parts[:-1]:
                cur = cur.get(p) or {}
            cur.pop(parts[-1], None)
        all_rows[key] = row
        _write_json(LOCK_FILE, all_rows)
