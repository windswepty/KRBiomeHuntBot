import os
import copy
import json
import time
import asyncio
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

load_dotenv()

DATA_DIR = "data"
ECONOMY_FILE = os.path.join(DATA_DIR, "economy.json")
SETTINGS_FILE = os.path.join(DATA_DIR, "report_settings.json")
SHOP_FILE = os.path.join(DATA_DIR, "shop_items.json")
GACHA_FILE = os.path.join(DATA_DIR, "gacha_settings.json")
LOGS_FILE = os.path.join(DATA_DIR, "activity_logs.json")
BLACKLIST_FILE = os.path.join(DATA_DIR, "blacklist.json")
BOOST_FILE = os.path.join(DATA_DIR, "channel_boosts.json")
MONGO_URI = os.getenv("MONGO_URI")

_lock = asyncio.Lock()
_settings_lock = asyncio.Lock()
_shop_lock = asyncio.Lock()
_gacha_lock = asyncio.Lock()
_logs_lock = asyncio.Lock()
_blacklist_lock = asyncio.Lock()
_boost_lock = asyncio.Lock()
KST = timezone(timedelta(hours=9))

# 호박 깨기 보상(포인트 N배) · 시즌 포인트. **벌어서 받는** 세 경로(출석 · 채팅 ·
# 퀘스트)에만 겁니다 — 관리자 지급 · 환불 · 뽑기 경품은 그대로입니다(환불이
# 2배가 되거나 뽑기로 포인트를 불리면 경제가 깨집니다). event_db 는 이 모듈을
# 부르지 않습니다(순환 import 방지).
from utils.event_db import point_multiplier, apply_multiplier, credit_earned

# 퀘스트 진행도는 유저×날짜라 계속 늘어납니다. **양쪽 경로 모두에** 상한을
# 겁니다 (Mongo 는 TTL 인덱스, JSON 은 _trim_quest_progress_local).
# ⚠️ 이 상수는 아래 인덱스 생성보다 **먼저** 정의돼야 합니다. 뒤에 두면
#    NameError 가 try/except 에 삼켜져 TTL 이 조용히 안 걸립니다.
QUEST_PROGRESS_TTL_DAYS = 21

# pymongo 의 ReturnDocument.AFTER. `if MONGO_URI:` 블록 안에서만 정의하면
# 폴백 모드에서 NameError 가 납니다. 항상 존재하도록 미리 둡니다.
_AFTER = None

# 중복 키 오류는 이 코드베이스에서 **정상 흐름**입니다. 필터에 '아직 안 받음'을
# 넣고 upsert 하면, 이미 받은 경우 필터에 안 걸려 삽입으로 넘어가고 같은 _id 라
# 중복 키가 납니다. 그걸로 '이미 받음'을 판정하므로 진짜 장애와 구분해야 합니다.
try:
    from pymongo.errors import DuplicateKeyError as _DuplicateKey
except ImportError:                       # 폴백 모드에서는 잡을 일이 없습니다
    class _DuplicateKey(Exception):
        pass

# MongoDB setup
# 저장 모드 — 포인트가 어디에 쌓이는지 나타냅니다.
#   "mongo"      : 영구 보존 (정상)
#   "local-json" : data/ 폴더의 임시 파일. Render 같은 환경에서는 재시작 시 소멸.
STORAGE_MODE = "local-json"
# MONGO_URI 가 설정돼 있는데도 연결에 실패한 경우 True — 명백한 장애 상황입니다.
MONGO_FAILED = False

mongo_db = None
users_col = None
settings_col = None
shop_col = None
gacha_col = None
logs_col = None
blacklist_col = None
boost_col = None
snapshot_col = None
backup_set_col = None
checkin_set_col = None
quest_set_col = None
quest_prog_col = None
quest_state_col = None
event_col = None

if MONGO_URI:
    try:
        from pymongo import MongoClient
        client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
        # Test connection
        client.admin.command('ping')
        mongo_db = client["krbiomehuntbot"]
        users_col = mongo_db["users"]
        settings_col = mongo_db["report_settings"]
        shop_col = mongo_db["shop_items"]
        gacha_col = mongo_db["gacha_settings"]
        logs_col = mongo_db["activity_logs"]
        blacklist_col = mongo_db["blacklist"]
        boost_col = mongo_db["channel_boosts"]
        snapshot_col = mongo_db["point_snapshots"]
        backup_set_col = mongo_db["backup_settings"]
        checkin_set_col = mongo_db["checkin_settings"]
        quest_set_col = mongo_db["quest_settings"]
        quest_prog_col = mongo_db["quest_progress"]
        quest_state_col = mongo_db["quest_state"]
        event_col = mongo_db["quest_events"]
        from pymongo import ReturnDocument as _ReturnDocument, UpdateOne as _UpdateOne
        _AFTER = _ReturnDocument.AFTER

        # 인덱스는 없어도 동작하므로, 실패해도 연결 자체를 포기하지 않습니다.
        # create_index 는 멱등이라 기동할 때마다 호출해도 안전합니다.
        try:
            # 랭킹(/포인트랭킹)이 길드별 포인트 정렬을 풀스캔하지 않도록
            users_col.create_index([("guild_id", 1), ("points", -1)], name="rank_by_points")
            # 모든 메시지마다 조회되는 블랙리스트
            blacklist_col.create_index([("guild_id", 1), ("user_id", 1)], name="bl_lookup")
            # /로그 조회 및 오래된 로그 정리
            logs_col.create_index([("guild_id", 1), ("_id", -1)], name="logs_recent")
            # 포인트 스냅샷 최신순 조회
            snapshot_col.create_index([("guild_id", 1), ("_id", -1)], name="snap_recent")
            # 퀘스트 진행도는 유저×날짜라 계속 늘어납니다. 봇이 꺼져 있어도
            # 서버가 지우도록 TTL 을 겁니다 (JSON 폴백에는 별도 trim 이 있습니다).
            quest_prog_col.create_index("created", name="quest_prog_ttl",
                                        expireAfterSeconds=QUEST_PROGRESS_TTL_DAYS * 86400)
            quest_state_col.create_index("created", name="quest_state_ttl",
                                         expireAfterSeconds=QUEST_PROGRESS_TTL_DAYS * 86400)
        except Exception as e:
            print(f"MongoDB index setup warning: {e}", flush=True)

        STORAGE_MODE = "mongo"
        print("Connected to MongoDB successfully for permanent data storage!", flush=True)
    except Exception as e:
        MONGO_FAILED = True
        # 포인트는 이 봇의 존재 이유이므로, 조용히 임시 저장소로 넘어가면 안 됩니다.
        # 이 상태로 운영되면 유저에게 포인트가 0으로 보이고, 그동안 새로 쌓인
        # 적립분은 재시작과 함께 사라집니다. 로그에서 절대 놓치지 않게 알립니다.
        print("\n" + "!" * 70, flush=True)
        print("!! 치명적: MONGO_URI 가 설정돼 있으나 연결에 실패했습니다.", flush=True)
        print(f"!! 원인: {e}", flush=True)
        print("!! 포인트가 '임시 파일'에 쌓이며, 재시작하면 전부 사라집니다.", flush=True)
        print("!! 기존 포인트도 유저에게 0으로 보입니다.", flush=True)
        print("!! -> 즉시 봇을 정지하고 MONGO_URI / Atlas 상태를 확인하세요.", flush=True)
        print("!" * 70 + "\n", flush=True)
        _AFTER = None
        _UpdateOne = None
        users_col = None
        settings_col = None
        shop_col = None
        gacha_col = None
        logs_col = None
        blacklist_col = None
        boost_col = None
        snapshot_col = None
        backup_set_col = None
        checkin_set_col = None
        quest_set_col = None
        quest_prog_col = None
        quest_state_col = None
        event_col = None

async def _db(fn, *args, **kwargs):
    """동기 드라이버(pymongo) 호출을 워커 스레드에서 실행합니다.

    pymongo 는 동기 라이브러리라 async 함수에서 그대로 부르면 그동안
    이벤트 루프 전체가 멈춥니다. 채팅 적립은 **서버의 모든 메시지마다**
    실행되므로, 이 정지가 누적되면 개인서버 링크 버튼 응답까지 느려집니다.

    ⚠️ 스레드로 넘기면 await 중단점이 생기므로, 읽기→수정→쓰기 형태의
    함수는 반드시 먼저 원자 연산($inc 등)으로 바꿔야 합니다.
    그렇지 않으면 포인트 중복 차감·채팅 카운트 유실이 발생합니다.
    """
    return await asyncio.to_thread(fn, *args, **kwargs)


class _TTLCache:
    """시간 기반 만료 + 크기 상한이 있는 캐시."""

    __slots__ = ("_data", "_ttl", "_max")

    def __init__(self, ttl_seconds: float, max_items: int):
        self._data: dict = {}
        self._ttl = ttl_seconds
        self._max = max_items

    def get(self, key):
        item = self._data.get(key)
        if item is None:
            return None
        stamped, value = item
        if time.monotonic() - stamped > self._ttl:
            self._data.pop(key, None)
            return None
        return value

    def set(self, key, value) -> None:
        self._data[key] = (time.monotonic(), value)
        if len(self._data) > self._max:
            self._evict()

    def pop(self, key, default=None):
        item = self._data.pop(key, None)
        return default if item is None else item[1]

    def clear(self) -> None:
        self._data.clear()

    def __len__(self) -> int:
        return len(self._data)

    def _evict(self) -> None:
        now = time.monotonic()
        for key in [k for k, (ts, _) in self._data.items() if now - ts > self._ttl]:
            self._data.pop(key, None)
        if len(self._data) > self._max:
            for key in list(self._data)[:len(self._data) - self._max // 2]:
                self._data.pop(key, None)


# 모든 메시지마다 조회되므로 캐시가 필수입니다.
# 추가/해제 시 즉시 무효화하므로 값이 어긋나지 않습니다.
_blacklist_cache = _TTLCache(ttl_seconds=300, max_items=64)
# 채널 부스트도 메시지마다 조회됩니다. 설정 변경 시 무효화합니다.
_boost_cache = _TTLCache(ttl_seconds=30, max_items=500)
# 퀘스트 설정은 **모든 메시지마다** 조회됩니다. 캐시가 없으면 채팅 한 줄마다
# DB 를 한 번씩 읽습니다(블랙리스트를 캐시하는 것과 같은 이유).
# save_quest_settings() 가 즉시 무효화하므로 설정 변경은 바로 반영됩니다.
_quest_settings_cache = _TTLCache(ttl_seconds=60, max_items=8)


async def _ensure_user_doc(guild_id: int, user_id: int) -> dict:
    """유저 문서가 반드시 존재하도록 보장하고 그 내용을 돌려줍니다.

    레거시 문서(_id 가 user_id 인 옛 형식, 현재 191건)를 흡수하는
    마이그레이션 경로를 그대로 유지합니다. 원자 연산이 대상 문서를 찾지
    못했을 때만 호출되는 보조 경로입니다.
    """
    str_key = _get_user_key(guild_id, user_id)
    base, _ = await _migrate_old_user_if_needed(guild_id, user_id)
    base = dict(base)
    base["_id"] = str_key
    base.setdefault("guild_id", str(guild_id))
    base.setdefault("user_id", str(user_id))
    base.setdefault("points", 0)
    base.setdefault("chat_count", 0)
    base.setdefault("last_checkin", "")
    base.setdefault("streak", 0)
    if users_col is not None:
        try:
            # 이미 있으면 건드리지 않고, 없을 때만 만듭니다.
            # _id 는 불변 필드라 $setOnInsert 페이로드에서 제외합니다
            # (필터의 _id 로 이미 결정됩니다).
            seed = {k: v for k, v in base.items() if k != "_id"}
            await _db(users_col.update_one, {"_id": str_key},
                      {"$setOnInsert": seed}, upsert=True)
        except Exception as e:
            print(f"MongoDB _ensure_user_doc error: {e}", flush=True)
    return base


if not MONGO_URI:
    print("\n" + "=" * 70, flush=True)
    print("== 경고: MONGO_URI 가 설정되지 않았습니다.", flush=True)
    print("== 포인트가 data/ 폴더의 로컬 파일에만 저장됩니다.", flush=True)
    print("== Render 등 클라우드 환경이라면 재시작마다 전부 사라집니다.", flush=True)
    print("=" * 70 + "\n", flush=True)


def storage_status() -> str:
    """기동 로그에 찍을 저장 상태 한 줄 요약."""
    if STORAGE_MODE == "mongo":
        return "MongoDB (영구 보존)"
    if MONGO_FAILED:
        return "로컬 임시 파일 -- !! MONGO_URI 연결 실패 / 재시작 시 소멸 !!"
    return "로컬 임시 파일 (MONGO_URI 미설정 / 재시작 시 소멸)"


def _ensure_data_dir():
    os.makedirs(DATA_DIR, exist_ok=True)
    if not os.path.exists(ECONOMY_FILE):
        with open(ECONOMY_FILE, "w", encoding="utf-8") as f:
            json.dump({}, f, ensure_ascii=False, indent=2)

def get_today_str() -> str:
    return datetime.now(KST).strftime("%Y-%m-%d")

async def load_data():
    _ensure_data_dir()
    async with _lock:
        try:
            with open(ECONOMY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

async def save_data(data):
    _ensure_data_dir()
    async with _lock:
        with open(ECONOMY_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

def _get_user_key(guild_id: int, user_id: int) -> str:
    return f"{guild_id}:{user_id}"

async def _migrate_old_user_if_needed(guild_id: int, user_id: int, data: dict = None) -> tuple[dict, bool]:
    """Helper to fetch or migrate old user data seamlessly."""
    str_key = _get_user_key(guild_id, user_id)
    str_uid = str(user_id)
    str_gid = str(guild_id)

    # 1. MongoDB
    if users_col is not None:
        try:
            doc = await _db(users_col.find_one, {"_id": str_key})
            if doc:
                return doc, False

            # Check for legacy doc without guild_id (with _id == user_id)
            old_doc = await _db(users_col.find_one, {"_id": str_uid})
            if old_doc:
                new_doc = {
                    "_id": str_key,
                    "guild_id": str_gid,
                    "user_id": str_uid,
                    "points": old_doc.get("points", 0),
                    "last_checkin": old_doc.get("last_checkin", ""),
                    "chat_count": old_doc.get("chat_count", 0)
                }
                # replace_one 이면 그 사이 다른 코루틴이 적립한 포인트를 덮어씁니다.
                # $setOnInsert 는 '문서가 없을 때만' 쓰므로 기존 값을 건드리지 않습니다.
                seed = {k: v for k, v in new_doc.items() if k != "_id"}
                await _db(users_col.update_one, {"_id": str_key},
                          {"$setOnInsert": seed}, upsert=True)
                return new_doc, True

            new_doc = {"_id": str_key, "guild_id": str_gid, "user_id": str_uid, "points": 0, "last_checkin": "", "chat_count": 0}
            return new_doc, False
        except Exception as e:
            print(f"MongoDB _migrate_old_user_if_needed error: {e}", flush=True)

    # 2. Local JSON
    if data is not None:
        if str_key in data:
            return data[str_key], False
        if str_uid in data:
            old_info = data[str_uid]
            data[str_key] = {
                "points": old_info.get("points", 0),
                "last_checkin": old_info.get("last_checkin", ""),
                "chat_count": old_info.get("chat_count", 0)
            }
            return data[str_key], True

        data[str_key] = {"points": 0, "last_checkin": "", "chat_count": 0}
        return data[str_key], False

    return {"points": 0, "last_checkin": "", "chat_count": 0}, False

async def checkin_user(guild_id: int, user_id: int) -> dict:
    """출석 처리. 연속일수를 세고 그에 맞는 보상을 지급합니다.

    반환값:
        {"ok": bool, "points": 총보유, "streak": 연속일수,
         "base": int, "bonus": int, "milestone": int, "gained": int}
    ok=False 면 이미 오늘 출석한 것이며 streak 등은 현재 값입니다.

    ⚠️ 원자성: '오늘 아직 출석 안 함'을 **필터 조건**에 넣어 서버 측에서
    판정합니다. 연타해도 두 번 지급되지 않습니다. 연속 판정은 어제 날짜를
    필터에 넣은 시도를 **먼저** 하고, 실패하면 연속이 끊긴 것으로 봅니다.

    보너스는 기본 지급이 성공한 뒤 **두 번째 `$inc`** 로 얹습니다.
    첫 연산이 이미 last_checkin 을 오늘로 바꿔놓아 동시 요청은 거기서
    걸리므로, 보너스가 중복 지급될 수 없습니다.
    """
    str_key = _get_user_key(guild_id, user_id)
    today, yesterday = get_today_str(), get_yesterday_str()
    settings = await get_checkin_settings(guild_id)
    # 호박 보상 배율 — 기본 · 연속 보너스 · 마일스톤 전부에 곱합니다
    mult = await point_multiplier(guild_id)
    base = apply_multiplier(int(settings.get("base", 100)), mult)

    def _result(ok, points, streak):
        b, bo, ms = streak_reward(streak, settings) if ok else (0, 0, 0)
        b, bo, ms = (apply_multiplier(x, mult) for x in (b, bo, ms))
        return {"ok": ok, "points": points, "streak": streak,
                "base": b, "bonus": bo, "milestone": ms, "gained": b + bo + ms,
                "mult": mult}

    async def _done(ok, points, streak):
        res = _result(ok, points, streak)
        if ok:
            res["season"] = await credit_earned(guild_id, user_id, res["gained"])
        return res

    if users_col is not None:
        try:
            common = {"last_checkin": today, "guild_id": str(guild_id),
                      "user_id": str(user_id)}
            # ① 어제 출석했다면 연속 이어가기
            doc = await _db(users_col.find_one_and_update,
                            {"_id": str_key, "last_checkin": yesterday},
                            {"$inc": {"points": base, "streak": 1}, "$set": common},
                            return_document=_AFTER)
            if doc is None:
                # ② 연속이 끊겼거나 첫 출석 — streak 을 1로 초기화
                doc = await _db(users_col.find_one_and_update,
                                {"_id": str_key, "last_checkin": {"$ne": today}},
                                {"$inc": {"points": base},
                                 "$set": {**common, "streak": 1}},
                                return_document=_AFTER)
            if doc is None:
                # 문서가 아직 없을 수 있습니다(레거시 포함). 만들고 다시 시도합니다.
                cur = await _ensure_user_doc(guild_id, user_id)
                if cur.get("last_checkin") == today:
                    return _result(False, cur.get("points", 0),
                                   int(cur.get("streak", 0) or 0))
                doc = await _db(users_col.find_one_and_update,
                                {"_id": str_key, "last_checkin": {"$ne": today}},
                                {"$inc": {"points": base},
                                 "$set": {**common, "streak": 1}},
                                return_document=_AFTER)
                if doc is None:
                    return _result(False, cur.get("points", 0),
                                   int(cur.get("streak", 0) or 0))

            streak = int(doc.get("streak", 1) or 1)
            _, bonus, milestone = streak_reward(streak, settings)
            extra = apply_multiplier(bonus, mult) + apply_multiplier(milestone, mult)
            if extra > 0:
                bumped = await _db(users_col.find_one_and_update,
                                   {"_id": str_key}, {"$inc": {"points": extra}},
                                   return_document=_AFTER)
                if bumped is not None:
                    doc = bumped
            return await _done(True, doc.get("points", 0), streak)
        except Exception as e:
            print(f"MongoDB checkin error: {e}, using local file.", flush=True)

    # Local JSON fallback
    data = await load_data()
    user_info, _ = await _migrate_old_user_if_needed(guild_id, user_id, data)
    if user_info.get("last_checkin") == today:
        return _result(False, user_info.get("points", 0),
                       int(user_info.get("streak", 0) or 0))

    streak = (int(user_info.get("streak", 0) or 0) + 1
              if user_info.get("last_checkin") == yesterday else 1)
    _, bonus, milestone = streak_reward(streak, settings)
    user_info["last_checkin"] = today
    user_info["streak"] = streak
    user_info["points"] = (user_info.get("points", 0) + base + apply_multiplier(bonus, mult)
                           + apply_multiplier(milestone, mult))
    await save_data(data)
    return await _done(True, user_info["points"], streak)

async def process_chat_message(guild_id: int, user_id: int, channel_id: int = 0) -> tuple[int, bool, int, float]:
    """Returns (new_points, awarded, earned_points, boost_mult)"""
    str_key = _get_user_key(guild_id, user_id)
    boost_mult = await get_channel_boost(guild_id, channel_id) if channel_id > 0 else 1.0
    # 채널 부스트 × 호박 보상 배율 (둘 다 캐시라 채팅마다 DB 를 읽지 않습니다)
    event_mult = await point_multiplier(guild_id)
    earned_points = max(1, int(round(10 * boost_mult * event_mult)))

    if users_col is not None:
        try:
            # 읽고→더하고→덮어쓰면 동시에 온 메시지끼리 카운트를 서로 지웁니다.
            # $inc 로 서버 측에서 증가시켜 유실을 없앱니다.
            bump = {"$inc": {"chat_count": 1},
                    "$set": {"guild_id": str(guild_id), "user_id": str(user_id)}}
            doc = await _db(users_col.find_one_and_update,
                            {"_id": str_key}, bump, return_document=_AFTER)
            if doc is None:
                # 문서가 아직 없음(레거시 포함) — 만든 뒤 한 번 더 시도
                await _ensure_user_doc(guild_id, user_id)
                doc = await _db(users_col.find_one_and_update,
                                {"_id": str_key}, bump, return_document=_AFTER)
                if doc is None:
                    return 0, False, earned_points, boost_mult

            new_count = doc.get("chat_count", 0)
            new_points = doc.get("points", 0)
            awarded = new_count % 10 == 0
            if awarded:
                bumped = await _db(users_col.find_one_and_update,
                                   {"_id": str_key},
                                   {"$inc": {"points": earned_points}},
                                   return_document=_AFTER)
                new_points = (bumped or {}).get("points", new_points + earned_points)
                await credit_earned(guild_id, user_id, earned_points)
            return new_points, awarded, earned_points, boost_mult
        except Exception as e:
            print(f"MongoDB chat process error: {e}, using local file.", flush=True)

    # Local JSON fallback
    data = await load_data()
    user_info, _ = await _migrate_old_user_if_needed(guild_id, user_id, data)
    user_info["chat_count"] = user_info.get("chat_count", 0) + 1

    awarded = False
    if user_info["chat_count"] % 10 == 0:
        user_info["points"] = user_info.get("points", 0) + earned_points
        awarded = True

    await save_data(data)
    if awarded:
        await credit_earned(guild_id, user_id, earned_points)
    return user_info["points"], awarded, earned_points, boost_mult

async def get_user_points(guild_id: int, user_id: int) -> int:
    if users_col is not None:
        try:
            doc, _ = await _migrate_old_user_if_needed(guild_id, user_id)
            return doc.get("points", 0)
        except Exception as e:
            print(f"MongoDB get points error: {e}, using local file.", flush=True)

    data = await load_data()
    user_info, _ = await _migrate_old_user_if_needed(guild_id, user_id, data)
    return user_info.get("points", 0)

async def add_user_points(guild_id: int, user_id: int, amount: int) -> int:
    str_key = _get_user_key(guild_id, user_id)

    if users_col is not None:
        try:
            # 음수는 차감이므로 잔액 검사가 있는 전용 함수로 넘깁니다
            # (여기서 $inc 로 처리하면 포인트가 음수가 될 수 있습니다).
            if amount < 0:
                _, remaining = await deduct_user_points(guild_id, user_id, -amount)
                return remaining

            bump = {"$inc": {"points": amount},
                    "$set": {"guild_id": str(guild_id), "user_id": str(user_id)}}
            doc = await _db(users_col.find_one_and_update,
                            {"_id": str_key}, bump, return_document=_AFTER)
            if doc is None:
                await _ensure_user_doc(guild_id, user_id)
                doc = await _db(users_col.find_one_and_update,
                                {"_id": str_key}, bump, return_document=_AFTER)
            return (doc or {}).get("points", 0)
        except Exception as e:
            print(f"MongoDB add_user_points error: {e}, using local file.", flush=True)

    data = await load_data()
    user_info, _ = await _migrate_old_user_if_needed(guild_id, user_id, data)
    current_points = user_info.get("points", 0)
    new_points = max(0, current_points + amount)
    user_info["points"] = new_points
    await save_data(data)
    return new_points

async def deduct_user_points(guild_id: int, user_id: int, amount: int) -> tuple[bool, int]:
    str_key = _get_user_key(guild_id, user_id)

    if users_col is not None:
        try:
            # '잔액이 충분함'을 **필터 조건**에 넣습니다. 구매 버튼을 연타해도
            # 포인트는 한 번만 빠지고, 두 번째 시도는 조건 불일치로 실패합니다.
            take = {"$inc": {"points": -amount},
                    "$set": {"guild_id": str(guild_id), "user_id": str(user_id)}}
            doc = await _db(users_col.find_one_and_update,
                            {"_id": str_key, "points": {"$gte": amount}},
                            take, return_document=_AFTER)
            if doc is not None:
                return True, doc.get("points", 0)

            # 조건 불일치 = 잔액 부족이거나, 문서가 아직 없음(레거시 포함)
            base = await _ensure_user_doc(guild_id, user_id)
            current_points = base.get("points", 0)
            if current_points < amount:
                return False, current_points
            doc = await _db(users_col.find_one_and_update,
                            {"_id": str_key, "points": {"$gte": amount}},
                            take, return_document=_AFTER)
            if doc is None:
                return False, current_points
            return True, doc.get("points", 0)
        except Exception as e:
            print(f"MongoDB deduct_user_points error: {e}, using local file.", flush=True)

    data = await load_data()
    user_info, _ = await _migrate_old_user_if_needed(guild_id, user_id, data)
    current_points = user_info.get("points", 0)
    if current_points < amount:
        return False, current_points

    new_points = current_points - amount
    user_info["points"] = new_points
    await save_data(data)
    return True, new_points

async def get_top_users(guild_id: int, limit: int = 10) -> list[dict]:
    str_gid = str(guild_id)

    if users_col is not None:
        try:
            # Query guild-specific docs
            result = []
            cursor = await _db(lambda: list(users_col.find({"guild_id": str_gid})
                                            .sort("points", -1).limit(limit)))
            for doc in cursor:
                uid_val = doc.get("user_id") or doc["_id"].split(":")[-1]
                result.append({
                    "user_id": int(uid_val),
                    "points": doc.get("points", 0)
                })

            # If no guild-specific docs found, try migrating legacy docs (one-time)
            if not result:
                def _migrate_legacy():
                    for old_doc in users_col.find({"guild_id": {"$exists": False}}):
                        uid_str = old_doc["_id"]
                        # Skip non-numeric IDs
                        if not uid_str.isdigit():
                            continue
                        target_key = f"{str_gid}:{uid_str}"
                        # 검사 후 replace_one 하면, 그 사이 다른 처리가 만든 문서를
                        # 통째로 덮어써 포인트가 사라질 수 있습니다.
                        # $setOnInsert 는 '없을 때만' 쓰므로 기존 값을 절대 건드리지 않습니다.
                        users_col.update_one(
                            {"_id": target_key},
                            {"$setOnInsert": {
                                "guild_id": str_gid,
                                "user_id": uid_str,
                                "points": old_doc.get("points", 0),
                                "last_checkin": old_doc.get("last_checkin", ""),
                                "chat_count": old_doc.get("chat_count", 0)
                            }},
                            upsert=True
                        )
                    # Re-query after migration
                    return list(users_col.find({"guild_id": str_gid})
                                .sort("points", -1).limit(limit))
                cursor = await _db(_migrate_legacy)
                for doc in cursor:
                    uid_val = doc.get("user_id") or doc["_id"].split(":")[-1]
                    result.append({
                        "user_id": int(uid_val),
                        "points": doc.get("points", 0)
                    })

            return result
        except Exception as e:
            print(f"MongoDB get_top_users error: {e}, using local file.", flush=True)

    data = await load_data()
    # Local JSON: check for legacy keys only if no guild keys exist
    guild_items = []
    has_guild_keys = False
    for key, info in data.items():
        if ":" in key:
            g_str, u_str = key.split(":", 1)
            if g_str == str_gid:
                guild_items.append({"user_id": int(u_str), "points": info.get("points", 0)})
                has_guild_keys = True

    if not has_guild_keys:
        legacy_keys = [k for k in list(data.keys()) if ":" not in k and k.isdigit()]
        for k in legacy_keys:
            t_key = f"{str_gid}:{k}"
            data[t_key] = {
                "points": data[k].get("points", 0),
                "last_checkin": data[k].get("last_checkin", ""),
                "chat_count": data[k].get("chat_count", 0)
            }
            guild_items.append({"user_id": int(k), "points": data[k].get("points", 0)})
        if legacy_keys:
            await save_data(data)

    sorted_users = sorted(guild_items, key=lambda x: x["points"], reverse=True)
    return sorted_users[:limit]

async def get_user_rank(guild_id: int, user_id: int) -> tuple[int, int]:
    """Returns (rank, points) for the specified user_id in the guild."""
    str_gid = str(guild_id)
    str_key = _get_user_key(guild_id, user_id)

    if users_col is not None:
        try:
            doc, _ = await _migrate_old_user_if_needed(guild_id, user_id)
            user_points = doc.get("points", 0)
            count_higher = await _db(users_col.count_documents,
                                     {"guild_id": str_gid, "points": {"$gt": user_points}})
            return count_higher + 1, user_points
        except Exception as e:
            print(f"MongoDB get_user_rank error: {e}, using local file.", flush=True)

    data = await load_data()
    user_info, _ = await _migrate_old_user_if_needed(guild_id, user_id, data)
    user_points = user_info.get("points", 0)
    count_higher = sum(
        1 for k, info in data.items()
        if ":" in k and k.split(":", 1)[0] == str_gid and info.get("points", 0) > user_points
    )
    return count_higher + 1, user_points


# ── Report Settings (per-guild) ──────────────────────────────────────

DEFAULT_REPORT_SETTINGS = {
    "enabled": True,
    "default_points": 500,
    "notify_role_id": None,
    "report_channel_id": None,
    # ── /제보 안내 임베드 꾸미기 ──────────────────────────────────
    # 전부 None 이면 cogs/report.py 의 기본 문구를 씁니다. 그래서 이 항목이
    # 생기기 전에 설정한 서버도 아무것도 바꾸지 않고 예전 그대로 보입니다.
    "embed_title": None,
    "embed_description": None,
    "embed_color": None,          # "파랑" 또는 "#5865F2"
    "embed_footer": None,
    "embed_image": None,
    "embed_thumbnail": None,
    "button_label": None,
}

async def get_report_settings(guild_id: int) -> dict:
    """Get report settings for a guild. Returns defaults if not yet configured."""
    str_gid = str(guild_id)

    if settings_col is not None:
        try:
            doc = await _db(settings_col.find_one, {"_id": str_gid})
            if doc:
                result = dict(DEFAULT_REPORT_SETTINGS)
                result.update({k: v for k, v in doc.items() if k != "_id"})
                return result
        except Exception as e:
            print(f"MongoDB get_report_settings error: {e}, using local file.", flush=True)

    # Local JSON fallback
    _ensure_data_dir()
    async with _settings_lock:
        try:
            if os.path.exists(SETTINGS_FILE):
                with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                    all_settings = json.load(f)
                if str_gid in all_settings:
                    result = dict(DEFAULT_REPORT_SETTINGS)
                    result.update(all_settings[str_gid])
                    return result
        except Exception:
            pass
    return dict(DEFAULT_REPORT_SETTINGS)

async def save_report_settings(guild_id: int, settings: dict) -> None:
    """Save report settings for a guild."""
    str_gid = str(guild_id)

    if settings_col is not None:
        try:
            await _db(
                settings_col.update_one,
                {"_id": str_gid},
                {"$set": settings},
                upsert=True
            )
            return
        except Exception as e:
            print(f"MongoDB save_report_settings error: {e}, using local file.", flush=True)

    # Local JSON fallback
    _ensure_data_dir()
    async with _settings_lock:
        all_settings = {}
        try:
            if os.path.exists(SETTINGS_FILE):
                with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                    all_settings = json.load(f)
        except Exception:
            pass

        if str_gid not in all_settings:
            all_settings[str_gid] = {}
        all_settings[str_gid].update(settings)

        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(all_settings, f, ensure_ascii=False, indent=2)


# ── Shop Settings & Items DB (per-guild) ──────────────────────────────

DEFAULT_SHOP_SETTINGS = {
    "enabled": True,
    "title": "한바헌 포인트 상점",
    "items": []
}

async def get_shop_settings(guild_id: int) -> dict:
    str_gid = str(guild_id)

    if shop_col is not None:
        try:
            doc = await _db(shop_col.find_one, {"_id": str_gid})
            if doc:
                res = dict(DEFAULT_SHOP_SETTINGS)
                res.update({k: v for k, v in doc.items() if k != "_id"})
                return res
        except Exception as e:
            print(f"MongoDB get_shop_settings error: {e}, using local file.", flush=True)

    _ensure_data_dir()
    async with _shop_lock:
        try:
            if os.path.exists(SHOP_FILE):
                with open(SHOP_FILE, "r", encoding="utf-8") as f:
                    all_shop = json.load(f)
                if str_gid in all_shop:
                    res = dict(DEFAULT_SHOP_SETTINGS)
                    res.update(all_shop[str_gid])
                    return res
        except Exception:
            pass
    return dict(DEFAULT_SHOP_SETTINGS)

async def save_shop_settings(guild_id: int, settings: dict) -> None:
    str_gid = str(guild_id)

    if shop_col is not None:
        try:
            await _db(
                shop_col.update_one,
                {"_id": str_gid},
                {"$set": settings},
                upsert=True
            )
            return
        except Exception as e:
            print(f"MongoDB save_shop_settings error: {e}, using local file.", flush=True)

    _ensure_data_dir()
    async with _shop_lock:
        all_shop = {}
        try:
            if os.path.exists(SHOP_FILE):
                with open(SHOP_FILE, "r", encoding="utf-8") as f:
                    all_shop = json.load(f)
        except Exception:
            pass

        if str_gid not in all_shop:
            all_shop[str_gid] = {}
        all_shop[str_gid].update(settings)

        with open(SHOP_FILE, "w", encoding="utf-8") as f:
            json.dump(all_shop, f, ensure_ascii=False, indent=2)

async def get_shop_items(guild_id: int) -> list[dict]:
    settings = await get_shop_settings(guild_id)
    return settings.get("items", [])

async def save_shop_items(guild_id: int, items: list[dict]) -> None:
    await save_shop_settings(guild_id, {"items": items})


# ── Gacha Settings & Items DB (per-guild) ─────────────────────────────

DEFAULT_GACHA_SETTINGS = {
    "enabled": True,
    "title": "한바헌 럭키 뽑기",
    "description": "",
    "price": 100,
    # 10회 뽑기 가격. 0 이면 `price * 10` (할인 없음)으로 계산합니다.
    # 값을 코드에 박지 않는 이유는 다른 수치와 같습니다 — 운영진이 할인폭을
    # 직접 조정하므로, 박아 두면 조정할 때마다 개발자를 불러야 합니다.
    "price_10": 0,
    "items": []
}

async def get_gacha_settings(guild_id: int) -> dict:
    str_gid = str(guild_id)

    if gacha_col is not None:
        try:
            doc = await _db(gacha_col.find_one, {"_id": str_gid})
            if doc:
                res = dict(DEFAULT_GACHA_SETTINGS)
                res.update({k: v for k, v in doc.items() if k != "_id"})
                return res
        except Exception as e:
            print(f"MongoDB get_gacha_settings error: {e}, using local file.", flush=True)

    _ensure_data_dir()
    async with _gacha_lock:
        try:
            if os.path.exists(GACHA_FILE):
                with open(GACHA_FILE, "r", encoding="utf-8") as f:
                    all_gacha = json.load(f)
                if str_gid in all_gacha:
                    res = dict(DEFAULT_GACHA_SETTINGS)
                    res.update(all_gacha[str_gid])
                    return res
        except Exception:
            pass
    return dict(DEFAULT_GACHA_SETTINGS)

async def set_gacha_item_stock(guild_id: int, item_id, stock: int) -> bool:
    """항목 하나의 재고만 **원자적으로** 씁니다.

    ⚠️ 설정 전체를 `$set` 하면 items 배열이 통째로 덮여, 그 사이 뽑기가
    `consume_gacha_stock()` 으로 깎아 둔 **다른 항목의 재고까지 되살아납니다.**
    관리자는 보통 재고가 떨어질 때 충전하는데, 그때가 바로 사람들이 뽑고
    있는 순간입니다.
    """
    str_gid = str(guild_id)
    if gacha_col is not None:
        try:
            res = await _db(gacha_col.update_one,
                            {"_id": str_gid, "items": {"$elemMatch": {"id": item_id}}},
                            {"$set": {"items.$.stock": int(stock)}})
            return res.matched_count > 0
        except Exception as e:
            print(f"MongoDB set_gacha_item_stock error: {e}, using local file.", flush=True)

    async with _gacha_lock:
        all_gacha = _read_json(GACHA_FILE)
        items = (all_gacha.get(str_gid) or {}).get("items") or []
        for item in items:
            if item.get("id") == item_id:
                item["stock"] = int(stock)
                all_gacha.setdefault(str_gid, {})["items"] = items
                _write_json(GACHA_FILE, all_gacha)
                return True
    return False


async def next_gacha_item_id(guild_id: int) -> int:
    """항목 id 를 **서버에서 원자적으로** 발급합니다.

    `max(id)+1` 은 읽고 나서 쓰는 사이에 다른 관리자가 끼어들면 **같은 id 가
    두 번 나옵니다.** id 가 겹치면 위치 연산자(`items.$.stock`)가 먼저 걸린
    항목만 고치고, `remove_gacha_item` 은 **둘 다 지웁니다.**

    기존 항목이 이미 1..N 을 쓰고 있으므로, 먼저 `$max` 로 카운터를 그보다
    낮지 않게 올린 뒤 `$inc` 합니다. `$max` 는 올리기만 해서 몇 번 돌려도
    안전하고, `$inc` 의 반환값은 동시 호출끼리 절대 겹치지 않습니다.
    """
    str_gid = str(guild_id)
    if gacha_col is not None:
        try:
            doc = await _db(gacha_col.find_one, {"_id": str_gid}) or {}
            base = max([int(i.get("id", 0) or 0) for i in doc.get("items", [])],
                       default=0)
            await _db(gacha_col.update_one, {"_id": str_gid},
                      {"$max": {"item_seq": base}}, upsert=True)
            got = await _db(gacha_col.find_one_and_update, {"_id": str_gid},
                            {"$inc": {"item_seq": 1}},
                            return_document=_AFTER, upsert=True)
            return int((got or {}).get("item_seq", base + 1))
        except Exception as e:
            print(f"MongoDB next_gacha_item_id error: {e}, using local file.", flush=True)

    async with _gacha_lock:
        all_gacha = _read_json(GACHA_FILE)
        row = all_gacha.setdefault(str_gid, {})
        base = max([int(i.get("id", 0) or 0) for i in row.get("items", [])], default=0)
        nxt = max(int(row.get("item_seq", 0) or 0), base) + 1
        row["item_seq"] = nxt
        _write_json(GACHA_FILE, all_gacha)
        return nxt


async def add_gacha_item(guild_id: int, item: dict) -> None:
    """항목 하나를 **덧붙입니다** (배열 전체를 다시 쓰지 않습니다)."""
    str_gid = str(guild_id)
    if gacha_col is not None:
        try:
            await _db(gacha_col.update_one, {"_id": str_gid},
                      {"$push": {"items": item}}, upsert=True)
            return
        except Exception as e:
            print(f"MongoDB add_gacha_item error: {e}, using local file.", flush=True)

    async with _gacha_lock:
        all_gacha = _read_json(GACHA_FILE)
        all_gacha.setdefault(str_gid, {}).setdefault("items", []).append(item)
        _write_json(GACHA_FILE, all_gacha)


async def remove_gacha_item(guild_id: int, item_id) -> None:
    """항목 하나만 **빼냅니다** (배열 전체를 다시 쓰지 않습니다)."""
    str_gid = str(guild_id)
    if gacha_col is not None:
        try:
            await _db(gacha_col.update_one, {"_id": str_gid},
                      {"$pull": {"items": {"id": item_id}}})
            return
        except Exception as e:
            print(f"MongoDB remove_gacha_item error: {e}, using local file.", flush=True)

    async with _gacha_lock:
        all_gacha = _read_json(GACHA_FILE)
        items = (all_gacha.get(str_gid) or {}).get("items") or []
        all_gacha.setdefault(str_gid, {})["items"] = [
            i for i in items if i.get("id") != item_id]
        _write_json(GACHA_FILE, all_gacha)


async def save_gacha_settings(guild_id: int, settings: dict) -> None:
    """⚠️ 넘긴 키만 `$set` 합니다. **items 를 넣지 마세요** — 배열이 통째로
    덮여 진행 중인 뽑기가 깎은 재고가 되살아납니다. 항목 변경은
    `add_gacha_item` / `remove_gacha_item` / `set_gacha_item_stock` 을 쓰세요.
    """
    str_gid = str(guild_id)

    if gacha_col is not None:
        try:
            await _db(
                gacha_col.update_one,
                {"_id": str_gid},
                {"$set": settings},
                upsert=True
            )
            return
        except Exception as e:
            print(f"MongoDB save_gacha_settings error: {e}, using local file.", flush=True)

    _ensure_data_dir()
    async with _gacha_lock:
        all_gacha = {}
        try:
            if os.path.exists(GACHA_FILE):
                with open(GACHA_FILE, "r", encoding="utf-8") as f:
                    all_gacha = json.load(f)
        except Exception:
            pass

        if str_gid not in all_gacha:
            all_gacha[str_gid] = {}
        all_gacha[str_gid].update(settings)

        with open(GACHA_FILE, "w", encoding="utf-8") as f:
            json.dump(all_gacha, f, ensure_ascii=False, indent=2)


# ── 재고 원자적 차감 ──────────────────────────────────────────────────
#
# 읽고→검사→쓰기를 파이썬에서 하면, 마지막 1개를 두 명이 동시에 사는 순간
# 둘 다 통과해 **재고 1개로 2개가 팔립니다**. 게다가 목록 전체를 덮어쓰면
# 다른 상품의 재고 변경까지 사라집니다(lost update).
# 조건을 MongoDB 필터에 넣어 서버 측에서 원자적으로 처리합니다.


def _stock_filter(str_gid: str, item_id):
    """항목 id 가 숫자/문자 어느 쪽으로 저장돼 있어도 매칭되도록 후보를 만듭니다."""
    candidates = [item_id]
    try:
        candidates.append(int(item_id))
    except (TypeError, ValueError):
        pass
    candidates.append(str(item_id))
    seen, uniq = set(), []
    for c in candidates:
        marker = (type(c).__name__, c)
        if marker not in seen:
            seen.add(marker)
            uniq.append(c)
    return {"_id": str_gid,
            "items": {"$elemMatch": {"id": {"$in": uniq}, "stock": {"$gt": 0}}}}


async def _consume_stock(col, file_path, lock, guild_id: int, item_id) -> bool:
    """재고를 원자적으로 1 차감합니다. 재고가 남아있지 않으면 False.

    무제한(-1) 항목은 호출부에서 거르므로 여기서는 다루지 않습니다.
    """
    str_gid = str(guild_id)
    delta = -1

    if col is not None:
        try:
            # '재고 > 0' 을 필터 조건에 넣어 서버 측에서 원자적으로 판정합니다
            res = await _db(col.update_one, _stock_filter(str_gid, item_id),
                            {"$inc": {"items.$.stock": delta}})
            return res.modified_count > 0
        except Exception as e:
            print(f"MongoDB stock update error: {e}, using local file.", flush=True)

    # 로컬 JSON 폴백 — 단일 프로세스이므로 락이 원자성을 보장합니다
    _ensure_data_dir()
    async with lock:
        all_data = {}
        try:
            if os.path.exists(file_path):
                with open(file_path, "r", encoding="utf-8") as f:
                    all_data = json.load(f)
        except Exception:
            pass
        items = all_data.get(str_gid, {}).get("items", [])
        for item in items:
            if str(item.get("id")) != str(item_id):
                continue
            current = item.get("stock", -1)
            if delta < 0 and current <= 0:
                return False
            item["stock"] = current + delta
            try:
                with open(file_path, "w", encoding="utf-8") as f:
                    json.dump(all_data, f, ensure_ascii=False, indent=2)
            except Exception as e:
                print(f"Local stock write error: {e}", flush=True)
                return False
            return True
    return False


async def consume_shop_stock(guild_id: int, item_id) -> bool:
    """상점 재고를 1 차감합니다. 이미 품절이면 False."""
    return await _consume_stock(shop_col, SHOP_FILE, _shop_lock, guild_id, item_id)


async def consume_gacha_stock(guild_id: int, item_id) -> bool:
    """뽑기 항목 재고를 1 차감합니다. 이미 품절이면 False."""
    return await _consume_stock(gacha_col, GACHA_FILE, _gacha_lock, guild_id, item_id)


# ── Activity Logs DB (per-guild) ──────────────────────────────────────

MAX_ACTIVITY_LOGS = 200
# 매 삽입마다 정리하면 낭비라, 기동 후 첫 삽입과 이후 N회마다 수행합니다.
# (재시작이 잦은 환경에서도 최소 한 번은 정리되도록 '첫 삽입'을 포함시켰습니다)
ACTIVITY_TRIM_EVERY = 50
_activity_insert_count: dict[str, int] = {}


def _maybe_trim_activity_logs(guild_id: int) -> None:
    """MongoDB의 활동 로그를 길드당 MAX_ACTIVITY_LOGS건으로 유지합니다.

    로컬 JSON 경로에는 원래 200건 상한이 있었지만 Mongo 경로에는 없어서
    영구 누적되고 있었습니다. 실패해도 기록을 방해하지 않도록 예외를 삼킵니다.
    """
    if logs_col is None:
        return
    str_gid = str(guild_id)
    count = _activity_insert_count.get(str_gid, 0) + 1
    _activity_insert_count[str_gid] = count
    if count != 1 and count % ACTIVITY_TRIM_EVERY != 0:
        return
    try:
        # ObjectId(_id)는 생성 시각 순으로 증가하므로 최신순 기준으로 씁니다.
        cursor = (logs_col.find({"guild_id": str_gid}, {"_id": 1})
                  .sort("_id", -1).skip(MAX_ACTIVITY_LOGS).limit(1))
        cutoff = next(iter(cursor), None)
        if cutoff is None:
            return
        logs_col.delete_many({"guild_id": str_gid, "_id": {"$lte": cutoff["_id"]}})
    except Exception as e:
        print(f"MongoDB activity log trim warning: {e}", flush=True)


async def add_activity_log(guild_id: int, log_type: str, user_id: int, user_name: str, details: str) -> None:
    """log_type: 'shop', 'gacha', 'report'"""
    str_gid = str(guild_id)
    timestamp = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")
    log_entry = {
        "type": log_type,
        "user_id": user_id,
        "user_name": user_name,
        "details": details,
        "timestamp": timestamp
    }

    if logs_col is not None:
        try:
            await _db(logs_col.insert_one, {"guild_id": str_gid, **log_entry})
        except Exception as e:
            print(f"MongoDB add_activity_log error: {e}", flush=True)
        else:
            # 삽입 성공 시에만 정리합니다. 정리 실패가 JSON 폴백을 부르면
            # 같은 기록이 두 곳에 남으므로 except 밖(else)에서 호출합니다.
            await asyncio.to_thread(_maybe_trim_activity_logs, guild_id)
            return

    _ensure_data_dir()
    async with _logs_lock:
        all_logs = {}
        try:
            if os.path.exists(LOGS_FILE):
                with open(LOGS_FILE, "r", encoding="utf-8") as f:
                    all_logs = json.load(f)
        except Exception:
            pass

        if str_gid not in all_logs:
            all_logs[str_gid] = []
        all_logs[str_gid].insert(0, log_entry)
        all_logs[str_gid] = all_logs[str_gid][:200]

        with open(LOGS_FILE, "w", encoding="utf-8") as f:
            json.dump(all_logs, f, ensure_ascii=False, indent=2)

async def get_activity_logs(guild_id: int, log_type: str = "all", limit: int = 15) -> list[dict]:
    str_gid = str(guild_id)

    if logs_col is not None:
        try:
            query = {"guild_id": str_gid}
            if log_type != "all":
                query["type"] = log_type
            def _fetch_logs():
                return list(logs_col.find(query).sort("_id", -1).limit(limit))
            cursor = await _db(_fetch_logs)
            return list(cursor)
        except Exception as e:
            print(f"MongoDB get_activity_logs error: {e}", flush=True)

    _ensure_data_dir()
    async with _logs_lock:
        try:
            if os.path.exists(LOGS_FILE):
                with open(LOGS_FILE, "r", encoding="utf-8") as f:
                    all_logs = json.load(f)
                logs = all_logs.get(str_gid, [])
                if log_type != "all":
                    logs = [l for l in logs if l.get("type") == log_type]
                return logs[:limit]
        except Exception:
            pass
    return []


# ── Blacklist DB (per-guild) ──────────────────────────────────────────

async def is_user_blacklisted(guild_id: int, user_id: int) -> bool:
    str_gid = str(guild_id)
    str_uid = str(user_id)

    # 이 함수는 **서버의 모든 메시지마다** 호출됩니다. 매번 DB를 왕복하면
    # 이벤트 루프가 계속 막히므로, 길드별 대상 집합을 통째로 캐싱합니다.
    # 추가/해제 시 즉시 무효화하므로 값이 어긋나지 않습니다.
    cached = _blacklist_cache.get(str_gid)
    if cached is not None:
        return str_uid in cached

    if blacklist_col is not None:
        try:
            def _fetch():
                return [d.get("user_id", "")
                        for d in blacklist_col.find({"guild_id": str_gid}, {"user_id": 1})]
            ids = set(await _db(_fetch))
            ids.discard("")
            _blacklist_cache.set(str_gid, ids)
            return str_uid in ids
        except Exception as e:
            print(f"MongoDB is_user_blacklisted error: {e}", flush=True)

    _ensure_data_dir()
    async with _blacklist_lock:
        try:
            if os.path.exists(BLACKLIST_FILE):
                with open(BLACKLIST_FILE, "r", encoding="utf-8") as f:
                    all_bl = json.load(f)
                ids = set(all_bl.get(str_gid, {}).keys())
                _blacklist_cache.set(str_gid, ids)
                return str_uid in ids
        except Exception:
            pass
    _blacklist_cache.set(str_gid, set())
    return False

async def add_blacklist_user(guild_id: int, user_id: int, reason: str = "") -> None:
    str_gid = str(guild_id)
    str_uid = str(user_id)
    _blacklist_cache.pop(str_gid)      # 캐시 무효화 — 다음 조회에서 다시 읽습니다
    timestamp = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")

    entry = {
        "guild_id": str_gid,
        "user_id": str_uid,
        "reason": reason if reason else "사유 미입력",
        "timestamp": timestamp
    }

    if blacklist_col is not None:
        try:
            await _db(
                blacklist_col.update_one,
                {"guild_id": str_gid, "user_id": str_uid},
                {"$set": entry},
                upsert=True
            )
            return
        except Exception as e:
            print(f"MongoDB add_blacklist_user error: {e}", flush=True)

    _ensure_data_dir()
    async with _blacklist_lock:
        all_bl = {}
        try:
            if os.path.exists(BLACKLIST_FILE):
                with open(BLACKLIST_FILE, "r", encoding="utf-8") as f:
                    all_bl = json.load(f)
        except Exception:
            pass

        if str_gid not in all_bl:
            all_bl[str_gid] = {}
        all_bl[str_gid][str_uid] = {
            "reason": reason if reason else "사유 미입력",
            "timestamp": timestamp
        }

        with open(BLACKLIST_FILE, "w", encoding="utf-8") as f:
            json.dump(all_bl, f, ensure_ascii=False, indent=2)

async def remove_blacklist_user(guild_id: int, user_id: int) -> bool:
    str_gid = str(guild_id)
    str_uid = str(user_id)
    _blacklist_cache.pop(str_gid)      # 캐시 무효화

    if blacklist_col is not None:
        try:
            res = await _db(blacklist_col.delete_one, {"guild_id": str_gid, "user_id": str_uid})
            return res.deleted_count > 0
        except Exception as e:
            print(f"MongoDB remove_blacklist_user error: {e}", flush=True)

    _ensure_data_dir()
    async with _blacklist_lock:
        try:
            if os.path.exists(BLACKLIST_FILE):
                with open(BLACKLIST_FILE, "r", encoding="utf-8") as f:
                    all_bl = json.load(f)
                if str_gid in all_bl and str_uid in all_bl[str_gid]:
                    del all_bl[str_gid][str_uid]
                    with open(BLACKLIST_FILE, "w", encoding="utf-8") as f:
                        json.dump(all_bl, f, ensure_ascii=False, indent=2)
                    return True
        except Exception:
            pass
    return False

async def get_blacklist_users(guild_id: int) -> list[dict]:
    str_gid = str(guild_id)

    if blacklist_col is not None:
        try:
            cursor = await _db(lambda: list(blacklist_col.find({"guild_id": str_gid})))
            result = []
            for doc in cursor:
                result.append({
                    "user_id": int(doc["user_id"]),
                    "reason": doc.get("reason", "사유 미입력"),
                    "timestamp": doc.get("timestamp", "")
                })
            return result
        except Exception as e:
            print(f"MongoDB get_blacklist_users error: {e}", flush=True)

    _ensure_data_dir()
    async with _blacklist_lock:
        try:
            if os.path.exists(BLACKLIST_FILE):
                with open(BLACKLIST_FILE, "r", encoding="utf-8") as f:
                    all_bl = json.load(f)
                guild_bl = all_bl.get(str_gid, {})
                result = []
                for uid_str, info in guild_bl.items():
                    result.append({
                        "user_id": int(uid_str),
                        "reason": info.get("reason", "사유 미입력"),
                        "timestamp": info.get("timestamp", "")
                    })
                return result
        except Exception:
            pass
    return []


# ── Channel Boost Helper Functions ───────────────────────────────────

async def get_channel_boost(guild_id: int, channel_id: int) -> float:
    """채널의 현재 부스트 배율 반환 (없거나 만료되었으면 1.0)"""
    if not channel_id:
        return 1.0
    str_cid = str(channel_id)
    now = datetime.now(timezone.utc)

    # 이것도 메시지마다 호출됩니다. 설정 변경 시 무효화하므로 안전합니다.
    boost_info = _boost_cache.get(str_cid)
    cache_hit = boost_info is not None
    if cache_hit and boost_info == {}:
        boost_info = None

    if not cache_hit and boost_col is not None:
        try:
            boost_info = await _db(boost_col.find_one, {"_id": str_cid})
        except Exception:
            boost_info = None
        _boost_cache.set(str_cid, boost_info if boost_info else {})
    elif not cache_hit:
        _ensure_data_dir()
        async with _boost_lock:
            try:
                if os.path.exists(BOOST_FILE):
                    with open(BOOST_FILE, "r", encoding="utf-8") as f:
                        all_b = json.load(f)
                        boost_info = all_b.get(str_cid)
            except Exception:
                pass

    if not boost_info:
        return 1.0

    exp_str = boost_info.get("expires_at")
    if exp_str:
        try:
            exp_time = datetime.fromisoformat(exp_str)
            if now > exp_time:
                await set_channel_boost(guild_id, channel_id, 1.0, 0)
                return 1.0
        except Exception:
            pass

    return float(boost_info.get("multiplier", 1.0))


async def set_channel_boost(guild_id: int, channel_id: int, multiplier: float, duration_seconds: int = 0) -> None:
    str_cid = str(channel_id)
    str_gid = str(guild_id)
    _boost_cache.pop(str_cid)          # 캐시 무효화 — 설정이 즉시 반영됩니다

    expires_at = None
    if duration_seconds > 0:
        exp_time = datetime.now(timezone.utc) + timedelta(seconds=duration_seconds)
        expires_at = exp_time.isoformat()

    data_doc = {
        "_id": str_cid,
        "guild_id": str_gid,
        "channel_id": str_cid,
        "multiplier": multiplier,
        "expires_at": expires_at
    }

    if boost_col is not None:
        try:
            if multiplier <= 1.0:
                await _db(boost_col.delete_one, {"_id": str_cid})
            else:
                await _db(boost_col.replace_one, {"_id": str_cid}, data_doc, upsert=True)
            return
        except Exception as e:
            print(f"MongoDB set_channel_boost error: {e}", flush=True)

    _ensure_data_dir()
    async with _boost_lock:
        all_b = {}
        if os.path.exists(BOOST_FILE):
            try:
                with open(BOOST_FILE, "r", encoding="utf-8") as f:
                    all_b = json.load(f)
            except Exception:
                all_b = {}
        if multiplier <= 1.0:
            all_b.pop(str_cid, None)
        else:
            all_b[str_cid] = data_doc
        with open(BOOST_FILE, "w", encoding="utf-8") as f:
            json.dump(all_b, f, ensure_ascii=False, indent=2)


async def get_all_active_boosts(guild_id: int) -> list[dict]:
    str_gid = str(guild_id)
    now = datetime.now(timezone.utc)
    active = []

    raw_boosts = []
    if boost_col is not None:
        try:
            raw_boosts = await _db(lambda: list(boost_col.find({"guild_id": str_gid})))
        except Exception:
            pass
    else:
        _ensure_data_dir()
        async with _boost_lock:
            if os.path.exists(BOOST_FILE):
                try:
                    with open(BOOST_FILE, "r", encoding="utf-8") as f:
                        all_b = json.load(f)
                        raw_boosts = [v for v in all_b.values() if v.get("guild_id") == str_gid]
                except Exception:
                    pass

    for b in raw_boosts:
        exp_str = b.get("expires_at")
        if exp_str:
            try:
                exp_time = datetime.fromisoformat(exp_str)
                if now > exp_time:
                    continue
            except Exception:
                pass
        if float(b.get("multiplier", 1.0)) > 1.0:
            active.append(b)

    return active


async def check_and_clear_expired_boosts() -> list[dict]:
    """만료 시간이 지난 부스트를 DB에서 자동 정리하고 만료된 항목 목록 반환"""
    _boost_cache.clear()               # 만료 정리 후 캐시가 옛 배율을 주지 않도록
    now = datetime.now(timezone.utc)
    expired = []

    if boost_col is not None:
        try:
            # 5초마다 도는 백그라운드 루프입니다. 스레드로 넘겨
            # 이벤트 루프를 주기적으로 막지 않게 합니다.
            def _sweep():
                found = []
                for doc in boost_col.find({"expires_at": {"$ne": None}}):
                    exp_str = doc.get("expires_at")
                    if not exp_str:
                        continue
                    try:
                        if now >= datetime.fromisoformat(exp_str):
                            found.append(dict(doc))
                            boost_col.delete_one({"_id": doc["_id"]})
                    except Exception:
                        pass
                return found
            expired.extend(await _db(_sweep))
        except Exception as e:
            print(f"MongoDB check_and_clear_expired_boosts error: {e}", flush=True)
    else:
        _ensure_data_dir()
        async with _boost_lock:
            try:
                if os.path.exists(BOOST_FILE):
                    with open(BOOST_FILE, "r", encoding="utf-8") as f:
                        all_b = json.load(f)
                    to_remove = []
                    for cid, b in all_b.items():
                        exp_str = b.get("expires_at")
                        if exp_str:
                            try:
                                exp_time = datetime.fromisoformat(exp_str)
                                if now >= exp_time:
                                    expired.append(dict(b))
                                    to_remove.append(cid)
                            except Exception:
                                pass
                    for cid in to_remove:
                        all_b.pop(cid, None)
                    with open(BOOST_FILE, "w", encoding="utf-8") as f:
                        json.dump(all_b, f, ensure_ascii=False, indent=2)
            except Exception:
                pass

    return expired


# ── 포인트 스냅샷 (백업 / 복원) ────────────────────────────────────────
#
# 포인트는 이 봇의 존재 이유이므로, 로직 사고나 실수로 초기화됐을 때
# 되돌릴 수단이 반드시 있어야 합니다.
#
#   · 매일 1회 자동 스냅샷 (cogs/backup.py 의 백그라운드 작업)
#   · /백업설정 으로 수동 스냅샷 · 목록 · 복원 · 파일 내려받기
#
# ⚠️ 스냅샷은 같은 Atlas 클러스터 안에 있습니다. 로직 사고는 막아주지만
#    클러스터·계정 사고까지는 못 막으므로, 파일 내려받기를 함께 쓰세요.

SNAPSHOT_FILE = os.path.join(DATA_DIR, "point_snapshots.json")
_snapshot_lock = asyncio.Lock()
MAX_SNAPSHOTS = 30          # 길드당 보관 개수 (1회 약 40KB)


def _read_snapshots() -> dict:
    try:
        if os.path.exists(SNAPSHOT_FILE):
            with open(SNAPSHOT_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def _read_json(path: str) -> dict:
    """JSON 파일을 읽습니다. 없거나 깨졌으면 빈 dict."""
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def _write_json(path: str, payload: dict) -> None:
    _ensure_data_dir()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def _write_snapshots(payload: dict) -> None:
    _ensure_data_dir()
    with open(SNAPSHOT_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def _trim_snapshots(str_gid: str) -> None:
    """길드당 MAX_SNAPSHOTS 개만 남기고 오래된 것부터 삭제합니다."""
    if snapshot_col is None:
        return
    try:
        cursor = (snapshot_col.find({"guild_id": str_gid}, {"_id": 1})
                  .sort("_id", -1).skip(MAX_SNAPSHOTS).limit(1))
        cutoff = next(iter(cursor), None)
        if cutoff is None:
            return
        snapshot_col.delete_many({"guild_id": str_gid, "_id": {"$lte": cutoff["_id"]}})
    except Exception as e:
        print(f"MongoDB snapshot trim warning: {e}", flush=True)


async def create_point_snapshot(guild_id: int, reason: str = "manual") -> dict:
    """현재 포인트 전체를 스냅샷으로 저장하고 요약을 돌려줍니다.

    reason: "auto"(매일 자동) / "manual"(관리자) / "pre-restore"(복원 직전 자동)
    """
    str_gid = str(guild_id)
    now_kst = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")

    if users_col is not None and snapshot_col is not None:
        def _collect():
            out = {}
            for d in users_col.find({"guild_id": str_gid}, {"user_id": 1, "points": 1}):
                uid = d.get("user_id") or str(d["_id"]).split(":")[-1]
                out[str(uid)] = int(d.get("points", 0) or 0)
            return out
        try:
            users = await _db(_collect)
            doc = {
                "guild_id": str_gid,
                "created": datetime.now(timezone.utc),   # 정렬·조회용 실제 날짜
                "created_kst": now_kst,
                "reason": reason,
                "user_count": len(users),
                "total_points": sum(users.values()),
                "users": users,
            }
            res = await _db(snapshot_col.insert_one, doc)
            await asyncio.to_thread(_trim_snapshots, str_gid)
            return {"ok": True, "id": str(res.inserted_id), "created_kst": now_kst,
                    "reason": reason, "user_count": doc["user_count"],
                    "total_points": doc["total_points"]}
        except Exception as e:
            print(f"MongoDB create_point_snapshot error: {e}", flush=True)
            return {"ok": False, "error": str(e)}

    # 로컬 JSON 폴백
    data = await load_data()
    users = {}
    for key, info in data.items():
        if ":" in key:
            g, u = key.split(":", 1)
            if g == str_gid:
                users[u] = int(info.get("points", 0) or 0)
    entry = {"id": now_kst.replace(" ", "_").replace(":", ""), "created_kst": now_kst,
             "reason": reason, "user_count": len(users),
             "total_points": sum(users.values()), "users": users}
    async with _snapshot_lock:
        allsnap = _read_snapshots()
        bucket = allsnap.setdefault(str_gid, [])
        bucket.insert(0, entry)
        del bucket[MAX_SNAPSHOTS:]
        _write_snapshots(allsnap)
    return {"ok": True, "id": entry["id"], "created_kst": entry["created_kst"],
            "reason": entry["reason"], "user_count": entry["user_count"],
            "total_points": entry["total_points"]}


async def list_point_snapshots(guild_id: int, limit: int = 25) -> list[dict]:
    """최근 스냅샷 목록 (users 본문은 제외한 요약)."""
    str_gid = str(guild_id)
    if snapshot_col is not None:
        try:
            def _fetch():
                return list(snapshot_col.find(
                    {"guild_id": str_gid}, {"users": 0}).sort("_id", -1).limit(limit))
            return [{"id": str(d["_id"]), "created_kst": d.get("created_kst", "?"),
                     "reason": d.get("reason", "?"), "user_count": d.get("user_count", 0),
                     "total_points": d.get("total_points", 0)}
                    for d in await _db(_fetch)]
        except Exception as e:
            print(f"MongoDB list_point_snapshots error: {e}", flush=True)

    async with _snapshot_lock:
        allsnap = _read_snapshots()
    return [{k: e.get(k) for k in ("id", "created_kst", "reason", "user_count", "total_points")}
            for e in allsnap.get(str_gid, [])[:limit]]


async def get_point_snapshot(guild_id: int, snapshot_id: str) -> dict | None:
    """스냅샷 하나를 users 포함해 통째로 가져옵니다."""
    str_gid = str(guild_id)
    if snapshot_col is not None:
        try:
            from bson import ObjectId
            doc = await _db(snapshot_col.find_one,
                            {"_id": ObjectId(snapshot_id), "guild_id": str_gid})
            if doc:
                return {"id": str(doc["_id"]), "created_kst": doc.get("created_kst", "?"),
                        "reason": doc.get("reason", "?"), "users": doc.get("users", {}),
                        "user_count": doc.get("user_count", 0),
                        "total_points": doc.get("total_points", 0)}
            return None
        except Exception as e:
            print(f"MongoDB get_point_snapshot error: {e}", flush=True)
            return None

    async with _snapshot_lock:
        allsnap = _read_snapshots()
    for e in allsnap.get(str_gid, []):
        if e.get("id") == snapshot_id:
            return e
    return None


async def restore_point_snapshot(guild_id: int, snapshot_id: str) -> dict:
    """스냅샷 시점의 포인트로 되돌립니다.

    · 복원 직전에 **현재 상태를 자동으로 스냅샷**하므로 복원 자체도 되돌릴 수 있습니다.
    · 스냅샷에 없는 유저(이후 새로 들어온 유저)는 **건드리지 않습니다**.
      잘못 되돌려 새 유저의 정당한 적립을 지우는 일이 없도록 한 선택입니다.
    """
    snap = await get_point_snapshot(guild_id, snapshot_id)
    if not snap:
        return {"ok": False, "error": "해당 백업을 찾을 수 없습니다."}

    safety = await create_point_snapshot(guild_id, reason="pre-restore")
    str_gid = str(guild_id)
    users = snap.get("users", {})

    if users_col is not None:
        def _apply():
            # 한 명씩 update_one 을 돌리면 유저 수만큼 왕복이 생깁니다.
            # 838명 기준 약 9초가 걸려 복원 버튼이 한참 멈춰 보였습니다.
            # bulk_write 로 묶어 왕복을 수백 분의 1로 줄입니다.
            ops = [
                _UpdateOne(
                    {"_id": f"{str_gid}:{uid}"},
                    {"$set": {"points": int(pts),
                              "guild_id": str_gid, "user_id": str(uid)}},
                    upsert=True)
                for uid, pts in users.items()
            ]
            if not ops:
                return 0
            changed = 0
            CHUNK = 500          # 한 번에 너무 많이 보내지 않도록 나눠 전송
            for i in range(0, len(ops), CHUNK):
                res = users_col.bulk_write(ops[i:i + CHUNK], ordered=False)
                changed += res.modified_count + len(res.upserted_ids or {})
            return changed
        try:
            changed = await _db(_apply)
        except Exception as e:
            print(f"MongoDB restore_point_snapshot error: {e}", flush=True)
            return {"ok": False, "error": str(e)}
    else:
        data = await load_data()
        changed = 0
        for uid, pts in users.items():
            info = data.setdefault(f"{str_gid}:{uid}",
                                   {"points": 0, "last_checkin": "", "chat_count": 0})
            if info.get("points") != int(pts):
                info["points"] = int(pts)
                changed += 1
        await save_data(data)

    return {"ok": True, "restored": changed, "total_in_snapshot": len(users),
            "snapshot_kst": snap.get("created_kst", "?"),
            "safety_id": safety.get("id") if safety.get("ok") else None,
            "total_points": snap.get("total_points", 0)}


async def export_points_payload(guild_id: int) -> dict:
    """파일로 내려받을 백업 본문을 만듭니다 (현재 시점)."""
    str_gid = str(guild_id)
    now_kst = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")
    if users_col is not None:
        def _collect():
            return [{"user_id": d.get("user_id") or str(d["_id"]).split(":")[-1],
                     "points": int(d.get("points", 0) or 0),
                     "chat_count": int(d.get("chat_count", 0) or 0),
                     "last_checkin": d.get("last_checkin", "")}
                    for d in users_col.find({"guild_id": str_gid})]
        try:
            rows = await _db(_collect)
        except Exception as e:
            rows = []
            print(f"MongoDB export_points_payload error: {e}", flush=True)
    else:
        data = await load_data()
        rows = [{"user_id": k.split(":", 1)[1], "points": int(v.get("points", 0) or 0),
                 "chat_count": int(v.get("chat_count", 0) or 0),
                 "last_checkin": v.get("last_checkin", "")}
                for k, v in data.items() if k.startswith(f"{str_gid}:")]
    rows.sort(key=lambda r: r["points"], reverse=True)
    return {"taken_at_kst": now_kst, "guild_id": str_gid, "user_count": len(rows),
            "total_points": sum(r["points"] for r in rows), "users": rows}


# ── 전체 백업 (서버 밖 보관용) ─────────────────────────────────────────
#
# `create_point_snapshot()` 은 포인트만, 그것도 **같은 클러스터 안에** 담습니다.
# 클러스터나 계정 사고가 나면 스냅샷도 같이 사라지고, 설정(상점 구성 · 인증
# 조건 · 링크 보호)은 애초에 담기지도 않습니다.
#
# 그래서 DB 전체를 파일 하나로 만들어 디스코드 채널에 올려 둡니다.
# 채널에 올려두면 PC 고장 · Atlas 사고 어느 쪽에도 살아남습니다.
#
# 형식은 `tools/full_backup.py` 와 **동일**합니다. 채널에서 받은 파일을
# 그대로 `restore` 에 넣을 수 있어야 백업으로서 의미가 있습니다.

BACKUP_SET_FILE = os.path.join(DATA_DIR, "backup_settings.json")
_backup_set_lock = asyncio.Lock()

# 디스코드 첨부 한도(비부스트 25MB)에 여유를 두고, 넘으면 압축합니다.
FULL_BACKUP_GZIP_OVER = 8 * 1024 * 1024

# ⚠️ 백업에서 **제외**하는 컬렉션.
#
# `protected_links` 는 실측 180,212건 / 41MB 로 DB 전체(43MB)의 95% 입니다.
# 그런데 담기는 값은 **7일 TTL 이 걸린 메시지별 행**이라 되돌릴 가치가 거의
# 없습니다(그 메시지들은 복원 시점에 이미 사라졌거나 곧 만료됩니다).
#
# 문제는 크기 자체가 **무인 가동의 위험**이라는 것입니다. 서버 RAM 은 954MB
# (여유 430MB)인데, 매일 05:00 에 이걸 전부 파이썬 객체로 올려 직렬화하고
# 압축합니다. OOM 이 나면 아무도 없는 새벽에 봇이 죽습니다.
#
# 빼면 백업이 41MB → 2MB 가 되고, 압축도 필요 없어지고, 복원도 즉시 끝납니다.
# **무엇이 빠졌는지는 `_meta.excluded` 에 남겨 복원하는 사람이 알 수 있게 합니다.**
#
# `oauth_pending` 도 뺍니다 — 30분짜리 로그인 대기라 되돌릴 가치가 없고,
# 백업 파일에 PKCE verifier 가 실릴 이유도 없습니다.
FULL_BACKUP_EXCLUDE = ("protected_links", "oauth_pending")


def _encode_bson(o):
    """ObjectId·datetime 을 타입 보존 형태로 직렬화합니다.

    문자열로 뭉개면 복원했을 때 타입이 달라져 TTL 인덱스(BSON Date 필요)가
    조용히 죽습니다. tools/full_backup.py 와 같은 규칙을 씁니다.
    """
    name = type(o).__name__
    if name == "ObjectId":
        return {"$oid": str(o)}
    if isinstance(o, datetime):
        return {"$date": o.isoformat()}
    raise TypeError(f"직렬화할 수 없는 타입: {type(o)}")


async def export_full_payload() -> dict:
    """DB 전체를 파일 본문으로 만듭니다 (모든 컬렉션).

    포인트 시스템과 인증 시스템은 코드가 분리돼 있지만 **같은 DB**에 삽니다.
    백업은 둘 다 담아야 의미가 있으므로, 여기서는 컬렉션 이름을 DB 에서
    직접 읽어옵니다. (verify_db 를 import 하지 않습니다 — 경계 유지)
    """
    now_kst = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")

    failed = None
    if mongo_db is not None:
        def _collect():
            out = {}
            for name in sorted(mongo_db.list_collection_names()):
                if name in FULL_BACKUP_EXCLUDE:
                    continue
                out[name] = list(mongo_db[name].find())
            return out
        try:
            data = await _db(_collect)
        except Exception as e:
            print(f"MongoDB export_full_payload error: {e}", flush=True)
            data = {}
            failed = f"{type(e).__name__}: {e}"
    else:
        # 폴백 모드에서는 data/ 안의 JSON 을 그대로 담습니다.
        def _collect_files():
            out = {}
            if os.path.isdir(DATA_DIR):
                for fn in sorted(os.listdir(DATA_DIR)):
                    if not fn.endswith(".json"):
                        continue
                    try:
                        with open(os.path.join(DATA_DIR, fn), "r", encoding="utf-8") as f:
                            out[fn[:-5]] = json.load(f)
                    except Exception:
                        continue
            return out
        data = await asyncio.to_thread(_collect_files)

    counts = {k: (len(v) if isinstance(v, (list, dict)) else 0) for k, v in data.items()}
    meta = {
        "database": "krbiomehuntbot",
        "created_kst": now_kst,
        "collections": counts,
        "storage_mode": STORAGE_MODE,
        "excluded": list(FULL_BACKUP_EXCLUDE),
        "note": "전체 덤프 — 유저 ID·포인트 포함. 공개 채널에 올리지 마세요.",
    }
    # ⚠️ 수집이 실패했으면 **반드시 표시합니다.** 예전에는 조용히 빈 dict 가
    # 되어, 내용이 0건인 파일이 '전체 백업' 으로 올라가고 마지막 백업 시각까지
    # 갱신됐습니다. 무인 운영에서는 그게 며칠 뒤 복구를 시도할 때 드러납니다.
    if failed:
        meta["failed"] = failed
    return {"_meta": meta, "data": data}


def full_backup_bytes(payload: dict) -> tuple[bytes, str]:
    """본문을 파일 바이트와 파일명으로 만듭니다. 크면 gzip 으로 압축합니다."""
    raw = json.dumps(payload, ensure_ascii=False, indent=1,
                     default=_encode_bson).encode("utf-8")
    stamp = payload["_meta"]["created_kst"].replace("-", "").replace(" ", "_").replace(":", "")
    if len(raw) > FULL_BACKUP_GZIP_OVER:
        import gzip
        return gzip.compress(raw, 6), f"full_{stamp}.json.gz"
    return raw, f"full_{stamp}.json"


async def get_backup_channel(guild_id: int) -> int | None:
    """전체 백업을 올릴 채널 ID. 설정 안 됐으면 None."""
    if backup_set_col is not None:
        try:
            doc = await _db(backup_set_col.find_one, {"_id": str(guild_id)})
            return int(doc["channel_id"]) if doc and doc.get("channel_id") else None
        except Exception as e:
            print(f"MongoDB get_backup_channel error: {e}, using local file.", flush=True)
    async with _backup_set_lock:
        try:
            if os.path.exists(BACKUP_SET_FILE):
                with open(BACKUP_SET_FILE, "r", encoding="utf-8") as f:
                    got = json.load(f).get(str(guild_id), {}).get("channel_id")
                    return int(got) if got else None
        except Exception:
            pass
    return None


async def save_backup_channel(guild_id: int, channel_id: int | None) -> None:
    """전체 백업을 올릴 채널을 지정합니다. None 이면 해제."""
    value = str(channel_id) if channel_id else None
    if backup_set_col is not None:
        try:
            await _db(backup_set_col.update_one, {"_id": str(guild_id)},
                      {"$set": {"channel_id": value}}, upsert=True)
            return
        except Exception as e:
            print(f"MongoDB save_backup_channel error: {e}, using local file.", flush=True)
    async with _backup_set_lock:
        _ensure_data_dir()
        try:
            with open(BACKUP_SET_FILE, "r", encoding="utf-8") as f:
                allset = json.load(f)
        except Exception:
            allset = {}
        allset.setdefault(str(guild_id), {})["channel_id"] = value
        with open(BACKUP_SET_FILE, "w", encoding="utf-8") as f:
            json.dump(allset, f, ensure_ascii=False, indent=2)


# ── 봇 상태 알림 ──────────────────────────────────────────────────────
#
# UptimeRobot 은 **밖에서** 응답 여부만 봅니다. 포인트가 임시 저장소에
# 쌓이고 있는지, 기능 일부가 로드에 실패했는지, 재시작을 반복하는지는
# 밖에서 알 수 없습니다. 그래서 봇이 직접 채널에 남깁니다.

async def get_status_channel(guild_id: int) -> int | None:
    """봇 상태 알림을 보낼 채널. 따로 없으면 전체 백업 채널을 씁니다."""
    if backup_set_col is not None:
        try:
            doc = await _db(backup_set_col.find_one, {"_id": str(guild_id)})
            if doc:
                got = doc.get("status_channel_id") or doc.get("channel_id")
                return int(got) if got else None
            return None
        except Exception as e:
            print(f"MongoDB get_status_channel error: {e}, using local file.", flush=True)
    async with _backup_set_lock:
        try:
            if os.path.exists(BACKUP_SET_FILE):
                with open(BACKUP_SET_FILE, "r", encoding="utf-8") as f:
                    row = json.load(f).get(str(guild_id), {})
                    got = row.get("status_channel_id") or row.get("channel_id")
                    return int(got) if got else None
        except Exception:
            pass
    return None


async def save_status_channel(guild_id: int, channel_id: int | None) -> None:
    value = str(channel_id) if channel_id else None
    if backup_set_col is not None:
        try:
            await _db(backup_set_col.update_one, {"_id": str(guild_id)},
                      {"$set": {"status_channel_id": value}}, upsert=True)
            return
        except Exception as e:
            print(f"MongoDB save_status_channel error: {e}, using local file.", flush=True)
    async with _backup_set_lock:
        _ensure_data_dir()
        try:
            with open(BACKUP_SET_FILE, "r", encoding="utf-8") as f:
                allset = json.load(f)
        except Exception:
            allset = {}
        allset.setdefault(str(guild_id), {})["status_channel_id"] = value
        with open(BACKUP_SET_FILE, "w", encoding="utf-8") as f:
            json.dump(allset, f, ensure_ascii=False, indent=2)


def _git_head() -> str:
    """작업 폴더의 `.git` 에서 현재 커밋 해시를 직접 읽습니다.

    `git` 명령을 부르지 않습니다 — 기동 경로라 하위 프로세스를 띄우고 싶지
    않고, 파일 두 개만 읽으면 되기 때문입니다.
    """
    try:
        with open(os.path.join(".git", "HEAD"), "r", encoding="utf-8") as f:
            head = f.read().strip()
    except Exception:
        return ""

    if not head.startswith("ref:"):
        return head[:12]        # 분리된 HEAD — 해시가 그대로 들어 있습니다

    ref = head[4:].strip()
    try:
        with open(os.path.join(".git", *ref.split("/")), "r", encoding="utf-8") as f:
            return f.read().strip()[:12]
    except Exception:
        pass
    # 느슨한 ref 파일이 없으면 packed-refs 를 뒤집니다 (git gc 후에 이렇게 됩니다)
    try:
        with open(os.path.join(".git", "packed-refs"), "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("#"):
                    continue
                parts = line.split()
                if len(parts) == 2 and parts[1] == ref:
                    return parts[0][:12]
    except Exception:
        pass
    return ""


def current_version() -> str:
    """지금 돌고 있는 코드의 버전(커밋 해시). 알 수 없으면 빈 문자열.

    이 값이 있어야 **재배포로 인한 재시작**과 **같은 코드가 계속 죽는
    크래시 루프**를 구분할 수 있습니다. 둘은 대응이 정반대라 섞이면 안 됩니다.

    ⚠️ Render 시절에는 `RENDER_GIT_COMMIT` 하나만 봤습니다. Oracle 로 옮긴
    뒤 그 변수가 사라져 **항상 빈 문자열**이 되었고, 크래시 루프 경보가
    조용히 죽어 있었습니다. 무인 운영에서는 그 경보가 유일한 신호이므로,
    호스팅에 기대지 않고 `.git` 에서 직접 읽습니다.
    """
    for key in ("RENDER_GIT_COMMIT", "GIT_COMMIT", "SOURCE_VERSION"):
        v = (os.getenv(key) or "").strip()
        if v:
            return v[:12]
    return _git_head()


async def record_boot(guild_id: int) -> dict:
    """기동 시각을 남기고, 직전 기동과의 간격을 돌려줍니다.

    재시작 **루프**는 무인 운영에서 가장 알아채기 어려운 고장입니다.
    밖에서 보면 응답이 잠깐씩 되기 때문에 감시 서비스도 정상으로 봅니다.
    직전 기동이 얼마 전이었는지 알면 루프를 바로 알 수 있습니다.
    """
    now = datetime.now(KST)
    now_s = now.strftime("%Y-%m-%d %H:%M:%S")
    previous = None

    if backup_set_col is not None:
        try:
            # 기동 이력을 최근 8개만 남깁니다. 임베드 하나에 다 보여주면서도
            # 재시작이 몰렸는지 한눈에 보이는 정도의 길이입니다.
            doc = await _db(backup_set_col.find_one_and_update,
                            {"_id": str(guild_id)},
                            {"$set": {"last_boot_kst": now_s},
                             "$inc": {"boot_count": 1},
                             "$push": {"boot_history": {
                                 "$each": [{"at": now_s, "v": current_version()}],
                                 "$slice": -8}}},
                            upsert=True)
            previous = (doc or {}).get("last_boot_kst")
        except Exception as e:
            print(f"MongoDB record_boot error: {e}, using local file.", flush=True)
    else:
        async with _backup_set_lock:
            _ensure_data_dir()
            try:
                with open(BACKUP_SET_FILE, "r", encoding="utf-8") as f:
                    allset = json.load(f)
            except Exception:
                allset = {}
            row = allset.setdefault(str(guild_id), {})
            previous = row.get("last_boot_kst")
            row["last_boot_kst"] = now_s
            row["boot_count"] = int(row.get("boot_count", 0)) + 1
            hist = list(row.get("boot_history", []))[-7:]
            hist.append({"at": now_s, "v": current_version()})
            row["boot_history"] = hist
            with open(BACKUP_SET_FILE, "w", encoding="utf-8") as f:
                json.dump(allset, f, ensure_ascii=False, indent=2)

    gap_minutes = None
    if previous:
        try:
            prev_dt = datetime.strptime(previous, "%Y-%m-%d %H:%M:%S").replace(tzinfo=KST)
            gap_minutes = (now - prev_dt).total_seconds() / 60
        except (ValueError, TypeError):
            gap_minutes = None

    return {"now_kst": now_s, "previous_kst": previous, "gap_minutes": gap_minutes}


async def get_status_state(guild_id: int) -> dict:
    """상태 임베드에 필요한 값들 (메시지 ID · 기동 이력 · 누적 횟수)."""
    if backup_set_col is not None:
        try:
            doc = await _db(backup_set_col.find_one, {"_id": str(guild_id)}) or {}
        except Exception as e:
            print(f"MongoDB get_status_state error: {e}, using local file.", flush=True)
            doc = {}
    else:
        doc = {}
        async with _backup_set_lock:
            try:
                if os.path.exists(BACKUP_SET_FILE):
                    with open(BACKUP_SET_FILE, "r", encoding="utf-8") as f:
                        doc = json.load(f).get(str(guild_id), {})
            except Exception:
                pass
    mid = doc.get("status_message_id")
    return {
        "message_id": int(mid) if mid else None,
        "boot_history": list(doc.get("boot_history", [])),
        "boot_count": int(doc.get("boot_count", 0) or 0),
        "last_boot_kst": doc.get("last_boot_kst"),
        "last_full_backup_kst": doc.get("last_full_backup_kst"),
    }


async def set_status_message(guild_id: int, message_id: int | None) -> None:
    """상태 임베드의 메시지 ID를 기억합니다.

    링크 보호가 쓰는 방식과 같습니다 — custom_id 에 값을 넣지 않고,
    **메시지 ID를 키로 DB를 찾습니다.** 봇이 재시작해도 같은 메시지를
    계속 수정할 수 있어야 채널이 알림으로 도배되지 않습니다.
    """
    value = str(message_id) if message_id else None
    if backup_set_col is not None:
        try:
            await _db(backup_set_col.update_one, {"_id": str(guild_id)},
                      {"$set": {"status_message_id": value}}, upsert=True)
            return
        except Exception as e:
            print(f"MongoDB set_status_message error: {e}, using local file.", flush=True)
    async with _backup_set_lock:
        _ensure_data_dir()
        try:
            with open(BACKUP_SET_FILE, "r", encoding="utf-8") as f:
                allset = json.load(f)
        except Exception:
            allset = {}
        allset.setdefault(str(guild_id), {})["status_message_id"] = value
        with open(BACKUP_SET_FILE, "w", encoding="utf-8") as f:
            json.dump(allset, f, ensure_ascii=False, indent=2)


async def mark_full_backup(guild_id: int, stamp: str) -> None:
    """마지막 전체 백업 시각을 기록합니다 (상태 임베드에 표시)."""
    if backup_set_col is not None:
        try:
            await _db(backup_set_col.update_one, {"_id": str(guild_id)},
                      {"$set": {"last_full_backup_kst": stamp}}, upsert=True)
            return
        except Exception as e:
            print(f"MongoDB mark_full_backup error: {e}, using local file.", flush=True)
    async with _backup_set_lock:
        _ensure_data_dir()
        try:
            with open(BACKUP_SET_FILE, "r", encoding="utf-8") as f:
                allset = json.load(f)
        except Exception:
            allset = {}
        allset.setdefault(str(guild_id), {})["last_full_backup_kst"] = stamp
        with open(BACKUP_SET_FILE, "w", encoding="utf-8") as f:
            json.dump(allset, f, ensure_ascii=False, indent=2)


# ── 연속 출석 ─────────────────────────────────────────────────────────
#
# 채팅과 출석만으로는 포인트가 잘 모이지 않아 수급처를 넓히는 작업입니다.
# 연속으로 나올수록 더 주되, **수치는 전부 설정값**입니다. 운영진이
# `/출석설정` 으로 직접 조정하므로 코드에 박아두면 안 됩니다.

CHECKIN_SET_FILE = os.path.join(DATA_DIR, "checkin_settings.json")
_checkin_set_lock = asyncio.Lock()

DEFAULT_CHECKIN_SETTINGS = {
    "base": 100,          # 매일 기본 지급량
    # 며칠마다 추가 보상을 줄지. 7이면 7·14·21일째에만 붙습니다.
    "cycle": 7,
    # 주차마다 커지는 폭. 1주차 +100, 2주차 +200, 3주차 +300 …
    # 매일 조금씩 얹는 방식이 아니라, **주기 달성일에 한 번** 크게 줍니다.
    "cycle_step": 100,
    "cycle_max": 0,       # 추가 보상 상한 (0 = 무제한)
    # 특정 연속일수에 한 번만 주는 특별 보상 (선택). 키는 문자열(일수).
    "milestones": {},
}


def get_yesterday_str() -> str:
    return (datetime.now(KST) - timedelta(days=1)).strftime("%Y-%m-%d")


def streak_reward(streak: int, settings: dict) -> tuple[int, int, int]:
    """연속일수에 대한 보상을 계산합니다.

    반환값은 (기본, 주기보상, 특별보상) 입니다. 나눠서 돌려주는 이유는
    유저에게 "왜 이만큼 받았는지"를 그대로 보여주기 위해서입니다.

    주기 보상은 **주기 달성일에만** 붙고, 주차가 올라갈수록 커집니다.
    cycle=7, cycle_step=100 이면:

        1~6일   100        (기본만)
        7일     100 + 100  = 200   ← 1주차
        8~13일  100
        14일    100 + 200  = 300   ← 2주차
        21일    100 + 300  = 400   ← 3주차

    매일 조금씩 얹는 방식보다 '일주일을 채웠다'는 감각이 분명합니다.
    """
    base = int(settings.get("base", 100))
    cycle = int(settings.get("cycle", 0) or 0)
    step = int(settings.get("cycle_step", 0) or 0)
    cap = int(settings.get("cycle_max", 0) or 0)

    bonus = 0
    if cycle > 0 and step > 0 and streak > 0 and streak % cycle == 0:
        bonus = (streak // cycle) * step
        if cap > 0:
            bonus = min(bonus, cap)

    milestone = int((settings.get("milestones") or {}).get(str(streak), 0) or 0)
    return base, bonus, milestone


def next_cycle_day(streak: int, settings: dict) -> tuple[int, int] | None:
    """다음 주기 달성일과 그때 받을 추가 보상. 주기를 안 쓰면 None."""
    cycle = int(settings.get("cycle", 0) or 0)
    step = int(settings.get("cycle_step", 0) or 0)
    cap = int(settings.get("cycle_max", 0) or 0)
    if cycle <= 0 or step <= 0:
        return None
    day = ((streak // cycle) + 1) * cycle
    reward = (day // cycle) * step
    if cap > 0:
        reward = min(reward, cap)
    return day, reward


async def get_checkin_settings(guild_id: int) -> dict:
    merged = dict(DEFAULT_CHECKIN_SETTINGS)
    if checkin_set_col is not None:
        try:
            doc = await _db(checkin_set_col.find_one, {"_id": str(guild_id)})
            if doc:
                merged.update({k: v for k, v in doc.items() if k != "_id"})
            return merged
        except Exception as e:
            print(f"MongoDB get_checkin_settings error: {e}, using local file.", flush=True)
    async with _checkin_set_lock:
        try:
            if os.path.exists(CHECKIN_SET_FILE):
                with open(CHECKIN_SET_FILE, "r", encoding="utf-8") as f:
                    merged.update(json.load(f).get(str(guild_id), {}))
        except Exception:
            pass
    return merged


async def save_checkin_settings(guild_id: int, patch: dict) -> None:
    if checkin_set_col is not None:
        try:
            await _db(checkin_set_col.update_one, {"_id": str(guild_id)},
                      {"$set": patch}, upsert=True)
            return
        except Exception as e:
            print(f"MongoDB save_checkin_settings error: {e}, using local file.", flush=True)
    async with _checkin_set_lock:
        _ensure_data_dir()
        try:
            with open(CHECKIN_SET_FILE, "r", encoding="utf-8") as f:
                allset = json.load(f)
        except Exception:
            allset = {}
        allset.setdefault(str(guild_id), {}).update(patch)
        with open(CHECKIN_SET_FILE, "w", encoding="utf-8") as f:
            json.dump(allset, f, ensure_ascii=False, indent=2)


# ── 퀘스트 ────────────────────────────────────────────────────────────
#
# 채팅·출석만으로는 포인트가 잘 모이지 않아 만든 수급처입니다.
# 미리 만들어 둔 풀에서 **매일 무작위로 몇 개를 뽑아** 제시합니다.
# 고정 목록이면 며칠 만에 지루해지기 때문입니다.
#
# 진행도는 전부 `$inc` 로만 올립니다. 읽고 계산해서 쓰면 동시 요청끼리
# 서로를 지웁니다(포인트와 같은 이유). 보상 수령도 '아직 안 받음'을
# **필터 조건**에 넣어 서버 측에서 판정합니다.

QUEST_SET_FILE = os.path.join(DATA_DIR, "quest_settings.json")
QUEST_PROG_FILE = os.path.join(DATA_DIR, "quest_progress.json")
QUEST_STATE_FILE = os.path.join(DATA_DIR, "quest_state.json")
_quest_lock = asyncio.Lock()

# 퀘스트 종류. key 는 저장값이라 바꾸면 기존 데이터와 어긋납니다.
QUEST_KINDS = {
    "chat":        {"label": "채팅 {n}회 보내기",           "unit": "회", "period": "daily"},
    "checkin":     {"label": "출석하기",                    "unit": "회", "period": "daily"},
    "channel_chat":{"label": "{ch} 에서 {n}회 대화하기", "unit": "회", "period": "daily"},
    "gacha":       {"label": "뽑기 {n}회 돌리기",           "unit": "회", "period": "daily"},
    "early_bird":  {"label": "출석 선착순 {n}등 안에 들기",  "unit": "등", "period": "daily"},
    "week_checkin":{"label": "이번 주 {n}일 출석하기",       "unit": "일", "period": "weekly"},
    "week_chat":   {"label": "이번 주 채팅 {n}회 보내기",    "unit": "회", "period": "weekly"},
    "week_gacha":  {"label": "이번 주 뽑기 {n}회 돌리기",    "unit": "회", "period": "weekly"},
    "week_channel":{"label": "이번 주 {ch} 에서 {n}회 대화", "unit": "회", "period": "weekly"},
}

DEFAULT_QUEST_SETTINGS = {
    "enabled": False,          # 만들어만 두고 켜는 건 운영진이 결정합니다
    # 켜면 **관리자만** /퀘스트 를 쓸 수 있습니다. 공개 전에 관리자끼리
    # 돌려보기 위한 것으로, 유저에게는 '아직 열리지 않았습니다'로 보입니다.
    "admin_only": False,
    "daily_count": 3,          # 매일 몇 개를 뽑을지
    "weekly_count": 2,         # 주마다 몇 개를 뽑을지
    "daily_reward": 150,       # 일일 퀘스트 1개당 보상
    "weekly_reward": 800,      # 주간 퀘스트 1개당 보상
    # 랜덤 채널 퀘스트 후보. 유저마다 이 중 하나가 무작위로 배정됩니다.
    # 하나만 넣으면 모두 같은 채널을 받습니다(예전 동작).
    "channel_ids": [],
    "channel_id": None,        # (구버전) 단일 채널 — channel_ids 가 비면 이걸 씁니다
    # 종류별 목표치. 슬로우모드 5초 기준으로 잡은 기본값입니다.
    "goals": {"chat": 20, "channel_chat": 10, "gacha": 2, "early_bird": 20,
              "week_checkin": 5, "week_chat": 150, "week_gacha": 10,
              "week_channel": 60},
    # 풀에서 제외할 종류 (운영진이 끄고 싶을 때)
    "disabled_kinds": [],
    # 서버 공동 목표
    "goal_enabled": False,
    "goal_target": 2000,       # 서버 전체 채팅 수
    "goal_reward": 300,        # 참여자 전원에게
    # 보상을 받으려면 목표치의 몇 %를 본인이 채워야 하는지.
    # 목표 3,000회 · 2% 면 본인이 60회는 쳐야 합니다. 아무것도 안 하고
    # 받아가는 무임승차를 막습니다. 0 이면 조건 없음.
    "goal_min_percent": 2,
    # 목표 달성 후 뒤늦게 참여한 유저(늦참자)에게 요구할 최소 참여 %.
    # 0이면 달성 후에도 goal_min_percent와 동일하게 적용합니다.
    "goal_late_min_percent": 4,
    "goal_channel_id": None,   # 진행도를 보여줄 채널
    "goal_reset_days": 1,      # 며칠마다 초기화할지 (기본 1일 = 매일)
}


def quest_day(when: datetime | None = None) -> str:
    return (when or datetime.now(KST)).strftime("%Y-%m-%d")


def quest_week(when: datetime | None = None) -> str:
    """주차 키. **일요일 00:00(KST) 에 새 주가 시작**됩니다.

    파이썬의 `isocalendar()` 는 월요일 시작이라 그대로 쓰면 주간 퀘스트가
    월요일에 초기화됩니다. 하루를 더해 ISO 기준을 밀어내면 일요일~토요일이
    한 묶음이 됩니다.

        일요일 -> +1일 = 월요일 -> 새 ISO 주  (여기서 주가 바뀝니다)
        토요일 -> +1일 = 일요일 -> 같은 ISO 주 (아직 같은 주)
    """
    d = (when or datetime.now(KST)) + timedelta(days=1)
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def quest_goal_period(reset_days: int, when: datetime | None = None) -> str:
    """공동 목표 초기화 주기에 따른 키를 생성합니다.
    reset_days가 1 이하면 매일 초기화와 동일합니다.
    """
    if reset_days <= 1:
        return quest_day(when)
    
    # 2024년 1월 1일을 기준일로 삼아 경과 일수를 계산합니다.
    # 이렇게 하면 서버 시작일과 무관하게 모든 서버가 같은 주기를 공유하게 됩니다.
    anchor = datetime(2024, 1, 1, tzinfo=KST)
    d = when or datetime.now(KST)
    days_since = (d.date() - anchor.date()).days
    
    # 예: reset_days가 3이면, 0~2일은 0, 3~5일은 1이 됩니다.
    period_idx = days_since // reset_days
    
    return f"G{reset_days}D-{period_idx}"


def _quest_key(guild_id: int, user_id: int, period_key: str) -> str:
    return f"{guild_id}:{user_id}:{period_key}"


async def get_quest_settings(guild_id: int) -> dict:
    cached = _quest_settings_cache.get(str(guild_id))
    if cached is not None:
        return copy.deepcopy(cached)
    merged = copy.deepcopy(DEFAULT_QUEST_SETTINGS)
    if quest_set_col is not None:
        try:
            doc = await _db(quest_set_col.find_one, {"_id": str(guild_id)})
            if doc:
                for k, v in doc.items():
                    if k == "_id":
                        continue
                    if k == "goals" and isinstance(v, dict):
                        merged["goals"].update(v)
                    else:
                        merged[k] = v
            _quest_settings_cache.set(str(guild_id), merged)
            return copy.deepcopy(merged)
        except Exception as e:
            print(f"MongoDB get_quest_settings error: {e}, using local file.", flush=True)
    async with _quest_lock:
        try:
            if os.path.exists(QUEST_SET_FILE):
                with open(QUEST_SET_FILE, "r", encoding="utf-8") as f:
                    row = json.load(f).get(str(guild_id), {})
                for k, v in row.items():
                    if k == "goals" and isinstance(v, dict):
                        merged["goals"].update(v)
                    else:
                        merged[k] = v
        except Exception:
            pass
    _quest_settings_cache.set(str(guild_id), merged)
    return copy.deepcopy(merged)


async def save_quest_settings(guild_id: int, patch: dict) -> None:
    _quest_settings_cache.pop(str(guild_id))   # 설정 변경을 즉시 반영
    if quest_set_col is not None:
        try:
            await _db(quest_set_col.update_one, {"_id": str(guild_id)},
                      {"$set": patch}, upsert=True)
            return
        except Exception as e:
            print(f"MongoDB save_quest_settings error: {e}, using local file.", flush=True)
    async with _quest_lock:
        _ensure_data_dir()
        try:
            with open(QUEST_SET_FILE, "r", encoding="utf-8") as f:
                allset = json.load(f)
        except Exception:
            allset = {}
        row = allset.setdefault(str(guild_id), {})
        for k, v in patch.items():
            if k == "goals" and isinstance(v, dict):
                row.setdefault("goals", {}).update(v)
            else:
                row[k] = v
        with open(QUEST_SET_FILE, "w", encoding="utf-8") as f:
            json.dump(allset, f, ensure_ascii=False, indent=2)


async def bump_quest_progress(guild_id: int, user_id: int, field: str,
                              amount: int = 1, weekly: bool = False,
                              period: str | None = None) -> None:
    """진행도를 원자적으로 올립니다.

    ⚠️ 반드시 `$inc` 여야 합니다. 읽고 계산해서 쓰면 채팅이 몰릴 때
    서로를 지워 집계가 뭉개집니다(사용 인원이 80명에서 1명으로 뭉개진 전례).
    """
    key = _quest_key(guild_id, user_id,
                     period or (quest_week() if weekly else quest_day()))
    if quest_prog_col is not None:
        try:
            upd = {"$inc": {f"p.{field}": amount},
                   "$setOnInsert": {"guild_id": str(guild_id),
                                    "user_id": str(user_id),
                                    "period": key.rsplit(":", 1)[-1]}}
            if period and period.startswith("E"):
                # 이벤트는 여러 날에 걸칩니다. created 를 고정하면 이벤트가
                # 끝나기 전에 TTL 이 문서를 지웁니다. 활동할 때마다 갱신해
                # **마지막 활동으로부터** 21일 뒤에 지워지게 합니다.
                upd["$set"] = {"created": datetime.now(timezone.utc)}
            else:
                upd["$setOnInsert"]["created"] = datetime.now(timezone.utc)
            await _db(quest_prog_col.update_one, {"_id": key}, upd, upsert=True)
            return
        except Exception as e:
            print(f"MongoDB bump_quest_progress error: {e}, using local file.", flush=True)
    async with _quest_lock:
        _ensure_data_dir()
        try:
            with open(QUEST_PROG_FILE, "r", encoding="utf-8") as f:
                allp = json.load(f)
        except Exception:
            allp = {}
        row = allp.setdefault(key, {"p": {}, "claimed": []})
        row.setdefault("p", {})[field] = int(row.get("p", {}).get(field, 0)) + amount
        _trim_quest_progress_local(allp)
        with open(QUEST_PROG_FILE, "w", encoding="utf-8") as f:
            json.dump(allp, f, ensure_ascii=False)


def _trim_quest_progress_local(allp: dict) -> None:
    """오래된 진행도를 지웁니다 (JSON 폴백 경로).

    Mongo 경로는 TTL 인덱스가 지웁니다. **양쪽 모두에 상한을 걸어야**
    합니다 — 한쪽만 걸어 영구 누적된 전례가 있습니다.
    """
    cutoff = (datetime.now(KST) - timedelta(days=QUEST_PROGRESS_TTL_DAYS))
    cut_day = cutoff.strftime("%Y-%m-%d")
    cut_week = quest_week(cutoff)
    for k in [k for k in allp]:
        period = k.rsplit(":", 1)[-1]
        if "W" in period:
            if period < cut_week:
                allp.pop(k, None)
        elif period < cut_day:
            allp.pop(k, None)


async def get_quest_progress(guild_id: int, user_id: int,
                            periods: list[str] | None = None) -> dict:
    """오늘/이번 주(+지정한 기간) 진행도와 수령 내역을 한 번에 돌려줍니다."""
    out = {"p": {}, "claimed": []}
    keys = [_quest_key(guild_id, user_id, quest_day()),
            _quest_key(guild_id, user_id, quest_week())]
    for extra in (periods or []):
        keys.append(_quest_key(guild_id, user_id, extra))
    if quest_prog_col is not None:
        try:
            def _fetch():
                return list(quest_prog_col.find({"_id": {"$in": keys}}))
            for doc in await _db(_fetch):
                out["p"].update(doc.get("p", {}) or {})
                out["claimed"] += list(doc.get("claimed", []) or [])
            return out
        except Exception as e:
            print(f"MongoDB get_quest_progress error: {e}, using local file.", flush=True)
    async with _quest_lock:
        try:
            with open(QUEST_PROG_FILE, "r", encoding="utf-8") as f:
                allp = json.load(f)
        except Exception:
            allp = {}
    for k in keys:
        row = allp.get(k) or {}
        out["p"].update(row.get("p", {}) or {})
        out["claimed"] += list(row.get("claimed", []) or [])
    return out


async def claim_quest(guild_id: int, user_id: int, quest_id: str,
                      reward: int, weekly: bool = False,
                      period: str | None = None, out: dict | None = None) -> bool:
    """보상을 지급합니다. 이미 받았으면 False (포인트는 나가지 않습니다).

    '아직 안 받음'을 **필터 조건**에 넣어 서버 측에서 판정합니다.
    버튼을 연타해도 두 번 지급되지 않습니다.

    호박 보상 배율이 걸려 있으면 곱해서 주고, 같은 양을 시즌 포인트로도 줍니다.
    실제로 준 양은 `out` 에 담습니다(화면에 설정값 그대로 쓰면 2배가 안 보입니다).
    """
    async def _pay():
        mult = await point_multiplier(guild_id)
        paid = apply_multiplier(int(reward), mult)
        if paid > 0:
            await add_user_points(guild_id, user_id, paid)
        season = await credit_earned(guild_id, user_id, paid)
        if out is not None:
            out.update(paid=paid, mult=mult, season=season)
    key = _quest_key(guild_id, user_id,
                     period or (quest_week() if weekly else quest_day()))
    if quest_prog_col is not None:
        try:
            doc = await _db(quest_prog_col.find_one_and_update,
                            {"_id": key, "claimed": {"$ne": quest_id}},
                            {"$push": {"claimed": quest_id},
                             "$setOnInsert": {"guild_id": str(guild_id),
                                              "user_id": str(user_id),
                                              "created": datetime.now(timezone.utc)}},
                            upsert=True, return_document=_AFTER)
            if doc is None:
                return False
        except _DuplicateKey:
            # 이미 받은 뒤 한 번 더 누른 경우입니다. 필터에 안 걸려 삽입으로
            # 넘어갔고 같은 _id 라 중복 키가 났습니다. **의도된 동작**이므로
            # 조용히 넘깁니다. 여기서 로그를 남기면 유저가 버튼을 두 번 누를
            # 때마다 'DB 오류'처럼 보이는 줄이 쌓입니다. 무인 운영에서 진짜
            # 장애를 찾을 때 이런 줄이 제일 방해가 됩니다.
            return False
        except Exception as e:
            # 여기까지 온 것은 진짜 이상입니다. 포인트 보호를 우선해 막습니다.
            print(f"MongoDB claim_quest error: {e}", flush=True)
            return False
        await _pay()
        return True

    async with _quest_lock:
        _ensure_data_dir()
        try:
            with open(QUEST_PROG_FILE, "r", encoding="utf-8") as f:
                allp = json.load(f)
        except Exception:
            allp = {}
        row = allp.setdefault(key, {"p": {}, "claimed": []})
        if quest_id in row.get("claimed", []):
            return False
        row.setdefault("claimed", []).append(quest_id)
        with open(QUEST_PROG_FILE, "w", encoding="utf-8") as f:
            json.dump(allp, f, ensure_ascii=False)
    await _pay()
    return True


def quest_channels(settings: dict) -> list[str]:
    """랜덤 채널 퀘스트의 후보 목록.

    구버전은 `channel_id` 하나만 저장했습니다. 목록이 비어 있으면 그걸
    한 개짜리 목록으로 취급해, 설정을 다시 하지 않아도 그대로 동작합니다.
    """
    ids = [str(c) for c in (settings.get("channel_ids") or []) if c]
    if ids:
        return ids
    single = settings.get("channel_id")
    return [str(single)] if single else []


def _pick_quests(seed: str, pool: list[str], count: int, goals: dict,
                 prefix: str, channels: list[str] | None = None) -> list[dict]:
    """씨앗으로 퀘스트를 고릅니다.

    **저장하지 않습니다.** 같은 씨앗이면 언제 불러도 같은 결과가 나오므로,
    봇이 재시작해도 목록이 흔들리지 않습니다. 1,245명분 문서를 매일 쓰는
    대신 계산으로 해결합니다.
    """
    if not pool or count <= 0:
        return []
    import random as _random
    rng = _random.Random(seed)
    picked = rng.sample(pool, min(count, len(pool)))

    out = []
    for k in picked:
        q = {"id": f"{prefix}:{k}", "kind": k, "goal": int(goals.get(k, 1) or 1)}
        if k in ("channel_chat", "week_channel") and channels:
            # 채널도 같은 씨앗으로 고릅니다. 사람마다 다른 채널을 받되,
            # 같은 사람은 그 기간 동안 같은 채널을 봅니다.
            q["channel"] = rng.choice(channels)
        out.append(q)
    return out


def _daily_pool(settings: dict) -> list[str]:
    pool = [k for k, v in QUEST_KINDS.items()
            if v["period"] == "daily" and k not in (settings.get("disabled_kinds") or [])]
    if "channel_chat" in pool and not quest_channels(settings):
        pool.remove("channel_chat")     # 후보 채널이 없으면 낼 수 없습니다
    return pool


def _weekly_pool(settings: dict) -> list[str]:
    pool = [k for k, v in QUEST_KINDS.items()
            if v["period"] == "weekly" and k not in (settings.get("disabled_kinds") or [])]
    if "week_channel" in pool and not quest_channels(settings):
        pool.remove("week_channel")
    return pool


async def get_daily_quests(guild_id: int, settings: dict | None = None,
                           user_id: int | None = None) -> list[dict]:
    """오늘의 일일 퀘스트. **유저마다 다르게** 나옵니다.

    씨앗이 `길드:유저:날짜` 라 사람마다 다른 조합을 받되, 같은 사람은
    하루 동안 같은 목록을 봅니다. 명령어를 칠 때마다 바뀌면 진행하던
    퀘스트가 사라지기 때문입니다.

    user_id 를 안 주면 길드 공통(예전 방식)으로 뽑습니다 — 미리보기용.
    """
    settings = settings or await get_quest_settings(guild_id)
    seed = (f"{guild_id}:{user_id}:{quest_day()}" if user_id is not None
            else f"{guild_id}:{quest_day()}")
    return _pick_quests(seed, _daily_pool(settings),
                        int(settings.get("daily_count", 3) or 3),
                        settings.get("goals", {}), "d",
                        quest_channels(settings))


def weekly_quests(settings: dict, guild_id: int | None = None,
                  user_id: int | None = None) -> list[dict]:
    """이번 주 퀘스트. 일일과 마찬가지로 **유저마다 다릅니다.**

    주가 바뀌면 새로 뽑히고, 그 주 안에서는 고정입니다.
    """
    seed = (f"{guild_id}:{user_id}:{quest_week()}" if user_id is not None
            else f"{guild_id}:{quest_week()}")
    return _pick_quests(seed, _weekly_pool(settings),
                        int(settings.get("weekly_count", 2) or 2),
                        settings.get("goals", {}), "w",
                        quest_channels(settings))


def channel_field(channel_id, weekly: bool = False) -> str:
    """채널별 진행도 필드 이름.

    사람마다 배정된 채널이 다르므로 카운터도 채널별로 나뉘어야 합니다.
    점(`.`)을 쓰면 Mongo 는 중첩 문서를 만들고 JSON 폴백은 납작한 키를
    만들어 서로 어긋나므로, 밑줄로 납작하게 유지합니다.
    """
    return f"{'wch' if weekly else 'ch'}_{channel_id}"


def quest_progress_value(kind: str, progress: dict, quest: dict | None = None) -> int:
    """종류별로 어느 카운터를 보는지."""
    p = progress.get("p", {}) or {}
    if kind == "chat":
        return int(p.get("chat", 0) or 0)
    if kind == "channel_chat":
        ch = (quest or {}).get("channel")
        if ch:
            return int(p.get(channel_field(ch), 0) or 0)
        return int(p.get("channel_chat", 0) or 0)   # 구버전 데이터
    if kind == "gacha":
        return int(p.get("gacha", 0) or 0)
    if kind == "checkin":
        return int(p.get("checkin", 0) or 0)
    if kind == "early_bird":
        # 선착순은 '몇 등이었나'라서 작을수록 좋습니다. 0 은 아직 출석 전.
        return int(p.get("rank", 0) or 0)
    if kind == "week_checkin":
        return int(p.get("wcheckin", 0) or 0)
    if kind == "week_chat":
        return int(p.get("wchat", 0) or 0)
    if kind == "week_gacha":
        return int(p.get("wgacha", 0) or 0)
    if kind == "week_channel":
        ch = (quest or {}).get("channel")
        if ch:
            return int(p.get(channel_field(ch, weekly=True), 0) or 0)
        return int(p.get("wchannel", 0) or 0)       # 구버전 데이터
    return 0


def quest_done(kind: str, value: int, goal: int) -> bool:
    if kind == "early_bird":
        return 0 < value <= goal      # 등수는 작을수록 달성
    return value >= goal


def quest_label(kind: str, goal: int, channel_text: str = "") -> str:
    """퀘스트 문구. 채널형이면 어느 채널인지 함께 보여줍니다.

    '지정 채널' 이라고만 하면 어디인지 알 수 없어 퀘스트를 깰 수가 없습니다.
    """
    tpl = QUEST_KINDS.get(kind, {}).get("label", kind)
    return (tpl.replace("{n}", f"{goal:,}")
               .replace("{ch}", channel_text or "**지정 채널**"))


async def take_checkin_rank(guild_id: int) -> int:
    """오늘 출석 순번을 원자적으로 받아옵니다 (1부터).

    선착순 퀘스트용입니다. 읽고 더해서 쓰면 동시에 출석한 사람들이
    같은 등수를 받습니다. `$inc` 의 반환값을 그대로 등수로 씁니다.
    """
    key = f"{guild_id}:{quest_day()}"
    if quest_state_col is not None:
        try:
            # created 를 함께 씁니다. 없으면 quest_state_ttl 인덱스가
            # **한 번도 발동하지 않습니다** — TTL 은 필드가 없는 문서를
            # 건드리지 않기 때문입니다. 실제로 5건 전부 영구 잔류였습니다.
            doc = await _db(quest_state_col.find_one_and_update,
                            {"_id": key},
                            {"$inc": {"checkin_seq": 1},
                             "$set": {"created": datetime.now(timezone.utc)}},
                            upsert=True, return_document=_AFTER)
            return int((doc or {}).get("checkin_seq", 1) or 1)
        except Exception as e:
            print(f"MongoDB take_checkin_rank error: {e}", flush=True)
            return 0
    async with _quest_lock:
        _ensure_data_dir()
        try:
            with open(QUEST_STATE_FILE, "r", encoding="utf-8") as f:
                allst = json.load(f)
        except Exception:
            allst = {}
        row = allst.setdefault(key, {})
        row["checkin_seq"] = int(row.get("checkin_seq", 0)) + 1
        with open(QUEST_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(allst, f, ensure_ascii=False)
        return row["checkin_seq"]


def goal_min_chat(settings: dict, is_late: bool = False) -> int:
    """공동 목표 보상을 받기 위해 **본인이** 쳐야 하는 최소 채팅 수.

    is_late=False: 목표 달성 전 기본 참여 기준 (goal_min_percent)
    is_late=True: 목표 달성 후 늦참 참여 기준 (goal_late_min_percent)
    """
    target = int(settings.get("goal_target", 0) or 0)
    pct_key = "goal_late_min_percent" if is_late else "goal_min_percent"
    pct = float(settings.get(pct_key, 0) or 0)
    if is_late and pct <= 0:
        pct = float(settings.get("goal_min_percent", 0) or 0)
    if target <= 0 or pct <= 0:
        return 0
    import math
    return max(1, math.ceil(target * pct / 100))


async def bump_server_goal(guild_id: int, amount: int = 1, reset_days: int = 1) -> int:
    """서버 공동 목표 진행도를 올리고 현재값을 돌려줍니다."""
    key = f"{guild_id}:{quest_goal_period(reset_days)}"
    if quest_state_col is not None:
        try:
            # $setOnInsert 가 아니라 매번 갱신합니다. 공동 목표 주기가
            # TTL(21일)보다 길게 설정되면, 고정해 둔 created 때문에
            # **진행 중인 목표 문서가 도중에 지워져** 진행도가 조용히
            # 0 이 됩니다. 마지막 활동 기준으로 만료되게 둡니다.
            doc = await _db(quest_state_col.find_one_and_update,
                            {"_id": key},
                            {"$inc": {"goal_progress": amount},
                             "$set": {"created": datetime.now(timezone.utc)}},
                            upsert=True, return_document=_AFTER)
            return int((doc or {}).get("goal_progress", 0) or 0)
        except Exception as e:
            print(f"MongoDB bump_server_goal error: {e}", flush=True)
            return 0
    async with _quest_lock:
        _ensure_data_dir()
        try:
            with open(QUEST_STATE_FILE, "r", encoding="utf-8") as f:
                allst = json.load(f)
        except Exception:
            allst = {}
        row = allst.setdefault(key, {})
        row["goal_progress"] = int(row.get("goal_progress", 0)) + amount
        with open(QUEST_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(allst, f, ensure_ascii=False)
        return row["goal_progress"]


async def get_server_goal(guild_id: int, reset_days: int = 1,
                          when: datetime | None = None) -> dict:
    """공동 목표 진행 상황. when을 넘기면 해당 시점의 주기를 조회합니다."""
    key = f"{guild_id}:{quest_goal_period(reset_days, when)}"
    doc = {}
    if quest_state_col is not None:
        try:
            doc = await _db(quest_state_col.find_one, {"_id": key}) or {}
        except Exception as e:
            print(f"MongoDB get_server_goal error: {e}, using local file.", flush=True)
            doc = {}
    else:
        async with _quest_lock:
            try:
                with open(QUEST_STATE_FILE, "r", encoding="utf-8") as f:
                    doc = json.load(f).get(key, {})
            except Exception:
                doc = {}
    return {"progress": int(doc.get("goal_progress", 0) or 0),
            "announced": bool(doc.get("goal_announced"))}


async def mark_server_goal(guild_id: int, reset_days: int = 1, **patch) -> None:
    key = f"{guild_id}:{quest_goal_period(reset_days)}"
    if quest_state_col is not None:
        try:
            await _db(quest_state_col.update_one, {"_id": key},
                      {"$set": {**patch, "created": datetime.now(timezone.utc)}}, upsert=True)
            return
        except Exception as e:
            print(f"MongoDB mark_server_goal error: {e}", flush=True)
            return
    async with _quest_lock:
        _ensure_data_dir()
        try:
            with open(QUEST_STATE_FILE, "r", encoding="utf-8") as f:
                allst = json.load(f)
        except Exception:
            allst = {}
        allst.setdefault(key, {}).update(patch)
        with open(QUEST_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(allst, f, ensure_ascii=False)


# ── 이벤트 퀘스트 ─────────────────────────────────────────────────────
#
# 일일·주간과 달리 **기간이 있고 관리자가 직접 만듭니다.**
# 보상으로 포인트뿐 아니라 역할도 줄 수 있어, 기념 역할 배포에 쓸 수 있습니다.
#
# 두 가지 방식을 지원합니다.
#   tier  단계별  — 하나의 조건을 여러 단계로 (채팅 100/300/1000회)
#   all   모두달성 — 여러 조건을 전부 (채팅 200 + 뽑기 10 + 출석 5)

EVENT_FILE = os.path.join(DATA_DIR, "quest_events.json")
_event_lock = asyncio.Lock()
MAX_EVENTS = 20            # 길드당 보관 개수
MAX_TIERS = 5              # 이벤트당 단계 수
MAX_OBJECTIVES = 4         # 이벤트당 조건 수

# 이벤트에서 쓸 수 있는 조건. 일일 퀘스트가 쓰는 카운터를 그대로 재사용하되,
# 이벤트는 기간 누적이라 별도 문서에 쌓입니다.
EVENT_KINDS = {
    "chat":    "채팅 {n}회",
    "gacha":   "뽑기 {n}회",
    "checkin": "출석 {n}일",
}


def event_period_key(event_id: str) -> str:
    """이벤트 진행도의 기간 키. 날짜가 아니라 이벤트 단위로 쌓입니다."""
    return f"E{event_id}"


def parse_event_day(raw):
    """관리자가 적은 날짜를 date 로. `2026-9-5` 처럼 한 자리도 받습니다.

    ⚠️ **글자로 비교하지 마세요.** `"2026-09-26" < "2026-9-21"` 입니다
    ('0' < '9'). 한 자리로 적은 이벤트가 기간 안인데도 늘 '기간 밖' 으로
    떠서 **진행도가 한 번도 쌓이지 않았습니다**(2026-09-26 발견).
    """
    try:
        return datetime.strptime(str(raw).strip(), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def event_is_active(ev: dict, when: datetime | None = None) -> bool:
    """지금이 이벤트 기간 안인지. 날짜는 KST 기준이며 양끝을 포함합니다.

    읽을 수 없는 날짜는 **기간 밖** 으로 봅니다. 끝을 무시하면 역할 보상이
    걸린 이벤트가 영원히 열려 있게 됩니다.
    """
    if not ev.get("enabled", True):
        return False
    today = (when or datetime.now(KST)).date()
    starts, ends = ev.get("starts"), ev.get("ends")
    if starts:
        day = parse_event_day(starts)
        if day is None or today < day:
            return False
    if ends:
        day = parse_event_day(ends)
        if day is None or today > day:
            return False
    return True


def event_label(ev: dict) -> str:
    """이벤트 조건을 사람이 읽을 문구로."""
    if ev.get("mode") == "tier":
        tpl = EVENT_KINDS.get(ev.get("kind"), "{n}")
        return tpl.replace("{n}", "누적")
    return "여러 조건 모두 달성"


async def list_events(guild_id: int) -> list[dict]:
    """이 길드의 이벤트 전부 (최신순)."""
    str_gid = str(guild_id)
    if event_col is not None:
        try:
            def _fetch():
                return list(event_col.find({"guild_id": str_gid})
                            .sort("_id", -1).limit(MAX_EVENTS))
            return [{k: v for k, v in d.items() if k != "_id"} | {"id": d["_id"].split(":")[-1]}
                    for d in await _db(_fetch)]
        except Exception as e:
            print(f"MongoDB list_events error: {e}, using local file.", flush=True)
    async with _event_lock:
        rows = _read_json(EVENT_FILE)
    out = [dict(v) | {"id": k.split(":")[-1]}
           for k, v in rows.items() if v.get("guild_id") == str_gid]
    out.sort(key=lambda x: x.get("created", ""), reverse=True)
    return out[:MAX_EVENTS]


async def get_event(guild_id: int, event_id: str) -> dict | None:
    key = f"{guild_id}:{event_id}"
    if event_col is not None:
        try:
            doc = await _db(event_col.find_one, {"_id": key})
            if doc:
                return {k: v for k, v in doc.items() if k != "_id"} | {"id": event_id}
            return None
        except Exception as e:
            print(f"MongoDB get_event error: {e}, using local file.", flush=True)
    async with _event_lock:
        row = _read_json(EVENT_FILE).get(key)
    return (dict(row) | {"id": event_id}) if row else None


async def save_event(guild_id: int, event_id: str, data: dict) -> None:
    """이벤트를 저장합니다. 같은 id 면 덮어씁니다."""
    _active_events_cache.pop(str(guild_id))   # 변경 즉시 반영
    key = f"{guild_id}:{event_id}"
    payload = dict(data)
    payload["guild_id"] = str(guild_id)
    payload.setdefault("created", datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S"))
    payload.pop("id", None)
    if event_col is not None:
        try:
            await _db(event_col.update_one, {"_id": key}, {"$set": payload}, upsert=True)
            return
        except Exception as e:
            print(f"MongoDB save_event error: {e}, using local file.", flush=True)
    async with _event_lock:
        _ensure_data_dir()
        rows = _read_json(EVENT_FILE)
        rows[key] = payload
        # 길드당 상한 — Mongo 경로는 조회에서 limit 을 걸지만, 파일은 계속
        # 늘어나므로 여기서 잘라야 합니다. (양쪽 모두에 상한을 거는 규칙)
        mine = sorted([k for k, v in rows.items() if v.get("guild_id") == str(guild_id)],
                      key=lambda k: rows[k].get("created", ""), reverse=True)
        for old in mine[MAX_EVENTS:]:
            rows.pop(old, None)
        _write_json(EVENT_FILE, rows)


async def delete_event(guild_id: int, event_id: str) -> bool:
    _active_events_cache.pop(str(guild_id))
    key = f"{guild_id}:{event_id}"
    if event_col is not None:
        try:
            res = await _db(event_col.delete_one, {"_id": key})
            return bool(getattr(res, "deleted_count", 0))
        except Exception as e:
            print(f"MongoDB delete_event error: {e}, using local file.", flush=True)
    async with _event_lock:
        rows = _read_json(EVENT_FILE)
        existed = key in rows
        rows.pop(key, None)
        _write_json(EVENT_FILE, rows)
    return existed


async def active_events(guild_id: int) -> list[dict]:
    """지금 기간 안인 이벤트만."""
    return [e for e in await list_events(guild_id) if event_is_active(e)]


# 활성 이벤트 목록도 모든 메시지 경로에서 조회됩니다. 캐시가 없으면
# 채팅 한 줄마다 DB 를 읽습니다(퀘스트 설정과 같은 이유).
_active_events_cache = _TTLCache(ttl_seconds=60, max_items=8)


async def bump_event_progress(guild_id: int, user_id: int, kind: str,
                              amount: int = 1) -> None:
    """진행 중인 이벤트들의 해당 조건 진행도를 올립니다.

    이벤트는 보통 0~1개라 추가 비용이 거의 없습니다. 목록은 60초 캐시라
    메시지마다 DB 를 읽지 않습니다.
    """
    cached = _active_events_cache.get(str(guild_id))
    if cached is None:
        cached = await active_events(guild_id)
        _active_events_cache.set(str(guild_id), cached)
    for ev in cached:
        # 이 이벤트가 그 조건을 쓰는지 확인 — 안 쓰면 셀 필요가 없습니다.
        if ev.get("mode") == "tier":
            if ev.get("kind") != kind:
                continue
        elif not any(o.get("kind") == kind for o in (ev.get("objectives") or [])):
            continue
        await bump_quest_progress(guild_id, user_id, f"ev_{kind}", amount,
                                  period=event_period_key(ev["id"]))


def invalidate_events(guild_id: int) -> None:
    """이벤트를 만들거나 고치면 즉시 반영되도록 캐시를 비웁니다."""
    _active_events_cache.pop(str(guild_id))


def event_progress_value(ev: dict, progress: dict, kind: str | None = None) -> int:
    """이벤트 진행도. 단계별이면 그 조건, 모두달성이면 지정한 조건."""
    p = progress.get("p", {}) or {}
    field = kind or ev.get("kind") or "chat"
    return int(p.get(f"ev_{field}", 0) or 0)


def event_status(ev: dict, progress: dict) -> dict:
    """이벤트 달성 상황을 한 번에 계산합니다.

    반환값:
        mode      "tier" | "all"
        rows      [{label, value, goal, done, claimed, points, role_id, id}]
        all_done  (모두달성 방식에서) 전부 깼는지
    """
    claimed = set(progress.get("claimed") or [])
    eid = ev.get("id", "")
    rows = []

    if ev.get("mode") == "tier":
        value = event_progress_value(ev, progress)
        tpl = EVENT_KINDS.get(ev.get("kind"), "{n}")
        for i, t in enumerate(ev.get("tiers") or []):
            goal = int(t.get("goal", 0) or 0)
            qid = f"e:{eid}:{i}"
            rows.append({
                "id": qid,
                "label": tpl.replace("{n}", f"{goal:,}"),
                "value": value, "goal": goal,
                "done": goal > 0 and value >= goal,
                "claimed": qid in claimed,
                "points": int(t.get("points", 0) or 0),
                "role_id": t.get("role_id"),
            })
        return {"mode": "tier", "rows": rows, "all_done": False}

    # 모두달성
    all_done = True
    for i, o in enumerate(ev.get("objectives") or []):
        kind = o.get("kind", "chat")
        goal = int(o.get("goal", 0) or 0)
        value = event_progress_value(ev, progress, kind)
        done = goal > 0 and value >= goal
        all_done = all_done and done
        rows.append({
            "id": f"e:{eid}:o{i}",
            "label": EVENT_KINDS.get(kind, "{n}").replace("{n}", f"{goal:,}"),
            "value": value, "goal": goal, "done": done,
            "claimed": False, "points": 0, "role_id": None,
        })
    if not rows:
        all_done = False
    reward = ev.get("reward") or {}
    return {"mode": "all", "rows": rows, "all_done": all_done,
            "reward_id": f"e:{eid}:all",
            "reward_claimed": f"e:{eid}:all" in claimed,
            "points": int(reward.get("points", 0) or 0),
            "role_id": reward.get("role_id")}
