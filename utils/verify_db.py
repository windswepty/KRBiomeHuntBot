"""
인증 & 링크 보호 전용 데이터 계층.

이 모듈은 포인트/상점/뽑기/제보 시스템(utils/database.py)과 완전히 분리되어 있습니다.
독립된 MongoDB 컬렉션과 독립된 로컬 JSON 파일만 사용하며,
기존 이코노미 코드를 전혀 import 하지 않습니다.
"""

import os
import json
import time
import asyncio
import hashlib
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

load_dotenv()

DATA_DIR = "data"
VERIFY_FILE = os.path.join(DATA_DIR, "verify_settings.json")
LINKGUARD_FILE = os.path.join(DATA_DIR, "linkguard_settings.json")
PLINK_FILE = os.path.join(DATA_DIR, "protected_links.json")
CLICKLOG_FILE = os.path.join(DATA_DIR, "link_clicks.json")
WATERMARK_FILE = os.path.join(DATA_DIR, "link_watermarks.json")
GUARDKEY_FILE = os.path.join(DATA_DIR, "guarded_keys.json")
USAGE_FILE = os.path.join(DATA_DIR, "link_usage.json")
LOGREF_FILE = os.path.join(DATA_DIR, "log_references.json")
MONGO_URI = os.getenv("MONGO_URI")

KST = timezone(timedelta(hours=9))

_verify_lock = asyncio.Lock()
_linkguard_lock = asyncio.Lock()
_plink_lock = asyncio.Lock()
_clicklog_lock = asyncio.Lock()
_watermark_lock = asyncio.Lock()
_guardkey_lock = asyncio.Lock()
_usage_lock = asyncio.Lock()
_logref_lock = asyncio.Lock()

verify_col = None
linkguard_col = None
plink_col = None
clicklog_col = None
watermark_col = None
guardkey_col = None
usage_col = None
logref_col = None

if MONGO_URI:
    try:
        from pymongo import MongoClient
        _client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
        _client.admin.command('ping')
        _db = _client["krbiomehuntbot"]
        verify_col = _db["verify_settings"]
        linkguard_col = _db["linkguard_settings"]
        plink_col = _db["protected_links"]
        clicklog_col = _db["link_clicks"]
        watermark_col = _db["link_watermarks"]
        guardkey_col = _db["guarded_keys"]
        usage_col = _db["link_usage"]
        logref_col = _db["log_references"]

        # 인덱스는 없어도 기능은 동작하므로, 생성에 실패해도 Mongo 연결 자체를
        # 포기하지 않도록 별도의 try로 감쌉니다. create_index는 멱등이라
        # 기동할 때마다 호출해도 안전합니다.
        try:
            # 보호 메시지 ID로 로그 참조를 역방향 조회할 때 사용 (풀스캔 방지)
            logref_col.create_index(
                [("guild_id", 1), ("protected_message_id", 1)],
                name="logref_by_protected"
            )
            # 길드별 최신순 조회 및 오래된 클릭 로그 정리에 사용
            clicklog_col.create_index(
                [("guild_id", 1), ("_id", -1)],
                name="clicklog_recent"
            )

            # ── 자동 청소(TTL) ──
            # MongoDB가 60초마다 스스로 만료 문서를 지웁니다. 봇이 꺼져 있어도
            # 동작하므로 별도 청소 코드나 스케줄러가 필요 없습니다.
            #   protected_links : 버튼의 실사용 수명은 바이옴 지속시간(2~10분)이라
            #                     7일이면 충분한 여유입니다.
            #   link_usage / log_references : 유출 조사용이라 넉넉히 30일.
            plink_col.create_index(
                "updated", name="plink_ttl", expireAfterSeconds=7 * 24 * 3600
            )
            usage_col.create_index(
                "updated", name="usage_ttl", expireAfterSeconds=30 * 24 * 3600
            )
            logref_col.create_index(
                "updated", name="logref_ttl", expireAfterSeconds=30 * 24 * 3600
            )
        except Exception as e:
            print(f"MongoDB index setup warning: {e}", flush=True)

        print("Connected to MongoDB successfully for verify/linkguard storage!", flush=True)
    except Exception as e:
        print(f"MongoDB (verify) connection warning: {e}. Falling back to local JSON storage.", flush=True)
        verify_col = None
        linkguard_col = None
        plink_col = None
        clicklog_col = None
        watermark_col = None
        guardkey_col = None
        usage_col = None
        logref_col = None


def _utcnow():
    """TTL 인덱스가 인식하는 BSON Date 를 만듭니다.

    기존 `created`/`timestamp` 필드는 사람이 읽기 위한 **문자열**이라
    TTL이 동작하지 않습니다. TTL은 Date 타입 필드에만 걸립니다.
    그래서 표시용 문자열은 그대로 두고, 만료 판정용 필드를 따로 둡니다.
    """
    return datetime.now(timezone.utc)


class _TTLCache:
    """시간 기반 만료 + 크기 상한이 있는 캐시.

    두 가지를 막습니다.
      1) MongoDB TTL이 문서를 지운 뒤에도 메모리 캐시가 옛 값을 계속 돌려주는 것
         (캐시 수명을 DB TTL보다 짧게 잡아 어긋나지 않게 합니다)
      2) 캐시가 무한히 늘어나 메모리를 잠식하는 것
    """

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
            # dict는 삽입 순서를 유지하므로 앞쪽이 가장 오래된 항목입니다
            for key in list(self._data)[:len(self._data) - self._max // 2]:
                self._data.pop(key, None)


async def _db(fn, *args, **kwargs):
    """동기 드라이버(pymongo) 호출을 워커 스레드에서 실행합니다.

    pymongo는 동기 라이브러리라 async 함수 안에서 그냥 호출하면 그동안
    이벤트 루프 전체가 멈춥니다. 개인서버 자리를 두고 수십 명이 동시에
    버튼을 누르는 상황에서는 이 정지가 다른 유저의 응답을 지연시키고,
    디스코드 상호작용 3초 제한을 넘겨 '상호작용 실패'까지 유발합니다.

    MongoClient는 스레드 안전하므로 스레드로 넘기는 것이 안전합니다.
    """
    return await asyncio.to_thread(fn, *args, **kwargs)


def _ensure_data_dir():
    if not os.path.exists(DATA_DIR):
        os.makedirs(DATA_DIR, exist_ok=True)


def _read_json(path: str) -> dict:
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


# ── 인증 설정 ─────────────────────────────────────────────────────────

DEFAULT_VERIFY_SETTINGS = {
    "enabled": True,
    "role_id": None,
    "panel_channel_id": None,
    "title": "✅ 서버 인증",
    "description": "아래 **[인증하기]** 버튼을 눌러 인증을 완료해 주세요.\n"
                   "인증이 완료되면 보호된 링크를 열람하실 수 있습니다.",
    "button_label": "인증하기",
}


# 링크 버튼 클릭은 수백 명이 동시에 누를 수 있으므로, 조회가 DB를 타지 않도록
# 메모리 캐시를 둡니다. 저장은 항상 save_* 를 거치므로 캐시가 어긋나지 않습니다.
_verify_cache: dict[str, dict] = {}


async def get_verify_settings(guild_id: int) -> dict:
    str_gid = str(guild_id)

    cached = _verify_cache.get(str_gid)
    if cached is not None:
        return dict(cached)

    result = dict(DEFAULT_VERIFY_SETTINGS)

    if verify_col is not None:
        try:
            doc = await _db(verify_col.find_one, {"_id": str_gid})
            if doc:
                result.update({k: v for k, v in doc.items() if k != "_id"})
            _verify_cache[str_gid] = dict(result)
            return dict(result)
        except Exception as e:
            print(f"MongoDB get_verify_settings error: {e}, using local file.", flush=True)

    async with _verify_lock:
        all_settings = _read_json(VERIFY_FILE)
    if str_gid in all_settings:
        result.update(all_settings[str_gid])
    _verify_cache[str_gid] = dict(result)
    return dict(result)


async def save_verify_settings(guild_id: int, settings: dict) -> None:
    str_gid = str(guild_id)

    # 캐시를 먼저 갱신해 저장 직후 조회에도 최신값이 보이게 합니다
    current = _verify_cache.get(str_gid) or dict(DEFAULT_VERIFY_SETTINGS)
    current.update(settings)
    _verify_cache[str_gid] = current

    if verify_col is not None:
        try:
            await _db(verify_col.update_one, {"_id": str_gid}, {"$set": settings}, upsert=True)
            return
        except Exception as e:
            print(f"MongoDB save_verify_settings error: {e}, using local file.", flush=True)

    async with _verify_lock:
        all_settings = _read_json(VERIFY_FILE)
        all_settings.setdefault(str_gid, {}).update(settings)
        _write_json(VERIFY_FILE, all_settings)


# ── 링크 보호(링크가드) 설정 ──────────────────────────────────────────

DEFAULT_LINKGUARD_SETTINGS = {
    "enabled": True,
    # 개별 채널 지정
    "channel_ids": [],
    # 카테고리 통째로 지정 (안에 있는 모든 채널이 자동 감시 대상, 새 채널도 자동 포함)
    "category_ids": [],
    "button_label": "🎮 개인서버 입장하기",
    "locked_text": "🔒 인증된 유저만 입장할 수 있습니다",
    # 열람 / 유출 기록을 실시간으로 보낼 채널
    "log_channel_id": None,
    # 유저마다 다른 워터마크(#hbh-xxxxxxxx)를 링크에 붙여 유출자를 추적
    "watermark": True,
    # 서버 안에서 보호 링크가 유출되면 그 메시지를 자동 삭제
    "auto_delete_leak": True,
    # 로블록스 링크만 보호 대상으로 삼습니다.
    # 매크로 임베드에 함께 들어있는 제작자 홍보 링크 등을 잘못 가로채지 않도록 하는 안전장치입니다.
    "roblox_only": True,
    # 링크 중계: 로블록스 링크를 그대로 주지 않고 만료되는 1회성 주소로 바꿔 전달합니다.
    # 유출된 링크를 무효화하지만, 봇의 웹 서버가 죽으면 아무도 입장할 수 없게 되는
    # 단일 장애점이 생깁니다. 그래서 기본값은 꺼둔 상태입니다.
    "relay_links": False,
}


_linkguard_cache: dict[str, dict] = {}


async def get_linkguard_settings(guild_id: int) -> dict:
    str_gid = str(guild_id)

    cached = _linkguard_cache.get(str_gid)
    if cached is not None:
        return dict(cached)

    result = dict(DEFAULT_LINKGUARD_SETTINGS)

    if linkguard_col is not None:
        try:
            doc = await _db(linkguard_col.find_one, {"_id": str_gid})
            if doc:
                result.update({k: v for k, v in doc.items() if k != "_id"})
            result["channel_ids"] = [int(c) for c in result.get("channel_ids", [])]
            result["category_ids"] = [int(c) for c in result.get("category_ids", [])]
            _linkguard_cache[str_gid] = dict(result)
            return dict(result)
        except Exception as e:
            print(f"MongoDB get_linkguard_settings error: {e}, using local file.", flush=True)

    async with _linkguard_lock:
        all_settings = _read_json(LINKGUARD_FILE)
    if str_gid in all_settings:
        result.update(all_settings[str_gid])
    result["channel_ids"] = [int(c) for c in result.get("channel_ids", [])]
    result["category_ids"] = [int(c) for c in result.get("category_ids", [])]
    _linkguard_cache[str_gid] = dict(result)
    return dict(result)


async def save_linkguard_settings(guild_id: int, settings: dict) -> None:
    str_gid = str(guild_id)

    current = _linkguard_cache.get(str_gid) or dict(DEFAULT_LINKGUARD_SETTINGS)
    current.update(settings)
    current["channel_ids"] = [int(c) for c in current.get("channel_ids", [])]
    current["category_ids"] = [int(c) for c in current.get("category_ids", [])]
    _linkguard_cache[str_gid] = current

    if linkguard_col is not None:
        try:
            await _db(linkguard_col.update_one, {"_id": str_gid}, {"$set": settings}, upsert=True)
            return
        except Exception as e:
            print(f"MongoDB save_linkguard_settings error: {e}, using local file.", flush=True)

    async with _linkguard_lock:
        all_settings = _read_json(LINKGUARD_FILE)
        all_settings.setdefault(str_gid, {}).update(settings)
        _write_json(LINKGUARD_FILE, all_settings)


# ── 보호된 링크 저장소 (메시지 ID 기준) ───────────────────────────────

# 버튼 클릭의 핫패스. DB TTL(7일)보다 짧게 잡아 지워진 값을 돌려주지 않게 합니다.
_plink_cache = _TTLCache(ttl_seconds=6 * 3600, max_items=3000)


async def save_protected_link(guild_id: int, message_id: int, links: list[str]) -> None:
    """봇이 재게시한 메시지 ID에 원본 링크 목록을 매핑해 저장합니다."""
    key = f"{guild_id}:{message_id}"

    # 버튼 클릭 시 DB를 타지 않도록 메모리에 먼저 올려둡니다
    _plink_cache.set(key, list(links))

    if plink_col is not None:
        try:
            await _db(
                plink_col.update_one,
                {"_id": key},
                {"$set": {"guild_id": str(guild_id), "message_id": str(message_id),
                          "links": links, "updated": _utcnow()}},
                upsert=True
            )
            return
        except Exception as e:
            print(f"MongoDB save_protected_link error: {e}, using local file.", flush=True)

    async with _plink_lock:
        all_links = _read_json(PLINK_FILE)
        all_links[key] = {"guild_id": str(guild_id), "message_id": str(message_id), "links": links}
        _write_json(PLINK_FILE, all_links)


async def get_protected_link(guild_id: int, message_id: int) -> list[str]:
    """메시지 ID에 매핑된 원본 링크 목록을 반환합니다. 없으면 빈 리스트.

    메모리 캐시를 먼저 확인하므로, 봇이 살아있는 동안의 클릭은 DB를 타지 않습니다.
    (봇 재시작 후 첫 클릭에서만 DB를 조회하고 다시 캐시에 올립니다)
    """
    key = f"{guild_id}:{message_id}"

    cached = _plink_cache.get(key)
    if cached is not None:
        return list(cached)

    if plink_col is not None:
        try:
            doc = await _db(plink_col.find_one, {"_id": key})
            if doc:
                links = list(doc.get("links", []))
                _plink_cache.set(key, list(links))
                return links
        except Exception as e:
            print(f"MongoDB get_protected_link error: {e}, using local file.", flush=True)

    async with _plink_lock:
        all_links = _read_json(PLINK_FILE)
    entry = all_links.get(key)
    if entry:
        links = list(entry.get("links", []))
        _plink_cache.set(key, list(links))
        return links
    return []


# ── 링크 클릭 기록 (유출 추적용) ──────────────────────────────────────
#
# 클릭 응답 속도에 영향을 주지 않도록, 호출부에서 응답을 보낸 **뒤에**
# 백그라운드 태스크로 기록합니다.

MAX_CLICK_LOGS = 500

# event_type: "view" = 정상 열람 / "leak" = 서버 내 유출 감지

# 오래된 클릭 로그 정리 주기.
# 삽입할 때마다 정리하면 낭비라, 프로세스 기동 후 첫 삽입과 이후 N회마다 수행합니다.
# (Render 무료 플랜처럼 재시작이 잦은 환경에서도 최소 한 번은 정리되도록
#  '첫 삽입'을 포함시켰습니다)
CLICKLOG_TRIM_EVERY = 50
_clicklog_insert_count: dict[str, int] = {}


def _maybe_trim_click_logs(guild_id: int) -> None:
    """MongoDB의 클릭 로그를 길드당 MAX_CLICK_LOGS건으로 유지합니다.

    로컬 JSON 경로에는 원래 캡이 걸려 있었지만 Mongo 경로에는 없어서
    클릭 1회당 1문서가 영구 누적되고 있었습니다. 이 함수가 그 격차를 메웁니다.

    기록 자체를 방해하면 안 되므로 예외를 모두 삼키고 경고만 남깁니다.
    """
    if clicklog_col is None:
        return

    str_gid = str(guild_id)
    n = _clicklog_insert_count.get(str_gid, 0) + 1
    _clicklog_insert_count[str_gid] = n
    if n != 1 and n % CLICKLOG_TRIM_EVERY != 0:
        return

    try:
        # ObjectId(_id)는 생성 시각 순으로 증가하므로 최신순 정렬 기준으로 씁니다.
        # (get_click_logs도 동일하게 _id 역순을 최신순으로 사용합니다)
        cursor = (clicklog_col.find({"guild_id": str_gid}, {"_id": 1})
                  .sort("_id", -1).skip(MAX_CLICK_LOGS).limit(1))
        cutoff = next(iter(cursor), None)
        if cutoff is None:
            return  # 아직 상한에 도달하지 않음
        clicklog_col.delete_many(
            {"guild_id": str_gid, "_id": {"$lte": cutoff["_id"]}}
        )
    except Exception as e:
        print(f"MongoDB click log trim warning: {e}", flush=True)


async def add_click_log(
    guild_id: int,
    user_id: int,
    user_name: str,
    url: str,
    event_type: str = "view",
    detail: str = "",
) -> None:
    """누가 언제 어떤 링크를 열람했는지 / 유출했는지 기록합니다."""
    timestamp = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")
    entry = {
        "guild_id": str(guild_id),
        "user_id": str(user_id),
        "user_name": user_name,
        "url": url,
        "type": event_type,
        "detail": detail,
        "timestamp": timestamp,
    }

    if clicklog_col is not None:
        try:
            await _db(clicklog_col.insert_one, dict(entry))
        except Exception as e:
            print(f"MongoDB add_click_log error: {e}, using local file.", flush=True)
        else:
            # 삽입에 성공했을 때만 정리합니다. 정리 실패가 JSON 폴백을 부르면
            # 같은 기록이 두 곳에 남으므로, except 밖(else)에서 호출합니다.
            await asyncio.to_thread(_maybe_trim_click_logs, guild_id)
            return

    async with _clicklog_lock:
        all_logs = _read_json(CLICKLOG_FILE)
        guild_logs = all_logs.setdefault(str(guild_id), [])
        guild_logs.insert(0, entry)
        del guild_logs[MAX_CLICK_LOGS:]
        _write_json(CLICKLOG_FILE, all_logs)


async def get_click_logs(guild_id: int, limit: int = 30, event_type: str = "all") -> list[dict]:
    """최근 열람 / 유출 기록을 최신순으로 반환합니다."""
    if clicklog_col is not None:
        try:
            query: dict = {"guild_id": str(guild_id)}
            if event_type != "all":
                query["type"] = event_type
            def _fetch():
                cur = clicklog_col.find(query).sort("_id", -1).limit(limit)
                return [{k: v for k, v in d.items() if k != "_id"} for d in cur]
            return await _db(_fetch)
        except Exception as e:
            print(f"MongoDB get_click_logs error: {e}, using local file.", flush=True)

    async with _clicklog_lock:
        all_logs = _read_json(CLICKLOG_FILE)
    logs = list(all_logs.get(str(guild_id), []))
    if event_type != "all":
        logs = [x for x in logs if x.get("type", "view") == event_type]
    return logs[:limit]


# ── 워터마크 (유출자 추적용) ──────────────────────────────────────────
#
# 유저마다 다른 코드를 링크 뒤에 프래그먼트(#hbh-xxxxxxxx)로 붙입니다.
# 프래그먼트는 서버로 전송되지 않으므로 로블록스 링크의 동작을 깨뜨리지 않으면서,
# 복사되어 퍼진 링크에는 그대로 따라붙습니다.

_watermark_cache: dict[str, dict] = {}


def make_watermark_code(guild_id: int, user_id: int) -> str:
    """길드+유저 조합으로 항상 같은 8자리 코드를 만듭니다."""
    raw = f"hbh:{guild_id}:{user_id}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:8]


async def save_watermark(guild_id: int, code: str, user_id: int, user_name: str) -> None:
    """워터마크 코드 → 유저 매핑을 저장합니다 (역추적용)."""
    key = f"{guild_id}:{code}"
    entry = {"guild_id": str(guild_id), "code": code, "user_id": str(user_id), "user_name": user_name}

    # 워터마크 코드는 hash(길드+유저)라 유저마다 고정입니다. 이미 같은 내용이
    # 저장돼 있으면 다시 쓸 이유가 없으므로, 몰림 상황에서 불필요한 쓰기를
    # 통째로 건너뜁니다 (재방문 유저는 클릭당 DB 쓰기가 1회 줄어듭니다).
    if _watermark_cache.get(key) == entry:
        return
    _watermark_cache[key] = entry

    if watermark_col is not None:
        try:
            await _db(watermark_col.update_one, {"_id": key}, {"$set": entry}, upsert=True)
            return
        except Exception as e:
            print(f"MongoDB save_watermark error: {e}, using local file.", flush=True)

    async with _watermark_lock:
        all_marks = _read_json(WATERMARK_FILE)
        all_marks[key] = entry
        _write_json(WATERMARK_FILE, all_marks)


async def get_watermark(guild_id: int, code: str) -> dict | None:
    """워터마크 코드로 발급 대상 유저를 역추적합니다."""
    key = f"{guild_id}:{code}"

    cached = _watermark_cache.get(key)
    if cached is not None:
        return dict(cached)

    if watermark_col is not None:
        try:
            doc = await _db(watermark_col.find_one, {"_id": key})
            if doc:
                entry = {k: v for k, v in doc.items() if k != "_id"}
                _watermark_cache[key] = entry
                return dict(entry)
        except Exception as e:
            print(f"MongoDB get_watermark error: {e}, using local file.", flush=True)

    async with _watermark_lock:
        all_marks = _read_json(WATERMARK_FILE)
    entry = all_marks.get(key)
    if entry:
        _watermark_cache[key] = entry
        return dict(entry)
    return None


# ── 보호 대상 링크 색인 (유출 감지용) ─────────────────────────────────

_guarded_keys_cache: dict[str, set[str]] = {}

# 보호 메시지별 사용자 집계 (임베드마다 따로)
_usage_cache = _TTLCache(ttl_seconds=3600, max_items=2000)


async def register_guarded_keys(guild_id: int, keys: list[str]) -> None:
    """이 길드에서 보호 중인 링크 식별자(개인서버 코드 등)를 색인에 추가합니다."""
    str_gid = str(guild_id)
    bucket = _guarded_keys_cache.setdefault(str_gid, set())
    bucket.update(keys)

    if guardkey_col is not None:
        try:
            def _write():
                for key in keys:
                    guardkey_col.update_one(
                        {"_id": f"{guild_id}:{key}"},
                        {"$set": {"guild_id": str_gid, "key": key}},
                        upsert=True
                    )
            await _db(_write)
            return
        except Exception as e:
            print(f"MongoDB register_guarded_keys error: {e}, using local file.", flush=True)

    async with _guardkey_lock:
        all_keys = _read_json(GUARDKEY_FILE)
        existing = set(all_keys.get(str_gid, []))
        existing.update(keys)
        all_keys[str_gid] = sorted(existing)
        _write_json(GUARDKEY_FILE, all_keys)


async def record_link_usage(guild_id: int, message_id: int,
                            user_id: int, user_name: str) -> tuple[int, bool]:
    """보호 메시지별로 '누가 링크를 사용했는지'를 기록합니다.

    반환값은 (지금까지 사용한 서로 다른 유저 수, 이번이 첫 사용인지) 입니다.
    임베드(보호 메시지)마다 따로 집계됩니다.
    """
    key = f"{guild_id}:{message_id}"
    timestamp = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")
    uid = str(user_id)

    bucket = _usage_cache.get(key)
    if bucket is None:
        loaded = await _load_usage(key)
        # 위 await 동안 다른 클릭이 먼저 캐시를 채웠을 수 있습니다.
        # 수십 명이 동시에 누르면 전원이 '빈 집계'를 각자 불러와 서로 덮어써서
        # 인원이 1명으로 뭉개지므로, 여기서 다시 확인해 **하나의 집계 객체를
        # 공유**하도록 합니다. 아래 재확인~set 사이에는 await 가 없으므로
        # 이벤트 루프 관점에서 원자적입니다.
        bucket = _usage_cache.get(key)
        if bucket is None:
            bucket = loaded
    _usage_cache.set(key, bucket)

    is_new = uid not in bucket
    entry = bucket.get(uid) or {"name": user_name, "count": 0, "first": timestamp}
    entry["name"] = user_name
    entry["count"] = entry.get("count", 0) + 1
    entry["last"] = timestamp
    bucket[uid] = entry
    total = len(bucket)

    if usage_col is not None:
        try:
            await _db(
                usage_col.update_one,
                {"_id": key},
                {
                    "$set": {
                        "guild_id": str(guild_id),
                        "message_id": str(message_id),
                        f"users.{uid}.name": user_name,
                        f"users.{uid}.last": timestamp,
                        # 클릭할 때마다 갱신 → 계속 쓰이는 링크는 만료되지 않고,
                        # 30일간 아무도 안 누른 것만 자동 삭제됩니다.
                        "updated": _utcnow(),
                    },
                    "$inc": {f"users.{uid}.count": 1},
                    "$setOnInsert": {"created": timestamp},
                },
                upsert=True,
            )
            return total, is_new
        except Exception as e:
            print(f"MongoDB record_link_usage error: {e}, using local file.", flush=True)

    async with _usage_lock:
        all_usage = _read_json(USAGE_FILE)
        all_usage[key] = bucket
        _write_json(USAGE_FILE, all_usage)
    return total, is_new


async def _load_usage(key: str) -> dict:
    if usage_col is not None:
        try:
            doc = await _db(usage_col.find_one, {"_id": key})
            if doc:
                return dict(doc.get("users", {}))
            return {}
        except Exception as e:
            print(f"MongoDB _load_usage error: {e}, using local file.", flush=True)

    async with _usage_lock:
        all_usage = _read_json(USAGE_FILE)
    return dict(all_usage.get(key, {}))


async def get_link_usage(guild_id: int, message_id: int) -> list[dict]:
    """보호 메시지 하나에 대해 링크를 사용한 유저 목록을 최신순으로 반환합니다."""
    key = f"{guild_id}:{message_id}"

    bucket = _usage_cache.get(key)
    if bucket is None:
        loaded = await _load_usage(key)
        # 위 await 동안 다른 클릭이 먼저 캐시를 채웠을 수 있습니다.
        # 수십 명이 동시에 누르면 전원이 '빈 집계'를 각자 불러와 서로 덮어써서
        # 인원이 1명으로 뭉개지므로, 여기서 다시 확인해 **하나의 집계 객체를
        # 공유**하도록 합니다. 아래 재확인~set 사이에는 await 가 없으므로
        # 이벤트 루프 관점에서 원자적입니다.
        bucket = _usage_cache.get(key)
        if bucket is None:
            bucket = loaded
    _usage_cache.set(key, bucket)

    rows = [
        {
            "user_id": uid,
            "user_name": info.get("name", "?"),
            "count": info.get("count", 1),
            "first": info.get("first", info.get("last", "")),
            "last": info.get("last", ""),
        }
        for uid, info in bucket.items()
    ]
    rows.sort(key=lambda r: r.get("last", ""), reverse=True)
    return rows


# ── 로그 메시지 ↔ 보호 메시지 연결 ────────────────────────────────────
#
# 로그 채널에 올린 임베드의 [링크 사용 유저] 버튼이, 어느 보호 메시지의
# 집계를 보여줘야 하는지 찾기 위한 매핑입니다.

_logref_cache = _TTLCache(ttl_seconds=3600, max_items=2000)

# protected_message_id -> 참조 (역방향 조회용, get_log_reference_by_protected 전용 캐시)
_logref_by_protected_cache = _TTLCache(ttl_seconds=3600, max_items=2000)


async def save_log_reference(guild_id: int, log_message_id: int,
                             protected_message_id: int, channel_id: int) -> None:
    key = f"{guild_id}:{log_message_id}"
    entry = {
        "guild_id": str(guild_id),
        "log_message_id": str(log_message_id),
        "protected_message_id": str(protected_message_id),
        "channel_id": str(channel_id),
    }
    _logref_cache.set(key, entry)
    _logref_by_protected_cache.set(f"{guild_id}:{protected_message_id}", entry)

    if logref_col is not None:
        try:
            # entry 는 로컬 JSON 저장에도 쓰이므로 datetime 을 섞지 않습니다
            # (json.dump 가 직렬화하지 못합니다). Mongo 쪽에만 덧붙입니다.
            await _db(logref_col.update_one, {"_id": key},
                      {"$set": {**entry, "updated": _utcnow()}}, upsert=True)
            return
        except Exception as e:
            print(f"MongoDB save_log_reference error: {e}, using local file.", flush=True)

    async with _logref_lock:
        all_refs = _read_json(LOGREF_FILE)
        all_refs[key] = entry
        _write_json(LOGREF_FILE, all_refs)


async def get_log_reference(guild_id: int, log_message_id: int) -> dict | None:
    key = f"{guild_id}:{log_message_id}"

    cached = _logref_cache.get(key)
    if cached is not None:
        return dict(cached)

    if logref_col is not None:
        try:
            doc = await _db(logref_col.find_one, {"_id": key})
            if doc:
                entry = {k: v for k, v in doc.items() if k != "_id"}
                _logref_cache.set(key, entry)
                return dict(entry)
        except Exception as e:
            print(f"MongoDB get_log_reference error: {e}, using local file.", flush=True)

    async with _logref_lock:
        all_refs = _read_json(LOGREF_FILE)
    entry = all_refs.get(key)
    if entry:
        _logref_cache.set(key, entry)
        return dict(entry)
    return None


async def get_log_reference_by_protected(guild_id: int, protected_message_id: int) -> dict | None:
    """보호 메시지 ID로 로그 메시지 참조를 역방향 조회합니다.

    link_guard.py의 `_LOG_MESSAGE_BY_PROTECTED`(프로세스 메모리 캐시)가
    비어있을 때(봇 재시작 직후 등) 영속 저장소에서 매핑을 복원하기 위한 용도입니다.
    이 함수 자체도 별도 캐시(`_logref_by_protected_cache`)를 먼저 확인하므로,
    같은 프로세스에서 반복 호출해도 DB를 다시 때리지 않습니다.
    """
    key = f"{guild_id}:{protected_message_id}"

    cached = _logref_by_protected_cache.get(key)
    if cached is not None:
        return dict(cached)

    str_gid = str(guild_id)
    str_pmid = str(protected_message_id)

    if logref_col is not None:
        try:
            doc = await _db(logref_col.find_one, {"guild_id": str_gid, "protected_message_id": str_pmid})
            if doc:
                entry = {k: v for k, v in doc.items() if k != "_id"}
                _logref_by_protected_cache.set(key, entry)
                return dict(entry)
            return None
        except Exception as e:
            print(f"MongoDB get_log_reference_by_protected error: {e}, using local file.", flush=True)

    async with _logref_lock:
        all_refs = _read_json(LOGREF_FILE)
    for entry in all_refs.values():
        if entry.get("guild_id") == str_gid and entry.get("protected_message_id") == str_pmid:
            _logref_by_protected_cache.set(key, entry)
            return dict(entry)
    return None


async def get_guarded_keys(guild_id: int) -> set[str]:
    """보호 중인 링크 식별자 집합을 반환합니다 (메모리 캐시 우선)."""
    str_gid = str(guild_id)

    cached = _guarded_keys_cache.get(str_gid)
    if cached is not None:
        return set(cached)

    result: set[str] = set()

    if guardkey_col is not None:
        try:
            def _fetch():
                return [d.get("key", "") for d in guardkey_col.find({"guild_id": str_gid})]
            result = set(await _db(_fetch))
            result.discard("")
            _guarded_keys_cache[str_gid] = set(result)
            return set(result)
        except Exception as e:
            print(f"MongoDB get_guarded_keys error: {e}, using local file.", flush=True)

    async with _guardkey_lock:
        all_keys = _read_json(GUARDKEY_FILE)
    result = set(all_keys.get(str_gid, []))
    _guarded_keys_cache[str_gid] = set(result)
    return set(result)
