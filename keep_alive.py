import os
import logging
from threading import Thread

from flask import Flask, redirect, request, Response, make_response

from utils import link_tokens
from utils.link_tokens import redeem, peek_owner
from utils import oauth_gate, roblox_oauth

app = Flask(__name__)

# Suppress flask default logs to keep console clean
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)


@app.route('/')
def home():
    return "KRBiomeHuntBot is alive!"


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
    return _doc("개인정보 처리방침 · KR Biome Hunt Verification", """
<h1>개인정보 처리방침</h1>
<p class="muted">KR Biome Hunt Verification (한바헌 봇)</p>

<h2>수집하는 정보</h2>
<p>로블록스 계정을 연동할 때 <code>openid</code> · <code>profile</code> 권한으로
다음만 받습니다.</p>
<ul>
  <li>로블록스 사용자 ID</li>
  <li>로블록스 사용자명 / 표시 이름</li>
  <li>계정 생성 일자</li>
</ul>
<p>비밀번호, 로벅스, 인벤토리, 결제 정보에는 접근하지 않으며 받지도 않습니다.</p>

<h2>이용 목적</h2>
<p>디스코드 계정과 로블록스 계정을 연결해, 커뮤니티에서 배포한 비공개 서버
링크를 누가 받아갔는지 확인하기 위한 용도로만 사용합니다. 링크가 외부로
유출되었을 때 확인하는 데 쓰입니다.</p>

<h2>보관 및 파기</h2>
<p>연동 정보는 운영자가 관리하는 데이터베이스에 보관합니다. 이용자가 연동
해제를 요청하면 즉시 삭제합니다. 디스코드에서 <code>/연동해제</code> 를 관리자에게
요청하시면 됩니다.</p>

<h2>제3자 제공</h2>
<p>수집한 정보를 외부에 판매하거나 제공하지 않습니다.</p>

<h2>문의</h2>
<p>커뮤니티 디스코드 서버의 운영진에게 문의해 주세요.</p>
""")


@app.route('/terms')
def terms():
    return _doc("이용약관 · KR Biome Hunt Verification", """
<h1>이용약관</h1>
<p class="muted">KR Biome Hunt Verification (한바헌 봇)</p>

<h2>서비스 내용</h2>
<p>이 애플리케이션은 디스코드 커뮤니티 구성원이 자신의 로블록스 계정을
연동할 수 있게 하는 도구입니다. 연동은 전적으로 자발적입니다.</p>

<h2>이용자의 책임</h2>
<p>본인 소유의 로블록스 계정만 연동해야 합니다. 커뮤니티에서 제공한 비공개
서버 링크를 외부에 배포하지 않아야 하며, 위반 시 커뮤니티 규정에 따라
이용이 제한될 수 있습니다.</p>

<h2>면책</h2>
<p>이 서비스는 있는 그대로 제공되며, 로블록스 또는 디스코드의 정책 변경이나
서비스 중단으로 인한 이용 불가에 대해 책임지지 않습니다.</p>

<h2>변경</h2>
<p>약관이 변경되면 이 페이지에 반영됩니다.</p>
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
