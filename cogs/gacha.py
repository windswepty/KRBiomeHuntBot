import random
import discord
from discord.ext import commands
from discord import app_commands
from utils.database import (
    get_gacha_settings, save_gacha_settings,
    get_user_points, deduct_user_points, add_user_points
)


# ── Gacha Play View ───────────────────────────────────────────────────

class GachaPlayView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)

    @discord.ui.button(label="🎲 1회 뽑기 실행", style=discord.ButtonStyle.primary, emoji="🎰")
    async def roll_gacha(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild_id = interaction.guild.id
        user_id = interaction.user.id

        settings = await get_gacha_settings(guild_id)
        price = settings.get("price", 100)
        items = settings.get("items", [])

        if not items:
            await interaction.response.send_message("⚠️ 등록된 뽑기 항목이 없습니다. 관리자에게 문의해 주세요.", ephemeral=True)
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

        # 확률 추첨 (random.choices)
        weights = [item.get("weight", 1) for item in items]
        selected_item = random.choices(items, weights=weights, k=1)[0]

        item_name = selected_item["name"]
        reward_points = selected_item.get("reward_points", 0)
        desc = selected_item.get("description", "")

        # 만약 포인트 보상이 포함된 당첨 항목이면 자동 추가
        new_balance = remaining
        if reward_points > 0:
            new_balance = await add_user_points(user_id, reward_points)

        # 결과 임베드
        embed = discord.Embed(
            title="🎰 럭키 뽑기 결과!",
            description=f"🎉 축하합니다! **[{item_name}]** 에 당첨되셨습니다!\n\n"
                        f"💳 **소모 포인트**: -{price:,} P\n"
                        f"💰 **현재 총 보유 포인트**: **{new_balance:,} P**",
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


class AddGachaItemModal(discord.ui.Modal, title="➕ 뽑기 당첨 항목 추가"):
    item_name = discord.ui.TextInput(
        label="당첨 항목 이름",
        placeholder="예: 💎 1,000P 대박 당첨! 또는 🎁 희귀 칭호",
        required=True,
        max_length=50
    )
    weight_input = discord.ui.TextInput(
        label="확률 가중치 (숫자가 높을수록 잘 나옴)",
        placeholder="예: 10 (가중치 비율에 따라 확률 계산)",
        default="10",
        required=True,
        max_length=10
    )
    reward_points_input = discord.ui.TextInput(
        label="포인트 보상 (포인트 당첨인 경우 입력, 없으면 0)",
        placeholder="예: 1000 (0 입력 시 포인트 지급 없음)",
        default="0",
        required=True,
        max_length=10
    )
    item_desc = discord.ui.TextInput(
        label="항목 설명",
        placeholder="예: 관리자에게 수령할 보상 메시지 등",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=200
    )

    async def on_submit(self, interaction: discord.Interaction):
        try:
            weight = int(self.weight_input.value.strip())
            reward_pts = int(self.reward_points_input.value.strip())
            if weight <= 0 or reward_pts < 0:
                raise ValueError()
        except ValueError:
            await interaction.response.send_message("⚠️ 가중치는 1 이상, 포인트 보상은 0 이상의 숫자를 입력해 주세요.", ephemeral=True)
            return

        settings = await get_gacha_settings(interaction.guild.id)
        items = settings.get("items", [])
        new_id = max([item.get("id", 0) for item in items], default=0) + 1

        new_item = {
            "id": new_id,
            "name": self.item_name.value.strip(),
            "weight": weight,
            "reward_points": reward_pts,
            "description": self.item_desc.value.strip() if self.item_desc.value else ""
        }
        items.append(new_item)
        settings["items"] = items
        await save_gacha_settings(interaction.guild.id, settings)

        embed = discord.Embed(
            title="✅ 뽑기 항목 추가 완료",
            description=f"**{new_item['name']}** 항목(가중치: **{weight}**)이 뽑기 리스트에 추가되었습니다.",
            color=discord.Color.green()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


class DeleteGachaItemSelect(discord.ui.Select):
    def __init__(self, items: list[dict]):
        options = [
            discord.SelectOption(
                label=item["name"],
                value=str(item["id"]),
                description=f"가중치: {item.get('weight', 1)} | 포인트 보상: {item.get('reward_points', 0):,}P",
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
        embed = discord.Embed(
            title="🗑️ 뽑기 항목 삭제 완료",
            description=f"**{item_name}** 항목이 뽑기 리스트에서 삭제되었습니다.",
            color=discord.Color.orange()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


class DeleteGachaItemView(discord.ui.View):
    def __init__(self, items: list[dict]):
        super().__init__(timeout=180)
        self.add_item(DeleteGachaItemSelect(items))


class GachaSettingsSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="1회 뽑기 가격 설정", value="price", description="뽑기 1회에 필요한 포인트를 변경합니다.", emoji="💰"),
            discord.SelectOption(label="당첨 항목 추가", value="add", description="새로운 뽑기 당첨 항목과 확률 가중치를 추가합니다.", emoji="➕"),
            discord.SelectOption(label="당첨 항목 삭제", value="delete", description="등록된 뽑기 당첨 항목을 삭제합니다.", emoji="🗑️"),
            discord.SelectOption(label="전체 확률표 확인", value="list", description="현재 등록된 뽑기 항목과 계산된 확률(%)을 확인합니다.", emoji="📊"),
        ]
        super().__init__(placeholder="뽑기 관리 기능을 선택하세요...", options=options)

    async def callback(self, interaction: discord.Interaction):
        choice = self.values[0]

        if choice == "price":
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
                    title="📊 뽑기 확률표",
                    description=f"💰 **1회 뽑기 가격**: {price:,} P\n\n현재 등록된 뽑기 항목이 없습니다.",
                    color=discord.Color.gold()
                )
            else:
                total_weight = sum(item.get("weight", 1) for item in items)
                embed = discord.Embed(
                    title="📊 뽑기 항목 및 확률표",
                    description=f"💰 **1회 뽑기 가격**: **{price:,} P** | 총 **{len(items)}개** 항목 등록됨",
                    color=discord.Color.gold()
                )
                for item in items:
                    weight = item.get("weight", 1)
                    percent = (weight / total_weight) * 100 if total_weight > 0 else 0
                    reward_pts = item.get("reward_points", 0)
                    reward_info = f" (+{reward_pts:,}P 자동지급)" if reward_pts > 0 else ""

                    embed.add_field(
                        name=f"🎲 {item['name']} - **{percent:.2f}%** (가중치: {weight})",
                        value=f"📝 {item.get('description', '설명 없음')}{reward_info}",
                        inline=False
                    )
            await interaction.response.send_message(embed=embed, ephemeral=True)


class GachaSettingsView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)
        self.add_item(GachaSettingsSelect())


# ── Cog ───────────────────────────────────────────────────────────────

class Gacha(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="뽑기", description="포인트를 소비하여 무작위 보상을 획득하는 럭키 뽑기를 진행합니다.")
    async def roll(self, interaction: discord.Interaction):
        settings = await get_gacha_settings(interaction.guild.id)
        price = settings.get("price", 100)
        items = settings.get("items", [])
        user_points = await get_user_points(interaction.user.id)

        if not items:
            embed = discord.Embed(
                title="🎰 한바헌 럭키 뽑기",
                description=f"현재 준비된 뽑기 항목이 없습니다!\n\n💰 **내 보유 포인트**: **{user_points:,} P**",
                color=discord.Color.gold()
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        total_weight = sum(item.get("weight", 1) for item in items)
        embed = discord.Embed(
            title="🎰 한바헌 럭키 뽑기",
            description=f"아래 **[🎲 1회 뽑기 실행]** 버튼을 눌러 행운을 시험해 보세요!\n\n"
                        f"💳 **1회 뽑기 가격**: **{price:,} P**\n"
                        f"💰 **현재 내 보유 포인트**: **{user_points:,} P**",
            color=discord.Color.gold()
        )
        for item in items:
            weight = item.get("weight", 1)
            percent = (weight / total_weight) * 100 if total_weight > 0 else 0
            embed.add_field(
                name=f"🎁 {item['name']} (**{percent:.2f}%**)",
                value=f"📝 {item.get('description', '설명 없음')}",
                inline=True
            )
        embed.set_footer(text="뽑기를 진행하면 포인트가 즉시 차감되고 당첨 결과가 표시됩니다.")

        view = GachaPlayView()
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @app_commands.command(name="뽑기설정", description="[관리자 전용] 럭키 뽑기의 가격, 당첨 항목, 확률을 설정합니다.")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def gacha_settings(self, interaction: discord.Interaction):
        settings = await get_gacha_settings(interaction.guild.id)
        price = settings.get("price", 100)
        items = settings.get("items", [])

        embed = discord.Embed(
            title="⚙️ 럭키 뽑기 설정 관리",
            description=f"현재 1회 뽑기 가격: **{price:,} P** | 등록된 항목 수: **{len(items)}개**\n\n"
                        "아래 드롭다운 메뉴에서 설정할 관리 기능을 선택하세요.",
            color=discord.Color.dark_gold()
        )
        view = GachaSettingsView()
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


async def setup(bot):
    await bot.add_cog(Gacha(bot))
