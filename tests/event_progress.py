"""이벤트 퀘스트 진행도가 '안 오르는' 것처럼 보이던 문제 (2026-10-10 제보).

실측(운영 DB, 읽기만):
  · 할로윈 채팅 이벤트 문서에는 ev_chat 826 이 정상으로 쌓여 있었는데
  · 같은 날 만든 출석 · 뽑기 이벤트가 **잠깐 '채팅' 종류로 만들어졌다 바뀌어서**
    그 문서들에도 ev_chat (최대 9) 이 들어갔고
  · /퀘스트 가 이벤트 문서들을 **하나로 합친 뒤** 읽어 그 9 가 826 을 덮었습니다
    → "할로윈 대화 퀘스트가 9회에서 오르지 않아요" · "1/200"
  · 또 이벤트를 17:06 에 만들어(시작일은 오늘) 그 전 채팅이 빠졌습니다(48 → 2).

지켜야 할 것:
  1. 이벤트 값은 **그 이벤트 문서에서만** 읽는다 (다른 이벤트의 같은 필드에 안 덮임)
  2. 이벤트를 만들기 전 기간 중 활동을 일일 기록으로 채운다 ($max · 반복해도 같음)
  3. 채운 뒤에 들어오는 활동도 정상으로 더해진다
"""
import sys, os, tempfile, asyncio, datetime

PROJ = sys.argv[1]
sys.path.insert(0, PROJ)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(tempfile.mkdtemp())
os.environ["MONGO_URI"] = ""

import fakemongo as FM
import fakes as F
from fakes import note
import utils.database as db

KST = db.KST


def ev(eid, kind, starts):
    return {"name": f"{kind} 이벤트", "mode": "tier", "kind": kind, "enabled": True,
            "starts": starts, "ends": "2099-12-31",
            "tiers": [{"goal": 200, "points": 300}, {"goal": 400, "points": 500}]}


async def scenario(label, gid):
    print(f"\n=== {label} ===")
    today = datetime.datetime.now(KST).strftime("%Y-%m-%d")
    U = 777

    print("--- 1. 다른 이벤트의 같은 필드가 덮어쓰면 안 됨 (실제 사고 재현) ---")
    await db._save_event_doc(gid, "eCHAT", ev("eCHAT", "chat", today))
    await db._save_event_doc(gid, "eCHK", ev("eCHK", "checkin", today))
    db.invalidate_events(gid)
    for _ in range(826):
        await db.bump_quest_progress(gid, U, "ev_chat", period=db.event_period_key("eCHAT"))
    # 출석 이벤트가 잠깐 '채팅' 이었을 때 들어간 값
    for _ in range(9):
        await db.bump_quest_progress(gid, U, "ev_chat", period=db.event_period_key("eCHK"))
    events = await db.active_events(gid)
    periods = [db.event_period_key(e["id"]) for e in events]
    # 순서를 바꿔 가며 — 합친 dict 는 '마지막 문서' 가 이깁니다
    for order in (periods, list(reversed(periods))):
        prog = await db.get_quest_progress(gid, U, order)
        chat_ev = next(e for e in events if e["id"] == "eCHAT")
        st = db.event_status(chat_ev, prog)
        note(st["rows"][0]["value"] == 826,
             f"**채팅 이벤트는 826** (출석 이벤트의 ev_chat=9 에 안 덮임) · 조회 순서 {order[0][-4:]}…",
             str(st["rows"][0]["value"]))
    chk_ev = next(e for e in events if e["id"] == "eCHK")
    note(db.event_status(chk_ev, prog)["rows"][0]["value"] == 0,
         "출석 이벤트는 자기 ev_checkin 만 (잘못 들어간 ev_chat 은 무시)")

    print("--- 2. 이벤트를 만들기 전 활동 채우기 ---")
    V = 778
    for _ in range(46):                       # 이벤트 만들기 전 오늘 채팅
        await db.bump_quest_progress(gid, V, "chat")
    await db.save_event(gid, "eNEW", ev("eNEW", "chat", today))   # 저장하면서 채움
    db.invalidate_events(gid)
    for _ in range(2):                        # 만든 뒤: 일일 · 이벤트 둘 다 오름
        await db.bump_quest_progress(gid, V, "chat")
        await db.bump_event_progress(gid, V, "chat")
    new_ev = next(e for e in await db.active_events(gid) if e["id"] == "eNEW")
    prog = await db.get_quest_progress(gid, V, [db.event_period_key("eNEW")])
    note(db.event_progress_value(new_ev, prog) == 48,
         "**저장할 때 오늘 앞선 채팅까지 채움** (48 = 46 + 2)",
         str(db.event_progress_value(new_ev, prog)))
    await db.backfill_active_events(gid)
    await db.backfill_active_events(gid)
    prog = await db.get_quest_progress(gid, V, [db.event_period_key("eNEW")])
    note(db.event_progress_value(new_ev, prog) == 48, "몇 번 다시 채워도 그대로 ($max)")
    await db.bump_quest_progress(gid, V, "chat")
    await db.bump_event_progress(gid, V, "chat")
    prog = await db.get_quest_progress(gid, V, [db.event_period_key("eNEW")])
    note(db.event_progress_value(new_ev, prog) == 49, "채운 뒤의 채팅도 정상으로 더해짐")

    print("--- 3. 기간 밖 · 다른 종류 ---")
    old_start = (datetime.datetime.now(KST) + datetime.timedelta(days=3)).strftime("%Y-%m-%d")
    await db.save_event(gid, "eFUT", ev("eFUT", "chat", old_start))
    prog = await db.get_quest_progress(gid, V, [db.event_period_key("eFUT")])
    note(not (prog["by_period"].get(db.event_period_key("eFUT")) or {}).get("ev_chat"),
         "아직 시작 안 한 이벤트는 채우지 않음")
    await db.bump_quest_progress(gid, V, "gacha", amount=7)
    await db.save_event(gid, "eGACHA", ev("eGACHA", "gacha", today))
    g_ev = await db.get_event(gid, "eGACHA")
    prog = await db.get_quest_progress(gid, V, [db.event_period_key("eGACHA")])
    note(db.event_progress_value(g_ev, prog) == 7 and
         "ev_chat" not in (prog["by_period"].get(db.event_period_key("eGACHA")) or {}),
         "뽑기 이벤트는 뽑기만 채움 (채팅이 섞이지 않음)")


async def main():
    await scenario("JSON 경로", 5151)
    FM.install(db, ["quest_prog_col", "event_col", "quest_set_col"])
    if hasattr(db, "_AFTER"):
        db._AFTER = FM.AFTER
    db._active_events_cache.clear() if hasattr(db._active_events_cache, "clear") else None
    await scenario("Mongo 경로", 5252)


asyncio.run(main())
print(f"\n  실패 {len(F.FAILS)}건 / 성공 {len(F.OKS)}건")
for x in F.FAILS:
    print("   -", x)
sys.exit(1 if F.FAILS else 0)
