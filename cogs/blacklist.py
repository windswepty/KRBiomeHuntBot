import discord
from discord.ext import commands
from discord import app_commands
from utils.database import (
    add_blacklist_user, remove_blacklist_user, get_blacklist_users
)


class Blacklist(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="블랙리스트", description="[관리자 전용] 특정 유저의 포인트 획득(출석체크, 채팅 적립 등)을 제한하거나 해제합니다.")
    @app_commands.describe(
        action="수행할 작업을 선택하세요 (등록, 해제, 목록)",
        user="대상 유저를 선택하세요 (등록/해제 시 필수)",
        reason="블랙리스트 등록 사유를 입력하세요 (선택 사항)"
    )
    @app_commands.choices(action=[
        app_commands.Choice(name="➕ 등록 (포인트 획득 차단)", value="add"),
        app_commands.Choice(name="➖ 해제 (포인트 획득 허용)", value="remove"),
        app_commands.Choice(name="📜 목록 (등록된 유저 조회)", value="list")
    ])
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def manage_blacklist(
        self,
        interaction: discord.Interaction,
        action: app_commands.Choice[str],
        user: discord.Member | None = None,
        reason: str | None = None
    ):
        guild_id = interaction.guild.id
        act = action.value

        if act == "add":
            if not user:
                await interaction.response.send_message("⚠️ 블랙리스트에 등록할 **대상 유저**를 지정해 주세요.", ephemeral=True)
                return

            if user.bot:
                await interaction.response.send_message("⚠️ 봇 계정은 블랙리스트에 등록할 필요가 없습니다.", ephemeral=True)
                return

            reason_str = reason.strip() if reason else "사유 미입력 (파밍 방지 차단)"
            await add_blacklist_user(guild_id, user.id, reason_str)

            embed = discord.Embed(
                title="⛔ 포인트 블랙리스트 등록 완료",
                description=f"**{user.mention} ({user.display_name})** 님이 포인트 블랙리스트에 등록되었습니다.\n\n"
                            f"📝 **제한 사유**: `{reason_str}`\n"
                            "⚠️ 대상 유저는 출석체크, 채팅 작성 등을 통한 포인트 자동 적립이 완전히 차단됩니다.",
                color=discord.Color.red()
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)

        elif act == "remove":
            if not user:
                await interaction.response.send_message("⚠️ 블랙리스트에서 해제할 **대상 유저**를 지정해 주세요.", ephemeral=True)
                return

            success = await remove_blacklist_user(guild_id, user.id)
            if success:
                embed = discord.Embed(
                    title="✅ 포인트 블랙리스트 해제 완료",
                    description=f"**{user.mention} ({user.display_name})** 님의 블랙리스트 제한이 해제되었습니다.\n\n"
                                "이제 정상적으로 출석체크 및 채팅 포인트를 획득할 수 있습니다.",
                    color=discord.Color.green()
                )
            else:
                embed = discord.Embed(
                    title="⚠️ 해제 실패",
                    description=f"**{user.display_name}** 님은 현재 블랙리스트 목록에 존재하지 않습니다.",
                    color=discord.Color.gold()
                )
            await interaction.response.send_message(embed=embed, ephemeral=True)

        elif act == "list":
            bl_users = await get_blacklist_users(guild_id)
            if not bl_users:
                embed = discord.Embed(
                    title="📜 포인트 블랙리스트 목록",
                    description="현재 블랙리스트에 등록된 유저가 없습니다.",
                    color=discord.Color.blue()
                )
                await interaction.response.send_message(embed=embed, ephemeral=True)
                return

            embed = discord.Embed(
                title="📜 포인트 블랙리스트 등록 목록",
                description=f"총 **{len(bl_users)}명**의 유저가 포인트 획득 제한 상태입니다.\n",
                color=discord.Color.dark_red()
            )

            bl_lines = []
            for b in bl_users:
                uid = b["user_id"]
                r = b.get("reason", "사유 미입력")
                ts = b.get("timestamp", "")
                member = interaction.guild.get_member(uid)
                u_str = f"**{member.display_name}** ({member.mention})" if member else f"<@{uid}> *(퇴장 유저)*"
                bl_lines.append(f"• ⛔ {u_str}\n  └ 등록일: `{ts}` | 사유: `{r}`")

            embed.add_field(name="📋 제한 대상자 리스트", value="\n".join(bl_lines), inline=False)
            embed.set_footer(text="한바헌 포인트 파밍 방지 차단 시스템")
            await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot):
    await bot.add_cog(Blacklist(bot))
