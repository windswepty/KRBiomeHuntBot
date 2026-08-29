"""DB 계정 권한 점검 (계정을 바꾸기 전에 확인하는 용도).

`atlasAdmin` 한 계정으로 여러 봇이 물려 있으면, 연결 문자열이 한 번 새는 순간
전부 삭제될 수 있습니다. 봇마다 자기 DB에만 `readWrite` 를 가진 계정으로
갈아타야 하는데, **갈아타기 전에** 그 계정으로 봇이 정상 동작할 수 있는지
확인하지 않으면 배포 후에야 알게 됩니다.

이 도구는 `.env` 의 `MONGO_URI` 로 접속해 다음을 확인합니다.

  1. 접속되는가
  2. 권한이 무엇인가 (atlasAdmin 이면 경고)
  3. 봇이 쓰는 컬렉션을 읽을 수 있는가
  4. **쓸 수 있는가** — 임시 컬렉션에 넣었다 지웁니다 (실 데이터 무관)
  5. **인덱스를 만들 수 있는가** — 봇이 기동할 때마다 TTL 인덱스를 만듭니다
  6. 다른 DB 가 안 보이는가 (범위가 좁혀졌다는 증거)

사용법
------
    python tools/check_db_user.py

비밀번호는 출력하지 않습니다.
"""

import os
import sys
import datetime

from dotenv import load_dotenv
from pymongo import MongoClient, ASCENDING
from pymongo.errors import OperationFailure, PyMongoError

load_dotenv()

DB_NAME = "krbiomehuntbot"
PROBE = "_permcheck"          # 점검 전용. 실 데이터와 무관합니다.

# 봇이 실제로 읽고 쓰는 컬렉션 (없으면 아직 안 만들어진 것일 수 있습니다)
EXPECTED = ["users", "verify_settings", "linkguard_settings",
            "shop_items", "gacha_settings", "roblox_links", "point_snapshots"]

ok_all = True


def line(ok: bool, label: str, detail: str = "") -> None:
    global ok_all
    if not ok:
        ok_all = False
    print(f"  {'OK  ' if ok else 'FAIL'} {label}{('  ' + detail) if detail else ''}")


def main() -> None:
    uri = (os.getenv("MONGO_URI") or "").strip()
    if not uri:
        sys.exit("MONGO_URI 가 없습니다. .env 를 확인하세요.")

    # 1) 접속
    try:
        client = MongoClient(uri, serverSelectionTimeoutMS=20000)
        client.admin.command("ping")
        line(True, "접속")
    except PyMongoError as e:
        sys.exit(f"  FAIL 접속 실패: {type(e).__name__}: {e}\n"
                 "  -> 아이디/비밀번호, 그리고 Network Access 의 IP 허용을 확인하세요.\n"
                 "     비밀번호에 @ : / ? 가 있으면 URL 인코딩이 필요합니다.")

    # 2) 권한
    try:
        info = client.admin.command("connectionStatus")["authInfo"]
        users = [u.get("user") for u in info.get("authenticatedUsers", [])]
        roles = [(r.get("role"), r.get("db")) for r in info.get("authenticatedUserRoles", [])]
        print(f"\n  계정: {', '.join(users) or '?'}")
        for r, d in roles:
            print(f"  권한: {r} @ {d}")

        admin_like = [r for r, _ in roles if r in ("atlasAdmin", "root", "dbOwner", "userAdminAnyDatabase")]
        scoped = [(r, d) for r, d in roles if r in ("readWrite", "read") and d == DB_NAME]
        if admin_like:
            line(False, "권한 범위",
                 f"'{admin_like[0]}' 는 너무 넓습니다 — 유출 시 전체 삭제 가능")
        elif scoped:
            line(True, "권한 범위", f"readWrite @ {DB_NAME} 로 좁혀짐")
        else:
            line(False, "권한 범위", f"{DB_NAME} 에 대한 readWrite 가 안 보입니다")
    except PyMongoError as e:
        line(False, "권한 확인", f"{type(e).__name__}: {e}")

    db = client[DB_NAME]

    # 3) 읽기
    print()
    try:
        names = set(db.list_collection_names())
        missing = [c for c in EXPECTED if c not in names]
        line(True, "읽기 (컬렉션 목록)", f"{len(names)}개")
        if missing:
            print(f"       참고: 아직 없는 컬렉션 {', '.join(missing)}")
        n = db.users.count_documents({})
        agg = list(db.users.aggregate([{"$group": {"_id": None, "s": {"$sum": "$points"}}}]))
        pts = agg[0]["s"] if agg else 0
        line(True, "포인트 조회", f"{n:,}명 / {pts:,} P")
    except PyMongoError as e:
        line(False, "읽기", f"{type(e).__name__}: {e}")

    # 4) 쓰기 — 전용 임시 컬렉션에만 손댑니다
    try:
        db[PROBE].insert_one({"_id": "probe", "at": datetime.datetime.now(datetime.timezone.utc)})
        got = db[PROBE].find_one({"_id": "probe"})
        db[PROBE].delete_one({"_id": "probe"})
        line(got is not None, "쓰기 (임시 컬렉션에 넣었다 지움)")
    except PyMongoError as e:
        line(False, "쓰기", f"{type(e).__name__}: {e}")

    # 5) 인덱스 생성 — 봇이 기동할 때마다 TTL 인덱스를 만듭니다.
    #    이게 막히면 봇이 부팅에서 걸립니다.
    try:
        db[PROBE].create_index([("at", ASCENDING)], name="probe_idx",
                               expireAfterSeconds=60)
        db[PROBE].drop_index("probe_idx")
        line(True, "TTL 인덱스 생성/삭제")
    except PyMongoError as e:
        line(False, "TTL 인덱스 생성", f"{type(e).__name__}: {e}")

    try:
        db.drop_collection(PROBE)
    except PyMongoError:
        pass

    # 6) 범위 확인 — 다른 DB 가 보이면 아직 넓은 계정입니다
    print()
    try:
        others = [d for d in client.list_database_names()
                  if d not in (DB_NAME, "admin", "local", "config")]
        if others:
            line(False, "다른 DB 접근", f"아직 보입니다: {', '.join(others)}")
        else:
            line(True, "다른 DB 접근", "차단됨 (이 DB 전용 계정)")
    except OperationFailure:
        line(True, "다른 DB 접근", "차단됨 (목록 조회 권한 없음 — 정상)")
    except PyMongoError as e:
        line(True, "다른 DB 접근", f"조회 불가 ({type(e).__name__}) — 정상으로 봅니다")

    client.close()

    print()
    if ok_all:
        print("  전부 통과 — 이 계정으로 Render 의 MONGO_URI 를 바꿔도 됩니다.")
    else:
        print("  실패 항목이 있습니다. Render 를 바꾸지 마세요.")
        print("  (위 FAIL 줄을 그대로 알려주시면 원인을 짚어드립니다)")
    sys.exit(0 if ok_all else 1)


if __name__ == "__main__":
    main()
