"""로그인 도중 봇이 재시작해도 로그인이 이어지는가.

실측(2026-09-25): 재시작 **9초 뒤** 콜백이 `unknown` 으로 실패했습니다.
로그인 대기(state · PKCE verifier)가 메모리에만 있어서였습니다.
`/봇업데이트` 로 재시작이 쉬워져 더 잦아질 자리라 DB 에도 둡니다.

여기서 '재시작' 은 메모리(`_pending` · `_used`)를 비우는 것으로 흉내 냅니다.
봇 루프는 별도 스레드에서 돌고, 콜백은 메인 스레드(= Flask 스레드)에서
부릅니다 — 실제 배치와 같습니다.
"""
import sys, os, tempfile, asyncio, time, threading, json

PROJ = sys.argv[1]
sys.path.insert(0, PROJ)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(tempfile.mkdtemp())
os.environ["MONGO_URI"] = ""
os.environ.setdefault("DISCORD_TOKEN", "x")
os.environ["ROBLOX_CLIENT_ID"] = "1234567890"
os.environ["ROBLOX_CLIENT_SECRET"] = "s" * 40
os.environ["PUBLIC_BASE_URL"] = "https://example.test"

import fakemongo as FM
import fakes as F
from fakes import note
from utils import roblox_oauth as ro
import utils.verify_db as vdb
import cogs.roblox_link as RL


# ── 봇 루프(별도 스레드) ──────────────────────────────────────────────
LOOP = asyncio.new_event_loop()
threading.Thread(target=LOOP.run_forever, daemon=True).start()


def restart():
    """봇 재시작 흉내 — 메모리만 날아가고 DB 는 남습니다."""
    with ro._lock:
        ro._pending.clear()
        ro._used.clear()


def wait_saved(col, sid, timeout=3.0):
    """저장은 기다리지 않는(fire-and-forget) 경로라 반영을 기다립니다."""
    end = time.time() + timeout
    while time.time() < end:
        if col is None:
            rows = json.load(open(vdb.OAUTH_PENDING_FILE, encoding="utf-8")) \
                if os.path.exists(vdb.OAUTH_PENDING_FILE) else {}
            if sid in rows:
                return True
        elif sid in col.docs:
            return True
        time.sleep(0.01)
    return False


def wait_used(col, sid, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if col.docs.get(sid, {}).get("used"):
            return True
        time.sleep(0.01)
    return False


def scenarios(col, label):
    print(f"\n=== [{label}] 재시작을 넘기는 로그인 ===")
    ro.clear()

    # 1. 핵심: 로그인 도중 재시작
    _, sid = ro.start(1, 7)
    note(wait_saved(col, sid), f"[{label}] 발급하면 DB 에도 남음")
    restart()
    info, why = ro.take_pending_detailed(sid)
    note(why == "ok" and info and info["user_id"] == 7 and info["guild_id"] == 1,
         f"[{label}] **재시작 뒤 돌아와도 로그인이 이어짐** (예전엔 unknown)", why)
    note(bool(info and len(info["verifier"]) > 40),
         f"[{label}] PKCE verifier 도 그대로 돌아옴 (토큰 교환에 필요)")

    # 2. 꺼낸 뒤에는 verifier 를 지움
    if col is not None:
        doc = col.docs.get(sid, {})
        note("verifier" not in doc and doc.get("used") is True,
             f"[{label}] 꺼낸 뒤 DB 에서 verifier 삭제", str(sorted(doc)))
    else:
        rows = json.load(open(vdb.OAUTH_PENDING_FILE, encoding="utf-8"))
        note("verifier" not in rows[sid] and rows[sid].get("used") is True,
             f"[{label}] 꺼낸 뒤 파일에서 verifier 삭제")

    # 3. 재시작 뒤 새로고침 → used (unknown 이 아니라)
    _, why = ro.take_pending_detailed(sid)
    note(why == "used", f"[{label}] 같은 주소 다시 → used", why)
    restart()
    _, why = ro.take_pending_detailed(sid)
    note(why == "used",
         f"[{label}] 재시작을 한 번 더 넘겨도 used (원인이 흐려지지 않음)", why)

    # 4. 메모리로 꺼낸 것도 DB 에 '씀' 표시 — 안 하면 재시작 뒤 한 번 더 통과
    _, sid = ro.start(1, 8)
    wait_saved(col, sid)
    info, why = ro.take_pending_detailed(sid)
    note(why == "ok", f"[{label}] 평소(재시작 없음)에는 메모리로 처리", why)
    if col is not None:
        wait_used(col, sid)
    else:
        time.sleep(0.2)
    restart()
    info, why = ro.take_pending_detailed(sid)
    note(info is None and why == "used",
         f"[{label}] **메모리로 쓴 주소가 재시작 뒤 다시 통과하지 않음** (1회용)", why)

    # 5. 재시작 뒤 만료
    _, sid = ro.start(1, 9)
    wait_saved(col, sid)
    if col is not None:
        col.docs[sid]["expires_at"] = time.time() - 1
    else:
        rows = json.load(open(vdb.OAUTH_PENDING_FILE, encoding="utf-8"))
        rows[sid]["expires_at"] = time.time() - 1
        json.dump(rows, open(vdb.OAUTH_PENDING_FILE, "w", encoding="utf-8"))
    restart()
    info, why = ro.take_pending_detailed(sid)
    note(info is None and why == "expired",
         f"[{label}] 재시작 뒤에도 제한 시간은 지킴 (벽시계 기준)", why)

    # 6. 발급한 적 없는 것
    restart()
    _, why = ro.take_pending_detailed("발급한적없음")
    note(why == "unknown", f"[{label}] 모르는 것은 여전히 unknown", why)


def concurrency(col):
    print("\n=== 재시작 뒤 같은 주소가 동시에 여러 번 와도 한 번만 ===")
    ro.clear()
    _, sid = ro.start(1, 7)
    wait_saved(col, sid)
    restart()
    results = []
    lock = threading.Lock()

    def hit():
        r = ro.take_pending_detailed(sid)[1]
        with lock:
            results.append(r)

    ts = [threading.Thread(target=hit) for _ in range(12)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    note(results.count("ok") == 1 and results.count("used") == 11,
         "12번 동시 → ok 1 · used 11 (원자적 소비)",
         f"ok {results.count('ok')} · used {results.count('used')}")


def failures(col):
    print("\n=== 저장소가 고장 나도 로그인은 막히지 않음 ===")
    real = ro._store

    def put_boom(*a):
        raise RuntimeError("DB 끊김")

    def take_boom(*a):
        raise RuntimeError("DB 끊김")

    ro.clear()
    ro.set_store(put_boom, take_boom)
    try:
        url, sid = ro.start(1, 7)
        note("state=" in url, "저장이 터져도 로그인 버튼은 만들어짐")
        info, why = ro.take_pending_detailed(sid)
        note(why == "ok", "저장이 터져도 재시작 없으면 메모리로 로그인 성공", why)
        _, why = ro.take_pending_detailed("모름")
        note(why == "unknown", "조회가 터지면 unknown 으로 (예외가 새지 않음)", why)
    finally:
        ro.set_store(*real)

    # DB 가 느리면 콜백이 무한정 기다리지 않음
    print("")
    old_timeout = ro.STORE_TIMEOUT
    real_consume = RL.consume_oauth_pending

    async def slow(_sid):
        await asyncio.sleep(3)
        return None, "missing"

    RL.consume_oauth_pending = slow
    ro.STORE_TIMEOUT = 0.3
    try:
        t0 = time.time()
        _, why = ro.take_pending_detailed("느린조회")
        took = time.time() - t0
        note(why == "unknown" and took < 1.5,
             "DB 가 느려도 STORE_TIMEOUT 뒤 포기", f"{took:.2f}초 · {why}")
    finally:
        RL.consume_oauth_pending = real_consume
        ro.STORE_TIMEOUT = old_timeout

    # 봇 루프 스레드에서 부르면 자기 루프를 기다리다 영원히 멈춥니다
    async def on_loop():
        return ro.take_pending_detailed("루프스레드에서")[1]

    fut = asyncio.run_coroutine_threadsafe(on_loop(), LOOP)
    try:
        why = fut.result(timeout=3)
        note(why == "unknown", "봇 루프 스레드에서 불러도 멈추지 않음", why)
    except Exception as e:
        note(False, "봇 루프 스레드에서 불러도 멈추지 않음", type(e).__name__)


def callback_route():
    print("\n=== 실제 콜백 라우트: 재시작 뒤 돌아온 사람 ===")
    import keep_alive
    c = keep_alive.app.test_client()
    got, saved = [], []
    real_rec, real_ex, real_save = ro._recorder, ro.exchange, ro.save_link
    ro.set_recorder(got.append)
    ro.exchange = lambda code, verifier: (
        {"roblox_id": 42, "roblox_name": "tester", "created_at": ""}
        if verifier else None)
    ro.save_link = lambda g, u, i: (saved.append((g, u)) or (True, "ok"))
    try:
        ro.clear()
        _, sid = ro.start(5, 77)
        wait_saved(vdb.oauth_pending_col, sid)
        restart()
        r = c.get(f"/roblox/callback?state={sid}&code=y")
        note(got == ["ok"] and saved == [(5, 77)],
             "재시작 뒤 콜백 → 연동 성공 · 맞는 사람에게 저장", f"{got} {saved}")
        got.clear()
        c.get(f"/roblox/callback?state={sid}&code=y")
        note(got == ["used"], "그 뒤 새로고침 → used (이미 처리된 요청 안내)", str(got))
    finally:
        ro.set_recorder(real_rec)
        ro.exchange, ro.save_link = real_ex, real_save


def texts():
    print("\n=== 화면에 적은 제한 시간 = 실제 제한 시간 ===")
    import inspect, cogs.verify as V
    for mod, name in ((RL, "roblox_link"), (V, "verify")):
        src = inspect.getsource(mod)
        note("10분 뒤 만료" not in src and "PENDING_TTL // 60" in src,
             f"{name}: '10분' 을 박아 두지 않고 PENDING_TTL 에서 읽음")
    import discord
    embed, _ = RL._login_embed_and_view(1, 7, "t", "d", discord.Color.orange())
    note(f"{ro.PENDING_TTL // 60}분" in (embed.footer.text or ""),
         "안내 임베드 바닥글이 실제 시간", embed.footer.text or "")

    import utils.database as db
    note("oauth_pending" in db.FULL_BACKUP_EXCLUDE,
         "전체 백업에서 제외 (verifier 가 백업 파일에 실리지 않음)")


def main():
    # 봇이 기동할 때와 같은 다리를 꽂습니다
    ro.set_store(*RL._make_store(LOOP))

    # ── Mongo 경로 (가짜 드라이버) ──
    col = FM.FakeCollection("oauth_pending")
    vdb.oauth_pending_col = col
    scenarios(col, "Mongo")
    concurrency(col)
    failures(col)
    callback_route()

    # ── JSON 폴백 (Mongo 가 없을 때도 파일은 재시작을 넘깁니다) ──
    vdb.oauth_pending_col = None
    scenarios(None, "JSON")

    texts()


main()
print(f"\n  실패 {len(F.FAILS)}건 / 성공 {len(F.OKS)}건")
for x in F.FAILS:
    print("   -", x)
sys.exit(1 if F.FAILS else 0)
