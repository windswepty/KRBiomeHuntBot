import os
import logging
from threading import Thread

from flask import Flask, redirect, request, Response, make_response

from utils.link_tokens import redeem, peek_owner
from utils import oauth_gate

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
    "auth_failed": ("신원 확인에 실패했습니다",
                    "다시 시도해 주세요.<br>"
                    "문제가 계속되면 디스코드 버튼을 다시 눌러 주세요."),
}


def _page(reason: str, status: int = 410) -> Response:
    title, body = _MESSAGES.get(reason, _MESSAGES["unknown"])
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
  <div class="mark">🔒</div>
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
        # 아직 신원이 없음 → 디스코드로 보내 한 번만 확인받습니다
        state = oauth_gate.sign_state(token)
        if not state:
            return _deliver(token)
        return redirect(oauth_gate.authorize_url(_redirect_uri(), state), code=302)

    if viewer != user_id:
        return _page("not_owner", status=403)

    allowed, _ = oauth_gate.check_member(guild_id, user_id)
    if not allowed:
        return _page("not_member", status=403)

    return _deliver(token)


@app.route('/auth/callback')
def auth_callback():
    if not oauth_gate.enabled():
        return _page("auth_failed")

    token = oauth_gate.read_state(request.args.get("state"))
    if not token:
        return _page("auth_failed")

    code = request.args.get("code")
    if not code:
        return _page("auth_failed")

    user_id = oauth_gate.exchange_code(code, _redirect_uri())
    if user_id is None:
        return _page("auth_failed")

    signed = oauth_gate.sign_user(user_id)
    if not signed:
        return _page("auth_failed")

    # 신원을 쿠키에 심고, 원래 가려던 링크로 되돌립니다.
    # 다음부터는 이 단계 없이 즉시 통과합니다.
    resp = make_response(redirect(f"/j/{token}", code=302))
    resp.set_cookie(
        oauth_gate.COOKIE_NAME, signed,
        max_age=oauth_gate.COOKIE_MAX_AGE,
        httponly=True, secure=True, samesite="Lax",
    )
    return resp


def run():
    port = int(os.getenv("PORT", 8080))
    app.run(host='0.0.0.0', port=port)


def keep_alive():
    t = Thread(target=run)
    t.daemon = True
    t.start()
