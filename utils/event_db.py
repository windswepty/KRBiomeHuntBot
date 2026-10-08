"""
호박 깨기 이벤트 · 시즌 포인트 데이터 계층 (포인트 이코노미 소속).

  pumpkin_event    길드별 호박 (체력 · 회차 · 깨진 시각 · 포인트 배율 기한 · 이미지)
  pumpkin_hits     하루 한 번 기록 (_id = 길드:회차:KST날짜:유저) — **삽입이 곧 판정**
  season_settings  길드별 시즌 (이름 · 시작 · 끝 · 시즌 ID)
  season_points    시즌 포인트 (_id = 길드:시즌ID:유저)
  fusion_settings  역할 합성 레시피 (_id = 길드, recipes = {레시피키: {...}})

utils/database.py 가 이 모듈을 부릅니다(배율 · 시즌 적립). **반대로 부르지
마세요** — 순환 import 가 됩니다. 그래서 Mongo 연결도 따로 엽니다(mod_db 와 같음).

## 원자성

- 하루 한 번: '오늘 쳤는지' 를 읽고 판단하지 않고 **기록을 삽입**합니다. 같은
  `_id` 가 있으면 DuplicateKey — 연타 · 동시 클릭에도 한 번만 들어갑니다.
- 체력: `$inc` + 필터 `hp > 0`. 깨는 사람은 `hp` 가 0 을 **넘어간** 한 번뿐이고,
  보상 기한 설정도 `broken_at: None` 을 필터에 넣어 **한 번만** 걸립니다.
- 시즌 포인트 차감: 필터 `points >= 가격` (기본 포인트와 같은 방식).

## 시즌 포인트는 시즌 ID 로 묶습니다

초기화는 **새 시즌 ID 를 발급**하는 것입니다. 수천 명 문서를 0 으로 덮어쓰는
대신 ID 만 바꾸면 옛 것은 아무도 읽지 않습니다(정리는 뒤에서 지움).
기간이 끝나면 쓸 수 없습니다 — 요청이 '끝나면 그대로 소멸' 입니다.
"""

import os
import json
import time
import asyncio
import base64
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

load_dotenv()

DATA_DIR = "data"
PUMPKIN_FILE = os.path.join(DATA_DIR, "pumpkin_event.json")
PUMPKIN_HITS_FILE = os.path.join(DATA_DIR, "pumpkin_hits.json")
SEASON_FILE = os.path.join(DATA_DIR, "season_settings.json")
SEASON_PTS_FILE = os.path.join(DATA_DIR, "season_points.json")
FUSION_FILE = os.path.join(DATA_DIR, "fusion_settings.json")
MONGO_URI = os.getenv("MONGO_URI")

KST = timezone(timedelta(hours=9))

_pumpkin_lock = asyncio.Lock()
_season_lock = asyncio.Lock()
_fusion_lock = asyncio.Lock()

_AFTER = None
try:
    from pymongo.errors import DuplicateKeyError as _DuplicateKey
except ImportError:              # pragma: no cover — pymongo 없는 환경
    class _DuplicateKey(Exception):
        pass

pumpkin_col = None
pumpkin_hits_col = None
season_col = None
season_pts_col = None
fusion_col = None

if MONGO_URI:
    try:
        from pymongo import MongoClient, ReturnDocument as _ReturnDocument
        _client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
        _client.admin.command('ping')
        _edb = _client["krbiomehuntbot"]
        pumpkin_col = _edb["pumpkin_event"]
        pumpkin_hits_col = _edb["pumpkin_hits"]
        season_col = _edb["season_settings"]
        season_pts_col = _edb["season_points"]
        fusion_col = _edb["fusion_settings"]
        _AFTER = _ReturnDocument.AFTER
        try:
            # '오늘 몇 명 쳤나' 를 셉니다. 한 달 지난 기록은 쓸 일이 없습니다.
            pumpkin_hits_col.create_index([("guild_id", 1), ("round", 1), ("day", 1)],
                                          name="pumpkin_hits_day")
            pumpkin_hits_col.create_index("created", name="pumpkin_hits_ttl",
                                          expireAfterSeconds=30 * 24 * 3600)
            season_pts_col.create_index([("guild_id", 1), ("season_id", 1), ("points", -1)],
                                        name="season_rank")
        except Exception as e:
            print(f"MongoDB (event) index setup warning: {e}", flush=True)
        print("Connected to MongoDB successfully for event storage!", flush=True)
    except Exception as e:
        print(f"MongoDB (event) connection warning: {e}. Falling back to local JSON storage.",
              flush=True)
        pumpkin_col = pumpkin_hits_col = season_col = season_pts_col = fusion_col = None


async def _db(fn, *args, **kwargs):
    """동기 드라이버(pymongo) 호출을 워커 스레드에서 실행합니다 (CLAUDE.md)."""
    return await asyncio.to_thread(fn, *args, **kwargs)


def _read_json(path: str) -> dict:
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        print(f"[EventDB] {path} 읽기 실패: {type(e).__name__}: {e}", flush=True)
    return {}


def _write_json(path: str, payload: dict) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)


def kst_day(when: datetime | None = None) -> str:
    return (when or datetime.now(KST)).astimezone(KST).strftime("%Y-%m-%d")


# 모든 채팅 · 출석 · 퀘스트 경로에서 읽으므로 짧게 캐시합니다(CLAUDE.md:
# 메시지 경로의 설정은 캐시). 저장하면 즉시 비웁니다.
CACHE_SECONDS = 30
_cache: dict = {}


def _cached(kind: str, gid: int):
    hit = _cache.get((kind, int(gid)))
    if hit and time.monotonic() - hit[0] < CACHE_SECONDS:
        return hit[1]
    return None


def _remember(kind: str, gid: int, value) -> None:
    _cache[(kind, int(gid))] = (time.monotonic(), value)


def _forget(kind: str, gid: int) -> None:
    _cache.pop((kind, int(gid)), None)


# ── 호박 ──────────────────────────────────────────────────────────────

DEFAULT_PUMPKIN = {
    "enabled": False,
    "round": 1,
    "title": "호박 깨기 이벤트",
    "hp_max": 15_000_000,
    "hp": 15_000_000,
    "damage": 1000,
    "crit_damage": 2000,
    "crit_percent": 20,
    "min_member_hours": 24,       # 서버에 들어온 지 이만큼 지난 계정만 (부계정 방지)
    "boost_mult": 2.0,            # 깨지면 받는 포인트 배율
    "boost_days": 7,
    "channel_id": None,
    "message_id": None,
    "hits": 0,
    "broken_at": None,
    "broken_by": None,
    "boost_until": None,          # 유닉스 시각. 이 시각까지 포인트 배율 적용
}
PUMPKIN_KEYS = tuple(DEFAULT_PUMPKIN)
MAX_IMAGE_BYTES = 4 * 1024 * 1024


def _pumpkin_out(doc: dict | None) -> dict:
    out = dict(DEFAULT_PUMPKIN)
    for k, v in (doc or {}).items():
        if k in DEFAULT_PUMPKIN:
            out[k] = v
    out["has_image"] = bool((doc or {}).get("has_image"))
    out["image_name"] = (doc or {}).get("image_name") or ""
    return out


async def get_pumpkin(guild_id: int, fresh: bool = False) -> dict:
    """호박 상태(이미지 제외). `fresh` 면 캐시를 건너뜁니다(때리기 판정 등)."""
    if not fresh:
        hit = _cached("pumpkin", guild_id)
        if hit is not None:
            return dict(hit)
    key = str(guild_id)
    doc = None
    if pumpkin_col is not None:
        try:
            doc = await _db(pumpkin_col.find_one, {"_id": key}, {"image": 0})
        except Exception as e:
            print(f"MongoDB get_pumpkin error: {e}, using local file.", flush=True)
            async with _pumpkin_lock:
                doc = _read_json(PUMPKIN_FILE).get(key)
    else:
        async with _pumpkin_lock:
            doc = _read_json(PUMPKIN_FILE).get(key)
    out = _pumpkin_out(doc)
    _remember("pumpkin", guild_id, out)
    return dict(out)


async def save_pumpkin(guild_id: int, changes: dict) -> None:
    """**바뀐 키만** `$set`. 체력·회차는 여기로 바꾸지 마세요(new_pumpkin_round)
    — 설정을 저장하다 그 사이 깎인 체력을 되돌리면 안 됩니다."""
    changes = {k: v for k, v in changes.items() if k in PUMPKIN_KEYS and k not in ("hp", "round", "hits")}
    if not changes:
        return
    _forget("pumpkin", guild_id)
    key = str(guild_id)
    if pumpkin_col is not None:
        try:
            await _db(pumpkin_col.update_one, {"_id": key},
                      {"$set": changes, "$setOnInsert": {"hp": DEFAULT_PUMPKIN["hp"],
                                                         "round": 1, "hits": 0}},
                      upsert=True)
            return
        except Exception as e:
            print(f"MongoDB save_pumpkin error: {e}, using local file.", flush=True)
    async with _pumpkin_lock:
        rows = _read_json(PUMPKIN_FILE)
        row = rows.setdefault(key, {"hp": DEFAULT_PUMPKIN["hp"], "round": 1, "hits": 0})
        row.update(changes)
        _write_json(PUMPKIN_FILE, rows)


async def new_pumpkin_round(guild_id: int) -> dict:
    """새 호박 — 체력을 꽉 채우고 회차를 올립니다(오늘 친 사람도 다시 칠 수 있음).
    진행 중인 포인트 배율(boost_until)은 **그대로 둡니다** — 이미 받은 보상입니다."""
    _forget("pumpkin", guild_id)
    cur = await get_pumpkin(guild_id, fresh=True)
    reset = {"hp": int(cur["hp_max"]), "hits": 0, "broken_at": None, "broken_by": None}
    key = str(guild_id)
    if pumpkin_col is not None:
        try:
            await _db(pumpkin_col.update_one, {"_id": key},
                      {"$set": reset, "$inc": {"round": 1}}, upsert=True)
            _forget("pumpkin", guild_id)
            return await get_pumpkin(guild_id, fresh=True)
        except Exception as e:
            print(f"MongoDB new_pumpkin_round error: {e}, using local file.", flush=True)
    async with _pumpkin_lock:
        rows = _read_json(PUMPKIN_FILE)
        row = rows.setdefault(key, {})
        row.update(reset)
        row["round"] = int(row.get("round", 1) or 1) + 1
        _write_json(PUMPKIN_FILE, rows)
    _forget("pumpkin", guild_id)
    return await get_pumpkin(guild_id, fresh=True)


async def save_pumpkin_image(guild_id: int, data: bytes, filename: str) -> None:
    """패널을 다시 올릴 때(메시지가 지워졌을 때 등) 필요해서 DB 에 둡니다.
    디스코드 첨부 주소는 만료되므로 주소만 저장하면 며칠 뒤 그림이 깨집니다."""
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError("이미지가 너무 큽니다")
    payload = {"image": base64.b64encode(data).decode(), "image_name": filename,
               "has_image": True}
    _forget("pumpkin", guild_id)
    key = str(guild_id)
    if pumpkin_col is not None:
        try:
            await _db(pumpkin_col.update_one, {"_id": key},
                      {"$set": payload, "$setOnInsert": {"hp": DEFAULT_PUMPKIN["hp"],
                                                         "round": 1, "hits": 0}},
                      upsert=True)
            return
        except Exception as e:
            print(f"MongoDB save_pumpkin_image error: {e}, using local file.", flush=True)
    async with _pumpkin_lock:
        rows = _read_json(PUMPKIN_FILE)
        rows.setdefault(key, {"hp": DEFAULT_PUMPKIN["hp"], "round": 1, "hits": 0}).update(payload)
        _write_json(PUMPKIN_FILE, rows)


async def get_pumpkin_image(guild_id: int) -> tuple[bytes, str] | None:
    key = str(guild_id)
    doc = None
    if pumpkin_col is not None:
        try:
            doc = await _db(pumpkin_col.find_one, {"_id": key}, {"image": 1, "image_name": 1})
        except Exception as e:
            print(f"MongoDB get_pumpkin_image error: {e}, using local file.", flush=True)
            async with _pumpkin_lock:
                doc = _read_json(PUMPKIN_FILE).get(key)
    else:
        async with _pumpkin_lock:
            doc = _read_json(PUMPKIN_FILE).get(key)
    if not doc or not doc.get("image"):
        return None
    try:
        return base64.b64decode(doc["image"]), str(doc.get("image_name") or "pumpkin.png")
    except (ValueError, TypeError):
        return None


def _hit_id(guild_id, rnd, day, user_id) -> str:
    return f"{guild_id}:{rnd}:{day}:{user_id}"


async def hit_pumpkin(guild_id: int, user_id: int, damage: int,
                      when: datetime | None = None) -> dict:
    """호박을 한 번 칩니다.

    돌려주는 것: {"status": "ok"|"already"|"dead"|"off", "hp", "hp_max",
                  "broke": bool, "boost_until"}
    """
    now = when or datetime.now(KST)
    day = kst_day(now)
    p = await get_pumpkin(guild_id, fresh=True)
    if not p["enabled"]:
        return {"status": "off"}
    if int(p["hp"]) <= 0:
        return {"status": "dead", "hp": 0, "hp_max": p["hp_max"]}
    rnd = int(p["round"])
    hid = _hit_id(guild_id, rnd, day, user_id)
    key = str(guild_id)
    damage = max(1, int(damage))

    if pumpkin_col is not None and pumpkin_hits_col is not None:
        try:
            try:
                await _db(pumpkin_hits_col.insert_one,
                          {"_id": hid, "guild_id": key, "round": rnd, "day": day,
                           "user_id": str(user_id), "damage": damage,
                           "created": datetime.now(timezone.utc)})
            except _DuplicateKey:
                return {"status": "already", "hp": p["hp"], "hp_max": p["hp_max"]}
            after = await _db(pumpkin_col.find_one_and_update,
                              {"_id": key, "round": rnd, "enabled": True, "hp": {"$gt": 0}},
                              {"$inc": {"hp": -damage, "hits": 1}},
                              projection={"image": 0}, return_document=_AFTER)
            if after is None:
                # 그 사이 깨졌거나 꺼졌습니다 — 오늘 기회를 돌려줍니다
                await _db(pumpkin_hits_col.delete_one, {"_id": hid})
                cur = await get_pumpkin(guild_id, fresh=True)
                return {"status": "dead" if cur["enabled"] else "off",
                        "hp": max(0, int(cur["hp"])), "hp_max": cur["hp_max"]}
            return await _after_hit(guild_id, user_id, after, damage, now, p)
        except Exception as e:
            print(f"MongoDB hit_pumpkin error: {e}, using local file.", flush=True)

    async with _pumpkin_lock:
        hits = _read_json(PUMPKIN_HITS_FILE)
        if hid in hits:
            return {"status": "already", "hp": p["hp"], "hp_max": p["hp_max"]}
        rows = _read_json(PUMPKIN_FILE)
        row = rows.get(key) or {}
        if not row.get("enabled") or int(row.get("hp", 0)) <= 0 or int(row.get("round", 1)) != rnd:
            return {"status": "dead" if row.get("enabled") else "off",
                    "hp": max(0, int(row.get("hp", 0))), "hp_max": p["hp_max"]}
        hits[hid] = {"guild_id": key, "round": rnd, "day": day, "user_id": str(user_id),
                     "damage": damage}
        row["hp"] = int(row.get("hp", 0)) - damage
        row["hits"] = int(row.get("hits", 0)) + 1
        broke = row["hp"] <= 0 and row["hp"] + damage > 0 and not row.get("broken_at")
        if broke:
            row["broken_at"] = now.timestamp()
            row["broken_by"] = str(user_id)
            row["boost_until"] = now.timestamp() + float(row.get("boost_days", 7)) * 86400
        _write_json(PUMPKIN_HITS_FILE, hits)
        _write_json(PUMPKIN_FILE, rows)
    _forget("pumpkin", guild_id)
    return {"status": "ok", "hp": max(0, row["hp"]), "hp_max": p["hp_max"],
            "broke": broke, "boost_until": row.get("boost_until")}


async def _after_hit(guild_id, user_id, after: dict, damage: int, now: datetime,
                     p: dict) -> dict:
    hp = int(after.get("hp", 0))
    broke = False
    boost_until = after.get("boost_until")
    if hp <= 0 and hp + damage > 0:
        # 0 을 넘어간 **바로 이 한 번** — 기한도 broken_at 이 비었을 때만 겁니다
        until = now.timestamp() + float(after.get("boost_days") or p["boost_days"]) * 86400
        res = await _db(pumpkin_col.update_one,
                        {"_id": str(guild_id), "round": after.get("round"), "broken_at": None},
                        {"$set": {"broken_at": now.timestamp(), "broken_by": str(user_id),
                                  "boost_until": until}})
        if getattr(res, "modified_count", 0):
            broke, boost_until = True, until
    _forget("pumpkin", guild_id)
    return {"status": "ok", "hp": max(0, hp), "hp_max": after.get("hp_max") or p["hp_max"],
            "broke": broke, "boost_until": boost_until}


async def pumpkin_hits_today(guild_id: int, rnd: int, when: datetime | None = None) -> int:
    day = kst_day(when)
    if pumpkin_hits_col is not None:
        try:
            return await _db(pumpkin_hits_col.count_documents,
                             {"guild_id": str(guild_id), "round": int(rnd), "day": day})
        except Exception as e:
            print(f"MongoDB pumpkin_hits_today error: {e}, using local file.", flush=True)
    async with _pumpkin_lock:
        hits = _read_json(PUMPKIN_HITS_FILE)
    return sum(1 for h in hits.values() if h.get("guild_id") == str(guild_id)
               and h.get("round") == int(rnd) and h.get("day") == day)


def boost_active(p: dict, now: float | None = None) -> bool:
    until = p.get("boost_until")
    return bool(until) and (now or time.time()) < float(until)


async def point_multiplier(guild_id: int) -> float:
    """지금 받는 포인트에 곱할 배율 (호박이 깨진 뒤 boost_days 동안 boost_mult).

    **모든 채팅 · 출석 · 퀘스트 경로**가 부르므로 캐시합니다. 실패해도 1.0 —
    배율 조회가 터져서 포인트가 아예 안 들어오면 그게 더 큰 사고입니다.
    """
    try:
        p = await get_pumpkin(guild_id)
    except Exception as e:
        print(f"[EventDB] 배율 조회 실패(1배로): {type(e).__name__}: {e}", flush=True)
        return 1.0
    if boost_active(p):
        try:
            return max(1.0, float(p.get("boost_mult") or 1.0))
        except (TypeError, ValueError):
            return 1.0
    return 1.0


async def multiplier_split(guild_id: int) -> tuple[float, float]:
    """호박 배율을 (기본 포인트에 곱할 것, 시즌 포인트에 곱할 것) 으로 나눕니다.

    요청(2026-10-05): **호박이 깨진 뒤의 2배는 시즌 포인트로.** 시즌이 진행 중이면
    기본 포인트는 1배, 시즌 포인트만 2배입니다(퀘스트 100 → 기본 100 · 시즌 200).
    시즌이 아니면 시즌 포인트가 없으니 예전처럼 기본 포인트가 2배입니다 — 그러지
    않으면 시즌 밖에서 호박을 깬 보상이 통째로 사라집니다.
    """
    mult = await point_multiplier(guild_id)
    if mult <= 1.0:
        return 1.0, 1.0
    try:
        active = season_state(await get_season(guild_id)) == "active"
    except Exception as e:
        print(f"[EventDB] 시즌 확인 실패(기본 포인트에 배율): {type(e).__name__}: {e}", flush=True)
        active = False
    return (1.0, mult) if active else (mult, mult)


def apply_multiplier(amount: int, mult: float) -> int:
    """배율 적용. 반올림 — 내림이면 10P × 1.5 같은 경우 유저가 손해를 봅니다."""
    if amount <= 0 or mult == 1.0:
        return int(amount)
    return int(round(amount * mult))


# ── 시즌 포인트 ───────────────────────────────────────────────────────

DEFAULT_SEASON = {"name": "시즌", "start": None, "end": None, "season_id": None,
                  # True 면 시즌 중에는 기본 포인트 없이 시즌 포인트로만 쌓입니다.
                  # 기본값이 켜짐인 것은 요청(2026-10-05: "시즌 기간에는 모든 획득 포인트를
                  # 시즌 포인트로") 이고, /시즌설정 의 적립 방식 버튼으로 끌 수 있습니다.
                  "season_only": True}


async def get_season(guild_id: int, fresh: bool = False) -> dict:
    if not fresh:
        hit = _cached("season", guild_id)
        if hit is not None:
            return dict(hit)
    key = str(guild_id)
    doc = None
    if season_col is not None:
        try:
            doc = await _db(season_col.find_one, {"_id": key})
        except Exception as e:
            print(f"MongoDB get_season error: {e}, using local file.", flush=True)
            async with _season_lock:
                doc = _read_json(SEASON_FILE).get(key)
    else:
        async with _season_lock:
            doc = _read_json(SEASON_FILE).get(key)
    out = {k: (doc or {}).get(k, v) for k, v in DEFAULT_SEASON.items()}
    _remember("season", guild_id, out)
    return dict(out)


def season_state(s: dict, now: float | None = None) -> str:
    """'none'(설정 없음) · 'before'(시작 전) · 'active' · 'ended'"""
    if not s.get("season_id") or not s.get("start") or not s.get("end"):
        return "none"
    t = now or time.time()
    if t < float(s["start"]):
        return "before"
    if t >= float(s["end"]):
        return "ended"
    return "active"


async def save_season(guild_id: int, name: str, start: float, end: float) -> dict:
    """기간 · 이름 저장. 시즌 ID 는 **처음 한 번만** 만듭니다 — 기간을 고칠 때마다
    새로 만들면 모은 포인트가 날아갑니다. 초기화는 reset_season 입니다."""
    if end <= start:
        raise ValueError("끝이 시작보다 빠릅니다")
    cur = await get_season(guild_id, fresh=True)
    sid = cur.get("season_id") or f"s{int(time.time())}"
    payload = {"name": (name or "시즌")[:40], "start": float(start), "end": float(end),
               "season_id": sid}
    await _save_season_doc(guild_id, payload)
    return payload


async def reset_season(guild_id: int) -> dict:
    """시즌 포인트 초기화 — 새 시즌 ID 를 발급합니다. 옛 포인트는 뒤에서 지웁니다."""
    cur = await get_season(guild_id, fresh=True)
    old = cur.get("season_id")
    payload = dict(cur)
    payload["season_id"] = f"s{int(time.time() * 1000)}"
    await _save_season_doc(guild_id, payload)
    if old:
        await _purge_old_season(guild_id, old)
    return payload


async def _save_season_doc(guild_id: int, payload: dict) -> None:
    _forget("season", guild_id)
    key = str(guild_id)
    if season_col is not None:
        try:
            await _db(season_col.update_one, {"_id": key}, {"$set": payload}, upsert=True)
            _forget("season", guild_id)
            return
        except Exception as e:
            print(f"MongoDB save_season error: {e}, using local file.", flush=True)
    async with _season_lock:
        rows = _read_json(SEASON_FILE)
        rows.setdefault(key, {}).update(payload)
        _write_json(SEASON_FILE, rows)
    _forget("season", guild_id)


async def _purge_old_season(guild_id: int, season_id: str) -> None:
    if season_pts_col is not None:
        try:
            await _db(season_pts_col.delete_many,
                      {"guild_id": str(guild_id), "season_id": season_id})
            return
        except Exception as e:
            print(f"MongoDB purge season error: {e}, using local file.", flush=True)
    async with _season_lock:
        rows = _read_json(SEASON_PTS_FILE)
        prefix = f"{guild_id}:{season_id}:"
        for k in [k for k in rows if k.startswith(prefix)]:
            rows.pop(k, None)
        _write_json(SEASON_PTS_FILE, rows)


def _sp_key(guild_id, season_id, user_id) -> str:
    return f"{guild_id}:{season_id}:{user_id}"


async def add_season_points(guild_id: int, user_id: int, amount: int,
                            only_active: bool = True) -> int:
    """시즌 포인트 적립. 시즌 기간이 아니면 0 (아무것도 안 함).
    `only_active=False` 는 환불용 — 사는 도중에 시즌이 끝나도 돌려줍니다."""
    amount = int(amount)
    if amount <= 0:
        return 0
    s = await get_season(guild_id)
    if only_active and season_state(s) != "active":
        return 0
    if not s.get("season_id"):
        return 0
    key = _sp_key(guild_id, s["season_id"], user_id)
    if season_pts_col is not None:
        try:
            await _db(season_pts_col.update_one, {"_id": key},
                      {"$inc": {"points": amount},
                       "$set": {"updated": datetime.now(timezone.utc)},
                       "$setOnInsert": {"guild_id": str(guild_id), "season_id": s["season_id"],
                                        "user_id": str(user_id)}},
                      upsert=True)
            return amount
        except Exception as e:
            print(f"MongoDB add_season_points error: {e}, using local file.", flush=True)
    async with _season_lock:
        rows = _read_json(SEASON_PTS_FILE)
        row = rows.setdefault(key, {"guild_id": str(guild_id), "season_id": s["season_id"],
                                    "user_id": str(user_id), "points": 0})
        row["points"] = int(row.get("points", 0)) + amount
        _write_json(SEASON_PTS_FILE, rows)
    return amount


async def get_season_points(guild_id: int, user_id: int) -> int:
    """지금 시즌의 보유량. 시즌이 없거나 끝났으면 0 (소멸)."""
    s = await get_season(guild_id)
    if season_state(s) not in ("active",):
        return 0
    key = _sp_key(guild_id, s["season_id"], user_id)
    if season_pts_col is not None:
        try:
            doc = await _db(season_pts_col.find_one, {"_id": key})
            return int((doc or {}).get("points", 0) or 0)
        except Exception as e:
            print(f"MongoDB get_season_points error: {e}, using local file.", flush=True)
    async with _season_lock:
        return int((_read_json(SEASON_PTS_FILE).get(key) or {}).get("points", 0) or 0)


async def spend_season_points(guild_id: int, user_id: int, amount: int) -> tuple[bool, int]:
    """원자 차감 — 잔액이 충분할 때만. 시즌 기간이 아니면 실패."""
    amount = int(amount)
    s = await get_season(guild_id, fresh=True)
    if season_state(s) != "active":
        return False, 0
    if amount <= 0:
        return True, await get_season_points(guild_id, user_id)
    key = _sp_key(guild_id, s["season_id"], user_id)
    if season_pts_col is not None:
        try:
            doc = await _db(season_pts_col.find_one_and_update,
                            {"_id": key, "points": {"$gte": amount}},
                            {"$inc": {"points": -amount},
                             "$set": {"updated": datetime.now(timezone.utc)}},
                            return_document=_AFTER)
            if doc is not None:
                return True, int(doc.get("points", 0))
            return False, await get_season_points(guild_id, user_id)
        except Exception as e:
            print(f"MongoDB spend_season_points error: {e}, using local file.", flush=True)
    async with _season_lock:
        rows = _read_json(SEASON_PTS_FILE)
        row = rows.get(key)
        have = int((row or {}).get("points", 0) or 0)
        if have < amount:
            return False, have
        row["points"] = have - amount
        _write_json(SEASON_PTS_FILE, rows)
        return True, row["points"]


async def season_top(guild_id: int, limit: int = 10) -> list[dict]:
    s = await get_season(guild_id)
    if not s.get("season_id"):
        return []
    q = {"guild_id": str(guild_id), "season_id": s["season_id"], "points": {"$gt": 0}}
    if season_pts_col is not None:
        try:
            def _fetch():
                return list(season_pts_col.find(q).sort("points", -1).limit(limit))
            return [{"user_id": int(d["user_id"]), "points": int(d.get("points", 0))}
                    for d in await _db(_fetch)]
        except Exception as e:
            print(f"MongoDB season_top error: {e}, using local file.", flush=True)
    async with _season_lock:
        rows = _read_json(SEASON_PTS_FILE)
    out = [{"user_id": int(r["user_id"]), "points": int(r.get("points", 0))}
           for r in rows.values() if r.get("guild_id") == str(guild_id)
           and r.get("season_id") == s["season_id"] and int(r.get("points", 0)) > 0]
    out.sort(key=lambda r: -r["points"])
    return out[:limit]


async def season_stats(guild_id: int) -> dict:
    s = await get_season(guild_id)
    if not s.get("season_id"):
        return {"users": 0, "total": 0}
    q = {"guild_id": str(guild_id), "season_id": s["season_id"]}
    if season_pts_col is not None:
        try:
            def _agg():
                n, total = 0, 0
                for d in season_pts_col.find(q, {"points": 1}):
                    n += 1
                    total += int(d.get("points", 0) or 0)
                return {"users": n, "total": total}
            return await _db(_agg)
        except Exception as e:
            print(f"MongoDB season_stats error: {e}, using local file.", flush=True)
    async with _season_lock:
        rows = _read_json(SEASON_PTS_FILE)
    mine = [r for r in rows.values() if r.get("guild_id") == str(guild_id)
            and r.get("season_id") == s["season_id"]]
    return {"users": len(mine), "total": sum(int(r.get("points", 0)) for r in mine)}


async def save_season_only(guild_id: int, on: bool) -> None:
    """'시즌 중에는 시즌 포인트로만 적립' 켜기 · 끄기. 기간 · 모은 포인트는 그대로."""
    await _save_season_doc(guild_id, {"season_only": bool(on)})


async def base_points_paused(guild_id: int) -> bool:
    """지금 **기본 포인트를 주지 않고 시즌 포인트로만** 쌓는가.

    '시즌 포인트로만' 이 켜져 있고 **시즌이 진행 중일 때만** True 입니다 —
    시작 전 · 끝난 뒤에는 저절로 예전처럼 기본 포인트가 들어옵니다(시즌이
    끝났는데 아무것도 안 들어오면 안 됩니다). 읽기에 실패해도 False 입니다
    — 확인을 못 했다고 사람들의 적립을 멈추지 않습니다.
    """
    try:
        s = await get_season(guild_id)
        return bool(s.get("season_only")) and season_state(s) == "active"
    except Exception as e:
        print(f"[EventDB] 시즌 전용 적립 확인 실패: {type(e).__name__}: {e}", flush=True)
        return False


async def credit_earned(guild_id: int, user_id: int, base_points: int,
                        paused: bool = False) -> int:
    """'벌어서 받은' 포인트가 들어간 **뒤에** 부릅니다 — 같은 양을 시즌 포인트로.

    실패해도 예외를 올리지 않습니다. 시즌 적립이 터져서 출석·채팅 같은 본래
    지급이 실패한 것처럼 보이면 안 됩니다(본 지급은 이미 끝났습니다).

    `paused` 는 기본 포인트를 **이미 건너뛴** 경우입니다. 그 판정과 적립 사이에
    시즌이 끝나도 시즌 포인트는 넣습니다 — 안 넣으면 둘 다 못 받습니다.
    """
    try:
        return await add_season_points(guild_id, user_id, base_points,
                                       only_active=not paused)
    except Exception as e:
        print(f"[EventDB] 시즌 적립 실패: {type(e).__name__}: {e}", flush=True)
        return 0


# ── 역할 합성 ─────────────────────────────────────────────────────────
#
# 역할 두 개를 합쳐 히든 역할을 얻습니다(요청 2026-10-08). 레시피는 **배열이
# 아니라 키로 찾는 사전**(`recipes.<키>`)에 둡니다 — 추가 · 삭제를 각각 한
# 칸만 건드리는 원자 연산으로 할 수 있어서, 관리자 둘이 동시에 고쳐도 서로의
# 레시피를 지우지 않습니다(뽑기 items 를 통째로 덮어써 재고가 되살아난 교훈).
# 키는 재료 역할 ID(2~5개)를 **정렬해서** 만듭니다 — A+B 와 B+A 는 같은 레시피입니다.
# 두 개짜리는 예전 키(`r작은_큰`)와 같은 모양이라 기존 레시피가 그대로 맞습니다.

# public: /합성 을 유저에게 보여 줄지. 처음엔 **관리자만** 봅니다(요청 2026-10-09 —
# 레시피를 다 만들어 두고 공개하려고).
DEFAULT_FUSION = {"enabled": True, "announce": False, "public": False, "recipes": {}}


FUSION_MIN_INPUTS, FUSION_MAX_INPUTS = 2, 5


def recipe_key(*role_ids) -> str:
    """재료 역할 ID 들 → 레시피 키. `recipe_key(a, b)` · `recipe_key([a, b, c])` 둘 다 됩니다."""
    if len(role_ids) == 1 and isinstance(role_ids[0], (list, tuple, set)):
        role_ids = tuple(role_ids[0])
    ids = sorted({int(r) for r in role_ids})
    return "r" + "_".join(str(i) for i in ids)


async def get_fusion(guild_id: int, fresh: bool = False) -> dict:
    if not fresh:
        hit = _cached("fusion", guild_id)
        if hit is not None:
            return {**hit, "recipes": dict(hit.get("recipes") or {})}
    key = str(guild_id)
    doc = None
    if fusion_col is not None:
        try:
            doc = await _db(fusion_col.find_one, {"_id": key})
        except Exception as e:
            print(f"MongoDB get_fusion error: {e}, using local file.", flush=True)
            async with _fusion_lock:
                doc = _read_json(FUSION_FILE).get(key)
    else:
        async with _fusion_lock:
            doc = _read_json(FUSION_FILE).get(key)
    out = {k: (doc or {}).get(k, v) for k, v in DEFAULT_FUSION.items()}
    out["recipes"] = dict(out.get("recipes") or {})
    _remember("fusion", guild_id, out)
    return {**out, "recipes": dict(out["recipes"])}


async def save_fusion_settings(guild_id: int, changes: dict) -> None:
    """켜기 · 공개 알림 같은 설정. **레시피는 여기서 저장하지 않습니다**(통째 덮어쓰기 금지)."""
    payload = {k: v for k, v in changes.items() if k in ("enabled", "announce", "public")}
    if not payload:
        return
    _forget("fusion", guild_id)
    key = str(guild_id)
    if fusion_col is not None:
        try:
            await _db(fusion_col.update_one, {"_id": key}, {"$set": payload}, upsert=True)
            _forget("fusion", guild_id)
            return
        except Exception as e:
            print(f"MongoDB save_fusion_settings error: {e}, using local file.", flush=True)
    async with _fusion_lock:
        rows = _read_json(FUSION_FILE)
        rows.setdefault(key, {}).update(payload)
        _write_json(FUSION_FILE, rows)
    _forget("fusion", guild_id)


async def add_fusion_recipe(guild_id: int, inputs: list[int], result: int,
                            consume: bool = True) -> bool:
    """레시피 추가. 같은 재료 조합이 이미 있으면 False (덮어쓰지 않습니다).

    '없을 때만 넣기' 를 **필터**(`$exists: False`)로 판정합니다. 문서는 있는데
    그 키가 이미 있으면 필터가 안 맞아 upsert 가 같은 _id 로 삽입을 시도하고,
    그게 DuplicateKey 로 거절됩니다 — 동시에 같은 조합을 넣어도 하나만 남습니다.
    """
    ids = sorted({int(r) for r in inputs})
    if not FUSION_MIN_INPUTS <= len(ids) <= FUSION_MAX_INPUTS:
        raise ValueError(f"재료는 {FUSION_MIN_INPUTS}~{FUSION_MAX_INPUTS}개입니다")
    rk = recipe_key(ids)
    entry = {"inputs": ids, "result": int(result),
             "consume": bool(consume), "created": time.time()}
    key = str(guild_id)
    _forget("fusion", guild_id)
    if fusion_col is not None:
        try:
            await _db(fusion_col.update_one,
                      {"_id": key, f"recipes.{rk}": {"$exists": False}},
                      {"$set": {f"recipes.{rk}": entry}}, upsert=True)
            _forget("fusion", guild_id)
            return True
        except _DuplicateKey:
            return False
        except Exception as e:
            print(f"MongoDB add_fusion_recipe error: {e}, using local file.", flush=True)
    async with _fusion_lock:
        rows = _read_json(FUSION_FILE)
        recipes = rows.setdefault(key, {}).setdefault("recipes", {})
        if rk in recipes:
            return False
        recipes[rk] = entry
        _write_json(FUSION_FILE, rows)
    _forget("fusion", guild_id)
    return True


async def remove_fusion_recipe(guild_id: int, rk: str) -> bool:
    """레시피 하나만 지웁니다. 없었으면 False."""
    key = str(guild_id)
    _forget("fusion", guild_id)
    if fusion_col is not None:
        try:
            res = await _db(fusion_col.update_one,
                            {"_id": key, f"recipes.{rk}": {"$exists": True}},
                            {"$unset": {f"recipes.{rk}": ""}})
            _forget("fusion", guild_id)
            return bool(getattr(res, "matched_count", 0))
        except Exception as e:
            print(f"MongoDB remove_fusion_recipe error: {e}, using local file.", flush=True)
    async with _fusion_lock:
        rows = _read_json(FUSION_FILE)
        gone = (rows.get(key, {}).get("recipes") or {}).pop(rk, None) is not None
        if gone:
            _write_json(FUSION_FILE, rows)
    _forget("fusion", guild_id)
    return gone


async def mark_recipe_discovered(guild_id: int, rk: str, user_id: int) -> bool:
    """처음 성공한 사람을 기록합니다. **이번 호출이 최초면 True.**

    결과 역할은 이 기록이 생기는 순간 /합성 화면에서 모두에게 공개됩니다.
    '아직 아무도 없을 때만' 을 필터로 판정해, 두 사람이 동시에 성공해도
    최초는 한 명뿐입니다.
    """
    key = str(guild_id)
    field = f"recipes.{rk}"
    stamp = {f"{field}.discovered_by": str(user_id), f"{field}.discovered_at": time.time()}
    _forget("fusion", guild_id)
    if fusion_col is not None:
        try:
            res = await _db(fusion_col.update_one,
                            {"_id": key, field: {"$exists": True},
                             f"{field}.discovered_by": {"$exists": False}},
                            {"$set": stamp})
            _forget("fusion", guild_id)
            return bool(getattr(res, "matched_count", 0))
        except Exception as e:
            print(f"MongoDB mark_recipe_discovered error: {e}, using local file.", flush=True)
    async with _fusion_lock:
        rows = _read_json(FUSION_FILE)
        rec = (rows.get(key, {}).get("recipes") or {}).get(rk)
        if rec is None or rec.get("discovered_by"):
            return False
        rec["discovered_by"], rec["discovered_at"] = str(user_id), time.time()
        _write_json(FUSION_FILE, rows)
    _forget("fusion", guild_id)
    return True


async def any_fusion_public(guild_ids) -> bool:
    """봇이 들어간 서버 중 하나라도 /합성 을 공개했는가 — 전역 명령어라 한 값을 같이 씁니다."""
    for gid in guild_ids:
        try:
            if (await get_fusion(gid)).get("public"):
                return True
        except Exception as e:
            print(f"[EventDB] 합성 공개 여부 확인 실패 {gid}: {type(e).__name__}: {e}", flush=True)
    return False
