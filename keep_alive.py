import os
import logging
from threading import Thread

from flask import Flask, redirect, Response

from utils.link_tokens import redeem

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
# 토큰은 짧게 만료되므로 퍼뜨려도 쓸 수 없습니다.
#
# Flask 는 keep_alive() 가 띄우는 데몬 스레드에서 돌고, 토큰 저장소는
# 같은 프로세스의 메모리를 공유합니다(utils/link_tokens.py 가 락으로 보호).

_MESSAGES = {
    "expired": ("링크가 만료되었습니다",
                "보안을 위해 짧은 시간만 유효합니다.<br>"
                "디스코드로 돌아가 버튼을 다시 눌러 주세요."),
    "exhausted": ("이미 사용된 링크입니다",
                  "이 링크는 사용 횟수를 다 썼습니다.<br>"
                  "디스코드로 돌아가 버튼을 다시 눌러 주세요."),
    "unknown": ("유효하지 않은 링크입니다",
                "주소가 잘못되었거나 이미 만료된 링크입니다.<br>"
                "디스코드로 돌아가 버튼을 다시 눌러 주세요."),
}


def _error_page(reason: str) -> Response:
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
    return Response(html, status=410, mimetype="text/html; charset=utf-8")


@app.route('/j/<token>')
def join(token: str):
    url, reason = redeem(token)
    if url is None:
        return _error_page(reason)
    # 302 로 곧바로 넘겨 지연을 최소화합니다 (자리 경쟁 상황 고려)
    return redirect(url, code=302)


def run():
    port = int(os.getenv("PORT", 8080))
    app.run(host='0.0.0.0', port=port)


def keep_alive():
    t = Thread(target=run)
    t.daemon = True
    t.start()
