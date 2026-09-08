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
    get_roblox_link,
)
from utils import roblox_oauth

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


def role_mentions(guild: discord.Guild, role_ids, limit: int = 1000) -> str:
    """역할 ID 목록을 멘션 문자열로 만듭니다. 임베드 필드 상한을 넘지 않게 자릅니다.

    임베드 필드는 1024자가 상한입니다. 역할 멘션 하나가 구분자까지 26자라
    **39개부터 상한을 넘겨 패널 전송이 통째로 실패**합니다.
    잘릴 때는 몇 개가 빠졌는지 반드시 밝힙니다 (조용히 자르면 다 보이는 줄 압니다).
    """
    parts, used, shown = [], 0, 0
    resolved = [r for r in (guild.get_role(int(i)) for i in role_ids) if r]
    for r in resolved:
        piece = r.mention
        # 남은 자리에 "…외 NN개" 안내까지 들어갈 여유를 남겨 둡니다
        if used + len(piece) + 3 > limit - 20:
            break
        parts.append(piece)
        used += len(piece) + 3
        shown += 1
    text = " · ".join(parts)
    if shown < len(resolved):
        text += f" …외 {len(resolved) - shown}개"
    return text


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
        names = role_mentions(guild, required)
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

    if settings.get("require_roblox", False):
        embed.add_field(
            name="🎮 로블록스 연동 필수",
            value="`/로블록스연동` 으로 로블록스 계정을 먼저 연동해야 합니다.\n"
                  "-# 개인서버 링크가 밖으로 새어나갔을 때 확인하기 위해서입니다.",
            inline=False
        )

    embed.set_footer(text="한바헌 서버 인증 시스템")
    return embed


# 추적할 패널 개수 상한. 무한히 늘어나면 설정을 바꿀 때마다 그만큼
# API 를 두드리게 됩니다.
MAX_TRACKED_PANELS = 25


async def refresh_all_panels(guild: discord.Guild) -> tuple[int, int]:
    """올려둔 인증 패널을 **전부** 최신 설정으로 다시 그립니다.

    패널은 여러 채널에 올릴 수 있고 전부 동시에 동작합니다(버튼 custom_id 가
    고정이라 어느 패널에서 눌러도 같습니다). 그런데 임베드에는 **인증 자격
    역할 목록**이 박혀 있어서, 자격을 바꾸면 이미 올라간 패널들이 옛 조건을
    계속 보여줍니다. 유저는 안 되는 조건을 보고 문의하게 됩니다.

    돌려주는 값: (갱신한 개수, 사라져서 목록에서 지운 개수)
    """
    settings = await get_verify_settings(guild.id)
    tracked = list(settings.get("panel_messages") or [])
    if not tracked:
        return 0, 0

    embed = build_panel_embed(settings, guild)
    view = VerifyPanelView(button_label=settings.get("button_label", "인증하기"))

    alive, updated, removed = [], 0, 0
    for ref in tracked:
        try:
            cid, mid = int(ref.get("c", 0)), int(ref.get("m", 0))
        except (TypeError, ValueError):
            removed += 1
            continue
        channel = guild.get_channel(cid)
        if channel is None:
            removed += 1                     # 채널째 사라졌습니다
            continue
        try:
            # 조회 없이 바로 고칩니다. fetch_message + edit 이면 패널 하나당
            # API 를 2번 쓰는데, 여러 개를 한꺼번에 고치므로 그대로 두 배가 됩니다.
            await channel.get_partial_message(mid).edit(embed=embed, view=view)
            alive.append({"c": cid, "m": mid})
            updated += 1
        except discord.NotFound:
            removed += 1                     # 메시지가 지워짐 — 목록에서 뺍니다
        except discord.Forbidden:
            # 권한이 잠시 빠진 것일 수 있으니 목록에는 남겨 둡니다.
            # 여기서 지워 버리면 권한을 되돌려도 다시는 갱신되지 않습니다.
            alive.append({"c": cid, "m": mid})
            print(f"[Verify] 패널 갱신 권한 없음: 채널 {cid}", flush=True)
        except discord.HTTPException as e:
            alive.append({"c": cid, "m": mid})
            print(f"[Verify] 패널 갱신 실패 (채널 {cid}): {e}", flush=True)

    if removed:
        await save_verify_settings(guild.id, {"panel_messages": alive})
    return updated, removed


async def save_panel_setting(guild: discord.Guild, patch: dict) -> str:
    """패널 내용에 영향을 주는 설정을 저장하고 **올려둔 패널을 전부 갱신**합니다.

    저장만 하고 끝내면 이미 올라간 패널들이 옛 조건을 계속 보여줍니다.
    패널이 하나일 때는 지우고 다시 올리면 그만이었지만, 여러 채널에
    올린 뒤에는 전부 찾아다녀야 합니다.

    돌려주는 값: 결과를 사람에게 보여줄 한 줄 (없으면 빈 문자열)
    """
    await save_verify_settings(guild.id, patch)
    updated, removed = await refresh_all_panels(guild)
    if not updated and not removed:
        return ""
    parts = []
    if updated:
        parts.append(f"올려둔 인증 패널 **{updated}개**를 함께 갱신했습니다.")
    if removed:
        parts.append(f"사라진 패널 {removed}개는 목록에서 정리했습니다.")
    return " ".join(parts)


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
            names = role_mentions(guild, required) or "관리자에게 문의"
            embed = discord.Embed(
                title="🔒 인증 자격이 없습니다",
                description="이 서버의 인증은 **아래 역할 중 하나**를 보유한 분만 신청할 수 있습니다.",
                color=discord.Color.red()
            )
            embed.add_field(name="🎫 필요한 역할", value=names, inline=False)
            embed.set_footer(text="역할을 받는 방법은 관리자에게 문의해 주세요")
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        # 로블록스 연동 검사 — 역할 조건을 통과해도 연동을 마치지 않았으면 막습니다.
        # 링크를 받아간 사람을 게임 내 플레이어 목록과 대조할 수 있어야 외부 유출을
        # 잡을 수 있는데, 연동하지 않은 사람이 섞여 있으면 그 대조가 무의미해집니다.
        if settings.get("require_roblox", False):
            link = await get_roblox_link(guild.id, member.id)
            if not link:
                # 이미 인증된 분에게 "인증을 마치려면" 이라고 하면 혼란스럽습니다.
                # (설정을 나중에 켜면 기존 인증자는 그대로 남아 있습니다)
                already = any(r.id == role.id for r in member.roles)
                embed = discord.Embed(
                    title="🎮 로블록스 연동이 필요합니다",
                    description=("인증은 이미 완료되어 있지만, 아직 로블록스 계정이 "
                                 "연동되지 않았습니다.\n"
                                 "-# 개인서버에 들어온 사람을 확인하는 데 쓰이니 "
                                 "연동해 주시면 좋습니다."
                                 if already else
                                 "인증을 마치려면 로블록스 계정을 먼저 연동해 주세요."),
                    color=discord.Color.orange()
                )
                embed.add_field(
                    name="🔒 확인하는 정보",
                    value="로블록스 **아이디와 유저명만** 확인합니다.\n"
                          "비밀번호 · 로벅스 · 인벤토리에는 접근하지 않습니다.",
                    inline=False
                )
                view = discord.ui.View(timeout=None)
                url = None
                if roblox_oauth.enabled():
                    try:
                        url, _ = roblox_oauth.start(guild.id, member.id)
                    except Exception as e:
                        # 링크 생성 실패가 안내 자체를 막으면 안 됩니다
                        print(f"[Verify] 로블록스 로그인 링크 생성 실패: "
                              f"{type(e).__name__}: {e}", flush=True)
                if url:
                    embed.add_field(
                        name="👉 연동 방법",
                        value="아래 버튼을 누르거나 `/로블록스연동` 을 사용해 주세요.",
                        inline=False
                    )
                    embed.set_footer(text="이 링크는 10분 뒤 만료됩니다 · 연동 후 다시 [인증하기]를 눌러 주세요")
                    view.add_item(discord.ui.Button(
                        label="로블록스로 로그인", url=url,
                        style=discord.ButtonStyle.link, emoji="🎮"))
                else:
                    embed.add_field(
                        name="⚠️ 안내",
                        value="로블록스 연동 기능이 아직 준비되지 않았습니다. "
                              "관리자에게 문의해 주세요.",
                        inline=False
                    )
                kwargs = {"embed": embed, "ephemeral": True}
                if view.children:
                    kwargs["view"] = view
                await interaction.response.send_message(**kwargs)
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
        # 저장만 하면 이미 올라간 패널들이 옛 문구를 계속 보여줍니다.
        # 예전에는 "새로 전송하는 패널부터 적용됩니다" 라고 안내했는데,
        # 여러 채널에 올린 뒤에는 전부 찾아다녀야 해서 현실적이지 않습니다.
        note = await save_panel_setting(interaction.guild, {
            "title": self.title_input.value.strip(),
            "description": self.desc_input.value.strip(),
            "button_label": self.label_input.value.strip(),
        })
        embed = discord.Embed(
            title="✅ 인증 패널 문구 저장 완료",
            description=(note or "아직 올려둔 인증 패널이 없습니다."
                         " `/인증설정` → **인증 패널 전송** 으로 보내 주세요."),
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
            msg = await channel.send(embed=embed, view=view)
        except discord.Forbidden:
            await interaction.response.send_message(
                f"⚠️ {channel.mention} 채널에 메시지를 보낼 권한이 없습니다.",
                ephemeral=True
            )
            return

        # 어디에 올렸는지 기록해 둡니다. 나중에 자격 역할이나 문구를 바꾸면
        # 이 목록을 보고 **전부 다시 그립니다.** 기록하지 않으면 옛 조건을
        # 보여주는 패널이 곳곳에 남고, 유저는 안 되는 조건을 보고 문의합니다.
        tracked = [r for r in (settings.get("panel_messages") or [])
                   if int(r.get("m", 0)) != msg.id]
        tracked.append({"c": channel.id, "m": msg.id})
        tracked = tracked[-MAX_TRACKED_PANELS:]
        await save_verify_settings(interaction.guild.id, {
            # 링크 보호 안내가 가리킬 대표 채널 (가장 마지막에 올린 곳)
            "panel_channel_id": channel.id,
            "panel_messages": tracked,
        })
        await interaction.response.send_message(
            f"✅ {channel.mention} 채널에 인증 패널을 전송했습니다."
            + chr(10)
            + f"-# 인증 패널 **{len(tracked)}개**가 살아 있습니다. "
              "전부 동시에 동작하고, 설정을 바꾸면 함께 갱신됩니다.",
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

        panel_note = ""
        if found:
            # 자격 목록은 패널 임베드에 그대로 박혀 있습니다. 저장만 하면
            # 이미 올라간 패널들이 옛 조건을 계속 보여줍니다.
            panel_note = await save_panel_setting(
                guild, {"required_role_ids": current})

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
        if panel_note:
            embed.add_field(name="📮 인증 패널", value=panel_note,
                            inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)


class RemoveRequiredRoleSelect(discord.ui.Select):
    def __init__(self, role_ids: list[int], guild: discord.Guild):
        options = []
        # 드롭다운은 25개가 상한입니다. 넘치면 나머지는 화면에서 해제할 수
        # 없으므로, 몇 개가 가려졌는지 반드시 알립니다.
        # (조용히 자르면 목록이 다 보이는 줄 알고 잘못 판단합니다)
        self.hidden = max(0, len(role_ids) - 25)
        for rid in role_ids[:25]:
            role = guild.get_role(rid)
            options.append(discord.SelectOption(
                label=(f"@{role.name}" if role else "(삭제된 역할)")[:100],
                value=str(rid),
                description=str(rid)
            ))
        if not options:
            options = [discord.SelectOption(label="지정된 역할이 없습니다", value="none")]
        placeholder = ("해제할 역할을 선택하세요..." if not self.hidden
                       else f"해제할 역할 선택 (⚠️ {self.hidden}개는 목록에 없음)")
        super().__init__(placeholder=placeholder, options=options)

    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "none":
            await interaction.response.send_message("해제할 역할이 없습니다.", ephemeral=True)
            return
        guild = interaction.guild
        settings = await get_verify_settings(guild.id)
        current = [int(r) for r in settings.get("required_role_ids", [])]
        rid = int(self.values[0])
        panel_note = ""
        if rid in current:
            current.remove(rid)
            panel_note = await save_panel_setting(
                guild, {"required_role_ids": current})
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
        if panel_note:
            embed.add_field(name="📮 인증 패널", value=panel_note, inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)


class RequiredRoleView(discord.ui.View):
    def __init__(self, role_ids: list[int], guild: discord.Guild):
        super().__init__(timeout=180)
        self.add_item(RemoveRequiredRoleSelect(role_ids, guild))

    @discord.ui.button(label="역할 ID로 추가", style=discord.ButtonStyle.success, emoji="🆔", row=1)
    async def add_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(RequiredRoleIdModal())


def build_settings_panel(settings: dict, guild: discord.Guild) -> discord.Embed:
    """인증 설정 패널. 값이 바뀔 때마다 이걸로 다시 그립니다."""
    role_id = settings.get("role_id")
    role = guild.get_role(int(role_id)) if role_id else None
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
    return embed


async def refresh_settings_panel(interaction: discord.Interaction,
                                 result: discord.Embed) -> None:
    """설정을 바꾼 뒤 패널을 최신 값으로 다시 그립니다.

    이 단계가 없으면 드롭다운 설명이 옛 값 그대로 남아, 켰는데도
    '꺼짐'으로 보입니다. 실제로 그런 신고가 있었습니다.

    패널 갱신에 실패해도 변경 결과는 반드시 알려야 하므로 갈라 둡니다.
    """
    settings = await get_verify_settings(interaction.guild.id)
    try:
        await interaction.response.edit_message(
            embed=build_settings_panel(settings, interaction.guild),
            view=VerifySettingsView(settings, interaction.guild))
        await interaction.followup.send(embed=result, ephemeral=True)
    except (discord.HTTPException, discord.InteractionResponded):
        if interaction.response.is_done():
            await interaction.followup.send(embed=result, ephemeral=True)
        else:
            await interaction.response.send_message(embed=result, ephemeral=True)


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
                label="로블록스 연동 필수 여부",
                value="reqroblox",
                description=("현재: ✅ 켜짐 — 연동해야 인증 가능"
                             if current.get("require_roblox")
                             else "현재: ❌ 꺼짐 — 연동 없이도 인증 가능"),
                emoji="🎮"
            ),
            discord.SelectOption(
                label="로블록스 계정 교체 알림",
                value="relinkalert",
                description=("현재: 켜짐 — 계정을 바꾸면 로그 채널에 기록"
                             if current.get("roblox_relink_alert", True)
                             else "현재: 꺼짐"),
                emoji="🔄"
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

        elif choice == "reqroblox":
            new_state = not settings.get("require_roblox", False)

            if new_state and not roblox_oauth.enabled():
                embed = discord.Embed(
                    title="⚠️ 로블록스 연동이 준비되지 않았습니다",
                    description="`ROBLOX_CLIENT_ID` · `ROBLOX_CLIENT_SECRET` 환경 변수가 "
                                "설정되어야 합니다.\n"
                                "지금 켜면 **아무도 인증을 받을 수 없게 됩니다.**",
                    color=discord.Color.orange()
                )
                await interaction.response.send_message(embed=embed, ephemeral=True)
                return

            # 패널에 "로블록스 연동 필수" 안내가 들어가고 빠집니다
            panel_note = await save_panel_setting(
                guild, {"require_roblox": new_state})
            embed = discord.Embed(
                title="🎮 로블록스 연동 필수 설정 변경",
                description=f"연동 필수가 **{'✅ 켜짐' if new_state else '❌ 꺼짐'}** 상태가 되었습니다.",
                color=discord.Color.green() if new_state else discord.Color.red()
            )
            if new_state:
                embed.add_field(
                    name="🔎 이렇게 동작합니다",
                    value="인증 역할을 받으려면 **필요 역할 + 로블록스 연동**을 "
                          "모두 만족해야 합니다.\n"
                          "연동하지 않은 채 [인증하기]를 누르면 로블록스 로그인 "
                          "버튼이 안내됩니다.",
                    inline=False
                )
                embed.add_field(
                    name="✅ 얻는 것",
                    value="링크를 받아간 사람 **전원**의 로블록스 아이디를 알게 되므로,\n"
                          "게임 안 플레이어 목록과 대조해 **명단에 없는 침입자**를 "
                          "바로 찾아낼 수 있습니다.",
                    inline=False
                )
                embed.add_field(
                    name="⚠️ 주의",
                    value="• **이미 인증받은 멤버는 그대로 유지**됩니다 (소급 적용 없음)\n"
                          "• 로블록스 계정이 만 13세 미만이면 연동할 수 없습니다\n"
                          "• 로블록스 앱은 2026-09-05 에 승인되어 인원 제한이 없습니다",
                    inline=False
                )
            else:
                embed.add_field(
                    name="🔎 이렇게 동작합니다",
                    value="연동 여부와 무관하게 인증할 수 있습니다. "
                          "(`/로블록스연동` 은 계속 사용 가능합니다)",
                    inline=False
                )
            if panel_note:
                embed.add_field(name="📮 인증 패널", value=panel_note,
                                inline=False)
            await refresh_settings_panel(interaction, embed)

        elif choice == "relinkalert":
            new_state = not settings.get("roblox_relink_alert", True)
            await save_verify_settings(guild.id, {"roblox_relink_alert": new_state})
            embed = discord.Embed(
                title="🔄 로블록스 계정 교체 알림",
                description=f"알림이 **{'✅ 켜짐' if new_state else '❌ 꺼짐'}** 상태가 되었습니다.",
                color=discord.Color.green() if new_state else discord.Color.red()
            )
            if new_state:
                embed.add_field(
                    name="🔎 이렇게 동작합니다",
                    value="멤버가 연동된 로블록스 계정을 **다른 계정으로 바꾸면**\n"
                          "링크 보호 로그 채널에 이전/이후 계정을 함께 기록합니다.\n"
                          "갑작스러운 계정 교체는 확인해 볼 만한 신호입니다.",
                    inline=False)
                embed.add_field(
                    name="ℹ️ 참고",
                    value="로그 채널이 설정돼 있어야 기록됩니다.\n"
                          "정상적인 계정 변경일 수도 있으니 정황과 함께 판단하세요.",
                    inline=False)
            else:
                embed.add_field(
                    name="🔎 이렇게 동작합니다",
                    value="계정을 바꿔도 로그 채널에 알리지 않습니다. (콘솔 로그에는 남습니다)",
                    inline=False)
            await refresh_settings_panel(interaction, embed)

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

            # 패널에 "자격을 모두 잃으면 자동 해제" 안내가 들어가고 빠집니다
            panel_note = await save_panel_setting(
                guild, {"auto_revoke": new_state})
            embed = discord.Embed(
                title="♻️ 인증 자동 회수 설정 변경",
                description=f"자동 회수가 **{'✅ 켜짐' if new_state else '❌ 꺼짐'}** 상태가 되었습니다.",
                color=discord.Color.green() if new_state else discord.Color.red()
            )
            if new_state:
                names = role_mentions(guild, required)
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
            if panel_note:
                embed.add_field(name="📮 인증 패널", value=panel_note,
                                inline=False)
            await refresh_settings_panel(interaction, embed)

        elif choice == "toggle":
            new_state = not settings.get("enabled", True)
            await save_verify_settings(guild.id, {"enabled": new_state})
            state_text = "✅ 활성화" if new_state else "❌ 비활성화"
            embed = discord.Embed(
                title="⚡ 인증 시스템 상태 변경",
                description=f"인증 시스템이 **{state_text}** 상태로 변경되었습니다.",
                color=discord.Color.green() if new_state else discord.Color.red()
            )
            await refresh_settings_panel(interaction, embed)

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
        await interaction.response.send_message(
            embed=build_settings_panel(settings, interaction.guild),
            view=VerifySettingsView(settings, interaction.guild),
            ephemeral=True)


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

            # 전원 검사가 가장 먼저입니다. 정지 상태에서는 '기능이 없는 것과
            # 같아야' 하는데, 여기서 회수가 계속 돌면 관리자가 껐다고 믿는 동안
            # 조용히 인증이 풀립니다. (비상 정지의 의미가 사라집니다)
            if not settings.get("enabled", True):
                return
            if not settings.get("auto_revoke", False):
                return

            required = [int(r) for r in settings.get("required_role_ids", [])]
            role_id = settings.get("role_id")
            if not required or not role_id:
                return

            verify_role = after.guild.get_role(int(role_id))
            if verify_role is None:
                return

            # 서버에서 삭제된 역할은 판단 근거가 될 수 없습니다.
            # discord.py 는 역할이 삭제돼도 member.roles 에서 조용히 빼기만 할 뿐
            # on_member_update 를 쏘지 않습니다. 그래서 삭제 당시에는 아무 일도
            # 없다가, 그 뒤 **아무 역할 변경에나** 걸려 한 명씩 인증이 풀립니다.
            # 남아있는 역할이 하나도 없으면 '자격 상실'을 판정할 수 없으므로
            # 회수하지 않습니다. (설정이 고쳐질 때까지 현상 유지)
            alive = [rid for rid in required if after.guild.get_role(rid) is not None]
            if not alive:
                print(f"[Verify] {after.guild.name}: 필요 역할이 모두 삭제되어 "
                      "자동 회수를 건너뜁니다 (/인증설정 에서 목록을 정리하세요)",
                      flush=True)
                return
            required = alive

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
