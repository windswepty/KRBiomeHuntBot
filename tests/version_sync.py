"""current_version() 이 .git 에서 읽는지 · 지문 저장 실패가 보이는지."""
import sys, os, tempfile, subprocess

PROJ = sys.argv[1]
sys.path.insert(0, PROJ)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["MONGO_URI"] = ""
for k in ("RENDER_GIT_COMMIT", "GIT_COMMIT", "SOURCE_VERSION"):
    os.environ.pop(k, None)

WORK = tempfile.mkdtemp()
os.chdir(WORK)

import fakes as F
from fakes import note
import utils.database as db

print("=== .git 이 없으면 빈 문자열 ===")
note(db.current_version() == "", "git 폴더 없음 → ''", repr(db.current_version()))

print("\n=== 진짜 git 저장소에서 읽는가 ===")
subprocess.run(["git", "init", "-q", "-b", "main"], cwd=WORK, check=True)
subprocess.run(["git", "config", "user.email", "t@t"], cwd=WORK, check=True)
subprocess.run(["git", "config", "user.name", "t"], cwd=WORK, check=True)
open(os.path.join(WORK, "a.txt"), "w").write("x")
subprocess.run(["git", "add", "-A"], cwd=WORK, check=True)
subprocess.run(["git", "commit", "-qm", "first"], cwd=WORK, check=True)
real = subprocess.run(["git", "rev-parse", "HEAD"], cwd=WORK,
                      capture_output=True, text=True).stdout.strip()[:12]
got = db.current_version()
note(got == real, "커밋 해시를 정확히 읽음", f"{got!r} == {real!r}")

print("\n=== 커밋하면 값이 바뀌는가 (재배포 구분의 근거) ===")
open(os.path.join(WORK, "a.txt"), "w").write("y")
subprocess.run(["git", "commit", "-aqm", "second"], cwd=WORK, check=True)
new = db.current_version()
note(new != got and len(new) == 12, "새 커밋 → 다른 해시", f"{got} → {new}")

print("\n=== packed-refs 만 있어도 읽는가 (git gc 후) ===")
subprocess.run(["git", "pack-refs", "--all"], cwd=WORK, check=True)
loose = os.path.join(WORK, ".git", "refs", "heads", "main")
note(not os.path.exists(loose), "느슨한 ref 가 사라짐")
note(db.current_version() == new, "packed-refs 에서 읽어냄",
     repr(db.current_version()))

print("\n=== 분리된 HEAD ===")
subprocess.run(["git", "checkout", "-q", "--detach", "HEAD"], cwd=WORK, check=True)
note(db.current_version() == new, "분리된 HEAD 도 읽음", repr(db.current_version()))

print("\n=== 지문 저장 실패가 눈에 보이는가 ===")
src = open(os.path.join(PROJ, "main.py"), encoding="utf-8").read()
i = src.index("# 성공했으면 지문 저장")
j = src.index("except discord.HTTPException", i)
block = src[i:j]
note("except Exception as e" in block, "실패를 잡아서 처리함")
note("health.add_note" in block, "상태 임베드에 경고가 남음")
note("print(" in block, "로그에도 남음")
note("except Exception:\n                pass" not in block,
     "조용히 삼키지 않음")

print(f"\n  실패 {len(F.FAILS)}건 / 성공 {len(F.OKS)}건")
for x in F.FAILS:
    print("   -", x)
