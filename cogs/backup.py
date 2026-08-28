"""포인트 백업 · 복원 (관리자 전용).

포인트는 이 봇의 존재 이유입니다. 로직 사고나 실수로 초기화됐을 때
되돌릴 수단을 제공합니다.

  · 매일 새벽 5시(KST) 자동 스냅샷 — 최근 30개 보관
  · /포인트백업 으로 수동 스냅샷 · 목록 · 복원 · 파일 내려받기

⚠️ 스냅샷은 같은 MongoDB 클러스터 안에 저장됩니다.
   로직 사고는 막아주지만 **클러스터·계정 사고까지는 막지 못하므로**,
   [파일로 내려받기]로 서버 밖에도 주기적으로 보관하세요.
"""

import io
import json
import datetime

import discord
from discord.ext import commands, tasks
from discord import app_commands

from utils.database import (
    create_point_snapshot, list_point_snapshots,
    get_point_snapshot, restore_point_snapshot,
    export_points_payload, storage_status, STORAGE_MODE,
)

KST = datetime.timezone(datetime.timedelta(hours=9))
# 활동이 가장 적은 시간대에 자동 백업
AUTO_BACKUP_TIME = datetime.time(hour=5, minute=0, tzinfo=KST)

REASON_LABEL = {
    "auto": "🕔 자동",
    "manual": "🙋 수동",
    "pre-restore": "🛟 복원 직전 자동",
}


def _fmt(snapshot: dict) -> str:
    reason = REASON_LABEL.get(snapshot.get("reason"), snapshot.get("reason", "?"))
    return (f"`{snapshot.get('created_kst', '?')}` · {reason}\n"
            f"     └ {snapshot.get('user_count', 0):,}명 / **{snapshot.get('total_points', 0):,} P**")


# ── 복원 확인 (2단계) ─────────────────────────────────────────────────

class RestoreConfirmView(discord.ui.View):
    """되돌릴 수 없는 작업이므로 한 번 더 확인받습니다."""

    def __init__(self, snapshot: dict):
        super().__init__(timeout=120)
        self.snapshot = snapshot

    @discord.ui.button(label="복원 실행", style=discord.ButtonStyle.danger, emoji="♻️")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        button.disabled = True
        self.stop()
        await interaction.response.defer(ephemeral=True, thinking=True)

        result = await restore_point_snapshot(interaction.guild.id, self.snapshot["id"])
        if not result.get("ok"):
            await interaction.followup.send(
                f"⚠️ 복원에 실패했습니다: {result.get('error', '알 수 없는 오류')}",
                ephemeral=True
            )
            return

        embed = discord.Embed(
            title="✅ 포인트 복원 완료",
            description=f"**{result['snapshot_kst']}** 시점의 포인트로 되돌렸습니다.",
            color=discord.Color.green()
        )
        embed.add_field(
            name="📊 결과",
            value=f"백업에 담긴 유저 **{result['total_in_snapshot']:,}명** 중 "
                  f"**{result['restored']:,}명**의 포인트를 변경했습니다.\n"
                  f"복원 후 합계: **{result['total_points']:,} P**",
            inline=False
        )
        embed.add_field(
            name="🛟 되돌리기",
            value=("복원 **직전 상태**도 자동으로 백업해 두었습니다.\n"
                   "잘못 복원했다면 목록에서 `🛟 복원 직전 자동` 항목을 골라 다시 복원하세요."
                   if result.get("safety_id") else
                   "⚠️ 복원 직전 백업을 만들지 못했습니다. 이 복원은 되돌릴 수 없습니다."),
            inline=False
        )
        embed.set_footer(text="백업에 없던 유저(이후 새로 들어온 유저)의 포인트는 그대로 두었습니다")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @discord.ui.button(label="취소", style=discord.ButtonStyle.secondary, emoji="✖️")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.send_message("복원을 취소했습니다.", ephemeral=True)


class RestoreSelect(discord.ui.Select):
    def __init__(self, snapshots: list[dict]):
        options = []
        for snap in snapshots[:25]:
            reason = REASON_LABEL.get(snap.get("reason"), "?")
            options.append(discord.SelectOption(
                label=f"{snap.get('created_kst', '?')}"[:100],
                value=str(snap["id"]),
                description=f"{reason} · {snap.get('user_count', 0):,}명 / "
                            f"{snap.get('total_points', 0):,} P"[:100],
            ))
        super().__init__(placeholder="되돌릴 시점을 선택하세요...", options=options)

    async def callback(self, interaction: discord.Interaction):
        snapshot = await get_point_snapshot(interaction.guild.id, self.values[0])
        if not snapshot:
            await interaction.response.send_message(
                "⚠️ 해당 백업을 찾을 수 없습니다.", ephemeral=True)
            return

        embed = discord.Embed(
            title="⚠️ 정말 복원하시겠습니까?",
            description=f"**{snapshot.get('created_kst', '?')}** 시점의 포인트로 되돌립니다.",
            color=discord.Color.orange()
        )
        embed.add_field(
            name="📦 이 백업의 내용",
            value=f"유저 **{snapshot.get('user_count', 0):,}명** / "
                  f"합계 **{snapshot.get('total_points', 0):,} P**",
            inline=False
        )
        embed.add_field(
            name="🔎 이렇게 동작합니다",
            value="• 백업에 담긴 유저의 포인트를 그 시점 값으로 **덮어씁니다**\n"
                  "• 백업 이후 새로 들어온 유저는 **건드리지 않습니다**\n"
                  "• 복원 직전 상태를 자동 백업하므로 **되돌릴 수 있습니다**",
            inline=False
        )
        await interaction.response.send_message(
            embed=embed, view=RestoreConfirmView(snapshot), ephemeral=True)


class RestoreView(discord.ui.View):
    def __init__(self, snapshots: list[dict]):
        super().__init__(timeout=180)
        self.add_item(RestoreSelect(snapshots))


# ── 메인 메뉴 ─────────────────────────────────────────────────────────

class BackupMenu(discord.ui.Select):
    def __init__(self):
        super().__init__(
            placeholder="백업 관리 기능을 선택하세요...",
            options=[
                discord.SelectOption(
                    label="지금 백업하기", value="snap", emoji="📸",
                    description="현재 포인트를 즉시 백업합니다."),
                discord.SelectOption(
                    label="백업 목록 보기", value="list", emoji="📋",
                    description="보관 중인 백업 시점을 확인합니다."),
                discord.SelectOption(
                    label="파일로 내려받기", value="file", emoji="📥",
                    description="서버 밖에 보관할 JSON 파일을 받습니다. (권장)"),
                discord.SelectOption(
                    label="백업으로 복원하기", value="restore", emoji="♻️",
                    description="포인트가 초기화됐을 때 되돌립니다."),
            ]
        )

    async def callback(self, interaction: discord.Interaction):
        choice = self.values[0]
        guild_id = interaction.guild.id

        if choice == "snap":
            await interaction.response.defer(ephemeral=True, thinking=True)
            result = await create_point_snapshot(guild_id, reason="manual")
            if not result.get("ok"):
                await interaction.followup.send(
                    f"⚠️ 백업에 실패했습니다: {result.get('error', '알 수 없는 오류')}",
                    ephemeral=True)
                return
            embed = discord.Embed(
                title="📸 백업 완료",
                description=f"`{result['created_kst']}` 시점으로 저장했습니다.",
                color=discord.Color.green()
            )
            embed.add_field(
                name="📊 담긴 내용",
                value=f"유저 **{result['user_count']:,}명** / 합계 **{result['total_points']:,} P**",
                inline=False)
            embed.set_footer(text="최근 30개까지 보관되며, 오래된 것부터 자동 삭제됩니다")
            await interaction.followup.send(embed=embed, ephemeral=True)

        elif choice == "list":
            await interaction.response.defer(ephemeral=True, thinking=True)
            snapshots = await list_point_snapshots(guild_id, limit=25)
            embed = discord.Embed(
                title="📋 보관 중인 포인트 백업",
                description=(f"총 **{len(snapshots)}개**의 백업이 있습니다."
                             if snapshots else
                             "아직 백업이 없습니다. `📸 지금 백업하기`로 만들어 보세요."),
                color=discord.Color.blurple()
            )
            if snapshots:
                lines = [f"`{i:2}.` {_fmt(s)}" for i, s in enumerate(snapshots, start=1)]
                embed.add_field(name="🗂️ 목록 (최신순)",
                                value="\n".join(lines)[:1024], inline=False)
            embed.set_footer(text="매일 새벽 5시에 자동 백업됩니다")
            await interaction.followup.send(embed=embed, ephemeral=True)

        elif choice == "file":
            await interaction.response.defer(ephemeral=True, thinking=True)
            payload = await export_points_payload(guild_id)
            raw = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
            stamp = payload["taken_at_kst"].replace(" ", "_").replace(":", "")
            file = discord.File(io.BytesIO(raw), filename=f"points-{stamp}.json")
            embed = discord.Embed(
                title="📥 포인트 백업 파일",
                description=f"`{payload['taken_at_kst']}` 시점의 전체 포인트입니다.",
                color=discord.Color.green()
            )
            embed.add_field(
                name="📊 담긴 내용",
                value=f"유저 **{payload['user_count']:,}명** / 합계 **{payload['total_points']:,} P**",
                inline=False)
            embed.add_field(
                name="💡 이 파일을 꼭 보관하세요",
                value="DB 안의 백업은 **클러스터나 계정 사고 때 함께 사라집니다**.\n"
                      "이 파일을 PC나 클라우드 등 **서버 밖에** 두면 그런 상황에서도 복구할 수 있습니다.",
                inline=False)
            await interaction.followup.send(embed=embed, file=file, ephemeral=True)

        elif choice == "restore":
            await interaction.response.defer(ephemeral=True, thinking=True)
            snapshots = await list_point_snapshots(guild_id, limit=25)
            if not snapshots:
                await interaction.followup.send(
                    "⚠️ 복원할 백업이 없습니다. 먼저 `📸 지금 백업하기`로 만들어 주세요.",
                    ephemeral=True)
                return
            embed = discord.Embed(
                title="♻️ 어느 시점으로 되돌릴까요?",
                description="아래에서 시점을 고르면 내용을 먼저 보여드리고, 한 번 더 확인합니다.",
                color=discord.Color.orange()
            )
            await interaction.followup.send(
                embed=embed, view=RestoreView(snapshots), ephemeral=True)


class BackupView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)
        self.add_item(BackupMenu())


# ── Cog ───────────────────────────────────────────────────────────────

class PointBackup(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.daily_snapshot.start()

    def cog_unload(self):
        self.daily_snapshot.cancel()

    @tasks.loop(time=AUTO_BACKUP_TIME)
    async def daily_snapshot(self):
        """매일 새벽 5시(KST)에 전 길드의 포인트를 자동 백업합니다."""
        await self.bot.wait_until_ready()
        for guild in self.bot.guilds:
            try:
                result = await create_point_snapshot(guild.id, reason="auto")
                if result.get("ok"):
                    print(f"[Backup] {guild.name}: 포인트 자동 백업 완료 "
                          f"({result['user_count']}명 / {result['total_points']}P)", flush=True)
                else:
                    print(f"[Backup] {guild.name}: 자동 백업 실패 - "
                          f"{result.get('error', '알 수 없음')}", flush=True)
            except Exception as e:
                print(f"[Backup] {guild.name}: 자동 백업 중 오류 - {e}", flush=True)

    @app_commands.command(
        name="포인트백업",
        description="[관리자 전용] 포인트 백업 · 복원 센터를 엽니다."
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def point_backup(self, interaction: discord.Interaction):
        snapshots = await list_point_snapshots(interaction.guild.id, limit=25)
        latest = snapshots[0]["created_kst"] if snapshots else "없음"

        embed = discord.Embed(
            title="💾 포인트 백업 · 복원 센터",
            description="포인트가 초기화되거나 잘못 변경됐을 때 되돌릴 수 있습니다.",
            color=discord.Color.blurple()
        )
        embed.add_field(name="🗂️ 보관 중인 백업", value=f"**{len(snapshots)}개**", inline=True)
        embed.add_field(name="🕒 마지막 백업", value=f"`{latest}`", inline=True)
        embed.add_field(name="💽 저장 상태", value=storage_status(), inline=False)
        if STORAGE_MODE != "mongo":
            embed.add_field(
                name="🚨 경고",
                value="포인트가 **영구 저장되지 않는 상태**입니다. 재시작하면 사라집니다.\n"
                      "`MONGO_URI` 설정을 즉시 확인하세요.",
                inline=False)
        embed.set_footer(text="매일 새벽 5시 자동 백업 · 최근 30개 보관")
        await interaction.response.send_message(embed=embed, view=BackupView(), ephemeral=True)


async def setup(bot):
    await bot.add_cog(PointBackup(bot))
