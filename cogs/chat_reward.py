import re
from datetime import datetime, timezone
import discord
from discord.ext import commands
from discord import app_commands
from utils.database import (
    process_chat_message, is_user_blacklisted,
    set_channel_boost, get_channel_boost, get_all_active_boosts, add_activity_log
)

class ChatReward(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        # Ignore bot messages and direct messages
        if message.author.bot or not message.guild:
            return

        # Check if user is blacklisted from receiving points
        if await is_user_blacklisted(message.guild.id, message.author.id):
            return

        # 1. Exclude photos/files/GIF attachments and stickers
        if message.attachments or message.stickers:
            return

        content = message.content.strip()
        if not content:
            return

        # 2. Exclude GIF links (Tenor, Giphy, Gfycat, or .gif/.gifv URLs)
        if re.search(r'https?://\S*(?:tenor\.com|giphy\.com|gfycat\.com|\.gif|\.gifv)', content, re.IGNORECASE):
            return

        # 3. Strip all general URLs (http://, https://)
        without_urls = re.sub(r'https?://\S+', '', content)

        # 4. Strip custom Discord emojis (<:name:id> or <a:name:id>)
        without_custom_emojis = re.sub(r'<a?:[a-zA-Z0-9_]+:[0-9]+>', '', without_urls)

        # 5. Strip unicode emojis
        without_emojis = re.sub(r'[\U00010000-\U0010ffff\u2600-\u27ff\u2300-\u23ff\200d\ufe0f]', '', without_custom_emojis).strip()

        # If nothing remains after removing URLs and emojis, ignore.
        if not without_emojis:
            return

        # Process message for chat points (with channel boost support)
        new_pts, awarded, earned_pts, boost_mult = await process_chat_message(
            message.guild.id, message.author.id, message.channel.id
        )

        if awarded and boost_mult > 1.0:
            try:
                # 게릴라 부스트 적립 안내 (5초 후 자동 삭제)
                await message.channel.send(
                    f"⚡ {message.author.mention} 님! **채널 부스트 이벤트** 혜택으로 10회 채팅 적립 포인트 **{boost_mult:g}배 (+{earned_pts}P)**를 획득하셨습니다!",
                    delete_after=5
                )
            except Exception:
                pass

    @app_commands.command(name="채널부스트", description="[관리자 전용] 특정 채널의 채팅 포인트 배율을 설정하거나 해제합니다.")
    @app_commands.rename(channel="채널", multiplier="배율", duration_minutes="유지시간_분")
    @app_commands.describe(
        channel="부스트를 적용/해제할 텍스트 채널",
        multiplier="포인트 배율 (예: 2 -> 2배, 3 -> 3배, 1 -> 부스트 해제)",
        duration_minutes="부스트 유지 시간(분 단위). 0 지정 시 직접 끌 때까지 무제한 유지"
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def set_boost(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        multiplier: float,
        duration_minutes: int = 0
    ):
        if multiplier < 1.0:
            await interaction.response.send_message("⚠️ 배율은 1.0 이상이어야 합니다. (1.0 설정 시 부스트 해제)", ephemeral=True)
            return

        await set_channel_boost(interaction.guild.id, channel.id, multiplier, duration_minutes)

        if multiplier == 1.0:
            await add_activity_log(
                interaction.guild.id, "admin", interaction.user.id, interaction.user.display_name,
                f"[{channel.name}] 채널 부스트 해제"
            )
            embed = discord.Embed(
                title="⚡ 채널 부스트 해제",
                description=f"{channel.mention} 채널의 포인트 부스트 이벤트가 해제(1.0배 기본)되었습니다.",
                color=discord.Color.orange()
            )
        else:
            time_str = f"**{duration_minutes}분간**" if duration_minutes > 0 else "**무제한(직접 해제 전까지)**"
            await add_activity_log(
                interaction.guild.id, "admin", interaction.user.id, interaction.user.display_name,
                f"[{channel.name}] {multiplier:g}배 부스트 설정 ({time_str})"
            )
            embed = discord.Embed(
                title="⚡ 게릴라 채널 부스트 설정 완료!",
                description=f"🎉 {channel.mention} 채널에 **{multiplier:g}배** 포인트 부스트가 설정되었습니다!\n\n"
                            f"⏱️ **유지 시간**: {time_str}\n"
                            f"💬 이제 해당 채널에서 채팅 10회 작성 시 기본 포인트의 **{multiplier:g}배(회당 {int(round(10 * multiplier))}P)**를 획득합니다.",
                color=discord.Color.green()
            )
            # 해당 채널에 게릴라 이벤트 시작 알림 메시지 띄우기 시도
            try:
                announce_embed = discord.Embed(
                    title="⚡ [게릴라 이벤트] 포인트 부스트 시작!",
                    description=f"🔥 이 채널에 **{multiplier:g}배** 포인트 부스트 이벤트가 시작되었습니다!\n"
                                f"💬 채팅 10회 작성 시마다 **+{int(round(10 * multiplier))}P**가 자동 적립됩니다! (유지시간: {time_str})",
                    color=discord.Color.gold()
                )
                await channel.send(embed=announce_embed)
            except Exception:
                pass

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="채널부스트목록", description="[관리자 전용] 현재 포인트 부스트가 적용 중인 채널 현황을 확인합니다.")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def list_boosts(self, interaction: discord.Interaction):
        active_boosts = await get_all_active_boosts(interaction.guild.id)

        if not active_boosts:
            embed = discord.Embed(
                title="⚡ 진행 중인 채널 부스트 현황",
                description="현재 부스트 이벤트가 적용 중인 채널이 없습니다.",
                color=discord.Color.gold()
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        lines = []
        now = datetime.now(timezone.utc)
        for b in active_boosts:
            cid = int(b["channel_id"])
            mult = float(b.get("multiplier", 1.0))
            channel = interaction.guild.get_channel(cid)
            ch_str = channel.mention if channel else f"채널 ID `{cid}`"

            exp_str = b.get("expires_at")
            if exp_str:
                try:
                    exp_time = datetime.fromisoformat(exp_str)
                    remain_sec = int((exp_time - now).total_seconds())
                    if remain_sec > 0:
                        mins, secs = divmod(remain_sec, 60)
                        time_info = f"⏱️ 남은 시간: `{mins}분 {secs}초`"
                    else:
                        time_info = "⏱️ 만료됨"
                except Exception:
                    time_info = "⏱️ 시간 정보 오류"
            else:
                time_info = "⏱️ 무제한 (지속 적용 중)"

            lines.append(f"• {ch_str} — 🔥 **{mult:g}배** ({time_info})")

        embed = discord.Embed(
            title="⚡ 진행 중인 게릴라 채널 부스트 목록",
            description="\n\n".join(lines),
            color=discord.Color.gold()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot):
    await bot.add_cog(ChatReward(bot))

