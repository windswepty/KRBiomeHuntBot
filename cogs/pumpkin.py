"""🎃 호박 깨기 이벤트.

채널에 호박 그림 + [🔨 호박 때리기] 버튼을 올립니다.

  · 한 계정당 **하루(KST) 한 번** — 기록을 삽입해 판정(연타·동시 클릭에도 한 번)
  · 서버에 들어온 지 **24시간** 지난 계정만 (부계정으로 몰아 치기 방지)
  · 체력 1,500만 · 기본 1,000 데미지 · **20% 확률로 2,000**
  · 깨지면 **7일간 받는 포인트 2배** — 채팅 · 출석 · 퀘스트 전부
    (2026-10-05: 시즌 중에는 **시즌 포인트만** 2배, 시즌이 아니면 기본 포인트 2배
     — utils/event_db.multiplier_split)
    (utils/database.py 의 세 경로가 point_multiplier 를 곱합니다)

수치는 전부 /호박설정 에서 바꿉니다. 그림은 /호박이미지 로 올리고 DB 에 둡니다
— 디스코드 첨부 주소는 만료돼서 주소만 저장하면 며칠 뒤 그림이 깨집니다.

패널은 **메시지 하나를 계속 고칩니다**(상태 임베드와 같은 이유). 수백 명이
한꺼번에 쳐도 수정이 몰리지 않게 5초 디바운스 + 트레일링입니다.
"""

import io
import random
import asyncio
import datetime

import discord
from discord import app_commands
from discord.ext import commands, tasks

from utils import health
from utils.database import is_user_blacklisted, add_activity_log
from utils.event_db import (
    get_pumpkin, save_pumpkin, new_pumpkin_round, save_pumpkin_image,
    get_pumpkin_image, hit_pumpkin, pumpkin_hits_today, boost_active, KST,
    MAX_IMAGE_BYTES, pumpkin_top, pumpkin_my_rank,
)

REFRESH_DEBOUNCE = 5
REFRESH_LOOP_MINUTES = 5          # 자정이 지나 '오늘 참여' 가 바뀌고, 배율 기한이 끝나도 맞춥니다
IMAGE_TYPES = ("image/png", "image/jpeg", "image/gif", "image/webp")


# ── 화면 ───────────────────────────────────────────────────────────────

def hp_bar(hp: int, hp_max: int, width: int = 12) -> str:
    ratio = 0 if hp_max <= 0 else max(0.0, min(1.0, hp / hp_max))
    full = round(ratio * width)
    if hp > 0 and full == 0:
        full = 1               # 조금이라도 남았으면 한 칸은 보여 줍니다
    return "🟧" * full + "⬛" * (width - full) + f"  **{ratio * 100:.1f}%**"


def roll_damage(p: dict, rng: random.Random | None = None) -> tuple[int, bool]:
    r = (rng or random).random()
    crit = r < max(0.0, min(100.0, float(p.get("crit_percent", 20)))) / 100
    return (int(p["crit_damage"]) if crit else int(p["damage"])), crit


def image_name(p: dict) -> str:
    """올린 파일의 확장자를 따릅니다 — gif 를 .png 로 붙이면 움직이지 않을 수 있습니다."""
    raw = str((p or {}).get("image_name") or "")
    ext = raw.rsplit(".", 1)[-1].lower() if "." in raw else "png"
    return f"pumpkin.{ext if ext in ('png', 'jpg', 'jpeg', 'gif', 'webp') else 'png'}"


# 배율이 어디에 붙는지 — 시즌 중이면 시즌 포인트에만, 아니면 기본 포인트에.
# 패널은 시즌 상태를 모른 채 그려지므로(5분마다 갱신) 두 경우를 함께 적습니다.
OFF_SEASON_NOTE = "시즌 기간이 아니면 기본 포인트에 대신 적용"


RANK_SIZE = 10
MEDALS = ("🥇", "🥈", "🥉")


def rank_lines(top: list[dict]) -> str:
    """누적 데미지 TOP. 임베드 안의 멘션은 알림이 가지 않아 이름 표시용으로만 씁니다."""
    out = []
    for i, r in enumerate(top):
        mark = MEDALS[i] if i < len(MEDALS) else f"`{i + 1}.`"
        out.append(f"{mark} <@{r['user_id']}> — **{int(r['damage']):,}** "
                   f"({int(r.get('hits', 0)):,}번)")
    return "\n".join(out)[:1024]


def build_pumpkin_embed(p: dict, today_hits: int = 0, now: float | None = None,
                        with_image: bool = False, top: list[dict] | None = None) -> discord.Embed:
    hp, hp_max = max(0, int(p["hp"])), int(p["hp_max"])
    broken = hp <= 0
    embed = discord.Embed(title=f"🎃 {p.get('title') or '호박 깨기 이벤트'}",
                          color=discord.Color.dark_grey() if broken else discord.Color.orange())
    if broken:
        by = p.get("broken_by")
        lines = ["💥 **호박이 깨졌습니다!**" + (f" — 마지막 한 방 <@{by}>" if by else "")]
        if boost_active(p, now):
            until = int(float(p["boost_until"]))
            lines.append(f"🔥 **받는 시즌 포인트 {float(p['boost_mult']):g}배** — <t:{until}:f> 까지 "
                         f"(<t:{until}:R>)\n-# 채팅 · 출석 · 퀘스트 전부 · {OFF_SEASON_NOTE}")
        elif p.get("boost_until"):
            lines.append("-# 포인트 배율 기간은 끝났습니다.")
        embed.description = "\n".join(lines)
    else:
        embed.description = (
            f"❤️ **{hp:,} / {hp_max:,}**\n{hp_bar(hp, hp_max)}\n\n"
            f"🔨 하루 한 번 · 기본 **{int(p['damage']):,}** "
            f"(**{int(p['crit_percent'])}%** 확률로 **{int(p['crit_damage']):,}**)\n"
            f"🎁 깨지면 **{int(p['boost_days'])}일간 받는 시즌 포인트 {float(p['boost_mult']):g}배** "
            f"(채팅 · 출석 · 퀘스트 전부)\n-# {OFF_SEASON_NOTE}")
        if boost_active(p, now):
            until = int(float(p["boost_until"]))
            embed.description += f"\n🔥 지난 호박 보상 진행 중 — <t:{until}:f> 까지"
    embed.add_field(name="👥 오늘 참여", value=f"**{today_hits:,}명**", inline=True)
    embed.add_field(name="🔨 누적 타격", value=f"**{int(p.get('hits', 0)):,}번**", inline=True)
    if not p.get("enabled") and not broken:
        embed.add_field(name="⏸️ 지금은 쉬는 중", value="관리자가 다시 열면 칠 수 있습니다",
                        inline=False)
    if top:
        # 깨진 뒤에도 남겨 둡니다 — 이번 호박의 최종 순위입니다(새 호박이면 0 부터)
        embed.add_field(name=f"🏆 데미지 랭킹 TOP {RANK_SIZE}" + (" (최종)" if broken else ""),
                        value=rank_lines(top), inline=False)
    embed.set_footer(text=f"서버에 들어온 지 {int(p['min_member_hours'])}시간이 지난 계정만 · "
                          "매일 자정(KST)에 다시 칠 수 있습니다")
    if with_image:
        embed.set_image(url=f"attachment://{image_name(p)}")
    return embed


class PumpkinView(discord.ui.View):
    """영구 View — 재시작 뒤에도 버튼이 살아 있어야 합니다.
    **인스턴스를 모두가 공유**하므로 self 에 상태를 두지 않습니다."""

    def __init__(self, disabled: bool = False):
        super().__init__(timeout=None)
        self.hit.disabled = disabled

    @discord.ui.button(label="호박 때리기", emoji="🔨", style=discord.ButtonStyle.primary,
                       custom_id="pumpkin:hit")
    async def hit(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog = interaction.client.get_cog("Pumpkin") if interaction.client else None
        await handle_hit(interaction, cog)


async def handle_hit(interaction: discord.Interaction, cog=None) -> None:
    guild, member = interaction.guild, interaction.user
    p = await get_pumpkin(guild.id)
    hours = int(p.get("min_member_hours", 24))
    joined = getattr(member, "joined_at", None)
    if joined is None or discord.utils.utcnow() - joined < datetime.timedelta(hours=hours):
        when = (f"<t:{int((joined + datetime.timedelta(hours=hours)).timestamp())}:R> 부터"
                if joined else "나중에")
        await interaction.response.send_message(
            f"⏳ 서버에 들어온 지 **{hours}시간**이 지난 계정만 칠 수 있습니다. {when} 칠 수 있어요.",
            ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    if await is_user_blacklisted(guild.id, member.id):
        await interaction.followup.send("⛔ 포인트 활동이 제한된 계정입니다.", ephemeral=True)
        return
    damage, crit = roll_damage(p)
    res = await hit_pumpkin(guild.id, member.id, damage)
    status = res.get("status")
    if status == "already":
        tomorrow = datetime.datetime.now(KST).replace(hour=0, minute=0, second=0,
                                                      microsecond=0) + datetime.timedelta(days=1)
        await interaction.followup.send(
            f"🕛 오늘은 이미 쳤습니다. <t:{int(tomorrow.timestamp())}:R>(자정) 다시 칠 수 있어요.",
            ephemeral=True)
        return
    if status == "off":
        await interaction.followup.send("⏸️ 지금은 호박 깨기를 쉬고 있습니다.", ephemeral=True)
        return
    if status == "dead":
        await interaction.followup.send("💥 호박은 이미 깨졌습니다! 다음 호박을 기다려 주세요.",
                                        ephemeral=True)
        return

    hp, hp_max = int(res["hp"]), int(res["hp_max"])
    title = (f"💥 치명타! **{damage:,}** 데미지!" if crit else f"🔨 **{damage:,}** 데미지!")
    embed = discord.Embed(title=title, color=discord.Color.red() if crit else discord.Color.orange())
    embed.description = f"남은 체력 **{hp:,} / {hp_max:,}**\n{hp_bar(hp, hp_max)}"
    if res.get("broke"):
        embed.title = "🎉 호박을 깼습니다!"
        embed.description = f"마지막 한 방 **{damage:,}**! 지금부터 모두의 포인트가 늘어납니다."
    try:
        total, rank = await pumpkin_my_rank(guild.id, int(p["round"]), member.id)
        if total:
            embed.add_field(name="🏆 내 누적 데미지", value=f"**{total:,}** · **{rank:,}위**",
                            inline=False)
    except Exception as e:
        print(f"[Pumpkin] 내 순위 조회 실패: {type(e).__name__}: {e}", flush=True)
    embed.set_footer(text="내일 자정(KST) 이후 다시 칠 수 있습니다")
    await interaction.followup.send(embed=embed, ephemeral=True)

    if res.get("broke"):
        await announce_broken(guild, member, res)
        try:
            await add_activity_log(guild.id, "event", member.id, member.display_name,
                                   "🎃 호박을 깼습니다 (포인트 배율 시작)")
        except Exception as e:
            print(f"[Pumpkin] 활동 기록 실패: {type(e).__name__}: {e}", flush=True)
    if cog is not None:
        cog.schedule_refresh(guild)


async def announce_broken(guild, member, res: dict) -> None:
    """깨진 순간은 채널에 **공개로** 알립니다 — 모두가 받는 보상이라서입니다."""
    p = await get_pumpkin(guild.id, fresh=True)
    channel = guild.get_channel(int(p["channel_id"])) if p.get("channel_id") else None
    if channel is None:
        return
    until = res.get("boost_until") or p.get("boost_until")
    text = (f"🎉 {member.mention} 님이 호박을 깼습니다!\n"
            f"🔥 지금부터 **{int(p['boost_days'])}일간 받는 시즌 포인트 {float(p['boost_mult']):g}배** "
            f"(채팅 · 출석 · 퀘스트 전부)"
            + (f" — <t:{int(float(until))}:f> 까지" if until else "")
            + f"\n-# {OFF_SEASON_NOTE}")
    try:
        await channel.send(content=text, allowed_mentions=discord.AllowedMentions(
            everyone=False, roles=False, users=[member]))
    except discord.HTTPException as e:
        print(f"[Pumpkin] 깨짐 알림 실패: {type(e).__name__}: {e}", flush=True)


# ── 관리자 ─────────────────────────────────────────────────────────────

async def _admin(interaction: discord.Interaction) -> bool:
    perms = getattr(interaction.user, "guild_permissions", None)
    if perms is not None and perms.administrator:
        return True
    await interaction.response.send_message("⛔ 관리자만 바꿀 수 있습니다.", ephemeral=True)
    return False


def build_admin_embed(p: dict, today_hits: int) -> discord.Embed:
    embed = build_pumpkin_embed(p, today_hits)
    embed.title = "🛠️ 호박 깨기 설정"
    embed.add_field(name="상태", value="🟢 켜짐" if p["enabled"] else "⚫ 꺼짐", inline=True)
    embed.add_field(name="패널", value=(f"<#{p['channel_id']}>" if p.get("channel_id")
                                       else "없음 — **패널 올리기**"), inline=True)
    embed.add_field(name="그림", value="🟢 있음" if p.get("has_image") else
                    "없음 — `/호박이미지` 로 올리세요", inline=True)
    embed.add_field(name="수치", value=(
        f"체력 {int(p['hp_max']):,} · 데미지 {int(p['damage']):,} / "
        f"치명타 {int(p['crit_damage']):,} ({int(p['crit_percent'])}%)\n"
        f"가입 후 {int(p['min_member_hours'])}시간 · 보상 {float(p['boost_mult']):g}배 × "
        f"{int(p['boost_days'])}일"), inline=False)
    embed.set_footer(text="이 화면은 본인에게만 보입니다")
    return embed


def _int(raw, lo, hi):
    try:
        v = int(str(raw).replace(",", "").strip())
    except ValueError:
        return None
    return v if lo <= v <= hi else None


class NumbersModal(discord.ui.Modal, title="🛠️ 호박 수치"):
    def __init__(self, cog, p: dict):
        super().__init__()
        self.cog = cog
        self.hp = discord.ui.TextInput(label="체력", default=str(int(p["hp_max"])), max_length=12)
        self.dmg = discord.ui.TextInput(label="기본 데미지", default=str(int(p["damage"])), max_length=9)
        self.crit = discord.ui.TextInput(label="치명타 데미지", default=str(int(p["crit_damage"])),
                                         max_length=9)
        self.rate = discord.ui.TextInput(label="치명타 확률 (%)", default=str(int(p["crit_percent"])),
                                         max_length=3)
        self.hours = discord.ui.TextInput(label="서버 가입 후 시간 (부계정 방지)",
                                          default=str(int(p["min_member_hours"])), max_length=4)
        for x in (self.hp, self.dmg, self.crit, self.rate, self.hours):
            self.add_item(x)

    async def on_submit(self, interaction: discord.Interaction):
        vals = {"hp_max": _int(self.hp.value, 1, 10 ** 11), "damage": _int(self.dmg.value, 1, 10 ** 9),
                "crit_damage": _int(self.crit.value, 1, 10 ** 9),
                "crit_percent": _int(self.rate.value, 0, 100),
                "min_member_hours": _int(self.hours.value, 0, 24 * 365)}
        bad = [k for k, v in vals.items() if v is None]
        if bad:
            await interaction.response.send_message(
                "⚠️ 숫자를 확인해 주세요 (확률은 0~100, 나머지는 1 이상).", ephemeral=True)
            return
        await interaction.response.defer()
        before = await get_pumpkin(interaction.guild.id, fresh=True)
        await save_pumpkin(interaction.guild.id, vals)
        note = ""
        if vals["hp_max"] != int(before["hp_max"]):
            if int(before.get("hits", 0)) == 0:
                await new_pumpkin_round(interaction.guild.id)   # 아직 아무도 안 쳤으면 바로 적용
            else:
                note = "\n-# 체력은 **다음 호박**부터 적용됩니다 (이미 맞은 호박은 그대로)."
        await self.cog.refresh_admin(interaction, note)


class RewardModal(discord.ui.Modal, title="🎁 보상 · 제목"):
    def __init__(self, cog, p: dict):
        super().__init__()
        self.cog = cog
        self.mult = discord.ui.TextInput(label="깨지면 포인트 배율 (예: 2)",
                                         default=f"{float(p['boost_mult']):g}", max_length=4)
        self.days = discord.ui.TextInput(label="배율 기간 (일)", default=str(int(p["boost_days"])),
                                         max_length=3)
        self.name = discord.ui.TextInput(label="패널 제목", default=p.get("title") or "호박 깨기 이벤트",
                                         max_length=40)
        for x in (self.mult, self.days, self.name):
            self.add_item(x)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            mult = float(self.mult.value.strip())
        except ValueError:
            mult = 0
        days = _int(self.days.value, 1, 60)
        if not (1 <= mult <= 10) or days is None:
            await interaction.response.send_message(
                "⚠️ 배율은 1~10, 기간은 1~60일로 적어 주세요.", ephemeral=True)
            return
        await interaction.response.defer()
        await save_pumpkin(interaction.guild.id, {"boost_mult": mult, "boost_days": days,
                                                  "title": self.name.value.strip() or "호박 깨기 이벤트"})
        await self.cog.refresh_admin(interaction, "\n-# 배율 · 기간은 **다음에 깨질 때**부터 적용됩니다.")


class PanelChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, cog):
        super().__init__(placeholder="📌 패널을 올릴 채널", channel_types=[discord.ChannelType.text],
                         row=0)
        self.cog = cog

    async def callback(self, interaction: discord.Interaction):
        if not await _admin(interaction):
            return
        await interaction.response.defer()
        channel = interaction.guild.get_channel(self.values[0].id)
        err = await self.cog.post_panel(interaction.guild, channel)
        await self.cog.refresh_admin(interaction, f"\n⚠️ {err}" if err else
                                     f"\n✅ {channel.mention} 에 패널을 올렸습니다.")


class ConfirmNewView(discord.ui.View):
    def __init__(self, cog):
        super().__init__(timeout=120)
        self.cog = cog

    @discord.ui.button(label="정말 새 호박 (체력 다시 꽉 채움)", emoji="🎃",
                       style=discord.ButtonStyle.danger)
    async def yes(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await _admin(interaction):
            return
        await interaction.response.defer()
        await new_pumpkin_round(interaction.guild.id)
        self.cog.schedule_refresh(interaction.guild, delay=0)
        await self.cog.refresh_admin(interaction, "\n🎃 새 호박을 놓았습니다 (오늘 친 사람도 다시 칠 수 있음).")


class AdminView(discord.ui.View):
    def __init__(self, cog, p: dict):
        super().__init__(timeout=600)
        self.cog, self.p = cog, p
        self.add_item(PanelChannelSelect(cog))
        self.toggle.label = "끄기" if p["enabled"] else "켜기"
        self.toggle.emoji = "⏸️" if p["enabled"] else "▶️"
        self.toggle.style = discord.ButtonStyle.secondary if p["enabled"] else discord.ButtonStyle.success

    @discord.ui.button(label="켜기", emoji="▶️", row=1)
    async def toggle(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await _admin(interaction):
            return
        await interaction.response.defer()
        await save_pumpkin(interaction.guild.id, {"enabled": not self.p["enabled"]})
        self.cog.schedule_refresh(interaction.guild, delay=0)
        await self.cog.refresh_admin(interaction)

    @discord.ui.button(label="수치", emoji="🛠️", style=discord.ButtonStyle.secondary, row=1)
    async def numbers(self, interaction: discord.Interaction, button: discord.ui.Button):
        if await _admin(interaction):
            await interaction.response.send_modal(NumbersModal(self.cog, self.p))

    @discord.ui.button(label="보상 · 제목", emoji="🎁", style=discord.ButtonStyle.secondary, row=1)
    async def reward(self, interaction: discord.Interaction, button: discord.ui.Button):
        if await _admin(interaction):
            await interaction.response.send_modal(RewardModal(self.cog, self.p))

    @discord.ui.button(label="새 호박", emoji="🎃", style=discord.ButtonStyle.danger, row=1)
    async def new_round(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await _admin(interaction):
            return
        await interaction.response.send_message(
            "🎃 체력을 꽉 채운 새 호박을 놓을까요? 지금 호박의 남은 체력은 사라집니다.\n"
            "-# 진행 중인 포인트 배율은 그대로 이어집니다.",
            view=ConfirmNewView(self.cog), ephemeral=True)


# ── 본체 ───────────────────────────────────────────────────────────────

class Pumpkin(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._pending: dict[int, asyncio.Task] = {}
        self._locks: dict[int, asyncio.Lock] = {}
        self.periodic.start()

    def cog_unload(self):
        self.periodic.cancel()

    async def render(self, guild) -> tuple[discord.Embed, discord.ui.View, dict]:
        p = await get_pumpkin(guild.id, fresh=True)
        today = await pumpkin_hits_today(guild.id, int(p["round"]))
        top = await pumpkin_top(guild.id, int(p["round"]), RANK_SIZE)
        embed = build_pumpkin_embed(p, today, with_image=bool(p.get("has_image")), top=top)
        view = PumpkinView(disabled=int(p["hp"]) <= 0 or not p["enabled"])
        return embed, view, p

    async def post_panel(self, guild, channel) -> str | None:
        if channel is None:
            return "채널을 찾을 수 없습니다."
        perms = channel.permissions_for(guild.me)
        if not (perms.send_messages and perms.embed_links and perms.attach_files):
            return f"{channel.mention} 에 봇의 '메시지 보내기 · 링크 첨부 · 파일 첨부' 권한이 없습니다."
        async with self._locks.setdefault(guild.id, asyncio.Lock()):
            embed, view, p = await self.render(guild)
            kw = {}
            img = await get_pumpkin_image(guild.id) if p.get("has_image") else None
            if img:
                kw["file"] = discord.File(io.BytesIO(img[0]), filename=image_name(p))
            else:
                embed.set_image(url=None)
            msg = await channel.send(embed=embed, view=view, **kw)
            await save_pumpkin(guild.id, {"channel_id": channel.id, "message_id": msg.id})
        return None

    async def refresh_panel(self, guild, new_image: bool = False) -> None:
        async with self._locks.setdefault(guild.id, asyncio.Lock()):
            embed, view, p = await self.render(guild)
            if not p.get("channel_id") or not p.get("message_id"):
                return
            channel = guild.get_channel(int(p["channel_id"]))
            if channel is None:
                return
            kw = {}
            if new_image:
                img = await get_pumpkin_image(guild.id)
                if img:
                    kw["attachments"] = [discord.File(io.BytesIO(img[0]), filename=image_name(p))]
            try:
                # 첨부를 넘기지 않으면 기존 그림이 그대로 남습니다
                await channel.get_partial_message(int(p["message_id"])).edit(
                    embed=embed, view=view, **kw)
            except discord.NotFound:
                print("[Pumpkin] 패널 메시지가 지워졌습니다 — /호박설정 에서 다시 올리세요", flush=True)
                await save_pumpkin(guild.id, {"message_id": None})
            except discord.HTTPException as e:
                print(f"[Pumpkin] 패널 갱신 실패: {type(e).__name__}: {e}", flush=True)

    def schedule_refresh(self, guild, delay: float = REFRESH_DEBOUNCE) -> None:
        """수백 명이 몰려도 수정은 5초에 한 번. 창 안 마지막 타격도 반영됩니다(트레일링)."""
        task = self._pending.get(guild.id)
        if task is not None and not task.done():
            return

        async def _later():
            await asyncio.sleep(delay)
            self._pending.pop(guild.id, None)
            await self.refresh_panel(guild)

        self._pending[guild.id] = asyncio.create_task(_later())

    async def refresh_admin(self, interaction: discord.Interaction, note: str = "") -> None:
        p = await get_pumpkin(interaction.guild.id, fresh=True)
        today = await pumpkin_hits_today(interaction.guild.id, int(p["round"]))
        embed = build_admin_embed(p, today)
        if note:
            embed.description = (embed.description or "") + "\n" + note
        await interaction.edit_original_response(embed=embed, view=AdminView(self, p))

    @tasks.loop(minutes=REFRESH_LOOP_MINUTES)
    async def periodic(self):
        await self.bot.wait_until_ready()
        for guild in self.bot.guilds:
            try:
                p = await get_pumpkin(guild.id)
                if p.get("message_id"):
                    await self.refresh_panel(guild)
            except Exception as e:
                print(f"[Pumpkin] 주기 갱신 실패: {type(e).__name__}: {e}", flush=True)

    @periodic.error
    async def periodic_error(self, error):
        await health.revive_loop(self.periodic, "pumpkin_refresh", error)

    @app_commands.command(name="호박설정", description="[관리자 전용] 호박 깨기 이벤트를 설정합니다.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def pumpkin_settings(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        p = await get_pumpkin(interaction.guild.id, fresh=True)
        today = await pumpkin_hits_today(interaction.guild.id, int(p["round"]))
        await interaction.followup.send(embed=build_admin_embed(p, today),
                                        view=AdminView(self, p), ephemeral=True)

    @app_commands.command(name="호박이미지", description="[관리자 전용] 호박 패널의 그림을 올립니다.")
    @app_commands.describe(이미지="호박 그림 (png · jpg · gif · webp, 4MB 이하)")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def pumpkin_image(self, interaction: discord.Interaction, 이미지: discord.Attachment):
        await interaction.response.defer(ephemeral=True, thinking=True)
        ctype = (이미지.content_type or "").split(";")[0].lower()
        if ctype not in IMAGE_TYPES:
            await interaction.followup.send("⚠️ png · jpg · gif · webp 그림만 올릴 수 있습니다.",
                                            ephemeral=True)
            return
        if 이미지.size > MAX_IMAGE_BYTES:
            await interaction.followup.send("⚠️ 4MB 이하 그림만 올릴 수 있습니다.", ephemeral=True)
            return
        try:
            data = await 이미지.read()
            await save_pumpkin_image(interaction.guild.id, data, 이미지.filename)
        except (discord.HTTPException, ValueError) as e:
            await interaction.followup.send(f"⚠️ 그림을 저장하지 못했습니다: {type(e).__name__}",
                                            ephemeral=True)
            return
        await self.refresh_panel(interaction.guild, new_image=True)
        await interaction.followup.send("✅ 호박 그림을 바꿨습니다. 올려 둔 패널에도 바로 반영됩니다.",
                                        ephemeral=True)


async def setup(bot):
    await bot.add_cog(Pumpkin(bot))
    bot.add_view(PumpkinView())
