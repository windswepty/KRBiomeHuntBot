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

import re
import time
import datetime

import discord
from discord.ext import commands, tasks
from discord import app_commands

from utils import health
from utils.database import (
    get_quest_settings, save_quest_settings,
    get_daily_quests, weekly_quests, get_quest_progress,
    quest_progress_value, quest_done, quest_label, claim_quest,
    get_server_goal, mark_server_goal, add_activity_log,
    quest_channels, goal_min_chat,
    active_events, event_status, event_period_key, KST,
    list_events, get_event, save_event, delete_event, event_is_active,
    EVENT_KINDS, MAX_TIERS, MAX_OBJECTIVES,
    QUEST_KINDS,
)

# 공동 목표 진행 임베드의 폴백 갱신 주기. 채팅 이벤트로 실시간 갱신되지만,
# 이벤트를 놓쳤을 때를 대비해 주기적으로도 갱신합니다.
GOAL_REFRESH_SECONDS = 120

# 채팅 이벤트에 의한 실시간 갱신 쿨다운. 이 시간 이내에 여러 채팅이 와도
# 임베드 수정은 한 번만 합니다.
GOAL_UPDATE_COOLDOWN = 10

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

    def __init__(self, user_id: int, claimable: list[dict]):
        super().__init__(timeout=180)
        self.user_id = user_id
        # [{id, name, points, role_id, weekly, period}]
        self.claimable = claimable
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
        total, names, roles_given, role_failed = 0, [], [], []
        for c in self.claimable:
            # claim_quest 가 '아직 안 받음'을 서버 측에서 판정합니다.
            # 연타해도 두 번 지급되지 않습니다.
            if not await claim_quest(interaction.guild.id, interaction.user.id,
                                     c["id"], int(c.get("points", 0) or 0),
                                     weekly=bool(c.get("weekly")),
                                     period=c.get("period")):
                continue
            total += int(c.get("points", 0) or 0)
            names.append(c["name"])

            # 역할 보상. 포인트 지급은 이미 끝났으므로, 역할이 실패해도
            # 되돌리지 않고 무엇이 안 됐는지 알려줍니다.
            rid = c.get("role_id")
            if rid:
                role = interaction.guild.get_role(int(rid))
                if role is None:
                    role_failed.append(f"`{rid}` (서버에 없음)")
                elif role in interaction.user.roles:
                    roles_given.append(role.mention)
                else:
                    try:
                        await interaction.user.add_roles(
                            role, reason="이벤트 퀘스트 보상")
                        roles_given.append(role.mention)
                    except discord.Forbidden:
                        role_failed.append(f"{role.mention} (봇 권한 부족)")
                    except discord.HTTPException as e:
                        role_failed.append(f"{role.mention} ({e.status})")

        if not names:
            await interaction.followup.send(
                "이미 모두 수령하셨습니다.", ephemeral=True)
            return

        desc = f"**+{total:,} P** 를 획득하셨습니다!" if total else "보상을 받았습니다!"
        embed = discord.Embed(title="🎁 퀘스트 보상 수령", description=desc,
                              color=discord.Color.green())
        embed.add_field(name="완료한 퀘스트",
                        value="\n".join(f"• {n}" for n in names)[:1024],
                        inline=False)
        if roles_given:
            embed.add_field(name="🎭 획득한 역할",
                            value="  ".join(roles_given)[:1024], inline=False)
        if role_failed:
            embed.add_field(
                name="⚠️ 지급하지 못한 역할",
                value="\n".join(f"• {x}" for x in role_failed)[:1024]
                      + "\n-# 포인트는 정상 지급되었습니다. 관리자에게 알려 주세요.",
                inline=False)
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
    on = bool(settings.get("enabled"))
    admin_only = bool(settings.get("admin_only"))
    embed.add_field(name="상태", value="✅ 켜짐" if on else "❌ 꺼짐", inline=True)
    embed.add_field(name="공개 범위",
                    value="🔧 관리자만" if admin_only else "👥 모두",
                    inline=True)
    embed.add_field(name="일일 퀘스트",
                    value=f"{settings.get('daily_count', 0)}개 / 일", inline=True)
    reset_days = settings.get("goal_reset_days", 1)
    reset_text = f"{reset_days}일마다" if reset_days > 1 else "매일"
    
    embed.add_field(name="공동 목표",
                    value=f"{'✅ 켜짐' if settings.get('goal_enabled') else '❌ 꺼짐'} ({reset_text})",
                    inline=True)

    # 관리자 전용 모드는 '켜짐' 위에 얹는 필터입니다. 퀘스트가 꺼져 있으면
    # 관리자도 못 씁니다. 이걸 안 알려주면 "켰는데 왜 안 되지"가 됩니다.
    if admin_only and not on:
        embed.add_field(
            name="⚠️ 지금은 아무도 쓸 수 없습니다",
            value="관리자 전용 모드는 **퀘스트가 켜져 있을 때만** 의미가 있습니다."
                  + NL + "`⚡ 퀘스트 켜기` 를 눌러야 관리자끼리 테스트할 수 있습니다.",
            inline=False)
    elif admin_only:
        embed.add_field(
            name="🔧 테스트 중",
            value="관리자만 `/퀘스트` 를 볼 수 있습니다. 일반 유저에게는 "
                  "**꺼진 것과 똑같이** 보입니다." + NL
                  + "-# 진행도는 평소처럼 쌓이므로 공개하면 그대로 이어집니다.",
            inline=False)
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


class ServerGoalModal(discord.ui.Modal, title="🤝 서버 공동 목표 설정"):
    target = discord.ui.TextInput(label="목표 채팅 수 (서버 전체 합계)",
                                  placeholder="예: 2000", required=True, max_length=8)
    reward = discord.ui.TextInput(label="달성 시 1인당 보상 (P)",
                                  placeholder="예: 300", required=True, max_length=7)
    min_percent = discord.ui.TextInput(
        label="본인 최소 참여 (%) · 0이면 조건 없음",
        placeholder="예: 1  ->  목표 3,000회면 본인이 30회 이상",
        required=False, max_length=4)
    reset_days = discord.ui.TextInput(
        label="초기화 주기 (일)",
        placeholder="예: 1 (매일), 3 (3일마다)",
        required=False, max_length=3, default="1")


    async def on_submit(self, interaction: discord.Interaction):
        try:
            t = int(str(self.target.value).strip())
            r = int(str(self.reward.value).strip())
            pct = float(str(self.min_percent.value or "0").strip() or 0)
            rd = int(str(self.reset_days.value or "1").strip() or 1)
        except ValueError:
            await interaction.response.send_message("⚠️ 숫자만 입력해 주세요.",
                                                    ephemeral=True)
            return
        if t < 1 or r < 0:
            await interaction.response.send_message(
                "⚠️ 목표는 1 이상, 보상은 0 이상이어야 합니다.", ephemeral=True)
            return
        if not (0 <= pct <= 100):
            await interaction.response.send_message(
                "⚠️ 참여 비율은 0~100 사이여야 합니다.", ephemeral=True)
            return
        if rd < 1:
            await interaction.response.send_message(
                "⚠️ 초기화 주기는 1일 이상이어야 합니다.", ephemeral=True)
            return
            
        await save_quest_settings(interaction.guild.id,
                                  {"goal_target": t, "goal_reward": r,
                                   "goal_min_percent": pct, "goal_reset_days": rd})
        embed = discord.Embed(title="🤝 공동 목표 변경 완료",
                              color=discord.Color.green())
        embed.add_field(name="목표", value=f"서버 전체 채팅 {t:,}회", inline=True)
        embed.add_field(name="1인당 보상", value=f"{r:,} P", inline=True)
        fresh = await get_quest_settings(interaction.guild.id)
        need = goal_min_chat(fresh)
        embed.add_field(
            name="🔒 본인 최소 참여",
            value=(f"{pct:g}% = **{need:,}회 이상**" if need else "조건 없음"),
            inline=True)
        reset_text = f"매일 밤 12시" if rd <= 1 else f"{rd}일마다 자정"
        embed.set_footer(text=f"목표는 {reset_text}(KST)에 초기화됩니다")
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
    """공동 목표 진행도를 보여줄 채널 또는 스레드 (하나)."""

    def __init__(self):
        super().__init__(placeholder="공동 목표 안내 채널/스레드를 고르세요...",
                         channel_types=[
                             discord.ChannelType.text,
                             discord.ChannelType.public_thread,
                             discord.ChannelType.private_thread,
                         ],
                         min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        ch = (interaction.guild.get_channel_or_thread(self.values[0].id)
              or interaction.guild.get_channel(self.values[0].id))
        if ch is None:
            await interaction.response.send_message("⚠️ 채널/스레드를 찾을 수 없습니다.",
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
    """랜덤 채널 후보와 공동 목표 안내 채널이 **같은 View 를 함께 씁니다.**

    그래서 해제 버튼은 반드시 `field` 를 보고 자기 설정만 지워야 합니다.
    예전에는 어느 화면에서 눌러도 랜덤 채널 후보를 지웠는데, 공동 목표
    화면에서 누른 관리자는 건드린 적도 없는 설정이 날아간 것을 모릅니다.
    게다가 후보가 비면 그날 뽑기 풀이 바뀌어 **전원의 퀘스트 목록이**
    통째로 달라집니다.
    """

    def __init__(self, field: str, label: str):
        super().__init__(timeout=180)
        self.field = field
        self.label = label
        self.add_item(RandomChannelSelect() if field == "channel_id"
                      else GoalChannelSelect())
        self.clear.label = ("후보 전체 해제" if field == "channel_id"
                            else "안내 채널 해제")

    @discord.ui.button(label="해제", style=discord.ButtonStyle.secondary,
                       emoji="🚫", row=1)
    async def clear(self, interaction: discord.Interaction,
                    button: discord.ui.Button):
        if self.field == "channel_id":
            await save_quest_settings(interaction.guild.id,
                                      {"channel_ids": [], "channel_id": None})
            embed = discord.Embed(
                title="🚫 랜덤 채널 후보 해제",
                description="후보가 없으면 채널 퀘스트는 **출제되지 않습니다.**",
                color=discord.Color.orange())
        else:
            await save_quest_settings(interaction.guild.id,
                                      {"goal_channel_id": None})
            embed = discord.Embed(
                title="🚫 공동 목표 안내 채널 해제",
                description="진행도 안내를 올리지 않습니다.\n"
                            "-# 공동 목표 자체는 그대로 돌아갑니다. "
                            "`/퀘스트` 에서는 계속 보입니다.",
                color=discord.Color.orange())
        await refresh_panel(interaction, embed)


def _kind_label(kind: str, goal: int) -> str:
    """조건 문구. `채팅 200회` 처럼."""
    return EVENT_KINDS.get(kind, "{n}").replace("{n}", f"{goal:,}")


def _fmt_event(ev: dict, guild=None) -> str:
    """이벤트 한 줄 요약."""
    state = "🟢 진행 중" if event_is_active(ev) else (
        "⏸️ 꺼짐" if not ev.get("enabled", True) else "🕒 기간 밖")
    mode = "단계별" if ev.get("mode") == "tier" else "모두달성"
    span = f"{ev.get('starts', '?')} ~ {ev.get('ends', '?')}"
    return f"{state}  **{ev.get('name', '?')}**  ·  {mode}  ·  `{span}`"


class EventBasicModal(discord.ui.Modal, title="🎉 이벤트 만들기"):
    """이름·기간·방식을 받습니다. 조건과 보상은 다음 단계에서 정합니다."""

    name = discord.ui.TextInput(label="이벤트 이름", placeholder="예: 추석 이벤트",
                                required=True, max_length=40)
    period = discord.ui.TextInput(
        label="기간 (시작 ~ 끝)",
        placeholder="예: 2026-09-15 ~ 2026-09-22  (양끝 포함)",
        required=True, max_length=30)
    mode = discord.ui.TextInput(
        label="방식: 단계 / 모두",
        placeholder="단계 = 하나를 여러 단계로 · 모두 = 여러 조건 전부",
        required=True, max_length=4)

    def __init__(self, event_id: str | None = None):
        super().__init__()
        self.event_id = event_id

    async def on_submit(self, interaction: discord.Interaction):
        raw = str(self.period.value).replace("~", " ").split()
        if len(raw) != 2:
            await interaction.response.send_message(
                "⚠️ 기간은 `2026-09-15 ~ 2026-09-22` 형태로 적어 주세요.",
                ephemeral=True)
            return
        starts, ends = raw[0].strip(), raw[1].strip()
        for d in (starts, ends):
            try:
                datetime.datetime.strptime(d, "%Y-%m-%d")
            except ValueError:
                await interaction.response.send_message(
                    f"⚠️ 날짜 형식이 올바르지 않습니다: `{d}`  (예: 2026-09-15)",
                    ephemeral=True)
                return
        if ends < starts:
            await interaction.response.send_message(
                "⚠️ 끝나는 날이 시작일보다 빠릅니다.", ephemeral=True)
            return

        m = str(self.mode.value).strip()
        mode = "tier" if m.startswith("단계") else ("all" if m.startswith("모두") else None)
        if mode is None:
            await interaction.response.send_message(
                "⚠️ 방식은 `단계` 또는 `모두` 로 적어 주세요.", ephemeral=True)
            return

        title = str(self.name.value).strip()
        if not title:
            await interaction.response.send_message(
                "⚠️ 이벤트 이름을 적어 주세요.", ephemeral=True)
            return

        eid = self.event_id
        if eid is None:
            # 초 단위 타임스탬프만 쓰면 같은 초에 만든 이벤트가 앞의 것을
            # **말없이 덮어씁니다.** 이미 있는 id 는 피해 갑니다.
            base = f"e{int(datetime.datetime.now(KST).timestamp())}"
            taken = {e["id"] for e in await list_events(interaction.guild.id)}
            eid, n = base, 1
            while eid in taken:
                eid, n = f"{base}_{n}", n + 1

        old = await get_event(interaction.guild.id, eid) or {}
        data = dict(old)
        data.update({"name": title, "mode": mode,
                     "starts": starts, "ends": ends})
        data.setdefault("enabled", True)
        if mode == "tier":
            data.setdefault("kind", "chat")
            data.setdefault("tiers", [])
        else:
            data.setdefault("objectives", [])
            data.setdefault("reward", {"points": 0, "role_id": None})
        await save_event(interaction.guild.id, eid, data)

        embed = discord.Embed(
            title="🎉 이벤트 저장 완료",
            description=_fmt_event(await get_event(interaction.guild.id, eid)),
            color=discord.Color.green())
        embed.add_field(
            name="👉 다음 단계",
            value=("`🎯 단계·보상 설정` 에서 단계별 목표와 보상을 정하세요."
                   if mode == "tier" else
                   "`🎯 조건 설정` 과 `🎁 보상 설정` 을 차례로 정하세요."),
            inline=False)
        embed.set_footer(text="조건이나 보상이 비어 있으면 유저에게 보이지 않습니다")
        await interaction.response.send_message(embed=embed, ephemeral=True)


class EventTierModal(discord.ui.Modal, title="🎯 단계 · 보상 설정"):
    """단계별 방식의 목표와 보상을 한 번에 입력받습니다."""

    kind = discord.ui.TextInput(label="조건 (채팅 / 뽑기 / 출석)",
                                placeholder="예: 채팅", required=True, max_length=6)
    tiers = discord.ui.TextInput(
        label="단계  목표:포인트:역할ID  (줄바꿈)",
        placeholder="100:500\n300:1500\n1000:0:1329788098187624500",
        style=discord.TextStyle.paragraph, required=True, max_length=400)

    def __init__(self, event_id: str):
        super().__init__()
        self.event_id = event_id

    async def on_submit(self, interaction: discord.Interaction):
        kmap = {"채팅": "chat", "뽑기": "gacha", "출석": "checkin"}
        kind = kmap.get(str(self.kind.value).strip())
        if not kind:
            await interaction.response.send_message(
                "⚠️ 조건은 `채팅` `뽑기` `출석` 중 하나입니다.", ephemeral=True)
            return

        tiers = []
        for line in re.split(r"[,\n]+", str(self.tiers.value)):
            line = line.strip()
            if not line:
                continue
            parts = [x.strip() for x in line.split(":")]
            if not (1 <= len(parts) <= 3) or not parts[0].isdigit():
                await interaction.response.send_message(
                    f"⚠️ `{line[:30]}` 를 읽을 수 없습니다.\n"
                    "`목표:포인트` 또는 `목표:포인트:역할ID` 형태로 적어 주세요.",
                    ephemeral=True)
                return
            goal = int(parts[0])
            points = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
            role_id = parts[2] if len(parts) > 2 and parts[2].isdigit() else None
            if goal < 1:
                await interaction.response.send_message(
                    "⚠️ 목표는 1 이상이어야 합니다.", ephemeral=True)
                return
            if role_id and interaction.guild.get_role(int(role_id)) is None:
                await interaction.response.send_message(
                    f"⚠️ 역할 `{role_id}` 을 서버에서 찾을 수 없습니다.", ephemeral=True)
                return
            tiers.append({"goal": goal, "points": points, "role_id": role_id})

        if not tiers:
            await interaction.response.send_message("⚠️ 단계를 하나 이상 적어 주세요.",
                                                    ephemeral=True)
            return
        if len(tiers) > MAX_TIERS:
            await interaction.response.send_message(
                f"⚠️ 단계는 최대 {MAX_TIERS}개까지입니다.", ephemeral=True)
            return
        tiers.sort(key=lambda t: t["goal"])

        ev = await get_event(interaction.guild.id, self.event_id)
        if ev is None:
            await interaction.response.send_message("⚠️ 이벤트를 찾을 수 없습니다.",
                                                    ephemeral=True)
            return
        ev.update({"kind": kind, "tiers": tiers, "mode": "tier"})
        await save_event(interaction.guild.id, self.event_id, ev)

        tpl = EVENT_KINDS.get(kind, "{n}")
        lines = []
        for t in tiers:
            rw = []
            if t["points"]:
                rw.append(f"+{t['points']:,} P")
            if t["role_id"]:
                role = interaction.guild.get_role(int(t["role_id"]))
                rw.append(role.mention if role else f"`{t['role_id']}`")
            label = _kind_label(kind, t["goal"])
            lines.append(f"• {label}  →  " + (" · ".join(rw) or "보상 없음"))
        embed = discord.Embed(title="🎯 단계 설정 완료", color=discord.Color.green())
        embed.add_field(name=f"{len(tiers)}단계", value=NL.join(lines)[:1024],
                        inline=False)
        embed.set_footer(text="단계는 목표가 작은 순으로 자동 정렬됩니다")
        await interaction.response.send_message(embed=embed, ephemeral=True)


class EventObjectiveModal(discord.ui.Modal, title="🎯 조건 설정 (모두 달성)"):
    objectives = discord.ui.TextInput(
        label="조건  종류:목표  (줄바꿈)",
        placeholder="채팅:200\n뽑기:10\n출석:5",
        style=discord.TextStyle.paragraph, required=True, max_length=200)

    def __init__(self, event_id: str):
        super().__init__()
        self.event_id = event_id

    async def on_submit(self, interaction: discord.Interaction):
        kmap = {"채팅": "chat", "뽑기": "gacha", "출석": "checkin"}
        objs = []
        for line in re.split(r"[,\n]+", str(self.objectives.value)):
            line = line.strip()
            if not line:
                continue
            parts = [x.strip() for x in line.split(":")]
            if len(parts) != 2 or parts[0] not in kmap or not parts[1].isdigit():
                await interaction.response.send_message(
                    f"⚠️ `{line[:30]}` 를 읽을 수 없습니다.\n"
                    "`채팅:200` 처럼 `종류:목표` 로 적어 주세요. "
                    "(채팅 · 뽑기 · 출석)", ephemeral=True)
                return
            objs.append({"kind": kmap[parts[0]], "goal": int(parts[1])})
        if not objs:
            await interaction.response.send_message("⚠️ 조건을 하나 이상 적어 주세요.",
                                                    ephemeral=True)
            return
        if len(objs) > MAX_OBJECTIVES:
            await interaction.response.send_message(
                f"⚠️ 조건은 최대 {MAX_OBJECTIVES}개까지입니다.", ephemeral=True)
            return

        ev = await get_event(interaction.guild.id, self.event_id)
        if ev is None:
            await interaction.response.send_message("⚠️ 이벤트를 찾을 수 없습니다.",
                                                    ephemeral=True)
            return
        ev.update({"objectives": objs, "mode": "all"})
        await save_event(interaction.guild.id, self.event_id, ev)

        embed = discord.Embed(title="🎯 조건 설정 완료", color=discord.Color.green())
        embed.add_field(
            name=f"{len(objs)}개 조건 (전부 달성해야 보상)",
            value=NL.join("• " + _kind_label(o["kind"], o["goal"])
                          for o in objs)[:1024],
            inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)


class EventRewardModal(discord.ui.Modal, title="🎁 보상 설정 (모두 달성)"):
    points = discord.ui.TextInput(label="포인트", placeholder="예: 2000",
                                  required=True, max_length=8)
    role_id = discord.ui.TextInput(label="역할 ID (선택)",
                                   placeholder="비워두면 역할 없음",
                                   required=False, max_length=25)

    def __init__(self, event_id: str):
        super().__init__()
        self.event_id = event_id

    async def on_submit(self, interaction: discord.Interaction):
        try:
            pts = int(str(self.points.value).strip())
        except ValueError:
            await interaction.response.send_message("⚠️ 포인트는 숫자로 적어 주세요.",
                                                    ephemeral=True)
            return
        if pts < 0:
            await interaction.response.send_message("⚠️ 포인트는 0 이상이어야 합니다.",
                                                    ephemeral=True)
            return
        rid = str(self.role_id.value or "").strip() or None
        if rid:
            if not rid.isdigit() or interaction.guild.get_role(int(rid)) is None:
                await interaction.response.send_message(
                    f"⚠️ 역할 `{rid[:25]}` 을 서버에서 찾을 수 없습니다.", ephemeral=True)
                return

        ev = await get_event(interaction.guild.id, self.event_id)
        if ev is None:
            await interaction.response.send_message("⚠️ 이벤트를 찾을 수 없습니다.",
                                                    ephemeral=True)
            return
        ev["reward"] = {"points": pts, "role_id": rid}
        await save_event(interaction.guild.id, self.event_id, ev)

        role = interaction.guild.get_role(int(rid)) if rid else None
        embed = discord.Embed(title="🎁 보상 설정 완료", color=discord.Color.green())
        embed.add_field(name="포인트", value=f"{pts:,} P", inline=True)
        embed.add_field(name="역할", value=role.mention if role else "없음", inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)


class EventPickSelect(discord.ui.Select):
    """이벤트 하나를 골라 다음 동작을 합니다."""

    def __init__(self, events: list[dict], action: str):
        self.action = action
        options = [
            discord.SelectOption(
                label=(e.get("name") or "이름 없음")[:100],
                value=e["id"],
                description=(f"{'진행 중' if event_is_active(e) else '대기/종료'} · "
                             f"{'단계별' if e.get('mode') == 'tier' else '모두달성'} · "
                             f"{e.get('starts', '?')}~{e.get('ends', '?')}")[:95])
            for e in events[:25]
        ] or [discord.SelectOption(label="이벤트가 없습니다", value="none")]
        super().__init__(placeholder="이벤트를 고르세요...", options=options)

    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "none":
            await interaction.response.send_message("이벤트가 없습니다.", ephemeral=True)
            return
        eid = self.values[0]
        ev = await get_event(interaction.guild.id, eid)
        if ev is None:
            await interaction.response.send_message("⚠️ 이벤트를 찾을 수 없습니다.",
                                                    ephemeral=True)
            return

        if self.action == "goal":
            if ev.get("mode") == "tier":
                await interaction.response.send_modal(EventTierModal(eid))
            else:
                await interaction.response.send_modal(EventObjectiveModal(eid))
        elif self.action == "reward":
            if ev.get("mode") == "tier":
                await interaction.response.send_message(
                    "ℹ️ 단계별 방식은 `🎯 단계·보상 설정` 에서 보상까지 함께 정합니다.",
                    ephemeral=True)
            else:
                await interaction.response.send_modal(EventRewardModal(eid))
        elif self.action == "edit":
            await interaction.response.send_modal(EventBasicModal(eid))
        elif self.action == "toggle":
            ev["enabled"] = not ev.get("enabled", True)
            await save_event(interaction.guild.id, eid, ev)
            await interaction.response.send_message(
                embed=discord.Embed(
                    title=f"{'▶️ 이벤트 켜짐' if ev['enabled'] else '⏸️ 이벤트 꺼짐'}",
                    description=_fmt_event(ev), color=discord.Color.green()
                    if ev["enabled"] else discord.Color.orange()),
                ephemeral=True)
        elif self.action == "delete":
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="🗑️ 이벤트를 삭제할까요?",
                    description=_fmt_event(ev) + NL
                                + "-# 유저가 이미 받은 보상은 회수되지 않습니다.",
                    color=discord.Color.red()),
                view=EventDeleteConfirm(eid, ev.get("name", "?")), ephemeral=True)


class EventPickView(discord.ui.View):
    def __init__(self, events: list[dict], action: str):
        super().__init__(timeout=180)
        self.add_item(EventPickSelect(events, action))


class EventDeleteConfirm(discord.ui.View):
    def __init__(self, event_id: str, name: str):
        super().__init__(timeout=120)
        self.event_id, self.name = event_id, name

    @discord.ui.button(label="삭제", style=discord.ButtonStyle.danger, emoji="🗑️")
    async def do_delete(self, interaction: discord.Interaction,
                        button: discord.ui.Button):
        ok = await delete_event(interaction.guild.id, self.event_id)
        await interaction.response.send_message(
            embed=discord.Embed(
                title="🗑️ 삭제 완료" if ok else "ℹ️ 이미 없습니다",
                description=f"**{self.name}** 이벤트를 삭제했습니다."
                            if ok else "이미 삭제된 이벤트입니다.",
                color=discord.Color.orange()),
            ephemeral=True)
        self.stop()

    @discord.ui.button(label="취소", style=discord.ButtonStyle.secondary, emoji="✖️")
    async def cancel(self, interaction: discord.Interaction,
                     button: discord.ui.Button):
        await interaction.response.send_message("취소했습니다.", ephemeral=True)
        self.stop()


class EventMenuSelect(discord.ui.Select):
    def __init__(self, events: list[dict]):
        active = sum(1 for e in events if event_is_active(e))
        super().__init__(
            placeholder="이벤트 관리 기능을 고르세요...",
            options=[
                discord.SelectOption(label="이벤트 만들기", value="new", emoji="➕",
                                     description="이름 · 기간 · 방식을 정합니다."),
                discord.SelectOption(label="목록 보기", value="list", emoji="📋",
                                     description=f"전체 {len(events)}개 · 진행 중 {active}개"),
                discord.SelectOption(label="조건 · 단계 설정", value="goal", emoji="🎯",
                                     description="목표치와 (단계별이면) 보상을 정합니다."),
                discord.SelectOption(label="보상 설정", value="reward", emoji="🎁",
                                     description="모두달성 방식의 최종 보상."),
                discord.SelectOption(label="이름 · 기간 수정", value="edit", emoji="✏️",
                                     description="기존 이벤트의 기본 정보를 고칩니다."),
                discord.SelectOption(label="켜기 / 끄기", value="toggle", emoji="⏸️",
                                     description="기간 안이어도 잠시 멈출 수 있습니다."),
                discord.SelectOption(label="삭제", value="delete", emoji="🗑️",
                                     description="이벤트를 지웁니다. (확인 후)"),
            ])
        self.events = events

    async def callback(self, interaction: discord.Interaction):
        c = self.values[0]
        if c == "new":
            await interaction.response.send_modal(EventBasicModal())
            return
        if c == "list":
            evs = await list_events(interaction.guild.id)
            embed = discord.Embed(
                title="📋 이벤트 목록",
                description=(NL.join(f"{i}. {_fmt_event(e)}"
                                     for i, e in enumerate(evs, 1))[:4000]
                             if evs else "아직 만든 이벤트가 없습니다."),
                color=discord.Color.blurple())
            if evs:
                embed.set_footer(text="🟢 진행 중 · 🕒 기간 밖 · ⏸️ 꺼짐")
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        evs = await list_events(interaction.guild.id)
        if not evs:
            await interaction.response.send_message(
                "먼저 `➕ 이벤트 만들기` 로 이벤트를 만들어 주세요.", ephemeral=True)
            return
        titles = {"goal": "🎯 조건 · 단계 설정", "reward": "🎁 보상 설정",
                  "edit": "✏️ 이름 · 기간 수정", "toggle": "⏸️ 켜기 / 끄기",
                  "delete": "🗑️ 삭제"}
        await interaction.response.send_message(
            embed=discord.Embed(title=titles.get(c, "이벤트 선택"),
                                description="어떤 이벤트에 적용할까요?",
                                color=discord.Color.blurple()),
            view=EventPickView(evs, c), ephemeral=True)


class EventMenuView(discord.ui.View):
    def __init__(self, events: list[dict]):
        super().__init__(timeout=180)
        self.add_item(EventMenuSelect(events))


class DailyGoalModal(discord.ui.Modal, title="☀️ 일일 퀘스트 목표치"):
    chat = discord.ui.TextInput(label="채팅 N회", placeholder="예: 20",
                                required=True, max_length=6)
    channel_chat = discord.ui.TextInput(label="랜덤 채널에서 N회 대화",
                                        placeholder="예: 10", required=True, max_length=6)
    gacha = discord.ui.TextInput(label="뽑기 N회", placeholder="예: 2",
                                 required=True, max_length=4)
    early_bird = discord.ui.TextInput(label="출석 선착순 N등 안에", placeholder="예: 20",
                                      required=True, max_length=5)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            goals = {"chat": int(str(self.chat.value).strip()),
                     "channel_chat": int(str(self.channel_chat.value).strip()),
                     "gacha": int(str(self.gacha.value).strip()),
                     "early_bird": int(str(self.early_bird.value).strip())}
        except ValueError:
            await interaction.response.send_message("⚠️ 숫자만 입력해 주세요.",
                                                    ephemeral=True)
            return
        if any(v < 1 for v in goals.values()):
            await interaction.response.send_message("⚠️ 모두 1 이상이어야 합니다.",
                                                    ephemeral=True)
            return
        await save_quest_settings(interaction.guild.id, {"goals": goals})
        embed = discord.Embed(title="☀️ 일일 목표치 변경 완료",
                              color=discord.Color.green())
        embed.add_field(name="변경된 목표",
                        value=NL.join(f"• {quest_label(k, v)}" for k, v in goals.items()),
                        inline=False)
        embed.set_footer(text="오늘 이미 진행 중인 퀘스트에도 즉시 반영됩니다")
        await refresh_panel(interaction, embed)


class WeeklyGoalModal(discord.ui.Modal, title="📅 주간 퀘스트 목표치"):
    week_checkin = discord.ui.TextInput(label="이번 주 출석 N일 (최대 7)",
                                        placeholder="예: 5", required=True, max_length=2)
    week_chat = discord.ui.TextInput(label="이번 주 채팅 N회", placeholder="예: 150",
                                     required=True, max_length=7)
    week_gacha = discord.ui.TextInput(label="이번 주 뽑기 N회", placeholder="예: 10",
                                      required=True, max_length=5)
    week_channel = discord.ui.TextInput(label="이번 주 랜덤 채널 N회",
                                        placeholder="예: 60", required=True, max_length=6)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            goals = {"week_checkin": int(str(self.week_checkin.value).strip()),
                     "week_chat": int(str(self.week_chat.value).strip()),
                     "week_gacha": int(str(self.week_gacha.value).strip()),
                     "week_channel": int(str(self.week_channel.value).strip())}
        except ValueError:
            await interaction.response.send_message("⚠️ 숫자만 입력해 주세요.",
                                                    ephemeral=True)
            return
        if any(v < 1 for v in goals.values()):
            await interaction.response.send_message("⚠️ 모두 1 이상이어야 합니다.",
                                                    ephemeral=True)
            return
        if goals["week_checkin"] > 7:
            await interaction.response.send_message(
                "⚠️ 주간 출석은 7일을 넘을 수 없습니다.", ephemeral=True)
            return
        await save_quest_settings(interaction.guild.id, {"goals": goals})
        embed = discord.Embed(title="📅 주간 목표치 변경 완료",
                              color=discord.Color.green())
        embed.add_field(name="변경된 목표",
                        value=NL.join(f"• {quest_label(k, v)}" for k, v in goals.items()),
                        inline=False)
        await refresh_panel(interaction, embed)


class CountRewardModal(discord.ui.Modal, title="💰 보상 · 개수"):
    daily = discord.ui.TextInput(label="일일 퀘스트 1개당 보상 (P)",
                                 placeholder="예: 150", required=True, max_length=7)
    count = discord.ui.TextInput(label="하루에 제시할 개수 (1~5)",
                                 placeholder="예: 3", required=True, max_length=2)
    weekly = discord.ui.TextInput(label="주간 퀘스트 1개당 보상 (P)",
                                  placeholder="예: 800", required=True, max_length=7)
    wcount = discord.ui.TextInput(label="주마다 제시할 개수 (1~4)",
                                  placeholder="예: 2", required=True, max_length=2)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            d = int(str(self.daily.value).strip())
            c = int(str(self.count.value).strip())
            w = int(str(self.weekly.value).strip())
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
        embed = discord.Embed(title="💰 보상 · 개수 변경 완료",
                              color=discord.Color.green())
        embed.add_field(name="일일", value=f"{c}개 × {d:,} P", inline=True)
        embed.add_field(name="주간", value=f"{wc}개 × {w:,} P", inline=True)
        embed.add_field(name="📊 하루 최대 수급 (1인)",
                        value=f"일일 **{d * c:,} P** + 주간 분할분", inline=False)
        embed.set_footer(text="개수 변경은 내일부터 반영됩니다 (오늘 목록은 이미 정해졌습니다)")
        await refresh_panel(interaction, embed)


class DailySettingsSelect(discord.ui.Select):
    def __init__(self, settings: dict, guild: discord.Guild):
        chans = quest_channels(settings)
        g = settings.get("goals", {})
        super().__init__(
            placeholder="일일 퀘스트 설정...",
            options=[
                discord.SelectOption(
                    label="목표치", value="goals", emoji="🎯",
                    description=(f"채팅 {g.get('chat', 0)} · 채널 {g.get('channel_chat', 0)} · "
                                 f"뽑기 {g.get('gacha', 0)} · 선착순 {g.get('early_bird', 0)}")[:95]),
                discord.SelectOption(
                    label="랜덤 채널 후보", value="channel", emoji="🎲",
                    description=(f"현재 {len(chans)}개 — 유저마다 무작위 배정" if chans
                                 else "미설정 — 채널 퀘스트는 출제되지 않습니다")[:95]),
            ])

    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "goals":
            await interaction.response.send_modal(DailyGoalModal())
            return
        settings = await get_quest_settings(interaction.guild.id)
        chans = quest_channels(settings)
        cur = []
        for cid in chans:
            c = interaction.guild.get_channel(int(cid))
            cur.append(c.mention if c else f"`#{cid}`")
        embed = discord.Embed(
            title="🎲 랜덤 채널 후보",
            description="채널 퀘스트가 쓸 후보입니다. "
                        "**유저마다 이 중 하나가 무작위로** 배정됩니다.",
            color=discord.Color.blurple())
        embed.add_field(name=f"📋 현재 후보 ({len(chans)}개)",
                        value=("  ".join(cur)[:1024] if cur
                               else "없음 — 채널 퀘스트가 출제되지 않습니다"),
                        inline=False)
        embed.add_field(name="💡 참고",
                        value="여러 개를 고르면 목록이 통째로 교체됩니다." + NL
                              + "후보가 많을수록 사람들이 여러 채널로 흩어집니다.",
                        inline=False)
        await interaction.response.send_message(
            embed=embed, view=QuestChannelView("channel_id", "랜덤 채널"),
            ephemeral=True)


class DailySettingsView(discord.ui.View):
    def __init__(self, settings: dict, guild: discord.Guild):
        super().__init__(timeout=180)
        self.add_item(DailySettingsSelect(settings, guild))


class WeeklySettingsSelect(discord.ui.Select):
    def __init__(self, settings: dict):
        g = settings.get("goals", {})
        super().__init__(
            placeholder="주간 퀘스트 설정...",
            options=[
                discord.SelectOption(
                    label="목표치", value="goals", emoji="🎯",
                    description=(f"출석 {g.get('week_checkin', 0)}일 · "
                                 f"채팅 {g.get('week_chat', 0)} · "
                                 f"뽑기 {g.get('week_gacha', 0)} · "
                                 f"채널 {g.get('week_channel', 0)}")[:95]),
            ])

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(WeeklyGoalModal())


class WeeklySettingsView(discord.ui.View):
    def __init__(self, settings: dict):
        super().__init__(timeout=180)
        self.add_item(WeeklySettingsSelect(settings))


class GoalSettingsSelect(discord.ui.Select):
    def __init__(self, settings: dict, guild: discord.Guild):
        gch = ((guild.get_channel_or_thread(int(settings["goal_channel_id"]))
                or guild.get_channel(int(settings["goal_channel_id"])))
               if settings.get("goal_channel_id") else None)
        super().__init__(
            placeholder="공동 목표 설정...",
            options=[
                discord.SelectOption(
                    label="켜기 / 끄기", value="toggle", emoji="⚡",
                    description=("현재: 켜짐" if settings.get("goal_enabled")
                                 else "현재: 꺼짐")),
                discord.SelectOption(
                    label="수치 설정", value="numbers", emoji="📊",
                    description=(f"{settings.get('goal_target', 0):,}회→"
                                 f"{settings.get('goal_reward', 0):,}P·"
                                 f"최소{goal_min_chat(settings):,}회·"
                                 f"{settings.get('goal_reset_days', 1)}일 주기")[:95]),
                discord.SelectOption(
                    label="안내 채널", value="channel", emoji="📢",
                    description=(f"현재: #{gch.name}" if gch else "미설정")[:95]),
            ])

    async def callback(self, interaction: discord.Interaction):
        c = self.values[0]
        settings = await get_quest_settings(interaction.guild.id)
        if c == "numbers":
            await interaction.response.send_modal(ServerGoalModal())
        elif c == "channel":
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="📢 공동 목표 안내 채널",
                    description="진행도 임베드 하나가 2분마다 갱신됩니다." + NL
                                + "메시지 수정이라 알림은 울리지 않습니다.",
                    color=discord.Color.blurple()),
                view=QuestChannelView("goal_channel_id", "공동 목표 안내"),
                ephemeral=True)
        else:
            new = not settings.get("goal_enabled", False)
            await save_quest_settings(interaction.guild.id, {"goal_enabled": new})
            embed = discord.Embed(
                title=f"🤝 서버 공동 목표 {'활성화' if new else '비활성화'}",
                color=discord.Color.green() if new else discord.Color.red())
            if new and not settings.get("goal_channel_id"):
                embed.add_field(name="⚠️ 안내 채널이 없습니다",
                                value="`📢 안내 채널` 을 지정해야 진행도가 보입니다.",
                                inline=False)
            await refresh_panel(interaction, embed)


class GoalSettingsView(discord.ui.View):
    def __init__(self, settings: dict, guild: discord.Guild):
        super().__init__(timeout=180)
        self.add_item(GoalSettingsSelect(settings, guild))


class QuestSettingsSelect(discord.ui.Select):
    def __init__(self, settings: dict, guild: discord.Guild):
        super().__init__(
            placeholder="무엇을 설정할까요?",
            options=[
                discord.SelectOption(
                    label="퀘스트 켜기 / 끄기", value="toggle", emoji="⚡",
                    description=("현재: 켜짐" if settings.get("enabled")
                                 else "현재: 꺼짐")),
                discord.SelectOption(
                    label="관리자 전용 모드", value="adminonly", emoji="🔧",
                    description=("현재: 켜짐 — 관리자만 /퀘스트 사용 가능"
                                 if settings.get("admin_only")
                                 else "현재: 꺼짐 — 모두 사용 가능")),
                discord.SelectOption(
                    label="보상 · 개수", value="reward", emoji="💰",
                    description=(f"일일 {settings.get('daily_count', 0)}개×"
                                 f"{settings.get('daily_reward', 0)} P · "
                                 f"주간 {settings.get('weekly_count', 0)}개×"
                                 f"{settings.get('weekly_reward', 0)} P")[:95]),
                discord.SelectOption(
                    label="☀️ 일일 퀘스트 설정", value="daily", emoji="☀️",
                    description="목표치 · 랜덤 채널 후보"),
                discord.SelectOption(
                    label="📅 주간 퀘스트 설정", value="weekly", emoji="📅",
                    description="주간 목표치"),
                discord.SelectOption(
                    label="🤝 공동 목표 설정", value="goal", emoji="🤝",
                    description=("켜짐 · " if settings.get("goal_enabled") else "꺼짐 · ")
                                + f"채팅 {settings.get('goal_target', 0):,}회"),
                discord.SelectOption(
                    label="🎉 이벤트 퀘스트", value="event", emoji="🎉",
                    description="기간 이벤트 만들기 · 보상으로 역할도 줍니다"),
                discord.SelectOption(
                    label="현재 설정 확인", value="view", emoji="📋",
                    description="지금 값으로 하루에 얼마가 나가는지 봅니다."),
            ])

    async def callback(self, interaction: discord.Interaction):
        c = self.values[0]
        guild = interaction.guild
        settings = await get_quest_settings(guild.id)

        if c == "toggle":
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
                embed.add_field(name="📊 하루 최대 수급 (1인)",
                                value=f"약 **{per_day:,} P**" + NL
                                      + "-# 주간 · 공동 목표 · 이벤트는 별도입니다.",
                                inline=False)
                if settings.get("admin_only"):
                    embed.add_field(
                        name="🔧 관리자 전용 모드가 켜져 있습니다",
                        value="지금은 **관리자만** `/퀘스트` 를 쓸 수 있습니다." + NL
                              + "공개하려면 이 모드를 꺼 주세요.",
                        inline=False)
            await refresh_panel(interaction, embed)

        elif c == "adminonly":
            new = not settings.get("admin_only", False)
            await save_quest_settings(guild.id, {"admin_only": new})
            embed = discord.Embed(
                title=f"🔧 관리자 전용 모드 {'켜짐' if new else '꺼짐'}",
                color=discord.Color.orange() if new else discord.Color.green())
            embed.add_field(
                name="🔎 이렇게 동작합니다",
                value=("관리자만 `/퀘스트` 를 쓸 수 있습니다." + NL
                       + "일반 유저에게는 **꺼진 것과 똑같이** 보여, 준비 중이라는"
                       + " 사실이 드러나지 않습니다." + NL
                       + "-# 진행도는 평소처럼 쌓이므로 공개 후 그대로 이어집니다."
                       if new else
                       "모든 유저가 `/퀘스트` 를 쓸 수 있습니다."),
                inline=False)
            # 이 모드만 켜 두면 아무 일도 일어나지 않습니다. 바로 알려줍니다.
            if new and not settings.get("enabled"):
                embed.add_field(
                    name="⚠️ 아직 퀘스트가 꺼져 있습니다",
                    value="이 모드는 **퀘스트가 켜져 있을 때만** 동작합니다."
                          + NL + "`⚡ 퀘스트 켜기` 까지 눌러야 관리자끼리 "
                          "테스트할 수 있습니다.",
                    inline=False)
            await refresh_panel(interaction, embed)

        elif c == "reward":
            await interaction.response.send_modal(CountRewardModal())

        elif c == "daily":
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="☀️ 일일 퀘스트 설정",
                    description="매일 무작위로 뽑히는 퀘스트의 목표치와 채널을 정합니다.",
                    color=discord.Color.blurple()),
                view=DailySettingsView(settings, guild), ephemeral=True)

        elif c == "weekly":
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="📅 주간 퀘스트 설정",
                    description="일요일 밤 12시에 새로 시작되는 주간 퀘스트입니다.",
                    color=discord.Color.blurple()),
                view=WeeklySettingsView(settings), ephemeral=True)

        elif c == "goal":
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="🤝 서버 공동 목표 설정",
                    description="서버 전체가 함께 채우는 목표입니다.",
                    color=discord.Color.blurple()),
                view=GoalSettingsView(settings, guild), ephemeral=True)

        elif c == "event":
            events = await list_events(guild.id)
            active = sum(1 for e in events if event_is_active(e))
            embed = discord.Embed(
                title="🎉 이벤트 퀘스트",
                description="기간을 정해 여는 특별 퀘스트입니다. "
                            "보상으로 **포인트와 역할**을 줄 수 있습니다.",
                color=discord.Color.blurple())
            embed.add_field(name="현재", value=f"전체 {len(events)}개 · 진행 중 {active}개",
                            inline=True)
            embed.add_field(
                name="🧩 두 가지 방식",
                value="**단계별** — 하나의 조건을 여러 단계로" + NL
                      + "　예) 채팅 100회 → 500 P, 300회 → 1,500 P, 1000회 → 역할" + NL
                      + "**모두달성** — 여러 조건을 전부 깨야 보상" + NL
                      + "　예) 채팅 200 + 뽑기 10 + 출석 5 → 2,000 P + 역할",
                inline=False)
            await interaction.response.send_message(
                embed=embed, view=EventMenuView(events), ephemeral=True)

        else:
            chans = quest_channels(settings)
            cur = []
            for cid in chans:
                ch = guild.get_channel(int(cid))
                cur.append(ch.mention if ch else f"`#{cid}`")
            gch = ((guild.get_channel_or_thread(int(settings["goal_channel_id"]))
                    or guild.get_channel(int(settings["goal_channel_id"])))
                   if settings.get("goal_channel_id") else None)
            goals = settings.get("goals", {})
            events = await list_events(guild.id)
            embed = discord.Embed(title="📋 현재 퀘스트 설정",
                                  color=discord.Color.blurple())
            embed.add_field(name="상태",
                            value=("켜짐" if settings.get("enabled") else "꺼짐")
                                  + (" · 🔧 관리자 전용" if settings.get("admin_only") else ""),
                            inline=False)
            embed.add_field(name="☀️ 일일",
                            value=f"{settings.get('daily_count', 0)}개 × "
                                  f"{settings.get('daily_reward', 0):,} P", inline=True)
            embed.add_field(name="📅 주간",
                            value=f"{settings.get('weekly_count', 0)}개 × "
                                  f"{settings.get('weekly_reward', 0):,} P", inline=True)
            embed.add_field(name="🎉 이벤트",
                            value=f"{sum(1 for e in events if event_is_active(e))}개 진행 중",
                            inline=True)
            embed.add_field(
                name="🎯 목표치",
                value=NL.join(f"• {quest_label(k, int(v))}"
                              for k, v in goals.items() if k in QUEST_KINDS)[:1024],
                inline=False)
            embed.add_field(name="🎲 랜덤 채널 후보",
                            value=("  ".join(cur)[:1024] if cur
                                   else "미설정 (채널 퀘스트 출제 안 됨)"),
                            inline=False)
            embed.add_field(
                name="🤝 공동 목표",
                value=(f"{'켜짐' if settings.get('goal_enabled') else '꺼짐'} · "
                       f"채팅 {settings.get('goal_target', 0):,}회 → "
                       f"{settings.get('goal_reward', 0):,} P" + NL
                       + "-# 본인 최소 참여: "
                       + (f"{goal_min_chat(settings):,}회 "
                          f"({settings.get('goal_min_percent', 0)}%)"
                          if goal_min_chat(settings) else "조건 없음") + NL
                       + f"-# 안내: {gch.mention if gch else '미설정'}"),
                inline=False)
            daily_max = (int(settings.get("daily_reward", 0))
                         * int(settings.get("daily_count", 1)))
            weekly_sum = (int(settings.get("weekly_reward", 0))
                          * int(settings.get("weekly_count", 2)))
            embed.add_field(
                name="📊 1인 최대 수급 (추정)",
                value=f"하루 **{daily_max:,} P** · 주 **{daily_max * 7 + weekly_sum:,} P**"
                      + NL
                      + f"-# 공동 목표 달성 시 하루 +{settings.get('goal_reward', 0):,} P"
                      + NL + "-# 이벤트는 별도입니다.",
                inline=False)
            await interaction.response.send_message(embed=embed, ephemeral=True)


class QuestSettingsView(discord.ui.View):
    def __init__(self, settings: dict, guild: discord.Guild):
        super().__init__(timeout=180)
        self.add_item(QuestSettingsSelect(settings, guild))


class Quest(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._goal_dirty: dict[int, bool] = {}   # guild_id → 갱신 필요 여부
        self._goal_last_update: dict[int, float] = {}  # guild_id → 마지막 갱신 시각
        self.goal_refresh.start()
        self._goal_realtime.start()

    def cog_unload(self):
        self.goal_refresh.cancel()
        self._goal_realtime.cancel()

    def notify_goal_changed(self, guild_id: int) -> None:
        """외부 cog에서 공동 목표 진행도가 변했을 때 호출합니다."""
        self._goal_dirty[guild_id] = True

    @tasks.loop(seconds=3)
    async def _goal_realtime(self):
        """dirty 플래그가 켜진 길드만 쿨다운 후 갱신합니다.

        3초마다 확인하되, 마지막 갱신으로부터 GOAL_UPDATE_COOLDOWN 초가
        지나지 않았으면 건너뜁니다. 채팅이 몰려도 10초에 1회만 수정합니다.
        """
        await self.bot.wait_until_ready()
        if health.goal_backoff.active():
            return
        now = time.monotonic()
        for guild_id in list(self._goal_dirty):
            if not self._goal_dirty.get(guild_id):
                continue
            last = self._goal_last_update.get(guild_id, 0)
            if now - last < GOAL_UPDATE_COOLDOWN:
                continue   # 쿨다운 중
            guild = self.bot.get_guild(guild_id)
            if guild is None:
                self._goal_dirty.pop(guild_id, None)
                continue
            try:
                await self.update_goal_message(guild)
                health.goal_backoff.reset()
                self._goal_last_update[guild_id] = time.monotonic()
            except Exception as e:
                print(f"[Quest] {guild.name}: 실시간 목표 갱신 실패 - "
                      f"{type(e).__name__}: {e}", flush=True)
            finally:
                self._goal_dirty[guild_id] = False

    @_goal_realtime.error
    async def _goal_realtime_error(self, error):
        await health.revive_loop(self._goal_realtime, "_goal_realtime", error)

    # ── 공동 목표 ────────────────────────────────────────────────────

    @tasks.loop(seconds=GOAL_REFRESH_SECONDS)
    async def goal_refresh(self):
        """공동 목표 진행도를 하나의 메시지로 계속 갱신합니다.

        매번 새로 보내면 채널이 도배됩니다. 메시지 ID 를 저장해 두고
        **수정**하므로 알림이 울리지 않습니다.

        429 를 만나면 백오프에 들어가 시도 자체를 멈춥니다.
        성공하면 백오프를 즉시 초기화합니다.
        """
        await self.bot.wait_until_ready()
        if health.goal_backoff.active():
            return   # 429 백오프 중 — 시도하지 않습니다
        for guild in self.bot.guilds:
            try:
                await self.update_goal_message(guild)
                health.goal_backoff.reset()
            except Exception as e:
                print(f"[Quest] {guild.name}: 공동 목표 갱신 실패 - "
                      f"{type(e).__name__}: {e}", flush=True)

    # 주기 작업은 예상 못한 예외가 **한 번** 나면 그대로 영구히 멈춥니다.
    # 재시도도 알림도 없고, 프로세스는 멀쩡해 호스팅도 감지하지 못합니다.
    # 무인 운영에서는 몇 달 뒤에야 발견되므로 스스로 되살립니다.
    @goal_refresh.error
    async def goal_refresh_error(self, error):
        await health.revive_loop(self.goal_refresh, "goal_refresh", error)

    async def update_goal_message(self, guild) -> None:
        settings = await get_quest_settings(guild.id)
        if not (settings.get("enabled") and settings.get("goal_enabled")):
            return
        channel_id = settings.get("goal_channel_id")
        if not channel_id:
            return
        channel = (guild.get_channel_or_thread(int(channel_id))
                   or guild.get_channel(int(channel_id)))
        if channel is None or not channel.permissions_for(guild.me).send_messages:
            return
        # 스레드가 보관(archived)되어 있으면 되살립니다.
        if isinstance(channel, discord.Thread) and channel.archived:
            try:
                await channel.edit(archived=False)
            except discord.HTTPException:
                return  # 권한 부족 등으로 되살리지 못하면 포기

        state = await get_server_goal(guild.id, reset_days=settings.get("goal_reset_days", 1))
        target = int(settings.get("goal_target", 0) or 0)
        progress = state["progress"]
        reached = target > 0 and progress >= target

        need = goal_min_chat(settings)
        desc = (f"서버 전체가 함께 **채팅 {target:,}회** 를 채우면\n"
                f"**+{int(settings.get('goal_reward', 0)):,} P** 가 지급됩니다.")
        if need > 0:
            desc += (f"\n\n단, 본인도 **{need:,}회 이상** 대화해야 받을 수 있습니다."
                     f"\n-# 목표치의 {settings.get('goal_min_percent', 0)}% 입니다. "
                     "다 같이 채운 목표라 최소한의 참여를 요구합니다.")
        embed = discord.Embed(
            title="🤝 오늘의 서버 공동 목표",
            description=desc,
            color=discord.Color.gold() if reached else discord.Color.blurple())
        pct = (progress / target * 100) if target else 0
        embed.add_field(
            name=("🎉 달성!" if reached else "📊 진행 상황"),
            value=f"`{progress:,} / {target:,}`  ({pct:.0f}%)\n{_bar(progress, target, 20)}",
            inline=False)
        if reached:
            embed.add_field(
                name="🎁 보상 받는 법",
                value="`/퀘스트` 를 열어 **[보상 받기]** 를 눌러 주세요."
                      + (f"\n-# 오늘 **{need:,}회 이상** 대화한 분만 받을 수 있습니다."
                         if need > 0 else ""),
                inline=False)
        rd = int(settings.get("goal_reset_days", 1))
        reset_text = f"매일 밤 12시" if rd <= 1 else f"{rd}일마다 자정"
        embed.set_footer(text=f"{reset_text}(KST)에 새로 시작됩니다")
        embed.timestamp = discord.utils.utcnow()

        if state["message_id"]:
            try:
                msg = await channel.fetch_message(int(state["message_id"]))
                await msg.edit(embed=embed)
                return
            except discord.NotFound:
                pass            # 지워졌으니 새로 만듭니다
            except discord.HTTPException as e:
                if e.status == 429:
                    health.mark_rate_limited()
                    health.goal_backoff.hit()
                    print(f"[Quest] 429 레이트 리밋 — "
                          f"{health.goal_backoff.delay:.0f}초 백오프", flush=True)
                else:
                    print(f"[Quest] 공동 목표 수정 실패: {e}", flush=True)
                return
        try:
            msg = await channel.send(embed=embed)
        except discord.HTTPException as e:
            print(f"[Quest] 공동 목표 생성 실패: {e}", flush=True)
            return
        await mark_server_goal(
            guild.id, 
            reset_days=settings.get("goal_reset_days", 1), 
            goal_message_id=str(msg.id)
        )

    # ── 유저용 ───────────────────────────────────────────────────────

    @app_commands.command(
        name="퀘스트",
        description="오늘의 퀘스트와 진행 상황을 확인하고 보상을 받습니다."
    )
    async def quest(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = interaction.guild
        settings = await get_quest_settings(guild.id)

        is_admin = interaction.user.guild_permissions.administrator

        # 일반 유저에게 나가는 문구는 **어느 이유든 똑같아야** 합니다.
        # 다르면 "지금 준비 중이구나"가 드러납니다.
        closed = discord.Embed(
            title="⚠️ 퀘스트가 아직 열리지 않았습니다",
            description="관리자가 설정을 마치면 이용하실 수 있습니다.",
            color=discord.Color.orange())

        if not settings.get("enabled"):
            # 관리자에게 같은 문구를 주면 무엇을 해야 할지 알 수 없습니다.
            # 관리자 전용 모드를 켜 둔 채 여기 걸리는 경우가 특히 헷갈립니다.
            if is_admin:
                hint = discord.Embed(
                    title="⚙️ 퀘스트가 꺼져 있습니다",
                    description="`/퀘스트설정` → **⚡ 퀘스트 켜기** 를 눌러 주세요.",
                    color=discord.Color.orange())
                if settings.get("admin_only"):
                    hint.add_field(
                        name="🔧 관리자 전용 모드는 켜져 있습니다",
                        value="이 모드는 **퀘스트가 켜져 있을 때만** 동작하는 "
                              "필터입니다." + NL
                              + "퀘스트를 켜면 관리자만 보이는 상태로 시작합니다.",
                        inline=False)
                await interaction.followup.send(embed=hint, ephemeral=True)
                return
            await interaction.followup.send(embed=closed, ephemeral=True)
            return

        # 공개 전에 관리자끼리 돌려보기 위한 모드입니다. 일반 유저에게는
        # 꺼진 것과 똑같이 보여, 준비 중이라는 사실이 드러나지 않습니다.
        if settings.get("admin_only") and not is_admin:
            await interaction.followup.send(embed=closed, ephemeral=True)
            return

        events = await active_events(guild.id)
        dailies = await get_daily_quests(guild.id, settings, interaction.user.id)
        weeklies = weekly_quests(settings, guild.id, interaction.user.id)
        progress = await get_quest_progress(
            guild.id, interaction.user.id,
            [event_period_key(e["id"]) for e in events])

        embed = discord.Embed(
            title="📋 나의 퀘스트",
            description="완료한 퀘스트는 아래 **[보상 받기]** 로 한 번에 받을 수 있습니다.",
            color=discord.Color.blurple())
        if settings.get("admin_only"):
            embed.description += "\n-# 🔧 지금은 **관리자 전용 테스트 모드**입니다."

        claimable = []

        if dailies:
            lines = []
            for q in dailies:
                text, done, claimed = _line(q, progress, guild)
                lines.append(text)
                if done and not claimed:
                    claimable.append({
                        "id": q["id"],
                        "name": quest_label(q["kind"], q["goal"],
                                            _channel_text(guild, q)),
                        "points": int(settings.get("daily_reward", 0) or 0),
                        "role_id": None, "weekly": False, "period": None})
            embed.add_field(
                name=f"☀️ 오늘의 퀘스트  ·  개당 +{int(settings.get('daily_reward', 0)):,} P",
                value=NL.join(lines), inline=False)

        if weeklies:
            lines = []
            for q in weeklies:
                text, done, claimed = _line(q, progress, guild)
                lines.append(text)
                if done and not claimed:
                    claimable.append({
                        "id": q["id"],
                        "name": quest_label(q["kind"], q["goal"],
                                            _channel_text(guild, q)),
                        "points": int(settings.get("weekly_reward", 0) or 0),
                        "role_id": None, "weekly": True, "period": None})
            embed.add_field(
                name=f"📅 이번 주 퀘스트 (일~토)  ·  개당 +{int(settings.get('weekly_reward', 0)):,} P",
                value=NL.join(lines), inline=False)

        # ── 이벤트 ──────────────────────────────────────────────────
        for ev in events:
            st = event_status(ev, progress)
            period = event_period_key(ev["id"])
            lines = []
            for r in st["rows"]:
                if r["claimed"]:
                    mark, tail = "✅", "  **수령 완료**"
                elif r["done"]:
                    mark = "🎁"
                    tail = ("  **← 받을 수 있습니다**" if st["mode"] == "tier" else "")
                else:
                    mark, tail = "▫️", ""
                reward_txt = []
                if r["points"]:
                    reward_txt.append(f"+{r['points']:,} P")
                if r["role_id"]:
                    role = guild.get_role(int(r["role_id"]))
                    reward_txt.append(role.mention if role else f"`역할 {r['role_id']}`")
                suffix = ("  →  " + " · ".join(reward_txt)) if reward_txt else ""
                lines.append(f"{mark} **{r['label']}**{suffix}")
                lines.append(f"　　`{min(r['value'], r['goal']):,}/{r['goal']:,}`  "
                             f"{_bar(r['value'], r['goal'])}{tail}")
                if st["mode"] == "tier" and r["done"] and not r["claimed"]:
                    claimable.append({
                        "id": r["id"], "name": f"{ev['name']} — {r['label']}",
                        "points": r["points"], "role_id": r["role_id"],
                        "weekly": False, "period": period})

            if st["mode"] == "all":
                reward_txt = []
                if st["points"]:
                    reward_txt.append(f"+{st['points']:,} P")
                if st["role_id"]:
                    role = guild.get_role(int(st["role_id"]))
                    reward_txt.append(role.mention if role else f"`역할 {st['role_id']}`")
                got = st["reward_claimed"]
                head = "✅" if got else ("🎁" if st["all_done"] else "▫️")
                lines.append("")
                lines.append(f"{head} **전부 달성 시**  →  "
                             + (" · ".join(reward_txt) or "보상 없음")
                             + ("  **수령 완료**" if got else
                                ("  **← 받을 수 있습니다**" if st["all_done"] else "")))
                if st["all_done"] and not got:
                    claimable.append({
                        "id": st["reward_id"], "name": f"{ev['name']} — 전부 달성",
                        "points": st["points"], "role_id": st["role_id"],
                        "weekly": False, "period": period})

            span = ""
            if ev.get("ends"):
                span = f"  ·  ~{ev['ends']}"
            embed.add_field(name=f"🎉 {ev['name']}{span}",
                            value=NL.join(lines)[:1024], inline=False)

        # ── 공동 목표 ───────────────────────────────────────────────
        if settings.get("goal_enabled"):
            state = await get_server_goal(guild.id, reset_days=settings.get("goal_reset_days", 1))
            target = int(settings.get("goal_target", 0) or 0)
            prog = state["progress"]
            reached = target > 0 and prog >= target
            claimed = "g:server" in (progress.get("claimed") or [])

            # 다 같이 채운 목표인데 아무것도 안 한 사람이 똑같이 받으면
            # 김이 빠집니다. 목표치의 일정 비율은 본인이 채워야 합니다.
            need = goal_min_chat(settings)
            mine = int((progress.get("p", {}) or {}).get("chat", 0) or 0)
            eligible = mine >= need

            if reached and eligible and not claimed:
                claimable.append({
                    "id": "g:server", "name": "🤝 서버 공동 목표",
                    "points": int(settings.get("goal_reward", 0) or 0),
                    "role_id": None, "weekly": False, "period": None})

            if claimed:
                mark, tail = "✅", "  **수령 완료**"
            elif reached and eligible:
                mark, tail = "🎁", "  **← 받을 수 있습니다**"
            elif reached:
                mark, tail = "🔒", "  **← 참여 조건 미달**"
            else:
                mark, tail = "▫️", ""

            lines = [f"{mark} **서버 전체 채팅 {target:,}회**",
                     f"　　`{min(prog, target):,}/{target:,}`  {_bar(prog, target)}{tail}"]
            if need > 0:
                ok = "✅" if eligible else "❌"
                lines.append(f"　　{ok} 내 채팅 `{min(mine, need):,}/{need:,}`"
                             f"  -# 목표의 {settings.get('goal_min_percent', 0)}% 이상 참여해야 받습니다")
            embed.add_field(name="🤝 서버 공동 목표", value=NL.join(lines),
                            inline=False)

        embed.set_footer(text="퀘스트는 사람마다 다릅니다 · 일일은 매일 밤 12시(KST), "
                              "주간은 일요일 밤 12시에 새로 시작됩니다")
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
