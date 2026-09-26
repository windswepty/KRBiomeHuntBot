"""채널 잠금 — Dyno 의 `/lock` 과 같은 것 + 매일 정해진 시간 자동 잠금.

  /잠금 [채널] [기간] [사유]   지금 잠급니다. 기간이 지나면 저절로 풀립니다.
  /잠금해제 [채널]             지금 풉니다.
  /채널잠금설정                매일 00:00~07:00(KST) 같은 시간에 자동으로 잠글 채널

잠기면 **관리자 권한이 있는 사람만** 채팅할 수 있습니다(관리자는 채널 권한을
무시합니다). 이 서버의 스태프 역할은 전부 관리자 권한이 있습니다.

## Dyno 와 다른 점 — 따로 허용된 사람까지 막습니다

Dyno 는 `@everyone` 의 '메시지 보내기' 만 끕니다. 그런데 디스코드는 **채널에
역할·사람별로 '허용' 이 걸려 있으면 그게 이깁니다.** 이 서버는 텍스트 채널
347개 중 46개에 그런 허용이 있어(2026-09-26 실측) Dyno 식으로는 그 사람들이
잠금 중에도 채팅합니다. 그래서 잠글 때 그 허용을 **잠시 거두고**, 풀 때
**원래 값 그대로** 되돌립니다. 관리자 역할 · 봇은 건드리지 않습니다.

## 어떻게 도는가 — '맞춰 놓기' 방식

30초마다 "지금 잠겨 있어야 하는가" 를 계산해 실제 채널을 거기에 맞춥니다.
명령어도 상태만 바꾸고 같은 맞추기를 부릅니다. 그래서

  · 봇이 7시에 꺼져 있었어도 다시 켜지는 순간 풀립니다.
  · 권한 변경이 한 번 실패해도 다음 주기에 다시 시도합니다.
  · 수동 잠금과 자동 잠금이 겹쳐도 원래 권한은 **처음 잠글 때 한 번만** 저장합니다.
    (두 번째 잠금이 '이미 잠긴 상태' 를 원래 값으로 저장하면 영영 못 풉니다)
"""

import re
import asyncio
import datetime

import discord
from discord import app_commands
from discord.ext import commands, tasks

from utils import health
from utils.mod_db import (
    get_lock_doc, save_lock_settings, set_lock_state,
    MAX_LOCK_CHANNELS, KST,
)

TICK_SECONDS = 30
MAX_LOCK_MINUTES = 30 * 24 * 60          # 수동 잠금 최대 30일

# 막는 권한. 스레드에서 쓰기 · 스레드 만들기도 막아야 '채팅 금지' 가 됩니다.
LOCK_PERMS = ("send_messages", "send_messages_in_threads",
              "create_public_threads", "create_private_threads")

LOCKABLE_TYPES = (discord.ChannelType.text, discord.ChannelType.news)


# ── 시간 ──────────────────────────────────────────────────────────────

def parse_hhmm(raw) -> int | None:
    """'0' · '00' · '7' · '07:00' · '7:30' · '7시' · '7시30분' → 하루 중 분."""
    s = str(raw or "").strip().replace(" ", "")
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2})|시(?:(\d{1,2})분)?)?", s)
    if not m:
        return None
    h = int(m.group(1))
    mi = int(m.group(2) or m.group(3) or 0)
    if h == 24 and mi == 0:
        h = 0
    if not (0 <= h <= 23 and 0 <= mi <= 59):
        return None
    return h * 60 + mi


def fmt_hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def in_window(minute: int, start: int, end: int) -> bool:
    """자정을 넘는 구간(00:00~07:00 이 아니라 22:00~06:00 같은)도 다룹니다."""
    if start == end:
        return False
    if start < end:
        return start <= minute < end
    return minute >= start or minute < end


def window_minutes(start: int, end: int) -> int:
    return (end - start) % (24 * 60)


def window_end_after(now: datetime.datetime, end: int) -> datetime.datetime:
    """지금 이후로 가장 가까운 '끝' 시각 (자동 잠금 중 풀었을 때 그날 밤만 쉬려고)."""
    cand = now.replace(hour=end // 60, minute=end % 60, second=0, microsecond=0)
    if cand <= now:
        cand += datetime.timedelta(days=1)
    return cand


def next_at(now: datetime.datetime, minute: int) -> datetime.datetime:
    return window_end_after(now, minute)


_DUR_UNITS = {"d": 1440, "일": 1440, "h": 60, "시간": 60, "m": 1, "분": 1}


def parse_duration(raw) -> int | None:
    """'30분' · '2시간' · '1시간30분' · '1d' · '2h30m' → 분. 못 읽으면 None."""
    s = str(raw or "").strip().lower().replace(" ", "")
    if not s:
        return None
    if s.isdigit():                      # 단위 없이 숫자만 → 분
        n = int(s)
        return n if 0 < n <= MAX_LOCK_MINUTES else None
    total, pos = 0, 0
    for m in re.finditer(r"(\d+)(d|일|h|시간|m|분)", s):
        if m.start() != pos:
            return None
        total += int(m.group(1)) * _DUR_UNITS[m.group(2)]
        pos = m.end()
    if pos != len(s) or not (0 < total <= MAX_LOCK_MINUTES):
        return None
    return total


def fmt_duration(minutes: int) -> str:
    d, rest = divmod(int(minutes), 1440)
    h, m = divmod(rest, 60)
    parts = [f"{d}일" if d else "", f"{h}시간" if h else "", f"{m}분" if m else ""]
    return " ".join(p for p in parts if p) or "0분"


# ── 권한 계산 (채널을 바꾸지 않는 순수 함수) ─────────────────────────────

def _is_role(target) -> bool:
    return isinstance(target, discord.Role) or (
        isinstance(target, discord.Object) and target.type is discord.Role)


def target_key(target) -> str:
    return ("r" if _is_role(target) else "m") + str(target.id)


def _target_for(guild, key: str):
    tid = int(key[1:])
    if key[0] == "r":
        return guild.get_role(tid) or discord.Object(id=tid, type=discord.Role)
    return guild.get_member(tid) or discord.Object(id=tid, type=discord.Member)


def exempt(target) -> bool:
    """허용을 거두지 않는 대상 — 관리자(어차피 잠금을 무시)와 봇."""
    if isinstance(target, discord.Role):
        return target.permissions.administrator or target.is_bot_managed()
    if isinstance(target, discord.Member):
        return target.bot or target.guild_permissions.administrator
    return False


def plan_lock(channel) -> dict:
    """잠그기 전 원래 값. `{key: {"existed": bool, "perms": {권한: True/False/None}}}`"""
    everyone = channel.guild.default_role
    current = channel.overwrites
    saved = {}
    ow = current.get(everyone)
    saved[target_key(everyone)] = {
        "existed": ow is not None,
        "perms": {p: (getattr(ow, p) if ow else None) for p in LOCK_PERMS}}
    for target, ow in current.items():
        if target.id == everyone.id or exempt(target):
            continue
        if any(getattr(ow, p) is True for p in LOCK_PERMS):
            saved[target_key(target)] = {
                "existed": True, "perms": {p: getattr(ow, p) for p in LOCK_PERMS}}
    return saved


def locked_overwrites(channel, saved: dict) -> dict:
    """원래 값(saved)을 기준으로 '잠긴 모양' 을 만듭니다. 몇 번 불러도 결과가 같습니다."""
    everyone = channel.guild.default_role
    new = dict(channel.overwrites)
    by_key = {target_key(t): t for t in new}
    for key, info in saved.items():
        target = by_key.get(key) or (everyone if key == target_key(everyone)
                                     else _target_for(channel.guild, key))
        ow = new.get(target) or discord.PermissionOverwrite()
        ow = discord.PermissionOverwrite(**dict(iter(ow)))
        if target.id == everyone.id:
            for p in LOCK_PERMS:
                setattr(ow, p, False)
        else:
            # '허용' 을 중립으로 — 그러면 @everyone 의 거부를 따릅니다
            for p in LOCK_PERMS:
                if (info.get("perms") or {}).get(p) is True:
                    setattr(ow, p, None)
        new[target] = ow
    return new


def restored_overwrites(channel, saved: dict) -> dict:
    """잠근 권한만 원래 값으로. 그 사이 관리자가 바꾼 **다른 권한은 그대로** 둡니다."""
    new = dict(channel.overwrites)
    by_key = {target_key(t): t for t in new}
    for key, info in saved.items():
        target = by_key.get(key) or _target_for(channel.guild, key)
        ow = discord.PermissionOverwrite(**dict(iter(new.get(target) or discord.PermissionOverwrite())))
        for p in LOCK_PERMS:
            setattr(ow, p, (info.get("perms") or {}).get(p))
        if not info.get("existed") and ow.is_empty():
            new.pop(target, None)       # 원래 없던 칸은 지웁니다(카테고리 동기화 유지)
        else:
            new[target] = ow
    return new


def overwrites_equal(a: dict, b: dict) -> bool:
    def norm(d):
        return {target_key(t): tuple(v.value for v in ow.pair()) for t, ow in d.items()}
    return norm(a) == norm(b)


# ── 무엇을 잠가야 하는가 ─────────────────────────────────────────────────

def desired_locks(doc: dict, now: datetime.datetime) -> dict:
    """{채널: 이유} — 이유는 'manual' 또는 'schedule'."""
    ts = now.timestamp()
    out = {}
    for cid, m in (doc.get("manual") or {}).items():
        until = (m or {}).get("until")
        if until is None or ts < float(until):
            out[int(cid)] = "manual"
    start, end = parse_hhmm(doc.get("start")), parse_hhmm(doc.get("end"))
    if doc.get("enabled") and start is not None and end is not None \
            and in_window(now.hour * 60 + now.minute, start, end):
        opened = doc.get("open_until") or {}
        for cid in doc.get("channel_ids") or []:
            if float(opened.get(str(cid), 0) or 0) > ts:
                continue        # 자동 잠금 시간 중 관리자가 풀어 둔 채널
            out.setdefault(int(cid), "schedule")
    return out


# ── 안내 · 패널 ──────────────────────────────────────────────────────

def lock_notice(reason: str, doc: dict, cid: int) -> discord.Embed:
    embed = discord.Embed(title="🔒 이 채널은 잠겨 있습니다", color=discord.Color.dark_grey())
    if reason == "manual":
        m = (doc.get("manual") or {}).get(str(cid)) or {}
        until = m.get("until")
        embed.description = (f"<t:{int(until)}:f> (<t:{int(until)}:R>)에 열립니다."
                             if until else "관리자가 풀 때까지 채팅할 수 없습니다.")
        if m.get("reason"):
            embed.add_field(name="사유", value=str(m["reason"])[:1000], inline=False)
    else:
        end = parse_hhmm(doc.get("end")) or 0
        embed.description = (f"매일 **{doc.get('start')} ~ {doc.get('end')}** 는 채팅을 쉽니다.\n"
                             f"**{fmt_hhmm(end)}** 에 다시 열립니다.")
    embed.set_footer(text="관리자는 잠금 중에도 채팅할 수 있습니다")
    return embed


def bot_problem(channel) -> str | None:
    me = channel.guild.me
    perms = channel.permissions_for(me)
    if not perms.view_channel:
        return "채널을 볼 수 없음"
    if not perms.manage_roles:
        return "'권한 관리' 권한이 없음"
    return None


def build_panel(guild, doc: dict, now: datetime.datetime | None = None) -> discord.Embed:
    now = now or datetime.datetime.now(KST)
    start, end = parse_hhmm(doc["start"]), parse_hhmm(doc["end"])
    on = doc.get("enabled")
    embed = discord.Embed(
        title="🔒 채널 자동 잠금",
        description=("정한 시간에 채널을 잠가 **관리자만** 채팅할 수 있게 합니다.\n"
                     "지금 바로 잠그려면 `/잠금` · 풀려면 `/잠금해제`."),
        color=discord.Color.dark_grey() if not on else discord.Color.blurple())
    span = f"매일 **{doc['start']} ~ {doc['end']}** (KST · {fmt_duration(window_minutes(start, end))})"
    embed.add_field(name="상태", value=("🟢 켜짐" if on else "⚫ 꺼짐") + "\n" + span, inline=False)
    if on:
        active = in_window(now.hour * 60 + now.minute, start, end)
        nxt = next_at(now, end if active else start)
        embed.add_field(name="지금",
                        value=(f"🔒 잠금 시간 — <t:{int(nxt.timestamp())}:t> 에 열림" if active
                               else f"🔓 열림 — <t:{int(nxt.timestamp())}:t> 에 잠김"),
                        inline=False)
    locked = doc.get("locked") or {}
    lines, problems = [], []
    for cid in doc.get("channel_ids") or []:
        ch = guild.get_channel(cid)
        if ch is None:
            lines.append(f"❔ `{cid}` (채널이 없어졌습니다)")
            continue
        extra = len(plan_lock(ch)) - 1
        mark = "🔒" if str(cid) in locked else "🔓"
        lines.append(f"{mark} {ch.mention}" + (f" · 따로 허용 {extra}개도 잠시 거둠" if extra else ""))
        why = bot_problem(ch)
        if why:
            problems.append(f"{ch.mention}: {why}")
    body = "\n".join(lines) or "없음 — 아래 **채널 추가** 로 고르세요"
    embed.add_field(name=f"자동으로 잠글 채널 ({len(doc.get('channel_ids') or [])}/{MAX_LOCK_CHANNELS})",
                    value=body[:1024], inline=False)
    manual = [c for c in (doc.get("manual") or {}) if c not in {str(x) for x in doc.get("channel_ids") or []}]
    if manual:
        embed.add_field(name="`/잠금` 으로 잠긴 다른 채널",
                        value=" ".join(f"<#{c}>" for c in manual)[:1024], inline=False)
    embed.add_field(name="📢 잠금 안내", value=(
        "🟢 잠글 때 채널에 안내를 올리고, 풀 때 지웁니다" if doc.get("announce")
        else "⚫ 안내 없이 조용히 잠급니다"), inline=False)
    if problems:
        # 초록불만 띄우면 잠기지 않는 걸 아침에야 압니다
        embed.add_field(name="⚠️ 봇이 잠글 수 없는 채널", value="\n".join(problems)[:1024], inline=False)
        embed.color = discord.Color.orange()
    embed.set_footer(text="관리자 권한이 있는 사람은 잠금 중에도 채팅할 수 있습니다")
    return embed


async def _require_admin(interaction: discord.Interaction) -> bool:
    perms = getattr(interaction.user, "guild_permissions", None)
    if perms is not None and perms.administrator:
        return True
    await interaction.response.send_message("⛔ 관리자만 바꿀 수 있습니다.", ephemeral=True)
    return False


class TimeModal(discord.ui.Modal, title="🕛 자동 잠금 시간"):
    start = discord.ui.TextInput(label="잠그는 시각 (KST)", placeholder="예: 00:00", max_length=8)
    end = discord.ui.TextInput(label="여는 시각 (KST)", placeholder="예: 07:00", max_length=8)

    def __init__(self, cog, doc: dict):
        super().__init__()
        self.cog = cog
        self.start.default = doc.get("start")
        self.end.default = doc.get("end")

    async def on_submit(self, interaction: discord.Interaction):
        s, e = parse_hhmm(self.start.value), parse_hhmm(self.end.value)
        if s is None or e is None:
            await interaction.response.send_message(
                "⚠️ 시각은 `00:00` · `7:30` · `7시` 처럼 적어 주세요.", ephemeral=True)
            return
        if s == e:
            await interaction.response.send_message(
                "⚠️ 잠그는 시각과 여는 시각이 같습니다.", ephemeral=True)
            return
        await interaction.response.defer()
        await save_lock_settings(interaction.guild.id, {"start": fmt_hhmm(s), "end": fmt_hhmm(e)})
        await self.cog.refresh_panel(interaction)


class AddChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, cog):
        super().__init__(placeholder="자동으로 잠글 채널 추가 (여러 개 가능)",
                         channel_types=list(LOCKABLE_TYPES), min_values=1, max_values=10, row=0)
        self.cog = cog

    async def callback(self, interaction: discord.Interaction):
        if not await _require_admin(interaction):
            return
        await interaction.response.defer()
        doc = await get_lock_doc(interaction.guild.id)
        ids = list(doc["channel_ids"])
        for ch in self.values:
            if ch.id not in ids and len(ids) < MAX_LOCK_CHANNELS:
                ids.append(ch.id)
        await save_lock_settings(interaction.guild.id, {"channel_ids": ids})
        await self.cog.refresh_panel(interaction)


class RemoveChannelSelect(discord.ui.Select):
    def __init__(self, cog, guild, doc: dict):
        opts = []
        for cid in (doc.get("channel_ids") or [])[:25]:
            ch = guild.get_channel(cid)
            opts.append(discord.SelectOption(label=(f"#{ch.name}" if ch else str(cid))[:100],
                                             value=str(cid)))
        super().__init__(placeholder="자동 잠금에서 뺄 채널", row=1,
                         options=opts or [discord.SelectOption(label="(없음)", value="none")],
                         min_values=1, max_values=max(1, len(opts)), disabled=not opts)
        self.cog = cog

    async def callback(self, interaction: discord.Interaction):
        if not await _require_admin(interaction):
            return
        await interaction.response.defer()
        drop = {int(v) for v in self.values if v != "none"}
        doc = await get_lock_doc(interaction.guild.id)
        await save_lock_settings(interaction.guild.id,
                                 {"channel_ids": [c for c in doc["channel_ids"] if c not in drop]})
        await self.cog.refresh_panel(interaction)


class LockPanelView(discord.ui.View):
    def __init__(self, cog, guild, doc: dict):
        super().__init__(timeout=600)
        self.cog = cog
        self.doc = doc
        self.add_item(AddChannelSelect(cog))
        self.add_item(RemoveChannelSelect(cog, guild, doc))
        self.toggle.label = "자동 잠금 끄기" if doc.get("enabled") else "자동 잠금 켜기"
        self.toggle.style = (discord.ButtonStyle.danger if doc.get("enabled")
                             else discord.ButtonStyle.success)
        self.announce.label = "안내 끄기" if doc.get("announce") else "안내 켜기"

    @discord.ui.button(label="켜기", emoji="⏻", row=2)
    async def toggle(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await _require_admin(interaction):
            return
        await interaction.response.defer()
        doc = await get_lock_doc(interaction.guild.id)
        await save_lock_settings(interaction.guild.id, {"enabled": not doc["enabled"]})
        await self.cog.refresh_panel(interaction)

    @discord.ui.button(label="시간 바꾸기", emoji="🕛", style=discord.ButtonStyle.secondary, row=2)
    async def time(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await _require_admin(interaction):
            return
        # 모달은 첫 응답이어야 해서 defer 할 수 없습니다 — DB 를 읽지 않고
        # 이 패널을 그릴 때 받은 값을 씁니다(3초 제한).
        await interaction.response.send_modal(TimeModal(self.cog, self.doc))

    @discord.ui.button(label="안내", emoji="📢", style=discord.ButtonStyle.secondary, row=2)
    async def announce(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await _require_admin(interaction):
            return
        await interaction.response.defer()
        doc = await get_lock_doc(interaction.guild.id)
        await save_lock_settings(interaction.guild.id, {"announce": not doc["announce"]})
        await self.cog.refresh_panel(interaction)


# ── 본체 ───────────────────────────────────────────────────────────────

class ChannelLock(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._locks: dict[int, asyncio.Lock] = {}
        self._last_err: dict[tuple, float] = {}
        self.tick.start()

    def cog_unload(self):
        self.tick.cancel()

    def _guild_lock(self, guild_id: int) -> asyncio.Lock:
        return self._locks.setdefault(guild_id, asyncio.Lock())

    def _log(self, key: tuple, msg: str) -> None:
        """같은 실패를 30초마다 찍으면 로그가 묻힙니다 — 한 시간에 한 번."""
        now = datetime.datetime.now().timestamp()
        if now - self._last_err.get(key, 0) >= 3600:
            self._last_err[key] = now
            print(msg, flush=True)

    # ── 맞추기 ──
    async def reconcile(self, guild, now: datetime.datetime | None = None) -> dict:
        """실제 채널을 '있어야 할 상태' 에 맞춥니다. 결과: {채널: 'locked'|'unlocked'|오류}."""
        if guild is None or getattr(guild, "unavailable", False):
            return {}
        now = now or datetime.datetime.now(KST)
        results = {}
        async with self._guild_lock(guild.id):
            doc = await get_lock_doc(guild.id)
            # 지난 기록 정리 — 끝난 수동 잠금, 지난 '오늘 밤은 열어 둠'
            ts = now.timestamp()
            for cid, m in list((doc.get("manual") or {}).items()):
                if (m or {}).get("until") is not None and ts >= float(m["until"]):
                    await set_lock_state(guild.id, "manual", int(cid), None)
                    doc["manual"].pop(cid, None)
            for cid, until in list((doc.get("open_until") or {}).items()):
                if ts >= float(until or 0):
                    await set_lock_state(guild.id, "open_until", int(cid), None)
                    doc["open_until"].pop(cid, None)

            want = desired_locks(doc, now)
            locked = doc.get("locked") or {}
            for cid in set(want) | {int(c) for c in locked}:
                ch = guild.get_channel(cid)
                if ch is None:
                    try:
                        ch = await guild.fetch_channel(cid)
                    except discord.NotFound:
                        # 채널이 지워졌습니다 — 되돌릴 곳이 없으니 기록만 지웁니다
                        await set_lock_state(guild.id, "locked", cid, None)
                        await set_lock_state(guild.id, "manual", cid, None)
                        continue
                    except discord.HTTPException as e:
                        # 잠깐 못 찾은 것일 수 있습니다. 여기서 기록을 지우면
                        # 그 채널은 **영영 잠긴 채** 남습니다.
                        self._log(("fetch", cid), f"[ChannelLock] 채널 {cid} 조회 실패: {e}")
                        continue
                try:
                    if cid in want:
                        results[cid] = await self._ensure_locked(ch, doc, want[cid])
                    else:
                        results[cid] = await self._ensure_unlocked(ch, locked[str(cid)])
                except discord.Forbidden:
                    results[cid] = "권한 없음"
                    self._log(("perm", cid), f"[ChannelLock] #{ch.name}: 권한이 없어 "
                              f"{'잠그지' if cid in want else '풀지'} 못했습니다 — "
                              "봇의 '권한 관리' 권한을 확인하세요")
                except discord.HTTPException as e:
                    results[cid] = f"오류 {e.status}"
                    self._log(("http", cid), f"[ChannelLock] #{ch.name}: {e} (다음 주기에 다시 시도)")
        return results

    async def _ensure_locked(self, ch, doc: dict, reason: str) -> str:
        state = (doc.get("locked") or {}).get(str(ch.id))
        if state is None:
            # 원래 권한을 **바꾸기 전에** 저장합니다 (mod_db 설명 참고)
            state = {"saved": plan_lock(ch),
                     "at": datetime.datetime.now(KST).strftime("%Y-%m-%d %H:%M"),
                     "notice_id": None}
            await set_lock_state(ch.guild.id, "locked", ch.id, state)
            doc.setdefault("locked", {})[str(ch.id)] = state
        new = locked_overwrites(ch, state["saved"])
        if not overwrites_equal(ch.overwrites, new):
            await ch.edit(overwrites=new, reason=f"채널 잠금 ({'수동' if reason == 'manual' else '자동'})")
        if doc.get("announce") and state.get("notice_id") is None:
            notice_id = -1       # 실패해도 30초마다 다시 올리지 않습니다
            try:
                msg = await ch.send(embed=lock_notice(reason, doc, ch.id),
                                    allowed_mentions=discord.AllowedMentions.none())
                notice_id = msg.id
            except discord.HTTPException as e:
                self._log(("notice", ch.id), f"[ChannelLock] #{ch.name}: 잠금 안내 실패 - {e}")
            state["notice_id"] = notice_id
            await set_lock_state(ch.guild.id, "locked", ch.id, state)
        return "locked"

    async def _ensure_unlocked(self, ch, state: dict) -> str:
        new = restored_overwrites(ch, state.get("saved") or {})
        if not overwrites_equal(ch.overwrites, new):
            await ch.edit(overwrites=new, reason="채널 잠금 해제")
        # 되돌린 **뒤에** 기록을 지웁니다 — 순서가 바뀌면 실패 시 풀 방법이 사라집니다
        await set_lock_state(ch.guild.id, "locked", ch.id, None)
        nid = state.get("notice_id")
        if nid and nid > 0:
            try:
                await ch.get_partial_message(nid).delete()
            except discord.NotFound:
                pass                # 누가 이미 지웠습니다 — 지우려던 것과 같은 결과
            except discord.HTTPException as e:
                self._log(("unnotice", ch.id), f"[ChannelLock] #{ch.name}: 안내 삭제 실패 - {e}")
        return "unlocked"

    @tasks.loop(seconds=TICK_SECONDS)
    async def tick(self):
        await self.bot.wait_until_ready()
        for guild in self.bot.guilds:
            try:
                await self.reconcile(guild)
            except Exception as e:
                print(f"[ChannelLock] {guild.name}: 맞추기 실패 - {type(e).__name__}: {e}", flush=True)

    # 주기 작업은 예외 한 번에 영구히 멈춥니다. 멈추면 **아침에 안 풀립니다.**
    @tick.error
    async def tick_error(self, error):
        await health.revive_loop(self.tick, "channel_lock", error)

    async def refresh_panel(self, interaction: discord.Interaction):
        await self.reconcile(interaction.guild)
        doc = await get_lock_doc(interaction.guild.id)
        await interaction.edit_original_response(
            embed=build_panel(interaction.guild, doc),
            view=LockPanelView(self, interaction.guild, doc))

    # ── 명령어 ──
    @app_commands.command(name="잠금", description="채널을 잠가 관리자만 채팅하게 합니다 (기간을 적으면 저절로 풀림).")
    @app_commands.describe(채널="잠글 채널 (비우면 이 채널)", 기간="예: 30분 · 2시간 · 1시간30분 · 1d (비우면 풀 때까지)",
                           사유="채널에 함께 올릴 사유")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_channels=True)
    @app_commands.checks.has_permissions(manage_channels=True)
    async def lock(self, interaction: discord.Interaction,
                   채널: discord.TextChannel | None = None,
                   기간: str | None = None, 사유: str | None = None):
        ch = 채널 or interaction.channel
        if not isinstance(ch, discord.TextChannel):
            await interaction.response.send_message(
                "⚠️ 텍스트 채널만 잠글 수 있습니다. 스레드라면 부모 채널을 잠그세요.", ephemeral=True)
            return
        minutes = None
        if 기간:
            minutes = parse_duration(기간)
            if minutes is None:
                await interaction.response.send_message(
                    "⚠️ 기간은 `30분` · `2시간` · `1시간30분` · `1d` 처럼 적어 주세요 (최대 30일).",
                    ephemeral=True)
                return
        why = bot_problem(ch)
        if why:
            await interaction.response.send_message(f"⚠️ {ch.mention} 을(를) 잠글 수 없습니다: {why}",
                                                    ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        now = datetime.datetime.now(KST)
        until = (now + datetime.timedelta(minutes=minutes)).timestamp() if minutes else None
        async with self._guild_lock(interaction.guild.id):
            await set_lock_state(interaction.guild.id, "manual", ch.id,
                                 {"until": until, "by": interaction.user.id,
                                  "reason": (사유 or "")[:500]})
            # 자동 잠금 중 풀어 둔 채널을 다시 잠그는 것이니 그 표시는 지웁니다
            await set_lock_state(interaction.guild.id, "open_until", ch.id, None)
        res = (await self.reconcile(interaction.guild)).get(ch.id)
        if res != "locked":
            await interaction.followup.send(f"⚠️ 잠그지 못했습니다: {res}", ephemeral=True)
            return
        await interaction.followup.send(embed=discord.Embed(
            title="🔒 잠갔습니다",
            description=(f"{ch.mention} — 관리자만 채팅할 수 있습니다.\n"
                         + (f"<t:{int(until)}:f> (**{fmt_duration(minutes)}** 뒤)에 저절로 풀립니다."
                            if until else "`/잠금해제` 할 때까지 잠겨 있습니다.")),
            color=discord.Color.dark_grey()), ephemeral=True)

    @app_commands.command(name="잠금해제", description="잠긴 채널을 지금 풉니다.")
    @app_commands.describe(채널="풀 채널 (비우면 이 채널)")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_channels=True)
    @app_commands.checks.has_permissions(manage_channels=True)
    async def unlock(self, interaction: discord.Interaction, 채널: discord.TextChannel | None = None):
        ch = 채널 or interaction.channel
        await interaction.response.defer(ephemeral=True, thinking=True)
        now = datetime.datetime.now(KST)
        async with self._guild_lock(interaction.guild.id):
            doc = await get_lock_doc(interaction.guild.id)
            if ch.id not in desired_locks(doc, now) and str(ch.id) not in doc["locked"]:
                await interaction.followup.send(f"ℹ️ {ch.mention} 은(는) 잠겨 있지 않습니다.", ephemeral=True)
                return
            await set_lock_state(interaction.guild.id, "manual", ch.id, None)
            doc["manual"].pop(str(ch.id), None)
            note = ""
            if desired_locks(doc, now).get(ch.id) == "schedule":
                # 자동 잠금 시간 중입니다 — 풀지 않으면 30초 뒤 다시 잠깁니다.
                # 이번 잠금 시간이 끝날 때까지만 열어 둡니다(내일 밤은 다시 잠급니다).
                end = window_end_after(now, parse_hhmm(doc["end"]))
                await set_lock_state(interaction.guild.id, "open_until", ch.id, end.timestamp())
                note = f"\n-# 자동 잠금 시간이지만 오늘은 열어 둡니다. 다음 자동 잠금은 평소대로 됩니다."
        res = (await self.reconcile(interaction.guild)).get(ch.id)
        if res not in ("unlocked", None):
            await interaction.followup.send(f"⚠️ 풀지 못했습니다: {res} (30초마다 다시 시도합니다)",
                                            ephemeral=True)
            return
        await interaction.followup.send(embed=discord.Embed(
            title="🔓 풀었습니다", description=f"{ch.mention} — 다시 채팅할 수 있습니다.{note}",
            color=discord.Color.green()), ephemeral=True)

    @app_commands.command(name="채널잠금설정", description="[관리자 전용] 매일 정한 시간(기본 00:00~07:00)에 채널을 자동으로 잠급니다.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def lock_settings(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        doc = await get_lock_doc(interaction.guild.id)
        await interaction.followup.send(embed=build_panel(interaction.guild, doc),
                                        view=LockPanelView(self, interaction.guild, doc),
                                        ephemeral=True)


async def setup(bot):
    await bot.add_cog(ChannelLock(bot))
