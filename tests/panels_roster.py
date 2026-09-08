"""인증 패널 다중 전송·자동 갱신, 그리고 미연동자 목록.

패널은 여러 채널에 올릴 수 있고 전부 동시에 동작합니다(버튼 custom_id 고정).
문제는 임베드에 **인증 자격 역할 목록**이 박혀 있다는 점입니다. 자격을 바꾸면
이미 올라간 패널들이 옛 조건을 계속 보여주고, 유저는 안 되는 조건을 보고
문의합니다. 하나일 때는 지우고 다시 올리면 됐지만 여러 개면 전부 찾아야 합니다.
"""
import sys, os, tempfile, asyncio

PROJ = sys.argv[1]
sys.path.insert(0, PROJ)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(tempfile.mkdtemp())
os.environ["MONGO_URI"] = ""
os.environ.setdefault("DISCORD_TOKEN", "x")

import discord
import fakes as F
from fakes import note, drain
import cogs.verify as V
import cogs.roblox_link as RL
from utils.verify_db import (save_verify_settings, get_verify_settings,
                             save_roblox_link, remove_roblox_link)


class Tracking(F.Channel):
    """올린 패널을 붙들고, 수정 요청을 기록하는 채널 대역."""

    def __init__(self, cid, name):
        super().__init__(cid, name, None)
        self.posted = {}
        self.edits = []
        self.gone = set()
        self.denied = set()

    async def send(self, *a, **k):
        m = F.Msg(cid_seq(), self)
        m.embeds = [k["embed"]] if k.get("embed") else []
        self.posted[m.id] = m
        return m

    def get_partial_message(self, mid):
        ch = self

        class P:
            id = mid

            async def edit(self, **kw):
                if mid in ch.gone:
                    raise discord.NotFound(
                        type("R", (), {"status": 404, "reason": ""})(), "gone")
                if mid in ch.denied:
                    raise discord.Forbidden(
                        type("R", (), {"status": 403, "reason": ""})(), "no perm")
                ch.edits.append((mid, kw))
        return P()


_seq = [9000]


def cid_seq():
    _seq[0] += 1
    return _seq[0]


class Guild(F.Guild):
    def __init__(self, channels):
        super().__init__()
        self._extra = {c.id: c for c in channels}
        for c in channels:
            c.guild = self

    def get_channel(self, cid):
        if cid is None:
            return None
        return self._extra.get(int(cid)) or super().get_channel(cid)


async def post_panel(guild, channel):
    """`/인증설정` → 인증 패널 전송 과 같은 경로를 탑니다."""
    it = F.Inter()
    it.guild = guild
    sel = V.PanelChannelSelect()
    sel._values = [type("C", (), {"id": channel.id})()]
    await sel.callback(it)
    await drain()
    return it


async def test_panels():
    g_all = [Tracking(801, "인증1"), Tracking(802, "인증2"), Tracking(803, "인증3")]
    guild = Guild(g_all)
    a, b, c = g_all

    print("=== 여러 채널에 올릴 수 있어야 합니다 ===")
    await save_verify_settings(guild.id, {
        "role_id": 902, "required_role_ids": [901], "panel_messages": [],
        "title": "서버 인증", "description": "눌러 주세요", "button_label": "인증하기"})

    for ch in (a, b, c):
        await post_panel(guild, ch)
    s = await get_verify_settings(guild.id)
    note(len(s.get("panel_messages") or []) == 3, "패널 3개가 기록됨",
         str(len(s.get("panel_messages") or [])))
    note(s.get("panel_channel_id") == c.id,
         "안내용 대표 채널은 마지막에 올린 곳", str(s.get("panel_channel_id")))

    print("")
    print("=== 자격 역할을 바꾸면 올려둔 패널이 전부 갱신돼야 ===")
    for ch in g_all:
        ch.edits.clear()
    note_text = await V.save_panel_setting(guild, {"required_role_ids": [901, 902]})
    total = sum(len(ch.edits) for ch in g_all)
    note(total == 3, "3개 전부 수정됨", f"{total}개")
    note("3개" in note_text, "몇 개를 고쳤는지 알려줌", note_text)

    body = a.edits[-1][1]["embed"].to_dict()
    field = next((f for f in body.get("fields", [])
                  if f["name"].startswith("🎫")), None)
    note(field is not None and "<@&902>" in field["value"],
         "새 자격 역할이 패널에 반영됨")

    print("")
    print("=== 문구를 바꿔도 전부 갱신돼야 ===")
    for ch in g_all:
        ch.edits.clear()
    await V.save_panel_setting(guild, {"title": "새 제목", "button_label": "인증"})
    note(sum(len(ch.edits) for ch in g_all) == 3, "3개 전부 수정됨")
    d = a.edits[-1][1]["embed"].to_dict()
    note(d.get("title") == "새 제목", "제목 반영", str(d.get("title")))
    view = a.edits[-1][1]["view"]
    note(view.children[0].label == "인증", "버튼 문구 반영",
         view.children[0].label)
    note(view.children[0].custom_id == V.VERIFY_BUTTON_ID,
         "custom_id 는 그대로 (재시작 후에도 동작)")

    print("")
    print("=== 지워진 패널은 목록에서 스스로 빠져야 ===")
    for ch in g_all:
        ch.edits.clear()
    s = await get_verify_settings(guild.id)
    victim = next(r for r in s["panel_messages"] if int(r["c"]) == b.id)
    b.gone.add(int(victim["m"]))
    upd, rem = await V.refresh_all_panels(guild)
    note((upd, rem) == (2, 1), "살아있는 2개 갱신 · 사라진 1개 정리", str((upd, rem)))
    s = await get_verify_settings(guild.id)
    note(len(s["panel_messages"]) == 2, "목록이 2개로 줄어듦",
         str(len(s["panel_messages"])))

    print("")
    print("=== 권한이 없으면 목록에 남겨 둬야 (되돌리면 다시 동작) ===")
    s = await get_verify_settings(guild.id)
    keep = next(r for r in s["panel_messages"] if int(r["c"]) == a.id)
    a.denied.add(int(keep["m"]))
    upd, rem = await V.refresh_all_panels(guild)
    s = await get_verify_settings(guild.id)
    note(any(int(r["c"]) == a.id for r in s["panel_messages"]),
         "권한 오류로는 목록에서 지우지 않음")
    a.denied.clear()

    print("")
    print("=== 패널이 하나도 없으면 조용히 넘어가야 ===")
    await save_verify_settings(guild.id, {"panel_messages": []})
    note(await V.refresh_all_panels(guild) == (0, 0), "0,0 반환")
    note(await V.save_panel_setting(guild, {"auto_revoke": True}) == "",
         "안내 문구도 비어 있음")

    print("")
    print("=== 추적 개수에 상한이 있어야 ===")
    await save_verify_settings(guild.id, {"panel_messages": []})
    for _ in range(V.MAX_TRACKED_PANELS + 5):
        await post_panel(guild, a)
    s = await get_verify_settings(guild.id)
    note(len(s["panel_messages"]) == V.MAX_TRACKED_PANELS,
         f"상한 {V.MAX_TRACKED_PANELS}개 유지", str(len(s["panel_messages"])))


async def test_roster():
    print("")
    print("=== 미연동자 목록 (공지 대상 추리기) ===")
    guild = Guild([])
    need = guild.get_role(902)          # 필수역할
    other = guild.get_role(901)

    # 자격 있는 4명 + 자격 없는 1명 + 봇 1명
    members = [F.Member(300 + i, f"자격자{i}", False, roles=[need], guild=guild)
               for i in range(4)]
    members.append(F.Member(400, "자격없음", False, roles=[other], guild=guild))
    bot_m = F.Member(401, "봇", False, roles=[need], guild=guild)
    bot_m.bot = True
    members.append(bot_m)
    guild.members = members

    await save_verify_settings(guild.id, {"required_role_ids": [need.id]})
    for m in members[:6]:
        await remove_roblox_link(guild.id, m.id)
    await save_roblox_link(guild.id, 300, 111, "이미연동", method="oauth", created_at="")

    it = F.Inter()
    it.guild = guild
    view = RL.RosterToolsView(guild.id)
    await view.unlinked.callback(it)
    await drain()

    sent = it.followup.sent[-1] if it.followup.sent else {}
    e = sent.get("embed")
    note(e is not None, "결과를 보냄")
    note("3명" in (e.description or ""), "자격 4명 중 연동 1명 → 3명",
         (e.description or "")[:60])
    note(sent.get("ephemeral") is True, "ephemeral=True")
    note(sent.get("file") is not None, "전체 목록 파일 첨부")

    mentions = next((f["value"] for f in e.to_dict().get("fields", [])
                     if f["name"].startswith("📢")), "")
    note("<@401>" not in mentions, "봇은 대상에서 제외")
    note("<@400>" not in mentions, "자격 없는 멤버는 제외")
    note("<@300>" not in mentions, "이미 연동한 멤버는 제외")

    print("")
    print("=== 자격 역할이 없으면 전원이 대상이라고 알려야 ===")
    await save_verify_settings(guild.id, {"required_role_ids": []})
    it = F.Inter(); it.guild = guild
    await RL.RosterToolsView(guild.id).unlinked.callback(it)
    await drain()
    d = (it.followup.sent[-1].get("embed").description or "")
    note("서버 전원" in d, "전원 대상임을 명시", d[:50])

    print("")
    print("=== 명단 파일 내려받기 ===")
    it = F.Inter(); it.guild = guild
    await RL.RosterToolsView(guild.id).download.callback(it)
    await drain()
    sent = it.followup.sent[-1]
    note(sent.get("file") is not None, "CSV 첨부됨")
    note(sent.get("ephemeral") is True, "ephemeral=True")

    print("")
    print("=== 관리자가 아니면 버튼이 막혀야 ===")
    it = F.Inter(uid=8, admin=False); it.guild = guild
    ok = await RL.RosterToolsView(guild.id).interaction_check(it)
    note(ok is False, "일반 유저 거부")
    note(bool(it.response.sent), "이유를 안내함")


async def main():
    await test_panels()
    await test_roster()

asyncio.run(main())
print(f"\n  실패 {len(F.FAILS)}건 / 성공 {len(F.OKS)}건")
for x in F.FAILS:
    print("   -", x)
sys.exit(1 if F.FAILS else 0)
