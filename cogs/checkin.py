import asyncio
import re
import discord
from discord.ext import commands
from discord import app_commands
from utils.database import (
    checkin_user, get_user_points, get_top_users, get_user_rank,
    is_user_blacklisted, add_user_points, deduct_user_points, add_activity_log,
    get_checkin_settings, save_checkin_settings, streak_reward, next_cycle_day,
    bump_quest_progress, take_checkin_rank, bump_event_progress,
)

# ── 포인트 랭킹 (30등까지, 10명씩 3쪽) ──────────────────────────────

RANK_PAGE_SIZE = 10
RANK_PAGES = 3
RANK_LIMIT = RANK_PAGE_SIZE * RANK_PAGES
RANK_MEDALS = ["🥇", "🥈", "🥉"]


async def resolve_members(guild, ids: list[int]) -> dict:
    """표시용 멤버를 모읍니다. **캐시에 없는 사람만 한꺼번에** 받아옵니다.

    ⚠️ 예전에는 빠진 사람마다 `fetch_member` 를 **순서대로** 불렀습니다.
    REST 왕복 하나에 100~200ms 라 10명이면 최대 2초, 30명이면 6초입니다 —
    그 구조로 `/포인트랭킹` 이 실제로 3초 제한을 넘겨 죽었습니다.
    한꺼번에 보내면 30명이라도 왕복 한 번 값으로 끝납니다.

    대부분은 캐시에 있습니다(SERVER MEMBERS INTENT). 캐시에 없는 사람은
    보통 **서버를 나간 사람**이라 `fetch_member` 도 실패하고, 그러면
    '서버 퇴장' 으로 표시됩니다.
    """
    out = {uid: guild.get_member(uid) for uid in ids}
    missing = [uid for uid, m in out.items() if m is None]
    if missing:
        got = await asyncio.gather(
            *[guild.fetch_member(uid) for uid in missing],
            return_exceptions=True)
        for uid, m in zip(missing, got):
            out[uid] = m if not isinstance(m, BaseException) else None
    return out


async def build_rank_page(interaction: discord.Interaction, page: int):
    """한 쪽을 그립니다. (임베드, 전체 인원) 을 돌려줍니다.

    30명을 한 번에 읽고 잘라 씁니다 — 쪽마다 따로 읽으면 그 사이 순위가
    바뀌어 **같은 사람이 두 쪽에 나오거나 아무 쪽에도 없게** 됩니다.
    """
    guild = interaction.guild
    rows = await get_top_users(guild.id, limit=RANK_LIMIT)
    my_rank, my_points = await get_user_rank(guild.id, interaction.user.id)
    total = len(rows)

    if not rows:
        embed = discord.Embed(
            title="🏆 포인트 랭킹 TOP 30",
            description="아직 등록된 포인트 랭킹 정보가 없습니다.",
            color=discord.Color.gold())
        return embed, 0

    page = max(0, min(page, RANK_PAGES - 1))
    start = page * RANK_PAGE_SIZE
    chunk = rows[start:start + RANK_PAGE_SIZE]

    members = await resolve_members(guild, [r["user_id"] for r in chunk])

    lines = []
    for offset, info in enumerate(chunk):
        idx = start + offset + 1
        member = members.get(info["user_id"])
        who = (f"**{member.display_name}** ({member.mention})" if member
               else "*알 수 없는 유저 (서버 퇴장)*")
        medal = RANK_MEDALS[idx - 1] if idx <= 3 else f"`{idx}위`"
        lines.append(f"{medal} {who} — **{info['points']:,} P**")

    lo, hi = start + 1, start + len(chunk)
    embed = discord.Embed(
        title="🏆 한바헌 포인트 랭킹 TOP 30",
        description=f"포인트가 가장 많은 상위 30명입니다. "
                    f"(**{lo}~{hi}위** · {page + 1}/{RANK_PAGES}쪽)",
        color=discord.Color.gold())
    embed.add_field(name="📊 보유 포인트 순위", value="\n".join(lines),
                    inline=False)

    my_medal = RANK_MEDALS[my_rank - 1] if my_rank <= 3 else f"`{my_rank}위`"
    embed.add_field(
        name="👤 내 랭킹 정보",
        value=f"• **현재 순위**: {my_medal} (전체 유저 중 {my_rank}위)\n"
              f"• **내 보유 포인트**: **{my_points:,} P**",
        inline=False)
    embed.set_footer(text="아래 버튼으로 쪽을 넘길 수 있습니다 · "
                          "랭킹은 활동에 따라 실시간으로 갱신됩니다.")
    return embed, total


class RankPageButton(discord.ui.Button):
    """쪽 번호를 **custom_id 에 박아** 둡니다.

    영구 View 는 인스턴스를 모두가 공유하므로 `self` 에 '지금 몇 쪽'을
    들고 있으면 남의 화면까지 오염됩니다. 쪽마다 버튼을 따로 두면
    상태를 들 필요가 없고, 재시작 뒤에도 그대로 동작합니다.
    """

    def __init__(self, page: int, active: bool = False, empty: bool = False):
        lo = page * RANK_PAGE_SIZE + 1
        hi = (page + 1) * RANK_PAGE_SIZE
        super().__init__(
            label=f"{lo}~{hi}위",
            style=(discord.ButtonStyle.primary if active
                   else discord.ButtonStyle.secondary),
            # 보고 있는 쪽과 **사람이 없는 쪽**은 누를 수 없습니다.
            disabled=active or empty,
            custom_id=f"rank:page:{page}")
        self._page = page

    async def callback(self, interaction: discord.Interaction):
        # 컴포넌트 상호작용이라 `thinking` 없이 **편집을 예약**합니다.
        # 새 메시지를 보내면 쪽을 넘길 때마다 화면이 쌓입니다.
        await interaction.response.defer()
        embed, total = await build_rank_page(interaction, self._page)
        await interaction.edit_original_response(
            embed=embed, view=RankView(self._page, total))


class RankView(discord.ui.View):
    """영구 View — 재시작 뒤에도 쪽 버튼이 살아 있어야 합니다."""

    def __init__(self, page: int = 0, total: int = RANK_LIMIT):
        super().__init__(timeout=None)
        for p in range(RANK_PAGES):
            # 그 쪽에 사람이 한 명도 없으면 눌러도 빈 화면이라 잠급니다.
            self.add_item(RankPageButton(
                p, active=(p == page), empty=(total <= p * RANK_PAGE_SIZE)))


def _next_goal(streak: int, settings: dict) -> tuple[int, int] | None:
    """다음에 올 목표(주기 달성일 또는 특별 보상) 중 가장 가까운 것."""
    upcoming = []
    cyc = next_cycle_day(streak, settings)
    if cyc:
        upcoming.append(cyc)
    for k, v in (settings.get("milestones") or {}).items():
        try:
            day = int(k)
        except (TypeError, ValueError):
            continue
        if day > streak and int(v or 0) > 0:
            upcoming.append((day, int(v)))
    return min(upcoming) if upcoming else None


class PointGuideSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(
                label="매일 출석체크",
                value="checkin",
                description="/출석 명령어로 하루 한번 포인트 획득",
                emoji="📅"
            ),
            discord.SelectOption(
                label="서버 채팅 활동",
                value="chat",
                description="서버 채팅 작성 시 무제한 자동 적립",
                emoji="💬"
            ),
            discord.SelectOption(
                label="기타 및 서버 이벤트",
                value="event",
                description="이벤트 및 관리자 보상 획득",
                emoji="🎁"
            ),
        ]
        super().__init__(placeholder="궁금한 포인트 획득 항목을 선택하세요...", options=options)

    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "checkin":
            embed = discord.Embed(
                title="📅 [매일 출석체크로 포인트 얻기]",
                description="• **/출석** 명령어를 사용하면 하루에 한 번 **100포인트**를 획득하실 수 있습니다!\n\n"
                            "• 매일 밤 12시 (한국 시간 기준)에 출석 기록이 리셋됩니다.\n"
                            "• 명령어를 친 본인에게만 비공개로 결과가 표시됩니다.",
                color=discord.Color.green()
            )
        elif self.values[0] == "chat":
            embed = discord.Embed(
                title="💬 [서버 채팅 활동으로 포인트 얻기]",
                description="• 서버에서 텍스트 채팅 **10회**를 작성할 때마다 **+10포인트**가 자동으로 적립됩니다!\n\n"
                            "• 하루 일일 상한선(제한) 없이 무제한으로 적립이 가능합니다.\n"
                            "• *(단, 사진/파일/스티커 및 단독 이모티콘 메시지는 카운트에서 제외됩니다)*",
                color=discord.Color.blue()
            )
        elif self.values[0] == "event":
            embed = discord.Embed(
                title="🎁 [기타 및 서버 이벤트]",
                description="• 서버 내에서 진행되는 특별 이벤트 및 관리자 보상을 통해 포인트를 획득하실 수 있습니다!\n\n"
                            "• 현재 보유 중인 총 포인트는 **/포인트확인** 명령어로 언제든지 확인 가능합니다.",
                color=discord.Color.purple()
            )
        else:
            return

        await interaction.response.send_message(embed=embed, ephemeral=True)


class PointGuideView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)
        self.add_item(PointGuideSelect())




def _build_checkin_panel(settings: dict) -> discord.Embed:
    """출석 보상 설정 패널. 값이 바뀔 때마다 이걸로 다시 그립니다."""
    embed = discord.Embed(
        title="🔥 연속 출석 보상 설정",
        description="연속으로 출석할수록 더 많이 받도록 조정할 수 있습니다.",
        color=discord.Color.blurple())
    embed.add_field(name="📈 지금 설정으로는", value=_reward_preview(settings),
                    inline=False)
    embed.set_footer(text="하루라도 거르면 연속은 1일로 초기화됩니다")
    return embed


async def _refresh_settings_panel(interaction, view_factory, embed_factory,
                                  result_embed):
    """설정을 바꾼 뒤 패널을 최신 값으로 다시 그립니다.

    이 단계가 없으면 드롭다운 설명이 옛 값 그대로 남습니다
    ('공동 목표를 켰는데 꺼짐으로 나온다' 같은 혼란).

    패널 갱신에 실패해도 변경 결과는 반드시 알려야 하므로 갈라 둡니다.
    """
    try:
        await interaction.response.edit_message(embed=embed_factory(),
                                                view=view_factory())
        await interaction.followup.send(embed=result_embed, ephemeral=True)
    except (discord.HTTPException, discord.InteractionResponded):
        if interaction.response.is_done():
            await interaction.followup.send(embed=result_embed, ephemeral=True)
        else:
            await interaction.response.send_message(embed=result_embed,
                                                    ephemeral=True)


class CheckinRewardModal(discord.ui.Modal, title="🔥 연속 출석 보상 설정"):
    """수치를 코드에 박지 않고 운영진이 직접 조정합니다."""

    base = discord.ui.TextInput(label="매일 기본 지급량 (P)", placeholder="예: 100",
                                required=True, max_length=6)
    cycle = discord.ui.TextInput(label="며칠마다 추가 보상? · 0이면 사용 안 함",
                                 placeholder="예: 7", required=True, max_length=4)
    cycle_step = discord.ui.TextInput(
        label="주기당 증가폭 (P)",
        placeholder="예: 100 -> 7일 +100, 14일 +200, 21일 +300",
        required=True, max_length=7)
    cycle_max = discord.ui.TextInput(label="추가 보상 상한 (P) · 0이면 무제한",
                                     placeholder="예: 0", required=False, max_length=7)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            base = int(str(self.base.value).strip())
            cycle = int(str(self.cycle.value).strip())
            step = int(str(self.cycle_step.value).strip())
            cap = int(str(self.cycle_max.value or "0").strip() or 0)
        except ValueError:
            await interaction.response.send_message(
                "⚠️ 숫자만 입력해 주세요.", ephemeral=True)
            return
        if min(base, cycle, step, cap) < 0:
            await interaction.response.send_message(
                "⚠️ 0 이상만 입력할 수 있습니다.", ephemeral=True)
            return
        if cycle > 365:
            await interaction.response.send_message(
                "⚠️ 주기는 365일 이하로 정해 주세요.", ephemeral=True)
            return

        await save_checkin_settings(interaction.guild.id, {
            "base": base, "cycle": cycle, "cycle_step": step, "cycle_max": cap})
        settings = await get_checkin_settings(interaction.guild.id)

        embed = discord.Embed(title="🔥 연속 출석 보상 변경 완료",
                              color=discord.Color.green())
        embed.add_field(name="매일 기본", value=f"{base:,} P", inline=True)
        embed.add_field(name="주기",
                        value=(f"{cycle}일마다" if cycle else "사용 안 함"), inline=True)
        embed.add_field(name="주기당 증가폭", value=f"+{step:,} P", inline=True)
        if cap:
            embed.add_field(name="추가 보상 상한", value=f"{cap:,} P", inline=True)
        embed.add_field(name="📈 이렇게 지급됩니다",
                        value=_reward_preview(settings), inline=False)
        embed.set_footer(text="추가 보상은 주기를 채운 날에만 붙습니다")
        await _refresh_settings_panel(
            interaction,
            lambda: CheckinSettingsView(settings),
            lambda: _build_checkin_panel(settings),
            embed)


class MilestoneModal(discord.ui.Modal, title="🎯 특별 보상 설정"):
    raw = discord.ui.TextInput(
        label="연속일수:보상 (쉼표로 구분)",
        placeholder="예: 7:300, 14:700, 30:2000",
        style=discord.TextStyle.paragraph, required=False, max_length=300)

    async def on_submit(self, interaction: discord.Interaction):
        text = str(self.raw.value or "").strip()
        milestones = {}
        if text:
            for chunk in re.split(r"[,\n]+", text):
                chunk = chunk.strip()
                if not chunk:
                    continue
                m = re.match(r"^(\d+)\s*[:=]\s*(\d+)$", chunk)
                if not m:
                    await interaction.response.send_message(
                        f"⚠️ `{chunk[:40]}` 형식을 읽을 수 없습니다.\n"
                        "`일수:보상` 형태로 적어 주세요. 예) `7:300, 30:2000`",
                        ephemeral=True)
                    return
                milestones[str(int(m.group(1)))] = int(m.group(2))

        await save_checkin_settings(interaction.guild.id, {"milestones": milestones})
        embed = discord.Embed(
            title="🎯 특별 보상 변경 완료",
            description=("설정된 목표가 없습니다. (달성 보상 미사용)" if not milestones else
                         "\n".join(f"• **{d}일** 연속 → **+{v:,} P**"
                                     for d, v in sorted(milestones.items(), key=lambda x: int(x[0])))),
            color=discord.Color.green())
        embed.set_footer(text="달성 보상은 그 날 한 번만 지급됩니다")
        fresh = await get_checkin_settings(interaction.guild.id)
        await _refresh_settings_panel(
            interaction,
            lambda: CheckinSettingsView(fresh),
            lambda: _build_checkin_panel(fresh),
            embed)


def _reward_preview(settings: dict) -> str:
    """설정값이 실제로 얼마가 되는지 미리 보여줍니다.

    숫자만 바꿔놓고 결과를 모르면 조정할 수가 없습니다. 며칠차에 얼마인지
    직접 계산해서 보여줍니다.
    """
    cycle = int(settings.get("cycle", 0) or 0)
    days = [1]
    if cycle > 0:
        days += [cycle - 1, cycle, cycle + 1, cycle * 2, cycle * 3, cycle * 4]
    else:
        days += [7, 14, 30]
    rows = []
    for d in sorted(set(x for x in days if x > 0))[:7]:
        b, bo, ms = streak_reward(d, settings)
        tag = "  🔥주기 달성" if bo else ""
        tag += f"  🎉특별 +{ms:,}" if ms else ""
        rows.append(f"`{d:3}일차`  **{b + bo + ms:,} P**{tag}")
    return "\n".join(rows)


class CheckinSettingsSelect(discord.ui.Select):
    def __init__(self, settings: dict):
        super().__init__(
            placeholder="출석 보상 설정을 선택하세요...",
            options=[
                discord.SelectOption(
                    label="연속 출석 보상", value="reward", emoji="🔥",
                    description=(f"매일 {settings.get('base', 100)} P · "
                                 f"{settings.get('cycle', 0)}일마다 "
                                 f"+{settings.get('cycle_step', 0)} P씩")[:95]),
                discord.SelectOption(
                    label="특별 보상 (선택)", value="milestone", emoji="🎯",
                    description=(f"현재 {len(settings.get('milestones') or {})}개 지정됨")[:95]),
                discord.SelectOption(
                    label="현재 설정 확인", value="view", emoji="📋",
                    description="지금 값으로 며칠차에 얼마를 주는지 봅니다."),
            ])

    async def callback(self, interaction: discord.Interaction):
        choice = self.values[0]
        if choice == "reward":
            await interaction.response.send_modal(CheckinRewardModal())
        elif choice == "milestone":
            await interaction.response.send_modal(MilestoneModal())
        else:
            settings = await get_checkin_settings(interaction.guild.id)
            embed = discord.Embed(title="📋 현재 출석 보상 설정",
                                  color=discord.Color.blurple())
            cyc = int(settings.get("cycle", 0) or 0)
            embed.add_field(name="매일 기본", value=f"{settings.get('base', 0):,} P", inline=True)
            embed.add_field(name="주기", value=(f"{cyc}일마다" if cyc else "사용 안 함"), inline=True)
            embed.add_field(name="주기당 증가폭",
                            value=f"+{settings.get('cycle_step', 0):,} P", inline=True)
            cap = int(settings.get("cycle_max", 0) or 0)
            if cap:
                embed.add_field(name="추가 보상 상한", value=f"{cap:,} P", inline=True)
            ms = settings.get("milestones") or {}
            embed.add_field(
                name="🎯 특별 보상",
                value=("\n".join(f"• **{d}일** → **+{int(v):,} P**"
                                   for d, v in sorted(ms.items(), key=lambda x: int(x[0])))
                       if ms else "없음"),
                inline=False)
            embed.add_field(name="📈 실제 지급 예시", value=_reward_preview(settings),
                            inline=False)
            await interaction.response.send_message(embed=embed, ephemeral=True)


class CheckinSettingsView(discord.ui.View):
    def __init__(self, settings: dict):
        super().__init__(timeout=180)
        self.add_item(CheckinSettingsSelect(settings))


class CheckIn(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="출석", description="하루에 한 번 출석체크를 하고 포인트를 획득합니다. (연속할수록 더 많이)")
    # DM 에서는 interaction.guild 가 None 이라 AttributeError 로 죽습니다.
    # (실제로 /출석 이 그렇게 터졌습니다). guild_only 를 걸면 디스코드가
    # **DM 목록에 아예 띄우지 않으므로** 방어 코드를 흩뿌릴 필요가 없습니다.
    @app_commands.guild_only()
    async def check_in(self, interaction: discord.Interaction):
        # 블랙리스트 유저 확인
        # 3초 제한을 넘기지 않도록 **먼저 응답을 예약**합니다. 아래에서 DB 를
        # 읽는데, 몰리는 순간 그 왕복이 3초를 넘기면 10062 로 명령어가 통째로
        # 죽습니다(실측: /포인트랭킹 이 실제로 그렇게 실패했습니다).
        await interaction.response.defer(ephemeral=True, thinking=True)
        if await is_user_blacklisted(interaction.guild.id, interaction.user.id):
            embed = discord.Embed(
                title="🚫 출석체크 불가",
                description="귀하는 현재 **포인트 획득 제한(블랙리스트)** 상태로 설정되어 출석체크를 진행할 수 없습니다.\n\n"
                            "이유 및 상세 문의는 서버 관리자에게 요청해 주세요.",
                color=discord.Color.red()
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        result = await checkin_user(interaction.guild.id, interaction.user.id)
        total_points = result["points"]
        streak = result["streak"]

        # 퀘스트 진행도. **출석에 실제로 성공했을 때만** 셉니다. 이미 출석한
        # 사람이 명령어를 또 쳐도 선착순 번호가 올라가면 안 됩니다.
        rank = None
        if result["ok"]:
            try:
                await bump_quest_progress(interaction.guild.id, interaction.user.id,
                                          "checkin")
                await bump_quest_progress(interaction.guild.id, interaction.user.id,
                                          "wcheckin", weekly=True)
                await bump_event_progress(interaction.guild.id,
                                          interaction.user.id, "checkin")
                rank = await take_checkin_rank(interaction.guild.id)
                if rank:
                    # 등수는 누적이 아니라 '그 날의 값'이라 $inc 로 올리면 안 됩니다.
                    # 0 에서 시작하므로 등수를 그대로 더해 한 번만 기록합니다.
                    await bump_quest_progress(interaction.guild.id, interaction.user.id,
                                              "rank", amount=rank)
            except Exception as e:
                print(f"[Quest] 출석 진행도 기록 실패: {type(e).__name__}: {e}", flush=True)

        if result["ok"]:
            # 어떻게 이 숫자가 나왔는지 그대로 보여줍니다. 연속이 이득이라는
            # 걸 눈으로 봐야 다음 날 또 오게 됩니다.
            parts = [f"기본 **{result['base']} P**"]
            if result["bonus"]:
                parts.append(f"🔥 {streak}일 연속 달성 **+{result['bonus']:,} P**")
            if result["milestone"]:
                parts.append(f"🎉 특별 보상 **+{result['milestone']:,} P**")

            embed = discord.Embed(
                title="✅ 출석체크 완료!",
                description=f"오늘 출석으로 **+{result['gained']:,} P** 를 획득하셨습니다!",
                color=discord.Color.green()
            )
            embed.add_field(name="🔥 연속 출석", value=f"**{streak}일째**", inline=True)
            embed.add_field(name="💰 보유 포인트",
                            value=f"**{total_points:,} P**", inline=True)
            embed.add_field(name="🧮 지급 내역", value="  ·  ".join(parts), inline=False)

            settings = await get_checkin_settings(interaction.guild.id)
            nxt = _next_goal(streak, settings)
            if nxt:
                days, reward = nxt
                embed.add_field(
                    name="🎯 다음 목표",
                    value=f"**{days}일** 연속까지 **{days - streak}일** 남았습니다 "
                          f"(보상 **+{reward:,} P**)",
                    inline=False)
            if rank:
                embed.add_field(name="🏃 오늘 출석 순번",
                                value=f"**{rank}번째**", inline=True)
            embed.set_footer(text="하루라도 거르면 연속이 처음부터 다시 시작됩니다")

            if result["milestone"]:
                # 마일스톤은 드물고 특별하므로 기록에 남깁니다. 매일 출석까지
                # 남기면 로그 상한(200)이 금세 찹니다.
                try:
                    await add_activity_log(
                        interaction.guild.id, "checkin", interaction.user.id,
                        interaction.user.display_name,
                        f"연속 출석 {streak}일 달성 (+{result['milestone']} P)")
                except Exception as e:
                    print(f"[CheckIn] 활동 로그 기록 실패: {e}", flush=True)
        else:
            embed = discord.Embed(
                title="⚠️ 출석체크 이미 완료됨",
                description="오늘 출석체크는 이미 완료하셨습니다. 내일 다시 시도해 주세요!",
                color=discord.Color.gold()
            )
            embed.add_field(name="🔥 연속 출석", value=f"**{streak}일째**", inline=True)
            embed.add_field(name="💰 보유 포인트",
                            value=f"**{total_points:,} P**", inline=True)

        # ephemeral=True makes the response visible ONLY to the user!
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="포인트확인", description="현재 내가 보유한 총 포인트를 확인합니다.")
    # DM 에서는 interaction.guild 가 None 이라 AttributeError 로 죽습니다.
    # (실제로 /출석 이 그렇게 터졌습니다). guild_only 를 걸면 디스코드가
    # **DM 목록에 아예 띄우지 않으므로** 방어 코드를 흩뿌릴 필요가 없습니다.
    @app_commands.guild_only()
    async def view_points(self, interaction: discord.Interaction):
        # 3초 제한을 넘기지 않도록 **먼저 응답을 예약**합니다. 아래에서 DB 를
        # 읽는데, 몰리는 순간 그 왕복이 3초를 넘기면 10062 로 명령어가 통째로
        # 죽습니다(실측: /포인트랭킹 이 실제로 그렇게 실패했습니다).
        await interaction.response.defer(ephemeral=True, thinking=True)
        total_points = await get_user_points(interaction.guild.id, interaction.user.id)
        embed = discord.Embed(
            title="💳 내 포인트 정보",
            description=f"💰 **현재 총 보유 포인트**: **{total_points:,}** P",
            color=discord.Color.blue()
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="포인트랭킹",
                          description="서버의 포인트 보유 순위 TOP 30을 확인합니다.")
    # DM 에서는 interaction.guild 가 None 이라 AttributeError 로 죽습니다.
    # (실제로 /출석 이 그렇게 터졌습니다). guild_only 를 걸면 디스코드가
    # **DM 목록에 아예 띄우지 않으므로** 방어 코드를 흩뿌릴 필요가 없습니다.
    @app_commands.guild_only()
    async def point_ranking(self, interaction: discord.Interaction):
        # 3초 제한을 넘기지 않도록 **먼저 응답을 예약**합니다. 아래에서 DB 를
        # 읽는데, 몰리는 순간 그 왕복이 3초를 넘기면 10062 로 명령어가 통째로
        # 죽습니다(실측: /포인트랭킹 이 실제로 그렇게 실패했습니다).
        await interaction.response.defer(ephemeral=True, thinking=True)
        embed, total = await build_rank_page(interaction, 0)
        await interaction.followup.send(embed=embed, view=RankView(0, total),
                                        ephemeral=True)

    @app_commands.command(name="포인트얻는법", description="포인트를 획득하는 다양한 방법과 도움말을 확인합니다.")
    # DM 에서는 interaction.guild 가 None 이라 AttributeError 로 죽습니다.
    # (실제로 /출석 이 그렇게 터졌습니다). guild_only 를 걸면 디스코드가
    # **DM 목록에 아예 띄우지 않으므로** 방어 코드를 흩뿌릴 필요가 없습니다.
    @app_commands.guild_only()
    async def point_guide(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="💡 한바헌 포인트 획득 방법 가이드",
            description="아래 **드롭다운 메뉴**에서 원하시는 항목을 선택해 보세요!\n상세한 포인트 획득 조건을 확인하실 수 있습니다.",
            color=discord.Color.gold()
        )
        view = PointGuideView()
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @app_commands.command(name="포인트지급", description="[관리자 전용] 특정 유저에게 포인트를 지급합니다.")
    # DM 에서는 interaction.guild 가 None 이라 AttributeError 로 죽습니다.
    # (실제로 /출석 이 그렇게 터졌습니다). guild_only 를 걸면 디스코드가
    # **DM 목록에 아예 띄우지 않으므로** 방어 코드를 흩뿌릴 필요가 없습니다.
    @app_commands.guild_only()
    @app_commands.rename(member="멤버", amount="포인트")
    @app_commands.describe(member="포인트를 지급할 유저", amount="지급할 포인트 수량")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def give_points(self, interaction: discord.Interaction, member: discord.Member, amount: int):
        if amount <= 0:
            await interaction.response.send_message("⚠️ 지급할 포인트는 1 P 이상이어야 합니다.", ephemeral=True)
            return

        new_total = await add_user_points(interaction.guild.id, member.id, amount)
        await add_activity_log(
            interaction.guild.id, "admin", interaction.user.id, interaction.user.display_name,
            f"[{member.display_name}]에게 {amount:,}P 지급"
        )

        embed = discord.Embed(
            title="🎁 포인트 지급 완료",
            description=f"{member.mention} 유저에게 **+{amount:,} P**를 지급하였습니다.\n\n"
                        f"💰 **해당 유저의 현재 총 포인트**: **{new_total:,} P**",
            color=discord.Color.green()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="포인트회수", description="[관리자 전용] 특정 유저의 포인트를 회수(차감)합니다.")
    # DM 에서는 interaction.guild 가 None 이라 AttributeError 로 죽습니다.
    # (실제로 /출석 이 그렇게 터졌습니다). guild_only 를 걸면 디스코드가
    # **DM 목록에 아예 띄우지 않으므로** 방어 코드를 흩뿌릴 필요가 없습니다.
    @app_commands.guild_only()
    @app_commands.rename(member="멤버", amount="포인트")
    @app_commands.describe(member="포인트를 회수할 유저", amount="회수할 포인트 수량")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def take_points(self, interaction: discord.Interaction, member: discord.Member, amount: int):
        # 3초 제한을 넘기지 않도록 **먼저 응답을 예약**합니다. 아래에서 DB 를
        # 읽는데, 몰리는 순간 그 왕복이 3초를 넘기면 10062 로 명령어가 통째로
        # 죽습니다(실측: /포인트랭킹 이 실제로 그렇게 실패했습니다).
        await interaction.response.defer(ephemeral=True, thinking=True)
        if amount <= 0:
            await interaction.followup.send("⚠️ 회수할 포인트는 1 P 이상이어야 합니다.", ephemeral=True)
            return

        current_pts = await get_user_points(interaction.guild.id, member.id)
        if current_pts < amount:
            embed = discord.Embed(
                title="⚠️ 포인트 회수 불가",
                description=f"{member.mention} 유저의 현재 보유 포인트(**{current_pts:,} P**)가 회수 요청 수량(**{amount:,} P**)보다 적습니다.",
                color=discord.Color.red()
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        success, new_total = await deduct_user_points(interaction.guild.id, member.id, amount)
        if not success:
            await interaction.followup.send("⚠️ 포인트 회수에 실패하였습니다.", ephemeral=True)
            return

        await add_activity_log(
            interaction.guild.id, "admin", interaction.user.id, interaction.user.display_name,
            f"[{member.display_name}]의 {amount:,}P 회수"
        )

        embed = discord.Embed(
            title="🔻 포인트 회수 완료",
            description=f"{member.mention} 유저의 포인트 **-{amount:,} P**를 회수하였습니다.\n\n"
                        f"💰 **해당 유저의 현재 총 포인트**: **{new_total:,} P**",
            color=discord.Color.orange()
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(
        name="출석설정",
        description="[관리자 전용] 연속 출석 보상을 설정합니다."
    )
    # DM 에서는 interaction.guild 가 None 이라 AttributeError 로 죽습니다.
    # (실제로 /출석 이 그렇게 터졌습니다). guild_only 를 걸면 디스코드가
    # **DM 목록에 아예 띄우지 않으므로** 방어 코드를 흩뿌릴 필요가 없습니다.
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def checkin_settings(self, interaction: discord.Interaction):
        # 3초 제한을 넘기지 않도록 **먼저 응답을 예약**합니다. 아래에서 DB 를
        # 읽는데, 몰리는 순간 그 왕복이 3초를 넘기면 10062 로 명령어가 통째로
        # 죽습니다(실측: /포인트랭킹 이 실제로 그렇게 실패했습니다).
        await interaction.response.defer(ephemeral=True, thinking=True)
        settings = await get_checkin_settings(interaction.guild.id)
        await interaction.followup.send(
            embed=_build_checkin_panel(settings),
            view=CheckinSettingsView(settings), ephemeral=True)


async def setup(bot):
    await bot.add_cog(CheckIn(bot))
    # 재시작 뒤에도 랭킹 쪽 버튼이 동작해야 합니다.
    bot.add_view(RankView())

