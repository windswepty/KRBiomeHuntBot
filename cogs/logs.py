import discord
from discord.ext import commands
from discord import app_commands
from utils.database import get_activity_logs


LOG_TYPE_EMOJIS = {
    "shop": "🛒",
    "gacha": "🎰",
    "report": "🎫"
}

LOG_TYPE_NAMES = {
    "all": "📋 전체 활동 로그",
    "shop": "🛒 상점 구매 로그",
    "gacha": "🎰 뽑기 당첨 로그",
    "report": "🎫 바이옴 제보 로그"
}


# ── Log Select View ───────────────────────────────────────────────────

class LogTypeSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="전체 활동 로그", value="all", description="상점, 뽑기, 제보 기록을 모두 봅니다.", emoji="📋"),
            discord.SelectOption(label="상점 구매 로그", value="shop", description="유저들의 상점 상품 구매 내역을 봅니다.", emoji="🛒"),
            discord.SelectOption(label="뽑기 당첨 로그", value="gacha", description="유저들의 럭키 뽑기 당첨 내역을 봅니다.", emoji="🎰"),
            discord.SelectOption(label="바이옴 제보 로그", value="report", description="희귀 바이옴 제보 보상 지급 내역을 봅니다.", emoji="🎫"),
        ]
        super().__init__(placeholder="조회할 로그 카테고리를 선택하세요...", options=options)

    async def callback(self, interaction: discord.Interaction):
        log_type = self.values[0]
        guild_id = interaction.guild.id

        logs = await get_activity_logs(guild_id, log_type=log_type, limit=15)
        title_str = LOG_TYPE_NAMES.get(log_type, "📋 활동 로그")

        if not logs:
            embed = discord.Embed(
                title=title_str,
                description="아직 기록된 로그 데이터가 없습니다.",
                color=discord.Color.blue()
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        embed = discord.Embed(
            title=title_str,
            description=f"최근 **{len(logs)}개**의 기록을 표시합니다.",
            color=discord.Color.blue()
        )

        for log in logs:
            l_type = log.get("type", "shop")
            emoji = LOG_TYPE_EMOJIS.get(l_type, "📌")
            user_name = log.get("user_name", "알 수 없는 유저")
            details = log.get("details", "")
            timestamp = log.get("timestamp", "")

            embed.add_field(
                name=f"{emoji} [{timestamp}] {user_name}",
                value=f"└ {details}",
                inline=False
            )

        embed.set_footer(text="한바헌 봇 관리자 로그 기록 시스템")
        await interaction.response.send_message(embed=embed, ephemeral=True)


class LogSelectView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)
        self.add_item(LogTypeSelect())


# ── Cog ───────────────────────────────────────────────────────────────

class Logs(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="로그", description="[관리자 전용] 상점 구매, 뽑기 당첨, 제보 보상 기록을 조회합니다.")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def view_logs(self, interaction: discord.Interaction):
        guild_id = interaction.guild.id
        logs = await get_activity_logs(guild_id, log_type="all", limit=15)

        embed = discord.Embed(
            title="📋 활동 기록 로그 센터",
            description="상점 구매, 뽑기 당첨, 바이옴 제보 보상 등 **최근 활동 내역**을 확인하세요.\n\n"
                        "드롭다운 메뉴에서 원하시는 로그 카테고리를 선택하세요.",
            color=discord.Color.dark_blue()
        )

        if logs:
            log_summary = []
            for log in logs[:5]:
                l_type = log.get("type", "shop")
                emoji = LOG_TYPE_EMOJIS.get(l_type, "📌")
                log_summary.append(f"{emoji} `{log.get('timestamp', '')}` **{log.get('user_name', '')}**: {log.get('details', '')}")
            embed.add_field(name="⏱️ 가장 최근 활동 (Top 5)", value="\n".join(log_summary), inline=False)
        else:
            embed.add_field(name="⏱️ 최근 활동", value="아직 기록된 로그가 없습니다.", inline=False)

        view = LogSelectView()
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


async def setup(bot):
    await bot.add_cog(Logs(bot))
