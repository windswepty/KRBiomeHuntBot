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
}


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


def add_note(note: str) -> None:
    """기동 중 발견한 이상을 남깁니다 (헬스 응답과 알림에 실립니다)."""
    with _lock:
        if note not in _state["notes"]:
            _state["notes"].append(note)


def snapshot() -> dict:
    with _lock:
        s = dict(_state)
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
    return unhealthy_seconds() >= RESTART_AFTER_SECONDS


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

_loop_failures: dict[str, int] = {}


def loop_failure_count(name: str) -> int:
    with _lock:
        return _loop_failures.get(name, 0)


def dead_loops() -> list[str]:
    """되살리기를 포기한 주기 작업 목록."""
    with _lock:
        return [n for n, c in _loop_failures.items() if c > LOOP_RESTART_LIMIT]


def _record_loop_failure(name: str) -> int:
    with _lock:
        _loop_failures[name] = _loop_failures.get(name, 0) + 1
        return _loop_failures[name]


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
