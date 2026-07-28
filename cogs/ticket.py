import discord
from discord.ext import commands
from discord import app_commands
from utils.database import add_points

class CustomPointModal(discord.ui.Modal, title="커스텀 포인트 지급"):
    points_input = discord.ui.TextInput(
        label="지급할 포인트 수량",
        placeholder="예: 750",
        required=True,
        max_length=7
    )
    reason_input = discord.ui.TextInput(
        label="제보 바이옴 / 상세 사유",
        placeholder="예: 아포칼립스 바이옴 스나이핑 성공",
        required=False,
        default="희귀 바이옴 제보 승인"
    )

    def __init__(self, target_user: discord.User, ticket_channel: discord.TextChannel):
        super().__init__()
        self.target_user = target_user
        self.ticket_channel = ticket_channel

    async def on_submit(self, interaction: discord.Interaction):
        try:
            pts = int(self.points_input.value)
            if pts <= 0:
                await interaction.response.send_message("❌ 포인트는 1P 이상이어야 합니다.", ephemeral=True)
                return
        except ValueError:
            await interaction.response.send_message("❌ 숫자만 입력해주세요.", ephemeral=True)
            return

        new_total = await add_points(self.target_user.id, pts, is_report=True)

        embed = discord.Embed(
            title="✅ 바이옴 제보 승인 완료!",
            description=f"**{self.target_user.mention}** 님의 희귀 바이옴 제보가 승인되었습니다!\n\n"
                        f"💰 **보상 포인트**: `+{pts:,} P`\n"
                        f"📝 **사유**: `{self.reason_input.value}`\n"
                        f"💳 **현재 총 잔액**: `{new_total:,} P`",
            color=discord.Color.green()
        )
        await interaction.response.send_message(embed=embed)

        # Notify user via DM if possible
        try:
            dm_embed = discord.Embed(
                title="🎉 희귀 바이옴 제보 승인 알림",
                description=f"**한바헌** 서버에서 제출하신 바이옴 제보가 승인되어 **+{pts:,} P**가 지급되었습니다!\n현재 총 잔액: **{new_total:,} P**",
                color=discord.Color.green()
            )
            await self.target_user.send(embed=dm_embed)
        except Exception:
            pass

        # Close ticket after 5 seconds
        await self.ticket_channel.send("🔒 5초 후 제보 티켓이 채널과 함께 자동으로 닫힙니다...")
        import asyncio
        await asyncio.sleep(5)
        await self.ticket_channel.delete(reason=f"바이옴 제보 승인 처리 완료 (지급자: {interaction.user.name})")


class TierSelectView(discord.ui.View):
    def __init__(self, target_user: discord.User, ticket_channel: discord.TextChannel):
        super().__init__(timeout=120)
        self.target_user = target_user
        self.ticket_channel = ticket_channel

    @discord.ui.select(
        placeholder="바이옴 등급을 선택하세요...",
        options=[
            discord.SelectOption(label="💎 S급 최상위 희귀 바이옴 (+1,000 P)", value="1000", description="아포칼립스 / 최상위 스나이핑 바이옴"),
            discord.SelectOption(label="🔴 A급 상위 희귀 바이옴 (+500 P)", value="500", description="상위 희귀 스나이핑 바이옴"),
            discord.SelectOption(label="🟡 B급 중위 희귀 바이옴 (+300 P)", value="300", description="일반 희귀 스나이핑 바이옴"),
            discord.SelectOption(label="🟢 C급 기본 바이옴 (+100 P)", value="100", description="기본 제보 바이옴"),
            discord.SelectOption(label="✏️ 직접 포인트 입력", value="custom", description="원하는 포인트 직접 작성"),
        ]
    )
    async def select_tier(self, interaction: discord.Interaction, select: discord.ui.Select):
        val = select.values[0]

        if val == "custom":
            await interaction.response.send_modal(CustomPointModal(self.target_user, self.ticket_channel))
            return

        pts = int(val)
        new_total = await add_points(self.target_user.id, pts, is_report=True)

        embed = discord.Embed(
            title="✅ 바이옴 제보 승인 완료!",
            description=f"**{self.target_user.mention}** 님의 희귀 바이옴 제보가 승인되었습니다!\n\n"
                        f"💰 **보상 포인트**: `+{pts:,} P`\n"
                        f"💳 **현재 총 잔액**: `{new_total:,} P`",
            color=discord.Color.green()
        )
        await interaction.response.send_message(embed=embed)

        try:
            dm_embed = discord.Embed(
                title="🎉 희귀 바이옴 제보 승인 알림",
                description=f"**한바헌** 서버에서 제출하신 바이옴 제보가 승인되어 **+{pts:,} P**가 지급되었습니다!\n현재 총 잔액: **{new_total:,} P**",
                color=discord.Color.green()
            )
            await self.target_user.send(embed=dm_embed)
        except Exception:
            pass

        await self.ticket_channel.send("🔒 5초 후 제보 티켓 채널이 자동으로 삭제됩니다...")
        import asyncio
        await asyncio.sleep(5)
        await self.ticket_channel.delete(reason=f"바이옴 제보 승인 완료 (지급자: {interaction.user.name})")


class TicketActionView(discord.ui.View):
    def __init__(self, owner_id: int):
        super().__init__(timeout=None)
        self.owner_id = owner_id

    @discord.ui.button(label="✅ 제보 승인 (포인트 지급)", style=discord.ButtonStyle.success, custom_id="approve_ticket_btn")
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ 관리자만 제보를 승인할 수 있습니다.", ephemeral=True)
            return

        target_user = interaction.guild.get_member(self.owner_id) or await interaction.client.fetch_user(self.owner_id)
        if not target_user:
            await interaction.response.send_message("❌ 제보자 유저 정보를 찾을 수 없습니다.", ephemeral=True)
            return

        view = TierSelectView(target_user, interaction.channel)
        await interaction.response.send_message("💰 **지급할 바이옴 등급을 선택해 주세요:**", view=view, ephemeral=True)

    @discord.ui.button(label="❌ 제보 거절 / 닫기", style=discord.ButtonStyle.danger, custom_id="reject_ticket_btn")
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.user.guild_permissions.administrator and interaction.user.id != self.owner_id:
            await interaction.response.send_message("❌ 권한이 없습니다.", ephemeral=True)
            return

        await interaction.response.send_message("🔒 제보 티켓을 닫습니다. 3초 후 채널이 삭제됩니다...")
        import asyncio
        await asyncio.sleep(3)
        await interaction.channel.delete(reason="바이옴 제보 티켓 거절/종료")


class TicketLaunchView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🎫 희귀 바이옴 제보 티켓 열기", style=discord.ButtonStyle.primary, custom_id="ticket_open_btn")
    async def open_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild
        user = interaction.user

        # Check existing channel
        channel_name = f"제보-{user.name.lower()}"
        existing = discord.utils.get(guild.text_channels, name=channel_name)
        if existing:
            await interaction.response.send_message(f"❌ 이미 오픈된 제보 티켓이 있습니다! ({existing.mention})", ephemeral=True)
            return

        # Setup Overwrites
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            user: discord.PermissionOverwrite(read_messages=True, send_messages=True, attach_files=True),
            guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True, manage_channels=True)
        }

        # Grant access to administrators
        for role in guild.roles:
            if role.permissions.administrator:
                overwrites[role] = discord.PermissionOverwrite(read_messages=True, send_messages=True)

        try:
            category = discord.utils.get(guild.categories, name="바이옴 제보 티켓")
            if not category:
                category = await guild.create_category("바이옴 제보 티켓")

            ticket_channel = await guild.create_text_channel(
                name=channel_name,
                category=category,
                overwrites=overwrites,
                topic=f"{user.name} ({user.id}) 님의 희귀 바이옴 제보 티켓"
            )
        except Exception as e:
            await interaction.response.send_message(f"❌ 티켓 채널 생성 중 오류가 발생했습니다: {e}", ephemeral=True)
            return

        await interaction.response.send_message(f"✅ 희귀 바이옴 제보 티켓이 생성되었습니다! -> {ticket_channel.mention}", ephemeral=True)

        # Send welcome message inside ticket channel
        embed = discord.Embed(
            title="🎫 희귀 바이옴 제보 양식",
            description=f"안녕하세요, **{user.mention}** 님!\n"
                        f"매크로/AFK로 발견하신 희귀 바이옴 정보를 아래 양식에 맞게 남겨주시면 관리자가 확인 후 **포인트를 지급**해 드립니다.\n\n"
                        f"**📋 양식 안내:**\n"
                        f"1. **바이옴 이름 / 등급**: (예: 아포칼립스)\n"
                        f"2. **서버 링크 / 코드**: (예: https://discord.gg/... 또는 게임 접속 코드)\n"
                        f"3. **증거 사진**: (인게임 화면 캡처 이미지 첨부)",
            color=discord.Color.blue()
        )
        embed.set_footer(text="관리자가 확인 후 [제보 승인] 버튼으로 포인트를 지급합니다.")

        action_view = TicketActionView(owner_id=user.id)
        await ticket_channel.send(content=f"{user.mention} | 관리자님 확인 부탁드립니다.", embed=embed, view=action_view)


class Ticket(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="티켓패널생성", description="[관리자 전용] 채널에 희귀 바이옴 제보 티켓 생성 버튼 패널을 만듭니다.")
    @app_commands.checks.has_permissions(administrator=True)
    async def create_ticket_panel(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="🎰 한바헌 희귀 바이옴 제보 센터",
            description="매크로 및 AFK로 희귀 바이옴을 스나이핑/발견하셨나요?\n"
                        "아래 **[🎫 희귀 바이옴 제보 티켓 열기]** 버튼을 눌러 1대1 제보를 진행해 주세요!\n\n"
                        "✅ 관리자가 제보 확인 후 **바이옴 등급에 따라 포인트**를 지급합니다.\n"
                        "💰 획득한 포인트는 `/도박`, `/송금`, `/상점` 등에 사용할 수 있습니다.",
            color=discord.Color.gold()
        )
        embed.set_thumbnail(url=interaction.guild.icon.url if interaction.guild.icon else None)

        view = TicketLaunchView()
        await interaction.channel.send(embed=embed, view=view)
        await interaction.response.send_message("✅ 제보 티켓 패널이 성공적으로 생성되었습니다!", ephemeral=True)

    @create_ticket_panel.error
    async def create_ticket_panel_error(self, interaction: discord.Interaction, error):
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message("❌ 관리자만 사용할 수 있는 명령어입니다.", ephemeral=True)

async def setup(bot):
    cog = Ticket(bot)
    await bot.add_cog(cog)
    bot.add_view(TicketLaunchView())
