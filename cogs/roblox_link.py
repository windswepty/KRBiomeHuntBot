"""로블록스 계정 연동 (독립 모듈).

개인서버 링크를 받아간 사람의 **로블록스 아이디**를 기록해 두면, 게임 안
플레이어 목록과 대조해 '명단에 없는데 들어와 있는 사람'을 찾아낼 수 있습니다.
서버 밖으로 유출된 링크를 탐지하는 현실적인 경로가 이것뿐입니다.

닉네임을 그냥 입력받으면 남의 이름을 적어 사칭할 수 있으므로,
로블록스 공식 OAuth 로 신원을 보증받습니다.

포인트/상점 시스템과는 연동되지 않습니다.
"""

import asyncio
import io
import os
import re
from datetime import datetime, timedelta

import discord
from discord.ext import commands
from discord import app_commands

from utils import roblox_oauth
from utils.verify_db import (
    get_roblox_link, find_roblox_owner, save_roblox_link,
    remove_roblox_link, list_roblox_links, search_roblox_links,
    get_verify_settings, get_linkguard_settings, get_usage_summary,
    get_link_usage, get_protected_link, get_log_reference,
    KST,
)


def _login_embed_and_view(guild_id: int, user_id: int,
                          title: str, description: str,
                          color: discord.Color) -> tuple[discord.Embed, discord.ui.View]:
    """로블록스 로그인 안내 임베드와 링크 버튼을 만듭니다.

    최초 연동과 계정 교체가 같은 화면을 쓰도록 한곳에 모았습니다.
    """
    embed = discord.Embed(title=title, description=description, color=color)
    embed.add_field(
        name="🔒 요청하는 권한",
        value="로블록스 **아이디와 유저명만** 확인합니다.\n"
              "비밀번호 · 로벅스 · 인벤토리에는 접근하지 않습니다.",
        inline=False)
    # 이 화면에서 막히는 사람이 실제로 나옵니다. 여기에 적어 두지
    # 않으면 운영진에게 문의가 오고, 운영진은 앱 설정을 건드립니다.
    embed.add_field(
        name="⚠️ 로그인 화면에서 오류가 뜨면",
        value=("① 이 메시지 말고 **`/로블록스연동` 을 새로 실행**해 주세요.\n"
               "　 예전에 받은 버튼에는 그때의 주소가 박혀 있습니다.\n"
               "② 그래도 뜨면 버튼을 **꾹 눌러 주소를 복사**해 크롬·사파리에서  열어 보세요."),
        inline=False)
    embed.set_footer(text="이 링크는 10분 뒤 만료됩니다")

    view = discord.ui.View(timeout=None)
    url, _ = roblox_oauth.start(guild_id, user_id)
    view.add_item(discord.ui.Button(
        label="로블록스로 로그인", url=url,
        style=discord.ButtonStyle.link, emoji="🎮"))
    return embed, view



class RosterToolsView(discord.ui.View):
    """`/연동명단` 결과에 붙는 도구.

    ephemeral 메시지에 붙는 일회성 View 라 timeout 을 둡니다.
    영구 View 가 아니므로 custom_id 를 고정할 필요가 없습니다.
    """

    def __init__(self, guild_id: int):
        super().__init__(timeout=180)
        self.guild_id = guild_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # 명단은 관리자 전용 정보입니다. ephemeral 이라 남이 볼 일은 없지만,
        # 버튼 상호작용은 별개의 요청이므로 여기서도 다시 확인합니다.
        if interaction.user.guild_permissions.administrator:
            return True
        await interaction.response.send_message(
            "⚠️ 관리자만 사용할 수 있습니다.", ephemeral=True)
        return False

    @discord.ui.button(label="미연동자 보기", style=discord.ButtonStyle.primary,
                       emoji="🚫")
    async def unlinked(self, interaction: discord.Interaction,
                       button: discord.ui.Button):
        """인증 자격은 있는데 아직 연동하지 않은 사람.

        공지를 누구에게 해야 하는지가 이 목록입니다. 자격이 없는 멤버는
        애초에 보호된 링크를 만날 일이 없으므로 대상이 아닙니다.
        """
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = interaction.guild

        settings = await get_verify_settings(guild.id)
        required = {int(r) for r in (settings.get("required_role_ids") or [])}
        linked = {str(r.get("user_id"))
                  for r in await list_roblox_links(guild.id)}

        # guild.members 는 게이트웨이 캐시라 API 를 쓰지 않습니다.
        # (여기서 REST 로 6천 명을 받아오면 이벤트 루프가 밀려 3초 제한에 걸립니다)
        targets = [
            m for m in guild.members
            if not m.bot
            and (not required or any(r.id in required for r in m.roles))
            and str(m.id) not in linked
        ]
        targets.sort(key=lambda m: m.display_name.lower())

        embed = discord.Embed(
            title="🚫 아직 연동하지 않은 멤버",
            color=discord.Color.orange() if targets else discord.Color.green())

        if not required:
            embed.description = (
                f"인증 자격 역할이 지정되지 않아 **서버 전원**이 대상이 됩니다.\n"
                f"현재 미연동 **{len(targets):,}명** / 전체 {len(guild.members):,}명\n"
                "-# `/인증설정` → 인증 필요 역할 을 지정하면 실제 대상만 추려집니다.")
        elif not targets:
            embed.description = "자격 있는 멤버가 **전원 연동을 마쳤습니다.**"
        else:
            names = ", ".join(f"`{r.name}`" for r in
                              [guild.get_role(i) for i in required] if r)
            embed.description = (
                f"인증 자격이 있는데 아직 연동하지 않은 사람 **{len(targets):,}명** 입니다.\n"
                f"-# 자격 역할: {names}")

        if targets:
            shown, used = [], 0
            for m in targets:
                piece = m.mention
                if used + len(piece) + 1 > 950:
                    break
                shown.append(piece)
                used += len(piece) + 1
            embed.add_field(
                name=f"📢 공지 대상 ({len(shown)}명 표시)",
                value=" ".join(shown), inline=False)
            if len(targets) > len(shown):
                embed.set_footer(
                    text=f"…외 {len(targets) - len(shown):,}명. "
                         "전체 목록은 아래 파일에 있습니다.")

            # 임베드에는 다 담기지 않으므로 파일로도 줍니다.
            body = "\n".join(
                f"{m.id}\t{m.display_name}\t{m.name}" for m in targets)
            head = ("# 인증 자격은 있으나 로블록스 연동을 하지 않은 멤버\n"
                    f"# {len(targets)}명 · 디스코드ID / 서버표시이름 / 사용자명\n")
            data = io.BytesIO((head + body).encode("utf-8"))
            await interaction.followup.send(
                embed=embed,
                file=discord.File(data, filename="미연동자.txt"),
                ephemeral=True)
            return

        await interaction.followup.send(embed=embed, ephemeral=True)

    @discord.ui.button(label="파일로 받기", style=discord.ButtonStyle.secondary,
                       emoji="📄")
    async def download(self, interaction: discord.Interaction,
                       button: discord.ui.Button):
        """연동 명단 전체를 파일로. 임베드는 1,000자 제한이라 다 못 담습니다."""
        await interaction.response.defer(ephemeral=True, thinking=True)
        rows = await list_roblox_links(interaction.guild.id)
        if not rows:
            await interaction.followup.send(
                "아직 연동한 멤버가 없습니다.", ephemeral=True)
            return

        # 엑셀에서 바로 열리도록 CSV. 쉼표가 든 이름이 열을 밀지 않게 감쌉니다.
        def cell(v):
            v = str(v if v is not None else "")
            return '"' + v.replace('"', '""') + '"'

        lines = ["discord_id,roblox_id,roblox_name,linked_at"]
        for r in rows:
            lines.append(",".join(cell(r.get(k)) for k in
                                  ("user_id", "roblox_id", "roblox_name", "linked_at")))
        # 엑셀이 UTF-8 을 알아보도록 BOM 을 붙입니다 (없으면 한글이 깨집니다)
        data = io.BytesIO(("﻿" + "\n".join(lines)).encode("utf-8"))
        await interaction.followup.send(
            f"📄 연동 명단 **{len(rows):,}명** 입니다.",
            file=discord.File(data, filename="연동명단.csv"),
            ephemeral=True)

class RelinkConfirmView(discord.ui.View):
    """이미 연동된 상태에서 다시 연동할 때 한 번 더 확인받습니다.

    로블록스 동의 화면은 로블록스가 만든 페이지라 우리가 손댈 수 없습니다.
    (거기서는 이미 연동돼 있다는 사실조차 알려줄 수 없습니다)
    그래서 **그 앞 단계인 디스코드에서** 확인을 받습니다.

    임시 View 입니다 — 재시작 후에는 동작하지 않지만, 명령어를 다시 부르면
    되므로 영구 View 로 만들 필요가 없습니다. 그래서 custom_id 도 없습니다.
    """

    def __init__(self, guild_id: int, user_id: int, existing: dict):
        super().__init__(timeout=180)
        self.guild_id = guild_id
        self.user_id = user_id
        self.existing = existing

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # ephemeral 이라 본인만 보이지만, 소유자 검사를 한 번 더 둡니다.
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "⚠️ 본인만 사용할 수 있습니다.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="다른 계정으로 연동하기",
                       style=discord.ButtonStyle.danger, emoji="🔄")
    async def relink(self, interaction: discord.Interaction,
                     button: discord.ui.Button):
        if not roblox_oauth.enabled():
            await interaction.response.send_message(
                "⚠️ 로블록스 연동이 아직 준비되지 않았습니다.", ephemeral=True)
            return
        try:
            embed, view = _login_embed_and_view(
                self.guild_id, self.user_id,
                title="🔄 다른 계정으로 연동",
                description=f"현재 **{self.existing.get('roblox_name', '?')}** 계정이 "
                            f"연동돼 있습니다.\n"
                            "아래 버튼을 눌러 **바꿀 계정으로** 로그인해 주세요.",
                color=discord.Color.orange())
        except Exception as e:
            print(f"[RobloxLink] 로그인 링크 생성 실패: {type(e).__name__}: {e}",
                  flush=True)
            await interaction.response.send_message(
                "⚠️ 로그인 링크를 만들지 못했습니다. 잠시 후 다시 시도해 주세요.",
                ephemeral=True)
            return

        embed.add_field(
            name="⚠️ 로블록스 화면에서 주의하세요",
            value="로블록스 동의 화면에는 **지금 브라우저에 로그인된 계정**이 뜹니다.\n"
                  "바꾸려는 계정이 맞는지 이름을 꼭 확인하고 눌러 주세요.\n"
                  "-# 다른 계정이면 로블록스에서 먼저 로그아웃한 뒤 진행하세요.",
            inline=False)
        embed.add_field(
            name="📝 참고",
            value="계정을 바꾸면 **관리자 로그에 기록**됩니다.\n"
                  "이전 계정으로 남아 있던 기록은 그대로 보존됩니다.",
            inline=False)
        await interaction.response.send_message(
            embed=embed, view=view, ephemeral=True)
        self.stop()

    @discord.ui.button(label="취소", style=discord.ButtonStyle.secondary, emoji="✖️")
    async def cancel(self, interaction: discord.Interaction,
                     button: discord.ui.Button):
        embed = discord.Embed(
            title="✖️ 취소되었습니다",
            description=f"연동은 **{self.existing.get('roblox_name', '?')}** 계정 "
                        "그대로 유지됩니다.",
            color=discord.Color.greyple())
        await interaction.response.send_message(embed=embed, ephemeral=True)
        self.stop()


def normalize_name(raw: str) -> str:
    """대조용으로 이름을 다듬습니다.

    붙여넣은 목록에는 `@이름`, 앞뒤 공백, 대소문자 차이가 섞입니다.
    로블록스 사용자명은 대소문자를 구분하지 않으므로 접어서 맞춥니다.
    """
    return (raw or "").strip().strip("@").strip().casefold()


def split_names(text: str) -> list[str]:
    """붙여넣은 덩어리를 이름 목록으로 자릅니다.

    **공백으로는 자르지 않습니다** — 표시이름에는 공백이 들어갈 수 있어서,
    공백까지 자르면 한 사람이 두 명으로 쪼개집니다.
    """
    out: list[str] = []
    seen: set[str] = set()
    for piece in re.split(r"[\n,;]+", text or ""):
        name = piece.strip().strip("@").strip()
        if not name:
            continue
        key = name.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(name)
    return out


def _join_capped(items: list[str], limit: int = 900) -> str:
    """임베드 필드(1024자) 안에 들어가도록 잘라 붙입니다."""
    shown: list[str] = []
    used = 0
    for piece in items:
        if used + len(piece) + 1 > limit:
            break
        shown.append(piece)
        used += len(piece) + 1
    text = " ".join(shown) if shown else "—"
    if len(shown) < len(items):
        text += f" …외 {len(items) - len(shown):,}명"
    return text


async def received_map(guild, message_id) -> dict:
    """'링크를 받아 간 사람' 집합.

    `message_id` 를 주면 **그 링크 하나**만, 없으면 최근 30일 전체를 봅니다.
    범위가 다르면 판정의 의미가 통째로 달라집니다 — 전체로 보면 *다른*
    개인서버 링크를 받았던 사람도 '받아 간 사람'이 되어, 정작 이 서버에
    무단으로 들어온 사람이 정상으로 보입니다.
    """
    if message_id is None:
        return await get_usage_summary(guild.id)
    rows = await get_link_usage(guild.id, int(message_id))
    return {str(r["user_id"]): {"name": r.get("user_name", ""),
                                "count": r.get("count", 1),
                                "links": 1,
                                "last": r.get("last", "")}
            for r in rows}


async def compare_players(guild, raw_text: str,
                          message_id=None,
                          scope_note: str = "") -> tuple:
    """게임 안 플레이어 목록을 연동 명단·수령 명단과 대조합니다.

    유출된 개인서버 주소에는 워터마크가 남지 않습니다(로블록스가 공유링크를
    펼치면서 프래그먼트를 버립니다). 그래서 **링크 쪽이 아니라 게임 쪽에서**
    확인합니다 — 받아 간 적이 없는데 들어와 있으면 누군가 넘긴 것입니다.

    `message_id` 로 **링크 하나에 범위를 좁힐 수 있습니다.** 좁히지 않으면
    "최근 30일에 아무 링크나 받은 적 있음"이 되어 판정이 무뎌집니다.
    """
    names = split_names(raw_text)
    if not names:
        # `required=True` 만으로는 공백을 막지 못합니다 — 디스코드는
        # 공백 문자를 '입력됨'으로 봅니다. 여기서 걸러 내지 않으면
        # **아무것도 대조하지 않고 초록색 '문제 없음'** 이 뜹니다.
        return discord.Embed(
            title="🎮 개인서버 플레이어 대조",
            description=("이름을 하나도 읽지 못했습니다.\n"
                         "게임 안 플레이어 목록을 **줄바꿈이나 쉼표로 구분해** "
                         "붙여넣어 주세요."),
            color=discord.Color.red()), None

    scoped = message_id is not None
    links = await list_roblox_links(guild.id)
    usage = await received_map(guild, message_id)

    # 저장된 이름은 낡습니다(개명). 게임 목록은 표시이름을 보여주므로
    # 현재 이름을 물어봐야 대조가 맞습니다. 실패해도 계속 진행합니다.
    fresh = await asyncio.to_thread(
        roblox_oauth.fetch_names, [r.get("roblox_id") for r in links])

    index: dict[str, list[dict]] = {}
    paired: set[tuple[str, str]] = set()
    for row in links:
        try:
            rid = int(row.get("roblox_id") or 0)
        except (TypeError, ValueError):
            rid = 0
        current = fresh.get(rid) or {}
        uid = str(row.get("user_id"))
        for candidate in (row.get("roblox_name"),
                          current.get("name"), current.get("display")):
            key = normalize_name(candidate or "")
            if not key or (key, uid) in paired:
                continue
            paired.add((key, uid))
            index.setdefault(key, []).append(row)

    received: list[str] = []
    not_received: list[str] = []
    unknown: list[str] = []
    report: list[str] = []
    seen_received_uids: set[str] = set()
    seen_not_received_uids: set[str] = set()

    for name in names:
        rows = index.get(normalize_name(name)) or []
        if not rows:
            unknown.append(name)
            report.append(f"[연동 명단에 없음]\t{name}")
            continue
        for row in rows:
            uid = str(row.get("user_id"))
            got = usage.get(uid)
            if got:
                if uid not in seen_received_uids:
                    seen_received_uids.add(uid)
                    received.append(f"<@{uid}>")
                report.append(f"[받아 감]\t{name}\t{uid}\t"
                              f"링크 {got['links']}개\t{got.get('last', '')}")
            else:
                if uid not in seen_not_received_uids:
                    seen_not_received_uids.add(uid)
                    not_received.append(f"<@{uid}>")
                report.append(f"[받은 기록 없음]\t{name}\t{uid}")

    suspicious = len(not_received) + len(unknown)
    scope_line = (scope_note or
                  (f"대상: 링크 하나 (메시지 `{message_id}`)" if scoped
                   else "대상: **최근 30일 안의 모든 링크**"))
    embed = discord.Embed(
        title="🎮 개인서버 플레이어 대조",
        description=(f"{scope_line}\n"
                     f"입력한 플레이어 **{len(names):,}명** · "
                     f"연동 명단 **{len(links):,}명** · "
                     f"이 범위의 수령자 **{len(usage):,}명**\n"
                     + ("확인이 필요한 사람이 **없습니다.**" if not suspicious
                        else f"확인이 필요한 사람 **{suspicious:,}명** 입니다.")),
        color=discord.Color.green() if not suspicious else discord.Color.orange())

    if received:
        embed.add_field(
            name=(f"✅ 이 링크를 받아 간 사람 ({len(received)}명)" if scoped
                  else f"✅ 보호된 링크를 받아 간 사람 ({len(received)}명)"),
            value=_join_capped(received), inline=False)

    if not_received:
        embed.add_field(
            name=(f"⚠️ 연동했는데 이 링크를 받은 적 없음 ({len(not_received)}명)"
                  if scoped else
                  f"⚠️ 연동했는데 받은 기록이 없음 ({len(not_received)}명)"),
            value=(("**이 링크를 받아 간 적이 없는데 서버 안에 있습니다.**\n"
                    "남에게 받았거나 다른 경로로 들어왔습니다.\n" if scoped else
                    "최근 30일 안에 **보호된 링크를 받은 적이 없습니다.**\n"
                    "남에게 받았거나, 사람이 직접 공유하는 채널에서 받았을 수 "
                    "있습니다.\n")
                   + _join_capped(not_received, 700)),
            inline=False)

    if unknown:
        embed.add_field(
            name=f"🚨 연동 명단에 없음 ({len(unknown)}명)",
            value=("이름만으로는 누구인지 알 수 없습니다. **유출로 들어온 "
                   "사람일 수도, 그냥 연동을 안 한 사람일 수도** 있습니다.\n"
                   + _join_capped([f"`{n}`" for n in unknown], 700)),
            inline=False)

    # ── 부계정(알트) 교차대조 ──────────────────────────────────────────
    # 미확인 플레이어가 있으면, "링크를 받았는데 게임 안에 연동 닉이 없는
    # 사람"을 추려 용의자로 보여줍니다. 그 사람이 부계정으로 접속한 것일
    # 수 있기 때문입니다. 미확인 인원이 없으면 보여줄 이유가 없습니다.
    if unknown and usage:
        name_pool = {normalize_name(n) for n in names}
        absent: list[str] = []      # 받았지만 게임 안에 이름이 없는 사람
        for uid, info in usage.items():
            # 이미 게임 안에서 확인된(received) 유저는 제외
            if uid in seen_received_uids:
                continue
            # 이 유저의 연동 이름이 게임 안에 있는지 확인
            found_in_game = False
            for row in links:
                if str(row.get("user_id")) != uid:
                    continue
                rid = 0
                try:
                    rid = int(row.get("roblox_id") or 0)
                except (TypeError, ValueError):
                    pass
                cur = fresh.get(rid) or {}
                for candidate in (row.get("roblox_name"),
                                  cur.get("name"), cur.get("display")):
                    if normalize_name(candidate or "") in name_pool:
                        found_in_game = True
                        break
                if found_in_game:
                    break
            if not found_in_game:
                roblox_name = ""
                for row in links:
                    if str(row.get("user_id")) == uid:
                        rid = 0
                        try:
                            rid = int(row.get("roblox_id") or 0)
                        except (TypeError, ValueError):
                            pass
                        cur = fresh.get(rid) or {}
                        roblox_name = (cur.get("name")
                                       or row.get("roblox_name") or "?")
                        break
                if roblox_name:
                    absent.append(f"<@{uid}> (연동: `{roblox_name}`)")
                else:
                    absent.append(f"<@{uid}>")
        if absent:
            embed.add_field(
                name=f"🔍 부계정 의심 — 교차대조 ({len(absent)}명)",
                value=("링크를 받았지만 **연동 닉네임이 게임 안에 없는** 사람입니다.\n"
                       "부계정(알트)으로 접속했을 가능성이 있습니다.\n"
                       + _join_capped(absent, 700)),
                inline=False)

    # 일부만 실패해도 알려야 합니다. 전부 실패했을 때만 알리면,
    # 절반이 낡은 이름으로 대조된 결과를 관리자가 그대로 믿습니다.
    unresolved = len(links) - len(fresh)
    if links and unresolved > 0:
        embed.add_field(
            name=("⚠️ 로블록스 이름 조회 실패" if not fresh
                  else f"⚠️ 이름을 못 받은 사람 {unresolved}명"),
            value=("그 사람들은 **저장된 이름으로만** 대조했습니다. "
                   "개명했거나 표시이름이 다르면 '명단에 없음'으로 잘못 "
                   "나올 수 있습니다.\n"
                   "-# 잠시 뒤 다시 실행하면 조회될 수 있습니다."),
            inline=False)

    embed.set_footer(
        text=f"연동 완료 {len(links):,}명 — 연동자가 적으면 '명단에 없음'이 "
             "많이 나옵니다. 그 자체는 유출의 증거가 아닙니다.")

    attachment = None
    if len(report) > 12:
        head = ("# 개인서버 플레이어 대조 결과\n"
                f"# {scope_line.replace('**', '')}\n"
                f"# 입력 {len(names)}명 · 연동 명단 {len(links)}명\n"
                "# 구분 / 게임 내 이름 / 디스코드ID / 비고\n")
        data = io.BytesIO((head + "\n".join(report)).encode("utf-8"))
        attachment = discord.File(data, filename="개인서버대조.txt")

    return embed, attachment


class ServerCompareModal(discord.ui.Modal, title="🎮 개인서버 플레이어 대조"):
    """게임 안 플레이어 목록을 붙여넣는 창.

    슬래시 명령어 옵션으로는 여러 줄을 붙여넣기가 어려워 모달을 씁니다.
    `message_id` 를 주면 **그 링크 하나**에만 범위를 좁혀 대조합니다.
    """

    players = discord.ui.TextInput(
        label="게임 안 플레이어 이름",
        style=discord.TextStyle.paragraph,
        placeholder="줄바꿈이나 쉼표로 구분해 붙여넣으세요",
        max_length=4000,
        required=True)

    def __init__(self, message_id=None, scope_note: str = ""):
        super().__init__()
        self.message_id = message_id
        self.scope_note = scope_note

    async def on_submit(self, interaction: discord.Interaction):
        # 로블록스 이름 조회가 끼어 있어 3초를 넘길 수 있습니다.
        await interaction.response.defer(ephemeral=True, thinking=True)
        embed, attachment = await compare_players(
            interaction.guild, str(self.players),
            message_id=self.message_id, scope_note=self.scope_note)
        if attachment is not None:
            await interaction.followup.send(embed=embed, file=attachment,
                                            ephemeral=True)
            return
        await interaction.followup.send(embed=embed, ephemeral=True)

_MSG_ID_RE = re.compile(r"(\d{15,25})")


async def resolve_link_target(guild, raw: str):
    """붙여넣은 값에서 **대조할 링크 하나**를 찾아냅니다.

    관리자가 손에 쥐고 있는 것은 보통 **로그 임베드**입니다. 그래서
    로그 메시지 ID · 보호 메시지 ID · 메시지 링크를 **전부 받아들입니다.**
    무엇을 복사해야 하는지 외우게 만들면 아무도 안 씁니다.

    반환: `(message_id, 안내문)` / 못 찾으면 `(None, 사유)`
    """
    matches = _MSG_ID_RE.findall(raw or "")
    if not matches:
        return None, ("메시지 ID 나 메시지 링크를 넣어 주세요.\n"
                      "-# 메시지 우클릭 → **메시지 ID 복사** "
                      "(개발자 모드가 켜져 있어야 보입니다)")
    mid = int(matches[-1])

    # ① 보호된 임베드 자체
    if await get_protected_link(guild.id, mid):
        return mid, f"대상: **링크 하나** (보호 메시지 `{mid}`)"

    # ② 로그 임베드 — 관리자가 제일 자주 복사하는 것
    ref = await get_log_reference(guild.id, mid)
    if ref:
        return int(ref["protected_message_id"]), "대상: **이 로그가 가리키는 링크 하나**"

    # ③ 보호 기록(7일)은 지워졌지만 사용 기록(30일)은 남아 있는 경우
    if await get_link_usage(guild.id, mid):
        return mid, f"대상: **링크 하나** (메시지 `{mid}`)"

    return None, ("그 메시지에 연결된 링크를 찾지 못했습니다.\n"
                  "-# 보호된 임베드 또는 **로그 임베드**의 메시지 ID 를 "
                  "넣어 주세요. 사용 기록은 30일간 보관됩니다.")


class RobloxLink(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    # ── 멤버용 ────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_roblox_linked(self, guild_id: int, user_id: int):
        """로블록스 연동이 성공하면 발생하는 이벤트를 처리합니다."""
        guild = self.bot.get_guild(guild_id)
        if not guild:
            return
        
        member = guild.get_member(user_id)
        if not member:
            return
            
        settings = await get_verify_settings(guild_id)
        linked_role_id = settings.get("linked_role_id")
        
        if linked_role_id:
            role = guild.get_role(int(linked_role_id))
            if role and role not in member.roles:
                try:
                    await member.add_roles(role, reason="로블록스 계정 자동 연동 완료")
                    print(f"[RobloxLink] {member} 에게 연동 완료 역할({role.name})을 부여했습니다.", flush=True)
                except Exception as e:
                    print(f"[RobloxLink] 역할 부여 실패: {e}", flush=True)

    @app_commands.command(
        name="로블록스연동",
        description="내 로블록스 계정을 연동합니다 (개인서버 이용 기록에 함께 남습니다)."
    )
    # DM 에서는 interaction.guild 가 None 이라 AttributeError 로 죽습니다.
    # (실제로 /출석 이 그렇게 터졌습니다). guild_only 를 걸면 디스코드가
    # **DM 목록에 아예 띄우지 않으므로** 방어 코드를 흩뿌릴 필요가 없습니다.
    @app_commands.guild_only()
    async def roblox_link(self, interaction: discord.Interaction):
        guild = interaction.guild
        if guild is None:
            return

        if not roblox_oauth.enabled():
            embed = discord.Embed(
                title="⚠️ 로블록스 연동이 아직 준비되지 않았습니다",
                description="관리자가 설정을 마치면 이용하실 수 있습니다.",
                color=discord.Color.orange()
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        existing = await get_roblox_link(guild.id, interaction.user.id)

        # 이미 연동돼 있으면 곧바로 로블록스로 보내지 않고 한 번 확인합니다.
        # 로블록스 동의 화면은 우리가 손댈 수 없어서, 거기서는 "이미 연동된
        # 계정입니다" 같은 안내를 띄울 방법이 없습니다.
        if existing:
            embed = discord.Embed(
                title="🔗 이미 연동된 계정이 있습니다",
                description="다른 계정으로 바꾸시겠습니까?",
                color=discord.Color.orange()
            )
            embed.add_field(
                name="🎮 현재 연동된 계정",
                value=f"**{existing.get('roblox_name', '?')}**\n"
                      f"`{existing.get('roblox_id', '?')}`",
                inline=True)
            embed.add_field(
                name="🕒 연동 시각",
                value=f"`{existing.get('linked_at', '?')}`",
                inline=True)
            embed.add_field(
                name="ℹ️ 그대로 두셔도 됩니다",
                value="지금 계정이 맞다면 **[취소]** 를 누르시면 됩니다.\n"
                      "다시 연동해도 이 계정이면 달라지는 것은 없습니다.",
                inline=False)
            await interaction.response.send_message(
                embed=embed,
                view=RelinkConfirmView(guild.id, interaction.user.id, existing),
                ephemeral=True)
            return

        try:
            embed, view = _login_embed_and_view(
                guild.id, interaction.user.id,
                title="🎮 로블록스 계정 연동",
                description="아래 버튼을 눌러 로블록스에 로그인하면 연동이 완료됩니다.",
                color=discord.Color.blurple())
        except Exception as e:
            print(f"[RobloxLink] 로그인 링크 생성 실패: {type(e).__name__}: {e}",
                  flush=True)
            await interaction.response.send_message(
                "⚠️ 로그인 링크를 만들지 못했습니다. 잠시 후 다시 시도해 주세요.",
                ephemeral=True)
            return

        embed.insert_field_at(
            0,
            name="ℹ️ 왜 필요한가요",
            value="개인서버 링크를 누가 받아갔는지 **로블록스 아이디로도** 남기기 "
                  "위해서입니다. 링크가 외부로 새어나갔을 때 확인하는 데 쓰입니다.",
            inline=False)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @app_commands.command(
        name="내연동정보",
        description="내 로블록스 연동 상태를 확인합니다."
    )
    # DM 에서는 interaction.guild 가 None 이라 AttributeError 로 죽습니다.
    # (실제로 /출석 이 그렇게 터졌습니다). guild_only 를 걸면 디스코드가
    # **DM 목록에 아예 띄우지 않으므로** 방어 코드를 흩뿌릴 필요가 없습니다.
    @app_commands.guild_only()
    async def my_link(self, interaction: discord.Interaction):
        link = await get_roblox_link(interaction.guild.id, interaction.user.id)
        if not link:
            embed = discord.Embed(
                title="🔗 연동 정보 없음",
                description="아직 로블록스 계정을 연동하지 않으셨습니다.\n"
                            "`/로블록스연동` 으로 등록할 수 있습니다.",
                color=discord.Color.orange())
        else:
            embed = discord.Embed(
                title="🔗 내 로블록스 연동 정보",
                color=discord.Color.green())
            embed.add_field(
                name="🎮 로블록스",
                value=f"**{link.get('roblox_name', '?')}**\n"
                      f"`{link.get('roblox_id', '?')}`",
                inline=True)
            embed.add_field(
                name="🕒 연동 시각",
                value=f"`{link.get('linked_at', '?')}`", inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── 관리자용 ──────────────────────────────────────────────────────

    @app_commands.command(
        name="연동명단",
        description="[관리자 전용] 로블록스 연동 명단을 봅니다 (게임 내 대조용)."
    )
    # DM 에서는 interaction.guild 가 None 이라 AttributeError 로 죽습니다.
    # (실제로 /출석 이 그렇게 터졌습니다). guild_only 를 걸면 디스코드가
    # **DM 목록에 아예 띄우지 않으므로** 방어 코드를 흩뿌릴 필요가 없습니다.
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def roster(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        rows = await list_roblox_links(interaction.guild.id)

        embed = discord.Embed(
            title="🎮 로블록스 연동 명단",
            description=(f"총 **{len(rows)}명**이 연동했습니다."
                         if rows else "아직 연동한 멤버가 없습니다."),
            color=discord.Color.blurple())

        if rows:
            # 게임 내 플레이어 목록과 눈으로 대조하기 좋게 이름만 한 줄로 모읍니다.
            names = ", ".join(r.get("roblox_name", "?") for r in rows[:100])
            embed.add_field(
                name="📋 로블록스 아이디 (게임 내 목록과 대조용)",
                value="```" + names[:990] + "```", inline=False)

            lines = []
            used = 0
            for r in rows:
                line = (f"<@{r.get('user_id')}> → **{r.get('roblox_name', '?')}** "
                        f"(`{r.get('roblox_id', '?')}`)")
                if used + len(line) + 1 > 1000:
                    break
                lines.append(line)
                used += len(line) + 1
            embed.add_field(name=f"🔗 매핑 ({len(lines)}명)",
                            value="\n".join(lines), inline=False)
            if len(rows) > len(lines):
                embed.set_footer(text=f"…외 {len(rows) - len(lines)}명 더 있습니다")

        # 임베드는 1,000자 제한이라 전원을 담을 수 없고, "누구에게 공지할지"는
        # 여기 없는 정보입니다. 두 가지를 버튼으로 붙여 둡니다.
        await interaction.followup.send(
            embed=embed, view=RosterToolsView(interaction.guild.id),
            ephemeral=True)

    @app_commands.command(
        name="연동조회",
        description="[관리자 전용] 연동 명단을 검색합니다 (디스코드 / 로블록스 양방향)."
    )
    # DM 에서는 interaction.guild 가 None 이라 AttributeError 로 죽습니다.
    # (실제로 /출석 이 그렇게 터졌습니다). guild_only 를 걸면 디스코드가
    # **DM 목록에 아예 띄우지 않으므로** 방어 코드를 흩뿌릴 필요가 없습니다.
    @app_commands.guild_only()
    @app_commands.describe(
        검색어="디스코드 ID·멘션, 로블록스 유저명, 또는 로블록스 숫자 ID"
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def search(self, interaction: discord.Interaction, 검색어: str):
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = interaction.guild
        query = (검색어 or "").strip()

        rows = await search_roblox_links(guild.id, query, limit=10)

        if not rows:
            embed = discord.Embed(
                title="🔍 검색 결과 없음",
                description=f"`{query[:80]}` 로 연동된 기록을 찾지 못했습니다.",
                color=discord.Color.orange()
            )
            
            # 연동 기록은 없지만, 디스코드 이름(별명, 유저명)이 비슷한 사람이 서버에 있는지 확인합니다.
            query_lower = query.lower()
            similar_members = []
            for m in guild.members:
                if query_lower in m.display_name.lower() or query_lower in m.name.lower():
                    similar_members.append(m)
                    if len(similar_members) >= 5:  # 너무 많이 나오면 자름
                        break
                        
            if similar_members:
                mentions = []
                for m in similar_members:
                    mentions.append(f"• {m.mention} (`{m.display_name}` / `{m.name}`)")
                embed.add_field(
                    name="💡 혹시 이 분인가요? (미연동 상태)",
                    value="현재 서버에 검색어와 이름이 일치하는 멤버가 있습니다.\n" + "\n".join(mentions) + "\n-# 위 사람이라면 아직 로블록스 연동을 하지 않은 것입니다.",
                    inline=False
                )

            # 게임에서 본 이름이 안 나온다는 것 자체가 중요한 신호입니다.
            embed.add_field(
                name="⚠️ 게임 안에서 본 이름인가요?",
                value="연동 명단에 없는 사람이 개인서버에 들어와 있다면,\n"
                      "**링크가 외부로 새어나갔다는 뜻**일 수 있습니다.\n"
                      "-# 다만 연동을 아직 안 한 멤버일 수도 있으니 함께 확인하세요.",
                inline=False
            )
            embed.add_field(
                name="💡 검색 가능한 값",
                value="• 디스코드 ID 또는 멘션\n"
                      "• 로블록스 유저명 (일부만 입력해도 됩니다)\n"
                      "• 로블록스 숫자 ID",
                inline=False
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        badge = {
            "discord_id": "🙋 디스코드 ID 일치",
            "roblox_id": "🔢 로블록스 ID 일치",
            "roblox_name": "🎮 로블록스 유저명 일치",
        }

        embed = discord.Embed(
            title="🔍 연동 검색 결과",
            description=f"`{query[:80]}` → **{len(rows)}건**",
            color=discord.Color.green()
        )

        for idx, r in enumerate(rows, 1):
            uid = r.get("user_id", "?")
            member = guild.get_member(int(uid)) if str(uid).isdigit() else None
            # 서버를 이미 나간 사람일 수 있습니다. 그것 자체가 확인할 신호입니다.
            who = member.mention if member else f"`{uid}` (⚠️ 서버에 없음)"
            embed.add_field(
                name=f"{idx}. {r.get('roblox_name', '?')}",
                value=f"{who}\n"
                      f"🎮 `{r.get('roblox_id', '?')}`\n"
                      f"🕒 `{r.get('linked_at', '?')}`\n"
                      f"-# {badge.get(r.get('matched_by'), '일치')}",
                inline=False
            )

        if len(rows) >= 10:
            embed.set_footer(text="결과가 10건을 넘어 잘렸습니다 · 더 정확한 검색어를 입력해 보세요")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(
        name="개인서버대조",
        description="[관리자 전용] 게임 안 플레이어 목록을 연동·수령 명단과 대조합니다."
    )
    @app_commands.describe(
        링크="대조할 링크의 메시지 ID (로그 임베드의 ID 도 됩니다). 비우면 최근 30일 전체"
    )
    # DM 에서는 interaction.guild 가 None 이라 길드를 쓰는 순간 죽습니다.
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def compare_server(self, interaction: discord.Interaction,
                             링크: str = None):
        """유출된 링크에는 워터마크가 남지 않으므로, 게임 쪽에서 확인합니다.

        받아 간 적이 없는데 개인서버에 들어와 있으면 누군가 넘긴 것입니다.

        **범위를 좁히지 않으면 판정이 무뎌집니다.** 전체로 보면 *다른*
        개인서버 링크를 받았던 사람도 '받아 간 사람'이 되어, 정작 이 서버에
        무단으로 들어온 사람이 정상으로 보입니다.
        """
        if not 링크:
            await interaction.response.send_modal(ServerCompareModal())
            return
        target, note_text = await resolve_link_target(interaction.guild, 링크)
        if target is None:
            await interaction.response.send_message("⚠️ " + note_text,
                                                    ephemeral=True)
            return
        await interaction.response.send_modal(
            ServerCompareModal(target, note_text))

    @app_commands.command(
        name="연동점검",
        description="[관리자 전용] 로블록스 연동이 지금 되는 상태인지 확인합니다."
    )
    # DM 에서는 interaction.guild 가 None 이라 길드를 쓰는 순간 죽습니다.
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def link_check(self, interaction: discord.Interaction):
        """"Redirect URI is invalid" 같은 제보를 받았을 때 제일 먼저 볼 화면.

        **전체가 고장 난 것인지, 그 사람만 그런 것인지**를 가릅니다.
        이 구분이 없으면 운영진이 멀쩡한 앱 설정을 건드리다 진짜로 망가뜨립니다.
        SSH 로만 볼 수 있으면 관리자가 자리를 비운 동안 아무도 못 봅니다.
        """
        await interaction.response.defer(ephemeral=True, thinking=True)

        now = datetime.now(KST)
        def ago(hours=0, days=0):
            return (now - timedelta(hours=hours, days=days)).strftime(
                "%Y-%m-%d %H:%M:%S")

        rows = await list_roblox_links(interaction.guild.id)
        stamps = sorted((r.get("linked_at") or "") for r in rows)
        last = stamps[-1] if stamps else ""
        h1, d1, d7 = ago(hours=1), ago(days=1), ago(days=7)
        n_1h = sum(1 for s in stamps if s >= h1)
        n_1d = sum(1 for s in stamps if s >= d1)
        n_7d = sum(1 for s in stamps if s >= d7)

        base = (os.getenv("PUBLIC_BASE_URL") or "").strip()
        enabled = roblox_oauth.enabled()

        # 판정 — 무엇을 해야 하는지까지 말해 줍니다.
        if not enabled:
            color, verdict = discord.Color.red(), (
                "🔴 **OAuth 가 꺼져 있습니다.**\n"
                "서버 `.env` 의 `ROBLOX_CLIENT_ID` · `ROBLOX_CLIENT_SECRET` 을 "
                "확인해야 합니다. 지금은 아무도 연동할 수 없습니다.")
        elif not base:
            color, verdict = discord.Color.red(), (
                "🔴 **공개 주소(`PUBLIC_BASE_URL`)가 없습니다.**\n"
                "돌아올 주소가 만들어지지 않아 전원이 실패합니다.")
        elif last >= h1:
            color, verdict = discord.Color.green(), (
                "🟢 **지금 정상입니다** — 한 시간 안에도 성공한 사람이 있습니다.\n"
                "전체 장애가 아니라 **그 사람 쪽 문제**일 가능성이 큽니다. "
                "아래 안내를 전달해 주세요.")
        elif last >= d1:
            color, verdict = discord.Color.green(), (
                "🟢 **최근 24시간 안에 성공한 기록이 있습니다.**\n"
                "설정이 깨졌다고 보기는 어렵습니다. 아래 안내를 먼저 전달해 "
                "보시고, 여러 명이 같이 실패하면 다시 확인하세요.")
        elif stamps:
            color, verdict = discord.Color.orange(), (
                "🟠 **24시간 넘게 성공 기록이 없습니다.**\n"
                f"마지막 성공은 `{last}` 입니다. 사람이 안 해서일 수도 있지만, "
                "여러 명이 실패를 호소한다면 아래 **Redirect URL** 을 "
                "Creator Hub 와 대조해 주세요.")
        else:
            color, verdict = discord.Color.orange(), (
                "🟠 **연동 성공 기록이 아직 없습니다.**\n"
                "아래 Redirect URL 이 Creator Hub 에 등록돼 있는지 확인하세요.")

        embed = discord.Embed(title="🔎 로블록스 연동 점검",
                              description=verdict, color=color)
        embed.add_field(
            name="⚙️ 설정",
            value=(f"OAuth: {'✅ 켜짐' if enabled else '❌ 꺼짐'}\n"
                   f"Client ID: `{roblox_oauth.client_id() or '(없음)'}`"),
            inline=False)
        embed.add_field(
            name="🔗 봇이 로블록스에 보내는 주소",
            value=(f"```{roblox_oauth.redirect_uri()}```"
                   "Creator Hub → 앱 → **Redirect URLs** 에 이 값이 "
                   "**한 글자도 다르지 않게** 있어야 합니다."),
            inline=False)
        embed.add_field(
            name="📈 연동 성공",
            value=(f"최근 1시간 **{n_1h}명** · 24시간 **{n_1d}명** · "
                   f"7일 **{n_7d}명**\n"
                   f"전체 **{len(rows)}명** · 마지막 `{last or '없음'}`"),
            inline=False)
        embed.add_field(
            name="🧾 안 된다는 사람에게 전달할 안내",
            value=("① `/로블록스연동` 을 **새로 실행**해서 새 버튼을 누르세요.\n"
                   "　 (예전에 받은 버튼은 그때 주소가 박혀 있어 실패합니다)\n"
                   "② 그래도 안 되면 버튼을 **꾹 눌러 주소를 복사**해 "
                   "크롬·사파리에서 열어 보세요.\n"
                   "③ 로블록스에 **먼저 로그인**해 둔 상태에서 다시 시도하세요."),
            inline=False)
        embed.set_footer(text="이 화면은 본인에게만 보입니다")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(
        name="연동해제",
        description="[관리자 전용] 특정 유저의 로블록스 연동을 해제합니다."
    )
    # DM 에서는 interaction.guild 가 None 이라 AttributeError 로 죽습니다.
    # (실제로 /출석 이 그렇게 터졌습니다). guild_only 를 걸면 디스코드가
    # **DM 목록에 아예 띄우지 않으므로** 방어 코드를 흩뿌릴 필요가 없습니다.
    @app_commands.guild_only()
    @app_commands.describe(user="연동을 해제할 유저")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def unlink(self, interaction: discord.Interaction, user: discord.User):
        removed = await remove_roblox_link(interaction.guild.id, user.id)
        embed = discord.Embed(
            title="🔗 연동 해제" if removed else "ℹ️ 연동 정보 없음",
            description=(f"{user.mention} 님의 로블록스 연동을 해제했습니다.\n"
                         "본인이 `/로블록스연동` 으로 다시 등록할 수 있습니다."
                         if removed else
                         f"{user.mention} 님은 연동된 계정이 없습니다."),
            color=discord.Color.orange() if removed else discord.Color.greyple())

        # 연동을 필수로 걸어둔 서버에서 연동만 지우고 인증 역할을 남겨두면,
        # '링크를 받아간 사람은 전원 연동돼 있다'는 전제가 깨집니다.
        # 그래서 인증 역할도 함께 회수합니다.
        if removed:
            member = interaction.guild.get_member(user.id)
            if member:
                note = await self._revoke_verify_if_required(interaction.guild, member)
                if note:
                    embed.add_field(name="♻️ 인증 역할", value=note, inline=False)

        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def _revoke_verify_if_required(self, guild: discord.Guild,
                                         member: discord.Member) -> str:
        """연동 필수 설정이 켜져 있으면 인증 역할을 회수하고 결과 문구를 돌려줍니다."""
        try:
            settings = await get_verify_settings(guild.id)
            if not settings.get("require_roblox", False):
                return ""
            role_id = settings.get("role_id")
            if not role_id:
                return ""
            role = guild.get_role(int(role_id))
            if role is None or not any(r.id == role.id for r in member.roles):
                return ""
            await member.remove_roles(role, reason="로블록스 연동 해제 (연동 필수 설정)")
            return f"{role.mention} 역할도 함께 회수했습니다.\n"                    "-# 다시 연동한 뒤 인증 버튼을 누르면 재발급됩니다."
        except discord.Forbidden:
            return "⚠️ 인증 역할을 회수하지 못했습니다 (봇 권한 부족)."
        except Exception as e:
            print(f"[RobloxLink] 인증 역할 회수 실패: {type(e).__name__}: {e}", flush=True)
            return "⚠️ 인증 역할 회수 중 오류가 발생했습니다."


async def _alert_relink(bot, guild_id: int, user_id: int,
                        before: dict, after: dict) -> None:
    """계정 교체를 로그 채널에 알립니다.

    갑작스러운 계정 교체는 그 자체로 확인해 볼 만한 신호입니다.
    (인증과 링크 보호는 같은 하위 시스템이라 로그 채널을 공유합니다)
    """
    try:
        settings = await get_verify_settings(guild_id)
        if not settings.get("roblox_relink_alert", True):
            return

        guard = await get_linkguard_settings(guild_id)
        channel_id = guard.get("log_channel_id")
        if not channel_id:
            return
        guild = bot.get_guild(int(guild_id))
        if guild is None:
            return
        channel = guild.get_channel(int(channel_id))
        if channel is None:
            return

        embed = discord.Embed(
            title="🔄 로블록스 계정 교체",
            description=f"<@{user_id}> 님이 연동 계정을 바꿨습니다.",
            color=discord.Color.orange()
        )
        embed.add_field(
            name="이전",
            value=f"**{before.get('roblox_name', '?')}**\n`{before.get('roblox_id', '?')}`",
            inline=True)
        embed.add_field(
            name="이후",
            value=f"**{after.get('roblox_name', '?')}**\n`{after.get('roblox_id', '?')}`",
            inline=True)
        embed.add_field(
            name="🙋 디스코드",
            value=f"`{user_id}`", inline=True)
        embed.set_footer(text="정상적인 계정 변경일 수도 있습니다 · /인증설정 에서 이 알림을 끌 수 있습니다")
        embed.timestamp = discord.utils.utcnow()
        await channel.send(embed=embed)
    except Exception as e:
        # 알림 실패가 연동 자체를 막으면 안 됩니다
        print(f"[RobloxLink] 교체 알림 실패: {type(e).__name__}: {e}", flush=True)


def _make_saver(bot, loop):
    """Flask 스레드가 호출할 저장 함수를 만듭니다.

    콜백은 Flask 스레드에서 처리되는데 저장은 async 라,
    봇의 이벤트 루프로 넘겨 실행하고 결과만 받아옵니다.
    """
    async def _save(guild_id: int, user_id: int, info: dict):
        # 로블록스 계정 하나는 디스코드 하나에만 연동되어야 합니다.
        # 그러지 않으면 여러 명이 같은 계정을 등록해 명단 대조가 무의미해집니다.
        owner = await find_roblox_owner(guild_id, info["roblox_id"])
        if owner and str(owner.get("user_id")) != str(user_id):
            return False, "taken"

        # 교체인지 판단하기 위해 이전 연동을 먼저 확인합니다
        previous = await get_roblox_link(guild_id, user_id)

        await save_roblox_link(
            guild_id, user_id, info["roblox_id"], info["roblox_name"],
            method="oauth", created_at=info.get("created_at", ""))

        if previous and int(previous.get("roblox_id", 0)) != int(info["roblox_id"]):
            print(f"[RobloxLink] 계정 교체: discord={user_id} "
                  f"{previous.get('roblox_id')} -> {info['roblox_id']}", flush=True)
            await _alert_relink(bot, guild_id, user_id, previous, info)
        return True, "ok"

    def bridge(guild_id: int, user_id: int, info: dict):
        future = asyncio.run_coroutine_threadsafe(
            _save(guild_id, user_id, info), loop)
        return future.result(timeout=15)

    return bridge


async def setup(bot):
    await bot.add_cog(RobloxLink(bot))
    # Flask 스레드에서 저장할 수 있도록 봇의 이벤트 루프를 연결합니다.
    roblox_oauth.set_saver(_make_saver(bot, asyncio.get_running_loop()))
    if roblox_oauth.enabled():
        print("[RobloxLink] 로블록스 연동: 활성화 (OAuth)", flush=True)
    else:
        print("[RobloxLink] 로블록스 연동: 비활성 "
              "(ROBLOX_CLIENT_ID / ROBLOX_CLIENT_SECRET 미설정)", flush=True)
