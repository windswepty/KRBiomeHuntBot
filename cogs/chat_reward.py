import re
import unicodedata
from datetime import datetime, timezone, timedelta
import discord
from discord.ext import commands, tasks
from discord import app_commands
from utils import health
from utils.database import (
    process_chat_message, is_user_blacklisted,
    set_channel_boost, get_channel_boost, get_all_active_boosts, add_activity_log,
    check_and_clear_expired_boosts,
    bump_quest_progress, get_quest_settings, bump_server_goal,
    quest_channels, channel_field, bump_event_progress,
    quest_goal_period, BOOST_CHAT, BOOST_SEASON, boost_kind,
)
from utils.event_db import get_season, season_state


GIF_LINK_RE = re.compile(r'https?://\S*(?:tenor\.com|giphy\.com|gfycat\.com|\.gif|\.gifv)',
                         re.IGNORECASE)
URL_RE = re.compile(r'https?://\S+')
CUSTOM_EMOJI_RE = re.compile(r'<a?:\w+:\d+>')
KEYCAP_RE = re.compile('[0-9#*]\ufe0f?\u20e3')     # 1️⃣ — 숫자가 남아 글자로 보이지 않게
# 문장부호로 분류되는 이모지(‼ ⁉ 〰 〽)와, 뒤에 이모지 변형 선택자(U+FE0F)가 붙은 글자
EMOJI_PUNCT_RE = re.compile('\\S\ufe0f|[\u203c\u2049\u3030\u303d]')


def counts_as_chat(content: str) -> bool:
    """채팅 포인트·퀘스트에 세는 메시지인가.

    URL · 커스텀 이모지 · 유니코드 이모지를 지우고 **남는 것이 있어야** 셉니다.
    이모지·스티커만 보내 포인트를 버는 것을 막습니다(2026-10 제보).

    유니코드 이모지는 범위를 적지 않고 **문자 분류**로 지웁니다. 예전 범위식은
    `⬆` `‼` 같은 BMP 이모지를 놓쳤고, `\\200d` 가 8진수로 읽혀 영문 `d` 까지
    지웠습니다. 기호(S*) · 결합 표시(M*, 이모지 변형 선택자) · 서식(Cf, ZWJ)이
    지울 대상이고, 글자 · 숫자 · 문장부호는 남습니다.
    """
    text = (content or "").strip()
    if not text or GIF_LINK_RE.search(text):
        return False
    text = KEYCAP_RE.sub("", CUSTOM_EMOJI_RE.sub("", URL_RE.sub("", text)))
    text = EMOJI_PUNCT_RE.sub("", text)
    return any(not ch.isspace() and unicodedata.category(ch)[0] not in "SMC"
               for ch in text)


def parse_duration_to_seconds(duration_str: str) -> tuple[int, str]:
    if not duration_str:
        return 0, "**무제한(직접 해제 전까지)**"
    s = str(duration_str).strip().lower()
    if s in ["0", "0초", "0분", "0시간", "0일", "무제한", "none"]:
        return 0, "**무제한(직접 해제 전까지)**"

    # 1. 초 단위 (s / 초)
    m_sec = re.match(r'^(\d+)(?:s|초|초동안)$', s)
    if m_sec:
        secs = int(m_sec.group(1))
        return secs, f"**{secs}초간**"

    # 2. 분 단위 (m / 분)
    m_min = re.match(r'^(\d+)(?:m|분|분동안)$', s)
    if m_min:
        mins = int(m_min.group(1))
        return mins * 60, f"**{mins}분간**"

    # 3. 시간 단위 (h / 시간)
    m_hour = re.match(r'^(\d+)(?:h|시간|시간동안)$', s)
    if m_hour:
        hours = int(m_hour.group(1))
        return hours * 3600, f"**{hours}시간 동안**"

    # 4. 일 단위 (d / 일)
    m_day = re.match(r'^(\d+)(?:d|일)$', s)
    if m_day:
        days = int(m_day.group(1))
        return days * 86400, f"**{days}일간**"

    # 5. 순수 숫자인 경우
    if s.isdigit():
        val = int(s)
        if val < 60:
            return val, f"**{val}초간**"
        else:
            mins = val // 60
            return val, f"**{val}초간 ({mins}분)**"

    return 0, "**무제한(직접 해제 전까지)**"


# ── Modals & Select Views for Channel Boost ───────────────────────────
#
# /채널부스트 와 /시즌부스트 가 **같은 화면**을 씁니다(kind 만 다름). 두 벌로 두면
# 한쪽만 고쳐지고, 저장 · 만료 · 목록이 어긋납니다.
#   chat   — 채팅 적립 전체(기본 · 시즌)에 곱함
#   season — 시즌 포인트에만 곱함 (시즌 기간에만 효과)
BOOST_TEXT = {
    BOOST_CHAT: {"icon": "⚡", "name": "채널 부스트", "what": "포인트", "unit": "P"},
    BOOST_SEASON: {"icon": "🏆", "name": "시즌 부스트", "what": "시즌 포인트", "unit": " 시즌 포인트"},
}


class SetChannelBoostModal(discord.ui.Modal):
    def __init__(self, channel_id: int, channel_name: str, kind: str = BOOST_CHAT):
        t = BOOST_TEXT[kind]
        title_str = f"{t['icon']} #{channel_name[:20]} {t['name']} 설정"
        super().__init__(title=title_str[:45])
        self.channel_id = channel_id
        self.channel_name = channel_name
        self.kind = kind

        self.multiplier_input = discord.ui.TextInput(
            label=f"{t['what']} 배율 (기본 1.0)",
            placeholder="예: 2 (2배 적립) 또는 3.5 (3.5배 적립)",
            default="2",
            required=True,
            max_length=10
        )
        self.duration_input = discord.ui.TextInput(
            label="유지 시간 (최소 10초 / 0은 무제한)",
            placeholder="예: 10s, 30m, 1h, 2d (또는 30분, 1시간, 2일)",
            default="30m",
            required=True,
            max_length=20
        )
        self.add_item(self.multiplier_input)
        self.add_item(self.duration_input)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            try:
                mult = float(self.multiplier_input.value.strip())
                if mult < 1.0:
                    raise ValueError()
            except ValueError:
                await interaction.response.send_message("⚠️ 배율은 1.0 이상의 숫자를 입력해 주세요. (예: 2 또는 3.5)", ephemeral=True)
                return

            duration_str = self.duration_input.value.strip()
            duration_seconds, time_str = parse_duration_to_seconds(duration_str)

            if 0 < duration_seconds < 10:
                await interaction.response.send_message("⚠️ 유지 시간의 최소 설정값은 **10초(10s)**입니다. (0 지정 시 무제한)", ephemeral=True)
                return

            channel = interaction.guild.get_channel(self.channel_id)
            channel_mention = channel.mention if channel else f"<#{self.channel_id}>"

            t = BOOST_TEXT[self.kind]
            await set_channel_boost(interaction.guild.id, self.channel_id, mult, duration_seconds,
                                    kind=self.kind)

            if mult == 1.0:
                await add_activity_log(
                    interaction.guild.id, "admin", interaction.user.id, interaction.user.display_name,
                    f"[{self.channel_name}] {t['name']} 해제"
                )
                embed = discord.Embed(
                    title=f"{t['icon']} {t['name']} 해제 완료",
                    description=f"{channel_mention} 채널의 {t['name']}가 해제(기본 10{t['unit']} 적립)되었습니다.",
                    color=discord.Color.orange()
                )
            else:
                await add_activity_log(
                    interaction.guild.id, "admin", interaction.user.id, interaction.user.display_name,
                    f"[{self.channel_name}] {mult:g}배 {t['name']} 설정 ({time_str})"
                )
                earned_pts = int(round(10 * mult))
                ts_str = ""
                if duration_seconds > 0:
                    exp_ts = int((datetime.now(timezone.utc) + timedelta(seconds=duration_seconds)).timestamp())
                    ts_str = f"\n⏱️ **종료 시각**: <t:{exp_ts}:R> (<t:{exp_ts}:t> 종료)"

                note = ""
                if self.kind == BOOST_SEASON and season_state(
                        await get_season(interaction.guild.id)) != "active":
                    # 시즌 밖에서 켜면 아무 효과가 없습니다 — 조용히 두면 '안 된다' 는 문의가 옵니다
                    note = ("\n\n⚠️ **지금은 시즌 기간이 아닙니다.** 시즌 포인트가 쌓이지 않으므로 "
                            "시즌이 시작돼야 효과가 납니다.")
                embed = discord.Embed(
                    title=f"{t['icon']} 게릴라 {t['name']} 설정 완료!",
                    description=f"🎉 {channel_mention} 채널에 **{mult:g}배** {t['what']} 부스트가 설정되었습니다!\n\n"
                                f"⏱️ **유지 시간**: {time_str}{ts_str}\n"
                                f"💬 채팅 10회 작성 시마다 **+{earned_pts}{t['unit']}**가 자동 적립됩니다.{note}",
                    color=discord.Color.green()
                )
                if channel:
                    try:
                        announce_embed = discord.Embed(
                            title=f"{t['icon']} [게릴라 이벤트] {t['what']} 부스트 시작!",
                            description=f"🔥 이 채널에 **{mult:g}배** {t['what']} 부스트 이벤트가 시작되었습니다!\n"
                                        f"💬 채팅 10회 작성 시마다 **+{earned_pts}{t['unit']}**가 자동 적립됩니다!\n"
                                        f"⏱️ **이벤트 기간**: {time_str}{ts_str}",
                            color=discord.Color.gold()
                        )
                        await channel.send(embed=announce_embed)
                    except Exception:
                        pass

            await interaction.response.send_message(embed=embed, ephemeral=True)
        except Exception as e:
            print(f"SetChannelBoostModal on_submit error: {e}", flush=True)
            await interaction.response.send_message(f"⚠️ 부스트 설정 처리 중 오류가 발생했습니다: `{e}`", ephemeral=True)


class SelectChannelForBoostSelect(discord.ui.ChannelSelect):
    def __init__(self, kind: str = BOOST_CHAT):
        self.kind = kind
        super().__init__(
            placeholder=f"{BOOST_TEXT[kind]['name']}를 설정할 채널을 선택하세요...",
            channel_types=[discord.ChannelType.text]
        )

    async def callback(self, interaction: discord.Interaction):
        selected_channel = self.values[0]
        modal = SetChannelBoostModal(
            channel_id=selected_channel.id,
            channel_name=selected_channel.name,
            kind=self.kind
        )
        await interaction.response.send_modal(modal)


class SelectChannelForBoostView(discord.ui.View):
    def __init__(self, kind: str = BOOST_CHAT):
        super().__init__(timeout=180)
        self.add_item(SelectChannelForBoostSelect(kind))


class RemoveChannelBoostSelect(discord.ui.Select):
    def __init__(self, active_boosts: list[dict], guild: discord.Guild, kind: str = BOOST_CHAT):
        self.kind = kind
        options = []
        for b in active_boosts:
            cid = int(b["channel_id"])
            mult = float(b.get("multiplier", 1.0))
            channel = guild.get_channel(cid)
            ch_name = channel.name if channel else f"채널 ID {cid}"
            options.append(
                discord.SelectOption(
                    label=f"#{ch_name} [{mult:g}배 부스트 중]",
                    value=str(cid),
                    description=f"선택 시 즉시 부스트 이벤트 해제 (기본 10{BOOST_TEXT[kind]['unit']} 복구)"[:100],
                    emoji="❌"
                )
            )
        super().__init__(placeholder="부스트를 해제할 채널을 선택하세요...", options=options)

    async def callback(self, interaction: discord.Interaction):
        cid = int(self.values[0])
        channel = interaction.guild.get_channel(cid)
        ch_mention = channel.mention if channel else f"<#{cid}>"
        ch_name = channel.name if channel else str(cid)

        t = BOOST_TEXT[self.kind]
        await set_channel_boost(interaction.guild.id, cid, 1.0, 0, kind=self.kind)
        await add_activity_log(
            interaction.guild.id, "admin", interaction.user.id, interaction.user.display_name,
            f"[{ch_name}] {t['name']} 해제"
        )
        embed = discord.Embed(
            title=f"❌ {t['name']} 해제 완료",
            description=f"{ch_mention} 채널의 {t['name']}가 성공적으로 해제(기본 10{t['unit']} 복구)되었습니다.",
            color=discord.Color.orange()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


class RemoveChannelBoostView(discord.ui.View):
    def __init__(self, active_boosts: list[dict], guild: discord.Guild, kind: str = BOOST_CHAT):
        super().__init__(timeout=180)
        self.add_item(RemoveChannelBoostSelect(active_boosts, guild, kind))


class ChannelBoostSettingsSelect(discord.ui.Select):
    def __init__(self, active_count: int, kind: str = BOOST_CHAT):
        self.kind = kind
        name = BOOST_TEXT[kind]["name"]
        options = [
            discord.SelectOption(
                label=f"{name} 설정 / 이벤트 시작",
                value="set",
                description="채널을 고르고 배율 및 기간을 입력하여 이벤트를 개최합니다.",
                emoji="⚡"
            ),
            discord.SelectOption(
                label=f"{name} 해제",
                value="remove",
                description=f"현재 진행 중인 {active_count}개 부스트 중 선택 해제",
                emoji="❌"
            ),
            discord.SelectOption(
                label="진행 중인 부스트 목록 확인",
                value="list",
                description="부스트 적용 채널, 배율 및 남은 시간 확인",
                emoji="📊"
            ),
        ]
        super().__init__(placeholder="부스트 관리 기능을 선택하세요...", options=options)

    async def callback(self, interaction: discord.Interaction):
        choice = self.values[0]

        if choice == "set":
            await interaction.response.send_message(
                f"{BOOST_TEXT[self.kind]['icon']} **부스트를 적용할 채널을 아래에서 선택하세요.**",
                view=SelectChannelForBoostView(self.kind),
                ephemeral=True
            )

        elif choice == "remove":
            active_boosts = await get_all_active_boosts(interaction.guild.id, kind=self.kind)
            if not active_boosts:
                await interaction.response.send_message("⚠️ 현재 해제할 부스트 채널이 없습니다.", ephemeral=True)
                return
            await interaction.response.send_message(
                "❌ **부스트를 해제할 채널을 선택하세요.**",
                view=RemoveChannelBoostView(active_boosts, interaction.guild, self.kind),
                ephemeral=True
            )

        elif choice == "list":
            active_boosts = await get_all_active_boosts(interaction.guild.id, kind=self.kind)
            if not active_boosts:
                embed = discord.Embed(
                    title=f"{BOOST_TEXT[self.kind]['icon']} 진행 중인 {BOOST_TEXT[self.kind]['name']} 현황",
                    description="현재 부스트 이벤트가 적용 중인 채널이 없습니다.",
                    color=discord.Color.gold()
                )
                await interaction.response.send_message(embed=embed, ephemeral=True)
                return

            lines = []
            now = datetime.now(timezone.utc)
            for b in active_boosts:
                cid = int(b["channel_id"])
                mult = float(b.get("multiplier", 1.0))
                channel = interaction.guild.get_channel(cid)
                ch_str = channel.mention if channel else f"채널 ID `{cid}`"

                exp_str = b.get("expires_at")
                if exp_str:
                    try:
                        exp_time = datetime.fromisoformat(exp_str)
                        ts = int(exp_time.timestamp())
                        if now > exp_time:
                            time_info = "⏱️ 만료됨 (자동 복구됨)"
                        else:
                            time_info = f"⏱️ 남은 시간: <t:{ts}:R> (<t:{ts}:t> 종료)"
                    except Exception:
                        time_info = "⏱️ 시간 정보 오류"
                else:
                    time_info = "⏱️ 무제한 (지속 적용 중)"

                lines.append(f"• {ch_str} — 🔥 **{mult:g}배** ({time_info})")

            embed = discord.Embed(
                title=f"{BOOST_TEXT[self.kind]['icon']} 진행 중인 게릴라 {BOOST_TEXT[self.kind]['name']} 목록",
                description="\n\n".join(lines),
                color=discord.Color.gold()
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)


class ChannelBoostSettingsView(discord.ui.View):
    def __init__(self, active_count: int, kind: str = BOOST_CHAT):
        super().__init__(timeout=180)
        self.add_item(ChannelBoostSettingsSelect(active_count, kind))


# ── Cog ───────────────────────────────────────────────────────────────

class ChatReward(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.check_boost_expiration.start()

    def cog_unload(self):
        self.check_boost_expiration.cancel()

    @tasks.loop(seconds=30)
    async def check_boost_expiration(self):
        await self.bot.wait_until_ready()
        try:
            expired_boosts = await check_and_clear_expired_boosts()
            for b in expired_boosts:
                cid = int(b["channel_id"])
                guild_id = int(b["guild_id"])
                guild = self.bot.get_guild(guild_id)
                if guild:
                    channel = guild.get_channel(cid)
                    if channel:
                        try:
                            t = BOOST_TEXT.get(boost_kind(b), BOOST_TEXT[BOOST_CHAT])
                            embed = discord.Embed(
                                title=f"{t['icon']} [게릴라 이벤트] {t['what']} 부스트 이벤트 종료!",
                                description=f"🎉 준비된 게릴라 {t['what']} 부스트 이벤트 시간이 완료되어 종료되었습니다!\n\n"
                                            f"💬 이제 채팅 10회당 기본 적립 보상(**+10{t['unit']}**)으로 정상 복구됩니다.",
                                color=discord.Color.orange()
                            )
                            await channel.send(embed=embed)
                        except Exception:
                            pass
        except Exception as e:
            print(f"check_boost_expiration loop error: {e}", flush=True)

    # 주기 작업은 예상 못한 예외가 **한 번** 나면 그대로 영구히 멈춥니다.
    # 재시도도 알림도 없고, 프로세스는 멀쩡해 호스팅도 감지하지 못합니다.
    # 무인 운영에서는 몇 달 뒤에야 발견되므로 스스로 되살립니다.
    @check_boost_expiration.error
    async def check_boost_expiration_error(self, error):
        await health.revive_loop(self.check_boost_expiration,
                                 "check_boost_expiration", error)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        # Ignore bot messages and direct messages
        if message.author.bot or not message.guild:
            return

        # 1. Exclude photos/files/GIF attachments
        if message.attachments:
            return

        # 스티커는 글자로 치지 않습니다 — 스티커만 보낸 메시지는 content 가
        # 비어 있어 아래 검사에서 빠집니다. 글자와 함께 보냈으면 그 글자로 셉니다.
        if not counts_as_chat(message.content):
            return

        # 블랙리스트 검사는 **가장 마지막**에 합니다.
        # 이 함수는 서버의 모든 메시지마다 호출되는데, 위 필터들은 순수 메모리
        # 연산이고 이 검사만 저장소를 봅니다. 순서가 반대면 스티커 하나에도
        # 조회가 일어나 대부분이 낭비됩니다.
        if await is_user_blacklisted(message.guild.id, message.author.id):
            return

        # Process message for chat points (with channel boost support)
        await process_chat_message(
            message.guild.id, message.author.id, message.channel.id
        )

        # 퀘스트 진행도. 포인트 지급과 **같은 필터를 통과한 메시지만** 셉니다.
        # 기준이 다르면 "채팅했는데 퀘스트가 안 오른다"는 혼란이 생깁니다.
        # 실패해도 채팅 적립을 막지 않도록 통째로 감쌉니다.
        try:
            # 켜져 있을 때만 씁니다. 꺼져 있는데도 진행도를 쌓으면 모든 메시지가
            # 쓸모없는 DB 쓰기가 됩니다. (설정 조회는 캐시라 매번 DB 를 보지 않습니다)
            settings = await get_quest_settings(message.guild.id)
            if settings.get("enabled"):
                await bump_quest_progress(message.guild.id, message.author.id, "chat")
                await bump_quest_progress(message.guild.id, message.author.id,
                                          "wchat", weekly=True)
                # 사람마다 배정된 채널이 다르므로 **채널별로** 셉니다.
                # 후보 채널에서 한 말이면 그 채널 카운터를 올려 두고,
                # 어떤 퀘스트를 받았는지는 /퀘스트 를 열 때 대조합니다.
                if str(message.channel.id) in quest_channels(settings):
                    field = channel_field(message.channel.id)
                    await bump_quest_progress(message.guild.id, message.author.id, field)
                    await bump_quest_progress(
                        message.guild.id, message.author.id,
                        channel_field(message.channel.id, weekly=True), weekly=True)
                if settings.get("goal_enabled"):
                    rd = int(settings.get("goal_reset_days", 1) or 1)
                    gperiod = quest_goal_period(rd)
                    new_prog = await bump_server_goal(
                        message.guild.id,
                        reset_days=rd
                    )
                    quest_cog = self.bot.get_cog("Quest")
                    if quest_cog:
                        quest_cog.notify_goal_changed(message.guild.id)
                    target = int(settings.get("goal_target", 0) or 0)
                    # 공동 목표 주기 내 본인 총 채팅 수
                    await bump_quest_progress(
                        message.guild.id, message.author.id,
                        "goal_chat_total", period=gperiod)
                    # 목표 달성 전(또는 달성되는 순간까지)의 기여 채팅 수
                    if target > 0 and (new_prog - 1) < target:
                        await bump_quest_progress(
                            message.guild.id, message.author.id,
                            "goal_chat_early", period=gperiod)
                await bump_event_progress(message.guild.id, message.author.id, "chat")
        except Exception as e:
            print(f"[Quest] 채팅 진행도 기록 실패: {type(e).__name__}: {e}", flush=True)

    @app_commands.command(name="채널부스트", description="[관리자 전용] 게릴라 채널 부스트 설정, 배율, 기간 및 해제를 관리합니다.")
    # DM 에서는 interaction.guild 가 None 이라 AttributeError 로 죽습니다.
    # (실제로 /출석 이 그렇게 터졌습니다). guild_only 를 걸면 디스코드가
    # **DM 목록에 아예 띄우지 않으므로** 방어 코드를 흩뿌릴 필요가 없습니다.
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def channel_boost_settings(self, interaction: discord.Interaction):
        active_boosts = await get_all_active_boosts(interaction.guild.id)
        active_count = len(active_boosts)

        embed = discord.Embed(
            title="⚡ 게릴라 채널 부스트 이벤트 관리",
            description=f"🔥 **현재 활성화된 부스트 채널**: **{active_count}개**\n\n"
                        "아래 드롭다운 메뉴에서 원하시는 관리 기능을 선택해 주세요.",
            color=discord.Color.gold()
        )
        view = ChannelBoostSettingsView(active_count)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @app_commands.command(name="시즌부스트", description="[관리자 전용] 채널의 시즌 포인트 적립 배율 · 기간 · 해제를 관리합니다.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def season_boost_settings(self, interaction: discord.Interaction):
        # DB 를 두 번 읽으므로 먼저 응답을 예약합니다(3초 제한)
        await interaction.response.defer(ephemeral=True, thinking=True)
        active_boosts = await get_all_active_boosts(interaction.guild.id, kind=BOOST_SEASON)
        active = season_state(await get_season(interaction.guild.id)) == "active"
        embed = discord.Embed(
            title="🏆 게릴라 시즌 부스트 관리",
            description=(f"🔥 **현재 시즌 부스트 채널**: **{len(active_boosts)}개**\n\n"
                         "고른 채널에서 채팅으로 쌓이는 **시즌 포인트에만** 배율을 곱합니다.\n"
                         "-# `/채널부스트` 와 같이 걸리면 두 배율이 곱해집니다.\n"
                         + ("" if active else
                            "\n⚠️ **지금은 시즌 기간이 아닙니다.** 설정은 되지만 시즌이 시작돼야 효과가 납니다.\n")
                         + "\n아래 메뉴에서 원하시는 관리 기능을 선택해 주세요."),
            color=discord.Color.gold())
        await interaction.followup.send(embed=embed,
                                        view=ChannelBoostSettingsView(len(active_boosts), BOOST_SEASON),
                                        ephemeral=True)


async def setup(bot):
    await bot.add_cog(ChatReward(bot))


