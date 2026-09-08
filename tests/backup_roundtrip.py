"""세 백업 경로의 형식이 정말 같은지 — 채널에 올린 파일을
tools/full_backup.py 가 그대로 복원할 수 있어야 백업으로서 의미가 있습니다."""
import sys, os, tempfile, asyncio, json, importlib.util

PROJ = sys.argv[1]
sys.path.insert(0, PROJ)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
WORK = tempfile.mkdtemp()
os.chdir(WORK)
os.environ["MONGO_URI"] = ""

import discord
import fakemongo as FM
import fakes as F
from fakes import GUILD, Member, note, drain

DB_COLS = ["users_col", "settings_col", "shop_col", "gacha_col", "logs_col",
           "blacklist_col", "boost_col", "snapshot_col", "backup_set_col",
           "checkin_set_col", "quest_set_col", "quest_prog_col",
           "quest_state_col", "event_col"]
VDB_COLS = ["verify_col", "linkguard_col", "plink_col", "clicklog_col",
            "watermark_col", "guardkey_col", "usage_col", "logref_col",
            "roblox_col"]


class UpdateOne:
    def __init__(self, filt, update, upsert=False):
        self._filter, self._update, self._upsert, self._doc = filt, update, upsert, None


class ReplaceOneOp:
    def __init__(self, filt, doc, upsert=False):
        self._filter, self._doc, self._upsert, self._update = filt, doc, upsert, None


async def main():
    import utils.database as db
    import utils.verify_db as vdb

    cols = FM.install(db, DB_COLS)
    vcols = FM.install(vdb, VDB_COLS)
    db._AFTER = FM.AFTER
    db._UpdateOne = UpdateOne
    vdb._AFTER = FM.AFTER
    db.STORAGE_MODE = "mongo"

    # export_full_payload 는 mongo_db 에서 컬렉션 목록을 직접 읽습니다.
    # 실제 Mongo 이름으로 묶어야 백업 파일이 진짜와 같은 모양이 됩니다.
    NAMES = {
        "users": cols["users_col"], "report_settings": cols["settings_col"],
        "shop_items": cols["shop_col"], "gacha_settings": cols["gacha_col"],
        "activity_logs": cols["logs_col"], "blacklist": cols["blacklist_col"],
        "channel_boosts": cols["boost_col"],
        "point_snapshots": cols["snapshot_col"],
        "backup_settings": cols["backup_set_col"],
        "checkin_settings": cols["checkin_set_col"],
        "quest_settings": cols["quest_set_col"],
        "quest_progress": cols["quest_prog_col"],
        "quest_state": cols["quest_state_col"],
        "quest_events": cols["event_col"],
        "verify_settings": vcols["verify_col"],
        "linkguard_settings": vcols["linkguard_col"],
        "protected_links": vcols["plink_col"],
        "link_clicks": vcols["clicklog_col"],
        "link_watermarks": vcols["watermark_col"],
        "guarded_keys": vcols["guardkey_col"],
        "link_usage": vcols["usage_col"],
        "log_references": vcols["logref_col"],
        "roblox_links": vcols["roblox_col"],
    }
    db.mongo_db = FM.FakeDatabase(NAMES)

    from discord.ext import commands
    bot = commands.Bot(command_prefix="!", intents=discord.Intents.all())
    for f in sorted(os.listdir(os.path.join(PROJ, "cogs"))):
        if f.endswith(".py") and not f.startswith("__"):
            await bot.load_extension(f"cogs.{f[:-3]}")
    bot.guilds_override = [GUILD]

    print("=== 채워 넣기 ===")
    for uid, pts in ((1001, 5000), (1002, 1200), (1003, 88)):
        await db.add_user_points(GUILD.id, uid, pts)
    await db.save_shop_items(GUILD.id, [{"id": 1, "name": "VIP", "price": 900,
                                         "stock": 5}])
    await db.save_quest_settings(GUILD.id, {"enabled": True,
                                            "channel_ids": ["111"],
                                            "goal_target": 3000})
    await vdb.save_verify_settings(GUILD.id, {"enabled": True, "role_id": 901,
                                              "required_role_ids": [902]})
    await vdb.save_linkguard_settings(GUILD.id, {"enabled": True,
                                                 "channel_ids": [111],
                                                 "relay_links": True})
    await vdb.save_roblox_link(GUILD.id, 1001, 777888, "RobloxUser")
    await db.save_event(GUILD.id, "e1", {"name": "추석", "mode": "tier",
                                         "kind": "chat", "enabled": True,
                                         "starts": "2026-01-01",
                                         "ends": "2099-12-31", "tiers": []})
    # ObjectId 가 실제로 들어가는 컬렉션도 채웁니다
    # (활동 로그·스냅샷은 _id 를 주지 않아 Mongo 가 ObjectId 를 만듭니다)
    await db.add_activity_log(GUILD.id, "shop", 1001, "유저", "VIP 구매")
    await db.create_point_snapshot(GUILD.id, reason="test")
    note(True, "설정·포인트·연동·로그·스냅샷을 모두 넣음")

    print("\n=== 전체 백업 페이로드 ===")
    payload = await db.export_full_payload()
    meta = payload.get("_meta", {})
    counts = meta.get("collections", {})
    keys = set(payload.get("data", {}))
    note(bool(counts), "컬렉션별 건수 기록",
         f"{len(counts)}종 / 총 {sum(counts.values())}건")

    # 포인트 이코노미와 인증 두 시스템이 **모두** 담겨야 합니다.
    # 스냅샷은 같은 클러스터라 못 막는 사고를 이 파일이 막습니다.
    note("users" in keys, "포인트가 담김", str(counts.get("users")))
    note({"verify_settings", "linkguard_settings"} <= keys,
         "인증·링크 보호 설정도 담김",
         str(sorted(k for k in keys if "verif" in k or "link" in k)))
    note({"quest_settings", "quest_events"} <= keys,
         "퀘스트·이벤트 설정도 담김",
         str(sorted(k for k in keys if "quest" in k)))
    note("roblox_links" in keys, "로블록스 연동 명단도 담김",
         str(counts.get("roblox_links")))

    print("\n=== 타입 보존 (ObjectId · datetime) ===")
    import cogs.backup as B
    data, filename = B.full_backup_bytes(payload)
    note(filename.endswith(".json"), "파일 이름", filename)
    blob = (data if isinstance(data, bytes) else data.read()).decode("utf-8")
    note("$oid" in blob, "ObjectId 가 {\"$oid\":...} 로 보존됨",
         "문자열로 뭉개지면 복원이 조용히 망가집니다")

    # created 는 BSON Date 여야 TTL 인덱스가 동작합니다
    await db.bump_quest_progress(GUILD.id, 1001, "chat")
    p2 = await db.export_full_payload()
    d2, _ = B.full_backup_bytes(p2)
    blob2 = (d2 if isinstance(d2, bytes) else d2.read()).decode("utf-8")
    note("$date" in blob2, "created 가 BSON Date 로 보존됨",
         "문자열이면 TTL 인덱스가 영영 안 지웁니다")

    print("\n=== tools/full_backup.py 가 같은 파일을 읽는가 ===")
    fn = os.path.join(WORK, "krbiomehunt-full-test.json")
    payload = p2
    keys = set(payload.get("data", {}))
    d3, _ = B.full_backup_bytes(payload)
    with open(fn, "wb") as f:
        f.write(d3 if isinstance(d3, bytes) else d3.read())

    spec = importlib.util.spec_from_file_location(
        "fullbk", os.path.join(PROJ, "tools", "full_backup.py"))
    fb = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fb)

    loaded = fb._load(fn)
    note(isinstance(loaded, dict) and loaded, "_load 가 파일을 읽음",
         f"{len(loaded)}개 키")
    got = set(loaded.get("data", loaded))
    note(got == keys, "컬렉션 구성이 백업과 완전히 일치",
         f"{len(got)}종" + ("" if got == keys else f" 차이 {got ^ keys}"))

    # _decode 가 $oid/$date 를 원래 타입으로 되돌리는지
    decoded = fb._decode({"a": {"$date": "2026-08-30T00:00:00+00:00"},
                          "b": {"$oid": "0123456789abcdef01234567"}})
    import datetime as _dt
    note(isinstance(decoded["a"], _dt.datetime),
         "$date → datetime 복원", type(decoded["a"]).__name__)
    note(type(decoded["b"]).__name__ == "ObjectId",
         "$oid → ObjectId 복원", type(decoded["b"]).__name__)

    # verify() 는 파일을 **실 DB 와 대조**하는 함수라 연결이 필요합니다.
    # 여기서는 대신 복원이 실제로 넣을 문서를 그대로 되살려 봅니다.
    restored = fb._decode(loaded)["data"]
    note(set(restored) == keys, "복원 대상 컬렉션이 그대로",
         f"{len(restored)}종")
    ids = [d["_id"] for d in restored["users"]]
    note(len(ids) == len(set(ids)) == 3, "유저 문서가 온전",
         f"{len(ids)}건")
    pts = {str(d["_id"]): d.get("points") for d in restored["users"]}
    note(sorted(pts.values()) == [88, 1200, 5000], "포인트 값이 정확히 왕복",
         str(sorted(pts.values())))
    logs = restored.get("activity_logs", [])
    note(logs and type(logs[0]["_id"]).__name__ == "ObjectId",
         "로그 _id 가 ObjectId 로 복원", type(logs[0]["_id"]).__name__ if logs else "없음")

    print("\n=== 복원을 실제로 실행 ===")
    # 사고를 냅니다: 포인트를 망가뜨리고, 봇이 살아 있는 것처럼 새 문서도 넣습니다
    await db.add_user_points(GUILD.id, 1001, -4900)
    await db.add_user_points(GUILD.id, 9999, 777)      # 파일에 없는 새 유저
    note(await db.get_user_points(GUILD.id, 1001) == 100, "사고 재현",
         f"{await db.get_user_points(GUILD.id, 1001)}P")

    fake_db = FM.FakeDatabase(NAMES)
    fb._connect = lambda: fake_db
    fb.dump = lambda tag="full": "(pre-restore 백업 생략)"
    fb.ReplaceOne = ReplaceOneOp

    fb.restore(fn, None, wipe=False, yes=True)
    note(await db.get_user_points(GUILD.id, 1001) == 5000, "포인트 복구",
         f"{await db.get_user_points(GUILD.id, 1001)}P")
    note(await db.get_user_points(GUILD.id, 9999) == 777,
         "파일에 없던 새 유저는 건드리지 않음 (wipe 아님)",
         f"{await db.get_user_points(GUILD.id, 9999)}P")

    print("\n  같은 파일로 두 번 돌려도 같은 결과 (멱등)")
    fb.restore(fn, None, wipe=False, yes=True)
    note(await db.get_user_points(GUILD.id, 1001) == 5000, "두 번째도 동일",
         f"{await db.get_user_points(GUILD.id, 1001)}P")

    print("\n  봇이 끼어들어도 E11000 으로 멈추지 않음")
    # 복원 도중 봇이 만든 문서와 _id 가 겹치는 상황
    await db.add_user_points(GUILD.id, 1002, 50)
    try:
        fb.restore(fn, None, wipe=False, yes=True)
        note(True, "_id 가 겹쳐도 끝까지 완주")
    except Exception as e:
        note(False, "_id 충돌로 중단", f"{type(e).__name__}: {e}")
    note(await db.get_user_points(GUILD.id, 1002) == 1200,
         "겹친 문서는 파일 값으로 덮임",
         f"{await db.get_user_points(GUILD.id, 1002)}P")

    print("\n  --wipe 는 넣고 나서 지웁니다")
    fb.restore(fn, ["users"], wipe=True, yes=True)
    note(await db.get_user_points(GUILD.id, 1001) == 5000,
         "파일에 있던 유저는 그대로")
    note(await db.get_user_points(GUILD.id, 9999) == 0,
         "파일에 없던 유저만 삭제됨",
         f"{await db.get_user_points(GUILD.id, 9999)}P")

    print("\n  --yes 없이는 아무것도 바꾸지 않음")
    await db.add_user_points(GUILD.id, 1001, -3000)
    try:
        fb.restore(fn, None, wipe=False, yes=False)
        note(False, "--yes 없이 실행됨 (막았어야 합니다)")
    except SystemExit:
        note(await db.get_user_points(GUILD.id, 1001) == 2000,
             "--yes 없으면 안내만 하고 종료",
             f"{await db.get_user_points(GUILD.id, 1001)}P (그대로)")

    print("\n=== /백업설정 → 채널 업로드 ===")
    await db.save_backup_channel(GUILD.id, 333)

    class FakeBot:
        guilds = [GUILD]

    ok, info = await B.send_full_backup(FakeBot(), GUILD, reason="test")
    note(ok, "채널 업로드 성공", str(info))
    ch = GUILD.get_channel(333)
    note(any(s.get("file") is not None for s in ch.sent),
         "파일이 첨부됨", f"{len(ch.sent)}건 전송")

    await db.save_backup_channel(GUILD.id, None)
    ok, info = await B.send_full_backup(FakeBot(), GUILD, reason="test")
    note(not ok and info == "no-channel", "채널 미설정이면 조용히 건너뜀", str(info))

    await bot.close()
    print(f"\n  실패 {len(F.FAILS)}건 / 성공 {len(F.OKS)}건")
    for x in F.FAILS:
        print("   -", x)


asyncio.run(main())
