"""검증 전체 실행기.

    python tests/run_all.py              전부 실행
    python tests/run_all.py rules        이름에 rules 가 들어간 것만
    python tests/run_all.py --cov        커버리지까지 (coverage 설치 필요)

**실 DB 를 건드리지 않습니다.** 모든 스위트가 `MONGO_URI=` 를 비우고
임시 디렉터리에서 돕니다. Mongo 경로를 봐야 하는 것들은 컬렉션을
가짜 드라이버로 갈아끼웁니다(`fakemongo.py`).

Windows 콘솔은 cp949 라 한글 출력에서 죽습니다. 실행기가 자식 프로세스에
`PYTHONIOENCODING=utf-8` 을 넣어 주므로 신경 쓰지 않아도 됩니다.
"""
import os
import subprocess
import sys
import time

# Windows 콘솔은 cp949 라 한글을 그대로 찍으면 깨지거나 죽습니다.
# 자식 프로세스에는 환경변수로 넣어 주지만, 이 파일 자신의 출력도 고쳐야 합니다.
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except (AttributeError, OSError):
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(HERE)

# 대역(fakes.py, fakemongo.py)은 스위트가 아니라 도구입니다.
NOT_SUITES = {"run_all.py", "fakes.py", "fakemongo.py"}

# 순서에 의미는 없지만, 빠른 것부터 두면 깨진 곳을 일찍 봅니다.
ORDER = [
    "rules",                # 프로젝트 규칙 (정적 검사, 제일 빠름)
    "fingerprint", "version_sync", "backoff", "restart_brake", "health_only",
    "healthendpoint", "errhandler", "ttl_fields", "legal_pages",
    "cmds", "events", "ui_all", "modals",
    "mongo_path", "linkflow", "linkguard_debounce",
    "report_flow", "report_decor", "ticket_guard",
    "adminonly", "goal_adminonly", "backup_roundtrip",
    "roblox_flow", "panels_roster",
]


def discover():
    found = [f[:-3] for f in sorted(os.listdir(HERE))
             if f.endswith(".py") and f not in NOT_SUITES]
    ordered = [n for n in ORDER if n in found]
    return ordered + [n for n in found if n not in ORDER]


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    use_cov = "--cov" in sys.argv

    suites = discover()
    if args:
        suites = [s for s in suites if any(a in s for a in args)]
        if not suites:
            print(f"'{' '.join(args)}' 에 맞는 스위트가 없습니다.")
            return 1

    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["MONGO_URI"] = ""                 # 실 DB 를 절대 건드리지 않습니다
    env.setdefault("DISCORD_TOKEN", "x")

    base = [sys.executable]
    if use_cov:
        base += ["-m", "coverage", "run", "--branch", "--parallel-mode",
                 "--source", "cogs,utils,keep_alive,main"]

    ok = fail = 0
    broken = []
    started = time.time()
    print(f"검증 {len(suites)}개 실행")
    print("-" * 58)

    for name in suites:
        out = subprocess.run(base + [os.path.join(HERE, name + ".py"), PROJ],
                             capture_output=True, text=True,
                             encoding="utf-8", errors="replace", env=env)
        text = out.stdout + out.stderr
        line = next((l for l in reversed(text.split("\n"))
                     if "실패 " in l and "성공 " in l), "")
        if not line:
            # 요약이 없으면 스위트 자체가 깨진 것입니다. 조용히 넘기면
            # '전부 통과'로 보이는데 실제로는 아무것도 검증하지 못한 것입니다.
            print(f"  {name:<20} 실행 실패")
            for l in text.strip().split("\n")[-4:]:
                print("      " + l)
            broken.append(name)
            continue

        nums = [int(t) for t in line.replace("건", " ").replace("/", " ").split()
                if t.isdigit()]
        f, o = (nums + [0, 0])[:2]
        ok += o
        fail += f
        # 폴백으로 새면 Mongo 경로를 한 줄도 검증하지 못한 것입니다
        leak = text.count("using local file")
        mark = "OK  " if f == 0 else "FAIL"
        extra = f"   폴백누출 {leak}" if leak else ""
        print(f"  {mark} {name:<20} 성공 {o:>3}  실패 {f}{extra}")
        if f:
            for l in text.split("\n"):
                if l.strip().startswith("- "):
                    print("        " + l.strip())

    print("-" * 58)
    print(f"성공 {ok} · 실패 {fail} · 깨진 스위트 {len(broken)} "
          f"({time.time() - started:.0f}초)")

    if use_cov:
        subprocess.run([sys.executable, "-m", "coverage", "combine"], cwd=PROJ)
        subprocess.run([sys.executable, "-m", "coverage", "report",
                        "--sort=cover"], cwd=PROJ)

    return 1 if (fail or broken) else 0


if __name__ == "__main__":
    sys.exit(main())
