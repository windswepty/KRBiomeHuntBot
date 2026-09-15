"""복권 — 1~25 중 5개를 골라 토요일 20:00(KST)에 맞춰 봅니다.

**이 기능이 사행행위가 되지 않는 이유는 설계에 있습니다.** 지키세요.

  1. 포인트는 **돈으로 살 수 없습니다** (봇에 결제 경로가 없습니다).
  2. 상금은 **포인트뿐**입니다. 현금·기프티콘·로벅스로 바꿔주지 마세요.
  3. **참가비를 모아 당첨자에게 몰아주지 않습니다.** 참가비는 소각되고
     상금은 서버가 고정액으로 지급합니다. "여러 사람으로부터 재물을 모아
     우연적 방법으로 득실을 결정" 이 사행행위규제법 제2조의 정의라,
     굳이 그 모양으로 만들 이유가 없습니다.

세 가지 중 하나라도 무너지면 그때부터는 다른 문제가 됩니다.

⚠️ **환수율을 반드시 화면에 띄웁니다.** 1개만 맞아도 5등이라 표의 45.6%가
당첨입니다. 운영진이 상금 다섯 개를 손으로 정하는데 그 감이 오지 않아,
무심코 100%를 넘기면 **포인트가 무한히 불어납니다.**
"""
import asyncio
import secrets
from datetime import datetime, time as dtime

import discord
from discord import app_commands
from discord.ext import commands, tasks

from utils.database import (
    KST, LOTTO_MAX_NUMBER, LOTTO_MAX_TICKETS, LOTTO_PICK,
    LOTTO_DRAW_HOUR, LOTTO_DRAW_WEEKDAY,
    add_activity_log, add_user_points, claim_lotto_payout, deduct_user_points,
    get_lotto_draw, get_lotto_settings, get_lotto_tickets, get_user_points,
    list_lotto_entrants, lotto_draw_at, lotto_payout, lotto_prev_round_key,
    lotto_return_rate, lotto_round_key, lotto_odds, record_lotto_draw,
    release_lotto_ticket, reserve_lotto_ticket, save_lotto_settings,
)

# 추첨 시각. 매일 이 시각에 깨어나 **토요일인지 확인**하고 돌립니다.
DRAW_TIME = dtime(hour=LOTTO_DRAW_HOUR, minute=0, tzinfo=KST)

# 봇이 20:00 에 꺼져 있었으면 그 회차는 영영 추첨되지 않습니다. 무인 운영이
# 전제라 사람이 알아채 주길 기대할 수 없으므로, 주기적으로 밀린 회차를 찾습니다.
CATCHUP_MINUTES = 20

RANK_LABEL = {1: "🥇 1등", 2: "🥈 2등", 3: "🥉 3등", 4: "4등", 5: "5등"}


def rank_of(matched: int) -> int | None:
    """맞춘 개수 → 등수. 0개는 꽝이라 None."""
    if matched < 1 or matched > LOTTO_PICK:
        return None
    return LOTTO_PICK - matched + 1


def parse_numbers(raw: str) -> tuple[list[int] | None, str]:
    """사람이 친 글자에서 번호 5개를 뽑습니다. (번호, 오류문구)

    쉼표·공백·슬래시 아무거나 받습니다 — 무엇으로 구분해야 하는지
    외우게 만들면 첫 시도에서 실패하고 아무도 다시 안 씁니다.
    """
    parts = [p for p in "".join(c if c.isdigit() else " " for c in str(raw or "")).split() if p]
    if not parts:
        return None, "번호를 입력해 주세요."
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return None, "숫자만 입력해 주세요."
    if len(nums) != LOTTO_PICK:
        return None, f"정확히 **{LOTTO_PICK}개**를 입력해 주세요. (지금 {len(nums)}개)"
    if len(set(nums)) != LOTTO_PICK:
        return None, "같은 번호를 두 번 고를 수 없습니다."
    bad = [n for n in nums if n < 1 or n > LOTTO_MAX_NUMBER]
    if bad:
        return None, f"**1~{LOTTO_MAX_NUMBER}** 사이만 됩니다. (잘못된 값: {', '.join(map(str, bad))})"
    return sorted(nums), ""


def pick_random() -> list[int]:
    """자동 선택. `secrets` 를 씁니다 — 추첨과 같은 품질이어야 합니다."""
    return sorted(secrets.SystemRandom().sample(range(1, LOTTO_MAX_NUMBER + 1), LOTTO_PICK))


def fmt_numbers(nums, hit=None) -> str:
    """번호를 보기 좋게. 맞은 번호는 굵게 표시합니다."""
    hit = set(hit or [])
    return " ".join(f"**`{n:02d}`**" if n in hit else f"`{n:02d}`" for n in nums)


def _ticket_numbers(doc: dict) -> list[list[int]]:
    return [list(t.get("numbers") or []) for t in (doc.get("tickets") or [])]


# ── 화면 ─────────────────────────────────────────────────────────────

def build_lotto_embed(settings: dict, mine: dict, draw: dict | None,
                      prev_mine: dict, balance: int) -> discord.Embed:
    """`/복권` 의 본 화면. 지난 결과와 이번 회차를 한 장에 담습니다."""
    price = int(settings.get("price", 0) or 0)
    embed = discord.Embed(
        title=f"🎰 복권 · 1~{LOTTO_MAX_NUMBER} 중 {LOTTO_PICK}개",
        color=discord.Color.gold())

    # ── 지난 회차 결과 (있으면 **맨 위**에) ──────────────────────────
    # 토요일 20시에 사람들이 보러 오는 것이 이것입니다.
    if draw:
        win = list(draw.get("numbers") or [])
        lines = [f"당첨 번호 {fmt_numbers(win)}"]
        results = prev_mine.get("results") or []
        if results:
            won = int(prev_mine.get("won", 0) or 0)
            for r in results:
                rk = rank_of(int(r.get("matched", 0)))
                tag = (f"{RANK_LABEL[rk]} · **+{int(r.get('prize', 0)):,}P**"
                       if rk and int(r.get("prize", 0)) > 0 else
                       f"{RANK_LABEL[rk]} · 0P" if rk else "꽝")
                lines.append(f"{fmt_numbers(r.get('numbers') or [], win)} → {tag}")
            lines.append(f"\n합계 **{won:,}P**" if won else "\n이번엔 아쉽네요.")
        else:
            lines.append("\n-# 이 회차에는 표를 사지 않으셨습니다.")
        embed.add_field(name=f"🎱 {draw.get('round')} 회차 결과",
                        value="\n".join(lines), inline=False)

    # ── 이번 회차 ────────────────────────────────────────────────────
    deadline = lotto_draw_at()
    ts = int(deadline.timestamp())
    count = int(mine.get("count", 0) or 0)
    # 마감까지 남은 시간은 **실제로 흘러가는 값**이라 상대시각이 맞습니다.
    # (보드의 '갱신 N초 전' 과 달리, 여기서는 그게 보여줄 정보 자체입니다)
    head = [f"추첨 <t:{ts}:F> · <t:{ts}:R>",
            f"한 장 **{price:,}P** · 내 표 **{count}/{LOTTO_MAX_TICKETS}장** · "
            f"보유 **{balance:,}P**"]
    if count:
        head.append("")
        head += [f"{i + 1}. {fmt_numbers(n)}" for i, n in enumerate(_ticket_numbers(mine))]
    else:
        head.append("\n-# 아직 산 표가 없습니다.")
    embed.add_field(name=f"🎟️ {lotto_round_key()} 회차 (접수 중)",
                    value="\n".join(head), inline=False)

    odds = lotto_odds()
    embed.add_field(
        name="🏆 상금",
        value=" · ".join(
            f"{RANK_LABEL[rank_of(m)]} `{lotto_payout(settings, m):,}P`"
            for m in range(LOTTO_PICK, 0, -1)) +
        f"\n-# 1등 확률 1/{odds['total'] // odds[LOTTO_PICK]:,} · "
        f"참가비는 소각되고 상금은 서버가 지급합니다.",
        inline=False)
    embed.set_footer(text="포인트로만 하는 놀이입니다. 현금·기프티콘으로 바꿔주지 않습니다.")
    return embed


class BuyNumbersModal(discord.ui.Modal, title="🎟️ 번호 직접 고르기"):
    numbers = discord.ui.TextInput(
        label=f"1~{LOTTO_MAX_NUMBER} 중 {LOTTO_PICK}개",
        placeholder="예: 3 9 14 21 25  (쉼표·공백 아무거나)",
        max_length=40, required=True)

    async def on_submit(self, interaction: discord.Interaction):
        nums, err = parse_numbers(str(self.numbers.value))
        if nums is None:
            await interaction.response.send_message(f"⚠️ {err}", ephemeral=True)
            return
        await buy_ticket(interaction, nums)


class LottoPanelView(discord.ui.View):
    """영구 View — 재시작 뒤에도 버튼이 살아 있어야 합니다.

    ⚠️ 인스턴스를 모두가 공유하므로 `self` 의 버튼 속성을 바꾸지 마세요.
    """

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="번호 고르기", emoji="🎟️",
                       style=discord.ButtonStyle.primary, custom_id="lotto:pick")
    async def pick(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(BuyNumbersModal())

    @discord.ui.button(label="자동 선택", emoji="🎲",
                       style=discord.ButtonStyle.secondary, custom_id="lotto:auto")
    async def auto(self, interaction: discord.Interaction, button: discord.ui.Button):
        await buy_ticket(interaction, pick_random())

    @discord.ui.button(label="새로고침", emoji="🔄",
                       style=discord.ButtonStyle.secondary, custom_id="lotto:refresh")
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        await interaction.followup.send(embed=await panel_embed(interaction),
                                        view=LottoPanelView(), ephemeral=True)


async def panel_embed(interaction: discord.Interaction) -> discord.Embed:
    gid, uid = interaction.guild.id, interaction.user.id
    settings = await get_lotto_settings(gid)
    rnd, prev = lotto_round_key(), lotto_prev_round_key()
    return build_lotto_embed(
        settings,
        await get_lotto_tickets(gid, uid, rnd),
        await get_lotto_draw(gid, prev),
        await get_lotto_tickets(gid, uid, prev),
        await get_user_points(gid, uid))


async def buy_ticket(interaction: discord.Interaction, nums: list[int]) -> None:
    """표 한 장을 삽니다.

    ⚠️ **자리를 먼저 잡고 포인트를 뺍니다.** 반대로 하면 한도 초과를
    되돌리는 사이에 봇이 죽었을 때 유저가 포인트만 잃습니다. 이 순서면
    최악이라도 공짜 표 한 장이 남을 뿐이라, 손해가 유저 쪽으로 가지 않습니다.
    """
    gid, uid = interaction.guild.id, interaction.user.id
    settings = await get_lotto_settings(gid)
    if not settings.get("enabled"):
        await interaction.response.send_message(
            "⚠️ 복권이 아직 열리지 않았습니다.", ephemeral=True)
        return

    price = int(settings.get("price", 0) or 0)
    rnd = lotto_round_key()

    got = await reserve_lotto_ticket(gid, uid, rnd, nums)
    if got is None:
        await interaction.response.send_message(
            f"⚠️ 한 회차에 **{LOTTO_MAX_TICKETS}장**까지만 살 수 있습니다.\n"
            f"-# 다음 회차는 추첨 직후에 열립니다.", ephemeral=True)
        return

    ok, remaining = (True, 0) if price <= 0 else await deduct_user_points(gid, uid, price)
    if not ok:
        await release_lotto_ticket(gid, uid, rnd, got["ticket"]["id"])
        await interaction.response.send_message(
            f"⚠️ 포인트가 부족합니다. (필요 **{price:,}P** · 보유 **{remaining:,}P**)",
            ephemeral=True)
        return
    if price <= 0:
        remaining = await get_user_points(gid, uid)

    deadline = int(lotto_draw_at().timestamp())
    embed = discord.Embed(
        title="🎟️ 복권을 샀습니다",
        description=f"{fmt_numbers(nums)}\n\n"
                    f"**{rnd} 회차** · 추첨 <t:{deadline}:R>\n"
                    f"내 표 **{got['count']}/{LOTTO_MAX_TICKETS}장** · "
                    f"남은 포인트 **{remaining:,}P**",
        color=discord.Color.green())
    embed.set_footer(text="결과는 추첨 뒤 /복권 에서 볼 수 있습니다.")
    if interaction.response.is_done():
        await interaction.followup.send(embed=embed, ephemeral=True)
    else:
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # 기록은 응답을 보낸 뒤에. 실패해도 구매는 이미 끝난 일입니다.
    try:
        await add_activity_log(gid, "lotto", uid, str(interaction.user),
                               f"{rnd} 회차 구매 {nums} (-{price}P)")
    except Exception as e:
        print(f"[Lotto] 활동 기록 실패: {type(e).__name__}: {e}", flush=True)


# ── 설정 ─────────────────────────────────────────────────────────────

class PriceModal(discord.ui.Modal, title="💰 복권 가격"):
    price = discord.ui.TextInput(label="한 장 가격 (P)", placeholder="예: 500",
                                 max_length=9, required=True)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            v = max(0, int(str(self.price.value).replace(",", "").strip()))
        except ValueError:
            await interaction.response.send_message("⚠️ 숫자만 입력해 주세요.", ephemeral=True)
            return
        await save_lotto_settings(interaction.guild.id, {"price": v})
        await send_settings(interaction, f"✅ 가격을 **{v:,}P** 로 바꿨습니다.")


class PrizeModal(discord.ui.Modal, title="🏆 등수별 상금"):
    """1~5등을 **한 번에** 받습니다. 하나씩 고치면 그 사이 환수율이
    엉뚱한 값이 되고, 운영진은 마지막 화면만 보고 괜찮다고 판단합니다."""

    p1 = discord.ui.TextInput(label="1등 · 5개 일치 (P)", max_length=12, required=True)
    p2 = discord.ui.TextInput(label="2등 · 4개 일치 (P)", max_length=12, required=True)
    p3 = discord.ui.TextInput(label="3등 · 3개 일치 (P)", max_length=12, required=True)
    p4 = discord.ui.TextInput(label="4등 · 2개 일치 (P)", max_length=12, required=True)
    p5 = discord.ui.TextInput(label="5등 · 1개 일치 (P)", max_length=12, required=True)

    def __init__(self, settings: dict):
        super().__init__()
        for i, box in enumerate((self.p1, self.p2, self.p3, self.p4, self.p5), start=1):
            box.default = str(int(settings.get(f"prize_{i}", 0) or 0))

    async def on_submit(self, interaction: discord.Interaction):
        vals = {}
        for i, box in enumerate((self.p1, self.p2, self.p3, self.p4, self.p5), start=1):
            try:
                vals[f"prize_{i}"] = max(0, int(str(box.value).replace(",", "").strip()))
            except ValueError:
                await interaction.response.send_message(
                    f"⚠️ {i}등 상금이 숫자가 아닙니다.", ephemeral=True)
                return
        await save_lotto_settings(interaction.guild.id, vals)
        await send_settings(interaction, "✅ 상금을 바꿨습니다.")


class AnnounceChannelSelect(discord.ui.ChannelSelect):
    def __init__(self):
        super().__init__(placeholder="결과를 발표할 채널을 고르세요...",
                         channel_types=[discord.ChannelType.text], max_values=1,
                         custom_id="lotto:announce")

    async def callback(self, interaction: discord.Interaction):
        cid = self.values[0].id
        await save_lotto_settings(interaction.guild.id, {"announce_channel_id": cid})
        await send_settings(interaction, f"✅ 발표 채널을 <#{cid}> 로 정했습니다.")


class AnnounceChannelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)
        self.add_item(AnnounceChannelSelect())


class LottoSettingsMenu(discord.ui.Select):
    def __init__(self):
        super().__init__(placeholder="복권 설정을 고르세요...", custom_id="lotto:set", options=[
            discord.SelectOption(label="현재 설정 · 환수율", value="show", emoji="📊",
                                 description="상금이 과한지 여기서 확인하세요."),
            discord.SelectOption(label="복권 가격", value="price", emoji="💰"),
            discord.SelectOption(label="등수별 상금 (1~5등)", value="prize", emoji="🏆"),
            discord.SelectOption(label="결과 발표 채널", value="channel", emoji="📢"),
            discord.SelectOption(label="켜기 / 끄기", value="toggle", emoji="🔀"),
            discord.SelectOption(label="지금 추첨하기 (테스트)", value="draw", emoji="🎱",
                                 description="이번에 마감된 회차를 즉시 추첨합니다."),
        ])

    async def callback(self, interaction: discord.Interaction):
        gid = interaction.guild.id
        choice = self.values[0]
        settings = await get_lotto_settings(gid)

        if choice == "show":
            await send_settings(interaction)
        elif choice == "price":
            await interaction.response.send_modal(PriceModal())
        elif choice == "prize":
            await interaction.response.send_modal(PrizeModal(settings))
        elif choice == "channel":
            await interaction.response.send_message(
                "📢 결과를 발표할 채널을 고르세요.", view=AnnounceChannelView(), ephemeral=True)
        elif choice == "toggle":
            new = not settings.get("enabled")
            await save_lotto_settings(gid, {"enabled": new})
            await send_settings(interaction,
                                "✅ 복권을 **열었습니다.**" if new else "⏸️ 복권을 **닫았습니다.**")
        elif choice == "draw":
            await interaction.response.defer(ephemeral=True, thinking=True)
            cog = interaction.client.get_cog("Lotto")
            res = await cog.run_draw(interaction.guild, announce=True)
            await interaction.followup.send(
                embed=discord.Embed(
                    title="🎱 추첨 결과",
                    description=(f"**{res['round']} 회차** {fmt_numbers(res['numbers'])}\n"
                                 f"참가 **{res['entrants']}명** · 당첨 **{res['winners']}명** · "
                                 f"지급 **{res['paid']:,}P**")
                    if res.get("ok") else f"⚠️ {res.get('error')}",
                    color=discord.Color.gold() if res.get("ok") else discord.Color.orange()),
                ephemeral=True)


class LottoSettingsView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)
        self.add_item(LottoSettingsMenu())


async def send_settings(interaction: discord.Interaction, note: str = "") -> None:
    """현재 설정 + **환수율**. 이 화면이 포인트 인플레를 막는 유일한 장치입니다."""
    settings = await get_lotto_settings(interaction.guild.id)
    rate = lotto_return_rate(settings)
    odds = lotto_odds()
    price = int(settings.get("price", 0) or 0)

    if rate >= 1.0:
        color, verdict = discord.Color.red(), (
            "🚨 **환수율이 100%를 넘습니다.** 사는 만큼 포인트가 늘어나므로, "
            "이대로 열면 **포인트 경제가 무너집니다.** 상금을 낮추거나 가격을 올리세요.")
    elif rate >= 0.9:
        color, verdict = discord.Color.orange(), (
            "⚠️ 환수율이 높습니다. 장기적으로 포인트가 늘어납니다.")
    else:
        color, verdict = discord.Color.blurple(), (
            "✅ 참가비로 빠지는 포인트가 상금보다 많습니다 (건전).")

    embed = discord.Embed(
        title="🎰 복권 설정",
        description=(f"{note}\n\n" if note else "") +
                    f"**상태** {'✅ 열림' if settings.get('enabled') else '⏸️ 닫힘'} · "
                    f"**가격** {price:,}P · "
                    f"**발표** " +
                    (f"<#{settings['announce_channel_id']}>"
                     if settings.get("announce_channel_id") else "미설정"),
        color=color)

    rows = []
    for m in range(LOTTO_PICK, 0, -1):
        rank = rank_of(m)
        one_in = odds["total"] / odds[m]
        rows.append(f"{RANK_LABEL[rank]} · {m}개 일치 · 1/{one_in:,.1f} · "
                    f"**{lotto_payout(settings, m):,}P**")
    embed.add_field(name="🏆 등수", value="\n".join(rows), inline=False)
    embed.add_field(
        name=f"📊 기대 환수율 {rate * 100:.1f}%",
        value=f"{verdict}\n-# 표 한 장(**{price:,}P**)당 평균 "
              f"**{rate * price:,.0f}P** 가 돌아갑니다. 실제 로또는 50% 입니다.",
        inline=False)
    embed.set_footer(text=f"추첨: 매주 토요일 {LOTTO_DRAW_HOUR}:00 (KST) · "
                          f"1인 {LOTTO_MAX_TICKETS}장까지")

    if interaction.response.is_done():
        await interaction.followup.send(embed=embed, view=LottoSettingsView(), ephemeral=True)
    else:
        await interaction.response.send_message(embed=embed, view=LottoSettingsView(),
                                                ephemeral=True)


# ── Cog ──────────────────────────────────────────────────────────────

class Lotto(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.weekly_draw.start()
        self.draw_catchup.start()

    def cog_unload(self):
        self.weekly_draw.cancel()
        self.draw_catchup.cancel()

    # ── 추첨 ─────────────────────────────────────────────────────────

    async def run_draw(self, guild: discord.Guild, *, announce: bool = True) -> dict:
        """마감된 회차를 추첨하고 상금을 지급합니다. **몇 번 돌려도 같습니다.**

        번호는 `$setOnInsert` 로 한 번만 확정되고, 지급은 문서의 `paid` 를
        필터에 넣어 서버 측에서 판정합니다. 재시작이나 루프 중복으로
        **이미 발표한 번호가 바뀌거나 상금이 두 번 나가면 되돌릴 수 없습니다.**
        """
        settings = await get_lotto_settings(guild.id)
        if not settings.get("enabled"):
            return {"ok": False, "error": "복권이 닫혀 있습니다."}

        rnd = lotto_prev_round_key()
        existing = await get_lotto_draw(guild.id, rnd)
        numbers = list(existing.get("numbers")) if existing else pick_random()
        draw = await record_lotto_draw(guild.id, rnd, numbers)
        win = set(draw.get("numbers") or [])

        entrants = await list_lotto_entrants(guild.id, rnd)
        winners, paid, top = 0, 0, []
        for doc in entrants:
            try:
                uid = int(doc.get("user_id"))
            except (TypeError, ValueError):
                continue
            results, total = [], 0
            for t in (doc.get("tickets") or []):
                nums = list(t.get("numbers") or [])
                matched = len(win & set(nums))
                prize = lotto_payout(settings, matched)
                results.append({"numbers": nums, "matched": matched, "prize": prize})
                total += prize

            # 이 호출이 정산을 맡았을 때만 지급합니다 (중복 지급 방지).
            if not await claim_lotto_payout(guild.id, uid, rnd, results, total):
                continue
            if total > 0:
                winners += 1
                paid += total
                await add_user_points(guild.id, uid, total)
                try:
                    await add_activity_log(guild.id, "lotto", uid, str(uid),
                                           f"{rnd} 회차 당첨 +{total}P")
                except Exception:
                    pass
            best = max((r["matched"] for r in results), default=0)
            if best >= LOTTO_PICK - 1:          # 1·2등만 발표에 이름을 올립니다
                top.append((best, uid, total))

        out = {"ok": True, "round": rnd, "numbers": sorted(win),
               "entrants": len(entrants), "winners": winners, "paid": paid,
               "top": top}
        if announce:
            await self.announce(guild, settings, out)
        return out

    async def announce(self, guild: discord.Guild, settings: dict, res: dict) -> None:
        cid = settings.get("announce_channel_id")
        if not cid:
            return
        channel = guild.get_channel(int(cid))
        if channel is None:
            return
        embed = discord.Embed(
            title=f"🎱 복권 {res['round']} 회차 당첨 번호",
            description=f"## {fmt_numbers(res['numbers'])}",
            color=discord.Color.gold())
        embed.add_field(
            name="📋 결과",
            value=f"참가 **{res['entrants']}명** · 당첨 **{res['winners']}명** · "
                  f"지급 **{res['paid']:,}P**", inline=False)
        if res["top"]:
            embed.add_field(
                name="🎉 1·2등",
                value="\n".join(f"{RANK_LABEL[rank_of(m)]} <@{u}> · **+{p:,}P**"
                                for m, u, p in sorted(res["top"], reverse=True)[:10]),
                inline=False)
        embed.add_field(
            name="🎟️ 다음 회차",
            value=f"지금부터 접수합니다. `/복권` 으로 사세요.\n"
                  f"-# 추첨 <t:{int(lotto_draw_at().timestamp())}:F>", inline=False)
        embed.set_footer(text="내 결과는 /복권 에서 볼 수 있습니다.")
        try:
            # 당첨자 멘션은 **알림이 가야 의미가 있습니다.** 다만 역할·전원
            # 호출은 하지 않습니다 — 매주 전원을 부를 일이 아닙니다.
            await channel.send(embed=embed,
                               allowed_mentions=discord.AllowedMentions(
                                   users=True, roles=False, everyone=False))
        except Exception as e:
            print(f"[Lotto] {guild.name}: 발표 실패 - {type(e).__name__}: {e}", flush=True)

    @tasks.loop(time=DRAW_TIME)
    async def weekly_draw(self):
        """매일 20:00 에 깨어나 **토요일일 때만** 추첨합니다."""
        await self.bot.wait_until_ready()
        if datetime.now(KST).weekday() != LOTTO_DRAW_WEEKDAY:
            return
        for guild in self.bot.guilds:
            try:
                res = await self.run_draw(guild)
                if res.get("ok"):
                    print(f"[Lotto] {guild.name}: {res['round']} 회차 추첨 "
                          f"{res['numbers']} · 참가 {res['entrants']} · "
                          f"지급 {res['paid']}P", flush=True)
            except Exception as e:
                print(f"[Lotto] {guild.name}: 추첨 실패 - {type(e).__name__}: {e}", flush=True)

    @tasks.loop(minutes=CATCHUP_MINUTES)
    async def draw_catchup(self):
        """**봇이 20:00 에 꺼져 있었으면 그 회차는 영영 추첨되지 않습니다.**

        사람이 알아채 주길 기대할 수 없는 운영이라, 밀린 회차를 스스로
        찾아 마무리합니다. 추첨·지급이 모두 멱등이라 겹쳐도 안전합니다.
        """
        await self.bot.wait_until_ready()
        rnd = lotto_prev_round_key()
        for guild in self.bot.guilds:
            try:
                settings = await get_lotto_settings(guild.id)
                if not settings.get("enabled"):
                    continue
                if await get_lotto_draw(guild.id, rnd):
                    continue                       # 이미 끝난 회차
                res = await self.run_draw(guild)
                if res.get("ok"):
                    print(f"[Lotto] {guild.name}: 밀린 {res['round']} 회차를 "
                          f"뒤늦게 추첨했습니다 {res['numbers']}", flush=True)
            except Exception as e:
                print(f"[Lotto] {guild.name}: 밀린 추첨 실패 - "
                      f"{type(e).__name__}: {e}", flush=True)
            await asyncio.sleep(0)

    # ── 명령어 ───────────────────────────────────────────────────────

    @app_commands.command(name="복권",
                          description="1~25 중 5개를 골라 토요일 추첨에 참여합니다.")
    @app_commands.guild_only()
    async def lotto(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        settings = await get_lotto_settings(interaction.guild.id)
        if not settings.get("enabled"):
            await interaction.followup.send("⚠️ 복권이 아직 열리지 않았습니다.", ephemeral=True)
            return
        await interaction.followup.send(embed=await panel_embed(interaction),
                                        view=LottoPanelView(), ephemeral=True)

    @app_commands.command(name="복권설정",
                          description="[관리자 전용] 복권 가격·상금·발표 채널을 설정합니다.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def lotto_settings(self, interaction: discord.Interaction):
        await send_settings(interaction)


async def setup(bot):
    await bot.add_cog(Lotto(bot))
    # 재시작 뒤에도 패널 버튼이 동작해야 합니다.
    bot.add_view(LottoPanelView())
