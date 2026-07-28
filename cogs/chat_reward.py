import time
import discord
from discord.ext import commands
from utils.database import process_chat_message

class ChatReward(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.last_message_time = {}

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        # Ignore bot messages and direct messages
        if message.author.bot or not message.guild:
            return

        user_id = message.author.id
        now = time.time()

        # Light 3-second cooldown per user to prevent instant spamming
        if user_id in self.last_message_time:
            if now - self.last_message_time[user_id] < 3:
                return

        self.last_message_time[user_id] = now
        await process_chat_message(user_id)

async def setup(bot):
    await bot.add_cog(ChatReward(bot))
