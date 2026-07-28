import random
import discord
from discord.ext import commands
from discord import app_commands
from utils.database import get_user_data, add_points, remove_points, load_economy

class Economy(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="지갑", description="내 보유 포인트와 제보 기록을 확인합니다.")
    async def wallet(self, interaction: discord.Interaction, user: discord.User = None):
        target = user or interaction.user
        user_info = await get_user_data(target.id)

        embed = discord.Embed(
            title=f"💳 {target.name} 님의 지갑 정보",
            color=discord.Color.gold()
        )
        embed.set_thumbnail(url=target.display_avatar.url)
        embed.add_field(name="💰 보유 포인트", value=f"**{user_info['points']:,}** P", inline=True)
        embed.add_field(name="📥 총 제보 횟수", value=f"**{user_info.get('total_reports', 0):,}** 회", inline=True)
        embed.add_field(name="✨ 누적 획득 포인트", value=f"**{user_info.get('earned_points', 0):,}** P", inline=True)
        embed.set_footer(text="한바헌 희귀 바이옴 제보로 포인트를 모아보세요!")

        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="포인트랭킹", description="서버 내 포인트 및 제보왕 TOP 10 랭킹을 확인합니다.")
    async def leaderboard(self, interaction: discord.Interaction):
        data = await load_economy()
        if not data:
            await interaction.response.send_message("❌ 아직 등록된 유저 데이터가 없습니다.", ephemeral=True)
            return

        sorted_points = sorted(data.items(), key=lambda x: x[1].get("points", 0), reverse=True)[:10]
        sorted_reports = sorted(data.items(), key=lambda x: x[1].get("total_reports", 0), reverse=True)[:10]

        embed = discord.Embed(
            title="🏆 한바헌 포인트 & 제보왕 랭킹",
            color=discord.Color.purple()
        )

        # Points Leaderboard
        point_text = ""
        for idx, (uid, info) in enumerate(sorted_points, 1):
            user = self.bot.get_user(int(uid))
            uname = user.name if user else f"유저({uid})"
            medal = "🥇" if idx == 1 else "🥈" if idx == 2 else "🥉" if idx == 3 else f"**{idx}.**"
            point_text += f"{medal} {uname} - **{info.get('points', 0):,}** P\n"

        # Reports Leaderboard
        report_text = ""
        for idx, (uid, info) in enumerate(sorted_reports, 1):
            if info.get("total_reports", 0) <= 0:
                continue
            user = self.bot.get_user(int(uid))
            uname = user.name if user else f"유저({uid})"
            medal = "🥇" if idx == 1 else "🥈" if idx == 2 else "🥉" if idx == 3 else f"**{idx}.**"
            report_text += f"{medal} {uname} - **{info.get('total_reports', 0):,}** 회\n"

        embed.add_field(name="💰 포인트 보유 TOP 10", value=point_text or "데이터 없음", inline=False)
        embed.add_field(name="🎯 바이옴 제보왕 TOP 10", value=report_text or "데이터 없음", inline=False)

        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="도박", description="포인트를 배팅하여 주사위 도박 게임을 진행합니다.")
    @app_commands.describe(amount="배팅할 포인트 금액 (최소 100P)")
    async def gamble(self, interaction: discord.Interaction, amount: int):
        if amount < 100:
            await interaction.response.send_message("❌ 최소 배팅 금액은 100P 입니다.", ephemeral=True)
            return

        user_info = await get_user_data(interaction.user.id)
        if user_info["points"] < amount:
            await interaction.response.send_message(f"❌ 포인트를 부족합니다. (현재: **{user_info['points']:,}** P)", ephemeral=True)
            return

        # Deduct bet amount first
        await remove_points(interaction.user.id, amount)

        user_roll = random.randint(1, 6) + random.randint(1, 6)
        bot_roll = random.randint(1, 6) + random.randint(1, 6)

        if user_roll > bot_roll:
            winnings = amount * 2
            await add_points(interaction.user.id, winnings)
            new_info = await get_user_data(interaction.user.id)
            embed = discord.Embed(
                title="🎲 잭팟! 주사위 승리!",
                description=f"내 주사위: 🎲 **{user_roll}** vs 봇 주사위: 🎲 **{bot_roll}**\n\n🎉 **{winnings:,} P** 획득! (현재 잔액: **{new_info['points']:,}** P)",
                color=discord.Color.green()
            )
        elif user_roll == bot_roll:
            await add_points(interaction.user.id, amount)
            new_info = await get_user_data(interaction.user.id)
            embed = discord.Embed(
                title="🎲 무승부!",
                description=f"내 주사위: 🎲 **{user_roll}** vs 봇 주사위: 🎲 **{bot_roll}**\n\n🤝 배팅금 **{amount:,} P**를 환불받았습니다.",
                color=discord.Color.gold()
            )
        else:
            new_info = await get_user_data(interaction.user.id)
            embed = discord.Embed(
                title="🎲 꽝! 주사위 패배",
                description=f"내 주사위: 🎲 **{user_roll}** vs 봇 주사위: 🎲 **{bot_roll}**\n\n💸 **{amount:,} P**를 잃었습니다... (현재 잔액: **{new_info['points']:,}** P)",
                color=discord.Color.red()
            )

        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="송금", description="다른 유저에게 포인트를 송금합니다.")
    @app_commands.describe(user="포인트를 받을 유저", amount="송금할 포인트 금액")
    async def pay(self, interaction: discord.Interaction, user: discord.User, amount: int):
        if user.id == interaction.user.id:
            await interaction.response.send_message("❌ 자기 자신에게는 송금할 수 없습니다.", ephemeral=True)
            return
        if user.bot:
            await interaction.response.send_message("❌ 봇에게는 송금할 수 없습니다.", ephemeral=True)
            return
        if amount <= 0:
            await interaction.response.send_message("❌ 1P 이상만 송금 가능합니다.", ephemeral=True)
            return

        success = await remove_points(interaction.user.id, amount)
        if not success:
            await interaction.response.send_message("❌ 잔여 포인트가 부족합니다.", ephemeral=True)
            return

        await add_points(user.id, amount)
        sender_info = await get_user_data(interaction.user.id)

        embed = discord.Embed(
            title="💸 포인트 송금 완료",
            description=f"**{interaction.user.name}** 님이 **{user.name}** 님에게 **{amount:,} P**를 송금하셨습니다!\n(내 잔액: **{sender_info['points']:,}** P)",
            color=discord.Color.blue()
        )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="포인트지급", description="[관리자 전용] 특정 유저에게 포인트를 지급하거나 차감합니다.")
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(user="대상 유저", amount="지급할 포인트 (음수 작성 시 차감)", reason="지급/차감 사유")
    async def grant_points(self, interaction: discord.Interaction, user: discord.User, amount: int, reason: str = "관리자 지급"):
        new_total = await add_points(user.id, amount)
        action_text = "지급" if amount >= 0 else "차감"

        embed = discord.Embed(
            title=f"⚙️ 관리자 포인트 {action_text}",
            description=f"**{user.name}** 님에게 **{amount:+,} P**를 {action_text}했습니다.\n사유: `{reason}`\n\n유저 현재 잔액: **{new_total:,} P**",
            color=discord.Color.gold()
        )
        await interaction.response.send_message(embed=embed)

    @grant_points.error
    async def grant_points_error(self, interaction: discord.Interaction, error):
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message("❌ 이 명령어는 관리자 전용입니다.", ephemeral=True)

async def setup(bot):
    await bot.add_cog(Economy(bot))
