import discord
from discord.ext import commands
from discord import app_commands
from utils.database import add_user_points, get_report_settings, save_report_settings


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
        embed = discord.Embed(
            title="🎫 희귀 바이옴 제보 티켓",
            description="해당티켓은 희귀 바이옴 인증 및 포인트 적립을 위해 생성되었습니다!\n"
                        "본인의 용건이 끝났다하더라도 티켓을 닫지말아주세요! 관리자가 알아서 닫아줍니다!\n\n"
                        "⚠️ **티켓이 열리면 즉시 희귀 바이옴 인증 스크린샷을 첨부해 주세요!**",
            color=discord.Color.green()
        )
        embed.add_field(name="👤 제보자", value=user.mention, inline=True)
        embed.add_field(name="🔗 희귀 바이옴 채널 링크", value=self.channel_link.value, inline=False)
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

        await interaction.response.send_message("🔒 3초 후 이 티켓 채널을 삭제합니다...")

        import asyncio
        await asyncio.sleep(3)
        try:
            await interaction.channel.delete(reason=f"티켓 닫기 - 관리자: {interaction.user.name}")
        except Exception as e:
            await interaction.followup.send(f"⚠️ 채널 삭제에 실패했습니다: {e}", ephemeral=True)

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

        # DB에 포인트 추가
        new_total = await add_user_points(target_user_id, amount)

        target_user = interaction.guild.get_member(target_user_id)
        target_mention = target_user.mention if target_user else f"<@{target_user_id}>"
        target_name = target_user.display_name if target_user else "제보자"

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
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="티켓 열기", style=discord.ButtonStyle.primary, emoji="🎫", custom_id="report_ticket_open")
    async def open_ticket_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        # 시스템 활성화 여부 확인
        settings = await get_report_settings(interaction.guild.id)
        if not settings.get("enabled", True):
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
            current = self.current_settings.get("enabled", True)
            new_val = not current
            await save_report_settings(interaction.guild.id, {"enabled": new_val})
            status = "✅ 활성화" if new_val else "❌ 비활성화"
            embed = discord.Embed(
                title="⚡ 제보 시스템 상태 변경",
                description=f"제보 시스템이 **{status}** 상태로 변경되었습니다.",
                color=discord.Color.green() if new_val else discord.Color.red()
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)

        elif choice == "default_points":
            await interaction.response.send_modal(DefaultPointsModal())

        elif choice == "notify_role":
            await interaction.response.send_modal(NotifyRoleModal())

        elif choice == "report_channel":
            await interaction.response.send_modal(ReportChannelModal())

        elif choice == "reset":
            from utils.database import DEFAULT_REPORT_SETTINGS
            await save_report_settings(interaction.guild.id, dict(DEFAULT_REPORT_SETTINGS))
            embed = discord.Embed(
                title="🔄 설정 초기화 완료",
                description="모든 제보 설정이 기본값으로 초기화되었습니다.\n\n"
                            "• 시스템: ✅ 활성화\n"
                            "• 기본 포인트: 500P\n"
                            "• 알림 역할: 없음\n"
                            "• 제보 카테고리: 없음",
                color=discord.Color.blue()
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)


class ReportSettingsView(discord.ui.View):
    def __init__(self, current_settings: dict):
        super().__init__(timeout=180)
        self.add_item(ReportSettingsSelect(current_settings))


# ── Settings Modals ───────────────────────────────────────────────────

class DefaultPointsModal(discord.ui.Modal, title="💰 기본 지급 포인트 설정"):
    points_input = discord.ui.TextInput(
        label="제보 승인 시 기본으로 지급할 포인트 수량",
        placeholder="예: 500",
        default="500",
        required=True,
        max_length=10
    )

    async def on_submit(self, interaction: discord.Interaction):
        try:
            amount = int(self.points_input.value.strip())
            if amount <= 0:
                raise ValueError()
        except ValueError:
            await interaction.response.send_message("⚠️ 1 이상의 올바른 숫자를 입력해 주세요.", ephemeral=True)
            return

        await save_report_settings(interaction.guild.id, {"default_points": amount})
        embed = discord.Embed(
            title="💰 기본 포인트 변경 완료",
            description=f"제보 승인 시 기본 지급 포인트가 **{amount:,} P**로 설정되었습니다.",
            color=discord.Color.green()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


class NotifyRoleModal(discord.ui.Modal, title="🔔 알림 역할 설정"):
    role_input = discord.ui.TextInput(
        label="멘션할 역할의 ID를 입력하세요 (제거: 0 입력)",
        placeholder="예: 1234567890123456789  (역할 ID)",
        required=True,
        max_length=25
    )

    async def on_submit(self, interaction: discord.Interaction):
        raw = self.role_input.value.strip()

        if raw == "0":
            await save_report_settings(interaction.guild.id, {"notify_role_id": None})
            embed = discord.Embed(
                title="🔔 알림 역할 제거 완료",
                description="티켓 생성 시 알림 역할 멘션이 **해제**되었습니다.",
                color=discord.Color.orange()
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        try:
            role_id = int(raw)
        except ValueError:
            await interaction.response.send_message("⚠️ 올바른 역할 ID(숫자)를 입력해 주세요.", ephemeral=True)
            return

        role = interaction.guild.get_role(role_id)
        if not role:
            await interaction.response.send_message(f"⚠️ ID `{role_id}`에 해당하는 역할을 찾을 수 없습니다.", ephemeral=True)
            return

        await save_report_settings(interaction.guild.id, {"notify_role_id": role_id})
        embed = discord.Embed(
            title="🔔 알림 역할 설정 완료",
            description=f"티켓 생성 시 {role.mention} 역할이 멘션됩니다.",
            color=discord.Color.green()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


class ReportChannelModal(discord.ui.Modal, title="📌 제보 카테고리 설정"):
    channel_input = discord.ui.TextInput(
        label="티켓 채널이 생성될 카테고리 ID를 입력하세요 (제거: 0)",
        placeholder="예: 1234567890123456789  (카테고리 ID)",
        required=True,
        max_length=25
    )

    async def on_submit(self, interaction: discord.Interaction):
        raw = self.channel_input.value.strip()

        if raw == "0":
            await save_report_settings(interaction.guild.id, {"report_channel_id": None})
            embed = discord.Embed(
                title="📌 제보 카테고리 해제 완료",
                description="제보 카테고리가 **해제**되었습니다.\n"
                            "카테고리를 다시 설정해야 티켓을 생성할 수 있습니다.",
                color=discord.Color.orange()
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        try:
            channel_id = int(raw)
        except ValueError:
            await interaction.response.send_message("⚠️ 올바른 카테고리 ID(숫자)를 입력해 주세요.", ephemeral=True)
            return

        category = interaction.guild.get_channel(channel_id)
        if not category or not isinstance(category, discord.CategoryChannel):
            await interaction.response.send_message(f"⚠️ ID `{channel_id}`에 해당하는 카테고리를 찾을 수 없습니다. 카테고리 ID를 입력해 주세요.", ephemeral=True)
            return

        await save_report_settings(interaction.guild.id, {"report_channel_id": channel_id})
        embed = discord.Embed(
            title="📌 제보 카테고리 설정 완료",
            description=f"이제 제보 티켓 채널이 **{category.name}** 카테고리 안에 생성됩니다.",
            color=discord.Color.green()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


# ── Cog ───────────────────────────────────────────────────────────────

class Report(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="제보", description="희귀 바이옴 제보 안내를 확인하고 제보 티켓을 생성합니다.")
    async def report_biome(self, interaction: discord.Interaction):
        # 시스템 활성화 여부 확인
        settings = await get_report_settings(interaction.guild.id)
        if not settings.get("enabled", True):
            await interaction.response.send_message(
                "⚠️ 현재 바이옴 제보 시스템이 **비활성화** 상태입니다. 관리자에게 문의해 주세요.",
                ephemeral=True
            )
            return

        embed = discord.Embed(
            title="🎫 희귀 바이옴 제보 안내",
            description="해당 제보는 티켓을 통해 희귀바이옴을 인증하면 포인트를 주는 시스템입니다.\n\n"
                        "이 티켓은 매크로로 희귀바이옴을 생성한 시점부터 하루내에 사용해야 포인트를 지급해드립니다.\n\n"
                        "⚠️ **장난식으로 티켓사용 시 제재를 당할 수 있습니다.**\n\n"
                        "아래 **[티켓 열기]** 버튼을 눌러 제출 양식을 작성해 주세요.",
            color=discord.Color.blue()
        )
        view = TicketLaunchView()
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @app_commands.command(name="제보설정", description="[관리자 전용] 바이옴 제보 시스템의 설정을 관리합니다.")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def report_settings(self, interaction: discord.Interaction):
        settings = await get_report_settings(interaction.guild.id)

        # 현재 설정 상태 표시
        enabled_text = "✅ 활성화" if settings.get("enabled", True) else "❌ 비활성화"
        points_text = f"**{settings.get('default_points', 500):,}** P"

        role_id = settings.get("notify_role_id")
        if role_id:
            role = interaction.guild.get_role(role_id)
            role_text = role.mention if role else f"❓ (삭제된 역할: {role_id})"
        else:
            role_text = "설정 안 됨"

        channel_id = settings.get("report_channel_id")
        if channel_id:
            ch = interaction.guild.get_channel(channel_id)
            channel_text = f"**{ch.name}**" if ch else f"❓ (삭제된 카테고리: {channel_id})"
        else:
            channel_text = "설정 안 됨"

        embed = discord.Embed(
            title="⚙️ 바이옴 제보 시스템 설정",
            description="아래 드롭다운 메뉴에서 변경할 설정 항목을 선택하세요.",
            color=discord.Color.dark_gold()
        )
        embed.add_field(name="⚡ 시스템 상태", value=enabled_text, inline=True)
        embed.add_field(name="💰 기본 지급 포인트", value=points_text, inline=True)
        embed.add_field(name="🔔 알림 역할", value=role_text, inline=False)
        embed.add_field(name="📌 제보 카테고리", value=channel_text, inline=False)
        embed.set_footer(text="설정은 이 서버에만 적용됩니다.")

        view = ReportSettingsView(settings)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


async def setup(bot):
    cog = Report(bot)
    bot.add_view(TicketLaunchView())
    bot.add_view(TicketControlView())
    await bot.add_cog(cog)
