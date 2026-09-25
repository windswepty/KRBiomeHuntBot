"""감시는 하는데 알림 규칙이 없는 구역을 찾아내는지.

**왜 있는가:** 운영 DB 를 손으로 들여다보다, 감시 카테고리 12곳 중 1곳이
어떤 알림 규칙에도 들어 있지 않은 것을 찾았습니다. 그 구역에서 희귀
바이옴이 뜨면 링크 보호는 정상이라 겉보기엔 멀쩡한데 **아무도 호출되지
않습니다.** 안 울리는 쪽 고장이라 제보도 들어오지 않습니다.

사람이 손으로 찾아야 하는 것은 **다음에는 아무도 안 찾습니다.**

여기서 지켜야 할 것:

  1. 규칙이 닿지 않는 감시 구역을 **빠짐없이** 찾는다.
  2. **멀쩡한 곳을 잡지 않는다.** 늘 켜진 경고는 없느니만 못합니다 —
     익숙해져서 진짜를 놓칩니다.
  3. 범위 없는 `@everyone` 규칙은 `match_notify_rule` 이 건너뛰므로
     **덮은 것으로 치면 안 된다.** 둘이 어긋나면 화면은 초록인데 알림은
     안 가는, 가장 나쁜 조합이 됩니다.
  4. 지워진 채널은 사각지대가 아니라 **설정에 남은 쓰레기**라 따로 센다.
     섞으면 고칠 수 없는 경고가 화면에 영원히 남습니다.
"""
import sys, os, tempfile, asyncio

PROJ = sys.argv[1]
sys.path.insert(0, PROJ)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(tempfile.mkdtemp())
os.environ["MONGO_URI"] = ""

import discord
import fakes as F
from fakes import note, GUILD, Channel


def ids(items):
    return [getattr(i, "id", i) for i in items]


async def main():
    import cogs.link_guard as LG

    # 카테고리 2개와 그 안의 채널, 그리고 카테고리 밖 채널 하나.
    cat_a = Channel(9001, "카테고리A", GUILD, kind="category")
    cat_b = Channel(9002, "카테고리B", GUILD, kind="category")
    ch_a = Channel(9101, "a채널", GUILD)
    ch_b = Channel(9102, "b채널", GUILD)
    lone = Channel(9103, "단독채널", GUILD)
    ch_a.category_id, ch_b.category_id, lone.category_id = 9001, 9002, None
    for c in (cat_a, cat_b, ch_a, ch_b, lone):
        if c not in GUILD.channels:
            GUILD.channels.append(c)
        # ⚠️ 가짜 길드는 `_chans` 를 **처음 한 번만** 만듭니다. 여기에
        #    넣지 않으면 `get_channel` 이 None 을 돌려줘, 사각지대가
        #    전부 '지워진 채널' 로 잡히면서 검사가 엉뚱한 곳을 봅니다.
        GUILD._chans[c.id] = c
    note(GUILD.get_channel(9002) is cat_b, "가짜 길드에 채널이 등록됨")

    rule = lambda **k: {"channel_ids": [], "category_ids": [], "biomes": [],
                        "mention": "roles", "role_ids": [1], **k}

    print("=== 1. 규칙이 닿지 않는 카테고리를 찾아야 ===")
    st = {"category_ids": [9001, 9002], "channel_ids": [],
          "notify_rules": [rule(category_ids=[9001])]}
    blind, missing = LG.notify_blind_spots(GUILD, st)
    note(ids(blind) == [9002], "규칙 없는 카테고리 1곳", str(ids(blind)))
    note(not missing, "지워진 것 없음")

    print("")
    print("  둘 다 덮여 있으면 **아무것도 경고하지 않아야** 합니다")
    st["notify_rules"] = [rule(category_ids=[9001, 9002])]
    blind, _ = LG.notify_blind_spots(GUILD, st)
    note(not blind, "멀쩡하면 조용함 (늘 켜진 경고 금지)", str(ids(blind)))

    print("")
    print("  규칙 여러 개가 나눠 덮어도 됩니다 (실제 설정이 이 형태입니다)")
    st["notify_rules"] = [rule(category_ids=[9001]), rule(category_ids=[9002])]
    blind, _ = LG.notify_blind_spots(GUILD, st)
    note(not blind, "두 규칙이 합쳐 전부 덮음", str(ids(blind)))

    print("")
    print("=== 2. 개별 채널 감시 ===")
    st = {"category_ids": [], "channel_ids": [9101, 9103],
          "notify_rules": [rule(channel_ids=[9101])]}
    blind, _ = LG.notify_blind_spots(GUILD, st)
    note(ids(blind) == [9103], "규칙 없는 채널 1곳", str(ids(blind)))

    print("")
    print("  채널이 규칙의 **카테고리 안**에 있으면 덮인 것입니다")
    st = {"category_ids": [], "channel_ids": [9101],
          "notify_rules": [rule(category_ids=[9001])]}
    blind, _ = LG.notify_blind_spots(GUILD, st)
    note(not blind, "채널의 카테고리로 덮임", str(ids(blind)))

    print("")
    print("  보호 제외 채널은 사각지대가 아닙니다 (원본을 그대로 두는 곳)")
    st = {"category_ids": [], "channel_ids": [9101, 9103],
          "no_guard_channel_ids": [9103],
          "notify_rules": [rule(channel_ids=[9101])]}
    blind, _ = LG.notify_blind_spots(GUILD, st)
    note(not blind, "보호 제외는 빼고 셈", str(ids(blind)))

    print("")
    print("=== 3. 범위 없는 규칙 ===")
    print("  범위가 없는 **역할 멘션** 규칙은 모든 감시 채널에 적용됩니다")
    st = {"category_ids": [9001, 9002], "channel_ids": [],
          "notify_rules": [rule()]}
    blind, _ = LG.notify_blind_spots(GUILD, st)
    note(not blind, "전 범위 규칙이 전부 덮음", str(ids(blind)))

    print("")
    print("  ⚠️ 범위 없는 `@everyone` 규칙은 **덮은 것으로 치면 안 됩니다**")
    # match_notify_rule 이 이런 규칙을 건너뛰므로, 여기서 덮었다고 하면
    # 화면은 초록인데 알림은 안 가는 상태가 됩니다.
    st = {"category_ids": [9001, 9002], "channel_ids": [],
          "notify_rules": [rule(mention="everyone", role_ids=[])]}
    blind, _ = LG.notify_blind_spots(GUILD, st)
    note(sorted(ids(blind)) == [9001, 9002],
         "**둘 다 사각지대로 잡힘** (matcher 와 같은 판정)", str(ids(blind)))

    print("")
    print("  실제 matcher 도 같은 판정인지 맞춰 봅니다")
    got, _ = LG.match_notify_rule(st, ch_a, "DREAMSPACE")
    note(got is None, "matcher 도 이 규칙을 건너뜀", str(got))

    print("")
    print("=== 4. 지워진 채널은 따로 셉니다 ===")
    st = {"category_ids": [9001, 999999], "channel_ids": [],
          "notify_rules": [rule(category_ids=[9001])]}
    blind, missing = LG.notify_blind_spots(GUILD, st)
    note(not blind, "사각지대로 세지 않음", str(ids(blind)))
    note(missing == [999999], "**지워진 것으로 따로 셈**", str(missing))

    print("")
    print("=== 5. 설정 화면에 실제로 뜨는지 ===")
    st = {"category_ids": [9001, 9002], "channel_ids": [],
          "notify_rules": [rule(category_ids=[9001])],
          "notify_exclude": ["Biome End"]}
    emb = LG.build_notify_embed(GUILD, st)
    blob = " ".join((f.name or "") + (f.value or "") for f in emb.fields)
    note("멘션을 붙이지 않는 감시 구역" in blob, "안내 칸이 뜸")
    note(cat_b.mention in blob, "어느 구역인지 짚어 줌")
    # ⚠️ **'아무도 호출되지 않는다'고 단정하면 안 됩니다.** 실측에서 이
    #    구역(바이옴 알림 카테고리)은 매크로가 본문에 역할을 적어 보내
    #    알림이 정상적으로 나가고 있었습니다. 단정하면 멀쩡한 설정을
    #    고치게 만들고, 그러면 되던 것까지 망가집니다.
    note("원본에 멘션이 있으면" in blob,
         "**단정하지 않고 조건을 밝힘** (거짓 경고 방지)")

    st["notify_rules"] = [rule(category_ids=[9001, 9002])]
    emb2 = LG.build_notify_embed(GUILD, st)
    blob2 = " ".join((f.name or "") + (f.value or "") for f in emb2.fields)
    note("멘션을 붙이지 않는 감시 구역" not in blob2, "멀쩡하면 칸이 아예 없음")

    print(f"\n  실패 {len(F.FAILS)}건 / 성공 {len(F.OKS)}건")
    for x in F.FAILS:
        print("   -", x)


asyncio.run(main())
