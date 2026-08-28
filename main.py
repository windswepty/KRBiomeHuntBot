import os
import asyncio
import discord
from discord.ext import commands
from dotenv import load_dotenv
from keep_alive import keep_alive

# Load environment variables
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

# Setup Bot Intents
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    print(f"==========================================", flush=True)
    print(f"Logged in as: {bot.user.name} (ID: {bot.user.id})", flush=True)
    print(f"Connected to {len(bot.guilds)} guild(s)", flush=True)

    # 포인트가 어디에 저장되는지 매 기동마다 눈에 보이게 남깁니다.
    # 임시 저장소로 떨어진 채 운영되면 유저 포인트가 사라지기 때문입니다.
    try:
        from utils.database import storage_status, STORAGE_MODE
        print(f"Point storage: {storage_status()}", flush=True)
        if STORAGE_MODE != "mongo":
            print("*** 포인트가 영구 저장되지 않는 상태입니다. 즉시 확인하세요. ***", flush=True)
    except Exception as e:
        print(f"Storage status check failed: {e}", flush=True)
    print(f"==========================================", flush=True)
    
    # Sync Slash Commands
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} slash command(s).", flush=True)
    except Exception as e:
        print(f"Failed to sync slash commands: {e}", flush=True)

async def load_cogs():
    os.makedirs("cogs", exist_ok=True)
    for filename in os.listdir("./cogs"):
        if filename.endswith(".py") and not filename.startswith("__"):
            try:
                await bot.load_extension(f"cogs.{filename[:-3]}")
                print(f"Loaded extension: cogs.{filename[:-3]}", flush=True)
            except Exception as e:
                print(f"Failed to load extension cogs.{filename[:-3]}: {e}", flush=True)

async def main():
    async with bot:
        await load_cogs()
        keep_alive()
        if not TOKEN:
            print("ERROR: DISCORD_TOKEN is not set in .env file!")
            return
        await bot.start(TOKEN)

if __name__ == "__main__":
    asyncio.run(main())
