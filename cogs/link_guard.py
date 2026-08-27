"""
웹훅 임베드 링크 보호 시스템 (독립 모듈).

지정한 채널에 **웹훅**이 보낸 임베드를 감지해서,
임베드 안의 링크를 제거한 뒤 버튼으로 치환합니다.
버튼을 누르면 인증 역할 보유 여부를 검사해, 인증된 유저에게만
본인에게만 보이는(에페메랄) 메시지로 실제 링크를 전달합니다.

포인트/상점/뽑기/제보 시스템과 전혀 연동되지 않습니다.
(이코노미 모듈을 import 하지 않고, 활동 로그도 남기지 않습니다)
"""

import re
import asyncio
import discord
from discord.ext import commands
from discord import app_commands
from utils.verify_db import (
    get_linkguard_settings, save_linkguard_settings,
    save_protected_link, get_protected_link,
    get_verify_settings, add_click_log, get_click_logs,
    make_watermark_code, save_watermark, get_watermark,
    register_guarded_keys, get_guarded_keys,
)

# 봇 재시작 후에도 버튼이 살아있도록 고정 custom_id 사용
# (링크 원본은 재게시된 메시지 ID를 키로 DB에 저장합니다)
PLINK_BUTTON_ID = "hbh_plink:open"

MD_LINK_RE = re.compile(r'\[([^\]]+)\]\(\s*(https?://[^\s)]+)\s*\)')
URL_RE = re.compile(r'https?://[^\s<>"\'`\]\)]+')

# 디스코드 링크 버튼이 허용하는 URL 최대 길이
MAX_URL_LENGTH = 512

# 로블록스 개인서버(Private Server) 링크 판별
#   https://www.roblox.com/games/<placeId>/<이름>?privateServerLinkCode=<코드>
#   https://www.roblox.com/share?code=<코드>&type=Server
ROBLOX_HOST_RE = re.compile(r'^https?://(?:www\.)?roblox\.com/', re.IGNORECASE)
ROBLOX_PLACE_RE = re.compile(r'roblox\.com/games/(\d+)', re.IGNORECASE)
ROBLOX_PS_CODE_RE = re.compile(r'privateServerLinkCode=([A-Za-z0-9_\-]+)', re.IGNORECASE)
ROBLOX_SHARE_RE = re.compile(r'roblox\.com/share\?[^\s]*\bcode=([A-Za-z0-9_\-]+)', re.IGNORECASE)


def clean_url(url: str) -> str:
    """문장 끝 문장부호가 URL에 딸려 들어오는 것을 방지합니다."""
    return url.strip().rstrip('.,!?;:')


def is_roblox_link(url: str) -> bool:
    return bool(ROBLOX_HOST_RE.match(url or ""))


def is_roblox_private_server(url: str) -> bool:
    """로블록스 개인서버 초대 링크인지 판별합니다."""
    if not is_roblox_link(url):
        return False
    if ROBLOX_PS_CODE_RE.search(url):
        return True
    # 신형 공유 링크는 type=Server 인 경우가 개인서버입니다
    return bool(ROBLOX_SHARE_RE.search(url)) and "type=server" in url.lower()


def resolve_base_channel(channel):
    """스레드면 부모 채널을, 그 외에는 채널 자신을 돌려줍니다."""
    if isinstance(channel, discord.Thread):
        return channel.parent
    return channel


def is_guarded_channel(channel, settings: dict) -> bool:
    """이 채널이 감시 대상인지 판정합니다.

    개별 채널 지정과 **카테고리 지정**을 모두 지원합니다.
    카테고리를 지정하면 그 안의 모든 채널(나중에 새로 만든 채널 포함)이
    자동으로 감시 대상이 됩니다.
    """
    base = resolve_base_channel(channel)
    if base is None:
        return False

    if base.id in settings.get("channel_ids", []):
        return True

    category_id = getattr(base, "category_id", None)
    return category_id is not None and category_id in settings.get("category_ids", [])


def collect_guarded_channels(guild: discord.Guild, settings: dict) -> list:
    """현재 감시 대상인 텍스트 채널을 모두 모읍니다 (권한 점검용)."""
    seen: dict[int, object] = {}

    for cid in settings.get("channel_ids", []):
        ch = guild.get_channel(cid)
        if ch is not None:
            seen[ch.id] = ch

    for cat_id in settings.get("category_ids", []):
        category = guild.get_channel(cat_id)
        if isinstance(category, discord.CategoryChannel):
            for ch in category.text_channels:
                seen[ch.id] = ch

    return list(seen.values())


def extract_component_links(message) -> list[str]:
    """메시지에 붙은 링크 버튼에서 URL을 뽑아냅니다.

    일부 매크로 웹훅(예: MultiScope)은 링크를 임베드 본문이 아니라
    **[Join Server] 같은 링크 버튼**에 담아 보냅니다. 본문만 검사하면
    이런 메시지가 무방비로 통과하므로 컴포넌트도 함께 훑습니다.

    디스코드 컴포넌트는 중첩될 수 있어 재귀적으로 탐색합니다.
    """
    found: list[str] = []

    def walk(node, depth: int = 0):
        if node is None or depth > 6:
            return
        url = getattr(node, "url", None)
        if isinstance(url, str) and url.startswith(("http://", "https://")):
            found.append(clean_url(url))
        for attr in ("children", "items", "components"):
            sub = getattr(node, attr, None)
            if not sub:
                continue
            try:
                for child in sub:
                    walk(child, depth + 1)
            except TypeError:
                continue

    try:
        for row in getattr(message, "components", None) or []:
            walk(row)
    except Exception as e:
        print(f"[LinkGuard] 컴포넌트 링크 추출 오류: {e}", flush=True)

    return list(dict.fromkeys(found))


def link_key(url: str) -> str:
    """링크를 식별하는 키를 만듭니다.

    로블록스 개인서버는 **링크 코드**가 본질이므로, URL 표기가 조금 달라져도
    (게임 이름 부분이 바뀌거나 파라미터 순서가 달라져도) 같은 링크로 인식됩니다.
    """
    if not url:
        return ""
    ps = ROBLOX_PS_CODE_RE.search(url)
    if ps:
        return f"ps:{ps.group(1).lower()}"
    share = ROBLOX_SHARE_RE.search(url)
    if share:
        return f"sh:{share.group(1).lower()}"
    return url.split("#", 1)[0].strip().lower()


def apply_watermark(url: str, code: str) -> str:
    """URL 뒤에 유저별 워터마크를 프래그먼트로 붙입니다.

    프래그먼트(#...)는 서버로 전송되지 않으므로 로블록스 링크 동작에 영향이 없고,
    링크를 복사해 퍼뜨리면 워터마크가 함께 따라갑니다.
    """
    base = url.split("#", 1)[0]
    return f"{base}#hbh-{code}"


WATERMARK_RE = re.compile(r'#hbh-([0-9a-f]{8})', re.IGNORECASE)


def describe_roblox_link(url: str) -> str:
    """관리자 화면에 보여줄 링크 요약 문자열을 만듭니다 (서버 코드는 가립니다)."""
    if not url:
        return "알 수 없는 링크"

    place = ROBLOX_PLACE_RE.search(url)
    code_match = ROBLOX_PS_CODE_RE.search(url) or ROBLOX_SHARE_RE.search(url)
    parts = []
    if place:
        parts.append(f"placeId `{place.group(1)}`")
    if code_match:
        code = code_match.group(1)
        masked = f"{code[:4]}…{code[-2:]}" if len(code) > 8 else "…"
        parts.append(f"코드 `{masked}`")
    if parts:
        return "🎮 " + " · ".join(parts)

    if is_roblox_link(url):
        return "🎮 로블록스 링크"

    # 로블록스가 아닌 링크는 호스트만 보여줍니다
    host = url.split("//", 1)[-1].split("/", 1)[0]
    return f"🔗 `{host[:60]}`" if host else "🔗 링크"


def strip_links(text: str, locked_text: str) -> tuple[str, list[str]]:
    """텍스트에서 링크를 뽑아내고, 그 자리를 잠금 문구로 치환합니다."""
    if not text:
        return text, []

    found: list[str] = []

    def _md_sub(match: re.Match) -> str:
        found.append(clean_url(match.group(2)))
        return f"{match.group(1)} `{locked_text}`"

    result = MD_LINK_RE.sub(_md_sub, text)

    def _url_sub(match: re.Match) -> str:
        found.append(clean_url(match.group(0)))
        return f"`{locked_text}`"

    result = URL_RE.sub(_url_sub, result)
    return result, found


def sanitize_embed(embed: discord.Embed, locked_text: str) -> tuple[discord.Embed, list[str]]:
    """임베드에서 링크를 제거한 사본과, 추출된 링크 목록을 반환합니다.

    이미지/썸네일/아이콘 URL은 실제 열람 대상이 아니므로 건드리지 않습니다.
    """
    data = embed.to_dict()
    links: list[str] = []

    # 제목의 하이퍼링크 제거 (title 클릭이 불가능해집니다)
    if data.get("url"):
        links.append(clean_url(data.pop("url")))

    if data.get("title"):
        data["title"], found = strip_links(data["title"], locked_text)
        links.extend(found)

    if data.get("description"):
        data["description"], found = strip_links(data["description"], locked_text)
        links.extend(found)

    for field in data.get("fields", []):
        if field.get("value"):
            field["value"], found = strip_links(field["value"], locked_text)
            links.extend(found)
        if field.get("name"):
            field["name"], found = strip_links(field["name"], locked_text)
            links.extend(found)

    author = data.get("author")
    if isinstance(author, dict) and author.get("url"):
        links.append(clean_url(author.pop("url")))

    # 중복 제거 (순서 유지)
    unique_links = list(dict.fromkeys(links))
    return discord.Embed.from_dict(data), unique_links


# ── 보호된 링크 버튼 (영구 View) ──────────────────────────────────────

class ProtectedLinkView(discord.ui.View):
    """보호된 링크 버튼.

    수백 명이 동시에 누르는 상황을 전제로, 클릭 처리 경로에서 DB·API 호출을
    최대한 제거했습니다. 링크 목록과 인증 설정은 메모리 캐시에서 읽고,
    역할 검사도 이미 메모리에 있는 `interaction.user.roles`로 처리합니다.
    클릭 기록은 응답을 보낸 **뒤에** 백그라운드로 남깁니다.
    """

    def __init__(self, button_label: str = "🎮 개인서버 입장하기"):
        super().__init__(timeout=None)
        self.open_btn.label = button_label

    @discord.ui.button(label="🎮 개인서버 입장하기", style=discord.ButtonStyle.success, custom_id=PLINK_BUTTON_ID)
    async def open_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild
        if guild is None or interaction.message is None:
            return

        # 1) 링크 조회 (봇이 살아있는 동안은 메모리 캐시에서 즉시 반환)
        links = await get_protected_link(guild.id, interaction.message.id)
        if not links:
            await interaction.response.send_message(
                "⚠️ 이 메시지에 연결된 링크 정보를 찾을 수 없습니다.\n"
                "잠시 후 다시 시도하시거나 관리자에게 문의해 주세요.",
                ephemeral=True
            )
            return

        settings = await get_linkguard_settings(guild.id)
        member = interaction.user

        # 2) 시스템이 꺼져 있으면 게이트를 열어 둡니다.
        #    `/인증시스템` 비상 정지 시 "기능이 없던 상태"와 같아야 하므로,
        #    이미 게시된 보호 메시지도 누구나 링크를 받을 수 있어야 합니다.
        #    (여기서 막으면 정지했는데도 유저가 못 들어가는 상황이 됩니다)
        if not settings.get("enabled", True):
            await self._send_links(interaction, links, code="", settings=settings, gated=False)
            return

        # 3) 인증 설정 조회 (메모리 캐시)
        verify_settings = await get_verify_settings(guild.id)
        role_id = verify_settings.get("role_id")

        if not role_id:
            await interaction.response.send_message(
                "⚠️ 인증 역할이 설정되지 않아 링크를 확인할 수 없습니다. 관리자에게 문의해 주세요.",
                ephemeral=True
            )
            return

        # 4) 역할 검사 (이미 메모리에 있는 정보라 추가 요청이 없습니다)
        if not any(r.id == int(role_id) for r in getattr(member, "roles", [])):
            role = guild.get_role(int(role_id))
            panel_id = verify_settings.get("panel_channel_id")
            panel = guild.get_channel(int(panel_id)) if panel_id else None
            where = f"\n👉 {panel.mention} 채널에서 인증을 진행해 주세요." if panel else ""

            embed = discord.Embed(
                title="🔒 인증이 필요합니다",
                description=f"이 링크는 {role.mention if role else '인증'} 역할을 보유한 유저만 열람할 수 있습니다."
                            f"{where}",
                color=discord.Color.red()
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        # 5) 인증 통과 → 곧바로 눌러 들어갈 수 있는 링크 버튼만 전달합니다.
        #    URL을 본문 텍스트로 적지 않아, 눈으로 보고 복사하기 어렵게 만듭니다.
        use_watermark = settings.get("watermark", True)
        code = make_watermark_code(guild.id, member.id) if use_watermark else ""
        await self._send_links(interaction, links, code=code, settings=settings, gated=True)

    async def _send_links(self, interaction, links, code: str, settings: dict, gated: bool) -> None:
        """링크 버튼을 본인에게만 보이는 메시지로 전달합니다.

        gated=False 는 시스템이 정지된 상태(누구나 열람 가능)를 뜻하며,
        이때는 워터마크와 열람 기록을 남기지 않습니다.
        """
        guild = interaction.guild
        member = interaction.user

        link_view = discord.ui.View(timeout=None)
        overflow: list[str] = []
        has_private_server = False

        for idx, link in enumerate(links[:5], start=1):
            marked = apply_watermark(link, code) if code else link
            if len(marked) > MAX_URL_LENGTH:
                overflow.append(link)
                continue

            if is_roblox_private_server(link):
                has_private_server = True
                label = "🎮 개인서버 입장" if len(links) == 1 else f"🎮 개인서버 {idx}"
            elif is_roblox_link(link):
                label = "🎮 로블록스 열기" if len(links) == 1 else f"🎮 로블록스 {idx}"
            else:
                label = "바로 가기" if len(links) == 1 else f"바로 가기 {idx}"

            try:
                link_view.add_item(discord.ui.Button(label=label, url=marked, style=discord.ButtonStyle.link))
            except Exception:
                overflow.append(link)

        headline = ("🎮 **개인서버가 열렸습니다!** 아래 버튼을 누르면 바로 접속됩니다."
                    if has_private_server else
                    "🔓 **아래 버튼을 눌러 이동하세요.**")

        if not gated:
            content = headline + "\n-# 현재 인증 시스템이 정지되어 있어 누구나 열람할 수 있습니다."
        elif code:
            content = (headline + "\n"
                       "-# 이 메시지는 본인에게만 보입니다. 이 링크에는 회원님을 식별하는 표식이 들어 있어,\n"
                       "-# 외부로 유출되면 누가 퍼뜨렸는지 확인됩니다.")
        else:
            content = headline + "\n-# 이 메시지는 본인에게만 보이며, 열람 기록이 남습니다."

        if overflow:
            content += "\n\n⚠️ 버튼으로 만들 수 없는 링크:\n" + "\n".join(f"• {u}" for u in overflow[:3])

        view_arg = link_view if link_view.children else discord.utils.MISSING
        await interaction.response.send_message(content=content, view=view_arg, ephemeral=True)

        # 기록은 응답을 보낸 뒤에 백그라운드로 남깁니다 (클릭 속도에 영향 없음)
        if gated:
            asyncio.create_task(
                _after_click(guild, member, links[0], code, settings)
            )


async def _after_click(guild, member, url: str, code: str, settings: dict) -> None:
    """클릭 응답을 보낸 뒤 실행되는 뒷정리 — 기록 저장과 로그 채널 발송."""
    try:
        if code:
            await save_watermark(guild.id, code, member.id, member.display_name)

        await add_click_log(
            guild.id, member.id, member.display_name, url,
            event_type="view",
            detail=f"워터마크 `{code}`" if code else "워터마크 없음"
        )

        embed = discord.Embed(
            title="👁️ 링크 열람",
            description=f"{member.mention} 님이 보호된 링크를 열람했습니다.",
            color=discord.Color.blue()
        )
        embed.add_field(name="👤 유저", value=f"**{member.display_name}**\n`{member.id}`", inline=True)
        embed.add_field(name="🔖 워터마크", value=f"`{code}`" if code else "`없음`", inline=True)
        embed.add_field(name="🔗 링크", value=describe_roblox_link(url), inline=False)
        embed.timestamp = discord.utils.utcnow()
        await send_guard_log(guild, settings, embed)
    except Exception as e:
        print(f"[LinkGuard] 클릭 후처리 오류: {e}", flush=True)


async def send_guard_log(guild: discord.Guild, settings: dict, embed: discord.Embed) -> None:
    """설정된 로그 채널로 기록 임베드를 보냅니다 (미설정이면 조용히 넘어갑니다)."""
    channel_id = settings.get("log_channel_id")
    if not channel_id:
        return
    channel = guild.get_channel(int(channel_id))
    if channel is None:
        return
    try:
        await channel.send(embed=embed)
    except Exception as e:
        print(f"[LinkGuard] 로그 채널 전송 실패: {e}", flush=True)


# ── 관리자 설정 UI ────────────────────────────────────────────────────

class GuardTextModal(discord.ui.Modal, title="📝 링크 보호 문구 설정"):
    def __init__(self, current: dict):
        super().__init__()
        self.label_input = discord.ui.TextInput(
            label="링크 버튼에 표시할 문구",
            default=current.get("button_label", "🎮 개인서버 입장하기"),
            required=True,
            max_length=40
        )
        self.locked_input = discord.ui.TextInput(
            label="링크 자리에 대신 표시할 잠금 문구",
            default=current.get("locked_text", "🔒 인증된 유저만 입장할 수 있습니다"),
            required=True,
            max_length=80
        )
        self.add_item(self.label_input)
        self.add_item(self.locked_input)

    async def on_submit(self, interaction: discord.Interaction):
        await save_linkguard_settings(interaction.guild.id, {
            "button_label": self.label_input.value.strip(),
            "locked_text": self.locked_input.value.strip(),
        })
        embed = discord.Embed(
            title="✅ 문구 저장 완료",
            description="변경된 문구는 **이후 새로 감지되는 임베드**부터 적용됩니다.",
            color=discord.Color.green()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


class AddGuardChannelSelect(discord.ui.ChannelSelect):
    def __init__(self):
        super().__init__(
            placeholder="보호할 채널을 선택하세요...",
            channel_types=[discord.ChannelType.text],
            max_values=5
        )

    async def callback(self, interaction: discord.Interaction):
        settings = await get_linkguard_settings(interaction.guild.id)
        channel_ids = list(settings.get("channel_ids", []))

        added = []
        for selected in self.values:
            if selected.id not in channel_ids:
                channel_ids.append(selected.id)
                added.append(selected.id)

        await save_linkguard_settings(interaction.guild.id, {"channel_ids": channel_ids})

        me = interaction.guild.me
        lines = []
        for cid in channel_ids:
            channel = interaction.guild.get_channel(cid)
            if channel is None:
                lines.append(f"• <#{cid}> ⚠️ 채널을 찾을 수 없음")
                continue
            can_delete = channel.permissions_for(me).manage_messages if me else False
            can_send = channel.permissions_for(me).send_messages if me else False
            mark = "✅" if (can_delete and can_send) else "⚠️"
            note = "" if (can_delete and can_send) else " (메시지 관리 / 보내기 권한 필요)"
            lines.append(f"• {mark} {channel.mention}{note}")

        embed = discord.Embed(
            title="✅ 보호 채널 추가 완료",
            description=f"새로 추가된 채널: **{len(added)}개**\n\n**현재 보호 중인 채널**\n" + ("\n".join(lines) or "없음"),
            color=discord.Color.green()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


class AddGuardChannelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)
        self.add_item(AddGuardChannelSelect())


class RemoveGuardChannelSelect(discord.ui.Select):
    def __init__(self, channel_ids: list[int], guild: discord.Guild):
        options = []
        for cid in channel_ids[:25]:
            channel = guild.get_channel(cid)
            name = f"#{channel.name}" if channel else f"채널 ID {cid}"
            options.append(
                discord.SelectOption(
                    label=name[:100],
                    value=str(cid),
                    description="선택 시 이 채널의 링크 보호가 해제됩니다",
                    emoji="❌"
                )
            )
        super().__init__(placeholder="보호를 해제할 채널을 선택하세요...", options=options)

    async def callback(self, interaction: discord.Interaction):
        cid = int(self.values[0])
        settings = await get_linkguard_settings(interaction.guild.id)
        channel_ids = [c for c in settings.get("channel_ids", []) if c != cid]
        await save_linkguard_settings(interaction.guild.id, {"channel_ids": channel_ids})

        channel = interaction.guild.get_channel(cid)
        mention = channel.mention if channel else f"<#{cid}>"
        embed = discord.Embed(
            title="❌ 보호 채널 해제 완료",
            description=f"{mention} 채널의 링크 보호가 해제되었습니다.\n"
                        "이제 이 채널의 웹훅 임베드는 그대로 표시됩니다.",
            color=discord.Color.orange()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


class RemoveGuardChannelView(discord.ui.View):
    def __init__(self, channel_ids: list[int], guild: discord.Guild):
        super().__init__(timeout=180)
        self.add_item(RemoveGuardChannelSelect(channel_ids, guild))


class AddCategorySelect(discord.ui.ChannelSelect):
    def __init__(self):
        super().__init__(
            placeholder="감시할 카테고리를 선택하세요 (여러 개 가능)...",
            channel_types=[discord.ChannelType.category],
            max_values=25
        )

    async def callback(self, interaction: discord.Interaction):
        guild = interaction.guild
        settings = await get_linkguard_settings(guild.id)
        category_ids = list(settings.get("category_ids", []))

        added = 0
        for selected in self.values:
            if selected.id not in category_ids:
                category_ids.append(selected.id)
                added += 1

        await save_linkguard_settings(guild.id, {"category_ids": category_ids})

        settings["category_ids"] = category_ids
        channels = collect_guarded_channels(guild, settings)

        embed = discord.Embed(
            title="✅ 감시 카테고리 추가 완료",
            description=f"새로 추가된 카테고리: **{added}개**\n"
                        f"현재 감시 카테고리: **{len(category_ids)}개** "
                        f"→ 그 안의 텍스트 채널 **{len(channels)}개**가 감시 대상입니다.\n\n"
                        "-# 카테고리에 채널을 새로 만들면 **자동으로 감시 대상에 포함**됩니다.",
            color=discord.Color.green()
        )

        me = guild.me
        lines = []
        for cat_id in category_ids[:15]:
            category = guild.get_channel(cat_id)
            if not isinstance(category, discord.CategoryChannel):
                lines.append(f"• ⚠️ <#{cat_id}> 카테고리를 찾을 수 없음")
                continue
            texts = category.text_channels
            bad = [
                c for c in texts
                if me and not (
                    c.permissions_for(me).manage_messages
                    and c.permissions_for(me).send_messages
                    and c.permissions_for(me).view_channel
                )
            ]
            mark = "✅" if not bad else "⚠️"
            note = "" if not bad else f" — 채널 {len(bad)}개 권한 부족"
            lines.append(f"• {mark} **{category.name}** (채널 {len(texts)}개){note}")

        embed.add_field(name="📂 감시 카테고리", value="\n".join(lines) or "없음", inline=False)
        if any("⚠️" in ln for ln in lines):
            embed.add_field(
                name="🔑 권한 안내",
                value="봇에게 해당 채널의 **채널 보기 · 메시지 보내기 · 메시지 관리** 권한이 필요합니다.\n"
                      "비공개 채널은 봇 역할을 채널 권한에 추가해 주세요.\n"
                      "`현재 설정 및 권한 점검`에서 문제 채널을 확인할 수 있습니다.",
                inline=False
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)


class AddCategoryView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)
        self.add_item(AddCategorySelect())


def build_category_result_embed(guild: discord.Guild, category_ids: list[int],
                                added: list[int], title: str) -> discord.Embed:
    """카테고리 추가 결과와 권한 상태를 정리한 임베드를 만듭니다."""
    me = guild.me
    channels = collect_guarded_channels(guild, {"category_ids": category_ids, "channel_ids": []})
    blocked = [c for c in channels if not _channel_ready(c, me)]

    embed = discord.Embed(
        title=title,
        description=f"새로 추가된 카테고리: **{len(added)}개**\n"
                    f"현재 감시 카테고리: **{len(category_ids)}개** "
                    f"→ 텍스트 채널 **{len(channels)}개**\n\n"
                    "-# 카테고리에 채널을 새로 만들면 자동으로 감시 대상에 포함됩니다.",
        color=discord.Color.green() if not blocked else discord.Color.orange()
    )

    if added:
        names = []
        for cid in added[:20]:
            cat = guild.get_channel(cid)
            names.append(f"• {cat.name}" if isinstance(cat, discord.CategoryChannel) else f"• `{cid}`")
        embed.add_field(name="➕ 추가된 카테고리", value="\n".join(names)[:1024], inline=False)

    if blocked:
        embed.add_field(
            name=f"⚠️ 봇 권한이 없는 채널 {len(blocked)}개",
            value=("이 채널들은 **감시되지 않습니다.**\n"
                   + ", ".join(c.mention for c in blocked[:8])
                   + ("…" if len(blocked) > 8 else "")
                   + "\n\n카테고리 편집 → 권한에서 봇 역할에\n"
                   "`채널 보기` · `메시지 보내기` · `메시지 관리` 를 주세요.")[:1024],
            inline=False
        )
    elif channels:
        embed.add_field(name="🔑 권한 점검", value="✅ 모든 채널에서 정상 동작합니다.", inline=False)

    return embed


class CategoryNameModal(discord.ui.Modal, title="🔎 이름으로 카테고리 일괄 추가"):
    keyword_input = discord.ui.TextInput(
        label="카테고리 이름에 포함된 단어를 입력하세요",
        placeholder="예: 매크로 채널   → '매크로 채널 1~15' 전부 추가",
        required=True,
        max_length=100
    )

    async def on_submit(self, interaction: discord.Interaction):
        guild = interaction.guild
        keyword = self.keyword_input.value.strip().lower()
        if not keyword:
            await interaction.response.send_message("⚠️ 검색할 단어를 입력해 주세요.", ephemeral=True)
            return

        matched = [c for c in guild.categories if keyword in c.name.lower()]
        if not matched:
            await interaction.response.send_message(
                f"⚠️ 이름에 `{self.keyword_input.value.strip()}` 이(가) 들어간 카테고리를 찾지 못했습니다.\n"
                "-# 봇이 볼 수 없는 카테고리는 검색되지 않습니다. "
                "카테고리 권한에 봇 역할을 추가한 뒤 다시 시도해 주세요.",
                ephemeral=True
            )
            return

        settings = await get_linkguard_settings(guild.id)
        category_ids = list(settings.get("category_ids", []))
        added = [c.id for c in matched if c.id not in category_ids]
        category_ids.extend(added)

        await save_linkguard_settings(guild.id, {"category_ids": category_ids})
        embed = build_category_result_embed(
            guild, category_ids, added,
            f"✅ '{self.keyword_input.value.strip()}' 카테고리 {len(matched)}개 검색됨"
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


class CategoryIdModal(discord.ui.Modal, title="🆔 카테고리 ID로 추가"):
    ids_input = discord.ui.TextInput(
        label="카테고리 ID를 입력하세요 (여러 개 가능)",
        style=discord.TextStyle.paragraph,
        placeholder="쉼표 · 띄어쓰기 · 줄바꿈으로 구분\n예:\n1329788098187624500\n1329788098187624501",
        required=True,
        max_length=2000
    )

    async def on_submit(self, interaction: discord.Interaction):
        guild = interaction.guild
        raw_ids = [int(m) for m in re.findall(r'\d{17,20}', self.ids_input.value)]
        if not raw_ids:
            await interaction.response.send_message(
                "⚠️ 올바른 카테고리 ID를 찾지 못했습니다.\n"
                "-# 디스코드 설정 → 고급 → 개발자 모드를 켠 뒤, 카테고리 우클릭 → `ID 복사`",
                ephemeral=True
            )
            return

        settings = await get_linkguard_settings(guild.id)
        category_ids = list(settings.get("category_ids", []))

        added, not_found = [], []
        for cid in raw_ids:
            target = guild.get_channel(cid)
            if not isinstance(target, discord.CategoryChannel):
                not_found.append(cid)
                continue
            if cid not in category_ids:
                category_ids.append(cid)
                added.append(cid)

        await save_linkguard_settings(guild.id, {"category_ids": category_ids})
        embed = build_category_result_embed(
            guild, category_ids, added, "✅ 카테고리 ID 추가 완료"
        )
        if not_found:
            embed.add_field(
                name=f"⚠️ 찾지 못한 ID {len(not_found)}개",
                value=("\n".join(f"• `{c}`" for c in not_found[:10])
                       + "\n\n카테고리가 아니거나, **봇이 볼 수 없는 카테고리**입니다.\n"
                         "카테고리 권한에 봇 역할을 추가해 주세요.")[:1024],
                inline=False
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)


class RemoveCategorySelect(discord.ui.Select):
    def __init__(self, category_ids: list[int], guild: discord.Guild):
        options = []
        for cat_id in category_ids[:25]:
            category = guild.get_channel(cat_id)
            if isinstance(category, discord.CategoryChannel):
                name = category.name
                desc = f"텍스트 채널 {len(category.text_channels)}개의 감시가 해제됩니다"
            else:
                name = f"카테고리 ID {cat_id}"
                desc = "선택 시 감시 목록에서 제거됩니다"
            options.append(
                discord.SelectOption(label=name[:100], value=str(cat_id), description=desc[:95], emoji="❌")
            )
        super().__init__(placeholder="감시를 해제할 카테고리를 선택하세요...", options=options)

    async def callback(self, interaction: discord.Interaction):
        cat_id = int(self.values[0])
        settings = await get_linkguard_settings(interaction.guild.id)
        category_ids = [c for c in settings.get("category_ids", []) if c != cat_id]
        await save_linkguard_settings(interaction.guild.id, {"category_ids": category_ids})

        category = interaction.guild.get_channel(cat_id)
        name = category.name if isinstance(category, discord.CategoryChannel) else str(cat_id)
        embed = discord.Embed(
            title="❌ 감시 카테고리 해제 완료",
            description=f"**{name}** 카테고리의 링크 보호가 해제되었습니다.\n"
                        "이제 이 카테고리의 웹훅 임베드는 그대로 표시됩니다.",
            color=discord.Color.orange()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


class RemoveCategoryView(discord.ui.View):
    def __init__(self, category_ids: list[int], guild: discord.Guild):
        super().__init__(timeout=180)
        self.add_item(RemoveCategorySelect(category_ids, guild))


class LogChannelSelect(discord.ui.ChannelSelect):
    def __init__(self):
        super().__init__(
            placeholder="기록을 보낼 로그 채널을 선택하세요...",
            channel_types=[discord.ChannelType.text]
        )

    async def callback(self, interaction: discord.Interaction):
        channel = interaction.guild.get_channel(self.values[0].id)
        if channel is None:
            await interaction.response.send_message("⚠️ 채널을 찾을 수 없습니다.", ephemeral=True)
            return

        await save_linkguard_settings(interaction.guild.id, {"log_channel_id": channel.id})

        me = interaction.guild.me
        can_send = channel.permissions_for(me).send_messages if me else False

        embed = discord.Embed(
            title="✅ 로그 채널 설정 완료",
            description=f"앞으로 다음 기록이 {channel.mention} 채널로 전송됩니다.\n\n"
                        "• 🛡️ 새 링크 보호 시작\n"
                        "• 👁️ 링크 열람 (누가 · 언제 · 워터마크)\n"
                        "• 🚨 링크 유출 감지 (게시자 · 원 수령자 · 조치 결과)",
            color=discord.Color.green() if can_send else discord.Color.orange()
        )
        if not can_send:
            embed.add_field(
                name="⚠️ 권한 경고",
                value=f"봇이 {channel.mention} 채널에 메시지를 보낼 권한이 없습니다. 채널 권한을 확인해 주세요.",
                inline=False
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)


class LogChannelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)
        self.add_item(LogChannelSelect())


class AutoDeleteToggleView(discord.ui.View):
    def __init__(self, settings: dict):
        super().__init__(timeout=180)
        self.current = settings.get("auto_delete_leak", True)
        self.toggle_btn.label = "자동 삭제 끄기" if self.current else "자동 삭제 켜기"
        self.toggle_btn.style = discord.ButtonStyle.danger if self.current else discord.ButtonStyle.success

    @discord.ui.button(label="자동 삭제 끄기", style=discord.ButtonStyle.danger, emoji="🧹")
    async def toggle_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        new_state = not self.current
        await save_linkguard_settings(interaction.guild.id, {"auto_delete_leak": new_state})
        embed = discord.Embed(
            title="🧹 유출 메시지 자동 삭제 설정 변경",
            description=f"유출이 감지된 메시지의 자동 삭제가 **{'✅ 켜짐' if new_state else '❌ 꺼짐'}** 상태가 되었습니다.",
            color=discord.Color.green() if new_state else discord.Color.red()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


class GuardSettingsSelect(discord.ui.Select):
    def __init__(self, current: dict):
        channel_count = len(current.get("channel_ids", []))
        category_count = len(current.get("category_ids", []))
        enabled_desc = "현재: ✅ 활성화" if current.get("enabled", True) else "현재: ❌ 비활성화"

        options = [
            discord.SelectOption(
                label="시스템 활성화 / 비활성화",
                value="toggle",
                description=enabled_desc,
                emoji="⚡"
            ),
            discord.SelectOption(
                label="이름으로 카테고리 일괄 추가 (권장)",
                value="addcatname",
                description="예: '매크로 채널' 입력 → 이름이 겹치는 카테고리 전부 추가",
                emoji="🔎"
            ),
            discord.SelectOption(
                label="카테고리 목록에서 추가",
                value="addcat",
                description="드롭다운에서 카테고리를 골라 감시 대상으로 지정합니다.",
                emoji="📂"
            ),
            discord.SelectOption(
                label="카테고리 ID로 추가",
                value="addcatid",
                description="목록에 안 보이는 카테고리를 ID로 직접 추가합니다.",
                emoji="🆔"
            ),
            discord.SelectOption(
                label="감시 카테고리 해제",
                value="removecat",
                description=f"현재 감시 중인 {category_count}개 카테고리 중 선택 해제",
                emoji="🗂️"
            ),
            discord.SelectOption(
                label="개별 채널 추가",
                value="add",
                description="카테고리 밖의 채널을 하나씩 추가합니다 (최대 5개씩).",
                emoji="➕"
            ),
            discord.SelectOption(
                label="개별 채널 해제",
                value="remove",
                description=f"개별 지정된 {channel_count}개 채널 중 선택 해제",
                emoji="❌"
            ),
            discord.SelectOption(
                label="버튼 / 잠금 문구 수정",
                value="text",
                description="링크 버튼 문구와 잠금 안내 문구를 변경합니다.",
                emoji="📝"
            ),
            discord.SelectOption(
                label="로그 채널 설정",
                value="logchannel",
                description="열람 · 유출 기록을 실시간으로 보낼 채널을 지정합니다.",
                emoji="📋"
            ),
            discord.SelectOption(
                label="유출 감지 기록",
                value="leaks",
                description="서버 안에서 링크를 퍼뜨린 유저 기록을 확인합니다.",
                emoji="🚨"
            ),
            discord.SelectOption(
                label="링크 열람 기록",
                value="clicks",
                description="누가 언제 개인서버 링크를 열었는지 확인합니다.",
                emoji="🔍"
            ),
            discord.SelectOption(
                label="워터마크 / 자동삭제 설정",
                value="protect",
                description="유출자 추적 표식과 유출 메시지 자동 삭제를 켜고 끕니다.",
                emoji="🔖"
            ),
            discord.SelectOption(
                label="현재 설정 및 권한 점검",
                value="status",
                description="보호 채널 목록과 봇 권한 상태를 확인합니다.",
                emoji="📊"
            ),
        ]
        super().__init__(placeholder="링크 보호 관리 기능을 선택하세요...", options=options)

    async def callback(self, interaction: discord.Interaction):
        choice = self.values[0]
        guild = interaction.guild
        settings = await get_linkguard_settings(guild.id)

        if choice == "toggle":
            new_state = not settings.get("enabled", True)
            await save_linkguard_settings(guild.id, {"enabled": new_state})
            state_text = "✅ 활성화" if new_state else "❌ 비활성화"
            embed = discord.Embed(
                title="⚡ 링크 보호 시스템 상태 변경",
                description=f"링크 보호 시스템이 **{state_text}** 상태로 변경되었습니다.",
                color=discord.Color.green() if new_state else discord.Color.red()
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)

        elif choice == "addcatname":
            await interaction.response.send_modal(CategoryNameModal())

        elif choice == "addcatid":
            await interaction.response.send_modal(CategoryIdModal())

        elif choice == "addcat":
            await interaction.response.send_message(
                "📂 **감시할 카테고리를 선택하세요.**\n"
                "-# 카테고리 안의 모든 텍스트 채널(비공개 포함)이 감시 대상이 되며,\n"
                "-# 나중에 새로 만드는 채널도 자동으로 포함됩니다.\n"
                "-# 목록에 안 보이면 검색창에 이름을 입력하거나, "
                "**이름으로 일괄 추가** / **ID로 추가**를 사용하세요.",
                view=AddCategoryView(),
                ephemeral=True
            )

        elif choice == "removecat":
            category_ids = settings.get("category_ids", [])
            if not category_ids:
                await interaction.response.send_message(
                    "ℹ️ 현재 감시 중인 카테고리가 없습니다.",
                    ephemeral=True
                )
                return
            await interaction.response.send_message(
                "🗂️ **감시를 해제할 카테고리를 선택하세요.**",
                view=RemoveCategoryView(category_ids, guild),
                ephemeral=True
            )

        elif choice == "add":
            await interaction.response.send_message(
                "➕ **웹훅 임베드를 감시할 채널을 선택하세요.**",
                view=AddGuardChannelView(),
                ephemeral=True
            )

        elif choice == "remove":
            channel_ids = settings.get("channel_ids", [])
            if not channel_ids:
                await interaction.response.send_message(
                    "ℹ️ 현재 보호 중인 채널이 없습니다.",
                    ephemeral=True
                )
                return
            await interaction.response.send_message(
                "❌ **보호를 해제할 채널을 선택하세요.**",
                view=RemoveGuardChannelView(channel_ids, guild),
                ephemeral=True
            )

        elif choice == "text":
            await interaction.response.send_modal(GuardTextModal(settings))

        elif choice == "logchannel":
            await interaction.response.send_message(
                "📋 **열람 · 유출 기록을 보낼 채널을 선택하세요.**\n"
                "-# 관리자만 볼 수 있는 채널로 지정하시길 권장합니다.",
                view=LogChannelView(),
                ephemeral=True
            )

        elif choice == "protect":
            wm = not settings.get("watermark", True)
            await save_linkguard_settings(guild.id, {"watermark": wm})
            embed = discord.Embed(
                title="🔖 워터마크 설정 변경",
                description=f"유출자 추적 워터마크가 **{'✅ 켜짐' if wm else '❌ 꺼짐'}** 상태가 되었습니다.\n\n"
                            "켜져 있으면 유저마다 다른 표식(`#hbh-xxxxxxxx`)이 링크에 붙어,\n"
                            "링크가 퍼졌을 때 **최초 수령자를 역추적**할 수 있습니다.\n"
                            "표식은 프래그먼트라 로블록스 접속에는 영향을 주지 않습니다.",
                color=discord.Color.green() if wm else discord.Color.red()
            )
            embed.add_field(
                name="🧹 유출 메시지 자동 삭제",
                value=("✅ 켜짐" if settings.get("auto_delete_leak", True) else "❌ 꺼짐")
                      + "\n-# 아래 버튼으로 변경할 수 있습니다.",
                inline=False
            )
            await interaction.response.send_message(
                embed=embed, view=AutoDeleteToggleView(settings), ephemeral=True
            )

        elif choice in ("clicks", "leaks"):
            is_leak = (choice == "leaks")
            logs = await get_click_logs(guild.id, limit=20, event_type="leak" if is_leak else "view")

            embed = discord.Embed(
                title="🚨 유출 감지 기록 (최근 20건)" if is_leak else "🔍 링크 열람 기록 (최근 20건)",
                description=("서버 안에서 보호 링크를 **다시 게시한** 유저 기록입니다."
                             if is_leak else
                             "개인서버 링크를 **열람한** 유저 기록입니다. 유출 시 용의자를 좁히는 데 씁니다."),
                color=discord.Color.red() if is_leak else discord.Color.dark_teal()
            )
            if not logs:
                embed.add_field(
                    name="ℹ️ 안내",
                    value="아직 감지된 유출이 없습니다." if is_leak else "아직 기록된 열람 이력이 없습니다.",
                    inline=False
                )
            else:
                lines = []
                for log in logs:
                    who = log.get("user_name", "알 수 없음")
                    uid = log.get("user_id", "")
                    ts = log.get("timestamp", "")
                    summary = describe_roblox_link(log.get("url", ""))
                    extra = log.get("detail", "")
                    lines.append(f"• `{ts}` **{who}** (<@{uid}>)\n  └ {summary}"
                                 + (f" · {extra}" if extra else ""))
                embed.add_field(name=f"📋 총 {len(logs)}건", value="\n".join(lines)[:1024], inline=False)
            embed.set_footer(text="링크 코드는 일부만 표시됩니다")
            await interaction.response.send_message(embed=embed, ephemeral=True)

        elif choice == "status":
            embed = build_status_embed(guild, settings, await get_verify_settings(guild.id))
            await interaction.response.send_message(embed=embed, ephemeral=True)


class GuardSettingsView(discord.ui.View):
    def __init__(self, current: dict):
        super().__init__(timeout=180)
        self.add_item(GuardSettingsSelect(current))


def _channel_ready(channel, me) -> bool:
    """봇이 이 채널에서 링크 보호를 수행할 수 있는지 확인합니다."""
    if me is None or channel is None:
        return False
    perms = channel.permissions_for(me)
    return perms.view_channel and perms.send_messages and perms.manage_messages


def build_status_embed(guild: discord.Guild, settings: dict, verify_settings: dict) -> discord.Embed:
    embed = discord.Embed(title="📊 링크 보호 시스템 현재 설정", color=discord.Color.blurple())
    embed.add_field(
        name="⚡ 시스템 상태",
        value="✅ 활성화" if settings.get("enabled", True) else "❌ 비활성화",
        inline=False
    )

    me = guild.me
    category_ids = settings.get("category_ids", [])
    channel_ids = settings.get("channel_ids", [])

    # 감시 카테고리
    if category_ids:
        cat_lines = []
        for cat_id in category_ids[:15]:
            category = guild.get_channel(cat_id)
            if not isinstance(category, discord.CategoryChannel):
                cat_lines.append(f"• ⚠️ `{cat_id}` 카테고리를 찾을 수 없음")
                continue
            texts = category.text_channels
            bad = [c for c in texts if not _channel_ready(c, me)]
            if bad:
                cat_lines.append(f"• ⚠️ **{category.name}** — 채널 {len(texts)}개 중 **{len(bad)}개 권한 부족**")
            else:
                cat_lines.append(f"• ✅ **{category.name}** — 채널 {len(texts)}개")
        embed.add_field(
            name=f"📂 감시 카테고리 ({len(category_ids)}개)",
            value="\n".join(cat_lines)[:1024],
            inline=False
        )

    # 개별 지정 채널
    if channel_ids:
        lines = []
        for cid in channel_ids[:10]:
            channel = guild.get_channel(cid)
            if channel is None:
                lines.append(f"• ⚠️ <#{cid}> 채널을 찾을 수 없음")
                continue
            lines.append(f"• {'✅' if _channel_ready(channel, me) else '⚠️'} {channel.mention}")
        embed.add_field(
            name=f"➕ 개별 지정 채널 ({len(channel_ids)}개)",
            value="\n".join(lines)[:1024],
            inline=False
        )

    guarded = collect_guarded_channels(guild, settings)
    if not guarded:
        embed.add_field(
            name="📡 감시 대상",
            value="아직 등록된 감시 대상이 없습니다. **감시 카테고리 추가**로 등록해 주세요.",
            inline=False
        )
    else:
        broken = [c for c in guarded if not _channel_ready(c, me)]
        summary = f"총 **{len(guarded)}개 채널**을 감시 중입니다."
        if broken:
            names = ", ".join(c.mention for c in broken[:10])
            summary += (f"\n\n⚠️ **{len(broken)}개 채널에서 동작하지 않습니다:**\n{names}"
                        + ("\n…" if len(broken) > 10 else "")
                        + "\n-# 봇에게 `채널 보기` · `메시지 보내기` · `메시지 관리` 권한을 주세요.\n"
                        "-# 비공개 채널은 채널 권한 설정에 봇 역할을 추가해야 합니다.")
        embed.add_field(name="📡 감시 대상 요약", value=summary[:1024], inline=False)

    role_id = verify_settings.get("role_id")
    role = guild.get_role(int(role_id)) if role_id else None
    embed.add_field(
        name="🎭 연동된 인증 역할",
        value=role.mention if role else "⚠️ 미설정 — `/인증설정`에서 먼저 역할을 지정해 주세요",
        inline=False
    )
    embed.add_field(
        name="🔗 버튼 문구",
        value=f"`{settings.get('button_label', '🎮 개인서버 입장하기')}`",
        inline=True
    )
    embed.add_field(
        name="🔒 잠금 문구",
        value=f"`{settings.get('locked_text', '🔒 인증된 유저만 입장할 수 있습니다')}`",
        inline=True
    )
    embed.set_footer(text="웹훅이 보낸 임베드만 감지합니다")
    return embed


# ── Cog ───────────────────────────────────────────────────────────────

class LinkGuard(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        # 같은 메시지를 두 번 처리하지 않도록 최근 처리분을 기억합니다
        self._handled: set[int] = set()

    def _mark_handled(self, message_id: int) -> bool:
        """이미 처리한 메시지면 False를 반환합니다."""
        if message_id in self._handled:
            return False
        self._handled.add(message_id)
        if len(self._handled) > 500:
            for old in list(self._handled)[:250]:
                self._handled.discard(old)
        return True

    async def _guard_message(self, message: discord.Message) -> None:
        guild = message.guild
        if guild is None:
            return

        # 1. 웹훅이 보낸 메시지만 대상으로 합니다 (봇 자신 및 일반 유저는 제외)
        if message.webhook_id is None:
            return
        if self.bot.user and message.author.id == self.bot.user.id:
            return

        # 2. 보호 대상 채널인지 확인
        settings = await get_linkguard_settings(guild.id)
        if not settings.get("enabled", True):
            return
        if not is_guarded_channel(message.channel, settings):
            return

        # 3. 링크가 들어올 수 있는 세 곳을 모두 훑습니다.
        #    ① 임베드 본문  ② 메시지 본문  ③ 링크 버튼 (예: MultiScope의 [Join Server])
        locked_text = settings.get("locked_text", "🔒 인증된 유저만 입장할 수 있습니다")

        sanitized_embeds: list[discord.Embed] = []
        all_links: list[str] = []
        for embed in message.embeds:
            clean, links = sanitize_embed(embed, locked_text)
            sanitized_embeds.append(clean)
            all_links.extend(links)

        # 본문(content)에 있는 링크
        clean_content, content_links = strip_links(message.content or "", locked_text)
        all_links.extend(content_links)

        # 링크 버튼(컴포넌트)에 담긴 링크 — 원본 버튼은 재게시하지 않으므로 함께 사라집니다
        button_links = extract_component_links(message)
        all_links.extend(button_links)

        all_links = list(dict.fromkeys(all_links))
        if not all_links:
            # 보호할 링크가 없으면 원본을 그대로 둡니다
            return

        me = guild.me
        perms = message.channel.permissions_for(me) if me else None
        if perms is None or not perms.manage_messages or not perms.send_messages:
            print(
                f"[LinkGuard] #{message.channel} 채널에 '메시지 관리' 또는 '메시지 보내기' 권한이 없어 "
                "링크 보호를 건너뜁니다.",
                flush=True
            )
            return

        if not self._mark_handled(message.id):
            return

        # 4. 원본을 먼저 삭제해 노출 시간을 최소화합니다
        try:
            await message.delete()
        except discord.NotFound:
            return
        except discord.Forbidden:
            print(f"[LinkGuard] #{message.channel} 원본 메시지 삭제에 실패했습니다 (권한 부족).", flush=True)
            return
        except Exception as e:
            print(f"[LinkGuard] 원본 메시지 삭제 중 오류: {e}", flush=True)
            return

        # 5. 원본 작성자(웹훅) 정보를 임베드에 남겨 누가 보냈는지 알 수 있게 합니다
        if sanitized_embeds:
            head = sanitized_embeds[0]
            if not head.author or not head.author.name:
                head.set_author(
                    name=message.author.display_name,
                    icon_url=message.author.display_avatar.url if message.author.display_avatar else None
                )

        view = ProtectedLinkView(button_label=settings.get("button_label", "🎮 개인서버 입장하기"))

        try:
            sent = await message.channel.send(
                content=clean_content or None,
                embeds=sanitized_embeds,
                view=view
            )
        except Exception as e:
            print(f"[LinkGuard] 보호 메시지 재전송 중 오류: {e}", flush=True)
            return

        # 6. 재게시된 메시지 ID를 키로 원본 링크를 저장 (버튼이 이 키로 조회합니다)
        await save_protected_link(guild.id, sent.id, all_links)

        # 7. 유출 감시 색인에 등록 (이 링크가 서버 어딘가에 다시 뜨면 잡아냅니다)
        await register_guarded_keys(guild.id, [k for k in (link_key(u) for u in all_links) if k])

        # 8. 새 링크가 보호되기 시작했음을 로그 채널에 알립니다
        log_embed = discord.Embed(
            title="🛡️ 새 링크 보호 시작",
            description=f"{message.channel.mention} 채널의 웹훅 임베드에서 링크 **{len(all_links)}개**를 보호했습니다.",
            color=discord.Color.green()
        )
        log_embed.add_field(name="🔗 링크", value=describe_roblox_link(all_links[0]), inline=False)
        log_embed.timestamp = discord.utils.utcnow()
        await send_guard_log(guild, settings, log_embed)

    async def _detect_leak(self, message: discord.Message) -> None:
        """서버 안에서 보호 링크가 다시 뿌려지는지 감시합니다."""
        guild = message.guild
        if guild is None:
            return
        # 원본 웹훅과 봇 자신의 재게시는 유출이 아닙니다
        if message.webhook_id is not None:
            return
        if self.bot.user and message.author.id == self.bot.user.id:
            return
        if message.author.bot:
            return

        content = message.content or ""
        if "http" not in content and "#hbh-" not in content:
            return

        settings = await get_linkguard_settings(guild.id)
        if not settings.get("enabled", True):
            return

        guarded = await get_guarded_keys(guild.id)
        found_urls = [clean_url(u) for u in URL_RE.findall(content)]

        leaked = [u for u in found_urls if link_key(u) in guarded]
        # URL 없이 워터마크 코드만 붙여넣은 경우도 잡습니다
        mark_only = not leaked and WATERMARK_RE.search(content)
        if not leaked and not mark_only:
            return

        leaked_url = leaked[0] if leaked else content
        source = ""
        mark = WATERMARK_RE.search(leaked_url) or WATERMARK_RE.search(content)
        if mark:
            info = await get_watermark(guild.id, mark.group(1).lower())
            if info:
                source = f"<@{info['user_id']}> (**{info.get('user_name', '?')}**, `{info['user_id']}`)"

        deleted = False
        if settings.get("auto_delete_leak", True):
            try:
                await message.delete()
                deleted = True
            except Exception:
                pass

        detail = f"#{message.channel} 채널에 게시"
        if source:
            detail += " / 원 수령자 추적됨"
        await add_click_log(
            guild.id, message.author.id, message.author.display_name,
            leaked_url, event_type="leak", detail=detail
        )

        embed = discord.Embed(
            title="🚨 링크 유출 감지",
            description=f"{message.author.mention} 님이 보호된 링크를 **{message.channel.mention}** 채널에 게시했습니다.",
            color=discord.Color.red()
        )
        embed.add_field(
            name="🙋 게시한 유저",
            value=f"**{message.author.display_name}**\n`{message.author.id}`",
            inline=True
        )
        embed.add_field(
            name="🔖 워터마크 추적",
            value=source if source else "표식 없음 (직접 입력했거나 표식이 제거됨)",
            inline=True
        )
        embed.add_field(name="🔗 링크", value=describe_roblox_link(leaked_url), inline=False)
        embed.add_field(
            name="🧹 조치",
            value="✅ 메시지를 자동 삭제했습니다" if deleted
                  else "⚠️ 메시지를 삭제하지 못했습니다 (권한 확인 필요)",
            inline=False
        )
        embed.timestamp = discord.utils.utcnow()
        await send_guard_log(guild, settings, embed)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        try:
            await self._guard_message(message)
        except Exception as e:
            print(f"[LinkGuard] on_message 처리 오류: {e}", flush=True)
        try:
            await self._detect_leak(message)
        except Exception as e:
            print(f"[LinkGuard] 유출 감지 오류: {e}", flush=True)

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        # 임베드가 전송 직후가 아니라 나중에 붙는 경우를 대비합니다
        if before.embeds or not after.embeds:
            return
        try:
            await self._guard_message(after)
        except Exception as e:
            print(f"[LinkGuard] on_message_edit 처리 오류: {e}", flush=True)

    @app_commands.command(name="링크보호설정", description="[관리자 전용] 웹훅 임베드의 링크를 인증 유저 전용으로 보호합니다.")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def linkguard_settings(self, interaction: discord.Interaction):
        settings = await get_linkguard_settings(interaction.guild.id)
        category_count = len(settings.get("category_ids", []))
        channel_count = len(settings.get("channel_ids", []))
        total = len(collect_guarded_channels(interaction.guild, settings))
        state_text = "✅ 활성화" if settings.get("enabled", True) else "❌ 비활성화"

        embed = discord.Embed(
            title="🔗 웹훅 링크 보호 시스템 관리",
            description=f"**시스템 상태**: {state_text}\n"
                        f"**감시 카테고리**: **{category_count}개** · "
                        f"**개별 채널**: **{channel_count}개**\n"
                        f"**실제 감시 중인 채널**: **{total}개**\n\n"
                        "감시 대상 채널에 **웹훅**이 임베드를 올리면, 링크를 제거한 뒤 버튼으로 치환합니다.\n"
                        "버튼을 누른 유저가 인증 역할을 가진 경우에만 실제 링크가 전달됩니다.\n\n"
                        "아래 드롭다운 메뉴에서 원하시는 관리 기능을 선택해 주세요.",
            color=discord.Color.blurple()
        )
        embed.set_footer(text="웹훅이 보낸 임베드만 감지합니다")
        view = GuardSettingsView(settings)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


async def setup(bot):
    await bot.add_cog(LinkGuard(bot))
    # 봇 재시작 후에도 링크 버튼이 동작하도록 영구 View 등록
    bot.add_view(ProtectedLinkView())
