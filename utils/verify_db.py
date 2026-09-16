"""
인증 & 링크 보호 전용 데이터 계층.

이 모듈은 포인트/상점/뽑기/제보 시스템(utils/database.py)과 완전히 분리되어 있습니다.
독립된 MongoDB 컬렉션과 독립된 로컬 JSON 파일만 사용하며,
기존 이코노미 코드를 전혀 import 하지 않습니다.
"""

import os
import json
import re
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
ROBLOX_FILE = os.path.join(DATA_DIR, "roblox_links.json")
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
_roblox_lock = asyncio.Lock()
_biome_lock = asyncio.Lock()

verify_col = None
linkguard_col = None
plink_col = None
clicklog_col = None
watermark_col = None
guardkey_col = None
usage_col = None
logref_col = None
roblox_col = None
biome_col = None

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
        roblox_col = _db["roblox_links"]
        biome_col = _db["biome_events"]

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
            # 유출 감지 색인. TTL 도 상한도 없어 영구 누적이었습니다.
            # 보호 링크 자체가 7일 TTL 이므로 그보다 넉넉한 30일로 둡니다.
            # (지난 링크를 다시 뿌리는 것도 한동안은 잡아야 하므로)
            guardkey_col.create_index(
                "updated", name="guardkey_ttl", expireAfterSeconds=30 * 24 * 3600
            )
            # 로블록스 계정 1개는 디스코드 1개에만 연동되어야 합니다.
            # 유출자가 남의 계정을 등록해 명단을 오염시키는 것을 막습니다.
            roblox_col.create_index(
                [("guild_id", 1), ("roblox_id", 1)], name="roblox_unique", unique=True
            )
            # 바이옴 등장 기록. 통계 화면이 기간으로 잘라 보므로 시간 인덱스가
            # 필요합니다(없으면 풀스캔).
            biome_col.create_index(
                [("guild_id", 1), ("kind", 1), ("day", -1)], name="biome_by_day"
            )
            # 90일. 통계는 '지난달 대비' 를 보고 싶어지므로 조사용(30일)보다
            # 넉넉히 둡니다. 한 건이 200바이트 남짓이라 부담이 없습니다.
            biome_col.create_index(
                "updated", name="biome_ttl", expireAfterSeconds=90 * 24 * 3600
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
        roblox_col = None


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
    # 로블록스 연동을 마치는 **즉시** 봇이 자동으로 주는 역할.
    # 이 역할에만 채널 보기 권한을 주면 입장 후 연동을 강제할 수
    # 있는데, 그래서 **부여가 실패하면 그 사람은 아무것도 못 봅니다.**
    # 봇 역할이 이 역할보다 위에 있어야 하고, 설정 화면이 그걸 검사합니다.
    "linked_role_id": None,
    # 링크 보호에 막힌 사람에게 "여기서 인증하세요" 라고 가리킬 대표 채널.
    # 여러 곳에 올렸다면 **가장 마지막에 올린 곳**이 됩니다.
    "panel_channel_id": None,
    # 실제로 올려둔 패널들의 위치 [{"c": 채널ID, "m": 메시지ID}, ...]
    # 패널은 여러 채널에 올릴 수 있고 전부 동시에 동작합니다.
    # 자격 역할 같은 설정이 바뀌면 이 목록을 보고 **전부 다시 그립니다.**
    # 기록하지 않으면 옛 조건을 보여주는 패널이 서버 곳곳에 남습니다.
    "panel_messages": [],
    "title": "✅ 서버 인증",
    "description": "아래 **[인증하기]** 버튼을 눌러 인증을 완료해 주세요.\n"
                   "인증이 완료되면 보호된 링크를 열람하실 수 있습니다.",
    "button_label": "인증하기",
    # 인증을 신청하려면 이 중 **하나 이상**을 갖고 있어야 합니다.
    # 비어 있으면 조건 없이 누구나 인증할 수 있습니다(기존 동작).
    # 스나이퍼가 서버에 막 들어와 바로 인증받는 것을 막는 장치입니다.
    "required_role_ids": [],
    # 멤버가 로블록스 계정을 다른 계정으로 바꾸면 로그 채널에 알릴지.
    # 갑작스러운 계정 교체는 그 자체로 확인해 볼 만한 신호입니다.
    "roblox_relink_alert": True,
    # 인증을 받으려면 로블록스 계정 연동까지 마쳐야 하는지.
    # 켜면 "역할 조건 + 연동" 을 모두 만족해야 인증 역할이 나갑니다.
    # 게임 내 플레이어 목록과 대조할 수 있는 사람만 링크를 받게 되므로,
    # 외부 유출을 잡아낼 확률이 크게 올라갑니다.
    "require_roblox": False,
    # 필요 역할을 잃으면 인증 역할도 자동으로 회수할지.
    # 잘못 설정하면 멀쩡한 인증자까지 줄줄이 풀리므로 기본은 꺼둡니다.
    "auto_revoke": False,
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
            result["required_role_ids"] = [int(r) for r in result.get("required_role_ids", [])]
            _verify_cache[str_gid] = dict(result)
            return dict(result)
        except Exception as e:
            print(f"MongoDB get_verify_settings error: {e}, using local file.", flush=True)

    async with _verify_lock:
        all_settings = _read_json(VERIFY_FILE)
    if str_gid in all_settings:
        result.update(all_settings[str_gid])
    result["required_role_ids"] = [int(r) for r in result.get("required_role_ids", [])]
    _verify_cache[str_gid] = dict(result)
    return dict(result)


def peek_verify_settings(guild_id: int) -> dict | None:
    """캐시에 있는 인증 설정을 **동기로** 읽습니다.

    Flask 스레드(중계 게이트)에서 호출하므로 await 를 쓸 수 없습니다.
    캐시에 없으면 None 을 돌려주고, 호출부는 그때 판정을 건너뜁니다.
    """
    cached = _verify_cache.get(str(guild_id))
    return dict(cached) if cached else None


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
    # 유출 감지에서 제외할 채널·카테고리.
    # 사람이 자기 개인서버를 정상적으로 공유하는 채널이 있으면 필요합니다.
    # **중계 주소(/j/) 유출은 여기서도 잡습니다** — 그건 한 사람에게만
    # 발급된 개인 주소라 어느 채널에서든 올라올 이유가 없습니다.
    "exception_channel_ids": [],
    # 보호를 **일부러 끄는** 채널. 원본 임베드가 그대로 남습니다.
    # 관리자만 볼 수 있어야 하며, UI 가 저장할 때 그걸 검사합니다.
    "no_guard_channel_ids": [],
    # 희귀 바이옴이 뜨면 **원본 링크를 그대로** 복사해 둘 관리자 채널.
    # 규칙(`notify_rules`)에 적힌 희귀 바이옴만 갑니다.
    "rare_copy_channel_id": None,
    # 로블록스 링크만 보호 대상으로 삼습니다.
    # 매크로 임베드에 함께 들어있는 제작자 홍보 링크 등을 잘못 가로채지 않도록 하는 안전장치입니다.
    "roblox_only": True,
    # 링크 중계: 로블록스 링크를 그대로 주지 않고 만료되는 1회성 주소로 바꿔 전달합니다.
    # 유출된 링크를 무효화하지만, 봇의 웹 서버가 죽으면 아무도 입장할 수 없게 되는
    # 단일 장애점이 생깁니다. 그래서 기본값은 꺼둔 상태입니다.
    "relay_links": False,

    # ── 재게시할 때 봇이 직접 붙이는 알림 멘션 ──────────────────────
    # 매크로 임베드에는 멘션이 **아예 없는** 경우가 많습니다(MultiScope 가
    # 그렇습니다: 임베드 + 링크 버튼뿐). 원본에 없으면 살릴 것도 없으므로,
    # 사람들이 바이옴이 뜬 것을 알게 하려면 봇이 붙여야 합니다.
    #
    #   {"channel_ids": [...], "category_ids": [...],
    #    "biomes": ["DREAMSPACE", ...],
    #    "mention": "everyone" | "here" | "roles", "role_ids": [...]}
    #
    # · channel_ids/category_ids 가 비어 있으면 **모든 감시 채널**에 적용됩니다.
    # · biomes 가 비어 있으면 **모든 바이옴**에 적용됩니다.
    # · 먼저 있는 규칙이 먼저 적용됩니다(처음 맞는 것 하나만).
    "notify_rules": [],

    # 원본 **임베드 안**에 적혀 있던 역할 멘션을 본문으로 끌어올립니다.
    # 임베드 안의 `<@&역할>` 은 글자로만 보이고 알림이 가지 않습니다.
    "carry_embed_mentions": True,

    # 이 말이 임베드에 들어 있으면 **봇은 멘션을 붙이지 않습니다.**
    # 바이옴 종료 임베드에도 같은 바이옴 이름이 들어 있어서, 이것이 없으면
    # 뜰 때와 끝날 때 두 번 울립니다.
    #
    # 실제 매크로 6종이 전부 `Biome Ended - <이름>` 이라 `Biome End` 하나로
    # 덮입니다(Ended · Ending · End 전부). 매크로가 늘어도 이 표기는 같습니다.
    #
    # 기본값을 `Ended` 처럼 **한 단어로** 두지 않은 것은 일부러입니다 — 비교가
    # '들어있는지' 로 도는데 `Recommended` `Extended` 안에도 `ended` 가 있어서,
    # **시작 알림이 조용히 안 울리는** 쪽으로 틀립니다. 잘못 울리는 것보다
    # 찾기 어렵습니다.
    "notify_exclude": ["Biome End", "바이옴 종료"],

    # 희귀 바이옴 실시간 카운터 보드. 지정한 채널의 **메시지 하나를 계속
    # 수정**합니다. 매번 새로 보내면 도배가 되어 정작 중요한 신호가 묻힙니다
    # (봇 상태 임베드와 같은 방식).
    "biome_board_channel_id": None,
    "biome_board_message_id": None,
}


def _normalize_notify_rules(rules) -> list:
    """저장된 알림 규칙의 ID 를 전부 int 로 맞춥니다.

    JSON 폴백을 거치면 숫자가 문자열이 되어 돌아옵니다. 그대로 두면
    `base.id in cids` 가 조용히 False 가 되어 **규칙이 있는데 안 울립니다.**
    """
    out = []
    for rule in rules or []:
        if not isinstance(rule, dict):
            continue
        clean = dict(rule)
        for key in ("channel_ids", "category_ids", "role_ids"):
            ids = []
            for v in clean.get(key) or []:
                try:
                    ids.append(int(v))
                except (TypeError, ValueError):
                    continue
            clean[key] = ids
        clean["biomes"] = [str(b) for b in (clean.get("biomes") or []) if str(b).strip()]
        if clean.get("mention") not in ("everyone", "here", "roles"):
            clean["mention"] = "roles"
        out.append(clean)
    return out


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
            result["notify_rules"] = _normalize_notify_rules(result.get("notify_rules"))
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
    result["notify_rules"] = _normalize_notify_rules(result.get("notify_rules"))
    _linkguard_cache[str_gid] = dict(result)
    return dict(result)


async def save_linkguard_settings(guild_id: int, settings: dict) -> None:
    str_gid = str(guild_id)

    current = _linkguard_cache.get(str_gid) or dict(DEFAULT_LINKGUARD_SETTINGS)
    current.update(settings)
    current["channel_ids"] = [int(c) for c in current.get("channel_ids", [])]
    current["category_ids"] = [int(c) for c in current.get("category_ids", [])]
    current["notify_rules"] = _normalize_notify_rules(current.get("notify_rules"))
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


# 매크로는 **같은 개인서버 링크를 상태 갱신마다 다시 올립니다.**
# 실측(2026-09-10): 보호 21,227건에 고유 링크는 146개 — 하나당 평균 145번,
# 최근 1시간 중복률 83%. 그때마다 TTL 갱신을 쓰면 의미 없는 쓰기가 쌓입니다.
# TTL 이 30일이므로 **1시간에 한 번만 갱신해도 넉넉합니다.**
#
# 재시작하면 이 기억이 비어 다시 쓰지만, 그건 한 번뿐이라 문제가 없습니다.
GUARDKEY_REFRESH_SECONDS = 3600
GUARDKEY_MEMO_MAX = 5000
_guardkey_written: dict[str, float] = {}


async def register_guarded_keys(guild_id: int, keys: list[str]) -> None:
    """이 길드에서 보호 중인 링크 식별자(개인서버 코드 등)를 색인에 추가합니다."""
    str_gid = str(guild_id)
    bucket = _guarded_keys_cache.setdefault(str_gid, set())
    # **메모리 색인은 항상 갱신합니다.** 유출 감지가 이걸 보기 때문에,
    # 쓰기를 건너뛴다고 여기까지 건너뛰면 방금 보호한 링크를 못 알아봅니다.
    bucket.update(keys)

    now_mono = time.monotonic()
    fresh = [k for k in keys
             if now_mono - _guardkey_written.get(f"{str_gid}:{k}", -1e9)
             >= GUARDKEY_REFRESH_SECONDS]
    if not fresh:
        return              # 최근에 갱신했습니다 — 다시 쓸 이유가 없습니다

    def _memo():
        for k in fresh:
            _guardkey_written[f"{str_gid}:{k}"] = now_mono
        if len(_guardkey_written) > GUARDKEY_MEMO_MAX:
            # dict 는 삽입 순서를 유지하므로 앞쪽이 가장 오래된 항목입니다
            for old_key in list(_guardkey_written)[:GUARDKEY_MEMO_MAX // 2]:
                _guardkey_written.pop(old_key, None)

    if guardkey_col is not None:
        try:
            def _write():
                now = datetime.now(timezone.utc)
                for key in fresh:
                    guardkey_col.update_one(
                        {"_id": f"{guild_id}:{key}"},
                        # updated 가 있어야 TTL 인덱스가 동작합니다.
                        # 같은 링크가 다시 보호되면 갱신되어 수명이 늘어납니다.
                        {"$set": {"guild_id": str_gid, "key": key,
                                  "updated": now}},
                        upsert=True
                    )
            await _db(_write)
            _memo()         # 쓰기에 성공했을 때만 기억합니다
            return
        except Exception as e:
            print(f"MongoDB register_guarded_keys error: {e}, using local file.", flush=True)

    async with _guardkey_lock:
        all_keys = _read_json(GUARDKEY_FILE)
        existing = set(all_keys.get(str_gid, []))
        existing.update(keys)
        all_keys[str_gid] = sorted(existing)
        _write_json(GUARDKEY_FILE, all_keys)
    _memo()


async def record_link_usage(guild_id: int, message_id: int,
                            user_id: int, user_name: str,
                            issued_url: str = "", watermark: str = "") -> tuple[int, bool]:
    """보호 메시지별로 '누가 링크를 사용했는지'를 기록합니다.

    반환값은 (지금까지 사용한 서로 다른 유저 수, 이번이 첫 사용인지) 입니다.
    임베드(보호 메시지)마다 따로 집계됩니다.

    issued_url 은 그 유저에게 실제로 발급한 주소입니다. 나중에 '누구에게
    무엇을 줬는지' 감사할 수 있도록 남깁니다. 같은 유저가 다시 누르면
    최신 발급분으로 갱신됩니다.
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
    if issued_url:
        entry["link"] = issued_url
    if watermark:
        entry["wm"] = watermark
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
                        **({f"users.{uid}.link": issued_url} if issued_url else {}),
                        **({f"users.{uid}.wm": watermark} if watermark else {}),
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
            "link": info.get("link", ""),
            "watermark": info.get("wm", ""),
        }
        for uid, info in bucket.items()
    ]
    rows.sort(key=lambda r: r.get("last", ""), reverse=True)
    return rows


_ISSUED_TOKEN_RE = re.compile(r"/j/([A-Za-z0-9_\-]+)")


def _issued_token(value: str) -> str:
    """발급 주소에서 토큰만 뽑습니다. 토큰만 들어오면 그대로 씁니다."""
    m = _ISSUED_TOKEN_RE.search(value or "")
    return m.group(1) if m else ""


async def find_issued_to(guild_id: int, needle: str) -> dict | None:
    """발급 주소(또는 그 안의 토큰)로 **누구에게 준 링크인지** 되찾습니다.

    중계 주소는 90초면 죽지만 발급 기록은 `link_usage` 에 30일 남습니다.
    그래서 토큰이 만료된 뒤에 그 주소가 게시돼도 소유자를 특정할 수 있습니다.
    워터마크와 달리 로블록스를 거치지 않으므로 **표식이 사라질 일이 없고**,
    누구 것인지가 추측이 아니라 발급 기록으로 나옵니다.

    문서를 훑지만, 중계 주소가 서버에 게시되는 일 자체가 드물고
    보호 메시지 1개당 문서 1개(30일 TTL)라 부담이 없습니다.
    """
    if not needle:
        return None
    str_gid = str(guild_id)
    # **부분 문자열로 맞추면 안 됩니다.** 모든 주소에 들어 있는 조각
    # (예: 도메인)을 넘기면 아무나 짚혀서, 엉뚱한 사람이 유출자로
    # 지목됩니다. 양쪽에서 토큰만 뽑아 정확히 비교합니다.
    wanted = _issued_token(needle) or needle

    def _hit(uid, info: dict, message_id) -> dict:
        return {
            "user_id": str(uid),
            "user_name": info.get("name", "?"),
            "message_id": str(message_id),
            "last": info.get("last", ""),
            "watermark": info.get("wm", ""),
        }

    if usage_col is not None:
        try:
            def _scan():
                for doc in usage_col.find({"guild_id": str_gid},
                                          {"users": 1, "message_id": 1}):
                    for uid, info in (doc.get("users") or {}).items():
                        if _issued_token(info.get("link")) == wanted:
                            return _hit(uid, info, doc.get("message_id", ""))
                return None
            return await _db(_scan)
        except Exception as e:
            print(f"MongoDB find_issued_to error: {e}, using local file.", flush=True)

    async with _usage_lock:
        all_usage = _read_json(USAGE_FILE)
    prefix = f"{str_gid}:"
    for key, bucket in all_usage.items():
        if not key.startswith(prefix):
            continue
        for uid, info in (bucket or {}).items():
            if _issued_token(info.get("link")) == wanted:
                return _hit(uid, info, key[len(prefix):])
    return None


async def get_usage_summary(guild_id: int) -> dict:
    """이 길드에서 **보호된 링크를 실제로 받아 간 사람** 전체 집계.

    보호 메시지마다 따로 쌓인 기록을 사람 기준으로 합칩니다.
    반환: `{user_id: {"name", "count"(총 클릭), "links"(서로 다른 링크 수), "last"}}`

    **여기 없다고 곧 유출은 아닙니다.** 보관 기간(30일)이 지났을 수도 있고,
    보호되지 않은 경로(사람이 직접 공유하는 채널)로 받았을 수도 있습니다.
    화면에서 그렇게 안내해야 합니다.
    """
    str_gid = str(guild_id)
    out: dict[str, dict] = {}

    def _add(uid, info: dict) -> None:
        row = out.setdefault(str(uid),
                             {"name": "", "count": 0, "links": 0, "last": ""})
        row["name"] = info.get("name") or row["name"]
        row["count"] += int(info.get("count", 1) or 1)
        row["links"] += 1
        last = info.get("last") or ""
        if last > row["last"]:
            row["last"] = last

    if usage_col is not None:
        try:
            def _fetch():
                return list(usage_col.find({"guild_id": str_gid}, {"users": 1}))
            for doc in await _db(_fetch):
                for uid, info in (doc.get("users") or {}).items():
                    _add(uid, info)
            return out
        except Exception as e:
            print(f"MongoDB get_usage_summary error: {e}, using local file.", flush=True)

    async with _usage_lock:
        all_usage = _read_json(USAGE_FILE)
    prefix = f"{str_gid}:"
    for key, bucket in all_usage.items():
        if not key.startswith(prefix):
            continue
        for uid, info in (bucket or {}).items():
            _add(uid, info)
    return out


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


# ── 로블록스 계정 연동 ────────────────────────────────────────────────
#
# 개인서버 링크를 받아간 사람의 로블록스 아이디를 알아야, 게임 안 플레이어
# 목록과 대조해 '명단에 없는데 들어와 있는 사람'을 찾아낼 수 있습니다.
# 그게 서버 밖 유출을 탐지하는 유일한 현실적 경로입니다.

_roblox_cache = _TTLCache(ttl_seconds=600, max_items=3000)


def _roblox_key(guild_id: int, user_id: int) -> str:
    return f"{guild_id}:{user_id}"


async def get_roblox_link(guild_id: int, user_id: int) -> dict | None:
    """이 디스코드 유저에게 연동된 로블록스 계정."""
    key = _roblox_key(guild_id, user_id)
    cached = _roblox_cache.get(key)
    if cached is not None:
        return dict(cached) if cached else None

    if roblox_col is not None:
        try:
            doc = await _db(roblox_col.find_one, {"_id": key})
            entry = {k: v for k, v in doc.items() if k != "_id"} if doc else None
            _roblox_cache.set(key, entry or {})
            return dict(entry) if entry else None
        except Exception as e:
            print(f"MongoDB get_roblox_link error: {e}, using local file.", flush=True)

    async with _roblox_lock:
        entry = _read_json(ROBLOX_FILE).get(key)
    _roblox_cache.set(key, entry or {})
    return dict(entry) if entry else None


async def find_roblox_owner(guild_id: int, roblox_id: int) -> dict | None:
    """이 로블록스 계정을 이미 연동한 디스코드 유저가 있는지.

    로블록스 계정 하나는 디스코드 하나에만 연동되어야 합니다. 그러지 않으면
    여러 명이 같은 계정을 등록해 명단 대조가 무의미해집니다.
    """
    str_gid, rid = str(guild_id), int(roblox_id)
    if roblox_col is not None:
        try:
            doc = await _db(roblox_col.find_one,
                            {"guild_id": str_gid, "roblox_id": rid})
            return {k: v for k, v in doc.items() if k != "_id"} if doc else None
        except Exception as e:
            print(f"MongoDB find_roblox_owner error: {e}, using local file.", flush=True)

    async with _roblox_lock:
        for entry in _read_json(ROBLOX_FILE).values():
            if entry.get("guild_id") == str_gid and int(entry.get("roblox_id", 0)) == rid:
                return dict(entry)
    return None


async def save_roblox_link(guild_id: int, user_id: int, roblox_id: int,
                           roblox_name: str, method: str = "oauth",
                           created_at: str = "") -> None:
    """연동을 저장합니다. 같은 디스코드 유저면 덮어씁니다(재연동)."""
    key = _roblox_key(guild_id, user_id)
    entry = {
        "guild_id": str(guild_id),
        "user_id": str(user_id),
        "roblox_id": int(roblox_id),
        # 로블록스는 개명이 가능하므로 표시용 이름은 갱신 대상입니다.
        # 불변 식별자는 roblox_id 이며, 대조·중복검사는 항상 이 값으로 합니다.
        "roblox_name": roblox_name,
        "method": method,
        "roblox_created_at": created_at,
        "linked_at": datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S"),
    }
    _roblox_cache.set(key, entry)

    if roblox_col is not None:
        try:
            await _db(roblox_col.update_one, {"_id": key}, {"$set": entry}, upsert=True)
            return
        except Exception as e:
            print(f"MongoDB save_roblox_link error: {e}, using local file.", flush=True)

    async with _roblox_lock:
        all_links = _read_json(ROBLOX_FILE)
        all_links[key] = entry
        _write_json(ROBLOX_FILE, all_links)


async def remove_roblox_link(guild_id: int, user_id: int) -> bool:
    """연동 해제 (관리자용)."""
    key = _roblox_key(guild_id, user_id)
    _roblox_cache.pop(key)
    if roblox_col is not None:
        try:
            res = await _db(roblox_col.delete_one, {"_id": key})
            return res.deleted_count > 0
        except Exception as e:
            print(f"MongoDB remove_roblox_link error: {e}, using local file.", flush=True)

    async with _roblox_lock:
        all_links = _read_json(ROBLOX_FILE)
        existed = key in all_links
        all_links.pop(key, None)
        _write_json(ROBLOX_FILE, all_links)
    return existed


async def list_roblox_links(guild_id: int, limit: int = 1000) -> list[dict]:
    """길드의 연동 명단 (최신순)."""
    str_gid = str(guild_id)
    if roblox_col is not None:
        try:
            def _fetch():
                return list(roblox_col.find({"guild_id": str_gid})
                            .sort("_id", -1).limit(limit))
            return [{k: v for k, v in d.items() if k != "_id"} for d in await _db(_fetch)]
        except Exception as e:
            print(f"MongoDB list_roblox_links error: {e}, using local file.", flush=True)

    async with _roblox_lock:
        rows = [e for e in _read_json(ROBLOX_FILE).values()
                if e.get("guild_id") == str_gid]
    rows.sort(key=lambda r: r.get("linked_at", ""), reverse=True)
    return rows[:limit]


async def get_roblox_names(guild_id: int, user_ids: list) -> dict:
    """여러 디스코드 유저의 로블록스 이름을 한 번에 조회합니다.

    링크 사용 명단에 로블록스 아이디를 함께 보여주기 위한 것으로,
    유저 수만큼 조회를 반복하지 않도록 한 번에 가져옵니다.
    """
    str_gid = str(guild_id)
    wanted = {str(u) for u in user_ids}
    if not wanted:
        return {}

    if roblox_col is not None:
        try:
            def _fetch():
                return list(roblox_col.find(
                    {"guild_id": str_gid, "user_id": {"$in": list(wanted)}},
                    {"user_id": 1, "roblox_name": 1, "roblox_id": 1}))
            return {str(d["user_id"]): d.get("roblox_name", "")
                    for d in await _db(_fetch) if d.get("user_id")}
        except Exception as e:
            print(f"MongoDB get_roblox_names error: {e}, using local file.", flush=True)

    async with _roblox_lock:
        all_links = _read_json(ROBLOX_FILE)
    return {str(e["user_id"]): e.get("roblox_name", "")
            for e in all_links.values()
            if e.get("guild_id") == str_gid and str(e.get("user_id")) in wanted}


async def search_roblox_links(guild_id: int, query: str, limit: int = 10) -> list[dict]:
    """연동 명단을 검색합니다.

    명단이 커지면 목록을 훑는 것이 비현실적이라, 어느 방향으로든 찾을 수
    있게 합니다. 특히 게임에서 낯선 이름을 봤을 때 '우리 멤버인가'를
    역으로 확인하는 용도가 중요합니다.

    query 는 다음 중 무엇이든 됩니다.
      · 디스코드 ID 또는 멘션      → 그 사람의 로블록스 계정
      · 로블록스 숫자 ID           → 그 계정의 주인
      · 로블록스 유저명(부분 일치) → 해당하는 사람들
    """
    str_gid = str(guild_id)
    raw = (query or "").strip()
    if not raw:
        return []

    digits = "".join(ch for ch in raw if ch.isdigit())
    results: list[dict] = []
    seen: set[str] = set()

    def _add(entry: dict, how: str) -> None:
        if not entry:
            return
        key = str(entry.get("user_id"))
        if key in seen:
            return
        seen.add(key)
        results.append({**entry, "matched_by": how})

    # 1) 숫자면 디스코드 ID 와 로블록스 ID 양쪽으로 시도합니다.
    #    디스코드 ID 는 17~20자리, 로블록스 ID 는 보통 그보다 짧습니다.
    if digits:
        if len(digits) >= 17:
            _add(await get_roblox_link(guild_id, int(digits)), "discord_id")
        try:
            _add(await find_roblox_owner(guild_id, int(digits)), "roblox_id")
        except (ValueError, OverflowError):
            pass

    # 2) 로블록스 유저명 (대소문자 무시, 부분 일치)
    if len(results) < limit and raw:
        needle = raw.lower()
        if roblox_col is not None:
            try:
                import re as _re
                pattern = _re.escape(raw)

                def _fetch():
                    return list(roblox_col.find({
                        "guild_id": str_gid,
                        "roblox_name": {"$regex": pattern, "$options": "i"},
                    }).limit(limit))
                for doc in await _db(_fetch):
                    _add({k: v for k, v in doc.items() if k != "_id"}, "roblox_name")
            except Exception as e:
                print(f"MongoDB search_roblox_links error: {e}, using local file.", flush=True)

        if len(results) < limit:
            async with _roblox_lock:
                rows = [e for e in _read_json(ROBLOX_FILE).values()
                        if e.get("guild_id") == str_gid]
            for e in rows:
                if needle in str(e.get("roblox_name", "")).lower():
                    _add(e, "roblox_name")
                    if len(results) >= limit:
                        break

    return results[:limit]


# ── 바이옴 등장 기록 (통계용) ─────────────────────────────────────────
#
# 지금까지 어디에도 **바이옴 이름이 남지 않았습니다.** `link_usage` 는 누가
# 눌렀는지만, `protected_links` 는 어떤 링크인지만 압니다.
#
# **세는 단위는 '바이옴 시작 임베드 하나' 입니다.** 예전에는 같은 링크를 한
# 건으로 묶었는데, 개인서버 링크는 매 임베드마다 바꿀 수 없어서 **같은 서버에서
# 나중에 다시 뜬 바이옴이 통째로 묻혔습니다.** (알림 쿨타임을 걷어낸 것과 같은
# 이유입니다 — 링크는 바이옴 사건의 신원이 아닙니다.)
#
# 대신 **날짜 버킷**에 모읍니다. 등장마다 문서를 하나씩 만들면 하루 3,000건씩
# 쌓여 백업까지 부풀지만(실측: 3일에 재게시 9,990건), 버킷은
# `바이옴 × 날짜` 라 90일 보관해도 문서 수천 개입니다.
#   _id = "<길드>:<바이옴>:<KST 날짜>"
# `updated`(그날 마지막 활동)에 TTL 이 걸려 있어 90일 뒤 스스로 사라집니다.

BIOME_FILE = os.path.join(DATA_DIR, "biome_events.json")

# 하루·한 바이옴에 기억할 고유 링크 수 상한. 고유 서버 수를 보여주기 위한
# 것이라 정확할 필요는 없고, 배열이 무한히 자라는 것만 막으면 됩니다.
BIOME_LINKS_PER_DAY = 300


def _biome_day(now=None) -> str:
    """KST 기준 날짜 문자열. 자정 경계는 한국 시간으로 갈라야 합니다."""
    return (now or datetime.now(KST)).astimezone(KST).strftime("%Y-%m-%d")


def _biome_bucket_id(guild_id: int, biome: str, day: str) -> str:
    return f"{guild_id}:{(biome or '?')[:60]}:{day}"


async def record_biome_event(guild_id: int, biome: str, link_key_str: str,
                             channel_id: int, message_id: int,
                             notified: bool) -> None:
    """바이옴 시작 임베드 하나를 집계에 더합니다.

    `notified` 는 그 등장에서 **알림이 실제로 나갔는지**입니다. "희귀 바이옴이
    떴는데 알림이 안 왔다" 를 가려내는 것이 통계의 존재 이유라, 나간 횟수도
    함께 셉니다.
    """
    now = datetime.now(timezone.utc)
    day = _biome_day()
    key = _biome_bucket_id(guild_id, biome, day)
    link = (link_key_str or "")[:80]

    if biome_col is not None:
        try:
            upd = {"$set": {"updated": now, "channel_id": int(channel_id),
                            "message_id": int(message_id)},
                   "$inc": {"count": 1, "notified_count": 1 if notified else 0},
                   "$setOnInsert": {"guild_id": str(guild_id), "kind": "day",
                                    "biome": biome or "?", "day": day,
                                    "first_seen": now}}
            if link:
                # 고유 서버 수를 보여주기 위한 것입니다. **읽고 나서 쓰지
                # 않습니다** — 보호는 시간당 1,000건이라 왕복을 하나 더 넣으면
                # 그만큼 핫패스가 느려집니다. 실측상 하루 고유 링크는 수십 개라
                # 배열이 문제될 크기로 자라지 않습니다.
                upd["$addToSet"] = {"links": link}
            await _db(biome_col.update_one, {"_id": key}, upd, upsert=True)
            return
        except Exception as e:
            print(f"MongoDB record_biome_event error: {e}, using local file.", flush=True)

    async with _biome_lock:
        all_rows = _read_json(BIOME_FILE)
        row = all_rows.get(key) or {"guild_id": str(guild_id), "kind": "day",
                                    "biome": biome or "?", "day": day,
                                    "first_seen": now.isoformat(),
                                    "count": 0, "notified_count": 0, "links": []}
        row["count"] = int(row.get("count", 0)) + 1
        if notified:
            row["notified_count"] = int(row.get("notified_count", 0)) + 1
        if link and link not in row.get("links", []) and len(row.get("links", [])) < BIOME_LINKS_PER_DAY:
            row.setdefault("links", []).append(link)
        row["updated"] = now.isoformat()
        row["channel_id"] = int(channel_id)
        row["message_id"] = int(message_id)
        all_rows[key] = row
        _write_json(BIOME_FILE, all_rows)


# 하루에 기억할 '못 읽은 제목' 표본 수. 몇 개만 있으면 어떤 형식인지
# 알아보기 충분하고, 개수는 따로 세므로 표본을 늘릴 이유가 없습니다.
BIOME_UNPARSED_SAMPLES = 12


def _biome_unparsed_id(guild_id: int, day: str) -> str:
    return f"{guild_id}:__unparsed__:{day}"


async def record_biome_unparsed(guild_id: int, title: str) -> None:
    """보호는 했는데 **바이옴 이름을 못 읽은** 임베드를 셉니다.

    보호까지 온 메시지는 개인서버 링크가 든 매크로 알림입니다. 그런데
    이름을 못 뽑았다면 우리가 모르는 제목 형식이 들어온 것이고, 그때부터
    그 매크로가 올리는 바이옴은 **통계에서 통째로 빠집니다.** 조용히
    버리지 않고 세어 두어야 `/바이옴통계` 가 그 사실을 말해 줍니다.

    `kind: "unparsed"` 라 `get_biome_buckets()` 의 집계에는 섞이지
    않습니다 — 섞이면 숫자가 조용히 틀어집니다.
    """
    now = datetime.now(timezone.utc)
    day = _biome_day()
    key = _biome_unparsed_id(guild_id, day)
    sample = str(title or "")[:80]

    if biome_col is not None:
        try:
            upd = {"$set": {"updated": now},
                   "$inc": {"count": 1},
                   "$setOnInsert": {"guild_id": str(guild_id), "kind": "unparsed",
                                    "day": day, "first_seen": now}}
            if sample:
                upd["$addToSet"] = {"titles": sample}
            await _db(biome_col.update_one, {"_id": key}, upd, upsert=True)
            return
        except Exception as e:
            print(f"MongoDB record_biome_unparsed error: {e}, using local file.", flush=True)

    async with _biome_lock:
        all_rows = _read_json(BIOME_FILE)
        row = all_rows.get(key) or {"guild_id": str(guild_id), "kind": "unparsed",
                                    "day": day, "first_seen": now.isoformat(),
                                    "count": 0, "titles": []}
        row["count"] = int(row.get("count", 0)) + 1
        if sample and sample not in row.get("titles", []) \
                and len(row.get("titles", [])) < BIOME_UNPARSED_SAMPLES:
            row.setdefault("titles", []).append(sample)
        row["updated"] = now.isoformat()
        all_rows[key] = row
        _write_json(BIOME_FILE, all_rows)


async def get_biome_unparsed(guild_id: int, since_days: int = 7) -> dict:
    """기간 안에 못 읽은 제목의 개수와 표본을 모읍니다."""
    days = max(1, int(since_days))
    cutoff_day = (datetime.now(KST) - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    out = {"count": 0, "titles": []}

    rows = []
    if biome_col is not None:
        try:
            def _fetch():
                return list(biome_col.find(
                    {"guild_id": str(guild_id), "kind": "unparsed",
                     "day": {"$gte": cutoff_day}}).sort("day", -1).limit(400))
            rows = await _db(_fetch)
        except Exception as e:
            print(f"MongoDB get_biome_unparsed error: {e}, using local file.", flush=True)
            rows = []
    if not rows:
        async with _biome_lock:
            all_rows = _read_json(BIOME_FILE)
        rows = [r for r in all_rows.values()
                if str(r.get("guild_id")) == str(guild_id)
                and r.get("kind") == "unparsed"
                and str(r.get("day", "")) >= cutoff_day]

    for r in rows:
        out["count"] += int(r.get("count", 0) or 0)
        for t in (r.get("titles") or []):
            if t not in out["titles"] and len(out["titles"]) < BIOME_UNPARSED_SAMPLES:
                out["titles"].append(t)
    return out


async def get_biome_buckets(guild_id: int, since_days: int = 30) -> list[dict]:
    """기간 안의 날짜 버킷을 돌려줍니다 (최근 날짜 순).

    `kind: "day"` 로 거릅니다 — 옛 형식(링크별 문서)이 섞여 들어오면 집계
    기준이 두 가지가 되어 숫자가 조용히 틀어집니다.
    """
    days = max(1, int(since_days))
    cutoff_day = (datetime.now(KST) - timedelta(days=days - 1)).strftime("%Y-%m-%d")

    if biome_col is not None:
        try:
            def _fetch():
                # 커서는 **스레드 안에서 리스트로** 만들어 돌려줍니다.
                return list(biome_col.find(
                    {"guild_id": str(guild_id), "kind": "day",
                     "day": {"$gte": cutoff_day}}
                ).sort("day", -1).limit(5000))
            return await _db(_fetch)
        except Exception as e:
            print(f"MongoDB get_biome_buckets error: {e}, using local file.", flush=True)

    async with _biome_lock:
        all_rows = _read_json(BIOME_FILE)
    out = []
    for row in all_rows.values():
        if str(row.get("guild_id")) != str(guild_id) or row.get("kind") != "day":
            continue
        if str(row.get("day", "")) >= cutoff_day:
            out.append(dict(row))
    out.sort(key=lambda r: r.get("day", ""), reverse=True)
    return out
