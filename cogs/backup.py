"""포인트 백업 · 복원 (관리자 전용).

포인트는 이 봇의 존재 이유입니다. 로직 사고나 실수로 초기화됐을 때
되돌릴 수단을 제공합니다.

  · 매일 새벽 5시(KST) 자동 스냅샷 — 최근 30개 보관
  · /백업설정 으로 수동 스냅샷 · 목록 · 복원 · 파일 내려받기 · 채널 지정

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
    get_status_state, set_status_message, mark_full_backup,
    current_version,
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
    await mark_full_backup(guild.id, meta.get("created_kst", ""))
    return True, filename


# 재시작이 이 간격보다 짧으면 '루프'로 보고 경고합니다.
# 정상 운영에서는 재배포할 때만 재시작되므로 여기 걸리지 않습니다.
# 이 시간 창 안에서 이만큼 켜지면 '재시작 루프'로 봅니다.
# 재배포는 보통 한두 번이라 걸리지 않고, 진짜 루프는 훨씬 잦습니다.
NL = chr(10)

RESTART_LOOP_WINDOW_MINUTES = 60
RESTART_LOOP_COUNT = 3
# 상태 임베드를 몇 초마다 새로 고칠지.
#
# 메시지 **수정**은 채널을 올리지 않고 알림도 울리지 않으므로, 짧게 잡아도
# 유저에게 전혀 방해가 되지 않습니다. 디스코드 편집 한도(초당 여러 번)에
# 비해서도 1분에 한 번은 매우 여유롭습니다.
#
# 짧게 두는 진짜 이유는 따로 있습니다. **임베드가 갱신되는 시각 자체가
# 생존 신호**입니다. 봇이 멈추면 갱신도 멈추므로, 시각이 굳어 있는 것만
# 보고도 이상을 알 수 있습니다. 주기가 짧을수록 그 판단이 빨라집니다.
STATUS_REFRESH_SECONDS = 60


def _ago(stamp: str | None) -> str:
    """KST 문자열을 '몇 분 전' 형태로. 못 읽으면 원문 그대로."""
    if not stamp:
        return "기록 없음"
    try:
        dt = datetime.datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S").replace(tzinfo=KST)
    except (ValueError, TypeError):
        return f"`{stamp}`"
    mins = (datetime.datetime.now(KST) - dt).total_seconds() / 60
    if mins < 1:
        return f"`{stamp}` (방금)"
    if mins < 60:
        return f"`{stamp}` ({mins:.0f}분 전)"
    if mins < 1440:
        return f"`{stamp}` ({mins/60:.1f}시간 전)"
    return f"`{stamp}` ({mins/1440:.1f}일 전)"


def _uptime(seconds: int) -> str:
    d, r = divmod(int(seconds), 86400)
    h, r = divmod(r, 3600)
    m = r // 60
    if d:
        return f"{d}일 {h}시간"
    if h:
        return f"{h}시간 {m}분"
    return f"{m}분"


async def build_status_embed(bot, guild, state: dict | None = None) -> discord.Embed:
    """지금 상태를 하나의 임베드로 만듭니다.

    매번 새 메시지를 보내면 채널이 도배되고, 정작 중요한 신호가 묻힙니다.
    그래서 **메시지 하나를 계속 수정**합니다. 대신 재시작 이력을 임베드 안에
    남겨, 도배 없이도 '재시작이 몰렸는지'를 볼 수 있게 합니다.
    """
    st = health.snapshot()
    # 1분마다 도는 경로라 같은 조회를 두 번 하지 않도록 받아서 씁니다.
    if state is None:
        state = await get_status_state(guild.id)

    storage_ok = st.get("storage") == "mongo"
    notes = st.get("notes") or []

    # 이력 항목은 예전엔 문자열, 지금은 {"at","v"} 입니다. 둘 다 받습니다.
    def _entry(x):
        if isinstance(x, dict):
            return x.get("at", ""), x.get("v", "")
        return str(x), ""

    raw_history = state["boot_history"] or ([state["last_boot_kst"]]
                                            if state["last_boot_kst"] else [])
    history = [_entry(x) for x in raw_history]
    # 최근 한 시간 안에 몇 번 켜졌는지로 판단합니다.
    #
    # 예전에는 '직전 두 기동의 간격'만 봤는데, 그러면 재배포로 두 번 연달아
    # 켜진 뒤 며칠이 지나도 계속 빨간색으로 남았습니다. 늘 빨간 표시는
    # 없는 것보다 나쁩니다 — 익숙해져서 진짜 경고를 놓치게 됩니다.
    # 시간 창으로 세면 안정되는 순간 저절로 풀립니다.
    now = datetime.datetime.now(KST)
    version = current_version()
    recent = 0          # 같은 코드로 다시 켜진 횟수 (진짜 루프의 신호)
    deploys = 0         # 코드가 바뀌어 켜진 횟수 (재배포 — 정상)
    for at, v in history:
        try:
            dt = datetime.datetime.strptime(at, "%Y-%m-%d %H:%M:%S").replace(tzinfo=KST)
        except (ValueError, TypeError):
            continue
        if (now - dt).total_seconds() > RESTART_LOOP_WINDOW_MINUTES * 60:
            continue
        if v == version:
            recent += 1
        else:
            deploys += 1

    # 재배포는 코드가 바뀌므로 버전이 매번 다릅니다. 배포를 연달아 해도
    # 경고가 뜨면 안 됩니다 — 대응할 것이 없는 정상 동작이기 때문입니다.
    # **같은 버전이 반복해서 죽는 경우**만 진짜 루프입니다.
    looping = recent >= RESTART_LOOP_COUNT

    ext_missing = st.get("ext_total", 0) > 0 and st.get("ext_loaded") != st.get("ext_total")
    problem = (not storage_ok) or notes or looping or ext_missing
    connected = st.get("connected")

    if problem:
        title, color = "🔴 봇 상태 — 확인이 필요합니다", discord.Color.red()
    elif not connected:
        title, color = "🟡 봇 상태 — 재연결 중", discord.Color.orange()
    else:
        title, color = "🟢 봇 상태 — 정상", discord.Color.green()

    embed = discord.Embed(title=title, color=color)
    embed.add_field(
        name="💽 포인트 저장",
        value=("✅ MongoDB" if storage_ok
               else f"🚨 **{st.get('storage')}**\n재시작하면 사라집니다"),
        inline=True)
    embed.add_field(name="⏱️ 연속 가동",
                    value=_uptime(st.get("uptime_seconds", 0)), inline=True)
    embed.add_field(
        name="📶 응답 속도",
        value=(f"{st['latency_ms']:.0f} ms" if st.get("latency_ms") else "측정 전"),
        inline=True)
    ext_ok = st.get("ext_total", 0) > 0 and st.get("ext_loaded") == st.get("ext_total")
    embed.add_field(
        name="⚙️ 기능",
        value=(f"{st.get('ext_loaded', 0)}/{st.get('ext_total', 0)}개"
               + ("" if ext_ok else "  🚨")),
        inline=True)
    embed.add_field(name="🔌 연결", value=("✅ 정상" if connected else "🔄 재연결 중"),
                    inline=True)
    embed.add_field(name="📋 명령어", value=f"{st.get('commands', 0)}개", inline=True)
    embed.add_field(
        name="🔁 누적 재시작",
        value=f"{state['boot_count']}회" + (f"\n-# `{version}`" if version else ""),
        inline=True)

    embed.add_field(name="💾 마지막 전체 백업",
                    value=_ago(state["last_full_backup_kst"]), inline=False)

    if history:
        lines = []
        for at, v in reversed(history[-5:]):
            tag = ""
            if v and v != version:
                tag = "  `이전 버전`"
            elif v and len(history) > 1:
                tag = ""
            lines.append(f"• {_ago(at)}{tag}")
        embed.add_field(name="🕒 최근 기동", value="\n".join(lines), inline=False)

    if looping:
        embed.add_field(
            name="🚨 재시작이 잦습니다",
            value=f"**같은 버전**이 최근 {RESTART_LOOP_WINDOW_MINUTES}분 안에 "
                  f"**{recent}번** 죽었습니다.\n"
                  "재배포가 아니라 **크래시 루프**입니다.\n"
                  "-# Render 로그에서 종료 원인을 확인해 주세요. "
                  "안정되면 이 경고는 저절로 사라집니다.",
            inline=False)
    if not storage_ok:
        embed.add_field(
            name="🚨 포인트가 영구 저장되지 않습니다",
            value="지금 쌓이는 포인트는 재시작 때 **전부 사라집니다**.\n"
                  "`MONGO_URI` 환경 변수를 즉시 확인하세요.",
            inline=False)
    if ext_missing:
        failed = st.get("ext_failed") or []
        embed.add_field(
            name="🚨 작동하지 않는 기능이 있습니다",
            value=(f"**{st['ext_total']}개 중 {st['ext_loaded']}개**만 켜졌습니다.\n"
                   + (f"빠진 기능: {', '.join(f'`{x}`' for x in failed)}\n" if failed else "")
                   + "-# 이 이름을 그대로 알려주시면 원인을 찾을 수 있습니다."),
            inline=False)
    if notes:
        embed.add_field(name="⚠️ 기동 중 발견된 문제",
                        value="\n".join(f"• {n}" for n in notes), inline=False)

    # 아래 시각이 멈춰 있으면 그 자체가 이상 신호입니다. 명시적으로 알려둡니다.
    embed.set_footer(text="1분마다 갱신 · 아래 시각이 멈춰 있으면 봇이 멈춘 것입니다")
    embed.timestamp = discord.utils.utcnow()
    return embed


async def upsert_status_message(bot, guild) -> bool:
    """상태 임베드를 갱신합니다. 없으면 새로 만들고 ID를 기억합니다.

    메시지 ID를 DB에 두고 계속 **수정**하므로 채널에 알림이 울리지 않습니다.
    누가 지웠거나 봇이 못 찾으면 그때만 새로 만듭니다.
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

    state = await get_status_state(guild.id)
    embed = await build_status_embed(bot, guild, state)

    if state["message_id"]:
        try:
            msg = await channel.fetch_message(state["message_id"])
            await msg.edit(embed=embed)
            return True
        except discord.NotFound:
            pass          # 지워졌으니 아래에서 새로 만듭니다
        except discord.Forbidden:
            return False
        except discord.HTTPException as e:
            print(f"[Status] 임베드 수정 실패: {e}", flush=True)
            return False

    try:
        msg = await channel.send(embed=embed)
    except discord.HTTPException as e:
        print(f"[Status] 임베드 생성 실패: {e}", flush=True)
        return False
    await set_status_message(guild.id, msg.id)
    return True


class BackupChannelSelect(discord.ui.ChannelSelect):
    """전체 백업 파일을 올릴 채널을 고릅니다."""

    def __init__(self):
        super().__init__(placeholder="전체 백업을 올릴 채널을 고르세요...",
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
                await record_boot(guild.id)       # 기동 이력에 한 줄 남깁니다
                await upsert_status_message(self.bot, guild)
            except Exception as e:
                # 상태 표시 실패가 봇 기동을 막으면 안 됩니다
                print(f"[Status] {guild.name}: 기동 기록 실패 - "
                      f"{type(e).__name__}: {e}", flush=True)
        if not self.status_refresh.is_running():
            self.status_refresh.start()

    @tasks.loop(seconds=STATUS_REFRESH_SECONDS)
    async def status_refresh(self):
        """상태 임베드를 주기적으로 새로 고칩니다.

        메시지 **수정**이라 채널이 올라가지 않고 알림도 울리지 않습니다.
        가동 시간·응답 속도가 계속 갱신되므로, 마지막 갱신 시각만 봐도
        봇이 살아 있는지 알 수 있습니다.

        갱신 실패가 쌓여도 봇 자체는 계속 돌아야 하므로 예외를 삼킵니다.
        (연결이 끊긴 동안에는 편집이 실패하는 것이 정상입니다)
        """
        await self.bot.wait_until_ready()
        for guild in self.bot.guilds:
            try:
                await upsert_status_message(self.bot, guild)
            except Exception as e:
                print(f"[Status] {guild.name}: 상태 갱신 실패 - "
                      f"{type(e).__name__}: {e}", flush=True)

    def cog_unload(self):
        self.daily_snapshot.cancel()
        self.status_refresh.cancel()

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
        name="백업설정",
        description="[관리자 전용] 백업 · 복원 · 백업 채널 · 봇 상태 알림을 관리합니다."
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def backup_settings(self, interaction: discord.Interaction):
        snapshots = await list_point_snapshots(interaction.guild.id, limit=25)
        latest = snapshots[0]["created_kst"] if snapshots else "없음"

        embed = discord.Embed(
            title="💾 백업 · 복원 센터",
            description="포인트 백업·복원과 전체 백업, 봇 상태 알림을 여기서 관리합니다.",
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
