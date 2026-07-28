import os
import json
import asyncio

DATA_DIR = "data"
ECONOMY_FILE = os.path.join(DATA_DIR, "economy.json")

_lock = asyncio.Lock()

def _ensure_data_dir():
    os.makedirs(DATA_DIR, exist_ok=True)
    if not os.path.exists(ECONOMY_FILE):
        with open(ECONOMY_FILE, "w", encoding="utf-8") as f:
            json.dump({}, f, ensure_ascii=False, indent=2)

async def load_economy():
    _ensure_data_dir()
    async with _lock:
        try:
            with open(ECONOMY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

async def save_economy(data):
    _ensure_data_dir()
    async with _lock:
        with open(ECONOMY_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

async def get_user_data(user_id: int) -> dict:
    data = await load_economy()
    str_id = str(user_id)
    if str_id not in data:
        data[str_id] = {
            "points": 0,
            "total_reports": 0,
            "earned_points": 0
        }
        await save_economy(data)
    return data[str_id]

async def add_points(user_id: int, points: int, is_report: bool = False) -> int:
    data = await load_economy()
    str_id = str(user_id)
    if str_id not in data:
        data[str_id] = {
            "points": 0,
            "total_reports": 0,
            "earned_points": 0
        }
    
    data[str_id]["points"] += points
    if points > 0:
        data[str_id]["earned_points"] = data[str_id].get("earned_points", 0) + points
    if is_report:
        data[str_id]["total_reports"] = data[str_id].get("total_reports", 0) + 1

    await save_economy(data)
    return data[str_id]["points"]

async def remove_points(user_id: int, points: int) -> bool:
    data = await load_economy()
    str_id = str(user_id)
    if str_id not in data or data[str_id]["points"] < points:
        return False
    
    data[str_id]["points"] -= points
    await save_economy(data)
    return True
