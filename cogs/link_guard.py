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
import time
import asyncio
import discord
from discord.ext import commands
from discord import app_commands
from utils import link_tokens, oauth_gate
from utils.verify_db import (
    get_linkguard_settings, save_linkguard_settings,
    save_protected_link, get_protected_link,
    get_verify_settings, peek_verify_settings, add_click_log, get_click_logs,
    make_watermark_code, save_watermark, get_watermark,
    register_guarded_keys, get_guarded_keys,
    record_link_usage, get_link_usage,
    save_log_reference, get_log_reference, get_log_reference_by_protected,
    get_roblox_names,
)

# 봇 재시작 후에도 버튼이 살아있도록 고정 custom_id 사용
# (링크 원본은 재게시된 메시지 ID를 키로 DB에 저장합니다)
PLINK_BUTTON_ID = "hbh_plink:open"
# 로그 임베드 밑의 [링크 사용 유저] 버튼
USAGE_BUTTON_ID = "hbh_usage:open"

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


def sort_links_by_priority(links: list[str]) -> list[str]:
    """개인서버 링크를 맨 앞으로 보냅니다 (버튼과 로그의 대표 링크가 되도록)."""
    def rank(u: str) -> int:
        if is_roblox_private_server(u):
            return 0
        if is_roblox_link(u):
            return 1
        return 2
    return sorted(links, key=rank)


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


def strip_links(text: str, locked_text: str, only_roblox: bool = True) -> tuple[str, list[str]]:
    """텍스트에서 링크를 뽑아내고, 그 자리를 잠금 문구로 치환합니다.

    only_roblox=True 면 **로블록스 링크만** 보호 대상으로 삼습니다.
    매크로 임베드에는 제작자 홍보 사이트 같은 링크가 함께 들어있는 경우가 많은데,
    그런 링크까지 감추면 정작 버튼이 엉뚱한 주소로 연결됩니다.
    """
    if not text:
        return text, []

    found: list[str] = []

    def _md_sub(match: re.Match) -> str:
        url = clean_url(match.group(2))
        if only_roblox and not is_roblox_link(url):
            return match.group(0)          # 보호 대상이 아니면 원문 그대로 둡니다
        found.append(url)
        return f"{match.group(1)} `{locked_text}`"

    result = MD_LINK_RE.sub(_md_sub, text)

    def _url_sub(match: re.Match) -> str:
        url = clean_url(match.group(0))
        if only_roblox and not is_roblox_link(url):
            return match.group(0)
        found.append(url)
        return f"`{locked_text}`"

    result = URL_RE.sub(_url_sub, result)
    return result, found


def sanitize_embed(embed: discord.Embed, locked_text: str,
                   only_roblox: bool = True) -> tuple[discord.Embed, list[str]]:
    """임베드에서 링크를 제거한 사본과, 추출된 링크 목록을 반환합니다.

    이미지/썸네일/아이콘 URL은 실제 열람 대상이 아니므로 건드리지 않습니다.
    only_roblox=True 면 로블록스 링크만 보호하고, 제작자 홍보 링크 등은 그대로 둡니다.
    """
    data = embed.to_dict()
    links: list[str] = []

    def _takes(url: str) -> bool:
        return bool(url) and (not only_roblox or is_roblox_link(url))

    # 제목의 하이퍼링크 제거 (title 클릭이 불가능해집니다)
    title_url = clean_url(data.get("url") or "")
    if _takes(title_url):
        links.append(title_url)
        data.pop("url", None)

    if data.get("title"):
        data["title"], found = strip_links(data["title"], locked_text, only_roblox)
        links.extend(found)

    if data.get("description"):
        data["description"], found = strip_links(data["description"], locked_text, only_roblox)
        links.extend(found)

    for field in data.get("fields", []):
        if field.get("value"):
            field["value"], found = strip_links(field["value"], locked_text, only_roblox)
            links.extend(found)
        if field.get("name"):
            field["name"], found = strip_links(field["name"], locked_text, only_roblox)
            links.extend(found)

    author = data.get("author")
    if isinstance(author, dict):
        author_url = clean_url(author.get("url") or "")
        if _takes(author_url):
            links.append(author_url)
            author.pop("url", None)

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

        # 1-2) 로블록스 전용 모드인데 저장된 링크에 로블록스 링크가 하나도 없다면,
        #      수정 이전에 잘못 보호된 메시지입니다 (매크로 홍보 링크 등).
        #      유저를 엉뚱한 주소로 보내지 않고 여기서 막습니다.
        if settings.get("roblox_only", True) and not any(is_roblox_link(u) for u in links):
            await interaction.response.send_message(
                "⚠️ **이 알림은 더 이상 유효하지 않습니다.**\n"
                "개인서버 링크가 아닌 안내 메시지였습니다. 최신 알림을 이용해 주세요.",
                ephemeral=True
            )
            return

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

        # 중계가 켜져 있고 공개 주소가 잡히면, 로블록스 링크 대신 1회성 토큰
        # 주소를 전달합니다. 실제 링크가 디스코드에 남지 않고, 퍼뜨려도 곧
        # 만료되어 쓸 수 없습니다. 주소를 못 구하면 조용히 직접 링크로 돌아갑니다.
        relay_base = link_tokens.public_base_url() if settings.get("relay_links") else None
        relayed = False
        # 이 유저에게 실제로 준 대표 주소 — 사용 기록에 남겨 감사에 씁니다
        issued_url = ""

        for idx, link in enumerate(links[:5], start=1):
            marked = apply_watermark(link, code) if code else link

            target = marked
            if relay_base:
                try:
                    token = link_tokens.issue(marked, member.id, guild.id)
                    target = f"{relay_base}/j/{token}"
                    relayed = True
                except Exception as e:
                    # 중계 실패가 입장 자체를 막으면 안 됩니다
                    print(f"[LinkGuard] 중계 토큰 발급 실패, 직접 링크로 대체: {e}", flush=True)
                    target = marked

            if len(target) > MAX_URL_LENGTH:
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
                link_view.add_item(discord.ui.Button(label=label, url=target, style=discord.ButtonStyle.link))
                if not issued_url:
                    issued_url = target
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

        if relayed:
            content += (f"\n-# 이 버튼은 회원님 전용이며 {link_tokens.TOKEN_TTL_SECONDS}초 뒤 만료됩니다. "
                        "남에게 넘겨도 열리지 않습니다.")

        if overflow:
            content += "\n\n⚠️ 버튼으로 만들 수 없는 링크:\n" + "\n".join(f"• {u}" for u in overflow[:3])

        view_arg = link_view if link_view.children else discord.utils.MISSING
        await interaction.response.send_message(content=content, view=view_arg, ephemeral=True)

        # 기록은 응답을 보낸 뒤에 백그라운드로 남깁니다 (클릭 속도에 영향 없음)
        if gated:
            asyncio.create_task(
                _after_click(guild, member, links, code, settings,
                             message=interaction.message, issued_url=issued_url)
            )


async def _after_click(guild, member, links: list[str], code: str, settings: dict,
                       message=None, issued_url: str = "") -> None:
    """클릭 응답을 보낸 뒤 실행되는 뒷정리 — 기록 저장과 로그 임베드 집계.

    수백 명이 동시에 누르는 상황을 고려해, 클릭마다 로그 메시지를 보내지 않습니다.
    대신 **보호 메시지 하나당 로그 임베드 하나**를 두고 사용 인원만 갱신합니다.
    """
    url = links[0] if links else ""
    try:
        if code:
            await save_watermark(guild.id, code, member.id, member.display_name)

        await add_click_log(
            guild.id, member.id, member.display_name, url,
            event_type="view",
            detail=f"워터마크 `{code}`" if code else "워터마크 없음"
        )

        if message is not None:
            total, _ = await record_link_usage(guild.id, message.id,
                                               member.id, member.display_name,
                                               issued_url=issued_url,
                                               watermark=code)
            await ensure_usage_log(guild, settings, message, links, total)
    except Exception as e:
        print(f"[LinkGuard] 클릭 후처리 오류: {e}", flush=True)


# 로그 임베드 갱신 빈도 제한 — 보호 메시지별 마지막 갱신 시각(단조 시계)
_last_log_refresh: dict[int, float] = {}
# 디바운스 창이 닫힌 뒤 반영할 '마지막 값'
_pending_total: dict[int, int] = {}
# 창이 닫히기를 기다리는 트레일링 갱신 태스크 (메시지당 1개)
_trailing_tasks: dict[int, asyncio.Task] = {}
# 동시 클릭이 로그 임베드를 중복 생성하지 않도록 하는 메시지별 잠금
_log_create_locks: dict[int, asyncio.Lock] = {}
LOG_REFRESH_INTERVAL = 10.0


def _cap(d: dict, limit: int) -> None:
    """메모리 누수 방지 — 한도를 넘으면 오래된 항목부터 버립니다.

    파이썬 dict는 삽입 순서를 유지하므로 앞쪽이 가장 오래된 항목입니다.
    """
    if len(d) > limit:
        for key in list(d)[:len(d) - limit // 2]:
            d.pop(key, None)


async def _resolve_log_ref(guild_id: int, message_id: int) -> int | None:
    """protected message id -> 로그 메시지 id 를 영속 저장소에서 복원합니다.

    `_LOG_MESSAGE_BY_PROTECTED`는 프로세스 메모리 캐시라 봇 재시작 시 비워지므로,
    캐시 미스일 때만 이 함수로 DB/로컬 JSON을 조회해 다시 채웁니다.

    반환값: 로그 메시지 ID(양수) / 0 = 저장소에 기록 자체가 없음(확정, 캐시 가능) /
    None = 조회가 실패함(일시적 오류일 수 있어 캐시하지 않고 다음 호출에 재시도).
    """
    try:
        found = await get_log_reference_by_protected(guild_id, message_id)
    except Exception as e:
        print(f"[LinkGuard] 로그 참조 복원 실패: {e}", flush=True)
        return None

    if not found:
        return 0
    try:
        return int(found["log_message_id"])
    except (KeyError, TypeError, ValueError):
        return 0


async def ensure_usage_log(guild, settings: dict, message,
                           links: list[str], total: int) -> None:
    """첫 클릭이면 로그 임베드를 만들고, 이미 있으면 사용 인원만 갱신합니다.

    임베드가 떠도 실제로 누르는 사람은 극소수(측정상 2%)이므로, 보호할 때마다
    로그를 올리면 로그 채널이 '0명 사용' 알림으로 뒤덮입니다. 그래서 **실제로
    처음 눌린 순간**에만 로그 임베드를 만듭니다.
    """
    if not settings.get("log_channel_id"):
        return

    mid = message.id
    ref = _LOG_MESSAGE_BY_PROTECTED.get(mid)

    if ref is None:
        # 프로세스 메모리에 없음 = 첫 클릭이거나 봇 재시작 직후.
        # 수십 명이 동시에 누르면 로그 임베드가 그 수만큼 생길 수 있으므로
        # 메시지별 잠금으로 한 번만 만들어지도록 보장합니다.
        lock = _log_create_locks.setdefault(mid, asyncio.Lock())
        _cap(_log_create_locks, 200)
        async with lock:
            ref = _LOG_MESSAGE_BY_PROTECTED.get(mid)
            if ref is None:
                resolved = await _resolve_log_ref(guild.id, mid)
                if resolved is None:
                    return          # 조회 실패 — 다음 클릭에 재시도
                if resolved:
                    ref = resolved
                    _LOG_MESSAGE_BY_PROTECTED[mid] = ref
                    _cap(_LOG_MESSAGE_BY_PROTECTED, 1000)
                else:
                    # 저장소에도 없음 = 아직 아무도 안 누른 링크 → 지금 만듭니다
                    await post_usage_log(guild, settings, message, links, total)
                    return

    if not ref:
        return
    await refresh_usage_log(guild, settings, mid, total, links[0] if links else "")


async def refresh_usage_log(guild, settings: dict, message_id: int,
                            total: int, url: str) -> None:
    """로그 임베드의 '사용 인원'을 갱신합니다 (10초 디바운스).

    몰릴 때 API를 과하게 호출하지 않도록 10초에 한 번만 실제 수정하되,
    **마지막 클릭이 반영되지 않고 끝나는 것을 막기 위해** 창이 닫힌 뒤
    최신 값으로 한 번 더 갱신합니다(트레일링).

    스로틀 검사를 저장소 조회보다 **먼저** 합니다. 순서가 반대면 동시에 누른
    수십 명이 전부 조회를 한 뒤에야 걸러져 조회가 폭증합니다.
    """
    # 동시 클릭은 완료 순서가 뒤섞이므로 '마지막에 도착한 값'이 가장 큰 값이라는
    # 보장이 없습니다. 사용 인원은 단조 증가하므로 최댓값을 유지합니다.
    # (이렇게 하지 않으면 20명이 눌렀는데 임베드에 13명으로 남는 일이 생깁니다)
    previous = _pending_total.get(message_id)
    _pending_total[message_id] = total if previous is None else max(previous, total)
    _cap(_pending_total, 500)

    now = time.monotonic()
    last = _last_log_refresh.get(message_id, 0.0)
    wait = LOG_REFRESH_INTERVAL - (now - last)
    if wait > 0:
        _schedule_trailing(guild, settings, message_id, url, wait)
        return

    _last_log_refresh[message_id] = now
    _cap(_last_log_refresh, 500)
    await _apply_usage_refresh(guild, settings, message_id,
                               _pending_total.pop(message_id, total), url)


def _schedule_trailing(guild, settings: dict, message_id: int,
                       url: str, delay: float) -> None:
    """디바운스 창이 닫힌 뒤 '마지막 값'으로 한 번 더 갱신하도록 예약합니다."""
    if message_id in _trailing_tasks:
        return

    async def _runner():
        try:
            await asyncio.sleep(delay)
            total = _pending_total.pop(message_id, None)
            if total is None:
                return
            _last_log_refresh[message_id] = time.monotonic()
            await _apply_usage_refresh(guild, settings, message_id, total, url)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[LinkGuard] 로그 임베드 지연 갱신 실패: {e}", flush=True)
        finally:
            _trailing_tasks.pop(message_id, None)

    _trailing_tasks[message_id] = asyncio.create_task(_runner())


async def _apply_usage_refresh(guild, settings: dict, message_id: int,
                               total: int, url: str) -> None:
    """실제로 로그 임베드의 '사용 인원' 필드를 수정합니다."""
    ref = _LOG_MESSAGE_BY_PROTECTED.get(message_id)
    if ref is None:
        resolved = await _resolve_log_ref(guild.id, message_id)
        if resolved is None:
            return  # 조회 실패 — 캐시하지 않고 다음 클릭에 재시도
        ref = resolved
        _LOG_MESSAGE_BY_PROTECTED[message_id] = ref
        _cap(_LOG_MESSAGE_BY_PROTECTED, 1000)

    if not ref:
        return

    channel_id = settings.get("log_channel_id")
    if not channel_id:
        return
    channel = guild.get_channel(int(channel_id))
    if channel is None:
        return

    try:
        log_msg = await channel.fetch_message(ref)
        if not log_msg.embeds:
            return
        embed = log_msg.embeds[0]
        data = embed.to_dict()
        for field in data.get("fields", []):
            if field.get("name", "").startswith("👥"):
                field["name"] = "👥 사용 인원"
                field["value"] = f"**{total}명**이 이 링크를 사용했습니다."
                break
        await log_msg.edit(embed=discord.Embed.from_dict(data), view=UsageStatsView())
    except discord.NotFound:
        # 로그 메시지가 삭제된 경우: 0으로 확정 캐시해 재시작 전까지 재조회를 막습니다.
        _LOG_MESSAGE_BY_PROTECTED[message_id] = 0
    except Exception as e:
        print(f"[LinkGuard] 로그 임베드 갱신 실패: {e}", flush=True)


# 보호 메시지 ID -> 로그 채널에 올린 메시지 ID (봇이 살아있는 동안의 빠른 조회용)
# 0 = "저장소 조회 결과 기록 없음"으로 확정된 상태(재조회 방지용 음성 캐시)
_LOG_MESSAGE_BY_PROTECTED: dict[int, int] = {}


class ClickLogPageView(discord.ui.View):
    """열람 / 유출 기록 — 한 페이지 10건씩 넘겨보는 뷰.

    예전에는 최근 20건을 한 필드에 몰아넣고 1024자에서 통째로 잘라내,
    '총 20건'이라 적어놓고 실제로는 그보다 적게 보였습니다.
    """

    PER_PAGE = 10
    MAX_PAGES = 10          # 최대 100건까지 넘겨봅니다

    def __init__(self, logs: list[dict], is_leak: bool):
        super().__init__(timeout=300)
        self.logs = logs[:self.PER_PAGE * self.MAX_PAGES]
        self.is_leak = is_leak
        self.page = 0
        self.pages = max(1, (len(self.logs) + self.PER_PAGE - 1) // self.PER_PAGE)
        self._sync_buttons()

    def _sync_buttons(self) -> None:
        self.prev_btn.disabled = self.page <= 0
        self.next_btn.disabled = self.page >= self.pages - 1

    def build_embed(self) -> discord.Embed:
        total = len(self.logs)
        embed = discord.Embed(
            title="🚨 유출 감지 기록" if self.is_leak else "🔍 링크 열람 기록",
            description=("서버 안에서 보호 링크를 **다시 게시한** 유저 기록입니다."
                         if self.is_leak else
                         "개인서버 링크를 **열람한** 유저 기록입니다. "
                         "유출 시 용의자를 좁히는 데 씁니다."),
            color=discord.Color.red() if self.is_leak else discord.Color.dark_teal()
        )
        if not total:
            embed.add_field(
                name="\u2139\ufe0f 안내",
                value=("아직 감지된 유출이 없습니다." if self.is_leak
                       else "아직 기록된 열람 이력이 없습니다."),
                inline=False
            )
            return embed

        begin = self.page * self.PER_PAGE
        chunk = self.logs[begin:begin + self.PER_PAGE]

        lines = []
        for offset, log in enumerate(chunk, start=begin + 1):
            who = log.get("user_name", "알 수 없음")
            uid = log.get("user_id", "")
            ts = log.get("timestamp", "")
            summary = describe_roblox_link(log.get("url", ""))
            extra = log.get("detail", "")
            lines.append(
                f"`{offset:2}.` **{who}** <@{uid}> (`{uid}`)\n"
                f"     └ `{ts}`" + (f" · {extra}" if extra else "") + "\n"
                f"     └ {summary}"
            )

        # 임베드 필드 1024자 상한 — 넘칠 때마다 필드를 새로 엽니다
        fields = [[]]
        used = 0
        for line in lines:
            if used + len(line) + 1 > 1000 and fields[-1]:
                fields.append([])
                used = 0
            fields[-1].append(line)
            used += len(line) + 1

        for idx, group in enumerate(fields):
            if not group:
                continue
            embed.add_field(
                name=(f"📋 {begin + 1}~{begin + len(chunk)}번 (총 {total}건)"
                      if idx == 0 else "\u200b"),
                value="\n".join(group),
                inline=False
            )
        embed.set_footer(text=f"{self.page + 1} / {self.pages} 페이지 · "
                              "최신순 · 링크 코드는 일부만 표시됩니다")
        return embed

    async def _turn(self, interaction: discord.Interaction, delta: int) -> None:
        self.page = max(0, min(self.pages - 1, self.page + delta))
        self._sync_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="이전", style=discord.ButtonStyle.secondary, emoji="◀")
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._turn(interaction, -1)

    @discord.ui.button(label="다음", style=discord.ButtonStyle.secondary, emoji="▶")
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._turn(interaction, +1)


class UsagePageView(discord.ui.View):
    """링크 사용 유저 명단 — 한 페이지 10명씩 넘겨보는 뷰.

    관리자에게만 보이는 임시(에페메랄) 응답이라 영구 View 가 아니며,
    5분 뒤 자동으로 만료됩니다.
    """

    PER_PAGE = 10

    def __init__(self, rows: list[dict], roblox: dict | None = None):
        super().__init__(timeout=300)
        self.rows = rows
        # 디스코드 ID -> 로블록스 이름. 게임 내 플레이어 목록과 대조하는 데 씁니다.
        self.roblox = roblox or {}
        self.page = 0
        self.pages = max(1, (len(rows) + self.PER_PAGE - 1) // self.PER_PAGE)
        self._sync_buttons()

    def _sync_buttons(self) -> None:
        self.prev_btn.disabled = self.page <= 0
        self.next_btn.disabled = self.page >= self.pages - 1

    def build_embed(self) -> discord.Embed:
        total = len(self.rows)
        embed = discord.Embed(
            title="👤 이 링크를 사용한 유저",
            description=(f"총 **{total}명**이 사용했습니다."
                         if total else "아직 이 링크를 사용한 유저가 없습니다."),
            color=discord.Color.blurple()
        )
        if not total:
            return embed

        begin = self.page * self.PER_PAGE
        chunk = self.rows[begin:begin + self.PER_PAGE]

        lines: list[str] = []
        for offset, r in enumerate(chunk, start=begin + 1):
            repeat = f" ×{r['count']}" if r.get("count", 1) > 1 else ""
            # 발급 주소는 백틱으로 감싸 **클릭되지 않게** 둡니다.
            # 링크가 1회용이라, 관리자가 눌러버리면 정작 그 유저가 못 들어갑니다.
            link = r.get("link") or ""
            link_line = f"`{link}`" if link else "`(기록 없음)`"
            # 워터마크는 유출된 링크에서 사람을 특정하는 열쇠라, 명단에서
            # 바로 대조할 수 있게 시각 옆에 함께 보여줍니다.
            wm = r.get("watermark") or ""
            wm_part = f" · 표식 `{wm}`" if wm else " · 표식 없음"
            # 로블록스 아이디가 있으면 함께 보여줍니다. 게임 안 플레이어 목록과
            # 대조해 '명단에 없는데 들어와 있는 사람'을 찾는 것이 목적입니다.
            rbx = self.roblox.get(str(r["user_id"])) or ""
            rbx_part = f" → 🎮 `{rbx}`" if rbx else " → 🎮 `연동 안 함`"
            lines.append(
                f"`{offset:2}.` <@{r['user_id']}> (`{r['user_id']}`){repeat}{rbx_part}\n"
                f"     └ `{r.get('last', '')}`{wm_part}\n"
                f"     └ {link_line}"
            )

        # 임베드 필드는 1024자 상한이라, 10명이 한 필드에 안 들어갑니다.
        # 넘칠 때마다 필드를 새로 열어 잘림 없이 담습니다.
        fields: list[list[str]] = [[]]
        used = 0
        for line in lines:
            if used + len(line) + 1 > 1000 and fields[-1]:
                fields.append([])
                used = 0
            fields[-1].append(line)
            used += len(line) + 1

        for idx, group in enumerate(fields):
            if not group:
                continue
            embed.add_field(
                name=(f"📋 사용자 목록 ({begin + 1}~{begin + len(chunk)}번)"
                      if idx == 0 else "\u200b"),
                value="\n".join(group),
                inline=False
            )
        # 게임 내 플레이어 목록과 눈으로 대조하기 좋게 한 줄로 모읍니다.
        # 여기 없는 이름이 게임 안에 있으면 유출로 들어온 사람입니다.
        picked = [self.roblox.get(str(r["user_id"])) for r in chunk]
        picked = [x for x in picked if x]
        if picked:
            embed.add_field(
                name="🎮 로블록스 아이디 (게임 내 목록과 대조)",
                value="```" + ", ".join(picked)[:990] + "```",
                inline=False)

        embed.set_footer(text=f"{self.page + 1} / {self.pages} 페이지 · 최근 사용순")
        return embed

    async def _turn(self, interaction: discord.Interaction, delta: int) -> None:
        self.page = max(0, min(self.pages - 1, self.page + delta))
        self._sync_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="이전", style=discord.ButtonStyle.secondary, emoji="◀")
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._turn(interaction, -1)

    @discord.ui.button(label="다음", style=discord.ButtonStyle.secondary, emoji="▶")
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._turn(interaction, +1)


class UsageStatsView(discord.ui.View):
    """로그 임베드 밑에 붙는 [👥 링크 사용 유저] 버튼 (영구 View)."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="👥 링크 사용 유저", style=discord.ButtonStyle.secondary,
                       custom_id=USAGE_BUTTON_ID)
    async def usage_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(
                "⚠️ 이 버튼은 관리자만 사용할 수 있습니다.", ephemeral=True
            )
            return

        guild = interaction.guild
        ref = await get_log_reference(guild.id, interaction.message.id)
        if not ref:
            await interaction.response.send_message(
                "⚠️ 이 로그와 연결된 보호 메시지를 찾을 수 없습니다.", ephemeral=True
            )
            return

        protected_id = int(ref["protected_message_id"])
        rows = await get_link_usage(guild.id, protected_id)
        # 연동한 멤버의 로블록스 이름을 한 번에 가져옵니다 (유저 수만큼 조회하지 않음)
        roblox = await get_roblox_names(guild.id, [r["user_id"] for r in rows])

        view = UsagePageView(rows, roblox)
        # 한 페이지에 다 들어가면 넘길 버튼을 보여줄 이유가 없습니다
        await interaction.response.send_message(
            embed=view.build_embed(),
            view=view if view.pages > 1 else discord.utils.MISSING,
            ephemeral=True
        )


async def post_usage_log(guild: discord.Guild, settings: dict,
                         posted: discord.Message, links: list[str],
                         total: int = 0) -> None:
    """이 링크가 **처음 사용된 시점**에 로그 채널로 집계 임베드를 올립니다.

    보호할 때가 아니라 첫 클릭 때 만드는 이유: 임베드는 10분마다 새로 뜨지만
    실제로 눌리는 것은 극소수라, 보호 시점에 올리면 로그 채널이 '0명 사용'
    알림으로 도배되고 디스코드 API도 그만큼 낭비됩니다.
    """
    channel_id = settings.get("log_channel_id")
    if not channel_id:
        return
    log_channel = guild.get_channel(int(channel_id))
    if log_channel is None:
        return
    if not links:
        return

    title = ""
    if posted.embeds and posted.embeds[0].title:
        title = posted.embeds[0].title

    embed = discord.Embed(
        title="🛡️ 링크 사용 시작",
        description=(f"{posted.channel.mention} 채널의 보호된 임베드에서 "
                     f"링크 **{len(links)}개** 중 첫 사용이 감지되었습니다."),
        color=discord.Color.green()
    )
    if title:
        embed.add_field(name="📌 임베드", value=title[:200], inline=False)
    embed.add_field(name="🔗 링크", value=describe_roblox_link(links[0]), inline=False)
    embed.add_field(name="👥 사용 인원",
                    value=f"**{total}명**이 이 링크를 사용했습니다.", inline=False)
    embed.add_field(
        name="🔍 보호된 메시지",
        value=f"[바로가기]({posted.jump_url})",
        inline=False
    )
    embed.set_footer(text="아래 버튼으로 이 링크를 사용한 유저를 확인할 수 있습니다")
    embed.timestamp = discord.utils.utcnow()

    try:
        log_msg = await log_channel.send(embed=embed, view=UsageStatsView())
    except Exception as e:
        print(f"[LinkGuard] 로그 임베드 전송 실패: {e}", flush=True)
        return

    _LOG_MESSAGE_BY_PROTECTED[posted.id] = log_msg.id
    _cap(_LOG_MESSAGE_BY_PROTECTED, 1000)
    _last_log_refresh[posted.id] = time.monotonic()
    _cap(_last_log_refresh, 500)
    await save_log_reference(guild.id, log_msg.id, posted.id, posted.channel.id)


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


class WatermarkLookupModal(discord.ui.Modal, title="🔎 유출 링크 추적"):
    target_input = discord.ui.TextInput(
        label="유출된 링크 또는 워터마크 코드를 붙여넣으세요",
        style=discord.TextStyle.paragraph,
        placeholder="https://www.roblox.com/share?code=...#hbh-d41ab726\n또는 d41ab726",
        required=True,
        max_length=1000
    )

    async def on_submit(self, interaction: discord.Interaction):
        guild = interaction.guild
        raw = self.target_input.value.strip()

        # 링크 전체를 붙여넣었으면 워터마크 부분만 뽑고, 코드만 넣었으면 그대로 사용
        match = WATERMARK_RE.search(raw)
        code = match.group(1).lower() if match else None
        if not code:
            bare = re.fullmatch(r'[0-9a-fA-F]{8}', raw)
            code = bare.group(0).lower() if bare else None

        if not code:
            embed = discord.Embed(
                title="⚠️ 워터마크를 찾을 수 없습니다",
                description="붙여넣은 값에 `#hbh-xxxxxxxx` 형태의 표식이 없습니다.\n\n"
                            "**표식이 없는 경우는 두 가지입니다.**\n"
                            "• 유출자가 `#` 뒤를 지우고 퍼뜨렸다\n"
                            "• 워터마크 기능이 꺼져 있을 때 발급된 링크다\n\n"
                            "이 경우 **링크 열람 기록**에서 해당 시간대에 링크를 받아간\n"
                            "유저 목록을 확인해 용의자를 좁히실 수 있습니다.",
                color=discord.Color.orange()
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        info = await get_watermark(guild.id, code)
        if not info:
            embed = discord.Embed(
                title="⚠️ 발급 기록이 없는 워터마크입니다",
                description=f"코드 `{code}` 로 발급된 기록을 찾지 못했습니다.\n"
                            "다른 서버에서 발급되었거나, 기록이 남기 전의 링크일 수 있습니다.",
                color=discord.Color.orange()
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        user_id = info["user_id"]
        member = guild.get_member(int(user_id))

        embed = discord.Embed(
            title="🎯 유출자 추적 결과",
            description=f"이 링크는 아래 유저에게 발급된 것입니다.",
            color=discord.Color.red()
        )
        embed.add_field(
            name="👤 최초 수령자",
            value=f"<@{user_id}>\n**{info.get('user_name', '?')}**\n`{user_id}`",
            inline=True
        )
        embed.add_field(name="🔖 워터마크", value=f"`{code}`", inline=True)
        embed.add_field(
            name="📌 서버 재적 여부",
            value="✅ 서버에 있음" if member else "❌ 이미 나감",
            inline=True
        )

        logs = await get_click_logs(guild.id, limit=100)
        mine = [x for x in logs if x.get("user_id") == str(user_id)]
        if mine:
            lines = [f"• `{x.get('timestamp','')}` {describe_roblox_link(x.get('url',''))}"
                     for x in mine[:5]]
            embed.add_field(name="🕓 이 유저의 최근 열람 기록", value="\n".join(lines)[:1024], inline=False)

        embed.add_field(
            name="⚠️ 판단 시 참고",
            value="워터마크는 **링크를 받아간 사람**을 가리킵니다.\n"
                  "이 유저가 직접 퍼뜨렸을 수도 있고, 이 유저의 화면·기기에서\n"
                  "제3자가 가져갔을 수도 있습니다. 조치 전에 확인해 주세요.",
            inline=False
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


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
        await interaction.response.send_message(
            embed=build_log_channel_embed(interaction.guild, channel), ephemeral=True
        )


def build_log_channel_embed(guild: discord.Guild, channel) -> discord.Embed:
    """로그 채널 설정 완료 임베드 (권한 경고 포함)."""
    me = guild.me
    perms = channel.permissions_for(me) if me else None
    can_send = perms.send_messages if perms else False
    can_embed = perms.embed_links if perms else False
    can_view = perms.view_channel if perms else False

    embed = discord.Embed(
        title="✅ 로그 채널 설정 완료",
        description=f"앞으로 다음 기록이 {channel.mention} 채널로 전송됩니다.\n\n"
                    "• 🛡️ **새 링크 보호 시작** — 링크마다 임베드 1개\n"
                    "  └ 👥 사용 인원이 이 임베드에 누적 표시됩니다\n"
                    "  └ **[👥 링크 사용 유저]** 버튼으로 사용자 명단 확인\n"
                    "• 🚨 **링크 유출 감지** — 게시자 · 원 수령자 · 조치 결과\n\n"
                    "-# 클릭할 때마다 메시지를 보내지 않으므로 채널이 도배되지 않습니다.",
        color=discord.Color.green() if (can_send and can_embed and can_view)
        else discord.Color.orange()
    )

    missing = []
    if not can_view:
        missing.append("채널 보기")
    if not can_send:
        missing.append("메시지 보내기")
    if not can_embed:
        missing.append("링크 첨부")
    if missing:
        embed.add_field(
            name="⚠️ 권한 부족",
            value=f"봇에게 {channel.mention} 채널의 `{' · '.join(missing)}` 권한이 없습니다.\n"
                  "이대로면 로그가 전송되지 않습니다. 채널 권한을 확인해 주세요.",
            inline=False
        )
    else:
        embed.add_field(name="🔑 권한 점검", value="✅ 정상적으로 전송할 수 있습니다.", inline=False)
    return embed


class LogChannelIdModal(discord.ui.Modal, title="🆔 로그 채널 ID로 설정"):
    channel_input = discord.ui.TextInput(
        label="로그 채널의 ID를 입력하세요",
        placeholder="예: 1329788098187624500  (채널 우클릭 → ID 복사)",
        required=True,
        max_length=50
    )

    async def on_submit(self, interaction: discord.Interaction):
        guild = interaction.guild
        match = re.search(r'\d{17,20}', self.channel_input.value)
        if not match:
            await interaction.response.send_message(
                "⚠️ 올바른 채널 ID를 찾지 못했습니다.\n"
                "-# 디스코드 설정 → 고급 → 개발자 모드를 켠 뒤, 채널 우클릭 → `ID 복사`",
                ephemeral=True
            )
            return

        channel = guild.get_channel(int(match.group(0)))
        if channel is None:
            await interaction.response.send_message(
                "⚠️ 이 서버에서 해당 ID의 채널을 찾을 수 없습니다.\n"
                "-# 봇이 볼 수 없는 비공개 채널이면 채널 권한에 봇 역할을 추가해 주세요.",
                ephemeral=True
            )
            return

        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message(
                "⚠️ 텍스트 채널만 로그 채널로 지정할 수 있습니다.",
                ephemeral=True
            )
            return

        await save_linkguard_settings(guild.id, {"log_channel_id": channel.id})
        await interaction.response.send_message(
            embed=build_log_channel_embed(guild, channel), ephemeral=True
        )


class LogChannelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)
        self.add_item(LogChannelSelect())

    @discord.ui.button(label="채널 ID로 입력", style=discord.ButtonStyle.secondary, emoji="🆔", row=1)
    async def by_id_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(LogChannelIdModal())


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
                label="유출 링크 추적 (워터마크 조회)",
                value="trace",
                description="외부에서 발견한 유출 링크로 최초 수령자를 찾습니다.",
                emoji="🎯"
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
                label="로블록스 링크만 보호 (권장)",
                value="robloxonly",
                description="제작자 홍보 링크 등 로블록스가 아닌 링크는 건드리지 않습니다.",
                emoji="🎮"
            ),
            discord.SelectOption(
                label="링크 중계 (유출 링크 무효화)",
                value="relay",
                description="로블록스 링크를 노출하지 않고 만료되는 주소로 전달합니다.",
                emoji="🛰️"
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

        elif choice == "robloxonly":
            new_state = not settings.get("roblox_only", True)
            await save_linkguard_settings(guild.id, {"roblox_only": new_state})
            embed = discord.Embed(
                title="🎮 보호 대상 범위 변경",
                description=f"로블록스 링크 전용 모드가 **{'✅ 켜짐' if new_state else '❌ 꺼짐'}** 상태가 되었습니다.\n\n"
                            + ("**켜짐**: 로블록스 링크만 보호합니다.\n"
                               "매크로 제작자의 홍보 사이트 링크 등은 그대로 두므로,\n"
                               "버튼이 엉뚱한 주소로 연결되지 않습니다.\n"
                               "로블록스 링크가 하나도 없는 메시지는 아예 건드리지 않습니다."
                               if new_state else
                               "**꺼짐**: 메시지 안의 **모든 링크**를 보호합니다.\n"
                               "⚠️ 매크로 홍보 링크까지 버튼에 섞여 들어갈 수 있습니다."),
                color=discord.Color.green() if new_state else discord.Color.orange()
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)

        elif choice == "relay":
            new_state = not settings.get("relay_links", False)
            base = link_tokens.public_base_url()
            await save_linkguard_settings(guild.id, {"relay_links": new_state})

            embed = discord.Embed(
                title="🛰️ 링크 중계 설정 변경",
                description=f"링크 중계가 **{'✅ 켜짐' if new_state else '❌ 꺼짐'}** 상태가 되었습니다.",
                color=discord.Color.green() if new_state else discord.Color.orange()
            )
            if new_state:
                embed.add_field(
                    name="🔎 이렇게 동작합니다",
                    value=f"• 로블록스 링크 대신 **{link_tokens.TOKEN_TTL_SECONDS}초 뒤 만료되는 주소**를 전달합니다\n"
                          "• 실제 링크가 디스코드에 남지 않습니다\n"
                          "• 그 주소를 남에게 넘겨도 만료·횟수 제한으로 열리지 않습니다",
                    inline=False
                )
                embed.add_field(
                    name="⚠️ 알아두실 점",
                    value="• 봇의 웹 서버를 한 번 거치므로 **약 0.3~0.5초** 느려집니다\n"
                          "• 웹 서버가 죽으면 **아무도 입장할 수 없습니다**. 문제가 생기면 "
                          "이 메뉴에서 즉시 끄세요\n"
                          "• 링크를 받은 본인이 주소창에서 복사하는 것까지는 막지 못합니다 "
                          "(워터마크로 추적)",
                    inline=False
                )
            else:
                embed.add_field(
                    name="🔎 이렇게 동작합니다",
                    value="로블록스 링크를 **그대로** 버튼에 담아 전달합니다.\n"
                          "가장 빠르지만, 유출된 링크는 계속 유효합니다.",
                    inline=False
                )

            if new_state and not base:
                embed.add_field(
                    name="🚨 공개 주소를 찾지 못했습니다",
                    value="`RENDER_EXTERNAL_URL` 또는 `PUBLIC_BASE_URL` 환경 변수가 필요합니다.\n"
                          "주소를 찾지 못하면 **자동으로 기존 방식(직접 링크)으로 동작**하므로 "
                          "입장이 막히지는 않습니다.",
                    inline=False
                )
            elif new_state:
                embed.add_field(name="🌐 중계 주소", value=f"`{base}/j/...`", inline=False)

            await interaction.response.send_message(embed=embed, ephemeral=True)

        elif choice == "trace":
            await interaction.response.send_modal(WatermarkLookupModal())

        elif choice == "text":
            await interaction.response.send_modal(GuardTextModal(settings))

        elif choice == "logchannel":
            current_id = settings.get("log_channel_id")
            current = guild.get_channel(int(current_id)) if current_id else None
            await interaction.response.send_message(
                f"📋 **열람 · 유출 기록을 보낼 채널을 선택하세요.**\n"
                f"-# 현재 설정: {current.mention if current else '미설정'}\n"
                "-# 관리자만 볼 수 있는 채널로 지정하시길 권장합니다.\n"
                "-# 목록에 안 보이는 비공개 채널은 아래 **[🆔 채널 ID로 입력]** 을 쓰세요.",
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
            logs = await get_click_logs(
                guild.id,
                limit=ClickLogPageView.PER_PAGE * ClickLogPageView.MAX_PAGES,
                event_type="leak" if is_leak else "view"
            )
            view = ClickLogPageView(logs, is_leak)
            await interaction.response.send_message(
                embed=view.build_embed(),
                view=view if view.pages > 1 else discord.utils.MISSING,
                ephemeral=True
            )

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
        only_roblox = settings.get("roblox_only", True)

        sanitized_embeds: list[discord.Embed] = []
        all_links: list[str] = []
        for embed in message.embeds:
            clean, links = sanitize_embed(embed, locked_text, only_roblox)
            sanitized_embeds.append(clean)
            all_links.extend(links)

        # 본문(content)에 있는 링크
        clean_content, content_links = strip_links(message.content or "", locked_text, only_roblox)
        all_links.extend(content_links)

        # 링크 버튼(컴포넌트)에 담긴 링크 — 원본 버튼은 재게시하지 않으므로 함께 사라집니다
        button_links = extract_component_links(message)
        if only_roblox:
            button_links = [u for u in button_links if is_roblox_link(u)]
        all_links.extend(button_links)

        all_links = sort_links_by_priority(list(dict.fromkeys(all_links)))
        if not all_links:
            # 보호할 링크가 없으면 원본을 그대로 둡니다.
            # (로블록스 전용 모드에서는 제작자 홍보 링크만 있는 메시지가 여기에 해당합니다)
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

        # 8. 로그 임베드는 여기서 만들지 않습니다.
        #    임베드는 10분마다 새로 뜨지만 실제로 눌리는 것은 극소수라, 보호할 때마다
        #    올리면 로그 채널이 '0명 사용' 알림으로 도배됩니다. 대신 **첫 클릭 시점**에
        #    ensure_usage_log() 가 만들고, 이후로는 그 임베드에 인원을 누적합니다.

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

        # URL 형태가 아니어도 잡습니다.
        #  · 개인서버 코드만 복사해 전달한 경우 (예: "코드 c26ae66d… 이거 써")
        #  · 워터마크 코드만 남은 경우
        bare_code = None
        if not leaked:
            lowered = content.lower()
            for key in guarded:
                code = key.split(":", 1)[-1]
                if len(code) >= 8 and code in lowered:
                    bare_code = code
                    break

        mark_only = not leaked and not bare_code and WATERMARK_RE.search(content)
        if not leaked and not bare_code and not mark_only:
            return

        if leaked:
            leaked_url, how = leaked[0], "링크 게시"
        elif bare_code:
            leaked_url, how = f"(코드 직접 게시) {bare_code}", "개인서버 코드만 게시"
        else:
            leaked_url, how = content[:200], "워터마크 코드 게시"

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

        detail = f"#{message.channel} 채널에 {how}"
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
    # DM 에서는 interaction.guild 가 None 이라 AttributeError 로 죽습니다.
    # (실제로 /출석 이 그렇게 터졌습니다). guild_only 를 걸면 디스코드가
    # **DM 목록에 아예 띄우지 않으므로** 방어 코드를 흩뿌릴 필요가 없습니다.
    @app_commands.guild_only()
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


def _make_member_gate(bot):
    """중계 게이트가 쓸 판정기 — 서버 멤버이고 인증 역할이 있는지.

    Flask 스레드에서 호출되므로 **동기이고 가벼운** 조회만 합니다.
    (봇이 이미 메모리에 들고 있는 캐시를 읽을 뿐, API 호출이 없습니다)

    판정할 수 없는 상황(길드 캐시 미로딩 등)에서는 **통과**시킵니다.
    확인이 안 된다고 정당한 유저를 막으면 자리 경쟁에서 손해가 큽니다.
    """
    def gate(guild_id: int, user_id: int) -> tuple[bool, str]:
        guild = bot.get_guild(int(guild_id))
        if guild is None:
            return True, "guild-unknown"

        member = guild.get_member(int(user_id))
        if member is None:
            return False, "not-member"      # 서버를 나갔습니다

        settings = peek_verify_settings(guild_id)
        if not settings:
            return True, "settings-unknown"

        role_id = settings.get("role_id")
        if role_id and not any(r.id == int(role_id) for r in member.roles):
            return False, "no-role"         # 인증 역할이 회수됨
        return True, "ok"

    return gate


async def setup(bot):
    await bot.add_cog(LinkGuard(bot))
    # 봇 재시작 후에도 링크 버튼이 동작하도록 영구 View 등록
    bot.add_view(ProtectedLinkView())
    bot.add_view(UsageStatsView())
    # 중계 게이트(Flask 스레드)가 멤버·역할을 판정할 수 있도록 연결합니다.
    # 환경 변수가 없으면 게이트 자체가 꺼져 있어 이 판정기도 호출되지 않습니다.
    oauth_gate.set_member_gate(_make_member_gate(bot))
    if oauth_gate.enabled():
        print("[LinkGuard] 중계 신원 게이트: 활성화 (디스코드 OAuth)", flush=True)
    else:
        print("[LinkGuard] 중계 신원 게이트: 비활성 "
              "(DISCORD_CLIENT_ID / DISCORD_CLIENT_SECRET 미설정)", flush=True)
