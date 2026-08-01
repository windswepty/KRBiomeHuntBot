import re
from datetime import datetime, timezone
import discord
from discord.ext import commands
from discord import app_commands
from utils.database import (
    process_chat_message, is_user_blacklisted,
    set_channel_boost, get_channel_boost, get_all_active_boosts, add_activity_log
)


def parse_duration_to_minutes(duration_str: str) -> tuple[int, str]:
    if not duration_str:
        return 0, "**무제한(직접 해제 전까지)**"
    s = str(duration_str).strip().lower()
    if s in ["0", "0분", "0시간", "무제한", "none"]:
        return 0, "**무제한(직접 해제 전까지)**"

    # 일 단위 (예: 1d, 1일, 2일)
    m_day = re.match(r'^(\d+)(?:d|일)$', s)
    if m_day:
        days = int(m_day.group(1))
        return days * 1440, f"**{days}일간**"

    # 시간 단위 (예: 2h, 2시간, 1시간)
    m_hour = re.match(r'^(\d+)(?:h|시간|시간동안)$', s)
    if m_hour:
        hours = int(m_hour.group(1))
        return hours * 60, f"**{hours}시간 동안**"

    # 분 단위 (예: 30m, 30, 30분)
    m_min = re.match(r'^(\d+)(?:m|분|분동안)?$', s)
    if m_min:
        mins = int(m_min.group(1))
        if mins == 0:
            return 0, "**무제한(직접 해제 전까지)**"
        return mins, f"**{mins}분간**"

    return 0, "**무제한(직접 해제 전까지)**"


# ── Modals & Select Views for Channel Boost ───────────────────────────

class SetChannelBoostModal(discord.ui.Modal, title="⚡ 채널 부스트 배율 및 기간 설정"):
    def __init__(self, channel_id: int, channel_name: str):
        super().__init__()
        self.channel_id = channel_id
        self.channel_name = channel_name

        self.multiplier_input = discord.ui.TextInput(
            label=f"[{channel_name[:15]}] 포인트 배율",
            placeholder="예: 2 (2배 적립) 또는 3 (3배 적립)",
            default="2",
            required=True,
            max_length=5
        )
        self.duration_input = discord.ui.TextInput(
            label="유지 시간 (0 지정 시 무제한)",
            placeholder="예: 30분, 1시간, 1일, 60 (0은 무제한)",
            default="1시간",
            required=True,
            max_length=20
        )
        self.add_item(self.multiplier_input)
        self.add_item(self.duration_input)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            mult = float(self.multiplier_input.value.strip())
            if mult < 1.0:
                raise ValueError()
        except ValueError:
            await interaction.response.send_message("⚠️ 배율은 1.0 이상의 숫자를 입력해 주세요.", ephemeral=True)
            return

        duration_str = self.duration_input.value.strip()
        duration_minutes, time_str = parse_duration_to_minutes(duration_str)

        channel = interaction.guild.get_channel(self.channel_id)
        channel_mention = channel.mention if channel else f"<#{self.channel_id}>"

        await set_channel_boost(interaction.guild.id, self.channel_id, mult, duration_minutes)

        if mult == 1.0:
            await add_activity_log(
                interaction.guild.id, "admin", interaction.user.id, interaction.user.display_name,
                f"[{self.channel_name}] 채널 부스트 해제"
            )
            embed = discord.Embed(
                title="⚡ 채널 부스트 해제 완료",
                description=f"{channel_mention} 채널의 부스트가 해제(기본 10P 적립)되었습니다.",
                color=discord.Color.orange()
            )
        else:
            await add_activity_log(
                interaction.guild.id, "admin", interaction.user.id, interaction.user.display_name,
                f"[{self.channel_name}] {mult:g}배 부스트 설정 ({time_str})"
            )
            earned_pts = int(round(10 * mult))
            embed = discord.Embed(
                title="⚡ 게릴라 채널 부스트 설정 완료!",
                description=f"🎉 {channel_mention} 채널에 **{mult:g}배** 포인트 부스트가 설정되었습니다!\n\n"
                            f"⏱️ **유지 시간**: {time_str} *(경과 시 자동 원복)*\n"
                            f"💬 채팅 10회 작성 시마다 **+{earned_pts}P**가 자동 적립됩니다.",
                color=discord.Color.green()
            )
            if channel:
                try:
                    announce_embed = discord.Embed(
                        title="⚡ [게릴라 이벤트] 포인트 부스트 시작!",
                        description=f"🔥 이 채널에 **{mult:g}배** 포인트 부스트 이벤트가 시작되었습니다!\n"
                                    f"💬 채팅 10회 작성 시마다 **+{earned_pts}P**가 자동 적립됩니다! (유지시간: {time_str})",
                        color=discord.Color.gold()
                    )
                    await channel.send(embed=announce_embed)
                except Exception:
                    pass

        await interaction.response.send_message(embed=embed, ephemeral=True)


class SelectChannelForBoostSelect(discord.ui.ChannelSelect):
    def __init__(self):
        super().__init__(
            placeholder="부스트를 설정할 채널을 선택하세요...",
            channel_types=[discord.ChannelType.text]
        )

    async def callback(self, interaction: discord.Interaction):
        selected_channel = self.values[0]
        modal = SetChannelBoostModal(
            channel_id=selected_channel.id,
            channel_name=selected_channel.name
        )
        await interaction.response.send_modal(modal)


class SelectChannelForBoostView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)
        self.add_item(SelectChannelForBoostSelect())


class RemoveChannelBoostSelect(discord.ui.Select):
    def __init__(self, active_boosts: list[dict], guild: discord.Guild):
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
                    description="선택 시 즉시 부스트 이벤트 해제 (기본 10P 복구)",
                    emoji="❌"
                )
            )
        super().__init__(placeholder="부스트를 해제할 채널을 선택하세요...", options=options)

    async def callback(self, interaction: discord.Interaction):
        cid = int(self.values[0])
        channel = interaction.guild.get_channel(cid)
        ch_mention = channel.mention if channel else f"<#{cid}>"
        ch_name = channel.name if channel else str(cid)

        await set_channel_boost(interaction.guild.id, cid, 1.0, 0)
        await add_activity_log(
            interaction.guild.id, "admin", interaction.user.id, interaction.user.display_name,
            f"[{ch_name}] 채널 부스트 해제"
        )
        embed = discord.Embed(
            title="❌ 채널 부스트 해제 완료",
            description=f"{ch_mention} 채널의 부스트가 성공적으로 해제(기본 10P 복구)되었습니다.",
            color=discord.Color.orange()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


class RemoveChannelBoostView(discord.ui.View):
    def __init__(self, active_boosts: list[dict], guild: discord.Guild):
        super().__init__(timeout=180)
        self.add_item(RemoveChannelBoostSelect(active_boosts, guild))


class ChannelBoostSettingsSelect(discord.ui.Select):
    def __init__(self, active_count: int):
        options = [
            discord.SelectOption(
                label="채널 부스트 설정 / 이벤트 시작",
                value="set",
                description="채널을 고르고 배율 및 기간을 입력하여 이벤트를 개최합니다.",
                emoji="⚡"
            ),
            discord.SelectOption(
                label="채널 부스트 해제",
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
                "⚡ **부스트를 적용할 채널을 아래에서 선택하세요.**",
                view=SelectChannelForBoostView(),
                ephemeral=True
            )

        elif choice == "remove":
            active_boosts = await get_all_active_boosts(interaction.guild.id)
            if not active_boosts:
                await interaction.response.send_message("⚠️ 현재 해제할 부스트 채널이 없습니다.", ephemeral=True)
                return
            await interaction.response.send_message(
                "❌ **부스트를 해제할 채널을 선택하세요.**",
                view=RemoveChannelBoostView(active_boosts, interaction.guild),
                ephemeral=True
            )

        elif choice == "list":
            active_boosts = await get_all_active_boosts(interaction.guild.id)
            if not active_boosts:
                embed = discord.Embed(
                    title="⚡ 진행 중인 채널 부스트 현황",
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
                        remain_sec = int((exp_time - now).total_seconds())
                        if remain_sec > 0:
                            mins, secs = divmod(remain_sec, 60)
                            time_info = f"⏱️ 남은 시간: `{mins}분 {secs}초`"
                        else:
                            time_info = "⏱️ 만료됨"
                    except Exception:
                        time_info = "⏱️ 시간 정보 오류"
                else:
                    time_info = "⏱️ 무제한 (지속 적용 중)"

                lines.append(f"• {ch_str} — 🔥 **{mult:g}배** ({time_info})")

            embed = discord.Embed(
                title="⚡ 진행 중인 게릴라 채널 부스트 목록",
                description="\n\n".join(lines),
                color=discord.Color.gold()
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)


class ChannelBoostSettingsView(discord.ui.View):
    def __init__(self, active_count: int):
        super().__init__(timeout=180)
        self.add_item(ChannelBoostSettingsSelect(active_count))


# ── Cog ───────────────────────────────────────────────────────────────

class ChatReward(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        # Ignore bot messages and direct messages
        if message.author.bot or not message.guild:
            return

        # Check if user is blacklisted from receiving points
        if await is_user_blacklisted(message.guild.id, message.author.id):
            return

        # 1. Exclude photos/files/GIF attachments and stickers
        if message.attachments or message.stickers:
            return

        content = message.content.strip()
        if not content:
            return

        # 2. Exclude GIF links (Tenor, Giphy, Gfycat, or .gif/.gifv URLs)
        if re.search(r'https?://\S*(?:tenor\.com|giphy\.com|gfycat\.com|\.gif|\.gifv)', content, re.IGNORECASE):
            return

        # 3. Strip all general URLs (http://, https://)
        without_urls = re.sub(r'https?://\S+', '', content)

        # 4. Strip custom Discord emojis (<:name:id> or <a:name:id>)
        without_custom_emojis = re.sub(r'<a?:[a-zA-Z0-9_]+:[0-9]+>', '', without_urls)

        # 5. Strip unicode emojis
        without_emojis = re.sub(r'[\U00010000-\U0010ffff\u2600-\u27ff\u2300-\u23ff\200d\ufe0f]', '', without_custom_emojis).strip()

        # If nothing remains after removing URLs and emojis, ignore.
        if not without_emojis:
            return

        # Process message for chat points (with channel boost support)
        new_pts, awarded, earned_pts, boost_mult = await process_chat_message(
            message.guild.id, message.author.id, message.channel.id
        )

        if awarded and boost_mult > 1.0:
            try:
                # 게릴라 부스트 적립 안내 (5초 후 자동 삭제)
                await message.channel.send(
                    f"⚡ {message.author.mention} 님! **채널 부스트 이벤트** 혜택으로 10회 채팅 적립 포인트 **{boost_mult:g}배 (+{earned_pts}P)**를 획득하셨습니다!",
                    delete_after=5
                )
            except Exception:
                pass

    @app_commands.command(name="채널부스트", description="[관리자 전용] 게릴라 채널 부스트 설정, 배율, 기간 및 해제를 관리합니다.")
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


async def setup(bot):
    await bot.add_cog(ChatReward(bot))


