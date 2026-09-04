import os
import logging
from threading import Thread

from flask import Flask, redirect, request, Response, make_response

from utils import link_tokens
from utils.link_tokens import redeem, peek_owner
from utils import oauth_gate, roblox_oauth
from utils import health

app = Flask(__name__)

# Suppress flask default logs to keep console clean
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)


@app.route('/health')
def home():
    """생존 확인 — **디스코드 연결 상태까지** 반영합니다.

    예전에는 무조건 "alive" 를 돌려줬습니다. 그래서 Flask 만 살고 디스코드
    연결이 끊긴 **좀비 상태**를 외부 감시 서비스가 정상으로 판정했고,
    아무도 모르는 채 봇만 멈춰 있었습니다.

    이상하면 503 을 돌려주므로 UptimeRobot 같은 감시 서비스가 알림을 보냅니다.
    (그와 별개로 봇 스스로도 일정 시간 뒤 재시작합니다)
    """
    st = health.snapshot()
    ok = health.is_healthy()
    body = {
        "status": "ok" if ok else "unhealthy",
        "ready": st["ready"],
        "connected": st["connected"],
        "guilds": st["guilds"],
        "latency_ms": st["latency_ms"],
        "storage": st["storage"],
        "extensions": f"{st.get('ext_loaded', 0)}/{st.get('ext_total', 0)}",
        "commands": st.get("commands", 0),
        "uptime_seconds": st["uptime_seconds"],
        "disconnects": st["disconnects"],
    }
    if st["notes"]:
        body["notes"] = st["notes"]
    if not ok:
        body["unhealthy_seconds"] = round(health.unhealthy_seconds())

    resp = make_response(body, 200 if ok else 503)
    resp.headers["Cache-Control"] = "no-store"
    return resp




@app.route('/')
def landing():
    """앱 소개 페이지 — 사람이 보는 곳.

    예전에는 여기도 /health 와 같은 JSON 을 돌려줬습니다. 그런데 이 주소는
    **로블록스 OAuth 앱의 Entry Link** 라 심사자가 직접 눌러 봅니다.
    JSON 덩어리만 보이면 "앱 소개가 없다"고 판단할 수 있습니다.
    (실제로 옛 주소가 죽어 있어 한 번 반려된 적이 있습니다.)

    좀비 판정이 필요한 감시는 **/health 하나로** 유지합니다. 여기까지
    503 을 내면, 일시적인 끊김 중에 심사자가 들어왔을 때 "서비스가
    고장났다"로 보여 반려될 수 있습니다. 그래서 이 페이지는 항상 200 이고,
    상태는 본문에 글로 적어 사람이 읽게 합니다.
    """
    st = health.snapshot()
    ok = health.is_healthy()
    status_line = ("정상 작동 중" if ok else "일시적으로 점검 중입니다")
    return _doc("KR Biome Hunt Verification", """
<h1>KR Biome Hunt Verification</h1>
<p class="muted">한국 바이옴 헌트 · 디스코드 인증 봇</p>

<h2>What this service does</h2>
<p>This service links a Discord member to their Roblox account so that a
Korean Roblox community (Sol&#39;s RNG biome hunting, about 6,500 members)
can grant verified roles and keep private-server links from being leaked.
It is used only inside that one Discord server.</p>

<h2>무엇을 하는 서비스인가요</h2>
<p>디스코드 계정과 로블록스 계정을 연결해, <b>본인이 맞는지 확인</b>합니다.
확인된 사람에게만 인증 역할을 주고, 개인서버 링크가 밖으로 새지 않도록
1회용 주소로 전달합니다.</p>

<h2>받는 정보</h2>
<p>로블록스에서 <code>openid</code> · <code>profile</code> 권한으로 아래만 받습니다.</p>
<ul>
  <li>로블록스 사용자 ID</li>
  <li>사용자명 / 표시 이름</li>
  <li>계정 생성 일자</li>
</ul>
<p>비밀번호·이메일·결제 정보는 받지 않으며, 로블록스 계정에 어떤 변경도
하지 않습니다. 자세한 내용은 아래 문서를 봐 주세요.</p>

<h2>문서</h2>
<ul>
  <li><a href="/privacy">개인정보 처리방침 (Privacy Policy)</a></li>
  <li><a href="/terms">이용약관 (Terms of Service)</a></li>
</ul>

<h2>상태</h2>
<p>""" + status_line + """ · 서버 """ + str(st["guilds"]) + """개 · 명령어 """
        + str(st.get("commands", 0)) + """개</p>
<p class="muted">서비스 상태를 기계가 읽는 주소는 <code>/health</code> 입니다.</p>
""")
# ── 개인서버 링크 중계 ────────────────────────────────────────────────
#
# 봇이 인증을 통과한 유저에게 이 주소를 주고, 실제 로블록스 링크는
# 여기서 리다이렉트로만 넘겨줍니다. 링크가 디스코드에 노출되지 않고,
# 토큰은 1회용이라 퍼뜨려도 쓸 수 없습니다.
#
# 환경 변수(DISCORD_CLIENT_ID / SECRET)가 갖춰져 있으면 여기에 더해
# **발급받은 본인인지** 디스코드 신원으로 확인합니다(oauth_gate).
# 갖춰지지 않았으면 게이트는 꺼진 채 토큰만으로 통과합니다.

_MESSAGES = {
    "expired": ("링크가 만료되었습니다",
                "보안을 위해 짧은 시간만 유효합니다.<br>"
                "디스코드로 돌아가 버튼을 다시 눌러 주세요."),
    "exhausted": ("이미 사용된 링크입니다",
                  "이 링크는 한 번만 사용할 수 있습니다.<br>"
                  "디스코드로 돌아가 버튼을 다시 눌러 주세요."),
    "unknown": ("유효하지 않은 링크입니다",
                "주소가 잘못되었거나 이미 만료된 링크입니다.<br>"
                "디스코드로 돌아가 버튼을 다시 눌러 주세요."),
    "not_owner": ("본인에게 발급된 링크가 아닙니다",
                  "이 링크는 다른 분에게 발급된 것입니다.<br>"
                  "디스코드로 돌아가 <b>직접</b> 버튼을 눌러 주세요."),
    "not_member": ("입장 자격이 없습니다",
                   "서버를 나갔거나 필요한 역할이 없습니다.<br>"
                   "관리자에게 문의해 주세요."),
    "no_cookie": ("브라우저가 쿠키를 저장하지 않습니다",
                  "본인 확인 정보를 저장할 수 없어 입장할 수 없습니다.<br>"
                  "시크릿 모드를 끄거나 쿠키 차단을 해제한 뒤,<br>"
                  "디스코드로 돌아가 버튼을 다시 눌러 주세요."),
    "rbx_ok": ("로블록스 계정이 연동되었습니다",
               "디스코드로 돌아가셔도 됩니다.<br>"
               "이제 개인서버 이용 기록에 로블록스 아이디가 함께 남습니다."),
    "rbx_taken": ("이미 다른 사람이 연동한 계정입니다",
                  "이 로블록스 계정은 다른 디스코드 계정에 이미 연결돼 있습니다.<br>"
                  "본인 계정이 맞다면 관리자에게 문의해 주세요."),
    "rbx_expired": ("연동 요청이 만료되었습니다",
                    "디스코드에서 <b>/로블록스연동</b> 을 다시 실행해 주세요."),
    "rbx_failed": ("로블록스 연동에 실패했습니다",
                   "잠시 후 다시 시도해 주세요.<br>"
                   "문제가 계속되면 관리자에게 문의해 주세요."),
    "auth_failed": ("신원 확인에 실패했습니다",
                    "다시 시도해 주세요.<br>"
                    "문제가 계속되면 디스코드 버튼을 다시 눌러 주세요."),
}


def _page(reason: str, status: int = 410) -> Response:
    title, body = _MESSAGES.get(reason, _MESSAGES["unknown"])
    mark = "✅" if reason.endswith("_ok") else "🔒"
    html = f"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>
  body {{ margin:0; min-height:100vh; display:flex; align-items:center;
         justify-content:center; background:#0f1115; color:#e6e6e6;
         font-family:system-ui,-apple-system,"Segoe UI","Malgun Gothic",sans-serif; }}
  .card {{ max-width:420px; padding:40px 32px; text-align:center;
          background:#171a21; border:1px solid #262b36; border-radius:16px; }}
  h1 {{ font-size:20px; margin:0 0 12px; }}
  p {{ font-size:14px; line-height:1.7; color:#9aa3b2; margin:0; }}
  .mark {{ font-size:40px; margin-bottom:16px; }}
</style></head>
<body><div class="card">
  <div class="mark">{mark}</div>
  <h1>{title}</h1>
  <p>{body}</p>
</div></body></html>"""
    return Response(html, status=status, mimetype="text/html; charset=utf-8")


def _redirect_uri() -> str:
    base = (os.getenv("PUBLIC_BASE_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").strip()
    return f"{base.rstrip('/')}/auth/callback"


def _deliver(token: str) -> Response:
    """토큰을 실제로 소비해 로블록스로 넘깁니다."""
    url, reason = redeem(token)
    if url is None:
        return _page(reason)
    return redirect(url, code=302)


@app.route('/j/<token>')
def join(token: str):
    # HEAD 는 링크 미리보기·프리페치·백신 검사 등이 보냅니다.
    # Flask 는 GET 라우트에 HEAD 를 자동으로 붙이므로, 그대로 두면
    # **아무도 클릭하지 않았는데 1회용 토큰이 타버립니다.**
    # 사람이 실제로 연 것이 아니므로 토큰에 손대지 않고 돌려보냅니다.
    if request.method == "HEAD":
        return Response(status=204)

    # 게이트가 꺼져 있으면 지금까지처럼 토큰만으로 통과시킵니다.
    if not oauth_gate.enabled():
        return _deliver(token)

    owner = peek_owner(token)          # 토큰을 소비하지 않고 발급 대상만 확인
    if owner is None:
        # 이미 없는 토큰 — 소비 경로에서 정확한 사유를 만들어 줍니다
        return _deliver(token)

    guild_id, user_id = owner
    viewer = oauth_gate.read_user(request.cookies.get(oauth_gate.COOKIE_NAME))

    if viewer is None:
        # 신원 확인을 이미 마치고 돌아왔는데도 쿠키가 없다면, 브라우저가
        # 쿠키를 저장하지 못하는 상태입니다. 그대로 다시 보내면 디스코드와
        # 이 페이지 사이를 무한히 오갑니다. 한 번 왕복한 표시를 보고 멈춥니다.
        if request.args.get("v") == "1":
            print("[OAuthGate] 쿠키 저장 실패로 재진입 — 루프 차단", flush=True)
            return _page("no_cookie", status=403)

        # 아직 신원이 없음 → 디스코드로 보내 한 번만 확인받습니다
        state = oauth_gate.sign_state(token)
        if not state:
            return _deliver(token)

        # 디스코드 로그인·동의에 걸리는 시간 동안 토큰이 죽지 않도록
        # 왕복 예산만큼 수명을 늘립니다. 이게 없으면 신원 확인에 성공하고도
        # 돌아온 순간 "링크가 만료되었습니다"를 보게 됩니다.
        link_tokens.extend(token, oauth_gate.STATE_MAX_AGE)
        return redirect(oauth_gate.authorize_url(_redirect_uri(), state), code=302)

    if viewer != user_id:
        return _page("not_owner", status=403)

    allowed, _ = oauth_gate.check_member(guild_id, user_id)
    if not allowed:
        return _page("not_member", status=403)

    return _deliver(token)


@app.route('/auth/callback')
def auth_callback():
    # 인가 코드는 1회용이라 미리보기 요청이 태우면 안 됩니다.
    if request.method == "HEAD":
        return Response(status=204)

    # 실패 원인을 로그로 남깁니다. 안내 페이지는 같아도, 관리자는
    # 어디서 막혔는지 알아야 고칠 수 있습니다.
    if not oauth_gate.enabled():
        print("[OAuthGate] 콜백 실패: 게이트 비활성(환경 변수 누락)", flush=True)
        return _page("auth_failed")

    err = request.args.get("error")
    if err:
        print(f"[OAuthGate] 콜백 실패: 디스코드가 거부 - {err} "
              f"({request.args.get('error_description', '')})", flush=True)
        return _page("auth_failed")

    token = oauth_gate.read_state(request.args.get("state"))
    if not token:
        print("[OAuthGate] 콜백 실패: state 검증 실패(만료 또는 서명 불일치)", flush=True)
        return _page("auth_failed")

    code = request.args.get("code")
    if not code:
        print("[OAuthGate] 콜백 실패: code 파라미터 없음", flush=True)
        return _page("auth_failed")

    redirect_uri = _redirect_uri()
    user_id = oauth_gate.exchange_code(code, redirect_uri)
    if user_id is None:
        print(f"[OAuthGate] 콜백 실패: 코드 교환 불가 "
              f"(사용한 redirect_uri={redirect_uri})", flush=True)
        return _page("auth_failed")

    signed = oauth_gate.sign_user(user_id)
    if not signed:
        print("[OAuthGate] 콜백 실패: 쿠키 서명 불가(서명 키 없음)", flush=True)
        return _page("auth_failed")

    print(f"[OAuthGate] 신원 확인 성공: {user_id}", flush=True)

    # 신원을 쿠키에 심고, 원래 가려던 링크로 되돌립니다.
    # 다음부터는 이 단계 없이 즉시 통과합니다.
    # ?v=1 은 "신원 확인을 이미 거쳤다"는 표시입니다. 돌아갔는데도 쿠키가
    # 없으면 브라우저가 쿠키를 막고 있다는 뜻이라, 무한 왕복을 멈춥니다.
    resp = make_response(redirect(f"/j/{token}?v=1", code=302))
    resp.set_cookie(
        oauth_gate.COOKIE_NAME, signed,
        max_age=oauth_gate.COOKIE_MAX_AGE,
        httponly=True, secure=True, samesite="Lax",
    )
    return resp


# ── 로블록스 OAuth 심사용 정적 페이지 ────────────────────────────────
#
# 로블록스는 앱 심사 시 진입 링크 · 개인정보 처리방침 · 이용약관 URL 을
# 요구합니다. 별도 사이트를 두지 않고 봇 서버에서 함께 제공합니다.

_DOC_CSS = (
    "body{margin:0;padding:48px 20px;background:#0f1115;color:#e6e6e6;"
    "font-family:system-ui,-apple-system,'Segoe UI','Malgun Gothic',sans-serif;"
    "line-height:1.8}"
    ".wrap{max-width:720px;margin:0 auto}"
    "h1{font-size:24px;margin:0 0 8px}"
    "h2{font-size:16px;margin:28px 0 8px;color:#9db4ff}"
    "p,li{font-size:14px;color:#c3c9d4}"
    "code{background:#1b1f28;padding:2px 6px;border-radius:4px}"
    ".muted{color:#7c8494;font-size:13px}"
)


def _doc(title: str, body_html: str) -> Response:
    html = (
        "<!doctype html><html lang=\"ko\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        "<title>" + title + "</title><style>" + _DOC_CSS + "</style></head>"
        "<body><div class=\"wrap\">" + body_html + "</div></body></html>"
    )
    return Response(html, status=200, mimetype="text/html; charset=utf-8")


@app.route('/privacy')
def privacy():
    return _doc("Privacy Policy · KR Biome Hunt Verification", """
<h1>Privacy Policy</h1>
<p class="muted">KR Biome Hunt Verification (한바헌 봇)<br>
Last updated: 3 September 2026 · 최종 수정일: 2026년 9월 3일</p>
<p class="muted">This document is provided in English and Korean.
이 문서는 영어와 한국어로 제공됩니다.</p>

<h2>1. Who we are · 서비스 운영자</h2>
<p>KR Biome Hunt Verification is a verification tool operated by the
administrators of a single Korean Discord community for the Roblox
experience <i>Sol&#39;s RNG</i> (biome hunting). It is a non-commercial,
volunteer-run service. It is used only inside that one Discord server.</p>
<p>본 서비스는 로블록스 <i>Sol&#39;s RNG</i> 바이옴 헌팅을 주제로 한 한국
디스코드 커뮤니티의 운영진이 운영하는 인증 도구입니다. 영리 목적이 아니며,
해당 디스코드 서버 안에서만 사용됩니다.</p>

<h2>2. Information we collect · 수집하는 정보</h2>
<p>When you choose to link your Roblox account, we request the
<code>openid</code> and <code>profile</code> scopes and receive only:</p>
<ul>
  <li>your Roblox user ID</li>
  <li>your Roblox username and display name</li>
  <li>your Roblox account creation date</li>
</ul>
<p>From Discord we store your Discord user ID and the ID of the server you
verified in. We also record which protected links you opened, and when, so
that a leaked link can be traced.</p>
<p>로블록스 계정을 연동할 때 <code>openid</code> · <code>profile</code>
권한으로 위 세 가지만 받습니다. 디스코드 쪽에서는 사용자 ID 와 인증한 서버
ID 를 저장하며, 보호된 링크를 누가 언제 열었는지도 기록합니다(유출 추적용).</p>

<h2>3. What we never access · 받지 않는 정보</h2>
<p>We never receive or request your password, email address, Robux balance,
inventory, payment information, private messages, or friends list. We cannot
make any change to your Roblox account. We do not use cookies for tracking
or advertising.</p>
<p>비밀번호, 이메일, 로벅스, 인벤토리, 결제 정보, 개인 메시지, 친구 목록은
받지도 요청하지도 않습니다. 회원님의 로블록스 계정에 어떤 변경도 할 수
없습니다. 추적이나 광고 목적의 쿠키를 쓰지 않습니다.</p>

<h2>4. Why we use it · 이용 목적</h2>
<p>The information is used solely to (a) confirm that a Discord member owns
the Roblox account they claim, (b) grant a verified role in the community,
and (c) identify who a leaked private-server link was issued to. It is never
used for advertising, profiling, or any commercial purpose.</p>
<p>수집한 정보는 (가) 디스코드 이용자가 해당 로블록스 계정의 본인인지 확인,
(나) 커뮤니티 인증 역할 부여, (다) 유출된 개인서버 링크가 누구에게 발급된
것인지 확인, 이 세 가지에만 씁니다. 광고·프로파일링·상업적 목적으로
사용하지 않습니다.</p>

<h2>5. Where it is stored · 보관 위치</h2>
<p>Data is stored in a private MongoDB Atlas database and processed on a
virtual server provided by Oracle Cloud Infrastructure. Both are used purely
as infrastructure providers and receive no separate copy of your data for
their own purposes. Traffic to our service is encrypted with HTTPS.</p>
<p>데이터는 비공개 MongoDB Atlas 데이터베이스에 저장되며, Oracle Cloud
Infrastructure 가 제공하는 가상 서버에서 처리됩니다. 두 곳은 인프라
제공자로만 쓰이며 자체 목적으로 데이터를 별도 보유하지 않습니다. 서비스
통신은 HTTPS 로 암호화됩니다.</p>

<h2>6. Retention · 보관 기간</h2>
<p>Account link records are kept while your link is active. Link-access logs
are automatically deleted after 30 days. If you unlink your account, or leave
the community, your link record is deleted on request.</p>
<p>연동 기록은 연동이 유지되는 동안 보관합니다. 링크 사용 기록은 30일 뒤
자동 삭제됩니다. 연동을 해제하거나 커뮤니티를 떠나시면, 요청 시 연동 기록을
삭제합니다.</p>

<h2>7. Sharing · 제3자 제공</h2>
<p>We do not sell, rent, or share your information with any third party. We
may disclose information only if required by applicable law.</p>
<p>수집한 정보를 판매·대여하거나 제3자에게 제공하지 않습니다. 법령에 따라
요구되는 경우에만 예외적으로 제공될 수 있습니다.</p>

<h2>8. Your rights · 이용자의 권리</h2>
<p>You may at any time ask us what we hold about you, ask us to correct it,
or ask us to delete it. To unlink and delete your record, use the
<code>/연동해제</code> command in the Discord server or contact an
administrator. You may also revoke this application&#39;s access from your
Roblox account settings at any time. Requests are handled without charge.</p>
<p>보유 정보의 확인·정정·삭제를 언제든 요청하실 수 있습니다. 디스코드
서버에서 <code>/연동해제</code> 를 쓰시거나 운영진에게 요청하시면 됩니다.
로블록스 계정 설정에서 이 앱의 접근 권한을 직접 취소하실 수도 있습니다.
모든 요청은 무료로 처리됩니다.</p>

<h2>9. Children · 아동의 개인정보</h2>
<p>Discord requires users to be at least 13 years old, and this service is
available only inside a Discord server. We do not knowingly collect
information from anyone under 13. If we learn that we have, we delete it.</p>
<p>디스코드는 만 13세 이상만 이용할 수 있으며, 본 서비스는 디스코드 서버
안에서만 제공됩니다. 만 13세 미만의 정보를 의도적으로 수집하지 않으며,
수집된 사실을 알게 되면 즉시 삭제합니다.</p>

<h2>10. Changes · 변경</h2>
<p>If this policy changes, the updated version is published on this page with
a new &quot;last updated&quot; date.</p>
<p>본 방침이 변경되면 이 페이지에 수정일과 함께 게시합니다.</p>

<h2>11. Contact · 문의</h2>
<p>Email: <code>windswept1325@gmail.com</code><br>
You may also contact an administrator directly in the community Discord
server.</p>
<p>이메일: <code>windswept1325@gmail.com</code><br>
커뮤니티 디스코드 서버의 운영진에게 직접 문의하셔도 됩니다.</p>

<h2>12. Not affiliated with Roblox · 비제휴 고지</h2>
<p>This service is independent and community-operated. It is not created by,
affiliated with, sponsored by, or endorsed by Roblox Corporation or Discord
Inc. &quot;Roblox&quot; is a trademark of Roblox Corporation.</p>
<p>본 서비스는 커뮤니티가 독립적으로 운영합니다. Roblox Corporation 또는
Discord Inc. 와 제휴·후원 관계가 없으며 승인받은 서비스가 아닙니다.</p>

<p class="muted"><a href="/">← Home</a> · <a href="/terms">Terms of Service</a></p>
""")


@app.route('/terms')
def terms():
    return _doc("Terms of Service · KR Biome Hunt Verification", """
<h1>Terms of Service</h1>
<p class="muted">KR Biome Hunt Verification (한바헌 봇)<br>
Last updated: 3 September 2026 · 최종 수정일: 2026년 9월 3일</p>
<p class="muted">This document is provided in English and Korean.
이 문서는 영어와 한국어로 제공됩니다.</p>

<h2>1. Acceptance of these Terms · 약관의 동의</h2>
<p>By linking your Roblox account to this service, or by using any feature it
provides, you agree to these Terms of Service and to our
<a href="/privacy">Privacy Policy</a>. If you do not agree, do not use the
service; you may simply choose not to link your account.</p>
<p>로블록스 계정을 연동하거나 본 서비스의 기능을 이용하시면 이 약관과
<a href="/privacy">개인정보 처리방침</a>에 동의하신 것으로 봅니다. 동의하지
않으시면 이용하지 않으시면 되며, 연동하지 않으셔도 불이익은 없습니다.</p>

<h2>2. Description of the service · 서비스 내용</h2>
<p>KR Biome Hunt Verification is a Discord bot operated by the administrators
of a single Korean community for the Roblox experience <i>Sol&#39;s RNG</i>.
It lets a member link their Roblox account so the community can (a) grant a
verified role and (b) hand out private-server links as one-time addresses so
that those links are not leaked publicly. Use of the service is entirely
voluntary and free of charge.</p>
<p>본 서비스는 로블록스 <i>Sol&#39;s RNG</i> 를 주제로 한 한국 디스코드
커뮤니티의 인증 봇입니다. 로블록스 계정을 연동하면 (가) 인증 역할을 받고,
(나) 개인서버 링크를 1회용 주소로 안전하게 받을 수 있습니다. 이용은 전적으로
자발적이며 무료입니다.</p>

<h2>3. Eligibility · 이용 자격</h2>
<p>You must be at least 13 years old, as required by Discord&#39;s Terms of
Service, and you must comply with the Roblox Terms of Use and Roblox
Community Standards. You must be a member of the community Discord server.</p>
<p>디스코드 이용약관에 따라 만 13세 이상이어야 하며, 로블록스 이용약관 및
커뮤니티 규정(Community Standards)을 준수하셔야 합니다. 커뮤니티 디스코드
서버의 구성원이어야 이용하실 수 있습니다.</p>

<h2>4. Account linking and revocation · 연동과 해제</h2>
<p>You may link only a Roblox account that you personally own. Linking uses
Roblox OAuth 2.0; we never see or ask for your Roblox password. You may
unlink at any time using the <code>/연동해제</code> command in Discord or by
asking an administrator, and you may revoke this application&#39;s access
from your Roblox account settings.</p>
<p>본인 소유의 로블록스 계정만 연동하셔야 합니다. 연동은 로블록스 OAuth 2.0
으로 이루어지며, 저희는 비밀번호를 보지도 요구하지도 않습니다. 디스코드에서
<code>/연동해제</code> 명령이나 운영진 요청으로 언제든 해제하실 수 있고,
로블록스 계정 설정에서 직접 권한을 취소하실 수도 있습니다.</p>

<h2>5. Acceptable use · 이용자의 의무</h2>
<p>You agree not to:</p>
<ul>
  <li>link an account you do not own, or impersonate another person;</li>
  <li>redistribute, resell, or publicly post private-server links you receive;</li>
  <li>attempt to bypass verification, watermarking, or access controls;</li>
  <li>use automated tools to abuse the service or overload it;</li>
  <li>use the service for anything unlawful, or in breach of the Roblox
      Terms of Use or Roblox Community Standards.</li>
</ul>
<p>다음 행위를 하지 않으셔야 합니다: 본인 소유가 아닌 계정 연동 또는 타인
사칭, 전달받은 개인서버 링크의 재배포·판매·공개 게시, 인증·워터마크·접근
제어의 우회 시도, 자동화 도구를 이용한 남용이나 과부하 유발, 위법하거나
로블록스 이용약관·커뮤니티 규정에 반하는 이용.</p>

<h2>6. Suspension and termination · 이용 제한 및 종료</h2>
<p>We may suspend or terminate your access, remove your verified role, or
delete your link record if you breach these Terms, if required to protect the
community, or if the service is discontinued. Where practical we will explain
why. You may stop using the service at any time by unlinking.</p>
<p>약관을 위반하시거나 커뮤니티 보호가 필요한 경우, 또는 서비스가 종료되는
경우 이용을 제한하거나 인증 역할 회수·연동 기록 삭제를 할 수 있습니다.
가능한 범위에서 사유를 안내합니다. 이용자는 연동 해제로 언제든 이용을
중단하실 수 있습니다.</p>

<h2>7. No fees · 요금</h2>
<p>The service is provided free of charge. We do not sell anything, accept
payments, or handle Robux or any virtual currency.</p>
<p>본 서비스는 무료입니다. 어떤 판매나 결제도 하지 않으며, 로벅스를 포함한
가상 화폐를 취급하지 않습니다.</p>

<h2>8. Intellectual property · 지식재산권</h2>
<p>Roblox, the Roblox logo, and <i>Sol&#39;s RNG</i> are the property of their
respective owners. This service claims no ownership of them and uses them only
to describe what it does.</p>
<p>Roblox, 로블록스 로고, <i>Sol&#39;s RNG</i> 은 각 권리자의 자산입니다. 본
서비스는 이에 대한 어떠한 권리도 주장하지 않으며, 서비스 설명 목적으로만
언급합니다.</p>

<h2>9. Disclaimer of warranties · 보증의 부인</h2>
<p>The service is provided &quot;as is&quot; and &quot;as available&quot;,
without warranties of any kind. We do not guarantee that it will be
uninterrupted, error-free, or available at any particular time. It is operated
by volunteers on free infrastructure and may be changed or discontinued at any
time.</p>
<p>본 서비스는 &quot;있는 그대로&quot; 제공되며 어떠한 보증도 하지 않습니다.
중단 없는 운영이나 무결성을 보장하지 않습니다. 자원봉사 운영진이 무료
인프라 위에서 운영하므로 언제든 변경되거나 종료될 수 있습니다.</p>

<h2>10. Limitation of liability · 책임의 제한</h2>
<p>To the fullest extent permitted by law, we are not liable for any indirect,
incidental, or consequential loss arising from use of the service, including
loss of access, loss of in-game items, or changes made by Roblox or Discord to
their own platforms or policies.</p>
<p>법이 허용하는 최대 범위에서, 서비스 이용으로 발생한 간접·부수적·결과적
손해에 대해 책임지지 않습니다. 여기에는 접근 불가, 게임 내 아이템 손실,
로블록스나 디스코드의 플랫폼·정책 변경으로 인한 문제가 포함됩니다.</p>

<h2>11. Changes to these Terms · 약관의 변경</h2>
<p>If these Terms change, the updated version is published on this page with a
new &quot;last updated&quot; date. Continuing to use the service after a change
means you accept the updated Terms.</p>
<p>약관이 변경되면 이 페이지에 수정일과 함께 게시합니다. 변경 이후에도 계속
이용하시면 변경된 약관에 동의하신 것으로 봅니다.</p>

<h2>12. Governing law · 준거법</h2>
<p>These Terms are governed by the laws of the Republic of Korea, without
regard to conflict-of-law rules.</p>
<p>본 약관은 대한민국 법률에 따릅니다.</p>

<h2>13. Contact · 문의</h2>
<p>Email: <code>windswept1325@gmail.com</code><br>
You may also contact an administrator directly in the community Discord
server.</p>
<p>이메일: <code>windswept1325@gmail.com</code><br>
커뮤니티 디스코드 서버의 운영진에게 직접 문의하셔도 됩니다.</p>

<h2>14. Not affiliated with Roblox · 비제휴 고지</h2>
<p>This service is independent and community-operated. It is not created by,
affiliated with, sponsored by, or endorsed by Roblox Corporation or Discord
Inc. &quot;Roblox&quot; is a trademark of Roblox Corporation.</p>
<p>본 서비스는 커뮤니티가 독립적으로 운영합니다. Roblox Corporation 또는
Discord Inc. 와 제휴·후원 관계가 없으며 승인받은 서비스가 아닙니다.</p>

<p class="muted"><a href="/">← Home</a> · <a href="/privacy">Privacy Policy</a></p>
""")


@app.route('/roblox/callback')
def roblox_callback():
    # 미리보기 요청이 1회용 state 를 태우지 않게 합니다.
    if request.method == "HEAD":
        return Response(status=204)

    if not roblox_oauth.enabled():
        print("[RobloxOAuth] 콜백 실패: 환경 변수 누락", flush=True)
        return _page("rbx_failed")

    err = request.args.get("error")
    if err:
        print(f"[RobloxOAuth] 콜백 실패: 로블록스가 거부 - {err} "
              f"({request.args.get('error_description', '')})", flush=True)
        return _page("rbx_failed")

    pending = roblox_oauth.take_pending(request.args.get("state") or "")
    if pending is None:
        print("[RobloxOAuth] 콜백 실패: state 없음 또는 만료", flush=True)
        return _page("rbx_expired")

    code = request.args.get("code")
    if not code:
        print("[RobloxOAuth] 콜백 실패: code 파라미터 없음", flush=True)
        return _page("rbx_failed")

    info = roblox_oauth.exchange(code, pending["verifier"])
    if info is None:
        return _page("rbx_failed")

    ok, reason = roblox_oauth.save_link(
        pending["guild_id"], pending["user_id"], info)
    if not ok:
        if reason == "taken":
            return _page("rbx_taken", status=409)
        print(f"[RobloxOAuth] 저장 거부: {reason}", flush=True)
        return _page("rbx_failed")

    print(f"[RobloxOAuth] 연동 성공: discord={pending['user_id']} "
          f"roblox={info['roblox_id']}({info['roblox_name']})", flush=True)
    return _page("rbx_ok", status=200)


def run():
    port = int(os.getenv("PORT", 8080))
    app.run(host='0.0.0.0', port=port)


def keep_alive():
    t = Thread(target=run)
    t.daemon = True
    t.start()
