"""전체 백업 · 복원 도구 (설정 · 포인트 · 연동 · 로그 전부).

`/백업설정` 의 **스냅샷은 포인트만** 담습니다. 상점 구성 · 인증 설정 ·
링크 보호 설정 · 로블록스 연동은 들어가지 않아, 설정이 날아가면 손으로 다시
맞춰야 합니다. 이 도구는 DB 전체를 파일 하나로 뜨고 되돌립니다.

사용법
------
    python tools/full_backup.py dump
    python tools/full_backup.py verify [파일]
    python tools/full_backup.py restore <파일> --only verify_settings --yes
    python tools/full_backup.py restore <파일> --all --yes

복원은 기본이 **덮어쓰기(upsert)** 입니다. 파일에 있는 문서만 덮어쓰고,
그 뒤에 새로 생긴 문서는 건드리지 않습니다. 파일 시점으로 **정확히** 되돌리려면
`--wipe` 를 붙이세요 (그 사이 쌓인 것이 사라지므로 기본값이 아닙니다).

어떤 복원이든 실행 직전에 현재 상태를 자동으로 한 벌 떠 둡니다.
복원 자체를 되돌릴 수 있어야 하기 때문입니다.

⚠️ 이 파일이 만드는 백업에는 유저 ID · 포인트 · 워터마크가 들어갑니다.
   `backups/` 는 .gitignore 에 있습니다. 커밋하지 마세요.
"""

import os
import sys
import json
import glob
import gzip
import datetime

from bson import ObjectId
from dotenv import load_dotenv
from pymongo import MongoClient, ReplaceOne

load_dotenv()

DB_NAME = "krbiomehuntbot"
BACKUP_DIR = "backups"
CHUNK = 500        # 한 번에 밀어 넣을 문서 수 (M0 에서 안전한 크기)
KST = datetime.timezone(datetime.timedelta(hours=9))


# ── 직렬화 ────────────────────────────────────────────────────────────
# ObjectId 와 datetime 은 JSON 이 모르는 타입입니다. 문자열로 뭉개면
# 복원할 때 타입이 달라져 TTL 인덱스(BSON Date 필요)가 동작하지 않습니다.
# 그래서 타입을 보존하는 형태로 감쌉니다.

def _encode(o):
    if isinstance(o, ObjectId):
        return {"$oid": str(o)}
    if isinstance(o, datetime.datetime):
        return {"$date": o.isoformat()}
    raise TypeError(f"직렬화할 수 없는 타입: {type(o)}")


def _decode(o):
    if isinstance(o, dict):
        if set(o) == {"$oid"}:
            return ObjectId(o["$oid"])
        if set(o) == {"$date"}:
            return datetime.datetime.fromisoformat(o["$date"])
        return {k: _decode(v) for k, v in o.items()}
    if isinstance(o, list):
        return [_decode(v) for v in o]
    return o


def _connect():
    uri = (os.getenv("MONGO_URI") or "").strip()
    if not uri:
        sys.exit("MONGO_URI 가 없습니다. .env 를 확인하세요.")
    return MongoClient(uri, serverSelectionTimeoutMS=20000)[DB_NAME]


def _now():
    return datetime.datetime.now(KST)


# ── 덤프 ──────────────────────────────────────────────────────────────

def dump(tag: str = "full") -> str:
    db = _connect()
    os.makedirs(BACKUP_DIR, exist_ok=True)
    path = os.path.join(BACKUP_DIR, f"{tag}_{_now():%Y%m%d_%H%M%S}.json")

    data, counts = {}, {}
    for name in sorted(db.list_collection_names()):
        docs = list(db[name].find())
        data[name] = docs
        counts[name] = len(docs)

    payload = {
        "_meta": {
            "database": DB_NAME,
            "created_kst": _now().strftime("%Y-%m-%d %H:%M:%S"),
            "collections": counts,
            "note": "전체 덤프 — 유저 ID·포인트 포함. 커밋 금지.",
        },
        "data": data,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, default=_encode, indent=1)

    total = sum(counts.values())
    size = os.path.getsize(path) / 1024 / 1024
    for n, c in counts.items():
        print(f"  {n:22} {c:6,}건")
    print(f"\n  총 {total:,}건 · {size:.2f} MB")
    print(f"  저장: {path}")
    return path


# ── 검증 ──────────────────────────────────────────────────────────────

def _latest() -> str:
    """가장 최근 전체 백업. 이름순이 아니라 수정 시각으로 고릅니다.

    `backups/` 에는 `/백업설정` 이 내려준 포인트 전용 파일(`users-*.json`)도
    섞여 있습니다. 형식이 달라 여기서 읽으면 안 되므로 우리 파일만 봅니다.
    """
    # ⚠️ `.json.gz` 를 빠뜨리면 **실제 백업을 하나도 못 찾습니다.**
    # 8MB 를 넘으면 압축되는데 실 DB 는 이미 40MB 대라 매일 .gz 로 올라옵니다.
    files = [f for pat in ("full_*.json", "full_*.json.gz",
                           "pre-restore_*.json", "pre-restore_*.json.gz")
             for f in glob.glob(os.path.join(BACKUP_DIR, pat))]
    if not files:
        sys.exit(f"{BACKUP_DIR}/ 에 전체 백업 파일이 없습니다. "
                 "먼저 `python tools/full_backup.py dump` 를 실행하세요.")
    return max(files, key=os.path.getmtime)


def _load(path: str) -> dict:
    """백업 파일을 읽고 우리 형식이 맞는지 확인합니다.

    **gzip 을 반드시 함께 처리해야 합니다.** 8MB 를 넘으면 압축해서 올리는데
    실 DB 는 이미 40MB 대라 채널에 올라오는 것은 **전부 `.json.gz`** 입니다.
    여기서 못 읽으면 백업은 있는데 되돌릴 수 없는 상태가 됩니다.
    """
    try:
        with open(path, "rb") as f:
            head = f.read(2)
            f.seek(0)
            blob = gzip.decompress(f.read()) if head == b"\x1f\x8b" else f.read()
        payload = json.loads(blob.decode("utf-8"))
    except (OSError, ValueError, EOFError) as e:
        # UnicodeDecodeError · JSONDecodeError 는 ValueError 하위라 여기 걸립니다.
        # 예전에는 둘 다 빠져나가 생짜 트레이스백이 났습니다.
        sys.exit(f"파일을 읽을 수 없습니다: {path}\n  {type(e).__name__}: {e}")

    excluded = (payload.get("_meta") or {}).get("excluded") or [] \
        if isinstance(payload, dict) else []
    if excluded:
        print(f"  ℹ️ 이 백업에서 일부러 뺀 컬렉션: {', '.join(excluded)}")
        print("     (7일이면 사라지는 임시 기록이라 복원 대상이 아닙니다)")

    if not isinstance(payload, dict) or "data" not in payload:
        hint = ""
        # 포인트 전용 파일의 실제 키는 `users` 입니다. 예전에는 `documents` 를
        # 보고 있어서 **이 안내가 한 번도 뜨지 않았습니다.**
        if isinstance(payload, dict) and ("users" in payload or "taken_at_kst" in payload):
            hint = ("\n  이 파일은 `/백업설정` 의 **포인트 전용** 백업입니다."
                    "\n  복원은 디스코드에서 `/백업설정` -> 복원 으로 하세요.")
        sys.exit(f"전체 백업 형식이 아닙니다: {path}{hint}")
    return payload


def verify(path: str | None = None) -> bool:
    path = path or _latest()
    data = _load(path)["data"]
    db = _connect()

    print(f"  파일: {path}\n")
    bad = 0
    for name in sorted(data):
        live = db[name].count_documents({})
        saved = len(data[name])
        if live != saved:
            bad += 1
        print(f"  {'OK ' if live == saved else 'DIFF'} {name:22} "
              f"파일 {saved:6,} / DB {live:6,}")

    if "users" in data:
        pf = sum(d.get("points", 0) for d in data["users"])
        agg = list(db.users.aggregate([{"$group": {"_id": None, "s": {"$sum": "$points"}}}]))
        pl = agg[0]["s"] if agg else 0
        print(f"\n  총 포인트  파일 {pf:,} / DB {pl:,}"
              f"  {'일치' if pf == pl else '불일치'}")

    print("\n  " + ("전 컬렉션 일치" if bad == 0 else f"{bad}개 컬렉션 불일치"))
    print("  -- 파일이 더 적으면 백업 이후 데이터가 쌓인 것이며 정상입니다.")
    return bad == 0


# ── 복원 ──────────────────────────────────────────────────────────────

def restore(path: str, only: list[str] | None, wipe: bool, yes: bool) -> None:
    payload = _load(path)
    data = payload["data"]
    meta = payload.get("_meta", {})

    targets = only if only else sorted(data)
    unknown = [t for t in targets if t not in data]
    if unknown:
        sys.exit(f"파일에 없는 컬렉션: {', '.join(unknown)}")

    print(f"  파일 시점 : {meta.get('created_kst', '?')}")
    print(f"  대상      : {', '.join(targets)}")
    print(f"  방식      : {'전체 교체 (--wipe: 이후 데이터 삭제됨)' if wipe else '덮어쓰기 (이후 데이터 유지)'}")
    for t in targets:
        print(f"    - {t:22} {len(data[t]):6,}건")

    if not yes:
        sys.exit("\n  실제로 실행하려면 --yes 를 붙이세요. (아무것도 바꾸지 않았습니다)")

    # 복원 자체를 되돌릴 수 있어야 합니다. 먼저 현재 상태를 떠 둡니다.
    print("\n  복원 전 현재 상태를 먼저 백업합니다...")
    dump(tag="pre-restore")

    db = _connect()
    print("\n  복원 중...")
    for name in targets:
        docs = [_decode(d) for d in data[name]]
        col = db[name]

        # 봇이 살아 있으면 복원 중에도 문서를 새로 만듭니다. 예전에는
        # delete_many 뒤에 insert_many 를 했는데, 그 사이에 봇이 끼어들어
        # _id 가 겹치면 **중간에서 통째로 실패**했습니다(E11000).
        # 그래서 항상 upsert 로 씁니다. 몇 번을 다시 돌려도 같은 결과가 되고,
        # 동시에 들어온 쓰기와도 부딪히지 않습니다.
        for i in range(0, len(docs), CHUNK):
            part = docs[i:i + CHUNK]
            if part:
                col.bulk_write([ReplaceOne({"_id": d["_id"]}, d, upsert=True)
                                for d in part], ordered=False)

        # --wipe 는 '파일 시점으로 정확히' 를 뜻합니다. 위에서 파일 내용을
        # 먼저 올린 뒤, 파일에 없는 문서만 지웁니다. 순서를 이렇게 해야
        # 중간에 실패해도 데이터가 빈 채로 남지 않습니다.
        removed = 0
        if wipe:
            keep = [d["_id"] for d in docs]
            removed = col.delete_many({"_id": {"$nin": keep}}).deleted_count

        extra = f"  (파일에 없는 {removed:,}건 삭제)" if removed else ""
        print(f"    {name:22} {len(docs):6,}건 복원{extra}")

    print("\n  완료. 봇을 재시작해 캐시를 비우세요 (Render: Manual Deploy).")


# ── 진입점 ────────────────────────────────────────────────────────────

def main() -> None:
    args = sys.argv[1:]
    if not args or args[0] not in ("dump", "verify", "restore"):
        print(__doc__)
        sys.exit(0)

    cmd = args[0]
    rest = args[1:]

    if cmd == "dump":
        dump()
    elif cmd == "verify":
        path = next((a for a in rest if not a.startswith("-")), None)
        sys.exit(0 if verify(path) else 1)
    else:
        path = next((a for a in rest if not a.startswith("-")), None)
        if not path:
            sys.exit("복원할 파일을 지정하세요.")
        only = None
        if "--only" in rest:
            i = rest.index("--only")
            if i + 1 >= len(rest):
                sys.exit("--only 뒤에 컬렉션 이름을 쉼표로 구분해 적으세요.")
            only = [x.strip() for x in rest[i + 1].split(",") if x.strip()]
        elif "--all" not in rest:
            sys.exit("--only <컬렉션> 또는 --all 중 하나를 지정하세요.")
        restore(path, only, wipe="--wipe" in rest, yes="--yes" in rest)


if __name__ == "__main__":
    main()
