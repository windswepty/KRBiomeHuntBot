"""
경고 · 밴 (모더레이션) — Dyno 식 처벌 관리.

  /경고 유저 사유        경고를 주고 누적 횟수에 따라 자동 처벌
  /밴 유저 사유          밴 **직전에** 사유 + 서포트 서버 링크를 DM
  /경고설정              관리 패널 (경고 해제 · 밴 해제 · 유저 검색 ·
                         밴 DM · 로그 채널 · 자동 처벌)

포인트(utils/database.py)와도, 인증·링크 보호(utils/verify_db.py)와도
독립입니다. 데이터는 `utils/mod_db.py` 에만 둡니다.

⚠️ **밴 DM 은 밴하기 전에 보냅니다.** 밴한 뒤에는 서버를 같이 쓰지 않아
DM 이 막힙니다. 대신 밴이 실패하면 **보낸 DM 을 지웁니다** — "밴됐다" 는
DM 만 받고 멀쩡히 남아 있으면 그게 더 혼란스럽습니다.
"""

import re
import asyncio
from datetime import timedelta

import discord
from discord.ext import commands
from discord import app_commands

from utils.mod_db import (
    get_mod_settings, save_mod_settings, add_case, add_warning,
    get_user_cases, get_active_warnings, find_users_by_name,
    remove_warnings, AUTO_ACTIONS, TIMEOUT_MAX_MINUTES,
)


KIND_META = {
    "warn": ("⚠️", "경고", discord.Color.gold()),
    "unwarn": ("♻️", "경고 해제", discord.Color.green()),
    "ban": ("🔨", "밴", discord.Color.red()),
    "unban": ("🔓", "밴 해제", discord.Color.green()),
    "timeout": ("🔇", "타임아웃", discord.Color.orange()),
    "kick": ("👢", "추방", discord.Color.dark_orange()),
}
ACTION_LABEL = {"timeout": "타임아웃", "kick": "추방", "ban": "밴"}
ACTION_ALIASES = {
    "timeout": "timeout", "타임아웃": "timeout", "뮤트": "timeout",
    "kick": "kick", "킥": "kick", "추방": "kick",
    "ban": "ban", "밴": "ban", "차단": "ban",
}
# 자동 처벌에 봇이 가져야 할 권한
ACTION_PERM = {"timeout": "moderate_members", "kick": "kick_members",
               "ban": "ban_members"}
PERM_LABEL = {"moderate_members": "멤버 타임아웃", "kick_members": "멤버 추방",
              "ban_members": "멤버 차단", "administrator": "관리자"}

DM_STATUS = {
    "sent": "✅ DM 전송됨",
    "closed": "🚫 DM 을 막아 둔 유저라 못 보냈습니다",
    "failed": "⚠️ DM 전송 실패",
    "off": "— DM 꺼짐",
    "absent": "— 서버에 없는 유저라 DM 을 보낼 수 없습니다",
}

# 서포트 링크는 DM 의 **링크 버튼**이 됩니다. 형식이 틀리면 DM 전송
# 자체가 실패하므로 저장할 때 막습니다.
LINK_RE = re.compile(r"^https?://[^\s<>]+$")


def parse_user_id(raw: str) -> int | None:
    m = re.search(r"\d{15,20}", raw or "")
    return int(m.group(0)) if m else None


def fmt_minutes(minutes: int) -> str:
    minutes = int(minutes)
    d, rest = divmod(minutes, 1440)
    h, m = divmod(rest, 60)
    parts = [f"{d}일" if d else "", f"{h}시간" if h else "", f"{m}분" if m else ""]
    return " ".join(p for p in parts if p) or "0분"


def describe_rule(rule: dict) -> str:
    act = ACTION_LABEL.get(rule.get("action"), rule.get("action"))
    if rule.get("action") == "timeout":
        act += f" {fmt_minutes(rule.get('minutes', 60))}"
    return f"경고 **{rule.get('count')}회** → {act}"


def match_auto_rule(rules: list, count: int) -> dict | None:
    """정확히 그 횟수에 걸린 규칙 하나. 이미 넘긴 규칙은 다시 걸지 않습니다
    (5회째 경고에 '3회 → 타임아웃' 이 또 걸리면 안 됩니다)."""
    for r in rules or []:
        try:
            if int(r.get("count")) == int(count) and r.get("action") in AUTO_ACTIONS:
                return r
        except (TypeError, ValueError):
            continue
    return None


def target_problem(guild, moderator, target, need_bot: bool = True) -> str | None:
    """처벌할 수 없는 대상이면 이유를, 아니면 None.

    `need_bot=False` 는 경고처럼 **봇이 직접 손대지 않는** 처벌입니다. 봇
    역할보다 높은 사람도 경고는 기록돼야 합니다 (자동 처벌만 실패를 알립니다).

    **DM 을 보내기 전에** 가려야 합니다. 권한이 모자라 밴이 실패할 사람에게
    "밴됐습니다" DM 부터 가면 안 됩니다.
    """
    if target.id == moderator.id:
        return "자기 자신은 처벌할 수 없습니다."
    if guild.me is not None and target.id == guild.me.id:
        return "봇 자신은 처벌할 수 없습니다."
    owner_id = getattr(guild, "owner_id", None)
    if owner_id and target.id == owner_id:
        return "서버 소유자는 처벌할 수 없습니다."
    member = guild.get_member(target.id)
    if member is None:
        return None
    if moderator.id != owner_id and member.top_role >= moderator.top_role:
        return (f"{member.mention} 님의 역할이 처리자와 같거나 더 높습니다.\n"
                "-# 디스코드는 자기보다 낮은 역할만 처벌할 수 있게 합니다.")
    if need_bot and guild.me is not None and member.top_role >= guild.me.top_role:
        return (f"{member.mention} 님의 역할이 **봇 역할보다 같거나 높아** 처벌할 수 "
                "없습니다.\n-# 서버 설정 → 역할에서 봇 역할을 위로 올려 주세요.")
    return None


def _truncate(text: str, n: int) -> str:
    text = (text or "").replace("\n", " ")
    return text if len(text) <= n else text[:n - 1] + "…"


def _chunk_lines(lines: list, limit: int = 1024) -> list:
    """임베드 칸 1024자를 넘기면 **전송 자체가 실패**하므로 나눠 담습니다."""
    out, cur = [], ""
    for ln in lines:
        ln = ln[:limit]
        if cur and len(cur) + 1 + len(ln) > limit:
            out.append(cur)
            cur = ln
        else:
            cur = f"{cur}\n{ln}" if cur else ln
    if cur:
        out.append(cur)
    return out


# ── DM · 로그 임베드 ───────────────────────────────────────────────────

def server_label(name: str) -> str:
    """'한바헌 서버' · 이름이 이미 '서버' 로 끝나면 그대로 ('OO 서버 서버' 방지)."""
    name = (name or "").strip()
    return name if name.endswith("서버") else f"{name} 서버"


def build_ban_dm(guild, reason: str, settings: dict) -> tuple[discord.Embed, discord.ui.View | None]:
    embed = discord.Embed(
        title=f"🔨 {server_label(guild.name)}에서 차단되었습니다",
        description=(settings.get("ban_dm_message") or
                     "서버 규칙 위반으로 차단되었습니다."),
        color=discord.Color.red())
    embed.add_field(name="📝 사유", value=_truncate(reason, 1000) or "사유 없음",
                    inline=False)
    link = (settings.get("support_link") or "").strip()
    view = None
    if link:
        embed.add_field(
            name="🛟 이의 제기",
            value=f"처분에 이의가 있다면 아래 서포트 서버로 문의해 주세요.\n{link}",
            inline=False)
        view = discord.ui.View()
        view.add_item(discord.ui.Button(style=discord.ButtonStyle.link,
                                        label="서포트 서버", emoji="🛟", url=link))
    icon = getattr(getattr(guild, "icon", None), "url", None)
    if icon:
        embed.set_thumbnail(url=icon)
    embed.set_footer(text=f"{guild.name} · 이 메시지는 자동으로 발송되었습니다")
    return embed, view


def build_warn_dm(guild, doc: dict, count: int, auto_note: str = "") -> discord.Embed:
    embed = discord.Embed(
        title=f"⚠️ {server_label(guild.name)}에서 경고를 받았습니다",
        color=discord.Color.gold())
    embed.add_field(name="📝 사유", value=_truncate(doc.get("reason"), 1000) or "사유 없음",
                    inline=False)
    embed.add_field(name="📊 누적 경고", value=f"**{count}회**", inline=True)
    embed.add_field(name="🗂️ 케이스", value=f"`#{doc.get('case')}`", inline=True)
    if auto_note:
        embed.add_field(name="⛔ 자동 처벌", value=auto_note, inline=False)
    embed.set_footer(text=f"{guild.name} · {doc.get('created', '')} KST")
    return embed


def build_log_embed(doc: dict, extra: list | None = None) -> discord.Embed:
    emoji, label, color = KIND_META.get(doc.get("kind"), ("📌", doc.get("kind"),
                                                          discord.Color.greyple()))
    title = f"{emoji} {label}{' (자동)' if doc.get('auto') else ''} | 케이스 #{doc.get('case')}"
    embed = discord.Embed(title=title, color=color)
    embed.add_field(name="대상",
                    value=f"<@{doc.get('user_id')}> `{_truncate(doc.get('user_name'), 40)}`\n"
                          f"`{doc.get('user_id')}`", inline=True)
    embed.add_field(name="처리자",
                    value=f"<@{doc.get('mod_id')}> `{_truncate(doc.get('mod_name'), 40)}`",
                    inline=True)
    embed.add_field(name="사유", value=_truncate(doc.get("reason"), 1024) or "사유 없음",
                    inline=False)
    for name, value in extra or []:
        embed.add_field(name=name, value=value[:1024], inline=False)
    embed.set_footer(text=f"{doc.get('created', '')} KST")
    return embed


async def send_log(guild, settings: dict, embed: discord.Embed) -> None:
    """로그 채널에 올립니다. 실패는 **무엇을 고쳐야 하는지** 로그에 남깁니다."""
    cid = settings.get("log_channel_id")
    if not cid:
        return
    channel = guild.get_channel(int(cid))
    if channel is None:
        print(f"[Moderation] 로그 채널 {cid} 을 찾을 수 없습니다 (지워졌거나 봇이 못 봄).",
              flush=True)
        return
    try:
        await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
    except discord.Forbidden:
        print(f"[Moderation] #{channel} 에 로그를 올릴 권한이 없습니다 "
              "('메시지 보내기'·'링크 첨부' 권한을 주세요).", flush=True)
    except discord.HTTPException as e:
        print(f"[Moderation] 로그 전송 실패: {type(e).__name__}: {e}", flush=True)


async def safe_dm(user, embed: discord.Embed, view=None):
    """DM 을 보내고 (메시지, 상태) 를 돌려줍니다. 막혀 있어도 처벌은 계속합니다."""
    try:
        kw = {"embed": embed}
        if view is not None:
            kw["view"] = view
        msg = await asyncio.wait_for(user.send(**kw), timeout=5)
        return msg, "sent"
    except discord.Forbidden:
        return None, "closed"
    except (discord.HTTPException, asyncio.TimeoutError, OSError) as e:
        print(f"[Moderation] DM 전송 실패({getattr(user, 'id', '?')}): "
              f"{type(e).__name__}: {e}", flush=True)
        return None, "failed"


def _audit_reason(moderator, reason: str) -> str:
    # 감사 로그 사유는 512자 상한입니다. 누가 했는지 앞에 붙입니다 (Dyno 방식)
    return f"{moderator} : {reason or '사유 없음'}"[:500]


async def execute_ban(guild, moderator, user, reason: str, settings: dict,
                      delete_days: int = 0, auto: bool = False) -> dict:
    """DM → 밴 → 기록 → 로그. 밴이 실패하면 보낸 DM 을 지웁니다."""
    member = guild.get_member(user.id)
    dm_msg, dm_status = None, "off"
    if member is None:
        dm_status = "absent" if settings.get("ban_dm") else "off"
    elif settings.get("ban_dm"):
        embed, view = build_ban_dm(guild, reason, settings)
        dm_msg, dm_status = await safe_dm(member, embed, view)

    async def _undo_dm():
        if dm_msg is None:
            return
        try:
            await dm_msg.delete()
        except discord.HTTPException as e:
            print(f"[Moderation] 밴 실패 후 DM 회수 실패: {type(e).__name__}: {e}",
                  flush=True)

    try:
        await guild.ban(user, reason=_audit_reason(moderator, reason),
                        delete_message_seconds=max(0, min(7, int(delete_days))) * 86400)
    except discord.Forbidden:
        await _undo_dm()
        return {"ok": False,
                "error": "봇에게 **멤버 차단** 권한이 없거나 대상의 역할이 봇보다 높습니다."}
    except discord.HTTPException as e:
        await _undo_dm()
        return {"ok": False, "error": f"디스코드 오류로 밴하지 못했습니다 ({e.status})."}

    name = str(getattr(user, "name", "") or user.id)
    doc = await add_case(guild.id, "ban", user.id, name, moderator.id, str(moderator),
                         reason, auto=auto,
                         extra={"dm": dm_status, "delete_days": int(delete_days)})
    await send_log(guild, settings, build_log_embed(
        doc, [("DM", DM_STATUS.get(dm_status, dm_status))]))
    return {"ok": True, "case": doc, "dm": dm_status}


def auto_rule_blocker(guild, member, action: str) -> str | None:
    """자동 처벌을 **실행할 수 없는 이유**. 할 수 있으면 None.

    `do_warn` 이 경고 DM 을 보낼지 정할 때와 `apply_auto_rule` 이 실행할 때
    **같은 기준**을 써야 합니다. 갈라지면 "곧 밴됩니다" DM 만 가고 밴은
    안 되거나, 밴 DM 을 기대하고 경고 DM 을 건너뛰었는데 밴이 막혀
    **아무 DM 도 안 가는** 일이 생깁니다.
    """
    me = guild.me
    perm = ACTION_PERM.get(action)
    if perm and not getattr(me.guild_permissions, perm, False):
        return f"봇에게 **{PERM_LABEL[perm]}** 권한이 없습니다."
    if member.top_role >= me.top_role:
        return "대상의 역할이 봇보다 같거나 높습니다."
    # 디스코드는 관리자 권한이 있는 멤버의 타임아웃을 거부합니다(403).
    # '권한이 부족합니다' 로만 뜨면 봇 권한을 고치러 가게 됩니다.
    if action == "timeout" and getattr(member.guild_permissions, "administrator", False):
        return "관리자 권한이 있는 멤버는 디스코드가 타임아웃을 막습니다."
    return None


async def apply_auto_rule(guild, member, rule: dict, count: int, last_reason: str,
                          settings: dict) -> str:
    """경고 누적 자동 처벌을 실행하고 결과 한 줄을 돌려줍니다."""
    action = rule.get("action")
    reason = f"경고 {count}회 누적 (자동) — 마지막 경고: {last_reason}"
    me = guild.me
    blocker = auto_rule_blocker(guild, member, action)
    if blocker:
        return f"⚠️ 자동 {ACTION_LABEL.get(action, action)} 실패 — {blocker}"
    try:
        if action == "ban":
            res = await execute_ban(guild, me, member, reason, settings, 0, auto=True)
            if not res["ok"]:
                return f"⚠️ 자동 밴 실패 — {res['error']}"
            return (f"🔨 **자동 밴** (케이스 `#{res['case']['case']}`) · "
                    f"{DM_STATUS.get(res['dm'], res['dm'])}")
        if action == "timeout":
            minutes = max(1, min(TIMEOUT_MAX_MINUTES, int(rule.get("minutes", 60))))
            await member.timeout(timedelta(minutes=minutes),
                                 reason=_audit_reason(me, reason))
            doc = await add_case(guild.id, "timeout", member.id, str(member), me.id,
                                 str(me), reason, auto=True, extra={"minutes": minutes})
            await send_log(guild, settings, build_log_embed(
                doc, [("기간", fmt_minutes(minutes))]))
            return f"🔇 **자동 타임아웃 {fmt_minutes(minutes)}** (케이스 `#{doc['case']}`)"
        if action == "kick":
            await member.kick(reason=_audit_reason(me, reason))
            doc = await add_case(guild.id, "kick", member.id, str(member), me.id,
                                 str(me), reason, auto=True)
            await send_log(guild, settings, build_log_embed(doc))
            return f"👢 **자동 추방** (케이스 `#{doc['case']}`)"
    except discord.Forbidden:
        return f"⚠️ 자동 {ACTION_LABEL.get(action, action)} 실패 — 권한이 부족합니다."
    except discord.HTTPException as e:
        return f"⚠️ 자동 {ACTION_LABEL.get(action, action)} 실패 — 디스코드 오류 ({e.status})"
    return f"⚠️ 알 수 없는 자동 처벌 `{action}` — `/경고설정` 에서 규칙을 확인하세요."


# ── 유저 찾기 · 기록 화면 ─────────────────────────────────────────────

def _member_label(m) -> str:
    dn = getattr(m, "display_name", "") or ""
    return f"{dn} ({m.name})" if dn and dn != m.name else str(m.name)


async def resolve_users(guild, raw: str) -> list:
    """입력(ID · 멘션 · 이름)으로 후보를 찾습니다 → [(user_id, 표시 이름), ...]

    **멤버 목록은 캐시로 봅니다.** 6,500명을 REST 로 훑으면 이벤트 루프가
    밀려 다른 명령어가 3초 제한을 넘깁니다 (/연동명단 에서 겪은 일).
    서버를 나간 사람·밴된 사람은 **처벌 기록에 남은 이름**으로 찾습니다.
    """
    uid = parse_user_id(raw)
    if uid:
        m = guild.get_member(uid)
        if m is not None:
            return [(uid, _member_label(m))]
        hits = [h for h in await get_user_cases(guild.id, uid, limit=1)]
        return [(uid, hits[0].get("user_name") if hits else str(uid))]

    q = (raw or "").strip().lower()
    if not q:
        return []
    found = {}
    for m in guild.members:
        names = [m.name, getattr(m, "display_name", ""), getattr(m, "global_name", "") or ""]
        if any(q in (n or "").lower() for n in names):
            found[m.id] = _member_label(m)
            if len(found) >= 25:
                break
    for uid_s, name in await find_users_by_name(guild.id, raw, limit=25):
        if len(found) >= 25:
            break
        found.setdefault(int(uid_s), f"{name} (서버에 없음)")
    return list(found.items())


async def fetch_ban_state(guild, user_id: int):
    """(밴 상태, 디스코드에 남은 사유). 봇 밖에서(우클릭) 한 밴도 여기서 잡힙니다."""
    try:
        entry = await guild.fetch_ban(discord.Object(id=user_id))
        return "banned", (entry.reason or "")
    except discord.NotFound:
        return "clear", ""
    except discord.Forbidden:
        return "unknown", ""
    except discord.HTTPException as e:
        print(f"[Moderation] 밴 상태 조회 실패: {type(e).__name__}: {e}", flush=True)
        return "unknown", ""


def current_ban_case(cases: list) -> dict | None:
    """**지금 걸려 있는 밴**의 봇 기록. `cases` 는 최신 순입니다.

    가장 최근의 밴/해제가 '해제' 라면, 지금 밴은 그 뒤에 봇 밖에서(우클릭)
    다시 걸린 것이라 봇 기록에는 없습니다. 그냥 '마지막 밴' 을 고르면
    **예전에 풀린 밴의 사유**를 지금 사유로 보여 줍니다.
    """
    last = next((c for c in cases if c.get("kind") in ("ban", "unban")), None)
    return last if last is not None and last.get("kind") == "ban" else None


def _case_ts(c: dict) -> int | None:
    """기록 시각(유닉스 초). Mongo 는 tz 없는 UTC datetime 을, JSON 폴백은
    ISO 문자열을 돌려주므로 둘 다 받습니다. 없으면 KST 문자열에서 읽습니다."""
    from datetime import datetime, timezone, timedelta
    v = c.get("created_at")
    try:
        if isinstance(v, str):
            v = datetime.fromisoformat(v)
        if isinstance(v, datetime):
            if v.tzinfo is None:
                v = v.replace(tzinfo=timezone.utc)
            return int(v.timestamp())
        raw = c.get("created") or ""
        return int(datetime.strptime(raw, "%Y-%m-%d %H:%M")
                   .replace(tzinfo=timezone(timedelta(hours=9))).timestamp())
    except (TypeError, ValueError):
        return None


def _kst_ts(raw: str) -> int | None:
    from datetime import datetime, timezone, timedelta
    try:
        return int(datetime.strptime(raw or "", "%Y-%m-%d %H:%M")
                   .replace(tzinfo=timezone(timedelta(hours=9))).timestamp())
    except ValueError:
        return None


def when(c: dict) -> str:
    """디스코드 시각 표기. 보는 사람의 언어·시간대로 **연월일 시분**이
    그대로 보입니다(예: 2026년 9월 25일 오후 2:03). 못 읽으면 KST 글자로."""
    ts = _case_ts(c)
    return f"<t:{ts}:f>" if ts else f"{c.get('created', '?')} KST"


# 한 임베드는 6,000자 · 25칸이 상한이고 넘기면 **전송 자체가 실패**합니다.
# 줄마다 시각을 넣으면 길어지므로, 넘치면 보여 줄 건수를 줄여 다시 만듭니다.
RECORD_LIMITS = (15, 8, 10)          # 유효 경고 · 해제된 경고 · 처벌 기록
EMBED_BUDGET = 5800


def build_record_embed(guild, user_id: int, name: str, cases: list,
                       ban_state: str, ban_reason: str) -> discord.Embed:
    la, lr, lp = RECORD_LIMITS
    while True:
        embed = _build_record_embed(guild, user_id, name, cases, ban_state,
                                    ban_reason, la, lr, lp)
        if (len(embed) <= EMBED_BUDGET and len(embed.fields) <= 25) or la + lr + lp <= 3:
            return embed
        la, lr, lp = max(1, la * 2 // 3), max(1, lr * 2 // 3), max(1, lp * 2 // 3)


def _build_record_embed(guild, user_id, name, cases, ban_state, ban_reason,
                        la, lr, lp) -> discord.Embed:
    member = guild.get_member(user_id)
    warns = [c for c in cases if c.get("kind") == "warn"]
    active = [c for c in warns if c.get("active")]
    removed = [c for c in warns if not c.get("active")]
    punish = [c for c in cases if c.get("kind") in ("ban", "unban", "kick", "timeout")]

    state = {"banned": "🔨 **현재 밴 상태**", "clear": "✅ 밴 아님",
             "unknown": "❔ 밴 상태 확인 불가 (봇에게 '멤버 차단' 권한 필요)"}[ban_state]
    where = "🟢 서버에 있음" if member is not None else "⚪ 서버에 없음"
    color = (discord.Color.red() if ban_state == "banned"
             else discord.Color.gold() if active else discord.Color.green())
    embed = discord.Embed(
        title=f"🔎 {name} 님의 처벌 기록",
        description=(f"<@{user_id}> · `{user_id}`\n{where} · {state}\n"
                     f"유효 경고 **{len(active)}회** · 해제된 경고 {len(removed)}회 · "
                     f"처벌 기록 {len(punish)}건"),
        color=color)
    if member is not None:
        embed.set_thumbnail(url=member.display_avatar.url)

    if ban_state == "banned":
        last_ban = current_ban_case(cases)
        why = (last_ban or {}).get("reason") or ban_reason or "사유 없음"
        how = (f"🕒 {when(last_ban)} · 케이스 `#{last_ban['case']}` · "
               f"처리자 {_truncate(last_ban.get('mod_name', '?'), 30)}" if last_ban
               else "-# 봇 밖에서(우클릭 등) 밴되어 디스코드에 남은 사유만 있습니다 "
                    "(밴한 시각은 알 수 없습니다)")
        embed.add_field(name="🔨 밴 사유", value=f"{_truncate(why, 800)}\n{how}",
                        inline=False)

    # 분모 없는 개수는 쓸모없습니다 — 몇 건 중 몇 건을 보여 주는지 적습니다
    def label(title, total, shown):
        return f"{title} ({total}건" + (f" 중 최근 {shown}건)" if total > shown else ")")

    shown = active[:la]
    lines = [f"`#{c['case']}` 🕒 {when(c)}\n└ {_truncate(c.get('reason'), 80)}"
             f" — {_truncate(c.get('mod_name'), 20)}" for c in shown]
    for i, chunk in enumerate(_chunk_lines(lines) or ["없음"]):
        embed.add_field(name=label("⚠️ 유효 경고", len(active), len(shown)) if i == 0 else "\u200b",
                        value=chunk, inline=False)

    if removed:
        rs = removed[:lr]
        lines = []
        for c in rs:
            off = _kst_ts(c.get("removed_at"))
            off_s = f"<t:{off}:f>" if off else (c.get("removed_at") or "?")
            lines.append(f"~~`#{c['case']}`~~ 🕒 {when(c)} · {_truncate(c.get('reason'), 60)}\n"
                         f"└ 해제 {off_s} — {_truncate(c.get('removed_by_name'), 20)}"
                         + (f" ({_truncate(c.get('removed_reason'), 40)})"
                            if c.get("removed_reason") else ""))
        for i, chunk in enumerate(_chunk_lines(lines)):
            embed.add_field(name=label("♻️ 해제된 경고", len(removed), len(rs)) if i == 0 else "\u200b",
                            value=chunk, inline=False)

    if punish:
        ps = punish[:lp]
        lines = []
        for c in ps:
            emoji, lab, _ = KIND_META[c["kind"]]
            extra = f" {fmt_minutes(c['minutes'])}" if c.get("minutes") else ""
            auto = " (자동)" if c.get("auto") else ""
            lines.append(f"{emoji} `#{c['case']}` **{lab}{extra}**{auto} 🕒 {when(c)}\n"
                         f"└ {_truncate(c.get('reason'), 70)} — {_truncate(c.get('mod_name'), 20)}")
        for i, chunk in enumerate(_chunk_lines(lines)):
            embed.add_field(name=label("🗂️ 처벌 기록", len(punish), len(ps)) if i == 0 else "\u200b",
                            value=chunk, inline=False)

    embed.set_footer(text="🕒 시각은 보는 사람의 시간대로 표시됩니다")
    return embed


async def show_record(interaction: discord.Interaction, user_id: int, name: str,
                      edit: bool = False) -> None:
    guild = interaction.guild
    cases = await get_user_cases(guild.id, user_id)
    ban_state, ban_reason = await fetch_ban_state(guild, user_id)
    embed = build_record_embed(guild, user_id, name, cases, ban_state, ban_reason)
    has_active = any(c.get("kind") == "warn" and c.get("active") for c in cases)
    view = UserRecordView(user_id, has_active, ban_state == "banned")
    if edit:
        # 호출한 쪽이 이미 defer 했습니다 (DB 를 읽는 동안 3초를 넘기지 않게)
        await interaction.edit_original_response(embed=embed, view=view)
    else:
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)


async def _require(interaction: discord.Interaction, perm: str) -> bool:
    """버튼·모달에서도 권한을 다시 봅니다. 패널이 어디에 남아 있든
    **누른 사람**의 권한으로 판정해야 합니다."""
    p = interaction.user.guild_permissions
    if getattr(p, "administrator", False) or getattr(p, perm, False):
        return True
    await interaction.response.send_message(
        f"⛔ **{PERM_LABEL.get(perm, perm)}** 권한이 있어야 쓸 수 있습니다.", ephemeral=True)
    return False


# ── 여러 명이 걸렸을 때 고르기 ────────────────────────────────────────

class PickUserSelect(discord.ui.Select):
    def __init__(self, candidates: list, action: str, reason: str = ""):
        self.action, self.reason = action, reason
        # 라벨이 비면 디스코드가 메뉴 전송을 통째로 거부합니다
        options = [discord.SelectOption(label=_truncate(name or str(uid), 100), value=str(uid),
                                        description=str(uid))
                   for uid, name in candidates[:25]]
        super().__init__(placeholder="누구인지 골라 주세요", options=options)
        self._names = {str(uid): name for uid, name in candidates}

    async def callback(self, interaction: discord.Interaction):
        uid = int(self.values[0])
        name = self._names.get(self.values[0], str(uid))
        # 아래는 전부 DB·디스코드 왕복이 있어 먼저 응답해 둡니다
        await interaction.response.defer()
        if self.action == "search":
            await show_record(interaction, uid, name, edit=True)
        elif self.action == "unwarn":
            await show_unwarn(interaction, uid, name, self.reason, edit=True)
        elif self.action == "unban":
            embed = await do_unban(interaction, uid, self.reason)
            await interaction.edit_original_response(embed=embed, view=None)
        else:
            print(f"[Moderation] 모르는 선택 동작: {self.action}", flush=True)
            await interaction.followup.send(
                f"⚠️ 처리 경로가 없습니다 (`{self.action}`). 개발자에게 알려 주세요.",
                ephemeral=True)


class PickUserView(discord.ui.View):
    def __init__(self, candidates, action, reason=""):
        super().__init__(timeout=300)
        self.add_item(PickUserSelect(candidates, action, reason))


async def _resolve_or_pick(interaction: discord.Interaction, raw: str, action: str,
                           reason: str = ""):
    """후보가 하나면 (id, 이름), 없거나 여럿이면 안내를 보내고 None."""
    cands = await resolve_users(interaction.guild, raw)
    if not cands:
        await interaction.followup.send(
            f"🔍 `{_truncate(raw, 50)}` 에 해당하는 유저를 찾지 못했습니다.\n"
            "-# 서버를 나간 사람은 **유저 ID** 로 찾아 주세요 "
            "(개발자 모드 → 프로필 우클릭 → ID 복사).", ephemeral=True)
        return None
    if len(cands) > 1:
        await interaction.followup.send(
            f"🔍 **{len(cands)}명**이 검색됐습니다. 누구인지 골라 주세요.",
            view=PickUserView(cands, action, reason), ephemeral=True)
        return None
    return cands[0]


# ── 경고 해제 ──────────────────────────────────────────────────────────

class UnwarnModal(discord.ui.Modal, title="♻️ 경고 해제"):
    def __init__(self, default_user: str = ""):
        super().__init__()
        self.user_input = discord.ui.TextInput(
            label="유저 (ID · 멘션 · 이름)", placeholder="예: 1531611325493678132",
            default=default_user or None, max_length=100)
        self.reason_input = discord.ui.TextInput(
            label="해제 사유 (선택)", placeholder="예: 오해로 확인됨",
            required=False, max_length=200)
        self.add_item(self.user_input)
        self.add_item(self.reason_input)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        hit = await _resolve_or_pick(interaction, self.user_input.value, "unwarn",
                                     self.reason_input.value or "")
        if hit:
            await show_unwarn(interaction, hit[0], hit[1], self.reason_input.value or "")


async def show_unwarn(interaction: discord.Interaction, user_id: int, name: str,
                      reason: str, edit: bool = False) -> None:
    warns = await get_active_warnings(interaction.guild.id, user_id)
    if not warns:
        embed = discord.Embed(title="♻️ 경고 해제",
                              description=f"<@{user_id}> 님은 **유효한 경고가 없습니다.**",
                              color=discord.Color.greyple())
        view = None
    else:
        lines = [f"`#{w['case']}` 🕒 {when(w)} · {_truncate(w.get('reason'), 80)}"
                 for w in warns[:25]]
        embed = discord.Embed(
            title=f"♻️ {name} 님의 경고 해제",
            description=(f"유효 경고 **{len(warns)}회**. 풀 경고를 고르거나 "
                         "**전체 해제**를 누르세요.\n\n" + "\n".join(lines))[:4000],
            color=discord.Color.gold())
        if len(warns) > 25:
            embed.set_footer(text=f"목록에는 최근 25건만 보입니다 (전체 {len(warns)}건) · "
                                  "전체 해제는 전부 풉니다")
        view = UnwarnView(user_id, name, warns, reason)
    if edit:
        await interaction.edit_original_response(embed=embed, view=view)
    elif view is None:
        await interaction.followup.send(embed=embed, ephemeral=True)
    else:
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)


async def stored_name(guild, user_id: int, fallback: str = "") -> str:
    """기록에 남길 이름 (사용자명). 화면용 이름을 넣으면 안 됩니다.

    검색 후보의 이름은 `표시이름 (사용자명)` · `이름 (서버에 없음)` 처럼
    꾸며져 있습니다. 그걸 기록하면 이름 검색이 그 꾸밈을 다시 읽어
    `이름 (서버에 없음) (서버에 없음)` 이 되고, 사용자명으로는 안 찾아집니다.
    """
    m = guild.get_member(user_id)
    if m is not None:
        return str(m)
    for c in await get_user_cases(guild.id, user_id, limit=20):
        if c.get("user_name"):
            return c["user_name"]
    return fallback or str(user_id)


async def _finish_unwarn(interaction, user_id, name, cases, reason):
    guild = interaction.guild
    await interaction.response.defer()
    done = await remove_warnings(guild.id, user_id, cases, interaction.user.id,
                                 str(interaction.user), reason)
    if not done:
        embed = discord.Embed(title="♻️ 경고 해제",
                              description="해제할 경고가 없습니다 (이미 누가 풀었을 수 있습니다).",
                              color=discord.Color.greyple())
        await interaction.edit_original_response(embed=embed, view=None)
        return
    remain = len(await get_active_warnings(guild.id, user_id))
    nums = ", ".join(f"#{c}" for c in sorted(done))
    doc = await add_case(guild.id, "unwarn", user_id,
                         await stored_name(guild, user_id, str(user_id)), interaction.user.id,
                         str(interaction.user),
                         f"경고 {nums} 해제" + (f" — {reason}" if reason else ""))
    settings = await get_mod_settings(guild.id)
    await send_log(guild, settings, build_log_embed(
        doc, [("남은 유효 경고", f"{remain}회")]))
    embed = discord.Embed(
        title="✅ 경고 해제 완료",
        description=(f"<@{user_id}> 님의 경고 **{len(done)}건**({nums})을 풀었습니다.\n"
                     f"남은 유효 경고: **{remain}회**"),
        color=discord.Color.green())
    await interaction.edit_original_response(embed=embed, view=None)


class UnwarnSelect(discord.ui.Select):
    def __init__(self, warns):
        options = [discord.SelectOption(
            label=_truncate(f"#{w['case']} · {w.get('reason') or '사유 없음'}", 100),
            value=str(w["case"]),
            description=_truncate(f"{w.get('created', '')} · {w.get('mod_name', '')}", 100))
            for w in warns[:25]]
        super().__init__(placeholder="풀 경고를 고르세요 (여러 개 가능)",
                         min_values=1, max_values=len(options), options=options)

    async def callback(self, interaction: discord.Interaction):
        v: UnwarnView = self.view
        await _finish_unwarn(interaction, v.user_id, v.name,
                             [int(x) for x in self.values], v.reason)


class UnwarnView(discord.ui.View):
    def __init__(self, user_id, name, warns, reason):
        super().__init__(timeout=300)
        self.user_id, self.name, self.reason = user_id, name, reason
        self.add_item(UnwarnSelect(warns))

    @discord.ui.button(label="전체 해제", style=discord.ButtonStyle.danger, emoji="🧹", row=1)
    async def clear_all(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _finish_unwarn(interaction, self.user_id, self.name, None, self.reason)


# ── 밴 해제 ────────────────────────────────────────────────────────────

async def do_unban(interaction: discord.Interaction, user_id: int, reason: str) -> discord.Embed:
    guild = interaction.guild
    state, discord_reason = await fetch_ban_state(guild, user_id)
    if state == "clear":
        return discord.Embed(title="🔓 밴 해제",
                             description=f"<@{user_id}> (`{user_id}`) 님은 **밴 상태가 아닙니다.**",
                             color=discord.Color.greyple())
    try:
        await guild.unban(discord.Object(id=user_id),
                          reason=_audit_reason(interaction.user, reason or "밴 해제"))
    except discord.Forbidden:
        return discord.Embed(title="⚠️ 밴 해제 실패",
                             description="봇에게 **멤버 차단** 권한이 없습니다.",
                             color=discord.Color.orange())
    except discord.NotFound:
        return discord.Embed(title="🔓 밴 해제",
                             description=f"<@{user_id}> 님은 **밴 상태가 아닙니다.**",
                             color=discord.Color.greyple())
    except discord.HTTPException as e:
        return discord.Embed(title="⚠️ 밴 해제 실패",
                             description=f"디스코드 오류 ({e.status}). 잠시 후 다시 시도하세요.",
                             color=discord.Color.orange())

    cases = await get_user_cases(guild.id, user_id, limit=50)
    last_ban = current_ban_case(cases)
    name = next((c["user_name"] for c in cases if c.get("user_name")), str(user_id))
    doc = await add_case(guild.id, "unban", user_id, name, interaction.user.id,
                         str(interaction.user), reason or "사유 없음")
    was = (last_ban or {}).get("reason") or discord_reason or "기록 없음"
    settings = await get_mod_settings(guild.id)
    await send_log(guild, settings, build_log_embed(doc, [("원래 밴 사유", _truncate(was, 1000))]))
    embed = discord.Embed(
        title="✅ 밴 해제 완료",
        description=f"<@{user_id}> (`{name}`) 님의 밴을 풀었습니다. 케이스 `#{doc['case']}`",
        color=discord.Color.green())
    embed.add_field(name="원래 밴 사유", value=_truncate(was, 1000), inline=False)
    embed.set_footer(text="밴이 풀려도 서버에 자동으로 다시 들어오지는 않습니다")
    return embed


class UnbanModal(discord.ui.Modal, title="🔓 밴 해제"):
    def __init__(self, default_user: str = ""):
        super().__init__()
        self.user_input = discord.ui.TextInput(
            label="유저 ID (또는 기록에 남은 이름)", placeholder="예: 1531611325493678132",
            default=default_user or None, max_length=100)
        self.reason_input = discord.ui.TextInput(
            label="해제 사유 (선택)", required=False, max_length=200)
        self.add_item(self.user_input)
        self.add_item(self.reason_input)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        hit = await _resolve_or_pick(interaction, self.user_input.value, "unban",
                                     self.reason_input.value or "")
        if hit:
            embed = await do_unban(interaction, hit[0], self.reason_input.value or "")
            await interaction.followup.send(embed=embed, ephemeral=True)


# ── 유저 검색 ──────────────────────────────────────────────────────────

class SearchModal(discord.ui.Modal, title="🔎 유저 처벌 기록 검색"):
    user_input = discord.ui.TextInput(
        label="유저 (ID · 멘션 · 이름 일부)", placeholder="예: 1531611325493678132 또는 닉네임",
        max_length=100)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        hit = await _resolve_or_pick(interaction, self.user_input.value, "search")
        if hit:
            await show_record(interaction, hit[0], hit[1])


class UserRecordView(discord.ui.View):
    """검색 결과 아래에서 바로 풀 수 있게 합니다 (유저 ID 를 채운 채로)."""

    def __init__(self, user_id: int, has_active: bool, banned: bool):
        super().__init__(timeout=600)
        self.user_id = user_id
        self.unwarn_btn.disabled = not has_active
        self.unban_btn.disabled = not banned

    @discord.ui.button(label="이 유저 경고 해제", style=discord.ButtonStyle.primary, emoji="♻️")
    async def unwarn_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if await _require(interaction, "moderate_members"):
            await interaction.response.send_modal(UnwarnModal(str(self.user_id)))

    @discord.ui.button(label="이 유저 밴 해제", style=discord.ButtonStyle.success, emoji="🔓")
    async def unban_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if await _require(interaction, "ban_members"):
            await interaction.response.send_modal(UnbanModal(str(self.user_id)))


# ── 밴 DM 설정 ─────────────────────────────────────────────────────────

def build_dm_settings_embed(settings: dict) -> discord.Embed:
    link = settings.get("support_link") or ""
    embed = discord.Embed(
        title="✉️ 밴 DM 설정",
        description=("밴하기 **직전에** 대상에게 사유와 서포트 서버 링크를 DM 으로 보냅니다.\n"
                     "-# 밴한 뒤에는 서버를 같이 쓰지 않아 DM 이 막히므로 먼저 보냅니다. "
                     "밴이 실패하면 보낸 DM 은 지웁니다."),
        color=discord.Color.blurple())
    embed.add_field(name="🔨 밴 DM", value="🟢 켜짐" if settings.get("ban_dm") else "⚫ 꺼짐",
                    inline=True)
    embed.add_field(name="⚠️ 경고 DM", value="🟢 켜짐" if settings.get("warn_dm") else "⚫ 꺼짐",
                    inline=True)
    embed.add_field(name="🛟 서포트 서버 링크",
                    value=link or "없음 — **✏️ 문구·링크 수정** 에서 넣어 주세요",
                    inline=False)
    embed.add_field(name="💬 덧붙일 문구",
                    value=_truncate(settings.get("ban_dm_message"), 1000) or
                    "기본값: 서버 규칙 위반으로 차단되었습니다.", inline=False)
    embed.set_footer(text="👀 미리보기로 받는 사람 화면을 확인할 수 있습니다")
    return embed


class BanDmModal(discord.ui.Modal, title="✉️ 밴 DM 문구·링크"):
    def __init__(self, settings: dict):
        super().__init__()
        self.link_input = discord.ui.TextInput(
            label="서포트 서버 초대 링크 (비우면 링크 없음)",
            placeholder="예: https://discord.gg/abcd1234",
            default=settings.get("support_link") or None, required=False, max_length=200)
        self.msg_input = discord.ui.TextInput(
            label="덧붙일 문구 (선택)", style=discord.TextStyle.paragraph,
            placeholder="예: 이의 제기는 서포트 서버의 #문의 채널에서 받습니다.",
            default=settings.get("ban_dm_message") or None, required=False, max_length=800)
        self.add_item(self.link_input)
        self.add_item(self.msg_input)

    async def on_submit(self, interaction: discord.Interaction):
        link = (self.link_input.value or "").strip()
        if link and not LINK_RE.match(link):
            await interaction.response.send_message(
                "⚠️ 링크는 `https://` 로 시작하고 공백이 없어야 합니다.\n"
                "-# 형식이 틀린 링크를 넣으면 DM 전송 자체가 실패합니다.", ephemeral=True)
            return
        await interaction.response.defer()
        await save_mod_settings(interaction.guild.id, {
            "support_link": link, "ban_dm_message": (self.msg_input.value or "").strip()})
        settings = await get_mod_settings(interaction.guild.id)
        await interaction.edit_original_response(embed=build_dm_settings_embed(settings),
                                                 view=BanDmView(settings))


class BanDmView(discord.ui.View):
    def __init__(self, settings: dict):
        super().__init__(timeout=600)
        self.ban_toggle.label = f"밴 DM: {'켜짐' if settings.get('ban_dm') else '꺼짐'}"
        self.ban_toggle.style = (discord.ButtonStyle.success if settings.get("ban_dm")
                                 else discord.ButtonStyle.secondary)
        self.warn_toggle.label = f"경고 DM: {'켜짐' if settings.get('warn_dm') else '꺼짐'}"
        self.warn_toggle.style = (discord.ButtonStyle.success if settings.get("warn_dm")
                                  else discord.ButtonStyle.secondary)

    @discord.ui.button(label="문구·링크 수정", style=discord.ButtonStyle.primary, emoji="✏️")
    async def edit_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await _require(interaction, "administrator"):
            return
        await interaction.response.send_modal(
            BanDmModal(await get_mod_settings(interaction.guild.id)))

    async def _toggle(self, interaction, key):
        if not await _require(interaction, "administrator"):
            return
        await interaction.response.defer()
        settings = await get_mod_settings(interaction.guild.id)
        await save_mod_settings(interaction.guild.id, {key: not settings.get(key)})
        settings = await get_mod_settings(interaction.guild.id)
        await interaction.edit_original_response(embed=build_dm_settings_embed(settings),
                                                 view=BanDmView(settings))

    @discord.ui.button(label="밴 DM", emoji="🔨")
    async def ban_toggle(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._toggle(interaction, "ban_dm")

    @discord.ui.button(label="경고 DM", emoji="⚠️")
    async def warn_toggle(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._toggle(interaction, "warn_dm")

    @discord.ui.button(label="미리보기", style=discord.ButtonStyle.secondary, emoji="👀", row=1)
    async def preview_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        settings = await get_mod_settings(interaction.guild.id)
        embed, view = build_ban_dm(interaction.guild, "(여기에 밴 사유가 들어갑니다)", settings)
        kw = {"view": view} if view is not None else {}
        await interaction.followup.send(
            content="👀 밴된 사람은 DM 으로 **이렇게** 받습니다"
                    + ("" if settings.get("ban_dm") else " — ⚫ 지금은 **꺼져 있어** 보내지 않습니다"),
            embed=embed, ephemeral=True, **kw)


# ── 로그 채널 ──────────────────────────────────────────────────────────

class LogChannelSelect(discord.ui.ChannelSelect):
    def __init__(self):
        super().__init__(placeholder="처벌 기록을 올릴 채널", channel_types=[discord.ChannelType.text])

    async def callback(self, interaction: discord.Interaction):
        if not await _require(interaction, "administrator"):
            return
        picked = self.values[0]
        channel = interaction.guild.get_channel(picked.id)
        await interaction.response.defer()
        await save_mod_settings(interaction.guild.id, {"log_channel_id": picked.id})
        # 초록불만 띄우면 운영진은 며칠 뒤에야 로그가 안 올라간 걸 압니다
        perms = channel.permissions_for(interaction.guild.me) if channel else None
        if perms is None or not (perms.send_messages and perms.embed_links):
            embed = discord.Embed(
                title="⚠️ 저장했지만 로그가 올라가지 않습니다",
                description=(f"{picked.mention} 에 봇의 **메시지 보내기 · 링크 첨부** 권한이 "
                             "없습니다. 채널 권한을 고쳐 주세요."),
                color=discord.Color.orange())
        else:
            embed = discord.Embed(title="✅ 로그 채널 설정 완료",
                                  description=f"이제 모든 경고·밴이 {picked.mention} 에 기록됩니다.",
                                  color=discord.Color.green())
        await interaction.edit_original_response(embed=embed, view=None)


class LogChannelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)
        self.add_item(LogChannelSelect())

    @discord.ui.button(label="로그 끄기", style=discord.ButtonStyle.secondary, emoji="🚫", row=1)
    async def off_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await _require(interaction, "administrator"):
            return
        await interaction.response.defer()
        await save_mod_settings(interaction.guild.id, {"log_channel_id": None})
        await interaction.edit_original_response(
            embed=discord.Embed(title="🚫 로그 채널 해제",
                                description="처벌 기록은 DB 에만 남고 채널에는 올라가지 않습니다.",
                                color=discord.Color.greyple()), view=None)


# ── 자동 처벌 ──────────────────────────────────────────────────────────

MAX_AUTO_RULES = 25


def build_auto_embed(guild, settings: dict) -> discord.Embed:
    rules = sorted(settings.get("auto_rules") or [], key=lambda r: int(r.get("count", 0)))
    lines = [f"• {describe_rule(r)}" for r in rules] or ["없음 — 경고만 쌓이고 자동 처벌은 없습니다"]
    embed = discord.Embed(
        title="⚖️ 경고 누적 자동 처벌",
        description=("유효 경고가 **정확히 그 횟수가 되는 순간** 한 번 실행됩니다.\n"
                     "-# 경고를 풀었다가 다시 쌓이면 다시 실행됩니다.\n\n" + "\n".join(lines)),
        color=discord.Color.dark_gold())
    # 권한이 없으면 규칙은 있는데 아무 일도 안 일어납니다 — 미리 알려 줍니다
    missing = sorted({PERM_LABEL[ACTION_PERM[r["action"]]] for r in rules
                      if r.get("action") in ACTION_PERM
                      and not getattr(guild.me.guild_permissions, ACTION_PERM[r["action"]], False)})
    if missing:
        embed.add_field(name="⚠️ 봇 권한 부족",
                        value=f"봇에게 **{', '.join(missing)}** 권한이 없어 해당 규칙이 실패합니다.",
                        inline=False)
    return embed


class AutoRuleModal(discord.ui.Modal, title="⚖️ 자동 처벌 규칙"):
    count_input = discord.ui.TextInput(label="경고 몇 회에서?", placeholder="예: 3", max_length=3)
    action_input = discord.ui.TextInput(label="처벌 (타임아웃 · 추방 · 밴)",
                                        placeholder="예: 타임아웃", max_length=10)
    minutes_input = discord.ui.TextInput(label="타임아웃 시간(분) · 타임아웃일 때만",
                                         placeholder="예: 60 (최대 40320 = 28일)",
                                         required=False, max_length=6)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            count = int(self.count_input.value.strip())
        except ValueError:
            count = 0
        action = ACTION_ALIASES.get(self.action_input.value.strip().lower())
        minutes = 60
        if (self.minutes_input.value or "").strip():
            try:
                minutes = int(self.minutes_input.value.strip())
            except ValueError:
                minutes = -1
        problem = None
        if not 1 <= count <= 50:
            problem = "경고 횟수는 1~50 사이 숫자로 적어 주세요."
        elif action is None:
            problem = "처벌은 **타임아웃 · 추방 · 밴** 중 하나로 적어 주세요."
        elif action == "timeout" and not 1 <= minutes <= TIMEOUT_MAX_MINUTES:
            problem = f"타임아웃은 1~{TIMEOUT_MAX_MINUTES}분(28일) 사이여야 합니다."
        if problem:
            await interaction.response.send_message(f"⚠️ {problem}", ephemeral=True)
            return
        await interaction.response.defer()
        settings = await get_mod_settings(interaction.guild.id)
        # 같은 횟수의 규칙은 바꿔 끼웁니다. 둘이면 어느 쪽이 도는지 알 수 없습니다.
        rules = [r for r in settings.get("auto_rules") or [] if int(r.get("count", 0)) != count]
        # 지우기 메뉴는 25개까지만 담습니다. 넘기면 **지울 수 없는 규칙**이 생깁니다.
        if len(rules) >= MAX_AUTO_RULES:
            await interaction.followup.send(
                f"⚠️ 자동 처벌 규칙은 최대 **{MAX_AUTO_RULES}개**입니다. 먼저 하나를 지워 주세요.",
                ephemeral=True)
            return
        rule = {"count": count, "action": action}
        if action == "timeout":
            rule["minutes"] = minutes
        rules.append(rule)
        rules.sort(key=lambda r: int(r["count"]))
        await save_mod_settings(interaction.guild.id, {"auto_rules": rules})
        settings["auto_rules"] = rules
        await interaction.edit_original_response(embed=build_auto_embed(interaction.guild, settings),
                                                 view=AutoRuleView(settings))


class AutoRuleDeleteSelect(discord.ui.Select):
    def __init__(self, rules):
        options = [discord.SelectOption(label=_truncate(describe_rule(r).replace("**", ""), 100),
                                        value=str(r["count"])) for r in rules[:25]]
        super().__init__(placeholder="지울 규칙 고르기", options=options, row=1)

    async def callback(self, interaction: discord.Interaction):
        if not await _require(interaction, "administrator"):
            return
        count = int(self.values[0])
        await interaction.response.defer()
        settings = await get_mod_settings(interaction.guild.id)
        rules = [r for r in settings.get("auto_rules") or [] if int(r.get("count", 0)) != count]
        await save_mod_settings(interaction.guild.id, {"auto_rules": rules})
        settings["auto_rules"] = rules
        await interaction.edit_original_response(embed=build_auto_embed(interaction.guild, settings),
                                                 view=AutoRuleView(settings))


class AutoRuleView(discord.ui.View):
    def __init__(self, settings: dict):
        super().__init__(timeout=600)
        rules = settings.get("auto_rules") or []
        if rules:
            self.add_item(AutoRuleDeleteSelect(rules))

    @discord.ui.button(label="규칙 추가·수정", style=discord.ButtonStyle.primary, emoji="➕")
    async def add_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if await _require(interaction, "administrator"):
            await interaction.response.send_modal(AutoRuleModal())


# ── /경고설정 패널 ─────────────────────────────────────────────────────

def build_panel_embed(guild, settings: dict) -> discord.Embed:
    embed = discord.Embed(
        title="🛡️ 경고 · 밴 관리",
        description=("`/경고 유저 사유` · `/밴 유저 사유` 로 처벌하고, "
                     "아래 버튼으로 풀거나 기록을 찾습니다."),
        color=discord.Color.dark_red())
    cid = settings.get("log_channel_id")
    embed.add_field(name="📋 로그 채널",
                    value=f"<#{cid}>" if cid else "없음 — 기록은 DB 에만 남습니다", inline=True)
    embed.add_field(
        name="✉️ 밴 DM",
        value=("🟢 켜짐" if settings.get("ban_dm") else "⚫ 꺼짐")
        + (" · 서포트 링크 있음" if settings.get("support_link") else " · 서포트 링크 없음"),
        inline=True)
    embed.add_field(name="⚠️ 경고 DM",
                    value="🟢 켜짐" if settings.get("warn_dm") else "⚫ 꺼짐", inline=True)
    rules = sorted(settings.get("auto_rules") or [], key=lambda r: int(r.get("count", 0)))
    # 칸이 1024자를 넘으면 패널 **전송 자체가 실패**해 /경고설정 이 안 열립니다
    for i, chunk in enumerate(_chunk_lines([describe_rule(r) for r in rules]) or ["없음"]):
        embed.add_field(name="⚖️ 자동 처벌" if i == 0 else "\u200b", value=chunk, inline=False)
    embed.set_footer(text="이 화면은 본인에게만 보입니다")
    return embed


class ModPanelView(discord.ui.View):
    """영구 View — 재시작 뒤에도 버튼이 살아 있어야 합니다.

    **인스턴스를 모두가 공유**하므로 `self` 에 상태를 두지 않습니다.
    누른 사람의 권한은 콜백마다 다시 봅니다.
    """

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="경고 해제", style=discord.ButtonStyle.primary, emoji="♻️",
                       custom_id="mod:panel:unwarn", row=0)
    async def unwarn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if await _require(interaction, "moderate_members"):
            await interaction.response.send_modal(UnwarnModal())

    @discord.ui.button(label="밴 해제", style=discord.ButtonStyle.success, emoji="🔓",
                       custom_id="mod:panel:unban", row=0)
    async def unban(self, interaction: discord.Interaction, button: discord.ui.Button):
        if await _require(interaction, "ban_members"):
            await interaction.response.send_modal(UnbanModal())

    @discord.ui.button(label="유저 검색", style=discord.ButtonStyle.secondary, emoji="🔎",
                       custom_id="mod:panel:search", row=0)
    async def search(self, interaction: discord.Interaction, button: discord.ui.Button):
        if await _require(interaction, "moderate_members"):
            await interaction.response.send_modal(SearchModal())

    @discord.ui.button(label="밴 DM 설정", style=discord.ButtonStyle.secondary, emoji="✉️",
                       custom_id="mod:panel:dm", row=1)
    async def dm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await _require(interaction, "administrator"):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        settings = await get_mod_settings(interaction.guild.id)
        await interaction.followup.send(embed=build_dm_settings_embed(settings),
                                        view=BanDmView(settings), ephemeral=True)

    @discord.ui.button(label="로그 채널", style=discord.ButtonStyle.secondary, emoji="📋",
                       custom_id="mod:panel:log", row=1)
    async def log(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await _require(interaction, "administrator"):
            return
        await interaction.response.send_message(
            embed=discord.Embed(title="📋 로그 채널",
                                description="경고·밴·해제가 기록될 채널을 고르세요.",
                                color=discord.Color.blurple()),
            view=LogChannelView(), ephemeral=True)

    @discord.ui.button(label="자동 처벌", style=discord.ButtonStyle.secondary, emoji="⚖️",
                       custom_id="mod:panel:auto", row=1)
    async def auto(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await _require(interaction, "administrator"):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        settings = await get_mod_settings(interaction.guild.id)
        await interaction.followup.send(embed=build_auto_embed(interaction.guild, settings),
                                        view=AutoRuleView(settings), ephemeral=True)


# ── 경고 · 밴 본체 (슬래시와 ?명령어가 같이 씁니다) ─────────────────────
#
# 두 갈래로 두면 한쪽에만 검사가 들어가 **다른 쪽으로 우회됩니다**
# (뽑기 1회/10회가 그랬습니다). 그래서 본체는 하나이고, 명령어는 입력을
# 받아 넘기고 결과 임베드를 보여 주기만 합니다.

def _result_embed(title: str, color, lines: list) -> discord.Embed:
    return discord.Embed(title=title, description="\n".join(l for l in lines if l), color=color)


async def do_warn(guild, moderator, member, reason: str) -> discord.Embed:
    if getattr(member, "bot", False):
        return _result_embed("⛔ 경고할 수 없습니다", discord.Color.greyple(),
                             ["봇에게는 경고할 수 없습니다."])
    problem = target_problem(guild, moderator, member, need_bot=False)
    if problem:
        return _result_embed("⛔ 경고할 수 없습니다", discord.Color.greyple(), [problem])

    settings = await get_mod_settings(guild.id)
    doc, count = await add_warning(guild.id, member.id, str(member),
                                   moderator.id, str(moderator), reason)
    rule = match_auto_rule(settings.get("auto_rules"), count)
    blocker = auto_rule_blocker(guild, member, rule["action"]) if rule else None
    # 자동 밴이 **실제로 실행되고 밴 DM 이 켜져 있을 때만** 밴 DM(서포트 링크
    # 포함)이 경고 DM 을 대신합니다 — 두 통 보내지 않습니다. 밴 DM 이 꺼져
    # 있거나 밴이 막히면 경고 DM 이라도 가야 합니다(아무것도 안 가면 안 됩니다).
    ban_dm_instead = bool(rule and rule.get("action") == "ban" and not blocker
                          and settings.get("ban_dm"))
    dm_status = "off"
    if settings.get("warn_dm") and not ban_dm_instead:
        note = (f"누적 {count}회로 **{describe_rule(rule).split('→')[-1].strip()}** 처리됩니다."
                if rule and not blocker else "")
        _, dm_status = await safe_dm(member, build_warn_dm(guild, doc, count, note))

    await send_log(guild, settings, build_log_embed(
        doc, [("누적 유효 경고", f"{count}회"), ("DM", DM_STATUS.get(dm_status, dm_status))]))

    auto_line = await apply_auto_rule(guild, member, rule, count, reason, settings) if rule else ""
    return _result_embed(
        f"⚠️ 경고 | 케이스 #{doc['case']}", discord.Color.gold(),
        [f"{member.mention} 님에게 경고를 줬습니다. 누적 유효 경고 **{count}회**",
         f"📝 {_truncate(reason, 300)}",
         "" if ban_dm_instead else DM_STATUS.get(dm_status, dm_status),
         auto_line])


async def do_ban(guild, moderator, user, reason: str, delete_days: int = 0) -> discord.Embed:
    problem = target_problem(guild, moderator, user)
    if not problem and not guild.me.guild_permissions.ban_members:
        problem = "봇에게 **멤버 차단** 권한이 없습니다."
    if not problem:
        state, why = await fetch_ban_state(guild, user.id)
        if state == "banned":
            problem = (f"<@{user.id}> 님은 **이미 밴 상태**입니다."
                       + (f"\n기존 사유: {_truncate(why, 300)}" if why else ""))
    if problem:
        return _result_embed("⛔ 밴할 수 없습니다", discord.Color.greyple(), [problem])

    settings = await get_mod_settings(guild.id)
    res = await execute_ban(guild, moderator, user, reason, settings, delete_days)
    if not res["ok"]:
        return _result_embed("⚠️ 밴 실패", discord.Color.orange(),
                             [res["error"], "-# 보낸 DM 이 있었다면 회수했습니다."])
    return _result_embed(
        f"🔨 밴 | 케이스 #{res['case']['case']}", discord.Color.red(),
        [f"<@{user.id}> (`{user}`) 님을 밴했습니다.",
         f"📝 {_truncate(reason, 300)}",
         DM_STATUS.get(res["dm"], res["dm"]),
         "-# 서포트 링크가 없습니다 — `/경고설정` → 밴 DM 설정에서 넣을 수 있습니다"
         if res["dm"] == "sent" and not settings.get("support_link") else ""])


async def record_embed_for(guild, user) -> discord.Embed:
    cases = await get_user_cases(guild.id, user.id)
    state, why = await fetch_ban_state(guild, user.id)
    member = guild.get_member(user.id)
    name = _member_label(member) if member is not None else str(getattr(user, "name", user.id))
    return build_record_embed(guild, user.id, name, cases, state, why)


PREFIX_USAGE = {
    "warn": "`?warn [유저 ID 또는 멘션] [사유]`",
    "ban": "`?ban [유저 ID 또는 멘션] [사유]`",
    "warnings": "`?warnings [유저 ID 또는 멘션]`",
}


# ── Cog ────────────────────────────────────────────────────────────────

class Moderation(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="경고", description="유저에게 경고를 줍니다 (누적되면 자동 처벌).")
    @app_commands.describe(유저="경고할 유저", 사유="경고 사유 (DM 과 기록에 남습니다)")
    @app_commands.guild_only()
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.checks.has_permissions(moderate_members=True)
    async def warn(self, interaction: discord.Interaction, 유저: discord.Member,
                   사유: app_commands.Range[str, 1, 500]):
        await interaction.response.defer(ephemeral=True, thinking=True)
        embed = await do_warn(interaction.guild, interaction.user, 유저, 사유)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="밴", description="유저를 밴합니다 (밴 직전에 사유·서포트 링크 DM).")
    @app_commands.describe(유저="밴할 유저 (서버에 없으면 ID 로)", 사유="밴 사유 (DM 과 기록에 남습니다)",
                           메시지삭제="최근 며칠 치 메시지를 지울지 (0~7일)")
    @app_commands.guild_only()
    @app_commands.default_permissions(ban_members=True)
    @app_commands.checks.has_permissions(ban_members=True)
    async def ban(self, interaction: discord.Interaction, 유저: discord.User,
                  사유: app_commands.Range[str, 1, 500],
                  메시지삭제: app_commands.Range[int, 0, 7] = 0):
        await interaction.response.defer(ephemeral=True, thinking=True)
        embed = await do_ban(interaction.guild, interaction.user, 유저, 사유, 메시지삭제)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="경고설정", description="[관리자 전용] 경고·밴 관리 패널을 엽니다.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def settings_panel(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        settings = await get_mod_settings(interaction.guild.id)
        await interaction.followup.send(embed=build_panel_embed(interaction.guild, settings),
                                        view=ModPanelView(), ephemeral=True)

    # ── ?명령어 (Dyno 식) ──────────────────────────────────────────────
    #
    # ⚠️ **CLAUDE.md 규칙 1(ephemeral)의 예외입니다.** 접두사 명령어는
    #    상호작용이 아니라 일반 메시지라 '본인에게만 보이기' 가 불가능합니다.
    #    Dyno 에 익숙한 운영진이 요청한 방식이라 채널에 답합니다.
    #    처벌은 슬래시와 **같은 본체**(do_warn · do_ban)를 씁니다.

    @commands.command(name="warn")
    @commands.guild_only()
    @commands.has_permissions(moderate_members=True)
    async def warn_prefix(self, ctx: commands.Context, 유저: discord.Member, *, 사유: str):
        embed = await do_warn(ctx.guild, ctx.author, 유저, 사유[:500])
        await ctx.reply(embed=embed, mention_author=False,
                        allowed_mentions=discord.AllowedMentions.none())

    @commands.command(name="ban")
    @commands.guild_only()
    @commands.has_permissions(ban_members=True)
    async def ban_prefix(self, ctx: commands.Context, 유저: discord.User, *, 사유: str):
        embed = await do_ban(ctx.guild, ctx.author, 유저, 사유[:500])
        await ctx.reply(embed=embed, mention_author=False,
                        allowed_mentions=discord.AllowedMentions.none())

    @commands.command(name="warnings")
    @commands.guild_only()
    @commands.has_permissions(moderate_members=True)
    async def warnings_prefix(self, ctx: commands.Context, 유저: discord.User):
        embed = await record_embed_for(ctx.guild, 유저)
        embed.set_footer(text="🕒 시각은 보는 사람의 시간대로 표시됩니다 · /경고설정 에서 해제")
        await ctx.reply(embed=embed, mention_author=False,
                        allowed_mentions=discord.AllowedMentions.none())

    @commands.Cog.listener()
    async def on_command_error(self, ctx: commands.Context, error):
        """?명령어 오류를 **무엇이 틀렸는지** 알려 줍니다.

        이 리스너가 있으면 라이브러리 기본 처리(콘솔에 트레이스백)가
        꺼집니다. 그래서 여기서 빠뜨린 오류는 반드시 로그로 남깁니다.
        """
        if isinstance(error, commands.CommandNotFound):
            return          # ?로 시작하는 평범한 채팅 — 조용히 넘어갑니다
        name = ctx.command.name if ctx.command else ""
        usage = PREFIX_USAGE.get(name, "")
        if isinstance(error, commands.MissingPermissions):
            msg = "⛔ 이 명령어를 쓸 권한이 없습니다."
        elif isinstance(error, commands.NoPrivateMessage):
            msg = "⛔ 서버 안에서만 쓸 수 있습니다."
        elif isinstance(error, commands.MissingRequiredArgument):
            msg = f"⚠️ `{error.param.name}` 을(를) 빠뜨렸습니다.\n사용법: {usage}"
        elif isinstance(error, (commands.MemberNotFound, commands.UserNotFound)):
            msg = ("⚠️ 유저를 찾지 못했습니다. **유저 ID** 나 멘션으로 적어 주세요.\n"
                   f"사용법: {usage}"
                   + ("\n-# `?warn` 은 서버에 있는 사람만 됩니다. 나간 사람은 `?ban` 으로 막으세요."
                      if name == "warn" else ""))
        elif isinstance(error, commands.BadArgument):
            msg = f"⚠️ 입력을 이해하지 못했습니다.\n사용법: {usage}"
        else:
            # CommandInvokeError 는 겉껍데기입니다. 원인을 남겨야 고칠 수 있습니다.
            cause = getattr(error, "original", error)
            print(f"[Moderation] ?{name} 실패: {type(cause).__name__}: {cause}", flush=True)
            msg = "⚠️ 처리 중 오류가 났습니다. 잠시 후 다시 시도해 주세요."
        try:
            await ctx.reply(msg, mention_author=False,
                            allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as e:
            print(f"[Moderation] 오류 안내 전송 실패: {type(e).__name__}: {e}", flush=True)


async def setup(bot):
    await bot.add_cog(Moderation(bot))
    bot.add_view(ModPanelView())
