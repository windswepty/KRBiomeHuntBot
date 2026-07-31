import random
import discord
from discord.ext import commands
from discord import app_commands
from utils.database import (
    get_gacha_settings, save_gacha_settings,
    get_user_points, deduct_user_points, add_user_points, add_activity_log
)


def format_stock_str(stock: int) -> str:
    if stock == -1:
        return "무제한"
    elif stock <= 0:
        return "품절"
    else:
        return f"{stock:,}개 남음"


def get_total_percent(items: list[dict]) -> float:
    return sum(float(item.get("percent", item.get("weight", 0))) for item in items)


def is_valid_100_percent(items: list[dict]) -> bool:
    if not items:
        return False
    total = get_total_percent(items)
    return abs(total - 100.0) < 0.001


# ── Gacha Play View ───────────────────────────────────────────────────

class GachaPlayView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)

    @discord.ui.button(label="🎲 1회 뽑기 실행", style=discord.ButtonStyle.primary, emoji="🎰")
    async def roll_gacha(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild_id = interaction.guild.id
        user_id = interaction.user.id

        settings = await get_gacha_settings(guild_id)
        is_enabled = settings.get("enabled", True)
        is_admin = interaction.user.guild_permissions.administrator

        if not is_enabled and not is_admin:
            await interaction.response.send_message("⚠️ 현재 럭키 뽑기가 **비활성화** 상태입니다.", ephemeral=True)
            return

        price = settings.get("price", 100)
        items = settings.get("items", [])

        if not items:
            await interaction.response.send_message("⚠️ 등록된 뽑기 항목이 없습니다. 관리자에게 문의해 주세요.", ephemeral=True)
            return

        # 재고 남아있는 가용 항목들 필터링
        available_items = [item for item in items if item.get("stock", -1) != 0]
        if not available_items:
            embed = discord.Embed(
                title="⚠️ 모든 뽑기 상품 품절",
                description="현재 남아있는 뽑기 상품의 재고가 모두 소진(품절)되었습니다.",
                color=discord.Color.red()
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        # 전체 등록 항목 기준 100% 검증
        total_pct = get_total_percent(items)
        if not is_valid_100_percent(items):
            embed = discord.Embed(
                title="⚠️ 뽑기 실행 불가",
                description=f"관리자가 설정한 전체 뽑기 항목들의 **확률 합이 100%가 아니어서** 현재 뽑기를 진행할 수 없습니다.\n\n"
                            f"📊 **현재 확률 합계**: `{total_pct:.2f}%` (100%로 설정되어야 함)",
                color=discord.Color.red()
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        # 포인트 차감 시도
        success, remaining = await deduct_user_points(user_id, price)
        if not success:
            embed = discord.Embed(
                title="⚠️ 포인트 부족",
                description=f"뽑기를 진행하려면 **{price:,} P**가 필요합니다.\n\n"
                            f"💰 **현재 보유 포인트**: **{remaining:,} P**",
                color=discord.Color.red()
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        # 가용한 항목들 중에서 확률 추첨 (random.choices)
        weights = [float(item.get("percent", item.get("weight", 0))) for item in available_items]
        selected_item = random.choices(available_items, weights=weights, k=1)[0]

        item_name = selected_item["name"]
        reward_points = selected_item.get("reward_points", 0)
        reward_role_id = selected_item.get("reward_role_id", 0)
        desc = selected_item.get("description", "")
        stock = selected_item.get("stock", -1)

        # 재고 1 차감 (stock > 0 인 경우)
        if stock > 0:
            for item in items:
                if item.get("id") == selected_item.get("id"):
                    item["stock"] -= 1
                    break
            settings["items"] = items
            await save_gacha_settings(guild_id, settings)

        new_stock = selected_item.get("stock", -1)
        stock_text = format_stock_str(new_stock)

        # 포인트 보상인 경우 자동 추가
        new_balance = remaining
        if reward_points > 0:
            new_balance = await add_user_points(user_id, reward_points)

        # 역할 보상 자동 지급 시도
        role_notice = ""
        if reward_role_id and reward_role_id > 0:
            role = interaction.guild.get_role(reward_role_id)
            if role:
                try:
                    await interaction.user.add_roles(role)
                    role_notice = f"\n🎭 **{role.mention}** 역할이 계정에 즉시 지급되었습니다!"
                except Exception as e:
                    role_notice = f"\n⚠️ 역할 자동 지급 실패 (봇 권한/역할 순서 확인 필요: {e})"
            else:
                role_notice = f"\n⚠️ 지정된 역할(ID: `{reward_role_id}`)을 서버에서 찾을 수 없습니다."

        # 로그 기록
        await add_activity_log(guild_id, "gacha", user_id, interaction.user.display_name, f"[{item_name}] 뽑기 당첨 (-{price:,}P)")

        # 결과 임베드
        embed = discord.Embed(
            title="🎰 럭키 뽑기 결과!",
            description=f"🎉 축하합니다! **[{item_name}]** 에 당첨되셨습니다!{role_notice}\n\n"
                        f"💳 **소모 포인트**: -{price:,} P\n"
                        f"💰 **현재 총 보유 포인트**: **{new_balance:,} P**\n"
                        f"📦 **해당 상품 남은 재고**: `{stock_text}`",
            color=discord.Color.gold()
        )
        if desc:
            embed.add_field(name="📝 항목 설명", value=desc, inline=False)

        if reward_points > 0:
            embed.add_field(
                name="🎁 보상 자동 적립",
                value=f"포인트 보상 **+{reward_points:,} P**가 지갑에 자동 적립되었습니다!",
                inline=False
            )

        embed.set_footer(text="한바헌 포인트 럭키 뽑기 시스템")
        await interaction.response.send_message(embed=embed, ephemeral=True)


# ── Admin Gacha Settings Modals & Views ───────────────────────────────

class SetGachaPriceModal(discord.ui.Modal, title="💰 1회 뽑기 가격 설정"):
    price_input = discord.ui.TextInput(
        label="1회 뽑기 가격 (포인트)",
        placeholder="예: 100",
        default="100",
        required=True,
        max_length=10
    )

    async def on_submit(self, interaction: discord.Interaction):
        try:
            price = int(self.price_input.value.strip())
            if price <= 0:
                raise ValueError()
        except ValueError:
            await interaction.response.send_message("⚠️ 1 이상의 올바른 숫자를 입력해 주세요.", ephemeral=True)
            return

        settings = await get_gacha_settings(interaction.guild.id)
        settings["price"] = price
        await save_gacha_settings(interaction.guild.id, settings)

        embed = discord.Embed(
            title="💰 뽑기 가격 변경 완료",
            description=f"1회 뽑기 가격이 **{price:,} P**로 설정되었습니다.",
            color=discord.Color.green()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


class AddGachaItemModal(discord.ui.Modal, title="➕ 뽑기 당첨 항목 및 역할 추가"):
    item_name = discord.ui.TextInput(
        label="당첨 항목 이름",
        placeholder="예: 💎 1,000P 대박 당첨! 또는 🎭 VIP 역할",
        required=True,
        max_length=50
    )
    percent_input = discord.ui.TextInput(
        label="당첨 확률 (%)",
        placeholder="예: 10 (10% 의미) 또는 0.5 (0.5% 의미)",
        required=True,
        max_length=10
    )
    item_stock_input = discord.ui.TextInput(
        label="재고/수량 (무제한은 -1 입력)",
        placeholder="예: 1 (1개 한정) 또는 -1 (무제한)",
        default="-1",
        required=True,
        max_length=10
    )
    role_id_input = discord.ui.TextInput(
        label="자동 지급 역할 ID (없으면 0 입력)",
        placeholder="예: 1234567890123456789 (0 입력 시 역할 보상 없음)",
        default="0",
        required=True,
        max_length=25
    )
    reward_points_input = discord.ui.TextInput(
        label="포인트 보상 (없으면 0 입력)",
        placeholder="예: 1000 (0 입력 시 포인트 지급 없음)",
        default="0",
        required=True,
        max_length=10
    )

    async def on_submit(self, interaction: discord.Interaction):
        try:
            percent = float(self.percent_input.value.strip())
            stock = int(self.item_stock_input.value.strip())
            role_id = int(self.role_id_input.value.strip())
            reward_pts = int(self.reward_points_input.value.strip())
            if percent <= 0 or percent > 100 or (stock < 0 and stock != -1) or role_id < 0 or reward_pts < 0:
                raise ValueError()
        except ValueError:
            await interaction.response.send_message("⚠️ 확률(0~100), 재고(-1 이상), 역할 ID(숫자), 포인트 보상을 정확히 입력해 주세요.", ephemeral=True)
            return

        settings = await get_gacha_settings(interaction.guild.id)
        items = settings.get("items", [])
        new_id = max([item.get("id", 0) for item in items], default=0) + 1

        new_item = {
            "id": new_id,
            "name": self.item_name.value.strip(),
            "percent": percent,
            "weight": percent,
            "stock": stock,
            "reward_role_id": role_id,
            "reward_points": reward_pts,
            "description": ""
        }
        items.append(new_item)
        settings["items"] = items
        await save_gacha_settings(interaction.guild.id, settings)

        current_total = get_total_percent(items)
        status_msg = "✅ 전체 확률 합계가 **100%**로 완벽합니다! (뽑기 플레이 가능)" if abs(current_total - 100.0) < 0.001 else f"⚠️ 현재 총 확률 합계가 **{current_total:.2f}%**입니다. (100%로 맞춰야 뽑기 생성 가능)"

        role_info = ""
        if role_id > 0:
            role = interaction.guild.get_role(role_id)
            role_info = f" | 지급 역할: {role.mention}" if role else f" | 지급 역할 ID: `{role_id}`"

        stock_str = format_stock_str(stock)
        embed = discord.Embed(
            title="✅ 뽑기 항목 추가 완료",
            description=f"**{new_item['name']}** 항목(확률: **{percent}%** | 재고: `{stock_str}`{role_info})이 추가되었습니다.\n\n{status_msg}",
            color=discord.Color.green() if abs(current_total - 100.0) < 0.001 else discord.Color.orange()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


class DeleteGachaItemSelect(discord.ui.Select):
    def __init__(self, items: list[dict]):
        options = [
            discord.SelectOption(
                label=f"{item['name']} [{format_stock_str(item.get('stock', -1))}]",
                value=str(item["id"]),
                description=f"확률: {item.get('percent', item.get('weight', 0))}% | 역할 ID: {item.get('reward_role_id', 0)}",
                emoji="🗑️"
            )
            for item in items
        ]
        super().__init__(placeholder="삭제할 뽑기 항목을 선택하세요...", options=options)

    async def callback(self, interaction: discord.Interaction):
        target_id = int(self.values[0])
        settings = await get_gacha_settings(interaction.guild.id)
        items = settings.get("items", [])

        deleted_item = next((item for item in items if item.get("id") == target_id), None)
        new_items = [item for item in items if item.get("id") != target_id]

        settings["items"] = new_items
        await save_gacha_settings(interaction.guild.id, settings)

        item_name = deleted_item["name"] if deleted_item else "선택 항목"
        current_total = get_total_percent(new_items)
        status_msg = "✅ 전체 확률 합계가 **100%**로 완벽합니다!" if abs(current_total - 100.0) < 0.001 else f"⚠️ 현재 총 확률 합계가 **{current_total:.2f}%**입니다. (100%로 맞춰야 뽑기 생성 가능)"

        embed = discord.Embed(
            title="🗑️ 뽑기 항목 삭제 완료",
            description=f"**{item_name}** 항목이 삭제되었습니다.\n\n{status_msg}",
            color=discord.Color.orange()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


class DeleteGachaItemView(discord.ui.View):
    def __init__(self, items: list[dict]):
        super().__init__(timeout=180)
        self.add_item(DeleteGachaItemSelect(items))


class GachaSettingsSelect(discord.ui.Select):
    def __init__(self, current_settings: dict):
        self.current_settings = current_settings
        is_enabled = current_settings.get("enabled", True)
        options = [
            discord.SelectOption(
                label="시스템 활성화 / 비활성화",
                value="toggle",
                description=f"현재: {'✅ 활성화' if is_enabled else '❌ 비활성화'}",
                emoji="⚡"
            ),
            discord.SelectOption(label="1회 뽑기 가격 설정", value="price", description="뽑기 1회에 필요한 포인트를 변경합니다.", emoji="💰"),
            discord.SelectOption(label="당첨 항목 및 역할 추가", value="add", description="새로운 당첨 항목, 확률(%), 수량, 지급 역할을 추가합니다.", emoji="➕"),
            discord.SelectOption(label="당첨 항목 삭제", value="delete", description="등록된 뽑기 당첨 항목을 삭제합니다.", emoji="🗑️"),
            discord.SelectOption(label="전체 확률표 및 재고 확인", value="list", description="현재 등록된 항목, 역할, 총 100% 달성 여부를 확인합니다.", emoji="📊"),
        ]
        super().__init__(placeholder="뽑기 관리 기능을 선택하세요...", options=options)

    async def callback(self, interaction: discord.Interaction):
        choice = self.values[0]

        if choice == "toggle":
            current = self.current_settings.get("enabled", True)
            new_val = not current
            await save_gacha_settings(interaction.guild.id, {"enabled": new_val})
            status = "✅ 활성화" if new_val else "❌ 비활성화"
            embed = discord.Embed(
                title="⚡ 럭키 뽑기 시스템 상태 변경",
                description=f"럭키 뽑기가 **{status}** 상태로 변경되었습니다.",
                color=discord.Color.green() if new_val else discord.Color.red()
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)

        elif choice == "price":
            settings = await get_gacha_settings(interaction.guild.id)
            modal = SetGachaPriceModal()
            modal.price_input.default = str(settings.get("price", 100))
            await interaction.response.send_modal(modal)

        elif choice == "add":
            await interaction.response.send_modal(AddGachaItemModal())

        elif choice == "delete":
            settings = await get_gacha_settings(interaction.guild.id)
            items = settings.get("items", [])
            if not items:
                await interaction.response.send_message("⚠️ 삭제할 등록된 뽑기 항목이 없습니다.", ephemeral=True)
                return
            await interaction.response.send_message("🗑️ 삭제할 항목을 아래 메뉴에서 선택하세요.", view=DeleteGachaItemView(items), ephemeral=True)

        elif choice == "list":
            settings = await get_gacha_settings(interaction.guild.id)
            price = settings.get("price", 100)
            items = settings.get("items", [])

            if not items:
                embed = discord.Embed(
                    title="📊 뽑기 확률표 및 재고 현황",
                    description=f"💰 **1회 뽑기 가격**: {price:,} P\n\n현재 등록된 뽑기 항목이 없습니다.",
                    color=discord.Color.gold()
                )
            else:
                total_pct = get_total_percent(items)
                status_str = "✅ **100.00%** (뽑기 정상 가동 가능)" if abs(total_pct - 100.0) < 0.001 else f"⚠️ **{total_pct:.2f}%** (100%가 되지 않아 뽑기 생성 불가!)"

                embed = discord.Embed(
                    title="📊 뽑기 항목, 확률표 및 재고 현황",
                    description=f"💰 **1회 뽑기 가격**: **{price:,} P** | 총 **{len(items)}개** 항목\n"
                                f"📊 **전체 확률 합계**: {status_str}",
                    color=discord.Color.green() if abs(total_pct - 100.0) < 0.001 else discord.Color.red()
                )
                for item in items:
                    pct = float(item.get("percent", item.get("weight", 0)))
                    stock_str = format_stock_str(item.get("stock", -1))
                    reward_pts = item.get("reward_points", 0)
                    role_id = item.get("reward_role_id", 0)

                    extra_info = []
                    if reward_pts > 0:
                        extra_info.append(f"+{reward_pts:,}P")
                    if role_id > 0:
                        role = interaction.guild.get_role(role_id)
                        role_str = role.mention if role else f"ID: {role_id}"
                        extra_info.append(f"역할: {role_str}")

                    extra_str = f" ({', '.join(extra_info)})" if extra_info else ""

                    embed.add_field(
                        name=f"🎲 {item['name']}",
                        value=f"• **확률**: `{pct:.2f}%` | **재고**: `{stock_str}`{extra_str}",
                        inline=False
                    )
            await interaction.response.send_message(embed=embed, ephemeral=True)


class GachaSettingsView(discord.ui.View):
    def __init__(self, current_settings: dict):
        super().__init__(timeout=180)
        self.add_item(GachaSettingsSelect(current_settings))


# ── Cog ───────────────────────────────────────────────────────────────

class Gacha(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="뽑기", description="포인트를 소비하여 무작위 보상 및 역할을 획득하는 럭키 뽑기를 진행합니다.")
    async def roll(self, interaction: discord.Interaction):
        settings = await get_gacha_settings(interaction.guild.id)
        is_enabled = settings.get("enabled", True)
        is_admin = interaction.user.guild_permissions.administrator

        if not is_enabled and not is_admin:
            await interaction.response.send_message("⚠️ 현재 럭키 뽑기가 **비활성화** 상태입니다. 관리자에게 문의해 주세요.", ephemeral=True)
            return

        price = settings.get("price", 100)
        items = settings.get("items", [])
        user_points = await get_user_points(interaction.user.id)

        title_str = "🎰 [비활성화 상태] 한바헌 럭키 뽑기" if not is_enabled else "🎰 한바헌 럭키 뽑기"

        if not items:
            embed = discord.Embed(
                title=title_str,
                description=f"현재 준비된 뽑기 항목이 없습니다!\n\n💰 **내 보유 포인트**: **{user_points:,} P**",
                color=discord.Color.gold() if is_enabled else discord.Color.red()
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        total_pct = get_total_percent(items)
        is_ready = is_valid_100_percent(items)
        status_line = "✅ 현재 뽑기를 플레이하실 수 있습니다." if is_ready else f"⚠️ 현재 전체 확률 합계가 **{total_pct:.2f}%**로 100%가 되지 않아 뽑기를 진행할 수 없습니다."

        embed = discord.Embed(
            title=title_str,
            description=f"아래 **[🎲 1회 뽑기 실행]** 버튼을 눌러 행운을 시험해 보세요!\n\n"
                        f"💳 **1회 뽑기 가격**: **{price:,} P**\n"
                        f"💰 **현재 내 보유 포인트**: **{user_points:,} P**\n\n"
                        f"{status_line}",
            color=discord.Color.gold() if is_ready else discord.Color.red()
        )
        for item in items:
            pct = float(item.get("percent", item.get("weight", 0)))
            stock_str = format_stock_str(item.get("stock", -1))
            role_id = item.get("reward_role_id", 0)

            info_lines = [f"• **확률**: `{pct:.2f}%` | **재고**: `{stock_str}`"]
            if role_id > 0:
                role = interaction.guild.get_role(role_id)
                role_info = role.mention if role else f"ID: {role_id}"
                info_lines.append(f"• **보상 혜택**: {role_info}")
            elif item.get("reward_points", 0) > 0:
                info_lines.append(f"• **보상 혜택**: `+{item['reward_points']:,} P` 적립")

            embed.add_field(
                name=f"🎁 {item['name']}",
                value="\n".join(info_lines),
                inline=False
            )
        embed.set_footer(text="당첨된 역할이나 포인트는 계정에 즉시 지급됩니다.")

        view = GachaPlayView()
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @app_commands.command(name="뽑기설정", description="[관리자 전용] 럭키 뽑기의 가격, 당첨 항목, 확률(총합 100%), 재고, 역할을 설정합니다.")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def gacha_settings(self, interaction: discord.Interaction):
        settings = await get_gacha_settings(interaction.guild.id)
        enabled_text = "✅ 활성화" if settings.get("enabled", True) else "❌ 비활성화"
        price = settings.get("price", 100)
        items = settings.get("items", [])

        total_pct = get_total_percent(items)
        status_str = "✅ **100%** (뽑기 생성 완료)" if is_valid_100_percent(items) else f"⚠️ **{total_pct:.2f}%** (100%를 맞춰야 유저들이 뽑기 가능)"

        embed = discord.Embed(
            title="⚙️ 럭키 뽑기, 역할 및 재고 설정 관리",
            description=f"⚡ 시스템 상태: **{enabled_text}** | 💰 1회 뽑기 가격: **{price:,} P** | 📦 항목 수: **{len(items)}개**\n"
                        f"📊 전체 확률 합계: {status_str}\n\n"
                        "아래 드롭다운 메뉴에서 설정할 관리 기능을 선택하세요.",
            color=discord.Color.dark_gold()
        )
        view = GachaSettingsView(settings)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


async def setup(bot):
    await bot.add_cog(Gacha(bot))
