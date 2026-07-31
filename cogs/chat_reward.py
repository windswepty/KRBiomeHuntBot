import re
import discord
from discord.ext import commands
from utils.database import process_chat_message, is_user_blacklisted

class ChatReward(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        # Ignore bot messages and direct messages
        if message.author.bot or not message.guild:
            return

        # Check if user is blacklisted from receiving points
        if await is_user_blacklisted(message.guild.id, message.author.id):
            return

        # 1. Exclude photos/files/GIF attachments and stickers
        if message.attachments or message.stickers:
            return

        content = message.content.strip()
        if not content:
            return

        # 2. Exclude GIF links (Tenor, Giphy, Gfycat, or .gif/.gifv URLs)
        if re.search(r'https?://\S*(?:tenor\.com|giphy\.com|gfycat\.com|\.gif|\.gifv)', content, re.IGNORECASE):
            return

        # 3. Strip all general URLs (http://, https://)
        without_urls = re.sub(r'https?://\S+', '', content)

        # 4. Strip custom Discord emojis (<:name:id> or <a:name:id>)
        without_custom_emojis = re.sub(r'<a?:[a-zA-Z0-9_]+:[0-9]+>', '', without_urls)

        # 5. Strip unicode emojis
        without_emojis = re.sub(r'[\U00010000-\U0010ffff\u2600-\u27ff\u2300-\u23ff\200d\ufe0f]', '', without_custom_emojis).strip()

        # If nothing remains after removing URLs and emojis, ignore.
        if not without_emojis:
            return

        # Process message for chat points
        await process_chat_message(message.author.id)

async def setup(bot):
    await bot.add_cog(ChatReward(bot))
