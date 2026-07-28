import discord
from discord.ext import commands
from discord import app_commands

class General(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="핑", description="봇의 응답 속도를 확인합니다.")
    async def ping(self, interaction: discord.Interaction):
        latency = round(self.bot.latency * 1000)
        await interaction.response.send_message(f"🏓 퐁! 현재 지연 시간: **{latency}ms**", ephemeral=True)

async def setup(bot):
    await bot.add_cog(General(bot))
