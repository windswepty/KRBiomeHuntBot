"""10회 뽑기 — 포인트·재고 원자성과 부분 환불.

**Mongo 경로를 가짜 드라이버로 실제 실행합니다.** 10회 뽑기는 재고를
열 번 건드리므로, JSON 폴백만 검증하면 실제로 배포되는 코드를 한 줄도
보지 못합니다.

가장 중요한 것은 **못 뽑았을 때 돈이 남지 않는가** 입니다.
재고가 모자라 7회만 됐는데 10회분을 받으면 그건 포인트를 훔치는 것이고,
반대로 전액 환불하면 상품을 공짜로 주는 것입니다.
"""
import sys, os, tempfile, asyncio

PROJ = sys.argv[1]
sys.path.insert(0, PROJ)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(tempfile.mkdtemp())
os.environ["MONGO_URI"] = ""          # 진짜 연결은 절대 하지 않습니다

import discord
import fakemongo as FM
import fakes as F
from fakes import GUILD, Member, Inter, note, drain

DB_COLS = ["users_col", "settings_col", "shop_col", "gacha_col", "logs_col",
           "blacklist_col", "boost_col", "snapshot_col", "backup_set_col",
           "checkin_set_col", "quest_set_col", "quest_prog_col",
           "quest_state_col", "event_col"]


def items(*specs):
    """(이름, 확률, 재고, 보상P, 역할ID) 목록을 뽑기 항목으로 만듭니다."""
    out = []
    for i, (name, pct, stock, pts, role) in enumerate(specs, start=1):
        out.append({"id": i, "name": name, "percent": pct, "stock": stock,
                    "reward_points": pts, "reward_role_id": role,
                    "description": ""})
    return out


async def main():
    import utils.database as db

    cols = FM.install(db, DB_COLS)
    db._AFTER = FM.AFTER
    db.STORAGE_MODE = "mongo"
    print(f"가짜 컬렉션 장착: {len(cols)}개")

    from discord.ext import commands
    bot = commands.Bot(command_prefix="!", intents=discord.Intents.all())
    for f in sorted(os.listdir(os.path.join(PROJ, "cogs"))):
        if f.endswith(".py") and not f.startswith("__"):
            await bot.load_extension(f"cogs.{f[:-3]}")

    # ⚠️ load_extension 은 sys.modules 를 **새 인스턴스로 덮어씁니다.**
    # 먼저 import 해 두면 버려진 옛 모듈을 잡습니다.
    G = sys.modules["cogs.gacha"]

    member = Member(4242, "뽑는사람", False, guild=GUILD)

    async def setup_gacha(item_list, price=100, price_10=0):
        await db.save_gacha_settings(GUILD.id, {
            "enabled": True, "title": "테스트 뽑기", "description": "",
            "price": price, "price_10": price_10, "items": item_list})
        return await db.get_gacha_settings(GUILD.id)

    async def set_points(amount):
        cur = await db.get_user_points(GUILD.id, member.id)
        if amount > cur:
            await db.add_user_points(GUILD.id, member.id, amount - cur)
        elif amount < cur:
            await db.deduct_user_points(GUILD.id, member.id, cur - amount)
        return await db.get_user_points(GUILD.id, member.id)

    async def stock_of(item_id):
        s = await db.get_gacha_settings(GUILD.id)
        return next((i.get("stock") for i in s["items"] if i.get("id") == item_id), None)

    print("=== 1. 가격 계산 ===")
    s = await setup_gacha(items(("보통", 100.0, -1, 0, 0)), price=100)
    note(G.bulk_price(s) == 1000, "price_10 이 0 이면 1회 가격 × 10", str(G.bulk_price(s)))
    note(G.bulk_discount(s) == 0, "할인 없음")

    s = await setup_gacha(items(("보통", 100.0, -1, 0, 0)), price=100, price_10=900)
    note(G.bulk_price(s) == 900, "설정한 10회 가격을 씀", str(G.bulk_price(s)))
    note(G.bulk_discount(s) == 100, "할인폭이 계산됨", str(G.bulk_discount(s)))

    print("")
    print("=== 1-2. 아주 작은 확률이 0.00% 로 보이면 안 됩니다 ===")
    for v, want in [(0.00001, "0.00001"), (0.005, "0.005"), (1e-12, "1e-12"),
                    (2, "2.00"), (54, "54.00"), (0, "0.00")]:
        note(G.pct_str(v) == want, f"{v} → {want}%", G.pct_str(v))
    note(G.pct_str(99.998, exact=True) == "99.998",
         "합계 99.998 이 100.00 으로 보이지 않음", G.pct_str(99.998, exact=True))
    note(G.pct_str(100, exact=True) == "100.00", "합계 100 은 100.00")

    print("=== 2. 10회 뽑기 기본 ===")
    s = await setup_gacha(items(("꽝", 90.0, -1, 0, 0), ("대박", 10.0, -1, 50, 0)),
                          price=100, price_10=900)
    await set_points(5000)
    out = await G.perform_rolls(GUILD, member, s, G.BULK_COUNT, G.bulk_price(s))
    note(out["error"] is None, "정상 실행")
    note(out["filled"] == 10, "10회 전부 채움", str(out["filled"]))
    note(out["spent"] == 900, "할인가 900P 만 소모", str(out["spent"]))
    note(out["refund"] == 0, "환불 없음")
    expected = 5000 - 900 + out["reward_points"]
    note(out["balance"] == expected, "잔액이 정확함",
         f"{out['balance']} (기대 {expected})")

    print("")
    print("=== 3. 무제한(-1) 재고는 줄지 않아야 ===")
    note(await stock_of(1) == -1, "무제한 항목의 재고가 그대로")

    print("")
    print("=== 4. 재고가 10회분씩 줄어야 ===")
    s = await setup_gacha(items(("한정", 100.0, 30, 0, 0)), price=100)
    await set_points(5000)
    await G.perform_rolls(GUILD, member, s, G.BULK_COUNT, G.bulk_price(s))
    note(await stock_of(1) == 20, "재고 30 → 20", str(await stock_of(1)))

    print("")
    print("=== 5. ⚠️ 재고가 모자라면 그만큼만 환불해야 ===")
    # 여기가 돈이 새는 자리입니다. 전액 환불하면 상품을 공짜로 주고,
    # 환불이 없으면 못 받은 회차 값을 그냥 가져가는 것입니다.
    s = await setup_gacha(items(("딱3개", 100.0, 3, 0, 0)), price=100)
    before = await set_points(5000)
    out = await G.perform_rolls(GUILD, member, s, G.BULK_COUNT, G.bulk_price(s))
    note(out["filled"] == 3, "재고만큼인 3회만 성공", str(out["filled"]))
    note(out["spent"] == 300, "3회분 300P 만 소모", str(out["spent"]))
    note(out["refund"] == 700, "7회분 700P 환불", str(out["refund"]))
    note(out["balance"] == before - 300, "잔액에서 300P 만 빠짐",
         f"{out['balance']} (기대 {before - 300})")
    note(await stock_of(1) == 0, "재고가 0 이 됨", str(await stock_of(1)))

    print("")
    print("  할인가에서 나누어떨어지지 않으면 **유저 쪽으로** 올림")
    s = await setup_gacha(items(("딱3개", 100.0, 3, 0, 0)), price=100, price_10=999)
    before = await set_points(5000)
    out = await G.perform_rolls(GUILD, member, s, G.BULK_COUNT, G.bulk_price(s))
    note(out["refund"] >= 999 - (999 * 3) // 10,
         "환불이 내림으로 깎이지 않음", f"환불 {out['refund']} / 소모 {out['spent']}")
    note(out["spent"] + out["refund"] == 999, "소모 + 환불 = 낸 돈",
         f"{out['spent']} + {out['refund']}")

    print("")
    print("=== 6. 전부 품절이면 전액 환불 ===")
    s = await setup_gacha(items(("품절", 100.0, 0, 0, 0)), price=100)
    before = await set_points(5000)
    out = await G.perform_rolls(GUILD, member, s, G.BULK_COUNT, G.bulk_price(s))
    note(out["filled"] == 0, "한 번도 못 뽑음")
    note(out["refund"] == 1000, "전액 환불", str(out["refund"]))
    note(out["balance"] == before, "잔액이 그대로", f"{out['balance']} (기대 {before})")

    print("")
    print("=== 7. 포인트가 모자라면 아무것도 일어나지 않아야 ===")
    s = await setup_gacha(items(("한정", 100.0, 50, 0, 0)), price=100)
    before = await set_points(500)
    out = await G.perform_rolls(GUILD, member, s, G.BULK_COUNT, G.bulk_price(s))
    note(out["error"] == "no_points", "포인트 부족으로 거부", str(out.get("error")))
    note(await db.get_user_points(GUILD.id, member.id) == before,
         "포인트가 빠지지 않음")
    note(await stock_of(1) == 50, "재고도 줄지 않음", str(await stock_of(1)))

    print("")
    print("=== 8. 포인트 보상은 한 번에 합산 적립 ===")
    s = await setup_gacha(items(("항상50P", 100.0, -1, 50, 0)), price=100)
    before = await set_points(5000)
    out = await G.perform_rolls(GUILD, member, s, G.BULK_COUNT, G.bulk_price(s))
    note(out["reward_points"] == 500, "50P × 10 = 500P", str(out["reward_points"]))
    note(out["balance"] == before - 1000 + 500, "잔액 반영",
         f"{out['balance']} (기대 {before - 1000 + 500})")

    print("")
    print("=== 9. 같은 역할에 여러 번 당첨돼도 한 번만 지급 ===")
    role = GUILD.roles[3]
    s = await setup_gacha(items(("역할당첨", 100.0, -1, 0, role.id)), price=100)
    await set_points(5000)
    member.added.clear()
    out = await G.perform_rolls(GUILD, member, s, G.BULK_COUNT, G.bulk_price(s))
    note(len(out["role_given"]) == 1, "역할 1개만 지급 대상", str(len(out["role_given"])))
    note(member.added.count(role.name) == 1,
         "add_roles 가 한 번만 불림", f"{member.added}")
    note(not out["role_failed"], "실패 없음")

    print("")
    print("  없는 역할이면 조용히 넘어가지 않고 알려야 합니다")
    s = await setup_gacha(items(("유령역할", 100.0, -1, 0, 999999999)), price=100)
    await set_points(5000)
    out = await G.perform_rolls(GUILD, member, s, G.BULK_COUNT, G.bulk_price(s))
    note(len(out["role_failed"]) == 1, "실패를 결과에 담음", str(out["role_failed"]))

    print("")
    print("=== 10. 퀘스트 진행도가 10회분 올라야 ===")
    s = await setup_gacha(items(("보통", 100.0, -1, 0, 0)), price=100)
    await set_points(5000)
    day = db.quest_day()
    key = db._quest_key(GUILD.id, member.id, day)
    doc = await db._db(db.quest_prog_col.find_one, {"_id": key})
    base = ((doc or {}).get("p") or {}).get("gacha", 0)
    await G.perform_rolls(GUILD, member, s, G.BULK_COUNT, G.bulk_price(s))
    doc = await db._db(db.quest_prog_col.find_one, {"_id": key})
    after = ((doc or {}).get("p") or {}).get("gacha", 0)
    note(after - base == 10, "일일 뽑기 진행도 +10", f"{base} → {after}")

    print("")
    print("=== 11. ⚠️ 동시에 10연차를 돌려도 재고 이상 나가면 안 됩니다 ===")
    # 재고 5개에 두 명이 동시에 10연차를 돌립니다. 합쳐서 5개만 나가야 합니다.
    # 읽고 계산해서 쓰면 여기서 10개가 나갑니다.
    s = await setup_gacha(items(("딱5개", 100.0, 5, 0, 0)), price=10)
    a = Member(5001, "A", False, guild=GUILD)
    b = Member(5002, "B", False, guild=GUILD)
    for m in (a, b):
        await db.add_user_points(GUILD.id, m.id, 10000)

    outs = await asyncio.gather(
        G.perform_rolls(GUILD, a, s, G.BULK_COUNT, G.bulk_price(s)),
        G.perform_rolls(GUILD, b, s, G.BULK_COUNT, G.bulk_price(s)),
    )
    total = sum(o["filled"] for o in outs)
    note(total == 5, "두 명이 합쳐 정확히 5개만 받음", f"{[o['filled'] for o in outs]}")
    note(await stock_of(1) == 0, "재고가 음수로 내려가지 않음", str(await stock_of(1)))
    for o, m in zip(outs, (a, b)):
        note(o["spent"] == o["filled"] * 10,
             f"{m.name}: 받은 만큼만 냄", f"{o['spent']}P / {o['filled']}개")

    print("")
    print("=== 11-2. ⚠️ 관리자 저장이 진행 중인 뽑기의 재고를 되살리면 안 됩니다 ===")
    # `save_gacha_settings(settings)` 는 `{\"$set\": settings}` 라 items 배열을
    # 통째로 덮습니다. 관리자가 가격만 바꿔도 그 사이 뽑기가 깎아 둔 재고가
    # 읽었던 시점으로 되돌아가, **한정 상품이 무한정 나갑니다.**
    s = await setup_gacha(items(("한정", 100.0, 30, 0, 0)), price=100)
    await set_points(5000)
    await G.perform_rolls(GUILD, member, s, G.BULK_COUNT, G.bulk_price(s))
    note(await stock_of(1) == 20, "10연차로 재고 30 → 20", str(await stock_of(1)))

    for label, coro in (
        ("가격", db.save_gacha_settings(GUILD.id, {"price": 200})),
        ("이름", db.save_gacha_settings(GUILD.id, {"title": "새 이름"})),
    ):
        await coro
        note(await stock_of(1) == 20, f"{label} 저장 후에도 재고가 20 그대로",
             str(await stock_of(1)))

    print("")
    print("  관리자 저장과 뽑기가 **동시에** 일어나도 마찬가지여야 합니다")
    s = await setup_gacha(items(("한정", 100.0, 30, 0, 0)), price=100)
    await set_points(5000)
    await asyncio.gather(
        G.perform_rolls(GUILD, member, s, G.BULK_COUNT, G.bulk_price(s)),
        db.save_gacha_settings(GUILD.id, {"price": 300}),
    )
    note(await stock_of(1) == 20, "동시 실행에서도 재고 20", str(await stock_of(1)))

    print("")
    print("  재고 충전은 그 항목만 바꿔야 합니다 (다른 항목을 되감지 않게)")
    s = await setup_gacha(items(("A", 50.0, 10, 0, 0), ("B", 50.0, 10, 0, 0)), price=10)
    await set_points(5000)
    await G.perform_rolls(GUILD, member, s, G.BULK_COUNT, G.bulk_price(s))
    a_before, b_before = await stock_of(1), await stock_of(2)
    note(a_before + b_before == 10, "합쳐서 10개 소모됨", f"A{a_before}/B{b_before}")

    await db.set_gacha_item_stock(GUILD.id, 1, 99)
    note(await stock_of(1) == 99, "A 재고만 99 로 설정됨", str(await stock_of(1)))
    note(await stock_of(2) == b_before, "B 재고는 건드리지 않음",
         f"{await stock_of(2)} (기대 {b_before})")

    print("")
    print("  항목 추가·삭제도 배열을 통째로 덮지 않아야 합니다")
    s = await setup_gacha(items(("한정", 100.0, 30, 0, 0)), price=100)
    await set_points(5000)
    await G.perform_rolls(GUILD, member, s, G.BULK_COUNT, G.bulk_price(s))
    await db.add_gacha_item(GUILD.id, {"id": 9, "name": "새항목", "percent": 0.0,
                                       "weight": 0.0, "stock": -1,
                                       "reward_role_id": 0, "reward_points": 0,
                                       "description": ""})
    note(await stock_of(1) == 20, "항목 추가 후에도 재고 20", str(await stock_of(1)))
    after = await db.get_gacha_settings(GUILD.id)
    note(len(after["items"]) == 2, "항목이 2개가 됨", str(len(after["items"])))

    await db.remove_gacha_item(GUILD.id, 9)
    after = await db.get_gacha_settings(GUILD.id)
    note(len(after["items"]) == 1, "삭제하면 1개로 돌아옴", str(len(after["items"])))
    note(await stock_of(1) == 20, "삭제 후에도 재고 20", str(await stock_of(1)))

    print("")
    print("=== 12. ⚠️ 잔액보다 많이 쓸 수 없어야 (연타) ===")
    s = await setup_gacha(items(("보통", 100.0, -1, 0, 0)), price=100)
    c = Member(5003, "C", False, guild=GUILD)
    await db.add_user_points(GUILD.id, c.id, 1000)     # 딱 10회분
    outs = await asyncio.gather(*[
        G.perform_rolls(GUILD, c, s, G.BULK_COUNT, G.bulk_price(s))
        for _ in range(3)
    ])
    paid = [o for o in outs if o.get("error") is None]
    note(len(paid) == 1, "세 번 눌러도 한 번만 통과", f"{len(paid)}건 통과")
    note(await db.get_user_points(GUILD.id, c.id) == 0,
         "포인트가 음수로 내려가지 않음",
         str(await db.get_user_points(GUILD.id, c.id)))

    print("")
    print("=== 13. 1회 뽑기도 같은 엔진을 쓰고 그대로 동작해야 ===")
    s = await setup_gacha(items(("한정", 100.0, 7, 0, 0)), price=100)
    before = await set_points(5000)
    out = await G.perform_rolls(GUILD, member, s, 1, 100)
    note(out["filled"] == 1, "1회 성공")
    note(out["spent"] == 100 and out["refund"] == 0, "100P 소모",
         f"{out['spent']}/{out['refund']}")
    note(out["balance"] == before - 100, "잔액 반영")
    note(await stock_of(1) == 6, "재고 7 → 6", str(await stock_of(1)))

    print("")
    print("=== 13-2. ⚠️ 이름이 같은 항목이 한 줄로 합쳐지면 안 됩니다 ===")
    # 운영진이 `500 포인트` 를 확률만 달리해 두 개 등록하는 일은 흔합니다.
    # 이름으로 묶으면 **남은 재고가 한쪽 것만** 보여, 유저는 자기가 뽑지
    # 않은 항목의 재고를 보고 "아직 남았네" 하고 또 돌립니다.
    res = [{"id": 1, "name": "500 포인트"}, {"id": 1, "name": "500 포인트"},
           {"id": 2, "name": "500 포인트"}, {"id": 3, "name": "꽝"}]
    summary = G.summarize_results(res)
    by_id = {t[0]: t for t in summary}
    note(len(summary) == 3, "id 가 다르면 줄도 따로", str(summary))
    note(by_id[1][2] == 2 and by_id[2][2] == 1, "각자 제 횟수를 셈", str(summary))
    note(all(len(t) == 3 for t in summary), "(id, 이름, 횟수) 로 돌려줌")

    print("")
    print("  화면의 '남은 재고' 도 항목마다 달라야 합니다")
    s = await setup_gacha([
        {"id": 1, "name": "500 포인트", "percent": 50.0, "stock": 9,
         "reward_points": 0, "reward_role_id": 0, "description": ""},
        {"id": 2, "name": "500 포인트", "percent": 50.0, "stock": 2,
         "reward_points": 0, "reward_role_id": 0, "description": ""},
    ], price=100)
    stock_by_id = {i["id"]: i["stock"] for i in s["items"]}
    note(stock_by_id[1] != stock_by_id[2],
         "재고가 서로 다른 동명 항목을 만들었음", str(stock_by_id))
    lines = [f"{name} 재고 {stock_by_id.get(iid)}"
             for iid, name, _ in G.summarize_results(res[:3])]
    note("재고 9" in " ".join(lines) and "재고 2" in " ".join(lines),
         "두 재고가 각각 나옴", str(lines))

    print("")
    print("=== 13-3. ⚠️ 화면 전송이 실패해도 기록은 남아야 ===")
    # 여기까지 왔다면 포인트는 이미 빠지고 재고도 이미 줄었습니다. 화면만
    # 못 보낸 것인데 기록까지 없으면 `/로그` 로도 못 찾고, 유저가 문의해도
    # 확인할 방법이 없습니다.
    s = await setup_gacha(items(("보통", 100.0, -1, 0, 0)), price=100)
    it = Inter(uid=member.id, admin=False)
    await db.add_user_points(GUILD.id, it.user.id, 100000)

    async def boom(*a, **k):
        raise RuntimeError("일부러 낸 전송 실패")

    it.followup.send = boom
    before_logs = len((await db.get_activity_logs(GUILD.id, "gacha")) or [])
    v = G.GachaPlayView(settings=s)
    try:
        await v.roll_gacha_10.callback(it)
    except Exception as e:
        note(False, "전송 실패가 밖으로 터져 나옴", f"{type(e).__name__}: {e}")
    await drain()
    after_logs = len((await db.get_activity_logs(GUILD.id, "gacha")) or [])
    note(after_logs == before_logs + 1,
         "전송이 터져도 활동 기록은 남음", f"{before_logs} → {after_logs}")

    print("")
    print("=== 13-4. ⚠️ 항목 id 는 동시에 발급해도 겹치면 안 됩니다 ===")
    # `max(id)+1` 은 읽고 쓰는 사이에 끼어들면 같은 id 가 두 번 나옵니다.
    # 겹치면 재고 수정(`items.$.stock`)이 엉뚱한 항목을 고치고,
    # 삭제는 **둘 다** 지웁니다.
    await setup_gacha(items(("가", 50.0, -1, 0, 0), ("나", 50.0, -1, 0, 0)))
    ids = await asyncio.gather(*[db.next_gacha_item_id(GUILD.id) for _ in range(20)])
    note(len(set(ids)) == 20, "동시 20회 발급이 전부 다른 값", str(sorted(ids)[:6]))
    note(min(ids) > 2, "기존 항목 id(1,2) 와도 겹치지 않음", str(min(ids)))

    print("")
    print("  폴백 경로에서도 겹치지 않아야 합니다")
    real = db.gacha_col
    db.gacha_col = None
    try:
        ids2 = await asyncio.gather(*[db.next_gacha_item_id(GUILD.id)
                                      for _ in range(10)])
        note(len(set(ids2)) == 10, "JSON 폴백도 전부 다른 값", str(sorted(ids2)))
    finally:
        db.gacha_col = real

    print("")
    print("=== 13-5. 못 채운 회차 분기 (부분 / 전무) ===")
    # 실제로 밟히는 분기인데 검증이 비어 있었습니다.
    s = await setup_gacha(items(("한정", 100.0, 3, 0, 0)), price=100)
    await set_points(5000)
    out = await G.perform_rolls(GUILD, member, s, 10, 1000)
    note(out["filled"] == 3, "재고만큼만 채움", str(out["filled"]))
    note(out["refund"] == 700, "못 채운 7회분 환불", str(out["refund"]))
    note(out["spent"] == 300, "실제 소모는 3회분", str(out["spent"]))

    print("")
    s = await setup_gacha(items(("품절", 100.0, 0, 0, 0)), price=100)
    before = await set_points(5000)
    out = await G.perform_rolls(GUILD, member, s, 10, 1000)
    note(out["filled"] == 0, "한 번도 못 뽑음", str(out["filled"]))
    note(out["refund"] == 1000, "전액 환불", str(out["refund"]))
    note(out["balance"] == before, "잔액이 그대로", f"{before} → {out['balance']}")

    print("")
    print("=== 14. 버튼 콜백이 실제로 동작해야 ===")
    # cog 로드만으로는 NameError 를 잡지 못합니다.
    s = await setup_gacha(items(("보통", 100.0, -1, 0, 0)), price=100)
    view = G.GachaPlayView(settings=s)
    note("10회" in view.roll_gacha_10.label, "10회 버튼 문구에 횟수가 보임",
         view.roll_gacha_10.label)
    note("1,000" in view.roll_gacha_10.label, "가격도 보임", view.roll_gacha_10.label)

    s = await setup_gacha(items(("보통", 100.0, -1, 0, 0)), price=100, price_10=900)
    view = G.GachaPlayView(settings=s)
    note("할인" in view.roll_gacha_10.label, "할인폭이 버튼에 보임",
         view.roll_gacha_10.label)

    for label, cb in (("1회", "roll_gacha"), ("10회", "roll_gacha_10")):
        it = Inter(uid=member.id, admin=False)
        await db.add_user_points(GUILD.id, it.user.id, 100000)
        v = G.GachaPlayView(settings=s)
        try:
            await getattr(v, cb).callback(it)
            await drain()
            got = bool(it.response.sent or it.response.done or it.followup.sent)
            note(got, f"{label} 버튼이 응답함", F.outcome(it))
        except Exception as e:
            import traceback
            note(False, f"{label} 버튼에서 예외", f"{type(e).__name__}: {e}")
            traceback.print_exc(limit=3)

    print("")
    print("  품절이면 두 버튼이 모두 잠겨야 합니다")
    s = await setup_gacha(items(("품절", 100.0, 0, 0, 0)), price=100)
    v = G.GachaPlayView(is_out_of_stock=True, settings=s)
    note(v.roll_gacha.disabled and v.roll_gacha_10.disabled,
         "두 버튼 모두 비활성화")

    print("")
    print("=== 15. 10회 응답은 ephemeral 이어야 ===")
    s = await setup_gacha(items(("보통", 100.0, -1, 0, 0)), price=100)
    it = Inter(uid=member.id, admin=False)
    await db.add_user_points(GUILD.id, it.user.id, 100000)
    v = G.GachaPlayView(settings=s)
    await v.roll_gacha_10.callback(it)
    await drain()
    leaked = [x for x in (it.response.sent + it.followup.sent)
              if not x.get("ephemeral")]
    note(not leaked, "공개로 나가는 응답이 없음", str(leaked)[:80])

    await bot.close()
    print(f"\n  실패 {len(F.FAILS)}건 / 성공 {len(F.OKS)}건")
    for x in F.FAILS:
        print("   -", x)


asyncio.run(main())
