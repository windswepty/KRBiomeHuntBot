import discord
from discord.ext import commands
from discord import app_commands
from utils.database import get_shop_items, save_shop_items, get_user_points, deduct_user_points


# ── Shop Purchase View ────────────────────────────────────────────────

class ShopSelect(discord.ui.Select):
    def __init__(self, items: list[dict]):
        self.items_dict = {str(item["id"]): item for item in items}
        options = []
        for item in items:
            options.append(
                discord.SelectOption(
                    label=item["name"],
                    value=str(item["id"]),
                    description=f"가격: {item['price']:,} P | {item.get('description', '')[:40]}",
                    emoji="🛒"
                )
            )
        super().__init__(placeholder="구매할 상품을 선택하세요...", options=options)

    async def callback(self, interaction: discord.Interaction):
        item_id = self.values[0]
        selected_item = self.items_dict.get(item_id)
        if not selected_item:
            await interaction.response.send_message("⚠️ 선택한 상품 정보를 찾을 수 없습니다.", ephemeral=True)
            return

        price = selected_item["price"]
        item_name = selected_item["name"]

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

        # 구매 성공
        embed = discord.Embed(
            title="🎉 상품 구매 완료!",
            description=f"**{item_name}** 상품을 성공적으로 구매하셨습니다!\n\n"
                        f"💳 **소모 포인트**: -{price:,} P\n"
                        f"💰 **잔여 포인트**: **{remaining:,} P**",
            color=discord.Color.green()
        )
        if selected_item.get("description"):
            embed.add_field(name="📝 상품 설명", value=selected_item["description"], inline=False)
        embed.set_footer(text="구매 내역이 관리자에게 전달되었거나 적용을 위해 관리자에게 안내해 주세요.")

        await interaction.response.send_message(embed=embed, ephemeral=True)


class ShopView(discord.ui.View):
    def __init__(self, items: list[dict]):
        super().__init__(timeout=180)
        self.add_item(ShopSelect(items))


# ── Admin Shop Settings Modals & Views ────────────────────────────────

class AddShopItemModal(discord.ui.Modal, title="➕ 상점 상품 추가"):
    item_name = discord.ui.TextInput(
        label="상품 이름",
        placeholder="예: 바이옴 감지권 1회",
        required=True,
        max_length=50
    )
    item_price = discord.ui.TextInput(
        label="상품 가격 (포인트)",
        placeholder="예: 1000",
        required=True,
        max_length=10
    )
    item_desc = discord.ui.TextInput(
        label="상품 설명",
        placeholder="예: 희귀 바이옴을 1회 탐지할 수 있는 상품입니다.",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=200
    )

    async def on_submit(self, interaction: discord.Interaction):
        try:
            price = int(self.item_price.value.strip())
            if price <= 0:
                raise ValueError()
        except ValueError:
            await interaction.response.send_message("⚠️ 올바른 가격(숫자)을 입력해 주세요.", ephemeral=True)
            return

        items = await get_shop_items(interaction.guild.id)
        new_id = max([item.get("id", 0) for item in items], default=0) + 1

        new_item = {
            "id": new_id,
            "name": self.item_name.value.strip(),
            "price": price,
            "description": self.item_desc.value.strip() if self.item_desc.value else ""
        }
        items.append(new_item)
        await save_shop_items(interaction.guild.id, items)

        embed = discord.Embed(
            title="✅ 상품 추가 완료",
            description=f"**{new_item['name']}** (가격: **{price:,} P**) 상품이 상점에 등록되었습니다.",
            color=discord.Color.green()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


class DeleteShopItemSelect(discord.ui.Select):
    def __init__(self, items: list[dict]):
        options = [
            discord.SelectOption(
                label=item["name"],
                value=str(item["id"]),
                description=f"가격: {item['price']:,}P",
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
            discord.SelectOption(label="상품 추가", value="add", description="새로운 상점 상품을 등록합니다.", emoji="➕"),
            discord.SelectOption(label="상품 삭제", value="delete", description="등록된 상점 상품을 삭제합니다.", emoji="🗑️"),
            discord.SelectOption(label="상품 목록 확인", value="list", description="현재 등록된 상점 상품 목록을 봅니다.", emoji="📜"),
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
                    title="🛒 등록된 상점 상품 목록",
                    description=f"총 **{len(items)}개**의 상품이 등록되어 있습니다.",
                    color=discord.Color.gold()
                )
                for item in items:
                    desc = item.get('description', '설명 없음')
                    embed.add_field(
                        name=f"📦 [{item['id']}] {item['name']} - {item['price']:,} P",
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

    @app_commands.command(name="상점", description="포인트로 다양한 상품이나 혜택을 구매합니다.")
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
            embed.add_field(
                name=f"📦 {item['name']} (💳 {item['price']:,} P)",
                value=f"📝 {item.get('description', '설명 없음')}",
                inline=False
            )
        embed.set_footer(text="선택 메뉴에서 항목을 고르면 즉시 포인트가 차감되고 구매됩니다.")

        view = ShopView(items)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @app_commands.command(name="상점설정", description="[관리자 전용] 포인트 상점의 상품을 추가, 수정, 삭제합니다.")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def shop_settings(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="⚙️ 상점 설정 관리",
            description="아래 드롭다운 메뉴에서 원하시는 상점 관리 기능을 선택하세요.",
            color=discord.Color.dark_purple()
        )
        view = ShopSettingsView()
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


async def setup(bot):
    await bot.add_cog(Shop(bot))
