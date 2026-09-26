"""
/봇업데이트 — SSH 없이 디스코드에서 새 코드를 받고 재시작합니다.

배포가 `ssh → git pull → systemctl restart` 뿐이라, 서버에 접속할 줄 아는
사람이 없으면 **코드를 고쳐 올려도 봇에는 영영 반영되지 않았습니다**
(실제로 새 명령어가 며칠간 안 보였습니다 — 재시작만 반복해서입니다).

  1. git fetch 로 GitHub 의 main 을 보고, 새 커밋 목록을 보여 줍니다
  2. [업데이트하고 재시작] → **보여 준 그 커밋까지만** fast-forward
  3. 모든 모듈을 **별도 프로세스에서 import** 해 봅니다
     → 실패하면 **되돌리고 재시작하지 않습니다** (깨진 코드로 켜지면
       봇이 통째로 죽고, 그때는 디스코드로 되돌릴 수도 없습니다)
  4. 성공하면 프로세스를 끝냅니다 → systemd 가 5초 뒤 새 코드로 띄웁니다

⚠️ 서버에서 누가 파일을 직접 고쳤거나(작업 트리가 더러움), main 이 아닌
브랜치에 있거나, 서버에만 있는 커밋이 있으면 **손대지 않습니다.** 그 상태에서
덮어쓰면 누군가의 수정이 사라집니다. 그때는 SSH 로 사람이 봐야 합니다.
"""

import os
import re
import sys
import time
import asyncio
from pathlib import Path

import discord
from discord.ext import commands
from discord import app_commands

from utils import health

REPO_DIR = Path(__file__).resolve().parent.parent
BRANCH = "main"
GIT_TIMEOUT = 90
PIP_TIMEOUT = 600
IMPORT_TIMEOUT = 180
RESTART_DELAY = 3          # 결과 메시지가 먼저 도착하도록 잠깐 기다립니다
MAX_LISTED = 15

# 동시에 두 명이 누르면 fetch · merge 가 엉킵니다
_lock = asyncio.Lock()

# 이 프로세스가 뜬 시각(= 마지막 재시작). cog 는 기동할 때 한 번 불러옵니다.
# health 의 booted_at 은 monotonic 이라 화면에 시각으로 쓸 수 없습니다.
PROCESS_STARTED = time.time()
# 받은 시각이 재시작보다 이만큼 늦으면 '받고 재시작 안 함' 으로 봅니다
STALE_GRACE = 60


async def run(*args, timeout: float = GIT_TIMEOUT, env: dict | None = None) -> tuple[int, str]:
    """명령을 저장소 폴더에서 실행하고 (종료 코드, 출력) 을 돌려줍니다.

    **이벤트 루프를 막지 않습니다**(asyncio 서브프로세스). git 이 비밀번호를
    물으면 영원히 기다리므로 `GIT_TERMINAL_PROMPT=0` 으로 바로 실패시킵니다.
    """
    full_env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", **(env or {})}
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, cwd=str(REPO_DIR), env=full_env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    except (OSError, ValueError) as e:
        return 127, f"{type(e).__name__}: {e}"
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, f"{timeout:.0f}초 안에 끝나지 않았습니다"
    # 앞 공백은 남깁니다 — `git status --porcelain` 은 첫 칸이 공백(" M 파일")이라
    # 지우면 첫 줄만 한 칸 밀려 파일 이름이 잘립니다.
    return proc.returncode, (out or b"").decode("utf-8", errors="replace").rstrip()


async def git(*args, timeout: float = GIT_TIMEOUT) -> tuple[int, str]:
    return await run("git", *args, timeout=timeout)


def _short(sha: str) -> str:
    return (sha or "")[:12]


async def check_updates() -> dict:
    """새 커밋이 있는지 봅니다. 서버 상태가 이상하면 `blocked` 에 이유를 담습니다."""
    info = {"blocked": "", "local": "", "remote": "", "commits": [], "files": [],
            "total": 0}
    rc, branch = await git("rev-parse", "--abbrev-ref", "HEAD")
    if rc != 0:
        info["blocked"] = f"git 저장소를 읽지 못했습니다.\n```{branch[:300]}```"
        return info
    if branch != BRANCH:
        info["blocked"] = (f"서버가 `{BRANCH}` 가 아니라 `{branch}` 브랜치에 있습니다.\n"
                           "누가 일부러 바꿔 둔 것일 수 있어 손대지 않습니다.")
        return info
    rc, dirty = await git("status", "--porcelain", "--untracked-files=no")
    if rc == 0 and dirty:
        files = "\n".join(l[3:] for l in dirty.splitlines()[:10])
        info["blocked"] = ("서버에서 **직접 고친 파일**이 있습니다. 덮어쓰면 그 수정이 "
                           f"사라지므로 손대지 않습니다.\n```{files}```")
        return info

    rc, out = await git("fetch", "--quiet", "origin", BRANCH)
    if rc != 0:
        info["blocked"] = f"GitHub 에서 받아 오지 못했습니다.\n```{out[:500]}```"
        return info
    _, info["local"] = await git("rev-parse", "HEAD")
    _, info["remote"] = await git("rev-parse", f"origin/{BRANCH}")
    rc, counts = await git("rev-list", "--left-right", "--count",
                           f"HEAD...origin/{BRANCH}")
    try:
        ahead, behind = (int(x) for x in counts.split())
    except ValueError:
        info["blocked"] = f"커밋 비교에 실패했습니다.\n```{counts[:300]}```"
        return info
    if ahead:
        info["blocked"] = (f"서버에만 있는 커밋이 **{ahead}개** 있습니다. 합치면 꼬일 수 "
                           "있어 손대지 않습니다.")
        return info
    info["total"] = behind
    if behind:
        _, log = await git("log", "--format=%h %s", f"-n{MAX_LISTED}",
                           f"HEAD..{info['remote']}")
        info["commits"] = [l for l in log.splitlines() if l.strip()]
        _, files = await git("diff", "--name-only", "HEAD", info["remote"])
        info["files"] = [f for f in files.splitlines() if f.strip()]
    return info


async def version_times() -> dict:
    """지금 버전을 **만든** 시각 · 서버에 **받은** 시각 · 봇을 **재시작한** 시각.

    받은 시각은 reflog(HEAD 가 움직인 기록)에서 읽습니다. `git pull` 이든
    `/봇업데이트` 든 HEAD 가 이 커밋으로 온 순간이 남습니다. 맨 위에서부터
    지금 커밋이 이어지는 동안 거슬러 올라가, **처음 이 커밋이 된 순간**을 씁니다.
    (업데이트가 실패해 되돌린 경우는 되돌린 시각이 됩니다 — 그때 다시 이
    버전이 된 것이 맞습니다.) 못 읽으면 None 이고, 화면은 그 줄만 뺍니다.
    """
    out = {"committed": None, "applied": None, "started": PROCESS_STARTED}
    rc, head = await git("rev-parse", "HEAD")
    if rc != 0:
        return out
    rc, ct = await git("log", "-1", "--format=%ct", "HEAD")
    if rc == 0 and ct.strip().isdigit():
        out["committed"] = int(ct.strip())
    rc, log = await git("log", "-g", "--date=unix", "--format=%H %gd", "-n", "50", "HEAD")
    if rc == 0:
        applied = None
        for line in log.splitlines():
            sha, _, sel = line.strip().partition(" ")
            if sha != head:
                break
            m = re.search(r"\{(\d+)\}", sel)
            if m:
                applied = int(m.group(1))
        out["applied"] = applied
    return out


def stale_restart(times: dict) -> bool:
    """새 코드를 받은 뒤 재시작하지 않았는가 — 그러면 봇은 **옛 코드로** 돌고 있습니다."""
    applied = times.get("applied")
    return bool(applied) and applied > times.get("started", 0) + STALE_GRACE


def _modules_to_check() -> list[str]:
    """새 코드에서 실제로 불러올 모듈 전부 (main.py 는 기동 자체라 제외)."""
    mods = []
    for folder in ("utils", "cogs"):
        d = REPO_DIR / folder
        if d.is_dir():
            mods += [f"{folder}.{p.stem}" for p in sorted(d.glob("*.py"))
                     if not p.name.startswith("__")]
    return mods


async def verify_new_code() -> tuple[bool, str]:
    """새 코드를 **별도 프로세스에서** 전부 import 해 봅니다.

    문법 오류 · 없는 라이브러리 · 모듈 최상단의 NameError 를 잡습니다.
    DB 에는 붙지 않습니다(`MONGO_URI` 를 비웁니다) — 확인하는 동안 진짜
    데이터를 건드릴 이유가 없습니다.
    """
    rc, out = await run(sys.executable, "-m", "py_compile", "main.py", "keep_alive.py",
                        timeout=IMPORT_TIMEOUT)
    if rc != 0:
        return False, out
    mods = _modules_to_check()
    code = ("import importlib\n"
            f"for m in {mods!r}:\n"
            "    importlib.import_module(m)\n"
            "print('OK')\n")
    rc, out = await run(sys.executable, "-c", code, timeout=IMPORT_TIMEOUT,
                        env={"MONGO_URI": "", "PYTHONIOENCODING": "utf-8"})
    if rc != 0:
        # 맨 끝 줄들이 원인입니다 (Traceback 의 마지막)
        return False, "\n".join(out.splitlines()[-8:])
    return True, f"모듈 {len(mods)}개 확인"


async def apply_update(target: str) -> dict:
    """`target` 커밋까지 fast-forward → (필요하면 pip) → import 검사.

    실패하면 원래 커밋으로 **되돌립니다.** 돌려주는 것:
      {"ok": bool, "step": 실패한 단계, "detail": 출력, "old": 원래 커밋}
    """
    _, old = await git("rev-parse", "HEAD")
    rc, out = await git("merge", "--ff-only", target)
    if rc != 0:
        return {"ok": False, "step": "merge", "detail": out, "old": old}

    async def rollback(step, detail):
        rrc, rout = await git("reset", "--hard", old)
        note = "" if rrc == 0 else f"\n⚠️ 되돌리기도 실패했습니다: {rout[:300]}"
        return {"ok": False, "step": step, "detail": detail + note, "old": old}

    _, changed = await git("diff", "--name-only", old, target)
    if "requirements.txt" in changed.splitlines():
        rc, out = await run(sys.executable, "-m", "pip", "install", "-q", "-r",
                            "requirements.txt", timeout=PIP_TIMEOUT)
        if rc != 0:
            return await rollback("pip", "\n".join(out.splitlines()[-8:]))

    ok, detail = await verify_new_code()
    if not ok:
        return await rollback("import", detail)
    return {"ok": True, "step": "", "detail": detail, "old": old}


def restart_soon(reason: str) -> None:
    """응답이 도착할 시간을 주고 끝냅니다. systemd 가 5초 뒤 다시 띄웁니다.

    종료 코드 1 — 0 이면 '정상 종료' 로 보고 다시 안 띄울 수 있습니다.
    """
    loop = asyncio.get_running_loop()
    loop.call_later(RESTART_DELAY, health.hard_exit, reason)


# ── 화면 ───────────────────────────────────────────────────────────────

def _add_times(embed: discord.Embed, times: dict | None) -> discord.Embed:
    """마지막 업데이트 · 재시작 시각. 디스코드 시각 표기라 보는 사람의 시간대로 나옵니다."""
    if not times:
        return embed

    def ts(t):
        return f"<t:{int(t)}:f> (<t:{int(t)}:R>)"

    lines = []
    if times.get("applied"):
        lines.append(f"📥 **마지막 업데이트** {ts(times['applied'])}")
    if times.get("committed"):
        lines.append(f"🛠️ 지금 버전을 만든 시각 {ts(times['committed'])}")
    if times.get("started"):
        lines.append(f"🔄 마지막 재시작 {ts(times['started'])}")
    if lines:
        embed.add_field(name="🕒 시각", value="\n".join(lines), inline=False)
    if stale_restart(times):
        # 받아만 두고 재시작하지 않아 며칠간 옛 코드로 돈 적이 있습니다
        embed.add_field(
            name="⚠️ 새 코드를 받은 뒤 재시작하지 않았습니다",
            value="지금 봇은 **받기 전의 옛 코드로** 돌고 있습니다. 아래 **재시작만** 을 누르세요.",
            inline=False)
        embed.color = discord.Color.orange()
    return embed


def build_check_embed(info: dict) -> discord.Embed:
    times = info.get("times")
    if info["blocked"]:
        return _add_times(discord.Embed(
            title="⛔ 업데이트할 수 없는 상태입니다",
            description=info["blocked"] + "\n\n-# 이 경우는 SSH 로 서버에 들어가 확인해야 합니다 "
                                          "(RUNBOOK.md 참고).",
            color=discord.Color.orange()), times)
    if not info["total"]:
        embed = discord.Embed(title="✅ 이미 최신입니다",
                              description=f"지금 버전 `{_short(info['local'])}` = GitHub main",
                              color=discord.Color.green())
        embed.set_footer(text="🔄 재시작만 할 수도 있습니다")
        return _add_times(embed, times)
    lines = "\n".join(f"• `{c[:7]}` {c[8:][:90]}" for c in info["commits"])
    more = info["total"] - len(info["commits"])
    embed = discord.Embed(
        title=f"⬆️ 새 커밋 {info['total']}개가 있습니다",
        description=(f"`{_short(info['local'])}` → `{_short(info['remote'])}`\n\n{lines}"
                     + (f"\n-# 외 {more}개 (오래된 것)" if more > 0 else ""))[:4000],
        color=discord.Color.blurple())
    files = info["files"]
    embed.add_field(name=f"바뀌는 파일 {len(files)}개",
                    value=("\n".join(f"`{f}`" for f in files[:15])
                           + (f"\n… 외 {len(files) - 15}개" if len(files) > 15 else ""))[:1024]
                    or "없음", inline=False)
    if "requirements.txt" in files:
        embed.add_field(name="📦 라이브러리 변경",
                        value="`requirements.txt` 가 바뀌어 설치부터 합니다 (몇 분 걸릴 수 있음).",
                        inline=False)
    embed.set_footer(text="적용 전에 새 코드를 전부 불러와 봅니다 · 실패하면 되돌리고 재시작하지 않습니다")
    return _add_times(embed, times)


async def _admin(interaction: discord.Interaction) -> bool:
    if interaction.user.guild_permissions.administrator:
        return True
    await interaction.response.send_message("⛔ 관리자만 쓸 수 있습니다.", ephemeral=True)
    return False


class UpdateView(discord.ui.View):
    def __init__(self, info: dict):
        super().__init__(timeout=600)
        # 보여 준 **그 커밋까지만** 적용합니다. 확인하는 사이에 새 커밋이 올라와도
        # 관리자가 보지 않은 코드가 섞여 들어가지 않게 합니다.
        self.target = info.get("remote") or ""
        self.apply_btn.disabled = bool(info["blocked"] or not info["total"])

    @discord.ui.button(label="업데이트하고 재시작", style=discord.ButtonStyle.success, emoji="⬆️")
    async def apply_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await _admin(interaction):
            return
        await interaction.response.defer()
        if _lock.locked():
            await interaction.followup.send("⏳ 다른 업데이트가 진행 중입니다.", ephemeral=True)
            return
        async with _lock:
            await interaction.edit_original_response(
                embed=discord.Embed(title="⏳ 적용 중…",
                                    description="새 코드를 받고 확인하는 중입니다. 1~2분 걸립니다.",
                                    color=discord.Color.blurple()), view=None)
            print(f"[Updater] {interaction.user} 가 업데이트 시작 → {_short(self.target)}",
                  flush=True)
            res = await apply_update(self.target)
        if not res["ok"]:
            step = {"merge": "새 코드 받기", "pip": "라이브러리 설치",
                    "import": "새 코드 확인"}.get(res["step"], res["step"])
            print(f"[Updater] 실패({res['step']}) — 되돌림: {res['detail'][:300]}", flush=True)
            await interaction.edit_original_response(embed=discord.Embed(
                title="❌ 업데이트하지 않았습니다",
                description=(f"**{step}** 단계에서 실패해 원래 버전 `{_short(res['old'])}` 으로 "
                             "되돌렸습니다. 봇은 지금 버전 그대로 돌고 있습니다.\n"
                             f"```{res['detail'][-1500:]}```\n"
                             "-# 이 내용을 개발자에게 그대로 전달해 주세요."),
                color=discord.Color.red()), view=None)
            return
        await interaction.edit_original_response(embed=discord.Embed(
            title="✅ 적용 완료 — 재시작합니다",
            description=(f"`{_short(res['old'])}` → `{_short(self.target)}` ({res['detail']})\n"
                         "약 10~30초 뒤 새 코드로 돌아옵니다.\n"
                         "-# 봇 상태 채널의 버전이 바뀌었는지 확인하세요. 새 명령어는 디스코드에서 "
                         "`Ctrl+R` 을 누르면 보입니다."),
            color=discord.Color.green()), view=None)
        restart_soon(f"/봇업데이트 ({interaction.user}) → {_short(self.target)}")

    @discord.ui.button(label="재시작만", style=discord.ButtonStyle.secondary, emoji="🔄")
    async def restart_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await _admin(interaction):
            return
        await interaction.response.edit_message(embed=discord.Embed(
            title="🔄 재시작합니다",
            description="코드는 그대로 두고 봇만 다시 켭니다. 약 10~30초 걸립니다.",
            color=discord.Color.blurple()), view=None)
        restart_soon(f"/봇업데이트 재시작만 ({interaction.user})")


class Updater(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="봇업데이트",
                          description="[관리자 전용] GitHub 의 새 코드를 받아 봇을 재시작합니다.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def update(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        async with _lock:
            info = await check_updates()
            info["times"] = await version_times()
        await interaction.followup.send(embed=build_check_embed(info), view=UpdateView(info),
                                        ephemeral=True)


async def setup(bot):
    await bot.add_cog(Updater(bot))
