"""🏆 시즌 포인트 — 기간 · 적립 · 소멸 · 초기화 · 2:1 교환 · 뽑기/상점에서 쓰기.

요청(2026-10-03):
  · 기간 동안 **기본 포인트와 같은 방식으로** 시즌 포인트도 받음 (퀘스트 100 → 기본 100 + 시즌 100)
  · 기간이 끝나면 그대로 소멸 · 초기화 명령어
  · (가능하면) 시즌 포인트로 뽑기 · 상점 · 기본 2 → 시즌 1 교환 · 채널 부스트도 적용

돈이 오가는 기능이라 **동시에 눌렀을 때 더 나가지 않는가** 를 함께 봅니다.
"""
import sys, os, tempfile, asyncio, time

PROJ = sys.argv[1]
sys.path.insert(0, PROJ)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(tempfile.mkdtemp())
os.environ["MONGO_URI"] = ""

import discord
import fakemongo as FM
import fakes as F
from fakes import note, drain, GUILD, Inter, Member
import utils.event_db as ev
import utils.database as db
import cogs.season as S
import cogs.gacha as G
import cogs.shop as SH

GID = GUILD.id


def use_mongo():
    ev.season_col = FM.FakeCollection("season_settings")
    ev.season_pts_col = FM.FakeCollection("season_points")
    ev.pumpkin_col = FM.FakeCollection("pumpkin_event")
    ev.pumpkin_hits_col = FM.FakeCollection("pumpkin_hits")
    ev._AFTER = FM.AFTER
    ev._cache.clear()


async def set_period(start_off, end_off, name="1시즌"):
    now = time.time()
    await ev.save_season(GID, name, now + start_off, now + end_off)
    ev._cache.clear()


async def sp(uid):
    return await ev.get_season_points(GID, uid)


async def main():
    use_mongo()

    print("=== 1. 시각 입력 (날짜를 글자로 비교하다 틀린 적이 있습니다) ===")
    for raw, want in (("2026-10-10 17:00", "2026-10-10 17:00"), ("2026-10-10 오후 5시", "2026-10-10 17:00"),
                      ("2026-10-1 9:05", "2026-10-01 09:05"), ("2026-11-14", "2026-11-14 00:00"),
                      ("2026-10-10 오전 12시", "2026-10-10 00:00"), ("2026-13-01 10:00", None),
                      ("내일", None)):
        d = S.parse_kst(raw)
        got = d.strftime("%Y-%m-%d %H:%M") if d else None
        note(got == want, f"'{raw}' → {want}", str(got))

    print("\n=== 2. 기간 밖에서는 안 쌓임 ===")
    note(ev.season_state(await ev.get_season(GID, fresh=True)) == "none", "설정 전에는 시즌 없음")
    await db.claim_quest(GID, 11, "q:before0", 100)
    note(await sp(11) == 0, "시즌이 없으면 시즌 포인트 0")
    await set_period(3600, 7200)
    note(ev.season_state(await ev.get_season(GID)) == "before", "시작 전")
    out = {}
    await db.claim_quest(GID, 11, "q:before", 100, out=out)
    note(out.get("season") == 0 and out.get("paid") == 100, "시작 전 퀘스트: 기본 100 · 시즌 0")

    print("\n=== 3. 기간 안에서는 버는 만큼 같이 쌓임 ===")
    await set_period(-3600, 3600)
    note(ev.season_state(await ev.get_season(GID)) == "active", "진행 중")
    uid = 21
    out = {}
    await db.claim_quest(GID, uid, "q:1", 100, out=out)
    note(out == {"paid": 100, "mult": 1.0, "season": 100}, "**퀘스트 100 → 기본 100 + 시즌 100**", str(out))
    r = await db.checkin_user(GID, uid)
    note(r["season"] == r["gained"] and r["gained"] > 0, "출석도 받은 만큼 시즌으로", str(r["gained"]))
    for _ in range(10):
        await db.process_chat_message(GID, uid, 0)
    note(await sp(uid) == 100 + r["gained"] + 10, "채팅 10P 도 시즌으로", str(await sp(uid)))
    await db.set_channel_boost(GID, 777, 3.0, 3600)
    before = await sp(uid)
    for _ in range(10):
        await db.process_chat_message(GID, uid, 777)
    note(await sp(uid) - before == 30, "**채널 부스트 3배 채널 → 시즌 포인트도 30**",
         str(await sp(uid) - before))
    await db.add_user_points(GID, uid, 5000)
    note(await sp(uid) - before == 30, "관리자 지급 · 환불은 시즌 포인트로 안 쌓임")

    await ev.save_pumpkin(GID, {"boost_until": time.time() + 3600, "enabled": True})
    ev._cache.clear()
    out = {}
    await db.claim_quest(GID, uid, "q:2x", 100, out=out)
    note(out.get("paid") == 200 and out.get("season") == 200, "호박 2배 중이면 시즌 포인트도 2배",
         str(out))
    await ev.save_pumpkin(GID, {"boost_until": None})
    ev._cache.clear()

    print("\n=== 4. 2:1 교환 ===")
    cog = S.Season(None)
    u2 = 31
    await db.add_user_points(GID, u2, 1000)
    it = Inter(uid=u2, admin=False)
    await S.Season.season_exchange.callback(cog, it, 300)
    note(await db.get_user_points(GID, u2) == 400 and await sp(u2) == 300,
         "**시즌 300 받으면 기본 600 빠짐**", f"{await db.get_user_points(GID, u2)} / {await sp(u2)}")
    it = Inter(uid=u2, admin=False)
    await S.Season.season_exchange.callback(cog, it, 300)
    note(await db.get_user_points(GID, u2) == 400 and await sp(u2) == 300
         and "부족" in (it.followup.sent[-1].get("content") or ""),
         "기본 포인트가 모자라면 아무것도 안 바뀜")

    u3 = 32
    await db.add_user_points(GID, u3, 1000)
    its = [Inter(uid=u3, admin=False) for _ in range(10)]
    await asyncio.gather(*[S.Season.season_exchange.callback(cog, i, 100) for i in its])
    note(await db.get_user_points(GID, u3) == 0 and await sp(u3) == 500,
         "**10번 동시에 교환해도 가진 만큼만** (기본 1000 → 시즌 500)",
         f"{await db.get_user_points(GID, u3)} / {await sp(u3)}")

    print("\n=== 5. 시즌 포인트로 뽑기 ===")
    await db.save_gacha_settings(GID, {"enabled": True, "price": 100, "price_10": 0, "title": "t"})
    await db.add_gacha_item(GID, {"id": 1, "name": "꽝", "percent": 100, "stock": -1,
                                  "reward_points": 7, "reward_role_id": 0, "description": ""})
    gs = await db.get_gacha_settings(GID)
    m = Member(41, "뽑는사람", False, guild=GUILD)
    await ev.add_season_points(GID, 41, 300)
    base_before = await db.get_user_points(GID, 41)
    res = await G.perform_rolls(GUILD, m, gs, 1, 100, G.SEASON)
    note(res["error"] is None and await sp(41) == 200 and res["season_balance"] == 200,
         "**시즌 포인트 100 으로 1회**", str(await sp(41)))
    note(await db.get_user_points(GID, 41) == base_before + 7, "당첨 포인트 보상은 **기본 포인트**로")
    ms = [Member(42, "동시", False, guild=GUILD)] * 10
    await ev.add_season_points(GID, 42, 300)
    rs = await asyncio.gather(*[G.perform_rolls(GUILD, mm, gs, 1, 100, G.SEASON) for mm in ms])
    note(sum(1 for r in rs if r["error"] is None) == 3 and await sp(42) == 0,
         "**시즌 300 으로 10번 동시에 → 정확히 3번**", str(sum(1 for r in rs if r["error"] is None)))
    res = await G.perform_rolls(GUILD, m, gs, 10, 1000, G.SEASON)
    note(res["error"] == "no_points" and await sp(41) == 200, "모자라면 안 뽑히고 그대로")

    await db.save_gacha_settings(GID, {"price": 100})
    await db.set_gacha_item_stock(GID, 1, 2)            # 재고 2 에 10연차 → 8회분 환불
    gs = await db.get_gacha_settings(GID)
    await ev.add_season_points(GID, 43, 1000)
    res = await G.perform_rolls(GUILD, Member(43, "환불", False, guild=GUILD), gs, 10, 1000, G.SEASON)
    note(res["filled"] == 2 and res["refund"] == 800 and await sp(43) == 800,
         "**품절 환불은 시즌 포인트로 돌려받음**", f"{res['filled']} · {await sp(43)}")
    await db.set_gacha_item_stock(GID, 1, -1)

    v = G.GachaPlayView(settings=gs, season_active=False)
    ids = {c.custom_id for c in v.children}
    note("gacha_season_btn" not in ids and "gacha_roll_btn" in ids, "시즌이 아니면 시즌 버튼을 숨김")
    v2 = G.GachaPlayView()
    note({"gacha_season_btn", "gacha_season10_btn"} <= {c.custom_id for c in v2.children},
         "등록용 영구 View 는 시즌 버튼까지 다 가짐 (재시작 뒤에도 동작)")

    print("\n=== 6. 시즌 포인트로 상점 ===")
    await db.save_shop_settings(GID, {"enabled": True, "title": "상점",
                                      "items": [{"id": 1, "name": "한정템", "price": 100, "stock": 3,
                                                 "reward_role_id": 0, "description": ""}]})
    await ev.add_season_points(GID, 51, 250)
    sel = SH.ShopSelect((await db.get_shop_settings(GID))["items"])
    sel._values = ["1"]
    it = Inter(uid=51, admin=False)
    await sel.callback(it)
    view = it.response.sent[-1].get("view")
    note(isinstance(view, SH.CurrencyChoiceView), "시즌 중에는 '어떤 포인트로' 를 고르게 함")
    its = [Inter(uid=51, admin=False) for _ in range(3)]
    await asyncio.gather(*[view.with_season.callback(i) for i in its])
    note(await sp(51) == 150, "**같은 창을 3번 동시에 눌러도 한 번만 삼**", str(await sp(51)))
    s = await db.get_shop_settings(GID)
    note(s["items"][0]["stock"] == 2, "재고도 한 번만 줄어듦")

    await db.save_shop_settings(GID, {"items": [{"id": 1, "name": "한정템", "price": 100, "stock": 1,
                                                 "reward_role_id": 0, "description": ""}]})
    real = SH.consume_shop_stock

    async def gone(*a, **k):
        return False
    SH.consume_shop_stock = gone
    try:
        await ev.add_season_points(GID, 52, 100)
        item = (await db.get_shop_settings(GID))["items"][0]
        got = []

        async def respond(e):
            got.append(e)
        await SH.buy_item(Inter(uid=52, admin=False), await db.get_shop_settings(GID), item, SH.SEASON, respond)
        note(await sp(52) == 100 and "환불" in got[-1].description,
             "방금 품절이면 **시즌 포인트로 환불**")
    finally:
        SH.consume_shop_stock = real

    print("\n=== 7. 기간이 끝나면 소멸 · 기간을 고쳐도 유지 · 초기화 ===")
    keep = await sp(21)
    await ev.save_season(GID, "1시즌 (연장)", time.time() - 3600, time.time() + 7200)
    ev._cache.clear()
    note(await sp(21) == keep, "**기간을 고쳐도 모은 포인트는 그대로**", str(keep))
    await set_period(-7200, -1)
    note(ev.season_state(await ev.get_season(GID)) == "ended" and await sp(21) == 0,
         "**끝나면 0 (소멸)**")
    ok, _ = await ev.spend_season_points(GID, 21, 1)
    note(not ok, "끝난 뒤엔 쓸 수 없음")
    res = await G.perform_rolls(GUILD, Member(21, "x", False, guild=GUILD), gs, 1, 100, G.SEASON)
    note(res["error"] == "no_season", "끝난 뒤 시즌 뽑기 → '시즌 기간이 아닙니다'")
    out = {}
    await db.claim_quest(GID, 21, "q:after", 100, out=out)
    note(out.get("season") == 0, "끝난 뒤 퀘스트는 시즌 포인트 없음")
    it = Inter(uid=21, admin=False)
    await S.Season.season_exchange.callback(cog, it, 1)
    note("시즌 기간이 아니라" in (it.followup.sent[-1].get("content") or ""), "끝난 뒤 교환 거절")

    await set_period(-3600, 3600)
    old_sid = (await ev.get_season(GID, fresh=True))["season_id"]
    before = await ev.season_stats(GID)
    await ev.reset_season(GID)
    ev._cache.clear()
    s = await ev.get_season(GID, fresh=True)
    note(s["season_id"] != old_sid and await sp(21) == 0 and await sp(51) == 0,
         "**초기화 → 전원 0** (새 시즌 ID)", f"{before}")
    note(not [d for d in ev.season_pts_col.docs.values() if d.get("season_id") == old_sid],
         "옛 시즌 기록은 DB 에서 지움")
    note(s["start"] and s["end"], "초기화해도 기간 설정은 그대로")

    print("\n=== 8. 화면 ===")
    await ev.add_season_points(GID, 61, 900)
    await ev.add_season_points(GID, 62, 500)
    it = Inter(uid=62, admin=False)
    await S.Season.season_points.callback(cog, it)
    e = it.followup.sent[-1]["embed"]
    top = next(f.value for f in e.fields if "TOP" in f.name)
    note("500" in e.description and top.index("61") < top.index("62"), "내 시즌 포인트 · 순위")
    note(it.followup.sent[-1].get("ephemeral") is True, "본인에게만")
    it = Inter()
    await S.Season.season_settings.callback(cog, it)
    note("진행 중" in " ".join(f.value for f in it.followup.sent[-1]["embed"].fields), "설정 화면 상태")
    m = S.PeriodModal(cog, s)
    m.name_in._value, m.start_in._value, m.end_in._value = "x", "2026-11-14 17:00", "2026-10-10 17:00"
    it = Inter()
    await m.on_submit(it)
    note(it.response.sent and "빠릅니다" in it.response.sent[0]["content"], "끝이 시작보다 빠르면 거절")
    m.start_in._value = "다음주"
    it = Inter()
    await m.on_submit(it)
    note(it.response.sent and "적어 주세요" in it.response.sent[0]["content"], "못 읽는 시각은 거절")


asyncio.run(main())
print(f"\n  실패 {len(F.FAILS)}건 / 성공 {len(F.OKS)}건")
for x in F.FAILS:
    print("   -", x)
sys.exit(1 if F.FAILS else 0)
