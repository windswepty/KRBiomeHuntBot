import re
import discord
from discord.ext import commands
from discord import app_commands
from utils.database import (
    add_blacklist_user, remove_blacklist_user, get_blacklist_users, is_user_blacklisted
)


def parse_user_id(raw_input: str) -> int | None:
    """Extract numeric Discord User ID from raw input (supports raw ID or <@id> mentions)."""
    if not raw_input:
        return None
    cleaned = raw_input.strip()
    match = re.search(r'\d{17,20}', cleaned)
    if match:
        try:
            return int(match.group(0))
        except ValueError:
            return None
    return None


# ── Add / Remove Modals ───────────────────────────────────────────────

class AddBlacklistModal(discord.ui.Modal, title="➕ 포인트 블랙리스트 등록"):
    user_input = discord.ui.TextInput(
        label="제한할 유저의 ID 또는 @멘션을 입력해 주세요!",
        placeholder="예: 1234567890123456789 또는 @유저멘션",
        required=True,
        max_length=50
    )
    reason_input = discord.ui.TextInput(
        label="블랙리스트 등록 사유를 입력해 주세요!",
        placeholder="예: 매크로 채팅 파밍 행위 적발",
        style=discord.TextStyle.paragraph,
        default="매크로 채팅 파밍 방지 차단",
        required=False,
        max_length=200
    )

    async def on_submit(self, interaction: discord.Interaction):
        user_id = parse_user_id(self.user_input.value)
        if not user_id:
            await interaction.response.send_message(
                "⚠️ 유저 ID를 찾을 수 없습니다. 올바른 유저 ID(17~20자리 숫자) 또는 @멘션을 입력해 주세요.",
                ephemeral=True
            )
            return

        reason_str = self.reason_input.value.strip() if self.reason_input.value else "사유 미입력 (파밍 방지 차단)"
        guild_id = interaction.guild.id

        await add_blacklist_user(guild_id, user_id, reason_str)

        target_member = interaction.guild.get_member(user_id)
        if not target_member:
            try:
                target_member = await interaction.guild.fetch_member(user_id)
            except Exception:
                target_member = None

        target_str = f"**{target_member.display_name}** ({target_member.mention})" if target_member else f"<@{user_id}>"

        embed = discord.Embed(
            title="⛔ 포인트 블랙리스트 등록 완료",
            description=f"{target_str} 님이 포인트 블랙리스트에 등록되었습니다.\n\n"
                        f"📝 **제한 사유**: `{reason_str}`\n\n"
                        "⚠️ 대상 유저는 출석체크, 채팅 작성 등 모든 포인트 자동 적립이 완전히 차단됩니다.",
            color=discord.Color.red()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


class RemoveBlacklistModal(discord.ui.Modal, title="➖ 포인트 블랙리스트 해제"):
    user_input = discord.ui.TextInput(
        label="제한 해제할 유저의 ID 또는 @멘션을 입력해 주세요!",
        placeholder="예: 1234567890123456789 또는 @유저멘션",
        required=True,
        max_length=50
    )

    async def on_submit(self, interaction: discord.Interaction):
        user_id = parse_user_id(self.user_input.value)
        if not user_id:
            await interaction.response.send_message(
                "⚠️ 유저 ID를 찾을 수 없습니다. 올바른 유저 ID(17~20자리 숫자) 또는 @멘션을 입력해 주세요.",
                ephemeral=True
            )
            return

        guild_id = interaction.guild.id
        success = await remove_blacklist_user(guild_id, user_id)

        target_member = interaction.guild.get_member(user_id)
        if not target_member:
            try:
                target_member = await interaction.guild.fetch_member(user_id)
            except Exception:
                target_member = None

        target_str = f"**{target_member.display_name}** ({target_member.mention})" if target_member else f"<@{user_id}>"

        if success:
            embed = discord.Embed(
                title="✅ 포인트 블랙리스트 해제 완료",
                description=f"{target_str} 님의 블랙리스트 제한이 성공적으로 해제되었습니다.\n\n"
                            "이제 정상적으로 출석체크 및 채팅 포인트를 획득할 수 있습니다.",
                color=discord.Color.green()
            )
        else:
            embed = discord.Embed(
                title="⚠️ 해제 실패",
                description=f"{target_str} 님은 현재 블랙리스트 목록에 존재하지 않습니다.",
                color=discord.Color.gold()
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)


# ── Blacklist Main View ───────────────────────────────────────────────

class BlacklistMainView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)

    @discord.ui.button(label="블랙리스트 추가", style=discord.ButtonStyle.danger, emoji="➕")
    async def add_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AddBlacklistModal())

    @discord.ui.button(label="블랙리스트 해제", style=discord.ButtonStyle.success, emoji="➖")
    async def remove_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(RemoveBlacklistModal())

    @discord.ui.button(label="목록 새로고침", style=discord.ButtonStyle.secondary, emoji="🔄")
    async def refresh_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild_id = interaction.guild.id
        bl_users = await get_blacklist_users(guild_id)

        embed = discord.Embed(
            title="⛔ 포인트 블랙리스트 관리 센터",
            description="포인트를 파밍하듯이 무분별하게 적립하는 유저의 **출석체크 및 채팅 적립을 차단**합니다.\n\n"
                        "아래 버튼을 눌러 유저를 등록하거나 해제하실 수 있습니다.",
            color=discord.Color.dark_red()
        )

        if not bl_users:
            embed.add_field(
                name="📋 등록된 제한 유저 목록 (총 0명)",
                value="현재 블랙리스트에 등록된 유저가 없습니다.",
                inline=False
            )
        else:
            bl_lines = []
            for b in bl_users[:15]:
                uid = b["user_id"]
                r = b.get("reason", "사유 미입력")
                ts = b.get("timestamp", "")
                member = interaction.guild.get_member(uid)
                if not member:
                    try:
                        member = await interaction.guild.fetch_member(uid)
                    except Exception:
                        member = None

                u_str = f"**{member.display_name}** ({member.mention})" if member else f"<@{uid}> *(서버 퇴장)*"
                bl_lines.append(f"• ⛔ {u_str}\n  └ 등록일: `{ts}` | 사유: `{r}`")

            embed.add_field(
                name=f"📋 등록된 제한 유저 목록 (총 {len(bl_users)}명)",
                value="\n".join(bl_lines),
                inline=False
            )

        embed.set_footer(text="한바헌 포인트 파밍 방지 차단 시스템")
        await interaction.response.edit_message(embed=embed, view=self)


# ── Cog ───────────────────────────────────────────────────────────────

class Blacklist(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="블랙리스트", description="[관리자 전용] 포인트 블랙리스트 관리 센터를 엽니다 (추가/해제 버튼 제공).")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def view_blacklist(self, interaction: discord.Interaction):
        guild_id = interaction.guild.id
        bl_users = await get_blacklist_users(guild_id)

        embed = discord.Embed(
            title="⛔ 포인트 블랙리스트 관리 센터",
            description="포인트를 파밍하듯이 무분별하게 적립하는 유저의 **출석체크 및 채팅 적립을 차단**합니다.\n\n"
                        "아래 버튼을 눌러 유저를 등록하거나 해제하실 수 있습니다.",
            color=discord.Color.dark_red()
        )

        if not bl_users:
            embed.add_field(
                name="📋 등록된 제한 유저 목록 (총 0명)",
                value="현재 블랙리스트에 등록된 유저가 없습니다.",
                inline=False
            )
        else:
            bl_lines = []
            for b in bl_users[:15]:
                uid = b["user_id"]
                r = b.get("reason", "사유 미입력")
                ts = b.get("timestamp", "")
                member = interaction.guild.get_member(uid)
                if not member:
                    try:
                        member = await interaction.guild.fetch_member(uid)
                    except Exception:
                        member = None

                u_str = f"**{member.display_name}** ({member.mention})" if member else f"<@{uid}> *(서버 퇴장)*"
                bl_lines.append(f"• ⛔ {u_str}\n  └ 등록일: `{ts}` | 사유: `{r}`")

            embed.add_field(
                name=f"📋 등록된 제한 유저 목록 (총 {len(bl_users)}명)",
                value="\n".join(bl_lines),
                inline=False
            )

        embed.set_footer(text="한바헌 포인트 파밍 방지 차단 시스템")
        view = BlacklistMainView()
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


async def setup(bot):
    await bot.add_cog(Blacklist(bot))
