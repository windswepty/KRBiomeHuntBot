"""iOS · iPadOS 에서 [개인서버 입장하기] 가 **앱스토어로 가지 않는가** (`/j/<token>`).

실제 사고(2026-10-04): iOS 에서 누르면 로블록스 앱이 아니라 앱스토어가
열렸습니다. 302 · meta refresh 로 넘어간 roblox.com 은 유니버설 링크가 안
걸려 웹으로 열리고, 로블록스 웹은 iOS 에서 앱스토어로 보냅니다. 게다가 iPad
는 User-Agent 가 맥이라 모바일 판별을 통과해 302 를 그대로 받았습니다.

지켜야 할 것:
  1. iPhone · iPad(맥 UA 포함)는 `roblox://` 딥링크 페이지를 받는다
  2. 그 페이지에 https 로 자동으로 넘기는 meta refresh 가 없다
  3. 진짜 맥은 터치 화면이 아니라 웹 주소로 보낸다 (페이지 스크립트)
  4. 안드로이드 · PC 는 예전 그대로 (브릿지 · 302)
  5. 링크 코드가 이상하면 딥링크를 만들지 않는다 (HTML · JS 주입 방지)
"""
import sys, os, tempfile, asyncio

PROJ = sys.argv[1]
sys.path.insert(0, PROJ)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(tempfile.mkdtemp())
os.environ["MONGO_URI"] = ""
for k in ("DISCORD_CLIENT_ID", "DISCORD_CLIENT_SECRET", "SECRET_KEY"):
    os.environ.pop(k, None)          # 게이트를 끈 상태 — 이 스위트는 전달 방식만 봅니다

import fakes as F
from fakes import note

import utils.link_tokens as LT
import keep_alive as K

SHARE = "https://www.roblox.com/share?code=4f1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d&type=Server"
OLD = "https://www.roblox.com/games/15532962292/Sols-RNG?privateServerLinkCode=12345678901234567890"

IPHONE = ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_6 like Mac OS X) AppleWebKit/605.1.15 "
          "(KHTML, like Gecko) Version/18.6 Mobile/15E148 Safari/604.1")
IPAD = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Version/18.6 Safari/605.1.15")
ANDROID = ("Mozilla/5.0 (Linux; Android 14; SM-S918N) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/129.0 Mobile Safari/537.36")
PC = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/129.0 Safari/537.36")
MAC_CHROME = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/129.0 Safari/537.36")


def open_with(url, ua):
    LT.clear()
    t = LT.issue(url + "#hbh-abcdef", 7001, 1)
    return K.app.test_client().get(f"/j/{t}", headers={"User-Agent": ua},
                                   follow_redirects=False)


async def main():
    note(not K.oauth_gate.enabled(), "게이트가 꺼진 상태로 시작")

    print("=== 1. 딥링크 만들기 ===")
    note(K.roblox_app_link(SHARE) ==
         "roblox://navigation/share_links?code=4f1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d&type=Server",
         "공유 링크 → roblox://navigation/share_links", str(K.roblox_app_link(SHARE)))
    note(K.roblox_app_link(OLD) == "roblox://placeId=15532962292&linkCode=12345678901234567890",
         "옛 개인서버 링크 → roblox://placeId=…&linkCode=…", str(K.roblox_app_link(OLD)))
    note(K.roblox_app_link("https://roblox.com/share?type=Server&code=ab_C-1") ==
         "roblox://navigation/share_links?code=ab_C-1&type=Server", "www 없음 · 순서 바뀜")
    for bad in ["https://evil.com/share?code=abc&type=Server",
                "https://www.roblox.com.evil.com/share?code=abc&type=Server",
                'https://www.roblox.com/share?code=a"><script>&type=Server',
                "https://www.roblox.com/share?code=abc&type=Server%22%3E",
                "https://www.roblox.com/games/1/x",
                "javascript:alert(1)", ""]:
        note(K.roblox_app_link(bad) is None, f"만들지 않음: {bad[:50]}")

    print("\n=== 2. iPhone: 앱을 바로 여는 페이지 ===")
    r = open_with(SHARE, IPHONE)
    body = r.get_data(as_text=True)
    note(r.status_code == 200, "302 가 아니라 페이지", str(r.status_code))
    note("roblox://navigation/share_links?code=4f1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d" in body,
         "딥링크가 들어 있음")
    note("http-equiv=\"refresh\"" not in body, "**https 로 자동으로 넘기지 않음** (앱스토어 원인)")
    note("location.href=app" in body and "maxTouchPoints" not in body, "열자마자 앱으로")
    note(SHARE.replace("&", "&amp;") in body, "웹 주소는 보조 링크로만")
    note("#hbh-" not in body, "워터마크 프래그먼트는 떼어 냄")
    r = open_with(OLD, IPHONE)
    note("roblox://placeId=15532962292&amp;linkCode=12345678901234567890" in r.get_data(as_text=True),
         "옛 링크도 딥링크로")

    print("\n=== 3. iPad (맥으로 자칭) ===")
    r = open_with(SHARE, IPAD)
    body = r.get_data(as_text=True)
    note(r.status_code == 200, "**302 를 받지 않음** (제보된 그 경로)", str(r.status_code))
    note("maxTouchPoints" in body and "location.replace(web)" in body,
         "터치 화면이면 앱, 아니면(진짜 맥) 웹 주소로")
    note("http-equiv=\"refresh\"" not in body, "자동 https 이동 없음")

    print("\n=== 4. 다른 기기는 예전 그대로 ===")
    r = open_with(SHARE, ANDROID)
    body = r.get_data(as_text=True)
    note(r.status_code == 200 and "http-equiv=\"refresh\"" in body and "roblox://" not in body,
         "안드로이드: 예전 브릿지 페이지")
    r = open_with(SHARE, PC)
    note(r.status_code == 302 and r.headers.get("Location") == SHARE, "PC: 302", str(r.status_code))
    r = open_with(SHARE, MAC_CHROME)
    note(r.status_code == 302, "맥 크롬: 302 (iPad 일 수 없음)", str(r.status_code))
    ipad_chrome = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
                   "(KHTML, like Gecko) CriOS/129.0 Version/18.0 Safari/604.1")
    r = open_with(SHARE, ipad_chrome)
    body = r.get_data(as_text=True)
    note(r.status_code == 200 and "roblox://" in body and "maxTouchPoints" not in body,
         "iPad 크롬 데스크톱 모드(CriOS): 앱 딥링크", str(r.status_code))
    r = open_with("https://www.roblox.com/games/1/x?foo=1", IPHONE)
    note("roblox://" not in r.get_data(as_text=True), "모르는 형식이면 딥링크 없이 예전 방식")

    print("\n=== 5. HEAD 는 여전히 토큰을 안 태움 ===")
    LT.clear()
    t = LT.issue(SHARE, 7001, 1)
    c = K.app.test_client()
    h = c.head(f"/j/{t}", headers={"User-Agent": IPHONE})
    g = c.get(f"/j/{t}", headers={"User-Agent": IPHONE})
    note(h.status_code == 204 and g.status_code == 200, "미리보기 뒤에도 본인은 열림")


asyncio.run(main())
print(f"\n  실패 {len(F.FAILS)}건 / 성공 {len(F.OKS)}건")
for x in F.FAILS:
    print("   -", x)
sys.exit(1 if F.FAILS else 0)
