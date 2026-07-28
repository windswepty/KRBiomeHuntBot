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

        # 2. Strip custom Discord emojis (<:name:id> or <a:name:id>)
        without_custom_emojis = re.sub(r'<a?:[a-zA-Z0-9_]+:[0-9]+>', '', content)

        # 3. Strip unicode emojis
        without_emojis = re.sub(r'[\U00010000-\U0010ffff\u2600-\u27ff\u2300-\u23ff\u200d\ufe0f]', '', without_custom_emojis).strip()

        # If nothing remains after removing emojis, the message was ONLY emoji(s) -> Exclude.
        # If text remains (even if emojis were present), it is ALLOWED -> Count it!
        if not without_emojis:
            return

        # Process message for chat points
        await process_chat_message(message.author.id)

async def setup(bot):
    await bot.add_cog(ChatReward(bot))
