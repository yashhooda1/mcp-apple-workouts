"""The few server-rendered pages (sign-in, sign-up, account): one shell, no
scripts, nothing third-party, not frameable."""
from __future__ import annotations

import html

esc = html.escape

_SHELL = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title><style>
body{{font:16px -apple-system,system-ui,sans-serif;background:#0b0d10;color:#e8eaed;
display:grid;place-items:center;min-height:100vh;margin:0}}
main{{background:#16191e;padding:28px;border-radius:16px;width:min(400px,90vw);margin:24px 0}}
h1{{font-size:20px;margin:0 0 6px}}h2{{font-size:15px;margin:22px 0 6px}}
p{{color:#9aa0a6;font-size:14px;margin:0 0 14px}}
label{{display:block;font-size:13px;color:#9aa0a6;margin:10px 0 4px}}
input{{width:100%;box-sizing:border-box;padding:12px;border-radius:10px;border:1px solid #333;
background:#0b0d10;color:inherit;font-size:16px}}
button,.btn{{display:block;margin-top:14px;width:100%;box-sizing:border-box;padding:12px;border:0;
border-radius:10px;background:#ff6b2c;color:#fff;font-weight:600;font-size:16px;
text-align:center;text-decoration:none;cursor:pointer}}
.btn.alt,button.alt{{background:#fff;color:#000}}.btn.quiet,button.quiet{{background:#262a31;color:#e8eaed}}
.err{{color:#ff8a80}}.ok{{color:#81c995}}a{{color:#ff9d6e}}
code{{display:block;background:#0b0d10;border:1px solid #333;border-radius:8px;padding:10px;
font-size:13px;word-break:break-all;user-select:all;margin:6px 0 10px}}
.or{{text-align:center;color:#9aa0a6;font-size:13px;margin:14px 0 0}}
</style></head><body><main>{body}</main></body></html>"""

_HEADERS = [
    (b"content-type", b"text/html; charset=utf-8"), (b"cache-control", b"no-store"),
    (b"x-frame-options", b"DENY"), (b"referrer-policy", b"no-referrer"),
    (b"x-content-type-options", b"nosniff"),
    (b"content-security-policy",
     b"default-src 'none'; style-src 'unsafe-inline'; form-action 'self' https:; "
     b"frame-ancestors 'none'")]


def client_ip(scope) -> str:
    """Best guess at the caller, for sign-in rate limits.

    Behind a hosting proxy the socket peer is the proxy, so every visitor
    would share one bucket and one person's typos would lock everyone out;
    prefer the first X-Forwarded-For hop. A caller can forge that header, which
    only moves them to a different per-IP bucket — the per-account lockout in
    accounts.py is what actually bounds password guessing.
    """
    forwarded = dict(scope.get("headers") or []).get(b"x-forwarded-for", b"").decode()
    first = forwarded.split(",")[0].strip()
    return first[:64] or (scope.get("client") or ("?",))[0]


def hidden(fields: dict) -> str:
    return "".join(f'<input type="hidden" name="{esc(k)}" value="{esc(str(v))}">'
                   for k, v in fields.items() if v)


def error(message: str) -> str:
    return f'<p class="err">{esc(message)}</p>' if message else ""


async def send_page(send, title: str, body: str, status: int = 200) -> None:
    """`body` is trusted markup built by the caller; escape every value in it."""
    page = _SHELL.format(title=esc(title), body=body)
    await send({"type": "http.response.start", "status": status, "headers": _HEADERS})
    await send({"type": "http.response.body", "body": page.encode()})


async def redirect(send, url: str) -> None:
    await send({"type": "http.response.start", "status": 302,
                "headers": [(b"location", url.encode()), (b"cache-control", b"no-store")]})
    await send({"type": "http.response.body", "body": b""})
