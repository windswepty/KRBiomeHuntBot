import discord
from discord.ext import commands
from discord import app_commands
from utils.database import checkin_user

class CheckIn(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="출석", description="하루에 한 번 출석체크를 하고 100포인트를 획득합니다.")
    async def check_in(self, interaction: discord.Interaction):
        success, total_points = await checkin_user(interaction.user.id)

        if success:
            embed = discord.Embed(
                title="✅ 출석체크 완료!",
                description=f"🎉 오늘 출석체크로 **+100 P**를 획득하셨습니다!\n\n💰 **현재 총 보유 포인트**: **{total_points:,}** P",
                color=discord.Color.green()
            )
        else:
            embed = discord.Embed(
                title="⚠️ 출석체크 이미 완료됨",
                description=f"오늘 출석체크는 이미 완료하셨습니다. 내일 다시 시도해 주세요!\n\n💰 **현재 총 보유 포인트**: **{total_points:,}** P",
                color=discord.Color.gold()
            )

        # ephemeral=True makes the response visible ONLY to the user!
        await interaction.response.send_message(embed=embed, ephemeral=True)

async def setup(bot):
    await bot.add_cog(CheckIn(bot))
