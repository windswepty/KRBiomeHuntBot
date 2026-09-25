"""디스코드가 잠깐 5xx 를 돌려줄 때 보호가 살아남는지.

실측(2026-09-18~25, 7일): 원본 삭제 실패 22건 · 재게시 실패 34건. 전부 503.
discord.py 2.7.1 은 `{500, 502, 504, 524}` 만 스스로 재시도하므로 **503 은
그대로 올라옵니다.**

실패했을 때의 결과가 둘이 서로 다릅니다.

  · 삭제 실패   → 원문 링크가 **채널에 그대로 남습니다** (보호 실패)
  · 재게시 실패 → 원본은 이미 지워진 뒤라 **바이옴이 통째로 사라집니다**

그래서 여기서 지켜야 할 것:

  1. 일시적 실패(5xx · 연결 끊김)는 **다시 시도**한다.
  2. 권한·없음(4xx)은 **즉시 포기**한다 — 되지 않을 일을 반복하면
     이벤트 루프만 잡아먹고 결과는 같다.
  3. 끝내 실패하면 **무슨 일이 벌어졌는지** 로그에 적는다.
"""
import sys, os, tempfile, asyncio

PROJ = sys.argv[1]
sys.path.insert(0, PROJ)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(tempfile.mkdtemp())
os.environ["MONGO_URI"] = ""

import discord
import fakes as F
from fakes import note


def http_error(status):
    """discord.HTTPException 을 상태코드와 함께 만듭니다."""
    resp = type("R", (), {"status": status, "reason": "test"})()
    return discord.HTTPException(resp, f"{status} test")


async def main():
    import cogs.link_guard as LG

    # 기다리지 않습니다 — 백오프를 실제로 자면 스위트가 5초씩 늘어납니다.
    slept = []
    real_sleep = asyncio.sleep

    async def fake_sleep(s):
        slept.append(s)
        await real_sleep(0)

    LG.asyncio.sleep = fake_sleep

    print("=== 1. 일시적 실패는 다시 시도해야 ===")
    for status in (500, 502, 503, 504, 524):
        tries = [0]

        async def flaky(st=status, t=tries):
            t[0] += 1
            if t[0] < 2:
                raise http_error(st)
            return f"ok-{st}"

        slept.clear()
        got = await LG._retry_transient(flaky)
        note(got == f"ok-{status}", f"{status}: 한 번 실패 후 성공", f"{tries[0]}회 시도")

    print("")
    print("  연결이 끊겨도 (Errno 104 등) 다시 시도합니다")
    tries = [0]

    async def reset(t=tries):
        t[0] += 1
        if t[0] < 3:
            raise OSError(104, "Connection reset by peer")
        return "ok"

    note(await LG._retry_transient(reset) == "ok", "연결 끊김 후 성공",
         f"{tries[0]}회 시도")

    print("")
    print("  계속 실패하면 마지막 예외를 올립니다 (조용히 성공하지 않음)")
    # ⚠️ 앞 구간의 대기가 남아 있으면 아래 검사가 엉뚱한 목록을 봅니다.
    slept.clear()
    tries = [0]

    async def always(t=tries):
        t[0] += 1
        raise http_error(503)

    try:
        await LG._retry_transient(always)
        note(False, "계속 실패하면 예외가 올라와야 함")
    except discord.HTTPException:
        note(True, "마지막 예외가 올라옴")
    note(tries[0] == len(LG.TRANSIENT_BACKOFF) + 1,
         "정해진 횟수만큼만 시도", f"{tries[0]}회 / 한도 {len(LG.TRANSIENT_BACKOFF) + 1}")
    note(len(slept) == len(LG.TRANSIENT_BACKOFF),
         "시도 사이에만 기다림 (마지막엔 안 기다림)", str(slept))
    note(list(slept) == sorted(slept), "기다리는 시간이 점점 늘어남", str(slept))

    print("")
    print("=== 2. ⚠️ 권한·없음은 즉시 포기해야 ===")
    # 되지 않을 일을 세 번 더 하면 이벤트 루프만 잡아먹습니다.
    for name, exc in (("Forbidden", discord.Forbidden),
                      ("NotFound", discord.NotFound)):
        tries = [0]

        async def denied(t=tries, E=exc):
            t[0] += 1
            resp = type("R", (), {"status": 403 if E is discord.Forbidden else 404,
                                  "reason": "test"})()
            raise E(resp, "test")

        try:
            await LG._retry_transient(denied)
            note(False, f"{name} 은 올라와야 함")
        except exc:
            note(True, f"{name} 이 그대로 올라옴")
        note(tries[0] == 1, f"{name}: **다시 시도하지 않음**", f"{tries[0]}회")

    print("")
    print("  4xx (예: 400 잘못된 요청) 도 다시 시도하지 않습니다")
    tries = [0]

    async def bad(t=tries):
        t[0] += 1
        raise http_error(400)

    try:
        await LG._retry_transient(bad)
        note(False, "400 은 올라와야 함")
    except discord.HTTPException:
        note(True, "400 이 그대로 올라옴")
    note(tries[0] == 1, "400: **다시 시도하지 않음**", f"{tries[0]}회")

    print("")
    print("=== 3. 처음부터 성공하면 기다리지 않아야 ===")
    slept.clear()

    async def fine():
        return "바로 성공"

    note(await LG._retry_transient(fine) == "바로 성공", "성공값을 그대로 돌려줌")
    note(not slept, "성공했으면 한 번도 안 기다림", str(slept))

    print(f"\n  실패 {len(F.FAILS)}건 / 성공 {len(F.OKS)}건")
    for x in F.FAILS:
        print("   -", x)


asyncio.run(main())
