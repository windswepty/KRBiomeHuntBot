"""🏆 시즌 포인트.

기간을 정해 두면 그동안 **버는 포인트와 같은 양**이 시즌 포인트로도 쌓이고,
기간이 끝나면 **그대로 소멸**합니다(요청 2026-10-03).

  · 쌓이는 곳: 채팅 · 출석 · 퀘스트 (utils/database.py 의 세 경로가
    credit_earned 를 부릅니다). 호박 2배 · 채널 부스트도 그대로 따라갑니다.
  · /시즌설정     기간 · 이름 · 초기화 (관리자)
  · /시즌포인트   내 시즌 포인트 · 순위
  · /시즌교환     기본 포인트 2 → 시즌 포인트 1

초기화는 새 시즌 ID 를 발급하는 것입니다(utils/event_db.py 설명 참고).
"""

import re
import time
import datetime

import discord
from discord import app_commands
from discord.ext import commands

from utils.database import deduct_user_points, add_user_points, get_user_points, add_activity_log
from utils.event_db import (
    get_season, save_season, reset_season, season_state, get_season_points,
    add_season_points, season_top, season_stats, KST,
)

EXCHANGE_RATE = 2           # 기본 2 → 시즌 1
MAX_EXCHANGE = 1_000_000


_DT_RE = re.compile(
    r"^\s*(\d{4})-(\d{1,2})-(\d{1,2})"
    r"(?:\s+(오전|오후)?\s*(\d{1,2})(?::(\d{1,2})|\s*시(?:\s*(\d{1,2})\s*분)?)?)?\s*$")


def parse_kst(raw: str) -> datetime.datetime | None:
    """'2026-10-10 17:00' · '2026-10-10 오후 5시' · '2026-10-10 5:30' · '2026-10-10' (자정).

    ⚠️ 한 자리 월·일도 받고 **날짜로 바꿔서** 다룹니다 — 글자로 비교하다
    이벤트 퀘스트가 '기간 밖' 으로 뜬 적이 있습니다(docs/LESSONS.md).
    """
    m = _DT_RE.match(str(raw or ""))
    if not m:
        return None
    y, mo, d, ampm, h, mi, mi2 = m.groups()
    hour, minute = int(h or 0), int(mi or mi2 or 0)
    if ampm == "오후" and hour < 12:
        hour += 12
    if ampm == "오전" and hour == 12:
        hour = 0
    try:
        return datetime.datetime(int(y), int(mo), int(d), hour, minute, tzinfo=KST)
    except ValueError:
        return None


def fmt_kst(ts: float | None) -> str:
    if not ts:
        return "-"
    return datetime.datetime.fromtimestamp(float(ts), KST).strftime("%Y-%m-%d %H:%M")


STATE_LABEL = {"none": "⚫ 설정 안 됨", "before": "⏳ 시작 전", "active": "🟢 진행 중",
               "ended": "🏁 끝남 (시즌 포인트 소멸)"}


def season_period_text(s: dict) -> str:
    if not s.get("start"):
        return "기간이 정해지지 않았습니다"
    a, b = int(float(s["start"])), int(float(s["end"]))
    return f"<t:{a}:f> ~ <t:{b}:f>"


async def _admin(interaction: discord.Interaction) -> bool:
    perms = getattr(interaction.user, "guild_permissions", None)
    if perms is not None and perms.administrator:
        return True
    await interaction.response.send_message("⛔ 관리자만 바꿀 수 있습니다.", ephemeral=True)
    return False


def build_admin_embed(s: dict, stats: dict) -> discord.Embed:
    state = season_state(s)
    embed = discord.Embed(title="🏆 시즌 포인트 설정",
                          color=discord.Color.gold() if state == "active" else discord.Color.greyple())
    embed.description = (
        "기간 안에 **버는 포인트와 같은 양**이 시즌 포인트로도 쌓입니다.\n"
        "-# 채팅 · 출석 · 퀘스트 · 호박 2배 · 채널 부스트 전부 그대로 따라갑니다. "
        "관리자 지급 · 환불은 쌓이지 않습니다.\n"
        f"-# 기본 포인트 **{EXCHANGE_RATE} → 시즌 1** 교환은 `/시즌교환`, 기간이 끝나면 **소멸**합니다.")
    embed.add_field(name="이름", value=s.get("name") or "시즌", inline=True)
    embed.add_field(name="상태", value=STATE_LABEL[state], inline=True)
    embed.add_field(name="기간 (KST)", value=season_period_text(s), inline=False)
    embed.add_field(name="이번 시즌", value=f"**{stats.get('users', 0):,}명** · 총 "
                                             f"**{stats.get('total', 0):,}** 시즌 포인트", inline=False)
    embed.set_footer(text="기간을 고쳐도 모은 시즌 포인트는 그대로입니다 · 초기화만 지웁니다")
    return embed


class PeriodModal(discord.ui.Modal, title="📅 시즌 기간 · 이름"):
    def __init__(self, cog, s: dict):
        super().__init__()
        self.cog = cog
        self.name_in = discord.ui.TextInput(label="이름", default=s.get("name") or "시즌",
                                            max_length=40)
        self.start_in = discord.ui.TextInput(
            label="시작 (KST)", placeholder="예: 2026-10-10 17:00  또는  2026-10-10 오후 5시",
            default=fmt_kst(s.get("start")) if s.get("start") else None, max_length=30)
        self.end_in = discord.ui.TextInput(
            label="끝 (KST)", placeholder="예: 2026-11-14 17:00",
            default=fmt_kst(s.get("end")) if s.get("end") else None, max_length=30)
        for x in (self.name_in, self.start_in, self.end_in):
            self.add_item(x)

    async def on_submit(self, interaction: discord.Interaction):
        a, b = parse_kst(self.start_in.value), parse_kst(self.end_in.value)
        if a is None or b is None:
            await interaction.response.send_message(
                "⚠️ 시각은 `2026-10-10 17:00` 이나 `2026-10-10 오후 5시` 처럼 적어 주세요.",
                ephemeral=True)
            return
        if b <= a:
            await interaction.response.send_message("⚠️ 끝이 시작보다 빠릅니다.", ephemeral=True)
            return
        await interaction.response.defer()
        await save_season(interaction.guild.id, self.name_in.value.strip(), a.timestamp(),
                          b.timestamp())
        await self.cog.refresh_admin(interaction)


class ConfirmResetView(discord.ui.View):
    def __init__(self, cog):
        super().__init__(timeout=120)
        self.cog = cog

    @discord.ui.button(label="정말 전원의 시즌 포인트를 0 으로", emoji="🗑️",
                       style=discord.ButtonStyle.danger)
    async def yes(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await _admin(interaction):
            return
        await interaction.response.defer()
        before = await season_stats(interaction.guild.id)
        await reset_season(interaction.guild.id)
        try:
            await add_activity_log(interaction.guild.id, "admin", interaction.user.id,
                                   interaction.user.display_name,
                                   f"시즌 포인트 초기화 ({before.get('users', 0)}명 · "
                                   f"{before.get('total', 0):,})")
        except Exception as e:
            print(f"[Season] 활동 기록 실패: {type(e).__name__}: {e}", flush=True)
        await interaction.edit_original_response(
            content=f"🗑️ 시즌 포인트를 초기화했습니다 ({before.get('users', 0):,}명 · "
                    f"{before.get('total', 0):,}). 기간 설정은 그대로입니다.", view=None)


class AdminView(discord.ui.View):
    def __init__(self, cog, s: dict):
        super().__init__(timeout=600)
        self.cog, self.s = cog, s

    @discord.ui.button(label="기간 · 이름", emoji="📅", style=discord.ButtonStyle.primary)
    async def period(self, interaction: discord.Interaction, button: discord.ui.Button):
        if await _admin(interaction):
            await interaction.response.send_modal(PeriodModal(self.cog, self.s))

    @discord.ui.button(label="시즌 포인트 초기화", emoji="🗑️", style=discord.ButtonStyle.danger)
    async def reset(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await _admin(interaction):
            return
        await interaction.response.send_message(
            "🗑️ **모든 사람의 시즌 포인트를 0 으로** 되돌릴까요? 되돌릴 수 없습니다.\n"
            "-# 기본 포인트는 그대로입니다.", view=ConfirmResetView(self.cog), ephemeral=True)


class Season(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def refresh_admin(self, interaction: discord.Interaction) -> None:
        s = await get_season(interaction.guild.id, fresh=True)
        st = await season_stats(interaction.guild.id)
        await interaction.edit_original_response(embed=build_admin_embed(s, st),
                                                 view=AdminView(self, s))

    @app_commands.command(name="시즌설정", description="[관리자 전용] 시즌 포인트 기간 · 이름 · 초기화")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def season_settings(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        s = await get_season(interaction.guild.id, fresh=True)
        st = await season_stats(interaction.guild.id)
        await interaction.followup.send(embed=build_admin_embed(s, st),
                                        view=AdminView(self, s), ephemeral=True)

    @app_commands.command(name="시즌포인트", description="내 시즌 포인트와 순위를 봅니다.")
    @app_commands.guild_only()
    async def season_points(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        gid = interaction.guild.id
        s = await get_season(gid)
        state = season_state(s)
        embed = discord.Embed(title=f"🏆 {s.get('name') or '시즌'} 포인트",
                              color=discord.Color.gold())
        if state == "none":
            embed.description = "지금은 진행 중인 시즌이 없습니다."
        elif state == "before":
            embed.description = (f"시즌이 <t:{int(float(s['start']))}:R> 시작합니다.\n"
                                 f"기간: {season_period_text(s)}")
        elif state == "ended":
            embed.description = ("🏁 시즌이 끝나 시즌 포인트가 **소멸**했습니다.\n"
                                 f"기간: {season_period_text(s)}")
        else:
            mine = await get_season_points(gid, interaction.user.id)
            top = await season_top(gid, 10)
            embed.description = (f"내 시즌 포인트 **{mine:,}**\n"
                                 f"⏳ <t:{int(float(s['end']))}:R> 끝나면 소멸합니다\n"
                                 f"-# 채팅 · 출석 · 퀘스트로 버는 만큼 쌓입니다 · "
                                 f"`/시즌교환` 으로 기본 {EXCHANGE_RATE} → 시즌 1")
            if top:
                medals = ["🥇", "🥈", "🥉"]
                lines = [f"{medals[i] if i < 3 else f'`{i + 1}.`'} <@{r['user_id']}> — "
                         f"**{r['points']:,}**" for i, r in enumerate(top)]
                embed.add_field(name="순위 TOP 10", value="\n".join(lines)[:1024], inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True,
                                        allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name="시즌교환",
                          description=f"기본 포인트 {EXCHANGE_RATE} → 시즌 포인트 1 로 바꿉니다.")
    @app_commands.describe(시즌포인트="받을 시즌 포인트 (기본 포인트는 그 2배가 빠집니다)")
    @app_commands.guild_only()
    async def season_exchange(self, interaction: discord.Interaction,
                              시즌포인트: app_commands.Range[int, 1, MAX_EXCHANGE]):
        await interaction.response.defer(ephemeral=True, thinking=True)
        gid, uid = interaction.guild.id, interaction.user.id
        s = await get_season(gid, fresh=True)
        if season_state(s) != "active":
            await interaction.followup.send("⚠️ 지금은 시즌 기간이 아니라 교환할 수 없습니다.",
                                            ephemeral=True)
            return
        cost = 시즌포인트 * EXCHANGE_RATE
        ok, remaining = await deduct_user_points(gid, uid, cost)
        if not ok:
            await interaction.followup.send(
                f"⚠️ 기본 포인트가 부족합니다. 필요 **{cost:,} P** · 보유 **{remaining:,} P**",
                ephemeral=True)
            return
        added = await add_season_points(gid, uid, 시즌포인트)
        if added <= 0:
            # 차감과 적립 사이에 시즌이 끝났습니다 — 기본 포인트를 그대로 돌려줍니다
            await add_user_points(gid, uid, cost)
            await interaction.followup.send("⚠️ 방금 시즌이 끝나 교환하지 못했습니다. "
                                            f"**{cost:,} P** 는 돌려드렸습니다.", ephemeral=True)
            return
        try:
            await add_activity_log(gid, "season", uid, interaction.user.display_name,
                                   f"기본 {cost:,}P → 시즌 {시즌포인트:,}")
        except Exception as e:
            print(f"[Season] 활동 기록 실패: {type(e).__name__}: {e}", flush=True)
        embed = discord.Embed(
            title="🔁 교환 완료",
            description=(f"기본 **-{cost:,} P** → 시즌 **+{시즌포인트:,}**\n\n"
                         f"💰 기본 포인트 **{remaining:,} P**\n"
                         f"🏆 시즌 포인트 **{await get_season_points(gid, uid):,}**"),
            color=discord.Color.gold())
        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot):
    await bot.add_cog(Season(bot))
