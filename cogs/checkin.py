import discord
from discord.ext import commands
from discord import app_commands
from utils.database import (
    checkin_user, get_user_points, get_top_users, get_user_rank,
    is_user_blacklisted, add_user_points, deduct_user_points, add_activity_log
)

class PointGuideSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(
                label="매일 출석체크",
                value="checkin",
                description="/출석 명령어로 하루 한번 포인트 획득",
                emoji="📅"
            ),
            discord.SelectOption(
                label="서버 채팅 활동",
                value="chat",
                description="서버 채팅 작성 시 무제한 자동 적립",
                emoji="💬"
            ),
            discord.SelectOption(
                label="기타 및 서버 이벤트",
                value="event",
                description="이벤트 및 관리자 보상 획득",
                emoji="🎁"
            ),
        ]
        super().__init__(placeholder="궁금한 포인트 획득 항목을 선택하세요...", options=options)

    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "checkin":
            embed = discord.Embed(
                title="📅 [매일 출석체크로 포인트 얻기]",
                description="• **/출석** 명령어를 사용하면 하루에 한 번 **100포인트**를 획득하실 수 있습니다!\n\n"
                            "• 매일 00:00 (한국 시간 기준)에 출석 기록이 리셋됩니다.\n"
                            "• 명령어를 친 본인에게만 비공개로 결과가 표시됩니다.",
                color=discord.Color.green()
            )
        elif self.values[0] == "chat":
            embed = discord.Embed(
                title="💬 [서버 채팅 활동으로 포인트 얻기]",
                description="• 서버에서 텍스트 채팅 **10회**를 작성할 때마다 **+5포인트**가 자동으로 적립됩니다!\n\n"
                            "• 하루 일일 상한선(제한) 없이 무제한으로 적립이 가능합니다.\n"
                            "• *(단, 사진/파일/스티커 및 단독 이모티콘 메시지는 카운트에서 제외됩니다)*",
                color=discord.Color.blue()
            )
        elif self.values[0] == "event":
            embed = discord.Embed(
                title="🎁 [기타 및 서버 이벤트]",
                description="• 서버 내에서 진행되는 특별 이벤트 및 관리자 보상을 통해 포인트를 획득하실 수 있습니다!\n\n"
                            "• 현재 보유 중인 총 포인트는 **/포인트확인** 명령어로 언제든지 확인 가능합니다.",
                color=discord.Color.purple()
            )
        else:
            return

        await interaction.response.send_message(embed=embed, ephemeral=True)


class PointGuideView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)
        self.add_item(PointGuideSelect())


class CheckIn(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="출석", description="하루에 한 번 출석체크를 하고 100포인트를 획득합니다.")
    async def check_in(self, interaction: discord.Interaction):
        # 블랙리스트 유저 확인
        if await is_user_blacklisted(interaction.guild.id, interaction.user.id):
            embed = discord.Embed(
                title="🚫 출석체크 불가",
                description="귀하는 현재 **포인트 획득 제한(블랙리스트)** 상태로 설정되어 출석체크를 진행할 수 없습니다.\n\n"
                            "이유 및 상세 문의는 서버 관리자에게 요청해 주세요.",
                color=discord.Color.red()
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        success, total_points = await checkin_user(interaction.guild.id, interaction.user.id)

        if success:
            embed = discord.Embed(
                title="✅ 출석체크 완료!",
                description=f"🎉 오늘 출석체크로 **+100 P**를 획득하셨습니다!\n\n💰 **현재 총 보유 포인트**: **{total_points:,}** P",
                color=discord.Color.green()
            )
        else:
            embed = discord.Embed(
                title="⚠️ 출석체크 이미 완료됨",
                description=f"오늘 출석체크는 이미 완료하셨습니다. 내일 다시 시도해 주세요!\n\n💰 **현재 총 보유 포인트**: **{total_points:,}** P",
                color=discord.Color.gold()
            )

        # ephemeral=True makes the response visible ONLY to the user!
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="포인트확인", description="현재 내가 보유한 총 포인트를 확인합니다.")
    async def view_points(self, interaction: discord.Interaction):
        total_points = await get_user_points(interaction.guild.id, interaction.user.id)
        embed = discord.Embed(
            title="💳 내 포인트 정보",
            description=f"💰 **현재 총 보유 포인트**: **{total_points:,}** P",
            color=discord.Color.blue()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="포인트랭킹", description="서버의 포인트 보유 순위 TOP 10을 확인합니다.")
    async def point_ranking(self, interaction: discord.Interaction):
        top_users = await get_top_users(interaction.guild.id, limit=10)
        my_rank, my_points = await get_user_rank(interaction.guild.id, interaction.user.id)

        if not top_users:
            embed = discord.Embed(
                title="🏆 포인트 랭킹 TOP 10",
                description="아직 등록된 포인트 랭킹 정보가 없습니다.",
                color=discord.Color.gold()
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        embed = discord.Embed(
            title="🏆 한바헌 포인트 랭킹 TOP 10",
            description="서버에서 가장 많은 포인트를 보유한 상위 10명의 유저입니다!\n",
            color=discord.Color.gold()
        )

        rank_medals = ["🥇", "🥈", "🥉"]

        ranking_lines = []
        for idx, u_info in enumerate(top_users, start=1):
            uid = u_info["user_id"]
            pts = u_info["points"]
            
            # 캐시 조회 및 API fetch 시도
            member = interaction.guild.get_member(uid)
            if not member:
                try:
                    member = await interaction.guild.fetch_member(uid)
                except Exception:
                    member = None

            if member:
                user_str = f"**{member.display_name}** ({member.mention})"
            else:
                user_str = "*알 수 없는 유저 (서버 퇴장)*"

            medal = rank_medals[idx - 1] if idx <= 3 else f"`{idx}위`"
            ranking_lines.append(f"{medal} {user_str} — **{pts:,} P**")

        embed.add_field(
            name="📊 보유 포인트 순위",
            value="\n".join(ranking_lines),
            inline=False
        )

        my_medal = rank_medals[my_rank - 1] if my_rank <= 3 else f"`{my_rank}위`"
        embed.add_field(
            name="👤 내 랭킹 정보",
            value=f"• **현재 순위**: {my_medal} (전체 유저 중 {my_rank}위)\n• **내 보유 포인트**: **{my_points:,} P**",
            inline=False
        )

        embed.set_footer(text="포인트 랭킹은 시각/활동에 따라 실시간으로 갱신됩니다.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="포인트얻는법", description="포인트를 획득하는 다양한 방법과 도움말을 확인합니다.")
    async def point_guide(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="💡 한바헌 포인트 획득 방법 가이드",
            description="아래 **드롭다운 메뉴**에서 원하시는 항목을 선택해 보세요!\n상세한 포인트 획득 조건을 확인하실 수 있습니다.",
            color=discord.Color.gold()
        )
        view = PointGuideView()
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @app_commands.command(name="포인트지급", description="[관리자 전용] 특정 유저에게 포인트를 지급합니다.")
    @app_commands.describe(member="포인트를 지급할 유저", amount="지급할 포인트 수량", reason="지급 사유 (선택)")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def give_points(self, interaction: discord.Interaction, member: discord.Member, amount: int, reason: str = "관리자 지급"):
        if amount <= 0:
            await interaction.response.send_message("⚠️ 지급할 포인트는 1 P 이상이어야 합니다.", ephemeral=True)
            return

        new_total = await add_user_points(interaction.guild.id, member.id, amount)
        await add_activity_log(
            interaction.guild.id, "admin", interaction.user.id, interaction.user.display_name,
            f"[{member.display_name}]에게 {amount:,}P 지급 (사유: {reason})"
        )

        embed = discord.Embed(
            title="🎁 포인트 지급 완료",
            description=f"{member.mention} 유저에게 **+{amount:,} P**를 지급하였습니다.\n\n"
                        f"💰 **해당 유저의 현재 총 포인트**: **{new_total:,} P**\n"
                        f"📝 **사유**: {reason}",
            color=discord.Color.green()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="포인트회수", description="[관리자 전용] 특정 유저의 포인트를 회수(차감)합니다.")
    @app_commands.describe(member="포인트를 회수할 유저", amount="회수할 포인트 수량", reason="회수 사유 (선택)")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def take_points(self, interaction: discord.Interaction, member: discord.Member, amount: int, reason: str = "관리자 회수"):
        if amount <= 0:
            await interaction.response.send_message("⚠️ 회수할 포인트는 1 P 이상이어야 합니다.", ephemeral=True)
            return

        current_pts = await get_user_points(interaction.guild.id, member.id)
        if current_pts < amount:
            embed = discord.Embed(
                title="⚠️ 포인트 회수 불가",
                description=f"{member.mention} 유저의 현재 보유 포인트(**{current_pts:,} P**)가 회수 요청 수량(**{amount:,} P**)보다 적습니다.",
                color=discord.Color.red()
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        success, new_total = await deduct_user_points(interaction.guild.id, member.id, amount)
        if not success:
            await interaction.response.send_message("⚠️ 포인트 회수에 실패하였습니다.", ephemeral=True)
            return

        await add_activity_log(
            interaction.guild.id, "admin", interaction.user.id, interaction.user.display_name,
            f"[{member.display_name}]의 {amount:,}P 회수 (사유: {reason})"
        )

        embed = discord.Embed(
            title="🔻 포인트 회수 완료",
            description=f"{member.mention} 유저의 포인트 **-{amount:,} P**를 회수하였습니다.\n\n"
                        f"💰 **해당 유저의 현재 총 포인트**: **{new_total:,} P**\n"
                        f"📝 **사유**: {reason}",
            color=discord.Color.orange()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

async def setup(bot):
    await bot.add_cog(CheckIn(bot))

