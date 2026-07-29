import discord
from discord.ext import commands
from discord import app_commands

class Shop(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="포인트상점", description="포인트로 상품이나 혜택을 구매할 수 있는 상점입니다.")
    async def point_shop(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="🛒 한바헌 포인트 상점",
            description="아직 구매할 수 있는 항목이 없습니다!",
            color=discord.Color.purple()
        )
        embed.set_footer(text="한바헌 포인트 시스템")
        await interaction.response.send_message(embed=embed, ephemeral=True)

async def setup(bot):
    await bot.add_cog(Shop(bot))
