"""/health 가 좀비 상태를 실제로 503 으로 드러내는가.

무인 운영에서 외부 감시(UptimeRobot 등)가 유일한 눈입니다.
무조건 200 이면 Flask 만 살고 봇이 죽은 상태를 '정상'으로 봅니다.
코드를 읽는 것으로는 부족합니다 — 상태를 실제로 만들어 응답을 봅니다.
"""
import sys, os, tempfile, time
PROJ = sys.argv[1]
sys.path.insert(0, PROJ)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(tempfile.mkdtemp())
os.environ["MONGO_URI"] = ""
os.environ.setdefault("DISCORD_TOKEN", "x")

import fakes as F
from fakes import note
from utils import health
import keep_alive

c = keep_alive.app.test_client()
GRACE = health.STARTUP_GRACE_SECONDS


def hit():
    r = c.get("/health")
    return r.status_code, r.get_json(), r.headers


def age(seconds):
    """기동 시각을 과거로 밀어 유예를 지나게 합니다."""
    health._state["booted_at"] = time.monotonic() - seconds


print(f"=== 기동 유예 {GRACE}초 (재시작마다 오탐하면 안 됩니다) ===")
code, body, _ = hit()
note(code == 200, "유예 안: 아직 준비 전이어도 200", f"{code} {body['status']}")

age(GRACE + 5)
code, body, _ = hit()
note(code == 503, "유예 지났는데 로그인 못 함 → 503", f"{code} {body['status']}")
note(body["ready"] is False, "ready=false 로 원인이 드러남")

print("\n=== 정상 ===")
health.mark_ready(guilds=1, storage="mongo")
health.set_extensions(12, 12, [])
health.set_command_count(27)
code, body, _ = hit()
note(code == 200, "정상 → 200", f"{code} {body['status']}")
note(body["extensions"] == "12/12", "분모가 있는 기능 수", body["extensions"])
note(body["commands"] == 27, "명령어 수", str(body["commands"]))

print("\n=== 좀비: Flask 는 멀쩡한데 디스코드만 끊김 ===")
health.mark_disconnected()
age(GRACE + 5)
code, body, _ = hit()
note(code == 503, "끊김 → 503 (감시 서비스가 알림)", f"{code} {body['status']}")
note(body["connected"] is False, "connected=false")
note("unhealthy_seconds" in body, "얼마나 끊겼는지 함께")

print("\n=== 복구하면 다시 정상 ===")
health.mark_connected(latency_ms=160.0)
code, body, _ = hit()
note(code == 200, "복구 → 200", f"{code} {body['status']}")

print("\n=== 감시 서비스가 캐시를 보면 안 됩니다 ===")
_, _, h = hit()
note(h.get("Cache-Control") == "no-store", "Cache-Control: no-store",
     str(h.get("Cache-Control")))

print("\n=== 조용한 실패가 응답에 실리는가 ===")
health.add_note("동기화 지문 저장 실패")
code, body, _ = hit()
note("notes" in body and any("지문" in n for n in body["notes"]),
     "notes 로 드러남", str(body.get("notes")))
print("")
print("=== / 는 사람이 보는 소개 페이지, /health 는 기계용 ===")
# 로블록스 OAuth 심사자가 Entry Link(/)를 직접 눌러 봅니다.
# JSON 만 보이면 "앱 소개가 없다"로 반려될 수 있습니다.
health.mark_connected()
r = c.get("/")
note(r.status_code == 200 and r.mimetype == "text/html",
     "/ 는 HTML 소개 페이지", f"{r.status_code} {r.mimetype}")
page = r.get_data(as_text=True)
for kw in ("KR Biome Hunt Verification", "openid", "/privacy", "/terms"):
    note(kw in page, f"/ 에 {kw} 포함")
note(c.get("/health").mimetype == "application/json", "/health 는 JSON")

# 끊긴 상태에서도 / 는 200 이어야 합니다. 일시적 끊김 중에 심사자가
# 들어왔을 때 503 이면 "서비스가 고장났다"로 보여 반려될 수 있습니다.
health.mark_disconnected()
age(GRACE + 5)
note(c.get("/").status_code == 200, "끊겨도 / 는 200 (심사 오해 방지)")
note(c.get("/health").status_code == 503, "그래도 /health 는 503 (감시는 여기)")
health.mark_connected()
print("\n=== HEAD 로 링크 미리보기가 토큰을 태우면 안 됩니다 ===")
for path in ("/j/testtoken123", "/auth/callback", "/roblox/callback"):
    r = c.head(path)
    note(r.status_code == 204, f"{path} HEAD → 204 (토큰 미소비)",
         str(r.status_code))

print("")
print("=== GET 은 정상 경로를 타야 (HEAD 만 막은 것) ===")
r = c.get("/j/없는토큰")
body = r.get_data(as_text=True)
note(r.status_code in (200, 410) and "유효하지 않은" in body,
     "없는 토큰 GET → 안내 페이지", str(r.status_code))
note("로블록스" not in body and "http" not in body.split("<body")[-1][:400].lower(),
     "안내 페이지가 링크를 흘리지 않음")

print(f"\n  실패 {len(F.FAILS)}건 / 성공 {len(F.OKS)}건")
for x in F.FAILS:
    print("   -", x)
sys.exit(1 if F.FAILS else 0)
