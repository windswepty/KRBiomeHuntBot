"""모든 모달에 정상값 · 이상값을 넣어 실행합니다."""
import sys, os, tempfile, asyncio, datetime

PROJ = sys.argv[1]
sys.path.insert(0, PROJ)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(tempfile.mkdtemp())
os.environ["MONGO_URI"] = ""

import discord
import fakes as F

# report.py 는 isinstance(x, discord.CategoryChannel) 로 "채널이 아니라
# 카테고리"를 확인합니다. 진짜 검사이므로 지우면 안 되고, 대역 쪽에서
# 맞춰 줍니다. (ticket_guard.py 와 같은 이유)
class FakeCategory(F.Channel):
    pass


# 카테고리만 별도 클래스로 바꿔 둡니다. 전부 같은 클래스로 두면
# "채널을 카테고리로 잘못 지정" 을 잡아내는지 확인할 수 없습니다.
for _c in F.GUILD.categories:
    _c.__class__ = FakeCategory
discord.CategoryChannel = FakeCategory
from fakes import Inter, GUILD, note, drain


def accepted(it):
    """설정이 저장됐는지 (성공 임베드/패널갱신) vs 거부됐는지."""
    if it.response.edited:
        return True
    for s in it.response.sent:
        e = s.get("embed")
        if e is None:
            continue
        blob = (e.title or "") + " ".join(f.name or "" for f in (e.fields or []))
        if "✅" in blob or "완료" in blob:
            return True
    return False


async def fire(label, modal, values, expect):
    """모달 필드에 값을 채우고 on_submit 을 실행합니다."""
    it = Inter()
    try:
        for name, val in values.items():
            getattr(modal, name)._value = val
        await modal.on_submit(it)
        await drain()
        got = accepted(it)
        note(got == expect, f"{label:38} {'통과기대' if expect else '거부기대'}",
             ("저장됨" if got else "거부됨") + "  " + F.outcome(it))
    except Exception as e:
        import traceback
        note(False, f"{label:38}", f"{type(e).__name__}: {e}")
        traceback.print_exc(limit=3)


async def main():
    import utils.database as db
    import utils.verify_db as vdb
    import cogs.quest as Q, cogs.checkin as C, cogs.shop as S
    import cogs.gacha as G, cogs.report as R, cogs.verify as V
    import cogs.link_guard as LG, cogs.blacklist as BL

    today = datetime.datetime.now(db.KST).strftime("%Y-%m-%d")

    print("=== 퀘스트 ===")
    await fire("일일 목표치 정상", Q.DailyGoalModal(),
               dict(chat="25", channel_chat="12", gacha="3", early_bird="15"), True)
    await fire("일일 목표치 음수", Q.DailyGoalModal(),
               dict(chat="-5", channel_chat="12", gacha="3", early_bird="15"), False)
    await fire("일일 목표치 문자", Q.DailyGoalModal(),
               dict(chat="다섯", channel_chat="12", gacha="3", early_bird="15"), False)
    await fire("주간 목표치 정상", Q.WeeklyGoalModal(),
               dict(week_checkin="6", week_chat="200", week_gacha="12",
                    week_channel="80"), True)
    await fire("주간 출석 8일(불가)", Q.WeeklyGoalModal(),
               dict(week_checkin="8", week_chat="200", week_gacha="12",
                    week_channel="80"), False)
    await fire("보상·개수 정상", Q.CountRewardModal(),
               dict(daily="300", weekly="1000", count="3", wcount="2"), True)
    await fire("공동목표 정상", Q.ServerGoalModal(),
               dict(target="3000", reward="200", min_percent="1"), True)
    await fire("공동목표 비율 150%", Q.ServerGoalModal(),
               dict(target="3000", reward="200", min_percent="150"), False)

    print("\n=== 이벤트 ===")
    await fire("이벤트 생성(단계)", Q.EventBasicModal(),
               dict(name="추석", period=f"{today} ~ 2099-12-31", mode="단계"), True)
    await fire("이벤트 생성(모두)", Q.EventBasicModal(),
               dict(name="가을", period=f"{today} ~ 2099-12-31", mode="모두"), True)
    await fire("이벤트 날짜 오류", Q.EventBasicModal(),
               dict(name="X", period="2026-13-45 ~ 2026-12-31", mode="단계"), False)
    await fire("이벤트 끝<시작", Q.EventBasicModal(),
               dict(name="X", period="2026-12-31 ~ 2026-01-01", mode="단계"), False)
    await fire("이벤트 방식 오타", Q.EventBasicModal(),
               dict(name="X", period=f"{today} ~ 2099-12-31", mode="abc"), False)
    await fire("이벤트 이름 공백", Q.EventBasicModal(),
               dict(name="   ", period=f"{today} ~ 2099-12-31", mode="단계"), False)

    evs = await db.list_events(GUILD.id)
    note(len(evs) >= 2, "이벤트 2개가 각각 남아 있음",
         f"{len(evs)}개: {[e['name'] for e in evs]}")
    eid = evs[0]["id"]
    eid2 = evs[-1]["id"]

    await fire("단계 3개", Q.EventTierModal(eid),
               dict(kind="채팅", tiers="100:500\n300:1500\n1000:0:111122223333444455"), True)
    await fire("단계 없는 역할", Q.EventTierModal(eid),
               dict(kind="채팅", tiers="100:500:12345"), False)
    await fire("단계 조건 오타", Q.EventTierModal(eid),
               dict(kind="낚시", tiers="100:500"), False)
    await fire("단계 형식 오류", Q.EventTierModal(eid),
               dict(kind="채팅", tiers="abc"), False)
    await fire("모두달성 조건 3개", Q.EventObjectiveModal(eid2),
               dict(objectives="채팅:200\n뽑기:10\n출석:5"), True)
    await fire("모두달성 조건 오타", Q.EventObjectiveModal(eid2),
               dict(objectives="낚시:200"), False)
    await fire("보상 포인트+역할", Q.EventRewardModal(eid2),
               dict(points="2000", role_id="111122223333444455"), True)
    await fire("보상 없는 역할", Q.EventRewardModal(eid2),
               dict(points="2000", role_id="12345"), False)

    print("\n=== 출석 ===")
    await fire("출석 보상 정상", C.CheckinRewardModal(),
               dict(base="100", cycle="7", cycle_step="100", cycle_max="1000"), True)
    await fire("출석 주기 0 (사용 안 함)", C.CheckinRewardModal(),
               dict(base="100", cycle="0", cycle_step="100", cycle_max="1000"), True)
    await fire("마일스톤 정상", C.MilestoneModal(),
               dict(raw="7:200\n14:300\n21:400"), True)
    await fire("마일스톤 형식오류", C.MilestoneModal(),
               dict(raw="칠일:이백"), False)

    print("\n=== 상점 · 뽑기 ===")
    await fire("상점 제목", S.SetShopTitleModal(), dict(title_input="한바헌 상점"), True)
    await fire("상점 항목 추가", S.AddShopItemModal(),
               dict(item_name="아이템", item_price="500", item_stock="10",
                    role_id_input="0", item_desc="설명"), True)
    await fire("상점 가격 음수", S.AddShopItemModal(),
               dict(item_name="아이템", item_price="-1", item_stock="10",
                    role_id_input="0", item_desc="설명"), False)
    await fire("뽑기 가격", G.SetGachaPriceModal(), dict(price_input="200"), True)
    await fire("뽑기 가격 문자", G.SetGachaPriceModal(), dict(price_input="공짜"), False)
    await fire("뽑기 항목 추가", G.AddGachaItemModal(),
               dict(item_name="레어", percent_input="5", item_stock_input="3",
                    role_id_input="0", reward_points_input="0"), True)

    print("\n=== 제보 · 인증 · 링크보호 ===")
    # 모달이 "지금 값"을 채워 열도록 바뀌어 설정 dict 를 받습니다.
    await fire("제보 기본 포인트", R.DefaultPointsModal({}), dict(points_input="300"), True)
    await fire("제보 포인트 문자", R.DefaultPointsModal({}), dict(points_input="많이"), False)
    await fire("제보 포인트 0 이하", R.DefaultPointsModal({}), dict(points_input="0"), False)
    await fire("알림 역할", R.NotifyRoleModal({}),
               dict(role_input="111122223333444455"), True)
    await fire("알림 역할 멘션 붙여넣기", R.NotifyRoleModal({}),
               dict(role_input="<@&111122223333444455>"), True)
    await fire("알림 역할 없는 ID", R.NotifyRoleModal({}),
               dict(role_input="999999999999999999"), False)
    await fire("알림 역할 해제", R.NotifyRoleModal({}), dict(role_input="0"), True)
    await fire("제보 카테고리", R.ReportChannelModal({}),
               dict(channel_input="333344445555666677"), True)
    await fire("제보 카테고리 해제", R.ReportChannelModal({}),
               dict(channel_input="0"), True)
    await fire("제보 카테고리에 채널 ID", R.ReportChannelModal({}),
               dict(channel_input="222233334444555566"), False)

    print("")
    print("=== 제보 안내 임베드 꾸미기 ===")
    await fire("전부 채우기", R.EmbedDecorModal({}),
               dict(title_input="제목", desc_input="설명", color_input="파랑",
                    footer_input="푸터", button_input="열기"), True)
    await fire("색상 코드", R.EmbedDecorModal({}),
               dict(title_input="", desc_input="", color_input="#5865F2",
                    footer_input="", button_input=""), True)
    await fire("알 수 없는 색", R.EmbedDecorModal({}),
               dict(title_input="", desc_input="", color_input="무지개",
                    footer_input="", button_input=""), False)
    await fire("전부 비우기(기본값 복귀)", R.EmbedDecorModal({}),
               dict(title_input="", desc_input="", color_input="",
                    footer_input="", button_input=""), True)
    await fire("이미지 정상", R.EmbedImageModal({}),
               dict(image_input="https://x/a.png", thumb_input=""), True)
    await fire("이미지 주소 아님", R.EmbedImageModal({}),
               dict(image_input="내컴퓨터/a.png", thumb_input=""), False)
    await fire("이미지 비우기", R.EmbedImageModal({}),
               dict(image_input="", thumb_input=""), True)

    await fire("인증 역할", V.VerifyRoleModal(), dict(role_input="111122223333444455"), True)
    await fire("인증 역할 없음", V.VerifyRoleModal(), dict(role_input="12345"), False)
    await fire("필수역할 ID 추가", V.RequiredRoleIdModal(), dict(role_input="111122223333444455"), True)
    await fire("필수역할 없는 ID", V.RequiredRoleIdModal(), dict(role_input="12345"), False)
    await fire("블랙리스트 추가", BL.AddBlacklistModal(),
               dict(user_input="12345678901234567", reason_input="테스트"), True)
    await fire("블랙리스트 잘못된 ID", BL.AddBlacklistModal(),
               dict(user_input="abc", reason_input="테스트"), False)
    await fire("카테고리 ID", LG.CategoryIdModal(), dict(ids_input="333344445555666677"), True)
    await fire("카테고리 없는 ID", LG.CategoryIdModal(), dict(ids_input="99999"), False)
    # 이 모달은 isinstance(ch, discord.TextChannel) 를 요구합니다.
    # 대역 채널로는 통과할 수 없어 거부를 기대값으로 둡니다.
    await fire("로그채널 ID(대역 한계)", LG.LogChannelIdModal(),
               dict(channel_input="222233334444555566"), False)
    await fire("로그채널 없는 ID", LG.LogChannelIdModal(), dict(channel_input="99999"), False)
    await fire("워터마크 조회", LG.WatermarkLookupModal(), dict(target_input="ABCD"), False)

    print(f"\n  실패 {len(F.FAILS)}건 / 성공 {len(F.OKS)}건")
    for x in F.FAILS:
        print("   -", x)


asyncio.run(main())
