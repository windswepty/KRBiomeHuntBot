import re
import discord
from discord.ext import commands
from discord import app_commands
from utils.database import add_user_points, get_report_settings, save_report_settings, add_activity_log


def parse_channel_mention(raw_input: str) -> str:
    raw_input = raw_input.strip()
    if not raw_input:
        return raw_input

    # 1. https://discord.com/channels/서버ID/채널ID 형태 파싱
    match = re.search(r"channels/\d+/(\d+)", raw_input)
    if match:
        return f"<#{match.group(1)}>"

    # 2. 이미 <#123456789> 채널 멘션 형태인 경우
    if re.match(r"^<#\d+>$", raw_input):
        return raw_input

    # 3. 17~20자리 숫자 ID만 포함된 경우
    digits = re.findall(r"\d{17,20}", raw_input)
    if digits:
        return f"<#{digits[0]}>"

    return raw_input


# ── 제보 안내 임베드 꾸미기 ────────────────────────────────────────────
#
# `/제보` 로 나가는 안내 임베드를 운영진이 직접 꾸밉니다. 문구 한 줄 바꾸려고
# 개발자를 부르지 않아도 되게 하는 것이 목적입니다(퀘스트·출석 설정과 같은 이유).
#
# 저장값이 None 이면 아래 기본값을 씁니다. 그래서 기존 서버는 아무것도
# 건드리지 않아도 예전과 똑같이 보입니다.

DEFAULT_EMBED_TITLE = "🎫 희귀 바이옴 제보 안내"
DEFAULT_EMBED_DESC = (
    "해당 제보는 티켓을 통해 희귀바이옴을 인증하면 포인트를 주는 시스템입니다.\n\n"
    "이 티켓은 매크로로 희귀바이옴을 생성한 시점부터 하루내에 사용해야 "
    "포인트를 지급해드립니다.\n\n"
    "⚠️ **장난식으로 티켓사용 시 제재를 당할 수 있습니다.**\n\n"
    "아래 **[티켓 열기]** 버튼을 눌러 제출 양식을 작성해 주세요."
)
DEFAULT_BUTTON_LABEL = "티켓 열기"

# 디스코드가 정한 상한입니다. 넘기면 전송이 400 으로 실패하는데, 그때는
# 유저에게 "상호작용 실패"만 뜨고 왜인지 아무 단서도 남지 않습니다.
# 그래서 저장하는 입력 단계에서 막습니다.
LIMIT_TITLE = 256
LIMIT_DESC = 4096
LIMIT_FOOTER = 2048
LIMIT_BUTTON = 80

COLOR_NAMES = {
    "파랑": 0x3498DB, "파란색": 0x3498DB, "블루": 0x3498DB,
    "빨강": 0xE74C3C, "빨간색": 0xE74C3C, "레드": 0xE74C3C,
    "초록": 0x2ECC71, "초록색": 0x2ECC71, "그린": 0x2ECC71,
    "노랑": 0xF1C40F, "노란색": 0xF1C40F,
    "주황": 0xE67E22, "주황색": 0xE67E22,
    "보라": 0x9B59B6, "보라색": 0x9B59B6,
    "분홍": 0xE91E63, "핑크": 0xE91E63,
    "청록": 0x1ABC9C, "하늘": 0x5DADE2, "하늘색": 0x5DADE2,
    "회색": 0x95A5A6, "검정": 0x2C3E50, "검은색": 0x2C3E50,
    "흰색": 0xECF0F1, "하양": 0xECF0F1,
    "금색": 0xF39C12, "골드": 0xF39C12,
}


def parse_color(raw: str):
    """색상 입력을 discord.Color 로. 못 알아보면 None 을 돌려줍니다.

    운영진에게 16진수만 요구하면 매번 검색해야 합니다. 한글 색이름을
    먼저 받고, 그다음 `#5865F2` / `5865F2` 를 받습니다.
    """
    raw = (raw or "").strip()
    if not raw:
        return None
    if raw in COLOR_NAMES:
        return discord.Color(COLOR_NAMES[raw])
    m = re.fullmatch(r"#?([0-9a-fA-F]{6})", raw)
    if m:
        return discord.Color(int(m.group(1), 16))
    return None


def valid_image_url(raw: str) -> bool:
    """디스코드가 실제로 불러올 수 있는 주소인지. 아니면 임베드가 통째로 안 나갑니다."""
    raw = (raw or "").strip()
    return bool(re.match(r"^https?://\S+$", raw))


# 채널 링크만 잡습니다. 뒤에 메시지 ID 가 더 붙는 '메시지 링크' 는 제외해야
# 하므로 숫자·슬래시가 이어지지 않는 경우만 매칭합니다.
_CHANNEL_LINK = re.compile(
    r"https?://(?:ptb\.|canary\.)?discord(?:app)?\.com/channels/(\d+)/(\d+)(?![\d/])"
)


def beautify_links(text: str, guild_id: int) -> str:
    """붙여넣은 디스코드 채널 링크를 채널 멘션으로 바꿉니다.

    운영진이 채널 링크를 복사해 넣으면 긴 주소가 그대로 보입니다.
    `<#채널ID>` 로 바꿔 두면 디스코드가 **채널 이름 그대로** 렌더링합니다 —
    `║💬║대화` 처럼 꾸며 놓은 이름도 그대로 나오고, 눌러서 이동도 됩니다.
    채널 이름을 나중에 바꿔도 자동으로 따라갑니다.

    건드리지 않는 것:
      - 메시지 링크(뒤에 메시지 ID가 더 붙는 형태) — 특정 메시지를 가리키려고
        넣은 것이라 채널 멘션으로 바꾸면 뜻이 달라집니다
      - 다른 서버의 링크 — 멘션으로 바꿔도 렌더링되지 않습니다
      - 외부 주소 — 그건 `[보일 글자](주소)` 로 직접 꾸미시면 됩니다
    """
    if not text:
        return text

    def _sub(m):
        if m.group(1) != str(guild_id):
            return m.group(0)
        return f"<#{m.group(2)}>"

    return _CHANNEL_LINK.sub(_sub, text)


def build_report_embed(settings: dict, is_enabled: bool, guild_id: int) -> discord.Embed:
    """`/제보` 안내 임베드.

    명령어와 미리보기가 **같은 함수**를 씁니다. 따로 두면 한쪽만 고쳐져
    미리보기와 실제가 어긋나고, 그건 미리보기가 없느니만 못합니다.
    """
    title = (settings.get("embed_title") or DEFAULT_EMBED_TITLE)
    desc = settings.get("embed_description") or DEFAULT_EMBED_DESC
    desc = beautify_links(desc, guild_id)

    if is_enabled:
        color = parse_color(settings.get("embed_color") or "") or discord.Color.blue()
    else:
        # 비활성 상태에서는 꾸밈보다 '지금 못 쓴다'가 먼저 보여야 합니다.
        title = f"[비활성화 상태] {title}"
        color = discord.Color.red()

    embed = discord.Embed(
        title=title[:LIMIT_TITLE],
        description=desc[:LIMIT_DESC],
        color=color,
    )

    footer = settings.get("embed_footer")
    if footer:
        embed.set_footer(text=footer[:LIMIT_FOOTER])

    image = settings.get("embed_image")
    if image and valid_image_url(image):
        embed.set_image(url=image)

    thumb = settings.get("embed_thumbnail")
    if thumb and valid_image_url(thumb):
        embed.set_thumbnail(url=thumb)

    return embed


def build_settings_embed(guild, settings: dict) -> discord.Embed:
    """`/제보설정` 패널.

    명령어와 갱신이 같은 함수를 써야 합니다. 따로 두면 한쪽만 고쳐져
    '켰는데 꺼짐으로 보이는' 상태가 다시 생깁니다.
    """
    enabled = settings.get("enabled", True)

    role_id = settings.get("notify_role_id")
    if role_id:
        role = guild.get_role(role_id)
        role_text = role.mention if role else f"❓ (삭제된 역할: {role_id})"
    else:
        role_text = "설정 안 됨"

    channel_id = settings.get("report_channel_id")
    if channel_id:
        ch = guild.get_channel(channel_id)
        channel_text = f"**{ch.name}**" if ch else f"❓ (삭제된 카테고리: {channel_id})"
    else:
        channel_text = "설정 안 됨"

    custom = [k for k in ("embed_title", "embed_description", "embed_color",
                          "embed_footer", "embed_image", "embed_thumbnail",
                          "button_label") if settings.get(k)]
    deco_text = f"✏️ {len(custom)}개 항목 변경됨" if custom else "기본값 사용 중"

    embed = discord.Embed(
        title="⚙️ 바이옴 제보 시스템 설정",
        description="아래 드롭다운 메뉴에서 변경할 설정 항목을 선택하세요.",
        color=discord.Color.dark_gold(),
    )
    embed.add_field(name="⚡ 시스템 상태",
                    value="✅ 활성화" if enabled else "❌ 비활성화", inline=True)
    embed.add_field(name="💰 기본 지급 포인트",
                    value=f"**{settings.get('default_points', 500):,}** P", inline=True)
    embed.add_field(name="🎨 안내 임베드", value=deco_text, inline=True)
    embed.add_field(name="🔔 알림 역할", value=role_text, inline=False)
    embed.add_field(name="📌 제보 카테고리", value=channel_text, inline=False)
    embed.set_footer(text="설정은 이 서버에만 적용됩니다.")
    return embed


# 꾸미기에 해당하는 설정 키. "꾸미기만 초기화" 가 이 목록만 지웁니다.
DECOR_KEYS = ("embed_title", "embed_description", "embed_color",
              "embed_footer", "embed_image", "embed_thumbnail", "button_label")


def build_decor_help_embed() -> discord.Embed:
    """꾸미기 문법 안내. 운영진이 매번 물어보지 않아도 되게 합니다."""
    embed = discord.Embed(
        title="❓ 안내 임베드 꾸미기 방법",
        description="**설명** 칸에서 아래 문법을 쓸 수 있습니다.",
        color=discord.Color.blurple())
    embed.add_field(
        name="채널을 예쁘게 보이게",
        value=("채널 링크를 그대로 붙여넣으세요. 자동으로 채널 이름이 됩니다."
               + chr(10) + "```" + chr(10)
               + "https://discord.com/channels/123.../456..." + chr(10)
               + "```" + chr(10)
               + "→ **║💬║대화** 처럼 채널 이름 그대로 보이고 눌러서 이동됩니다."
               + chr(10) + "채널 이름을 나중에 바꿔도 자동으로 따라갑니다."),
        inline=False)
    embed.add_field(
        name="바깥 주소를 글자로 감추기",
        value=("```" + chr(10) + "[공략 보러가기](https://example.com)" + chr(10)
               + "```" + chr(10) + "→ 주소는 숨고 **공략 보러가기** 글자만 보입니다."),
        inline=False)
    embed.add_field(
        name="글자 꾸미기",
        value=("```" + chr(10) + "**굵게**  *기울임*  __밑줄__  ~~취소선~~" + chr(10)
               + "> 인용문" + chr(10) + "```"),
        inline=False)
    embed.add_field(
        name="색상",
        value=("파랑 · 빨강 · 초록 · 노랑 · 주황 · 보라 · 분홍 · 청록 · 하늘 ·"
               " 회색 · 검정 · 흰색 · 금색" + chr(10)
               + "또는 `#5865F2` 형태의 색상코드"),
        inline=False)
    embed.set_footer(text="칸을 비우면 그 항목은 기본값으로 돌아갑니다.")
    return embed


async def refresh_panel(interaction: discord.Interaction, notice: str = None,
                        preview: discord.Embed = None,
                        preview_view: discord.ui.View = None):
    """패널을 다시 그리고, 안내는 followup 으로 따로 보냅니다.

    **순서가 중요합니다.** `send_message()` 를 먼저 하면 그게 이 상호작용의
    '원본 응답'이 되어, 뒤이은 `edit_original_response()` 가 패널이 아니라
    방금 보낸 안내 메시지를 고칩니다. 패널은 옛 값을 붙든 채 남고, 드롭다운
    설명이 'View 를 만들 때 값'으로 굳어 켰는데도 '꺼짐'으로 보입니다.

    그래서 **먼저 패널을 고치고(edit_message), 안내는 followup** 으로 냅니다.
    모달 제출도 자기를 띄운 메시지를 이렇게 고칠 수 있습니다.
    """
    settings = await get_report_settings(interaction.guild.id)
    panel = build_settings_embed(interaction.guild, settings)
    panel_view = ReportSettingsView(settings)

    try:
        if interaction.response.is_done():
            await interaction.edit_original_response(embed=panel, view=panel_view)
        else:
            await interaction.response.edit_message(embed=panel, view=panel_view)
    except discord.HTTPException:
        # 패널 메시지가 이미 사라졌을 수 있습니다. 안내는 계속 보내야 하므로
        # 응답만 확보하고 넘어갑니다.
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)

    if notice is None and preview is None:
        return
    kwargs = {"ephemeral": True}
    if notice is not None:
        kwargs["content"] = notice
    if preview is not None:
        kwargs["embed"] = preview
    if preview_view is not None:
        kwargs["view"] = preview_view
    await interaction.followup.send(**kwargs)


# ── Modals ────────────────────────────────────────────────────────────



class ReportModal(discord.ui.Modal, title="티켓을 생성하기전 2차 절차 ( 희귀 바이옴 제보 티켓 )"):
    channel_link = discord.ui.TextInput(
        label="희귀바이옴을 띄운 본인의 채널링크를 붙여넣어주세요!",
        placeholder="https://discord.com/channels/1325960789462618182/1329788098187624500",
        style=discord.TextStyle.short,
        required=True
    )

    rules_agree = discord.ui.TextInput(
        label="서버 및 티켓규칙을 모두 확인했나요?",
        placeholder="확인했다면 '확인' 이라고 입력해주세요!",
        style=discord.TextStyle.short,
        required=True
    )

    async def on_submit(self, interaction: discord.Interaction):
        user = interaction.user
        guild = interaction.guild

        # 설정 불러오기
        settings = await get_report_settings(guild.id)

        # 카테고리 결정
        category_id = settings.get("report_channel_id")
        category = None
        if category_id:
            category = guild.get_channel(category_id)
            if category and not isinstance(category, discord.CategoryChannel):
                category = None

        if not category:
            await interaction.response.send_message(
                "⚠️ 제보 카테고리가 설정되지 않았습니다. 관리자에게 `/제보설정`으로 카테고리를 설정해달라고 요청해 주세요.",
                ephemeral=True
            )
            return

        # 이미 열려 있는 티켓이 있으면 그리로 안내합니다.
        # 없으면 제출할 때마다 채널이 하나씩 생기는데, 6,500명이 누를 수 있는
        # 버튼이라 길드 채널 상한(500개)까지 밀어 넣을 수 있습니다.
        # 권한 덮어쓰기로 찾으므로 닉네임이 바뀌어도 정확히 걸립니다.
        for existing in category.text_channels:
            if existing.overwrites_for(user).view_channel:
                await interaction.response.send_message(
                    f"⚠️ 이미 열려 있는 제보가 있습니다 → {existing.mention}\n"
                    "-# 그 채널에 이어서 적어 주세요. 처리되면 자동으로 닫힙니다.",
                    ephemeral=True)
                return

        # 비공개 채널 권한 설정 (제보자 + 관리자만 볼 수 있음)
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            user: discord.PermissionOverwrite(view_channel=True, send_messages=True, attach_files=True),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True),
        }

        # 티켓 채널 생성
        channel_name = f"🎫-제보-{user.name}"
        try:
            ticket_channel = await guild.create_text_channel(
                name=channel_name,
                category=category,
                overwrites=overwrites
            )
        except Exception as e:
            await interaction.response.send_message(f"⚠️ 티켓 채널을 생성하지 못했습니다: {e}", ephemeral=True)
            return

        # 알림 역할 멘션 생성
        notify_role_id = settings.get("notify_role_id")
        if notify_role_id:
            role = guild.get_role(notify_role_id)
            role_mention = role.mention if role else ""
        else:
            role_mention = ""

        welcome_msg = (
            f"{user.mention} 님 안녕하세요! 용건을 보내주시고 관리자분들이 오실때까지 조금만 기다려주세요!\n"
            f"{role_mention}"
        ).strip()
        await ticket_channel.send(welcome_msg)

        # 티켓 임베드
        channel_mention_str = parse_channel_mention(self.channel_link.value)
        embed = discord.Embed(
            title="🎫 희귀 바이옴 제보 티켓",
            description="해당티켓은 희귀 바이옴 인증 및 포인트 적립을 위해 생성되었습니다!\n"
                        "본인의 용건이 끝났다하더라도 티켓을 닫지말아주세요! 관리자가 알아서 닫아줍니다!\n\n"
                        "⚠️ **티켓이 열리면 즉시 희귀 바이옴 인증 스크린샷을 첨부해 주세요!**",
            color=discord.Color.green()
        )
        embed.add_field(name="👤 제보자", value=user.mention, inline=True)
        embed.add_field(name="🔗 희귀 바이옴 채널 링크", value=channel_mention_str, inline=False)
        embed.add_field(name="📝 규칙 동의 답변", value=self.rules_agree.value, inline=False)
        embed.set_footer(text="Vinceticket / KRBiomeHuntBot • Ticket System")

        view = TicketControlView(target_user_id=user.id)
        await ticket_channel.send(embed=embed, view=view)

        await interaction.response.send_message(
            f"✅ **희귀 바이옴 제보 티켓이 생성되었습니다!**\n👉 {ticket_channel.mention} 채널로 이동하여 인증 스크린샷을 첨부해 주세요.",
            ephemeral=True
        )


# ── Persistent Views ─────────────────────────────────────────────────

class TicketControlView(discord.ui.View):
    def __init__(self, target_user_id: int = None):
        super().__init__(timeout=None)
        if target_user_id:
            self.close_btn.custom_id = f"ticket_close:{target_user_id}"
            self.give_points_btn.custom_id = f"ticket_give_points:{target_user_id}"

    @discord.ui.button(label="티켓 닫기", style=discord.ButtonStyle.danger, emoji="❌", custom_id="ticket_close_default")
    async def close_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("⚠️ 이 버튼은 관리자만 사용할 수 있습니다.", ephemeral=True)
            return

        cid = button.custom_id
        target_user_id = None
        if ":" in cid:
            try:
                target_user_id = int(cid.split(":")[1])
            except ValueError:
                pass

        # 1. 이미 '티켓 삭제' 버튼 상태인 경우 -> 3초 후 채널 삭제
        if button.label == "티켓 삭제" or cid.startswith("ticket_delete"):
            await interaction.response.send_message("🔒 3초 후 이 티켓 채널을 완전히 삭제합니다...")
            import asyncio
            await asyncio.sleep(3)
            try:
                await interaction.channel.delete(reason=f"티켓 삭제 - 관리자: {interaction.user.name}")
            except Exception as e:
                await interaction.followup.send(f"⚠️ 채널 삭제에 실패했습니다: {e}", ephemeral=True)
            return

        # 2. '티켓 닫기' 상태인 경우 -> 유저를 채널 권한에서 제외 및 채널 이름 변경
        target_mention = "제보 유저"
        user_name = "유저"
        if target_user_id:
            target_user = interaction.guild.get_member(target_user_id)
            if target_user:
                target_mention = target_user.mention
                user_name = target_user.name
                await interaction.channel.set_permissions(target_user, view_channel=False)

        # 채널 이름 변경 (🔒-닫힘-아이디)
        new_channel_name = f"🔒-닫힘-{user_name}"
        try:
            await interaction.channel.edit(name=new_channel_name)
        except Exception as e:
            print(f"Failed to rename ticket channel: {e}", flush=True)

        await interaction.response.send_message("🔒 제보 유저를 채널에서 제외하고 티켓을 닫았습니다.", ephemeral=True)
        await interaction.channel.send(f"🔒 {target_mention} 님이 티켓 채널에서 제외되었습니다.")

        # 버튼을 '티켓 삭제' 상태로 변경
        button.label = "티켓 삭제"
        button.emoji = "🗑️"
        button.style = discord.ButtonStyle.danger
        if target_user_id:
            button.custom_id = f"ticket_delete:{target_user_id}"

        await interaction.message.edit(view=button.view)

    @discord.ui.button(label="포인트 지급", style=discord.ButtonStyle.success, emoji="💰", custom_id="ticket_give_points_default")
    async def give_points_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("⚠️ 이 버튼은 관리자만 사용할 수 있습니다.", ephemeral=True)
            return

        cid = button.custom_id
        target_user_id = None
        if ":" in cid:
            try:
                target_user_id = int(cid.split(":")[1])
            except ValueError:
                pass

        if not target_user_id:
            await interaction.response.send_message("⚠️ 제보자 유저 정보를 찾을 수 없습니다.", ephemeral=True)
            return

        # 설정에서 기본 지급 포인트 불러오기
        settings = await get_report_settings(interaction.guild.id)
        amount = settings.get("default_points", 500)

        # DB에 포인트 추가 및 로그 기록
        new_total = await add_user_points(interaction.guild.id, target_user_id, amount)
        target_user = interaction.guild.get_member(target_user_id)
        target_mention = target_user.mention if target_user else f"<@{target_user_id}>"
        target_name = target_user.display_name if target_user else "제보자"

        await add_activity_log(
            interaction.guild.id,
            "report",
            target_user_id,
            target_name,
            f"희귀 바이옴 제보 보상 +{amount:,}P 지급 (담당 관리자: {interaction.user.display_name})"
        )

        # Ephemeral 응답 보내기
        await interaction.response.send_message(f"✅ {target_mention}님에게 {amount:,}포인트를 성공적으로 지급했습니다.", ephemeral=True)

        # 티켓 채널에 성공 임베드 메시지 전송
        embed = discord.Embed(
            title="🎁 희귀 바이옴 제보 보상 지급 완료",
            description=f"관리자 {interaction.user.mention} 님이 {target_mention} 님에게 **+{amount:,} P**를 지급했습니다!\n\n"
                        f"💰 **{target_name}** 님의 현재 총 포인트: **{new_total:,} P**",
            color=discord.Color.green()
        )
        await interaction.channel.send(embed=embed)

        # 버튼 상태 업데이트 (포인트 중복 지급 방지)
        button.disabled = True
        button.label = f"포인트 지급 완료 (+{amount:,}P)"
        button.style = discord.ButtonStyle.secondary
        await interaction.message.edit(view=button.view)


class TicketLaunchView(discord.ui.View):
    def __init__(self, label: str = None, preview_only: bool = False):
        super().__init__(timeout=None)
        # 문구는 바꿔도 custom_id 는 고정입니다. 영구 View 는 custom_id 로
        # 짝을 찾으므로, 문구를 바꿔도 재시작 후 버튼이 계속 동작합니다.
        if label:
            self.open_ticket_button.label = label[:LIMIT_BUTTON]
        if preview_only:
            # 미리보기에서 누르면 진짜 티켓이 열립니다. 눌리지 않게 잠급니다.
            self.open_ticket_button.disabled = True

    @discord.ui.button(label="티켓 열기", style=discord.ButtonStyle.primary, emoji="🎫", custom_id="report_ticket_open")
    async def open_ticket_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        # 시스템 활성화 여부 확인 (관리자는 비활성화 상태여도 이용 가능)
        settings = await get_report_settings(interaction.guild.id)
        is_admin = interaction.user.guild_permissions.administrator
        if not settings.get("enabled", True) and not is_admin:
            await interaction.response.send_message(
                "⚠️ 현재 바이옴 제보 시스템이 **비활성화** 상태입니다. 관리자에게 문의해 주세요.",
                ephemeral=True
            )
            return
        await interaction.response.send_modal(ReportModal())


# ── /제보설정 Settings Select View ────────────────────────────────────

class ReportSettingsSelect(discord.ui.Select):
    def __init__(self, current_settings: dict):
        self.current_settings = current_settings
        options = [
            discord.SelectOption(
                label="시스템 활성화 / 비활성화",
                value="toggle",
                description=f"현재: {'✅ 활성화' if current_settings.get('enabled', True) else '❌ 비활성화'}",
                emoji="⚡"
            ),
            discord.SelectOption(
                label="기본 지급 포인트 설정",
                value="default_points",
                description=f"현재: {current_settings.get('default_points', 500):,}P",
                emoji="💰"
            ),
            discord.SelectOption(
                label="알림 역할 설정",
                value="notify_role",
                description="티켓 생성 시 멘션할 역할을 지정합니다",
                emoji="🔔"
            ),
            discord.SelectOption(
                label="제보 카테고리 설정",
                value="report_channel",
                description="티켓 채널이 생성될 카테고리를 지정합니다",
                emoji="📌"
            ),
            discord.SelectOption(
                label="안내 임베드 꾸미기",
                value="decor",
                description="제목 · 설명 · 색상 · 푸터 · 버튼 문구",
                emoji="🎨"
            ),
            discord.SelectOption(
                label="임베드 이미지",
                value="decor_image",
                description="큰 이미지 · 작은 썸네일",
                emoji="🖼️"
            ),
            discord.SelectOption(
                label="미리보기",
                value="preview",
                description="유저에게 보이는 그대로 확인합니다",
                emoji="👀"
            ),
            discord.SelectOption(
                label="꾸미기 도움말",
                value="decor_help",
                description="채널 링크 · 바깥 주소 · 글자 꾸미기 방법",
                emoji="❓"
            ),
            discord.SelectOption(
                label="꾸미기만 초기화",
                value="decor_reset",
                description="문구·색상·이미지만 기본값으로 (나머지 설정 유지)",
                emoji="↩️"
            ),
            discord.SelectOption(
                label="현재 설정 초기화",
                value="reset",
                description="모든 제보 설정을 기본값으로 되돌립니다",
                emoji="🔄"
            ),
        ]
        super().__init__(placeholder="변경할 설정 항목을 선택하세요...", options=options)

    async def callback(self, interaction: discord.Interaction):
        choice = self.values[0]

        if choice == "toggle":
            new_val = not self.current_settings.get("enabled", True)
            await save_report_settings(interaction.guild.id, {"enabled": new_val})
            status = "✅ 활성화" if new_val else "❌ 비활성화"
            await refresh_panel(
                interaction,
                notice=f"⚡ 제보 시스템이 **{status}** 상태로 변경되었습니다.")

        elif choice == "default_points":
            await interaction.response.send_modal(
                DefaultPointsModal(self.current_settings))

        elif choice == "notify_role":
            await interaction.response.send_modal(
                NotifyRoleModal(self.current_settings))

        elif choice == "report_channel":
            await interaction.response.send_modal(
                ReportChannelModal(self.current_settings))

        elif choice == "decor":
            await interaction.response.send_modal(
                EmbedDecorModal(self.current_settings))

        elif choice == "decor_image":
            await interaction.response.send_modal(
                EmbedImageModal(self.current_settings))

        elif choice == "preview":
            # 저장된 값을 다시 읽습니다. self.current_settings 는 패널을 띄운
            # 시점의 값이라, 그 뒤에 바꾼 내용이 미리보기에 안 나옵니다.
            settings = await get_report_settings(interaction.guild.id)
            preview = build_report_embed(settings, settings.get("enabled", True),
                                         interaction.guild.id)
            await refresh_panel(
                interaction,
                notice=("👀 아래가 `/제보` 에서 유저에게 보이는 모습입니다."
                        " 버튼은 미리보기라 잠겨 있습니다."),
                preview=preview,
                preview_view=TicketLaunchView(settings.get("button_label"),
                                              preview_only=True))

        elif choice == "decor_help":
            await refresh_panel(interaction, preview=build_decor_help_embed())

        elif choice == "decor_reset":
            await save_report_settings(interaction.guild.id,
                                       {k: None for k in DECOR_KEYS})
            await refresh_panel(
                interaction,
                notice=("↩️ 안내 임베드를 기본 모습으로 되돌렸습니다."
                        " 포인트·역할·카테고리 설정은 그대로입니다."))

        elif choice == "reset":
            from utils.database import DEFAULT_REPORT_SETTINGS
            await save_report_settings(interaction.guild.id,
                                       dict(DEFAULT_REPORT_SETTINGS))
            await refresh_panel(
                interaction,
                notice=("🔄 모든 제보 설정이 기본값으로 초기화되었습니다."
                        " (시스템 활성화 · 500P · 역할 없음 · 카테고리 없음 ·"
                        " 임베드 기본 모습)"))


class ReportSettingsView(discord.ui.View):
    def __init__(self, current_settings: dict):
        super().__init__(timeout=180)
        self.add_item(ReportSettingsSelect(current_settings))


# ── Settings Modals ───────────────────────────────────────────────────

class DefaultPointsModal(discord.ui.Modal, title="💰 기본 지급 포인트 설정"):
    points_input = discord.ui.TextInput(
        label="제보 승인 시 기본으로 지급할 포인트 수량",
        placeholder="예: 500",
        required=True,
        max_length=10
    )

    def __init__(self, settings: dict):
        super().__init__()
        # 지금 값을 채워 둡니다. 늘 500 으로 열리면, 다른 값을 쓰던 서버가
        # 확인만 하려고 열었다가 저장해도 500 으로 되돌아갑니다.
        self.points_input.default = str(settings.get("default_points", 500))

    async def on_submit(self, interaction: discord.Interaction):
        try:
            amount = int(self.points_input.value.strip())
            if amount <= 0:
                raise ValueError()
        except ValueError:
            await interaction.response.send_message(
                "⚠️ 1 이상의 올바른 숫자를 입력해 주세요.", ephemeral=True)
            return

        await save_report_settings(interaction.guild.id, {"default_points": amount})
        await refresh_panel(
            interaction,
            notice=f"💰 제보 승인 시 기본 지급 포인트가 **{amount:,} P** 로 바뀌었습니다.")


class NotifyRoleModal(discord.ui.Modal, title="🔔 알림 역할 설정"):
    role_input = discord.ui.TextInput(
        label="멘션할 역할의 ID를 입력하세요 (제거: 0 입력)",
        placeholder="예: 1234567890123456789  (역할 ID)",
        required=True,
        max_length=25
    )

    def __init__(self, settings: dict):
        super().__init__()
        rid = settings.get("notify_role_id")
        self.role_input.default = str(rid) if rid else None

    async def on_submit(self, interaction: discord.Interaction):
        raw = self.role_input.value.strip()

        if raw == "0":
            await save_report_settings(interaction.guild.id, {"notify_role_id": None})
            await refresh_panel(
                interaction,
                notice="🔔 티켓 생성 시 알림 역할 멘션이 **해제**되었습니다.")
            return

        # 역할 멘션(<@&123>)을 그대로 붙여넣는 경우가 많습니다. 숫자만 뽑습니다.
        digits = re.findall(r"[0-9]{17,20}", raw)
        if not digits:
            await interaction.response.send_message(
                "⚠️ 올바른 역할 ID(숫자)를 입력해 주세요. 역할을 멘션해서 "
                "붙여넣으셔도 됩니다.", ephemeral=True)
            return
        role_id = int(digits[0])

        role = interaction.guild.get_role(role_id)
        if not role:
            await interaction.response.send_message(
                f"⚠️ ID `{role_id}` 에 해당하는 역할을 찾을 수 없습니다.", ephemeral=True)
            return

        await save_report_settings(interaction.guild.id, {"notify_role_id": role_id})
        await refresh_panel(
            interaction,
            notice=f"🔔 티켓 생성 시 {role.mention} 역할이 멘션됩니다.")


class ReportChannelModal(discord.ui.Modal, title="📌 제보 카테고리 설정"):
    channel_input = discord.ui.TextInput(
        label="티켓 채널이 생성될 카테고리 ID를 입력하세요 (제거: 0)",
        placeholder="예: 1234567890123456789  (카테고리 ID)",
        required=True,
        max_length=25
    )

    def __init__(self, settings: dict):
        super().__init__()
        cid = settings.get("report_channel_id")
        self.channel_input.default = str(cid) if cid else None

    async def on_submit(self, interaction: discord.Interaction):
        raw = self.channel_input.value.strip()

        if raw == "0":
            await save_report_settings(interaction.guild.id,
                                       {"report_channel_id": None})
            await refresh_panel(
                interaction,
                notice=("📌 제보 카테고리가 **해제**되었습니다."
                        " 다시 설정해야 티켓을 생성할 수 있습니다."))
            return

        digits = re.findall(r"[0-9]{17,20}", raw)
        if not digits:
            await interaction.response.send_message(
                "⚠️ 올바른 카테고리 ID(숫자)를 입력해 주세요.", ephemeral=True)
            return
        channel_id = int(digits[0])

        category = interaction.guild.get_channel(channel_id)
        if not category or not isinstance(category, discord.CategoryChannel):
            await interaction.response.send_message(
                f"⚠️ ID `{channel_id}` 에 해당하는 카테고리를 찾을 수 없습니다."
                " **채널이 아니라 카테고리** ID 를 입력해 주세요.", ephemeral=True)
            return

        await save_report_settings(interaction.guild.id,
                                   {"report_channel_id": channel_id})
        await refresh_panel(
            interaction,
            notice=f"📌 이제 제보 티켓이 **{category.name}** 카테고리 안에 생성됩니다.")


class EmbedDecorModal(discord.ui.Modal, title="🎨 제보 안내 임베드 꾸미기"):
    """`/제보` 안내 임베드의 글자 부분을 한 번에 고칩니다.

    **칸을 비우면 그 항목은 기본값으로 돌아갑니다.** '비움 = 유지' 로 하면
    한 번 넣은 문구를 다시는 지울 수 없게 되어, 지우려면 개발자를 불러야 합니다.
    """

    title_input = discord.ui.TextInput(
        label="제목",
        placeholder="비우면 기본값: 🎫 희귀 바이옴 제보 안내",
        required=False,
        max_length=LIMIT_TITLE,
    )
    desc_input = discord.ui.TextInput(
        label="설명 (채널 링크를 붙여넣으면 채널 이름으로 보입니다)",
        placeholder=("채널 링크를 그대로 붙여넣으면 ║💬║대화 처럼 채널 이름으로 바뀝니다.\n"
                     "바깥 주소는 [보일 글자](주소) 로 쓰면 글자만 보입니다.\n"
                     "**굵게** *기울임* 도 됩니다. 비우면 기본 안내문으로 돌아갑니다."),
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=4000,
    )
    color_input = discord.ui.TextInput(
        label="색상",
        placeholder="파랑 / 빨강 / 초록 / 보라 … 또는 #5865F2",
        required=False,
        max_length=20,
    )
    footer_input = discord.ui.TextInput(
        label="아래 작은 글씨 (푸터)",
        placeholder="비우면 표시하지 않습니다",
        required=False,
        max_length=LIMIT_FOOTER,
    )
    button_input = discord.ui.TextInput(
        label="버튼에 적힐 글자",
        placeholder="비우면 기본값: 티켓 열기",
        required=False,
        max_length=LIMIT_BUTTON,
    )

    def __init__(self, settings: dict):
        super().__init__()
        # 지금 값을 채워 둡니다. 빈 칸으로 열리면 "비우면 기본값" 규칙 때문에
        # 열었다 닫기만 해도 설정이 날아갑니다.
        self.title_input.default = settings.get("embed_title") or None
        self.desc_input.default = settings.get("embed_description") or None
        self.color_input.default = settings.get("embed_color") or None
        self.footer_input.default = settings.get("embed_footer") or None
        self.button_input.default = settings.get("button_label") or None

    async def on_submit(self, interaction: discord.Interaction):
        color_raw = self.color_input.value.strip()
        if color_raw and parse_color(color_raw) is None:
            await interaction.response.send_message(
                f"⚠️ 색상 `{color_raw}` 을 알아볼 수 없습니다.\n"
                "**파랑 · 빨강 · 초록 · 노랑 · 주황 · 보라 · 분홍 · 청록 · "
                "하늘 · 회색 · 검정 · 흰색 · 금색** 중 하나이거나 "
                "`#5865F2` 형태의 색상코드를 입력해 주세요.",
                ephemeral=True)
            return

        # 빈 문자열이 아니라 None 으로 저장합니다. 빈 문자열을 넣으면
        # `or 기본값` 판정에는 걸리지만 '변경됨' 개수에는 잡혀서,
        # 패널이 바꾼 적 없는 항목을 바뀐 것으로 셉니다.
        patch = {
            "embed_title": self.title_input.value.strip() or None,
            "embed_description": self.desc_input.value.strip() or None,
            "embed_color": color_raw or None,
            "embed_footer": self.footer_input.value.strip() or None,
            "button_label": self.button_input.value.strip() or None,
        }
        await save_report_settings(interaction.guild.id, patch)

        settings = await get_report_settings(interaction.guild.id)
        preview = build_report_embed(settings, settings.get("enabled", True),
                                     interaction.guild.id)
        await refresh_panel(
            interaction,
            notice=("🎨 **저장했습니다.** 아래가 `/제보` 에서 실제로 "
                    "보이는 모습입니다."),
            preview=preview,
            preview_view=TicketLaunchView(settings.get("button_label"),
                                          preview_only=True))


class EmbedImageModal(discord.ui.Modal, title="🖼️ 제보 임베드 이미지"):
    """큰 이미지와 작은 썸네일. 비우면 표시하지 않습니다."""

    image_input = discord.ui.TextInput(
        label="큰 이미지 주소 (임베드 아래 넓게)",
        placeholder="https://... (비우면 표시 안 함)",
        required=False,
        max_length=500,
    )
    thumb_input = discord.ui.TextInput(
        label="작은 썸네일 주소 (오른쪽 위 구석)",
        placeholder="https://... (비우면 표시 안 함)",
        required=False,
        max_length=500,
    )

    def __init__(self, settings: dict):
        super().__init__()
        self.image_input.default = settings.get("embed_image") or None
        self.thumb_input.default = settings.get("embed_thumbnail") or None

    async def on_submit(self, interaction: discord.Interaction):
        image = self.image_input.value.strip()
        thumb = self.thumb_input.value.strip()

        # 주소가 잘못되면 임베드가 통째로 전송에 실패합니다. 유저에게는
        # "상호작용 실패"만 뜨고 원인은 아무 데도 남지 않습니다.
        for label, url in (("큰 이미지", image), ("썸네일", thumb)):
            if url and not valid_image_url(url):
                await interaction.response.send_message(
                    f"⚠️ {label} 주소가 올바르지 않습니다.\n"
                    "`http://` 또는 `https://` 로 시작하는 주소를 넣어 주세요.",
                    ephemeral=True)
                return

        await save_report_settings(interaction.guild.id, {
            "embed_image": image or None,
            "embed_thumbnail": thumb or None,
        })

        settings = await get_report_settings(interaction.guild.id)
        preview = build_report_embed(settings, settings.get("enabled", True),
                                     interaction.guild.id)
        await refresh_panel(
            interaction,
            notice=("🖼️ **저장했습니다.** 아래가 `/제보` 에서 실제로 보이는 "
                    "모습입니다." + chr(10) + "이미지가 안 보이면 주소가 "
                    "이미지 파일을 직접 가리키는지 확인해 주세요(`.png` `.jpg` 등)."),
            preview=preview,
            preview_view=TicketLaunchView(settings.get("button_label"),
                                          preview_only=True))


# ── Cog ───────────────────────────────────────────────────────────────

class Report(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(
        name="제보",
        description="희귀 바이옴 제보 안내를 확인하고 제보 티켓을 생성합니다.")
    async def report_biome(self, interaction: discord.Interaction):
        settings = await get_report_settings(interaction.guild.id)
        is_enabled = settings.get("enabled", True)
        is_admin = interaction.user.guild_permissions.administrator

        # 관리자는 비활성 상태에서도 확인할 수 있어야 합니다. 켜기 전에
        # 어떻게 보이는지 봐야 하니까요.
        if not is_enabled and not is_admin:
            await interaction.response.send_message(
                "⚠️ 현재 바이옴 제보 시스템이 **비활성화** 상태입니다."
                " 관리자에게 문의해 주세요.", ephemeral=True)
            return

        # 미리보기와 **같은 함수**를 씁니다. 따로 두면 한쪽만 고쳐져
        # 미리보기가 실제와 어긋나고, 그러면 미리보기가 없느니만 못합니다.
        embed = build_report_embed(settings, is_enabled, interaction.guild.id)
        view = TicketLaunchView(settings.get("button_label"))
        await interaction.response.send_message(embed=embed, view=view,
                                                ephemeral=True)

    @app_commands.command(
        name="제보설정",
        description="[관리자 전용] 바이옴 제보 시스템의 설정을 관리합니다.")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def report_settings(self, interaction: discord.Interaction):
        settings = await get_report_settings(interaction.guild.id)
        await interaction.response.send_message(
            embed=build_settings_embed(interaction.guild, settings),
            view=ReportSettingsView(settings),
            ephemeral=True)


async def setup(bot):
    cog = Report(bot)
    bot.add_view(TicketLaunchView())
    bot.add_view(TicketControlView())
    await bot.add_cog(cog)
