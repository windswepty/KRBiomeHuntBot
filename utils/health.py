"""봇 생존 상태 (무인 운영용).

관리자가 오래 자리를 비울 수 있다는 전제로 만들었습니다. 사람이 알아채고
손을 쓰는 것을 기대할 수 없으므로, **스스로 이상을 감지하고 스스로 재시작**
해야 합니다.

가장 위험한 것은 죽는 게 아니라 **좀비 상태**입니다.
Flask 는 살아서 200 을 돌려주는데 디스코드 연결만 끊긴 경우, 외부 감시
서비스(UptimeRobot 등)는 정상으로 판정합니다. 아무도 모르는 채로 봇만
멈춰 있습니다. 그래서 헬스 응답이 **디스코드 연결 상태를 반영**해야 합니다.

  ├ 봇 스레드가 상태를 갱신     mark_ready() / mark_disconnected() / touch()
  ├ Flask 스레드가 상태를 읽음  snapshot()  ->  /health 가 200 또는 503
  └ 감시 루프가 판정            unhealthy_seconds()  ->  일정 시간 넘으면 재시작

두 스레드가 함께 만지므로 모든 접근을 Lock 으로 감쌉니다.
"""

import os
import time
import threading

# 기동 직후에는 아직 연결 전이므로 잠시 봐 줍니다. 이 시간이 너무 짧으면
# 부팅 중에 감시 서비스가 장애로 오인해 알림이 쏟아집니다.
STARTUP_GRACE_SECONDS = 180

# 이 시간 넘게 끊겨 있으면 스스로 재시작합니다. discord.py 가 알아서
# 재접속하므로, 그걸로도 안 되는 상황만 여기까지 옵니다.
RESTART_AFTER_SECONDS = 300

# ── 레이트 리밋 대응 ─────────────────────────────────────────────────
#
# 디스코드 전역 레이트 리밋(429)에 걸리면, 재시작은 차단을 연장할 뿐입니다.
# 로그인 + sync 가 무효 요청으로 집계돼 CloudFlare 차단이 더 길어지기
# 때문입니다. **기다리는 것이 유일한 해법**이므로, 429 를 관측하면 잠시
# 재시작을 억제합니다. 단, 영원히 억제하면 진짜 좀비를 못 잡으므로 상한
# 을 둡니다.

# 429 관측 후 최대 이 시간(초) 동안 watchdog 재시작을 억제합니다
RATE_LIMIT_SUPPRESS_SECONDS = 1800   # 30분

# 짧은 시간에 반복 재시작하는 것도 막습니다
RESTART_COOLDOWN_WINDOW = 1800       # 최근 30분
RESTART_COOLDOWN_LIMIT = 3           # 이 횟수 이상이면 대기 시간 늘림
RESTART_COOLDOWN_EXTRA = 300         # 추가 대기 5분

_lock = threading.Lock()
_state = {
    "booted_at": time.monotonic(),
    "ready": False,          # 한 번이라도 로그인에 성공했는가
    "connected": False,      # 지금 게이트웨이에 붙어 있는가
    "last_ok": None,         # 마지막으로 정상이었던 시각(monotonic)
    "guilds": 0,
    "latency_ms": None,
    "storage": "unknown",
    "disconnects": 0,        # 누적 끊김 횟수 (재시작 판단 아닌 참고용)
    "resumes": 0,
    "notes": [],             # 기동 시 발견한 이상 (콕 로드 실패 등)
    # 기능이 몇 개 중 몇 개 살아있는지. 분모가 없으면 하나가 빠져도
    # 알아챌 수 없습니다. cogs/ 폴더의 파일 수가 '있어야 할 개수'입니다.
    "ext_loaded": 0,
    "ext_total": 0,
    "ext_failed": [],
    "commands": 0,
    "rate_limited_at": None,      # 마지막 429 관측 시각 (monotonic)
    "restart_times": [],          # 최근 재시작 시각들 (monotonic)
    # 링크 보호가 디스코드 일시 오류(5xx)를 만난 결과. 서버 로그에만 남으면
    # SSH 를 못 쓰는 사람은 재시도가 효과가 있는지 볼 수 없습니다.
    "guard_counts": {},
}


# ── 지수 백오프 ──────────────────────────────────────────────────────
#
# 주기 작업(status_refresh, goal_refresh)이 429 를 받으면 다음 주기에
# 똑같이 다시 시도합니다. 차단을 연장시키는 행동이므로, 한동안 시도
# 자체를 멈춰야 합니다. 성공하면 즉시 초기화됩니다.

class Backoff:
    """지수 백오프. 429 를 만나면 점점 더 오래 기다립니다."""

    __slots__ = ("name", "initial", "max_delay", "delay", "_until")

    def __init__(self, name: str, initial: float = 300,
                 max_delay: float = 1800):
        self.name = name
        self.initial = initial     # 첫 백오프: 5분
        self.max_delay = max_delay # 상한: 30분
        self.delay = 0.0
        self._until = 0.0         # monotonic

    def hit(self) -> None:
        """429 를 받았을 때 호출. 다음 대기 시간을 늘립니다."""
        self.delay = min((self.delay * 2) or self.initial, self.max_delay)
        self._until = time.monotonic() + self.delay

    def active(self) -> bool:
        """아직 백오프 중인가. True 면 요청을 **보내지 않아야** 합니다."""
        return time.monotonic() < self._until

    def reset(self) -> None:
        """성공 시 호출. 백오프를 초기화합니다."""
        self.delay = 0.0
        self._until = 0.0

    def remaining(self) -> float:
        """남은 백오프 시간(초). 0 이면 백오프가 아닙니다."""
        return max(0.0, self._until - time.monotonic())


# 주기 작업별 백오프 인스턴스. cog 에서 import 해서 씁니다.
status_backoff = Backoff("status_refresh")
goal_backoff = Backoff("goal_refresh")


def set_extensions(loaded: int, total: int, failed: list[str]) -> None:
    with _lock:
        _state["ext_loaded"] = loaded
        _state["ext_total"] = total
        _state["ext_failed"] = list(failed)


def set_command_count(n: int) -> None:
    with _lock:
        _state["commands"] = n


def extensions_ok() -> bool:
    with _lock:
        return _state["ext_total"] > 0 and _state["ext_loaded"] == _state["ext_total"]


def mark_ready(guilds: int, storage: str) -> None:
    with _lock:
        _state["ready"] = True
        _state["connected"] = True
        _state["last_ok"] = time.monotonic()
        _state["guilds"] = guilds
        _state["storage"] = storage


def mark_connected(latency_ms: float | None = None) -> None:
    """게이트웨이가 살아 있음을 기록합니다."""
    with _lock:
        _state["connected"] = True
        _state["last_ok"] = time.monotonic()
        if latency_ms is not None:
            _state["latency_ms"] = round(latency_ms, 1)


def mark_disconnected() -> None:
    with _lock:
        if _state["connected"]:
            _state["disconnects"] += 1
        _state["connected"] = False


def mark_resumed() -> None:
    with _lock:
        _state["resumes"] += 1
        _state["connected"] = True
        _state["last_ok"] = time.monotonic()


# ── 레이트 리밋 추적 ─────────────────────────────────────────────────

def mark_rate_limited() -> None:
    """429 전역 레이트 리밋을 관측했을 때 호출합니다.

    이 시각을 기준으로 watchdog 재시작을 일정 시간 억제합니다.
    429 응답 자체가 무효 요청으로 집계되므로, 차단 중에 재시작하면
    차단이 연장됩니다. 기다리는 것이 유일한 해법입니다.
    """
    with _lock:
        _state["rate_limited_at"] = time.monotonic()
    print("[Health] 전역 레이트 리밋(429) 관측 — "
          f"{RATE_LIMIT_SUPPRESS_SECONDS}초간 재시작 억제", flush=True)


def rate_limited_recently() -> bool:
    """최근에 429 를 관측했는가. True 면 재시작을 미뤄야 합니다."""
    with _lock:
        at = _state["rate_limited_at"]
    if at is None:
        return False
    return (time.monotonic() - at) < RATE_LIMIT_SUPPRESS_SECONDS


# 재시작 이력 파일. **메모리에 두면 안 됩니다** — 바로 다음 줄에서
# os._exit 로 프로세스가 죽으므로, 다음 기동은 이력이 비어 있는 상태로
# 시작합니다. 그러면 "최근 3회 이상이면 더 기다린다"는 제동 장치가
# 영원히 작동하지 않습니다. 크래시 루프에 브레이크가 없다는 뜻입니다.
RESTART_LOG_FILE = os.path.join("data", ".restart_times")


def _read_restart_times() -> list[float]:
    """최근 재시작 시각(벽시계). monotonic 은 재시작을 넘기지 못합니다."""
    try:
        with open(RESTART_LOG_FILE, "r") as f:
            raw = [float(x) for x in f.read().split() if x.strip()]
    except Exception:
        return []
    cutoff = time.time() - RESTART_COOLDOWN_WINDOW
    return [t for t in raw if t > cutoff]


def record_restart() -> None:
    """watchdog 이 재시작을 결정했을 때 호출. 이력을 남깁니다.

    곧바로 프로세스가 죽으므로 **디스크에** 적습니다.
    """
    times = _read_restart_times() + [time.time()]
    with _lock:
        _state["restart_times"] = list(times)
    try:
        os.makedirs(os.path.dirname(RESTART_LOG_FILE) or ".", exist_ok=True)
        with open(RESTART_LOG_FILE, "w") as f:
            f.write("\n".join(f"{t:.0f}" for t in times[-20:]))
    except Exception as e:
        # 조용히 넘기면 제동 장치가 없는 줄도 모릅니다
        print(f"*** 재시작 이력 저장 실패 ({RESTART_LOG_FILE}): "
              f"{type(e).__name__}: {e} — 크래시 루프 제동이 동작하지 "
              f"않습니다 ***", flush=True)


def restart_suppressed() -> bool:
    """최근 30분 내 재시작이 너무 잦으면 True.

    True 면 다음 재시작 전 추가 대기(RESTART_COOLDOWN_EXTRA)를 적용합니다.
    """
    return len(_read_restart_times()) >= RESTART_COOLDOWN_LIMIT


def recent_restart_count() -> int:
    """최근 30분 내 재시작 횟수. 상태 임베드에 보여 주기 위한 것입니다."""
    return len(_read_restart_times())


def backoff_status() -> dict | None:
    """상태 임베드에 백오프 정보를 표시하기 위한 요약.

    백오프 중인 작업이 없으면 None.
    """
    items = []
    for bo in (status_backoff, goal_backoff):
        r = bo.remaining()
        if r > 0:
            items.append(f"`{bo.name}` {r:.0f}초 남음")
    if not items:
        return None
    return {"remaining": max(status_backoff.remaining(),
                             goal_backoff.remaining()),
            "details": items}


def add_note(note: str) -> None:
    """기동 중 발견한 이상을 남깁니다 (헬스 응답과 알림에 실립니다)."""
    with _lock:
        if note not in _state["notes"]:
            _state["notes"].append(note)


def count_guard(kind: str) -> None:
    """링크 보호의 재시도 결과를 셉니다 (이번 가동 중 누적).

    `retry_saved` 재시도로 살림 · `delete_failed` 원문이 남음 ·
    `send_failed` 재게시 실패로 알림이 사라짐
    """
    with _lock:
        c = _state["guard_counts"]
        c[kind] = c.get(kind, 0) + 1


def snapshot() -> dict:
    with _lock:
        s = dict(_state)
        # 얕은 복사로는 안쪽 dict 를 다른 스레드와 같이 쥐게 됩니다.
        s["guard_counts"] = dict(_state["guard_counts"])
    s["uptime_seconds"] = round(time.monotonic() - s["booted_at"])
    s["notes"] = list(s["notes"])
    return s


def unhealthy_seconds() -> float:
    """정상이 아닌 상태가 몇 초째인지. 정상이면 0.

    기동 유예 시간 안에서는 항상 0 을 돌려줍니다.
    """
    with _lock:
        booted, ready = _state["booted_at"], _state["ready"]
        connected, last_ok = _state["connected"], _state["last_ok"]

    now = time.monotonic()
    if now - booted < STARTUP_GRACE_SECONDS:
        return 0.0
    if not ready:
        # 유예 시간이 지났는데 로그인조차 못 했습니다 (토큰·네트워크 문제)
        return now - booted
    if connected:
        return 0.0
    return now - (last_ok or booted)


def is_healthy() -> bool:
    return unhealthy_seconds() == 0.0


def should_restart() -> bool:
    """재시작이 필요한가.

    레이트 리밋(429) 관측 직후에는 재시작해도 차단만 연장하므로
    **기다립니다**. 단, 영원히 억제하면 진짜 좀비를 못 잡으므로
    RATE_LIMIT_SUPPRESS_SECONDS 가 지나면 원래대로 재시작합니다.

    짧은 시간에 반복 재시작하는 것도 막습니다. 최근 30분 내 3회 이상
    재시작했다면 추가 대기(5분)를 적용합니다.
    """
    return restart_decision()[0]


def restart_decision() -> tuple[bool, str]:
    """(재시작할까, 이유). **이유를 함께 돌려주는 것이 핵심입니다.**

    예전에는 watchdog 이 `should_restart()` 로 물어본 뒤 그 안에서 이미
    False 가 된 억제 조건을 다시 검사했습니다. 그 분기는 도달할 수 없어
    **억제가 걸려도 로그에 한 줄도 남지 않았습니다.** 무인 운영에서는
    "억제가 동작하고 있다"를 확인할 방법이 사라진다는 뜻입니다.
    """
    secs = unhealthy_seconds()
    if secs < RESTART_AFTER_SECONDS:
        return False, ""
    # 429 관측 후 30분 이내면 재시작 억제 (기다리는 게 유일한 해법)
    if rate_limited_recently():
        return False, (f"레이트 리밋 억제 중 — 재시작 보류 "
                       f"(끊김 {secs:.0f}초)")
    # 최근 30분 내 3회 이상 재시작했으면 추가 대기
    if restart_suppressed() and secs < RESTART_AFTER_SECONDS + RESTART_COOLDOWN_EXTRA:
        return False, (f"최근 30분 내 재시작 {recent_restart_count()}회 — "
                       f"추가 대기 중 (끊김 {secs:.0f}초)")
    return True, f"디스코드 연결이 {secs:.0f}초째 끊겨 있습니다"


_last_suppress_log = [0.0]


def log_suppression(reason: str, every: float = 300.0) -> None:
    """억제 사유를 남기되 60초마다 도배하지 않습니다."""
    now = time.monotonic()
    if now - _last_suppress_log[0] < every:
        return
    _last_suppress_log[0] = now
    print(f"[Watchdog] {reason}", flush=True)


def hard_exit(reason: str) -> None:
    """프로세스를 즉시 끝냅니다. 호스팅이 새 프로세스로 다시 띄웁니다.

    sys.exit() 는 예외라서 이벤트 루프나 남의 except 절에 먹힐 수 있습니다.
    여기서는 **확실히 죽는 것**이 목적이라 os._exit 를 씁니다.
    종료 코드를 0 이 아닌 값으로 둬야 호스팅이 '실패'로 보고 재시작합니다.
    """
    print(f"*** 자동 재시작: {reason} ***", flush=True)
    import sys
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(1)


# ── 주기 작업 감시 ───────────────────────────────────────────────────
#
# discord.py 의 tasks.loop 은 **예상 못한 예외가 한 번 나면 영구히 멈춥니다.**
# 재시도도, 알림도 없습니다. 프로세스는 멀쩡하니 호스팅도 감지하지 못하고,
# 무인 운영에서는 몇 달 뒤에야 발견됩니다 — 그동안 자동 백업이 한 번도
# 돌지 않았다는 뜻입니다. 그래서 직접 되살리되, 같은 곳에서 계속 터지면
# 포기하고 상태에 남겨 임베드에 보이게 합니다.

LOOP_RESTART_LIMIT = 5
LOOP_RESTART_DELAY = 10.0
# 실패는 **이 시간 안의 것만** 셉니다. 누적으로 세면 몇 달 도는 봇에서
# 몇 주에 한 번씩 나는 일시적 네트워크 오류만으로도 결국 상한에 닿아
# 주기 작업이 영구히 멈춥니다. 무인 운영에서는 그게 조용한 죽음입니다.
LOOP_FAILURE_WINDOW = 3600.0

_loop_failures: dict[str, list[float]] = {}


def _recent_failures(name: str) -> list[float]:
    cutoff = time.monotonic() - LOOP_FAILURE_WINDOW
    return [t for t in _loop_failures.get(name, []) if t > cutoff]


def loop_failure_count(name: str) -> int:
    with _lock:
        return len(_recent_failures(name))


def dead_loops() -> list[str]:
    """되살리기를 포기한 주기 작업 목록."""
    with _lock:
        return [n for n in _loop_failures
                if len(_recent_failures(n)) > LOOP_RESTART_LIMIT]


def _record_loop_failure(name: str) -> int:
    with _lock:
        times = _recent_failures(name) + [time.monotonic()]
        _loop_failures[name] = times
        return len(times)


async def revive_loop(task_loop, name: str, error: BaseException,
                      delay: float = LOOP_RESTART_DELAY) -> None:
    """예외로 멈춘 tasks.loop 을 되살립니다. `@루프.error` 에서 부릅니다.

    지금은 죽어가는 태스크 **안**이라 곧바로 start() 를 부를 수 없습니다
    ("이미 실행 중"으로 거부됩니다). 그래서 별도 태스크로 잠시 뒤에 켭니다.
    restart() 는 이미 끝난 태스크에는 아무 일도 하지 않으므로 쓰지 않습니다.
    """
    import asyncio
    import traceback

    print(f"[Loop] 주기 작업 '{name}' 이 예외로 멈췄습니다: "
          f"{type(error).__name__}: {error}", flush=True)
    traceback.print_exception(type(error), error, error.__traceback__)

    n = _record_loop_failure(name)
    if n > LOOP_RESTART_LIMIT:
        add_note(f"주기 작업 `{name}` 이 {n}회 실패해 멈춰 있습니다")
        print(f"[Loop] '{name}' {n}회째 실패 — 되살리기를 포기합니다. "
              "상태 임베드를 확인하세요.", flush=True)
        return

    async def _again():
        await asyncio.sleep(delay)
        if task_loop.is_running():
            return
        try:
            task_loop.start()
            print(f"[Loop] '{name}' 재시작 완료 ({n}/{LOOP_RESTART_LIMIT})",
                  flush=True)
        except Exception as e:
            add_note(f"주기 작업 `{name}` 재시작 실패")
            print(f"[Loop] '{name}' 재시작 실패: {type(e).__name__}: {e}",
                  flush=True)

    asyncio.create_task(_again())
