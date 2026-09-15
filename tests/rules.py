"""CLAUDE.md 가 '예외 없이 적용'이라고 못박은 규칙을 코드 전체에서 검사합니다.

규칙은 문서에만 있고 확인하는 장치가 없었습니다. 사람이 지키기로 한 규칙은
반드시 언젠가 깨지는데, 무인 운영에서는 그걸 몇 달 뒤에나 알게 됩니다.

여기서 보는 것 (전부 과거에 실제로 사고가 났던 항목입니다):
  ① 디스코드 응답은 전부 ephemeral=True
  ② pymongo 호출은 _db() 로 감싸기 (안 하면 이벤트 루프가 멈춥니다)
  ③ 조용한 실패 금지 (except: pass)
  ④ 영구 View 의 버튼은 고정 custom_id
  ⑤ 포인트를 절대값으로 덮어쓰지 않기 ($inc 만)
  ⑥ 슬래시 명령어는 전부 guild_only (DM 에서 guild 가 None)
  ⑦ 비밀이 담기는 파일이 .gitignore 에 잡히는가
"""
import sys, os, ast, io, pathlib

PROJ = pathlib.Path(sys.argv[1])
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fakes import note, FAILS, OKS

FILES = [p for p in PROJ.rglob("*.py")
         if "venv" not in p.parts and ".git" not in p.parts]


def rel(p):
    return str(p.relative_to(PROJ)).replace(os.sep, "/")


def parse(p):
    return ast.parse(io.open(p, encoding="utf-8").read()), io.open(p, encoding="utf-8").read().split("\n")


# ── ① ephemeral=True ───────────────────────────────────────────────
RESP_METHODS = {"send_message", "send"}

# 규칙을 어기는 것이 **맞는** 자리. 숨기지 않고 이유와 함께 적어 둡니다.
# 여기 없는 위반은 전부 실패로 잡힙니다.
EPHEMERAL_EXCEPTIONS = {
    "3초 후 이 티켓 채널을 완전히 삭제합니다":
        "티켓 채널이 곧 삭제되므로 제보자도 사라지는 이유를 봐야 합니다. "
        "채널째 없어지니 잔여물도 남지 않습니다.",
}


def _allowed(call) -> str:
    """이 호출이 기록된 예외인지. 맞으면 이유를 돌려줍니다."""
    for a in ast.walk(call):
        if isinstance(a, ast.Constant) and isinstance(a.value, str):
            for key, why in EPHEMERAL_EXCEPTIONS.items():
                if key in a.value:
                    return why
    return ""


def check_ephemeral():
    print("=== ① 디스코드 응답은 전부 ephemeral=True ===")
    bad = []
    allowed = []
    for p in FILES:
        if p.parts[-2] not in ("cogs",) and p.name != "main.py":
            continue
        tree, lines = parse(p)
        enclosing = {}
        for fn in ast.walk(tree):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for x in ast.walk(fn):
                    enclosing.setdefault(id(x), fn)
        for n in ast.walk(tree):
            if not isinstance(n, ast.Call) or not isinstance(n.func, ast.Attribute):
                continue
            if n.func.attr not in RESP_METHODS:
                continue
            src = ast.unparse(n.func)
            # 상호작용 응답만 대상입니다. channel.send / member.send 는
            # 애초에 ephemeral 이라는 개념이 없습니다.
            if not ("response.send_message" in src or "followup.send" in src):
                continue
            eph = next((k for k in n.keywords if k.arg == "ephemeral"), None)
            if eph is not None:
                ok = isinstance(eph.value, ast.Constant) and eph.value.value is True
                if not ok:
                    bad.append(f"{rel(p)}:{n.lineno}  {src}  (ephemeral 이 True 가 아님)")
                continue
            # send_message(**kwargs) 처럼 펼쳐 넣는 경우가 있습니다.
            # 그때는 그 함수 안에서 ephemeral 을 True 로 넣었는지 봅니다.
            if any(k.arg is None for k in n.keywords):
                owner = enclosing.get(id(n))
                # 문자열 비교로 찾으면 안 됩니다 — ast.unparse 는 작은따옴표로
                # 출력해서 큰따옴표로 검색하면 전부 놓칩니다. AST 로 봅니다.
                found = False
                if owner is not None:
                    for d in ast.walk(owner):
                        if not isinstance(d, ast.Dict):
                            continue
                        for k, v in zip(d.keys, d.values):
                            if (isinstance(k, ast.Constant) and k.value == "ephemeral"
                                    and isinstance(v, ast.Constant) and v.value is True):
                                found = True
                    for a in ast.walk(owner):
                        if (isinstance(a, ast.Subscript) and isinstance(a.slice, ast.Constant)
                                and a.slice.value == "ephemeral"):
                            found = True
                if found:
                    continue
            why = _allowed(n)
            if why:
                allowed.append(f"{rel(p)}:{n.lineno}  {why}")
                continue
            bad.append(f"{rel(p)}:{n.lineno}  {src}")
    note(not bad, f"위반 {len(bad)}건")
    for a in allowed:
        print("      (기록된 예외) " + a)
    for b in bad[:10]:
        print("     ", b)


# ── ② pymongo 는 _db() 로 ──────────────────────────────────────────
PYMONGO = {"find_one", "find", "update_one", "update_many", "insert_one",
           "insert_many", "delete_one", "delete_many", "count_documents",
           "find_one_and_update", "replace_one", "bulk_write",
           "aggregate", "estimated_document_count", "distinct"}


def check_db_wrap():
    print("")
    print("=== ② pymongo 호출은 _db() 로 감싸야 합니다 ===")
    print("    (동기 라이브러리라 그냥 부르면 이벤트 루프 전체가 멈춥니다)")
    bad = []
    for p in FILES:
        if p.name not in ("database.py", "verify_db.py"):
            continue
        tree, _ = parse(p)

        # _db(...) 안에 들어 있는 호출은 정상입니다
        # _db(...) 호출 **안에 있는 모든 것**을 감싸진 것으로 봅니다.
        # _db(col.find_one, q) 뿐 아니라 _db(lambda: list(col.find(q))) 도
        # 정상인데, 인자만 보면 후자를 놓쳐 오탐이 납니다.
        wrapped = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "_db":
                for x in ast.walk(n):
                    wrapped.add(id(x))

        for fn in ast.walk(tree):
            if not isinstance(fn, ast.AsyncFunctionDef):
                continue
            # 스레드로 넘길 내부 동기 함수는 여기서 제외됩니다
            inner = {id(x) for d in ast.walk(fn)
                     if isinstance(d, ast.FunctionDef) for x in ast.walk(d)}
            for n in ast.walk(fn):
                if id(n) in inner:
                    continue
                if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)):
                    continue
                if n.func.attr not in PYMONGO:
                    continue
                tgt = n.func.value
                name = getattr(tgt, "id", None) or getattr(tgt, "attr", None) or ""
                if not (name.endswith("_col") or name.endswith("_db")):
                    continue
                if id(n) in wrapped or id(n.func) in wrapped:
                    continue
                bad.append(f"{rel(p)}:{n.lineno}  {ast.unparse(n.func)}()  [{fn.name}]")
    note(not bad, f"감싸지 않은 호출 {len(bad)}건")
    for b in bad[:10]:
        print("     ", b)


# ── ③ 조용한 실패 ──────────────────────────────────────────────────
def check_silent():
    print("")
    print("=== ③ 조용한 실패 금지 (except: pass) ===")
    print("    (data/ 가 root 소유였던 사고를 9번의 기동 동안 아무도 몰랐던 이유)")
    bad = []
    for p in FILES:
        tree, _ = parse(p)
        for n in ast.walk(tree):
            if not isinstance(n, ast.ExceptHandler):
                continue
            if n.type is not None:          # 무엇을 잡는지 밝힌 것은 허용
                continue
            if len(n.body) == 1 and isinstance(n.body[0], ast.Pass):
                bad.append(f"{rel(p)}:{n.lineno}  bare except: pass")
    note(not bad, f"이유 없는 삼킴 {len(bad)}건")
    for b in bad[:10]:
        print("     ", b)


# ── ④ 영구 View 는 고정 custom_id ──────────────────────────────────
def check_custom_id():
    print("")
    print("=== ④ 영구 View(timeout=None) 의 버튼은 고정 custom_id ===")
    print("    (없으면 봇 재시작 후 버튼이 죽습니다)")
    bad = []
    for p in FILES:
        if p.parts[-2] != "cogs":
            continue
        tree, _ = parse(p)
        for cls in ast.walk(tree):
            if not isinstance(cls, ast.ClassDef):
                continue
            src = ast.unparse(cls)
            if "timeout=None" not in src:
                continue
            for fn in cls.body:
                if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for d in fn.decorator_list:
                    if not (isinstance(d, ast.Call) and "ui." in ast.unparse(d.func)):
                        continue
                    if "custom_id" not in {k.arg for k in d.keywords}:
                        bad.append(f"{rel(p)}:{fn.lineno}  {cls.name}.{fn.name}")
    note(not bad, f"custom_id 없는 영구 버튼 {len(bad)}건")
    for b in bad[:10]:
        print("     ", b)


# ── ⑤ 포인트 절대값 덮어쓰기 ───────────────────────────────────────
def check_points():
    print("")
    print("=== ⑤ 포인트는 $inc 로만 (절대값 덮어쓰기 금지) ===")
    print("    (두 명이 동시에 사면 포인트는 한 번만 빠지고 상품은 두 개 나갑니다)")
    bad = []
    for p in FILES:
        if p.name != "database.py":
            continue
        tree, _ = parse(p)
        # 바깥 함수 이름까지 봅니다. 복원은 restore_point_snapshot 안의
        # 중첩 함수 _apply 에서 실제 쓰기를 하므로, 안쪽 이름만 보면 놓칩니다.
        outer = {}
        for fn0 in ast.walk(tree):
            if isinstance(fn0, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for x in ast.walk(fn0):
                    outer.setdefault(id(x), []).append(fn0.name)
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            # 백업 복원은 의도된 덮어쓰기입니다 (CLAUDE.md 가 명시한 유일한 예외)
            if any("restore" in nm for nm in outer.get(id(fn), []) + [fn.name]):
                continue
            for n in ast.walk(fn):
                if not isinstance(n, ast.Dict):
                    continue
                keys = [k.value for k in n.keys
                        if isinstance(k, ast.Constant) and isinstance(k.value, str)]
                if "$set" not in keys:
                    continue
                idx = keys.index("$set") if "$set" in keys else -1
                val = n.values[[i for i, k in enumerate(n.keys)
                                if isinstance(k, ast.Constant) and k.value == "$set"][0]]
                if not isinstance(val, ast.Dict):
                    continue
                inner = [k.value for k in val.keys
                         if isinstance(k, ast.Constant) and isinstance(k.value, str)]
                if "points" in inner:
                    bad.append(f"{rel(p)}:{n.lineno}  $set points  [{fn.name}]")
    note(not bad, f"절대값 덮어쓰기 {len(bad)}건")
    for b in bad[:10]:
        print("     ", b)


# ── ⑥ 슬래시 명령어는 전부 guild_only ──────────────────────────────
def check_guild_only():
    print("")
    print("=== ⑥ 모든 슬래시 명령어는 guild_only ===")
    print("    (DM 에서는 interaction.guild 가 None 이라 그냥 죽습니다)")
    bad = []
    for p in FILES:
        if p.parts[-2] != "cogs":
            continue
        tree, _ = parse(p)
        for n in ast.walk(tree):
            if not isinstance(n, ast.AsyncFunctionDef):
                continue
            decs = [ast.unparse(d) for d in n.decorator_list]
            if not any("app_commands.command" in d for d in decs):
                continue
            if any("guild_only" in d for d in decs):
                continue
            name = n.name
            for d in n.decorator_list:
                if (isinstance(d, ast.Call)
                        and "app_commands.command" in ast.unparse(d.func)):
                    for kw in d.keywords:
                        if kw.arg == "name":
                            name = kw.value.value
            bad.append(f"{rel(p)}:{n.lineno}  /{name}")
    note(not bad, f"guild_only 없는 명령어 {len(bad)}건")
    for b in bad[:10]:
        print("     ", b)



def check_secrets_ignored():
    """비밀이 담기는 파일이 정말로 .gitignore 에 걸리는지 봅니다.

    `.env` 만 적어 두면 `.env.bak.20260903` 같은 백업본은 **그대로 커밋
    대상**입니다. 실제로 서버에 그런 파일(DISCORD_TOKEN·MONGO_URI 포함)이
    남아 있었습니다. 한 번 올라가면 되돌릴 수 없습니다.
    """
    text = io.open(PROJ / ".gitignore", encoding="utf-8").read()
    lines = {l.strip() for l in text.splitlines()}
    need = [".env", ".env.*", "data/", "backups/",
            "*.key", "*.pem", "*.ppk"]
    missing = [n for n in need if n not in lines]
    note(not missing, f".gitignore 에 비밀 파일 규칙이 있음 (빠짐 {len(missing)}건)",
         ", ".join(missing))
    # 예외로 남겨 두는 것 — 이게 빠지면 예제 파일이 사라집니다
    note("!.env.example" in lines,
         ".env.example 은 예외로 되살려 둠 (문서용이라 추적해야 합니다)")



def check_docs_budget():
    """CLAUDE.md 가 다시 부풀지 않는지 봅니다.

    이 파일은 **세션이 열릴 때마다 통째로 읽힙니다.** 65KB 까지 자랐을 때
    그만큼을 먼저 먹고 시작해서 대화가 일찍 잘렸고, 그래서 2026-09-11 에
    과거 사고 기록을 `docs/LESSONS.md` 로 옮겼습니다.

    **또 여기에 쌓으면 같은 일이 반복됩니다.** 새 지뢰는 LESSONS 에 적으세요.
    """
    budget = 52000
    claude = (PROJ / "CLAUDE.md").read_bytes()
    lessons = PROJ / "docs" / "LESSONS.md"
    note(len(claude) <= budget,
         f"CLAUDE.md 가 예산 안에 있음 ({len(claude):,} / {budget:,} B)",
         "넘으면 과거 기록을 docs/LESSONS.md 로 옮기세요")
    note(lessons.exists() and lessons.stat().st_size > 5000,
         "docs/LESSONS.md 가 있고 비어 있지 않음",
         f"{lessons.stat().st_size:,} B" if lessons.exists() else "없음")
    text = claude.decode("utf-8")
    note("docs/LESSONS.md" in text,
         "CLAUDE.md 가 LESSONS 를 가리킴 (안 그러면 아무도 안 읽습니다)")


check_ephemeral()
check_db_wrap()
check_silent()
check_custom_id()
check_points()
check_guild_only()
check_secrets_ignored()
check_docs_budget()

print(f"\n  실패 {len(FAILS)}건 / 성공 {len(OKS)}건")
for x in FAILS:
    print("   -", x)
sys.exit(1 if FAILS else 0)
