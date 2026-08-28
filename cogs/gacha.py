import random
import re
import discord
from discord.ext import commands
from discord import app_commands
from utils.database import (
    get_gacha_settings, save_gacha_settings,
    get_user_points, deduct_user_points, add_user_points, add_activity_log,
    consume_gacha_stock
)


def parse_role_mentions(text: str) -> str:
    if not text:
        return text
    # {@12345}, {12345}, {@&12345} 등의 패턴을 디스코드 표준 역할 멘션 <@&12345> 로 자동 변환
    return re.sub(r'\{@?&?(\d{17,20})\}', r'<@&\1>', text)



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
    def __init__(self, is_out_of_stock: bool = False):
        super().__init__(timeout=180)
        if is_out_of_stock:
            self.roll_gacha.disabled = True
            self.roll_gacha.label = "🚫 모든 상품 품절 (재고 없음)"
            self.roll_gacha.style = discord.ButtonStyle.secondary
            self.roll_gacha.emoji = "🚫"

    @discord.ui.button(label="1회 뽑기 실행", style=discord.ButtonStyle.primary, emoji="🎲", custom_id="gacha_roll_btn")
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
            button.disabled = True
            button.label = "🚫 모든 상품 품절 (재고 없음)"
            button.style = discord.ButtonStyle.secondary
            button.emoji = "🚫"
            try:
                await interaction.message.edit(view=button.view)
            except Exception:
                pass

            embed = discord.Embed(
                title="⚠️ 모든 뽑기 상품 품절",
                description="현재 남아있는 모든 뽑기 상품의 재고가 소진(품절)되었습니다.",
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
        success, remaining = await deduct_user_points(guild_id, user_id, price)
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

        # 재고 1 차감 — 목록 전체를 덮어쓰지 않고 **원자적으로** 처리합니다.
        # 동시에 뽑은 두 명이 같은 마지막 항목에 당첨되면 2개가 나가기 때문입니다.
        if stock > 0:
            got = await consume_gacha_stock(guild_id, selected_item.get("id"))
            if not got:
                # 그 사이 다른 유저가 마지막 재고를 가져갔습니다 → 전액 환불
                await add_user_points(guild_id, user_id, price)
                embed = discord.Embed(
                    title="⚠️ 방금 품절되었습니다",
                    description=f"당첨된 **{item_name}** 항목의 마지막 재고가 방금 소진되었습니다.\n\n"
                                f"💰 차감된 **{price:,} P**는 **전액 환불**되었습니다. 다시 시도해 주세요.",
                    color=discord.Color.orange()
                )
                await interaction.response.send_message(embed=embed, ephemeral=True)
                return
            # 화면 표시용으로 로컬 사본도 맞춰 둡니다
            for item in items:
                if item.get("id") == selected_item.get("id"):
                    item["stock"] = stock - 1
                    break

        new_stock = stock - 1 if stock > 0 else -1
        stock_text = format_stock_str(new_stock)

        # 뽑기 후 남은 가용 항목 재확인하여 다 떨어졌으면 버튼 비활성화
        new_available = [item for item in items if item.get("stock", -1) != 0]
        if not new_available:
            button.disabled = True
            button.label = "🚫 모든 상품 품절 (재고 없음)"
            button.style = discord.ButtonStyle.secondary
            button.emoji = "🚫"
            try:
                await interaction.message.edit(view=button.view)
            except Exception:
                pass

        # 포인트 보상인 경우 자동 추가
        new_balance = remaining
        if reward_points > 0:
            new_balance = await add_user_points(guild_id, user_id, reward_points)

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

        gacha_title = settings.get("title", "한바헌 럭키 뽑기")

        # 결과 임베드
        embed = discord.Embed(
            title=f"{gacha_title} 결과!",
            description=f"🎉 축하합니다! **[{item_name}]** 에 당첨되셨습니다!{role_notice}\n\n"
                        f"💳 **소모 포인트**: -{price:,} P\n"
                        f"💰 **현재 총 보유 포인트**: **{new_balance:,} P**\n"
                        f"📦 **해당 상품 남은 재고**: `{stock_text}`",
            color=discord.Color.gold()
        )
        if desc:
            embed.add_field(name="📝 항목 설명", value=parse_role_mentions(desc), inline=False)

        if reward_points > 0:
            embed.add_field(
                name="🎁 보상 자동 적립",
                value=f"포인트 보상 **+{reward_points:,} P**가 지갑에 자동 적립되었습니다!",
                inline=False
            )

        embed.set_footer(text=f"{gacha_title} 시스템")
        await interaction.response.send_message(embed=embed, ephemeral=True)


# ── Admin Gacha Settings Modals & Views ───────────────────────────────

class SetGachaTitleModal(discord.ui.Modal, title="🏷️ 뽑기 이름/제목 변경"):
    title_input = discord.ui.TextInput(
        label="새로운 뽑기 이름/제목",
        placeholder="예: 럭키 뽑기 파라다이스 또는 🎁 바이옴 랜덤 뽑기",
        default="한바헌 럭키 뽑기",
        required=True,
        max_length=50
    )

    async def on_submit(self, interaction: discord.Interaction):
        try:
            new_title = self.title_input.value.strip()
            if not new_title:
                await interaction.response.send_message("⚠️ 올바른 뽑기 이름을 입력해 주세요.", ephemeral=True)
                return

            settings = await get_gacha_settings(interaction.guild.id)
            settings["title"] = new_title
            await save_gacha_settings(interaction.guild.id, settings)

            embed = discord.Embed(
                title="🏷️ 뽑기 이름 변경 완료",
                description=f"뽑기 이름이 **{new_title}**(으)로 변경되었습니다.",
                color=discord.Color.green()
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"⚠️ 처리 중 오류가 발생했습니다: `{e}`", ephemeral=True)


class SetGachaDescriptionModal(discord.ui.Modal, title="📝 뽑기 설명/안내 문구 변경"):
    desc_input = discord.ui.TextInput(
        label="뽑기 설명/안내 문구",
        placeholder="예: 🎁 럭키 뽑기에 참여하여 대박 보상과 한정 역할을 노려보세요! (비워두면 표시 안 함)",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=500
    )

    async def on_submit(self, interaction: discord.Interaction):
        try:
            new_desc = self.desc_input.value.strip()
            settings = await get_gacha_settings(interaction.guild.id)
            settings["description"] = new_desc
            await save_gacha_settings(interaction.guild.id, settings)

            desc_preview = f"**{new_desc}**" if new_desc else "*설정 안 함 (삭제됨)*"
            embed = discord.Embed(
                title="📝 뽑기 설명 변경 완료",
                description=f"뽑기 설명이 성공적으로 설정/변경되었습니다.\n\n📝 **현재 설정된 설명**:\n{desc_preview}",
                color=discord.Color.green()
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"⚠️ 처리 중 오류가 발생했습니다: `{e}`", ephemeral=True)



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
        sorted_items = sorted(items, key=lambda x: float(x.get("percent", x.get("weight", 0))))
        options = [
            discord.SelectOption(
                label=f"{item['name']} [{format_stock_str(item.get('stock', -1))}]",
                value=str(item["id"]),
                description=f"확률: {item.get('percent', item.get('weight', 0))}% | 역할 ID: {item.get('reward_role_id', 0)}",
                emoji="🗑️"
            )
            for item in sorted_items
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


class SetGachaItemStockModal(discord.ui.Modal, title="📦 뽑기 항목 재고 충전/수정"):
    def __init__(self, item_id: int, current_stock: int, item_name: str):
        super().__init__()
        self.item_id = item_id
        self.item_name = item_name

        default_val = str(current_stock)
        self.stock_input = discord.ui.TextInput(
            label=f"새로운 재고 수량 (무제한: -1)",
            placeholder="예: 10 (10개로 충전) 또는 -1 (무제한)",
            default=default_val,
            required=True,
            max_length=10
        )
        self.add_item(self.stock_input)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            new_stock = int(self.stock_input.value.strip())
            if new_stock < 0 and new_stock != -1:
                raise ValueError()
        except ValueError:
            await interaction.response.send_message("⚠️ 0 이상의 수량 또는 무제한(-1)을 숫자로 입력해 주세요.", ephemeral=True)
            return

        settings = await get_gacha_settings(interaction.guild.id)
        items = settings.get("items", [])

        target_item = None
        for item in items:
            if item.get("id") == self.item_id:
                item["stock"] = new_stock
                target_item = item
                break

        if not target_item:
            await interaction.response.send_message("⚠️ 대상 항목을 찾을 수 없습니다.", ephemeral=True)
            return

        settings["items"] = items
        await save_gacha_settings(interaction.guild.id, settings)

        stock_str = format_stock_str(new_stock)
        embed = discord.Embed(
            title="📦 재고 수정/충전 완료",
            description=f"**{target_item['name']}** 항목의 재고가 **`{stock_str}`**(으)로 변경/충전되었습니다.",
            color=discord.Color.green()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


class RefillGachaItemStockSelect(discord.ui.Select):
    def __init__(self, items: list[dict]):
        sorted_items = sorted(items, key=lambda x: float(x.get("percent", x.get("weight", 0))))
        options = [
            discord.SelectOption(
                label=f"{item['name'][:25]} [{format_stock_str(item.get('stock', -1))}]",
                value=str(item["id"]),
                description=f"현재 재고: {format_stock_str(item.get('stock', -1))} | 확률: {item.get('percent', item.get('weight', 0))}%",
                emoji="📦" if item.get('stock', -1) != 0 else "🚫"
            )
            for item in sorted_items
        ]
        super().__init__(placeholder="재고를 충전/수정할 뽑기 항목을 선택하세요...", options=options)

    async def callback(self, interaction: discord.Interaction):
        item_id = int(self.values[0])
        settings = await get_gacha_settings(interaction.guild.id)
        items = settings.get("items", [])
        item = next((i for i in items if i.get("id") == item_id), None)
        if not item:
            await interaction.response.send_message("⚠️ 선택 항목 정보를 찾을 수 없습니다.", ephemeral=True)
            return

        modal = SetGachaItemStockModal(
            item_id=item_id,
            current_stock=item.get("stock", -1),
            item_name=item.get("name", "항목")
        )
        await interaction.response.send_modal(modal)


class RefillGachaItemStockView(discord.ui.View):
    def __init__(self, items: list[dict]):
        super().__init__(timeout=180)
        self.add_item(RefillGachaItemStockSelect(items))



class GachaSettingsSelect(discord.ui.Select):
    def __init__(self, current_settings: dict):
        self.current_settings = current_settings
        is_enabled = current_settings.get("enabled", True)
        gacha_title = current_settings.get("title", "한바헌 럭키 뽑기")
        gacha_desc = current_settings.get("description", "")
        desc_summary = (gacha_desc[:20] + "...") if len(gacha_desc) > 20 else (gacha_desc if gacha_desc else "미설정")
        options = [
            discord.SelectOption(
                label="시스템 활성화 / 비활성화",
                value="toggle",
                description=f"현재: {'✅ 활성화' if is_enabled else '❌ 비활성화'}",
                emoji="⚡"
            ),
            discord.SelectOption(
                label="뽑기 이름/제목 변경",
                value="title",
                description=f"현재 이름: {gacha_title[:25]}",
                emoji="🏷️"
            ),
            discord.SelectOption(
                label="뽑기 설명/안내 문구 변경",
                value="desc",
                description=f"현재 설명: {desc_summary}",
                emoji="📝"
            ),
            discord.SelectOption(label="1회 뽑기 가격 설정", value="price", description="뽑기 1회에 필요한 포인트를 변경합니다.", emoji="💰"),
            discord.SelectOption(label="당첨 항목 및 역할 추가", value="add", description="새로운 당첨 항목, 확률(%), 수량, 지급 역할을 추가합니다.", emoji="➕"),
            discord.SelectOption(label="당첨 항목 재고 수정/충전", value="stock", description="일부분/전부 품절된 뽑기 항목의 재고를 수정 및 충전합니다.", emoji="📦"),
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

        elif choice == "title":
            modal = SetGachaTitleModal()
            modal.title_input.default = self.current_settings.get("title", "한바헌 럭키 뽑기")
            await interaction.response.send_modal(modal)

        elif choice == "desc":
            settings = await get_gacha_settings(interaction.guild.id)
            modal = SetGachaDescriptionModal()
            modal.desc_input.default = settings.get("description", "")
            await interaction.response.send_modal(modal)

        elif choice == "price":
            settings = await get_gacha_settings(interaction.guild.id)
            modal = SetGachaPriceModal()
            modal.price_input.default = str(settings.get("price", 100))
            await interaction.response.send_modal(modal)

        elif choice == "add":
            await interaction.response.send_modal(AddGachaItemModal())

        elif choice == "stock":
            settings = await get_gacha_settings(interaction.guild.id)
            items = settings.get("items", [])
            if not items:
                await interaction.response.send_message("⚠️ 재고를 변경/충전할 등록된 뽑기 항목이 없습니다.", ephemeral=True)
                return
            await interaction.response.send_message("📦 재고를 수정/충전할 항목을 선택하세요.", view=RefillGachaItemStockView(items), ephemeral=True)

        elif choice == "delete":
            settings = await get_gacha_settings(interaction.guild.id)
            items = settings.get("items", [])
            if not items:
                await interaction.response.send_message("⚠️ 삭제할 등록된 뽑기 항목이 없습니다.", ephemeral=True)
                return
            await interaction.response.send_message("🗑️ 삭제할 항목을 아래 메뉴에서 선택하세요.", view=DeleteGachaItemView(items), ephemeral=True)

        elif choice == "list":
            settings = await get_gacha_settings(interaction.guild.id)
            gacha_title = settings.get("title", "한바헌 럭키 뽑기")
            price = settings.get("price", 100)
            items = settings.get("items", [])

            if not items:
                embed = discord.Embed(
                    title=f"📊 {gacha_title} 확률표 및 재고 현황",
                    description=f"💰 **1회 뽑기 가격**: {price:,} P\n\n현재 등록된 뽑기 항목이 없습니다.",
                    color=discord.Color.gold()
                )
            else:
                total_pct = get_total_percent(items)
                status_str = "✅ **100.00%** (뽑기 정상 가동 가능)" if abs(total_pct - 100.0) < 0.001 else f"⚠️ **{total_pct:.2f}%** (100%가 되지 않아 뽑기 생성 불가!)"

                embed = discord.Embed(
                    title=f"📊 {gacha_title} 확률표 및 재고 현황",
                    description=f"💰 **1회 뽑기 가격**: **{price:,} P** | 총 **{len(items)}개** 항목\n"
                                f"📊 **전체 확률 합계**: {status_str}",
                    color=discord.Color.green() if abs(total_pct - 100.0) < 0.001 else discord.Color.red()
                )
                sorted_items = sorted(items, key=lambda x: float(x.get("percent", x.get("weight", 0))))
                for item in sorted_items:
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

        gacha_title = settings.get("title", "한바헌 럭키 뽑기")
        price = settings.get("price", 100)
        items = settings.get("items", [])
        user_points = await get_user_points(interaction.guild.id, interaction.user.id)

        title_str = f"[비활성화 상태] {gacha_title}" if not is_enabled else gacha_title

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

        # 가용 항목(재고 0이 아닌 항목) 확인
        available_items = [item for item in items if item.get("stock", -1) != 0]
        is_all_sold_out = len(available_items) == 0

        if is_all_sold_out:
            status_line = "⚠️ 현재 모든 뽑기 상품의 재고가 소진되어 뽑기를 진행할 수 없습니다."
        elif not is_ready:
            status_line = f"⚠️ 현재 전체 확률 합계가 **{total_pct:.2f}%**로 100%가 되지 않아 뽑기를 진행할 수 없습니다."
        else:
            status_line = "✅ 현재 뽑기를 플레이하실 수 있습니다."

        gacha_desc = parse_role_mentions(settings.get("description", "").strip())
        desc_header = f"{gacha_desc}\n\n" if gacha_desc else ""

        embed = discord.Embed(
            title=title_str,
            description=f"{desc_header}"
                        f"💳 **1회 뽑기 가격**: **{price:,} P**\n"
                        f"💰 **현재 내 보유 포인트**: **{user_points:,} P**\n\n"
                        f"{status_line}",
            color=discord.Color.gold() if (is_ready and not is_all_sold_out) else discord.Color.red()
        )

        sorted_items = sorted(items, key=lambda x: float(x.get("percent", x.get("weight", 0))))
        for item in sorted_items:
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

        view = GachaPlayView(is_out_of_stock=is_all_sold_out)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @app_commands.command(name="뽑기설정", description="[관리자 전용] 럭키 뽑기의 이름, 설명, 가격, 당첨 항목, 확률(총합 100%), 재고, 역할을 관리합니다.")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def gacha_settings(self, interaction: discord.Interaction):
        settings = await get_gacha_settings(interaction.guild.id)
        enabled_text = "✅ 활성화" if settings.get("enabled", True) else "❌ 비활성화"
        gacha_title = settings.get("title", "한바헌 럭키 뽑기")
        gacha_desc = parse_role_mentions(settings.get("description", "").strip())
        desc_text = f"**{gacha_desc}**" if gacha_desc else "*미설정*"
        price = settings.get("price", 100)
        items = settings.get("items", [])

        total_pct = get_total_percent(items)
        status_str = "✅ **100%** (뽑기 생성 완료)" if is_valid_100_percent(items) else f"⚠️ **{total_pct:.2f}%** (100%를 맞춰야 유저들이 뽑기 가능)"

        embed = discord.Embed(
            title="⚙️ 럭키 뽑기, 역할 및 재고 설정 관리",
            description=f"🏷️ 뽑기 이름: **{gacha_title}**\n"
                        f"📝 뽑기 설명: {desc_text}\n"
                        f"⚡ 시스템 상태: **{enabled_text}** | 💰 1회 뽑기 가격: **{price:,} P** | 📦 항목 수: **{len(items)}개**\n"
                        f"📊 전체 확률 합계: {status_str}\n\n"
                        "아래 드롭다운 메뉴에서 설정할 관리 기능을 선택하세요.",
            color=discord.Color.dark_gold()
        )
        view = GachaSettingsView(settings)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


async def setup(bot):
    await bot.add_cog(Gacha(bot))
