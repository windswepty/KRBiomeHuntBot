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

    print("\n=== 3. 기간 안에서는 버는 만큼 같이 쌓임 ('둘 다' 방식) ===")
    await set_period(-3600, 3600)
    note(ev.season_state(await ev.get_season(GID)) == "active", "진행 중")
    note((await ev.get_season(GID, fresh=True)).get("season_only") is True,
         "**기본값은 '시즌 포인트로만'** (2026-10-05: 시즌 중엔 모든 획득 포인트를 시즌으로)")
    await ev.save_season_only(GID, False)          # 이 절은 '둘 다' 방식을 봅니다
    ev._cache.clear()
    uid = 21
    out = {}
    await db.claim_quest(GID, uid, "q:1", 100, out=out)
    note(out == {"paid": 100, "mult": 1.0, "season": 100, "paused": False,
                 "mult_season_only": False},
         "**퀘스트 100 → 기본 100 + 시즌 100**", str(out))
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
    note(out.get("paid") == 100 and out.get("season") == 200 and out.get("mult_season_only"),
         "**호박 2배는 시즌 포인트에만** (기본 100 · 시즌 200, 2026-10-05 요청)", str(out))
    r_c = await db.checkin_user(GID, 22)
    note(r_c["season"] == 2 * r_c["gained"] and r_c["mult_season_only"],
         "출석도 시즌만 2배", f"{r_c['gained']} / {r_c['season']}")
    before = await sp(uid)
    p_before = await db.get_user_points(GID, uid)
    for _ in range(10):
        await db.process_chat_message(GID, uid, 0)
    note(await sp(uid) - before == 20 and await db.get_user_points(GID, uid) - p_before == 10,
         "채팅도 기본 10 · 시즌 20")
    await ev.save_season_only(GID, True)
    ev._cache.clear()
    out = {}
    await db.claim_quest(GID, uid, "q:2x-only", 100, out=out)
    note(out.get("paused") and out.get("season") == 200,
         "'시즌 포인트로만' + 호박 → 시즌 200 (기본 0)", str(out))
    s_now = await ev.get_season(GID, fresh=True)
    await set_period(-7200, -60)
    out = {}
    await db.claim_quest(GID, uid, "q:2x-off", 100, out=out)
    note(out.get("paid") == 200 and out.get("season") == 0,
         "시즌이 아니면 호박 2배는 기본 포인트에 (보상이 사라지지 않게)", str(out))
    await ev.save_season(GID, s_now["name"], s_now["start"], s_now["end"])
    await ev.save_season_only(GID, False)
    await ev.save_pumpkin(GID, {"boost_until": None})
    ev._cache.clear()

    print("\n=== 3-2. '시즌 포인트로만' — 시즌 중 기본 포인트는 안 들어옴 (2026-10-05 요청) ===")
    for label, users in (("JSON", None), ("Mongo", FM.FakeCollection("users"))):
        saved_users, saved_after = db.users_col, getattr(db, "_AFTER", None)
        if users is not None:
            db.users_col, db._AFTER = users, FM.AFTER
        try:
            await ev.save_season_only(GID, True)
            ev._cache.clear()
            note(await ev.base_points_paused(GID), f"[{label}] 켜고 시즌 중이면 멈춤")
            u = 141 if users is None else 142
            await db.add_user_points(GID, u, 500)          # 이미 가진 기본 포인트
            p0, s0 = await db.get_user_points(GID, u), await sp(u)
            out = {}
            await db.claim_quest(GID, u, "q:only", 100, out=out)
            note(out.get("paused") is True and out.get("season") == 100,
                 f"[{label}] 퀘스트 → 시즌 100", str(out))
            r = await db.checkin_user(GID, u)
            note(r["ok"] and r["paused"] and r["season"] == r["gained"] > 0,
                 f"[{label}] 출석 → 시즌으로만", str(r))
            r2 = await db.checkin_user(GID, u)
            note(not r2["ok"], f"[{label}] **출석 연타 방지는 그대로** (기본 0 이어도 오늘 출석으로 기록)")
            for _ in range(10):
                await db.process_chat_message(GID, u, 0)
            note(await db.get_user_points(GID, u) == p0,
                 f"[{label}] **기본 포인트는 하나도 안 늘어남** (퀘스트 · 출석 · 채팅)",
                 f"{p0} → {await db.get_user_points(GID, u)}")
            note(await sp(u) - s0 == 100 + r["gained"] + 10, f"[{label}] 전부 시즌으로",
                 str(await sp(u) - s0))
            note(await db.get_user_points(GID, u) == 500, f"[{label}] 이미 가진 500 은 그대로")

            # 시즌이 끝나면 저절로 기본 포인트로 돌아갑니다
            s_now = await ev.get_season(GID, fresh=True)
            await set_period(-7200, -60)
            note(not await ev.base_points_paused(GID), f"[{label}] 시즌이 끝나면 멈춤 풀림")
            out = {}
            await db.claim_quest(GID, u, "q:after-only", 100, out=out)
            note(out.get("paused") is False and await db.get_user_points(GID, u) == 600,
                 f"[{label}] 끝난 뒤 퀘스트 → 기본 100 다시 들어옴", str(out))
            await ev.save_season(GID, s_now["name"], s_now["start"], s_now["end"])
            await ev.save_season_only(GID, False)
            ev._cache.clear()
            note(not await ev.base_points_paused(GID), f"[{label}] 끄면 둘 다 쌓이는 방식으로")
        finally:
            db.users_col = saved_users
            if users is not None:
                db._AFTER = saved_after
    note(ev.season_state(await ev.get_season(GID, fresh=True)) == "active"
         and not (await ev.get_season(GID)).get("season_only"), "기간 · 설정 원상복구")

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
    # '둘 다' 방식 · 시즌 중: 경품 7 은 기본 7 + 시즌 7 (2026-10-08: 시즌 중 뽑기 포인트도 시즌으로)
    note(res["error"] is None and await sp(41) == 207 and res["season_balance"] == 207,
         "**시즌 포인트 100 으로 1회** (경품 7 은 시즌으로도 들어옴)", str(await sp(41)))
    note(await db.get_user_points(GID, 41) == base_before + 7 and res["prize_to_season"],
         "'둘 다' 방식이면 경품은 기본 + 시즌 둘 다")
    ms = [Member(42, "동시", False, guild=GUILD)] * 10
    await ev.add_season_points(GID, 42, 300)
    rs = await asyncio.gather(*[G.perform_rolls(GUILD, mm, gs, 1, 100, G.SEASON) for mm in ms])
    note(sum(1 for r in rs if r["error"] is None) == 3 and await sp(42) == 21,
         "**시즌 300 으로 10번 동시에 → 정확히 3번**", str(sum(1 for r in rs if r["error"] is None)))
    res = await G.perform_rolls(GUILD, m, gs, 10, 1000, G.SEASON)
    note(res["error"] == "no_points" and await sp(41) == 207, "모자라면 안 뽑히고 그대로")

    await db.save_gacha_settings(GID, {"price": 100})
    await db.set_gacha_item_stock(GID, 1, 2)            # 재고 2 에 10연차 → 8회분 환불
    gs = await db.get_gacha_settings(GID)
    await ev.add_season_points(GID, 43, 1000)
    res = await G.perform_rolls(GUILD, Member(43, "환불", False, guild=GUILD), gs, 10, 1000, G.SEASON)
    note(res["filled"] == 2 and res["refund"] == 800 and await sp(43) == 800 + 14,
         "**품절 환불은 시즌 포인트로 돌려받음**", f"{res['filled']} · {await sp(43)}")
    await db.set_gacha_item_stock(GID, 1, -1)
    gs = await db.get_gacha_settings(GID)

    # '시즌 포인트로만' 이면 경품 포인트도 시즌으로 (모든 획득 포인트)
    await ev.save_season_only(GID, True)
    ev._cache.clear()
    await db.add_user_points(GID, 44, 300)
    s44 = await sp(44)
    res = await G.perform_rolls(GUILD, Member(44, "경품", False, guild=GUILD), gs, 1, 100)
    note(res["prize_to_season"] and await sp(44) - s44 == 7 and await db.get_user_points(GID, 44) == 200
         and await G.prize_unit(GID) == "🏆",
         "**시즌 중 경품 포인트 7 → 시즌 포인트로** (기본은 100 만 빠짐)",
         f"{await db.get_user_points(GID, 44)} / {await sp(44)}")
    await ev.save_season_only(GID, False)
    ev._cache.clear()
    note(await G.prize_unit(GID) == "P + 🏆", "패널 글자: '둘 다' → P + 🏆")
    s_now = await ev.get_season(GID, fresh=True)
    await set_period(-7200, -60)
    await db.add_user_points(GID, 45, 300)
    res = await G.perform_rolls(GUILD, Member(45, "시즌끝", False, guild=GUILD), gs, 1, 100)
    note(not res["prize_to_season"] and await db.get_user_points(GID, 45) == 207,
         "시즌이 아니면 경품은 기본 포인트로만")
    note(await G.prize_unit(GID) == "P", "패널 글자: 시즌 아님 → P")
    await ev.save_season(GID, s_now["name"], s_now["start"], s_now["end"])
    ev._cache.clear()

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

    print("\n=== 6-2. /시즌상점 · /시즌뽑기 (시즌 포인트 전용 창) ===")
    await db.save_shop_settings(GID, {"items": [{"id": 1, "name": "한정템", "price": 100, "stock": 5,
                                                 "reward_role_id": 0, "description": ""}]})
    shop_cog, gacha_cog = SH.Shop(None), G.Gacha(None)
    it = Inter(uid=53, admin=False)
    await SH.Shop.season_shop.callback(shop_cog, it)
    sent = it.followup.sent[-1]
    note(sent.get("ephemeral") is True and isinstance(sent.get("view"), SH.ShopView)
         and "🏆" in sent["embed"].fields[0].value, "시즌 중 /시즌상점: 시즌 가격으로 보임")
    await ev.add_season_points(GID, 53, 150)
    p53 = await db.get_user_points(GID, 53)
    sel = sent["view"].children[0]
    sel._values = ["1"]
    it2 = Inter(uid=53, admin=False)
    await sel.callback(it2)
    note(await sp(53) == 50 and await db.get_user_points(GID, 53) == p53
         and "구매 완료" in (it2.response.sent[-1]["embed"].title or ""),
         "**고르면 바로 시즌 포인트로 삼** (고르는 창 없음 · 기본 포인트 그대로)", str(await sp(53)))

    it = Inter(uid=53, admin=False)
    await G.Gacha.season_roll.callback(gacha_cog, it)
    sent = it.followup.sent[-1]
    ids = {getattr(c, "custom_id", None) for c in sent["view"].children}
    note(ids == {"gacha_season_btn", "gacha_season10_btn"},
         "/시즌뽑기: 시즌 버튼만 (기본 포인트 버튼 없음)", str(ids))
    note(not any(c.disabled for c in sent["view"].children), "시즌 중이면 누를 수 있음")

    s_now = await ev.get_season(GID, fresh=True)
    await set_period(-7200, -60)
    for label, fn, cog_ in (("시즌상점", SH.Shop.season_shop, shop_cog),
                            ("시즌뽑기", G.Gacha.season_roll, gacha_cog)):
        it = Inter(uid=53, admin=False)
        await fn.callback(cog_, it)
        note("시즌 기간이 아닙니다" in (it.followup.sent[-1].get("content") or ""),
             f"시즌 밖 /{label}: 일반 유저는 막힘")
        it = Inter(uid=7, admin=True)
        await fn.callback(cog_, it)
        e = it.followup.sent[-1].get("embed")
        note(e is not None and "관리자 미리보기" in e.description, f"시즌 밖 /{label}: 관리자는 미리보기")
    v = it.followup.sent[-1]["view"]
    note(all(c.disabled for c in v.children), "미리보기 뽑기 버튼은 잠김")
    await ev.save_season(GID, s_now["name"], s_now["start"], s_now["end"])
    ev._cache.clear()

    print("\n=== 6-3. 시즌 명령어 표시 대상 (기본 권한) 전환 ===")
    note(S.desired_season_perms(True) is None and S.desired_season_perms(False) == "8",
         "시즌 중 → 모두 · 아니면 → 관리자만(8)")
    for name in ("season_shop", "season_roll"):
        cmd = getattr(SH.Shop if name == "season_shop" else G.Gacha, name)
        note(cmd.default_permissions is not None and cmd.default_permissions.administrator,
             f"/{cmd.name} 등록 기본값은 관리자만 (재시작 직후에도 새지 않게)")

    class FakeCmd:
        def __init__(self, name, cid, perms):
            self.name, self.id, self.default_member_permissions = name, cid, perms

    edits = []
    remote = {1: "8", 2: "8", 3: None}       # 디스코드에 저장된 값 (재시작 직후 = 관리자만)

    def as_perms(v):
        return None if v is None else discord.Permissions(int(v))

    class FakeTree:
        async def fetch_commands(self):
            return [FakeCmd("시즌뽑기", 1, as_perms(remote[1])),
                    FakeCmd("시즌상점", 2, as_perms(remote[2])),
                    FakeCmd("뽑기", 3, as_perms(remote[3]))]

    class FakeHttp:
        async def edit_global_command(self, app, cid, payload):
            edits.append((cid, payload))
            remote[cid] = payload["default_member_permissions"]

    class FakeBot:
        application_id = 99
        guilds = [GUILD]
        tree, http = FakeTree(), FakeHttp()

        async def wait_until_ready(self):
            return None

    sc = S.Season(FakeBot())
    await sc.sync_season_commands.coro(sc)
    note(sorted(edits) == [(1, {"default_member_permissions": None}),
                           (2, {"default_member_permissions": None})],
         "**시즌 중이면 두 명령어만 '모두' 로** (/뽑기 는 건드리지 않음)", str(edits))
    edits.clear()
    await sc.sync_season_commands.coro(sc)
    note(edits == [], "같은 상태면 다시 묻지 않음")
    await set_period(-7200, -60)
    await sc.sync_season_commands.coro(sc)
    note(sorted(edits) == [(1, {"default_member_permissions": "8"}),
                           (2, {"default_member_permissions": "8"})],
         "시즌이 끝나면 다시 관리자만", str(edits))
    await ev.save_season(GID, s_now["name"], s_now["start"], s_now["end"])
    ev._cache.clear()

    print("\n=== 6-4. /시즌부스트 — 시즌 포인트에만 곱하는 채널 배율 ===")
    import cogs.chat_reward as CR
    await ev.save_season_only(GID, False)                  # 기본 · 시즌이 따로 보이게
    ev._cache.clear()
    await db.set_channel_boost(GID, 888, 3.0, 3600, kind=db.BOOST_SEASON)
    note(await db.get_channel_boost(GID, 888, kind=db.BOOST_SEASON) == 3.0
         and await db.get_channel_boost(GID, 888) == 1.0,
         "시즌 부스트는 채널 부스트와 따로 저장")
    u = 61
    p0, s0 = await db.get_user_points(GID, u), await sp(u)
    for _ in range(10):
        await db.process_chat_message(GID, u, 888)
    note(await db.get_user_points(GID, u) - p0 == 10 and await sp(u) - s0 == 30,
         "**시즌 부스트 3배 채널: 기본 10 · 시즌 30**",
         f"{await db.get_user_points(GID, u) - p0} / {await sp(u) - s0}")
    await db.set_channel_boost(GID, 888, 2.0, 3600)            # 채널 부스트도 같이
    p0, s0 = await db.get_user_points(GID, u), await sp(u)
    for _ in range(10):
        await db.process_chat_message(GID, u, 888)
    note(await db.get_user_points(GID, u) - p0 == 20 and await sp(u) - s0 == 60,
         "채널 부스트 2배와 겹치면 기본 20 · 시즌 60 (곱해짐)",
         f"{await db.get_user_points(GID, u) - p0} / {await sp(u) - s0}")
    chat_list = await db.get_all_active_boosts(GID)
    season_list = await db.get_all_active_boosts(GID, kind=db.BOOST_SEASON)
    note({b["channel_id"] for b in season_list} == {"888"}
         and all(db.boost_kind(b) == "chat" for b in chat_list),
         "/채널부스트 목록에 시즌 부스트가 섞이지 않음")
    await db.set_channel_boost(GID, 888, 1.0, 0, kind=db.BOOST_SEASON)
    note(await db.get_channel_boost(GID, 888, kind=db.BOOST_SEASON) == 1.0
         and await db.get_channel_boost(GID, 888) == 2.0, "시즌 부스트만 해제 (채널 부스트는 그대로)")
    await db.set_channel_boost(GID, 888, 1.0, 0)
    await db.set_channel_boost(GID, 889, 2.0, 10, kind=db.BOOST_SEASON)
    # 만료 정리는 종류를 남겨서 돌려줍니다 (종료 안내 문구가 달라야 합니다)
    import datetime as _dt
    real_now = db.datetime

    class Later(_dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return real_now.now(tz) + _dt.timedelta(seconds=60)
    db.datetime = Later
    try:
        gone = await db.check_and_clear_expired_boosts()
    finally:
        db.datetime = real_now
    note(any(db.boost_kind(b) == "season" and b["channel_id"] == "889" for b in gone),
         "만료된 시즌 부스트는 kind=season 으로 돌려줌", str(gone))

    rc = CR.ChatReward.__new__(CR.ChatReward)
    it = Inter()
    await CR.ChatReward.season_boost_settings.callback(rc, it)
    sent = it.followup.sent[-1]
    note(sent.get("ephemeral") and isinstance(sent.get("view"), CR.ChannelBoostSettingsView)
         and sent["view"].children[0].kind == "season", "/시즌부스트: 관리 메뉴가 시즌 종류로 열림")
    sel = sent["view"].children[0]
    sel._values = ["set"]
    it = Inter()
    await sel.callback(it)
    picker = it.response.sent[-1]["view"].children[0]
    note(picker.kind == "season", "채널 고르기도 시즌 종류")
    modal = CR.SetChannelBoostModal(890, "이벤트", kind=db.BOOST_SEASON)
    modal.multiplier_input._value, modal.duration_input._value = "2", "30m"
    it = Inter()
    await modal.on_submit(it)
    note(await db.get_channel_boost(GID, 890, kind=db.BOOST_SEASON) == 2.0
         and await db.get_channel_boost(GID, 890) == 1.0
         and "시즌 부스트" in it.response.sent[-1]["embed"].title,
         "모달로 시즌 부스트 설정 → 시즌 쪽에만 저장")
    sel._values = ["list"]
    it = Inter()
    await sel.callback(it)
    note("시즌 부스트" in it.response.sent[-1]["embed"].title
         and "890" in it.response.sent[-1]["embed"].description, "목록도 시즌 부스트만")
    rm = CR.RemoveChannelBoostSelect(await db.get_all_active_boosts(GID, kind=db.BOOST_SEASON),
                                     GUILD, db.BOOST_SEASON)
    rm._values = ["890"]
    await rm.callback(Inter())
    note(await db.get_channel_boost(GID, 890, kind=db.BOOST_SEASON) == 1.0, "해제 메뉴로 시즌 부스트 해제")

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

    print("\n=== 9. 적립 방식 버튼 ===")
    await ev.save_season_only(GID, False)
    s = await ev.get_season(GID, fresh=True)
    v = S.AdminView(cog, s)
    note("둘 다" in v.only.label, "꺼져 있으면 '둘 다'", v.only.label)
    it = Inter(admin=False)
    await v.only.callback(it)
    note(not (await ev.get_season(GID, fresh=True)).get("season_only"), "관리자가 아니면 못 바꿈")
    it = Inter()
    await v.only.callback(it)
    s = await ev.get_season(GID, fresh=True)
    note(s.get("season_only") is True, "누르면 '시즌 포인트로만' 으로")
    emb = S.build_admin_embed(s, await ev.season_stats(GID))
    note(any("시즌 포인트로만" in f.value for f in emb.fields if f.name == "적립 방식"),
         "설정 화면에 적립 방식 표시")
    note("시즌 포인트로만" in S.AdminView(cog, s).only.label, "버튼 글자도 바뀜")


asyncio.run(main())
print(f"\n  실패 {len(F.FAILS)}건 / 성공 {len(F.OKS)}건")
for x in F.FAILS:
    print("   -", x)
sys.exit(1 if F.FAILS else 0)
