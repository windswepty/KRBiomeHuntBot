"""Mongo 경로를 가짜 드라이버로 실제 실행합니다 (실 DB 를 건드리지 않음).

이 파일이 검증하는 것은 **JSON 폴백이 아니라 실제로 배포되어 도는 코드**입니다.
`_db()` 가 스레드로 넘기므로 await 중단점이 생기고, 그 틈에서 읽기→수정→쓰기가
깨지는지를 봅니다.
"""
import sys, os, tempfile, asyncio, datetime

PROJ = sys.argv[1]
sys.path.insert(0, PROJ)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(tempfile.mkdtemp())
os.environ["MONGO_URI"] = ""          # 진짜 연결은 절대 하지 않습니다

import fakemongo as FM
import fakes as F
from fakes import note

DB_COLS = ["users_col", "settings_col", "shop_col", "gacha_col", "logs_col",
           "blacklist_col", "boost_col", "snapshot_col", "backup_set_col",
           "checkin_set_col", "quest_set_col", "quest_prog_col",
           "quest_state_col", "event_col"]

VDB_COLS = ["verify_col", "linkguard_col", "plink_col", "clicklog_col",
            "watermark_col", "guardkey_col", "usage_col", "logref_col",
            "roblox_col", "oauth_col", "oauth_pending_col"]


class UpdateOne:
    def __init__(self, filt, update, upsert=False):
        self._filter, self._update, self._upsert, self._doc = filt, update, upsert, None


class ReplaceOne:
    def __init__(self, filt, doc, upsert=False):
        self._filter, self._doc, self._upsert, self._update = filt, doc, upsert, None


async def main():
    import utils.database as db
    import utils.verify_db as vdb

    # ── 가짜 드라이버 장착 ─────────────────────────────────────────
    cols = FM.install(db, DB_COLS)
    have_v = [c for c in VDB_COLS if hasattr(vdb, c)]
    vcols = FM.install(vdb, have_v)
    db._AFTER = FM.AFTER
    db._UpdateOne = UpdateOne
    db.STORAGE_MODE = "mongo"
    for m in (db, vdb):
        if hasattr(m, "_AFTER"):
            m._AFTER = FM.AFTER
        if hasattr(m, "_ReplaceOne"):
            m._ReplaceOne = ReplaceOne
    print(f"가짜 컬렉션 장착: database {len(cols)}개 / verify_db {len(vcols)}개")

    # 폴백으로 새는지 감시합니다. Mongo 경로가 예외로 죽고 조용히 JSON 으로
    # 넘어가면 '통과'로 보이지만 실제로는 아무것도 검증하지 못한 것입니다.
    fell_back = []
    real_print = __builtins__["print"] if isinstance(__builtins__, dict) \
        else __builtins__.print

    import builtins

    def watch_print(*a, **k):
        msg = " ".join(str(x) for x in a)
        if "using local file" in msg or "MongoDB" in msg and "error" in msg:
            fell_back.append(msg)
        real_print(*a, **k)

    builtins.print = watch_print

    try:
        await run_all(db, vdb, cols, vcols)
    finally:
        builtins.print = real_print

    print("\n=== 폴백 누수 검사 ===")
    note(not fell_back, "Mongo 경로가 JSON 으로 새지 않음",
         f"{len(fell_back)}건" + (f" 예: {fell_back[0][:80]}" if fell_back else ""))

    print("\n=== 실제로 실행된 연산 ===")
    for name, c in sorted(list(cols.items()) + list(vcols.items())):
        if c.op_counts:
            print(f"   {name:18} {c.op_counts}")

    print(f"\n  실패 {len(F.FAILS)}건 / 성공 {len(F.OKS)}건")
    for x in F.FAILS:
        print("   -", x)


async def run_all(db, vdb, cols, vcols):
    G = 1

    print("\n=== 포인트 (Mongo $inc · 조건부 차감) ===")
    await db.add_user_points(G, 500, 1000)
    note(await db.get_user_points(G, 500) == 1000, "지급",
         f"{await db.get_user_points(G, 500)}P")

    res = await asyncio.gather(*[db.deduct_user_points(G, 500, 100)
                                 for _ in range(25)])
    ok_n = sum(1 for ok, _ in res if ok)
    left = await db.get_user_points(G, 500)
    note(ok_n == 10 and left == 0, "동시 차감 25회 → 정확히 10회만",
         f"성공 {ok_n}회 / 잔액 {left}P")

    res = await asyncio.gather(*[db.deduct_user_points(G, 500, 1)
                                 for _ in range(10)])
    note(all(not ok for ok, _ in res) and await db.get_user_points(G, 500) == 0,
         "잔액 0 에서는 전부 실패 (마이너스 없음)",
         f"{await db.get_user_points(G, 500)}P")

    await asyncio.gather(*[db.add_user_points(G, 501, 7) for _ in range(200)])
    note(await db.get_user_points(G, 501) == 1400, "동시 지급 200회 합계",
         f"{await db.get_user_points(G, 501)}P")

    print("\n=== 출석 (연속 판정 · 보너스 중복) ===")
    await db.save_checkin_settings(G, {"base": 100, "cycle": 7,
                                       "cycle_step": 100, "cycle_max": 0,
                                       "milestones": {"7": 200}})
    r = await db.checkin_user(G, 510)
    note(r["ok"] and r["streak"] == 1 and r["gained"] == 100, "첫 출석",
         f"{r['gained']}P / {r['streak']}일")

    res = await asyncio.gather(*[db.checkin_user(G, 511) for _ in range(40)])
    ok_n = sum(1 for x in res if x["ok"])
    note(ok_n == 1, "동시 출석 40회 → 1회만 지급", f"{ok_n}회")
    note(await db.get_user_points(G, 511) == 100, "포인트도 1회분만",
         f"{await db.get_user_points(G, 511)}P")

    # 어제 출석 → 오늘 연속 이어짐
    key = db._get_user_key(G, 512)
    cols["users_col"].docs[key] = {
        "_id": key, "guild_id": str(G), "user_id": "512",
        "points": 600, "streak": 6, "last_checkin": db.get_yesterday_str()}
    r = await db.checkin_user(G, 512)
    note(r["ok"] and r["streak"] == 7, "연속 7일째 판정", f"{r['streak']}일")
    note(r["bonus"] == 100 and r["milestone"] == 200,
         "주기 보너스 + 특별 보상 동시 지급",
         f"기본{r['base']} + 주기{r['bonus']} + 특별{r['milestone']}")
    note(await db.get_user_points(G, 512) == 600 + 400, "합계 정확",
         f"{await db.get_user_points(G, 512)}P")

    # 연속이 끊긴 경우
    key = db._get_user_key(G, 513)
    cols["users_col"].docs[key] = {
        "_id": key, "guild_id": str(G), "user_id": "513", "points": 0,
        "streak": 30, "last_checkin": "2020-01-01"}
    r = await db.checkin_user(G, 513)
    note(r["streak"] == 1, "끊기면 1일로 초기화", f"{r['streak']}일")

    print("\n=== 재고 ($elemMatch + 위치 연산자) ===")
    await db.save_shop_items(G, [{"id": 1, "name": "한정판", "price": 10,
                                  "stock": 3},
                                 {"id": 2, "name": "다른것", "stock": 99}])
    res = await asyncio.gather(*[db.consume_shop_stock(G, 1) for _ in range(20)])
    items = {i["id"]: i for i in await db.get_shop_items(G)}
    note(sum(res) == 3 and items[1]["stock"] == 0,
         "재고 3개 · 20명 동시 → 3명만", f"성공 {sum(res)}회 / 남은 {items[1]['stock']}")
    note(items[2]["stock"] == 99, "다른 항목 재고는 건드리지 않음",
         str(items[2]["stock"]))

    res = await asyncio.gather(*[db.consume_shop_stock(G, 1) for _ in range(5)])
    note(not any(res) and items[1]["stock"] == 0, "품절 뒤에는 전부 실패")

    g = await db.get_gacha_settings(G)
    g["items"] = [{"id": "a", "name": "레어", "stock": 2}]
    await db.save_gacha_settings(G, g)
    res = await asyncio.gather(*[db.consume_gacha_stock(G, "a") for _ in range(9)])
    left = (await db.get_gacha_settings(G))["items"][0]["stock"]
    note(sum(res) == 2 and left == 0, "뽑기 재고 (문자 id)",
         f"성공 {sum(res)}회 / 남은 {left}")

    print("\n=== 퀘스트 진행도 · 수령 ===")
    await asyncio.gather(*[db.bump_quest_progress(G, 520, "chat")
                           for _ in range(300)])
    p = await db.get_quest_progress(G, 520)
    note(p["p"].get("chat") == 300, "동시 진행도 300회", str(p["p"].get("chat")))

    await asyncio.gather(*[db.bump_quest_progress(G, 520, "wchat", weekly=True)
                           for _ in range(150)])
    p = await db.get_quest_progress(G, 520)
    note(p["p"].get("wchat") == 150 and p["p"].get("chat") == 300,
         "주간과 일일이 섞이지 않음",
         f"일일 {p['p'].get('chat')} / 주간 {p['p'].get('wchat')}")

    res = await asyncio.gather(*[db.claim_quest(G, 520, "d:chat", 500)
                                 for _ in range(20)])
    note(sum(1 for x in res if x) == 1, "동시 수령 20회 → 1회만",
         f"{sum(1 for x in res if x)}회")
    note(await db.get_user_points(G, 520) == 500, "포인트도 1회분",
         f"{await db.get_user_points(G, 520)}P")

    ranks = await asyncio.gather(*[db.take_checkin_rank(G) for _ in range(60)])
    note(len(set(ranks)) == 60 and max(ranks) == 60, "선착순 등수 60개 전부 다름",
         f"{len(set(ranks))}종 / 최대 {max(ranks)}")

    goals = await asyncio.gather(*[db.bump_server_goal(G) for _ in range(400)])
    note((await db.get_server_goal(G))["progress"] == 400, "공동 목표 400회",
         str((await db.get_server_goal(G))["progress"]))
    note(len(set(goals)) == 400, "반환값도 전부 다름 (등수로 쓸 수 있음)",
         f"{len(set(goals))}종")

    print("\n=== 이벤트 진행도 ===")
    today = datetime.datetime.now(db.KST).strftime("%Y-%m-%d")
    await db.save_event(G, "e1", {"name": "테스트", "mode": "tier",
                                  "kind": "chat", "enabled": True,
                                  "starts": today, "ends": "2099-12-31",
                                  "tiers": [{"goal": 100, "points": 500}]})
    evs = await db.active_events(G)
    note(len(evs) == 1, "활성 이벤트 조회", f"{len(evs)}개")
    await asyncio.gather(*[db.bump_event_progress(G, 530, "chat")
                           for _ in range(120)])
    p = await db.get_quest_progress(G, 530, [db.event_period_key("e1")])
    note(p["p"].get("ev_chat") == 120, "이벤트 진행도 동시 120회",
         str(p["p"].get("ev_chat")))

    print("\n=== 랭킹 · 로그 (find/sort/limit) ===")
    for uid, pts in ((601, 5000), (602, 3000), (603, 9000), (604, 100)):
        await db.add_user_points(G, uid, pts)
    top = await db.get_top_users(G, 3)
    note([t["points"] for t in top] == [9000, 5000, 3000], "랭킹 정렬",
         str([t["points"] for t in top]))
    rank, total = await db.get_user_rank(G, 602)
    note(rank == 3 and total == 3000,
         "내 등수 (count_documents)", f"{rank}등 / 보유 {total}P")

    for i in range(5):
        await db.add_activity_log(G, "shop", 700, "유저", f"구매 {i}")
    logs = await db.get_activity_logs(G, "shop", 10)
    note(len(logs) == 5, "활동 로그 조회", f"{len(logs)}건")

    print("\n=== 스냅샷 · 복원 (bulk_write) ===")
    snap = await db.create_point_snapshot(G, reason="test")
    note(snap["ok"], "스냅샷 생성",
         f"{snap['user_count']}명 / {snap['total_points']}P")
    await db.add_user_points(G, 603, -8000)
    note(await db.get_user_points(G, 603) == 1000, "사고 재현")
    snaps = await db.list_point_snapshots(G, limit=10)
    sid = next(s["id"] for s in snaps if s.get("reason") == "test")
    r = await db.restore_point_snapshot(G, sid)
    note(r["ok"], "복원 실행", str(r.get("user_count")))
    note(await db.get_user_points(G, 603) == 9000, "포인트 복구",
         f"{await db.get_user_points(G, 603)}P")

    print("\n=== 블랙리스트 · 부스트 ===")
    await db.add_blacklist_user(G, 800, "도배")
    note(await db.is_user_blacklisted(G, 800), "차단됨")
    note(not await db.is_user_blacklisted(G, 801), "다른 사람은 아님")
    note(await db.remove_blacklist_user(G, 800), "해제 성공")
    note(not await db.is_user_blacklisted(G, 800), "해제 반영")

    await db.set_channel_boost(G, 111, 2.0, 3600)
    note(await db.get_channel_boost(G, 111) == 2.0, "부스트 적용",
         str(await db.get_channel_boost(G, 111)))
    note(await db.get_channel_boost(G, 222) == 1.0, "다른 채널은 1.0")

    print("\n=== 기동 기록 ($push $each $slice) ===")
    for _ in range(12):
        st = await db.record_boot(G)
    hist = cols["backup_set_col"].docs.get(str(G), {}).get("boot_history", [])
    note(len(hist) == 8, "기동 이력이 8개로 잘림 ($slice -8)", f"{len(hist)}개")

    print("\n=== 인증 · 연동 (verify_db) ===")
    await vdb.save_verify_settings(G, {"enabled": True, "role_id": 901})
    s = await vdb.get_verify_settings(G)
    note(s["role_id"] == 901 and s["enabled"], "인증 설정 저장·조회")

    if hasattr(vdb, "save_roblox_link"):
        await vdb.save_roblox_link(G, 900, 12345, "TestUser")
        got = await vdb.get_roblox_link(G, 900)
        note(got and str(got.get("roblox_id")) == "12345", "연동 저장·조회",
             str(got.get("roblox_name") if got else None))
        rows = await vdb.search_roblox_links(G, "TestUser", 10)
        note(len(rows) == 1, "이름으로 검색 ($regex)", f"{len(rows)}건")
        rows = await vdb.search_roblox_links(G, ".*", 10)
        note(len(rows) == 0, "정규식 특수문자는 문자 그대로 (re.escape)",
             f"{len(rows)}건 — 명단 덤프 방지")

    if hasattr(vdb, "record_oauth_result"):
        print("\n=== 로그인 결과 집계 ($inc upsert · $gte) ===")
        await asyncio.gather(*(vdb.record_oauth_result(r)
                               for r in ["ok"] * 5 + ["used", "unknown"]))
        await vdb.record_oauth_result("x.y")
        res = await vdb.get_oauth_results(7)
        c = res["counts"]
        note(c.get("ok") == 5 and c.get("used") == 1 and c.get("unknown") == 1,
             "동시에 기록해도 빠짐없이 셈 ($inc)", str(c))
        note(c.get("other") == 1 and "x.y" not in c,
             "모르는 이유는 other (점이 필드 이름으로 가지 않음)", str(c))
        note(bool(res["first_day"]), "기록 시작일", res["first_day"])


asyncio.run(main())
