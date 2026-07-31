import discord
from discord.ext import commands
from discord import app_commands
from utils.database import get_shop_items, save_shop_items, get_user_points, deduct_user_points, add_activity_log


def format_stock_str(stock: int) -> str:
    if stock == -1:
        return "무제한"
    elif stock <= 0:
        return "품절"
    else:
        return f"{stock:,}개 남음"


# ── Shop Purchase View ────────────────────────────────────────────────

class ShopSelect(discord.ui.Select):
    def __init__(self, items: list[dict]):
        self.items_dict = {str(item["id"]): item for item in items}
        options = []
        for item in items:
            stock = item.get("stock", -1)
            stock_label = format_stock_str(stock)
            options.append(
                discord.SelectOption(
                    label=f"{item['name']} [{stock_label}]",
                    value=str(item["id"]),
                    description=f"가격: {item['price']:,} P | {item.get('description', '')[:35]}",
                    emoji="🛒" if stock != 0 else "🚫"
                )
            )
        super().__init__(placeholder="구매할 상품을 선택하세요...", options=options)

    async def callback(self, interaction: discord.Interaction):
        item_id = self.values[0]
        guild_id = interaction.guild.id
        items = await get_shop_items(guild_id)

        selected_item = next((item for item in items if str(item.get("id")) == item_id), None)
        if not selected_item:
            await interaction.response.send_message("⚠️ 선택한 상품 정보를 찾을 수 없습니다.", ephemeral=True)
            return

        stock = selected_item.get("stock", -1)
        item_name = selected_item["name"]
        price = selected_item["price"]
        reward_role_id = selected_item.get("reward_role_id", 0)

        # 재고 검사 (0개면 품절)
        if stock == 0:
            embed = discord.Embed(
                title="⚠️ 상품 품절",
                description=f"**{item_name}** 상품은 현재 **품절(재고 0개)**되어 구매할 수 없습니다.",
                color=discord.Color.red()
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        # 포인트 차감 시도
        success, remaining = await deduct_user_points(interaction.user.id, price)
        if not success:
            embed = discord.Embed(
                title="⚠️ 포인트 부족",
                description=f"**{item_name}** 상품을 구매하기 위해 **{price:,} P**가 필요합니다.\n\n"
                            f"💰 **현재 보유 포인트**: **{remaining:,} P**",
                color=discord.Color.red()
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        # 재고 차감 (stock > 0 인 경우 1 차감)
        if stock > 0:
            selected_item["stock"] -= 1
            await save_shop_items(guild_id, items)

        new_stock = selected_item.get("stock", -1)
        stock_text = format_stock_str(new_stock)

        # 역할 자동 지급 시도
        role_notice = ""
        if reward_role_id and reward_role_id > 0:
            role = interaction.guild.get_role(reward_role_id)
            if role:
                try:
                    await interaction.user.add_roles(role)
                    role_notice = f"\n🎭 **{role.mention}** 역할이 계정에 즉시 지급되었습니다!"
                except Exception as e:
                    role_notice = f"\n⚠️ 역할 자동 부여 실패 (봇 권한/역할 순서 확인 필요: {e})"
            else:
                role_notice = f"\n⚠️ 지정된 역할(ID: `{reward_role_id}`)을 서버에서 찾을 수 없습니다."

        # 로그 기록
        await add_activity_log(guild_id, "shop", interaction.user.id, interaction.user.display_name, f"[{item_name}] 상점 상품 구매 (-{price:,}P)")

        # 구매 성공
        embed = discord.Embed(
            title="🎉 상품 구매 완료!",
            description=f"**{item_name}** 상품을 성공적으로 구매하셨습니다!{role_notice}\n\n"
                        f"💳 **소모 포인트**: -{price:,} P\n"
                        f"💰 **잔여 포인트**: **{remaining:,} P**\n"
                        f"📦 **남은 재고**: `{stock_text}`",
            color=discord.Color.green()
        )
        if selected_item.get("description"):
            embed.add_field(name="📝 상품 설명", value=selected_item["description"], inline=False)
        embed.set_footer(text="한바헌 포인트 상점 시스템")

        await interaction.response.send_message(embed=embed, ephemeral=True)


class ShopView(discord.ui.View):
    def __init__(self, items: list[dict]):
        super().__init__(timeout=180)
        self.add_item(ShopSelect(items))


# ── Admin Shop Settings Modals & Views ────────────────────────────────

class AddShopItemModal(discord.ui.Modal, title="➕ 상점 상품 및 역할 추가"):
    item_name = discord.ui.TextInput(
        label="상품 이름",
        placeholder="예: VIP 역할 구매 또는 바이옴 감지권",
        required=True,
        max_length=50
    )
    item_price = discord.ui.TextInput(
        label="상품 가격 (포인트)",
        placeholder="예: 1000",
        required=True,
        max_length=10
    )
    item_stock = discord.ui.TextInput(
        label="재고 수량 (무제한은 -1 입력)",
        placeholder="예: 10 (10개 수량) 또는 -1 (무제한)",
        default="-1",
        required=True,
        max_length=10
    )
    role_id_input = discord.ui.TextInput(
        label="구매 시 즉시 지급할 역할 ID (없으면 0)",
        placeholder="예: 1234567890123456789 (0 입력 시 역할 없음)",
        default="0",
        required=True,
        max_length=25
    )
    item_desc = discord.ui.TextInput(
        label="상품 설명",
        placeholder="예: 구매 시 지정된 역할이 즉시 계정에 부여됩니다.",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=200
    )

    async def on_submit(self, interaction: discord.Interaction):
        try:
            price = int(self.item_price.value.strip())
            stock = int(self.item_stock.value.strip())
            role_id = int(self.role_id_input.value.strip())
            if price <= 0 or (stock < 0 and stock != -1) or role_id < 0:
                raise ValueError()
        except ValueError:
            await interaction.response.send_message("⚠️ 가격(1 이상), 재고(-1 이상), 역할 ID(숫자)를 올바르게 입력해 주세요.", ephemeral=True)
            return

        items = await get_shop_items(interaction.guild.id)
        new_id = max([item.get("id", 0) for item in items], default=0) + 1

        new_item = {
            "id": new_id,
            "name": self.item_name.value.strip(),
            "price": price,
            "stock": stock,
            "reward_role_id": role_id,
            "description": self.item_desc.value.strip() if self.item_desc.value else ""
        }
        items.append(new_item)
        await save_shop_items(interaction.guild.id, items)

        role_info = ""
        if role_id > 0:
            role = interaction.guild.get_role(role_id)
            role_info = f" | 부여 역할: {role.mention}" if role else f" | 부여 역할 ID: `{role_id}`"

        stock_str = format_stock_str(stock)
        embed = discord.Embed(
            title="✅ 상점 상품 추가 완료",
            description=f"**{new_item['name']}** (가격: **{price:,} P** | 재고: `{stock_str}`{role_info}) 상품이 등록되었습니다.",
            color=discord.Color.green()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


class DeleteShopItemSelect(discord.ui.Select):
    def __init__(self, items: list[dict]):
        options = [
            discord.SelectOption(
                label=f"{item['name']} [{format_stock_str(item.get('stock', -1))}]",
                value=str(item["id"]),
                description=f"가격: {item['price']:,}P | 역할 ID: {item.get('reward_role_id', 0)}",
                emoji="🗑️"
            )
            for item in items
        ]
        super().__init__(placeholder="삭제할 상품을 선택하세요...", options=options)

    async def callback(self, interaction: discord.Interaction):
        target_id = int(self.values[0])
        items = await get_shop_items(interaction.guild.id)

        deleted_item = next((item for item in items if item.get("id") == target_id), None)
        new_items = [item for item in items if item.get("id") != target_id]

        await save_shop_items(interaction.guild.id, new_items)

        item_name = deleted_item["name"] if deleted_item else "선택한 상품"
        embed = discord.Embed(
            title="🗑️ 상품 삭제 완료",
            description=f"**{item_name}** 상품이 상점에서 삭제되었습니다.",
            color=discord.Color.orange()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


class DeleteShopItemView(discord.ui.View):
    def __init__(self, items: list[dict]):
        super().__init__(timeout=180)
        self.add_item(DeleteShopItemSelect(items))


class ShopSettingsSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="상품 및 역할 추가", value="add", description="새로운 상점 상품, 가격, 지급 역할을 등록합니다.", emoji="➕"),
            discord.SelectOption(label="상품 삭제", value="delete", description="등록된 상점 상품을 삭제합니다.", emoji="🗑️"),
            discord.SelectOption(label="상품 목록 확인", value="list", description="현재 등록된 상점 상품 및 재고 현황을 확인합니다.", emoji="📜"),
        ]
        super().__init__(placeholder="상점 관리 기능을 선택하세요...", options=options)

    async def callback(self, interaction: discord.Interaction):
        choice = self.values[0]

        if choice == "add":
            await interaction.response.send_modal(AddShopItemModal())

        elif choice == "delete":
            items = await get_shop_items(interaction.guild.id)
            if not items:
                await interaction.response.send_message("⚠️ 삭제할 등록된 상품이 없습니다.", ephemeral=True)
                return
            await interaction.response.send_message("🗑️ 삭제할 상품을 아래 메뉴에서 선택하세요.", view=DeleteShopItemView(items), ephemeral=True)

        elif choice == "list":
            items = await get_shop_items(interaction.guild.id)
            if not items:
                embed = discord.Embed(
                    title="🛒 등록된 상점 상품",
                    description="현재 상점에 등록된 상품이 없습니다.",
                    color=discord.Color.gold()
                )
            else:
                embed = discord.Embed(
                    title="🛒 등록된 상점 상품 및 재고 현황",
                    description=f"총 **{len(items)}개**의 상품이 등록되어 있습니다.",
                    color=discord.Color.gold()
                )
                for item in items:
                    desc = item.get('description', '설명 없음')
                    stock_str = format_stock_str(item.get('stock', -1))
                    role_id = item.get('reward_role_id', 0)
                    role_str = ""
                    if role_id > 0:
                        role = interaction.guild.get_role(role_id)
                        role_str = f" | 부여 역할: {role.mention}" if role else f" | 부여 역할 ID: `{role_id}`"

                    embed.add_field(
                        name=f"📦 [{item['id']}] {item['name']} - {item['price']:,} P (재고: `{stock_str}`{role_str})",
                        value=f"📝 {desc}",
                        inline=False
                    )
            await interaction.response.send_message(embed=embed, ephemeral=True)


class ShopSettingsView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)
        self.add_item(ShopSettingsSelect())


# ── Cog ───────────────────────────────────────────────────────────────

class Shop(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="상점", description="포인트로 다양한 상품이나 혜택, 역할을 구매합니다.")
    async def point_shop(self, interaction: discord.Interaction):
        items = await get_shop_items(interaction.guild.id)
        user_points = await get_user_points(interaction.user.id)

        if not items:
            embed = discord.Embed(
                title="🛒 한바헌 포인트 상점",
                description=f"현재 준비된 상점 상품이 없습니다!\n\n💰 **내 보유 포인트**: **{user_points:,} P**",
                color=discord.Color.purple()
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        embed = discord.Embed(
            title="🛒 한바헌 포인트 상점",
            description=f"원하시는 상품을 아래 메뉴에서 선택하여 구매해 보세요!\n\n💰 **현재 내 보유 포인트**: **{user_points:,} P**",
            color=discord.Color.purple()
        )
        for item in items:
            stock_str = format_stock_str(item.get('stock', -1))
            role_id = item.get('reward_role_id', 0)
            role_info = ""
            if role_id > 0:
                role = interaction.guild.get_role(role_id)
                role_info = f"\n🎭 혜택: {role.mention} 역할 부여" if role else "\n🎭 혜택: 역할 부여"

            embed.add_field(
                name=f"📦 {item['name']} (💳 {item['price']:,} P | 재고: `{stock_str}`)",
                value=f"📝 {item.get('description', '설명 없음')}{role_info}",
                inline=False
            )
        embed.set_footer(text="선택 메뉴에서 항목을 고르면 즉시 포인트 차감 및 역할이 부여됩니다.")

        view = ShopView(items)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @app_commands.command(name="상점설정", description="[관리자 전용] 포인트 상점의 상품, 가격, 재고, 역할을 관리합니다.")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def shop_settings(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="⚙️ 상점 및 역할 설정 관리",
            description="아래 드롭다운 메뉴에서 원하시는 상점 관리 기능을 선택하세요.",
            color=discord.Color.dark_purple()
        )
        view = ShopSettingsView()
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


async def setup(bot):
    await bot.add_cog(Shop(bot))
