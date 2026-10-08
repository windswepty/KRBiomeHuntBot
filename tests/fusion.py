"""⚗️ 역할 합성 (/합성 · /합성설정, 2026-10-08 요청).

  · 역할 두 개 → 레시피에 맞으면 새 역할
  · **재료는 보이고 결과는 ???** — 누군가 처음 성공하면 그때 모두에게 공개
    ("합성에 필요한 역할들을 보여주고 나오는 역할은 공개하지 않는 것" +
     "합성에 성공한 사람이 나오면 그때 공개")
  · 맞지 않으면 **아무것도 잃지 않음**
  · 결과를 **먼저 주고** 재료를 뺌 — 거꾸로면 실패할 때 재료만 날아감
  · 연타 · 동시 클릭에도 한 번만
  · 레시피 추가 · 삭제는 한 칸만 건드림(관리자 둘이 동시에 고쳐도 안 지워짐)
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
from fakes import note, Member, Role, Inter

import utils.event_db as ev
import cogs.fusion as FU


def world(gid):
    g = F.Guild()
    g.id = gid
    roles = {n: Role(rid, n, pos) for n, rid, pos in
             (("불", 10, 5), ("물", 11, 6), ("증기", 12, 7), ("흙", 13, 8),
              ("히든", 14, 9), ("너무높음", 15, 100), ("바람", 16, 4))}
    for r in roles.values():
        g.roles.append(r)
        g._roles[r.id] = r
    return g, roles


def inter_for(g, member):
    it = Inter(uid=member.id, admin=member.guild_permissions.administrator)
    it.guild, it.user = g, member
    it.channel = g.channels[0]
    return it


async def scenario(label, gid):
    print(f"\n=== {label} ===")
    g, R = world(gid)
    ev._cache.clear()

    print("--- 레시피 저장 ---")
    note(await ev.add_fusion_recipe(gid, [R["불"].id, R["물"].id], R["증기"].id), "공개 레시피 불+물→증기")
    note(not await ev.add_fusion_recipe(gid, [R["물"].id, R["불"].id], R["흙"].id),
         "**같은 재료 조합(순서만 다름)은 거절** — 덮어쓰지 않음")
    note(await ev.add_fusion_recipe(gid, [R["증기"].id, R["흙"].id], R["히든"].id),
         "레시피 증기+흙→히든")
    note(await ev.add_fusion_recipe(gid, [R["바람"].id, R["흙"].id], R["너무높음"].id, consume=False),
         "봇보다 높은 결과 역할 레시피도 저장은 됨")
    s = await ev.get_fusion(gid, fresh=True)
    note(len(s["recipes"]) == 3, "레시피 3개", str(len(s["recipes"])))

    # 동시에 같은 조합 추가 → 하나만
    rs = await asyncio.gather(*[ev.add_fusion_recipe(gid, [20 + 0, 21], 22) for _ in range(5)])
    note(sum(rs) == 1, "**같은 조합을 5번 동시에 추가해도 하나만**", str(rs))
    note(await ev.remove_fusion_recipe(gid, ev.recipe_key(20, 21)), "레시피 하나 삭제")
    note(len((await ev.get_fusion(gid, fresh=True))["recipes"]) == 3, "다른 레시피는 그대로")

    print("--- 유저 화면: 재료는 보이고 결과는 ??? ---")
    m = Member(501, "합성러", False, roles=[R["불"], R["물"], R["흙"]], guild=g)
    g.members.append(m)
    e = FU.build_user_embed(g, m, await ev.get_fusion(gid, fresh=True))
    blob = " ".join(f.name + " " + f.value for f in e.fields)
    note(f"<@&{R['불'].id}> + <@&{R['물'].id}> → ❓ **???**" in blob and "✅" in blob,
         "**재료(불 + 물)는 보이고 결과는 ???** · 재료 있음 ✅")
    for n in ("증기", "히든", "너무높음"):
        note(f"<@&{R[n].id}>" not in blob.replace(f"<@&{R['증기'].id}> +", ""),
             f"아무도 안 만든 결과 `{n}` 은 안 보임")
    note("3개" in (e.footer.text or ""), "아직 미발견 레시피 개수 안내", e.footer.text)

    it = inter_for(g, m)
    await FU.Fusion.fuse.callback(FU.Fusion(None), it)
    sent = it.followup.sent[-1]
    view = sent.get("view")
    note(sent.get("ephemeral") and isinstance(view, FU.FuseView), "/합성: 본인에게만 · 합성 창")
    sel = next(c for c in view.children if isinstance(c, FU.FuseSelect))
    opts = {o.value for o in sel.options}
    note(opts == {str(R["불"].id), str(R["물"].id), str(R["흙"].id)},
         "고를 수 있는 것은 재료로 쓰이는 내 역할만", str(opts))
    note(view.go.disabled, "고르기 전엔 합성 버튼 잠김")

    print("--- 맞지 않는 조합 → 아무것도 안 잃음 ---")
    res = await FU.perform_fusion(g, m, R["불"].id, R["흙"].id)
    note(res["status"] == "no_recipe" and {r.id for r in m.roles} == {10, 11, 13},
         "**레시피 없으면 역할 그대로**")
    note((await FU.perform_fusion(g, m, R["불"].id, R["불"].id))["status"] == "same", "같은 역할 두 번은 거절")

    print("--- 공개 레시피 합성 (창으로) ---")
    sel._values = [str(R["불"].id), str(R["물"].id)]
    it2 = inter_for(g, m)
    await sel.callback(it2)
    note(not view.go.disabled and view.picked == [10, 11], "고르면 합성 버튼이 열림")
    it3 = inter_for(g, m)
    await view.go.callback(it3)
    ids = {r.id for r in m.roles}
    note(R["증기"].id in ids and R["불"].id not in ids and R["물"].id not in ids,
         "**증기를 얻고 불 · 물은 사라짐**", str(ids))
    note(m.added.index("증기") == 0 and m.removed[:2] == ["불", "물"],
         "**결과를 먼저 주고 재료를 뺌**", f"{m.added} / {m.removed}")
    note(it3.response.edited and it3.response.edited.get("view", "x") is None,
         "**성공하면 창의 버튼을 거둠** (다시 눌러 '상호작용 실패' 가 뜨지 않게)")
    e = it3.followup.sent[-1]["embed"]
    note("최초 발견" in e.title and it3.followup.sent[-1].get("ephemeral"),
         "처음 성공 → '최초 발견' 화면 (본인에게만)", e.title)
    s = await ev.get_fusion(gid, fresh=True)
    note(s["recipes"][ev.recipe_key(10, 11)].get("discovered_by") == str(m.id), "최초 발견자 기록")
    other = Member(509, "구경꾼", False, roles=[R["흙"]], guild=g)
    e = FU.build_user_embed(g, other, s)
    blob = " ".join(f.value for f in e.fields)
    note(f"→ ✨ <@&{R['증기'].id}> (최초 발견 <@{m.id}>)" in blob,
         "**성공한 사람이 나오면 결과가 모두에게 공개**")
    note(f"<@&{R['히든'].id}>" not in blob and "❓ **???**" in blob, "다른 레시피는 여전히 ???")

    print("--- 히든 레시피 발견 ---")
    res = await FU.perform_fusion(g, m, R["증기"].id, R["흙"].id)
    note(res["status"] == "ok" and res["first"] and R["히든"].id in {r.id for r in m.roles},
         "**증기 + 흙 → 히든 역할 (최초 발견)**")
    note(await FU.perform_fusion(g, m, R["증기"].id, R["흙"].id) == {"status": "missing"},
         "재료가 없으면 거절")

    print("--- 두 번째 성공은 최초가 아님 · 동시에 처음 성공해도 최초는 한 명 ---")
    m6 = Member(506, "두번째성공", False, roles=[R["불"], R["물"]], guild=g)
    res = await FU.perform_fusion(g, m6, 10, 11)
    note(res["status"] == "ok" and not res["first"], "이미 발견된 레시피는 최초가 아님")
    await ev.add_fusion_recipe(gid, [R["바람"].id, R["물"].id], R["흙"].id)
    racers = [Member(600 + i, f"경쟁{i}", False, roles=[R["바람"], R["물"]], guild=g) for i in range(6)]
    rs = await asyncio.gather(*[FU.perform_fusion(g, mm, R["바람"].id, R["물"].id) for mm in racers])
    note(all(r["status"] == "ok" for r in rs) and sum(1 for r in rs if r["first"]) == 1,
         "**6명이 동시에 처음 성공해도 최초 발견은 한 명**", str([r["first"] for r in rs]))

    print("--- 재료 3~5개 레시피 (2026-10-08: 최대 5개) ---")
    five = [R["불"].id, R["물"].id, R["흙"].id, R["바람"].id, R["증기"].id]
    note(await ev.add_fusion_recipe(gid, five, R["히든"].id), "재료 5개 레시피 저장")
    note(await ev.add_fusion_recipe(gid, five[:3], R["증기"].id), "재료 3개 레시피 저장")
    try:
        await ev.add_fusion_recipe(gid, five + [R["히든"].id], R["너무높음"].id)
        note(False, "재료 6개는 거절")
    except ValueError:
        note(True, "재료 6개는 거절")
    rec5 = (await ev.get_fusion(gid, fresh=True))["recipes"][ev.recipe_key(five)]
    note(sorted(rec5["inputs"]) == sorted(five), "다섯 재료가 그대로 저장됨")
    e = FU.build_user_embed(g, Member(700, "보기", False, roles=[], guild=g), await ev.get_fusion(gid, fresh=True))
    blob = " ".join(f.value for f in e.fields)
    note(" + ".join(f"<@&{i}>" for i in sorted(five)) in blob, "유저 화면에 다섯 재료가 다 보임")
    m5r = Member(701, "다섯", False, roles=[g.get_role(i) for i in five], guild=g)
    res = await FU.perform_fusion(g, m5r, five[:2] + [R["바람"].id])
    note(res["status"] == "no_recipe" and len(m5r.roles) == 5,
         "레시피와 다른 조합(3개)은 아무것도 안 잃음")
    res = await FU.perform_fusion(g, m5r, list(reversed(five)))
    ids = {r.id for r in m5r.roles}
    note(res["status"] == "ok" and R["히든"].id in ids and not (set(five) & ids),
         "**다섯 개를 순서 상관없이 고르면 합성 · 재료 다섯 개 모두 소모**", str(ids))
    m3r = Member(702, "셋", False, roles=[g.get_role(i) for i in five[:3]], guild=g)
    res = await FU.perform_fusion(g, m3r, five[:3])
    note(res["status"] == "ok" and R["증기"].id in {r.id for r in m3r.roles}, "재료 3개 레시피 합성")
    note((await FU.perform_fusion(g, m3r, [10] * 2))["status"] == "same", "같은 역할 중복은 거절")
    note((await FU.perform_fusion(g, m3r, five + [14]))["status"] == "count", "6개 고르면 거절")
    v = FU.FuseView(m5r.id, [discord.SelectOption(label=str(i), value=str(i)) for i in range(7)])
    sel5 = next(c for c in v.children if isinstance(c, FU.FuseSelect))
    note(sel5.min_values == 2 and sel5.max_values == 5, "고르기 칸: 최소 2 · 최대 5")
    b5 = FU.RecipeBuilder(FU.Fusion(None))
    ins_sel = next(c for c in b5.children if isinstance(c, FU.InputsSelect))
    note(ins_sel.max_values == 5 and len(ins_sel.placeholder) <= 12, "빌더 재료 칸: 최대 5 · 안내 문구가 짧음",
         ins_sel.placeholder)

    print("--- 이미 결과를 가진 사람 · 재료 유지 · 권한 ---")
    m2 = Member(502, "두번째", False, roles=[R["불"], R["물"], R["증기"]], guild=g)
    res = await FU.perform_fusion(g, m2, R["불"].id, R["물"].id)
    note(res["status"] == "already" and len(m2.roles) == 3, "**이미 있으면 재료를 안 뺌**")
    m3 = Member(503, "세번째", False, roles=[R["바람"], R["흙"]], guild=g)
    res = await FU.perform_fusion(g, m3, R["바람"].id, R["흙"].id)
    note(res["status"] == "cannot" and "봇 역할보다 위" in res["why"] and len(m3.roles) == 2,
         "봇보다 높은 역할이면 아무것도 안 바꾸고 이유를 알림", res.get("why", ""))

    print("--- 연타 / 동시 클릭 ---")
    m4 = Member(504, "연타", False, roles=[R["불"], R["물"]], guild=g)
    # 진짜 디스코드는 역할 주기가 HTTP 왕복입니다 — 그 사이 다른 클릭이 끼어듭니다.
    # 가짜가 즉시 끝나면 잠금을 빼도 이 검증이 통과해 버립니다(실제로 그랬습니다).
    real_add = m4.add_roles

    async def slow_add(*roles, **k):
        await asyncio.sleep(0.01)
        await real_add(*roles, **k)
    m4.add_roles = slow_add
    rs = await asyncio.gather(*[FU.perform_fusion(g, m4, R["불"].id, R["물"].id) for _ in range(10)])
    note(sum(1 for r in rs if r["status"] == "ok") == 1 and m4.added.count("증기") == 1,
         "**10번 동시에 눌러도 한 번만**", str([r["status"] for r in rs]))

    print("--- 공개 알림 · 끄기 ---")
    before_n = len((await ev.get_fusion(gid, fresh=True))["recipes"])
    await ev.save_fusion_settings(gid, {"announce": True, "recipes": {"x": 1}})
    s = await ev.get_fusion(gid, fresh=True)
    note(s["announce"] and len(s["recipes"]) == before_n and "x" not in s["recipes"],
         "설정 저장은 레시피를 건드리지 않음", f"{before_n} → {len(s['recipes'])}")
    m5 = Member(505, "알림", False, roles=[R["불"], R["물"]], guild=g)
    v = FU.FuseView(m5.id, [])
    v.picked = [10, 11]
    before = len(g.channels[0].sent)
    it5 = inter_for(g, m5)
    await v.go.callback(it5)
    sent = g.channels[0].sent[before:]
    note(len(sent) == 1 and "님이 합성으로" in str(sent[0]), "공개 알림이 켜져 있으면 채널에 한 줄")
    note("roles" not in str(sent[0].get("allowed_mentions", "")) or
         sent[0]["allowed_mentions"].roles is False, "알림은 역할을 울리지 않음")
    other = inter_for(g, m4)
    note(not await v.interaction_check(other), "남의 합성 창은 못 누름")
    await ev.save_fusion_settings(gid, {"enabled": False})
    note((await FU.perform_fusion(g, m4, 10, 11))["status"] == "off", "꺼지면 합성 안 됨")
    await ev.save_fusion_settings(gid, {"enabled": True})

    print("--- 관리자 화면 ---")
    admin = Member(7, "관리자", True, guild=g)
    it = inter_for(g, admin)
    cog = FU.Fusion(None)
    await FU.Fusion.fuse_settings.callback(cog, it)
    e = it.followup.sent[-1]["embed"]
    blob = " ".join(f.value for f in e.fields)
    note(f"<@&{R['히든'].id}>" in blob and f"<@&{R['너무높음'].id}>" in blob and "미발견" in blob,
         "관리자는 미발견 레시피의 결과까지 전부 봄")
    b = FU.RecipeBuilder(cog)
    b.inputs, b.result = [R["흙"].id, R["불"].id], R["물"].id
    await b.consume_btn.callback(inter_for(g, admin))
    note(not b.consume and "유지" in b.consume_btn.label, "재료 소모 토글")
    it = inter_for(g, admin)
    await b.save.callback(it)
    s = await ev.get_fusion(gid, fresh=True)
    rec = s["recipes"].get(ev.recipe_key(R["흙"].id, R["불"].id))
    note(rec is not None and rec["consume"] is False and rec["result"] == R["물"].id,
         "빌더로 레시피 저장 (재료 유지)")
    b2 = FU.RecipeBuilder(cog)
    b2.inputs, b2.result = [R["불"].id, R["물"].id], R["불"].id
    it = inter_for(g, admin)
    await b2.save.callback(it)
    note("재료와 같을 수 없습니다" in it.response.sent[-1]["content"], "결과가 재료면 거절")
    it = inter_for(g, Member(8, "유저", False, guild=g))
    await b2.save.callback(it)
    note("관리자만" in it.response.sent[-1]["content"], "관리자가 아니면 저장 못 함")
    rm = FU.RemoveRecipeSelect(g, s["recipes"])
    rm._values = [ev.recipe_key(R["흙"].id, R["불"].id)]
    await rm.callback(inter_for(g, admin))
    note(ev.recipe_key(R["흙"].id, R["불"].id) not in (await ev.get_fusion(gid, fresh=True))["recipes"],
         "삭제 메뉴로 지움")


async def main():
    await scenario("JSON 경로", 4242)
    ev.fusion_col = FM.FakeCollection("fusion_settings")
    ev._AFTER = FM.AFTER
    await scenario("Mongo 경로", 4343)


asyncio.run(main())
print(f"\n  실패 {len(F.FAILS)}건 / 성공 {len(F.OKS)}건")
for x in F.FAILS:
    print("   -", x)
sys.exit(1 if F.FAILS else 0)
