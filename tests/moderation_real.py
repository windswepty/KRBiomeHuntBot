"""경고 · 밴 — 가짜로는 못 보는 것들을 **진짜 discord.py** 로 봅니다.

tests/moderation.py 는 콜백을 직접 부릅니다. 그래서 다음은 거기서 안 보입니다.

  1. 디스코드 API 상한 — 모달 제목·라벨 45자, 선택지 100자·25개, 임베드
     칸 1024자·합계 6000자. 하나라도 넘으면 **메시지·모달 전송 자체가
     실패**해 유저에게는 '상호작용 실패' 만 뜹니다. 로컬에서는 멀쩡합니다.
  2. 슬래시 명령어 동기화 페이로드 — 하나가 규격을 어기면 sync 가 통째로
     실패해 **모든 명령어가** 갱신되지 않습니다.
  3. !명령어의 실제 경로 — 접두사 파싱 · 나머지 인자(`*, 사유`) · 변환기 ·
     권한 검사 · 오류 리스너 · 답장 페이로드. 콜백을 직접 부르면 이 전부를
     건너뜁니다.
  4. 정밀 점검에서 나온 버그들의 회귀 검사.
"""
import sys, os, tempfile, asyncio, re, io, contextlib

PROJ = sys.argv[1]
sys.path.insert(0, PROJ)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(tempfile.mkdtemp())
os.environ["MONGO_URI"] = ""

import discord
from discord.ext import commands
import fakes as F
from fakes import note, GUILD, Inter, Role, Member, Perms

import cogs.moderation as M
import utils.mod_db as mdb

BASE = 100000000000000000        # 디스코드 ID 는 17~20자리입니다
MOD_ROLE = Role(6001, "운영진", 50)
LOW_ROLE = Role(6002, "일반", 10)
GUILD.me.guild_permissions = Perms()


def member(uid, name, roles=None):
    m = Member(BASE + uid, name, False, roles=roles if roles is not None else [LOW_ROLE],
               guild=GUILD)
    GUILD.members.append(m)
    return m


def text_of(e):
    if e is None:
        return ""
    return "\n".join([e.title or "", e.description or ""]
                     + [(f.name or "") + (f.value or "") for f in e.fields])


# ── 디스코드 규격 검사기 ──────────────────────────────────────────────
def embed_problems(e: discord.Embed) -> list:
    d, out = e.to_dict(), []
    if len(d.get("title", "")) > 256:
        out.append(f"제목 {len(d['title'])}자 > 256")
    if len(d.get("description", "")) > 4096:
        out.append(f"설명 {len(d['description'])}자 > 4096")
    fields = d.get("fields", [])
    if len(fields) > 25:
        out.append(f"칸 {len(fields)}개 > 25")
    for f in fields:
        if not f["name"] or len(f["name"]) > 256:
            out.append(f"칸 이름 길이 {len(f['name'])}")
        if not f["value"] or len(f["value"]) > 1024:
            out.append(f"칸 '{f['name'][:10]}' 값 {len(f['value'])}자")
    if len(d.get("footer", {}).get("text", "")) > 2048:
        out.append("바닥글 > 2048")
    if len(e) > 6000:
        out.append(f"합계 {len(e)}자 > 6000")
    return out


def component_problems(rows: list) -> list:
    out = []
    if len(rows) > 5:
        out.append(f"줄 {len(rows)}개 > 5")
    for row in rows:
        items = row.get("components", [])
        selects = [c for c in items if c["type"] in (3, 5, 6, 7, 8)]
        if selects and len(items) > 1:
            out.append("선택 메뉴와 다른 것이 한 줄에 있음")
        if len(items) > 5:
            out.append(f"한 줄에 {len(items)}개 > 5")
        for c in items:
            if c["type"] == 2:
                if len(c.get("label") or "") > 80:
                    out.append(f"버튼 라벨 {len(c['label'])}자 > 80")
                if c.get("style") == 5:
                    if not c.get("url") or len(c["url"]) > 512:
                        out.append("링크 버튼 URL 이 없거나 512자 초과")
                elif len(c.get("custom_id") or "") > 100 or not c.get("custom_id"):
                    out.append("버튼 custom_id 없음/초과")
            if c["type"] in (3, 8):
                if len(c.get("placeholder") or "") > 150:
                    out.append("선택 메뉴 안내문 > 150")
            if c["type"] == 3:
                opts = c.get("options", [])
                if not 1 <= len(opts) <= 25:
                    out.append(f"선택지 {len(opts)}개")
                for o in opts:
                    if not 1 <= len(o["label"]) <= 100:
                        out.append(f"선택지 라벨 {len(o['label'])}자")
                    if not 1 <= len(o["value"]) <= 100:
                        out.append("선택지 값 길이")
                    if len(o.get("description") or "") > 100:
                        out.append(f"선택지 설명 {len(o['description'])}자")
                mn, mx = c.get("min_values", 1), c.get("max_values", 1)
                if not (0 <= mn <= mx <= min(25, len(opts))):
                    out.append(f"min/max {mn}/{mx} (선택지 {len(opts)})")
    return out


def modal_problems(m: discord.ui.Modal) -> list:
    d, out = m.to_dict(), []
    if not 1 <= len(d["title"]) <= 45:
        out.append(f"모달 제목 {len(d['title'])}자 > 45")
    if not 1 <= len(d["components"]) <= 5:
        out.append(f"모달 줄 {len(d['components'])}개")
    for row in d["components"]:
        for c in row["components"]:
            if not 1 <= len(c.get("label") or "") <= 45:
                out.append(f"입력칸 라벨 '{c.get('label')}' {len(c.get('label') or '')}자 > 45")
            if len(c.get("placeholder") or "") > 100:
                out.append("입력칸 안내문 > 100")
            mx = c.get("max_length", 4000)
            if mx > 4000 or len(c.get("value") or "") > mx:
                out.append(f"입력칸 기본값 {len(c.get('value') or '')}자 > 최대 {mx}")
    return out


def check(label, problems):
    note(not problems, label, "; ".join(problems[:3]))


# ── 1. 디스코드 규격 (최대 입력으로) ──────────────────────────────────
async def test_limits():
    print("=== 1. 디스코드 규격 — 최대 길이 입력으로 전부 만들어 봅니다 ===")
    long_guild = "가" * 100                          # 서버 이름 상한
    old_name, GUILD.name = GUILD.name, long_guild
    s = {"ban_dm": True, "warn_dm": True, "log_channel_id": 1,
         "support_link": "https://discord.gg/" + "a" * 181,          # 입력칸 상한 200
         "ban_dm_message": "문" * 800,                                # 입력칸 상한 800
         "auto_rules": [{"count": 50 - i, "action": "timeout", "minutes": 40319}
                        for i in range(M.MAX_AUTO_RULES)]}
    reason = "사" * 500                                                # 명령어 상한 500
    auto_reason = f"경고 50회 누적 (자동) — 마지막 경고: {reason}"
    try:
        e, v = M.build_ban_dm(GUILD, auto_reason, s)
        check("밴 DM 임베드", embed_problems(e))
        check("밴 DM 링크 버튼", component_problems(v.to_components()))
        doc = {"case": 999999, "reason": reason, "created": "2026-09-25 14:03",
               "user_id": str(BASE), "user_name": "u" * 100, "mod_id": str(BASE),
               "mod_name": "m" * 100, "kind": "ban", "auto": True}
        check("경고 DM 임베드", embed_problems(M.build_warn_dm(
            GUILD, doc, 50, "누적 50회로 **타임아웃 27일 23시간 59분** 처리됩니다.")))
        check("로그 임베드", embed_problems(M.build_log_embed(
            dict(doc, reason="이" * 5000), [("원래 밴 사유", "x" * 5000), ("DM", "y" * 3000)])))
        check("패널 임베드 (규칙 25개)", embed_problems(M.build_panel_embed(GUILD, s)))
        s30 = dict(s, auto_rules=s["auto_rules"] + [{"count": 60 + i, "action": "timeout",
                                                     "minutes": 40319} for i in range(30)])
        check("패널 임베드 (손으로 고쳐 55개가 된 설정)", embed_problems(M.build_panel_embed(GUILD, s30)))
        check("자동 처벌 임베드", embed_problems(M.build_auto_embed(GUILD, s30)))
        check("밴 DM 설정 임베드", embed_problems(M.build_dm_settings_embed(s)))

        print("\n  기록이 아주 많은 사람의 검색 결과")
        cases = []
        for i in range(60):
            cases.append({"case": 1000 - i, "kind": "warn", "active": i % 2 == 0,
                          "reason": "경" * 1000, "mod_name": "m" * 100,
                          "created": "2026-09-25 14:03", "removed_at": "2026-09-25 15:00",
                          "removed_by_name": "r" * 100, "removed_reason": "해" * 500})
        for i in range(30):
            cases.append({"case": 500 - i, "kind": ("ban", "unban", "kick", "timeout")[i % 4],
                          "reason": "밴" * 1000, "mod_name": "m" * 100, "minutes": 40319,
                          "auto": True, "created": "2026-09-25 14:03"})
        cases.sort(key=lambda c: -c["case"])
        for st in ("banned", "clear", "unknown"):
            e = M.build_record_embed(GUILD, BASE + 1, "이름" * 60, cases, st, "디" * 600)
            check(f"처벌 기록 임베드 ({st})", embed_problems(e))

        warns = [{"case": 10**6 + i, "reason": "경" * 1000, "mod_name": "m" * 100,
                  "created": "2026-09-25 14:03", "created_at": None} for i in range(40)]
        check("경고 해제 선택 메뉴 (40건)", component_problems(
            M.UnwarnView(BASE, "이름", warns, "").to_components()))
        cands = [(BASE + i, "긴이름" * 70) for i in range(40)] + [(BASE + 99, "")]
        check("후보 고르기 메뉴 (40명 · 빈 이름 포함)", component_problems(
            M.PickUserView(cands, "search").to_components()))
        for label, view in (("관리 패널 버튼", M.ModPanelView()),
                            ("검색 결과 버튼", M.UserRecordView(BASE, True, True)),
                            ("밴 DM 설정 버튼", M.BanDmView(s)),
                            ("로그 채널 메뉴", M.LogChannelView()),
                            ("자동 처벌 메뉴 (규칙 25개)", M.AutoRuleView(s))):
            check(label, component_problems(view.to_components()))

        print("\n  모달 (기본값을 최대로 채워서)")
        for mod in (M.UnwarnModal(str(BASE)), M.UnbanModal(str(BASE)), M.SearchModal(),
                    M.BanDmModal(s), M.AutoRuleModal()):
            check(f"{type(mod).__name__}", modal_problems(mod))
    finally:
        GUILD.name = old_name


# ── 2. 슬래시 명령어 동기화 페이로드 ──────────────────────────────────
NAME_RE = re.compile(r"^[-_\w฀-๿]{1,32}$")


async def test_tree():
    print("\n=== 2. 슬래시 명령어 동기화 페이로드 (전체) ===")
    bot = commands.Bot(command_prefix="!", intents=discord.Intents.all())
    for f in sorted(os.listdir(os.path.join(PROJ, "cogs"))):
        if f.endswith(".py") and not f.startswith("__"):
            await bot.load_extension(f"cogs.{f[:-3]}")
    cmds = bot.tree.get_commands()
    payload = [c.to_dict(bot.tree) for c in cmds]
    bad = []
    for p in payload:
        if not NAME_RE.match(p["name"]) or p["name"] != p["name"].lower():
            bad.append(f"/{p['name']} 이름")
        if not 1 <= len(p.get("description", "")) <= 100:
            bad.append(f"/{p['name']} 설명 {len(p.get('description', ''))}자")
        opts = p.get("options", [])
        if len(opts) > 25:
            bad.append(f"/{p['name']} 인자 {len(opts)}개")
        seen_optional = False
        for o in opts:
            if not NAME_RE.match(o["name"]) or not 1 <= len(o.get("description", "")) <= 100:
                bad.append(f"/{p['name']} 인자 {o['name']}")
            if o.get("required"):
                if seen_optional:
                    bad.append(f"/{p['name']} 필수 인자가 선택 인자 뒤")
            else:
                seen_optional = True
    note(len(payload) <= 100, f"명령어 {len(payload)}개 (상한 100)")
    note(not bad, "모든 명령어가 디스코드 규격 안", "; ".join(bad[:4]))

    mine = {p["name"]: p for p in payload if p["name"] in ("경고", "밴", "경고설정")}
    note(len(mine) == 3, "경고 · 밴 · 경고설정 이 동기화 목록에 있음", str(sorted(mine)))
    perms = {k: int(v.get("default_member_permissions") or 0) for k, v in mine.items()}
    P = discord.Permissions
    note(perms.get("경고") == P(moderate_members=True).value
         and perms.get("밴") == P(ban_members=True).value
         and perms.get("경고설정") == P(administrator=True).value,
         "권한 없는 사람의 명령어 목록에서 숨겨짐 (default_member_permissions)", str(perms))
    note(all(v.get("contexts") == [0] or v.get("dm_permission") is False for v in mine.values()),
         "DM 에서는 안 보임 (guild_only)")
    ban_opts = {o["name"]: o for o in mine["밴"]["options"]}
    note(ban_opts["사유"].get("max_length") == 500 and ban_opts["메시지삭제"].get("max_value") == 7,
         "사유 500자 · 메시지삭제 0~7 이 디스코드에서 검사됨")
    await bot.close()


# ── 3. !명령어를 진짜 경로로 ──────────────────────────────────────────
class PermChannel(F.Channel):
    """권한 검사가 진짜 `discord.Permissions` 를 보게 합니다."""
    def permissions_for(self, m):
        if MOD_ROLE in getattr(m, "roles", []):
            return discord.Permissions(moderate_members=True, ban_members=True,
                                       send_messages=True)
        return discord.Permissions(send_messages=True, read_messages=True)


class Msg:
    _seq = 0

    def __init__(self, bot, content, author, channel):
        Msg._seq += 1
        self.id = BASE + 900000 + Msg._seq
        self.content, self.author, self.channel = content, author, channel
        self.guild = GUILD
        self.mentions, self.raw_mentions, self.role_mentions = [], [], []
        self.attachments, self.stickers = [], []
        self.webhook_id = None
        self._state = bot._connection

    def to_message_reference_dict(self):
        return {"message_id": self.id, "channel_id": self.channel.id,
                "guild_id": GUILD.id, "fail_if_not_exists": True}


async def test_prefix_real():
    print("\n=== 3. !명령어 — 진짜 discord.py 경로 (파싱 · 변환 · 권한 · 오류 · 답장) ===")
    src = io.open(os.path.join(PROJ, "main.py"), encoding="utf-8").read()
    note("command_prefix=prefix_for" in src and "help_command=None" in src
         and "case_insensitive=True" in src, "main.py 의 봇 설정을 그대로 씁니다")
    bot = commands.Bot(command_prefix=mdb.prefix_for, intents=discord.Intents.all(),
                       help_command=None, case_insensitive=True)
    await bot._async_setup_hook()          # 로그인 때 라이브러리가 하는 루프 연결
    await bot.load_extension("cogs.moderation")
    bot._connection.user = Member(2, "bot")

    sent = []

    async def http_send(channel_id, *, params):
        sent.append(params.payload)
        return {}
    bot.http.send_message = http_send
    bot._connection.create_message = lambda *, channel, data: data

    # 게이트웨이 없이는 캐시 밖 조회가 안 되므로, 서버 안 멤버 조회만 대신합니다
    async def q_by_id(self, bot_, guild, user_id):
        return guild.get_member(user_id)

    async def q_named(self, guild, argument):
        return next((m for m in guild.members if m.name == argument), None)
    commands.MemberConverter.query_member_by_id = q_by_id
    commands.MemberConverter.query_member_named = q_named
    GUILD.get_member_named = lambda n: None
    bot.get_user = lambda uid: GUILD.get_member(uid)

    async def fetch_user(uid):
        raise discord.NotFound(type("R", (), {"status": 404, "reason": ""})(), "Unknown User")
    bot.fetch_user = fetch_user

    ch = PermChannel(BASE + 777, "운영진채팅", GUILD)
    mod = Member(7, "운영자", False, roles=[MOD_ROLE], guild=GUILD)
    user = Member(8, "일반인", False, roles=[LOW_ROLE], guild=GUILD)
    t = member(7001, "접두사대상")

    async def run(content, author=mod):
        before = len(sent)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            await bot.process_commands(Msg(bot, content, author, ch))
            for _ in range(20):
                await asyncio.sleep(0.01)
        sys.stdout.write(buf.getvalue())
        return sent[before:], buf.getvalue()

    def embed_of(p):
        return discord.Embed.from_dict(p["embeds"][0]) if p.get("embeds") else None

    out, _ = await run(f"!warn {t.id} 채팅 도배 여러 단어 사유")
    e = embed_of(out[0]) if out else None
    note(e is not None and "경고 | 케이스" in e.title, "!warn ID 사유 → 경고", e.title if e else str(out))
    note(e is not None and "채팅 도배 여러 단어 사유" in e.description,
         "사유가 띄어쓰기째 전부 들어감 (나머지 인자)")
    p = out[0] if out else {}
    note(p.get("allowed_mentions") == {"parse": [], "replied_user": False},
         "답장이 아무도 호출하지 않음 (명령어 친 사람 포함)", str(p.get("allowed_mentions")))
    note((p.get("message_reference") or {}).get("message_id") is not None,
         "명령어 메시지에 답장으로 붙음")
    check("!warn 답장 임베드가 규격 안", embed_problems(e) if e else ["없음"])
    note(t.dms and "채팅 도배 여러 단어 사유" in text_of(t.dms[-1]["embed"]), "대상에게 DM")

    out, _ = await run(f"!WARN <@{t.id}> 대문자와 멘션")
    note(out and "케이스" in (embed_of(out[0]).title or ""), "!WARN (대문자) · 멘션도 됨")

    out, _ = await run(f"!warn {t.id}")
    c = (out[0].get("content") if out else "") or ""
    note("`사유` 을(를) 빠뜨렸습니다" in c and "!warn [유저 ID" in c,
         "사유를 빠뜨리면 무엇이 빠졌는지 + 사용법", c[:60])

    out, _ = await run("!warn 없는사람 사유")
    c = (out[0].get("content") if out else "") or ""
    note("유저를 찾지 못했습니다" in c, "없는 사람은 찾지 못했다고", c[:40])

    n_before = len(await mdb.get_active_warnings(GUILD.id, t.id))
    out, _ = await run(f"!warn {t.id} 권한없이", author=user)
    c = (out[0].get("content") if out else "") or ""
    note("권한이 없습니다" in c, "권한 없는 사람은 거부 (진짜 권한 검사)", c[:40])
    note(len(await mdb.get_active_warnings(GUILD.id, t.id)) == n_before, "거부됐으면 경고도 안 쌓임")

    out, _ = await run(f"!warnings {t.id}")
    e = embed_of(out[0]) if out else None
    note(e is not None and "채팅 도배 여러 단어 사유" in text_of(e) and "<t:" in text_of(e),
         "!warnings → 사유와 시각")
    check("!warnings 답장 임베드가 규격 안", embed_problems(e) if e else ["없음"])

    out, _ = await run("!warnings")
    note(out and "`유저` 을(를) 빠뜨렸습니다" in (out[0].get("content") or ""), "!warnings 인자 빠뜨림")

    out, _ = await run(f"!ban {t.id} 진짜 밴 사유")
    e = embed_of(out[0]) if out else None
    note(e is not None and "밴 |" in e.title and t.id in GUILD.bans, "!ban ID 사유 → 밴",
         e.title if e else str(out))
    note("진짜 밴 사유" in text_of(t.dms[-1]["embed"]), "밴 DM 에 사유")

    out, _ = await run(f"!ban {BASE + 55555} 없는계정")
    note(out and "유저를 찾지 못했습니다" in (out[0].get("content") or ""),
         "존재하지 않는 ID 는 찾지 못했다고")

    out, _ = await run(f"!ban {t.id} 한번더", author=user)
    note(out and "권한이 없습니다" in (out[0].get("content") or ""), "!ban 도 권한 검사")

    out, log = await run("!아무말이나 적은 채팅")
    note(not out and "Traceback" not in log and "Ignoring exception" not in log,
         "!로 시작하는 평범한 채팅은 조용히 무시")
    out, _ = await run("!help")
    note(not out, "기본 !help 는 꺼져 있음")
    out, _ = await run(f"?warn {t.id} Dyno 접두사")
    note(not out, "Dyno 의 ?warn 에는 반응하지 않음 (두 봇이 한꺼번에 답하지 않게)")

    print("\n  접두사 ! · ㅂ! · q! 셋 다 늘 동작 (다른 봇과 겹치면 다른 것으로 치면 됨)")
    for cmd in (f"ㅂ!warn {t.id} 한글 접두사", f"q!warn {t.id} 영문 접두사",
                f"Q!warn {t.id} 휴대폰 대문자", f"!warn {t.id} 기본 접두사"):
        out, _ = await run(cmd)
        note(out and "경고 | 케이스" in (embed_of(out[0]).title or ""), f"{cmd.split()[0]} 동작")
    out, _ = await run(f"q!WARNINGS {t.id}")
    note(out and embed_of(out[0]) is not None, "q!WARNINGS (명령어 대문자) 도 동작")
    out, _ = await run(f"ㅂ!warn {t.id}")
    note(out and "`ㅂ!warn [유저 ID" in (out[0].get("content") or ""),
         "사용법 안내는 친 접두사 그대로", (out[0].get("content") or "")[-40:] if out else "")
    print("\n  한글 자판 그대로 친 명령어 (q!warn → ㅂ!ㅈㅁ구, 2026-09-26 제보)")
    for cmd, label in ((f"ㅂ!ㅈㅁ구 {t.id} 한글자판", "ㅂ!ㅈㅁ구 (q!warn 을 한글 자판으로)"),
                       (f"q!ㅈㅁ구 {t.id} 중간에전환", "q!ㅈㅁ구 (접두사만 영문)"),
                       (f"ㅃ!ㅈㅁ구 {t.id} 쉬프트", "ㅃ!ㅈㅁ구 (Shift+q)"),
                       (f"!ㅈㅁ구 {t.id} 느낌표", "!ㅈㅁ구")):
        out, _ = await run(cmd)
        note(out and embed_of(out[0]) is not None and "경고 | 케이스" in (embed_of(out[0]).title or ""),
             f"{label} → 경고", str(out[0].get("content") if out else "무응답")[:50])
    out, _ = await run(f"ㅂ!ㅈㅁ구ㅑㅜㅎㄴ {t.id}")
    note(out and embed_of(out[0]) is not None, "ㅂ!ㅈㅁ구ㅑㅜㅎㄴ (warnings) → 기록")
    out, _ = await run(f"ㅂ!ㅈㅁ궇ㄴ {t.id}")
    note(not out, "ㅂ!ㅈㅁ궇ㄴ(= warngs, 오타) 는 다른 명령어로 오인하지 않음")
    out, _ = await run(f"ㅂ!ㅈㅁ구 {t.id} 권한없음", author=user)
    note(out and "권한이 없습니다" in (out[0].get("content") or ""),
         "한글 자판으로 쳐도 **권한 검사는 그대로**")
    out, _ = await run("ㅂ!ㅈㅁ구")
    note(out and "`ㅂ!warn [유저 ID" in (out[0].get("content") or ""),
         "인자를 빠뜨리면 사용법을 영문 이름으로 안내", (out[0].get("content") or "")[-30:] if out else "")
    for chat in ("!안녕하세요", "ㅂ!ㅋㅋㅋㅋ", "!ㅎㅇ"):
        out, log = await run(chat)
        note(not out and "Traceback" not in log, f"'{chat}' 같은 평범한 채팅은 여전히 조용히 무시")

    for other in (f"/warn {t.id} x", f"ㅂwarn {t.id} x", f"q warn {t.id} x"):
        out, _ = await run(other)
        note(not out, f"'{other.split()[0]}' 같은 다른 형태에는 반응 안 함")

    calls = []
    real_get = mdb.get_mod_settings

    async def counting(gid):
        calls.append(gid)
        return await real_get(gid)
    mdb.get_mod_settings = counting
    try:
        for _ in range(50):
            await bot.process_commands(Msg(bot, "그냥 채팅", mod, ch))
    finally:
        mdb.get_mod_settings = real_get
    note(not calls, "평범한 채팅 50줄에 DB 조회 0번 (접두사 판정은 DB 를 안 읽음)", f"{len(calls)}번")

    botuser = Member(BASE + 3, "다른봇", False, guild=GUILD)
    botuser.bot = True
    out, _ = await run(f"!warn {t.id} 봇이보냄", author=botuser)
    note(not out, "다른 봇의 !명령어는 무시")

    print("\n  예상 못한 오류는 원인을 로그에 남기고 사람에게는 짧게")
    # ⚠️ load_extension 은 모듈을 새로 만듭니다(LESSONS). 위에서 import 한 M 을
    #    고치면 봇이 실제로 쓰는 모듈에는 닿지 않아 검사가 헛돕니다.
    LM = sys.modules["cogs.moderation"]
    real = LM.do_warn

    async def boom(*a, **k):
        raise KeyError("일부러 낸 오류")
    LM.do_warn = boom
    try:
        out, log = await run(f"!warn {BASE + 7001} x")
    finally:
        LM.do_warn = real
    note(out and "처리 중 오류" in (out[0].get("content") or ""), "사람에게는 짧은 안내")
    note("KeyError" in log and "일부러 낸 오류" in log, "로그에는 감싼 예외가 아니라 원인",
         log.strip().splitlines()[-1][:80] if log.strip() else "")
    await bot.close()


# ── 4. 정밀 점검에서 나온 버그들 ─────────────────────────────────────
async def test_regressions():
    print("\n=== 4. 정밀 점검에서 고친 것들 (회귀 방지) ===")
    mod = Member(7, "운영자", False, roles=[MOD_ROLE], guild=GUILD)

    print("\n  ① 자동 밴인데 밴 DM 이 꺼져 있으면 경고 DM 이라도 가야 합니다")
    await mdb.save_mod_settings(GUILD.id, {"auto_rules": [{"count": 1, "action": "ban"}],
                                           "ban_dm": False, "warn_dm": True})
    t = member(7101, "밴DM꺼짐")
    e = await M.do_warn(GUILD, mod, t, "첫경고")
    note(t.id in GUILD.bans, "자동 밴 실행")
    note(len(t.dms) == 1 and "경고" in t.dms[0]["embed"].title
         and "밴" in text_of(t.dms[0]["embed"]),
         "경고 DM 이 갔고 '밴 처리됩니다' 가 적힘", str(len(t.dms)))
    note("DM 전송됨" in e.description, "결과에 DM 전송 여부가 보임")

    await mdb.save_mod_settings(GUILD.id, {"ban_dm": True})
    t = member(7102, "밴DM켜짐")
    await M.do_warn(GUILD, mod, t, "첫경고")
    note(len(t.dms) == 1 and "차단" in t.dms[0]["embed"].title,
         "밴 DM 이 켜져 있으면 밴 DM 한 통만 (중복 없음)")

    print("\n  ② 자동 밴이 막히면(봇보다 높음) 경고 DM 이 가고 '곧 밴' 이라고 하지 않습니다")
    top = Role(6100, "봇보다위", 150)
    mod_top = Member(7, "최상위", False, roles=[Role(6101, "최상위운영", 200)], guild=GUILD)
    t = member(7103, "막힘", [top])
    e = await M.do_warn(GUILD, mod_top, t, "첫경고")
    note(t.id not in GUILD.bans and "자동 밴 실패" in e.description, "밴은 안 되고 실패를 알림")
    note(len(t.dms) == 1 and "경고" in t.dms[0]["embed"].title
         and "자동 처벌" not in text_of(t.dms[0]["embed"]),
         "경고 DM 은 갔고, 되지도 않을 처벌을 예고하지 않음")

    print("\n  ③ 관리자에게 자동 타임아웃 → 이유를 정확히")
    await mdb.save_mod_settings(GUILD.id, {"auto_rules": [{"count": 1, "action": "timeout",
                                                          "minutes": 10}]})
    adm = member(7104, "관리자멤버")
    adm.guild_permissions = Perms(administrator=True)
    e = await M.do_warn(GUILD, mod, adm, "x")
    note("관리자 권한이 있는 멤버" in e.description and not getattr(adm, "timed_out", None),
         "관리자는 타임아웃 불가를 그대로 알림", e.description[-60:])
    await mdb.save_mod_settings(GUILD.id, {"auto_rules": []})

    print("\n  ④ 풀렸다가 봇 밖에서 다시 걸린 밴 → 예전 사유를 보여 주면 안 됩니다")
    t = member(7105, "재밴")
    await M.do_ban(GUILD, mod, t, "옛날사유")
    it = Inter(uid=7, roles=[MOD_ROLE])
    await M.do_unban(it, t.id, "풀어줌")
    GUILD.bans[t.id] = "우클릭 새 사유"               # 봇 밖에서 다시 밴
    e = await M.record_embed_for(GUILD, t)
    f = next((x for x in e.fields if "밴 사유" in x.name), None)
    note(f is not None and "우클릭 새 사유" in f.value and "옛날사유" not in f.value,
         "검색: 지금 밴의 사유", f.value[:40] if f else "")
    e2 = await M.do_unban(it, t.id, "")
    note("우클릭 새 사유" in text_of(e2) and "옛날사유" not in text_of(e2),
         "밴 해제: 원래 사유도 지금 밴의 것")

    print("\n  ⑤ 해제 기록에 화면용 이름('(서버에 없음)')이 들어가면 안 됩니다")
    t = member(7106, "떠날사람")
    await M.do_warn(GUILD, mod, t, "경고하나")
    GUILD.members.remove(t)
    it = Inter(uid=7, roles=[MOD_ROLE])
    await M._finish_unwarn(it, t.id, "떠날사람 (서버에 없음)", None, "")
    cases = await mdb.get_user_cases(GUILD.id, t.id)
    note(all("(서버에 없음)" not in c["user_name"] for c in cases),
         "기록에는 사용자명만", str([c["user_name"] for c in cases]))
    names = await M.resolve_users(GUILD, "떠날")
    note(names == [(t.id, "떠날사람 (서버에 없음)")], "이름 검색이 꾸밈을 겹쳐 붙이지 않음",
         str(names))

    print("\n  ⑥ 자동 처벌 규칙은 25개까지 (지우기 메뉴에 다 들어가야 합니다)")
    await mdb.save_mod_settings(GUILD.id, {"auto_rules": [
        {"count": i, "action": "kick"} for i in range(1, M.MAX_AUTO_RULES + 1)]})
    m_ = M.AutoRuleModal()
    m_.count_input._value, m_.action_input._value, m_.minutes_input._value = "40", "밴", ""
    it = Inter()
    await m_.on_submit(it)
    rules = (await mdb.get_mod_settings(GUILD.id))["auto_rules"]
    note(len(rules) == M.MAX_AUTO_RULES and "최대" in (it.followup.sent[-1].get("content") or ""),
         "26번째 새 규칙은 거부", str(len(rules)))
    m_ = M.AutoRuleModal()
    m_.count_input._value, m_.action_input._value, m_.minutes_input._value = "3", "밴", ""
    it = Inter()
    await m_.on_submit(it)
    rules = (await mdb.get_mod_settings(GUILD.id))["auto_rules"]
    note(len(rules) == M.MAX_AUTO_RULES and {"count": 3, "action": "ban"} in rules,
         "꽉 차도 같은 횟수를 바꾸는 것은 됨")
    await mdb.save_mod_settings(GUILD.id, {"auto_rules": []})

    print("\n  ⑦ 기록이 200건을 넘는 사람도 '전체 해제' 가 전부 풉니다")
    t = member(7107, "기록많음")
    for i in range(210):
        await mdb.add_warning(GUILD.id, t.id, "기록많음", 7, "운영자", f"w{i}")
    note(len(await mdb.get_active_warnings(GUILD.id, t.id)) == 210, "유효 경고 210건 전부 보임")
    done = await mdb.remove_warnings(GUILD.id, t.id, None, 7, "운영자", "정리")
    note(len(done) == 210 and not await mdb.get_active_warnings(GUILD.id, t.id),
         "전체 해제 → 210건 전부", str(len(done)))
    note(await mdb._bump_active(GUILD.id, t.id, 0) == 0, "유효 경고 수도 0")

    print("\n  ⑦-2 같은 것을 Mongo 경로로")
    import fakemongo as FM
    saved = (mdb.mod_settings_col, mdb.mod_cases_col, mdb.mod_users_col, mdb._AFTER)
    FM.install(mdb, ["mod_settings_col", "mod_cases_col", "mod_users_col"])
    mdb._AFTER = FM.AFTER
    leaks = []
    import builtins
    real_print = builtins.print

    def watch(*a, **k):
        if "using local file" in " ".join(str(x) for x in a):
            leaks.append(a)
        real_print(*a, **k)
    builtins.print = watch
    try:
        for i in range(210):
            await mdb.add_warning(88, 5, "n", 7, "m", f"w{i}")
        note(len(await mdb.get_active_warnings(88, 5)) == 210, "Mongo: 유효 경고 210건")
        done = await mdb.remove_warnings(88, 5, None, 7, "m", "")
        note(len(done) == 210, "Mongo: 전체 해제 210건", str(len(done)))
    finally:
        builtins.print = real_print
        (mdb.mod_settings_col, mdb.mod_cases_col, mdb.mod_users_col, mdb._AFTER) = saved
    note(not leaks, "Mongo 경로가 JSON 으로 새지 않음", f"{len(leaks)}건")


def test_server_label():
    print("\n=== 5. DM 제목의 서버 이름 ===")
    note(M.server_label("한바헌") == "한바헌 서버", "보통 이름 → 'OO 서버'")
    note(M.server_label("테스트 서버") == "테스트 서버", "'서버' 로 끝나면 겹쳐 붙이지 않음")
    e, _ = M.build_ban_dm(type("G", (), {"name": "바이옴 헌터 서버", "icon": None})(), "x", {})
    note(e.title == "🔨 바이옴 헌터 서버에서 차단되었습니다", "밴 DM 제목", e.title)


async def main():
    test_server_label()
    await test_limits()
    await test_tree()
    await test_prefix_real()
    await test_regressions()


asyncio.run(main())
print(f"\n  실패 {len(F.FAILS)}건 / 성공 {len(F.OKS)}건")
for x in F.FAILS:
    print("   -", x)
sys.exit(1 if F.FAILS else 0)
