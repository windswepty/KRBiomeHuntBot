import discord
from discord.ext import commands
from discord import app_commands
from utils.database import checkin_user, get_user_points

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
        success, total_points = await checkin_user(interaction.user.id)

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
        total_points = await get_user_points(interaction.user.id)
        embed = discord.Embed(
            title="💳 내 포인트 정보",
            description=f"💰 **현재 총 보유 포인트**: **{total_points:,}** P",
            color=discord.Color.blue()
        )
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

async def setup(bot):
    await bot.add_cog(CheckIn(bot))
