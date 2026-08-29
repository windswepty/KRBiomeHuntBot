"""클러스터 이전 (옛 Atlas -> 새 Atlas).

계정을 바꾸는 것과 클러스터를 옮기는 것은 다른 일입니다. 클러스터를 옮기면
데이터가 따라오지 않으므로, 옛 쪽을 통째로 읽어 새 쪽에 넣어야 합니다.

`.env` 에서 두 곳을 읽습니다.

    MONGO_URI_OLD   원본 (옛 클러스터)   — 읽기만 합니다
    MONGO_URI       대상 (새 클러스터)   — 여기에 씁니다

사용법
------
    python tools/migrate_cluster.py            # 계획만 출력 (아무것도 안 바꿈)
    python tools/migrate_cluster.py --yes      # 실제 이전
    python tools/migrate_cluster.py --verify   # 이전 결과를 문서 단위로 대조

**옛 클러스터는 읽기만 합니다.** 실패하더라도 원본은 그대로이므로,
새 쪽만 지우고 다시 시도하면 됩니다. 옛 클러스터를 먼저 지우지 마세요.

이전 중에도 봇은 옛 클러스터에 쓰고 있습니다(아직 Render 를 안 바꿨다면).
그 사이 들어온 변경은 따라오지 않으므로, **이전 직후 바로 Render 를 바꾸고**
가능하면 사람이 적은 시간에 하세요.
"""

import os
import sys
import datetime

from dotenv import load_dotenv
from pymongo import MongoClient
from pymongo.errors import PyMongoError

load_dotenv()

DB_NAME = "krbiomehuntbot"
CHUNK = 500          # 한 번에 밀어 넣을 문서 수 (M0 에서 안전한 크기)


def _client(var: str):
    uri = (os.getenv(var) or "").strip()
    if not uri:
        sys.exit(f"{var} 가 .env 에 없습니다.")
    try:
        c = MongoClient(uri, serverSelectionTimeoutMS=20000)
        c.admin.command("ping")
        return c
    except PyMongoError as e:
        sys.exit(f"{var} 접속 실패: {type(e).__name__}: {e}")


def _host(c) -> str:
    try:
        return sorted(s.address[0] for s in c.topology_description.server_descriptions().values())[0]
    except Exception:
        return "?"


def _points(db) -> int:
    agg = list(db.users.aggregate([{"$group": {"_id": None, "s": {"$sum": "$points"}}}]))
    return agg[0]["s"] if agg else 0


def main() -> None:
    yes = "--yes" in sys.argv
    verify_only = "--verify" in sys.argv

    src_c = _client("MONGO_URI_OLD")
    dst_c = _client("MONGO_URI")
    src, dst = src_c[DB_NAME], dst_c[DB_NAME]

    print(f"  원본: {_host(src_c)}")
    print(f"  대상: {_host(dst_c)}")
    if _host(src_c) == _host(dst_c):
        sys.exit("  원본과 대상이 같은 클러스터입니다. .env 를 확인하세요.")

    names = sorted(src.list_collection_names())

    # ── 대조만 ────────────────────────────────────────────────────────
    if verify_only:
        print("\n  === 문서 단위 대조 ===")
        same = diff = missing = 0
        for n in names:
            s_docs = {d["_id"]: d for d in src[n].find()}
            d_docs = {d["_id"]: d for d in dst[n].find()}
            ok = sum(1 for i, d in s_docs.items() if d_docs.get(i) == d)
            miss = len(set(s_docs) - set(d_docs))
            bad = len(s_docs) - ok - miss
            same += ok
            missing += miss
            diff += bad
            mark = "OK  " if (miss == 0 and bad == 0) else "DIFF"
            print(f"  {mark} {n:22} 일치 {ok:5,} / 누락 {miss:4,} / 불일치 {bad:4,}")
        ps, pd = _points(src), _points(dst)
        print(f"\n  포인트  원본 {ps:,} P / 대상 {pd:,} P  "
              f"{'일치' if ps == pd else f'차이 {pd - ps:+,}'}")
        print(f"  문서    일치 {same:,} / 누락 {missing:,} / 불일치 {diff:,}")
        print("\n  " + ("이전 완료 — 대상이 원본과 같습니다."
                        if missing == 0 and diff == 0 else
                        "차이가 있습니다. 누락이 있으면 다시 이전하세요."))
        sys.exit(0 if (missing == 0 and diff == 0) else 1)

    # ── 계획 ──────────────────────────────────────────────────────────
    print(f"\n  옮길 컬렉션 {len(names)}개")
    total = 0
    for n in names:
        c = src[n].count_documents({})
        total += c
        exists = dst[n].count_documents({})
        warn = f"  <- 대상에 이미 {exists:,}건 있음 (덮어씀)" if exists else ""
        print(f"    {n:22} {c:6,}건{warn}")
    print(f"\n  총 {total:,}건 · 포인트 {_points(src):,} P")

    if not yes:
        print("\n  실제로 옮기려면 --yes 를 붙이세요. (아무것도 바꾸지 않았습니다)")
        sys.exit(0)

    # ── 실행 ──────────────────────────────────────────────────────────
    print("\n  이전 중...")
    started = datetime.datetime.now()
    moved = 0
    for n in names:
        docs = list(src[n].find())
        if dst[n].count_documents({}):
            dst[n].delete_many({})          # 재실행해도 중복이 생기지 않게
        for i in range(0, len(docs), CHUNK):
            part = docs[i:i + CHUNK]
            if part:
                dst[n].insert_many(part, ordered=False)
        moved += len(docs)
        print(f"    {n:22} {len(docs):6,}건")

    took = (datetime.datetime.now() - started).total_seconds()
    print(f"\n  {moved:,}건 이전 완료 ({took:.1f}초)")
    print(f"  포인트  원본 {_points(src):,} P -> 대상 {_points(dst):,} P")
    print("\n  다음: python tools/migrate_cluster.py --verify 로 대조하세요.")
    print("  인덱스는 봇이 기동할 때 다시 만듭니다.")

    src_c.close()
    dst_c.close()


if __name__ == "__main__":
    main()
