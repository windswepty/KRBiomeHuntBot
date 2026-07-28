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
