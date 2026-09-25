"""퀘스트 UI 전수 점검 — 버튼·채널선택·이벤트 선택의 콜백을 실제로 호출합니다.

`ui_all` 은 Select 5종만, `modals` 는 모달만 훑습니다. 그 사이에 **버튼과
ChannelSelect 가 통째로 비어 있었습니다** — cog 로드만으로는 `NameError` 가
잡히지 않습니다. 그 줄이 **실행될 때만** 나기 때문입니다. `BackupChannelView`
를 지운 채 배포해 "적시에 응답하지 않았어요" 가 났던 것이 정확히 이 구멍입니다.

여기서 지켜야 할 것:

  1. **모든 버튼을 실제로 누릅니다.** 화면에 그려지는지가 아니라 콜백이
     예외 없이 끝나는지를 봅니다.
  2. **`QuestChannelView` 의 해제는 자기 설정만** 지워야 합니다. 공동 목표
     화면에서 눌렀는데 랜덤 채널 후보가 날아가면, 그날 뽑기 풀이 바뀌어
     **전원의 퀘스트 목록이** 통째로 달라집니다.
  3. **`QuestView` 는 남이 누르면 막아야** 합니다 (`interaction_check`).
  4. **보상은 두 번 나가면 안 됩니다.** 연타해도 한 번만.
"""
import sys, os, tempfile, asyncio

PROJ = sys.argv[1]
sys.path.insert(0, PROJ)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(tempfile.mkdtemp())
os.environ["MONGO_URI"] = ""

import discord
import fakes as F
from fakes import Inter, run, note, GUILD, drain


def set_values(sel, values):
    """Select/ChannelSelect 에 고른 값을 심습니다.

    discord.py 는 버전에 따라 `_values` 와 `_selected_values` 중 하나를
    봅니다. **심은 뒤 `sel.values` 로 되읽어 확인**하지 않으면, 아무것도
    안 고른 상태로 콜백이 돌면서 테스트가 거짓 통과합니다.
    """
    for attr in ("_values", "_selected_values"):
        try:
            setattr(sel, attr, list(values))
        except Exception:
            pass
    return list(getattr(sel, "values", []) or [])


async def main():
    import utils.database as db
    import cogs.quest as Q

    print("=== 0. 준비 ===")
    note(Q.__dict__ is Q.QuestView.__init__.__globals__,
         "테스트가 실제로 도는 모듈을 잡았는지")
    st = await db.get_quest_settings(GUILD.id)
    note(isinstance(st, dict), "퀘스트 설정을 읽음")

    # ── 1. QuestView (유저가 실제로 누르는 버튼) ──────────────────────
    print("")
    print("=== 1. QuestView — 보상 받기 ===")
    claimable = [
        {"id": "chat", "name": "채팅 20회", "points": 100,
         "role_id": None, "weekly": False, "period": None},
        {"id": "week_chat", "name": "주간 채팅 150회", "points": 300,
         "role_id": None, "weekly": True, "period": None},
    ]
    v = Q.QuestView(7, claimable)
    await run("보상 받기 (받을 것 있음)", v.claim_all.callback)

    print("")
    print("  ⚠️ 연타해도 두 번 지급되면 안 됩니다")
    it1 = Inter(uid=11)
    v2 = Q.QuestView(11, claimable)
    before = await db.get_user_points(GUILD.id, 11)
    await v2.claim_all.callback(it1)
    await drain()
    mid = await db.get_user_points(GUILD.id, 11)
    await v2.claim_all.callback(Inter(uid=11))
    await drain()
    after = await db.get_user_points(GUILD.id, 11)
    note(mid > before, "첫 수령으로 포인트가 늘어남", f"{before}→{mid}")
    note(after == mid, "**두 번째 수령은 늘지 않음**", f"{mid}→{after}")

    print("")
    print("  받을 것이 없으면 버튼이 잠겨야 합니다")
    empty = Q.QuestView(7, [])
    note(empty.claim_all.disabled, "버튼 비활성")
    note("없습니다" in (empty.claim_all.label or ""), "이유를 적음",
         empty.claim_all.label)

    print("")
    print("  ⚠️ 남의 퀘스트 창은 누를 수 없어야 합니다")
    mine = Q.QuestView(7, claimable)
    other = Inter(uid=999)
    ok = await mine.interaction_check(other)
    note(ok is False, "본인이 아니면 거부")
    note(bool(other.response.sent), "이유를 알려줌", str(other.response.sent)[:60])
    note(all(s.get("ephemeral") is True for s in other.response.sent),
         "거부 안내도 ephemeral")
    same = Inter(uid=7)
    note(await mine.interaction_check(same) is True, "본인은 통과")

    # ── 2. QuestChannelView (해제 버튼 + ChannelSelect) ───────────────
    print("")
    print("=== 2. QuestChannelView — 채널 선택과 해제 ===")
    for field, label in (("channel_id", "랜덤 채널"), ("goal_channel_id", "공동 목표")):
        view = Q.QuestChannelView(field, label)
        note(len(view.children) >= 2, f"{label}: 선택 + 해제 버튼이 있음",
             str(len(view.children)))
        await run(f"{label} / [해제]", view.clear.callback)

    print("")
    print("  ⚠️ 해제는 **자기 설정만** 지워야 합니다")
    await db.save_quest_settings(GUILD.id, {
        "channel_ids": ["111", "222"], "goal_channel_id": 333})
    gv = Q.QuestChannelView("goal_channel_id", "공동 목표")
    await gv.clear.callback(Inter())
    await drain()
    cur = await db.get_quest_settings(GUILD.id)
    note(cur.get("goal_channel_id") in (None, 0), "공동 목표 안내 채널이 지워짐",
         str(cur.get("goal_channel_id")))
    # 여기가 깨지면 그날 뽑기 풀이 바뀌어 전원의 퀘스트가 달라집니다.
    note(list(cur.get("channel_ids") or []) == ["111", "222"],
         "**랜덤 채널 후보는 건드리지 않음**", str(cur.get("channel_ids")))

    await db.save_quest_settings(GUILD.id, {
        "channel_ids": ["111", "222"], "goal_channel_id": 333})
    rv = Q.QuestChannelView("channel_id", "랜덤 채널")
    await rv.clear.callback(Inter())
    await drain()
    cur = await db.get_quest_settings(GUILD.id)
    note(not (cur.get("channel_ids") or []), "랜덤 채널 후보가 지워짐",
         str(cur.get("channel_ids")))
    note(cur.get("goal_channel_id") == 333,
         "**공동 목표 안내 채널은 건드리지 않음**",
         str(cur.get("goal_channel_id")))

    print("")
    print("  ChannelSelect 콜백")
    picked = [GUILD.channels[0], GUILD.channels[1]]
    rs = Q.RandomChannelSelect()
    got = set_values(rs, picked)
    note(len(got) == 2, "고른 채널이 실제로 심어짐 (거짓 통과 방지)", str(len(got)))
    if len(got) == 2:
        await run("랜덤 채널 후보 저장", rs.callback)
        cur = await db.get_quest_settings(GUILD.id)
        note([str(c.id) for c in picked] == list(cur.get("channel_ids") or []),
             "고른 채널이 설정에 들어감", str(cur.get("channel_ids")))
        # 구버전 단일 키가 남아 있으면 두 값이 어긋납니다.
        note(cur.get("channel_id") in (None, 0),
             "구버전 단일 키는 비워짐", str(cur.get("channel_id")))

    gs = Q.GoalChannelSelect()
    if len(set_values(gs, [GUILD.channels[0]])) == 1:
        await run("공동 목표 안내 채널 저장", gs.callback)

    # ── 3. 이벤트 UI ──────────────────────────────────────────────────
    print("")
    print("=== 3. 이벤트 — 선택 · 삭제 확인 ===")
    # ⚠️ **이벤트를 실제로 만들어 두고 돌립니다.** 0개인 채로 돌리면 콜백이
    #    전부 '이벤트가 없습니다' 분기만 타고 OK 가 찍혀, 정작 검증하려던
    #    본 경로는 한 줄도 실행되지 않습니다 (거짓 통과).
    today = db.datetime.now(db.KST).strftime("%Y-%m-%d")
    later = (db.datetime.now(db.KST) + db.timedelta(days=7)).strftime("%Y-%m-%d")
    await db.save_event(GUILD.id, "ev_tier", {
        "name": "단계별 이벤트", "mode": "tier", "kind": "chat",
        "starts": today, "ends": later,
        "tiers": [{"goal": 10, "points": 100}, {"goal": 50, "points": 500}]})
    await db.save_event(GUILD.id, "ev_all", {
        "name": "모두달성 이벤트", "mode": "all",
        "starts": today, "ends": later,
        "objectives": [{"kind": "chat", "goal": 30}],
        "points": 2000, "role_id": None})
    events = await db.list_events(GUILD.id)
    note(len(events) >= 2, "이벤트 2개를 실제로 만들어 둠 (거짓 통과 방지)",
         str(len(events)))
    note(all(e.get("id") != "none" for e in events),
         "'없습니다' 자리표시가 아닌 진짜 이벤트")

    # `EventMenuSelect` 가 실제로 넘기는 action 전부입니다. 없는 이름을
    # 넣어 돌리면 통과해도 아무 의미가 없으므로 메뉴와 맞춰 둡니다.
    for action in ("goal", "reward", "edit", "toggle", "delete"):
        for ev in events:
            sel = Q.EventPickSelect(events, action)
            set_values(sel, [ev["id"]])
            it = await run(f"이벤트 선택 / {action} / {ev['id']}", sel.callback)
            # ⚠️ **아무 응답도 없으면 화면에 '상호작용 실패' 가 뜹니다.**
            # 예외가 안 났다고 통과시키면 이 상태를 놓칩니다.
            note(it.response.done or bool(it.followup.sent),
                 f"  └ {action}/{ev['id']} 응답함")

    print("")
    print("  ⚠️ 모르는 action 이 와도 화면에 무언가는 떠야 합니다")
    unknown = Q.EventPickSelect(events, "존재하지않는기능")
    set_values(unknown, [events[0]["id"]])
    it = await run("이벤트 선택 / 모르는 action", unknown.callback)
    note(it.response.done or bool(it.followup.sent),
         "**말없이 끝나지 않음** (상호작용 실패 방지)")

    print("")
    print("  ⚠️ 이벤트가 없을 때도 터지지 않아야 합니다")
    empty_sel = Q.EventPickSelect([], "delete")
    set_values(empty_sel, ["none"])
    await run("이벤트 0개 / delete", empty_sel.callback)

    print("")
    print("  삭제 확인 버튼 — 실제로 지워져야 합니다")
    conf = Q.EventDeleteConfirm("ev_tier", "단계별 이벤트")
    await run("삭제 확인 / [삭제]", conf.do_delete.callback)
    note(await db.get_event(GUILD.id, "ev_tier") is None,
         "**이벤트가 실제로 지워짐**")

    conf2 = Q.EventDeleteConfirm("ev_all", "모두달성 이벤트")
    await run("삭제 확인 / [취소]", conf2.cancel.callback)
    note(await db.get_event(GUILD.id, "ev_all") is not None,
         "**취소는 지우지 않음**")

    print("")
    print("  이미 지워진 것을 다시 눌러도 터지지 않아야 합니다")
    again = Q.EventDeleteConfirm("ev_tier", "단계별 이벤트")
    await run("삭제 확인 / [삭제] 재실행", again.do_delete.callback)

    # ── 4. View 조립 (지워진 클래스를 참조하면 여기서 터집니다) ────────
    print("")
    print("=== 4. View 조립 ===")
    builders = [
        ("EventPickView", lambda: Q.EventPickView(events, "view")),
        ("EventMenuView", lambda: Q.EventMenuView(events)),
        ("DailySettingsView", lambda: Q.DailySettingsView(st, GUILD)),
        ("WeeklySettingsView", lambda: Q.WeeklySettingsView(st)),
        ("GoalSettingsView", lambda: Q.GoalSettingsView(st, GUILD)),
        ("QuestSettingsView", lambda: Q.QuestSettingsView(st, GUILD)),
    ]
    for name, make in builders:
        try:
            view = make()
            note(len(view.children) > 0, f"{name} 조립 + 항목 있음",
                 str(len(view.children)))
        except Exception as e:
            note(False, f"{name} 조립", f"{type(e).__name__}: {e}")

    # ── 5. 패널 렌더 ─────────────────────────────────────────────────
    print("")
    print("=== 5. 패널 렌더 ===")
    try:
        panel = Q.build_quest_panel(st)
        note(isinstance(panel, discord.Embed), "퀘스트 패널 임베드 생성")
        note(len(panel.title or "") > 0, "제목 있음", panel.title)
    except Exception as e:
        note(False, "퀘스트 패널", f"{type(e).__name__}: {e}")

    # 순서를 바꾸지 마세요 — run_all 이 '실패 → 성공' 순으로 숫자를 읽습니다.
    print(f"\n  실패 {len(F.FAILS)}건 / 성공 {len(F.OKS)}건")
    for x in F.FAILS:
        print("   -", x)


asyncio.run(main())
