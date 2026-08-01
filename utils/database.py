import os
import json
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
MONGO_URI = os.getenv("MONGO_URI")

_lock = asyncio.Lock()
_settings_lock = asyncio.Lock()
_shop_lock = asyncio.Lock()
_gacha_lock = asyncio.Lock()
_logs_lock = asyncio.Lock()
_blacklist_lock = asyncio.Lock()
KST = timezone(timedelta(hours=9))

# MongoDB setup
mongo_db = None
users_col = None
settings_col = None
shop_col = None
gacha_col = None
logs_col = None
blacklist_col = None

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
        print("Connected to MongoDB successfully for permanent data storage!", flush=True)
    except Exception as e:
        print(f"MongoDB connection warning: {e}. Falling back to local JSON storage.", flush=True)
        users_col = None
        settings_col = None
        shop_col = None
        gacha_col = None
        logs_col = None
        blacklist_col = None

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
            doc = users_col.find_one({"_id": str_key})
            if doc:
                return doc, False

            # Check for legacy doc without guild_id (with _id == user_id)
            old_doc = users_col.find_one({"_id": str_uid})
            if old_doc:
                new_doc = {
                    "_id": str_key,
                    "guild_id": str_gid,
                    "user_id": str_uid,
                    "points": old_doc.get("points", 0),
                    "last_checkin": old_doc.get("last_checkin", ""),
                    "chat_count": old_doc.get("chat_count", 0)
                }
                users_col.replace_one({"_id": str_key}, new_doc, upsert=True)
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

async def checkin_user(guild_id: int, user_id: int) -> tuple[bool, int]:
    str_key = _get_user_key(guild_id, user_id)
    today = get_today_str()

    if users_col is not None:
        try:
            doc, _ = await _migrate_old_user_if_needed(guild_id, user_id)
            if doc.get("last_checkin") == today:
                return False, doc.get("points", 0)

            new_points = doc.get("points", 0) + 100
            users_col.update_one(
                {"_id": str_key},
                {"$set": {"guild_id": str(guild_id), "user_id": str(user_id), "last_checkin": today, "points": new_points}},
                upsert=True
            )
            return True, new_points
        except Exception as e:
            print(f"MongoDB checkin error: {e}, using local file.", flush=True)

    # Local JSON fallback
    data = await load_data()
    user_info, _ = await _migrate_old_user_if_needed(guild_id, user_id, data)
    if user_info.get("last_checkin") == today:
        return False, user_info.get("points", 0)

    user_info["last_checkin"] = today
    user_info["points"] = user_info.get("points", 0) + 100
    await save_data(data)

    return True, user_info["points"]

async def process_chat_message(guild_id: int, user_id: int) -> tuple[int, bool]:
    str_key = _get_user_key(guild_id, user_id)

    if users_col is not None:
        try:
            doc, _ = await _migrate_old_user_if_needed(guild_id, user_id)
            new_count = doc.get("chat_count", 0) + 1
            awarded = False
            new_points = doc.get("points", 0)

            if new_count % 10 == 0:
                new_points += 10
                awarded = True

            users_col.update_one(
                {"_id": str_key},
                {"$set": {"guild_id": str(guild_id), "user_id": str(user_id), "chat_count": new_count, "points": new_points}},
                upsert=True
            )
            return new_points, awarded
        except Exception as e:
            print(f"MongoDB chat process error: {e}, using local file.", flush=True)

    # Local JSON fallback
    data = await load_data()
    user_info, _ = await _migrate_old_user_if_needed(guild_id, user_id, data)
    user_info["chat_count"] = user_info.get("chat_count", 0) + 1

    awarded = False
    if user_info["chat_count"] % 10 == 0:
        user_info["points"] = user_info.get("points", 0) + 10
        awarded = True

    await save_data(data)
    return user_info["points"], awarded

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
            doc, _ = await _migrate_old_user_if_needed(guild_id, user_id)
            current_points = doc.get("points", 0)
            new_points = max(0, current_points + amount)
            users_col.update_one(
                {"_id": str_key},
                {"$set": {"guild_id": str(guild_id), "user_id": str(user_id), "points": new_points}},
                upsert=True
            )
            return new_points
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
            doc, _ = await _migrate_old_user_if_needed(guild_id, user_id)
            current_points = doc.get("points", 0)
            if current_points < amount:
                return False, current_points

            new_points = current_points - amount
            users_col.update_one(
                {"_id": str_key},
                {"$set": {"guild_id": str(guild_id), "user_id": str(user_id), "points": new_points}},
                upsert=True
            )
            return True, new_points
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
            cursor = users_col.find({"guild_id": str_gid}).sort("points", -1).limit(limit)
            for doc in cursor:
                uid_val = doc.get("user_id") or doc["_id"].split(":")[-1]
                result.append({
                    "user_id": int(uid_val),
                    "points": doc.get("points", 0)
                })

            # If no guild-specific docs found, try migrating legacy docs (one-time)
            if not result:
                legacy_cursor = users_col.find({"guild_id": {"$exists": False}})
                for old_doc in legacy_cursor:
                    uid_str = old_doc["_id"]
                    # Skip non-numeric IDs
                    if not uid_str.isdigit():
                        continue
                    target_key = f"{str_gid}:{uid_str}"
                    if not users_col.find_one({"_id": target_key}):
                        users_col.replace_one(
                            {"_id": target_key},
                            {
                                "_id": target_key,
                                "guild_id": str_gid,
                                "user_id": uid_str,
                                "points": old_doc.get("points", 0),
                                "last_checkin": old_doc.get("last_checkin", ""),
                                "chat_count": old_doc.get("chat_count", 0)
                            },
                            upsert=True
                        )
                # Re-query after migration
                cursor = users_col.find({"guild_id": str_gid}).sort("points", -1).limit(limit)
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
            count_higher = users_col.count_documents({"guild_id": str_gid, "points": {"$gt": user_points}})
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
}

async def get_report_settings(guild_id: int) -> dict:
    """Get report settings for a guild. Returns defaults if not yet configured."""
    str_gid = str(guild_id)

    if settings_col is not None:
        try:
            doc = settings_col.find_one({"_id": str_gid})
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
            settings_col.update_one(
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
            doc = shop_col.find_one({"_id": str_gid})
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
            shop_col.update_one(
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
    "items": []
}

async def get_gacha_settings(guild_id: int) -> dict:
    str_gid = str(guild_id)

    if gacha_col is not None:
        try:
            doc = gacha_col.find_one({"_id": str_gid})
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

async def save_gacha_settings(guild_id: int, settings: dict) -> None:
    str_gid = str(guild_id)

    if gacha_col is not None:
        try:
            gacha_col.update_one(
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


# ── Activity Logs DB (per-guild) ──────────────────────────────────────

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
            logs_col.insert_one({"guild_id": str_gid, **log_entry})
            return
        except Exception as e:
            print(f"MongoDB add_activity_log error: {e}", flush=True)

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
            cursor = logs_col.find(query).sort("_id", -1).limit(limit)
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

    if blacklist_col is not None:
        try:
            doc = blacklist_col.find_one({"guild_id": str_gid, "user_id": str_uid})
            return doc is not None
        except Exception as e:
            print(f"MongoDB is_user_blacklisted error: {e}", flush=True)

    _ensure_data_dir()
    async with _blacklist_lock:
        try:
            if os.path.exists(BLACKLIST_FILE):
                with open(BLACKLIST_FILE, "r", encoding="utf-8") as f:
                    all_bl = json.load(f)
                guild_bl = all_bl.get(str_gid, {})
                return str_uid in guild_bl
        except Exception:
            pass
    return False

async def add_blacklist_user(guild_id: int, user_id: int, reason: str = "") -> None:
    str_gid = str(guild_id)
    str_uid = str(user_id)
    timestamp = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")

    entry = {
        "guild_id": str_gid,
        "user_id": str_uid,
        "reason": reason if reason else "사유 미입력",
        "timestamp": timestamp
    }

    if blacklist_col is not None:
        try:
            blacklist_col.update_one(
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

    if blacklist_col is not None:
        try:
            res = blacklist_col.delete_one({"guild_id": str_gid, "user_id": str_uid})
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
            cursor = blacklist_col.find({"guild_id": str_gid})
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
