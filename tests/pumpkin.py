"""🎃 호박 깨기 — 하루 한 번 · 체력 · 깨는 한 명 · 깨지면 포인트 N배.

요청(2026-10-03): 하루 한 번 · 가입 24시간 지난 계정만 · 체력 1,500만 ·
기본 1,000 / 20% 확률 2,000 · 깨지면 1주일간 **받는 포인트 2배**(채팅 ·
출석 · 퀘스트 전부).

'정상 입력이 되는가' 보다 **몰렸을 때 틀리지 않는가** 를 봅니다 — 수백 명이
같은 버튼을 누르는 기능입니다.
"""
import sys, os, tempfile, asyncio, random, datetime, time

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
import cogs.pumpkin as P

KST = ev.KST
GID = GUILD.id


def use_mongo():
    ev.pumpkin_col = FM.FakeCollection("pumpkin_event")
    ev.pumpkin_hits_col = FM.FakeCollection("pumpkin_hits")
    ev.pumpkin_dmg_col = FM.FakeCollection("pumpkin_damage")
    ev._AFTER = FM.AFTER
    ev._cache.clear()


def set_hp(hp):
    """테스트용 — 체력을 직접 맞춥니다(설정 저장으로는 못 바꾸게 막아 둠)."""
    ev.pumpkin_col.docs[str(GID)]["hp"] = hp
    ev._cache.clear()


def member(uid, hours_ago=48, name=None):
    m = Member(uid, name or f"u{uid}", False, guild=GUILD)
    m.joined_at = discord.utils.utcnow() - datetime.timedelta(hours=hours_ago)
    return m


async def press(m, rng_crit=None):
    it = Inter(uid=m.id, admin=False)
    it.user = m
    if rng_crit is not None:
        real = P.roll_damage
        P.roll_damage = lambda p, rng=None: ((int(p["crit_damage"]), True) if rng_crit
                                             else (int(p["damage"]), False))
    try:
        await P.handle_hit(it, None)
    finally:
        if rng_crit is not None:
            P.roll_damage = real
    await drain()
    sent = it.followup.sent[-1] if it.followup.sent else (it.response.sent[-1] if it.response.sent else {})
    e = sent.get("embed")
    return (e.title if e else "") + " " + (sent.get("content") or ""), it


async def main():
    use_mongo()
    print("=== 1. 기본값 (요청 그대로) ===")
    p = await ev.get_pumpkin(GID, fresh=True)
    note((p["hp_max"], p["damage"], p["crit_damage"], p["crit_percent"], p["min_member_hours"],
          p["boost_mult"], p["boost_days"]) == (15_000_000, 1000, 2000, 20, 24, 2.0, 7),
         "체력 1,500만 · 1,000 / 2,000 (20%) · 24시간 · 2배 7일")
    await ev.save_pumpkin(GID, {"enabled": True, "channel_id": 333})

    print("\n=== 2. 치명타 확률 ===")
    rng = random.Random(42)
    rolls = [P.roll_damage(p, rng) for _ in range(20000)]
    rate = sum(1 for _, c in rolls if c) / len(rolls)
    note(0.19 <= rate <= 0.21 and {d for d, _ in rolls} == {1000, 2000},
         f"2만 번 중 치명타 {rate * 100:.1f}% · 데미지는 1,000 / 2,000 뿐")

    print("\n=== 3. 하루 한 번 ===")
    a = member(1001)
    text, _ = await press(a, rng_crit=False)
    note("1,000" in text, "처음엔 1,000 데미지", text[:40])
    text, _ = await press(a)
    note("이미 쳤습니다" in text, "같은 날 두 번째는 거절", text[:40])
    p = await ev.get_pumpkin(GID, fresh=True)
    note(p["hp"] == 15_000_000 - 1000 and p["hits"] == 1, "체력은 한 번만 깎임")
    tomorrow = datetime.datetime.now(KST) + datetime.timedelta(days=1)
    res = await ev.hit_pumpkin(GID, a.id, 1000, when=tomorrow)
    note(res["status"] == "ok", "다음 날(KST)엔 다시 칠 수 있음")

    print("\n=== 4. 몰릴 때 ===")
    b = member(1002)
    rs = await asyncio.gather(*[ev.hit_pumpkin(GID, b.id, 1000) for _ in range(10)])
    note(sum(1 for r in rs if r["status"] == "ok") == 1,
         "**같은 사람이 10번 동시에 눌러도 한 번만**",
         str([r["status"] for r in rs].count("ok")))
    before = (await ev.get_pumpkin(GID, fresh=True))["hp"]
    rs = await asyncio.gather(*[ev.hit_pumpkin(GID, 2000 + i, 1000) for i in range(50)])
    after = (await ev.get_pumpkin(GID, fresh=True))["hp"]
    note(before - after == 50_000 and all(r["status"] == "ok" for r in rs),
         "**50명이 동시에 쳐도 체력은 정확히 50번분**", f"{before - after:,}")

    print("\n=== 5. 누가 칠 수 있나 ===")
    new = member(1003, hours_ago=1)
    text, it = await press(new)
    note("24시간" in text and not it.followup.sent, "**들어온 지 1시간 → 거절** (부계정 방지)",
         text[:50])
    old = member(1004, hours_ago=25)
    text, _ = await press(old)
    note("데미지" in text, "25시간 → 칠 수 있음")
    bl = member(1005)
    await db.add_blacklist_user(GID, bl.id, "테스트")
    db._blacklist_cache.clear() if hasattr(db, "_blacklist_cache") else None
    text, _ = await press(bl)
    note("제한" in text, "블랙리스트는 거절 (포인트를 받을 수 없는 계정)", text[:40])
    await ev.save_pumpkin(GID, {"enabled": False})
    text, _ = await press(member(1006))
    note("쉬고" in text, "꺼져 있으면 거절")
    await ev.save_pumpkin(GID, {"enabled": True})

    print("\n=== 6. 깨는 사람은 정확히 한 명 ===")
    set_hp(5000)                 # 2,000 짜리 다섯 방이면 깨짐
    users = [member(3000 + i) for i in range(20)]
    rs = await asyncio.gather(*[ev.hit_pumpkin(GID, u.id, 2000) for u in users])
    broke = [r for r in rs if r.get("broke")]
    dead = [r for r in rs if r["status"] == "dead"]
    ok = [r for r in rs if r["status"] == "ok"]
    note(len(broke) == 1, "**20명이 동시에 마지막 한 방을 쳐도 깬 사람은 1명**", str(len(broke)))
    note(len(ok) == 3 and len(dead) == 17, "체력이 있을 때 친 3명만 들어감 · 나머지는 '이미 깨짐'",
         f"ok {len(ok)} · dead {len(dead)}")
    p = await ev.get_pumpkin(GID, fresh=True)
    until = float(p["boost_until"])
    note(abs(until - (time.time() + 7 * 86400)) < 120, "보상 기한 = 지금부터 7일")
    loser = next(u for u, r in zip(users, rs) if r["status"] == "dead")
    hid = ev._hit_id(GID, p["round"], ev.kst_day(), loser.id)
    note(hid not in ev.pumpkin_hits_col.docs, "깨진 뒤 친 사람은 **오늘 기회를 돌려받음** (기록 삭제)")
    await ev.new_pumpkin_round(GID)
    res = await ev.hit_pumpkin(GID, loser.id, 1000)
    note(res["status"] == "ok", "새 호박이 오면 그 사람도 바로 칠 수 있음")
    note(float((await ev.get_pumpkin(GID, fresh=True))["boost_until"]) == until,
         "새 호박을 놓아도 **진행 중인 보상은 그대로**")

    print("\n=== 7. 깨지면 받는 포인트 2배 (채팅 · 출석 · 퀘스트) ===")
    ev._cache.clear()
    note(await ev.point_multiplier(GID) == 2.0, "배율 2.0")
    uid = 5001
    r = await db.checkin_user(GID, uid)
    s = await db.get_checkin_settings(GID)
    base = int(s.get("base", 100))
    note(r["ok"] and r["base"] == base * 2 and r["mult"] == 2.0
         and r["points"] == r["gained"], f"**출석 기본 {base} → {base * 2}**", str(r["gained"]))
    for _ in range(10):
        pts, awarded, earned, _m = await db.process_chat_message(GID, uid, 0)
    note(awarded and earned == 20, "**채팅 10P → 20P**", str(earned))
    out = {}
    ok_ = await db.claim_quest(GID, uid, "d:test", 100, out=out)
    note(ok_ and out.get("paid") == 200, "**퀘스트 100P → 200P** (실제로 준 양을 돌려줌)", str(out))
    total = await db.get_user_points(GID, uid)
    note(total == r["gained"] + 20 + 200, "실제 잔액에 모두 2배로 들어감", f"{total:,}")
    gacha_like = await db.add_user_points(GID, uid, 50)
    note(gacha_like == total + 50, "관리자 지급 · 환불(add_user_points 직접)은 **2배가 아님**")

    ev.pumpkin_col.docs[str(GID)]["boost_until"] = time.time() - 1
    ev._cache.clear()
    note(await ev.point_multiplier(GID) == 1.0, "기한이 지나면 1배로 돌아옴")
    out = {}
    await db.claim_quest(GID, uid, "d:test2", 100, out=out)
    note(out.get("paid") == 100, "기한 뒤 퀘스트는 100P 그대로")

    print("\n=== 8. 패널 ===")
    p = await ev.get_pumpkin(GID, fresh=True)
    e = P.build_pumpkin_embed(p, 12, with_image=True)
    note("❤️" in e.description and "%" in e.description and e.image.url.startswith("attachment://"),
         "체력 · 막대 · 그림(첨부)")
    note(all(len(f.value) <= 1024 for f in e.fields) and len(e) <= 6000, "임베드 규격 안")
    p2 = dict(p, hp=0, broken_by="77", boost_until=time.time() + 3600)
    e2 = P.build_pumpkin_embed(p2, 0)
    note("깨졌습니다" in e2.description and "2배" in e2.description, "깨지면 '2배 진행 중' 표시")
    v = P.PumpkinView()
    note(v.hit.custom_id == "pumpkin:hit" and v.timeout is None, "영구 버튼 (재시작 뒤에도 동작)")
    note(P.PumpkinView(disabled=True).hit.disabled, "깨진 뒤에는 버튼이 꺼짐")
    note(P.image_name({"image_name": "a.GIF"}) == "pumpkin.gif"
         and P.image_name({"image_name": ""}) == "pumpkin.png", "그림 확장자를 따름 (gif 가 움직이게)")

    print("\n=== 9. 깨지는 순간 공개 알림 ===")
    LOG = GUILD.get_channel(333)
    LOG.sent.clear()
    await ev.new_pumpkin_round(GID)
    set_hp(1000)
    breaker = member(7001)
    text, _ = await press(breaker, rng_crit=False)
    note("깼습니다" in text, "깬 사람에게 '호박을 깼습니다'", text[:30])
    pub = [s for s in LOG.sent if "호박을 깼습니다" in (s.get("content") or "")]
    am = pub[-1].get("allowed_mentions") if pub else None
    note(pub and am is not None and not am.everyone and not am.roles,
         "채널에 공개 알림 · @everyone/역할은 울리지 않음")

    print("\n=== 10. 관리자 설정 ===")
    cog = P.Pumpkin.__new__(P.Pumpkin)
    cog._pending, cog._locks = {}, {}

    async def no_refresh(it, note=""):
        it.response.edited = {"note": note}
    cog.refresh_admin = no_refresh
    m = P.NumbersModal(cog, p)
    m.hp._value, m.dmg._value, m.crit._value, m.rate._value, m.hours._value = "abc", "1", "2", "3", "4"
    it = Inter()
    await m.on_submit(it)
    note(it.response.sent and "숫자" in it.response.sent[0]["content"], "숫자가 아니면 거절")
    m.hp._value, m.rate._value = "20,000,000", "150"
    it = Inter()
    await m.on_submit(it)
    note(it.response.sent, "확률 150% 는 거절")
    await ev.new_pumpkin_round(GID)
    m = P.NumbersModal(cog, await ev.get_pumpkin(GID, fresh=True))
    m.hp._value, m.dmg._value, m.crit._value, m.rate._value, m.hours._value = \
        "20,000,000", "500", "1500", "10", "48"
    await m.on_submit(Inter())
    p = await ev.get_pumpkin(GID, fresh=True)
    note((p["hp_max"], p["hp"], p["damage"], p["crit_percent"], p["min_member_hours"])
         == (20_000_000, 20_000_000, 500, 10, 48),
         "아무도 안 친 호박이면 새 체력이 바로 적용")
    await ev.save_pumpkin(GID, {"hp": 1, "round": 99})
    p2 = await ev.get_pumpkin(GID, fresh=True)
    note(p2["hp"] == p["hp"] and p2["round"] == p["round"],
         "**설정 저장으로 체력 · 회차를 못 바꿈** (깎인 체력이 되돌아가지 않게)")

    print("\n=== 10-2. 누적 데미지 랭킹 (2026-10-10) ===")
    await ev.new_pumpkin_round(GID)
    await ev.save_pumpkin(GID, {"enabled": True})
    rnd = int((await ev.get_pumpkin(GID, fresh=True))["round"])
    days = [datetime.datetime.now(KST) + datetime.timedelta(days=d) for d in range(3)]
    # A: 3일 × 2,000 = 6,000 · B: 3일 × 1,000 = 3,000 · C: 1일 × 2,000
    for d in days:
        await ev.hit_pumpkin(GID, 8001, 2000, when=d)
        await ev.hit_pumpkin(GID, 8002, 1000, when=d)
    await ev.hit_pumpkin(GID, 8003, 2000, when=days[0])
    top = await ev.pumpkin_top(GID, rnd, 10)
    note([(r["user_id"], r["damage"], r["hits"]) for r in top]
         == [("8001", 6000, 3), ("8002", 3000, 3), ("8003", 2000, 1)],
         "**누적 데미지 많은 순** (여러 날 합산 · 타격 횟수)", str(top))
    note(await ev.pumpkin_my_rank(GID, rnd, 8002) == (3000, 2), "내 누적 · 순위 (2위)")
    note(await ev.pumpkin_my_rank(GID, rnd, 9999) == (0, 0), "안 친 사람은 기록 없음")
    await asyncio.gather(*[ev.hit_pumpkin(GID, 8100 + i, 1000) for i in range(30)])
    top = await ev.pumpkin_top(GID, rnd, 100)
    note(sum(r["damage"] for r in top) == 6000 + 3000 + 2000 + 30_000,
         "**30명이 동시에 쳐도 누적이 정확함** ($inc)")
    set_hp(500)
    res = await ev.hit_pumpkin(GID, 8200, 2000)
    note(res.get("broke") and res.get("applied") == 500
         and (await ev.pumpkin_my_rank(GID, rnd, 8200))[0] == 500,
         "**마지막 한 방은 남은 체력만큼만** 셈 (500 남았는데 2,000 → 500)")
    p = await ev.get_pumpkin(GID, fresh=True)
    e = P.build_pumpkin_embed(p, 0, top=await ev.pumpkin_top(GID, rnd, 10))
    f = next((x for x in e.fields if "랭킹" in x.name), None)
    note(f is not None and "(최종)" in f.name and f.value.startswith("🥇 <@8001> — **6,000**"),
         "패널에 TOP 10 · 깨진 뒤엔 '최종'", f.value[:40] if f else "")
    note(f is not None and len(f.value.splitlines()) == 10
         and all(len(x.value) <= 1024 for x in e.fields), "TOP 10 까지만 · 칸 규격 안")
    await ev.new_pumpkin_round(GID)
    await ev.save_pumpkin(GID, {"enabled": True})
    rnd2 = int((await ev.get_pumpkin(GID, fresh=True))["round"])
    note(await ev.pumpkin_top(GID, rnd2, 10) == [], "새 호박이면 랭킹 0 부터")
    note(len(await ev.pumpkin_top(GID, rnd, 10)) == 10, "지난 호박 순위는 그대로 남음")
    text, it = await press(member(8300), rng_crit=False)
    e = it.followup.sent[-1]["embed"]
    mine = next((x for x in e.fields if "내 누적" in x.name), None)
    dmg = int((await ev.get_pumpkin(GID, fresh=True))["damage"])      # 10번에서 500 으로 바꿔 둠
    note(mine is not None and f"**{dmg:,}**" in mine.value and "1위" in mine.value,
         "때리면 내 누적 · 순위를 보여 줌", mine.value if mine else "")
    cog2 = P.Pumpkin.__new__(P.Pumpkin)
    emb, view, _ = await P.Pumpkin.render(cog2, GUILD)
    note(any("랭킹" in x.name and "<@8300>" in x.value for x in emb.fields),
         "실제 패널 그리기에 랭킹이 들어감")

    print("\n=== 11. JSON 폴백도 같은 규칙 ===")
    ev.pumpkin_col = ev.pumpkin_hits_col = ev.pumpkin_dmg_col = None
    ev._cache.clear()
    await ev.save_pumpkin(GID + 1, {"enabled": True})
    rs = await asyncio.gather(*[ev.hit_pumpkin(GID + 1, 9, 1000) for _ in range(5)])
    note([r["status"] for r in rs].count("ok") == 1, "JSON 에서도 하루 한 번")
    rnd_j = int((await ev.get_pumpkin(GID + 1, fresh=True))["round"])
    await ev.hit_pumpkin(GID + 1, 10, 2000)
    top = await ev.pumpkin_top(GID + 1, rnd_j, 10)
    note([(r["user_id"], r["damage"]) for r in top] == [("10", 2000), ("9", 1000)]
         and await ev.pumpkin_my_rank(GID + 1, rnd_j, 9) == (1000, 2), "JSON 에서도 랭킹",
         str(top))


asyncio.run(main())
print(f"\n  실패 {len(F.FAILS)}건 / 성공 {len(F.OKS)}건")
for x in F.FAILS:
    print("   -", x)
sys.exit(1 if F.FAILS else 0)
