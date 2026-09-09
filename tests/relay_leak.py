"""중계 주소(/j/<토큰>)가 서버에 그대로 게시되는 경우.

이 주소는 **한 사람에게만 발급된 개인 주소**라, 게시되면 누구 것인지가
추측이 아니라 발급 기록으로 나옵니다. 워터마크와 달리 로블록스를 거치지
않으므로 표식이 사라지지도 않습니다.

주의할 지점이 두 가지입니다.
  · **게시자와 발급 대상은 다릅니다.** 자기 주소를 올린 것은 문의였을 수
    있으므로 남의 주소를 올린 것과 같은 제목으로 알리면 안 됩니다.
  · **호스트를 정확히 맞춰야 합니다.** 접두사만 보면 남의 도메인
    (relay.test.evil.com)까지 우리 주소로 오인합니다.
"""
import sys, os, tempfile, asyncio

PROJ = sys.argv[1]
sys.path.insert(0, PROJ)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(tempfile.mkdtemp())
os.environ["MONGO_URI"] = ""
os.environ["PUBLIC_BASE_URL"] = "https://relay.test"

import discord
import fakemongo as FM
import fakes as F
from fakes import GUILD, Member, Msg, note, drain

VDB_COLS = ["verify_col", "linkguard_col", "plink_col", "clicklog_col",
            "watermark_col", "guardkey_col", "usage_col", "logref_col",
            "roblox_col"]

TOKEN = "AbCdEfGhIjKlMnOpQrSt"          # 20자 — 실제 토큰은 32자입니다
CH = GUILD.channels[0]                  # 잡담
LOG = GUILD.get_channel(333)            # 로그


def post(author, content, mid=7000):
    m = Msg(mid, CH, author)
    m.content = content
    m.guild = GUILD
    m.webhook_id = None
    return m


def last_embed():
    return LOG.sent[-1].get("embed") if LOG.sent else None


def fields_of(embed):
    return {f["name"]: f["value"] for f in embed.to_dict().get("fields", [])}


def field_value(embed, prefix):
    """필드 이름에 인원수가 붙으므로 접두사로 찾습니다."""
    for name, value in fields_of(embed).items():
        if name.startswith(prefix):
            return value
    return None


async def main():
    import utils.verify_db as vdb
    import utils.link_tokens as LT

    FM.install(vdb, VDB_COLS)
    if hasattr(vdb, "_AFTER"):
        vdb._AFTER = FM.AFTER

    from discord.ext import commands
    bot = commands.Bot(command_prefix="!", intents=discord.Intents.all())
    for f in sorted(os.listdir(os.path.join(PROJ, "cogs"))):
        if f.endswith(".py") and not f.startswith("__"):
            await bot.load_extension(f"cogs.{f[:-3]}")
    lg = bot.get_cog("LinkGuard")
    import cogs.link_guard as LGM

    await vdb.save_linkguard_settings(GUILD.id, {
        "enabled": True, "log_channel_id": 333,
        "auto_delete_leak": True, "relay_links": True})

    owner = Member(41, "받은사람", False, guild=GUILD)
    other = Member(42, "퍼뜨린사람", False, guild=GUILD)

    # 발급 기록을 남깁니다 (실제로는 버튼을 눌렀을 때 남습니다)
    await vdb.record_link_usage(GUILD.id, 7001, owner.id, owner.display_name,
                                issued_url="https://relay.test/j/" + TOKEN,
                                watermark="deadbeef")

    print("=== 1. 주소를 뽑아내는 규칙 ===")
    note(LGM.relay_tokens_in("이거 https://relay.test/j/" + TOKEN + " 써") == [TOKEN],
         "우리 중계 주소에서 토큰을 뽑음")
    note(LGM.relay_tokens_in("https://other.example/j/" + TOKEN) == [],
         "남의 도메인의 /j/ 는 무시")
    note(LGM.relay_tokens_in("https://relay.test.evil.com/j/" + TOKEN) == [],
         "호스트 접두사만 같은 주소는 무시")
    note(LGM.relay_tokens_in("https://relay.test/health") == [],
         "다른 경로는 무시")
    note(LGM.relay_tokens_in("a https://relay.test/j/" + TOKEN +
                             " b https://relay.test/j/" + TOKEN) == [TOKEN],
         "같은 토큰이 두 번 나와도 하나로")
    os.environ["PUBLIC_BASE_URL"] = ""
    note(LGM.relay_tokens_in("https://relay.test/j/" + TOKEN) == [],
         "공개 주소가 없으면(중계 미사용) 아무것도 안 잡음")
    os.environ["PUBLIC_BASE_URL"] = "https://relay.test"

    print("")
    print("=== 2. 남의 주소를 올리면 유출 확정 ===")
    LOG.sent.clear()
    m = post(other, "이거 써 https://relay.test/j/" + TOKEN)
    await lg._detect_leak(m)
    await drain()
    e = last_embed()
    note(e is not None, "로그 임베드를 보냄")
    note(m.deleted is True, "메시지를 삭제함")
    d = e.to_dict() if e else {}
    note(d.get("title") == "🚨 중계 주소 유출", "제목", str(d.get("title")))
    note(d.get("color") == discord.Color.red().value, "빨간색")
    ff = fields_of(e)
    note(str(owner.id) in (field_value(e, "🎫 발급 대상") or ""),
         "발급 대상을 특정", str(field_value(e, "🎫 발급 대상")))
    note(str(other.id) in ff.get("🙋 게시한 유저", ""), "게시자도 표시")
    note("다른 사람에게 발급된 주소" in ff.get("🧭 판정", ""),
         "판정이 '남의 주소'", ff.get("🧭 판정", "")[:40])
    note(owner.display_name in ff.get("🧭 판정", ""),
         "발급 대상 이름도 함께")

    logs = await vdb.get_click_logs(GUILD.id, limit=5, event_type="leak")
    note(len(logs) == 1, "유출 기록이 남음", str(len(logs)) + "건")
    note(TOKEN not in str(logs), "토큰 전체는 기록에 남기지 않음")

    print("")
    print("=== 3. 본인 주소를 올린 것은 다르게 알림 (문의일 수 있음) ===")
    LOG.sent.clear()
    m2 = post(owner, "이거 눌러도 안 돼요 https://relay.test/j/" + TOKEN, 7002)
    await lg._detect_leak(m2)
    await drain()
    e2 = last_embed()
    d2 = e2.to_dict()
    note(d2.get("title") == "⚠️ 개인 전용 주소가 공개됨", "제목이 다름",
         str(d2.get("title")))
    note(d2.get("color") == discord.Color.orange().value, "주황색")
    f2 = fields_of(e2)
    note("본인에게 발급된 주소" in f2.get("🧭 판정", ""), "판정이 '본인 주소'")
    note(m2.deleted is True, "그래도 삭제는 함 (남이 토큰을 태울 수 있음)")

    print("")
    print("=== 4. 아직 살아있는 토큰은 메모리에서 바로 특정 ===")
    LT.clear()
    live = LT.issue("https://www.roblox.com/share?code=x&type=Server", 43, GUILD.id)
    LOG.sent.clear()
    m3 = post(other, "https://relay.test/j/" + live, 7003)
    await lg._detect_leak(m3)
    await drain()
    f3 = fields_of(last_embed())
    note("43" in (field_value(last_embed(), "🎫 발급 대상") or ""),
         "발급 기록이 없어도 특정", str(field_value(last_embed(), "🎫 발급 대상")))
    note(LT.peek_owner(live) is not None, "확인만 하고 토큰을 소비하지 않음")

    print("")
    print("=== 5. 기록이 없어도 조용히 넘기지 않음 ===")
    LT.clear()
    LOG.sent.clear()
    m4 = post(other, "https://relay.test/j/ZZZZZZZZZZZZZZZZZZZZ", 7004)
    await lg._detect_leak(m4)
    await drain()
    f4 = fields_of(last_embed())
    note(field_value(last_embed(), "🎫 발급 대상") == "기록 없음",
         "발급 대상은 '기록 없음'",
         str(field_value(last_embed(), "🎫 발급 대상")))
    note("찾지 못했습니다" in f4.get("🧭 판정", ""), "이유를 설명함")
    note(m4.deleted is True, "그래도 삭제함")

    print("")
    print("=== 6. 자동 삭제를 끄면 지우지 않되 기록은 남김 ===")
    await vdb.save_linkguard_settings(GUILD.id, {"auto_delete_leak": False})
    LOG.sent.clear()
    m5 = post(other, "https://relay.test/j/" + TOKEN, 7005)
    await lg._detect_leak(m5)
    await drain()
    note(m5.deleted is False, "삭제하지 않음")
    note(last_embed() is not None, "로그는 남김")
    note("삭제하지 못했습니다" in str(last_embed().to_dict()),
         "삭제하지 않았음을 알림")
    await vdb.save_linkguard_settings(GUILD.id, {"auto_delete_leak": True})

    print("")
    print("=== 7. 잡으면 안 되는 것 ===")
    LOG.sent.clear()
    wh = post(other, "https://relay.test/j/" + TOKEN, 7006)
    wh.webhook_id = 999
    await lg._detect_leak(wh)
    note(not LOG.sent and wh.deleted is False, "웹훅 메시지는 무시")

    bm = post(other, "https://relay.test/j/" + TOKEN, 7007)
    bm.author.bot = True
    await lg._detect_leak(bm)
    bm.author.bot = False
    note(not LOG.sent and bm.deleted is False, "봇 메시지는 무시")

    await vdb.save_linkguard_settings(GUILD.id, {"enabled": False})
    off = post(other, "https://relay.test/j/" + TOKEN, 7008)
    await lg._detect_leak(off)
    note(not LOG.sent and off.deleted is False, "시스템이 꺼져 있으면 무시")
    await vdb.save_linkguard_settings(GUILD.id, {"enabled": True})

    print("")
    print("=== 8. on_message 에 실제로 연결돼 있음 ===")
    LOG.sent.clear()
    m6 = post(other, "https://relay.test/j/" + TOKEN, 7009)
    await lg.on_message(m6)
    await drain()
    note(m6.deleted is True and last_embed() is not None,
         "리스너 경로로도 잡힘")

    print("")
    print("=== 9. 발급 기록 조회 (find_issued_to) ===")
    hit = await vdb.find_issued_to(GUILD.id, TOKEN)
    note(hit is not None and hit["user_id"] == str(owner.id),
         "토큰으로 소유자 조회", str(hit))
    note(hit and hit.get("message_id") == "7001", "어느 링크였는지도 알려줌",
         str(hit.get("message_id") if hit else None))
    note(await vdb.find_issued_to(GUILD.id, "없는토큰") is None,
         "없는 토큰은 None")
    note(await vdb.find_issued_to(GUILD.id, "") is None, "빈 값은 None")
    note(await vdb.find_issued_to(GUILD.id + 999, TOKEN) is None,
         "다른 길드의 기록은 보이지 않음")

    print("")
    print("=== 10. 우회 시도 (감사에서 나온 것들) ===")
    ev = [
        ("대문자로 적기", "HTTPS://RELAY.TEST/j/" + TOKEN, True),
        ("포트를 붙이기", "https://relay.test:443/j/" + TOKEN, True),
        ("주소 뒤 물음표", "https://relay.test/j/" + TOKEN + "?v=1", True),
        ("꺾쇠로 감싸기", "<https://relay.test/j/" + TOKEN + ">", True),
        ("마크다운 뒤에 숨기기", "[click](https://relay.test/j/" + TOKEN + ")", True),
        ("하위 도메인 흉내", "https://evil.relay.test/j/" + TOKEN, False),
        ("뒤에 붙인 가짜 도메인", "https://relay.test.evil.com/j/" + TOKEN, False),
        ("경로로 흉내", "https://evil.com/relay.test/j/" + TOKEN, False),
    ]
    for label, text, want in ev:
        note(bool(LGM.relay_tokens_in(text)) is want, label,
             "잡힘" if LGM.relay_tokens_in(text) else "안 잡힘")

    print("")
    print("=== 11. 남의 주소를 모아 뿌리면 전부 짚어야 ===")
    LT.clear()
    LOG.sent.clear()
    toks = []
    for n in range(7):
        t = f"MultiToken{n}Padding0000"
        toks.append(t)
        await vdb.record_link_usage(GUILD.id, 7200 + n, 60 + n, f"피해자{n}",
                                    issued_url="https://relay.test/j/" + t)
    collector = Member(70, "수집가", False, guild=GUILD)
    m7 = post(collector,
              chr(10).join("https://relay.test/j/" + t for t in toks), 7300)
    await lg._detect_leak(m7)
    await drain()
    e7 = last_embed()
    target = field_value(e7, "🎫 발급 대상") or ""
    note(e7.to_dict().get("title") == "🚨 중계 주소 유출", "유출로 판정")
    note(sum(f"<@{60 + n}>" in target for n in range(7)) == LGM.MAX_RELAY_TOKENS,
         f"상한 {LGM.MAX_RELAY_TOKENS}명까지 특정 (무한 조회 방지)", target[:60])
    note(field_value(e7, "🔗 게시된 주소 수") == "**7개**",
         "몇 개를 뿌렸는지 그대로 알림", str(field_value(e7, "🔗 게시된 주소 수")))
    note("명분을 모아" in (field_value(e7, "🧭 판정") or ""),
         "'모아서 뿌렸다'를 앞세움")

    print("")
    print("=== 12. 보낸 뒤 고쳐서 끼워 넣는 회피 ===")
    LOG.sent.clear()
    clean = post(collector, "안녕하세요", 7400)
    await lg.on_message(clean)
    note(not LOG.sent, "깨끗한 글은 그냥 지나감")
    edited = post(collector, "https://relay.test/j/" + TOKEN, 7400)
    await lg.on_message_edit(clean, edited)
    await drain()
    note(bool(LOG.sent), "수정본도 검사함")
    note(edited.deleted is True, "수정본을 삭제함")

    before_count = len(LOG.sent)
    again = post(collector, "https://relay.test/j/" + TOKEN, 7400)
    await lg.on_message_edit(edited, again)
    await drain()
    note(len(LOG.sent) == before_count,
         "같은 메시지를 또 고쳐도 다시 알리지 않음 (도배 방지)",
         f"{before_count} -> {len(LOG.sent)}")

    print("")
    print("=== 13. 소유자 조회는 '정확히' 맞아야 ===")
    note(await vdb.find_issued_to(GUILD.id, "relay.test") is None,
         "모든 주소에 든 문자열로는 아무도 짚히지 않음")
    note(await vdb.find_issued_to(GUILD.id, TOKEN[:8]) is None,
         "토큰 앞부분만으로는 짚히지 않음")
    full = await vdb.find_issued_to(GUILD.id, "https://relay.test/j/" + TOKEN)
    note(full is not None and full["user_id"] == str(owner.id),
         "주소 전체를 넣어도 찾아짐", str(full))

    print("")
    print("=== 14. 개인서버 코드 직접 게시(bare code) 및 대문자 주소 감지 ===")
    bare_ps_code = "c26ae66df0241bba12345678"
    await vdb.register_guarded_keys(GUILD.id, [f"ps:{bare_ps_code}"])
    LOG.sent.clear()
    bare_msg = post(other, f"야 서버 코드 {bare_ps_code} 이거 복사해서 들어와", 7500)
    await lg._detect_leak(bare_msg)
    await drain()
    note(bare_msg.deleted is True, "URL 없는 개인서버 코드 단독 게시도 삭제함")
    note(bool(LOG.sent), "코드 단독 게시도 로그 알림을 전송함")

    LOG.sent.clear()
    upper_msg = post(other, f"HTTPS://WWW.ROBLOX.COM/GAMES/SHARE?CODE={bare_ps_code}&TYPE=SERVER", 7501)
    await lg._detect_leak(upper_msg)
    await drain()
    note(upper_msg.deleted is True, "대문자 HTTPS URL 도 삭제함")
    note(bool(LOG.sent), "대문자 URL 도 로그 알림을 전송함")

    print("")
    print("=== 16. 예외 채널이라도 중계 주소는 잡아야 ===")
    # 사람이 자기 개인서버를 정상적으로 공유하는 채널을 예외로 둡니다.
    # 하지만 `/j/` 는 한 사람에게만 발급된 개인 주소라 어느 채널에서든
    # 올라올 이유가 없습니다. 여기까지 면제하면 **그 채널이 그대로
    # 유출 통로가 됩니다.**
    await vdb.save_linkguard_settings(GUILD.id, {"exception_channel_ids": [CH.id]})
    LOG.sent.clear()
    m16 = post(other, "https://relay.test/j/" + TOKEN, 7600)
    await lg._detect_leak(m16)
    await drain()
    note(bool(LOG.sent), "예외 채널에서도 중계 주소 유출은 잡힘")
    note(m16.deleted is True, "삭제도 그대로 함")

    print("")
    print("=== 17. 예외 채널의 '보통' 보호 링크는 면제돼야 ===")
    await vdb.register_guarded_keys(GUILD.id, ["ps:exceptioncode123"])
    LOG.sent.clear()
    m17 = post(other,
               "https://www.roblox.com/games/1/x?privateServerLinkCode=exceptioncode123",
               7601)
    await lg._detect_leak(m17)
    await drain()
    note(not LOG.sent and m17.deleted is False,
         "예외 채널에서는 일반 보호 링크를 건드리지 않음")

    # 예외를 풀면 같은 링크가 다시 잡혀야 합니다 (예외가 진짜 원인이었는지 확인)
    await vdb.save_linkguard_settings(GUILD.id, {"exception_channel_ids": []})
    LOG.sent.clear()
    m18 = post(other,
               "https://www.roblox.com/games/1/x?privateServerLinkCode=exceptioncode123",
               7602)
    await lg._detect_leak(m18)
    await drain()
    note(bool(LOG.sent) and m18.deleted is True,
         "예외를 풀면 같은 링크가 다시 잡힘 (예외가 원인이 맞음)")

asyncio.run(main())
print("\n  실패 " + str(len(F.FAILS)) + "건 / 성공 " + str(len(F.OKS)) + "건")
for x in F.FAILS:
    print("   -", x)
sys.exit(1 if F.FAILS else 0)
