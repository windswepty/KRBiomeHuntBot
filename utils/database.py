import os
import json
import asyncio
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

load_dotenv()

DATA_DIR = "data"
ECONOMY_FILE = os.path.join(DATA_DIR, "economy.json")
MONGO_URI = os.getenv("MONGO_URI")

_lock = asyncio.Lock()
KST = timezone(timedelta(hours=9))

# MongoDB setup
mongo_db = None
users_col = None

if MONGO_URI:
    try:
        from pymongo import MongoClient
        client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
        # Test connection
        client.admin.command('ping')
        mongo_db = client["krbiomehuntbot"]
        users_col = mongo_db["users"]
        print("Connected to MongoDB successfully for permanent data storage!", flush=True)
    except Exception as e:
        print(f"MongoDB connection warning: {e}. Falling back to local JSON storage.", flush=True)
        users_col = None

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
