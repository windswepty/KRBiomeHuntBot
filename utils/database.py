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
MONGO_URI = os.getenv("MONGO_URI")

_lock = asyncio.Lock()
_settings_lock = asyncio.Lock()
_shop_lock = asyncio.Lock()
_gacha_lock = asyncio.Lock()
_logs_lock = asyncio.Lock()
KST = timezone(timedelta(hours=9))

# MongoDB setup
mongo_db = None
users_col = None
settings_col = None
shop_col = None
gacha_col = None
logs_col = None

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
        print("Connected to MongoDB successfully for permanent data storage!", flush=True)
    except Exception as e:
        print(f"MongoDB connection warning: {e}. Falling back to local JSON storage.", flush=True)
        users_col = None
        settings_col = None
        shop_col = None
        gacha_col = None
        logs_col = None

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

async def checkin_user(user_id: int) -> tuple[bool, int]:
    str_id = str(user_id)
    today = get_today_str()

    if users_col is not None:
        try:
            doc = users_col.find_one({"_id": str_id})
            if not doc:
                doc = {"_id": str_id, "points": 0, "last_checkin": "", "chat_count": 0}

            if doc.get("last_checkin") == today:
                return False, doc.get("points", 0)

            new_points = doc.get("points", 0) + 100
            users_col.update_one(
                {"_id": str_id},
                {"$set": {"last_checkin": today, "points": new_points}},
                upsert=True
            )
            return True, new_points
        except Exception as e:
            print(f"MongoDB checkin error: {e}, using local file.", flush=True)

    # Local JSON fallback
    data = await load_data()
    if str_id not in data:
        data[str_id] = {"points": 0, "last_checkin": "", "chat_count": 0}

    user_info = data[str_id]
    if user_info.get("last_checkin") == today:
        return False, user_info.get("points", 0)

    user_info["last_checkin"] = today
    user_info["points"] = user_info.get("points", 0) + 100
    await save_data(data)

    return True, user_info["points"]

async def process_chat_message(user_id: int) -> tuple[int, bool]:
    str_id = str(user_id)

    if users_col is not None:
        try:
            doc = users_col.find_one({"_id": str_id})
            if not doc:
                doc = {"_id": str_id, "points": 0, "last_checkin": "", "chat_count": 0}

            new_count = doc.get("chat_count", 0) + 1
            awarded = False
            new_points = doc.get("points", 0)

            if new_count % 10 == 0:
                new_points += 5
                awarded = True

            users_col.update_one(
                {"_id": str_id},
                {"$set": {"chat_count": new_count, "points": new_points}},
                upsert=True
            )
            return new_points, awarded
        except Exception as e:
            print(f"MongoDB chat process error: {e}, using local file.", flush=True)

    # Local JSON fallback
    data = await load_data()
    if str_id not in data:
        data[str_id] = {"points": 0, "last_checkin": "", "chat_count": 0}

    user_info = data[str_id]
    user_info["chat_count"] = user_info.get("chat_count", 0) + 1

    awarded = False
    if user_info["chat_count"] % 10 == 0:
        user_info["points"] = user_info.get("points", 0) + 5
        awarded = True

    await save_data(data)
    return user_info["points"], awarded

async def get_user_points(user_id: int) -> int:
    str_id = str(user_id)

    if users_col is not None:
        try:
            doc = users_col.find_one({"_id": str_id})
            if doc:
                return doc.get("points", 0)
            return 0
        except Exception as e:
            print(f"MongoDB get points error: {e}, using local file.", flush=True)

    data = await load_data()
    if str_id in data:
        return data[str_id].get("points", 0)
    return 0

async def add_user_points(user_id: int, amount: int) -> int:
    str_id = str(user_id)

    if users_col is not None:
        try:
            doc = users_col.find_one({"_id": str_id})
            current_points = doc.get("points", 0) if doc else 0
            new_points = max(0, current_points + amount)
            users_col.update_one(
                {"_id": str_id},
                {"$set": {"points": new_points}},
                upsert=True
            )
            return new_points
        except Exception as e:
            print(f"MongoDB add_user_points error: {e}, using local file.", flush=True)

    data = await load_data()
    if str_id not in data:
        data[str_id] = {"points": 0, "last_checkin": "", "chat_count": 0}

    current_points = data[str_id].get("points", 0)
    new_points = max(0, current_points + amount)
    data[str_id]["points"] = new_points
    await save_data(data)
    return new_points


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


async def deduct_user_points(user_id: int, amount: int) -> tuple[bool, int]:
    """Deduct points from user if they have enough. Returns (success, new_or_current_balance)."""
    str_id = str(user_id)

    if users_col is not None:
        try:
            doc = users_col.find_one({"_id": str_id})
            current_points = doc.get("points", 0) if doc else 0
            if current_points < amount:
                return False, current_points
            new_points = current_points - amount
            users_col.update_one(
                {"_id": str_id},
                {"$set": {"points": new_points}},
                upsert=True
            )
            return True, new_points
        except Exception as e:
            print(f"MongoDB deduct_user_points error: {e}, using local file.", flush=True)

    data = await load_data()
    if str_id not in data:
        data[str_id] = {"points": 0, "last_checkin": "", "chat_count": 0}

    current_points = data[str_id].get("points", 0)
    if current_points < amount:
        return False, current_points

    new_points = current_points - amount
    data[str_id]["points"] = new_points
    await save_data(data)
    return True, new_points


# ── Shop Items DB (per-guild) ─────────────────────────────────────────

async def get_shop_items(guild_id: int) -> list[dict]:
    str_gid = str(guild_id)

    if shop_col is not None:
        try:
            doc = shop_col.find_one({"_id": str_gid})
            if doc and "items" in doc:
                return doc["items"]
            return []
        except Exception as e:
            print(f"MongoDB get_shop_items error: {e}, using local file.", flush=True)

    _ensure_data_dir()
    async with _shop_lock:
        try:
            if os.path.exists(SHOP_FILE):
                with open(SHOP_FILE, "r", encoding="utf-8") as f:
                    all_shop = json.load(f)
                return all_shop.get(str_gid, [])
        except Exception:
            pass
    return []

async def save_shop_items(guild_id: int, items: list[dict]) -> None:
    str_gid = str(guild_id)

    if shop_col is not None:
        try:
            shop_col.update_one(
                {"_id": str_gid},
                {"$set": {"items": items}},
                upsert=True
            )
            return
        except Exception as e:
            print(f"MongoDB save_shop_items error: {e}, using local file.", flush=True)

    _ensure_data_dir()
    async with _shop_lock:
        all_shop = {}
        try:
            if os.path.exists(SHOP_FILE):
                with open(SHOP_FILE, "r", encoding="utf-8") as f:
                    all_shop = json.load(f)
        except Exception:
            pass

        all_shop[str_gid] = items
        with open(SHOP_FILE, "w", encoding="utf-8") as f:
            json.dump(all_shop, f, ensure_ascii=False, indent=2)


# ── Gacha Settings & Items DB (per-guild) ─────────────────────────────

DEFAULT_GACHA_SETTINGS = {
    "price": 100,
    "items": []  # list of {"name": str, "weight": int, "reward_points": int, "description": str}
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
