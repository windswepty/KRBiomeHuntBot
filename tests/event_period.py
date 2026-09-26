"""이벤트 기간 판정 — 한 자리 월·일로 적어도 맞게 판정하는가.

실제 사고(2026-09-26): 관리자가 `2026-9-21 ~ 2026-9-30` 으로 만든 이벤트가
9월 26일에 **'🕒 기간 밖'** 으로 떴습니다. 날짜를 글자로 비교해서
`"2026-09-26" < "2026-9-21"` ('0' < '9') 이 되었기 때문입니다.
표시만 틀린 게 아니라 **진행도가 한 번도 쌓이지 않았습니다**
(`active_events` 가 같은 판정을 씁니다).

기존 검증은 날짜를 전부 두 자리(`2026-09-15`)로만 넣어서 이 길을 한 번도
밟지 않았습니다. 여기서는 사람이 실제로 치는 모양으로 넣습니다.
"""
import sys, os, tempfile, asyncio, datetime

PROJ = sys.argv[1]
sys.path.insert(0, PROJ)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(tempfile.mkdtemp())
os.environ["MONGO_URI"] = ""

import discord
import fakes as F
from fakes import Inter, GUILD, note, drain
import utils.database as db
import cogs.quest as Q

KST = db.KST


def at(y, m, d):
    return datetime.datetime(y, m, d, 12, 0, tzinfo=KST)


def short(d: datetime.date) -> str:
    """사람이 치는 모양 — 앞의 0 을 뺍니다."""
    return f"{d.year}-{d.month}-{d.day}"


def test_screenshot():
    print("=== 스크린샷 그대로 (2026-09-26 기준) ===")
    today = at(2026, 9, 26)
    for s, e in (("2026-9-21", "2026-9-30"), ("2026-9-15", "2026-9-30")):
        ev = {"starts": s, "ends": e, "enabled": True}
        note(db.event_is_active(ev, today),
             f"{s} ~ {e} → **진행 중** (예전엔 기간 밖)")


def test_boundaries():
    print("\n=== 양끝 포함 · 바깥은 제외 ===")
    ev = {"starts": "2026-9-5", "ends": "2026-9-9"}
    cases = [((2026, 9, 4), False, "시작 전날"), ((2026, 9, 5), True, "시작일"),
             ((2026, 9, 9), True, "마지막 날"), ((2026, 9, 10), False, "끝난 다음 날")]
    for ymd, want, label in cases:
        got = db.event_is_active(ev, at(*ymd))
        note(got == want, f"{label} → {'진행' if want else '밖'}", str(got))

    ev = {"starts": "2026-9-30", "ends": "2026-10-5"}
    note(db.event_is_active(ev, at(2026, 10, 2)), "달을 넘기는 기간 (9-30 ~ 10-5)")
    ev = {"starts": "2026-12-28", "ends": "2027-1-3"}
    note(db.event_is_active(ev, at(2027, 1, 1)), "해를 넘기는 기간 (12-28 ~ 1-3)")

    print("\n=== 이상한 값 ===")
    note(not db.event_is_active({"starts": "2026-9-1", "ends": "언제까지"}, at(2026, 9, 5)),
         "끝을 못 읽으면 기간 밖 (보상이 영원히 열려 있지 않게)")
    note(not db.event_is_active({"starts": "abc", "ends": "2026-9-30"}, at(2026, 9, 5)),
         "시작을 못 읽으면 기간 밖")
    note(db.event_is_active({}, at(2026, 9, 5)), "기간이 아예 없으면 진행 (예전 동작 유지)")
    note(not db.event_is_active({"starts": "2026-9-1", "ends": "2026-9-30",
                                 "enabled": False}, at(2026, 9, 5)),
         "꺼 두면 기간 안이어도 밖")
    note(db.event_is_active({"starts": " 2026-9-1 ", "ends": "2026-9-30"}, at(2026, 9, 5)),
         "앞뒤 공백은 무시")


def test_exhaustive():
    print("\n=== 전수 비교: 한 자리로 적어도 두 자리와 결과가 같은가 ===")
    # 9월 초 ~ 11월 중순: 한 자리 월(9) · 두 자리 월(10, 11) · 한 자리 일이
    # 모두 섞이는 구간입니다.
    base = datetime.date(2026, 8, 28)
    days = [base + datetime.timedelta(n) for n in range(80)]
    bad, total = [], 0
    for i, s in enumerate(days[::3]):
        for e in days[i * 3::3]:
            padded = {"starts": s.isoformat(), "ends": e.isoformat()}
            loose = {"starts": short(s), "ends": short(e)}
            for t in days:
                when = at(t.year, t.month, t.day)
                total += 1
                want = s <= t <= e
                if db.event_is_active(padded, when) != want or \
                        db.event_is_active(loose, when) != want:
                    bad.append((short(s), short(e), t.isoformat()))
    note(not bad, f"{total:,}개 조합 전부 실제 날짜 비교와 일치",
         f"어긋남 {len(bad)}건 예: {bad[:2]}")


async def fire(values):
    it = Inter()
    modal = Q.EventBasicModal()
    for name, val in values.items():
        getattr(modal, name)._value = val
    await modal.on_submit(it)
    await drain()
    return it


async def test_modal():
    print("\n=== 이벤트 만들기 모달 ===")
    it = await fire(dict(name="달넘김", period="2026-9-30 ~ 2026-10-5", mode="모두"))
    evs = {e["name"]: e for e in await db.list_events(GUILD.id)}
    ev = evs.get("달넘김")
    note(ev is not None, "`2026-9-30 ~ 2026-10-5` 저장됨 (예전엔 '끝이 더 빠름' 으로 거부)",
         F.outcome(it))
    note(bool(ev) and (ev["starts"], ev["ends"]) == ("2026-09-30", "2026-10-05"),
         "두 자리로 정리해 저장", str(ev and (ev["starts"], ev["ends"])))

    await fire(dict(name="스샷", period="2026-9-21~2026-9-30", mode="모두"))
    ev = {e["name"]: e for e in await db.list_events(GUILD.id)}.get("스샷")
    note(bool(ev) and (ev["starts"], ev["ends"]) == ("2026-09-21", "2026-09-30"),
         "`~` 에 공백이 없어도 · 한 자리여도 정리해 저장",
         str(ev and (ev["starts"], ev["ends"])))

    n_before = len(await db.list_events(GUILD.id))
    it = await fire(dict(name="거꾸로", period="2026-10-5 ~ 2026-9-30", mode="모두"))
    n_after = len(await db.list_events(GUILD.id))
    note(n_after == n_before, "끝이 정말 빠르면 여전히 거부 (10-5 ~ 9-30)", F.outcome(it))

    it = await fire(dict(name="없는날", period="2026-9-31 ~ 2026-10-5", mode="모두"))
    note(len(await db.list_events(GUILD.id)) == n_after,
         "없는 날짜(9월 31일)는 거부", F.outcome(it))


async def test_progress():
    print("\n=== 실제 영향: 진행도가 쌓이는가 ===")
    # 판정이 `datetime.now()` 를 쓰므로 날짜를 스크린샷 날로 고정합니다.
    real = db.datetime

    class Fixed(real):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 9, 26, 12, 0, tzinfo=tz or KST)

    db.datetime = Fixed
    try:
        gid, uid = 4242, 77
        await db.save_event(gid, "eshot", {
            "name": "테스트", "mode": "all", "enabled": True,
            "starts": "2026-9-21", "ends": "2026-9-30",   # 옛 모양 그대로 저장된 것
            "objectives": [{"kind": "chat", "goal": 5}]})
        db.invalidate_events(gid)
        for _ in range(3):
            await db.bump_event_progress(gid, uid, "chat")
        prog = await db.get_quest_progress(gid, uid,
                                           periods=[db.event_period_key("eshot")])
        got = db.event_progress_value({"kind": "chat"}, prog, "chat")
        note(got == 3, "이미 한 자리로 저장된 이벤트도 **다시 저장하지 않고** 채팅 3회가 쌓임",
             f"진행도 {got}")
    finally:
        db.datetime = real


async def main():
    test_screenshot()
    test_boundaries()
    test_exhaustive()
    await test_modal()
    await test_progress()


asyncio.run(main())
print(f"\n  실패 {len(F.FAILS)}건 / 성공 {len(F.OKS)}건")
for x in F.FAILS:
    print("   -", x)
sys.exit(1 if F.FAILS else 0)
