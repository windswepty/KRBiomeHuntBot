"""일일 · 주간 퀘스트 (포인트 수급처).

채팅과 출석만으로는 포인트가 잘 모이지 않아 만든 수급처입니다.
미리 정해 둔 종류 중에서 **매일 무작위로 몇 개를 뽑아** 제시합니다.
고정 목록이면 며칠 만에 지루해집니다.

진행도는 다른 cog(`chat_reward` `checkin` `gacha`)가 활동할 때마다
`bump_quest_progress()` 로 올립니다. 이 파일은 **보여주고 보상을 주는**
역할만 합니다.

수치는 전부 `/퀘스트설정` 에서 조정합니다. 코드에 박아두면 운영진이
조정할 때마다 개발자를 불러야 합니다.
"""

import discord
from discord.ext import commands, tasks
from discord import app_commands

from utils.database import (
    get_quest_settings, save_quest_settings,
    get_daily_quests, weekly_quests, get_quest_progress,
    quest_progress_value, quest_done, quest_label, claim_quest,
    get_server_goal, mark_server_goal, add_activity_log,
    quest_channels,
    QUEST_KINDS,
)

# 공동 목표 진행 임베드를 몇 초마다 갱신할지. 메시지 수정이라 채널이
# 올라가지 않고 알림도 울리지 않습니다.
GOAL_REFRESH_SECONDS = 120

NL = chr(10)


def _channel_text(guild, quest: dict) -> str:
    """퀘스트에 배정된 채널의 표시 문구."""
    cid = quest.get("channel")
    if not cid or guild is None:
        return ""
    ch = guild.get_channel(int(cid))
    return ch.mention if ch else f"`#{cid}`"


def _bar(value: int, goal: int, width: int = 12) -> str:
    """진행도 막대. 숫자만 있으면 얼마나 남았는지 감이 안 옵니다."""
    if goal <= 0:
        return "─" * width
    filled = min(width, int(width * value / goal))
    return "█" * filled + "░" * (width - filled)


def _line(quest: dict, progress: dict, guild=None) -> tuple[str, bool, bool]:
    """퀘스트 한 줄. (문구, 달성여부, 수령여부)"""
    kind, goal = quest["kind"], quest["goal"]
    value = quest_progress_value(kind, progress, quest)
    done = quest_done(kind, value, goal)
    claimed = quest["id"] in (progress.get("claimed") or [])

    # 채널형 퀘스트는 어디인지 보여줘야 깰 수 있습니다.
    ch_text = ""
    if quest.get("channel") and guild is not None:
        ch = guild.get_channel(int(quest["channel"]))
        ch_text = ch.mention if ch else f"`#{quest['channel']}`"
    label = quest_label(kind, goal, ch_text)
    if kind == "early_bird":
        shown = f"{value}등" if value else "미출석"
        detail = f"`{shown}`"
    else:
        detail = f"`{min(value, goal):,}/{goal:,}`  {_bar(value, goal)}"

    mark = "✅" if claimed else ("🎁" if done else "▫️")
    tail = "  **수령 완료**" if claimed else ("  **← 받을 수 있습니다**" if done else "")
    return f"{mark} **{label}**\n　　{detail}{tail}", done, claimed


class QuestView(discord.ui.View):
    """보상 수령 버튼.

    임시 View 입니다. 재시작 후에는 명령어를 다시 부르면 되므로 영구 View 로
    만들 필요가 없고, 그래서 custom_id 도 없습니다.
    """

    def __init__(self, user_id: int, claimable: list[tuple[dict, bool]]):
        super().__init__(timeout=180)
        self.user_id = user_id
        self.claimable = claimable      # [(quest, weekly)]
        if not claimable:
            self.claim_all.disabled = True
            self.claim_all.label = "받을 보상이 없습니다"

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "⚠️ 본인만 사용할 수 있습니다.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="보상 받기", style=discord.ButtonStyle.success, emoji="🎁")
    async def claim_all(self, interaction: discord.Interaction,
                        button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        settings = await get_quest_settings(interaction.guild.id)
        total, names = 0, []
        for quest, weekly in self.claimable:
            # 공동 목표는 보상 항목이 따로입니다. 일일 보상으로 주면 안 됩니다.
            if quest["kind"] == "server_goal":
                reward = int(settings.get("goal_reward", 0) or 0)
            else:
                reward = int(settings.get(
                    "weekly_reward" if weekly else "daily_reward", 0) or 0)
            # claim_quest 가 '아직 안 받음'을 서버 측에서 판정합니다.
            # 연타해도 두 번 지급되지 않습니다.
            if await claim_quest(interaction.guild.id, interaction.user.id,
                                 quest["id"], reward, weekly=weekly):
                total += reward
                names.append("🤝 서버 공동 목표" if quest["kind"] == "server_goal"
                             else quest_label(quest["kind"], quest["goal"],
                                              _channel_text(interaction.guild, quest)))

        if not names:
            await interaction.followup.send(
                "이미 모두 수령하셨습니다.", ephemeral=True)
            return

        embed = discord.Embed(
            title="🎁 퀘스트 보상 수령",
            description=f"**+{total:,} P** 를 획득하셨습니다!",
            color=discord.Color.green())
        embed.add_field(name="완료한 퀘스트",
                        value="\n".join(f"• {n}" for n in names), inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)

        try:
            await add_activity_log(
                interaction.guild.id, "quest", interaction.user.id,
                interaction.user.display_name,
                f"퀘스트 {len(names)}개 완료 (+{total:,}P)")
        except Exception as e:
            print(f"[Quest] 활동 로그 기록 실패: {e}", flush=True)

        button.disabled = True
        button.label = "수령 완료"
        try:
            await interaction.edit_original_response(view=None)
        except (discord.HTTPException, discord.InteractionResponded):
            pass


def build_quest_panel(settings: dict) -> discord.Embed:
    """설정 패널 임베드. 설정이 바뀔 때마다 이걸로 다시 그립니다."""
    embed = discord.Embed(
        title="\U0001f9ed 퀘스트 관리",
        description="퀘스트는 **유저마다 다르게, 매일 무작위로** 뽑힙니다." + NL
                    + "수치는 전부 여기서 조정할 수 있습니다.",
        color=discord.Color.blurple())
    embed.add_field(name="상태",
                    value="✅ 켜짐" if settings.get("enabled") else "❌ 꺼짐",
                    inline=True)
    embed.add_field(name="일일 퀘스트",
                    value=f"{settings.get('daily_count', 0)}개 / 일", inline=True)
    embed.add_field(name="공동 목표",
                    value="✅ 켜짐" if settings.get("goal_enabled") else "❌ 꺼짐",
                    inline=True)
    return embed


async def refresh_panel(interaction: discord.Interaction, result: discord.Embed) -> None:
    """패널을 최신 설정으로 다시 그리고, 변경 결과를 따로 알립니다.

    이 한 단계가 없으면 드롭다운 설명이 옛 값 그대로 남아
    '켰는데 꺼짐으로 보이는' 상태가 됩니다.
    """
    settings = await get_quest_settings(interaction.guild.id)
    try:
        await interaction.response.edit_message(
            embed=build_quest_panel(settings),
            view=QuestSettingsView(settings, interaction.guild))
        await interaction.followup.send(embed=result, ephemeral=True)
    except (discord.HTTPException, discord.InteractionResponded):
        # 패널을 못 고치는 상황(메시지 만료 등)에서도 결과는 알려야 합니다
        if interaction.response.is_done():
            await interaction.followup.send(embed=result, ephemeral=True)
        else:
            await interaction.response.send_message(embed=result, ephemeral=True)


class QuestRewardModal(discord.ui.Modal, title="💰 퀘스트 보상 설정"):
    daily = discord.ui.TextInput(label="일일 퀘스트 1개당 보상 (P)",
                                 placeholder="예: 150", required=True, max_length=7)
    weekly = discord.ui.TextInput(label="주간 퀘스트 1개당 보상 (P)",
                                  placeholder="예: 800", required=True, max_length=7)
    count = discord.ui.TextInput(label="하루에 제시할 퀘스트 개수",
                                 placeholder="예: 3", required=True, max_length=2)
    wcount = discord.ui.TextInput(label="주마다 제시할 퀘스트 개수",
                                  placeholder="예: 2", required=True, max_length=2)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            d = int(str(self.daily.value).strip())
            w = int(str(self.weekly.value).strip())
            c = int(str(self.count.value).strip())
            wc = int(str(self.wcount.value).strip())
        except ValueError:
            await interaction.response.send_message("⚠️ 숫자만 입력해 주세요.",
                                                    ephemeral=True)
            return
        if min(d, w) < 0 or not (1 <= c <= 5) or not (1 <= wc <= 4):
            await interaction.response.send_message(
                "⚠️ 보상은 0 이상, 일일 개수는 1~5, 주간 개수는 1~4 사이여야 합니다.",
                ephemeral=True)
            return
        await save_quest_settings(interaction.guild.id,
                                  {"daily_reward": d, "weekly_reward": w,
                                   "daily_count": c, "weekly_count": wc})
        embed = discord.Embed(title="💰 퀘스트 보상 변경 완료",
                              color=discord.Color.green())
        embed.add_field(name="일일", value=f"개당 {d:,} P", inline=True)
        embed.add_field(name="주간", value=f"개당 {w:,} P", inline=True)
        embed.add_field(name="개수", value=f"일일 {c}개 · 주간 {wc}개", inline=True)
        embed.add_field(name="📊 하루 최대 수급 (1인)",
                        value=f"일일 **{d * c:,} P** + 주간 분할분", inline=False)
        embed.set_footer(text="개수 변경은 내일부터 반영됩니다 (오늘 목록은 이미 정해졌습니다)")
        await refresh_panel(interaction, embed)


class QuestGoalModal(discord.ui.Modal, title="🎯 퀘스트 목표치 설정"):
    chat = discord.ui.TextInput(label="채팅 N회", placeholder="예: 20",
                                required=True, max_length=6)
    channel_chat = discord.ui.TextInput(label="지정 채널 채팅 N회", placeholder="예: 10",
                                        required=True, max_length=6)
    gacha = discord.ui.TextInput(label="뽑기 N회", placeholder="예: 2",
                                 required=True, max_length=4)
    early_bird = discord.ui.TextInput(label="출석 선착순 N등 안에", placeholder="예: 20",
                                      required=True, max_length=5)
    week = discord.ui.TextInput(label="주간: 출석 N일 / 채팅 N회 (쉼표)",
                                placeholder="예: 5, 150", required=True, max_length=20)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            parts = [x.strip() for x in str(self.week.value).split(",")]
            goals = {
                "chat": int(str(self.chat.value).strip()),
                "channel_chat": int(str(self.channel_chat.value).strip()),
                "gacha": int(str(self.gacha.value).strip()),
                "early_bird": int(str(self.early_bird.value).strip()),
                "week_checkin": int(parts[0]),
                "week_chat": int(parts[1]),
            }
        except (ValueError, IndexError):
            await interaction.response.send_message(
                "⚠️ 숫자만 입력해 주세요. 주간은 `5, 150` 처럼 쉼표로 두 개.",
                ephemeral=True)
            return
        if any(v < 1 for v in goals.values()):
            await interaction.response.send_message(
                "⚠️ 모두 1 이상이어야 합니다.", ephemeral=True)
            return
        if goals["week_checkin"] > 7:
            await interaction.response.send_message(
                "⚠️ 주간 출석은 7일을 넘을 수 없습니다.", ephemeral=True)
            return

        await save_quest_settings(interaction.guild.id, {"goals": goals})
        embed = discord.Embed(title="🎯 목표치 변경 완료",
                              color=discord.Color.green())
        embed.add_field(
            name="변경된 목표",
            value=NL.join(f"• {quest_label(k, v)}" for k, v in goals.items()),
            inline=False)
        embed.set_footer(text="오늘 이미 진행 중인 퀘스트에도 즉시 반영됩니다")
        await refresh_panel(interaction, embed)


class ServerGoalModal(discord.ui.Modal, title="🤝 서버 공동 목표 설정"):
    target = discord.ui.TextInput(label="목표 채팅 수 (서버 전체 합계)",
                                  placeholder="예: 2000", required=True, max_length=8)
    reward = discord.ui.TextInput(label="달성 시 1인당 보상 (P)",
                                  placeholder="예: 300", required=True, max_length=7)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            t = int(str(self.target.value).strip())
            r = int(str(self.reward.value).strip())
        except ValueError:
            await interaction.response.send_message("⚠️ 숫자만 입력해 주세요.",
                                                    ephemeral=True)
            return
        if t < 1 or r < 0:
            await interaction.response.send_message(
                "⚠️ 목표는 1 이상, 보상은 0 이상이어야 합니다.", ephemeral=True)
            return
        await save_quest_settings(interaction.guild.id,
                                  {"goal_target": t, "goal_reward": r})
        embed = discord.Embed(title="🤝 공동 목표 변경 완료",
                              color=discord.Color.green())
        embed.add_field(name="목표", value=f"서버 전체 채팅 {t:,}회", inline=True)
        embed.add_field(name="1인당 보상", value=f"{r:,} P", inline=True)
        embed.set_footer(text="목표는 매일 밤 12시(KST)에 초기화됩니다")
        await refresh_panel(interaction, embed)


class RandomChannelSelect(discord.ui.ChannelSelect):
    """랜덤 채널 퀘스트의 후보 목록. **여러 개를 한 번에** 고릅니다.

    고른 것으로 목록을 통째로 교체합니다. 하나씩 더하고 빼는 것보다
    지금 무엇이 들어 있는지 헷갈리지 않습니다.
    """

    def __init__(self):
        super().__init__(placeholder="후보 채널을 모두 고르세요 (여러 개 가능)...",
                         channel_types=[discord.ChannelType.text],
                         min_values=1, max_values=25)

    async def callback(self, interaction: discord.Interaction):
        ids = [str(c.id) for c in self.values]
        await save_quest_settings(interaction.guild.id, {"channel_ids": ids,
                                                         "channel_id": None})
        mentions = []
        for cid in ids:
            ch = interaction.guild.get_channel(int(cid))
            mentions.append(ch.mention if ch else f"`#{cid}`")
        embed = discord.Embed(
            title="✅ 랜덤 채널 후보 설정 완료",
            description=f"**{len(ids)}개** 채널이 후보로 등록되었습니다.",
            color=discord.Color.green())
        embed.add_field(name="📋 후보 채널", value="  ".join(mentions)[:1024],
                        inline=False)
        embed.add_field(
            name="🎲 이렇게 동작합니다",
            value="유저마다 이 중 **하나가 무작위로** 배정됩니다." + NL
                  + "같은 사람은 그날 하루 같은 채널을 봅니다." + NL
                  + "-# 후보가 많을수록 사람들이 여러 채널로 흩어집니다.",
            inline=False)
        await refresh_panel(interaction, embed)


class GoalChannelSelect(discord.ui.ChannelSelect):
    """공동 목표 진행도를 보여줄 채널 (하나)."""

    def __init__(self):
        super().__init__(placeholder="공동 목표 안내 채널을 고르세요...",
                         channel_types=[discord.ChannelType.text],
                         min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        ch = interaction.guild.get_channel(self.values[0].id)
        if ch is None:
            await interaction.response.send_message("⚠️ 채널을 찾을 수 없습니다.",
                                                    ephemeral=True)
            return
        if not ch.permissions_for(interaction.guild.me).send_messages:
            await interaction.response.send_message(
                f"⚠️ {ch.mention} 에 메시지를 보낼 권한이 없습니다.", ephemeral=True)
            return
        await save_quest_settings(interaction.guild.id,
                                  {"goal_channel_id": str(ch.id)})
        await refresh_panel(interaction, discord.Embed(
            title="✅ 공동 목표 안내 채널 설정 완료",
            description=f"{ch.mention} 으로 지정했습니다.",
            color=discord.Color.green()))


class QuestChannelView(discord.ui.View):
    def __init__(self, field: str, label: str):
        super().__init__(timeout=180)
        self.add_item(RandomChannelSelect() if field == "channel_id"
                      else GoalChannelSelect())

    @discord.ui.button(label="후보 전체 해제", style=discord.ButtonStyle.secondary,
                       emoji="🚫", row=1)
    async def clear(self, interaction: discord.Interaction,
                    button: discord.ui.Button):
        await save_quest_settings(interaction.guild.id,
                                  {"channel_ids": [], "channel_id": None})
        await refresh_panel(interaction, discord.Embed(
            title="🚫 랜덤 채널 후보 해제",
            description="후보가 없으면 채널 퀘스트는 **출제되지 않습니다.**",
            color=discord.Color.orange()))


class QuestSettingsSelect(discord.ui.Select):
    def __init__(self, settings: dict, guild: discord.Guild):
        chans = quest_channels(settings)
        gch = (guild.get_channel(int(settings["goal_channel_id"]))
               if settings.get("goal_channel_id") else None)
        super().__init__(
            placeholder="퀘스트 설정을 선택하세요...",
            options=[
                discord.SelectOption(
                    label="퀘스트 켜기 / 끄기", value="toggle", emoji="⚡",
                    description=("현재: 켜짐" if settings.get("enabled")
                                 else "현재: 꺼짐")),
                discord.SelectOption(
                    label="보상 · 개수", value="reward", emoji="💰",
                    description=(f"일일 {settings.get('daily_reward', 0)} P x "
                                 f"{settings.get('daily_count', 0)}개 · "
                                 f"주간 {settings.get('weekly_reward', 0)} P")[:95]),
                discord.SelectOption(
                    label="목표치", value="goals", emoji="🎯",
                    description="채팅 N회, 뽑기 N회 등 각 퀘스트의 목표를 정합니다."),
                discord.SelectOption(
                    label="랜덤 채널 후보", value="channel", emoji="🎲",
                    description=(f"현재 {len(chans)}개 — 유저마다 무작위 배정" if chans
                                 else "미설정 — 채널 퀘스트는 출제되지 않습니다")[:95]),
                discord.SelectOption(
                    label="서버 공동 목표 켜기 / 끄기", value="goaltoggle", emoji="🤝",
                    description=("현재: 켜짐" if settings.get("goal_enabled")
                                 else "현재: 꺼짐")),
                discord.SelectOption(
                    label="공동 목표 수치", value="goalset", emoji="📊",
                    description=(f"채팅 {settings.get('goal_target', 0):,}회 -> "
                                 f"1인당 {settings.get('goal_reward', 0):,} P")[:95]),
                discord.SelectOption(
                    label="공동 목표 안내 채널", value="goalchannel", emoji="📢",
                    description=(f"현재: #{gch.name}" if gch else "미설정")[:95]),
                discord.SelectOption(
                    label="현재 설정 확인", value="view", emoji="📋",
                    description="지금 값으로 하루에 얼마가 나가는지 봅니다."),
            ])

    async def callback(self, interaction: discord.Interaction):
        choice = self.values[0]
        guild = interaction.guild
        settings = await get_quest_settings(guild.id)

        if choice == "toggle":
            new = not settings.get("enabled", False)
            await save_quest_settings(guild.id, {"enabled": new})
            embed = discord.Embed(
                title=f"⚡ 퀘스트 {'활성화' if new else '비활성화'}",
                description=("이제 `/퀘스트` 로 이용할 수 있습니다."
                             if new else "퀘스트가 중단되었습니다."),
                color=discord.Color.green() if new else discord.Color.red())
            if new:
                per_day = (int(settings.get("daily_reward", 0))
                           * int(settings.get("daily_count", 1)))
                embed.add_field(
                    name="📊 하루 최대 수급 (1인)",
                    value=f"약 **{per_day:,} P**" + NL
                          + "-# 주간 · 공동 목표는 별도입니다.",
                    inline=False)
            await refresh_panel(interaction, embed)

        elif choice == "reward":
            await interaction.response.send_modal(QuestRewardModal())
        elif choice == "goals":
            await interaction.response.send_modal(QuestGoalModal())
        elif choice == "goalset":
            await interaction.response.send_modal(ServerGoalModal())

        elif choice == "channel":
            chans = quest_channels(settings)
            cur = []
            for cid in chans:
                c = guild.get_channel(int(cid))
                cur.append(c.mention if c else f"`#{cid}`")
            embed = discord.Embed(
                title="🎲 랜덤 채널 후보",
                description="채널 퀘스트가 쓸 후보입니다. "
                            "**유저마다 이 중 하나가 무작위로** 배정됩니다.",
                color=discord.Color.blurple())
            embed.add_field(
                name=f"📋 현재 후보 ({len(chans)}개)",
                value=("  ".join(cur)[:1024] if cur
                       else "없음 — 채널 퀘스트가 출제되지 않습니다"),
                inline=False)
            embed.add_field(
                name="💡 참고",
                value="여러 개를 고르면 목록이 통째로 교체됩니다." + NL
                      + "후보가 많을수록 사람들이 여러 채널로 흩어집니다.",
                inline=False)
            await interaction.response.send_message(
                embed=embed, view=QuestChannelView("channel_id", "랜덤 채널"),
                ephemeral=True)

        elif choice == "goalchannel":
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="📢 공동 목표 안내 채널",
                    description="진행도 임베드 하나가 2분마다 갱신됩니다." + NL
                                + "메시지 수정이라 알림은 울리지 않습니다.",
                    color=discord.Color.blurple()),
                view=QuestChannelView("goal_channel_id", "공동 목표 안내"), ephemeral=True)

        elif choice == "goaltoggle":
            new = not settings.get("goal_enabled", False)
            await save_quest_settings(guild.id, {"goal_enabled": new})
            embed = discord.Embed(
                title=f"🤝 서버 공동 목표 {'활성화' if new else '비활성화'}",
                color=discord.Color.green() if new else discord.Color.red())
            if new and not settings.get("goal_channel_id"):
                embed.add_field(
                    name="⚠️ 안내 채널이 없습니다",
                    value="`공동 목표 안내 채널` 을 지정해야 진행도가 보입니다.",
                    inline=False)
            await refresh_panel(interaction, embed)

        else:
            gch = (guild.get_channel(int(settings["goal_channel_id"]))
                   if settings.get("goal_channel_id") else None)
            goals = settings.get("goals", {})
            embed = discord.Embed(title="📋 현재 퀘스트 설정",
                                  color=discord.Color.blurple())
            embed.add_field(name="상태",
                            value="켜짐" if settings.get("enabled") else "꺼짐",
                            inline=True)
            embed.add_field(name="일일",
                            value=f"{settings.get('daily_count', 0)}개 x "
                                  f"{settings.get('daily_reward', 0):,} P", inline=True)
            embed.add_field(name="주간",
                            value=f"개당 {settings.get('weekly_reward', 0):,} P",
                            inline=True)
            embed.add_field(
                name="🎯 목표치",
                value=NL.join(f"• {quest_label(k, int(v))}"
                              for k, v in goals.items() if k in QUEST_KINDS),
                inline=False)
            chans = quest_channels(settings)
            cur = []
            for cid in chans:
                c = guild.get_channel(int(cid))
                cur.append(c.mention if c else f"`#{cid}`")
            embed.add_field(name="🎲 랜덤 채널 후보",
                            value=("  ".join(cur)[:1024] if cur
                                   else "미설정 (채널 퀘스트 출제 안 됨)"),
                            inline=False)
            embed.add_field(
                name="🤝 공동 목표",
                value=(f"{'켜짐' if settings.get('goal_enabled') else '꺼짐'} · "
                       f"채팅 {settings.get('goal_target', 0):,}회 -> "
                       f"{settings.get('goal_reward', 0):,} P" + NL
                       + f"-# 안내: {gch.mention if gch else '미설정'}"),
                inline=False)
            daily_max = (int(settings.get("daily_reward", 0))
                         * int(settings.get("daily_count", 1)))
            weekly_sum = int(settings.get("weekly_reward", 0)) * 2
            embed.add_field(
                name="📊 1인 최대 수급 (추정)",
                value=f"하루 **{daily_max:,} P** · 주 **{daily_max * 7 + weekly_sum:,} P**"
                      + NL
                      + f"-# 공동 목표 달성 시 하루 +{settings.get('goal_reward', 0):,} P",
                inline=False)
            await interaction.response.send_message(embed=embed, ephemeral=True)


class QuestSettingsView(discord.ui.View):
    def __init__(self, settings: dict, guild: discord.Guild):
        super().__init__(timeout=180)
        self.add_item(QuestSettingsSelect(settings, guild))


class Quest(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.goal_refresh.start()

    def cog_unload(self):
        self.goal_refresh.cancel()

    # ── 공동 목표 ────────────────────────────────────────────────────

    @tasks.loop(seconds=GOAL_REFRESH_SECONDS)
    async def goal_refresh(self):
        """공동 목표 진행도를 하나의 메시지로 계속 갱신합니다.

        매번 새로 보내면 채널이 도배됩니다. 메시지 ID 를 저장해 두고
        **수정**하므로 알림이 울리지 않습니다.
        """
        await self.bot.wait_until_ready()
        for guild in self.bot.guilds:
            try:
                await self.update_goal_message(guild)
            except Exception as e:
                print(f"[Quest] {guild.name}: 공동 목표 갱신 실패 - "
                      f"{type(e).__name__}: {e}", flush=True)

    async def update_goal_message(self, guild) -> None:
        settings = await get_quest_settings(guild.id)
        if not (settings.get("enabled") and settings.get("goal_enabled")):
            return
        channel_id = settings.get("goal_channel_id")
        if not channel_id:
            return
        channel = guild.get_channel(int(channel_id))
        if channel is None or not channel.permissions_for(guild.me).send_messages:
            return

        state = await get_server_goal(guild.id)
        target = int(settings.get("goal_target", 0) or 0)
        progress = state["progress"]
        reached = target > 0 and progress >= target

        embed = discord.Embed(
            title="🤝 오늘의 서버 공동 목표",
            description=(f"서버 전체가 함께 **채팅 {target:,}회** 를 채우면\n"
                         f"오늘 퀘스트에 참여한 **모두에게** "
                         f"**+{int(settings.get('goal_reward', 0)):,} P** 가 지급됩니다."),
            color=discord.Color.gold() if reached else discord.Color.blurple())
        pct = (progress / target * 100) if target else 0
        embed.add_field(
            name=("🎉 달성!" if reached else "📊 진행 상황"),
            value=f"`{progress:,} / {target:,}`  ({pct:.0f}%)\n{_bar(progress, target, 20)}",
            inline=False)
        if reached:
            embed.add_field(
                name="🎁 보상 받는 법",
                value="`/퀘스트` 를 열어 **[보상 받기]** 를 눌러 주세요.",
                inline=False)
        embed.set_footer(text="매일 밤 12시(KST)에 새로 시작됩니다")
        embed.timestamp = discord.utils.utcnow()

        if state["message_id"]:
            try:
                msg = await channel.fetch_message(int(state["message_id"]))
                await msg.edit(embed=embed)
                return
            except discord.NotFound:
                pass            # 지워졌으니 새로 만듭니다
            except discord.HTTPException as e:
                print(f"[Quest] 공동 목표 수정 실패: {e}", flush=True)
                return
        try:
            msg = await channel.send(embed=embed)
        except discord.HTTPException as e:
            print(f"[Quest] 공동 목표 생성 실패: {e}", flush=True)
            return
        await mark_server_goal(guild.id, goal_message_id=str(msg.id))

    # ── 유저용 ───────────────────────────────────────────────────────

    @app_commands.command(
        name="퀘스트",
        description="오늘의 퀘스트와 진행 상황을 확인하고 보상을 받습니다."
    )
    async def quest(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = interaction.guild
        settings = await get_quest_settings(guild.id)

        if not settings.get("enabled"):
            await interaction.followup.send(
                embed=discord.Embed(
                    title="⚠️ 퀘스트가 아직 열리지 않았습니다",
                    description="관리자가 설정을 마치면 이용하실 수 있습니다.",
                    color=discord.Color.orange()),
                ephemeral=True)
            return

        dailies = await get_daily_quests(guild.id, settings, interaction.user.id)
        weeklies = weekly_quests(settings, guild.id, interaction.user.id)
        progress = await get_quest_progress(guild.id, interaction.user.id)

        embed = discord.Embed(
            title="📋 나의 퀘스트",
            description="완료한 퀘스트는 아래 **[보상 받기]** 로 한 번에 받을 수 있습니다.",
            color=discord.Color.blurple())

        claimable, done_cnt = [], 0
        if dailies:
            lines = []
            for q in dailies:
                text, done, claimed = _line(q, progress, guild)
                lines.append(text)
                if done and not claimed:
                    claimable.append((q, False))
                if done:
                    done_cnt += 1
            embed.add_field(
                name=f"☀️ 오늘의 퀘스트  ·  개당 +{int(settings.get('daily_reward', 0)):,} P",
                value="\n".join(lines), inline=False)

        if weeklies:
            lines = []
            for q in weeklies:
                text, done, claimed = _line(q, progress, guild)
                lines.append(text)
                if done and not claimed:
                    claimable.append((q, True))
            embed.add_field(
                name=f"📅 이번 주 퀘스트 (일~토)  ·  개당 +{int(settings.get('weekly_reward', 0)):,} P",
                value="\n".join(lines), inline=False)

        if settings.get("goal_enabled"):
            state = await get_server_goal(guild.id)
            target = int(settings.get("goal_target", 0) or 0)
            prog = state["progress"]
            reached = target > 0 and prog >= target
            goal_q = {"id": "g:server", "kind": "server_goal", "goal": target}
            claimed = "g:server" in (progress.get("claimed") or [])
            if reached and not claimed:
                claimable.append((goal_q, False))
            embed.add_field(
                name="🤝 서버 공동 목표",
                value=(f"{'✅' if claimed else ('🎁' if reached else '▫️')} "
                       f"**서버 전체 채팅 {target:,}회**\n"
                       f"　　`{min(prog, target):,}/{target:,}`  {_bar(prog, target)}"
                       + ("  **수령 완료**" if claimed else
                          ("  **← 받을 수 있습니다**" if reached else ""))),
                inline=False)

        embed.set_footer(text="퀘스트는 사람마다 다릅니다 · 일일은 매일 밤 12시(KST), 주간은 일요일 밤 12시에 새로 시작됩니다")
        await interaction.followup.send(
            embed=embed, view=QuestView(interaction.user.id, claimable), ephemeral=True)


    @app_commands.command(
        name="퀘스트설정",
        description="[관리자 전용] 퀘스트 보상 · 목표치 · 공동 목표를 관리합니다."
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def quest_settings(self, interaction: discord.Interaction):
        settings = await get_quest_settings(interaction.guild.id)
        await interaction.response.send_message(
            embed=build_quest_panel(settings),
            view=QuestSettingsView(settings, interaction.guild),
            ephemeral=True)


async def setup(bot):
    await bot.add_cog(Quest(bot))
