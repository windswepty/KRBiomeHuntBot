"""채팅 적립과 상점 구매 — 서버에서 가장 자주 도는 두 경로.

이 둘은 **검증이 비어 있었습니다.**

  · `process_chat_message` 는 서버의 **모든 메시지마다** 불리는데 검증이
    한 건도 없었습니다. 여기가 틀리면 포인트가 조용히 새거나 덜 쌓입니다.
  · 상점 구매는 로직이 맞게 짜여 있지만(원자 차감 → 재고 → 실패 시 환불)
    **그걸 증명하는 검증이 없었습니다.** 뽑기에서 실제로 사고가 났던 것과
    똑같은 자리라, 다음 사람이 순서를 바꿔도 아무도 모릅니다.

**Mongo 경로를 가짜 드라이버로 실제 실행합니다.** 폴백만 검증하면
배포되는 코드를 한 줄도 보지 못합니다.
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
from fakes import GUILD, Member, Msg, Inter, note, drain

DB_COLS = ["users_col", "settings_col", "shop_col", "gacha_col", "logs_col",
           "blacklist_col", "boost_col", "snapshot_col", "backup_set_col",
           "checkin_set_col", "quest_set_col", "quest_prog_col",
           "quest_state_col", "event_col"]


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

    # ⚠️ load_extension 은 sys.modules 를 새 인스턴스로 덮어씁니다.
    CR = sys.modules["cogs.chat_reward"]
    SH = sys.modules["cogs.shop"]
    cog = bot.get_cog("ChatReward")
    note(CR.__dict__ is cog.on_message.__func__.__globals__,
         "테스트가 실제로 도는 모듈을 잡았는지")

    ch = GUILD.channels[0]

    async def reset():
        for c in ("users_col", "boost_col", "blacklist_col", "shop_col", "logs_col"):
            if getattr(getattr(db, c), "docs", None) is not None:
                getattr(db, c).docs.clear()
        if hasattr(db, "_blacklist_cache"):
            try:
                db._blacklist_cache.clear()
            except Exception:
                pass

    async def points(uid):
        return await db.get_user_points(GUILD.id, uid)

    async def chat_count(uid):
        doc = await db._db(db.users_col.find_one,
                           {"_id": db._get_user_key(GUILD.id, uid)})
        return (doc or {}).get("chat_count", 0)

    print("=== 1. 열 번째 메시지마다 적립 ===")
    await reset()
    awarded_at = []
    for i in range(1, 31):
        _, awarded, earned, mult = await db.process_chat_message(GUILD.id, 11, 0)
        if awarded:
            awarded_at.append(i)
    note(awarded_at == [10, 20, 30], "10·20·30번째에만 지급", str(awarded_at))
    note(await points(11) == 30, "3회 × 10P = 30P", str(await points(11)))
    note(await chat_count(11) == 30, "카운트 30", str(await chat_count(11)))

    print("")
    print("=== 2. ⚠️ 동시에 쏟아져도 카운트가 유실되면 안 됩니다 ===")
    # 읽고→더하고→덮어쓰면 동시 메시지끼리 서로를 지웁니다.
    await reset()
    await asyncio.gather(*[db.process_chat_message(GUILD.id, 12, 0)
                           for _ in range(100)])
    note(await chat_count(12) == 100, "100개가 전부 세어짐",
         str(await chat_count(12)))
    note(await points(12) == 100, "정확히 10회 지급 (10 × 10P)",
         str(await points(12)))

    print("")
    print("  지급 횟수도 정확히 10번이어야 합니다 (더도 덜도 아니게)")
    await reset()
    outs = await asyncio.gather(*[db.process_chat_message(GUILD.id, 13, 0)
                                  for _ in range(100)])
    note(sum(1 for o in outs if o[1]) == 10, "awarded 가 정확히 10회",
         str(sum(1 for o in outs if o[1])))

    print("")
    print("=== 3. 채널 부스트 배율 ===")
    await reset()
    # 기간은 **초**로 줍니다 (datetime 이 아닙니다).
    await db.set_channel_boost(GUILD.id, ch.id, 2.0, 3600)
    _, _, earned, mult = await db.process_chat_message(GUILD.id, 14, ch.id)
    note(mult == 2.0, "부스트 배율이 잡힘", str(mult))
    note(earned == 20, "지급액이 2배 (10 → 20P)", str(earned))

    _, _, earned2, mult2 = await db.process_chat_message(GUILD.id, 14, GUILD.channels[1].id)
    note(mult2 == 1.0, "다른 채널은 배율 1.0", str(mult2))
    note(earned2 == 10, "다른 채널은 10P", str(earned2))

    print("")
    print("  ⚠️ 부스트가 끝나면 배율이 돌아와야 합니다")
    await db.set_channel_boost(GUILD.id, ch.id, 5.0, 1)
    # 만료를 기다리지 않고 저장된 시각을 과거로 돌려 놓습니다.
    await db._db(db.boost_col.update_one, {"_id": str(ch.id)},
                 {"$set": {"expires_at": (datetime.datetime.now(datetime.timezone.utc)
                                          - datetime.timedelta(minutes=1)).isoformat()}})
    db._boost_cache.pop(str(ch.id))
    _, _, earned3, mult3 = await db.process_chat_message(GUILD.id, 15, ch.id)
    note(mult3 == 1.0, "만료된 부스트는 적용되지 않음", str(mult3))
    note(earned3 == 10, "지급액도 기본값", str(earned3))

    print("")
    print("  배율이 아무리 낮아도 최소 1P 는 나가야 합니다")
    await db.set_channel_boost(GUILD.id, ch.id, 0.01, 3600)
    _, _, earned4, _ = await db.process_chat_message(GUILD.id, 16, ch.id)
    note(earned4 >= 1, "0P 가 되지 않음", str(earned4))

    print("")
    print("=== 4. on_message 필터 (적립되면 안 되는 것들) ===")
    await reset()

    def msg(uid, content="안녕하세요", *, bot_author=False, attach=False,
            stickers=False, guild=GUILD):
        m = Msg(7000 + uid, ch, Member(uid, f"u{uid}", guild=GUILD))
        m.guild = guild
        m.author.bot = bot_author
        m.content = content
        m.attachments = [object()] if attach else []
        m.stickers = [object()] if stickers else []
        return m

    cases = [
        ("봇이 보낸 메시지", msg(21, bot_author=True), False),
        ("첨부파일", msg(22, attach=True), False),
        ("빈 메시지", msg(23, ""), False),
        ("텐서 GIF", msg(24, "https://tenor.com/view/abc"), False),
        (".gif 링크", msg(25, "https://x.com/a.gif"), False),
        ("링크만 있는 메시지", msg(26, "https://naver.com"), False),
        ("일반 채팅", msg(27, "안녕"), True),
        ("이모지만", msg(28, "😀"), False),
        ("스티커만", msg(29, "", stickers=True), False),
        ("이모지 여러 개 + 공백", msg(40, "😀 👍🏻 ❤️ ⬆️ ‼️ 1️⃣ 🇰🇷 👨‍👩‍👧"), False),
        ("커스텀 이모지만", msg(41, "<:pepe:123456789> <a:dance:987654321>"), False),
        ("스티커 + 글자", msg(42, "ㅋㅋ", stickers=True), True),
        ("이모지 + 글자", msg(43, "좋아요 😀"), True),
        ("영문 d 하나 (옛 필터가 지우던 글자)", msg(44, "d"), True),
        ("링크 + 글자", msg(30, "이거 봐 https://naver.com 좋지"), True),
    ]
    for label, m, want in cases:
        before = await chat_count(m.author.id)
        await cog.on_message(m)
        await drain()
        after = await chat_count(m.author.id)
        got = after > before
        note(got == want,
             f"{label}: {'적립됨' if want else '무시됨'}", f"{before} → {after}")

    print("")
    print("  이모지만 · 글자 섞임을 문장 단위로 가립니다 (counts_as_chat)")
    for text, want in [("😀😀😀", False), ("‼️⁉️", False), ("〰", False), ("✅", False),
                       ("#️⃣ 9️⃣", False), ("🙏🏽", False), ("ㅋ", True), ("?", True),
                       ("ok 👍", True), ("123", True), ("<@123> 안녕", True),
                       ("https://tenor.com/x 하하", False), ("dd", True)]:
        note(CR.counts_as_chat(text) == want, f"{text!r} → {'셈' if want else '안 셈'}")

    print("")
    print("  DM 은 guild 가 None 이라 그냥 넘어가야 합니다")
    m = msg(31)
    m.guild = None
    try:
        await cog.on_message(m)
        await drain()
        note(True, "DM 에서 예외가 나지 않음")
    except Exception as e:
        note(False, "DM 에서 예외", f"{type(e).__name__}: {e}")

    print("")
    print("  ⚠️ 블랙리스트는 적립되면 안 됩니다")
    await db.add_blacklist_user(GUILD.id, 32, "테스트")
    m = msg(32, "안녕")
    await cog.on_message(m)
    await drain()
    note(await chat_count(32) == 0, "블랙리스트 유저는 세지 않음",
         str(await chat_count(32)))

    print("")
    print("=== 5. ⚠️ 상점: 마지막 재고를 둘이 동시에 사면 ===")
    # 뽑기에서 실제로 사고가 났던 자리와 같은 구조입니다.
    await reset()
    await db.save_shop_settings(GUILD.id, {
        "enabled": True, "title": "테스트 상점",
        "items": [{"id": 1, "name": "한정템", "price": 100, "stock": 1,
                   "reward_role_id": 0, "description": ""}]})
    for uid in (41, 42):
        await db.add_user_points(GUILD.id, uid, 1000)

    got = await asyncio.gather(*[db.consume_shop_stock(GUILD.id, 1)
                                 for _ in range(2)])
    note(sum(1 for g in got if g) == 1, "둘 중 하나만 성공", str(got))
    s = await db.get_shop_settings(GUILD.id)
    note(s["items"][0]["stock"] == 0, "재고가 정확히 0", str(s["items"][0]["stock"]))
    note(s["items"][0]["stock"] >= 0, "재고가 음수로 내려가지 않음")

    print("")
    print("  재고 5개에 10명이 달려들어도 5개만 나가야 합니다")
    await db.save_shop_settings(GUILD.id, {
        "items": [{"id": 1, "name": "한정템", "price": 100, "stock": 5,
                   "reward_role_id": 0, "description": ""}]})
    got = await asyncio.gather(*[db.consume_shop_stock(GUILD.id, 1)
                                 for _ in range(10)])
    s = await db.get_shop_settings(GUILD.id)
    note(sum(1 for g in got if g) == 5, "정확히 5명만 성공",
         str(sum(1 for g in got if g)))
    note(s["items"][0]["stock"] == 0, "재고 0", str(s["items"][0]["stock"]))

    print("")
    print("  ⚠️ 포인트도 재고 이상으로 빠지면 안 됩니다 (구매 콜백 전체)")
    await reset()
    await db.save_shop_settings(GUILD.id, {
        "enabled": True, "title": "테스트 상점",
        "items": [{"id": 1, "name": "한정템", "price": 100, "stock": 2,
                   "reward_role_id": 0, "description": ""}]})
    buyers = list(range(51, 59))
    for uid in buyers:
        await db.add_user_points(GUILD.id, uid, 500)

    async def buy(uid):
        try:
            settings = await db.get_shop_settings(GUILD.id)
            sel = SH.ShopSelect(settings.get("items", []))
            sel._values = ["1"]
            await sel.callback(Inter(uid=uid, admin=False))
        except Exception as e:
            print("   구매 중 예외:", type(e).__name__, e)

    await asyncio.gather(*[buy(u) for u in buyers])
    await drain()
    left = await asyncio.gather(*[points(u) for u in buyers])
    spent = sum(500 - v for v in left)
    s = await db.get_shop_settings(GUILD.id)
    note(s["items"][0]["stock"] == 0, "재고가 정확히 0", str(s["items"][0]["stock"]))
    note(spent == 200, "**빠진 포인트도 딱 2개분(200P)**", f"{spent}P")

    print("")
    print("  포인트가 모자라면 재고도 줄지 않아야 합니다")
    await reset()
    await db.save_shop_settings(GUILD.id, {
        "enabled": True, "title": "테스트 상점",
        "items": [{"id": 1, "name": "비싼템", "price": 10000, "stock": 3,
                   "reward_role_id": 0, "description": ""}]})
    await db.add_user_points(GUILD.id, 61, 50)
    await buy(61)
    await drain()
    s = await db.get_shop_settings(GUILD.id)
    note(s["items"][0]["stock"] == 3, "재고 그대로", str(s["items"][0]["stock"]))
    note(await points(61) == 50, "포인트도 그대로", str(await points(61)))

    await bot.close()
    print(f"\n  실패 {len(F.FAILS)}건 / 성공 {len(F.OKS)}건")
    for x in F.FAILS:
        print("   -", x)


asyncio.run(main())
