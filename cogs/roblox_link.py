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

import discord
from discord.ext import commands
from discord import app_commands

from utils import roblox_oauth
from utils.verify_db import (
    get_roblox_link, find_roblox_owner, save_roblox_link,
    remove_roblox_link, list_roblox_links, search_roblox_links,
    get_verify_settings, get_linkguard_settings,
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


class RobloxLink(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    # ── 멤버용 ────────────────────────────────────────────────────────

    @app_commands.command(
        name="로블록스연동",
        description="내 로블록스 계정을 연동합니다 (개인서버 이용 기록에 함께 남습니다)."
    )
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
        name="연동해제",
        description="[관리자 전용] 특정 멤버의 로블록스 연동을 해제합니다."
    )
    @app_commands.describe(member="연동을 해제할 멤버")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def unlink(self, interaction: discord.Interaction, member: discord.Member):
        removed = await remove_roblox_link(interaction.guild.id, member.id)
        embed = discord.Embed(
            title="🔗 연동 해제" if removed else "ℹ️ 연동 정보 없음",
            description=(f"{member.mention} 님의 로블록스 연동을 해제했습니다.\n"
                         "본인이 `/로블록스연동` 으로 다시 등록할 수 있습니다."
                         if removed else
                         f"{member.mention} 님은 연동된 계정이 없습니다."),
            color=discord.Color.orange() if removed else discord.Color.greyple())

        # 연동을 필수로 걸어둔 서버에서 연동만 지우고 인증 역할을 남겨두면,
        # '링크를 받아간 사람은 전원 연동돼 있다'는 전제가 깨집니다.
        # 그래서 인증 역할도 함께 회수합니다.
        if removed:
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
