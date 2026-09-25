"""디스코드 없이 UI 콜백을 실제로 실행하기 위한 최소 대역."""
import asyncio, datetime, discord

FAILS = []
OKS = []


def note(ok, label, extra=""):
    (OKS if ok else FAILS).append(label)
    print(f"  {'OK  ' if ok else 'FAIL'} {label}{('   ' + extra) if extra else ''}")


class Perms:
    def __init__(self, **kw):
        self._d = {"administrator": True, "send_messages": True,
                   "manage_roles": True, "read_messages": True,
                   "view_channel": True, "embed_links": True,
                   "attach_files": True, "manage_messages": True,
                   "read_message_history": True, "manage_channels": True}
        self._d.update(kw)

    def __getattr__(self, k):
        return self.__dict__["_d"].get(k, True)


class Role:
    def __init__(self, rid, name, position=1):
        self.id, self.name, self.position = rid, name, position
        self.mention = f"<@&{rid}>"
        self.managed = False
        # 실제 디스코드 역할에는 이 값이 있습니다. 대부분의 알림 역할은
        # False 이고, 그러면 봇이 핑을 울리려면 별도 권한이 필요합니다.
        self.mentionable = False

    def __repr__(self):
        return f"<Role {self.name}>"

    def __eq__(self, o):
        return isinstance(o, Role) and o.id == self.id

    def __hash__(self):
        return hash(self.id)

    def __lt__(self, o):
        return self.position < o.position

    def __le__(self, o):
        return self.position <= o.position

    def __gt__(self, o):
        return self.position > o.position

    def __ge__(self, o):
        return self.position >= o.position


class Msg:
    def __init__(self, mid=555, channel=None, author=None):
        self.id, self.channel, self.author = mid, channel, author
        self.embeds, self.components, self.attachments = [], [], []
        self.stickers, self.reactions = [], []
        self.mentions, self.role_mentions, self.channel_mentions = [], [], []
        self.mention_everyone = False
        self.reference, self.interaction = None, None
        self.type = discord.MessageType.default
        self.flags = discord.MessageFlags()
        self.created_at = datetime.datetime.now(datetime.timezone.utc)
        self.pinned = self.tts = False
        self.content = ""
        self.webhook_id = None
        self.edited, self.deleted = None, False
        self.jump_url = f"https://discord.com/channels/1/2/{mid}"

    async def edit(self, **kw):
        self.edited = kw
        return self

    async def delete(self, **kw):
        self.deleted = True

    async def add_reaction(self, *a):
        pass


class Channel:
    def __init__(self, cid, name, guild=None, kind="text"):
        self.id, self.name, self.guild = cid, name, guild
        self.mention = f"<#{cid}>"
        self.type = (discord.ChannelType.text if kind == "text"
                     else discord.ChannelType.category)
        self.sent = []
        self.category = None
        self.channels = []
        self._ow = {}

    def permissions_for(self, m):
        return Perms()

    def overwrites_for(self, target):
        return self._ow.get(getattr(target, "id", target),
                            discord.PermissionOverwrite())

    @property
    def text_channels(self):
        return [c for c in self.channels
                if c.type == discord.ChannelType.text]

    async def send(self, *a, **k):
        m = Msg(900 + len(self.sent), self, None)
        m.embeds = [k["embed"]] if k.get("embed") else list(k.get("embeds") or [])
        m.content = a[0] if a else k.get("content", "")
        self.sent.append(k or {"content": m.content})
        return m

    async def fetch_message(self, mid):
        return Msg(mid, self)

    def get_partial_message(self, mid):
        return Msg(mid, self)

    def history(self, **kw):
        async def gen():
            for _ in ():
                yield None
        return gen()

    async def create_text_channel(self, *a, **k):
        return Channel(777, a[0] if a else "ticket", self.guild)

    async def set_permissions(self, *a, **k):
        pass


class Member:
    def __init__(self, uid=7, name="tester", admin=True, roles=None, guild=None):
        self.id, self.name, self.display_name = uid, name, name
        self.mention = f"<@{uid}>"
        self.bot = False
        self.roles = list(roles or [])
        self.guild = guild
        self.guild_permissions = Perms(administrator=admin)
        self.added, self.removed, self.dms = [], [], []
        self.display_avatar = type("A", (), {"url": "http://x/a.png"})()
        self.avatar = self.display_avatar
        self.created_at = datetime.datetime(2020, 1, 1, tzinfo=datetime.timezone.utc)
        self.joined_at = self.created_at
        self.top_role = (max(self.roles, key=lambda r: r.position)
                         if self.roles else Role(0, "none", 0))

    async def add_roles(self, *roles, reason=None):
        for r in roles:
            self.added.append(r.name)
            if r not in self.roles:
                self.roles.append(r)

    async def remove_roles(self, *roles, reason=None):
        for r in roles:
            self.removed.append(r.name)
            if r in self.roles:
                self.roles.remove(r)

    async def send(self, *a, **k):
        if getattr(self, "dm_closed", False):
            raise discord.Forbidden(type("R", (), {"status": 403, "reason": ""})(),
                                    "Cannot send messages to this user")
        self.dms.append(k or {"pos": a})
        m = Msg(700 + len(self.dms), None, None)
        m.dm_of = self
        self.__dict__.setdefault("dm_msgs", []).append(m)
        return m

    async def timeout(self, until, reason=None):
        self.timed_out = (until, reason)

    async def kick(self, reason=None):
        self.kicked = reason
        if self.guild is not None and self in self.guild.members:
            self.guild.members.remove(self)

    def __str__(self):
        return self.name


class Guild:
    def __init__(self):
        self.id, self.name = 1, "테스트 서버"
        self.me = Member(2, "bot", roles=[Role(1, "봇역할", 99)])
        self.icon = type("I", (), {"url": "http://x/i.png"})()
        self.roles = [Role(900, "@everyone", 0), Role(901, "인증", 1),
                      Role(902, "필수역할", 2), Role(999, "추석기념", 3)]
        self.roles.append(Role(111122223333444455, "스노플레이크역할", 4))
        self._roles = {r.id: r for r in self.roles}
        self.channels = [Channel(111, "잡담", self), Channel(222, "공략", self),
                         Channel(333, "로그", self)]
        self.text_channels = self.channels
        self.categories = [Channel(444, "카테고리", self, "category")]
        self.channels.append(Channel(222233334444555566, "스노채널", self))
        self.categories.append(Channel(333344445555666677, "스노카테고리",
                                       self, "category"))
        self.text_channels = self.channels
        self._chans = {c.id: c for c in self.channels + self.categories}
        self.members = [Member(7, "관리자", True, guild=self),
                        Member(8, "유저", False, guild=self)]
        self.member_count = 6500
        self.default_role = self.roles[0]
        self.owner_id = None
        self.bans = {}          # user_id -> reason
        self.ban_fail = None    # 넣으면 ban() 이 그 예외를 던집니다

    async def ban(self, user, reason=None, delete_message_seconds=0):
        if self.ban_fail is not None:
            raise self.ban_fail
        self.bans[user.id] = reason

    async def unban(self, user, reason=None):
        if user.id not in self.bans:
            raise discord.NotFound(type("R", (), {"status": 404, "reason": ""})(), "Unknown Ban")
        self.bans.pop(user.id)

    async def fetch_ban(self, user):
        if user.id not in self.bans:
            raise discord.NotFound(type("R", (), {"status": 404, "reason": ""})(), "Unknown Ban")
        return type("BanEntry", (), {"reason": self.bans[user.id], "user": user})()

    def get_role(self, rid):
        return self._roles.get(int(rid)) if rid else None

    def get_channel(self, cid):
        return self._chans.get(int(cid)) if cid else None

    def get_channel_or_thread(self, cid):
        return self.get_channel(cid)

    def get_member(self, uid):
        return next((m for m in self.members if m.id == int(uid)), None)

    async def fetch_member(self, uid):
        m = self.get_member(uid)
        if m is None:
            raise discord.NotFound(type("R", (), {"status": 404, "reason": ""})(), "x")
        return m

    async def create_text_channel(self, *a, **k):
        return Channel(778, a[0] if a else "ch", self)


class Resp:
    def __init__(self):
        self.sent, self.edited, self.modal, self.done = [], None, None, False

    async def send_message(self, *a, **k):
        if self.done:
            raise RuntimeError("이미 응답함 (InteractionResponded)")
        self.done = True
        self.sent.append({**k, "content": a[0] if a else k.get("content")})

    async def defer(self, *a, **k):
        if self.done:
            raise RuntimeError("이미 응답함 (InteractionResponded)")
        self.done = True

    async def edit_message(self, **k):
        if self.done:
            raise RuntimeError("이미 응답함 (InteractionResponded)")
        self.done = True
        self.edited = k

    async def send_modal(self, m):
        if self.done:
            raise RuntimeError("이미 응답함 (InteractionResponded)")
        self.done = True
        self.modal = type(m).__name__

    def is_done(self):
        return self.done


class Follow:
    def __init__(self):
        self.sent = []

    async def send(self, *a, **k):
        self.sent.append({**k, "content": a[0] if a else k.get("content")})
        return Msg(901)


class Client:
    def __init__(self, guild):
        self.user = Member(2, "bot")
        self.guilds = [guild]
        self.latency = 0.05
        self.loop = None

    def get_guild(self, gid):
        return self.guilds[0]

    def get_channel(self, cid):
        return self.guilds[0].get_channel(cid)

    async def fetch_channel(self, cid):
        return self.guilds[0].get_channel(cid)

    def get_cog(self, n):
        return None

    def add_view(self, *a, **k):
        pass


GUILD = Guild()


class Inter:
    def __init__(self, uid=7, admin=True, values=None, roles=None):
        self.guild = GUILD
        self.guild_id = GUILD.id
        self.user = Member(uid, "관리자" if admin else "유저", admin,
                           roles=roles or [], guild=GUILD)
        self.response = Resp()
        self.followup = Follow()
        self.channel = GUILD.channels[0]
        self.channel_id = self.channel.id
        self.message = Msg(555, self.channel, self.user)
        self.client = Client(GUILD)
        self.data = {"values": values or []}
        self.created_at = datetime.datetime.now(datetime.timezone.utc)

    async def edit_original_response(self, **k):
        self.response.edited = k
        return self.message

    async def original_response(self):
        return self.message


def outcome(it):
    if it.response.modal:
        return "modal:" + it.response.modal
    if it.response.edited:
        return "패널갱신"
    if it.response.sent:
        return "응답"
    if it.followup.sent:
        return "followup"
    return "무응답(defer만)"


async def run(label, coro_fn, uid=7, admin=True, roles=None):
    it = Inter(uid=uid, admin=admin, roles=roles)
    try:
        await coro_fn(it)
        await drain()
        note(True, label, outcome(it))
    except Exception as e:
        import traceback
        note(False, label, f"{type(e).__name__}: {e}")
        traceback.print_exc(limit=3)
    return it


async def drain():
    for _ in range(8):
        await asyncio.sleep(0)
