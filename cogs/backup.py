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
    export_full_payload, full_backup_bytes,
    get_backup_channel, save_backup_channel,
    get_status_channel, save_status_channel, record_boot,
)
from utils import health

KST = datetime.timezone(datetime.timedelta(hours=9))
# 활동이 가장 적은 시간대에 자동 백업
AUTO_BACKUP_TIME = datetime.time(hour=5, minute=0, tzinfo=KST)
# 마지막 백업이 이보다 오래됐으면 기동 시 즉시 한 번 백업합니다.
# 하루 주기(24h)에 여유 1시간을 둬, 정상 운영 중에는 발동하지 않습니다.
CATCHUP_AFTER_HOURS = 25

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

async def send_full_backup(bot, guild, *, reason: str = "manual") -> tuple[bool, str]:
    """전체 백업을 만들어 지정된 채널에 올립니다.

    DB 안의 스냅샷은 클러스터·계정 사고를 못 막습니다. 디스코드 채널에
    올려두면 PC 고장이든 Atlas 사고든 어느 쪽에도 살아남습니다.

    반환값은 (성공여부, 사유) 입니다.
    """
    channel_id = await get_backup_channel(guild.id)
    if not channel_id:
        return False, "no-channel"
    channel = guild.get_channel(int(channel_id))
    if channel is None:
        return False, "channel-gone"

    perms = channel.permissions_for(guild.me)
    if not (perms.send_messages and perms.attach_files):
        return False, "no-permission"

    payload = await export_full_payload()
    data, filename = full_backup_bytes(payload)
    meta = payload["_meta"]
    counts = meta.get("collections", {})
    total = sum(counts.values())

    embed = discord.Embed(
        title="💾 전체 백업",
        description=f"`{meta.get('created_kst', '?')}` 시점의 **DB 전체**입니다.",
        color=discord.Color.green() if reason == "auto" else discord.Color.blurple()
    )
    users = counts.get("users", 0)
    embed.add_field(
        name="📊 담긴 내용",
        value=f"컬렉션 **{len(counts)}개** · 문서 **{total:,}건**\n"
              f"유저 **{users:,}명** · 파일 **{len(data)/1024/1024:.2f} MB**",
        inline=False)
    embed.add_field(
        name="🗂️ 포인트 백업과 다른 점",
        value="상점 구성 · 뽑기 확률 · 인증 조건 · 링크 보호 설정 · "
              "로블록스 연동까지 **전부** 들어 있습니다.",
        inline=False)
    embed.add_field(
        name="♻️ 되돌리는 법",
        value="이 파일을 받아 프로젝트 폴더에서:\n"
              "```python tools/full_backup.py restore <파일> --all --yes```",
        inline=False)
    embed.set_footer(text=("매일 새벽 5시 자동 업로드" if reason == "auto"
                           else "수동 업로드") + " · 이 채널은 비공개로 두세요")
    embed.timestamp = discord.utils.utcnow()

    await channel.send(embed=embed, file=discord.File(io.BytesIO(data), filename=filename))
    return True, filename


# 재시작이 이 간격보다 짧으면 '루프'로 보고 경고합니다.
# 정상 운영에서는 재배포할 때만 재시작되므로 여기 걸리지 않습니다.
RESTART_LOOP_MINUTES = 15


async def announce_boot(bot, guild) -> bool:
    """기동 결과를 상태 채널에 남깁니다.

    UptimeRobot 은 밖에서 응답 여부만 봅니다. 포인트가 임시 저장소에
    쌓이는 중인지, 기능 일부가 로드에 실패했는지, 재시작을 반복하는지는
    **밖에서 알 수 없습니다.** 그래서 봇이 직접 남깁니다.

    나중에 채널만 훑어도 그동안 무슨 일이 있었는지 파악되는 것이 목적입니다.
    """
    channel_id = await get_status_channel(guild.id)
    if not channel_id:
        return False
    channel = guild.get_channel(int(channel_id))
    if channel is None:
        return False
    perms = channel.permissions_for(guild.me)
    if not perms.send_messages:
        return False

    boot = await record_boot(guild.id)
    st = health.snapshot()
    storage_ok = st.get("storage") == "mongo"
    notes = st.get("notes") or []
    looping = (boot["gap_minutes"] is not None
               and boot["gap_minutes"] < RESTART_LOOP_MINUTES)

    problem = (not storage_ok) or notes or looping
    embed = discord.Embed(
        title="🔴 봇 시작됨 (확인 필요)" if problem else "🔄 봇 시작됨",
        color=discord.Color.red() if problem else discord.Color.green(),
    )
    embed.add_field(
        name="💽 포인트 저장",
        value=("✅ MongoDB (영구 보존)" if storage_ok
               else f"🚨 **{st.get('storage')}** — 재시작하면 사라집니다"),
        inline=True)
    embed.add_field(name="⚙️ 기능",
                    value=f"{len(bot.cogs)}개 로드", inline=True)
    embed.add_field(name="📡 서버",
                    value=f"{len(bot.guilds)}개 연결", inline=True)

    if boot["previous_kst"]:
        gap = boot["gap_minutes"]
        if gap is None:
            when = f"`{boot['previous_kst']}`"
        elif gap < 60:
            when = f"`{boot['previous_kst']}` ({gap:.0f}분 전)"
        elif gap < 1440:
            when = f"`{boot['previous_kst']}` ({gap/60:.1f}시간 전)"
        else:
            when = f"`{boot['previous_kst']}` ({gap/1440:.1f}일 전)"
        embed.add_field(name="🕒 직전 기동", value=when, inline=False)

    if looping:
        embed.add_field(
            name="🚨 재시작이 잦습니다",
            value=f"직전 기동으로부터 **{boot['gap_minutes']:.0f}분** 만에 다시 켜졌습니다.\n"
                  "재배포 중이 아니라면 **재시작 루프**일 수 있습니다.\n"
                  "-# Render 로그에서 종료 원인을 확인해 주세요.",
            inline=False)
    if not storage_ok:
        embed.add_field(
            name="🚨 포인트가 영구 저장되지 않습니다",
            value="지금 쌓이는 포인트는 재시작 때 **전부 사라집니다**.\n"
                  "`MONGO_URI` 환경 변수를 즉시 확인하세요.",
            inline=False)
    if notes:
        embed.add_field(name="⚠️ 기동 중 발견된 문제",
                        value="\n".join(f"• {n}" for n in notes), inline=False)

    if not problem:
        embed.set_footer(text="정상 기동 · 이 알림은 봇이 켜질 때마다 남습니다")
    await channel.send(embed=embed)
    return True


class BackupChannelSelect(discord.ui.ChannelSelect):
    """전체 백업을 올릴 채널을 고릅니다."""

    def __init__(self):
        super().__init__(
            placeholder="전체 백업을 올릴 채널을 고르세요...",
            channel_types=[discord.ChannelType.text],
            min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        guild = interaction.guild
        channel = guild.get_channel(self.values[0].id)
        if channel is None:
            await interaction.response.send_message(
                "⚠️ 채널을 찾을 수 없습니다.", ephemeral=True)
            return

        perms = channel.permissions_for(guild.me)
        missing = []
        if not perms.send_messages:
            missing.append("메시지 보내기")
        if not perms.attach_files:
            missing.append("파일 첨부")
        if missing:
            await interaction.response.send_message(
                f"⚠️ {channel.mention} 에 **{' · '.join(missing)}** 권한이 없습니다.\n"
                "봇에게 권한을 주고 다시 지정해 주세요.", ephemeral=True)
            return

        await save_backup_channel(guild.id, channel.id)
        embed = discord.Embed(
            title="📡 전체 백업 채널 설정 완료",
            description=f"매일 새벽 5시에 {channel.mention} 로 전체 백업을 올립니다.",
            color=discord.Color.green())
        embed.add_field(
            name="🔒 이 채널은 반드시 비공개로",
            value="백업 파일에는 **유저 ID · 포인트 · 워터마크**가 들어 있습니다.\n"
                  "관리자만 볼 수 있는 채널로 두세요.",
            inline=False)
        embed.add_field(
            name="💡 왜 채널에 올리나요",
            value="DB 안의 백업은 **클러스터·계정 사고 때 같이 사라집니다**.\n"
                  "PC에만 두면 PC 고장 때 사라집니다.\n"
                  "디스코드에 있으면 **양쪽 어디에 사고가 나도** 남습니다.",
            inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)


class BackupChannelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)
        self.add_item(BackupChannelSelect())

    @discord.ui.button(label="설정 해제", style=discord.ButtonStyle.secondary,
                       emoji="🚫", row=1)
    async def clear(self, interaction: discord.Interaction, button: discord.ui.Button):
        await save_backup_channel(interaction.guild.id, None)
        await interaction.response.send_message(
            embed=discord.Embed(
                title="🚫 전체 백업 채널 해제",
                description="자동 업로드를 중단했습니다. "
                            "DB 안의 포인트 스냅샷은 계속 동작합니다.",
                color=discord.Color.orange()),
            ephemeral=True)


class StatusChannelSelect(discord.ui.ChannelSelect):
    """봇 상태 알림을 보낼 채널을 고릅니다."""

    def __init__(self):
        super().__init__(placeholder="상태 알림을 받을 채널을 고르세요...",
                         channel_types=[discord.ChannelType.text],
                         min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        guild = interaction.guild
        channel = guild.get_channel(self.values[0].id)
        if channel is None or not channel.permissions_for(guild.me).send_messages:
            await interaction.response.send_message(
                "⚠️ 그 채널에 메시지를 보낼 권한이 없습니다.", ephemeral=True)
            return
        await save_status_channel(guild.id, channel.id)
        embed = discord.Embed(
            title="🔔 봇 상태 알림 채널 설정 완료",
            description=f"봇이 켜질 때마다 {channel.mention} 에 기록을 남깁니다.",
            color=discord.Color.green())
        embed.add_field(
            name="📌 이런 걸 알 수 있습니다",
            value="• 재시작을 반복하고 있는가 (**루프**)\n"
                  "• 포인트가 임시 저장소에 쌓이고 있는가\n"
                  "• 기능 일부가 로드에 실패했는가\n"
                  "-# 전부 밖에서 감시하는 UptimeRobot 으로는 알 수 없는 것들입니다.",
            inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)


class StatusChannelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)
        self.add_item(StatusChannelSelect())

    @discord.ui.button(label="백업 채널과 같이 쓰기", style=discord.ButtonStyle.secondary,
                       emoji="🔗", row=1)
    async def same(self, interaction: discord.Interaction, button: discord.ui.Button):
        await save_status_channel(interaction.guild.id, None)
        await interaction.response.send_message(
            embed=discord.Embed(
                title="🔗 백업 채널과 함께 사용",
                description="상태 알림도 전체 백업 채널로 보냅니다.",
                color=discord.Color.blurple()),
            ephemeral=True)


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
                discord.SelectOption(
                    label="전체 백업 채널 설정", value="chan", emoji="📡",
                    description="매일 새벽 전체 백업을 올릴 채널을 지정합니다. (권장)"),
                discord.SelectOption(
                    label="지금 전체 백업 보내기", value="sendnow", emoji="📤",
                    description="설정·연동까지 포함한 전체 백업을 채널로 올립니다."),
                discord.SelectOption(
                    label="봇 상태 알림 채널", value="status", emoji="🔔",
                    description="재시작·이상을 알릴 채널. 미지정 시 백업 채널을 씁니다."),
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

        elif choice == "chan":
            current = await get_backup_channel(guild_id)
            ch = interaction.guild.get_channel(int(current)) if current else None
            embed = discord.Embed(
                title="📡 전체 백업 채널",
                description=("현재 채널: " + ch.mention if ch else
                             "**아직 지정되지 않았습니다.**"),
                color=discord.Color.blurple() if ch else discord.Color.orange())
            if current and ch is None:
                embed.description = (f"지정된 채널(`{current}`)을 찾을 수 없습니다.\n"
                                     "삭제됐거나 봇이 볼 수 없는 채널입니다.")
                embed.color = discord.Color.red()
            embed.add_field(
                name="📦 무엇이 올라가나요",
                value="포인트뿐 아니라 **상점 구성 · 뽑기 확률 · 인증 조건 · "
                      "링크 보호 설정 · 로블록스 연동**까지 전부입니다.",
                inline=False)
            embed.add_field(
                name="🕔 언제",
                value="매일 새벽 5시(KST), 포인트 스냅샷 직후에 올립니다.",
                inline=False)
            await interaction.response.send_message(
                embed=embed, view=BackupChannelView(), ephemeral=True)

        elif choice == "sendnow":
            await interaction.response.defer(ephemeral=True, thinking=True)
            try:
                ok, info = await send_full_backup(
                    interaction.client, interaction.guild, reason="manual")
            except discord.HTTPException as e:
                # 파일이 첨부 한도를 넘거나 디스코드가 거절한 경우
                await interaction.followup.send(
                    f"⚠️ 업로드에 실패했습니다: `{e.status}` {e.text[:200]}",
                    ephemeral=True)
                return
            except Exception as e:
                await interaction.followup.send(
                    f"⚠️ 백업 중 오류가 발생했습니다: {type(e).__name__}: {e}",
                    ephemeral=True)
                return

            if ok:
                await interaction.followup.send(
                    embed=discord.Embed(
                        title="📤 전체 백업 업로드 완료",
                        description=f"`{info}` 파일을 백업 채널에 올렸습니다.",
                        color=discord.Color.green()),
                    ephemeral=True)
            else:
                why = {
                    "no-channel": "먼저 `📡 전체 백업 채널 설정`에서 채널을 지정해 주세요.",
                    "channel-gone": "지정된 채널을 찾을 수 없습니다. 다시 지정해 주세요.",
                    "no-permission": "그 채널에 **메시지 보내기 · 파일 첨부** 권한이 없습니다.",
                }.get(info, f"알 수 없는 오류: {info}")
                await interaction.followup.send(
                    embed=discord.Embed(title="⚠️ 업로드하지 못했습니다",
                                        description=why,
                                        color=discord.Color.orange()),
                    ephemeral=True)

        elif choice == "status":
            current = await get_status_channel(guild_id)
            backup_ch = await get_backup_channel(guild_id)
            ch = interaction.guild.get_channel(int(current)) if current else None
            shared = current and backup_ch and int(current) == int(backup_ch)
            embed = discord.Embed(
                title="🔔 봇 상태 알림 채널",
                description=(f"현재 채널: {ch.mention}" + ("  *(백업 채널과 공유)*" if shared else "")
                             if ch else "**아직 지정되지 않았습니다.**"),
                color=discord.Color.blurple() if ch else discord.Color.orange())
            embed.add_field(
                name="🆚 UptimeRobot 과 무엇이 다른가요",
                value="UptimeRobot 은 **밖에서** 응답 여부만 봅니다.\n"
                      "포인트가 임시 저장소에 쌓이는 중인지, 기능이 빠졌는지,\n"
                      "재시작을 반복하는지는 **안에서만** 알 수 있습니다.",
                inline=False)
            await interaction.response.send_message(
                embed=embed, view=StatusChannelView(), ephemeral=True)

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
        # on_ready 는 재접속 때마다 다시 불립니다. 기동 알림은 프로세스가
        # 새로 뜬 경우에만 보내야 하므로 한 번만 보내도록 잠급니다.
        self._announced = False
        self.daily_snapshot.start()

    @commands.Cog.listener()
    async def on_ready(self):
        if self._announced:
            return
        self._announced = True
        for guild in self.bot.guilds:
            try:
                await announce_boot(self.bot, guild)
            except Exception as e:
                # 알림 실패가 봇 기동을 막으면 안 됩니다
                print(f"[Backup] {guild.name}: 기동 알림 실패 - "
                      f"{type(e).__name__}: {e}", flush=True)

    def cog_unload(self):
        self.daily_snapshot.cancel()

    async def _snapshot_age_hours(self, guild_id: int) -> float | None:
        """마지막 백업이 몇 시간 전인지. 백업이 없거나 시각을 못 읽으면 None."""
        recent = await list_point_snapshots(guild_id, limit=1)
        if not recent:
            return None
        try:
            last = datetime.datetime.strptime(
                recent[0].get("created_kst", ""), "%Y-%m-%d %H:%M:%S"
            ).replace(tzinfo=KST)
        except (ValueError, TypeError):
            return None
        return (datetime.datetime.now(KST) - last).total_seconds() / 3600

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

            # 스냅샷은 같은 클러스터 안에 남습니다. 클러스터·계정 사고까지
            # 대비하려면 서버 밖에도 있어야 하므로, 지정된 채널에 전체 백업을
            # 올립니다. 실패해도 스냅샷은 이미 끝났으므로 넘어갑니다.
            try:
                ok, info = await send_full_backup(self.bot, guild, reason="auto")
                if ok:
                    print(f"[Backup] {guild.name}: 전체 백업 채널 업로드 완료 ({info})",
                          flush=True)
                elif info != "no-channel":
                    print(f"[Backup] {guild.name}: 전체 백업 업로드 실패 - {info}",
                          flush=True)
            except Exception as e:
                print(f"[Backup] {guild.name}: 전체 백업 업로드 중 오류 - "
                      f"{type(e).__name__}: {e}", flush=True)

    @daily_snapshot.before_loop
    async def before_daily_snapshot(self):
        """기동 직후, 마지막 백업이 너무 오래됐으면 즉시 한 번 백업합니다.

        새벽 5시 정각에 재배포·재시작이 겹치면 그날 자동 백업이 통째로
        건너뛰어집니다. 이 보강이 그 빈틈을 메웁니다.
        백업이 하나도 없는 첫 배포에서도 즉시 하나를 만들어 둡니다.
        """
        await self.bot.wait_until_ready()
        for guild in self.bot.guilds:
            try:
                age = await self._snapshot_age_hours(guild.id)
                if age is not None and age < CATCHUP_AFTER_HOURS:
                    continue    # 최근 백업이 있으니 건너뜁니다
                reason = "백업 없음" if age is None else f"마지막 백업 {age:.0f}시간 전"
                result = await create_point_snapshot(guild.id, reason="auto")
                if result.get("ok"):
                    print(f"[Backup] {guild.name}: 기동 시 따라잡기 백업 완료 "
                          f"({reason} / {result['user_count']}명 / "
                          f"{result['total_points']}P)", flush=True)
                else:
                    print(f"[Backup] {guild.name}: 따라잡기 백업 실패 - "
                          f"{result.get('error', '알 수 없음')}", flush=True)
            except Exception as e:
                print(f"[Backup] {guild.name}: 따라잡기 백업 중 오류 - {e}", flush=True)

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
