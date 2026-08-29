"""로블록스 계정 연동 (독립 모듈).

개인서버 링크를 받아간 사람의 **로블록스 아이디**를 기록해 두면, 게임 안
플레이어 목록과 대조해 '명단에 없는데 들어와 있는 사람'을 찾아낼 수 있습니다.
서버 밖으로 유출된 링크를 탐지하는 현실적인 경로가 이것뿐입니다.

닉네임을 그냥 입력받으면 남의 이름을 적어 사칭할 수 있으므로,
로블록스 공식 OAuth 로 신원을 보증받습니다.

포인트/상점 시스템과는 연동되지 않습니다.
"""

import asyncio

import discord
from discord.ext import commands
from discord import app_commands

from utils import roblox_oauth
from utils.verify_db import (
    get_roblox_link, find_roblox_owner, save_roblox_link,
    remove_roblox_link, list_roblox_links, search_roblox_links,
    get_verify_settings, get_linkguard_settings,
)


class RobloxLink(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    # ── 멤버용 ────────────────────────────────────────────────────────

    @app_commands.command(
        name="로블록스연동",
        description="내 로블록스 계정을 연동합니다 (개인서버 이용 기록에 함께 남습니다)."
    )
    async def roblox_link(self, interaction: discord.Interaction):
        guild = interaction.guild
        if guild is None:
            return

        if not roblox_oauth.enabled():
            embed = discord.Embed(
                title="⚠️ 로블록스 연동이 아직 준비되지 않았습니다",
                description="관리자가 설정을 마치면 이용하실 수 있습니다.",
                color=discord.Color.orange()
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        existing = await get_roblox_link(guild.id, interaction.user.id)
        url, _ = roblox_oauth.start(guild.id, interaction.user.id)

        embed = discord.Embed(
            title="🎮 로블록스 계정 연동",
            description="아래 버튼을 눌러 로블록스에 로그인하면 연동이 완료됩니다.",
            color=discord.Color.blurple()
        )
        if existing:
            embed.add_field(
                name="🔗 현재 연동된 계정",
                value=f"**{existing.get('roblox_name', '?')}** "
                      f"(`{existing.get('roblox_id', '?')}`)\n"
                      "-# 다시 연동하면 이 계정으로 교체됩니다.",
                inline=False)
        embed.add_field(
            name="ℹ️ 왜 필요한가요",
            value="개인서버 링크를 누가 받아갔는지 **로블록스 아이디로도** 남기기 "
                  "위해서입니다. 링크가 외부로 새어나갔을 때 확인하는 데 쓰입니다.",
            inline=False)
        embed.add_field(
            name="🔒 요청하는 권한",
            value="로블록스 **아이디와 유저명만** 확인합니다.\n"
                  "비밀번호 · 로벅스 · 인벤토리에는 접근하지 않습니다.",
            inline=False)
        embed.set_footer(text="이 링크는 10분 뒤 만료됩니다")

        view = discord.ui.View(timeout=None)
        view.add_item(discord.ui.Button(
            label="로블록스로 로그인", url=url,
            style=discord.ButtonStyle.link, emoji="🎮"))
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @app_commands.command(
        name="내연동정보",
        description="내 로블록스 연동 상태를 확인합니다."
    )
    async def my_link(self, interaction: discord.Interaction):
        link = await get_roblox_link(interaction.guild.id, interaction.user.id)
        if not link:
            embed = discord.Embed(
                title="🔗 연동 정보 없음",
                description="아직 로블록스 계정을 연동하지 않으셨습니다.\n"
                            "`/로블록스연동` 으로 등록할 수 있습니다.",
                color=discord.Color.orange())
        else:
            embed = discord.Embed(
                title="🔗 내 로블록스 연동 정보",
                color=discord.Color.green())
            embed.add_field(
                name="🎮 로블록스",
                value=f"**{link.get('roblox_name', '?')}**\n"
                      f"`{link.get('roblox_id', '?')}`",
                inline=True)
            embed.add_field(
                name="🕒 연동 시각",
                value=f"`{link.get('linked_at', '?')}`", inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── 관리자용 ──────────────────────────────────────────────────────

    @app_commands.command(
        name="연동명단",
        description="[관리자 전용] 로블록스 연동 명단을 봅니다 (게임 내 대조용)."
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def roster(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        rows = await list_roblox_links(interaction.guild.id)

        embed = discord.Embed(
            title="🎮 로블록스 연동 명단",
            description=(f"총 **{len(rows)}명**이 연동했습니다."
                         if rows else "아직 연동한 멤버가 없습니다."),
            color=discord.Color.blurple())

        if rows:
            # 게임 내 플레이어 목록과 눈으로 대조하기 좋게 이름만 한 줄로 모읍니다.
            names = ", ".join(r.get("roblox_name", "?") for r in rows[:100])
            embed.add_field(
                name="📋 로블록스 아이디 (게임 내 목록과 대조용)",
                value="```" + names[:990] + "```", inline=False)

            lines = []
            used = 0
            for r in rows:
                line = (f"<@{r.get('user_id')}> → **{r.get('roblox_name', '?')}** "
                        f"(`{r.get('roblox_id', '?')}`)")
                if used + len(line) + 1 > 1000:
                    break
                lines.append(line)
                used += len(line) + 1
            embed.add_field(name=f"🔗 매핑 ({len(lines)}명)",
                            value="\n".join(lines), inline=False)
            if len(rows) > len(lines):
                embed.set_footer(text=f"…외 {len(rows) - len(lines)}명 더 있습니다")

        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(
        name="연동조회",
        description="[관리자 전용] 연동 명단을 검색합니다 (디스코드 / 로블록스 양방향)."
    )
    @app_commands.describe(
        검색어="디스코드 ID·멘션, 로블록스 유저명, 또는 로블록스 숫자 ID"
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def search(self, interaction: discord.Interaction, 검색어: str):
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = interaction.guild
        query = (검색어 or "").strip()

        rows = await search_roblox_links(guild.id, query, limit=10)

        if not rows:
            embed = discord.Embed(
                title="🔍 검색 결과 없음",
                description=f"`{query[:80]}` 로 연동된 기록을 찾지 못했습니다.",
                color=discord.Color.orange()
            )
            # 게임에서 본 이름이 안 나온다는 것 자체가 중요한 신호입니다.
            embed.add_field(
                name="⚠️ 게임 안에서 본 이름인가요?",
                value="연동 명단에 없는 사람이 개인서버에 들어와 있다면,\n"
                      "**링크가 외부로 새어나갔다는 뜻**일 수 있습니다.\n"
                      "-# 다만 연동을 아직 안 한 멤버일 수도 있으니 함께 확인하세요.",
                inline=False
            )
            embed.add_field(
                name="💡 검색 가능한 값",
                value="• 디스코드 ID 또는 멘션\n"
                      "• 로블록스 유저명 (일부만 입력해도 됩니다)\n"
                      "• 로블록스 숫자 ID",
                inline=False
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        badge = {
            "discord_id": "🙋 디스코드 ID 일치",
            "roblox_id": "🔢 로블록스 ID 일치",
            "roblox_name": "🎮 로블록스 유저명 일치",
        }

        embed = discord.Embed(
            title="🔍 연동 검색 결과",
            description=f"`{query[:80]}` → **{len(rows)}건**",
            color=discord.Color.green()
        )

        for idx, r in enumerate(rows, 1):
            uid = r.get("user_id", "?")
            member = guild.get_member(int(uid)) if str(uid).isdigit() else None
            # 서버를 이미 나간 사람일 수 있습니다. 그것 자체가 확인할 신호입니다.
            who = member.mention if member else f"`{uid}` (⚠️ 서버에 없음)"
            embed.add_field(
                name=f"{idx}. {r.get('roblox_name', '?')}",
                value=f"{who}\n"
                      f"🎮 `{r.get('roblox_id', '?')}`\n"
                      f"🕒 `{r.get('linked_at', '?')}`\n"
                      f"-# {badge.get(r.get('matched_by'), '일치')}",
                inline=False
            )

        if len(rows) >= 10:
            embed.set_footer(text="결과가 10건을 넘어 잘렸습니다 · 더 정확한 검색어를 입력해 보세요")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(
        name="연동해제",
        description="[관리자 전용] 특정 멤버의 로블록스 연동을 해제합니다."
    )
    @app_commands.describe(member="연동을 해제할 멤버")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def unlink(self, interaction: discord.Interaction, member: discord.Member):
        removed = await remove_roblox_link(interaction.guild.id, member.id)
        embed = discord.Embed(
            title="🔗 연동 해제" if removed else "ℹ️ 연동 정보 없음",
            description=(f"{member.mention} 님의 로블록스 연동을 해제했습니다.\n"
                         "본인이 `/로블록스연동` 으로 다시 등록할 수 있습니다."
                         if removed else
                         f"{member.mention} 님은 연동된 계정이 없습니다."),
            color=discord.Color.orange() if removed else discord.Color.greyple())

        # 연동을 필수로 걸어둔 서버에서 연동만 지우고 인증 역할을 남겨두면,
        # '링크를 받아간 사람은 전원 연동돼 있다'는 전제가 깨집니다.
        # 그래서 인증 역할도 함께 회수합니다.
        if removed:
            note = await self._revoke_verify_if_required(interaction.guild, member)
            if note:
                embed.add_field(name="♻️ 인증 역할", value=note, inline=False)

        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def _revoke_verify_if_required(self, guild: discord.Guild,
                                         member: discord.Member) -> str:
        """연동 필수 설정이 켜져 있으면 인증 역할을 회수하고 결과 문구를 돌려줍니다."""
        try:
            settings = await get_verify_settings(guild.id)
            if not settings.get("require_roblox", False):
                return ""
            role_id = settings.get("role_id")
            if not role_id:
                return ""
            role = guild.get_role(int(role_id))
            if role is None or not any(r.id == role.id for r in member.roles):
                return ""
            await member.remove_roles(role, reason="로블록스 연동 해제 (연동 필수 설정)")
            return f"{role.mention} 역할도 함께 회수했습니다.\n"                    "-# 다시 연동한 뒤 인증 버튼을 누르면 재발급됩니다."
        except discord.Forbidden:
            return "⚠️ 인증 역할을 회수하지 못했습니다 (봇 권한 부족)."
        except Exception as e:
            print(f"[RobloxLink] 인증 역할 회수 실패: {type(e).__name__}: {e}", flush=True)
            return "⚠️ 인증 역할 회수 중 오류가 발생했습니다."


async def _alert_relink(bot, guild_id: int, user_id: int,
                        before: dict, after: dict) -> None:
    """계정 교체를 로그 채널에 알립니다.

    갑작스러운 계정 교체는 그 자체로 확인해 볼 만한 신호입니다.
    (인증과 링크 보호는 같은 하위 시스템이라 로그 채널을 공유합니다)
    """
    try:
        settings = await get_verify_settings(guild_id)
        if not settings.get("roblox_relink_alert", True):
            return

        guard = await get_linkguard_settings(guild_id)
        channel_id = guard.get("log_channel_id")
        if not channel_id:
            return
        guild = bot.get_guild(int(guild_id))
        if guild is None:
            return
        channel = guild.get_channel(int(channel_id))
        if channel is None:
            return

        embed = discord.Embed(
            title="🔄 로블록스 계정 교체",
            description=f"<@{user_id}> 님이 연동 계정을 바꿨습니다.",
            color=discord.Color.orange()
        )
        embed.add_field(
            name="이전",
            value=f"**{before.get('roblox_name', '?')}**\n`{before.get('roblox_id', '?')}`",
            inline=True)
        embed.add_field(
            name="이후",
            value=f"**{after.get('roblox_name', '?')}**\n`{after.get('roblox_id', '?')}`",
            inline=True)
        embed.add_field(
            name="🙋 디스코드",
            value=f"`{user_id}`", inline=True)
        embed.set_footer(text="정상적인 계정 변경일 수도 있습니다 · /인증설정 에서 이 알림을 끌 수 있습니다")
        embed.timestamp = discord.utils.utcnow()
        await channel.send(embed=embed)
    except Exception as e:
        # 알림 실패가 연동 자체를 막으면 안 됩니다
        print(f"[RobloxLink] 교체 알림 실패: {type(e).__name__}: {e}", flush=True)


def _make_saver(bot, loop):
    """Flask 스레드가 호출할 저장 함수를 만듭니다.

    콜백은 Flask 스레드에서 처리되는데 저장은 async 라,
    봇의 이벤트 루프로 넘겨 실행하고 결과만 받아옵니다.
    """
    async def _save(guild_id: int, user_id: int, info: dict):
        # 로블록스 계정 하나는 디스코드 하나에만 연동되어야 합니다.
        # 그러지 않으면 여러 명이 같은 계정을 등록해 명단 대조가 무의미해집니다.
        owner = await find_roblox_owner(guild_id, info["roblox_id"])
        if owner and str(owner.get("user_id")) != str(user_id):
            return False, "taken"

        # 교체인지 판단하기 위해 이전 연동을 먼저 확인합니다
        previous = await get_roblox_link(guild_id, user_id)

        await save_roblox_link(
            guild_id, user_id, info["roblox_id"], info["roblox_name"],
            method="oauth", created_at=info.get("created_at", ""))

        if previous and int(previous.get("roblox_id", 0)) != int(info["roblox_id"]):
            print(f"[RobloxLink] 계정 교체: discord={user_id} "
                  f"{previous.get('roblox_id')} -> {info['roblox_id']}", flush=True)
            await _alert_relink(bot, guild_id, user_id, previous, info)
        return True, "ok"

    def bridge(guild_id: int, user_id: int, info: dict):
        future = asyncio.run_coroutine_threadsafe(
            _save(guild_id, user_id, info), loop)
        return future.result(timeout=15)

    return bridge


async def setup(bot):
    await bot.add_cog(RobloxLink(bot))
    # Flask 스레드에서 저장할 수 있도록 봇의 이벤트 루프를 연결합니다.
    roblox_oauth.set_saver(_make_saver(bot, asyncio.get_running_loop()))
    if roblox_oauth.enabled():
        print("[RobloxLink] 로블록스 연동: 활성화 (OAuth)", flush=True)
    else:
        print("[RobloxLink] 로블록스 연동: 비활성 "
              "(ROBLOX_CLIENT_ID / ROBLOX_CLIENT_SECRET 미설정)", flush=True)
