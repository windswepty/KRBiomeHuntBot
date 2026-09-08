import math
import discord
from discord.ext import commands
from discord import app_commands
from utils.database import get_activity_logs


LOG_TYPE_EMOJIS = {
    "shop": "🛒",
    "gacha": "🎰",
    "report": "🎫",
    "admin": "🛠️"
}

LOG_TYPE_NAMES = {
    "all": "📋 전체 활동 로그",
    "shop": "🛒 상점 구매 로그",
    "gacha": "🎰 뽑기 당첨 로그",
    "report": "🎫 바이옴 제보 로그",
    "admin": "🛠️ 관리자 조정 로그"
}


def build_log_embed(log_type: str, logs: list[dict], page: int, per_page: int = 10) -> discord.Embed:
    title_str = LOG_TYPE_NAMES.get(log_type, "📋 활동 로그")
    total_logs = len(logs)
    max_pages = max(1, min(5, math.ceil(total_logs / per_page)))
    current_page = max(0, min(page, max_pages - 1))

    start_idx = current_page * per_page
    end_idx = start_idx + per_page
    page_logs = logs[start_idx:end_idx]

    embed = discord.Embed(
        title=f"{title_str} (페이지 {current_page + 1}/{max_pages})",
        description=f"총 **{total_logs:,}개**의 기록 중 **{start_idx + 1}~{min(end_idx, total_logs)}번째** 항목입니다." if logs else "아직 기록된 로그 데이터가 없습니다.",
        color=discord.Color.blue()
    )

    if not page_logs:
        embed.add_field(name="ℹ️ 안내", value="이 카테고리에는 아직 저장된 로그가 없습니다.", inline=False)
        embed.set_footer(text="한바헌 봇 로그 시스템 • 1 / 1 페이지")
        return embed

    for log in page_logs:
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

    embed.set_footer(text=f"한바헌 봇 로그 시스템 • {current_page + 1} / {max_pages} 페이지 (최대 5페이지)")
    return embed


class LogCategorySelect(discord.ui.Select):
    def __init__(self, current_log_type: str = "all"):
        options = [
            discord.SelectOption(label="전체 활동 로그", value="all", description="상점, 뽑기, 제보, 관리자 기록을 모두 봅니다.", emoji="📋", default=(current_log_type == "all")),
            discord.SelectOption(label="상점 구매 로그", value="shop", description="유저들의 상점 상품 구매 내역을 봅니다.", emoji="🛒", default=(current_log_type == "shop")),
            discord.SelectOption(label="뽑기 당첨 로그", value="gacha", description="유저들의 럭키 뽑기 당첨 내역을 봅니다.", emoji="🎰", default=(current_log_type == "gacha")),
            discord.SelectOption(label="바이옴 제보 로그", value="report", description="희귀 바이옴 제보 보상 지급 내역을 봅니다.", emoji="🎫", default=(current_log_type == "report")),
            discord.SelectOption(label="관리자 조정 로그", value="admin", description="관리자의 포인트 지급 및 회수 내역을 봅니다.", emoji="🛠️", default=(current_log_type == "admin")),
        ]
        super().__init__(placeholder="조회할 로그 카테고리를 선택하세요...", options=options, row=0)

    async def callback(self, interaction: discord.Interaction):
        log_type = self.values[0]
        guild_id = interaction.guild.id

        # 최대 5페이지 분량 (50개) 로드
        logs = await get_activity_logs(guild_id, log_type=log_type, limit=50)

        view = LogPaginationView(logs=logs, log_type=log_type, current_page=0)
        embed = build_log_embed(log_type, logs, page=0)
        await interaction.response.edit_message(embed=embed, view=view)


class LogPaginationView(discord.ui.View):
    def __init__(self, logs: list[dict], log_type: str = "all", current_page: int = 0, per_page: int = 10):
        super().__init__(timeout=300)
        self.logs = logs
        self.log_type = log_type
        self.current_page = current_page
        self.per_page = per_page
        self.max_pages = max(1, min(5, math.ceil(len(logs) / per_page)))

        # 카테고리 셀렉트 추가
        self.add_item(LogCategorySelect(current_log_type=log_type))

        # 버튼 상태 업데이트
        self.update_buttons()

    def update_buttons(self):
        self.prev_button.disabled = (self.current_page <= 0)
        self.next_button.disabled = (self.current_page >= self.max_pages - 1 or self.current_page >= 4)
        self.page_indicator.label = f"{self.current_page + 1} / {self.max_pages} 페이지"

    @discord.ui.button(label="◀ 이전", style=discord.ButtonStyle.primary, row=1, custom_id="log_prev_btn")
    async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.current_page > 0:
            self.current_page -= 1
            self.update_buttons()
            embed = build_log_embed(self.log_type, self.logs, page=self.current_page)
            await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="1 / 1 페이지", style=discord.ButtonStyle.secondary, disabled=True, row=1, custom_id="log_indicator_btn")
    async def page_indicator(self, interaction: discord.Interaction, button: discord.ui.Button):
        pass

    @discord.ui.button(label="다음 ▶", style=discord.ButtonStyle.primary, row=1, custom_id="log_next_btn")
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.current_page < self.max_pages - 1 and self.current_page < 4:
            self.current_page += 1
            self.update_buttons()
            embed = build_log_embed(self.log_type, self.logs, page=self.current_page)
            await interaction.response.edit_message(embed=embed, view=self)


# ── Cog ───────────────────────────────────────────────────────────────

class Logs(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="로그", description="[관리자 전용] 상점 구매, 뽑기 당첨, 제보, 관리자 기록을 5페이지까지 조회합니다.")
    # DM 에서는 interaction.guild 가 None 이라 AttributeError 로 죽습니다.
    # (실제로 /출석 이 그렇게 터졌습니다). guild_only 를 걸면 디스코드가
    # **DM 목록에 아예 띄우지 않으므로** 방어 코드를 흩뿌릴 필요가 없습니다.
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def view_logs(self, interaction: discord.Interaction):
        guild_id = interaction.guild.id
        log_type = "all"
        # 5페이지 분량 (최대 50개) 로드
        logs = await get_activity_logs(guild_id, log_type=log_type, limit=50)

        embed = build_log_embed(log_type, logs, page=0)
        view = LogPaginationView(logs=logs, log_type=log_type, current_page=0)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


async def setup(bot):
    await bot.add_cog(Logs(bot))
