"""오류 핸들러가 '응답할 수 없는 오류'에 API 를 또 쓰지 않는가.

실 로그에서 오류 14건이 전부 두 줄씩 남았습니다 —
본 오류 1건 + '오류 메시지 전송 실패' 1건. 뒤엣것도 400/404 라
디스코드가 무효 요청으로 집계합니다. 몰릴 때 스스로 차단을 당기는 피드백입니다.
"""
import sys, os, tempfile, importlib.util, asyncio

PROJ = sys.argv[1]
sys.path.insert(0, PROJ)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(tempfile.mkdtemp())
os.environ["MONGO_URI"] = ""
os.environ.setdefault("DISCORD_TOKEN", "x")

import discord
from discord import app_commands
import fakes as F
from fakes import note

spec = importlib.util.spec_from_file_location("mainmod", os.path.join(PROJ, "main.py"))
mainmod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mainmod)
handler = mainmod.on_app_command_error


class Res:
    status = 400
    reason = "Bad Request"


def http(code, status=400):
    Res.status = status
    return discord.HTTPException(Res(), {"code": code, "message": "x"})


def notfound(code):
    r = type("R", (), {"status": 404, "reason": "Not Found"})()
    return discord.NotFound(r, {"code": code, "message": "Unknown interaction"})


class CountingResp(F.Resp):
    """API 호출 횟수를 셉니다. raises 를 주면 그 예외를 냅니다."""
    def __init__(self, raises=None, done=False):
        super().__init__()
        self.calls = 0
        self.raises = raises
        self.done = done

    async def send_message(self, *a, **k):
        self.calls += 1
        if self.raises:
            raise self.raises
        self.done = True

    def is_done(self):
        return self.done


class CountingFollow(F.Follow):
    def __init__(self, raises=None):
        super().__init__()
        self.calls = 0
        self.raises = raises

    async def send(self, *a, **k):
        self.calls += 1
        if self.raises:
            raise self.raises
        return None


def make(resp, follow, cmdname="퀘스트"):
    it = F.Inter()
    it.response = resp
    it.followup = follow
    it.command = type("C", (), {"name": cmdname})()
    return it


async def main():
    print("=== 응답 불가 오류: API 를 한 번도 쓰지 않아야 합니다 ===")
    for label, exc in [
        ("10062 Unknown interaction (3초 초과)", notfound(10062)),
        ("40060 already acknowledged (재전송)", http(40060)),
    ]:
        r, f = CountingResp(), CountingFollow()
        it = make(r, f)
        err = app_commands.CommandInvokeError(
            type("C", (), {"name": "퀘스트", "qualified_name": "퀘스트"})(), exc)
        await handler(it, err)
        note(r.calls == 0 and f.calls == 0, label,
             f"API 호출 {r.calls + f.calls}회")

    print("\n=== 진짜 오류: 유저에게 안내는 계속 나가야 합니다 ===")
    r, f = CountingResp(), CountingFollow()
    it = make(r, f)
    err = app_commands.CommandInvokeError(
        type("C", (), {"name": "퀘스트", "qualified_name": "퀘스트"})(),
        ValueError("진짜 버그"))
    await handler(it, err)
    note(r.calls == 1, "일반 예외 → 안내 1회", f"{r.calls}회")
    note(r.done, "인터랙션이 응답을 받음 (무한 로딩 방지)")

    r, f = CountingResp(done=True), CountingFollow()
    it = make(r, f)
    await handler(it, err)
    note(f.calls == 1 and r.calls == 0, "이미 defer 됨 → followup 으로", f"followup {f.calls}회")

    print("\n=== 권한 오류 문구는 그대로 ===")
    r, f = CountingResp(), CountingFollow()
    it = make(r, f)
    await handler(it, app_commands.errors.MissingPermissions(["administrator"]))
    note(r.calls == 1, "권한 오류 → 안내 1회")

    print("\n=== is_done() 이 거짓말할 때(원격은 이미 응답됨) ===")
    # 로컬 플래그는 False 인데 디스코드는 40060 을 돌려주는 상황.
    # 예전 코드는 여기서 그냥 실패하고 끝났습니다 — 유저는 무한 로딩.
    r, f = CountingResp(raises=http(40060)), CountingFollow()
    it = make(r, f)
    await handler(it, err)
    note(f.calls == 1, "40060 → followup 으로 한 번 더 시도", f"followup {f.calls}회")
    note(r.calls == 1, "response 재시도는 하지 않음", f"response {r.calls}회")

    print("\n=== 그 재시도마저 실패해도 무한 루프가 없어야 ===")
    r, f = CountingResp(raises=http(40060)), CountingFollow(raises=http(40060))
    it = make(r, f)
    await handler(it, err)
    note(r.calls + f.calls == 2, "총 2회에서 멈춤", f"{r.calls + f.calls}회")

    print("\n=== 실 로그 14건 재현: 예전 vs 지금 ===")
    total = 0
    for exc in [notfound(10062)] * 9 + [http(40060)] * 3:
        r, f = CountingResp(), CountingFollow()
        it = make(r, f)
        await handler(it, app_commands.CommandInvokeError(
            type("C", (), {"name": "퀘스트", "qualified_name": "퀘스트"})(), exc))
        total += r.calls + f.calls
    note(total == 0, "실제 발생 12건 → 낭비 요청 0회", f"예전 12회 → 지금 {total}회")

asyncio.run(main())
print(f"\n  실패 {len(F.FAILS)}건 / 성공 {len(F.OKS)}건")
for x in F.FAILS:
    print("   -", x)
sys.exit(1 if F.FAILS else 0)
