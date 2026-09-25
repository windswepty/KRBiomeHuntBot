import os
import sys
import asyncio
import hashlib
import traceback
from concurrent.futures import ThreadPoolExecutor
import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv
import keep_alive as ka
from utils import health

# Load environment variables
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

# Setup Bot Intents
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

# 접두사 `!` 는 Dyno 식 처벌 명령어(!warn · !ban · !warnings, cogs/moderation.py)
# 용입니다. `?` 가 아닌 이유: Dyno 가 아직 서버에 있고 `?warn` 에 "Prefix commands
# have been deprecated" 로 답합니다. 같은 접두사를 쓰면 두 봇이 한꺼번에 답합니다.
# 기본 help 는 영어이고 명령어 목록을 드러내므로 끕니다.
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None,
                   case_insensitive=True)

# 무인 운영 전제입니다. 관리자가 오래 자리를 비워도 스스로 살아나야 하므로,
# 이상을 발견하면 사람을 기다리지 않고 프로세스를 끝냅니다.
# 호스팅(Render)이 죽은 프로세스를 자동으로 새로 띄웁니다.
WATCHDOG_INTERVAL = 60          # 초

# ── 명령어 동기화 지문 ────────────────────────────────────────────────
#
# tree.sync() 는 등록된 전 명령어를 디스코드에 보내므로, 재시작마다
# 호출하면 레이트 리밋을 직격합니다. 명령어 집합이 바뀌지 않았으면
# 건너뛰고, FORCE_SYNC=1 이면 무조건 동기화합니다.

SYNC_HASH_FILE = os.path.join("data", ".sync_hash")


def _command_fingerprint(tree) -> str:
    """디스코드에 등록되는 내용을 전부 담아 해시. 바뀌지 않았으면 같은 값.

    ⚠️ **빠뜨린 항목은 "바뀌었는데 안 바뀐 것"으로 판정됩니다.** 그러면
    동기화를 건너뛰어 변경이 디스코드에 영영 반영되지 않습니다.
    특히 `default_permissions` 는 누가 그 명령어를 쓸 수 있는지를 정하므로,
    권한을 조였는데 반영되지 않으면 그대로 열려 있게 됩니다.
    서브커맨드 그룹도 재귀로 훑습니다(지금은 없지만 나중에 생길 수 있습니다).
    """
    def _one(cmd, prefix=""):
        out = []
        name = f"{prefix}{cmd.name}"
        parts = [name, cmd.description or ""]
        # 누가 쓸 수 있는지 — 빠지면 권한 변경이 조용히 묻힙니다
        parts.append(str(getattr(cmd, "default_permissions", None)))
        parts.append(str(getattr(cmd, "guild_only", None)))
        parts.append(str(getattr(cmd, "nsfw", None)))
        for p in sorted(getattr(cmd, "parameters", []), key=lambda p: p.name):
            parts.extend([p.name, str(p.type), str(p.required),
                          p.description or "",
                          ",".join(str(c.value) for c in (p.choices or []))])
        out.append("|".join(parts))
        for sub in sorted(getattr(cmd, "commands", []) or [],
                          key=lambda c: c.name):
            out.extend(_one(sub, prefix=f"{name} "))
        return out

    items = []
    for cmd in sorted(tree.get_commands(), key=lambda c: c.name):
        items.extend(_one(cmd))
    return hashlib.sha256("\n".join(items).encode()).hexdigest()[:16]


@bot.event
async def on_ready():
    print(f"==========================================", flush=True)
    print(f"Logged in as: {bot.user.name} (ID: {bot.user.id})", flush=True)
    print(f"Connected to {len(bot.guilds)} guild(s)", flush=True)

    # 포인트가 어디에 저장되는지 매 기동마다 눈에 보이게 남깁니다.
    # 임시 저장소로 떨어진 채 운영되면 유저 포인트가 사라지기 때문입니다.
    storage = "unknown"
    try:
        from utils.database import storage_status, STORAGE_MODE
        storage = STORAGE_MODE
        print(f"Point storage: {storage_status()}", flush=True)
        if STORAGE_MODE != "mongo":
            print("*** 포인트가 영구 저장되지 않는 상태입니다. 즉시 확인하세요. ***", flush=True)
    except Exception as e:
        print(f"Storage status check failed: {e}", flush=True)
    print(f"==========================================", flush=True)

    health.mark_ready(len(bot.guilds), storage)

    # ── 조건부 명령어 동기화 ──────────────────────────────────────────
    #
    # 재배포·재시작마다 27개 명령어를 전역 동기화하면 레이트 리밋을
    # 직격합니다. 명령어 집합이 바뀌지 않았으면 건너뜁니다.
    fingerprint = _command_fingerprint(bot.tree)
    force = os.getenv("FORCE_SYNC", "").strip() == "1"
    prev_hash = ""
    try:
        os.makedirs("data", exist_ok=True)
        if os.path.exists(SYNC_HASH_FILE):
            with open(SYNC_HASH_FILE, "r") as f:
                prev_hash = f.read().strip()
    except Exception:
        pass

    if fingerprint == prev_hash and not force:
        # 명령어가 바뀌지 않았으므로 동기화를 건너뜁니다.
        # 이전에 등록한 명령어가 디스코드에 그대로 남아 있습니다.
        health.set_command_count(len(bot.tree.get_commands()))
        print(f"tree.sync() 건너뜀 — 명령어 지문 일치 ({fingerprint})", flush=True)
    else:
        reason = "FORCE_SYNC=1" if force else f"지문 변경 ({prev_hash[:8]}→{fingerprint[:8]})"
        print(f"tree.sync() 실행 — {reason}", flush=True)
        try:
            synced = await bot.tree.sync()
            health.set_command_count(len(synced))
            print(f"Synced {len(synced)} slash command(s).", flush=True)
            # 성공했으면 지문 저장.
            # ⚠️ 여기서 실패를 조용히 넘기면 안 됩니다. 실제로 data/ 가 root
            # 소유라 쓰기가 계속 실패했고, 그 탓에 건너뛰기가 **9번의 기동 동안
            # 한 번도 동작하지 않은 채** 매번 전역 동기화가 나갔습니다.
            # 아무도 몰랐던 이유가 정확히 이 `except: pass` 였습니다.
            try:
                with open(SYNC_HASH_FILE, "w") as f:
                    f.write(fingerprint)
            except Exception as e:
                msg = (f"지문 저장 실패 ({SYNC_HASH_FILE}): {type(e).__name__}: {e}"
                       " — 다음 기동에서도 전역 동기화가 반복됩니다")
                print(f"*** {msg} ***", flush=True)
                health.add_note(msg)
        except discord.HTTPException as e:
            if e.status == 429:
                health.mark_rate_limited()
                print(f"tree.sync() 429 레이트 리밋 — 재시도 없이 기동 계속", flush=True)
            else:
                print(f"Failed to sync slash commands: {e}", flush=True)
            health.set_command_count(len(bot.tree.get_commands()))
            health.add_note(f"슬래시 명령어 동기화 실패: {e}")
        except Exception as e:
            print(f"Failed to sync slash commands: {e}", flush=True)
            health.set_command_count(len(bot.tree.get_commands()))
            health.add_note(f"슬래시 명령어 동기화 실패: {e}")

    if not watchdog.is_running():
        watchdog.start()


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    """슬래시 명령어 실행 중 오류가 발생했을 때 처리합니다.

    핸들러가 없으면 defer된 인터랙션이 응답을 받지 못해
    디스코드에서 '생각 중이에요...' 무한 로딩 상태로 남습니다.
    """
    cmd_name = interaction.command.name if interaction.command else "알 수 없음"
    print(f"[Slash Command Error] /{cmd_name}: {error}", flush=True)
    traceback.print_exception(type(error), error, error.__traceback__)

    # 응답을 보낼 수 없는 오류인가.
    #   10062 Unknown interaction        — 3초 안에 응답 못 해 토큰이 죽음
    #   40060 already acknowledged       — 이미 응답됨(재전송 등)
    # 이때 응답을 시도하면 **반드시 또 실패**합니다.
    # 그리고 그 실패는 400/404 — 디스코드가 무효 요청으로 집계하는
    # 값입니다(10분에 10,000건이면 1시간 차단). 즉 오류 하나당
    # 무효 요청이 두 배로 늘어나, 몰림 때 스스로 차단을 당기는
    # 피드백이 됩니다. 실제로 오류 14건 전부가 그렇게 두 줄씩 남았습니다.
    orig = getattr(error, "original", error)
    code = getattr(orig, "code", None)
    if code in (10062, 40060):
        print(f"[Slash Command Error] 응답 불가(code {code}) — "
              f"재시도하지 않습니다", flush=True)
        return

    if isinstance(error, app_commands.errors.MissingPermissions):
        msg = "⚠️ 이 명령어를 실행할 권한이 없습니다."
    elif isinstance(error, app_commands.errors.CheckFailure):
        msg = "⚠️ 이 명령어를 실행할 수 없습니다."
    else:
        msg = "⚠️ 명령어를 처리하는 도중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요."

    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except discord.HTTPException as e:
        # is_done() 은 **이 프로세스의 기억**일 뿐입니다. 디스코드 쪽이
        # 이미 응답된 것으로 알고 있으면 40060 이 나오는데,
        # 그때는 followup 으로는 보낼 수 있습니다. 딱 한 번만 더 시도합니다.
        if e.code == 40060 and not interaction.response.is_done():
            try:
                await interaction.followup.send(msg, ephemeral=True)
                return
            except discord.HTTPException as e2:
                e = e2
        print(f"[Slash Command Error] 오류 메시지 전송 실패: {e}", flush=True)
    except Exception as e:
        print(f"[Slash Command Error] 오류 메시지 전송 실패: {e}", flush=True)


# ── 연결 상태 추적 ────────────────────────────────────────────────────
# discord.py 는 끊기면 스스로 재접속합니다. 아래 이벤트는 그 과정을
# 기록해 두기 위한 것이고, 재접속이 끝내 안 되는 경우만 watchdog 이 잡습니다.

@bot.event
async def on_disconnect():
    health.mark_disconnected()


@bot.event
async def on_resumed():
    health.mark_resumed()
    print("[Watchdog] 게이트웨이 재연결됨", flush=True)


@tasks.loop(seconds=WATCHDOG_INTERVAL)
async def watchdog():
    """스스로 생존을 확인하고, 회복이 안 되면 재시작합니다.

    좀비 상태(프로세스는 살아 있는데 디스코드 연결만 죽은 경우)는 호스팅이
    감지하지 못합니다. 프로세스가 멀쩡하기 때문입니다. 그래서 봇이 직접
    판단해 끝내야 합니다.
    """
    try:
        if bot.is_closed():
            health.mark_disconnected()
        else:
            latency = bot.latency          # 끊겨 있으면 nan/inf
            if latency == latency and latency != float("inf"):
                health.mark_connected(latency * 1000)
            else:
                health.mark_disconnected()

        # 이유를 함께 받습니다. 억제가 걸린 경우에도 로그가 남아야
        # "제동 장치가 동작 중"임을 밖에서 확인할 수 있습니다.
        go, reason = health.restart_decision()
        if go:
            health.record_restart()
            health.hard_exit(reason)
        elif reason:
            health.log_suppression(reason)
    except Exception as e:
        # 감시 자체가 죽으면 감시가 없는 것과 같습니다. 절대 조용히 넘기지 않습니다.
        print(f"[Watchdog] 점검 중 오류: {type(e).__name__}: {e}", flush=True)


@watchdog.error
async def watchdog_error(error):
    """감시가 죽으면 자가 재시작 능력이 통째로 사라집니다.

    좀비 상태를 감지할 사람이 없다는 것이 이 봇의 전제라, 감시만은
    무슨 일이 있어도 다시 살아나야 합니다.
    """
    await health.revive_loop(watchdog, "watchdog", error)


@watchdog.before_loop
async def before_watchdog():
    await bot.wait_until_ready()


async def load_cogs():
    """cogs/ 의 기능 파일을 모두 불러옵니다.

    **폴더의 파일 수가 '있어야 할 개수'** 입니다. 이걸 분모로 기록해 두면
    나중에 상태 임베드에 `10/11` 처럼 보여줄 수 있고, 하나가 빠졌을 때
    바로 눈에 띕니다. 분모가 없으면 조용히 사라져도 알 수 없습니다.
    """
    os.makedirs("cogs", exist_ok=True)
    targets = [f for f in sorted(os.listdir("./cogs"))
               if f.endswith(".py") and not f.startswith("__")]
    loaded, failed = 0, []
    for filename in targets:
        name = filename[:-3]
        try:
            await bot.load_extension(f"cogs.{name}")
            loaded += 1
            print(f"Loaded extension: cogs.{name}", flush=True)
        except Exception as e:
            failed.append(name)
            print(f"Failed to load extension cogs.{name}: {e}", flush=True)

    health.set_extensions(loaded, len(targets), failed)
    print(f"Extensions: {loaded}/{len(targets)} loaded", flush=True)
    if failed:
        # 일부만 동작하는 봇이라도 아예 없는 것보다는 낫습니다. 다만 조용히
        # 넘어가면 무인 운영 중에 아무도 모르므로 헬스 응답에 남깁니다.
        health.add_note(f"로드 실패한 기능: {', '.join(failed)}")


def stray_instance_reason() -> str:
    """옛 호스팅에서 돌고 있는지 판정합니다. 정상이면 빈 문자열.

    **판정과 조치를 분리합니다** — 이유를 돌려주지 않으면 억제가 걸려도
    로그에 한 줄도 안 남아, 무엇 때문에 멈췄는지 알 수 없습니다.
    """
    if (os.getenv("ALLOW_LEGACY_HOST") or "").strip():
        return ""
    legacy = (os.getenv("RENDER_EXTERNAL_URL") or "").strip()
    if legacy:
        return f"RENDER_EXTERNAL_URL = {legacy}"
    return ""


def check_single_home_or_die() -> None:
    """**이 봇은 한 곳에서만 돌아야 합니다.**

    Oracle 로 옮긴 뒤 옛 호스팅(Render)의 서비스를 지우지 않으면, 같은
    토큰으로 두 번째 인스턴스가 계속 붙어 있습니다. 겉보기에는 아무 문제가
    없습니다 — 양쪽 다 `/health` 가 초록이고 로그도 깨끗합니다.
    실제로는 이런 일이 벌어집니다.

      · 버튼 클릭을 둘이 나눠 처리해서, **어느 쪽이 잡느냐에 따라 서로 다른
        중계 주소**가 나갑니다. 실제로 2026-09-08 에 재 보니 발급된 주소의
        **60%(19/32)** 가 옛 호스팅 것이었습니다.
      · 그쪽은 유휴 시 잠들어 첫 요청에 **25초 넘게** 걸립니다. 토큰 수명은
        90초이고, 깨어나며 프로세스가 새로 뜨면 메모리의 토큰이 통째로
        사라집니다 — **받은 사람은 개인서버에 못 들어갑니다.**
      · 상호작용을 둘이 동시에 처리해 10062 / 40060 이 늘어납니다.
      · 둘 다 같은 DB 에 씁니다.

    그래서 옛 호스팅에서는 **아예 기동하지 않습니다.** 근본 해결은 그쪽
    서비스를 지우는 것이고, 이건 그때까지의 안전장치입니다.
    정말 그쪽에서 돌려야 하면 `ALLOW_LEGACY_HOST=1` 을 주세요.
    """
    reason = stray_instance_reason()
    if not reason:
        return
    print("=" * 60, flush=True)
    print("옛 호스팅 환경에서 실행됐습니다. 기동을 중단합니다.", flush=True)
    print(f"  감지: {reason}", flush=True)
    print("이 봇은 Oracle 인스턴스 한 곳에서만 돌아야 합니다.", flush=True)
    print("두 곳에서 돌면 클릭을 나눠 처리해 잠든 쪽 주소가 발급되고,", flush=True)
    print("그 주소를 받은 사람은 개인서버에 들어가지 못합니다.", flush=True)
    print("Render 대시보드에서 이 서비스를 Suspend 또는 Delete 하세요.", flush=True)
    print("=" * 60, flush=True)
    sys.stdout.flush()
    os._exit(1)


def check_storage_or_die() -> None:
    """MONGO_URI 를 줬는데 접속이 안 되면 **차라리 죽습니다**.

    폴백(로컬 JSON)으로 조용히 넘어가면 봇은 정상처럼 보이는데 Render
    파일시스템은 휘발성이라 재시작 때 포인트가 전부 사라집니다. 무인
    운영에서는 아무도 눈치채지 못하고 며칠치가 날아갈 수 있습니다.

    멈춰 있으면 호스팅이 재시작을 반복하고, Atlas 가 돌아오면 저절로
    복구됩니다. **틀린 데이터로 도는 것보다 잠깐 멈추는 편이 낫습니다.**
    """
    if not (os.getenv("MONGO_URI") or "").strip():
        print("MONGO_URI 가 설정되지 않았습니다. 로컬 파일 모드로 실행합니다.", flush=True)
        return
    try:
        from utils.database import STORAGE_MODE
    except Exception as e:
        print(f"저장소 상태를 확인할 수 없습니다: {e}", flush=True)
        return
    if STORAGE_MODE != "mongo":
        print("=" * 60, flush=True)
        print("MONGO_URI 가 설정돼 있는데 MongoDB 에 접속하지 못했습니다.", flush=True)
        print("이 상태로 계속 돌면 포인트가 임시 파일에 쌓였다가 사라집니다.", flush=True)
        print("데이터를 지키기 위해 기동을 중단합니다. 잠시 후 자동으로 다시 시도합니다.", flush=True)
        print("=" * 60, flush=True)
        sys.stdout.flush()
        os._exit(1)


async def main():
    if not TOKEN:
        print("ERROR: DISCORD_TOKEN is not set in .env file!", flush=True)
        sys.exit(1)

    # ── DB 워커 스레드 수 ────────────────────────────────────────────
    #
    # 모든 pymongo 호출은 `_db()` 를 통해 `asyncio.to_thread` 로 넘어가고,
    # 그것은 이벤트 루프의 **기본 실행기**를 씁니다. 기본 크기는
    # `min(32, cpu_count + 4)` 라 2코어 서버에서는 **워커가 6개뿐**입니다.
    #
    # 그런데 이 작업들은 CPU 가 아니라 **Atlas 응답을 기다리는 시간**이
    # 대부분입니다. CPU 수로 정할 값이 아닙니다. 개인서버 링크에 수백 명이
    # 몰리면 클릭 뒷정리 작업이 이 6개에 줄을 서고, 그동안 슬래시 명령어의
    # DB 조회까지 밀려 **디스코드 3초 제한**을 넘깁니다.
    workers = max(4, int(os.getenv("DB_WORKERS", "24") or 24))
    asyncio.get_running_loop().set_default_executor(
        ThreadPoolExecutor(max_workers=workers, thread_name_prefix="db"))
    print(f"DB 워커 스레드: {workers}개 (기본값 "
          f"{min(32, (os.cpu_count() or 1) + 4)}개에서 상향)", flush=True)

    async with bot:
        await load_cogs()
        await bot.start(TOKEN)


# ── 429 재시도 래퍼 ──────────────────────────────────────────────────
#
# bot.start(TOKEN) 이 429 로 실패하면 Render 가 프로세스를 재시작합니다.
# 재시작하면 또 로그인을 시도하고, 그것도 무효 요청으로 집계돼 차단이
# 연장됩니다. **프로세스 안에서 기다려야** 이 고리가 끊어집니다.
#
# 429 를 받으면 60초 간격으로 최대 30회(약 30분) 기다린 뒤 재시도합니다.
# 그래도 안 되면 그때 프로세스를 종료해 Render 에 맡깁니다.

MAX_429_RETRIES = 30
RETRY_WAIT_SECONDS = 60


def _run_with_429_retry():
    """429 에 걸려도 프로세스를 즉시 죽이지 않고, 충분히 기다린 뒤 재시도."""
    import time as _time

    # Flask 헬스 서버를 가장 먼저 띄웁니다. 429 대기 중에도 /health 가
    # 응답해야 Render 가 프로세스를 강제 종료하지 않습니다.
    check_single_home_or_die()
    check_storage_or_die()
    ka.set_bot(bot)
    ka.keep_alive()

    for attempt in range(1, MAX_429_RETRIES + 1):
        try:
            # ⚠️ `async with bot:` 이 끝날 때 close() 가 불려 `_closed` 가
            # True 로 남습니다. discord.py 의 connect() 는
            # `while not self.is_closed():` 라, 그대로 재시도하면 **즉시
            # 반환**해 버립니다. 차단이 풀렸어도 연결하지 못한 채 프로세스가
            # 끝나고, 그 재시작이 다시 무효 요청이 됩니다 — 이 재시도 루프가
            # 막으려던 바로 그 고리입니다. clear() 로 되살려야 합니다.
            if attempt > 1:
                bot.clear()
            asyncio.run(main())
            # main() 이 정상 반환 → 봇이 스스로 종료 (무인 운영에서는 비정상)
            return
        except KeyboardInterrupt:
            return
        except discord.HTTPException as e:
            if e.status == 429:
                health.mark_rate_limited()
                print(f"[Boot] 429 전역 레이트 리밋 — "
                      f"{RETRY_WAIT_SECONDS}초 대기 후 재시도 "
                      f"({attempt}/{MAX_429_RETRIES})", flush=True)
                _time.sleep(RETRY_WAIT_SECONDS)
                continue
            # 429 가 아닌 HTTP 오류는 재시도해도 의미 없음
            print(f"FATAL: {type(e).__name__}: {e}", flush=True)
            sys.exit(1)
        except Exception as e:
            print(f"FATAL: {type(e).__name__}: {e}", flush=True)
            sys.exit(1)

    # 30회 재시도(약 30분) 후에도 실패
    print(f"[Boot] {MAX_429_RETRIES}회 재시도 후에도 429 — "
          "프로세스를 종료합니다", flush=True)
    sys.exit(1)


if __name__ == "__main__":
    _run_with_429_retry()
    # 정상 흐름으로 여기까지 왔다는 것은 봇이 스스로 종료된 것입니다.
    # 무인 운영에서는 이것도 비정상이므로 재시작을 유도합니다.
    sys.exit(1)

