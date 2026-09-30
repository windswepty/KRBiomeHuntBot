"""경고 · 밴 (cogs/moderation.py) — 명령어·버튼·모달·!명령어를 실제로 호출합니다.

여기서 지켜야 할 것 (전부 '겉보기엔 되는데 조용히 틀리는' 자리입니다):

  1. 밴 DM 은 **밴 전에** 가고, 밴이 실패하면 **회수**된다
  2. 처벌할 수 없는 대상(역할이 높음)에게는 **DM 부터 가지 않는다**
  3. 자동 처벌은 **정확히 그 횟수에 한 번** — 동시에 경고해도 건너뛰지 않는다
  4. 같은 경고를 두 번 풀어도 **한 번만** 빠진다
  5. 이름 검색은 `re.escape` — `.*` 로 기록이 통째로 나오면 안 된다
  6. 슬래시와 !명령어가 **같은 본체**를 쓴다 (한쪽만 검사가 빠지지 않게)
"""
import sys, os, tempfile, asyncio

PROJ = sys.argv[1]
sys.path.insert(0, PROJ)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(tempfile.mkdtemp())
os.environ["MONGO_URI"] = ""

import discord
from discord.ext import commands
import fakes as F
from fakes import note, drain, GUILD, Inter, Role, Member, Perms

import cogs.moderation as M
import re as _re_mod


def _re_search(p, t):
    return _re_mod.search(p, t) is not None
import utils.mod_db as mdb

MOD_ROLE = Role(5001, "운영진", 50)
LOW_ROLE = Role(5002, "일반", 10)
HIGH_ROLE = Role(5003, "관리자급", 60)
LOG = GUILD.get_channel(333)


# 디스코드 ID 는 17~20자리입니다. 짧은 가짜 ID 를 쓰면 ID 파싱이
# 실제와 다르게 동작해 테스트가 엉뚱한 것을 봅니다.
BASE = 100000000000000000


def member(uid, name, roles=None):
    uid = BASE + uid
    m = Member(uid, name, False, roles=roles if roles is not None else [LOW_ROLE], guild=GUILD)
    GUILD.members.append(m)
    return m


def mod_inter(**kw):
    return Inter(uid=7, roles=[MOD_ROLE], **kw)


def last_embed(it):
    for s in reversed(it.followup.sent + it.response.sent):
        if s.get("embed") is not None:
            return s["embed"]
    if it.response.edited and it.response.edited.get("embed") is not None:
        return it.response.edited["embed"]
    return None


def text_of(e):
    if e is None:
        return ""
    return "\n".join([e.title or "", e.description or ""]
                     + [(f.name or "") + (f.value or "") for f in e.fields])


class Ctx:
    """!명령어용 가짜 Context."""
    def __init__(self, author, command=None):
        self.guild, self.author, self.command = GUILD, author, command
        self.prefix = "!"                    # 진짜 Context 는 친 접두사를 담습니다
        self.replies = []

    async def reply(self, *a, **k):
        self.replies.append({**k, "content": a[0] if a else k.get("content")})


async def test_warn():
    print("=== 1. 경고 · DM · 로그 · 케이스 번호 ===")
    await mdb.save_mod_settings(GUILD.id, {"log_channel_id": LOG.id})
    t = member(8101, "경고대상")
    it = mod_inter()
    await M.Moderation.warn.callback(M.Moderation(None), it, t, "도배")
    e = last_embed(it)
    note(e and "케이스 #1" in e.title and "1회" in e.description, "첫 경고 = 케이스 #1, 누적 1회",
         e.title if e else "")
    note(t.dms and "도배" in text_of(t.dms[-1]["embed"]), "대상에게 사유가 담긴 DM")
    note(LOG.sent and "경고" in LOG.sent[-1]["embed"].title, "로그 채널에 기록")
    note(LOG.sent[-1].get("allowed_mentions") is not None
         and not LOG.sent[-1]["allowed_mentions"].users,
         "로그는 아무도 호출하지 않음 (allowed_mentions none)")

    it = mod_inter()
    await M.Moderation.warn.callback(M.Moderation(None), it, t, "욕설")
    note("케이스 #2" in last_embed(it).title and "2회" in last_embed(it).description,
         "두 번째 경고 = #2, 누적 2회")

    print("\n  처벌할 수 없는 대상에게는 DM 부터 가면 안 됩니다")
    boss = member(8102, "높은사람", [HIGH_ROLE])
    it = mod_inter()
    await M.Moderation.warn.callback(M.Moderation(None), it, boss, "테스트")
    note("경고할 수 없습니다" in text_of(last_embed(it)), "역할이 더 높으면 거부")
    note(not boss.dms, "거부했으면 DM 도 없음")
    note(not await mdb.get_user_cases(GUILD.id, boss.id), "기록도 없음")

    it = mod_inter()
    await M.Moderation.warn.callback(M.Moderation(None), it, it.user, "자기")
    note("자기 자신" in text_of(last_embed(it)), "자기 자신 거부")

    bot_target = member(8103, "다른봇")
    bot_target.bot = True
    it = mod_inter()
    await M.Moderation.warn.callback(M.Moderation(None), it, bot_target, "x")
    note("봇에게는" in text_of(last_embed(it)), "봇에게는 경고 안 함")

    print("\n  DM 을 막아 둔 사람도 경고는 들어가야 합니다")
    closed = member(8104, "DM막음")
    closed.dm_closed = True
    it = mod_inter()
    await M.Moderation.warn.callback(M.Moderation(None), it, closed, "x")
    note("DM 을 막아 둔" in text_of(last_embed(it))
         and len(await mdb.get_active_warnings(GUILD.id, closed.id)) == 1,
         "DM 이 막혀도 경고는 기록되고 그 사실을 알려 줌")
    note("어떤 봇도" in text_of(last_embed(it)),
         "**우리 봇 고장이 아니라는 것**까지 알려 줌 (디스코드가 거부 · Dyno 도 같음)")

    print("\n  50007 이 아닌 거부는 '막아 둔 유저' 로 뭉개지 않습니다")
    import io, contextlib

    class Odd:
        id = 8105

        async def send(self, **k):
            raise discord.Forbidden(type("R", (), {"status": 403, "reason": ""})(),
                                    {"code": 50001, "message": "Missing Access"})

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        _, status = await M.safe_dm(Odd(), discord.Embed(title="x"))
    note(status == "failed" and "code=50001" in buf.getvalue(),
         "다른 코드는 '전송 실패' + 로그에 코드", f"{status} · {buf.getvalue().strip()[:60]}")


async def test_auto():
    print("\n=== 2. 누적 자동 처벌 ===")
    await mdb.save_mod_settings(GUILD.id, {"auto_rules": [
        {"count": 2, "action": "timeout", "minutes": 90},
        {"count": 3, "action": "ban"}]})
    await mdb.save_mod_settings(GUILD.id, {"support_link": "https://discord.gg/help123"})
    t = member(8201, "누적대상")
    cog = M.Moderation(None)
    await M.Moderation.warn.callback(cog, mod_inter(), t, "1차")
    note(not hasattr(t, "timed_out"), "1회째는 아무 일 없음")

    it = mod_inter()
    await M.Moderation.warn.callback(cog, it, t, "2차")
    note(getattr(t, "timed_out", None) is not None, "2회째 → 타임아웃")
    note("1시간 30분" in text_of(last_embed(it)), "기간을 사람이 읽는 형태로", text_of(last_embed(it))[-60:])
    note("타임아웃" in text_of(t.dms[-1]["embed"]), "경고 DM 에 곧 받을 처벌이 적힘")

    before = len(t.dms)
    it = mod_inter()
    await M.Moderation.warn.callback(cog, it, t, "3차")
    note(t.id in GUILD.bans, "3회째 → 자동 밴")
    note(len(t.dms) == before + 1, "DM 은 한 통 (경고 DM 대신 밴 DM)",
         f"{len(t.dms) - before}통")
    dm = t.dms[-1]
    note("차단" in dm["embed"].title and "3회 누적" in text_of(dm["embed"]),
         "밴 DM 에 누적 사유", dm["embed"].title)
    btn = dm.get("view").children[0] if dm.get("view") else None
    note(btn is not None and btn.url == "https://discord.gg/help123",
         "밴 DM 에 서포트 서버 링크 버튼", getattr(btn, "url", None))
    cases = await mdb.get_user_cases(GUILD.id, t.id)
    note(any(c["kind"] == "ban" and c.get("auto") for c in cases), "자동 밴이 (자동) 으로 기록됨")

    print("\n  ⚠️ 이미 넘긴 규칙은 다시 걸지 않습니다")
    t2 = member(8202, "많이받음")
    await mdb.save_mod_settings(GUILD.id, {"auto_rules": [
        {"count": 2, "action": "timeout", "minutes": 10}]})
    for r in ("a", "b"):
        await M.Moderation.warn.callback(cog, mod_inter(), t2, r)
    t2.timed_out = None
    await M.Moderation.warn.callback(cog, mod_inter(), t2, "c")
    note(t2.timed_out is None, "3회째에 '2회 규칙' 이 또 걸리지 않음")

    print("\n  봇 역할이 낮으면 자동 처벌은 실패를 알려야 합니다 (조용히 넘어가면 안 됨)")
    await mdb.save_mod_settings(GUILD.id, {"auto_rules": [{"count": 1, "action": "kick"}]})
    top = Role(5010, "봇보다위", 150)
    mod_top = Role(5011, "최상위운영", 200)
    t3 = member(8203, "봇위", [top])
    it = Inter(uid=7, roles=[mod_top])
    await M.Moderation.warn.callback(cog, it, t3, "x")
    note("봇보다 같거나 높" in text_of(last_embed(it)), "봇 역할이 낮으면 거부 사유를 알림",
         text_of(last_embed(it))[-80:])
    await mdb.save_mod_settings(GUILD.id, {"auto_rules": []})


async def test_ban():
    print("\n=== 3. 밴 — DM 은 밴 전에, 실패하면 회수 ===")
    cog = M.Moderation(None)
    await mdb.save_mod_settings(GUILD.id, {"ban_dm": True,
                                           "support_link": "https://discord.gg/help123",
                                           "ban_dm_message": "이의는 서포트 서버로"})
    t = member(8301, "밴대상")
    it = mod_inter()
    await M.Moderation.ban.callback(cog, it, t, "사기", 1)
    e = last_embed(it)
    note(t.id in GUILD.bans and "밴 |" in e.title, "밴 성공", e.title)
    note("사기" in (GUILD.bans[t.id] or "") and "관리자" in GUILD.bans[t.id],
         "감사 로그 사유에 처리자·사유", GUILD.bans[t.id])
    dm = t.dms[-1]["embed"]
    note("사기" in text_of(dm) and "이의는 서포트 서버로" in text_of(dm)
         and "discord.gg/help123" in text_of(dm), "DM 에 사유·문구·서포트 링크")
    note("✅ DM 전송됨" in e.description, "결과에 DM 전송 여부")

    it = mod_inter()
    await M.Moderation.ban.callback(cog, it, t, "또", 0)
    note("이미 밴 상태" in text_of(last_embed(it)) and "사기" in text_of(last_embed(it)),
         "이미 밴이면 거부하고 기존 사유를 보여 줌")

    print("\n  ⚠️ 밴이 실패하면 보낸 DM 을 지워야 합니다")
    t2 = member(8302, "밴실패")
    GUILD.ban_fail = discord.Forbidden(type("R", (), {"status": 403, "reason": ""})(), "x")
    it = mod_inter()
    await M.Moderation.ban.callback(cog, it, t2, "x", 0)
    GUILD.ban_fail = None
    note("밴 실패" in last_embed(it).title, "실패를 알림")
    note(t2.dm_msgs and t2.dm_msgs[-1].deleted, "보낸 DM 을 회수함")
    note(not await mdb.get_user_cases(GUILD.id, t2.id), "실패한 밴은 기록하지 않음")

    print("\n  서버에 없는 사람도 ID 로 밴 (DM 은 못 보냄)")
    ghost = type("U", (), {"id": BASE + 8303999, "name": "나간사람", "__str__": lambda s: "나간사람"})()
    it = mod_inter()
    await M.Moderation.ban.callback(cog, it, ghost, "우회", 0)
    note(ghost.id in GUILD.bans and "서버에 없는" in text_of(last_embed(it)),
         "나간 사람 밴 + DM 불가 안내")

    boss = member(8304, "높은사람2", [HIGH_ROLE])
    it = mod_inter()
    await M.Moderation.ban.callback(cog, it, boss, "x", 0)
    note(boss.id not in GUILD.bans and not boss.dms, "역할이 높으면 DM 도 밴도 없음")

    await mdb.save_mod_settings(GUILD.id, {"ban_dm": False})
    t4 = member(8305, "DM꺼짐")
    await M.Moderation.ban.callback(cog, mod_inter(), t4, "x", 0)
    note(t4.id in GUILD.bans and not t4.dms, "밴 DM 을 끄면 안 보냄")
    await mdb.save_mod_settings(GUILD.id, {"ban_dm": True})


async def test_unwarn_unban():
    print("\n=== 4. 경고 해제 · 밴 해제 ===")
    cog = M.Moderation(None)
    await mdb.save_mod_settings(GUILD.id, {"auto_rules": []})
    t = member(8401, "해제대상")
    for r in ("하나", "둘", "셋"):
        await M.Moderation.warn.callback(cog, mod_inter(), t, r)
    warns = await mdb.get_active_warnings(GUILD.id, t.id)
    note(len(warns) == 3, "경고 3개", str(len(warns)))

    it = mod_inter()
    modal = M.UnwarnModal()
    modal.user_input._value = str(t.id)
    modal.reason_input._value = "오해"
    await modal.on_submit(it)
    sent = it.followup.sent[-1]
    view = sent.get("view")
    note(isinstance(view, M.UnwarnView), "ID 를 넣으면 경고 고르기 화면")

    sel = next(c for c in view.children if isinstance(c, M.UnwarnSelect))
    sel._values = [str(warns[0]["case"])]
    it2 = mod_inter()
    await sel.callback(it2)
    e = it2.response.edited or {}
    note("1건" in text_of(e.get("embed")) and "남은 유효 경고: **2회**" in text_of(e.get("embed")),
         "하나만 해제 · 남은 수 표시", text_of(e.get("embed"))[:80])

    print("\n  ⚠️ 같은 경고를 두 번 풀어도 한 번만 빠져야 합니다")
    done = await asyncio.gather(*(mdb.remove_warnings(GUILD.id, t.id, [warns[1]["case"]],
                                                      7, "a", "") for _ in range(5)))
    note(sum(len(d) for d in done) == 1, "동시에 다섯 번 → 한 번만 해제",
         str([len(d) for d in done]))
    note(len(await mdb.get_active_warnings(GUILD.id, t.id)) == 1, "남은 유효 경고 1")

    it3 = mod_inter()
    await view.clear_all.callback(it3)
    note(not await mdb.get_active_warnings(GUILD.id, t.id), "전체 해제")
    rec = await mdb.get_user_cases(GUILD.id, t.id)
    removed = [c for c in rec if c["kind"] == "warn" and not c.get("active")]
    note(len(removed) == 3 and all(c.get("removed_by_name") for c in removed),
         "지우지 않고 '누가 풀었는지' 까지 남김")
    note(any(c["kind"] == "unwarn" for c in rec), "해제도 케이스로 기록")

    print("\n  밴 해제 — 원래 밴 사유를 보여 줍니다")
    banned = member(8402, "밴후해제")
    await M.Moderation.ban.callback(cog, mod_inter(), banned, "원래사유", 0)
    it = mod_inter()
    modal = M.UnbanModal()
    modal.user_input._value = str(banned.id)
    modal.reason_input._value = "반성"
    await modal.on_submit(it)
    e = last_embed(it)
    note(banned.id not in GUILD.bans and "원래사유" in text_of(e), "밴 해제 + 원래 사유", e.title)

    it = mod_inter()
    modal = M.UnbanModal()
    modal.user_input._value = str(banned.id)
    modal.reason_input._value = ""
    await modal.on_submit(it)
    note("밴 상태가 아닙니다" in text_of(last_embed(it)), "밴이 아니면 그렇다고 알림")

    GUILD.bans[BASE + 8403999] = "우클릭 밴"
    it = mod_inter()
    embed = await M.do_unban(it, BASE + 8403999, "")
    note("우클릭 밴" in text_of(embed), "봇 밖에서 한 밴도 디스코드의 사유로 보여 줌")


async def test_search():
    print("\n=== 5. 유저 검색 ===")
    cog = M.Moderation(None)
    t = member(8501, "검색대상닉")
    await M.Moderation.warn.callback(cog, mod_inter(), t, "검색용 사유")

    it = mod_inter()
    modal = M.SearchModal()
    modal.user_input._value = f"<@{t.id}>"
    await modal.on_submit(it)
    e = last_embed(it)
    note(e and "검색용 사유" in text_of(e) and "유효 경고 **1회**" in e.description,
         "멘션으로 찾으면 사유가 보임")
    v = it.followup.sent[-1].get("view")
    note(isinstance(v, M.UserRecordView) and not v.unwarn_btn.disabled and v.unban_btn.disabled,
         "경고가 있으면 해제 버튼 켜짐, 밴이 아니면 밴 해제 꺼짐")

    it = mod_inter()
    modal = M.SearchModal()
    modal.user_input._value = "검색대상"
    await modal.on_submit(it)
    note("검색용 사유" in text_of(last_embed(it)), "이름 일부로도 찾음")

    print("\n  서버를 나간 사람은 기록의 이름으로 찾습니다")
    left = member(8502, "떠난사람")
    await M.Moderation.ban.callback(cog, mod_inter(), left, "나가기전밴", 0)
    GUILD.members.remove(left)
    it = mod_inter()
    modal = M.SearchModal()
    modal.user_input._value = "떠난"
    await modal.on_submit(it)
    e = last_embed(it)
    note(e and "나가기전밴" in text_of(e) and "현재 밴 상태" in e.description,
         "나간 사람도 이름으로 찾고 밴 사유가 보임")

    print("\n  ⚠️ 정규식 특수문자는 글자 그대로 (기록 통째로 덤프 방지)")
    hits = await mdb.find_users_by_name(GUILD.id, ".*")
    note(hits == [], "`.*` 로는 아무도 안 나옴", str(hits[:3]))

    print("\n  여러 명이 걸리면 고르게 합니다")
    member(8503, "동명이인A")
    member(8504, "동명이인B")
    it = mod_inter()
    modal = M.SearchModal()
    modal.user_input._value = "동명이인"
    await modal.on_submit(it)
    pv = it.followup.sent[-1].get("view")
    note(isinstance(pv, M.PickUserView), "후보 고르기 화면")
    sel = pv.children[0]
    sel._values = [str(BASE + 8504)]
    it2 = mod_inter()
    await sel.callback(it2)
    note("동명이인B" in text_of((it2.response.edited or {}).get("embed")),
         "고른 사람의 기록으로 바뀜")

    it = mod_inter()
    modal = M.SearchModal()
    modal.user_input._value = "없는사람이름xyz"
    await modal.on_submit(it)
    note("찾지 못했습니다" in (it.followup.sent[-1].get("content") or ""), "없으면 없다고")

    print("\n  경고가 아주 많아도 임베드가 깨지면 안 됩니다 (1024 · 6000자)")
    heavy = member(8505, "경고폭탄")
    for i in range(40):
        await mdb.add_warning(GUILD.id, heavy.id, "경고폭탄", 7, "관리자", "매우 긴 사유 " * 30)
    await mdb.remove_warnings(GUILD.id, heavy.id, None, 7, "관리자", "정리 " * 50)
    for i in range(12):
        await mdb.add_warning(GUILD.id, heavy.id, "경고폭탄", 7, "관리자", "또 " * 200)
    e = await M.record_embed_for(GUILD, heavy)
    note(all(len(f.value) <= 1024 for f in e.fields) and len(e) <= 6000 and len(e.fields) <= 25,
         "칸 1024 · 합계 6000 · 25칸 이내", f"{len(e)}자 / {len(e.fields)}칸")
    note(_re_search(r"40건 중 최근 \d+건", text_of(e)), "몇 건 중 몇 건인지 적음 (분모)")


async def test_panel():
    print("\n=== 6. /경고설정 패널 ===")
    cog = M.Moderation(None)
    it = Inter()
    await M.Moderation.settings_panel.callback(cog, it)
    sent = it.followup.sent[-1]
    note(isinstance(sent.get("view"), M.ModPanelView) and sent.get("ephemeral") is True,
         "패널은 본인에게만")
    view = M.ModPanelView()
    ids = sorted(b.custom_id for b in view.children)
    note(len(ids) == 6 and all(i.startswith("mod:panel:") for i in ids),
         "버튼 6개, 전부 고정 custom_id (재시작 후에도 동작)", str(ids))

    expect = {"mod:panel:unwarn": "modal:UnwarnModal", "mod:panel:unban": "modal:UnbanModal",
              "mod:panel:search": "modal:SearchModal", "mod:panel:dm": "followup",
              "mod:panel:log": "응답", "mod:panel:auto": "followup"}
    for b in view.children:
        it = Inter()
        await b.callback(it)
        note(F.outcome(it) == expect[b.custom_id], f"[{b.label}] 동작", F.outcome(it))

    print("\n  권한 없는 사람이 눌러도 막혀야 합니다 (패널이 어디 남아 있든)")
    for b in view.children:
        it = Inter(uid=8, admin=False)
        it.user.guild_permissions = Perms(administrator=False, moderate_members=False,
                                          ban_members=False)
        await b.callback(it)
        body = (it.response.sent[-1].get("content") if it.response.sent else "") or ""
        note("권한이 있어야" in body and not it.response.modal, f"[{b.label}] 거부")


async def test_settings_ui():
    print("\n=== 7. 밴 DM · 로그 채널 · 자동 처벌 설정 ===")
    s = await mdb.get_mod_settings(GUILD.id)
    modal = M.BanDmModal(s)
    modal.link_input._value = "discord.gg/abc"
    modal.msg_input._value = ""
    it = Inter()
    await modal.on_submit(it)
    note("https://" in (it.response.sent[-1].get("content") or ""), "https 없는 링크는 거부")

    modal = M.BanDmModal(s)
    modal.link_input._value = "https://discord.gg/newlink"
    modal.msg_input._value = "문의는 여기로"
    it = Inter()
    await modal.on_submit(it)
    s2 = await mdb.get_mod_settings(GUILD.id)
    note(s2["support_link"] == "https://discord.gg/newlink" and s2["ban_dm_message"] == "문의는 여기로",
         "링크·문구 저장")

    v = M.BanDmView(s2)
    it = Inter()
    await v.ban_toggle.callback(it)
    note((await mdb.get_mod_settings(GUILD.id))["ban_dm"] is (not s2["ban_dm"]), "밴 DM 토글")
    await v.ban_toggle.callback(Inter())
    it = Inter()
    await v.preview_btn.callback(it)
    pe = it.followup.sent[-1].get("embed")
    note(pe and "newlink" in text_of(pe) and it.followup.sent[-1].get("ephemeral"),
         "미리보기는 본인 화면에 (실제 DM 아님)")

    sel = M.LogChannelSelect()
    sel._values = [GUILD.channels[1]]
    it = Inter()
    await sel.callback(it)
    note((await mdb.get_mod_settings(GUILD.id))["log_channel_id"] == GUILD.channels[1].id,
         "로그 채널 저장")

    print("\n  자동 처벌 규칙")
    for cnt, act, mins, ok in (("0", "밴", "", False), ("3", "폭파", "", False),
                               ("3", "타임아웃", "999999", False), ("3", "타임아웃", "30", True),
                               ("3", "킥", "", True), ("5", "밴", "", True)):
        m = M.AutoRuleModal()
        m.count_input._value, m.action_input._value, m.minutes_input._value = cnt, act, mins
        it = Inter()
        await m.on_submit(it)
        rejected = bool(it.response.sent) and "⚠️" in (it.response.sent[-1].get("content") or "")
        note(rejected != ok, f"{cnt}회 {act} {mins} → {'저장' if ok else '거부'}")
    rules = (await mdb.get_mod_settings(GUILD.id))["auto_rules"]
    note(rules == [{"count": 3, "action": "kick"}, {"count": 5, "action": "ban"}],
         "같은 횟수는 바꿔 끼움 · 횟수 순 정렬", str(rules))
    dsel = M.AutoRuleDeleteSelect(rules)
    dsel._values = ["3"]
    await dsel.callback(Inter())
    note((await mdb.get_mod_settings(GUILD.id))["auto_rules"] == [{"count": 5, "action": "ban"}],
         "규칙 삭제")
    await mdb.save_mod_settings(GUILD.id, {"auto_rules": [], "log_channel_id": LOG.id})


async def test_prefix():
    print("\n=== 8. !warn · !ban · !warnings ===")
    src = open(os.path.join(PROJ, "main.py"), encoding="utf-8").read()
    note("command_prefix=prefix_for" in src and set(mdb.PREFIXES) >= {"!", "ㅂ!", "q!"},
         "접두사 ! · ㅂ! · q! 를 셋 다 받음")
    bot = commands.Bot(command_prefix="!", intents=discord.Intents.all())
    await bot.load_extension("cogs.moderation")
    names = {c.name for c in bot.commands}
    note({"warn", "ban", "warnings"} <= names, "!명령어 3개 등록", str(sorted(names)))
    cog = bot.get_cog("Moderation")
    moder = Member(7, "관리자", True, roles=[MOD_ROLE], guild=GUILD)

    t = member(8601, "접두사대상")
    ctx = Ctx(moder)
    sayu = "채팅 도배"
    await cog.warn_prefix.callback(cog, ctx, t, 사유=sayu)
    e = ctx.replies[-1]["embed"]
    note("경고 | 케이스" in e.title and sayu in e.description, "!warn → 경고", e.title)
    note(t.dms and ctx.replies[-1]["allowed_mentions"].users is False,
         "대상 DM · 채널 답장은 아무도 호출 안 함")

    ctx = Ctx(moder)
    await cog.warnings_prefix.callback(cog, ctx, t)
    note(sayu in text_of(ctx.replies[-1]["embed"]), "!warnings → 사유가 보임")

    print("\n  !warnings 에 경고·밴을 받은 **시각**이 보여야 합니다")
    import re as _re, datetime as _dt
    tt = member(8603, "시각대상")
    await cog.warn_prefix.callback(cog, Ctx(moder), tt, 사유="시각확인경고")
    await cog.ban_prefix.callback(cog, Ctx(moder), tt, 사유="시각확인밴")
    await mdb.remove_warnings(GUILD.id, tt.id, None, 7, "관리자", "풀어줌")
    ctx = Ctx(moder)
    await cog.warnings_prefix.callback(cog, ctx, tt)
    body = text_of(ctx.replies[-1]["embed"])
    stamps = [int(x) for x in _re.findall(r"<t:(\d+):f>", body)]
    now = _dt.datetime.now(_dt.timezone.utc).timestamp()
    note(len(stamps) >= 3, "해제된 경고 · 밴 · 밴 사유 칸에 시각", f"{len(stamps)}개")
    note(all(abs(now - x) < 300 for x in stamps), "시각이 실제 처리 시각과 맞음 (UTC 어긋남 없음)",
         str([int(now - x) for x in stamps]))
    note("해제 <t:" in body, "해제된 경고는 해제한 시각도 보임")
    note("시간대로 표시" in ctx.replies[-1]["embed"].footer.text, "시간대 안내")

    print("\n  Mongo 는 tz 없는 UTC, JSON 은 문자열 — 둘 다 같은 시각이어야 합니다")
    naive = {"created_at": _dt.datetime(2026, 9, 25, 5, 3), "created": "2026-09-25 14:03"}
    iso = {"created_at": "2026-09-25T05:03:00+00:00", "created": "2026-09-25 14:03"}
    only = {"created": "2026-09-25 14:03"}
    want = int(_dt.datetime(2026, 9, 25, 5, 3, tzinfo=_dt.timezone.utc).timestamp())
    note(M._case_ts(naive) == M._case_ts(iso) == M._case_ts(only) == want,
         "세 형태가 전부 KST 14:03 = UTC 05:03",
         str([M._case_ts(naive), M._case_ts(iso), M._case_ts(only), want]))
    note(M.when({}) == "? KST", "시각이 없으면 깨지지 않고 ? 로")

    ctx = Ctx(moder)
    await cog.ban_prefix.callback(cog, ctx, t, 사유="!로 밴")
    note(t.id in GUILD.bans and "밴 |" in ctx.replies[-1]["embed"].title, "!ban → 밴 + DM",
         ctx.replies[-1]["embed"].title)

    print("\n  ⚠️ 슬래시와 같은 검사를 거쳐야 합니다 (한쪽으로 우회 금지)")
    boss = member(8602, "높은사람3", [HIGH_ROLE])
    ctx = Ctx(moder)
    await cog.ban_prefix.callback(cog, ctx, boss, 사유="x")
    note(boss.id not in GUILD.bans and not boss.dms, "!ban 도 역할이 높으면 거부")

    print("\n  오류는 무엇이 틀렸는지 알려 줍니다")
    param = type("P", (), {"name": "사유", "displayed_name": "사유"})()
    for err, expect in ((commands.CommandNotFound("x"), None),
                        (commands.MissingPermissions(["ban_members"]), "권한이 없습니다"),
                        (commands.MissingRequiredArgument(param), "사용법"),
                        (commands.MemberNotFound("abc"), "유저 ID")):
        ctx = Ctx(moder, bot.get_command("warn"))
        await cog.on_command_error(ctx, err)
        got = ctx.replies[-1]["content"] if ctx.replies else None
        ok = (got is None) if expect is None else (expect in (got or ""))
        note(ok, f"{type(err).__name__} → {'조용히' if expect is None else expect}", str(got)[:50])
    await bot.close()


async def test_mongo():
    print("\n=== 9. Mongo 경로 (가짜 드라이버) · 동시성 ===")
    import fakemongo as FM
    FM.install(mdb, ["mod_settings_col", "mod_cases_col", "mod_users_col"])
    mdb._AFTER = FM.AFTER
    leaks = []
    import builtins
    real = builtins.print

    def watch(*a, **k):
        msg = " ".join(str(x) for x in a)
        if "using local file" in msg:
            leaks.append(msg)
        real(*a, **k)
    builtins.print = watch
    try:
        G = 77
        await mdb.save_mod_settings(G, {"auto_rules": [{"count": 3, "action": "ban"}]})
        res = await asyncio.gather(*(mdb.add_warning(G, 900, "n", 1, "m", f"r{i}")
                                     for i in range(6)))
        counts = sorted(c for _, c in res)
        cases = sorted(d["case"] for d, _ in res)
        note(counts == [1, 2, 3, 4, 5, 6], "동시에 6번 경고 → 누적 1~6 이 한 번씩 ($inc 반환값)",
             str(counts))
        note(cases == [1, 2, 3, 4, 5, 6], "케이스 번호가 겹치지 않음", str(cases))
        rules = (await mdb.get_mod_settings(G))["auto_rules"]
        hits = [c for c in counts if M.match_auto_rule(rules, c)]
        note(hits == [3], "'3회 규칙' 은 정확히 한 번 걸림 (건너뛰지도, 두 번 걸리지도 않음)",
             str(hits))
        await mdb.save_mod_settings(G, {"log_channel_id": 5})
        s = await mdb.get_mod_settings(G)
        note(s["auto_rules"] and s["log_channel_id"] == 5,
             "설정은 바뀐 키만 저장 (다른 설정이 지워지지 않음)")
        c7 = await mdb.add_case(G, "ban", 900, "n", 1, "m", "x")
        note(c7["case"] == 7, "설정을 저장해도 케이스 번호가 되돌아가지 않음", str(c7["case"]))
        done = await asyncio.gather(*(mdb.remove_warnings(G, 900, None, 1, "m", "")
                                      for _ in range(4)))
        note(sum(len(d) for d in done) == 6, "동시에 전체 해제 네 번 → 정확히 6건",
             str([len(d) for d in done]))
        note(await mdb._bump_active(G, 900, 0) == 0, "유효 경고 수가 0 으로 맞음")
        found = await mdb.find_users_by_name(G, "n")
        note(found == [("900", "n")], "이름 검색 ($regex)", str(found))
    finally:
        builtins.print = real
    note(not leaks, "Mongo 경로가 JSON 으로 새지 않음", f"{len(leaks)}건")


async def test_ban_delete():
    print("\n=== 10. 밴하면 그 유저의 최근 메시지도 지움 (2026-10-01 요청) ===")
    # 7일이 디스코드 상한 — 밴 API 의 delete_message_seconds 로 지웁니다
    DAY = 86400
    moder = Member(7, "운영자", False, roles=[MOD_ROLE], guild=GUILD)
    GUILD.ban_fail = None

    s = await mdb.get_mod_settings(GUILD.id)
    note(s.get("ban_delete_days") == 7, "기본값은 7일 (디스코드 최대)", str(s.get("ban_delete_days")))

    t1 = member(9101, "지울사람1")
    e = await M.do_ban(GUILD, moder, t1, "도배")
    note(GUILD.ban_delete.get(t1.id) == 7 * DAY, "**!ban · 기본 → 최근 7일 메시지 삭제 요청**",
         str(GUILD.ban_delete.get(t1.id)))
    note("최근 **7일**" in text_of(e), "결과에 몇 일 치를 지웠는지 적음")

    t2 = member(9102, "남길사람")
    it = mod_inter()
    await M.Moderation.ban.callback(M.Moderation(None), it, t2, "x", 0)
    note(GUILD.ban_delete.get(t2.id) == 0 and "지우지 않았습니다" in text_of(last_embed(it)),
         "/밴 메시지삭제:0 → 지우지 않음 (적은 값이 우선)")

    t3 = member(9103, "기본슬래시")
    it = mod_inter()
    await M.Moderation.ban.callback(M.Moderation(None), it, t3, "x")
    note(GUILD.ban_delete.get(t3.id) == 7 * DAY, "/밴 에서 비워 두면 설정값(7일)")

    print("\n  설정 화면에서 바꾸기")
    sel = M.BanDeleteSelect(await mdb.get_mod_settings(GUILD.id))
    note([o.value for o in sel.options] == ["0", "1", "3", "7"]
         and next(o for o in sel.options if o.default).value == "7",
         "선택지 0·1·3·7일, 지금 값(7)이 선택돼 있음")
    sel._values = ["3"]
    it = Inter(uid=7, roles=[MOD_ROLE])
    await sel.callback(it)
    s = await mdb.get_mod_settings(GUILD.id)
    note(s.get("ban_delete_days") == 3 and it.response.edited, "3일로 저장 · 화면 갱신")
    it = Inter(uid=8, admin=False, roles=[LOW_ROLE])
    sel2 = M.BanDeleteSelect(s)
    sel2._values = ["0"]
    await sel2.callback(it)
    note((await mdb.get_mod_settings(GUILD.id)).get("ban_delete_days") == 3, "관리자가 아니면 못 바꿈")

    t4 = member(9104, "접두사밴")
    cog = M.Moderation(None)
    ctx = Ctx(moder)
    await cog.ban_prefix.callback(cog, ctx, t4, 사유="x")
    note(GUILD.ban_delete.get(t4.id) == 3 * DAY, "!ban 도 바뀐 설정(3일)을 따름")

    t5 = member(9105, "자동밴")
    await M.apply_auto_rule(GUILD, t5, {"count": 1, "action": "ban"}, 1, "누적",
                            await mdb.get_mod_settings(GUILD.id))
    note(GUILD.ban_delete.get(t5.id) == 3 * DAY, "경고 누적 **자동 밴**도 설정을 따름",
         str(GUILD.ban_delete.get(t5.id)))

    note(M.ban_delete_days({"ban_delete_days": 30}) == 7
         and M.ban_delete_days({"ban_delete_days": "x"}) == 7
         and M.ban_delete_days({}, 0) == 0 and M.ban_delete_days({"ban_delete_days": -2}) == 0,
         "손으로 고친 이상한 값은 0~7 로 자름")
    emb = M.build_dm_settings_embed(await mdb.get_mod_settings(GUILD.id))
    note("최근 **3일**" in text_of(emb) and "디스코드 상한" in text_of(emb),
         "밴 DM 설정 화면에 현재 값과 7일 상한 안내")
    note("최근 3일" in text_of(M.build_panel_embed(GUILD, await mdb.get_mod_settings(GUILD.id))),
         "/경고설정 첫 화면에도 표시")
    await mdb.save_mod_settings(GUILD.id, {"ban_delete_days": 7})


async def main():
    GUILD.me.guild_permissions = Perms()
    await test_warn()
    await test_auto()
    await test_ban()
    await test_unwarn_unban()
    await test_search()
    await test_panel()
    await test_settings_ui()
    await test_prefix()
    await test_ban_delete()
    await test_mongo()
    await drain()


asyncio.run(main())
print(f"\n  실패 {len(F.FAILS)}건 / 성공 {len(F.OKS)}건")
for x in F.FAILS:
    print("   -", x)
sys.exit(1 if F.FAILS else 0)
