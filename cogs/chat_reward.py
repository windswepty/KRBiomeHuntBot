import re
import discord
from discord.ext import commands
from utils.database import process_chat_message

class ChatReward(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        # Ignore bot messages and direct messages
        if message.author.bot or not message.guild:
            return

        # 1. Exclude photos/files and stickers
        if message.attachments or message.stickers:
            return

        content = message.content.strip()
        if not content:
            return

        # 2. Clean custom Discord emojis (<:name:id> or <a:name:id>)
        cleaned = re.sub(r'<a?:[a-zA-Z0-9_]+:[0-9]+>', '', content)

        # 3. Must contain valid text characters (Korean, English, Numbers)
        # Excludes pure emojis or empty symbol messages
        if not re.search(r'[a-zA-Z0-9가-힣ㄱ-ㅎㅏ-ㅣ]', cleaned):
            return

        # Cooldown removed per request -> process every valid text message immediately
        await process_chat_message(message.author.id)

async def setup(bot):
    await bot.add_cog(ChatReward(bot))
