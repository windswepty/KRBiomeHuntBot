import os
import json
import asyncio
from datetime import datetime, timezone, timedelta

DATA_DIR = "data"
ECONOMY_FILE = os.path.join(DATA_DIR, "economy.json")

_lock = asyncio.Lock()
KST = timezone(timedelta(hours=9))

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
    """
    Returns (success, current_points)
    If success is True: user gained 100 points today.
    If success is False: user already checked in today.
    """
    data = await load_data()
    str_id = str(user_id)
    today = get_today_str()

    if str_id not in data:
        data[str_id] = {
            "points": 0,
            "last_checkin": ""
        }

    user_info = data[str_id]

    if user_info.get("last_checkin") == today:
        return False, user_info.get("points", 0)

    user_info["last_checkin"] = today
    user_info["points"] = user_info.get("points", 0) + 100
    await save_data(data)

    return True, user_info["points"]
