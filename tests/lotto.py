"""복권 (1~25 중 5개, 토요일 20:00 KST 추첨).

이 스위트가 지키는 것은 세 가지입니다.

  1. **포인트가 새지 않는다** — 자리 선점과 차감의 순서, 한도 초과 되돌리기.
  2. **추첨이 두 번 일어나지 않는다** — 번호가 바뀌거나 상금이 두 번 나가면
     되돌릴 수 없습니다. 재시작·루프 중복을 가정하고 찌릅니다.
  3. **경계에서 표를 살 수 없다** — 토요일 20:00 정각부터는 다음 회차.
     여기가 새면 당첨 번호를 보고 표를 사는 구멍이 됩니다.
"""
import sys, os, tempfile, asyncio, datetime

PROJ = sys.argv[1]
sys.path.insert(0, PROJ)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(tempfile.mkdtemp())
os.environ["MONGO_URI"] = ""

import discord
import fakemongo as FM
import fakes as F
from fakes import GUILD, Member, Inter, note, drain

DB_COLS = ["users_col", "shop_col", "gacha_col", "logs_col", "blacklist_col",
           "boost_col", "snapshot_col", "backup_set_col", "checkin_set_col",
           "quest_set_col", "quest_prog_col", "quest_state_col", "event_col",
           "lotto_set_col", "lotto_ticket_col", "lotto_draw_col"]

KST = datetime.timezone(datetime.timedelta(hours=9))


def at(s):
    return datetime.datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=KST)


class StubBot:
    """주기 작업이 길드를 훑을 수 있게 최소한만 흉내 냅니다."""

    def __init__(self, guild):
        self.guilds = [guild]

    async def wait_until_ready(self):
        return None


async def main():
    import utils.database as db

    cols = FM.install(db, DB_COLS)
    if hasattr(db, "_AFTER"):
        db._AFTER = FM.AFTER
    print(f"가짜 컬렉션 장착: {len(cols)}개")

    from discord.ext import commands
    bot = commands.Bot(command_prefix="!", intents=discord.Intents.all())
    for f in sorted(os.listdir(os.path.join(PROJ, "cogs"))):
        if f.endswith(".py") and not f.startswith("__"):
            await bot.load_extension(f"cogs.{f[:-3]}")
    cog = bot.get_cog("Lotto")

    # ⚠️ load_extension 은 sys.modules 를 새 인스턴스로 덮어씁니다.
    LT = sys.modules["cogs.lotto"]
    note(LT.__dict__ is cog.run_draw.__func__.__globals__,
         "테스트가 실제로 도는 모듈을 잡았는지")

    async def setup(**over):
        base = {"enabled": True, "price": 500, "prize_1": 1000000,
                "prize_2": 50000, "prize_3": 3000, "prize_4": 500,
                "prize_5": 100, "announce_channel_id": None}
        base.update(over)
        for c in ("lotto_set_col", "lotto_ticket_col", "lotto_draw_col", "users_col"):
            if getattr(getattr(db, c), "docs", None) is not None:
                getattr(db, c).docs.clear()
        await db.save_lotto_settings(GUILD.id, base)
        return base

    async def give(uid, amount):
        await db.add_user_points(GUILD.id, uid, amount)

    print("=== 1. 회차 경계 — 토요일 20:00 정각부터는 다음 회차 ===")
    # 여기가 새면 **당첨 번호를 보고 표를 사는** 구멍이 됩니다.
    cases = [
        ("2026-09-15 10:00", "2026-09-19", "2026-09-12"),   # 화
        ("2026-09-19 00:00", "2026-09-19", "2026-09-12"),   # 토 새벽
        ("2026-09-19 19:59", "2026-09-19", "2026-09-12"),   # 마감 1분 전
        ("2026-09-19 20:00", "2026-09-26", "2026-09-19"),   # 정각 → 다음 회차
        ("2026-09-19 20:01", "2026-09-26", "2026-09-19"),
        ("2026-09-20 03:00", "2026-09-26", "2026-09-19"),   # 일
    ]
    for s, want_cur, want_prev in cases:
        n = at(s)
        got_cur = db.lotto_round_key(n)
        got_prev = db.lotto_prev_round_key(n)
        note(got_cur == want_cur and got_prev == want_prev,
             f"{s} → 접수 {want_cur} / 직전 {want_prev}", f"{got_cur} / {got_prev}")

    note(db.lotto_draw_at(at("2026-09-15 10:00")).hour == 20, "추첨은 20시")
    note(db.lotto_draw_at(at("2026-09-15 10:00")).weekday() == 5, "추첨은 토요일")

    print("")
    print("=== 2. 번호 입력 — 구분자를 외우게 하면 안 됩니다 ===")
    ok_cases = ["1 7 12 19 25", "1,7,12,19,25", "1/7/12/19/25", " 25 1 19 7 12 "]
    for raw in ok_cases:
        got, err = LT.parse_numbers(raw)
        note(got == [1, 7, 12, 19, 25], f"{raw!r} → 정렬해서 받음", f"{got} {err}")

    bad_cases = [
        ("1 2 3 4", "개수 부족"), ("1 2 3 4 5 6", "개수 초과"),
        ("1 1 2 3 4", "중복"), ("0 1 2 3 4", "범위 아래"),
        ("1 2 3 4 26", "범위 위"), ("", "빈 입력"), ("가나다", "숫자 아님"),
    ]
    for raw, why in bad_cases:
        got, err = LT.parse_numbers(raw)
        note(got is None and bool(err), f"{why}: {raw!r} 를 거절하고 이유를 말함", repr(err))

    print("")
    print("  자동 선택도 규칙을 지켜야 합니다")
    for _ in range(200):
        p = LT.pick_random()
        if not (len(p) == 5 and len(set(p)) == 5 and all(1 <= n <= 25 for n in p)
                and p == sorted(p)):
            note(False, "자동 선택이 규칙을 벗어남", str(p))
            break
    else:
        note(True, "자동 선택 200회 전부 5개·중복 없음·1~25·정렬")

    print("")
    print("=== 3. 구매 — 포인트와 표가 정확히 맞아야 ===")
    await setup()
    await give(101, 2000)
    it = Inter(uid=101)
    await LT.buy_ticket(it, [1, 2, 3, 4, 5])
    await drain()
    note(await db.get_user_points(GUILD.id, 101) == 1500, "가격만큼 빠짐",
         str(await db.get_user_points(GUILD.id, 101)))
    mine = await db.get_lotto_tickets(GUILD.id, 101, db.lotto_round_key())
    note(mine["count"] == 1, "표 1장", str(mine["count"]))
    note(mine["tickets"][0]["numbers"] == [1, 2, 3, 4, 5], "번호가 그대로 저장됨")
    sent = it.response.sent or it.followup.sent
    note(sent and sent[0].get("ephemeral"), "ephemeral 응답", F.outcome(it))

    print("")
    print("  ⚠️ 한도(5장)를 넘길 수 없어야 합니다")
    await setup()
    await give(102, 10000)
    for i in range(7):
        it = Inter(uid=102)
        await LT.buy_ticket(it, LT.pick_random())
        await drain()
    mine = await db.get_lotto_tickets(GUILD.id, 102, db.lotto_round_key())
    note(mine["count"] == 5, "5장에서 멈춤", str(mine["count"]))
    note(await db.get_user_points(GUILD.id, 102) == 10000 - 5 * 500,
         "**6번째부터는 포인트도 안 빠짐**",
         str(await db.get_user_points(GUILD.id, 102)))

    print("")
    print("  ⚠️ 동시에 눌러도 5장을 넘으면 안 됩니다")
    await setup()
    await give(103, 10000)
    await asyncio.gather(*[
        LT.buy_ticket(Inter(uid=103), LT.pick_random())
        for _ in range(12)])
    await drain()
    mine = await db.get_lotto_tickets(GUILD.id, 103, db.lotto_round_key())
    spent = 10000 - await db.get_user_points(GUILD.id, 103)
    note(mine["count"] == 5, "동시 12번이어도 5장", str(mine["count"]))
    note(spent == 5 * 500, "빠진 포인트도 5장분뿐", f"{spent}P")

    print("")
    print("  ⚠️ 포인트가 모자라면 표도 남지 않아야 합니다 (자리 되돌리기)")
    await setup()
    await give(104, 300)                      # 가격 500 에 못 미침
    it = Inter(uid=104)
    await LT.buy_ticket(it, [1, 2, 3, 4, 5])
    await drain()
    mine = await db.get_lotto_tickets(GUILD.id, 104, db.lotto_round_key())
    note(mine["count"] == 0, "잡아 둔 자리가 되돌려짐", str(mine["count"]))
    note(not mine["tickets"], "표도 남지 않음", str(mine["tickets"]))
    note(await db.get_user_points(GUILD.id, 104) == 300, "포인트는 그대로")

    print("")
    print("  닫혀 있으면 살 수 없어야 합니다")
    await setup(enabled=False)
    await give(105, 5000)
    it = Inter(uid=105)
    await LT.buy_ticket(it, [1, 2, 3, 4, 5])
    await drain()
    note(await db.get_user_points(GUILD.id, 105) == 5000, "포인트가 빠지지 않음")
    note((await db.get_lotto_tickets(GUILD.id, 105, db.lotto_round_key()))["count"] == 0,
         "표도 생기지 않음")

    print("")
    print("=== 4. 추첨과 지급 ===")
    await setup()
    rnd = db.lotto_prev_round_key()

    async def put_ticket(uid, numbers):
        await db.reserve_lotto_ticket(GUILD.id, uid, rnd, numbers)

    await put_ticket(201, [1, 2, 3, 4, 5])      # 1등이 될 표
    await put_ticket(202, [1, 2, 3, 4, 6])      # 4개
    await put_ticket(203, [1, 2, 3, 20, 21])    # 3개
    await put_ticket(204, [21, 22, 23, 24, 25])  # 0개
    await db.record_lotto_draw(GUILD.id, rnd, [1, 2, 3, 4, 5])

    res = await cog.run_draw(GUILD, announce=False)
    note(res["ok"], "추첨이 돌았음", str(res))
    note(res["numbers"] == [1, 2, 3, 4, 5], "번호는 이미 확정된 것을 씀", str(res["numbers"]))
    note(res["entrants"] == 4, "참가 4명", str(res["entrants"]))
    note(res["winners"] == 3, "당첨 3명 (0개 맞춘 사람 제외)", str(res["winners"]))
    pts = {u: await db.get_user_points(GUILD.id, u) for u in (201, 202, 203, 204)}
    note(pts[201] == 1000000, "1등 = 5개 일치", str(pts[201]))
    note(pts[202] == 50000, "2등 = 4개 일치", str(pts[202]))
    note(pts[203] == 3000, "3등 = 3개 일치", str(pts[203]))
    note(pts[204] == 0, "0개는 꽝", str(pts[204]))
    note(res["paid"] == 1053000, "지급 합계", str(res["paid"]))

    print("")
    print("  ⚠️ **다시 돌려도 상금이 두 번 나가면 안 됩니다**")
    # 재시작·루프 중복·밀린 추첨 보정이 전부 이 자리를 두 번 칩니다.
    for _ in range(3):
        again = await cog.run_draw(GUILD, announce=False)
    note(again["numbers"] == [1, 2, 3, 4, 5], "번호도 그대로", str(again["numbers"]))
    note(again["winners"] == 0 and again["paid"] == 0,
         "두 번째부터는 지급할 것이 없음", str((again["winners"], again["paid"])))
    pts2 = {u: await db.get_user_points(GUILD.id, u) for u in (201, 202, 203)}
    note(pts2 == {201: 1000000, 202: 50000, 203: 3000},
         "포인트가 늘어나지 않음", str(pts2))

    print("")
    print("  ⚠️ 이미 발표된 번호는 **바뀌면 안 됩니다**")
    before = (await db.get_lotto_draw(GUILD.id, rnd))["numbers"]
    await db.record_lotto_draw(GUILD.id, rnd, [11, 12, 13, 14, 15])
    after = (await db.get_lotto_draw(GUILD.id, rnd))["numbers"]
    note(before == after == [1, 2, 3, 4, 5],
         "다시 기록해도 첫 번호가 유지됨", f"{before} → {after}")

    print("")
    print("  한 사람이 여러 장 샀으면 전부 정산돼야 합니다")
    await setup()
    rnd = db.lotto_prev_round_key()
    for nums in ([1, 2, 3, 4, 5], [1, 2, 3, 4, 6], [21, 22, 23, 24, 25]):
        await db.reserve_lotto_ticket(GUILD.id, 301, rnd, nums)
    await db.record_lotto_draw(GUILD.id, rnd, [1, 2, 3, 4, 5])
    res = await cog.run_draw(GUILD, announce=False)
    note(await db.get_user_points(GUILD.id, 301) == 1000000 + 50000,
         "1등 + 2등이 함께 지급됨", str(await db.get_user_points(GUILD.id, 301)))
    doc = await db.get_lotto_tickets(GUILD.id, 301, rnd)
    note(len(doc.get("results") or []) == 3, "표 3장의 결과가 모두 남음",
         str(len(doc.get("results") or [])))

    print("")
    print("  닫혀 있으면 추첨하지 않아야 합니다")
    await setup(enabled=False)
    res = await cog.run_draw(GUILD, announce=False)
    note(not res.get("ok"), "닫힌 상태에서는 추첨 안 함", str(res))

    print("")
    print("=== 5. ⚠️ 환수율 — 포인트 무한 증식을 막는 유일한 장치 ===")
    # 1개만 맞아도 5등이라 표의 45.6%가 당첨입니다. 운영진이 상금 다섯 개를
    # 손으로 정하는데 그 감이 오지 않습니다.
    odds = db.lotto_odds()
    note(odds["total"] == 53130, "1~25 중 5개 = 53,130가지", str(odds["total"]))
    note(odds[5] == 1, "5개 일치는 1가지")
    note(odds[4] == 100 and odds[3] == 1900, "4개 100 · 3개 1900",
         f"{odds[4]} {odds[3]}")
    note(sum(odds[m] for m in range(6)) == odds["total"], "경우의 수 합이 전체와 같음")

    base = dict(db.DEFAULT_LOTTO_SETTINGS)
    rate = db.lotto_return_rate(base)
    note(0.70 < rate < 0.80, f"기본 설정 환수율이 건전 ({rate * 100:.1f}%)", str(rate))

    # 1개만 맞아도 5등인데 5,000P 를 주면 환수율이 100%를 크게 넘습니다.
    greedy = dict(base)
    greedy["prize_5"] = 5000
    note(db.lotto_return_rate(greedy) > 1.0,
         "5등을 올리면 100%를 넘는 것을 잡아냄",
         f"{db.lotto_return_rate(greedy) * 100:.0f}%")

    print("")
    print("  화면이 **빨간 경고**로 말해야 합니다")
    await setup(**{k: v for k, v in greedy.items()})
    it = Inter(uid=1)
    await LT.send_settings(it)
    await drain()
    sent = (it.response.sent or it.followup.sent)
    note(sent, "설정 화면이 응답함", F.outcome(it))
    note(sent and sent[0].get("ephemeral"), "ephemeral")
    emb = sent[0].get("embed")
    blob = (emb.description or "") + " ".join(
        (f.name or "") + (f.value or "") for f in emb.fields)
    note("100%" in blob and "무너집니다" in blob, "위험을 분명히 말함", blob[:80])
    note(emb.color == discord.Color.red(), "빨간색으로 띄움", str(emb.color))

    print("")
    print("  건전하면 초록으로 확인해 줍니다 (늘 빨간 화면은 무시됩니다)")
    await setup()
    it = Inter(uid=1)
    await LT.send_settings(it)
    await drain()
    emb = (it.response.sent or it.followup.sent)[0].get("embed")
    blob = " ".join((f.name or "") + (f.value or "") for f in emb.fields)
    note("건전" in blob, "정상일 때는 정상이라고 말함", blob[:70])
    note("환수율" in blob, "환수율을 숫자로 보여줌")

    print("")
    print("=== 6. /복권 화면 ===")
    await setup()
    await give(401, 5000)
    it = Inter(uid=401)
    await LT.buy_ticket(it, [2, 4, 6, 8, 10])
    await drain()

    it = Inter(uid=401)
    await cog.lotto.callback(cog, it)
    await drain()
    sent = it.followup.sent or it.response.sent
    note(sent, "/복권 이 응답함", F.outcome(it))
    note(sent and sent[0].get("ephemeral"), "ephemeral")
    emb = sent[0].get("embed")
    blob = (emb.description or "") + " ".join(
        (f.name or "") + (f.value or "") for f in emb.fields)
    note("02" in blob and "10" in blob, "내 번호가 보임", blob[:90])
    note("1/5" in blob or "1/5장" in blob or "**1/5" in blob, "내 표 수가 보임")
    note("소각" in blob, "참가비가 소각된다는 것을 밝힘")

    print("")
    print("  ⚠️ 추첨 뒤에는 **결과가 먼저** 보여야 합니다")
    # 토요일 20시에 사람들이 보러 오는 것이 이것입니다.
    await setup()
    prev = db.lotto_prev_round_key()
    await db.reserve_lotto_ticket(GUILD.id, 402, prev, [1, 2, 3, 4, 5])
    await db.reserve_lotto_ticket(GUILD.id, 402, prev, [9, 9 + 1, 20, 21, 22])
    await db.record_lotto_draw(GUILD.id, prev, [1, 2, 3, 4, 5])
    await cog.run_draw(GUILD, announce=False)

    it = Inter(uid=402)
    await cog.lotto.callback(cog, it)
    await drain()
    emb = (it.followup.sent or it.response.sent)[0].get("embed")
    first = emb.fields[0]
    note("결과" in (first.name or ""), "결과 칸이 맨 위", repr(first.name))
    note("당첨 번호" in (first.value or ""), "당첨 번호를 보여줌")
    note("1등" in (first.value or ""), "내 등수를 알려줌", (first.value or "")[:80])
    note("1,000,000P" in (first.value or "") or "1000000" in (first.value or ""),
         "받은 상금을 알려줌")

    print("")
    print("  표를 안 산 사람에게도 결과는 보여야 합니다")
    it = Inter(uid=403)
    await cog.lotto.callback(cog, it)
    await drain()
    emb = (it.followup.sent or it.response.sent)[0].get("embed")
    blob = " ".join((f.name or "") + (f.value or "") for f in emb.fields)
    note("당첨 번호" in blob, "당첨 번호는 누구나 볼 수 있음")
    note("사지 않으셨습니다" in blob, "표가 없다는 것을 알려줌")

    print("")
    print("=== 7. 버튼·모달이 실제로 동작해야 ===")
    # cog 로드만으로는 NameError 를 잡지 못합니다.
    await setup()
    await give(501, 5000)
    view = LT.LottoPanelView()
    note(view.timeout is None, "패널은 영구 View (timeout=None)")
    ids = [c.custom_id for c in view.children]
    note(all(ids) and len(set(ids)) == len(ids), "custom_id 가 전부 고정·고유", str(ids))

    it = Inter(uid=501)
    await view.auto.callback(it)
    await drain()
    note((await db.get_lotto_tickets(GUILD.id, 501, db.lotto_round_key()))["count"] == 1,
         "[자동 선택] 이 표를 삼")

    it = Inter(uid=501)
    await view.refresh.callback(it)
    await drain()
    note(it.followup.sent and it.followup.sent[0].get("ephemeral"),
         "[새로고침] 이 ephemeral 로 응답", F.outcome(it))

    m = LT.BuyNumbersModal()
    m.numbers._value = "1 2 3 4 5"
    it = Inter(uid=501)
    await m.on_submit(it)
    await drain()
    note((await db.get_lotto_tickets(GUILD.id, 501, db.lotto_round_key()))["count"] == 2,
         "모달 제출이 표를 삼")

    m = LT.BuyNumbersModal()
    m.numbers._value = "1 1 1"
    it = Inter(uid=501)
    await m.on_submit(it)
    await drain()
    note((await db.get_lotto_tickets(GUILD.id, 501, db.lotto_round_key()))["count"] == 2,
         "잘못된 입력은 표를 만들지 않음")

    print("")
    print("  설정 메뉴의 모든 항목이 예외 없이 돌아야 합니다")
    for value in ("show", "price", "prize", "channel", "toggle"):
        sel = LT.LottoSettingsMenu()
        sel._values = [value]
        it = Inter(uid=1)
        try:
            await sel.callback(it)
            await drain()
            got = it.response.sent or it.followup.sent or it.response.modal
            note(bool(got), f"[{value}] 가 응답함", F.outcome(it))
        except Exception as e:
            import traceback
            note(False, f"[{value}] 예외", f"{type(e).__name__}: {e}")
            traceback.print_exc(limit=3)

    print("")
    print("=== 8. 발표 ===")
    await setup(announce_channel_id=GUILD.channels[0].id)
    ch = GUILD.channels[0]
    ch.sent.clear()
    prev = db.lotto_prev_round_key()
    await db.reserve_lotto_ticket(GUILD.id, 601, prev, [1, 2, 3, 4, 5])
    await db.record_lotto_draw(GUILD.id, prev, [1, 2, 3, 4, 5])
    await cog.run_draw(GUILD, announce=True)
    note(ch.sent, "발표 채널에 올라감", str(len(ch.sent)))
    emb = ch.sent[-1].get("embed")
    blob = (emb.description or "") + " ".join(
        (f.name or "") + (f.value or "") for f in emb.fields)
    note("01" in blob and "05" in blob, "당첨 번호가 보임", blob[:70])
    note("<@601>" in blob, "1등을 멘션함", blob[:120])

    am = ch.sent[-1].get("allowed_mentions")
    note(am is not None and am.everyone is False and am.roles is False,
         "**전원·역할 호출은 하지 않음** (매주 부를 일이 아닙니다)", str(am))
    note(am is not None and am.users is True, "당첨자 멘션은 실제로 울림")

    print("")
    print("  발표 채널이 없으면 조용히 넘어가야 합니다 (추첨은 되어야 합니다)")
    await setup(announce_channel_id=None)
    prev = db.lotto_prev_round_key()
    await db.reserve_lotto_ticket(GUILD.id, 602, prev, [1, 2, 3, 4, 5])
    await db.record_lotto_draw(GUILD.id, prev, [1, 2, 3, 4, 5])
    res = await cog.run_draw(GUILD, announce=True)
    note(res.get("ok") and res["paid"] > 0, "발표가 없어도 지급은 됨", str(res["paid"]))

    print("")
    print("=== 9. 밀린 추첨 보정 (무인 운영) ===")
    # 봇이 20:00 에 꺼져 있었으면 그 회차는 영영 추첨되지 않습니다.
    await setup()
    prev = db.lotto_prev_round_key()
    await db.reserve_lotto_ticket(GUILD.id, 701, prev, [1, 2, 3, 4, 5])
    note(await db.get_lotto_draw(GUILD.id, prev) is None, "아직 추첨되지 않은 회차")

    # 실제 봇은 테스트에서 길드를 갖지 않으므로 잠깐 갈아끼웁니다.
    cog.bot = StubBot(GUILD)
    await LT.Lotto.draw_catchup.coro(cog)
    await drain()
    drawn = await db.get_lotto_draw(GUILD.id, prev)
    note(drawn is not None, "보정 루프가 밀린 회차를 추첨함", str(drawn))
    doc = await db.get_lotto_tickets(GUILD.id, 701, prev)
    note(doc.get("paid") is True, "정산까지 끝남")

    print("")
    print("  이미 끝난 회차는 다시 건드리지 않아야 합니다")
    pts_before = await db.get_user_points(GUILD.id, 701)
    await LT.Lotto.draw_catchup.coro(cog)
    await drain()
    note(await db.get_user_points(GUILD.id, 701) == pts_before,
         "보정을 두 번 돌려도 포인트가 그대로", str(pts_before))
    note((await db.get_lotto_draw(GUILD.id, prev))["numbers"] == drawn["numbers"],
         "번호도 그대로")

    print("")
    print("=== 9-2. TTL 이 볼 필드를 쓰기 경로가 채워야 ===")
    # MongoDB TTL 은 **필드가 없는 문서를 지우지 않습니다.** 인덱스만 걸고
    # 쓰기에서 안 채우면 만료는 영원히 일어나지 않습니다(실제로 quest_state
    # 5건, guarded_keys 117건이 그랬습니다).
    await setup()
    rnd2 = db.lotto_round_key()
    await db.reserve_lotto_ticket(GUILD.id, 801, rnd2, [1, 2, 3, 4, 5])
    doc = await db.get_lotto_tickets(GUILD.id, 801, rnd2)
    note(isinstance(doc.get("updated"), datetime.datetime),
         "표 문서에 updated 가 datetime 으로 있음", repr(doc.get("updated")))

    await db.reserve_lotto_ticket(GUILD.id, 801, rnd2, [6, 7, 8, 9, 10])
    doc2 = await db.get_lotto_tickets(GUILD.id, 801, rnd2)
    note(doc2["updated"] >= doc["updated"], "표를 더 사면 updated 가 갱신됨")

    d = await db.record_lotto_draw(GUILD.id, rnd2, [1, 2, 3, 4, 5])
    note(isinstance(d.get("updated"), datetime.datetime),
         "추첨 문서에도 updated 가 있음", repr(d.get("updated")))

    await db.claim_lotto_payout(GUILD.id, 801, rnd2, [], 0)
    doc3 = await db.get_lotto_tickets(GUILD.id, 801, rnd2)
    note(isinstance(doc3.get("updated"), datetime.datetime),
         "정산 뒤에도 updated 가 살아 있음", repr(doc3.get("updated")))

    print("")
    print("=== 10. 등수 계산 ===")
    for m, want in ((5, 1), (4, 2), (3, 3), (2, 4), (1, 5), (0, None)):
        note(LT.rank_of(m) == want, f"{m}개 일치 → {want}등" if want else "0개는 꽝",
             str(LT.rank_of(m)))
    s = dict(db.DEFAULT_LOTTO_SETTINGS)
    note(db.lotto_payout(s, 0) == 0, "0개 상금은 0")
    note(db.lotto_payout(s, 5) == s["prize_1"], "5개 = 1등 상금")
    note(db.lotto_payout(s, 1) == s["prize_5"], "1개 = 5등 상금")

    await bot.close()
    print(f"\n  실패 {len(F.FAILS)}건 / 성공 {len(F.OKS)}건")
    for x in F.FAILS:
        print("   -", x)


asyncio.run(main())
