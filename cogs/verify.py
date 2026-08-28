"""
서버 인증 시스템 (독립 모듈).

포인트/상점/뽑기/제보 시스템과 전혀 연동되지 않습니다.
인증 통과 시 지정된 역할만 부여하며, 활동 로그에도 기록하지 않습니다.
"""

import re
import discord
from discord.ext import commands
from discord import app_commands
from utils.verify_db import (
    get_verify_settings, save_verify_settings,
    get_linkguard_settings, save_linkguard_settings,
)

# 봇 재시작 후에도 버튼이 살아있도록 고정 custom_id 사용
VERIFY_BUTTON_ID = "hbh_verify_panel:click"


def parse_role_id(raw_input: str) -> int | None:
    """원시 입력에서 역할 ID를 추출합니다 (숫자 ID 또는 역할 멘션 지원)."""
    if not raw_input:
        return None
    match = re.search(r'\d{17,20}', raw_input.strip())
    if match:
        try:
            return int(match.group(0))
        except ValueError:
            return None
    return None


async def is_verified(guild: discord.Guild, member: discord.Member) -> bool:
    """멤버가 인증 역할을 보유하고 있는지 확인합니다."""
    if member is None:
        return False
    settings = await get_verify_settings(guild.id)
    role_id = settings.get("role_id")
    if not role_id:
        return False
    return any(r.id == int(role_id) for r in getattr(member, "roles", []))


# ── 인증 패널 (영구 View) ─────────────────────────────────────────────

def build_panel_embed(settings: dict, guild: discord.Guild) -> discord.Embed:
    """인증 패널 임베드를 만듭니다.

    인증 자격(필요 역할)이 지정돼 있으면 함께 보여줍니다. 조건을 모른 채
    버튼을 눌렀다가 거부당하는 것보다, 미리 알려주는 편이 낫습니다.
    """
    embed = discord.Embed(
        title=settings.get("title", "✅ 서버 인증"),
        description=settings.get("description", ""),
        color=discord.Color.green()
    )

    required = [int(r) for r in settings.get("required_role_ids", [])]
    if required:
        roles = [guild.get_role(rid) for rid in required]
        names = " · ".join(r.mention for r in roles if r)
        if names:
            embed.add_field(
                name="🎫 인증 자격",
                value=f"아래 역할 중 **하나 이상**을 보유해야 인증할 수 있습니다.\n{names}",
                inline=False
            )
        if settings.get("auto_revoke", False):
            embed.add_field(
                name="♻️ 자격 유지",
                value="위 역할을 **모두 잃으면** 인증이 자동으로 해제됩니다.",
                inline=False
            )

    embed.set_footer(text="한바헌 서버 인증 시스템")
    return embed


class VerifyPanelView(discord.ui.View):
    def __init__(self, button_label: str = "인증하기"):
        super().__init__(timeout=None)
        self.verify_btn.label = button_label

    @discord.ui.button(label="인증하기", style=discord.ButtonStyle.success, emoji="✅", custom_id=VERIFY_BUTTON_ID)
    async def verify_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild
        if guild is None:
            return

        settings = await get_verify_settings(guild.id)

        if not settings.get("enabled", True):
            await interaction.response.send_message(
                "⚠️ 현재 인증 시스템이 비활성화되어 있습니다. 관리자에게 문의해 주세요.",
                ephemeral=True
            )
            return

        role_id = settings.get("role_id")
        if not role_id:
            await interaction.response.send_message(
                "⚠️ 인증 역할이 아직 설정되지 않았습니다. 관리자에게 `/인증설정`으로 역할을 지정해 달라고 요청해 주세요.",
                ephemeral=True
            )
            return

        role = guild.get_role(int(role_id))
        if role is None:
            await interaction.response.send_message(
                "⚠️ 설정된 인증 역할을 서버에서 찾을 수 없습니다. 관리자에게 문의해 주세요.",
                ephemeral=True
            )
            return

        member = interaction.user

        # 인증 자격 검사 — 지정된 역할 중 하나라도 갖고 있어야 합니다.
        # 비어 있으면 조건 없이 통과합니다(기존 동작).
        required = [int(r) for r in settings.get("required_role_ids", [])]
        if required and not any(r.id in required for r in member.roles):
            have = [guild.get_role(rid) for rid in required]
            names = " · ".join(r.mention for r in have if r) or "관리자에게 문의"
            embed = discord.Embed(
                title="🔒 인증 자격이 없습니다",
                description="이 서버의 인증은 **아래 역할 중 하나**를 보유한 분만 신청할 수 있습니다.",
                color=discord.Color.red()
            )
            embed.add_field(name="🎫 필요한 역할", value=names, inline=False)
            embed.set_footer(text="역할을 받는 방법은 관리자에게 문의해 주세요")
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        if any(r.id == role.id for r in member.roles):
            await interaction.response.send_message(
                f"ℹ️ 이미 인증이 완료된 상태입니다! (보유 역할: {role.mention})",
                ephemeral=True
            )
            return

        try:
            await member.add_roles(role, reason="서버 인증 버튼 통과")
        except discord.Forbidden:
            await interaction.response.send_message(
                "⚠️ 봇에게 역할을 부여할 권한이 없습니다.\n"
                "서버 설정에서 **봇의 역할이 인증 역할보다 위에 있는지**, "
                "그리고 **역할 관리 권한**이 있는지 확인해 주세요.",
                ephemeral=True
            )
            return
        except Exception as e:
            await interaction.response.send_message(f"⚠️ 인증 처리 중 오류가 발생했습니다: {e}", ephemeral=True)
            return

        embed = discord.Embed(
            title="✅ 인증이 완료되었습니다!",
            description=f"{member.mention} 님에게 {role.mention} 역할이 부여되었습니다.\n\n"
                        "이제 보호된 링크를 정상적으로 열람하실 수 있습니다.",
            color=discord.Color.green()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


# ── 관리자 설정 UI ────────────────────────────────────────────────────

class VerifyRoleModal(discord.ui.Modal, title="🎭 인증 역할 설정"):
    role_input = discord.ui.TextInput(
        label="인증 통과 시 부여할 역할의 ID를 입력해 주세요!",
        placeholder="예: 1329788098187624500",
        required=True,
        max_length=50
    )

    async def on_submit(self, interaction: discord.Interaction):
        role_id = parse_role_id(self.role_input.value)
        if not role_id:
            await interaction.response.send_message("⚠️ 올바른 역할 ID 숫자를 입력해 주세요.", ephemeral=True)
            return

        role = interaction.guild.get_role(role_id)
        if role is None:
            await interaction.response.send_message(
                "⚠️ 해당 ID의 역할을 이 서버에서 찾을 수 없습니다. ID를 다시 확인해 주세요.",
                ephemeral=True
            )
            return

        await save_verify_settings(interaction.guild.id, {"role_id": role_id})

        warn = ""
        me = interaction.guild.me
        if me and role >= me.top_role:
            warn = ("\n\n⚠️ **주의**: 이 역할이 봇의 역할보다 위에 있어 부여에 실패할 수 있습니다.\n"
                    "서버 설정 → 역할에서 **봇 역할을 인증 역할보다 위로** 올려 주세요.")

        embed = discord.Embed(
            title="✅ 인증 역할 설정 완료",
            description=f"인증 통과 시 {role.mention} 역할이 부여됩니다.{warn}",
            color=discord.Color.green() if not warn else discord.Color.orange()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


class VerifyTextModal(discord.ui.Modal, title="📝 인증 패널 문구 설정"):
    def __init__(self, current: dict):
        super().__init__()
        self.title_input = discord.ui.TextInput(
            label="인증 패널 제목",
            default=current.get("title", "✅ 서버 인증"),
            required=True,
            max_length=100
        )
        self.desc_input = discord.ui.TextInput(
            label="인증 패널 안내 문구",
            style=discord.TextStyle.paragraph,
            default=current.get("description", ""),
            required=True,
            max_length=1000
        )
        self.label_input = discord.ui.TextInput(
            label="인증 버튼에 표시할 문구",
            default=current.get("button_label", "인증하기"),
            required=True,
            max_length=40
        )
        self.add_item(self.title_input)
        self.add_item(self.desc_input)
        self.add_item(self.label_input)

    async def on_submit(self, interaction: discord.Interaction):
        await save_verify_settings(interaction.guild.id, {
            "title": self.title_input.value.strip(),
            "description": self.desc_input.value.strip(),
            "button_label": self.label_input.value.strip(),
        })
        embed = discord.Embed(
            title="✅ 인증 패널 문구 저장 완료",
            description="변경된 문구는 **새로 전송하는 인증 패널**부터 적용됩니다.\n"
                        "`/인증설정` → **인증 패널 전송**으로 다시 보내 주세요.",
            color=discord.Color.green()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


class PanelChannelSelect(discord.ui.ChannelSelect):
    def __init__(self):
        super().__init__(
            placeholder="인증 패널을 전송할 채널을 선택하세요...",
            channel_types=[discord.ChannelType.text]
        )

    async def callback(self, interaction: discord.Interaction):
        channel = interaction.guild.get_channel(self.values[0].id)
        if channel is None:
            await interaction.response.send_message("⚠️ 채널을 찾을 수 없습니다.", ephemeral=True)
            return

        settings = await get_verify_settings(interaction.guild.id)

        embed = build_panel_embed(settings, interaction.guild)

        view = VerifyPanelView(button_label=settings.get("button_label", "인증하기"))

        try:
            await channel.send(embed=embed, view=view)
        except discord.Forbidden:
            await interaction.response.send_message(
                f"⚠️ {channel.mention} 채널에 메시지를 보낼 권한이 없습니다.",
                ephemeral=True
            )
            return

        await save_verify_settings(interaction.guild.id, {"panel_channel_id": channel.id})
        await interaction.response.send_message(
            f"✅ {channel.mention} 채널에 인증 패널을 전송했습니다.",
            ephemeral=True
        )


class PanelChannelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)
        self.add_item(PanelChannelSelect())


class RequiredRoleIdModal(discord.ui.Modal, title="🆔 인증 필요 역할 추가"):
    role_input = discord.ui.TextInput(
        label="역할 ID를 입력하세요 (여러 개 가능)",
        placeholder="예: 1329788098187624500, 1329788098187624501",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=600
    )

    async def on_submit(self, interaction: discord.Interaction):
        guild = interaction.guild
        settings = await get_verify_settings(guild.id)
        current = [int(r) for r in settings.get("required_role_ids", [])]

        found, missing, dupes = [], [], []
        for raw in re.findall(r"[0-9]{17,20}", self.role_input.value):
            rid = int(raw)
            role = guild.get_role(rid)
            if role is None:
                missing.append(raw)
            elif rid in current:
                dupes.append(role)
            else:
                current.append(rid)
                found.append(role)

        if found:
            await save_verify_settings(guild.id, {"required_role_ids": current})

        embed = discord.Embed(
            title="🎫 인증 필요 역할 추가",
            color=discord.Color.green() if found else discord.Color.orange()
        )
        if found:
            embed.add_field(name="✅ 추가됨",
                            value="\n".join(f"• {r.mention} (`{r.id}`)" for r in found),
                            inline=False)
        if dupes:
            embed.add_field(name="ℹ️ 이미 있음",
                            value="\n".join(f"• {r.mention}" for r in dupes), inline=False)
        if missing:
            embed.add_field(
                name="⚠️ 서버에서 찾을 수 없음",
                value="\n".join(f"• `{m}`" for m in missing)
                      + "\n-# 역할 우클릭 → ID 복사 로 다시 확인해 주세요",
                inline=False)
        if not found and not dupes and not missing:
            embed.description = "입력에서 역할 ID를 찾지 못했습니다. 17~20자리 숫자를 넣어 주세요."

        embed.add_field(name="📋 현재 지정된 역할 수", value=f"**{len(current)}개**", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)


class RemoveRequiredRoleSelect(discord.ui.Select):
    def __init__(self, role_ids: list[int], guild: discord.Guild):
        options = []
        for rid in role_ids[:25]:
            role = guild.get_role(rid)
            options.append(discord.SelectOption(
                label=(f"@{role.name}" if role else f"(삭제된 역할)")[:100],
                value=str(rid),
                description=str(rid)
            ))
        if not options:
            options = [discord.SelectOption(label="지정된 역할이 없습니다", value="none")]
        super().__init__(placeholder="해제할 역할을 선택하세요...", options=options)

    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "none":
            await interaction.response.send_message("해제할 역할이 없습니다.", ephemeral=True)
            return
        guild = interaction.guild
        settings = await get_verify_settings(guild.id)
        current = [int(r) for r in settings.get("required_role_ids", [])]
        rid = int(self.values[0])
        if rid in current:
            current.remove(rid)
            await save_verify_settings(guild.id, {"required_role_ids": current})
        role = guild.get_role(rid)
        embed = discord.Embed(
            title="🎫 인증 필요 역할 해제",
            description=f"{role.mention if role else f'`{rid}`'} 을(를) 목록에서 제거했습니다.",
            color=discord.Color.orange()
        )
        embed.add_field(name="📋 남은 역할 수", value=f"**{len(current)}개**", inline=False)
        if not current:
            embed.add_field(
                name="⚠️ 주의",
                value="지정된 역할이 하나도 없어, **누구나 인증할 수 있는 상태**가 되었습니다.",
                inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)


class RequiredRoleView(discord.ui.View):
    def __init__(self, role_ids: list[int], guild: discord.Guild):
        super().__init__(timeout=180)
        self.add_item(RemoveRequiredRoleSelect(role_ids, guild))

    @discord.ui.button(label="역할 ID로 추가", style=discord.ButtonStyle.success, emoji="🆔", row=1)
    async def add_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(RequiredRoleIdModal())


class VerifySettingsSelect(discord.ui.Select):
    def __init__(self, current: dict, guild: discord.Guild):
        role_id = current.get("role_id")
        role = guild.get_role(int(role_id)) if role_id else None
        role_desc = f"현재: @{role.name}" if role else "아직 설정되지 않았습니다"
        enabled_desc = "현재: ✅ 활성화" if current.get("enabled", True) else "현재: ❌ 비활성화"

        options = [
            discord.SelectOption(
                label="시스템 활성화 / 비활성화",
                value="toggle",
                description=enabled_desc,
                emoji="⚡"
            ),
            discord.SelectOption(
                label="인증 역할 설정",
                value="role",
                description=role_desc[:95],
                emoji="🎭"
            ),
            discord.SelectOption(
                label="인증 필요 역할 관리",
                value="reqrole",
                description=(f"현재 {len(current.get('required_role_ids', []))}개 지정됨"
                             " (비어있으면 누구나 인증 가능)")[:95],
                emoji="🎫"
            ),
            discord.SelectOption(
                label="자격 상실 시 인증 자동 회수",
                value="autorevoke",
                description=("현재: ✅ 켜짐" if current.get("auto_revoke")
                             else "현재: ❌ 꺼짐"),
                emoji="♻️"
            ),
            discord.SelectOption(
                label="인증 패널 전송",
                value="panel",
                description="선택한 채널에 인증 버튼 임베드를 전송합니다.",
                emoji="📨"
            ),
            discord.SelectOption(
                label="인증 패널 문구 수정",
                value="text",
                description="패널 제목, 안내 문구, 버튼 문구를 변경합니다.",
                emoji="📝"
            ),
            discord.SelectOption(
                label="현재 설정 확인",
                value="status",
                description="인증 역할, 패널 채널, 권한 상태를 점검합니다.",
                emoji="📊"
            ),
        ]
        super().__init__(placeholder="인증 관리 기능을 선택하세요...", options=options)

    async def callback(self, interaction: discord.Interaction):
        choice = self.values[0]
        guild = interaction.guild
        settings = await get_verify_settings(guild.id)

        if choice == "reqrole":
            required = [int(r) for r in settings.get("required_role_ids", [])]
            embed = discord.Embed(
                title="🎫 인증 필요 역할 관리",
                description=("인증을 신청하려면 **아래 역할 중 하나 이상**을 갖고 있어야 합니다.\n"
                             "여러 개를 지정하면 그중 하나만 있어도 통과합니다."),
                color=discord.Color.blurple()
            )
            if required:
                lines = []
                for rid in required:
                    role = guild.get_role(rid)
                    lines.append(f"• {role.mention} (`{rid}`)" if role
                                 else f"• ⚠️ 서버에 없는 역할 (`{rid}`)")
                embed.add_field(name=f"📋 지정된 역할 {len(required)}개",
                                value="\n".join(lines)[:1024], inline=False)
            else:
                embed.add_field(
                    name="⚠️ 현재 조건 없음",
                    value="지정된 역할이 없어 **서버에 들어온 누구나 인증**할 수 있습니다.\n"
                          "링크를 노리고 방금 들어온 사람도 즉시 인증됩니다.",
                    inline=False)
            embed.set_footer(text="역할 우클릭 → ID 복사 로 ID를 얻을 수 있습니다")
            await interaction.response.send_message(
                embed=embed, view=RequiredRoleView(required, guild), ephemeral=True)

        elif choice == "autorevoke":
            required = [int(r) for r in settings.get("required_role_ids", [])]
            new_state = not settings.get("auto_revoke", False)

            if new_state and not required:
                embed = discord.Embed(
                    title="⚠️ 먼저 인증 필요 역할을 지정하세요",
                    description="필요 역할이 하나도 없으면 '자격 상실'을 판단할 수 없습니다.\n"
                                "`🎫 인증 필요 역할 관리` 에서 먼저 지정해 주세요.",
                    color=discord.Color.orange()
                )
                await interaction.response.send_message(embed=embed, ephemeral=True)
                return

            await save_verify_settings(guild.id, {"auto_revoke": new_state})
            embed = discord.Embed(
                title="♻️ 인증 자동 회수 설정 변경",
                description=f"자동 회수가 **{'✅ 켜짐' if new_state else '❌ 꺼짐'}** 상태가 되었습니다.",
                color=discord.Color.green() if new_state else discord.Color.red()
            )
            if new_state:
                names = " · ".join(
                    r.mention for r in (guild.get_role(rid) for rid in required) if r)
                embed.add_field(
                    name="🔎 이렇게 동작합니다",
                    value=f"유저가 {names} 를 **전부 잃으면**, 인증 역할을 자동으로 회수합니다.\n"
                          "역할이 다시 생기면 인증 버튼을 눌러 재인증할 수 있습니다.",
                    inline=False
                )
                embed.add_field(
                    name="⚠️ 주의",
                    value="• 필요 역할을 잘못 지정하면 **멀쩡한 인증자까지 풀립니다**\n"
                          "• 봇 역할이 인증 역할보다 **위**에 있어야 회수할 수 있습니다\n"
                          "• 회수 기록은 링크 보호 로그 채널에 남습니다",
                    inline=False
                )
            else:
                embed.add_field(
                    name="🔎 이렇게 동작합니다",
                    value="필요 역할을 잃어도 인증 역할은 **그대로 유지**됩니다.",
                    inline=False
                )
            await interaction.response.send_message(embed=embed, ephemeral=True)

        elif choice == "toggle":
            new_state = not settings.get("enabled", True)
            await save_verify_settings(guild.id, {"enabled": new_state})
            state_text = "✅ 활성화" if new_state else "❌ 비활성화"
            embed = discord.Embed(
                title="⚡ 인증 시스템 상태 변경",
                description=f"인증 시스템이 **{state_text}** 상태로 변경되었습니다.",
                color=discord.Color.green() if new_state else discord.Color.red()
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)

        elif choice == "role":
            await interaction.response.send_modal(VerifyRoleModal())

        elif choice == "panel":
            await interaction.response.send_message(
                "📨 **인증 패널을 전송할 채널을 선택하세요.**",
                view=PanelChannelView(),
                ephemeral=True
            )

        elif choice == "text":
            await interaction.response.send_modal(VerifyTextModal(settings))

        elif choice == "status":
            role_id = settings.get("role_id")
            role = guild.get_role(int(role_id)) if role_id else None
            panel_id = settings.get("panel_channel_id")
            panel = guild.get_channel(int(panel_id)) if panel_id else None

            embed = discord.Embed(
                title="📊 인증 시스템 현재 설정",
                color=discord.Color.blurple()
            )
            embed.add_field(
                name="⚡ 시스템 상태",
                value="✅ 활성화" if settings.get("enabled", True) else "❌ 비활성화",
                inline=False
            )
            embed.add_field(
                name="🎭 인증 역할",
                value=role.mention if role else "⚠️ 미설정 (인증이 동작하지 않습니다)",
                inline=False
            )
            embed.add_field(
                name="📨 최근 패널 전송 채널",
                value=panel.mention if panel else "아직 전송한 적이 없습니다",
                inline=False
            )

            me = guild.me
            perm_lines = []
            if me:
                mark = "✅" if me.guild_permissions.manage_roles else "❌"
                perm_lines.append(f"{mark} 역할 관리 권한")
                if role:
                    mark2 = "✅" if role < me.top_role else "❌"
                    perm_lines.append(f"{mark2} 봇 역할이 인증 역할보다 위에 있음")
            embed.add_field(
                name="🔑 봇 권한 점검",
                value="\n".join(perm_lines) if perm_lines else "확인 불가",
                inline=False
            )
            embed.set_footer(text="한바헌 서버 인증 시스템")

            await interaction.response.send_message(embed=embed, ephemeral=True)


class VerifySettingsView(discord.ui.View):
    def __init__(self, current: dict, guild: discord.Guild):
        super().__init__(timeout=180)
        self.add_item(VerifySettingsSelect(current, guild))


# ── 시스템 전원 스위치 ────────────────────────────────────────────────
#
# 인증 · 링크 보호를 즉시 켜고 끄는 비상 스위치입니다.
# 끄면 봇은 웹훅 임베드를 건드리지 않고 인증 버튼도 응답하지 않아,
# 기능이 아예 없는 상태와 동일하게 동작합니다.

def build_power_embed(verify_settings: dict, guard_settings: dict, guild: discord.Guild) -> discord.Embed:
    v_on = verify_settings.get("enabled", True)
    g_on = guard_settings.get("enabled", True)

    if v_on and g_on:
        state, color = "🟢 전체 가동 중", discord.Color.green()
    elif not v_on and not g_on:
        state, color = "🔴 전체 정지됨", discord.Color.red()
    else:
        state, color = "🟡 일부만 가동 중", discord.Color.orange()

    role_id = verify_settings.get("role_id")
    role = guild.get_role(int(role_id)) if role_id else None
    channel_count = len(guard_settings.get("channel_ids", []))

    embed = discord.Embed(
        title="🎛️ 인증 시스템 전원 관리",
        description=f"**현재 상태: {state}**\n\n"
                    "문제가 생기면 여기서 **즉시 정지**할 수 있습니다.\n"
                    "정지 상태에서는 봇이 웹훅 임베드를 전혀 건드리지 않고,\n"
                    "인증 버튼도 동작하지 않아 **기능이 없는 것과 같은 상태**가 됩니다.",
        color=color
    )
    embed.add_field(
        name="🔐 서버 인증",
        value=("✅ 켜짐" if v_on else "❌ 꺼짐")
              + f"\n-# 인증 역할: {role.mention if role else '⚠️ 미설정'}",
        inline=True
    )
    embed.add_field(
        name="🔗 링크 보호",
        value=("✅ 켜짐" if g_on else "❌ 꺼짐")
              + f"\n-# 보호 채널: {channel_count}개",
        inline=True
    )
    # 인증만 끄고 링크 보호를 켜두면 신규 유저가 인증을 못 받아
    # 링크에 영영 접근하지 못하는 상태가 됩니다. 명시적으로 경고합니다.
    if (not v_on) and g_on:
        embed.add_field(
            name="⚠️ 주의: 신규 유저가 잠깁니다",
            value="인증은 꺼졌는데 링크 보호가 켜져 있습니다.\n"
                  "이 상태에서는 **아직 인증받지 못한 유저가 인증을 받을 방법이 없어**\n"
                  "링크에 영영 접근하지 못합니다.\n"
                  "평상시 상태로 되돌리려면 **[🛑 전체 끄기]** 를 눌러 주세요.",
            inline=False
        )
    elif (not v_on) and (not g_on):
        embed.add_field(
            name="✅ 평상시 상태",
            value="봇이 웹훅 임베드를 건드리지 않고, 유출 감지도 동작하지 않습니다.\n"
                  "이미 치환된 과거 메시지의 버튼은 **누구나** 링크를 받을 수 있습니다.",
            inline=False
        )

    embed.set_footer(text="변경은 즉시 반영됩니다 (봇 재시작 불필요)")
    return embed


class SystemPowerView(discord.ui.View):
    def __init__(self, verify_settings: dict, guard_settings: dict):
        super().__init__(timeout=300)
        self.v_on = verify_settings.get("enabled", True)
        self.g_on = guard_settings.get("enabled", True)
        self._sync_labels()

    def _sync_labels(self):
        self.verify_btn.label = "인증 끄기" if self.v_on else "인증 켜기"
        self.verify_btn.style = discord.ButtonStyle.danger if self.v_on else discord.ButtonStyle.success
        self.guard_btn.label = "링크 보호 끄기" if self.g_on else "링크 보호 켜기"
        self.guard_btn.style = discord.ButtonStyle.danger if self.g_on else discord.ButtonStyle.success

    async def _refresh(self, interaction: discord.Interaction):
        v = await get_verify_settings(interaction.guild.id)
        g = await get_linkguard_settings(interaction.guild.id)
        self.v_on = v.get("enabled", True)
        self.g_on = g.get("enabled", True)
        self._sync_labels()
        await interaction.response.edit_message(
            embed=build_power_embed(v, g, interaction.guild), view=self
        )

    @discord.ui.button(label="인증 끄기", style=discord.ButtonStyle.danger, emoji="🔐", row=0)
    async def verify_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await save_verify_settings(interaction.guild.id, {"enabled": not self.v_on})
        await self._refresh(interaction)

    @discord.ui.button(label="링크 보호 끄기", style=discord.ButtonStyle.danger, emoji="🔗", row=0)
    async def guard_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await save_linkguard_settings(interaction.guild.id, {"enabled": not self.g_on})
        await self._refresh(interaction)

    @discord.ui.button(label="전체 켜기", style=discord.ButtonStyle.success, emoji="🟢", row=1)
    async def all_on_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await save_verify_settings(interaction.guild.id, {"enabled": True})
        await save_linkguard_settings(interaction.guild.id, {"enabled": True})
        await self._refresh(interaction)

    @discord.ui.button(label="전체 끄기 (비상 정지)", style=discord.ButtonStyle.danger, emoji="🛑", row=1)
    async def all_off_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await save_verify_settings(interaction.guild.id, {"enabled": False})
        await save_linkguard_settings(interaction.guild.id, {"enabled": False})
        await self._refresh(interaction)


# ── Cog ───────────────────────────────────────────────────────────────

class Verify(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="인증시스템", description="[관리자 전용] 인증 및 링크 보호 시스템을 즉시 켜거나 끕니다.")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def system_power(self, interaction: discord.Interaction):
        verify_settings = await get_verify_settings(interaction.guild.id)
        guard_settings = await get_linkguard_settings(interaction.guild.id)

        embed = build_power_embed(verify_settings, guard_settings, interaction.guild)
        view = SystemPowerView(verify_settings, guard_settings)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @app_commands.command(name="인증설정", description="[관리자 전용] 서버 인증 역할, 인증 패널 및 문구를 관리합니다.")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def verify_settings(self, interaction: discord.Interaction):
        settings = await get_verify_settings(interaction.guild.id)
        role_id = settings.get("role_id")
        role = interaction.guild.get_role(int(role_id)) if role_id else None
        state_text = "✅ 활성화" if settings.get("enabled", True) else "❌ 비활성화"
        role_text = role.mention if role else "⚠️ 미설정"

        embed = discord.Embed(
            title="🔐 서버 인증 시스템 관리",
            description=f"**시스템 상태**: {state_text}\n"
                        f"**인증 역할**: {role_text}\n\n"
                        "아래 드롭다운 메뉴에서 원하시는 관리 기능을 선택해 주세요.",
            color=discord.Color.blurple()
        )
        embed.set_footer(text="한바헌 서버 인증 시스템")
        view = VerifySettingsView(settings, interaction.guild)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


class VerifyRevoke(commands.Cog):
    """필요 역할을 잃은 유저의 인증 역할을 자동으로 회수합니다."""

    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        # 이 이벤트는 닉네임 변경 등으로도 자주 발생합니다.
        # 역할이 실제로 바뀐 경우가 아니면 즉시 빠져나갑니다.
        if before.roles == after.roles:
            return

        try:
            settings = await get_verify_settings(after.guild.id)
            if not settings.get("auto_revoke", False):
                return

            required = [int(r) for r in settings.get("required_role_ids", [])]
            role_id = settings.get("role_id")
            if not required or not role_id:
                return

            verify_role = after.guild.get_role(int(role_id))
            if verify_role is None:
                return

            # 인증 역할이 없으면 회수할 것도 없습니다
            if not any(r.id == verify_role.id for r in after.roles):
                return
            # 필요 역할이 하나라도 남아 있으면 유지합니다
            if any(r.id in required for r in after.roles):
                return

            await after.remove_roles(verify_role, reason="인증 필요 역할 상실 (자동 회수)")
            print(f"[Verify] {after.guild.name}: {after.display_name}"
                  f"({after.id}) 인증 역할 자동 회수", flush=True)

            lost = [r.name for r in before.roles
                    if r.id in required and r not in after.roles]
            embed = discord.Embed(
                title="♻️ 인증 역할 자동 회수",
                description=f"{after.mention} 님이 인증 자격을 잃어 "
                            f"{verify_role.mention} 역할을 회수했습니다.",
                color=discord.Color.orange()
            )
            embed.add_field(name="🙋 대상",
                            value=f"**{after.display_name}**\n`{after.id}`", inline=True)
            if lost:
                embed.add_field(name="📉 잃은 역할",
                                value=" · ".join(f"`{x}`" for x in lost), inline=True)
            embed.timestamp = discord.utils.utcnow()
            await _send_verify_log(after.guild, embed)

        except discord.Forbidden:
            print(f"[Verify] {after.guild.name}: 인증 역할 회수 실패 "
                  "(봇 권한 또는 역할 서열 부족)", flush=True)
        except Exception as e:
            print(f"[Verify] 자동 회수 처리 오류: {e}", flush=True)


async def _send_verify_log(guild, embed) -> None:
    """링크 보호 로그 채널로 회수 기록을 보냅니다 (미설정이면 조용히 넘어갑니다).

    인증과 링크 보호는 같은 하위 시스템이라 로그 채널을 공유합니다.
    """
    try:
        settings = await get_linkguard_settings(guild.id)
        channel_id = settings.get("log_channel_id")
        if not channel_id:
            return
        channel = guild.get_channel(int(channel_id))
        if channel is None:
            return
        await channel.send(embed=embed)
    except Exception as e:
        print(f"[Verify] 회수 로그 전송 실패: {e}", flush=True)


async def setup(bot):
    await bot.add_cog(Verify(bot))
    await bot.add_cog(VerifyRevoke(bot))
    # 봇 재시작 후에도 인증 버튼이 동작하도록 영구 View 등록
    bot.add_view(VerifyPanelView())
