"""health · keep_alive 를 실제 상태 이름으로 정확히 시험합니다."""
import sys, os, tempfile, time

PROJ = sys.argv[1]
sys.path.insert(0, PROJ)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(tempfile.mkdtemp())
os.environ["MONGO_URI"] = ""

import fakes as F
from fakes import note

import utils.health as H
import keep_alive

c = keep_alive.app.test_client()


def codes():
    return c.get("/").status_code, c.get("/health").status_code


print("=== 기동 직후 (유예 180초 안) ===")
note(H.unhealthy_seconds() == 0, "이상 시간 0")
note(not H.should_restart(), "재시작 안 함")
print("   응답:", codes())

print("\n=== 정상 연결 ===")
H.mark_ready(1, "mongo")
H.mark_connected(45.0)
note(codes() == (200, 200), "/ 와 /health 둘 다 200", str(codes()))
note(H.is_healthy(), "정상 판정")

print("\n=== 좀비 상태 (연결 끊김 + 유예 초과) ===")
H._state["booted_at"] = time.monotonic() - 10_000
H._state["last_ok"] = time.monotonic() - 10_000
H.mark_disconnected()
secs = H.unhealthy_seconds()
# / 와 /health 는 일부러 다릅니다. / 는 로블록스 OAuth 심사자가 직접 눌러 보는
# Entry Link 라 항상 200 이어야 하고, 좀비 판정은 /health 가 담당합니다.
note(secs > H.RESTART_AFTER_SECONDS, "이상 시간 누적", f"{secs:.0f}초")
note(codes() == (200, 503), "/ 는 200(소개 페이지) · /health 는 503(감시)", str(codes()))
note(H.should_restart(), "재시작 판정 True")

print("\n=== 로그인조차 못한 상태 ===")
H._state.update({"ready": False, "connected": False,
                 "booted_at": time.monotonic() - 10_000, "last_ok": None})
note(H.should_restart(), "토큰/네트워크 문제도 재시작 판정",
     f"{H.unhealthy_seconds():.0f}초")
note(codes() == (200, 503), "/health 만 503", str(codes()))

print("\n=== 회복 (실제 경로: on_ready 이후 끊겼다 재연결) ===")
H.mark_ready(1, "mongo")
H._state["booted_at"] = time.monotonic() - 10_000
H._state["last_ok"] = time.monotonic() - 10_000
H.mark_disconnected()
note(codes() == (200, 503) and H.should_restart(), "끊긴 동안 /health 503")
H.mark_resumed()
note(codes() == (200, 200), "재연결되면 다시 200", str(codes()))
note(not H.should_restart(), "재시작 취소")

print("\n=== 기능 개수 (분모) ===")
H.set_extensions(12, 12, [])
note(H.extensions_ok(), "12/12 정상")
H.set_extensions(11, 12, ["quest"])
note(not H.extensions_ok(), "11/12 는 이상으로 판정")
s = H.snapshot()
note(s["ext_loaded"] == 11 and s["ext_total"] == 12,
     "스냅샷에 분자·분모 모두 실림", f"{s['ext_loaded']}/{s['ext_total']}")

print("\n=== HEAD 는 1회용 토큰을 소비하지 않아야 함 ===")
for rule in keep_alive.app.url_map.iter_rules():
    p = str(rule)
    if "<" not in p or "static" in p:
        continue
    r = c.head(p.replace("<token>", "AAAABBBBCCCC"))
    note(r.status_code == 204, f"HEAD {p}", str(r.status_code))

print(f"\n  실패 {len(F.FAILS)}건 / 성공 {len(F.OKS)}건")
for x in F.FAILS:
    print("   -", x)
