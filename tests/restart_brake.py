"""크래시 루프 제동 장치가 실제로 걸리는가.

세 가지가 조용히 죽어 있었습니다.
① 재시작 이력이 메모리에만 있어 os._exit 를 못 넘김 → 제동이 무효
② watchdog 의 '억제 중' 분기가 도달 불가능 → 억제에 흔적이 없음
③ revive_loop 실패 카운터가 줄지 않아 장기 가동 시 루프가 영구 포기
"""
import sys, os, tempfile, time, importlib

PROJ = sys.argv[1]
sys.path.insert(0, PROJ)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
WORK = tempfile.mkdtemp()
os.chdir(WORK)
os.environ["MONGO_URI"] = ""

import fakes as F
from fakes import note
import utils.health as H


def unhealthy(secs):
    """끊긴 지 secs 초인 상태를 만듭니다."""
    H._state["ready"] = True
    H._state["connected"] = False
    H._state["booted_at"] = time.monotonic() - secs - H.STARTUP_GRACE_SECONDS - 10
    H._state["last_ok"] = time.monotonic() - secs


print("=== ① 재시작 이력이 프로세스를 넘어가는가 ===")
H._state["rate_limited_at"] = None
for _ in range(3):
    H.record_restart()
note(os.path.exists(H.RESTART_LOG_FILE), "디스크에 기록됨", H.RESTART_LOG_FILE)
note(H.recent_restart_count() == 3, "3회로 셈", str(H.recent_restart_count()))

print("\n  프로세스가 죽었다 살아난 상황 (메모리 초기화)")
H._state["restart_times"] = []          # os._exit 로 메모리가 날아간 상태
importlib.reload  # noqa — 파일만 보면 되므로 재로드는 불필요
note(H.recent_restart_count() == 3, "이력이 살아남음 (예전엔 0이었음)",
     str(H.recent_restart_count()))
note(H.restart_suppressed(), "제동 장치가 걸림 (3회 이상)")

print("\n=== ② 억제가 걸리면 이유가 밖으로 나오는가 ===")
unhealthy(H.RESTART_AFTER_SECONDS + 10)
go, reason = H.restart_decision()
note(not go, "재시작 보류")
note("재시작" in reason and "회" in reason, "이유가 구체적", reason)

print("\n  레이트 리밋 억제")
os.remove(H.RESTART_LOG_FILE)
H.mark_rate_limited()
unhealthy(H.RESTART_AFTER_SECONDS + 10)
go, reason = H.restart_decision()
note(not go, "재시작 보류")
note("레이트 리밋" in reason, "이유가 레이트 리밋임을 밝힘", reason)

print("\n  도배하지 않는가")
H._last_suppress_log[0] = 0.0
H.log_suppression(reason)
first = H._last_suppress_log[0]
H.log_suppression(reason)
note(H._last_suppress_log[0] == first, "5분에 한 번만 남김")

print("\n=== 정상이면 재시작하는가 ===")
H._state["rate_limited_at"] = None
unhealthy(H.RESTART_AFTER_SECONDS + 10)
go, reason = H.restart_decision()
note(go, "재시작 결정", reason)
note("끊겨" in reason, "이유가 연결 끊김")

print("\n  아직 유예 안에 있으면 재시작 안 함")
unhealthy(10)
go, reason = H.restart_decision()
note(not go and reason == "", "조용히 넘어감")

print("\n=== ③ 루프 실패 카운터가 시간이 지나면 잊히는가 ===")
H._loop_failures.clear()
for _ in range(H.LOOP_RESTART_LIMIT + 1):
    H._record_loop_failure("flaky")
note("flaky" in H.dead_loops(), "연달아 실패하면 포기함", str(H.dead_loops()))

print("\n  한 시간 전 실패는 잊혀야 합니다 (몇 달 도는 봇)")
old = time.monotonic() - H.LOOP_FAILURE_WINDOW - 60
H._loop_failures["flaky"] = [old] * 10
note(H.loop_failure_count("flaky") == 0, "옛 실패는 안 셈",
     str(H.loop_failure_count("flaky")))
note("flaky" not in H.dead_loops(), "포기 목록에서 빠짐", str(H.dead_loops()))
note(H._record_loop_failure("flaky") == 1, "다시 1회부터 셈")

print("\n  누적이 아니라 최근 1시간 기준인가")
H._loop_failures["flaky"] = [old] * 5 + [time.monotonic()] * 2
note(H.loop_failure_count("flaky") == 2, "최근 것만 셈",
     str(H.loop_failure_count("flaky")))

print(f"\n  실패 {len(F.FAILS)}건 / 성공 {len(F.OKS)}건")
for x in F.FAILS:
    print("   -", x)
