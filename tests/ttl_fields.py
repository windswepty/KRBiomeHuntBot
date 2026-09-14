"""TTL 인덱스가 보는 필드를 쓰기 경로가 실제로 채우는가.

MongoDB TTL 은 **필드가 없는 문서를 지우지 않습니다.** 인덱스가 있어도
쓰기 쪽에서 그 필드를 안 쓰면 만료는 영원히 일어나지 않습니다.
실 DB 에서 quest_state 5건 전부, guarded_keys 117건 전부가 그랬습니다.

인덱스 선언을 코드에서 읽어와, 같은 컬렉션에 실제로 쓴 문서가
그 필드를 datetime 으로 갖는지 대조합니다.
"""
import sys, os, tempfile, asyncio, datetime, re, io

PROJ = sys.argv[1]
sys.path.insert(0, PROJ)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(tempfile.mkdtemp())
os.environ["MONGO_URI"] = ""

import fakemongo as FM
import fakes as F
from fakes import note

DB_COLS = ["users_col", "settings_col", "shop_col", "gacha_col", "logs_col",
           "blacklist_col", "boost_col", "snapshot_col", "backup_set_col",
           "checkin_set_col", "quest_set_col", "quest_prog_col",
           "quest_state_col", "event_col"]
VDB_COLS = ["verify_col", "linkguard_col", "plink_col", "clicklog_col",
            "watermark_col", "guardkey_col", "usage_col", "logref_col",
            "roblox_col", "biome_col"]


def declared_ttls(path):
    """create_index(..., expireAfterSeconds=...) 선언을 소스에서 읽습니다."""
    src = io.open(path, encoding="utf-8").read()
    out = {}
    for m in re.finditer(
            r"(\w+)\.create_index\(\s*[\"'](\w+)[\"'][^)]*?expireAfterSeconds",
            src, re.S):
        out[m.group(1)] = m.group(2)
    return out


async def main():
    import utils.database as db
    import utils.verify_db as vdb

    ttl_db = declared_ttls(os.path.join(PROJ, "utils/database.py"))
    ttl_vdb = declared_ttls(os.path.join(PROJ, "utils/verify_db.py"))
    print("선언된 TTL 인덱스:")
    for k, v in {**ttl_db, **ttl_vdb}.items():
        print(f"   {k:18s} → {v}")

    cols = FM.install(db, DB_COLS)
    vcols = FM.install(vdb, [c for c in VDB_COLS if hasattr(vdb, c)])
    G, U = 1, 7

    print("\n=== 쓰기 경로를 실제로 실행합니다 ===")
    await db.take_checkin_rank(G)
    await db.bump_server_goal(G, 5)
    await db.mark_server_goal(G, 1, announced=True)
    await db.bump_quest_progress(G, U, "chat", 1)
    await vdb.register_guarded_keys(G, ["https://example.com/a"])
    await vdb.save_protected_link(G, 12345, ["https://example.com/a"])
    await vdb.record_biome_event(G, "DREAMSPACE", "ps:abc", 111, 222, True)

    checks = [("quest_state_col", cols), ("quest_prog_col", cols),
              ("guardkey_col", vcols), ("plink_col", vcols),
              ("usage_col", vcols), ("logref_col", vcols),
              ("biome_col", vcols)]

    print()
    for attr, pool in checks:
        field = ttl_db.get(attr) or ttl_vdb.get(attr)
        if not field or attr not in pool:
            continue
        docs = list(pool[attr].docs.values())
        if not docs:
            note(True, f"{attr}: 이번 실행에서 쓰지 않음 (건너뜀)")
            continue
        missing = [d for d in docs if field not in d]
        wrongtype = [d for d in docs
                     if field in d and not isinstance(d[field], datetime.datetime)]
        note(not missing and not wrongtype,
             f"{attr}.{field} — 쓴 문서 {len(docs)}건 전부 Date",
             f"누락 {len(missing)} / 타입불일치 {len(wrongtype)}")

    print("\n=== created 는 고정이 아니라 갱신되어야 합니다 ===")
    # 주기가 TTL(21일)보다 길게 설정되면, 고정된 created 때문에
    # 진행 중인 목표 문서가 도중에 지워져 진행도가 0 이 됩니다.
    doc = list(cols["quest_state_col"].docs.values())[0]
    first = doc["created"]
    await asyncio.sleep(0.01)
    await db.bump_server_goal(G, 3)
    doc = list(cols["quest_state_col"].docs.values())[0]
    note(doc["created"] > first, "다시 활동하면 created 갱신 (마지막 활동 기준)",
         f"{first.time()} → {doc['created'].time()}")
    note(doc["goal_progress"] == 8, "진행도는 $inc 로 누적", str(doc["goal_progress"]))

    print("")
    print("=== Mongo 경로를 탔는지 (폴백이면 검증한 게 없습니다) ===")
    tally = sum(sum(getattr(c, "op_counts", {}).values())
                for c in list(cols.values()) + list(vcols.values()))
    note(tally > 0, "가짜 Mongo 컬렉션이 실제로 호출됨", f"연산 {tally}회")

asyncio.run(main())
print(f"\n  실패 {len(F.FAILS)}건 / 성공 {len(F.OKS)}건")
for x in F.FAILS:
    print("   -", x)
sys.exit(1 if F.FAILS else 0)
