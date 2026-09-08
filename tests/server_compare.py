"""게임 안 플레이어 목록 대조 (`/개인서버대조`).

유출된 개인서버 주소에는 워터마크가 남지 않습니다 — 로블록스가 공유링크를
펼치면서 프래그먼트를 버리기 때문입니다. 그래서 **링크 쪽이 아니라 게임
쪽에서** 확인합니다: 받아 간 적이 없는데 들어와 있으면 누군가 넘긴 것입니다.

특히 조심할 것 두 가지입니다.
  · **게임 목록은 표시이름(displayName)을 보여줍니다.** 저장해 둔
    사용자명만 가지고 맞춰 보면 대부분 어긋납니다. 개명도 마찬가지입니다.
  · **'연동 명단에 없음'은 유출의 증거가 아닙니다.** 그냥 연동을 안 한
    사람일 수 있습니다. 화면이 그 사실을 반드시 말해야 합니다.
"""
import sys, os, tempfile, asyncio

PROJ = sys.argv[1]
sys.path.insert(0, PROJ)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(tempfile.mkdtemp())
os.environ["MONGO_URI"] = ""

import discord
import fakemongo as FM
import fakes as F
from fakes import GUILD, Inter, note, drain

VDB_COLS = ["verify_col", "linkguard_col", "plink_col", "clicklog_col",
            "watermark_col", "guardkey_col", "usage_col", "logref_col",
            "roblox_col"]


def fields_of(embed):
    return {f["name"]: f["value"] for f in embed.to_dict().get("fields", [])}


def find_field(embed, prefix):
    for name, value in fields_of(embed).items():
        if name.startswith(prefix):
            return name, value
    return None, None


async def main():
    import utils.verify_db as vdb
    FM.install(vdb, VDB_COLS)
    if hasattr(vdb, "_AFTER"):
        vdb._AFTER = FM.AFTER

    import cogs.roblox_link as RL

    # 로블록스 공개 API 는 부르지 않습니다 (대역으로 갈아끼웁니다).
    # **진짜 함수를 먼저 보관합니다** — 모듈 전역을 덮어쓰는 것이라,
    # 나중에 진짜를 시험하려면 되돌릴 방법이 있어야 합니다.
    ORIG_FETCH = RL.roblox_oauth.fetch_names
    fake_names = {}
    calls = []

    def fetch(ids):
        calls.append(list(ids))
        return dict(fake_names)
    RL.roblox_oauth.fetch_names = fetch

    print("=== 1. 이름 자르기 ===")
    note(RL.split_names("모노\nbbb, ccc; ddd") == ["모노", "bbb", "ccc", "ddd"],
         "줄바꿈·쉼표·세미콜론으로 자름", str(RL.split_names("모노\nbbb, ccc; ddd")))
    note(RL.split_names("@someone") == ["someone"], "@ 를 떼어냄")
    note(RL.split_names("Abc\nabc") == ["Abc"], "대소문자만 다른 중복은 하나로")
    note(RL.split_names("Cool Guy") == ["Cool Guy"],
         "공백으로는 자르지 않음 (표시이름에 공백이 들어갑니다)")
    note(RL.split_names("  \n , ; ") == [], "빈 입력은 빈 목록")
    note(RL.normalize_name(" @Mono ") == "mono", "대조용 정규화")

    print("")
    print("=== 2. 세 갈래로 나뉘어야 ===")
    # 연동 3명: 41(받아 감) · 42(받은 적 없음) · 43(개명함)
    await vdb.save_roblox_link(GUILD.id, 41, 1001, "받은사람", method="oauth")
    await vdb.save_roblox_link(GUILD.id, 42, 1002, "안받은사람", method="oauth")
    await vdb.save_roblox_link(GUILD.id, 43, 1003, "옛이름", method="oauth")
    # 41 번만 보호된 링크를 받아 갔습니다
    await vdb.record_link_usage(GUILD.id, 8001, 41, "받은사람",
                                issued_url="https://relay.test/j/x" * 4)

    embed, attach = await RL.compare_players(
        GUILD, "받은사람\n안받은사람\n모르는사람")
    # 명단은 최신순으로 나오므로 순서는 보지 않습니다
    note(calls and set(calls[0]) == {1001, 1002, 1003},
         "연동자의 로블록스 아이디로 현재 이름을 물어봄", str(calls[:1]))

    n1, v1 = find_field(embed, "✅")
    n2, v2 = find_field(embed, "⚠️ 연동했는데")
    n3, v3 = find_field(embed, "🚨")
    note("<@41>" in (v1 or ""), "받아 간 사람은 ✅", str(n1))
    note("<@42>" in (v2 or ""), "연동했는데 기록 없으면 ⚠️", str(n2))
    note("`모르는사람`" in (v3 or ""), "명단에 없으면 🚨", str(n3))
    note("<@41>" not in (v2 or "") + (v3 or ""), "받아 간 사람은 다른 칸에 없음")
    note(embed.color == discord.Color.orange(), "확인 필요하면 주황")

    print("")
    print("=== 3. 겁주지 않게 말해야 (오해가 사람을 잡습니다) ===")
    note("유출로 들어온 사람일 수도" in (v3 or "") and "연동을 안 한" in (v3 or ""),
         "'명단에 없음'이 곧 유출은 아님을 명시")
    note("직접 공유하는 채널" in (v2 or ""),
         "'받은 기록 없음'의 다른 가능성도 안내")
    note("연동 완료 3명" in (embed.footer.text or ""),
         "연동 인원을 알려줌 (적으면 🚨 가 많이 나옵니다)",
         str(embed.footer.text))
    note("유출의 증거가 아닙니다" in (embed.footer.text or ""),
         "바닥글에서 한 번 더 못 박음")

    print("")
    print("=== 4. 개명·표시이름 대응 ===")
    fake_names[1003] = {"name": "새이름", "display": "표시이름"}
    embed2, _ = await RL.compare_players(GUILD, "새이름")
    note(find_field(embed2, "⚠️ 연동했는데")[1] and
         "<@43>" in find_field(embed2, "⚠️ 연동했는데")[1],
         "개명한 현재 사용자명으로 찾아냄")
    embed3, _ = await RL.compare_players(GUILD, "표시이름")
    note(find_field(embed3, "⚠️ 연동했는데")[1] and
         "<@43>" in find_field(embed3, "⚠️ 연동했는데")[1],
         "표시이름으로도 찾아냄 (게임 목록이 보여주는 값)")
    embed4, _ = await RL.compare_players(GUILD, "옛이름")
    note(find_field(embed4, "⚠️ 연동했는데")[1] and
         "<@43>" in find_field(embed4, "⚠️ 연동했는데")[1],
         "저장된 옛 이름으로도 여전히 찾아냄")

    print("")
    print("=== 5. 로블록스 조회가 실패해도 멈추지 않아야 ===")
    def boom(ids):
        return {}
    RL.roblox_oauth.fetch_names = boom
    embed5, _ = await RL.compare_players(GUILD, "받은사람\n새이름")
    note(find_field(embed5, "✅")[1] and "<@41>" in find_field(embed5, "✅")[1],
         "저장된 이름으로는 계속 대조함")
    note(find_field(embed5, "⚠️ 로블록스 이름 조회 실패")[1] is not None,
         "실패했다는 사실을 화면에 알림")
    note("잘못 나올 수 있습니다" in (find_field(embed5, "⚠️ 로블록스 이름 조회 실패")[1] or ""),
         "결과를 얼마나 믿어야 하는지 알려줌")
    RL.roblox_oauth.fetch_names = fetch

    print("")
    print("=== 6. 아무 문제 없으면 초록 ===")
    embed6, _ = await RL.compare_players(GUILD, "받은사람")
    note(embed6.color == discord.Color.green(), "초록")
    note("없습니다" in (embed6.description or ""), "확인할 사람이 없다고 말함")
    note(find_field(embed6, "⚠️ 연동했는데")[1] is None, "⚠️ 칸이 아예 없음")

    print("")
    print("=== 7. 길면 파일로도 줘야 ===")
    many = "\n".join(f"사람{i}" for i in range(30))
    embed7, attach7 = await RL.compare_players(GUILD, many)
    note(attach7 is not None, "파일 첨부")
    note(attach7.filename == "개인서버대조.txt", "파일 이름", str(attach7.filename))
    body = attach7.fp.read().decode("utf-8")
    note("사람29" in body, "임베드에서 잘린 사람도 파일에는 있음")
    note("연동 명단에 없음" in body, "구분이 파일에도 표시됨")
    note(attach is None, "짧으면 파일을 만들지 않음")

    print("")
    print("=== 8. 집계 (get_usage_summary) ===")
    await vdb.record_link_usage(GUILD.id, 8002, 41, "받은사람",
                                issued_url="https://relay.test/j/y" * 4)
    summary = await vdb.get_usage_summary(GUILD.id)
    note(summary.get("41", {}).get("links") == 2, "서로 다른 링크 수를 셈",
         str(summary.get("41")))
    note(summary.get("41", {}).get("count") == 2, "총 클릭 수도 셈")
    note("42" not in summary, "받은 적 없는 사람은 집계에 없음")
    note(await vdb.get_usage_summary(GUILD.id + 999) == {},
         "다른 길드는 비어 있음")

    print("")
    print("=== 9. 모달·명령어 배선 ===")
    it = Inter()
    it.guild = GUILD
    modal = RL.ServerCompareModal()
    modal.players._value = "받은사람\n모르는사람"
    await modal.on_submit(it)
    await drain()
    sent = it.followup.sent[-1] if it.followup.sent else {}
    note(sent.get("embed") is not None, "결과를 보냄")
    note(sent.get("ephemeral") is True, "ephemeral=True")
    note(it.response.done is True, "먼저 defer 함 (이름 조회로 3초를 넘길 수 있음)")

    it2 = Inter()
    it2.guild = GUILD
    cog = RL.RobloxLink(type("B", (), {"user": None})())
    await cog.compare_server.callback(cog, it2)
    note(it2.response.modal is not None, "명령어가 모달을 띄움")

    print("")
    print("=== 10. 아무 이름도 못 읽었을 때 (공백만 붙여넣기) ===")
    # `required=True` 는 공백을 막지 못합니다 — 디스코드가 '입력됨'으로 봅니다.
    for label, text in [("공백만", "   " + chr(10) + "  " + chr(9)),
                        ("구분자만", " , ; " + chr(10) + " , ")]:
        e10, a10 = await RL.compare_players(GUILD, text)
        note(e10.color != discord.Color.green(),
             f"{label} → 초록 '문제 없음'으로 보이지 않음", str(e10.color))
        note("읽지 못했습니다" in (e10.description or ""),
             f"{label} → 왜 결과가 없는지 설명", (e10.description or "")[:30])
        note(a10 is None, f"{label} → 파일도 만들지 않음")

    print("")
    print("=== 11. 이름 조회가 '일부만' 실패해도 알려야 ===")
    # 전부 실패했을 때만 알리면, 절반이 낡은 이름으로 대조된 결과를
    # 관리자가 그대로 믿게 됩니다.
    fake_names.clear()
    fake_names[1001] = {"name": "n1", "display": "표시1"}
    e11, _ = await RL.compare_players(GUILD, "표시1")
    warn = find_field(e11, "⚠️ 이름을 못 받은")[1]
    note(warn is not None, "일부 실패를 알림",
         str([f.name for f in e11.fields]))
    note("저장된 이름으로만" in (warn or ""), "무엇으로 대조했는지 밝힘")
    note("다시 실행하면" in (warn or ""), "어떻게 하면 되는지도 알려줌")

    fake_names.clear()
    fake_names.update({1001: {"name": "a", "display": "b"},
                       1002: {"name": "c", "display": "d"},
                       1003: {"name": "e", "display": "f"}})
    e11b, _ = await RL.compare_players(GUILD, "b")
    note(find_field(e11b, "⚠️ 이름을 못 받은")[1] is None,
         "전원 조회되면 경고하지 않음")

    print("")
    print("=== 12. 진짜 이름 조회 (배치·캐시·실패) ===")
    from utils import roblox_oauth as RO
    RO.fetch_names = ORIG_FETCH          # 대역을 걷어내고 진짜를 시험합니다
    RO.clear()
    http = []

    def fake_request(url, data, headers, method):
        import json as _json
        body = _json.loads(data.decode())
        http.append(list(body["userIds"]))
        return {"data": [{"id": i, "name": f"n{i}", "displayName": f"d{i}"}
                         for i in body["userIds"]]}
    orig_request = RO._request
    RO._request = fake_request

    got = RO.fetch_names([1001, 1002, 1003])
    note(len(http) == 1 and len(got) == 3, "세 명을 한 번의 왕복으로",
         f"{len(http)}회 / {len(got)}명")
    note(got[1001] == {"name": "n1001", "display": "d1001"},
         "사용자명과 표시이름을 모두 담음", str(got.get(1001)))

    RO.fetch_names([1001, 1002, 1003])
    note(len(http) == 1, "두 번째 호출은 캐시 (왕복 없음)", f"{len(http)}회")
    RO.fetch_names([1001, 9999])
    note(http[-1] == [9999], "모르는 아이디만 새로 물어봄", str(http[-1]))

    http.clear()
    RO.clear()
    RO.fetch_names(list(range(2001, 2251)))
    note([len(c) for c in http] == [100, 100, 50],
         "250명은 100명씩 끊어서", str([len(c) for c in http]))

    RO.clear()
    def boom(url, data, headers, method):
        raise OSError("네트워크 끊김")
    RO._request = boom
    note(RO.fetch_names([1001]) == {}, "실패해도 예외를 밖으로 내지 않음")
    RO._request = fake_request
    note(len(RO.fetch_names([1001])) == 1,
         "실패는 캐시하지 않음 (다음에 다시 시도)")

    note(RO.fetch_names([]) == {}, "빈 목록은 조회하지 않음")
    note(RO.fetch_names([None, "x", -1, 0]) == {},
         "이상한 값은 걸러 냄 (예외 없이)")
    RO._request = orig_request
    RO.clear()

asyncio.run(main())
print("\n  실패 " + str(len(F.FAILS)) + "건 / 성공 " + str(len(F.OKS)) + "건")
for x in F.FAILS:
    print("   -", x)
sys.exit(1 if F.FAILS else 0)
