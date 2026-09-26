"""채널 잠금 (/잠금 · /잠금해제 · /채널잠금설정) — 누가 채팅할 수 있는지를 실제로 계산합니다.

'권한을 바꿨다' 가 아니라 **디스코드의 권한 계산 규칙으로 사람마다 채팅
가능 여부를 계산해** 봅니다. 잠그면 관리자만 남는지, 풀면 **한 칸도 안
틀리고** 원래대로 돌아오는지.

실제 서버 모양을 넣습니다(2026-09-26 실측): 347개 중 46개 채널에 역할·사람별
'채팅 허용' 이 있어 Dyno 식(@everyone 만 막기)으로는 그 사람들이 새어 나갑니다.
"""
import sys, os, tempfile, asyncio, datetime

PROJ = sys.argv[1]
sys.path.insert(0, PROJ)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(tempfile.mkdtemp())
os.environ["MONGO_URI"] = ""

import discord
import fakemongo as FM
import fakes as F
from fakes import note, drain
import utils.mod_db as mdb
import cogs.channel_lock as CL

KST = mdb.KST
P = discord.Permissions
OW = discord.PermissionOverwrite


# ── 진짜 타입을 상속한 대역 (isinstance 판정을 그대로 타게) ─────────────────

class Role(discord.Role):
    def __init__(self, rid, name, perms=None, bot=False):
        self.id, self.name = rid, name
        self._p = perms or P.none()
        self._bot = bot

    @property
    def permissions(self):
        return self._p

    def is_bot_managed(self):
        return self._bot

    def __hash__(self):
        return hash(("r", self.id))

    def __eq__(self, o):
        return isinstance(o, Role) and o.id == self.id

    def __repr__(self):
        return f"<Role {self.name}>"


class Member(discord.Member):
    def __init__(self, mid, name, roles, bot=False):
        self._mid, self._mname = mid, name
        self._roles_list = roles
        self._bot = bot

    # discord.Member 는 id · name 을 읽기 전용 속성으로 둡니다
    @property
    def id(self):
        return self._mid

    @property
    def name(self):
        return self._mname

    @property
    def bot(self):
        return self._bot

    @property
    def guild_permissions(self):
        v = 0
        for r in self._roles_list:
            v |= r.permissions.value
        return P(v)

    def __hash__(self):
        return hash(("m", self.id))

    def __eq__(self, o):
        return isinstance(o, Member) and o.id == self.id

    def __repr__(self):
        return f"<Member {self.name}>"


class Msg:
    def __init__(self, mid, ch):
        self.id, self.channel = mid, ch


class Partial:
    def __init__(self, ch, mid):
        self.ch, self.id = ch, mid

    async def delete(self):
        if self.id not in self.ch.messages:
            raise discord.NotFound(_Resp(404), "없음")
        self.ch.messages.pop(self.id)


class _Resp:
    def __init__(self, status):
        self.status, self.reason = status, "x"


class Channel:
    def __init__(self, cid, name, guild, overwrites):
        self.id, self.name, self.guild = cid, name, guild
        self.mention = f"<#{cid}>"
        self._ow = dict(overwrites)
        self.edits = 0
        self.fail_next = []          # 다음 edit 에서 던질 예외들
        self.messages = {}
        self.send_fail = None
        self._mid = 1000

    @property
    def overwrites(self):
        # 디스코드처럼 매번 새 사본을 줍니다
        return {t: OW(**dict(iter(o))) for t, o in self._ow.items()}

    async def edit(self, overwrites=None, reason=None):
        if self.fail_next:
            raise self.fail_next.pop(0)
        self.edits += 1
        # 디스코드는 대상 종류를 id+type 으로만 압니다 — Object 도 받아야 합니다
        self._ow = {t: OW(**dict(iter(o))) for t, o in overwrites.items()}

    async def send(self, embed=None, allowed_mentions=None, **k):
        if self.send_fail:
            raise self.send_fail
        self._mid += 1
        self.messages[self._mid] = embed
        return Msg(self._mid, self)

    def get_partial_message(self, mid):
        return Partial(self, mid)

    def permissions_for(self, m):
        return P.all()


_GID = [9000]


class Guild:
    def __init__(self):
        # 테스트마다 다른 서버 — 같으면 앞 테스트의 잠금 상태가 새어 듭니다
        _GID[0] += 1
        self.id, self.name, self.unavailable = _GID[0], "테스트서버", False
        self.default_role = Role(self.id, "@everyone", P(P.text().value | P.general().value))
        self.channels = {}
        self.deleted = set()
        self.fetch_error = None

    def get_role(self, rid):
        return self.roles.get(rid)

    def get_member(self, mid):
        return self.members.get(mid)

    def get_channel(self, cid):
        return None if cid in self.deleted else self.channels.get(cid)

    async def fetch_channel(self, cid):
        if self.fetch_error:
            raise self.fetch_error
        raise discord.NotFound(_Resp(404), "없음")


def build_world():
    g = Guild()
    admin = Role(1, "스태프", P(administrator=True))
    vip = Role(2, "Private Chat1 role", P.none())
    botr = Role(3, "Giveaway Boat", P(P.text().value), bot=True)
    macro = Role(4, "매크로 사용 유저", P(P.text().value))       # 서버 전체 채팅 권한
    g.roles = {r.id: r for r in (g.default_role, admin, vip, botr, macro)}
    people = {
        "평범": Member(11, "평범", [g.default_role]),
        "VIP": Member(12, "VIP", [g.default_role, vip]),
        "개인허용": Member(13, "개인허용", [g.default_role]),
        "스태프": Member(14, "스태프", [g.default_role, admin]),
        "매크로": Member(15, "매크로", [g.default_role, macro]),
    }
    g.members = {m.id: m for m in people.values()}
    g.me = Member(2, "봇", [g.default_role, admin], bot=True)
    # 오프_토픽 모양: 개인 허용 1 + 역할 허용 + 봇 역할 허용. @everyone 칸은 원래 없음
    ch = Channel(500, "오프_토픽", g, {
        vip: OW(send_messages=True),
        g.members[13]: OW(send_messages=True, attach_files=True),
        botr: OW(send_messages=True),
        admin: OW(send_messages=True),
    })
    # 서버를 떠난 사람의 개인 허용 — discord.py 는 type 이 붙은 Object 로 줍니다
    ghost = discord.Object(id=77, type=discord.Member)
    ch._ow[ghost] = OW(send_messages=True)
    g.channels[ch.id] = ch
    # @everyone 칸이 원래 있던 채널(보기 거부 + 역할만 보기·채팅 허용)
    pc = Channel(501, "private_chat1", g, {
        g.default_role: OW(view_channel=False),
        vip: OW(view_channel=True, send_messages=True),
    })
    g.channels[pc.id] = pc
    return g, ch, pc, people


def can_send(ch, member) -> bool:
    """디스코드 권한 계산 (채널 기준 · 관리자는 전부 통과)."""
    g = ch.guild
    base = 0
    for r in member._roles_list:
        base |= r.permissions.value
    if base & P(administrator=True).value:
        return True
    ows = {CL.target_key(t): o for t, o in ch._ow.items()}
    perms = base

    def apply(o):
        nonlocal perms
        a, d = o.pair()
        perms &= ~d.value
        perms |= a.value

    if CL.target_key(g.default_role) in ows:
        apply(ows[CL.target_key(g.default_role)])
    allow = deny = 0
    for r in member._roles_list:
        if r.id == g.default_role.id:
            continue
        o = ows.get("r" + str(r.id))
        if o:
            a, d = o.pair()
            allow |= a.value
            deny |= d.value
    perms &= ~deny
    perms |= allow
    if "m" + str(member.id) in ows:
        apply(ows["m" + str(member.id)])
    need = P(view_channel=True, send_messages=True).value
    return perms & need == need


def who(ch, people):
    return sorted(n for n, m in people.items() if can_send(ch, m))


def snapshot(ch):
    return {CL.target_key(t): tuple(v.value for v in o.pair()) for t, o in ch._ow.items()}


def at(h, mi=0, day=26):
    return datetime.datetime(2026, 9, day, h, mi, tzinfo=KST)


# ── 순수 함수 ─────────────────────────────────────────────────────────

def test_pure():
    print("=== 시각 · 기간 읽기 ===")
    for raw, want in (("00:00", 0), ("0", 0), ("7", 420), ("07:00", 420), ("7:30", 450),
                      ("7시", 420), ("7시 30분", 450), ("24:00", 0), ("23:59", 1439),
                      ("25:00", None), ("7:60", None), ("아침", None), ("", None)):
        note(CL.parse_hhmm(raw) == want, f"시각 '{raw}' → {want}", str(CL.parse_hhmm(raw)))
    for raw, want in (("30분", 30), ("2시간", 120), ("1시간30분", 90), ("1d", 1440),
                      ("2h30m", 150), ("45", 45), ("1시간 30분", 90),
                      ("0분", None), ("31일", None), ("abc", None), ("30분x", None), ("", None)):
        note(CL.parse_duration(raw) == want, f"기간 '{raw}' → {want}", str(CL.parse_duration(raw)))

    print("\n=== 잠금 시간 판정 (00:00~07:00, 22:00~06:00) ===")
    s, e = 0, 420
    for h, mi, want in ((23, 59, False), (0, 0, True), (3, 0, True), (6, 59, True), (7, 0, False)):
        note(CL.in_window(h * 60 + mi, s, e) == want, f"{h:02d}:{mi:02d} → {'잠금' if want else '열림'}")
    for h, want in ((21, False), (22, True), (2, True), (6, False)):
        note(CL.in_window(h * 60, 1320, 360) == want, f"자정 넘김 22~06 · {h:02d}시 → {want}")
    note(CL.window_minutes(0, 420) == 420 and CL.fmt_duration(420) == "7시간", "00~07 은 7시간")
    note(not CL.in_window(100, 300, 300), "시작=끝 이면 잠그지 않음")


# ── 권한 계산 ─────────────────────────────────────────────────────────

async def test_lock_cycle(cog, label):
    print(f"\n=== [{label}] 잠그면 관리자만 · 풀면 한 칸도 안 틀리고 원래대로 ===")
    g, ch, pc, people = build_world()
    before, before_pc = snapshot(ch), snapshot(pc)
    note(who(ch, people) == sorted(["평범", "VIP", "개인허용", "스태프", "매크로"]),
         "잠그기 전: 전원 채팅 가능", str(who(ch, people)))
    await mdb.save_lock_settings(g.id, {"enabled": True, "channel_ids": [ch.id, pc.id],
                                         "announce": True})

    await cog.reconcile(g, at(0, 0))
    note(who(ch, people) == ["스태프"], "**00:00 잠금 → 스태프(관리자)만**", str(who(ch, people)))
    note(who(pc, people) == ["스태프"], "비공개 채널도 VIP 가 못 침 (역할 허용을 거둠)",
         str(who(pc, people)))
    ghost_ow = next(o for t, o in ch._ow.items() if t.id == 77)
    note(ghost_ow.send_messages is None, "서버를 떠난 사람의 개인 허용도 거둠 (Object 대상)")
    bot_ow = next(o for t, o in ch._ow.items() if t.id == 3)
    note(bot_ow.send_messages is True, "봇 역할의 허용은 건드리지 않음")
    note(len(ch.messages) == 1, "잠금 안내 1개 게시")

    edits = ch.edits
    await cog.reconcile(g, at(3, 0))
    note(ch.edits == edits and len(ch.messages) == 1,
         "잠긴 동안 다시 맞춰도 아무것도 안 바꿈 (권한 변경 · 안내 0회)")

    # 잠긴 사이 관리자가 다른 권한을 바꿈 → 풀 때 그건 유지
    for t in list(ch._ow):
        if t.id == 12 or (isinstance(t, Role) and t.id == 2):
            ch._ow[t].embed_links = False
    await cog.reconcile(g, at(7, 0))
    note(who(ch, people) == sorted(["평범", "VIP", "개인허용", "스태프", "매크로"]),
         "**07:00 해제 → 다시 전원**", str(who(ch, people)))
    after = snapshot(ch)
    vip_key = "r2"
    changed = {k for k in set(before) | set(after) if before.get(k) != after.get(k)}
    note(changed == {vip_key}, "되돌린 뒤 달라진 칸은 잠긴 사이 관리자가 바꾼 것뿐",
         str(changed))
    note(f"r{g.id}" not in after, "원래 없던 @everyone 칸은 지움 (카테고리 동기화 유지)")
    note(snapshot(pc) == before_pc, "비공개 채널은 한 칸도 안 틀리고 원래대로")
    note(len(ch.messages) == 0, "풀면 잠금 안내를 지움")
    doc = await mdb.get_lock_doc(g.id)
    note(not doc["locked"], "풀고 나면 저장해 둔 원래 권한도 지움")


async def test_restart_and_downtime(label):
    print(f"\n=== [{label}] 잠긴 채 재시작 · 7시에 꺼져 있었음 ===")
    g, ch, pc, people = build_world()
    before = snapshot(ch)
    await mdb.save_lock_settings(g.id, {"enabled": True, "channel_ids": [ch.id]})
    await CL.ChannelLock.reconcile(FakeCog(), g, at(0, 1))
    note(who(ch, people) == ["스태프"], "잠김")
    # 봇 재시작 = 새 인스턴스(메모리 없음). 7시에는 꺼져 있다가 9시에 켜짐
    await CL.ChannelLock.reconcile(FakeCog(), g, at(9, 12))
    note(snapshot(ch) == before, "**재시작 뒤 · 늦게 켜져도 원래대로 풀림**")


async def test_failures(label):
    print(f"\n=== [{label}] 실패해도 영영 잠기거나 못 풀리지 않음 ===")
    g, ch, pc, people = build_world()
    before = snapshot(ch)
    await mdb.save_lock_settings(g.id, {"enabled": True, "channel_ids": [ch.id], "announce": False})
    cog = FakeCog()

    ch.fail_next = [discord.HTTPException(_Resp(503), "Service Unavailable")]
    await CL.ChannelLock.reconcile(cog, g, at(0, 0))
    note(who(ch, people) != ["스태프"], "디스코드 503 → 이번엔 못 잠금")
    doc = await mdb.get_lock_doc(g.id)
    note(str(ch.id) in doc["locked"], "그래도 원래 권한은 먼저 저장돼 있음")
    await CL.ChannelLock.reconcile(cog, g, at(0, 1))
    note(who(ch, people) == ["스태프"], "다음 주기(30초 뒤)에 잠금")

    ch.fail_next = [discord.Forbidden(_Resp(403), "Missing Permissions")]
    await CL.ChannelLock.reconcile(cog, g, at(7, 0))
    doc = await mdb.get_lock_doc(g.id)
    note(str(ch.id) in doc["locked"], "풀기가 거부되면 원래 권한을 **지우지 않음** (다시 시도할 수 있게)")
    await CL.ChannelLock.reconcile(cog, g, at(7, 1))
    note(snapshot(ch) == before, "권한이 돌아오면 원래대로 풀림")

    # 저장이 실패하면 권한을 바꾸지 않음 — 바꾼 뒤 모르게 되면 영영 잠김
    g2, ch2, _, _ = build_world()
    await mdb.save_lock_settings(g2.id, {"enabled": True, "channel_ids": [ch2.id]})
    real = CL.set_lock_state

    async def boom(*a, **k):
        raise RuntimeError("DB 끊김")

    CL.set_lock_state = boom
    try:
        try:
            await CL.ChannelLock.reconcile(FakeCog(), g2, at(0, 0))
        except RuntimeError:
            pass        # 주기 작업이 이 예외를 잡아 로그로 남깁니다
        note(ch2.edits == 0, "원래 권한을 저장 못 하면 **잠그지 않음**")
    finally:
        CL.set_lock_state = real

    print("")
    g3, ch3, _, _ = build_world()
    await mdb.save_lock_settings(g3.id, {"enabled": True, "channel_ids": [ch3.id]})
    await CL.ChannelLock.reconcile(FakeCog(), g3, at(0, 0))
    g3.deleted.add(ch3.id)
    g3.fetch_error = discord.HTTPException(_Resp(500), "잠깐 오류")
    await CL.ChannelLock.reconcile(FakeCog(), g3, at(7, 0))
    doc = await mdb.get_lock_doc(g3.id)
    note(str(ch3.id) in doc["locked"], "채널을 잠깐 못 찾으면 기록을 지우지 않음 (지우면 영영 잠김)")
    g3.fetch_error = None
    await CL.ChannelLock.reconcile(FakeCog(), g3, at(7, 1))
    doc = await mdb.get_lock_doc(g3.id)
    note(str(ch3.id) not in doc["locked"], "정말 지워진 채널이면 기록만 정리")

    g4, ch4, _, _ = build_world()
    await mdb.save_lock_settings(g4.id, {"enabled": True, "channel_ids": [ch4.id], "announce": True})
    ch4.send_fail = discord.Forbidden(_Resp(403), "no")
    cog4 = FakeCog()
    await CL.ChannelLock.reconcile(cog4, g4, at(0, 0))
    await CL.ChannelLock.reconcile(cog4, g4, at(0, 1))
    note(who(ch4, build_world()[3]) == ["스태프"] and not ch4.messages,
         "안내를 못 올려도 잠금은 됨 · 30초마다 다시 올리지 않음")


async def test_manual(label):
    print(f"\n=== [{label}] /잠금 · /잠금해제 와 자동 잠금이 겹칠 때 ===")
    g, ch, pc, people = build_world()
    before = snapshot(ch)
    await mdb.save_lock_settings(g.id, {"enabled": True, "channel_ids": [ch.id], "announce": False})
    cog = FakeCog()
    # 23:00 에 2시간 수동 잠금 → 00:00 부터 자동 잠금도 원함
    until = at(23, 0, day=25) + datetime.timedelta(hours=2)
    await mdb.set_lock_state(g.id, "manual", ch.id, {"until": until.timestamp(), "by": 1, "reason": ""})
    await CL.ChannelLock.reconcile(cog, g, at(23, 0, day=25))
    saved1 = (await mdb.get_lock_doc(g.id))["locked"][str(ch.id)]["saved"]
    await CL.ChannelLock.reconcile(cog, g, at(0, 30))
    saved2 = (await mdb.get_lock_doc(g.id))["locked"][str(ch.id)]["saved"]
    note(saved1 == saved2, "**겹쳐 잠가도 원래 권한은 처음 한 번만 저장** (잠긴 모양을 원래로 착각하지 않음)")
    await CL.ChannelLock.reconcile(cog, g, at(1, 30))
    doc = await mdb.get_lock_doc(g.id)
    note(who(ch, people) == ["스태프"] and str(ch.id) not in doc["manual"],
         "01:30 수동 잠금은 끝났지만 자동 잠금 시간이라 계속 잠김")
    await CL.ChannelLock.reconcile(cog, g, at(7, 0))
    note(snapshot(ch) == before, "07:00 원래대로")

    # 자동 잠금 중 관리자가 풀면 그날 밤만 열어 둠
    await CL.ChannelLock.reconcile(cog, g, at(0, 5, day=27))
    note(who(ch, people) == ["스태프"], "다음 날 00:05 다시 자동 잠금")
    end = CL.window_end_after(at(2, 0, day=27), 420)
    await mdb.set_lock_state(g.id, "open_until", ch.id, end.timestamp())
    await CL.ChannelLock.reconcile(cog, g, at(2, 0, day=27))
    note(snapshot(ch) == before, "02:00 관리자가 풂 → 바로 열림")
    await CL.ChannelLock.reconcile(cog, g, at(4, 0, day=27))
    note(snapshot(ch) == before, "04:00 에도 다시 잠그지 않음 (30초 뒤 도로 잠기면 풀 수가 없음)")
    await CL.ChannelLock.reconcile(cog, g, at(0, 1, day=28))
    note(who(ch, people) == ["스태프"], "그다음 날 밤은 평소대로 잠금")
    await CL.ChannelLock.reconcile(cog, g, at(7, 0, day=28))

    # 자동 잠금을 끄면 즉시 풀림
    await CL.ChannelLock.reconcile(cog, g, at(1, 0, day=29))
    await mdb.save_lock_settings(g.id, {"enabled": False})
    await CL.ChannelLock.reconcile(cog, g, at(1, 1, day=29))
    note(snapshot(ch) == before, "자동 잠금을 끄면 잠긴 채널이 바로 풀림")


class FakeCog:
    """reconcile 만 쓰는 최소 대역 — 새로 만들면 재시작과 같습니다(메모리 없음)."""
    def __init__(self):
        self._locks, self._last_err = {}, {}

    _guild_lock = CL.ChannelLock._guild_lock
    _log = CL.ChannelLock._log
    _ensure_locked = CL.ChannelLock._ensure_locked
    _ensure_unlocked = CL.ChannelLock._ensure_unlocked
    reconcile = CL.ChannelLock.reconcile


# ── 명령어 · 패널 (디스코드 대역) ─────────────────────────────────────

async def test_ui():
    print("\n=== 명령어 · 패널 ===")
    cog = FakeCog()
    cog.refresh_panel = lambda it: CL.ChannelLock.refresh_panel(cog, it)
    bound = CL.ChannelLock.__new__(CL.ChannelLock)
    bound.__dict__.update(cog.__dict__)

    async def call(cmd, it, **kw):
        await cmd.callback(bound, it, **kw)
        await drain()

    it = F.Inter()
    await call(CL.ChannelLock.lock, it, 기간="내일까지")
    note(it.response.sent and "텍스트 채널" in (it.response.sent[0].get("content") or ""),
         "텍스트 채널이 아니면(대역 채널) 거절", F.outcome(it))

    it = F.Inter()
    await call(CL.ChannelLock.lock_settings, it)
    emb = it.followup.sent[-1]["embed"]
    note(emb.title.startswith("🔒"), "/채널잠금설정 패널이 열림", emb.title)
    note(it.followup.sent[-1].get("ephemeral") is True, "패널은 본인에게만")
    note("00:00 ~ 07:00" in emb.fields[0].value and "7시간" in emb.fields[0].value,
         "기본값 00:00 ~ 07:00 (7시간)", emb.fields[0].value)

    view = it.followup.sent[-1]["view"]
    it2 = F.Inter()
    await view.toggle.callback(it2)
    doc = await mdb.get_lock_doc(F.GUILD.id)
    note(doc["enabled"] is True and it2.response.edited, "켜기 버튼 → 켜지고 패널 갱신")
    it3 = F.Inter(admin=False)
    await view.toggle.callback(it3)
    note((await mdb.get_lock_doc(F.GUILD.id))["enabled"] is True and it3.response.sent,
         "관리자가 아니면 바꾸지 못함")

    it4 = F.Inter()
    await view.time.callback(it4)
    note(it4.response.modal == "TimeModal", "시간 바꾸기 → 모달 (DB 를 읽지 않고 바로)")
    for s, e, ok in (("22:00", "6시", True), ("25:00", "07:00", False), ("7", "7:00", False)):
        m = CL.TimeModal(cog, doc)
        m.start._value, m.end._value = s, e
        itm = F.Inter()
        await m.on_submit(itm)
        d = await mdb.get_lock_doc(F.GUILD.id)
        saved = (d["start"], d["end"]) == ("22:00", "06:00")
        note(saved if ok else bool(itm.response.sent),
             f"시간 {s} ~ {e} → {'저장' if ok else '거절'}", str((d["start"], d["end"])))

    # 25채널 · 긴 이름으로 패널 규격(칸 1024자) 확인
    g, ch, pc, people = build_world()
    for i in range(30):
        g.channels[600 + i] = Channel(600 + i, "아주긴채널이름" * 8, g, {})
    doc = {**mdb.DEFAULT_LOCK_SETTINGS, "enabled": True,
           "channel_ids": list(range(600, 625)), "locked": {}, "manual": {}, "open_until": {}}
    emb = CL.build_panel(g, doc, at(1, 0))
    ok = all(len(f.value) <= 1024 for f in emb.fields) and len(emb) <= 6000
    note(ok, "채널 25개여도 임베드 규격 안 (칸 1024 · 합계 6000)", f"{len(emb)}자")


async def run_suite(label):
    cog = FakeCog()
    await test_lock_cycle(cog, label)
    await test_restart_and_downtime(label)
    await test_failures(label)
    await test_manual(label)


async def main():
    test_pure()
    # Mongo 경로 (가짜 드라이버) — 점 경로 $set/$unset 이 실제로 쓰입니다
    mdb.channel_lock_col = FM.FakeCollection("channel_locks")
    await run_suite("Mongo")
    # JSON 폴백
    mdb.channel_lock_col = None
    await run_suite("JSON")
    await test_ui()


asyncio.run(main())
print(f"\n  실패 {len(F.FAILS)}건 / 성공 {len(F.OKS)}건")
for x in F.FAILS:
    print("   -", x)
sys.exit(1 if F.FAILS else 0)
