"""⚗️ 역할 합성 (요청 2026-10-08).

가진 역할 두 개를 합쳐 새 역할을 얻습니다.

  · /합성       누구나 — 레시피의 재료를 보고, 가진 역할 두 개를 골라 합성
  · /합성설정   관리자 — 레시피 추가(재료 2 + 결과 · 재료 소모) · 삭제 · 켜기 · 공개 알림

## 재료는 보이고, 결과는 누군가 처음 만들 때 공개됩니다

요청: "합성에 필요한 역할들을 보여주고 합성으로 나오는 역할 정보는 공개하지 않는
것" + "합성에 성공한 사람이 나오면 그때 나오는 역할 정보를 공개".
/합성 화면은 레시피마다 `재료 A + 재료 B → ❓ ???` 로 보이다가, **누군가 처음
성공하는 순간** `→ ✨ 결과 역할 (최초 발견: @누구)` 로 모두에게 바뀝니다.
최초 발견은 레시피에 기록되며, 동시에 성공해도 **한 명만** 최초가 됩니다
(`mark_recipe_discovered` 가 '아직 없을 때만' 을 필터로 판정).
관리자 화면(/합성설정)은 처음부터 전부 보입니다.

## 순서가 중요합니다

결과 역할을 **먼저 주고**, 그다음 재료를 뺍니다. 거꾸로 하면 결과 지급이 실패했을
때 재료만 날아갑니다. 같은 사람이 버튼을 연타해도 한 번만 돌도록 사람마다 잠급니다.
맞는 레시피가 없으면 **아무것도 잃지 않습니다.**
"""

import asyncio

import discord
from discord import app_commands
from discord.ext import commands

from utils.database import add_activity_log
from utils.event_db import (
    get_fusion, save_fusion_settings, add_fusion_recipe, remove_fusion_recipe, recipe_key,
    mark_recipe_discovered,
)

# 사람마다 한 번에 하나 — 연타 · 두 창 동시 클릭에도 재료가 두 번 빠지지 않게
_locks: dict[tuple[int, int], asyncio.Lock] = {}


def _lock(guild_id: int, user_id: int) -> asyncio.Lock:
    return _locks.setdefault((int(guild_id), int(user_id)), asyncio.Lock())


def _role_text(guild, rid) -> str:
    role = guild.get_role(int(rid))
    return role.mention if role else f"`삭제된 역할 {rid}`"


def ingredient_ids(recipes: dict) -> set[int]:
    return {int(r) for rec in recipes.values() for r in rec.get("inputs", [])}


def manage_problem(guild, roles) -> str | None:
    """봇이 이 역할들을 주고 뺄 수 있는지. 안 되면 **무엇을 고쳐야 하는지** 돌려줍니다."""
    me = guild.me
    if me is None or not me.guild_permissions.manage_roles:
        return "봇에게 **역할 관리** 권한이 없습니다"
    for role in roles:
        if role is None:
            continue
        if role.managed or getattr(role, "is_default", lambda: False)():
            return f"{role.mention} 은(는) 봇이 줄 수 없는 역할입니다(연동 · @everyone)"
        if role >= me.top_role:
            return f"{role.mention} 이(가) 봇 역할보다 위에 있습니다 — 서버 설정에서 봇 역할을 위로 올려 주세요"
    return None


async def perform_fusion(guild, member, a: int, b: int) -> dict:
    """합성 본체. 화면과 상관없이 결과만 돌려줍니다(검증하기 쉽게).

    status: ok · off · same · no_recipe · missing · already · result_gone · cannot · failed
    ok 이면 `first` 가 최초 발견인지입니다.
    """
    a, b = int(a), int(b)
    if a == b:
        return {"status": "same"}
    async with _lock(guild.id, member.id):
        s = await get_fusion(guild.id, fresh=True)
        if not s.get("enabled", True):
            return {"status": "off"}
        rk = recipe_key(a, b)
        rec = (s.get("recipes") or {}).get(rk)
        # 맞는 레시피가 없으면 **아무것도 잃지 않습니다**
        if rec is None:
            return {"status": "no_recipe"}
        have = {r.id for r in getattr(member, "roles", [])}
        if a not in have or b not in have:
            return {"status": "missing"}
        result = guild.get_role(int(rec["result"]))
        if result is None:
            return {"status": "result_gone", "recipe": rec}
        if result.id in have:
            return {"status": "already", "result": result, "recipe": rec}
        inputs = [guild.get_role(a), guild.get_role(b)]
        problem = manage_problem(guild, [result] + (inputs if rec.get("consume", True) else []))
        if problem:
            return {"status": "cannot", "why": problem, "recipe": rec}
        try:
            await member.add_roles(result, reason="역할 합성")
        except discord.HTTPException as e:
            return {"status": "failed", "why": f"결과 역할을 주지 못했습니다 ({e.status})"}
        consumed, kept = [], []
        if rec.get("consume", True):
            try:
                await member.remove_roles(*[r for r in inputs if r is not None],
                                          reason="역할 합성 재료")
                consumed = [r for r in inputs if r is not None]
            except discord.HTTPException as e:
                # 결과는 이미 받았습니다. 재료가 남은 것은 유저에게 손해가 아니라 알리기만 합니다.
                print(f"[Fusion] 재료 회수 실패 {member.id}: {e.status}", flush=True)
                kept = [r for r in inputs if r is not None]
        # 처음 성공한 사람이면 이 레시피의 결과가 **모두에게 공개**됩니다
        first = False
        if not rec.get("discovered_by"):
            try:
                first = await mark_recipe_discovered(guild.id, rk, member.id)
            except Exception as e:
                print(f"[Fusion] 최초 발견 기록 실패: {type(e).__name__}: {e}", flush=True)
        try:
            await add_activity_log(
                guild.id, "fusion", member.id, member.display_name,
                f"역할 합성: {inputs[0].name if inputs[0] else a} + "
                f"{inputs[1].name if inputs[1] else b} → {result.name}"
                + (" (최초 발견)" if first else ""))
        except Exception as e:
            print(f"[Fusion] 활동 기록 실패: {type(e).__name__}: {e}", flush=True)
        return {"status": "ok", "result": result, "recipe": rec, "consumed": consumed,
                "kept": kept, "first": first, "announce": bool(s.get("announce"))}


FAIL_TEXT = {
    "off": "⏸️ 지금은 역할 합성이 꺼져 있습니다.",
    "same": "⚠️ 서로 다른 역할 두 개를 골라 주세요.",
    "no_recipe": "💨 아무 일도 일어나지 않았습니다…\n이 조합으로는 나오는 역할이 없습니다. **역할은 그대로입니다.**",
    "missing": "⚠️ 고른 역할을 지금 가지고 있지 않습니다.",
    "result_gone": "⚠️ 이 레시피의 결과 역할이 서버에서 사라졌습니다. 관리자에게 알려 주세요.",
}


def recipe_line(guild, r: dict, have: set[int]) -> str:
    """유저 화면의 레시피 한 줄 — 재료는 보이고, 결과는 **최초 발견 전까지 ???**."""
    a, b = r["inputs"]
    ready = "✅" if a in have and b in have else "▫️"
    if r.get("discovered_by"):
        out = f"✨ {_role_text(guild, r['result'])} (최초 발견 <@{r['discovered_by']}>)"
    else:
        out = "❓ **???**"
    keep = "" if r.get("consume", True) else " · 재료 유지"
    return f"{ready} {_role_text(guild, a)} + {_role_text(guild, b)} → {out}{keep}"


def _fields(embed, name, lines):
    chunk, n = "", 0
    for line in lines:
        if chunk and len(chunk) + len(line) + 1 > 1000:
            embed.add_field(name=name if n == 0 else "​", value=chunk, inline=False)
            chunk, n = "", n + 1
        chunk = f"{chunk}\n{line}" if chunk else line[:1000]
    embed.add_field(name=name if n == 0 else "​", value=chunk or "—", inline=False)


def build_user_embed(guild, member, s: dict) -> discord.Embed:
    recipes = s.get("recipes") or {}
    have = {r.id for r in getattr(member, "roles", [])}
    embed = discord.Embed(
        title="⚗️ 역할 합성",
        description=("가진 역할 **두 개**를 골라 합칩니다.\n"
                     "-# 맞는 조합이면 새 역할을 얻고, 재료 역할은 레시피에 따라 사라집니다. "
                     "맞지 않으면 아무것도 잃지 않습니다."),
        color=discord.Color.purple())
    lines = [recipe_line(guild, r, have)
             for r in sorted(recipes.values(), key=lambda x: x.get("created", 0))]
    _fields(embed, "📜 레시피", lines or ["아직 레시피가 없습니다."])
    hidden = sum(1 for r in recipes.values() if not r.get("discovered_by"))
    embed.set_footer(text=(f"✅ = 지금 재료가 다 있음 · ❓ 아직 아무도 만들지 못한 레시피 {hidden}개 — "
                           "처음 만드는 사람이 나오면 공개됩니다") if hidden
                     else "✅ = 지금 재료가 다 있음")
    return embed


class FuseSelect(discord.ui.Select):
    def __init__(self, options):
        super().__init__(placeholder="합칠 역할 두 개를 고르세요", min_values=2, max_values=2,
                         options=options)

    async def callback(self, interaction: discord.Interaction):
        self.view.picked = [int(v) for v in self.values]
        names = " + ".join(_role_text(interaction.guild, v) for v in self.view.picked)
        self.view.go.disabled = False
        await interaction.response.edit_message(
            content=f"⚗️ {names} 을(를) 합성할까요? 아래 **합성하기** 를 누르세요.", view=self.view)


class FuseView(discord.ui.View):
    """한 사람 전용 합성 창. 본인 것만 누를 수 있습니다."""

    def __init__(self, owner_id: int, options):
        super().__init__(timeout=180)
        self.owner_id = owner_id
        self.picked: list[int] = []
        if options:
            self.add_item(FuseSelect(options))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("⚠️ 본인 창에서만 합성할 수 있습니다.",
                                                    ephemeral=True)
            return False
        return True

    @discord.ui.button(label="합성하기", emoji="⚗️", style=discord.ButtonStyle.success,
                       disabled=True, row=1)
    async def go(self, interaction: discord.Interaction, button: discord.ui.Button):
        if len(self.picked) != 2:
            await interaction.response.send_message("⚠️ 역할 두 개를 먼저 고르세요.", ephemeral=True)
            return
        # 이 창 자체를 고칠 수 있게 '창 갱신' 으로 미룹니다(성공하면 버튼을 거둡니다)
        await interaction.response.defer()
        res = await perform_fusion(interaction.guild, interaction.user, *self.picked)
        st = res["status"]
        if st == "ok":
            result, first = res["result"], res.get("first")
            embed = discord.Embed(
                title="🏆 최초 발견!" if first else "✨ 합성 성공!",
                description=(f"🌟 {result.mention} 역할을 얻었습니다!"
                             + ("\n이 레시피의 결과가 이제 **모두에게 공개**됩니다." if first else "")),
                color=discord.Color.gold())
            if res["consumed"]:
                embed.add_field(name="사용한 재료",
                                value=" · ".join(r.mention for r in res["consumed"]), inline=False)
            if res["kept"]:
                embed.add_field(name="⚠️ 재료 회수 실패",
                                value="재료 역할을 빼지 못해 그대로 남았습니다(관리자에게 알려 주세요).",
                                inline=False)
            await interaction.followup.send(embed=embed, ephemeral=True)
            # 역할이 바뀌었으니 이 창의 선택지는 낡았습니다. 남겨 두면 다시 눌렀을 때
            # '상호작용 실패' 만 뜹니다 — 버튼을 거두고 /합성 을 다시 열게 합니다.
            try:
                await interaction.edit_original_response(
                    content="✅ 합성 완료 — 다시 하려면 `/합성` 을 새로 열어 주세요.", view=None)
            except discord.HTTPException as e:
                print(f"[Fusion] 창 정리 실패: {e.status}", flush=True)
            if res.get("announce") and interaction.channel is not None:
                try:
                    msg = (f"🏆 {interaction.user.mention} 님이 **처음으로** 합성에 성공해 "
                           f"{result.mention} 을(를) 발견했습니다!" if first else
                           f"✨ {interaction.user.mention} 님이 합성으로 {result.mention} 을(를) 얻었습니다!")
                    await interaction.channel.send(
                        content=msg, allowed_mentions=discord.AllowedMentions(
                            everyone=False, roles=False, users=[interaction.user]))
                except discord.HTTPException as e:
                    print(f"[Fusion] 공개 알림 실패: {e.status}", flush=True)
            self.stop()
            return
        if st == "already":
            text = f"🏅 이미 {res['result'].mention} 역할을 가지고 있습니다. **재료는 그대로입니다.**"
        elif st in ("cannot", "failed"):
            text = f"⚠️ 합성하지 못했습니다 — {res['why']}\n**역할은 그대로입니다.** 관리자에게 알려 주세요."
        else:
            text = FAIL_TEXT.get(st, "⚠️ 합성하지 못했습니다.")
        await interaction.followup.send(text, ephemeral=True)


# ── 관리자 ────────────────────────────────────────────────────────────

def build_admin_embed(guild, s: dict) -> discord.Embed:
    recipes = s.get("recipes") or {}
    embed = discord.Embed(
        title="⚗️ 역할 합성 설정",
        description=(f"상태 **{'🟢 켜짐' if s.get('enabled', True) else '🔴 꺼짐'}** · "
                     f"공개 알림 **{'켜짐' if s.get('announce') else '꺼짐'}**\n"
                     "-# 유저 화면에는 재료만 보이고, 결과는 누군가 처음 만들 때 공개됩니다."),
        color=discord.Color.purple())
    lines = []
    for r in sorted(recipes.values(), key=lambda x: x.get("created", 0)):
        a, b = r["inputs"]
        keep = "" if r.get("consume", True) else " · 재료 유지"
        found = (f" · 🏆 <@{r['discovered_by']}> 최초 발견" if r.get("discovered_by")
                 else " · ❓ 미발견")
        lines.append(f"• {_role_text(guild, a)} + {_role_text(guild, b)} → "
                     f"**{_role_text(guild, r['result'])}**{keep}{found}")
    _fields(embed, "레시피", lines or ["아직 레시피가 없습니다. **➕ 레시피 추가** 로 만드세요."])
    embed.set_footer(text=f"레시피 {len(recipes)}개 · 이 화면은 본인에게만 보입니다")
    return embed


async def _admin(interaction: discord.Interaction) -> bool:
    perms = getattr(interaction.user, "guild_permissions", None)
    if perms is not None and perms.administrator:
        return True
    await interaction.response.send_message("⛔ 관리자만 바꿀 수 있습니다.", ephemeral=True)
    return False


class InputsSelect(discord.ui.RoleSelect):
    def __init__(self):
        super().__init__(placeholder="① 재료 역할 두 개", min_values=2, max_values=2, row=0)

    async def callback(self, interaction: discord.Interaction):
        self.view.inputs = [r.id for r in self.values]
        await interaction.response.edit_message(embed=self.view.preview(interaction.guild),
                                                view=self.view)


class ResultSelect(discord.ui.RoleSelect):
    def __init__(self):
        super().__init__(placeholder="② 나올 역할 (결과)", min_values=1, max_values=1, row=1)

    async def callback(self, interaction: discord.Interaction):
        self.view.result = self.values[0].id
        await interaction.response.edit_message(embed=self.view.preview(interaction.guild),
                                                view=self.view)


class RecipeBuilder(discord.ui.View):
    """레시피 만들기 창 — 재료 · 결과를 고르고 재료 소모 여부를 정해 저장합니다."""

    def __init__(self, cog):
        super().__init__(timeout=600)
        self.cog = cog
        self.inputs: list[int] = []
        self.result: int | None = None
        self.consume = True
        self.add_item(InputsSelect())
        self.add_item(ResultSelect())
        self._labels()

    def _labels(self):
        self.consume_btn.label = "재료 소모: 켜짐" if self.consume else "재료 소모: 꺼짐(유지)"

    def preview(self, guild) -> discord.Embed:
        ins = " + ".join(_role_text(guild, r) for r in self.inputs) or "(아직 안 고름)"
        res = _role_text(guild, self.result) if self.result else "(아직 안 고름)"
        return discord.Embed(
            title="➕ 레시피 만들기",
            description=(f"{ins} → **{res}**\n\n"
                         f"재료 소모 **{'켜짐' if self.consume else '꺼짐'}**\n"
                         "-# 유저 화면에는 재료만 보이고, 결과는 누군가 처음 만들 때 공개됩니다."),
            color=discord.Color.purple())

    @discord.ui.button(label="재료 소모", style=discord.ButtonStyle.secondary, row=2)
    async def consume_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.consume = not self.consume
        self._labels()
        await interaction.response.edit_message(embed=self.preview(interaction.guild), view=self)

    @discord.ui.button(label="저장", emoji="💾", style=discord.ButtonStyle.success, row=2)
    async def save(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await _admin(interaction):
            return
        guild = interaction.guild
        if len(self.inputs) != 2 or not self.result:
            await interaction.response.send_message("⚠️ 재료 두 개와 결과 역할을 모두 고르세요.",
                                                    ephemeral=True)
            return
        if self.result in self.inputs:
            await interaction.response.send_message("⚠️ 결과 역할이 재료와 같을 수 없습니다.",
                                                    ephemeral=True)
            return
        roles = [guild.get_role(self.result)] + (
            [guild.get_role(r) for r in self.inputs] if self.consume else [])
        problem = manage_problem(guild, roles)
        await interaction.response.defer()
        ok = await add_fusion_recipe(guild.id, self.inputs[0], self.inputs[1], self.result,
                                     consume=self.consume)
        if not ok:
            await interaction.followup.send(
                "⚠️ 같은 재료 조합의 레시피가 이미 있습니다. 먼저 지우고 다시 만들어 주세요.",
                ephemeral=True)
            return
        try:
            await add_activity_log(guild.id, "admin", interaction.user.id,
                                   interaction.user.display_name, "합성 레시피 추가")
        except Exception as e:
            print(f"[Fusion] 활동 기록 실패: {type(e).__name__}: {e}", flush=True)
        note = (f"\n\n⚠️ 저장했지만 지금은 합성이 실패합니다 — {problem}" if problem else "")
        await interaction.edit_original_response(
            content=f"💾 레시피를 저장했습니다.{note}", embed=self.preview(guild), view=None)
        self.stop()


class RemoveRecipeSelect(discord.ui.Select):
    def __init__(self, guild, recipes: dict):
        def nm(rid):
            role = guild.get_role(int(rid))
            return role.name if role else str(rid)
        opts = [discord.SelectOption(
                    label=f"{nm(r['inputs'][0])} + {nm(r['inputs'][1])} → {nm(r['result'])}"[:100],
                    value=rk)
                for rk, r in list(recipes.items())[:25]]
        super().__init__(placeholder="지울 레시피", options=opts)

    async def callback(self, interaction: discord.Interaction):
        if not await _admin(interaction):
            return
        gone = await remove_fusion_recipe(interaction.guild.id, self.values[0])
        await interaction.response.edit_message(
            content="🗑️ 레시피를 지웠습니다." if gone else "ℹ️ 이미 지워진 레시피입니다.", view=None)


class RemoveRecipeView(discord.ui.View):
    def __init__(self, guild, recipes):
        super().__init__(timeout=180)
        self.add_item(RemoveRecipeSelect(guild, recipes))


class AdminView(discord.ui.View):
    def __init__(self, cog, s: dict):
        super().__init__(timeout=600)
        self.cog = cog
        self.toggle.label = "끄기" if s.get("enabled", True) else "켜기"
        self.toggle.emoji = "🔴" if s.get("enabled", True) else "🟢"
        self.announce.label = f"공개 알림: {'켜짐' if s.get('announce') else '꺼짐'}"
        self.remove.disabled = not (s.get("recipes") or {})

    @discord.ui.button(label="레시피 추가", emoji="➕", style=discord.ButtonStyle.primary)
    async def add(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await _admin(interaction):
            return
        b = RecipeBuilder(self.cog)
        await interaction.response.send_message(embed=b.preview(interaction.guild), view=b,
                                                ephemeral=True)

    @discord.ui.button(label="레시피 삭제", emoji="🗑️", style=discord.ButtonStyle.danger)
    async def remove(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await _admin(interaction):
            return
        s = await get_fusion(interaction.guild.id, fresh=True)
        if not s.get("recipes"):
            await interaction.response.send_message("ℹ️ 지울 레시피가 없습니다.", ephemeral=True)
            return
        await interaction.response.send_message(
            "🗑️ 지울 레시피를 고르세요.", view=RemoveRecipeView(interaction.guild, s["recipes"]),
            ephemeral=True)

    @discord.ui.button(label="끄기", style=discord.ButtonStyle.secondary)
    async def toggle(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await _admin(interaction):
            return
        s = await get_fusion(interaction.guild.id, fresh=True)
        await save_fusion_settings(interaction.guild.id, {"enabled": not s.get("enabled", True)})
        await self.cog.refresh_admin(interaction)

    @discord.ui.button(label="공개 알림", emoji="📣", style=discord.ButtonStyle.secondary)
    async def announce(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await _admin(interaction):
            return
        s = await get_fusion(interaction.guild.id, fresh=True)
        await save_fusion_settings(interaction.guild.id, {"announce": not s.get("announce")})
        await self.cog.refresh_admin(interaction)

    @discord.ui.button(label="새로고침", emoji="🔄", style=discord.ButtonStyle.secondary)
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.refresh_admin(interaction)


class Fusion(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def refresh_admin(self, interaction: discord.Interaction) -> None:
        s = await get_fusion(interaction.guild.id, fresh=True)
        await interaction.response.edit_message(embed=build_admin_embed(interaction.guild, s),
                                                view=AdminView(self, s))

    @app_commands.command(name="합성", description="가진 역할 두 개를 합쳐 새 역할을 얻습니다.")
    @app_commands.guild_only()
    async def fuse(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        s = await get_fusion(interaction.guild.id, fresh=True)
        if not s.get("enabled", True):
            await interaction.followup.send(FAIL_TEXT["off"], ephemeral=True)
            return
        recipes = s.get("recipes") or {}
        embed = build_user_embed(interaction.guild, interaction.user, s)
        # 고를 수 있는 것은 **레시피 재료로 쓰이는 역할 중 내가 가진 것**만입니다
        usable = ingredient_ids(recipes)
        mine = [r for r in sorted(getattr(interaction.user, "roles", []),
                                  key=lambda r: getattr(r, "position", 0), reverse=True)
                if r.id in usable][:25]
        if len(mine) < 2:
            embed.add_field(name="🧪 합성할 수 있는 역할",
                            value="재료 역할을 **2개 이상** 가지고 있어야 합성할 수 있습니다.",
                            inline=False)
            await interaction.followup.send(embed=embed, ephemeral=True)
            return
        options = [discord.SelectOption(label=r.name[:100], value=str(r.id)) for r in mine]
        await interaction.followup.send(embed=embed, view=FuseView(interaction.user.id, options),
                                        ephemeral=True)

    @app_commands.command(name="합성설정", description="[관리자 전용] 역할 합성 레시피 · 켜기 · 공개 알림")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def fuse_settings(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        s = await get_fusion(interaction.guild.id, fresh=True)
        await interaction.followup.send(embed=build_admin_embed(interaction.guild, s),
                                        view=AdminView(self, s), ephemeral=True)


async def setup(bot):
    await bot.add_cog(Fusion(bot))
