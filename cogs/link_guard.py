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
from datetime import datetime, timezone, timedelta
import discord
from discord.ext import commands, tasks
from discord import app_commands
from utils import link_tokens, oauth_gate, health
from utils.verify_db import (
    get_linkguard_settings, save_linkguard_settings,
    save_protected_link, get_protected_link,
    get_verify_settings, peek_verify_settings, add_click_log, get_click_logs,
    make_watermark_code, save_watermark, get_watermark,
    register_guarded_keys, get_guarded_keys,
    record_link_usage, get_link_usage, find_issued_to,
    record_biome_event, get_biome_buckets,
    record_biome_unparsed, get_biome_unparsed,
    save_log_reference, get_log_reference, get_log_reference_by_protected,
    get_roblox_names,
)

# 봇 재시작 후에도 버튼이 살아있도록 고정 custom_id 사용
# (링크 원본은 재게시된 메시지 ID를 키로 DB에 저장합니다)
PLINK_BUTTON_ID = "hbh_plink:open"
# 로그 임베드 밑의 [링크 사용 유저] 버튼
USAGE_BUTTON_ID = "hbh_usage:open"
# 그 옆의 [게임 안 인원과 대조] — 이 로그가 가리키는 링크 하나로 범위가
# 좁혀집니다. ID 를 복사해 옮겨 적게 만들면 아무도 안 씁니다.
COMPARE_BUTTON_ID = "hbh_usage:compare"

MD_LINK_RE = re.compile(r'\[([^\]]+)\]\(\s*(https?://[^\s)]+)\s*\)')
URL_RE = re.compile(r'https?://[^\s<>"\'`\]\)]+', re.IGNORECASE)

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

    # **보호 제외가 가장 먼저입니다.** 카테고리로 통째로 감시하면서 그 안의
    # 채널 하나만 빼고 싶을 때 쓰므로, 나중에 보면 카테고리에 먹힙니다.
    if is_no_guard_channel(channel, settings):
        return False

    if base.id in settings.get("channel_ids", []):
        return True

    category_id = getattr(base, "category_id", None)
    return category_id is not None and category_id in settings.get("category_ids", [])


def is_no_guard_channel(channel, settings: dict) -> bool:
    """보호를 **일부러 끈** 채널인지.

    관리자가 원본 임베드를 그대로 보려고 지정하는 채널입니다. 링크가 원문
    그대로 남으므로 **관리자만 볼 수 있어야** 하고, 설정할 때 UI 가 그걸
    검사합니다. 채널과 카테고리를 모두 받습니다.
    """
    base = resolve_base_channel(channel)
    if base is None:
        return False
    ids = settings.get("no_guard_channel_ids") or []
    if base.id in ids:
        return True
    category_id = getattr(base, "category_id", None)
    return category_id is not None and category_id in ids


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


# 중계 주소(/j/<토큰>)는 **한 사람에게만 발급된 개인 주소**입니다.
# 정상 경로로는 디스코드에 다시 나타날 이유가 없어서, 그대로 게시되면
# 유출로 봅니다. 워터마크와 달리 로블록스를 거치지 않으므로 표식이
# 사라지지 않고, 누구 것인지가 발급 기록으로 정확히 나옵니다.
#
# (로블록스가 공유링크를 펼치면서 프래그먼트를 버리기 때문에, 펼쳐진
#  개인서버 주소에는 워터마크가 남지 않습니다. 그쪽은 추적할 수 없습니다.)
RELAY_PATH_RE = re.compile(r'^/j/([A-Za-z0-9_\-]{16,})')

# 한 메시지에서 소유자를 찾아볼 주소 개수. 여러 개를 뿌리는 쪽이 더
# 위험하지만, 수백 개를 붙여넣어 조회를 유발하게 두면 안 됩니다.
MAX_RELAY_TOKENS = 5


def relay_tokens_in(text: str) -> list[str]:
    """본문에서 **이 봇의** 중계 주소만 골라 토큰을 뽑습니다.

    공개 주소가 없으면(중계를 쓰지 않는 상태) 아무것도 잡지 않습니다.

    **호스트는 정확히 같아야 합니다.** 앞부분만 비교하면
    `krbiomehunt.duckdns.org.evil.com` 이 걸리고, 뒷부분만 비교하면
    `evil.krbiomehunt.duckdns.org` 가 걸립니다.

    포트(`:443`)는 떼고 비교합니다. 붙여 두면 주소는 그대로 열리는데
    감지만 피해 가므로, 한 글자로 우회가 됩니다.
    """
    base = link_tokens.public_base_url()
    if not base:
        return []

    base_after = base.split("://", 1)[-1].rstrip("/")
    cut = base_after.find("/")
    base_host = (base_after[:cut] if cut >= 0 else base_after).split(":", 1)[0].lower()
    base_path = base_after[cut:] if cut >= 0 else ""
    if not base_host:
        return []

    found: list[str] = []
    for raw in URL_RE.findall(text or ""):
        after = clean_url(raw).split("://", 1)[-1]
        cut = after.find("/")
        host = (after[:cut] if cut >= 0 else after).split(":", 1)[0].lower()
        rest = after[cut:] if cut >= 0 else ""
        if host != base_host or not rest.startswith(base_path):
            continue
        m = RELAY_PATH_RE.match(rest[len(base_path):])
        if m and m.group(1) not in found:
            found.append(m.group(1))
    return found


# 같은 메시지를 수정할 때마다 다시 알리지 않도록 최근 처리분을 기억합니다.
# (자동 삭제가 꺼져 있으면 메시지가 남아 몇 번이든 고쳐질 수 있습니다)
LEAK_MEMO_SECONDS = 600
_leak_reported: dict[int, float] = {}


def _already_reported(message_id: int) -> bool:
    """이미 알린 메시지인지 확인하고, 아니면 알린 것으로 표시합니다."""
    now = time.monotonic()
    for mid, at in list(_leak_reported.items()):
        if now - at > LEAK_MEMO_SECONDS:
            _leak_reported.pop(mid, None)
    if message_id in _leak_reported:
        return True
    _leak_reported[message_id] = now
    return False


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


# ── 알림 멘션 ─────────────────────────────────────────────────────────
#
# 매크로 채널의 원본(MultiScope)은 **멘션이 아예 없습니다** — 임베드와
# 링크 버튼뿐입니다. 원본에 없으면 살릴 것도 없으므로, 사람들이 바이옴이
# 뜬 것을 알게 하려면 재게시할 때 봇이 직접 붙여야 합니다.
#
# 세 갈래가 합쳐집니다.
#   ① 원본 **본문**의 멘션      → clean_content 에 그대로 남습니다 (기존 동작)
#   ② 원본 **임베드 안**의 멘션 → 본문으로 끌어올려야 실제로 울립니다
#   ③ 설정한 규칙               → 채널·바이옴별로 봇이 붙입니다

ROLE_MENTION_RE = re.compile(r'<@&(\d+)>')
EVERYONE_RE = re.compile(r'@everyone(?![A-Za-z0-9_])', re.IGNORECASE)
HERE_RE = re.compile(r'@here(?![A-Za-z0-9_])', re.IGNORECASE)


def embed_texts(embed, *, meta: bool = True) -> list[str]:
    """임베드에서 사람이 읽는 텍스트를 모읍니다 (이미지·URL 은 제외).

    `meta=False` 면 **작성자(author)와 바닥글(footer)을 뺍니다.**
    거기에는 매크로 **이름과 버전**이 들어갑니다 — `크로마틱`,
    `frluxky's Macro`, `MultiScope v2.0.5`, `EggSol v1.9.9-2`,
    `Coteab Macro v2.2.0`, `maxstellar's Biome Macro | v2.3`.
    바이옴 판정에 이걸 섞으면 **매크로 이름에 바이옴 이름이 들어간 순간
    그 매크로의 모든 메시지가 희귀 바이옴으로 잡힙니다.** 실제 매크로
    6종을 확인해 보니 바이옴 이름은 **전부 제목**에 있었고, 작성자·바닥글에
    있던 적은 없었습니다.
    """
    try:
        data = embed.to_dict()
    except Exception:
        return []

    out: list[str] = []
    for key in ("title", "description"):
        value = data.get(key)
        if value:
            out.append(str(value))

    if meta:
        for key in ("author", "footer"):
            part = data.get(key)
            if isinstance(part, dict):
                for sub in ("name", "text"):
                    if part.get(sub):
                        out.append(str(part[sub]))

    for field in data.get("fields") or []:
        if not isinstance(field, dict):
            continue
        for sub in ("name", "value"):
            if field.get(sub):
                out.append(str(field[sub]))

    return out


def extract_embed_mentions(message) -> tuple[list[int], bool, bool]:
    """임베드 **안**에 적혀 있는 멘션을 찾아냅니다.

    임베드 안의 `<@&역할>` 은 역할 이름으로 예쁘게 보이지만 **알림이 가지
    않습니다.** 디스코드는 본문(content)에 있는 멘션만 울립니다. 그래서
    임베드에 적힌 것을 본문으로 끌어올려야 실제로 사람에게 닿습니다.
    """
    role_ids: list[int] = []
    everyone = here = False

    for embed in getattr(message, "embeds", None) or []:
        for text in embed_texts(embed):
            for raw in ROLE_MENTION_RE.findall(text):
                try:
                    rid = int(raw)
                except (TypeError, ValueError):
                    continue
                if rid not in role_ids:
                    role_ids.append(rid)
            if EVERYONE_RE.search(text):
                everyone = True
            if HERE_RE.search(text):
                here = True

    return role_ids, everyone, here


# 매크로가 쓰는 제목은 **두 갈래**입니다. 이름이 뒤에 오는 형식만 보던
# 동안, 앞에 오는 형식으로 뜬 바이옴은 **한 건도 세어지지 않았습니다.**
#
#   ① `Biome Started - DREAMSPACE` · `BIOME STARTED :: HELL Found`
#   ② `🌀 WINDY Biome Started` · `🩸 BLOOD RAIN Biome Started`
#
# ② 은 MultiScope v0.9.8-Stable 이 쓰는 형식입니다. 2026-09-15 실측(감시
# 채널 244곳 / 메시지 23,482건)에서 `BLOOD RAIN` 은 이 형식으로만 올라와
# **통계에 아예 존재하지 않았습니다.** 매크로는 판올림하면서 제목 배치를
# 바꿉니다 — **하나에서 확인하고 "된다"고 하지 마세요.**
BIOME_TITLE_RE = re.compile(
    r"biome\s*(started|ended)\s*[-\u2013\u2014:]+\s*(.+?)\s*(?:found)?\s*$",
    re.IGNORECASE | re.MULTILINE)
BIOME_TITLE_TAIL_RE = re.compile(
    r"^(.+?)\s*biome\s*(started|ended)\s*$",
    re.IGNORECASE | re.MULTILINE)

# '바이옴' 이라고 적혀 있는가 — **계측 대상을 가르는 기준**입니다.
# 매크로 시작·정지나 NPC 등장까지 '못 읽음' 으로 세면 경고가 늘 켜져
# 있게 됩니다. 실측으로 보호본의 25%가 그런 것이었습니다.
BIOME_WORD_RE = re.compile(r"biome", re.IGNORECASE)

# 이름 앞뒤의 이모지·기호를 떼어냅니다 (`🌀 WINDY` → `WINDY`).
# 떼지 않으면 같은 바이옴이 이모지 유무로 **두 칸으로 갈라져** 집계됩니다.
BIOME_TRIM_RE = re.compile(r"^[^0-9A-Za-z가-힣]+|[^0-9A-Za-z가-힣]+$")


def _biome_from_text(raw: str):
    """한 덩이의 글에서 (종류, 이름) 을 뽑습니다. 못 찾으면 None."""
    m = BIOME_TITLE_RE.search(raw)
    if m:
        return m.group(1), m.group(2)
    m = BIOME_TITLE_TAIL_RE.search(raw)
    if m:
        return m.group(2), m.group(1)
    return None


def detect_biome_name(message) -> str | None:
    """임베드에서 바이옴 이름을 뽑습니다. **시작 알림만** 셉니다.

    보는 곳은 **제목과 본문뿐**입니다. 작성자·바닥글을 섞으면 매크로
    이름이 바이옴으로 잡힙니다(알림 판정에서 이미 겪은 문제입니다).
    형식이 맞지 않으면 **`None` 을 돌려주고 아무것도 기록하지 않습니다** —
    이름을 모르는 채로 '?' 를 쌓으면 통계가 쓰레기가 됩니다.
    (대신 못 읽은 것은 `unparsed_biome_title()` 로 **드러냅니다.**)

    **제목만 보면 안 됩니다.** Coteab v2.2.0 과 maxstellar v2.3 은 제목에
    시각(`[12:10:40]`, `<t:…:F>`)을 넣고 본문에 `> ## Biome Started - SNOWY`
    를 씁니다. 제목만 보던 동안 이 두 매크로의 바이옴은 **한 건도 세어지지
    않았습니다.** 그래서 제목에서 못 찾으면 본문까지 내려갑니다.

    **바이옴 이름은 어디에도 박아 두지 않습니다.** 이 게임은 이벤트마다
    바이옴을 1~3종 넣었다 빼므로, 목록을 코드에 적으면 이벤트가 열릴
    때마다 사람이 손봐야 합니다. 형식만 알면 이름은 뭐든 읽습니다.
    """
    for embed in getattr(message, "embeds", None) or []:
        try:
            data = embed.to_dict() or {}
        except Exception:
            continue
        # 제목이 먼저입니다 — 본문에는 다른 문장이 섞일 수 있습니다.
        for key in ("title", "description"):
            hit = _biome_from_text(str(data.get(key) or ""))
            if not hit:
                continue
            kind, name = hit
            if kind.lower() != "started":
                return None        # 종료 알림은 '등장' 이 아닙니다
            name = BIOME_TRIM_RE.sub("", name.strip(" -:_").strip())
            return name[:60] or None
    return None


# 표본 제목은 **프로세스마다 몇 개만** 올려 보냅니다. 개수는 어차피 DB 에서
# 따로 세므로, 같은 제목을 매번 올려 쓰기를 늘릴 이유가 없습니다.
# (지우고 다시 채우지 않습니다 — 지우면 같은 제목이 계속 다시 올라가
#  배열이 끝없이 자랍니다.)
UNPARSED_SAMPLE_CAP = 12
_unparsed_seen: dict[int, set[str]] = {}


def _take_unparsed_sample(guild_id: int, title: str) -> bool:
    seen = _unparsed_seen.setdefault(guild_id, set())
    if title in seen or len(seen) >= UNPARSED_SAMPLE_CAP:
        return False
    seen.add(title)
    return True


def unparsed_biome_title(message) -> str | None:
    """**바이옴 알림인데** 이름을 못 읽은 것만 돌려줍니다 (계측용).

    매크로가 판올림하며 형식을 바꾸면 그 매크로의 바이옴이 통계에서 통째로
    빠지는데, 세어 두지 않으면 **비어 있다는 사실조차 아무도 모릅니다.**

    ⚠️ **'바이옴' 이라고 적힌 것만 셉니다.** 매크로 시작·정지(`MultiScope
    Started`)나 NPC 등장(`MARI HAS ARRIVED!`)까지 세면 이 경고가 늘 켜져
    있게 되고, **늘 켜진 경고는 없는 것보다 나쁩니다** — 익숙해져서 진짜
    신호를 놓칩니다. 실측으로 보호본의 25%가 그런 것이었습니다.
    """
    for embed in getattr(message, "embeds", None) or []:
        try:
            data = embed.to_dict() or {}
        except Exception:
            continue
        title = str(data.get("title") or "").strip()
        body = str(data.get("description") or "")
        if not BIOME_WORD_RE.search(title + " " + body):
            continue
        # ⚠️ 읽힌 것은 경고하지 않습니다. **종료 알림은 읽고 나서
        # 일부러 안 세는 것**이지 못 읽은 것이 아닙니다. 이걸 섮으면
        # 바이옴이 떠고 끝날 때마다 경고가 올라 늘 켜져 있게 됩니다.
        if any(_biome_from_text(str(data.get(k) or ""))
               for k in ("title", "description")):
            continue
        # 제목이 비었으면 본문 첫 줄을 보여 줍니다 — 어떤 형식인지
        # 알아볼 수 있어야 인식 범위를 넓힐 수 있습니다.
        head = title or next((x.strip() for x in body.splitlines() if x.strip()), "")
        return head[:80] or None
    return None


def biome_haystack(message) -> str:
    """바이옴 이름을 찾아볼 텍스트를 모읍니다 — **임베드 안만** 봅니다.

    ⚠️ **본문(content)은 절대 보지 않습니다.** 누군가 채팅에 "DREAMSPACE"
    라고 치는 것만으로 @everyone 이 울리면 그건 되돌릴 수 없는 사고입니다.
    매크로가 만든 임베드에만 바이옴 이름이 들어 있으므로 그것만 봅니다.

    (호출하는 쪽인 `_guard_message` 는 이미 **웹훅이 보낸 메시지**만 다루지만,
     그 검사 하나에 전원 호출을 걸어 두지 않습니다. 안전장치는 겹쳐야
     하나가 무너져도 버팁니다.)

    실제 매크로 6종(MultiScope · DroidScope · EggSol · Coteab ·
    maxstellar · 크로마틱)을 확인해 보니 전부 제목이 `Biome Started - <이름>`
    형식이었습니다. 그래도 제목만 보지는 않습니다 — 필드에 넣는 매크로가
    나중에 들어올 수 있습니다. 다만 **작성자·바닥글은 뺍니다**
    (매크로 이름과 버전이라 바이옴과 무관하고, 섞으면 오검출이 납니다).
    """
    parts: list[str] = []
    for embed in getattr(message, "embeds", None) or []:
        parts.extend(embed_texts(embed, meta=False))
    return " ".join(parts)


def _norm_biome(text: str) -> str:
    """대소문자·공백·기호를 지워 비교할 수 있는 형태로 만듭니다.

    매크로마다 `DREAMSPACE` `Dream Space` `dream_space` 로 제각각이라
    글자 그대로 비교하면 **설정해 둔 바이옴이 조용히 안 걸립니다.**
    """
    return re.sub(r'[^0-9A-Z가-힣]+', '', str(text or "").upper())


def biome_matches(biomes, haystack: str) -> str | None:
    """설정된 바이옴 중 이 메시지에 들어있는 첫 번째를 돌려줍니다.

    빈 목록이면 None 이 아니라 호출한 쪽에서 '전부 해당'으로 다룹니다.
    """
    norm = _norm_biome(haystack)
    if not norm:
        return None
    for name in biomes or []:
        key = _norm_biome(name)
        if key and key in norm:
            return name
    return None


def notify_excluded(settings: dict, haystack: str) -> str | None:
    """멘션을 붙이면 안 되는 임베드인지 봅니다 (바이옴 종료 등).

    종료 임베드에도 같은 바이옴 이름이 들어 있어서, 이 검사가 없으면
    **뜰 때와 끝날 때 두 번** 울립니다. 보호 자체는 그대로 하고 멘션만
    붙이지 않습니다.
    """
    return biome_matches(settings.get("notify_exclude") or [], haystack)


def rule_covers_channel(rule: dict, channel) -> bool:
    """이 규칙이 이 채널에 적용되는지 봅니다.

    채널·카테고리를 하나도 지정하지 않은 규칙은 **모든 감시 채널**에
    적용됩니다 (채널 하나뿐인 서버에서 매번 채널을 고르게 만들지 않으려는 것).
    """
    base = resolve_base_channel(channel)
    if base is None:
        return False

    channel_ids = rule.get("channel_ids") or []
    category_ids = rule.get("category_ids") or []
    if not channel_ids and not category_ids:
        return True

    if base.id in channel_ids:
        return True

    category_id = getattr(base, "category_id", None)
    return category_id is not None and category_id in category_ids


def match_notify_rule(settings: dict, channel, haystack: str) -> tuple[dict | None, str | None]:
    """이 채널·이 메시지에 적용할 알림 규칙을 찾습니다.

    **처음 맞는 규칙 하나만** 씁니다. 여러 규칙을 합치면 좁은 규칙(특정
    바이옴에만 @everyone)이 넓은 규칙에 먹혀 의도치 않게 전원 호출이
    나가는데, 그건 되돌릴 수 없는 사고입니다.
    """
    for rule in settings.get("notify_rules") or []:
        if not isinstance(rule, dict):
            continue

        # @everyone 은 **채널을 지정한 규칙에서만** 허용합니다.
        # 범위를 안 적은 @everyone 규칙 하나가 남아 있으면 감시 중인 모든
        # 채널에서 6,500명을 부르게 됩니다. UI 가 그런 규칙을 만들지 못하게
        # 막고 있지만, 손으로 고친 설정이 들어올 수 있으므로 여기서도 막습니다.
        # (안전장치는 겹쳐야 하나가 무너져도 버팁니다)
        if (rule.get("mention") == "everyone"
                and not (rule.get("channel_ids") or rule.get("category_ids"))):
            continue

        if not rule_covers_channel(rule, channel):
            continue

        biomes = rule.get("biomes") or []
        if not biomes:
            return rule, None

        # haystack 은 **임베드에서만** 모은 것입니다(biome_haystack 참고).
        # 채팅 본문으로는 절대 걸리지 않습니다.
        hit = biome_matches(biomes, haystack)
        if hit is not None:
            return rule, hit

    return None, None


# ⚠️ **같은 링크로 알림을 막지 마세요.**
#
# 예전에 "같은 링크를 다시 보호하면 30분간 멘션 생략" 을 넣었다가 걷어냈습니다.
# 개인서버 링크는 **그대로인 채 바이옴만 바뀝니다.** 그래서 같은 서버에서
# 00:10 DREAMSPACE 를 알린 뒤 00:25 CYBERSPACE 가 떠도 창에 걸려 묻혔습니다.
# 바이옴은 길어야 10분이라 30분 창은 **다음 희귀 바이옴을 통째로 삼킵니다.**
#
# 도배가 실제로 생기면 키를 **(링크 + 바이옴)** 으로 잡고 창을 바이옴 길이보다
# 짧게 두세요. 링크만으로는 무엇을 해도 틀립니다.


def _merge_role_targets(original_roles, extra_ids: list[int]) -> list:
    """원본이 멘션한 역할 + 봇이 붙일 역할을 하나의 허용 목록으로 만듭니다.

    봇이 붙이는 쪽은 역할 객체가 없을 수 있어 `discord.Object` 로 넘깁니다.
    `AllowedMentions.roles` 는 id 만 있으면 되므로 이걸로 충분합니다.
    """
    merged = list(original_roles or [])
    seen = {getattr(r, "id", None) for r in merged}
    for rid in extra_ids or []:
        if rid in seen:
            continue
        merged.append(discord.Object(id=rid))
        seen.add(rid)
    return merged


def describe_notify_rule(rule: dict, guild) -> str:
    """관리자 화면에 한 줄로 보여줄 규칙 설명."""
    kind = rule.get("mention", "roles")
    if kind == "everyone":
        target = "@everyone"
    elif kind == "here":
        target = "@here"
    else:
        names = []
        for rid in rule.get("role_ids") or []:
            role = guild.get_role(rid) if guild else None
            names.append(role.mention if role else f"⚠️ 삭제된 역할({rid})")
        target = " ".join(names) or "⚠️ 역할 없음"

    scope = []
    for cid in rule.get("channel_ids") or []:
        scope.append(f"<#{cid}>")
    for cat_id in rule.get("category_ids") or []:
        category = guild.get_channel(cat_id) if guild else None
        scope.append(f"📂 {category.name}" if category else f"📂 {cat_id}")
    where = " ".join(scope) or "감시 중인 모든 채널"

    biomes = rule.get("biomes") or []
    when = ", ".join(f"`{b}`" for b in biomes) if biomes else "모든 바이옴"

    return f"{where} · {when} → {target}"


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

    @discord.ui.button(label="🎮 게임 안 인원과 대조",
                       style=discord.ButtonStyle.primary,
                       custom_id=COMPARE_BUTTON_ID)
    async def compare_btn(self, interaction: discord.Interaction,
                          button: discord.ui.Button):
        """이 로그가 가리키는 **링크 하나**로 범위를 좁혀 대조합니다."""
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(
                "⚠️ 이 버튼은 관리자만 사용할 수 있습니다.", ephemeral=True)
            return
        ref = await get_log_reference(interaction.guild.id,
                                      interaction.message.id)
        if not ref:
            await interaction.response.send_message(
                "⚠️ 이 로그와 연결된 보호 메시지를 찾을 수 없습니다.",
                ephemeral=True)
            return
        # 같은 하위 시스템이지만 로드 순서에 기대지 않도록 여기서 가져옵니다
        from cogs.roblox_link import ServerCompareModal
        await interaction.response.send_modal(
            ServerCompareModal(int(ref["protected_message_id"]),
                               "대상: **이 로그가 가리키는 링크 하나**"))

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


def build_rare_copy_embed_settings(guild, settings: dict,
                                   note: str = "") -> discord.Embed:
    """현재 복사 채널과 **어떤 바이옴이 갈지**를 함께 보여줍니다."""
    cid = settings.get("rare_copy_channel_id")
    ch = guild.get_channel(int(cid)) if cid else None
    rare = rare_biome_names(settings)

    embed = discord.Embed(
        title="\U0001f3c3 희귀 바이옴 바로 들어가기 채널",
        description=(f"{note}\n\n" if note else "")
        + "희귀 바이옴이 뜨면 **원본 개인서버 주소를 그대로** 이 채널에\n"
          "복사합니다. 버튼을 한 번 더 누를 필요 없이 바로 입장합니다.\n\n"
        + (f"**현재 채널** {ch.mention}" if ch
           else (f"**\u26a0\ufe0f 채널(<#{cid}>)을 찾을 수 없습니다**" if cid
                 else "**아직 지정되지 않았습니다.**")),
        color=discord.Color.gold() if ch else discord.Color.orange())

    embed.add_field(
        name="\U0001f3f7\ufe0f 복사되는 바이옴",
        value=(", ".join(f"`{n}`" for n in rare) if rare
               else "\u26a0\ufe0f **알림 규칙이 없어 아무것도 복사되지 않습니다.**\n"
                    "-# `/링크보호설정` → 알림 규칙에서 희귀 바이옴을 먼저 정하세요."),
        inline=False)
    mins = int(settings.get("rare_copy_dedup_minutes", 0) or 0)
    embed.add_field(
        name="\U0001f4e7 중복 보내기",
        value=("**끔 — 올라오는 대로 전부 보냅니다.**\n"
               "-# 실측상 희귀 바이옴은 서버당 1번만 올라와 도배가 생기지 "
               "않습니다. 혹시 도배가 생기면 아래 버튼으로 켜세요."
               if mins <= 0 else
               f"같은 **(링크 + 바이옴)** 조합은 **{mins}분** 안에 한 번만 갑니다.\n"
               "-# 다른 서버에서 뜬 같은 바이옴은 따로 갑니다."),
        inline=False)

    if ch is not None:
        everyone = guild.default_role
        try:
            public = ch.permissions_for(everyone).read_messages
        except Exception:
            public = False
        if public:
            embed.add_field(
                name="\U0001f6a8 이 채널은 전체 공개입니다",
                value="**개인서버 주소가 모두에게 그대로 보입니다.**\n"
                      "채널 권한에서 `@everyone` 의 **채널 보기**를 꺼 주세요.",
                inline=False)
            embed.color = discord.Color.red()

    embed.set_footer(text="멘션은 울리지 않습니다 — 매크로 채널에서 이미 나갑니다.")
    return embed


class RareCopyChannelSelect(discord.ui.ChannelSelect):
    def __init__(self):
        super().__init__(
            placeholder="희귀 바이옴을 복사할 채널을 고르세요...",
            channel_types=[discord.ChannelType.text], max_values=1)

    async def callback(self, interaction: discord.Interaction):
        cid = self.values[0].id
        await save_linkguard_settings(interaction.guild.id,
                                      {"rare_copy_channel_id": cid})
        settings = await get_linkguard_settings(interaction.guild.id)
        await interaction.response.send_message(
            embed=build_rare_copy_embed_settings(
                interaction.guild, settings, f"\u2705 <#{cid}> 로 정했습니다."),
            ephemeral=True)


class RareCopyOffButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="복사 끄기", emoji="\u23f8\ufe0f",
                         style=discord.ButtonStyle.secondary)

    async def callback(self, interaction: discord.Interaction):
        await save_linkguard_settings(interaction.guild.id,
                                      {"rare_copy_channel_id": None})
        settings = await get_linkguard_settings(interaction.guild.id)
        await interaction.response.send_message(
            embed=build_rare_copy_embed_settings(
                interaction.guild, settings, "\u23f8\ufe0f 복사를 껐습니다."),
            ephemeral=True)


class RareCopyDedupButton(discord.ui.Button):
    """중복 보내기 창을 바꿉니다. **끄는 경로를 없애지 마세요** —
    매크로가 동작을 바꿔 도배가 생겼을 때 이게 유일한 탈출구입니다."""

    def __init__(self, minutes: int, active: bool = False):
        super().__init__(
            label="전부 보내기" if minutes <= 0 else f"{minutes}분에 한 번",
            style=(discord.ButtonStyle.primary if active
                   else discord.ButtonStyle.secondary),
            disabled=active, custom_id=f"rarecopy:dedup:{minutes}")
        self._minutes = minutes

    async def callback(self, interaction: discord.Interaction):
        await save_linkguard_settings(
            interaction.guild.id, {"rare_copy_dedup_minutes": self._minutes})
        settings = await get_linkguard_settings(interaction.guild.id)
        await interaction.response.send_message(
            embed=build_rare_copy_embed_settings(
                interaction.guild, settings,
                "\u2705 전부 보냅니다." if self._minutes <= 0
                else f"\u2705 {self._minutes}분에 한 번만 보냅니다."),
            ephemeral=True)


class RareCopyChannelView(discord.ui.View):
    def __init__(self, settings: dict | None = None):
        super().__init__(timeout=180)
        self.add_item(RareCopyChannelSelect())
        self.add_item(RareCopyOffButton())
        cur = int((settings or {}).get("rare_copy_dedup_minutes", 0) or 0)
        for m in RARE_COPY_WINDOW_CHOICES:
            self.add_item(RareCopyDedupButton(m, active=(m == cur)))


def public_no_guard_channels(guild, ids: list[int]) -> list:
    """보호 제외 채널 중 **`@everyone` 이 볼 수 있는 것**을 골라냅니다.

    이 기능에서 가장 위험한 실수가 '관리자 전용으로 만들었다고 생각했는데
    실제로는 전체 공개' 인 경우입니다. 보호를 끈 채널이 공개면 링크 보호
    전체가 무의미해지는데, 화면에는 아무 표시도 안 나서 며칠 뒤 사고로
    알게 됩니다. 그래서 **저장하는 순간** 검사합니다.
    """
    out = []
    everyone = guild.default_role
    for cid in ids:
        ch = guild.get_channel(cid)
        if ch is None:
            continue
        targets = ([c for c in ch.text_channels]
                   if isinstance(ch, discord.CategoryChannel) else [ch])
        for t in targets:
            try:
                if t.permissions_for(everyone).read_messages:
                    out.append(t)
            except Exception:
                continue
    return out


def no_guard_list_lines(guild, ids: list[int]) -> list[str]:
    lines = []
    for cid in ids:
        ch = guild.get_channel(cid)
        if ch is None:
            lines.append(f"• <#{cid}> \u26a0\ufe0f 채널을 찾을 수 없음")
        elif isinstance(ch, discord.CategoryChannel):
            lines.append(f"• \U0001f5c2\ufe0f **{ch.name}** (카테고리)")
        else:
            lines.append(f"• {ch.mention}")
    return lines


def build_no_guard_embed(guild, ids: list[int], note: str = "") -> discord.Embed:
    """현재 보호 제외 채널과 **공개 여부 경고**를 함께 보여줍니다."""
    public = public_no_guard_channels(guild, ids)
    embed = discord.Embed(
        title="\U0001f513 보호 제외 채널",
        description=(f"{note}\n\n" if note else "")
        + "여기 지정한 채널에서는 **원본 임베드가 그대로** 올라갑니다.\n"
          "링크 제거도, 버튼 치환도 하지 않습니다.\n\n"
        + ("**현재 목록**\n" + "\n".join(no_guard_list_lines(guild, ids))
           if ids else "지정된 채널이 없습니다."),
        color=discord.Color.red() if public else discord.Color.blurple())

    if public:
        embed.add_field(
            name="\U0001f6a8 이 채널은 전체 공개입니다",
            value="\n".join(f"• {c.mention}" for c in public[:10])
                  + "\n\n**개인서버 주소가 모두에게 그대로 보입니다.**\n"
                    "채널 권한에서 `@everyone` 의 **채널 보기**를 끄고 "
                    "관리자 역할에만 열어 주세요.",
            inline=False)
    elif ids:
        embed.add_field(
            name="\u2705 관리자만 볼 수 있는 상태",
            value="`@everyone` 이 볼 수 없는 것을 확인했습니다.",
            inline=False)

    embed.add_field(
        name="\U0001f50e 유출 감지는 계속 됩니다",
        value="보호는 건너뛰지만 링크는 **감지 색인에 그대로 넣습니다.** "
              "여기서 복사해 다른 채널에 뿌리면 잡힙니다.",
        inline=False)
    embed.set_footer(text="카테고리를 지정하면 그 안의 모든 채널이 제외됩니다.")
    return embed


class AddNoGuardChannelSelect(discord.ui.ChannelSelect):
    def __init__(self):
        super().__init__(
            placeholder="보호를 끌 채널이나 카테고리를 선택하세요...",
            channel_types=[discord.ChannelType.text,
                           discord.ChannelType.category],
            max_values=5)

    async def callback(self, interaction: discord.Interaction):
        settings = await get_linkguard_settings(interaction.guild.id)
        ids = list(settings.get("no_guard_channel_ids") or [])
        added = 0
        for selected in self.values:
            if selected.id not in ids:
                ids.append(selected.id)
                added += 1
        await save_linkguard_settings(interaction.guild.id,
                                      {"no_guard_channel_ids": ids})
        await interaction.response.send_message(
            embed=build_no_guard_embed(interaction.guild, ids,
                                       f"**{added}개**를 추가했습니다."),
            ephemeral=True)


class AddNoGuardChannelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)
        self.add_item(AddNoGuardChannelSelect())


class RemoveNoGuardChannelSelect(discord.ui.Select):
    def __init__(self, ids: list[int], guild: discord.Guild):
        options = []
        for cid in ids[:25]:
            ch = guild.get_channel(cid)
            name = (f"\U0001f5c2\ufe0f {ch.name}"
                    if isinstance(ch, discord.CategoryChannel)
                    else f"#{ch.name}" if ch else f"ID {cid}")
            options.append(discord.SelectOption(
                label=name[:100], value=str(cid),
                description="선택하면 이 채널도 다시 보호합니다", emoji="\U0001f512"))
        super().__init__(placeholder="다시 보호할 채널을 선택하세요...",
                         options=options)

    async def callback(self, interaction: discord.Interaction):
        cid = int(self.values[0])
        settings = await get_linkguard_settings(interaction.guild.id)
        ids = [c for c in (settings.get("no_guard_channel_ids") or [])
               if c != cid]
        await save_linkguard_settings(interaction.guild.id,
                                      {"no_guard_channel_ids": ids})
        await interaction.response.send_message(
            embed=build_no_guard_embed(interaction.guild, ids,
                                       f"<#{cid}> 를 **다시 보호합니다.**"),
            ephemeral=True)


class RemoveNoGuardChannelView(discord.ui.View):
    def __init__(self, ids: list[int], guild: discord.Guild):
        super().__init__(timeout=180)
        self.add_item(RemoveNoGuardChannelSelect(ids, guild))


class AddExceptionChannelSelect(discord.ui.ChannelSelect):
    def __init__(self):
        super().__init__(
            placeholder="유출 예외로 지정할 채널이나 카테고리를 선택하세요...",
            channel_types=[discord.ChannelType.text, discord.ChannelType.category],
            max_values=5
        )

    async def callback(self, interaction: discord.Interaction):
        settings = await get_linkguard_settings(interaction.guild.id)
        exc_ids = list(settings.get("exception_channel_ids", []))

        added = []
        for selected in self.values:
            if selected.id not in exc_ids:
                exc_ids.append(selected.id)
                added.append(selected.id)

        await save_linkguard_settings(interaction.guild.id, {"exception_channel_ids": exc_ids})

        lines = []
        for cid in exc_ids:
            channel = interaction.guild.get_channel(cid)
            if channel is None:
                lines.append(f"• <#{cid}> ⚠️ 채널을 찾을 수 없음")
            else:
                if isinstance(channel, discord.CategoryChannel):
                    lines.append(f"• ✅ 🗂️ **{channel.name}** (카테고리)")
                else:
                    lines.append(f"• ✅ {channel.mention}")

        embed = discord.Embed(
            title="✅ 유출 예외 채널 추가 완료",
            description=f"새로 추가된 예외 채널: **{len(added)}개**\n\n**현재 예외 채널 목록**\n" + ("\n".join(lines) or "없음"),
            color=discord.Color.green()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


class AddExceptionChannelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)
        self.add_item(AddExceptionChannelSelect())


class RemoveExceptionChannelSelect(discord.ui.Select):
    def __init__(self, exc_ids: list[int], guild: discord.Guild):
        options = []
        for cid in exc_ids[:25]:
            channel = guild.get_channel(cid)
            name = f"#{channel.name}" if channel else f"채널 ID {cid}"
            options.append(
                discord.SelectOption(
                    label=name[:100],
                    value=str(cid),
                    description="선택 시 이 채널이 유출 감지 예외에서 해제됩니다",
                    emoji="❌"
                )
            )
        super().__init__(placeholder="예외에서 해제할 채널을 선택하세요...", options=options)

    async def callback(self, interaction: discord.Interaction):
        cid = int(self.values[0])
        settings = await get_linkguard_settings(interaction.guild.id)
        exc_ids = [c for c in settings.get("exception_channel_ids", []) if c != cid]
        await save_linkguard_settings(interaction.guild.id, {"exception_channel_ids": exc_ids})

        channel = interaction.guild.get_channel(cid)
        mention = channel.mention if channel else f"<#{cid}>"
        embed = discord.Embed(
            title="➖ 예외 채널 해제 완료",
            description=f"{mention} 채널이 유출 감지 예외에서 해제되었습니다.\n"
                        "이제 이 채널에서 보호된 링크를 올리면 다시 유출로 감지됩니다.",
            color=discord.Color.orange()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


class RemoveExceptionChannelView(discord.ui.View):
    def __init__(self, exc_ids: list[int], guild: discord.Guild):
        super().__init__(timeout=180)
        self.add_item(RemoveExceptionChannelSelect(exc_ids, guild))


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


# ── 바이옴 알림 멘션 설정 UI ─────────────────────────────────────────

def build_notify_embed(guild: discord.Guild, settings: dict) -> discord.Embed:
    rules = settings.get("notify_rules") or []
    carry = settings.get("carry_embed_mentions", True)

    if rules:
        lines = [f"`{i + 1}.` {describe_notify_rule(r, guild)}"
                 for i, r in enumerate(rules)]
    else:
        lines = ["아직 규칙이 없습니다. **➕ 규칙 추가** 로 만드세요."]

    embed = discord.Embed(
        title="🔔 바이옴 알림 멘션",
        description=(
            "매크로 임베드에는 멘션이 **아예 없는** 경우가 많습니다. 그러면 "
            "링크를 보호해 다시 올려도 사람들은 바이옴이 뜬 것을 모릅니다. "
            "여기서 **봇이 직접 붙일 멘션**을 정합니다.\n\n"
            + "\n".join(lines)
        ),
        color=discord.Color.blurple()
    )
    embed.add_field(
        name="🔁 임베드 안의 멘션 살리기",
        value=("✅ 켜짐" if carry else "❌ 꺼짐") +
              "\n임베드 **안**에 적힌 `<@&역할>` 은 글자로만 보이고 알림이 "
              "가지 않습니다. 켜면 본문으로 끌어올려 실제로 울립니다.",
        inline=False
    )
    excludes = settings.get("notify_exclude") or []
    embed.add_field(
        name="🚫 이 말이 들어 있으면 멘션 안 함",
        value=((", ".join(f"`{w}`" for w in excludes)
                + "\n바이옴 **종료** 임베드에도 같은 이름이 들어 있어서, 이게 "
                  "없으면 뜰 때와 끝날 때 두 번 울립니다.")
               if excludes else
               "**없음** ⚠️ 종료 임베드에도 같은 바이옴 이름이 들어 있으면 "
               "끝날 때도 울립니다."),
        inline=False
    )
    embed.set_footer(text="위에 있는 규칙이 먼저 적용됩니다 (맞는 것 하나만).")
    return embed


def _notify_perm_warning(guild: discord.Guild, rule: dict) -> str:
    """이 규칙이 실제로 울릴 수 있는지 **저장하는 순간** 검사합니다.

    초록불만 띄우면 운영진은 며칠 뒤 "왜 알림이 안 와요" 문의를 받고서야
    압니다. 연동 역할(`linked_role_id`)에서 이미 겪은 일입니다.
    """
    me = guild.me
    if me is None:
        return ""

    kind = rule.get("mention", "roles")
    needs = kind in ("everyone", "here")
    if not needs:
        for rid in rule.get("role_ids") or []:
            role = guild.get_role(rid)
            if role is None or not role.mentionable:
                needs = True
                break
    if not needs:
        return ""

    targets = []
    for cid in rule.get("channel_ids") or []:
        ch = guild.get_channel(cid)
        if ch is not None:
            targets.append(ch)
    for cat_id in rule.get("category_ids") or []:
        category = guild.get_channel(cat_id)
        if isinstance(category, discord.CategoryChannel):
            targets.extend(category.text_channels)
    if not targets:
        targets = list(guild.text_channels)

    blocked = [c for c in targets if not c.permissions_for(me).mention_everyone]
    if not blocked:
        return ""

    shown = " ".join(c.mention for c in blocked[:5])
    more = f" 외 {len(blocked) - 5}곳" if len(blocked) > 5 else ""
    return (f"⚠️ 봇에게 **'@everyone, @here, 모든 역할 멘션'** 권한이 없어 "
            f"{shown}{more} 에서는 **글자만 남고 알림이 가지 않습니다.**")


async def _save_notify_rule(interaction: discord.Interaction, rule: dict) -> None:
    # @everyone 은 채널을 반드시 지정해야 합니다. 범위 없는 전원 호출은
    # 되돌릴 수 없고, 매크로가 아닌 웹훅이 있는 채널까지 휩쓸립니다.
    if (rule.get("mention") == "everyone"
            and not (rule.get("channel_ids") or rule.get("category_ids"))):
        await interaction.response.send_message(
            embed=discord.Embed(
                title="⛔ @everyone 은 채널을 지정해야 합니다",
                description=(
                    "채널을 고르지 않으면 **감시 중인 모든 채널**에서 서버 전원을 "
                    "부르게 됩니다. 매크로 채널을 직접 골라 주세요.\n\n"
                    "-# 범위를 좁히지 않은 전원 호출은 되돌릴 수 없습니다.\n"
                    "-# 채널을 안 고르고 쓰려면 `@here` 나 역할을 쓰세요."),
                color=discord.Color.red()),
            ephemeral=True)
        return

    settings = await get_linkguard_settings(interaction.guild.id)
    rules = list(settings.get("notify_rules") or [])
    rules.append(rule)
    await save_linkguard_settings(interaction.guild.id, {"notify_rules": rules})

    warning = _notify_perm_warning(interaction.guild, rule)
    embed = discord.Embed(
        title="✅ 알림 규칙 추가 완료" if not warning else "⚠️ 저장했지만 확인이 필요합니다",
        description=(f"**{len(rules)}번 규칙**\n"
                     f"{describe_notify_rule(rule, interaction.guild)}"
                     + (f"\n\n{warning}" if warning else "")),
        color=discord.Color.green() if not warning else discord.Color.orange()
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


class NotifyTargetRoleSelect(discord.ui.RoleSelect):
    def __init__(self, channel_ids, category_ids, biomes):
        super().__init__(placeholder="울릴 역할을 고르세요 (여러 개 가능)...",
                         min_values=1, max_values=5)
        self._channel_ids = channel_ids
        self._category_ids = category_ids
        self._biomes = biomes

    async def callback(self, interaction: discord.Interaction):
        await _save_notify_rule(interaction, {
            "channel_ids": self._channel_ids,
            "category_ids": self._category_ids,
            "biomes": self._biomes,
            "mention": "roles",
            "role_ids": [r.id for r in self.values],
        })


class NotifyTargetView(discord.ui.View):
    def __init__(self, channel_ids, category_ids, biomes):
        super().__init__(timeout=180)
        self._channel_ids = channel_ids
        self._category_ids = category_ids
        self._biomes = biomes
        self.add_item(NotifyTargetRoleSelect(channel_ids, category_ids, biomes))

    async def _pick(self, interaction: discord.Interaction, kind: str):
        await _save_notify_rule(interaction, {
            "channel_ids": self._channel_ids,
            "category_ids": self._category_ids,
            "biomes": self._biomes,
            "mention": kind,
            "role_ids": [],
        })

    @discord.ui.button(label="@everyone (서버 전원)", style=discord.ButtonStyle.danger, emoji="📢")
    async def everyone(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._pick(interaction, "everyone")

    @discord.ui.button(label="@here (접속 중인 사람)", style=discord.ButtonStyle.primary, emoji="🟢")
    async def here(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._pick(interaction, "here")


class NotifyBiomeModal(discord.ui.Modal, title="🔔 어떤 바이옴에 알릴까요"):
    biomes = discord.ui.TextInput(
        label="바이옴 이름 (쉼표 또는 줄바꿈으로 구분)",
        placeholder="예: DREAMSPACE, CYBERSPACE, GLITCHED   ·   비우면 모든 바이옴",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=500
    )

    def __init__(self, channel_ids, category_ids):
        super().__init__()
        self._channel_ids = channel_ids
        self._category_ids = category_ids

    async def on_submit(self, interaction: discord.Interaction):
        raw = str(self.biomes.value or "").replace(chr(10), ",")
        names = [x.strip() for x in raw.split(",") if x.strip()]

        where = [f"<#{cid}>" for cid in self._channel_ids]
        for cat_id in self._category_ids:
            category = interaction.guild.get_channel(cat_id)
            where.append(f"📂 {category.name}" if category else f"📂 {cat_id}")

        listed = ", ".join("`" + n + "`" for n in names)
        embed = discord.Embed(
            title="🔔 누구를 부를까요",
            description=(
                f"**어디서** {' '.join(where) or '감시 중인 모든 채널'}\n"
                f"**언제** {listed or '모든 바이옴'}\n\n"
                "울릴 대상을 고르세요."
            ),
            color=discord.Color.blurple()
        )
        if names:
            embed.set_footer(text="이름은 대소문자·공백·기호를 무시하고 '들어있는지'로 봅니다.")
        await interaction.response.send_message(
            embed=embed,
            view=NotifyTargetView(self._channel_ids, self._category_ids, names),
            ephemeral=True
        )


class NotifyScopeSelect(discord.ui.ChannelSelect):
    def __init__(self):
        super().__init__(
            placeholder="📂 카테고리(권장) 또는 채널을 고르세요...",
            channel_types=[discord.ChannelType.text, discord.ChannelType.category],
            max_values=10
        )

    async def callback(self, interaction: discord.Interaction):
        channel_ids, category_ids = [], []
        for picked in self.values:
            if picked.type == discord.ChannelType.category:
                category_ids.append(picked.id)
            else:
                channel_ids.append(picked.id)
        await interaction.response.send_modal(NotifyBiomeModal(channel_ids, category_ids))


class NotifyScopeView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)
        self.add_item(NotifyScopeSelect())

    @discord.ui.button(label="채널 구분 없이 전부", style=discord.ButtonStyle.secondary, emoji="🌐")
    async def everywhere(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(NotifyBiomeModal([], []))


class NotifyRuleRemoveSelect(discord.ui.Select):
    def __init__(self, rules: list, guild: discord.Guild):
        options = []
        for i, rule in enumerate(rules[:25]):
            options.append(discord.SelectOption(
                label=f"{i + 1}번 규칙"[:100],
                value=str(i),
                description=describe_notify_rule(rule, guild)[:95],
                emoji="🗑️"
            ))
        super().__init__(placeholder="삭제할 규칙을 고르세요...", options=options)

    async def callback(self, interaction: discord.Interaction):
        index = int(self.values[0])
        settings = await get_linkguard_settings(interaction.guild.id)
        rules = list(settings.get("notify_rules") or [])
        if index >= len(rules):
            await interaction.response.send_message(
                "⚠️ 그 사이에 규칙이 바뀌었습니다. 다시 열어 주세요.", ephemeral=True)
            return
        removed = rules.pop(index)
        await save_linkguard_settings(interaction.guild.id, {"notify_rules": rules})
        await interaction.response.send_message(
            embed=discord.Embed(
                title="🗑️ 알림 규칙 삭제 완료",
                description=describe_notify_rule(removed, interaction.guild),
                color=discord.Color.orange()),
            ephemeral=True)


class NotifyRuleRemoveView(discord.ui.View):
    def __init__(self, rules: list, guild: discord.Guild):
        super().__init__(timeout=180)
        self.add_item(NotifyRuleRemoveSelect(rules, guild))


class NotifyExcludeModal(discord.ui.Modal, title="🚫 멘션하지 않을 임베드"):
    words = discord.ui.TextInput(
        label="이 말이 들어 있으면 멘션하지 않습니다",
        placeholder="예: Biome Ended, 바이옴 종료   ·   비우면 전부 멘션합니다",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=500
    )

    async def on_submit(self, interaction: discord.Interaction):
        raw = str(self.words.value or "").replace(chr(10), ",")
        items = [x.strip() for x in raw.split(",") if x.strip()]
        await save_linkguard_settings(interaction.guild.id,
                                      {"notify_exclude": items})

        if items:
            desc = ("이 말이 임베드에 들어 있으면 봇이 멘션을 붙이지 않습니다.\n"
                    + ", ".join(f"`{w}`" for w in items)
                    + "\n\n-# 보호는 그대로 합니다 — 멘션만 안 붙입니다.\n"
                      "-# 대소문자·공백·기호는 무시하고 '들어있는지'로 봅니다.")
            color = discord.Color.green()
        else:
            desc = ("⚠️ **비웠습니다.** 이제 바이옴 **종료** 임베드에도 같은 "
                    "이름이 들어 있으면 끝날 때 한 번 더 울립니다.")
            color = discord.Color.orange()
        await interaction.response.send_message(
            embed=discord.Embed(title="🚫 제외 단어 변경", description=desc, color=color),
            ephemeral=True)


class NotifyRuleView(discord.ui.View):
    def __init__(self, settings: dict):
        super().__init__(timeout=180)
        carry = settings.get("carry_embed_mentions", True)
        self.carry.label = "임베드 멘션 살리기: " + ("켜짐" if carry else "꺼짐")
        self.carry.style = (discord.ButtonStyle.success if carry
                            else discord.ButtonStyle.secondary)

    @discord.ui.button(label="규칙 추가", style=discord.ButtonStyle.primary, emoji="➕")
    async def add(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            embed=discord.Embed(
                title="🔔 어디에 적용할까요",
                description=(
                    "멘션을 붙일 **채널 또는 카테고리**를 고르세요 (최대 10개, "
                    "섞어서 고를 수 있습니다).\n\n"
                    "📂 **카테고리를 고르는 쪽을 권합니다.** 그 안의 채널이 전부 "
                    "적용되고, 나중에 그 카테고리에 **새 채널이 생겨도 규칙을 "
                    "고칠 필요가 없습니다.** 채널을 하나씩 걸어 두면 새 매크로 "
                    "채널이 생긴 날 그 채널의 희귀 바이옴은 조용히 지나갑니다.\n\n"
                    "-# 고르지 않고 **채널 구분 없이 전부** 를 누르면 감시 중인 "
                    "모든 채널에 적용됩니다. `@everyone` 은 이 방식으로는 "
                    "저장할 수 없습니다."),
                color=discord.Color.blurple()),
            view=NotifyScopeView(), ephemeral=True)

    @discord.ui.button(label="규칙 삭제", style=discord.ButtonStyle.danger, emoji="🗑️")
    async def remove(self, interaction: discord.Interaction, button: discord.ui.Button):
        settings = await get_linkguard_settings(interaction.guild.id)
        rules = settings.get("notify_rules") or []
        if not rules:
            await interaction.response.send_message("삭제할 규칙이 없습니다.", ephemeral=True)
            return
        await interaction.response.send_message(
            embed=discord.Embed(title="🗑️ 삭제할 규칙 선택", color=discord.Color.orange()),
            view=NotifyRuleRemoveView(rules, interaction.guild), ephemeral=True)

    @discord.ui.button(label="제외 단어", style=discord.ButtonStyle.secondary, emoji="🚫")
    async def exclude(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(NotifyExcludeModal())

    @discord.ui.button(label="임베드 멘션 살리기", style=discord.ButtonStyle.secondary, emoji="🔁")
    async def carry(self, interaction: discord.Interaction, button: discord.ui.Button):
        settings = await get_linkguard_settings(interaction.guild.id)
        new_state = not settings.get("carry_embed_mentions", True)
        await save_linkguard_settings(interaction.guild.id,
                                      {"carry_embed_mentions": new_state})
        await interaction.response.send_message(
            embed=discord.Embed(
                title="🔁 임베드 멘션 살리기 " + ("✅ 켜짐" if new_state else "❌ 꺼짐"),
                description=("임베드 **안**에 적힌 역할 멘션을 본문으로 끌어올려 "
                             "실제로 알림이 가게 합니다."
                             if new_state else
                             "임베드 안의 멘션은 글자로만 남고 알림이 가지 않습니다."),
                color=discord.Color.green() if new_state else discord.Color.greyple()),
            ephemeral=True)


class GuardSettingsSelect(discord.ui.Select):
    def __init__(self, current: dict):
        channel_count = len(current.get("channel_ids", []))
        category_count = len(current.get("category_ids", []))
        exc_count = len(current.get("exception_channel_ids", []))
        enabled_desc = "현재: ✅ 활성화" if current.get("enabled", True) else "현재: ❌ 비활성화"
        board_id = current.get("biome_board_channel_id")
        board_summary = (f"현재 <#{board_id}> 에 실시간 카운터를 올립니다."
                         if board_id else
                         "희귀 바이옴 등장 수를 한 채널에 모아 보여줍니다.")
        rule_count = len(current.get("notify_rules", []))
        notify_desc = (f"현재 {rule_count}개 규칙 — 재게시할 때 봇이 직접 멘션합니다."
                       if rule_count else
                       "매크로 임베드에 멘션이 없으면 봇이 직접 붙입니다.")

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
                label="\U0001f3c3 희귀 바이옴 바로 들어가기 채널",
                value="rarecopy",
                description="희귀 바이옴이 뜨면 원본 주소를 이 채널로 복사합니다.",
                emoji="\U0001f3c3",
            ),
            discord.SelectOption(
                label="\U0001f513 보호 제외 채널 지정 (원본 그대로)",
                value="addnoguard",
                description="관리자 전용 채널에 원본 임베드를 그대로 둡니다.",
                emoji="\U0001f513",
            ),
            discord.SelectOption(
                label="\U0001f512 보호 제외 해제 (다시 보호)",
                value="removenoguard",
                description="제외했던 채널을 다시 보호합니다.",
                emoji="\U0001f512",
            ),
            discord.SelectOption(
                label="유출 감지 예외 채널/카테고리 추가",
                value="addexc",
                description="특정 채널/카테고리에서는 보호된 링크를 올려도 삭제하지 않습니다.",
                emoji="🛡️"
            ),
            discord.SelectOption(
                label="유출 예외 채널/카테고리 해제",
                value="removeexc",
                description=f"지정된 {exc_count}개 예외 채널/카테고리 중 선택 해제",
                emoji="➖"
            ),
            discord.SelectOption(
                label="버튼 / 잠금 문구 수정",
                value="text",
                description="링크 버튼 문구와 잠금 안내 문구를 변경합니다.",
                emoji="📝"
            ),
            discord.SelectOption(
                label="바이옴 카운터 채널",
                value="biomeboard",
                description=board_summary,
                emoji="🌋"
            ),
            discord.SelectOption(
                label="바이옴 알림 멘션",
                value="notify",
                description=notify_desc,
                emoji="🔔"
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

        elif choice == "rarecopy":
            await interaction.response.send_message(
                embed=build_rare_copy_embed_settings(guild, settings),
                view=RareCopyChannelView(settings), ephemeral=True)

        elif choice == "addnoguard":
            ids = list(settings.get("no_guard_channel_ids") or [])
            await interaction.response.send_message(
                embed=build_no_guard_embed(guild, ids),
                view=AddNoGuardChannelView(), ephemeral=True)

        elif choice == "removenoguard":
            ids = list(settings.get("no_guard_channel_ids") or [])
            if not ids:
                await interaction.response.send_message(
                    "\u2139\ufe0f 보호를 제외한 채널이 없습니다. "
                    "지금은 감시 대상이 전부 보호되고 있습니다.",
                    ephemeral=True)
                return
            await interaction.response.send_message(
                "\U0001f512 **다시 보호할 채널을 선택하세요.**",
                view=RemoveNoGuardChannelView(ids, guild), ephemeral=True)

        elif choice == "addexc":
            await interaction.response.send_message(
                "🛡️ **유출 감지에서 제외할 예외 채널(또는 카테고리)을 선택하세요.**",
                view=AddExceptionChannelView(),
                ephemeral=True
            )

        elif choice == "removeexc":
            exc_ids = settings.get("exception_channel_ids", [])
            if not exc_ids:
                await interaction.response.send_message(
                    "ℹ️ 현재 예외로 지정된 채널/카테고리가 없습니다.",
                    ephemeral=True
                )
                return
            await interaction.response.send_message(
                "➖ **예외 지정에서 해제할 채널/카테고리를 선택하세요.**",
                view=RemoveExceptionChannelView(exc_ids, guild),
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

        elif choice == "biomeboard":
            board_id = settings.get("biome_board_channel_id")
            current_ch = guild.get_channel(int(board_id)) if board_id else None
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="🌋 바이옴 실시간 카운터",
                    description=(
                        "희귀 바이옴이 **오늘 · 7일 · 30일** 각각 몇 번 떴는지를 "
                        "한 채널에 모아 보여줍니다.\n\n"
                        f"**현재 채널** {current_ch.mention if current_ch else '미설정'}\n"
                        "-# 메시지 하나를 계속 수정하므로 도배되지 않습니다."),
                    color=discord.Color.gold()),
                view=BiomeBoardView(), ephemeral=True)

        elif choice == "notify":
            await interaction.response.send_message(
                embed=build_notify_embed(guild, settings),
                view=NotifyRuleView(settings),
                ephemeral=True
            )

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

    # 유출 감지 예외 채널/카테고리
    exc_ids = settings.get("exception_channel_ids", [])
    if exc_ids:
        lines = []
        for cid in exc_ids[:15]:
            channel = guild.get_channel(cid)
            if channel is None:
                lines.append(f"• ⚠️ <#{cid}> 채널/카테고리를 찾을 수 없음")
                continue
            if isinstance(channel, discord.CategoryChannel):
                lines.append(f"• 🛡️ 🗂️ **{channel.name}** (카테고리 전체 예외)")
            else:
                lines.append(f"• 🛡️ {channel.mention}")
        embed.add_field(
            name=f"🛡️ 유출 감지 예외 ({len(exc_ids)}개)",
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
    # 알림 멘션은 **설정했는데 권한이 없어 조용히 죽는** 대표적인 자리입니다.
    # 여기서 보여주지 않으면 "왜 알림이 안 와요"를 아무도 못 풉니다.
    rules = settings.get("notify_rules") or []
    if rules:
        lines = []
        for i, rule in enumerate(rules[:5]):
            warn = _notify_perm_warning(guild, rule)
            mark = "⚠️" if warn else "✅"
            lines.append(f"{mark} `{i + 1}.` {describe_notify_rule(rule, guild)}")
        if len(rules) > 5:
            lines.append(f"-# 외 {len(rules) - 5}개")
        excludes = settings.get("notify_exclude") or []
        lines.append("-# 🚫 멘션 안 함: " + (", ".join(f"`{w}`" for w in excludes)
                     if excludes else "**없음** — 종료 임베드에도 울립니다"))
        if any(_notify_perm_warning(guild, r) for r in rules):
            lines.append("-# ⚠️ 표시된 규칙은 봇에게 "
                         "`@everyone, @here, 모든 역할 멘션` 권한이 없어 "
                         "글자만 남고 알림이 가지 않습니다.")
        value = "\n".join(lines)
    else:
        value = ("미설정 — 매크로 임베드에 멘션이 없으면 아무도 바이옴이 뜬 것을 "
                 "모릅니다. **바이옴 알림 멘션**에서 규칙을 만드세요.")
    embed.add_field(name="🔔 바이옴 알림 멘션", value=value[:1024], inline=False)

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


# ── 희귀 바이옴 바로 들어가기 채널 ──────────────────────────────────
#
# 희귀 바이옴이 뜨면 **원본 개인서버 주소를 그대로** 관리자 채널에 복사합니다.
# 매크로 채널은 보호된 버튼이라 한 번 더 눌러야 하는데, 희귀 바이옴은
# 길어야 10분이라 그 한 번이 아쉽습니다.
#
# ⚠️ **도배를 반드시 막아야 합니다.** 매크로는 같은 개인서버 링크를 상태
# 갱신마다 다시 올립니다(실측 평균 145번, 최다 527번). 그대로 두면 이
# 채널이 같은 링크로 도배돼 정작 새로 뜬 바이옴이 묻힙니다.
#
# 키는 **(링크 + 바이옴)** 입니다. 링크만으로 잡으면 안 됩니다 —
# 개인서버 주소는 그대로인 채 바이옴만 바뀌므로, 같은 서버에서 DREAMSPACE
# 뒤에 CYBERSPACE 가 떠도 통째로 묻힙니다(알림 쿨타임을 걷어낸 것과 같은
# 이유입니다). 창은 바이옴 수명(최대 10분)보다 **짧게** 둡니다.
# 중복 제거 창(분). **기본은 0 = 끔** — 실측(2026-09-16)에서 희귀 바이옴은
# 서버당 딱 1번만 올라왔습니다(합계 5건 / 고유 서버 5곳). 같은 링크가 수백 번
# 다시 올라오는 것은 **흔한 바이옴** 얘기였습니다(NORMAL 123.6번).
# 매크로가 동작을 바꿔 도배가 생기면 설정으로 되돌릴 수 있게 남겨 둡니다.
RARE_COPY_WINDOW_CHOICES = (0, 3, 8)
_rare_copied: dict[str, float] = {}


def should_copy_rare(link: str, biome: str, window: float = 0,
                     now: float | None = None) -> bool:
    """이 (링크, 바이옴) 조합을 지금 복사해도 되는지.

    `window` 가 0 이하면 **무조건 보냅니다.** 그 위면 같은 조합이 창 안에
    이미 갔는지 봅니다. 창이 지나면 다시 보냅니다 — 같은 서버에서 같은
    바이옴이 다시 뜬 것일 수 있고, 그건 새 사건입니다.

    ⚠️ 키는 **(링크 + 바이옴)** 입니다. 링크만으로 막으면 주소는 그대로인 채
    바이옴만 바뀌는 경우(같은 서버의 DREAMSPACE → CYBERSPACE)가 통째로
    묻힙니다 — 알림 쿨타임을 걷어낸 것과 같은 이유입니다.
    """
    if window <= 0:
        return True
    now = time.monotonic() if now is None else now
    key = f"{link}|{_norm_biome(biome)}"
    last = _rare_copied.get(key)
    if last is not None and now - last < window:
        return False
    _rare_copied[key] = now
    # 창이 지난 것은 버립니다. 안 버리면 하루에 수천 개가 쌓입니다.
    if len(_rare_copied) > 2000:
        for k, t in list(_rare_copied.items()):
            if now - t >= window:
                _rare_copied.pop(k, None)
    return True


def build_rare_copy_embed(biome: str, link: str, source, when) -> discord.Embed:
    """관리자 채널에 올릴 '바로 들어가기' 카드."""
    embed = discord.Embed(
        title=f"\U0001f3c3 {biome}",
        description=f"## [\u25b6 바로 들어가기]({link})\n"
                    f"-# 주소가 그대로 있으니 눌러서 바로 입장하세요.",
        color=discord.Color.gold())
    embed.add_field(name="\U0001f4cd 어느 채널에서", value=source, inline=True)
    embed.add_field(name="\u23f0 뜬 시각",
                    value=f"<t:{int(when)}:T> (<t:{int(when)}:R>)", inline=True)
    embed.add_field(
        name="\U0001f517 주소", value=f"```{link}```", inline=False)
    embed.set_footer(text="희귀 바이옴은 길어야 10분입니다 · 관리자 전용")
    return embed


# ── 바이옴 등장 통계 ─────────────────────────────────────────────────
#
# **세는 단위는 '바이옴 시작 임베드 하나' 입니다.** 종료 임베드는 세지 않고
# (`detect_biome_name` 이 None 을 돌려줍니다), 같은 링크가 몇 번 올라오든
# 올라온 만큼 셉니다 — 개인서버 링크는 매 임베드마다 바꿀 수 없어서, 링크로
# 묶으면 같은 서버에서 나중에 다시 뜬 바이옴이 통째로 묻힙니다.

BIOME_PERIODS = [("오늘", 1), ("7일", 7), ("30일", 30)]

# 카운트 대상은 **스스로 유지됩니다.**
#   · 새 바이옴이 감지되면 자동으로 들어옵니다 (기록되는 순간 목록에 뜹니다)
#   · 이 기간 동안 한 번도 안 뜬 바이옴은 자동으로 빠집니다
# 게임에 바이옴이 추가·삭제돼도 사람이 목록을 손볼 일이 없어야 합니다.
#
# 규칙에 적어 둔 **희귀 바이옴은 이 기간과 무관하게 항상** 남습니다
# (설정한 것이 안 뜨고 있다는 사실 자체가 봐야 할 정보입니다).
BIOME_ACTIVE_DAYS = 7


def rare_biome_names(settings: dict) -> list[str]:
    """알림 규칙이 지정한 바이옴 이름을 모두 모읍니다 (희귀 표시용)."""
    names = []
    for rule in settings.get("notify_rules") or []:
        if isinstance(rule, dict):
            for b in rule.get("biomes") or []:
                if b and b not in names:
                    names.append(b)
    return names


def summarize_biomes(buckets: list[dict], days: int) -> dict[str, dict]:
    """날짜 버킷을 바이옴별로 접습니다.

    `total` 은 보관 기간(90일) 전체의 종합 등장 횟수이고, `count` 는 고른
    기간 안의 것입니다. 둘을 함께 보여줘야 "오늘은 조용한데 원래 흔한
    바이옴인지" 를 알 수 있습니다.
    """
    today = _biome_today()
    cutoff = _biome_day_str(days)

    out: dict[str, dict] = {}
    for b in buckets:
        name = str(b.get("biome") or "?")
        day = str(b.get("day") or "")
        cur = out.setdefault(name, {"count": 0, "today": 0, "total": 0,
                                    "notified": 0, "servers": set(),
                                    "last": "", "first": "9999-99-99"})
        n = int(b.get("count", 0) or 0)
        cur["total"] += n
        if day >= cutoff:
            cur["count"] += n
            cur["notified"] += int(b.get("notified_count", 0) or 0)
            for lk in (b.get("links") or []):
                cur["servers"].add(lk)
        if day == today:
            cur["today"] += n
        if day > cur["last"]:
            cur["last"] = day
        if day and day < cur["first"]:
            cur["first"] = day
    return out


def active_biomes(agg: dict, days: int = BIOME_ACTIVE_DAYS) -> tuple[list[str], list[str]]:
    """카운트 대상과, 활동이 끊겨 빠진 바이옴을 갈라 돌려줍니다.

    **조용히 빼면 안 됩니다.** 목록에서 사라진 이유를 알 수 없으면
    "고장난 건가" 를 판단할 방법이 없으므로, 빠진 것은 숫자로 알립니다.
    """
    cutoff = _biome_day_str(days)
    live = [k for k, v in agg.items() if v.get("last", "") >= cutoff]
    dropped = [k for k in agg if k not in live]
    return live, dropped





def _biome_today() -> str:
    return datetime.now(KST).strftime("%Y-%m-%d")


def _biome_day_str(days_back: int) -> str:
    return (datetime.now(KST) - timedelta(days=max(1, days_back) - 1)).strftime("%Y-%m-%d")


def _chunk_field(embed: discord.Embed, name: str, lines: list[str]) -> None:
    """한 칸에 안 들어가면 나눠 담습니다.

    디스코드 임베드 필드는 1024자 한도입니다. 넘으면 **전송 자체가 실패**해
    보드가 통째로 안 올라갑니다. 바이옴을 전부 보여주기로 한 이상 줄 수가
    늘어날 수 있으므로 잘라내지 말고 나눠 담습니다.
    """
    if not lines:
        embed.add_field(name=name, value="없음", inline=False)
        return
    buf, part = [], 0
    for line in lines:
        cand = "\n".join(buf + [line])
        if len(cand) > 1024 and buf:
            embed.add_field(name=name if part == 0 else f"{name} (이어서)",
                            value="\n".join(buf), inline=False)
            buf, part = [line], part + 1
        else:
            buf.append(line)
    if buf:
        embed.add_field(name=name if part == 0 else f"{name} (이어서)",
                        value="\n".join(buf), inline=False)


def _blank_biome() -> dict:
    return {"count": 0, "today": 0, "total": 0, "notified": 0,
            "servers": set(), "last": "", "first": "9999-99-99"}


def _biome_lines(agg: dict, rare: list[str], names: list[str]) -> list[str]:
    out = []
    for name in names:
        v = agg.get(name) or _blank_biome()
        is_rare = biome_matches(rare, name) is not None
        mark = "⭐" if is_rare else "•"
        servers = len(v["servers"]) if isinstance(v["servers"], set) else 0
        bell = ""
        if is_rare:
            bell = f" · 🔔{v['notified']}" if v["notified"] else " · 🔕"
        srv = f" · 매크로 채널 {servers}곳" if servers else ""
        out.append(f"{mark} **{name}** — {v['count']:,}회{srv}{bell}\n"
                   f"　누적 {v['total']:,}회 · 마지막 {v['last'] or '-'}")
    return out


def build_biome_stats_embed(guild: discord.Guild, buckets: list[dict],
                            settings: dict, days: int,
                            unparsed: dict | None = None) -> discord.Embed:
    label = next((n for n, d in BIOME_PERIODS if d == days), f"{days}일")
    rare = rare_biome_names(settings)
    agg = summarize_biomes(buckets, days)

    embed = discord.Embed(title=f"📊 바이옴 등장 통계 · 최근 {label}",
                          color=discord.Color.blurple())

    if not agg:
        embed.description = (
            "이 기간에 기록된 바이옴이 없습니다.\n\n"
            "-# 기록은 **링크 보호가 동작한 시점부터** 쌓입니다.\n"
            "-# 매크로 임베드 제목이 `Biome Started - <이름>` 형식이 아니면 "
            "이름을 알 수 없어 세지 않습니다.")
        return embed

    total_now = sum(v["count"] for v in agg.values())
    total_all = sum(v["total"] for v in agg.values())
    notified = sum(v["notified"] for v in agg.values())
    rare_now = sum(v["count"] for k, v in agg.items() if biome_matches(rare, k))

    embed.description = (
        f"🌍 **{total_now:,}회 등장** · 누적 **{total_all:,}회**\n"
        f"🔔 알림 **{notified:,}회**"
        + (f" · 희귀 바이옴 **{rare_now:,}회**" if rare else "")
        + "\n-# **바이옴 시작 임베드 하나 = 1회.** 종료 임베드는 세지 않습니다.")

    # 희귀한 것을 먼저, 그다음 많이 뜬 순서. **전부 보여줍니다** — '외 N종'
    # 으로 접으면 정작 궁금한 바이옴이 그 안에 숨습니다.
    # **카운트 대상은 스스로 유지됩니다.** 새 바이옴은 기록되는 순간 들어오고,
    # 30일 동안 한 번도 안 뜬 것은 빠집니다. 게임에 바이옴이 추가·삭제돼도
    # 사람이 목록을 손볼 일이 없어야 합니다.
    live, dropped = active_biomes(agg)

    rare_names = [k for k in live if biome_matches(rare, k)]
    # 규칙에 적어 둔 희귀 바이옴은 활동이 없어도 **항상** 보여줍니다.
    # 설정해 둔 것이 안 뜨고 있다는 사실 자체가 봐야 할 정보입니다.
    for name in rare:
        if not any(biome_matches([name], k) for k in rare_names):
            rare_names.append(name)
            agg.setdefault(name, _blank_biome())
    rare_names = sorted(dict.fromkeys(rare_names),
                        key=lambda k: (-agg[k]["count"], k))
    others = sorted([k for k in live if k not in rare_names],
                    key=lambda k: (-agg[k]["count"], k))

    if rare:
        _chunk_field(embed, "⭐ 희귀 바이옴", _biome_lines(agg, rare, rare_names))
    if others:
        _chunk_field(embed, "🌍 그 밖의 바이옴", _biome_lines(agg, rare, others))

    if rare:
        silent = [k for k in rare_names if agg[k]["count"] and not agg[k]["notified"]]
        if silent:
            embed.add_field(
                name="⚠️ 떴는데 알림이 안 나간 희귀 바이옴",
                value=(", ".join(f"`{n}`" for n in silent[:15])
                       + "\n-# 해당 채널이 알림 규칙 범위에 없거나, 봇에게 "
                         "`@everyone, @here, 모든 역할 멘션` 권한이 없습니다.\n"
                         "-# `/링크보호설정` → 📊 현재 설정 및 권한 점검 에서 확인하세요."),
                inline=False)
        elif rare_now:
            # 문제가 없을 때도 **명시적으로** 말해 줘야 합니다. 아무 칸도
            # 없으면 검사를 한 것인지 안 한 것인지 알 수 없습니다.
            embed.add_field(
                name="✅ 희귀 바이옴 알림",
                value=f"이 기간에 뜬 희귀 바이옴 **{rare_now:,}회** 모두 알림이 나갔습니다.",
                inline=False)
        embed.set_footer(text="⭐ = 알림 규칙에 등록된 희귀 바이옴")
    else:
        embed.set_footer(text="알림 규칙이 없어 희귀 바이옴 구분이 없습니다.")

    # **못 읽은 제목이 있으면 반드시 말합니다.** 이 칸이 비어 있어야만
    # 위의 숫자를 믿을 수 있습니다 — 안 보이면 "0회" 와 "못 셌음" 을
    # 구분할 방법이 없습니다.
    if unparsed and unparsed.get("count"):
        samples = unparsed.get("titles") or []
        embed.add_field(
            name="❓ 이름을 못 읽은 임베드",
            value=(f"**{unparsed['count']:,}건**의 보호 임베드에서 바이옴 이름을 "
                   "읽지 못했습니다. 그만큼은 위 숫자에 **빠져 있습니다.**\n"
                   + ("".join(f"-# `{t}`\n" for t in samples[:5]) if samples else "")
                   + "-# 매크로가 판올림하며 제목 형식을 바꿨을 수 있습니다. "
                     "이 제목들을 알려 주시면 인식 범위를 넓히겠습니다."),
            inline=False)

    # 조용히 빼면 왜 사라졌는지 알 수 없습니다. 숫자로 알립니다.
    if dropped:
        embed.add_field(
            name="💤 카운트 대상에서 빠진 바이옴",
            value=(f"**{len(dropped)}종**이 최근 {BIOME_ACTIVE_DAYS}일간 한 번도 "
                   "뜨지 않아 목록에서 빠졌습니다.\n"
                   f"-# {', '.join(f'`{n}`' for n in sorted(dropped)[:12])}"
                   + (" …" if len(dropped) > 12 else "")
                   + "\n-# 다시 뜨면 자동으로 돌아옵니다."),
            inline=False)
    return embed


class BiomeStatsView(discord.ui.View):
    def __init__(self, days: int = 1):
        super().__init__(timeout=180)
        for name, d in BIOME_PERIODS:
            self.add_item(BiomeStatsButton(name, d, active=(d == days)))


class BiomeStatsButton(discord.ui.Button):
    def __init__(self, label: str, days: int, active: bool = False):
        super().__init__(
            label=f"최근 {label}",
            style=discord.ButtonStyle.primary if active else discord.ButtonStyle.secondary,
            emoji="📊", disabled=active)
        self._days = days

    async def callback(self, interaction: discord.Interaction):
        # 기간이 길면 버킷을 수천 건 읽습니다. defer 없이는 3초를 넘깁니다.
        await interaction.response.defer(ephemeral=True, thinking=True)
        buckets = await get_biome_buckets(interaction.guild.id, 90)
        settings = await get_linkguard_settings(interaction.guild.id)
        unparsed = await get_biome_unparsed(interaction.guild.id, self._days)
        await interaction.followup.send(
            embed=build_biome_stats_embed(interaction.guild, buckets,
                                          settings, self._days, unparsed),
            view=BiomeStatsView(self._days), ephemeral=True)


class BiomeBoardSelect(discord.ui.ChannelSelect):
    def __init__(self):
        super().__init__(placeholder="카운터를 올릴 채널을 고르세요...",
                         channel_types=[discord.ChannelType.text], max_values=1)

    async def callback(self, interaction: discord.Interaction):
        channel = self.values[0]
        resolved = interaction.guild.get_channel(channel.id)

        # **옮기면 옛 메시지 ID 를 반드시 버립니다.** 안 버리면 새 채널에서
        # 그 ID 를 찾다 NotFound 가 나고, 그때마다 보드를 새로 올려 쌓입니다.
        await save_linkguard_settings(interaction.guild.id, {
            "biome_board_channel_id": channel.id,
            "biome_board_message_id": None,
        })

        me = interaction.guild.me
        perms = resolved.permissions_for(me) if (resolved and me) else None
        warn = ""
        if perms is not None and not (perms.send_messages and perms.embed_links):
            warn = ("\n\n⚠️ 봇에게 이 채널의 **`메시지 보내기` · `링크 첨부`** "
                    "권한이 없어 카운터가 올라가지 않습니다.")

        await interaction.response.send_message(
            embed=discord.Embed(
                title="🌋 바이옴 카운터 채널 설정" + ("" if not warn else " (확인 필요)"),
                description=(f"카운터를 {channel.mention} 에 올립니다.\n"
                             "-# 메시지 **하나를 계속 수정**합니다 — 도배되지 않습니다.\n"
                             "-# 희귀 바이옴이 뜰 때마다 갱신되고, 10분마다 한 번 더 "
                             "맞춥니다(날짜가 바뀌면 '오늘' 이 0 이 되어야 하므로).\n"
                             "-# 유저가 볼 채널이면 **읽기 전용**으로 두세요."
                             + warn),
                color=discord.Color.gold() if not warn else discord.Color.orange()),
            ephemeral=True)

        settings = await get_linkguard_settings(interaction.guild.id)
        await _apply_board_refresh(interaction.guild, settings)


class BiomeBoardView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)
        self.add_item(BiomeBoardSelect())

    @discord.ui.button(label="지금 새로 고침", style=discord.ButtonStyle.primary, emoji="🔄")
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button):
        settings = await get_linkguard_settings(interaction.guild.id)
        if not settings.get("biome_board_channel_id"):
            await interaction.response.send_message(
                "⚠️ 먼저 카운터를 올릴 채널을 지정해 주세요.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await _apply_board_refresh(interaction.guild, settings)
        await interaction.followup.send("🔄 카운터를 갱신했습니다.", ephemeral=True)

    @discord.ui.button(label="카운터 끄기", style=discord.ButtonStyle.danger, emoji="🚫")
    async def disable(self, interaction: discord.Interaction, button: discord.ui.Button):
        await save_linkguard_settings(interaction.guild.id, {
            "biome_board_channel_id": None, "biome_board_message_id": None})
        await interaction.response.send_message(
            embed=discord.Embed(
                title="🚫 바이옴 카운터 끔",
                description="더 이상 갱신하지 않습니다.\n"
                            "-# 이미 올라간 메시지는 **지우지 않았습니다** — "
                            "필요하면 직접 지우세요.",
                color=discord.Color.greyple()),
            ephemeral=True)


KST = timezone(timedelta(hours=9))

# 보드 갱신 빈도 제한. 보호는 시간당 수백 건이라 매번 고치면 디스코드
# 편집 한도에 닿습니다. 창이 닫힌 뒤 **최신 값으로 한 번 더** 고칩니다.
BOARD_REFRESH_INTERVAL = 60.0
_last_board_refresh: dict[int, float] = {}
_board_trailing: dict[int, asyncio.Task] = {}
# 동시 요청이 보드를 **두 개 만들지** 않도록 하는 길드별 잠금
_board_locks: dict[int, asyncio.Lock] = {}


def build_biome_board_embed(guild: discord.Guild, buckets: list[dict],
                            settings: dict) -> discord.Embed:
    rare = rare_biome_names(settings)
    agg = summarize_biomes(buckets, BIOME_ACTIVE_DAYS)

    embed = discord.Embed(title="🌋 희귀 바이옴 실시간 카운터",
                          color=discord.Color.gold())

    # 카운트 대상은 스스로 유지됩니다 (새 바이옴 자동 추가 / 30일 무활동 제외).
    live, dropped = active_biomes(agg)

    # 규칙에 있는 희귀 바이옴은 **0회여도 항상** 보여줍니다. 빈칸이면 안 뜬
    # 건지 고장난 건지 판단할 수 없습니다.
    rare_names = [k for k in live if biome_matches(rare, k)]
    for name in rare:
        if not any(biome_matches([name], k) for k in rare_names):
            rare_names.append(name)
            agg.setdefault(name, _blank_biome())

    rare_names = sorted(dict.fromkeys(rare_names),
                        key=lambda k: (-agg[k]["today"], -agg[k]["count"], k))
    others = sorted([k for k in live if k not in rare_names],
                    key=lambda k: (-agg[k]["today"], -agg[k]["count"], k))

    def rows(names):
        out = []
        for name in names:
            v = agg[name]
            servers = len(v["servers"]) if isinstance(v["servers"], set) else 0
            srv = f" · 매크로 채널 {servers}곳" if servers else ""
            out.append(f"**{name}** — 오늘 **{v['today']:,}** · "
                       f"7일 **{v['count']:,}** · 누적 **{v['total']:,}**{srv}")
        return out

    if rare:
        _chunk_field(embed, "⭐ 희귀 바이옴", rows(rare_names))
    else:
        embed.add_field(
            name="⭐ 희귀 바이옴",
            value="아직 알림 규칙이 없어 무엇이 희귀인지 모릅니다.\n"
                  "-# `/링크보호설정` → 🔔 바이옴 알림 멘션 에서 지정하세요.",
            inline=False)

    # **전부 보여줍니다.** '외 N종' 으로 접으면 정작 궁금한 것이 그 안에 숨습니다.
    if others:
        _chunk_field(embed, "🌍 그 밖의 바이옴", rows(others))

    today_total = sum(v["today"] for v in agg.values())
    all_total = sum(v["total"] for v in agg.values())
    embed.description = (
        f"오늘 **{today_total:,}회** 등장 · 누적 **{all_total:,}회**\n"
        "-# 1분마다 갱신 · **바이옴 시작 임베드 하나 = 1회** (종료는 세지 않습니다)")
    if dropped:
        embed.add_field(
            name="💤 빠진 바이옴",
            value=f"**{len(dropped)}종**이 최근 {BIOME_ACTIVE_DAYS}일간 뜨지 않아 "
                  "목록에서 빠졌습니다. 다시 뜨면 자동으로 돌아옵니다.",
            inline=False)
    embed.set_footer(text="1분마다 자동 갱신 · 목록은 스스로 유지됩니다")
    embed.timestamp = datetime.now(timezone.utc)
    return embed


async def _apply_board_refresh(guild: discord.Guild, settings: dict) -> None:
    channel_id = settings.get("biome_board_channel_id")
    if not channel_id:
        return
    channel = guild.get_channel(int(channel_id))
    if channel is None:
        print(f"[LinkGuard] 바이옴 보드 채널({channel_id})을 찾을 수 없습니다.",
              flush=True)
        return

    me = guild.me
    perms = channel.permissions_for(me) if me else None
    if perms is not None and not (perms.send_messages and perms.embed_links):
        print(f"[LinkGuard] #{channel} 에 보드를 올릴 권한이 없습니다 "
              "('메시지 보내기' · '링크 첨부' 확인).", flush=True)
        return

    buckets = await get_biome_buckets(guild.id, 90)
    embed = build_biome_board_embed(guild, buckets, settings)

    lock = _board_locks.setdefault(guild.id, asyncio.Lock())
    async with lock:
        # 잠금 안에서 **다시 읽습니다.** 기다리는 사이 다른 요청이 보드를
        # 만들었을 수 있는데, 캐시된 값을 쓰면 두 개가 생깁니다.
        fresh = await get_linkguard_settings(guild.id)
        message_id = fresh.get("biome_board_message_id")

        if message_id:
            try:
                msg = await channel.fetch_message(int(message_id))
                await msg.edit(embed=embed)
                return
            except discord.NotFound:
                pass          # 누가 지웠습니다 — 아래에서 다시 올립니다
            except discord.Forbidden:
                print(f"[LinkGuard] #{channel} 보드 수정 권한이 없습니다.", flush=True)
                return
            except Exception as e:
                print(f"[LinkGuard] 바이옴 보드 수정 실패: {type(e).__name__}: {e}",
                      flush=True)
                return

        try:
            sent = await channel.send(embed=embed)
        except Exception as e:
            print(f"[LinkGuard] 바이옴 보드 게시 실패: {type(e).__name__}: {e}",
                  flush=True)
            return
        await save_linkguard_settings(guild.id,
                                      {"biome_board_message_id": sent.id})


def schedule_board_refresh(guild: discord.Guild, settings: dict) -> None:
    """보드 갱신을 예약합니다 (디바운스 + 트레일링).

    보호는 시간당 수백 건입니다. 매번 고치면 디스코드 편집 한도에 닿고,
    그렇다고 창 안의 것을 버리면 **마지막 등장이 반영되지 않은 채** 끝납니다.
    """
    if not settings.get("biome_board_channel_id"):
        return

    now = time.monotonic()
    last = _last_board_refresh.get(guild.id, 0.0)
    wait = BOARD_REFRESH_INTERVAL - (now - last)

    if wait <= 0:
        _last_board_refresh[guild.id] = now
        asyncio.create_task(_board_runner(guild, settings, 0))
        return

    if guild.id in _board_trailing:
        return
    _board_trailing[guild.id] = asyncio.create_task(
        _board_runner(guild, settings, wait))


async def _board_runner(guild: discord.Guild, settings: dict, delay: float) -> None:
    try:
        if delay > 0:
            await asyncio.sleep(delay)
            _last_board_refresh[guild.id] = time.monotonic()
        await _apply_board_refresh(guild, settings)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        print(f"[LinkGuard] 바이옴 보드 갱신 오류: {type(e).__name__}: {e}", flush=True)
    finally:
        if delay > 0:
            _board_trailing.pop(guild.id, None)


# ── Cog ───────────────────────────────────────────────────────────────

class LinkGuard(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        # 같은 메시지를 두 번 처리하지 않도록 최근 처리분을 기억합니다
        self._handled: set[int] = set()
        self.board_refresh.start()

    def cog_unload(self):
        self.board_refresh.cancel()

    # 바이옴이 안 떠도 **날짜가 바뀌면 '오늘' 이 0 이 되어야** 합니다.
    # 등장할 때만 갱신하면 자정 넘어 조용한 날 어제 숫자가 그대로 남습니다.
    # 화면에 "1분마다 갱신" 이라고 적혀 있으므로 주기도 1분이어야 합니다 —
    # 문구와 실제가 어긋나면 그 자체가 거짓 정보입니다.
    @tasks.loop(minutes=1)
    async def board_refresh(self):
        for guild in self.bot.guilds:
            try:
                settings = await get_linkguard_settings(guild.id)
                if settings.get("biome_board_channel_id"):
                    await _apply_board_refresh(guild, settings)
            except Exception as e:
                print(f"[LinkGuard] {guild.name}: 바이옴 보드 주기 갱신 실패 - "
                      f"{type(e).__name__}: {e}", flush=True)

    @board_refresh.before_loop
    async def before_board_refresh(self):
        await self.bot.wait_until_ready()

    # 주기 작업은 예상 못한 예외가 **한 번** 나면 그대로 영구히 멈춥니다.
    # 재시도도 알림도 없고 프로세스는 멀쩡해 호스팅도 감지하지 못합니다.
    @board_refresh.error
    async def board_refresh_error(self, error):
        await health.revive_loop(self.board_refresh, "board_refresh", error)

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
        # 보호 제외 채널: 원본을 그대로 두지만 **유출 감지 색인에는 넣습니다.**
        # 건너뛰면 이 채널에만 올라온 링크는 다른 데로 새도 못 알아봅니다.
        # (`_detect_leak` 은 웹훅 메시지를 무시하므로 이 채널의 원본이
        #  스스로 유출로 잡히는 일은 없습니다.)
        if is_no_guard_channel(message.channel, settings):
            await self._index_without_guarding(message, settings)
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

        # 원본이 울리던 알림을 그대로 살립니다.
        # **매크로가 희귀 바이옴을 역할 멘션으로 알리는 채널이 있습니다**
        # (본문이 `<@&역할>` + 링크 한 줄인 형태). 재게시하면서 이게 빠지면
        # 사람들은 바이옴이 뜬 걸 아예 모릅니다.
        #
        # 라이브러리 기본값에 맡기지 않고 **명시**합니다. 지금은 기본값이
        # 멘션을 허용하지만, 나중에 누가 Bot(allowed_mentions=...) 를 주는
        # 순간 이 알림이 조용히 죽습니다.
        #
        # 그런데 **원본에 멘션이 아예 없는 채널이 있습니다** — MultiScope 는
        # 임베드와 링크 버튼만 보냅니다. 살릴 것이 없으니 사람들은 바이옴이
        # 뜬 것을 모릅니다. 그래서 아래 두 갈래를 더 얹습니다.
        #   ② 임베드 **안**에 적힌 멘션을 본문으로 끌어올리기
        #      (임베드 안의 `<@&역할>` 은 글자로만 보이고 알림이 안 갑니다)
        #   ③ 설정한 규칙대로 봇이 직접 붙이기 (채널·바이옴별)
        add_roles: list[int] = []
        add_everyone = add_here = False

        if settings.get("carry_embed_mentions", True):
            e_roles, e_everyone, e_here = extract_embed_mentions(message)
            add_roles.extend(e_roles)
            add_everyone = add_everyone or e_everyone
            add_here = add_here or e_here

        haystack = biome_haystack(message)

        # 바이옴 **종료** 임베드에는 붙이지 않습니다. 같은 바이옴 이름이
        # 들어 있어서, 이게 없으면 뜰 때와 끝날 때 두 번 울립니다.
        excluded = notify_excluded(settings, haystack)

        rule, matched_biome = match_notify_rule(settings, message.channel, haystack)
        if excluded is not None:
            rule = None
            add_roles, add_everyone, add_here = [], False, False
        if rule is not None:
            kind = rule.get("mention", "roles")
            if kind == "everyone":
                add_everyone = True
            elif kind == "here":
                add_here = True
            for rid in rule.get("role_ids") or []:
                if rid not in add_roles:
                    add_roles.append(rid)

        # 본문에 이미 있는 멘션은 두 번 적지 않습니다.
        existing = clean_content or ""
        bits: list[str] = []
        if add_everyone and not EVERYONE_RE.search(existing):
            bits.append("@everyone")
        elif add_here and not add_everyone and not HERE_RE.search(existing):
            bits.append("@here")
        for rid in add_roles:
            token = f"<@&{rid}>"
            if token not in existing:
                bits.append(token)

        if bits:
            line = " ".join(bits)
            clean_content = f"{line}\n{existing}" if existing.strip() else line

        allowed = discord.AllowedMentions(
            everyone=bool(message.mention_everyone or add_everyone or add_here),
            roles=_merge_role_targets(message.role_mentions, add_roles) or False,
            # 유저 멘션까지 다시 울릴 이유는 없습니다. 원본은 이미 지워졌고,
            # 같은 사람에게 두 번 알림이 가는 것이 더 이상합니다.
            users=False,
            replied_user=False,
        )

        # 멘션이 있는데 울릴 권한이 없으면 **글자만 남고 알림은 안 갑니다.**
        # 조용히 지나가면 "왜 알림이 안 오지?" 를 아무도 못 풉니다.
        needs_perm = bool(message.mention_everyone or add_everyone or add_here) or any(
            not r.mentionable for r in message.role_mentions)
        for rid in add_roles:
            role = guild.get_role(rid)
            if role is None:
                print(
                    f"[LinkGuard] 알림 역할 {rid} 을 찾을 수 없습니다 — 역할이 "
                    "삭제된 것 같습니다. /링크보호설정 → 바이옴 알림 멘션 에서 "
                    "규칙을 고쳐 주세요.",
                    flush=True)
            elif not role.mentionable:
                needs_perm = True
        if needs_perm and perms is not None and not perms.mention_everyone:
            print(
                f"[LinkGuard] #{message.channel} 알림 멘션을 울릴 수 "
                "없습니다 — 봇에게 '@everyone, @here, 모든 역할 멘션' 권한을 "
                "주세요. 지금은 멘션 글자만 남고 알림은 가지 않습니다.",
                flush=True)

        try:
            sent = await message.channel.send(
                content=clean_content or None,
                embeds=sanitized_embeds,
                view=view,
                allowed_mentions=allowed,
            )
        except Exception as e:
            print(f"[LinkGuard] 보호 메시지 재전송 중 오류: {e}", flush=True)
            return

        # 6. 재게시된 메시지 ID를 키로 원본 링크를 저장 (버튼이 이 키로 조회합니다)
        await save_protected_link(guild.id, sent.id, all_links)

        # 6-2. 통계용 바이옴 기록. **실패해도 보호를 멈추지 않습니다** —
        #      통계는 있으면 좋은 것이고 보호는 없으면 안 되는 것입니다.
        biome_name = detect_biome_name(message)

        # 6-3. 희귀 바이옴이면 **원본 링크를 관리자 채널에 복사**합니다.
        #      실패해도 보호를 멈추지 않습니다 — 편의 기능이고, 보호는
        #      없으면 안 되는 것입니다.
        if biome_name:
            try:
                await self._copy_rare_biome(
                    guild, settings, biome_name, all_links, message)
            except Exception as e:
                print(f"[LinkGuard] 희귀 바이옴 복사 실패: "
                      f"{type(e).__name__}: {e}", flush=True)

        if biome_name:
            # ⚠️ `bits` 는 **봇이 새로 덧붙인** 멘션만 담습니다. 원본 본문에
            # 이미 `<@&역할>` 이나 `@everyone` 이 있으면 덧붙일 것이 없어
            # 비지만, 그 멘션은 clean_content 에 남아 **정상적으로 울립니다.**
            # bool(bits) 로 기록하면 알림이 갔는데도 '안 감' 으로 남아,
            # 통계의 존재 이유인 "떴는데 알림이 안 나갔다" 판정이 거짓말이 됩니다.
            alerted = bool(bits or message.role_mentions or message.mention_everyone)
            try:
                await record_biome_event(
                    guild.id, biome_name,
                    link_key(all_links[0]) if all_links else "",
                    message.channel.id, sent.id, alerted)
                schedule_board_refresh(guild, settings)
            except Exception as e:
                print(f"[LinkGuard] 바이옴 기록 실패: {type(e).__name__}: {e}", flush=True)
        else:
            # **못 읽은 제목을 그냥 버리지 않습니다.** 여기까지 왔다는 것은
            # 개인서버 링크가 든 매크로 알림이라는 뜻인데 이름을 못 뽑았다면,
            # 그 매크로가 올리는 바이옴은 그때부터 통계에서 통째로 빠집니다.
            # 세어 두지 않으면 **비어 있다는 사실조차 아무도 모릅니다**
            # (MultiScope v0.9.8 이 제목 배치를 바꿔 실제로 그랬습니다).
            unknown = unparsed_biome_title(message)
            if unknown:
                try:
                    await record_biome_unparsed(
                        guild.id,
                        unknown if _take_unparsed_sample(guild.id, unknown) else "")
                except Exception as e:
                    print(f"[LinkGuard] 미인식 제목 기록 실패: "
                          f"{type(e).__name__}: {e}", flush=True)

        # 7. 유출 감시 색인에 등록 (이 링크가 서버 어딘가에 다시 뜨면 잡아냅니다)
        await register_guarded_keys(guild.id, [k for k in (link_key(u) for u in all_links) if k])

        # 8. 로그 임베드는 여기서 만들지 않습니다.
        #    임베드는 10분마다 새로 뜨지만 실제로 눌리는 것은 극소수라, 보호할 때마다
        #    올리면 로그 채널이 '0명 사용' 알림으로 도배됩니다. 대신 **첫 클릭 시점**에
        #    ensure_usage_log() 가 만들고, 이후로는 그 임베드에 인원을 누적합니다.

    async def _copy_rare_biome(self, guild, settings: dict, biome: str,
                               links: list[str], message) -> None:
        """희귀 바이옴이면 원본 링크를 관리자 채널로 복사합니다.

        **어떤 바이옴이 희귀한지는 알림 규칙을 그대로 씁니다**
        (`rare_biome_names`). 목록을 따로 두면 둘이 어긋나서, 알림은 가는데
        복사는 안 되는(또는 그 반대인) 상태를 아무도 못 알아챕니다.
        """
        cid = settings.get("rare_copy_channel_id")
        if not cid:
            return
        rare = rare_biome_names(settings)
        if not rare or biome_matches(rare, biome) is None:
            return

        # 개인서버 링크를 먼저 씁니다 (sort_links_by_priority 가 앞으로 보냄)
        link = next((u for u in links if is_roblox_private_server(u)),
                    links[0] if links else None)
        if not link:
            return
        window = int(settings.get("rare_copy_dedup_minutes", 0) or 0) * 60
        if not should_copy_rare(link, biome, window):
            return                      # 같은 (링크+바이옴) 이 방금 갔습니다

        channel = guild.get_channel(int(cid))
        if channel is None:
            print(f"[LinkGuard] 희귀 복사 채널({cid})을 찾을 수 없습니다.",
                  flush=True)
            return
        me = guild.me
        perms = channel.permissions_for(me) if me else None
        if perms is not None and not perms.send_messages:
            # 조용히 지나가면 "왜 안 오지?" 를 아무도 못 풉니다.
            print(f"[LinkGuard] #{channel} 에 '메시지 보내기' 권한이 없어 "
                  "희귀 바이옴 복사를 건너뜁니다.", flush=True)
            return

        src = getattr(message.channel, "mention", None) or f"#{message.channel}"
        when = getattr(message, "created_at", None)
        ts = when.timestamp() if when else time.time()
        await channel.send(
            embed=build_rare_copy_embed(biome, link, src, ts),
            # 관리자 전용 채널이라도 **멘션은 울리지 않습니다.**
            # 매크로 채널에서 이미 전원 호출이 나갔습니다.
            allowed_mentions=discord.AllowedMentions.none())

    async def _index_without_guarding(self, message: discord.Message,
                                      settings: dict) -> None:
        """보호는 하지 않고 **유출 감지 색인만** 남깁니다.

        보호 제외 채널의 링크도 어딘가로 새면 알아야 합니다. 색인을
        빼면 그 링크만 감지에서 사라지는데, 그건 조용한 구멍입니다.
        실패해도 조용히 넘어갑니다 — 여기서 막으면 원본까지 못 보게 됩니다.
        """
        try:
            only_roblox = settings.get("roblox_only", True)
            locked = settings.get("locked_text", "")
            links: list[str] = []
            # 보호할 때와 **같은 경로**로 모읍니다 — 여기서만 다르게 훑으면
            # 어느 한쪽이 놓치는 링크가 생깁니다.
            for embed in message.embeds:
                _, got = sanitize_embed(embed, locked, only_roblox)
                links.extend(got)
            _, got = strip_links(message.content or "", locked, only_roblox)
            links.extend(got)
            buttons = extract_component_links(message)
            if only_roblox:
                buttons = [u for u in buttons if is_roblox_link(u)]
            links.extend(buttons)
            keys = [k for k in (link_key(u) for u in links) if k]
            if keys:
                await register_guarded_keys(message.guild.id, keys)
        except Exception as e:
            print(f"[LinkGuard] 보호 제외 채널 색인 실패: "
                  f"{type(e).__name__}: {e}", flush=True)

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
        if not content:
            return

        settings = await get_linkguard_settings(guild.id)
        if not settings.get("enabled", True):
            return

        # 중계 주소는 판정 근거가 달라(발급 기록으로 소유자가 특정됩니다)
        # 별도 경로로 처리합니다.
        # **예외 채널에서도 이것만은 봅니다.** `/j/` 는 한 사람에게만
        # 발급된 개인 주소라 어느 채널에서든 올라올 정당한 이유가 없습니다.
        # 여기까지 면제하면 그 채널이 그대로 유출 통로가 됩니다.
        relay = relay_tokens_in(content)
        if relay:
            if not _already_reported(message.id):
                await self._report_relay_leak(message, settings, relay)
            return

        # 사람이 자기 개인서버를 정상적으로 공유하는 채널은 여기서 빠집니다.
        base = resolve_base_channel(message.channel)
        if base:
            exc_ids = settings.get("exception_channel_ids", [])
            if base.id in exc_ids or (getattr(base, "category_id", None) in exc_ids):
                return

        # 빠른 탈출: URL도 없고 워터마크도 없으면, 보호된 개인서버 코드(bare code)가
        # 본문에 포함되어 있는지 확인합니다. 셋 다 없으면 즉시 종료합니다.
        lowered = content.lower()
        has_url = "http://" in lowered or "https://" in lowered
        has_mark = "#hbh-" in lowered or "hbh-" in lowered

        guarded = await get_guarded_keys(guild.id)

        bare_code = None
        if not has_url and not has_mark:
            for key in guarded:
                code = key.split(":", 1)[-1]
                if len(code) >= 8 and code in lowered:
                    bare_code = code
                    break
            if not bare_code:
                return

        found_urls = [clean_url(u) for u in URL_RE.findall(content)]

        leaked = [u for u in found_urls if link_key(u) in guarded]

        # URL 형태가 아니어도 잡습니다.
        #  · 개인서버 코드만 복사해 전달한 경우 (예: "코드 c26ae66d… 이거 써")
        #  · 워터마크 코드만 남은 경우
        if not leaked and not bare_code:
            for key in guarded:
                code = key.split(":", 1)[-1]
                if len(code) >= 8 and code in lowered:
                    bare_code = code
                    break

        mark_only = not leaked and not bare_code and WATERMARK_RE.search(content)
        if not leaked and not bare_code and not mark_only:
            return
        # 수정할 때마다 같은 유출을 다시 알리지 않습니다
        if _already_reported(message.id):
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

    async def _report_relay_leak(self, message: discord.Message,
                                 settings: dict, tokens: list) -> None:
        """중계 주소가 그대로 게시된 경우를 기록합니다.

        **게시자와 발급 대상을 구분합니다.** 남의 주소를 올렸으면 유출이
        분명하지만, 자기 주소를 올린 것은 "이거 눌러도 안 돼요" 하고
        문의하다 붙여넣은 것일 수 있습니다. 둘을 같은 제목·같은 색으로
        올리면 관리자가 문의한 사람을 유출자로 오해합니다.

        **여러 개를 한꺼번에 뿌리는 쪽이 더 위험합니다** — 남의 주소를
        모아 둔 사람이니까요. 첫 번째만 보고 넘어가면 그 사실이 묻힙니다.
        """
        guild = message.guild
        poster = str(message.author.id)

        owners: list[str] = []                 # 발급 대상 (중복 없이, 순서 유지)
        owner_names: dict[str, str] = {}
        unknown = 0

        for token in tokens[:MAX_RELAY_TOKENS]:
            owner_id = ""
            # 아직 살아있는 토큰이면 메모리에서 바로 나옵니다(소비하지 않습니다).
            alive = link_tokens.peek_owner(token)
            if alive and alive[0] == guild.id:
                owner_id = str(alive[1])
            if not owner_id:
                # 토큰은 90초면 죽지만 발급 기록은 30일 남습니다.
                found = await find_issued_to(guild.id, token)
                if found:
                    owner_id = str(found.get("user_id") or "")
                    if owner_id and found.get("user_name"):
                        owner_names[owner_id] = found["user_name"]
            if not owner_id:
                unknown += 1
                continue
            if owner_id not in owners:
                owners.append(owner_id)

        others = [o for o in owners if o != poster]
        by_owner = bool(owners) and not others

        if not owners:
            verdict = ("발급 기록을 찾지 못했습니다 — 30일이 지났거나 "
                       "이 서버에서 발급한 주소가 아닙니다.")
        elif by_owner:
            verdict = ("**본인에게 발급된 주소**입니다.\n"
                       "남에게 넘기려던 것일 수도, 안 열린다고 문의하다 "
                       "붙여넣은 것일 수도 있습니다. 확인해 주세요.")
        else:
            who = ", ".join(
                f"<@{o}>" + (f" (**{owner_names[o]}**)" if owner_names.get(o) else "")
                for o in others)
            verdict = (f"**다른 사람에게 발급된 주소**입니다 — {who}.\n"
                       "게시자가 그 사람에게 받았거나, 그 사람이 넘긴 것입니다.")
            if len(others) > 1:
                verdict = (f"🚨 **{len(others)}명분을 모아** 뿌렸습니다.\n" + verdict)

        deleted = False
        if settings.get("auto_delete_leak", True):
            try:
                await message.delete()
                deleted = True
            except Exception:
                pass

        # 토큰 전체를 로그에 남기지 않습니다. 아직 살아 있을 수 있고,
        # 남겨 봐야 소유자는 이미 위에서 특정했습니다.
        await add_click_log(
            guild.id, message.author.id, message.author.display_name,
            f"(중계 주소 {len(tokens)}개) /j/{tokens[0][:8]}…",
            event_type="leak",
            detail=(f"#{message.channel} 채널에 중계 주소 게시 / "
                    + ("본인 주소" if by_owner
                       else f"발급 대상 {len(others)}명" if others
                       else "발급 기록 없음")),
        )

        embed = discord.Embed(
            title="⚠️ 개인 전용 주소가 공개됨" if by_owner else "🚨 중계 주소 유출",
            description=(f"{message.author.mention} 님이 "
                         f"**{message.channel.mention}** 채널에 "
                         "중계 주소를 그대로 게시했습니다."),
            color=discord.Color.orange() if by_owner else discord.Color.red()
        )
        embed.add_field(
            name="🙋 게시한 유저",
            value=f"**{message.author.display_name}**\n`{message.author.id}`",
            inline=True
        )
        if owners:
            listed = " ".join(f"<@{o}>" for o in owners[:10])
            if unknown:
                listed += f"\n-# 기록을 못 찾은 주소 {unknown}개"
            embed.add_field(name=f"🎫 발급 대상 ({len(owners)}명)",
                            value=listed, inline=True)
        else:
            embed.add_field(name="🎫 발급 대상", value="기록 없음", inline=True)
        if len(tokens) > 1:
            embed.add_field(name="🔗 게시된 주소 수",
                            value=f"**{len(tokens)}개**", inline=True)
        embed.add_field(name="🧭 판정", value=verdict, inline=False)
        embed.add_field(
            name="🧹 조치",
            value="✅ 메시지를 자동 삭제했습니다" if deleted
                  else "⚠️ 메시지를 삭제하지 못했습니다 (권한 확인 필요)",
            inline=False
        )
        embed.set_footer(text="이 주소는 1회용이라 이미 만료됐을 수 있습니다.")
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
        if not before.embeds and after.embeds:
            try:
                await self._guard_message(after)
            except Exception as e:
                print(f"[LinkGuard] on_message_edit 처리 오류: {e}", flush=True)

        # **보낸 뒤 고쳐서 링크를 끼워 넣는 회피를 막습니다.**
        # 깨끗한 글을 올리면 on_message 는 그냥 지나가고, 그 뒤 수정해도
        # 다시 불리지 않습니다. 수정본도 같은 눈으로 봐야 합니다.
        try:
            await self._detect_leak(after)
        except Exception as e:
            print(f"[LinkGuard] 유출 감지(수정) 오류: {e}", flush=True)

    @app_commands.command(name="바이옴통계",
                          description="[관리자 전용] 어떤 바이옴이 몇 번 떴고 알림이 나갔는지 확인합니다.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def biome_stats(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        buckets = await get_biome_buckets(interaction.guild.id, 90)
        settings = await get_linkguard_settings(interaction.guild.id)
        unparsed = await get_biome_unparsed(interaction.guild.id, 1)
        await interaction.followup.send(
            embed=build_biome_stats_embed(interaction.guild, buckets, settings, 1,
                                          unparsed),
            view=BiomeStatsView(1), ephemeral=True)

    @app_commands.command(name="링크보호설정", description="[관리자 전용] 웹훅 임베드의 링크를 인증 유저 전용으로 보호합니다.")
    # DM 에서는 interaction.guild 가 None 이라 AttributeError 로 죽습니다.
    # (실제로 /출석 이 그렇게 터졌습니다). guild_only 를 걸면 디스코드가
    # **DM 목록에 아예 띄우지 않으므로** 방어 코드를 흩뿌릴 필요가 없습니다.
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def linkguard_settings(self, interaction: discord.Interaction):
        # 3초 제한을 넘기지 않도록 **먼저 응답을 예약**합니다. 아래에서 DB 를
        # 읽는데, 몰리는 순간 그 왕복이 3초를 넘기면 10062 로 명령어가 통째로
        # 죽습니다(실측: /포인트랭킹 이 실제로 그렇게 실패했습니다).
        await interaction.response.defer(ephemeral=True, thinking=True)
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
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)


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
