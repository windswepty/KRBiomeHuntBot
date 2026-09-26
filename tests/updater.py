"""/봇업데이트 — 진짜 git 저장소 두 개(GitHub 역할 · 서버 역할)로 검증합니다.

지켜야 할 것 (틀리면 봇이 죽고, 그때는 디스코드로 되돌릴 방법도 없습니다):
  1. 새 커밋을 보여 주고, **보여 준 그 커밋까지만** 적용한다
  2. 깨진 코드(문법 · import)는 **되돌리고 재시작하지 않는다**
  3. 서버에서 직접 고친 파일 · 다른 브랜치 · 서버에만 있는 커밋 → 손대지 않는다
  4. 성공하면 종료 코드 1 로 끝낸다 (systemd 가 다시 띄움)
  5. 관리자만
"""
import sys, os, tempfile, asyncio, subprocess, textwrap

PROJ = sys.argv[1]
sys.path.insert(0, PROJ)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(tempfile.mkdtemp())
os.environ["MONGO_URI"] = ""

import fakes as F
from fakes import note, Inter
import cogs.updater as U

WORK = tempfile.mkdtemp()
ORIGIN = os.path.join(WORK, "origin.git")       # GitHub 역할
DEV = os.path.join(WORK, "dev")                  # 개발자가 push 하는 곳
SERVER = os.path.join(WORK, "server")            # 봇이 도는 서버

GIT_ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


def sh(cwd, *args):
    r = subprocess.run(["git", *args], cwd=cwd, env=GIT_ENV, capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(f"git {' '.join(args)}: {r.stdout}{r.stderr}")
    return r.stdout.strip()


def write(root, rel, text):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(textwrap.dedent(text))


def push(msg, files):
    for rel, text in files.items():
        write(DEV, rel, text)
    sh(DEV, "add", "-A")
    sh(DEV, "commit", "-qm", msg)
    sh(DEV, "push", "-q", "origin", "main")
    return sh(DEV, "rev-parse", "HEAD")


def head():
    return sh(SERVER, "rev-parse", "HEAD")


def setup_repos():
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", ORIGIN], check=True)
    subprocess.run(["git", "clone", "-q", ORIGIN, DEV], check=True, env=GIT_ENV,
                   capture_output=True)
    sh(DEV, "checkout", "-q", "-b", "main")
    push("첫 커밋", {"main.py": "print('main')\n", "keep_alive.py": "x = 1\n",
                     "requirements.txt": "# 없음\n",
                     "cogs/alpha.py": "VALUE = 1\n", "utils/helper.py": "def f():\n    return 1\n"})
    subprocess.run(["git", "clone", "-q", ORIGIN, SERVER], check=True, env=GIT_ENV,
                   capture_output=True)


restarts = []


async def main():
    setup_repos()
    U.REPO_DIR = __import__("pathlib").Path(SERVER)
    U.RESTART_DELAY = 0
    U.health.hard_exit = lambda reason: restarts.append(reason)

    print("=== 1. 새 커밋이 없으면 '이미 최신' ===")
    info = await U.check_updates()
    note(not info["blocked"] and info["total"] == 0, "최신 상태 판정", str(info["blocked"])[:60])
    e = U.build_check_embed(info)
    v = U.UpdateView(info)
    note("이미 최신" in e.title and v.apply_btn.disabled, "적용 버튼은 꺼짐")

    print("\n=== 2. 새 커밋 → 목록을 보여 주고 적용 → 재시작 ===")
    new = push("feat: 새 명령어", {"cogs/beta.py": "import utils.helper\nVALUE = 2\n"})
    old = head()
    it = Inter()
    await U.Updater.update.callback(U.Updater(None), it)
    sent = it.followup.sent[-1]
    e, view = sent["embed"], sent["view"]
    note(sent.get("ephemeral") is True and "새 커밋 1개" in e.title and "새 명령어" in e.description,
         "새 커밋과 제목을 보여 줌 (본인에게만)", e.title)
    note("cogs/beta.py" in str(e.fields[0].value), "바뀌는 파일을 보여 줌")
    note(head() == old, "보여 주기만 하고 아직 적용 안 함 (fetch 만)")

    it = Inter()
    await view.apply_btn.callback(it)
    await asyncio.sleep(0.05)
    res = (it.response.edited or {}).get("embed")
    note(head() == new, "새 커밋으로 이동", f"{head()[:8]} == {new[:8]}")
    note(res is not None and "적용 완료" in res.title, "완료를 알림", res.title if res else "")
    note(len(restarts) == 1 and new[:12] in restarts[-1], "재시작 요청 (systemd 가 다시 띄움)",
         str(restarts[-1:]))

    print("\n=== 3. ⚠️ 문법이 깨진 코드 → 되돌리고 재시작하지 않음 ===")
    good = head()
    push("fix: 실수", {"cogs/beta.py": "def broken(:\n"})
    info = await U.check_updates()
    view = U.UpdateView(info)
    it = Inter()
    n = len(restarts)
    await view.apply_btn.callback(it)
    res = (it.response.edited or {}).get("embed")
    note(head() == good, "원래 커밋으로 되돌림", head()[:8])
    note(len(restarts) == n, "재시작하지 않음 (깨진 코드로 켜지면 봇이 죽습니다)")
    note(res is not None and "업데이트하지 않았습니다" in res.title and "SyntaxError" in res.description,
         "무엇이 틀렸는지 보여 줌", res.description[:80] if res else "")
    note(not sh(SERVER, "status", "--porcelain", "--untracked-files=no"), "작업 트리도 깨끗하게 복구")

    print("\n=== 4. ⚠️ 없는 모듈을 부르는 코드 → 되돌림 ===")
    push("fix: 문법 고침, 그런데 import 가 틀림",
         {"cogs/beta.py": "import 없는_라이브러리\nVALUE = 3\n"})
    info = await U.check_updates()
    res = await U.apply_update(info["remote"])
    note(not res["ok"] and res["step"] == "import" and head() == good,
         "import 실패도 잡아서 되돌림", res["detail"][-60:])

    push("fix: 진짜로 고침", {"cogs/beta.py": "VALUE = 4\n"})

    print("\n=== 5. 보여 준 커밋까지만 (확인하는 사이 올라온 것은 안 섞음) ===")
    info = await U.check_updates()
    shown = info["remote"]
    push("feat: 관리자가 못 본 커밋", {"cogs/gamma.py": "VALUE = 5\n"})
    view = U.UpdateView(info)
    await view.apply_btn.callback(Inter())
    note(head() == shown, "보여 준 커밋까지만 적용", head()[:8])
    note(not os.path.exists(os.path.join(SERVER, "cogs/gamma.py")), "못 본 커밋은 안 들어옴")

    print("\n=== 6. 손대면 안 되는 상태 ===")
    write(SERVER, "cogs/alpha.py", "VALUE = '서버에서 직접 고침'\n")
    info = await U.check_updates()
    note("직접 고친 파일" in info["blocked"] and "cogs/alpha.py" in info["blocked"],
         "서버에서 직접 고친 파일이 있으면 거부 (그 수정이 사라지므로)")
    note(U.UpdateView(info).apply_btn.disabled, "적용 버튼 꺼짐")
    sh(SERVER, "checkout", "-q", "--", "cogs/alpha.py")

    sh(SERVER, "checkout", "-q", "-b", "실험")
    info = await U.check_updates()
    note("`실험` 브랜치" in info["blocked"], "main 이 아니면 거부", info["blocked"][:40])
    sh(SERVER, "checkout", "-q", "main")

    write(SERVER, "local.txt", "서버에서만\n")
    sh(SERVER, "add", "local.txt")
    sh(SERVER, "commit", "-qm", "서버에서 직접 커밋")
    info = await U.check_updates()
    note("서버에만 있는 커밋" in info["blocked"], "서버에만 있는 커밋이 있으면 거부")
    sh(SERVER, "reset", "-q", "--hard", "HEAD~1")

    write(SERVER, "data/points.json", "{}\n")
    info = await U.check_updates()
    note(not info["blocked"], "추적하지 않는 파일(data/ 등)은 막지 않음", info["blocked"][:40])

    print("\n=== 7. requirements.txt 가 바뀌면 설치부터 ===")
    pip_calls = []
    real_run = U.run

    async def spy(*args, **kw):
        if "pip" in args:
            pip_calls.append(args)
            return 0, "ok"
        return await real_run(*args, **kw)
    U.run = spy
    try:
        push("chore: 라이브러리", {"requirements.txt": "# 바뀜\n"})
        info = await U.check_updates()
        note(any("라이브러리 변경" in f.name for f in U.build_check_embed(info).fields),
             "적용 전에 라이브러리가 바뀐다고 알림")
        res = await U.apply_update(info["remote"])
        note(res["ok"] and pip_calls and "requirements.txt" in pip_calls[-1],
             "pip install -r requirements.txt 를 돌림")

        async def pip_fail(*args, **kw):
            if "pip" in args:
                return 1, "ERROR: externally-managed-environment"
            return await real_run(*args, **kw)
        U.run = pip_fail
        before = head()
        push("chore: 라이브러리 2", {"requirements.txt": "# 또 바뀜\n"})
        info = await U.check_updates()
        res = await U.apply_update(info["remote"])
        note(not res["ok"] and res["step"] == "pip" and head() == before,
             "설치가 실패하면 되돌림")
    finally:
        U.run = real_run

    print("\n=== 8. 관리자만 · 재시작만 ===")
    info = await U.check_updates()
    view = U.UpdateView(info)
    it = Inter(uid=8, admin=False)
    n = len(restarts)
    await view.apply_btn.callback(it)
    note("관리자만" in (it.response.sent[-1].get("content") or "") and len(restarts) == n,
         "관리자가 아니면 거부")
    it = Inter()
    await view.restart_btn.callback(it)
    await asyncio.sleep(0.05)
    note(len(restarts) == n + 1 and "재시작만" in restarts[-1], "재시작만 (코드는 그대로)")

    print("\n=== 9. git 이 비밀번호를 물어도 멈추지 않음 ===")
    rc, out = await U.run("git", "ls-remote", "https://invalid.example.invalid/x.git",
                          timeout=20)
    note(rc != 0, "실패로 끝남 (멈춰서 기다리지 않음)", f"rc={rc}")

    print("\n=== 10. 마지막 업데이트 · 재시작 시각 ===")
    # 받은 시각은 reflog 에서 읽습니다 — 진짜 git 이 남긴 값과 맞는지 봅니다
    before = int(__import__("time").time())
    await asyncio.sleep(1.1)           # 초 단위라 앞 단계와 구분되게
    push("fix: 시각 확인용", {"cogs/beta.py": "VALUE = 3\n"})
    info = await U.check_updates()
    res = await U.apply_update(info["remote"])
    note(res["ok"], "업데이트 적용", res.get("detail", "")[:60])
    t = await U.version_times()
    commit_ts = int(sh(SERVER, "log", "-1", "--format=%ct", "HEAD"))
    note(t["committed"] == commit_ts, "지금 버전을 만든 시각 = git 커밋 시각",
         f"{t['committed']} / {commit_ts}")
    note(t["applied"] is not None and t["applied"] >= before,
         "**마지막 업데이트 = 방금 받은 시각** (처음 clone 시각이 아님)",
         f"{t['applied']} ≥ {before}")
    note(t["started"] == U.PROCESS_STARTED, "마지막 재시작 = 봇이 뜬 시각")

    # 받은 뒤 재시작을 안 했으면 경고 — 옛 코드로 돌고 있다는 뜻입니다
    stale = dict(t, started=t["applied"] - 3600)
    e = U.build_check_embed({"blocked": "", "total": 0, "local": "a" * 12, "times": stale})
    names = [f.name for f in e.fields]
    note(any("재시작하지 않았습니다" in n for n in names) and e.color == U.discord.Color.orange(),
         "받고 재시작 안 함 → 주황 경고", str(names))
    fresh = dict(t, started=t["applied"] + 5)
    e = U.build_check_embed({"blocked": "", "total": 0, "local": "a" * 12, "times": fresh})
    val = e.fields[0].value if e.fields else ""
    note(not any("재시작하지 않았습니다" in f.name for f in e.fields)
         and "마지막 업데이트" in val and f"<t:{t['applied']}:f>" in val,
         "재시작까지 했으면 경고 없이 시각만", val[:60])
    e = U.build_check_embed({"blocked": "", "total": 0, "local": "a" * 12,
                             "times": {"committed": None, "applied": None, "started": 0}})
    note(not e.fields, "시각을 못 읽으면 칸을 통째로 뺌 (빈 칸 · 오류 없음)")

    # /봇업데이트 명령어가 실제로 시각을 붙이는지
    it = Inter()
    await U.Updater.update.callback(U.Updater(None), it)
    emb = it.followup.sent[-1]["embed"]
    note(any(f.name == "🕒 시각" for f in emb.fields), "/봇업데이트 화면에 시각 칸이 뜸",
         emb.title)

    print(f"\n  실패 {len(F.FAILS)}건 / 성공 {len(F.OKS)}건")
    for x in F.FAILS:
        print("   -", x)


asyncio.run(main())
sys.exit(1 if F.FAILS else 0)
