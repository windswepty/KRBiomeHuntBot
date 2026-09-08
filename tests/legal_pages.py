"""로블록스 OAuth 심사가 요구하는 항목이 약관·방침에 실제로 있는가.

한 번 반려된 사유가 "Terms of service is missing information" 이었습니다.
그때 약관은 한국어 4문단(약 150단어)이었고, 심사자는 영어권이라
읽을 수조차 없었습니다.

여기서 지키는 것은 **내용의 존재**입니다. 페이지를 손보다 항목 하나를
지우면 다음 심사에서 또 반려되는데, 그 사실은 몇 주 뒤에야 알게 됩니다.
"""
import sys, os, tempfile, re

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
health.mark_ready(guilds=1, storage="mongo")
health.mark_connected()


def text(path):
    r = c.get(path)
    return r.status_code, r.get_data(as_text=True)


# 심사자는 영어권입니다. 한국어만 있으면 "정보 없음"으로 읽힙니다.
REQUIRED = {
    "/terms": [
        ("영문 제목", "Terms of Service"),
        ("최종 수정일", "Last updated"),
        ("동의 조항", "Acceptance"),
        ("서비스 설명", "Description of the service"),
        ("연령 제한", "13 years"),
        ("로블록스 규정 준수", "Community Standards"),
        ("연동 해제 방법", "revoke"),
        ("금지 행위", "Acceptable use"),
        ("이용 제한/종료", "Suspension and termination"),
        ("무료 고지", "free of charge"),
        ("지식재산권", "Intellectual property"),
        ("보증 부인", "as is"),
        ("책임 제한", "Limitation of liability"),
        ("약관 변경", "Changes to these Terms"),
        ("준거법", "Governing law"),
        ("연락처", "@"),
        ("로블록스 비제휴 고지", "not created by"),
        ("방침 링크", 'href="/privacy"'),
    ],
    "/privacy": [
        ("영문 제목", "Privacy Policy"),
        ("최종 수정일", "Last updated"),
        ("운영자 소개", "Who we are"),
        ("수집 항목", "Roblox user ID"),
        ("요청 권한 명시", "openid"),
        ("받지 않는 정보", "never receive"),
        ("이용 목적", "Why we use it"),
        ("보관 위치", "MongoDB Atlas"),
        ("보관 기간", "Retention"),
        ("제3자 미제공", "do not sell"),
        ("이용자 권리", "Your rights"),
        ("아동 보호", "Children"),
        ("변경 고지", "Changes"),
        ("연락처", "@"),
        ("로블록스 비제휴 고지", "not created by"),
        ("약관 링크", 'href="/terms"'),
    ],
}

for path, checks in REQUIRED.items():
    code, body = text(path)
    plain = re.sub(r"<[^>]+>", " ", body)
    words = len(plain.split())
    print(f"=== {path} — {code} · {words} 단어 · h2 {body.count('<h2>')}개 ===")
    note(code == 200, f"{path} 200")
    # 반려 당시 약관이 150단어였습니다. 그 수준으로 돌아가면 잡아냅니다.
    note(words >= 500, f"{path} 분량 충분", f"{words} 단어 (500 이상)")
    note(bool(re.search(r"[가-힣]", plain)), f"{path} 한국어 포함")
    note(len(re.findall(r"[A-Za-z]{4,}", plain)) >= 200,
         f"{path} 영문 포함 (심사자가 읽을 수 있어야)")
    for label, needle in checks:
        note(needle in body, f"{path} · {label}")
    print()

print("=== 두 문서가 서로를 가리키는가 (심사자가 오갈 수 있게) ===")
_, t = text("/terms")
_, p = text("/privacy")
note('href="/privacy"' in t and 'href="/terms"' in p, "상호 링크")
note('href="/"' in t and 'href="/"' in p, "홈으로 돌아가는 링크")

print()
print("=== 소개 페이지와 내용이 어긋나지 않는가 ===")
# 방침에 적은 수집 항목과 소개 페이지의 설명이 다르면 심사에서 걸립니다.
_, home = text("/")
for kw in ("openid", "profile"):
    note(kw in home and kw in p, f"권한 {kw} 이 홈·방침 양쪽에 동일하게 명시")

print()
print("=== 접근에 인증이 걸려 있으면 안 됩니다 (심사자는 비로그인) ===")
for path in ("/", "/privacy", "/terms"):
    code, _ = text(path)
    note(code == 200, f"{path} 비로그인 200", str(code))

print(f"\n  실패 {len(F.FAILS)}건 / 성공 {len(F.OKS)}건")
for x in F.FAILS:
    print("   -", x)
sys.exit(1 if F.FAILS else 0)
