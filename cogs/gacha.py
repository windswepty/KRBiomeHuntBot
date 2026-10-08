import random
import re
import discord
from discord.ext import commands
from discord import app_commands
from utils.database import (
    get_gacha_settings, save_gacha_settings,
    add_gacha_item, remove_gacha_item, set_gacha_item_stock,
    get_user_points, deduct_user_points, add_user_points, add_activity_log,
    next_gacha_item_id,
    bump_quest_progress, bump_event_progress,
    consume_gacha_stock
)
from utils.event_db import (
    get_season, season_state, spend_season_points, add_season_points, get_season_points,
    base_points_paused, credit_earned,
)

# 낼 포인트. 시즌 포인트는 시즌 기간에만(utils/event_db.py).
POINTS, SEASON = "points", "season"


async def current_currency(guild_id: int) -> str:
    """지금 낼 포인트 — **시즌 중이면 시즌 포인트, 아니면 기본 포인트**(요청 2026-10-08).

    누르는 순간에 정합니다. 패널을 띄운 뒤 시즌이 시작 · 종료돼도 그때 기준으로
    맞는 포인트가 빠집니다(패널 글자는 띄운 시점 기준이라 다를 수 있습니다).
    """
    try:
        return SEASON if season_state(await get_season(guild_id)) == "active" else POINTS
    except Exception as e:
        print(f"[Gacha] 시즌 확인 실패(기본 포인트로): {type(e).__name__}: {e}", flush=True)
        return POINTS


async def prize_unit(guild_id: int) -> str:
    """경품 포인트가 어디로 들어가는지 — 패널 글자가 실제 적립과 같아야 합니다.

    시즌 중 '시즌 포인트로만' → 🏆 · '둘 다' → P + 🏆 · 시즌 아님 → P
    """
    if await base_points_paused(guild_id):
        return "🏆"
    if season_state(await get_season(guild_id)) == "active":
        return "P + 🏆"
    return "P"


def parse_role_mentions(text: str) -> str:
    if not text:
        return text
    # {@12345}, {12345}, {@&12345} 등의 패턴을 디스코드 표준 역할 멘션 <@&12345> 로 자동 변환
    return re.sub(r'\{@?&?(\d{17,20})\}', r'<@&\1>', text)



def format_stock_str(stock: int) -> str:
    if stock == -1:
        return "무제한"
    elif stock <= 0:
        return "품절"
    else:
        return f"{stock:,}개 남음"


def pct_str(p, exact: bool = False) -> str:
    """확률을 화면용 글자로. 기본은 소수 둘째 자리(`2.00`).

    `0.00001%` 처럼 아주 작은 확률을 `.2f` 로 찍으면 **`0.00%`** 가 되어,
    유저는 "절대 안 나오는 상품" 으로 읽고 관리자는 설정이 날아간 줄 압니다.
    그래서 0.01 보다 작으면 0 이 아닌 자리까지 보여 줍니다. 파이썬 기본
    표기(`1e-05`)도 쓰지 않습니다.

    `exact=True` 는 합계용입니다 — `99.998%` 가 `100.00%` 로 보이면서
    "100%가 아니라 뽑기 불가" 라고 나오면 무엇을 고칠지 알 수 없습니다.
    """
    try:
        p = float(p)
    except (TypeError, ValueError):
        return "0.00"
    if exact or 0 < abs(p) < 0.01:
        s = f"{p:.10f}".rstrip("0")
        whole, _, frac = s.partition(".")
        s = f"{whole}.{frac.ljust(2, '0')}"
        if float(s) == 0 and p:
            s = f"{p:.3g}"          # 1e-10 보다 작은 값 — 0 으로 보이게 두지 않습니다
        return s
    return f"{p:.2f}"


def get_total_percent(items: list[dict]) -> float:
    return sum(float(item.get("percent", item.get("weight", 0))) for item in items)


def is_valid_100_percent(items: list[dict]) -> bool:
    if not items:
        return False
    total = get_total_percent(items)
    return abs(total - 100.0) < 0.001


# ── Gacha Play View ───────────────────────────────────────────────────

# ── 추첨 엔진 (1회 · 10회 공용) ───────────────────────────────────────

BULK_COUNT = 10


def bulk_price(settings: dict) -> int:
    """10회 뽑기 가격. 설정이 없으면 1회 가격의 10배입니다."""
    price = int(settings.get("price", 100) or 0)
    discounted = int(settings.get("price_10", 0) or 0)
    return discounted if discounted > 0 else price * BULK_COUNT


def bulk_discount(settings: dict) -> int:
    """10회로 묶었을 때 아끼는 포인트 (없으면 0)."""
    return max(0, int(settings.get("price", 100) or 0) * BULK_COUNT
               - bulk_price(settings))


async def perform_rolls(guild, member, settings: dict, count: int,
                        total_price: int, currency: str = POINTS) -> dict:
    """실제 추첨을 수행합니다. 1회도 10회도 이 함수 하나를 씁니다.

    **포인트는 먼저 전액 차감하고, 못 채운 횟수만큼 돌려줍니다.**
    먼저 뽑고 나중에 차감하면 잔액이 모자란 사람이 상품만 받아 갑니다.

    재고가 있는 항목에 당첨되면 `consume_gacha_stock()` 으로 **원자적으로**
    1 차감합니다. 그 사이 다른 사람이 마지막 하나를 가져갔으면 그 항목만
    후보에서 빼고 **같은 회차를 다시 뽑습니다** — 한 회차를 통째로 날리지
    않기 위해서입니다. 뽑을 수 있는 항목이 하나도 남지 않으면 거기서 멈추고
    남은 회차분을 환불합니다.

    `currency` 가 SEASON 이면 **시즌 포인트로** 내고, 환불도 시즌 포인트로
    돌려줍니다. 당첨 포인트 보상은 언제나 **기본 포인트**입니다.
    """
    items = [dict(i) for i in settings.get("items", [])]   # 로컬 사본
    if currency == SEASON:
        ok, remaining = await spend_season_points(guild.id, member.id, total_price)
        if not ok and season_state(await get_season(guild.id)) != "active":
            return {"error": "no_season", "results": [], "currency": currency}
    else:
        ok, remaining = await deduct_user_points(guild.id, member.id, total_price)
    if not ok:
        return {"error": "no_points", "balance": remaining,
                "need": total_price, "results": [], "currency": currency}

    results: list[dict] = []
    for _ in range(count):
        picked = None
        # 품절로 빠지는 항목이 있을 수 있으므로 항목 수만큼은 다시 시도합니다
        for _attempt in range(len(items) + 1):
            avail = [i for i in items if i.get("stock", -1) != 0]
            weights = [float(i.get("percent", i.get("weight", 0))) for i in avail]
            if not avail or sum(weights) <= 0:
                break
            cand = random.choices(avail, weights=weights, k=1)[0]

            stock = cand.get("stock", -1)
            if stock > 0:
                got = await consume_gacha_stock(guild.id, cand.get("id"))
                if not got:
                    # 방금 다른 사람이 마지막 재고를 가져갔습니다.
                    # 이 항목만 빼고 이 회차를 다시 뽑습니다.
                    cand["stock"] = 0
                    continue
                cand["stock"] = stock - 1
            picked = cand
            break

        if picked is None:
            break               # 더 뽑을 것이 없습니다
        results.append(dict(picked))

    filled = len(results)
    # 못 채운 회차는 환불합니다. 나누어떨어지지 않으면 **유저 쪽으로** 올립니다.
    refund = (total_price - (total_price * filled) // count) if filled < count else 0
    reward_points = sum(int(r.get("reward_points", 0) or 0) for r in results)

    # **시즌 중에는 경품 포인트도 시즌 포인트로** 들어갑니다(요청 2026-10-08 — 채팅 ·
    # 출석 · 퀘스트와 같은 '얻는 포인트' 이므로 credit_earned 를 그대로 씁니다).
    #   · '시즌 포인트로만'  → 시즌만   · '둘 다' → 기본 + 시즌   · 시즌 아님 → 기본만
    # 환불은 낸 것을 돌려주는 것이라 **낸 포인트 그대로**입니다.
    paused = bool(reward_points) and await base_points_paused(guild.id)
    season_prize = (await credit_earned(guild.id, member.id, reward_points, paused=paused)
                    if reward_points else 0)
    prize_to_season = season_prize > 0
    base_prize = 0 if paused else reward_points

    if currency == SEASON:
        # 환불은 시즌이 방금 끝났어도 돌려줍니다(only_active=False)
        if refund:
            await add_season_points(guild.id, member.id, refund, only_active=False)
        season_balance = remaining + refund + season_prize
        balance = (await add_user_points(guild.id, member.id, base_prize)
                   if base_prize else await get_user_points(guild.id, member.id))
    else:
        season_balance = (await get_season_points(guild.id, member.id)
                          if prize_to_season else None)
        credit = base_prize + refund
        balance = (await add_user_points(guild.id, member.id, credit)
                   if credit else remaining)

    # 역할은 같은 것을 여러 번 뽑아도 한 번만 줍니다
    role_given, role_failed = [], []
    for rid in dict.fromkeys(int(r.get("reward_role_id", 0) or 0) for r in results):
        if rid <= 0:
            continue
        role = guild.get_role(rid)
        if role is None:
            role_failed.append(f"ID `{rid}` (서버에서 찾을 수 없음)")
            continue
        try:
            await member.add_roles(role)
            role_given.append(role)
        except Exception as e:
            # 무엇을 고쳐야 하는지 남깁니다. 뭉뚱그리면 원인을 못 찾습니다.
            role_failed.append(f"{role.mention} (봇 권한/역할 순서 확인: {type(e).__name__})")

    if filled:
        # 진행도 기록이 실패해도 뽑기 결과에는 영향을 주지 않습니다
        try:
            await bump_quest_progress(guild.id, member.id, "gacha", amount=filled)
            await bump_quest_progress(guild.id, member.id, "wgacha",
                                      amount=filled, weekly=True)
            await bump_event_progress(guild.id, member.id, "gacha", amount=filled)
        except Exception as e:
            print(f"[Quest] 뽑기 진행도 기록 실패: {type(e).__name__}: {e}", flush=True)

    return {
        "error": None,
        "results": results,
        "items": items,              # 재고가 반영된 사본 (품절 판정용)
        "requested": count,
        "filled": filled,
        "spent": total_price - refund,
        "refund": refund,
        "reward_points": reward_points,
        "prize_to_season": prize_to_season,
        "base_prize": base_prize,
        "balance": balance,
        "season_balance": season_balance,
        "currency": currency,
        "role_given": role_given,
        "role_failed": role_failed,
    }


def summarize_results(results: list[dict]) -> list[tuple]:
    """같은 **항목**끼리 묶어 `(id, 이름, 횟수)` 로 돌려줍니다 (많이 나온 순).

    ⚠️ **이름이 아니라 id 로 묶습니다.** 이름이 같은 항목을 두 개 등록하는
    일은 흔한데(`500 포인트` 를 확률만 달리해 두 번 넣는 식), 이름으로 묶으면
    둘이 한 줄로 합쳐지고 **남은 재고는 그중 한쪽 것만** 보입니다. 유저는
    자기가 뽑지 않은 항목의 재고를 보고 "아직 남았네" 하고 또 돌립니다.
    """
    counts: dict = {}
    for r in results:
        key = r.get("id")
        if key is None:                       # id 가 없는 옛 기록 대비
            key = ("name", r.get("name", "알 수 없음"))
        cur = counts.setdefault(key, {"name": r.get("name", "알 수 없음"), "n": 0})
        cur["n"] += 1
    return sorted(((k, v["name"], v["n"]) for k, v in counts.items()),
                  key=lambda t: (-t[2], t[1]))


class GachaPlayView(discord.ui.View):
    def __init__(self, is_out_of_stock: bool = False, settings: dict | None = None,
                 season_active: bool = False):
        # ⚠️ **영구 View 입니다** (`timeout=None` + `setup()` 의 `bot.add_view`).
        # 예전에는 `timeout=180` 이라 유저가 확률표를 3분 넘게 읽고 누르면
        # 아무 핸들러에도 닿지 않아 "적시에 응답하지 않았어요" 만 떴습니다.
        # ephemeral 패널이라 새로고침할 방법도 없어 명령어를 다시 쳐야 했습니다.
        #
        # 영구 View 는 **인스턴스 하나를 모두가 공유**하므로 self 의 버튼
        # 속성을 바꾸면 남의 화면까지 오염됩니다. 상태를 바꿔야 할 때는
        # 새 인스턴스를 만들어 `response.edit_message` 로 교체하세요.
        super().__init__(timeout=None)

        if settings is not None:
            # 시즌 중이면 가격 단위가 🏆(시즌 포인트)입니다 — 누를 때도 시즌 포인트가 빠집니다
            unit = "🏆" if season_active else "P"
            saving = bulk_discount(settings)
            self.roll_gacha.label = f"1회 뽑기 ({settings.get('price', 100):,} {unit})"
            self.roll_gacha_10.label = (
                f"{BULK_COUNT}회 뽑기 ({bulk_price(settings):,} {unit}"
                + (f" · {saving:,} {unit} 할인)" if saving else ")")
            )
            # 예전 '🏆 시즌' 버튼은 화면에 두지 않습니다(버튼 두 개가 같은 일을 합니다).
            # **등록용 영구 View(settings=None)는 그대로 갖고** 있어야, 그 버튼이
            # 남아 있는 옛 패널을 눌러도 처리됩니다.
            self.remove_item(self.season_1)
            self.remove_item(self.season_10)

        if is_out_of_stock:
            for btn in (self.roll_gacha, self.roll_gacha_10, self.season_1, self.season_10):
                btn.disabled = True
                btn.style = discord.ButtonStyle.secondary
                btn.emoji = "🚫"
            self.roll_gacha.label = "🚫 모든 상품 품절 (재고 없음)"
            self.roll_gacha_10.label = "🚫 품절"

    def _sold_out_all(self, items: list[dict]) -> bool:
        return not [i for i in items if i.get("stock", -1) != 0]

    async def _precheck(self, interaction: discord.Interaction, settings: dict):
        """뽑기를 진행할 수 있는 상태인지 봅니다. 문제가 있으면 응답까지 마칩니다.

        1회와 10회가 **같은 검사**를 써야 합니다. 한쪽에만 검사를 넣으면
        다른 쪽으로 우회됩니다.
        """
        is_enabled = settings.get("enabled", True)
        if not is_enabled and not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(
                "⚠️ 현재 럭키 뽑기가 **비활성화** 상태입니다.", ephemeral=True)
            return False

        items = settings.get("items", [])
        if not items:
            await interaction.response.send_message(
                "⚠️ 등록된 뽑기 항목이 없습니다. 관리자에게 문의해 주세요.", ephemeral=True)
            return False

        if self._sold_out_all(items):
            # ⚠️ ephemeral 메시지는 `interaction.message.edit()` 으로 **수정할 수
            # 없습니다** (봇 토큰 PATCH 가 아니라 상호작용 토큰으로만 됩니다).
            # 예전 코드는 그걸 `except: pass` 로 삼켜서, 품절 잠금이 **한 번도
            # 화면에 반영된 적이 없고 로그도 남지 않았습니다.**
            # 컴포넌트 상호작용은 자기 메시지를 `response.edit_message` 로
            # 고칠 수 있으므로 그쪽을 씁니다.
            locked = GachaPlayView(is_out_of_stock=True, settings=settings,
                                   season_active=await current_currency(interaction.guild.id) == SEASON)
            embed = discord.Embed(
                title="⚠️ 모든 뽑기 상품 품절",
                description="현재 남아있는 모든 뽑기 상품의 재고가 소진(품절)되었습니다.",
                color=discord.Color.red())
            try:
                await interaction.response.edit_message(embed=embed, view=locked)
            except Exception as e:
                # 여기까지 실패하면 조용히 넘어가지 않고 알립니다.
                print(f"[Gacha] 품절 패널 갱신 실패: {type(e).__name__}: {e}", flush=True)
                if not interaction.response.is_done():
                    await interaction.response.send_message(embed=embed, ephemeral=True)
            return False

        if not is_valid_100_percent(items):
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="⚠️ 뽑기 실행 불가",
                    description="관리자가 설정한 전체 뽑기 항목들의 **확률 합이 100%가 아니어서** "
                                "현재 뽑기를 진행할 수 없습니다.\n\n"
                                f"📊 **현재 확률 합계**: `{pct_str(get_total_percent(items), exact=True)}%` "
                                "(100%로 설정되어야 함)",
                    color=discord.Color.red()),
                ephemeral=True)
            return False

        return True

    async def _run(self, interaction: discord.Interaction, count: int, total_price: int):
        settings = await get_gacha_settings(interaction.guild.id)
        currency = await current_currency(interaction.guild.id)
        if not await self._precheck(interaction, settings):
            return

        # 10회는 재고 차감이 여러 번이라 3초를 넘길 수 있습니다.
        # 먼저 defer 해 두지 않으면 "상호작용 실패" 가 납니다.
        await interaction.response.defer(ephemeral=True, thinking=True)

        outcome = await perform_rolls(interaction.guild, interaction.user,
                                      settings, count, total_price, currency)

        if outcome.get("error") == "no_season":
            await interaction.followup.send("⚠️ 지금은 시즌 기간이 아니라 시즌 포인트를 쓸 수 없습니다.",
                                            ephemeral=True)
            return
        if outcome.get("error") == "no_points":
            unit = "시즌 포인트" if currency == SEASON else "P"
            mine = "🏆 **현재 시즌 포인트**" if currency == SEASON else "💰 **현재 보유 포인트**"
            await interaction.followup.send(
                embed=discord.Embed(
                    title="⚠️ 포인트 부족",
                    description=f"{count}회 뽑기를 진행하려면 **{total_price:,} {unit}**가 필요합니다.\n\n"
                                f"{mine}: **{outcome['balance']:,}**",
                    color=discord.Color.red()),
                ephemeral=True)
            return

        if outcome["filled"] == 0:
            # 차감분은 perform_rolls 가 전액 환불했습니다
            await interaction.followup.send(
                embed=discord.Embed(
                    title="⚠️ 방금 품절되었습니다",
                    description="뽑을 수 있는 상품의 재고가 방금 모두 소진되었습니다.\n\n"
                                f"💰 차감된 **{total_price:,} {'시즌 포인트' if currency == SEASON else 'P'}**는 "
                                "**전액 환불**되었습니다.",
                    color=discord.Color.orange()),
                ephemeral=True)
            return

        # 뽑은 뒤 품절이 됐어도 **패널을 고치지 않습니다.** 이미 defer 로
        # 응답을 시작해서 그 메시지를 되돌아가 고칠 방법이 없고, 다음 클릭은
        # `_precheck` 가 차감 전에 막아 줍니다. 되지도 않는 수정을 시도하며
        # 예외를 삼키는 코드는 "동작하는 줄 알았다" 를 만들 뿐입니다.
        await self._send_result(interaction, settings, outcome, count, total_price)

    async def _send_result(self, interaction, settings, outcome, count, total_price):
        gacha_title = settings.get("title", "한바헌 럭키 뽑기")
        results = outcome["results"]
        filled = outcome["filled"]

        season = outcome.get("currency") == SEASON
        unit = "시즌 포인트" if season else "P"
        spent_label = "시즌 포인트" if season else "포인트"
        lines = [f"💳 **소모 {spent_label}**: -{outcome['spent']:,} {unit}"]
        if outcome["refund"]:
            lines.append(f"↩️ **환불**: +{outcome['refund']:,} {unit} "
                         f"({count - filled}회분 — 재고가 부족했습니다)")
        if outcome["reward_points"]:
            if outcome.get("prize_to_season") and outcome.get("base_prize"):
                lines.append(f"🎁 **포인트 보상**: +{outcome['reward_points']:,} P · "
                             f"🏆 시즌 포인트 +{outcome['reward_points']:,}")
            elif outcome.get("prize_to_season"):
                lines.append(f"🎁 **포인트 보상**: 🏆 시즌 포인트 +{outcome['reward_points']:,} "
                             "(시즌 중에는 시즌 포인트로 받습니다)")
            else:
                lines.append(f"🎁 **포인트 보상**: +{outcome['reward_points']:,} P")
        if season or outcome.get("season_balance") is not None:
            lines.append(f"🏆 **남은 시즌 포인트**: **{outcome['season_balance']:,}**")
        lines.append(f"💰 **현재 총 보유 포인트**: **{outcome['balance']:,} P**")

        if count == 1:
            item = results[0]
            head = f"🎉 축하합니다! **[{item['name']}]** 에 당첨되셨습니다!"
            stock_text = format_stock_str(
                next((i.get("stock", -1) for i in outcome["items"]
                      if i.get("id") == item.get("id")), -1))
            lines.append(f"📦 **해당 상품 남은 재고**: `{stock_text}`")
        else:
            head = f"🎲 **{filled}회** 뽑기 결과입니다!"

        embed = discord.Embed(
            title=f"{gacha_title} 결과!",
            description=head + "\n\n" + "\n".join(lines),
            color=discord.Color.gold()
        )

        if count > 1:
            summary = summarize_results(results)
            stock_by_id = {i.get("id"): i.get("stock", -1) for i in outcome["items"]}
            body = []
            for item_id, name, got in summary:
                stock_text = format_stock_str(stock_by_id.get(item_id, -1))
                body.append(f"• **{name}** × **{got}** (남은 재고 `{stock_text}`)")
            embed.add_field(name="🎁 당첨 내역", value="\n".join(body)[:1024], inline=False)
        else:
            desc = results[0].get("description", "")
            if desc:
                embed.add_field(name="📝 항목 설명",
                                value=parse_role_mentions(desc), inline=False)

        if outcome["role_given"]:
            embed.add_field(
                name="🎭 지급된 역할",
                value=" ".join(r.mention for r in outcome["role_given"])
                      + "\n-# 같은 역할에 여러 번 당첨돼도 한 번만 지급됩니다.",
                inline=False)
        if outcome["role_failed"]:
            # 조용히 넘어가면 "역할을 왜 못 받았지?" 를 아무도 못 풉니다
            embed.add_field(name="⚠️ 역할 지급 실패",
                            value="\n".join(outcome["role_failed"])[:1024], inline=False)

        embed.set_footer(text=f"{gacha_title} 시스템")

        # ⚠️ **기록을 먼저 남깁니다.** 여기까지 왔다는 것은 포인트가 이미
        # 빠졌고 재고도 이미 줄었다는 뜻입니다. 화면 전송이 실패했다고 그
        # 사실이 없던 일이 되지 않는데, 기록이 뒤에 있으면 그때 **포인트만
        # 사라지고 아무 기록도 없는 상태**가 됩니다 — `/로그` 로도 못 찾고
        # 유저가 문의해도 확인할 방법이 없습니다.
        try:
            await add_activity_log(
                interaction.guild.id, "gacha", interaction.user.id,
                interaction.user.display_name,
                (f"[{results[0]['name']}] 뽑기 당첨 (-{outcome['spent']:,}{'시즌' if season else 'P'})"
                 if count == 1 else
                 f"{filled}연차 뽑기 (-{outcome['spent']:,}{'시즌' if season else 'P'}) — "
                 + ", ".join(f"{n}x{c}" for _, n, c in summarize_results(results))))
        except Exception as e:
            print(f"[Gacha] 활동 기록 실패: {type(e).__name__}: {e}", flush=True)

        try:
            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as e:
            # 이미 지급까지 끝난 상태라 되돌리지 않습니다. 화면만 못 본
            # 것이므로 무엇이 일어났는지 로그에 남깁니다.
            print(f"[Gacha] 결과 전송 실패(지급은 완료됨): "
                  f"{type(e).__name__}: {e}", flush=True)

    @discord.ui.button(label="1회 뽑기 실행", style=discord.ButtonStyle.primary,
                       emoji="🎲", custom_id="gacha_roll_btn")
    async def roll_gacha(self, interaction: discord.Interaction, button: discord.ui.Button):
        settings = await get_gacha_settings(interaction.guild.id)
        await self._run(interaction, 1, int(settings.get("price", 100) or 0))

    @discord.ui.button(label="10회 뽑기", style=discord.ButtonStyle.success,
                       emoji="🎰", custom_id="gacha_roll10_btn")
    async def roll_gacha_10(self, interaction: discord.Interaction, button: discord.ui.Button):
        settings = await get_gacha_settings(interaction.guild.id)
        await self._run(interaction, BULK_COUNT, bulk_price(settings))

    # 예전 패널에 남아 있는 '🏆 시즌' 버튼 — 이제 위 버튼과 똑같이 동작합니다
    # (시즌 중이면 시즌 포인트, 아니면 기본 포인트). 새 패널에는 보이지 않습니다.
    @discord.ui.button(label="시즌 1회", style=discord.ButtonStyle.secondary,
                       emoji="🏆", custom_id="gacha_season_btn", row=1)
    async def season_1(self, interaction: discord.Interaction, button: discord.ui.Button):
        settings = await get_gacha_settings(interaction.guild.id)
        await self._run(interaction, 1, int(settings.get("price", 100) or 0))

    @discord.ui.button(label="시즌 10회", style=discord.ButtonStyle.secondary,
                       emoji="🏆", custom_id="gacha_season10_btn", row=1)
    async def season_10(self, interaction: discord.Interaction, button: discord.ui.Button):
        settings = await get_gacha_settings(interaction.guild.id)
        await self._run(interaction, BULK_COUNT, bulk_price(settings))


# ── Admin Gacha Settings Modals & Views ───────────────────────────────

class SetGachaTitleModal(discord.ui.Modal, title="🏷️ 뽑기 이름/제목 변경"):
    title_input = discord.ui.TextInput(
        label="새로운 뽑기 이름/제목",
        placeholder="예: 럭키 뽑기 파라다이스 또는 🎁 바이옴 랜덤 뽑기",
        default="한바헌 럭키 뽑기",
        required=True,
        max_length=50
    )

    async def on_submit(self, interaction: discord.Interaction):
        try:
            new_title = self.title_input.value.strip()
            if not new_title:
                await interaction.response.send_message("⚠️ 올바른 뽑기 이름을 입력해 주세요.", ephemeral=True)
                return

            settings = await get_gacha_settings(interaction.guild.id)
            # 바뀐 키만 저장합니다. settings 전체를 넘기면 items 가 덮여
            # 진행 중인 뽑기가 깎은 재고가 되살아납니다.
            await save_gacha_settings(interaction.guild.id, {"title": new_title})

            embed = discord.Embed(
                title="🏷️ 뽑기 이름 변경 완료",
                description=f"뽑기 이름이 **{new_title}**(으)로 변경되었습니다.",
                color=discord.Color.green()
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"⚠️ 처리 중 오류가 발생했습니다: `{e}`", ephemeral=True)


class SetGachaDescriptionModal(discord.ui.Modal, title="📝 뽑기 설명/안내 문구 변경"):
    desc_input = discord.ui.TextInput(
        label="뽑기 설명/안내 문구",
        placeholder="예: 🎁 럭키 뽑기에 참여하여 대박 보상과 한정 역할을 노려보세요! (비워두면 표시 안 함)",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=500
    )

    async def on_submit(self, interaction: discord.Interaction):
        try:
            new_desc = self.desc_input.value.strip()
            settings = await get_gacha_settings(interaction.guild.id)
            await save_gacha_settings(interaction.guild.id, {"description": new_desc})

            desc_preview = f"**{new_desc}**" if new_desc else "*설정 안 함 (삭제됨)*"
            embed = discord.Embed(
                title="📝 뽑기 설명 변경 완료",
                description=f"뽑기 설명이 성공적으로 설정/변경되었습니다.\n\n📝 **현재 설정된 설명**:\n{desc_preview}",
                color=discord.Color.green()
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"⚠️ 처리 중 오류가 발생했습니다: `{e}`", ephemeral=True)



class SetGachaPriceModal(discord.ui.Modal, title="💰 1회 뽑기 가격 설정"):
    price_input = discord.ui.TextInput(
        label="1회 뽑기 가격 (포인트)",
        placeholder="예: 100",
        default="100",
        required=True,
        max_length=10
    )

    async def on_submit(self, interaction: discord.Interaction):
        try:
            price = int(self.price_input.value.strip())
            if price <= 0:
                raise ValueError()
        except ValueError:
            await interaction.response.send_message("⚠️ 1 이상의 올바른 숫자를 입력해 주세요.", ephemeral=True)
            return

        settings = await get_gacha_settings(interaction.guild.id)
        await save_gacha_settings(interaction.guild.id, {"price": price})

        embed = discord.Embed(
            title="💰 뽑기 가격 변경 완료",
            description=f"1회 뽑기 가격이 **{price:,} P**로 설정되었습니다.",
            color=discord.Color.green()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


class SetGachaBulkPriceModal(discord.ui.Modal, title="🎰 10회 뽑기 가격 설정"):
    price_input = discord.ui.TextInput(
        label="10회 뽑기 가격 (0 이면 1회 가격의 10배)",
        placeholder="예: 900   ·   0 이면 할인 없이 1회 가격 × 10",
        required=True,
        max_length=12
    )

    async def on_submit(self, interaction: discord.Interaction):
        raw = (self.price_input.value or "").strip()
        # required=True 는 공백을 막지 못합니다. 여기서 거르지 않으면
        # '설정 완료'만 뜨고 값은 그대로인 상태가 됩니다.
        try:
            price = int(raw)
        except ValueError:
            await interaction.response.send_message(
                "⚠️ 숫자만 입력해 주세요 (0 이면 1회 가격의 10배). 설정은 그대로 두었습니다.",
                ephemeral=True)
            return
        if price < 0:
            await interaction.response.send_message(
                "⚠️ 0 이상을 입력해 주세요. 설정은 그대로 두었습니다.", ephemeral=True)
            return

        settings = await get_gacha_settings(interaction.guild.id)
        one = int(settings.get("price", 100) or 0)
        if price > one * BULK_COUNT:
            # 막지는 않습니다 — 막으면 '10회는 프리미엄' 같은 운영을 못 합니다.
            # 다만 유저가 1회를 열 번 누르는 것이 더 싸다는 것은 알려야 합니다.
            notice = (f"\n\n⚠️ 1회를 10번 누르는 것(**{one * BULK_COUNT:,} P**)보다 "
                      "비쌉니다. 의도한 것이 맞는지 확인해 주세요.")
            color = discord.Color.orange()
        else:
            notice = ""
            color = discord.Color.green()

        settings["price_10"] = price          # 아래 안내 문구 계산용 (저장은 부분)
        await save_gacha_settings(interaction.guild.id, {"price_10": price})

        effective = bulk_price(settings)
        saving = bulk_discount(settings)
        await interaction.response.send_message(
            embed=discord.Embed(
                title="🎰 10회 뽑기 가격 변경 완료",
                description=(f"{BULK_COUNT}회 뽑기 가격이 **{effective:,} P** 로 설정되었습니다."
                             + (f"\n💸 1회 × {BULK_COUNT} 대비 **{saving:,} P 할인**됩니다."
                                if saving else
                                "\n-# `0` 이라 1회 가격의 10배(할인 없음)로 계산합니다."
                                if price == 0 else "")
                             + notice),
                color=color),
            ephemeral=True)


class AddGachaItemModal(discord.ui.Modal, title="➕ 뽑기 당첨 항목 및 역할 추가"):
    item_name = discord.ui.TextInput(
        label="당첨 항목 이름",
        placeholder="예: 💎 1,000P 대박 당첨! 또는 🎭 VIP 역할",
        required=True,
        max_length=50
    )
    percent_input = discord.ui.TextInput(
        label="당첨 확률 (%)",
        placeholder="예: 10 (10% 의미) 또는 0.5 (0.5% 의미)",
        required=True,
        max_length=10
    )
    item_stock_input = discord.ui.TextInput(
        label="재고/수량 (무제한은 -1 입력)",
        placeholder="예: 1 (1개 한정) 또는 -1 (무제한)",
        default="-1",
        required=True,
        max_length=10
    )
    role_id_input = discord.ui.TextInput(
        label="자동 지급 역할 ID (없으면 0 입력)",
        placeholder="예: 1234567890123456789 (0 입력 시 역할 보상 없음)",
        default="0",
        required=True,
        max_length=25
    )
    reward_points_input = discord.ui.TextInput(
        label="포인트 보상 (없으면 0 입력)",
        placeholder="예: 1000 (0 입력 시 포인트 지급 없음)",
        default="0",
        required=True,
        max_length=10
    )

    async def on_submit(self, interaction: discord.Interaction):
        try:
            percent = float(self.percent_input.value.strip())
            stock = int(self.item_stock_input.value.strip())
            role_id = int(self.role_id_input.value.strip())
            reward_pts = int(self.reward_points_input.value.strip())
            if percent <= 0 or percent > 100 or (stock < 0 and stock != -1) or role_id < 0 or reward_pts < 0:
                raise ValueError()
        except ValueError:
            await interaction.response.send_message("⚠️ 확률(0~100), 재고(-1 이상), 역할 ID(숫자), 포인트 보상을 정확히 입력해 주세요.", ephemeral=True)
            return

        settings = await get_gacha_settings(interaction.guild.id)
        items = settings.get("items", [])
        # ⚠️ `max(id)+1` 로 만들면 **두 관리자가 동시에 추가할 때 같은 id 가
        # 두 번 나옵니다.** 그러면 재고 수정(`items.$.stock`)이 엉뚱한 항목을
        # 고치고 삭제는 둘 다 지웁니다. 서버에서 원자적으로 받아 옵니다.
        new_id = await next_gacha_item_id(interaction.guild.id)

        new_item = {
            "id": new_id,
            "name": self.item_name.value.strip(),
            "percent": percent,
            "weight": percent,
            "stock": stock,
            "reward_role_id": role_id,
            "reward_points": reward_pts,
            "description": ""
        }
        await add_gacha_item(interaction.guild.id, new_item)
        items.append(new_item)                # 아래 확률 합계 계산용

        current_total = get_total_percent(items)
        status_msg = "✅ 전체 확률 합계가 **100%**로 완벽합니다! (뽑기 플레이 가능)" if abs(current_total - 100.0) < 0.001 else f"⚠️ 현재 총 확률 합계가 **{pct_str(current_total, exact=True)}%**입니다. (100%로 맞춰야 뽑기 생성 가능)"

        role_info = ""
        if role_id > 0:
            role = interaction.guild.get_role(role_id)
            role_info = f" | 지급 역할: {role.mention}" if role else f" | 지급 역할 ID: `{role_id}`"

        stock_str = format_stock_str(stock)
        embed = discord.Embed(
            title="✅ 뽑기 항목 추가 완료",
            description=f"**{new_item['name']}** 항목(확률: **{pct_str(percent)}%** | 재고: `{stock_str}`{role_info})이 추가되었습니다.\n\n{status_msg}",
            color=discord.Color.green() if abs(current_total - 100.0) < 0.001 else discord.Color.orange()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


class DeleteGachaItemSelect(discord.ui.Select):
    def __init__(self, items: list[dict]):
        sorted_items = sorted(items, key=lambda x: float(x.get("percent", x.get("weight", 0))))
        options = [
            discord.SelectOption(
                label=f"{item['name']} [{format_stock_str(item.get('stock', -1))}]",
                value=str(item["id"]),
                description=f"확률: {pct_str(item.get('percent', item.get('weight', 0)))}% | 역할 ID: {item.get('reward_role_id', 0)}",
                emoji="🗑️"
            )
            for item in sorted_items
        ]
        super().__init__(placeholder="삭제할 뽑기 항목을 선택하세요...", options=options)

    async def callback(self, interaction: discord.Interaction):
        target_id = int(self.values[0])
        settings = await get_gacha_settings(interaction.guild.id)
        items = settings.get("items", [])

        deleted_item = next((item for item in items if item.get("id") == target_id), None)
        new_items = [item for item in items if item.get("id") != target_id]

        await remove_gacha_item(interaction.guild.id, target_id)

        item_name = deleted_item["name"] if deleted_item else "선택 항목"
        current_total = get_total_percent(new_items)
        status_msg = "✅ 전체 확률 합계가 **100%**로 완벽합니다!" if abs(current_total - 100.0) < 0.001 else f"⚠️ 현재 총 확률 합계가 **{pct_str(current_total, exact=True)}%**입니다. (100%로 맞춰야 뽑기 생성 가능)"

        embed = discord.Embed(
            title="🗑️ 뽑기 항목 삭제 완료",
            description=f"**{item_name}** 항목이 삭제되었습니다.\n\n{status_msg}",
            color=discord.Color.orange()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


class DeleteGachaItemView(discord.ui.View):
    def __init__(self, items: list[dict]):
        super().__init__(timeout=180)
        self.add_item(DeleteGachaItemSelect(items))


class SetGachaItemStockModal(discord.ui.Modal, title="📦 뽑기 항목 재고 충전/수정"):
    def __init__(self, item_id: int, current_stock: int, item_name: str):
        super().__init__()
        self.item_id = item_id
        self.item_name = item_name

        default_val = str(current_stock)
        self.stock_input = discord.ui.TextInput(
            label=f"새로운 재고 수량 (무제한: -1)",
            placeholder="예: 10 (10개로 충전) 또는 -1 (무제한)",
            default=default_val,
            required=True,
            max_length=10
        )
        self.add_item(self.stock_input)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            new_stock = int(self.stock_input.value.strip())
            if new_stock < 0 and new_stock != -1:
                raise ValueError()
        except ValueError:
            await interaction.response.send_message("⚠️ 0 이상의 수량 또는 무제한(-1)을 숫자로 입력해 주세요.", ephemeral=True)
            return

        settings = await get_gacha_settings(interaction.guild.id)
        items = settings.get("items", [])

        target_item = None
        for item in items:
            if item.get("id") == self.item_id:
                item["stock"] = new_stock
                target_item = item
                break

        if not target_item:
            await interaction.response.send_message("⚠️ 대상 항목을 찾을 수 없습니다.", ephemeral=True)
            return

        if not await set_gacha_item_stock(interaction.guild.id, self.item_id, new_stock):
            await interaction.response.send_message(
                "⚠️ 대상 항목을 찾을 수 없습니다.", ephemeral=True)
            return

        stock_str = format_stock_str(new_stock)
        embed = discord.Embed(
            title="📦 재고 수정/충전 완료",
            description=f"**{target_item['name']}** 항목의 재고가 **`{stock_str}`**(으)로 변경/충전되었습니다.",
            color=discord.Color.green()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


class RefillGachaItemStockSelect(discord.ui.Select):
    def __init__(self, items: list[dict]):
        sorted_items = sorted(items, key=lambda x: float(x.get("percent", x.get("weight", 0))))
        options = [
            discord.SelectOption(
                label=f"{item['name'][:25]} [{format_stock_str(item.get('stock', -1))}]",
                value=str(item["id"]),
                description=f"현재 재고: {format_stock_str(item.get('stock', -1))} | 확률: {pct_str(item.get('percent', item.get('weight', 0)))}%",
                emoji="📦" if item.get('stock', -1) != 0 else "🚫"
            )
            for item in sorted_items
        ]
        super().__init__(placeholder="재고를 충전/수정할 뽑기 항목을 선택하세요...", options=options)

    async def callback(self, interaction: discord.Interaction):
        item_id = int(self.values[0])
        settings = await get_gacha_settings(interaction.guild.id)
        items = settings.get("items", [])
        item = next((i for i in items if i.get("id") == item_id), None)
        if not item:
            await interaction.response.send_message("⚠️ 선택 항목 정보를 찾을 수 없습니다.", ephemeral=True)
            return

        modal = SetGachaItemStockModal(
            item_id=item_id,
            current_stock=item.get("stock", -1),
            item_name=item.get("name", "항목")
        )
        await interaction.response.send_modal(modal)


class RefillGachaItemStockView(discord.ui.View):
    def __init__(self, items: list[dict]):
        super().__init__(timeout=180)
        self.add_item(RefillGachaItemStockSelect(items))



class GachaSettingsSelect(discord.ui.Select):
    def __init__(self, current_settings: dict):
        saving = bulk_discount(current_settings)
        bulk_price_summary = (f"현재 {bulk_price(current_settings):,} P"
                              + (f" ({saving:,} P 할인)" if saving
                                 else " (할인 없음 — 1회 가격의 10배)"))
        self.current_settings = current_settings
        is_enabled = current_settings.get("enabled", True)
        gacha_title = current_settings.get("title", "한바헌 럭키 뽑기")
        gacha_desc = current_settings.get("description", "")
        desc_summary = (gacha_desc[:20] + "...") if len(gacha_desc) > 20 else (gacha_desc if gacha_desc else "미설정")
        options = [
            discord.SelectOption(
                label="시스템 활성화 / 비활성화",
                value="toggle",
                description=f"현재: {'✅ 활성화' if is_enabled else '❌ 비활성화'}",
                emoji="⚡"
            ),
            discord.SelectOption(
                label="뽑기 이름/제목 변경",
                value="title",
                description=f"현재 이름: {gacha_title[:25]}",
                emoji="🏷️"
            ),
            discord.SelectOption(
                label="뽑기 설명/안내 문구 변경",
                value="desc",
                description=f"현재 설명: {desc_summary}",
                emoji="📝"
            ),
            discord.SelectOption(label="1회 뽑기 가격 설정", value="price", description="뽑기 1회에 필요한 포인트를 변경합니다.", emoji="💰"),
            discord.SelectOption(label="10회 뽑기 가격 설정", value="price10",
                                 description=bulk_price_summary, emoji="🎰"),
            discord.SelectOption(label="당첨 항목 및 역할 추가", value="add", description="새로운 당첨 항목, 확률(%), 수량, 지급 역할을 추가합니다.", emoji="➕"),
            discord.SelectOption(label="당첨 항목 재고 수정/충전", value="stock", description="일부분/전부 품절된 뽑기 항목의 재고를 수정 및 충전합니다.", emoji="📦"),
            discord.SelectOption(label="당첨 항목 삭제", value="delete", description="등록된 뽑기 당첨 항목을 삭제합니다.", emoji="🗑️"),
            discord.SelectOption(label="전체 확률표 및 재고 확인", value="list", description="현재 등록된 항목, 역할, 총 100% 달성 여부를 확인합니다.", emoji="📊"),
        ]
        super().__init__(placeholder="뽑기 관리 기능을 선택하세요...", options=options)

    async def callback(self, interaction: discord.Interaction):
        choice = self.values[0]

        if choice == "toggle":
            current = self.current_settings.get("enabled", True)
            new_val = not current
            await save_gacha_settings(interaction.guild.id, {"enabled": new_val})
            status = "✅ 활성화" if new_val else "❌ 비활성화"
            embed = discord.Embed(
                title="⚡ 럭키 뽑기 시스템 상태 변경",
                description=f"럭키 뽑기가 **{status}** 상태로 변경되었습니다.",
                color=discord.Color.green() if new_val else discord.Color.red()
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)

        elif choice == "title":
            modal = SetGachaTitleModal()
            modal.title_input.default = self.current_settings.get("title", "한바헌 럭키 뽑기")
            await interaction.response.send_modal(modal)

        elif choice == "desc":
            settings = await get_gacha_settings(interaction.guild.id)
            modal = SetGachaDescriptionModal()
            modal.desc_input.default = settings.get("description", "")
            await interaction.response.send_modal(modal)

        elif choice == "price":
            settings = await get_gacha_settings(interaction.guild.id)
            modal = SetGachaPriceModal()
            modal.price_input.default = str(settings.get("price", 100))
            await interaction.response.send_modal(modal)

        elif choice == "price10":
            settings = await get_gacha_settings(interaction.guild.id)
            modal = SetGachaBulkPriceModal()
            modal.price_input.default = str(settings.get("price_10", 0) or 0)
            await interaction.response.send_modal(modal)

        elif choice == "add":
            await interaction.response.send_modal(AddGachaItemModal())

        elif choice == "stock":
            settings = await get_gacha_settings(interaction.guild.id)
            items = settings.get("items", [])
            if not items:
                await interaction.response.send_message("⚠️ 재고를 변경/충전할 등록된 뽑기 항목이 없습니다.", ephemeral=True)
                return
            await interaction.response.send_message("📦 재고를 수정/충전할 항목을 선택하세요.", view=RefillGachaItemStockView(items), ephemeral=True)

        elif choice == "delete":
            settings = await get_gacha_settings(interaction.guild.id)
            items = settings.get("items", [])
            if not items:
                await interaction.response.send_message("⚠️ 삭제할 등록된 뽑기 항목이 없습니다.", ephemeral=True)
                return
            await interaction.response.send_message("🗑️ 삭제할 항목을 아래 메뉴에서 선택하세요.", view=DeleteGachaItemView(items), ephemeral=True)

        elif choice == "list":
            settings = await get_gacha_settings(interaction.guild.id)
            gacha_title = settings.get("title", "한바헌 럭키 뽑기")
            price = settings.get("price", 100)
            items = settings.get("items", [])

            if not items:
                embed = discord.Embed(
                    title=f"📊 {gacha_title} 확률표 및 재고 현황",
                    description=f"💰 **1회 뽑기 가격**: {price:,} P\n\n현재 등록된 뽑기 항목이 없습니다.",
                    color=discord.Color.gold()
                )
            else:
                total_pct = get_total_percent(items)
                status_str = "✅ **100.00%** (뽑기 정상 가동 가능)" if abs(total_pct - 100.0) < 0.001 else f"⚠️ **{pct_str(total_pct, exact=True)}%** (100%가 되지 않아 뽑기 생성 불가!)"

                embed = discord.Embed(
                    title=f"📊 {gacha_title} 확률표 및 재고 현황",
                    description=f"💰 **1회 뽑기 가격**: **{price:,} P** | 총 **{len(items)}개** 항목\n"
                                f"📊 **전체 확률 합계**: {status_str}",
                    color=discord.Color.green() if abs(total_pct - 100.0) < 0.001 else discord.Color.red()
                )
                sorted_items = sorted(items, key=lambda x: float(x.get("percent", x.get("weight", 0))))
                for item in sorted_items:
                    pct = float(item.get("percent", item.get("weight", 0)))
                    stock_str = format_stock_str(item.get("stock", -1))
                    reward_pts = item.get("reward_points", 0)
                    role_id = item.get("reward_role_id", 0)

                    extra_info = []
                    if reward_pts > 0:
                        extra_info.append(f"+{reward_pts:,}P")
                    if role_id > 0:
                        role = interaction.guild.get_role(role_id)
                        role_str = role.mention if role else f"ID: {role_id}"
                        extra_info.append(f"역할: {role_str}")

                    extra_str = f" ({', '.join(extra_info)})" if extra_info else ""

                    embed.add_field(
                        name=f"🎲 {item['name']}",
                        value=f"• **확률**: `{pct_str(pct)}%` | **재고**: `{stock_str}`{extra_str}",
                        inline=False
                    )
            await interaction.response.send_message(embed=embed, ephemeral=True)


class GachaSettingsView(discord.ui.View):
    def __init__(self, current_settings: dict):
        super().__init__(timeout=180)
        self.add_item(GachaSettingsSelect(current_settings))


# ── Cog ───────────────────────────────────────────────────────────────

class Gacha(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="뽑기", description="포인트를 소비하여 무작위 보상 및 역할을 획득하는 럭키 뽑기를 진행합니다.")
    # DM 에서는 interaction.guild 가 None 이라 AttributeError 로 죽습니다.
    # (실제로 /출석 이 그렇게 터졌습니다). guild_only 를 걸면 디스코드가
    # **DM 목록에 아예 띄우지 않으므로** 방어 코드를 흩뿌릴 필요가 없습니다.
    @app_commands.guild_only()
    async def roll(self, interaction: discord.Interaction):
        # 3초 제한을 넘기지 않도록 **먼저 응답을 예약**합니다. 아래에서 DB 를
        # 읽는데, 몰리는 순간 그 왕복이 3초를 넘기면 10062 로 명령어가 통째로
        # 죽습니다(실측: /포인트랭킹 이 실제로 그렇게 실패했습니다).
        await interaction.response.defer(ephemeral=True, thinking=True)
        settings = await get_gacha_settings(interaction.guild.id)
        is_enabled = settings.get("enabled", True)
        is_admin = interaction.user.guild_permissions.administrator

        if not is_enabled and not is_admin:
            await interaction.followup.send("⚠️ 현재 럭키 뽑기가 **비활성화** 상태입니다. 관리자에게 문의해 주세요.", ephemeral=True)
            return

        gacha_title = settings.get("title", "한바헌 럭키 뽑기")
        price = settings.get("price", 100)
        items = settings.get("items", [])
        # 시즌 중이면 **시즌 포인트로** 뽑습니다. 화면의 가격 · 잔액도 시즌 포인트입니다.
        season_on = await current_currency(interaction.guild.id) == SEASON
        if season_on:
            user_points = await get_season_points(interaction.guild.id, interaction.user.id)
            unit, mine = "🏆", "🏆 **현재 내 시즌 포인트**"
        else:
            user_points = await get_user_points(interaction.guild.id, interaction.user.id)
            unit, mine = "P", "💰 **현재 내 보유 포인트**"

        title_str = f"[비활성화 상태] {gacha_title}" if not is_enabled else gacha_title

        if not items:
            embed = discord.Embed(
                title=title_str,
                description=f"현재 준비된 뽑기 항목이 없습니다!\n\n{mine}: **{user_points:,} {unit}**",
                color=discord.Color.gold() if is_enabled else discord.Color.red()
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        total_pct = get_total_percent(items)
        is_ready = is_valid_100_percent(items)

        # 가용 항목(재고 0이 아닌 항목) 확인
        available_items = [item for item in items if item.get("stock", -1) != 0]
        is_all_sold_out = len(available_items) == 0

        if is_all_sold_out:
            status_line = "⚠️ 현재 모든 뽑기 상품의 재고가 소진되어 뽑기를 진행할 수 없습니다."
        elif not is_ready:
            status_line = f"⚠️ 현재 전체 확률 합계가 **{pct_str(total_pct, exact=True)}%**로 100%가 되지 않아 뽑기를 진행할 수 없습니다."
        else:
            status_line = "✅ 현재 뽑기를 플레이하실 수 있습니다."

        gacha_desc = parse_role_mentions(settings.get("description", "").strip())
        desc_header = f"{gacha_desc}\n\n" if gacha_desc else ""

        embed = discord.Embed(
            title=title_str,
            description=f"{desc_header}"
                        + (f"🏆 **시즌 기간이라 시즌 포인트로 뽑습니다.**\n" if season_on else "")
                        + f"💳 **1회 뽑기 가격**: **{price:,} {unit}**\n"
                        f"🎰 **{BULK_COUNT}회 뽑기 가격**: **{bulk_price(settings):,} {unit}**"
                        + (f" (**{bulk_discount(settings):,} {unit} 할인**)\n"
                           if bulk_discount(settings) else "\n")
                        + f"{mine}: **{user_points:,} {unit}**\n\n"
                        f"{status_line}",
            color=discord.Color.gold() if (is_ready and not is_all_sold_out) else discord.Color.red()
        )

        sorted_items = sorted(items, key=lambda x: float(x.get("percent", x.get("weight", 0))))
        unit_label = await prize_unit(interaction.guild.id)
        for item in sorted_items:
            pct = float(item.get("percent", item.get("weight", 0)))
            stock_str = format_stock_str(item.get("stock", -1))
            role_id = item.get("reward_role_id", 0)

            info_lines = [f"• **확률**: `{pct_str(pct)}%` | **재고**: `{stock_str}`"]
            if role_id > 0:
                role = interaction.guild.get_role(role_id)
                role_info = role.mention if role else f"ID: {role_id}"
                info_lines.append(f"• **보상 혜택**: {role_info}")
            elif item.get("reward_points", 0) > 0:
                info_lines.append(f"• **보상 혜택**: `+{item['reward_points']:,} {unit_label}` 적립")

            embed.add_field(
                name=f"🎁 {item['name']}",
                value="\n".join(info_lines),
                inline=False
            )
        embed.set_footer(text="당첨된 역할이나 포인트는 계정에 즉시 지급됩니다.")

        view = GachaPlayView(is_out_of_stock=is_all_sold_out, settings=settings,
                             season_active=season_on)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    @app_commands.command(name="뽑기설정", description="[관리자 전용] 럭키 뽑기의 이름, 설명, 가격, 당첨 항목, 확률(총합 100%), 재고, 역할을 관리합니다.")
    # DM 에서는 interaction.guild 가 None 이라 AttributeError 로 죽습니다.
    # (실제로 /출석 이 그렇게 터졌습니다). guild_only 를 걸면 디스코드가
    # **DM 목록에 아예 띄우지 않으므로** 방어 코드를 흩뿌릴 필요가 없습니다.
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def gacha_settings(self, interaction: discord.Interaction):
        # 3초 제한을 넘기지 않도록 **먼저 응답을 예약**합니다. 아래에서 DB 를
        # 읽는데, 몰리는 순간 그 왕복이 3초를 넘기면 10062 로 명령어가 통째로
        # 죽습니다(실측: /포인트랭킹 이 실제로 그렇게 실패했습니다).
        await interaction.response.defer(ephemeral=True, thinking=True)
        settings = await get_gacha_settings(interaction.guild.id)
        enabled_text = "✅ 활성화" if settings.get("enabled", True) else "❌ 비활성화"
        gacha_title = settings.get("title", "한바헌 럭키 뽑기")
        gacha_desc = parse_role_mentions(settings.get("description", "").strip())
        desc_text = f"**{gacha_desc}**" if gacha_desc else "*미설정*"
        price = settings.get("price", 100)
        items = settings.get("items", [])

        total_pct = get_total_percent(items)
        status_str = "✅ **100%** (뽑기 생성 완료)" if is_valid_100_percent(items) else f"⚠️ **{pct_str(total_pct, exact=True)}%** (100%를 맞춰야 유저들이 뽑기 가능)"

        embed = discord.Embed(
            title="⚙️ 럭키 뽑기, 역할 및 재고 설정 관리",
            description=f"🏷️ 뽑기 이름: **{gacha_title}**\n"
                        f"📝 뽑기 설명: {desc_text}\n"
                        f"⚡ 시스템 상태: **{enabled_text}** | 💰 1회: **{price:,} P** | "
                        f"🎰 {BULK_COUNT}회: **{bulk_price(settings):,} P**"
                        + (f" (**{bulk_discount(settings):,} P 할인**)" if bulk_discount(settings) else "")
                        + f" | 📦 항목 수: **{len(items)}개**\n"
                        f"📊 전체 확률 합계: {status_str}\n\n"
                        "아래 드롭다운 메뉴에서 설정할 관리 기능을 선택하세요.",
            color=discord.Color.dark_gold()
        )
        view = GachaSettingsView(settings)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)


async def setup(bot):
    await bot.add_cog(Gacha(bot))
    # 봇 재시작 후에도, 그리고 패널을 띄워 둔 지 3분이 지나도 버튼이
    # 동작해야 합니다. custom_id 가 고정값이라 등록만 하면 됩니다.
    bot.add_view(GachaPlayView())
