"""`/제보` 안내 임베드 꾸미기 — 실제로 콜백을 실행해 확인합니다.

cog 로드 검사만으로는 부족합니다. UI 는 유저가 누를 때 처음 실행되므로,
여기서 드롭다운·모달을 전부 눌러 봅니다.
"""
import sys, os, tempfile, asyncio

PROJ = sys.argv[1]
sys.path.insert(0, PROJ)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(tempfile.mkdtemp())
os.environ["MONGO_URI"] = ""
os.environ.setdefault("DISCORD_TOKEN", "x")

import discord
import fakes as F
from fakes import note, run, drain
import cogs.report as R
from utils.database import get_report_settings, save_report_settings

GID = F.GUILD.id


# ── 채널 링크 → 채널 이름 ──────────────────────────────────────────
def test_links():
    print("=== 채널 링크를 붙여넣으면 채널 이름으로 보여야 합니다 ===")
    ch = "222233334444555566"
    same = f"https://discord.com/channels/{GID}/{ch}"

    note(R.beautify_links(same, GID) == f"<#{ch}>",
         "채널 링크 → 채널 멘션", R.beautify_links(same, GID))

    note(R.beautify_links(f"공지는 {same} 를 보세요", GID) == f"공지는 <#{ch}> 를 보세요",
         "문장 속에서도 바뀜")

    two = f"{same} 와 {same}"
    note(R.beautify_links(two, GID).count(f"<#{ch}>") == 2, "여러 개 전부 바뀜")

    # 메시지 링크는 특정 메시지를 가리키려고 넣은 것입니다. 채널 멘션으로
    # 바꾸면 뜻이 달라지므로 건드리면 안 됩니다.
    msg = f"https://discord.com/channels/{GID}/{ch}/1543122457777541170"
    note(R.beautify_links(msg, GID) == msg, "메시지 링크는 그대로")

    other = f"https://discord.com/channels/999999999999999999/{ch}"
    note(R.beautify_links(other, GID) == other, "다른 서버 링크는 그대로")

    ext = "https://www.roblox.com/games/123"
    note(R.beautify_links(ext, GID) == ext, "바깥 주소는 그대로")

    note(R.beautify_links("", GID) == "", "빈 문자열 안전")
    note(R.beautify_links(None, GID) is None, "None 안전")

    canary = f"https://canary.discord.com/channels/{GID}/{ch}"
    note(R.beautify_links(canary, GID) == f"<#{ch}>", "canary/ptb 주소도 인식")

    # 마스킹 링크는 디스코드가 알아서 렌더링합니다. 손대면 안 됩니다.
    masked = "[공략](https://example.com)"
    note(R.beautify_links(masked, GID) == masked, "마스킹 링크는 그대로")


# ── 색상 ───────────────────────────────────────────────────────────
def test_color():
    print("\n=== 색상 입력 ===")
    note(R.parse_color("파랑") is not None, "한글 색이름")
    note(R.parse_color("#5865F2").value == 0x5865F2, "#RRGGBB")
    note(R.parse_color("5865F2").value == 0x5865F2, "# 없이도")
    note(R.parse_color("  파랑  ") is not None, "앞뒤 공백 허용")
    note(R.parse_color("무지개") is None, "모르는 색 → None")
    note(R.parse_color("#12345") is None, "자릿수 틀리면 None")
    note(R.parse_color("") is None, "빈 값 → None")
    note(R.parse_color(None) is None, "None 안전")


# ── 임베드 생성 ────────────────────────────────────────────────────
def test_embed():
    print("\n=== 임베드 생성 ===")
    base = {"enabled": True}
    e = R.build_report_embed(base, True, GID)
    note(e.title == R.DEFAULT_EMBED_TITLE, "설정이 비면 기본 제목", e.title)
    note(e.description == R.DEFAULT_EMBED_DESC, "설정이 비면 기본 설명")
    note(e.color == discord.Color.blue(), "기본 색")
    note(e.image.url is None, "이미지 없음이 기본")

    ch = "222233334444555566"
    custom = {
        "embed_title": "제목입니다",
        "embed_description": f"채널: https://discord.com/channels/{GID}/{ch}",
        "embed_color": "빨강",
        "embed_footer": "푸터",
        "embed_image": "https://x/a.png",
        "embed_thumbnail": "https://x/t.png",
    }
    e = R.build_report_embed(custom, True, GID)
    note(e.title == "제목입니다", "제목 반영")
    note(f"<#{ch}>" in e.description, "설명 안의 채널 링크가 멘션으로")
    note(e.color.value == 0xE74C3C, "색상 반영")
    note(e.footer.text == "푸터", "푸터 반영")
    note(e.image.url == "https://x/a.png", "큰 이미지 반영")
    note(e.thumbnail.url == "https://x/t.png", "썸네일 반영")

    # 비활성 상태에서는 '지금 못 쓴다'가 꾸밈보다 먼저 보여야 합니다.
    e = R.build_report_embed(custom, False, GID)
    note("비활성화" in e.title, "비활성 표시가 제목에", e.title)
    note(e.color == discord.Color.red(), "비활성이면 빨강 (꾸민 색보다 우선)")

    # 상한을 넘기면 전송이 400 으로 실패하고 원인이 안 남습니다.
    over = {"embed_title": "가" * 400, "embed_description": "나" * 5000,
            "embed_footer": "다" * 3000}
    e = R.build_report_embed(over, True, GID)
    note(len(e.title) <= 256, f"제목 상한 {len(e.title)}")
    note(len(e.description) <= 4096, f"설명 상한 {len(e.description)}")
    note(len(e.footer.text) <= 2048, f"푸터 상한 {len(e.footer.text)}")

    bad = {"embed_image": "내컴퓨터/a.png", "embed_thumbnail": "ftp://x/t.png"}
    e = R.build_report_embed(bad, True, GID)
    note(e.image.url is None and e.thumbnail.url is None,
         "잘못된 주소는 무시 (임베드 전체가 실패하지 않게)")


# ── 버튼 ───────────────────────────────────────────────────────────
def test_button():
    print("\n=== 티켓 버튼 ===")
    v = R.TicketLaunchView()
    b = v.children[0]
    note(b.label == "티켓 열기", "기본 문구", b.label)
    note(b.custom_id == "report_ticket_open", "custom_id 고정", b.custom_id)
    note(b.disabled is False, "평소엔 눌림")

    v = R.TicketLaunchView("제보하기")
    b = v.children[0]
    note(b.label == "제보하기", "문구 변경 반영", b.label)
    note(b.custom_id == "report_ticket_open",
         "문구를 바꿔도 custom_id 는 그대로 (재시작 후에도 동작)")

    v = R.TicketLaunchView("제보하기", preview_only=True)
    note(v.children[0].disabled is True, "미리보기 버튼은 잠김")

    v = R.TicketLaunchView("가" * 200)
    note(len(v.children[0].label) <= 80, "버튼 문구 상한", str(len(v.children[0].label)))


# ── 드롭다운 전 항목 실행 ──────────────────────────────────────────
async def test_dropdown():
    print("\n=== 드롭다운 항목을 전부 실제로 눌러 봅니다 ===")
    for value in ("toggle", "default_points", "notify_role", "report_channel",
                  "decor", "decor_image", "preview", "decor_help",
                  "decor_reset", "reset"):
        settings = await get_report_settings(GID)

        async def go(it, v=value, s=settings):
            sel = R.ReportSettingsSelect(s)
            sel._values = [v]
            await sel.callback(it)

        await run(f"드롭다운 · {value}", go)


async def test_reset_scope():
    print("\n=== '꾸미기만 초기화' 는 다른 설정을 건드리면 안 됩니다 ===")
    await save_report_settings(GID, {
        "default_points": 777,
        "notify_role_id": 111122223333444455,
        "report_channel_id": 333344445555666677,
        "embed_title": "꾸민 제목",
        "embed_color": "보라",
        "button_label": "눌러",
    })
    settings = await get_report_settings(GID)

    it = F.Inter()
    sel = R.ReportSettingsSelect(settings)
    sel._values = ["decor_reset"]
    await sel.callback(it)
    await drain()

    after = await get_report_settings(GID)
    note(all(after.get(k) is None for k in R.DECOR_KEYS),
         "꾸미기 항목은 전부 비워짐")
    note(after.get("default_points") == 777, "포인트 유지", str(after.get("default_points")))
    note(after.get("notify_role_id") == 111122223333444455, "알림 역할 유지")
    note(after.get("report_channel_id") == 333344445555666677, "카테고리 유지")

    e = R.build_report_embed(after, True, GID)
    note(e.title == R.DEFAULT_EMBED_TITLE, "초기화 후 기본 제목으로 복귀")


async def test_panel_refresh():
    print("\n=== 값을 바꾸면 패널이 즉시 다시 그려져야 합니다 ===")
    # 다시 그리지 않으면 드롭다운 설명이 'View 를 만들 때 값'으로 굳어,
    # 껐는데도 '활성화'로 보입니다.
    await save_report_settings(GID, {"enabled": True})
    settings = await get_report_settings(GID)

    it = F.Inter()
    sel = R.ReportSettingsSelect(settings)
    sel._values = ["toggle"]
    await sel.callback(it)
    await drain()

    note(it.response.edited is not None, "패널 메시지를 고쳤음(edit_message)")
    edited = it.response.edited or {}
    view = edited.get("view")
    note(view is not None, "새 View 로 교체됨")
    if view is not None:
        opt = next(o for o in view.children[0].options if o.value == "toggle")
        note("비활성화" in (opt.description or ""),
             "드롭다운 설명이 바뀐 값을 반영", opt.description)
    note(bool(it.followup.sent), "안내는 followup 으로 따로 나감")


async def test_command_matches_preview():
    print("\n=== /제보 와 미리보기가 같은 결과여야 합니다 ===")
    await save_report_settings(GID, {
        "enabled": True, "embed_title": "동일성 확인", "embed_color": "초록"})
    settings = await get_report_settings(GID)

    cog = R.Report(None)
    it = F.Inter()
    await cog.report_biome.callback(cog, it)
    await drain()

    sent = it.response.sent[0] if it.response.sent else {}
    shown = sent.get("embed")
    expected = R.build_report_embed(settings, True, GID)
    note(shown is not None, "/제보 가 임베드를 보냄")
    if shown is not None:
        note(shown.title == expected.title and shown.description == expected.description,
             "제목·설명이 미리보기와 일치", shown.title)
        note(sent.get("ephemeral") is True, "ephemeral=True (프로젝트 규칙)")

    # 비활성 상태에서 일반 유저는 막히고, 관리자는 볼 수 있어야 합니다.
    await save_report_settings(GID, {"enabled": False})
    it = F.Inter(uid=8, admin=False)
    await cog.report_biome.callback(cog, it)
    await drain()
    body = (it.response.sent[0] if it.response.sent else {}).get("content") or ""
    note("비활성화" in body, "꺼져 있으면 일반 유저는 안내만", body[:30])

    it = F.Inter(uid=7, admin=True)
    await cog.report_biome.callback(cog, it)
    await drain()
    sent = it.response.sent[0] if it.response.sent else {}
    note(sent.get("embed") is not None, "관리자는 꺼져 있어도 확인 가능")
    await save_report_settings(GID, {"enabled": True})


async def main():
    test_links()
    test_color()
    test_embed()
    test_button()
    await test_dropdown()
    await test_reset_scope()
    await test_panel_refresh()
    await test_command_matches_preview()

asyncio.run(main())
print(f"\n  실패 {len(F.FAILS)}건 / 성공 {len(F.OKS)}건")
for x in F.FAILS:
    print("   -", x)
sys.exit(1 if F.FAILS else 0)
