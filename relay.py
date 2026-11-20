#!/usr/bin/env python3
"""中间层 v3：JumpServer 钉钉通知 → 飞书(Lark) 交互卡片按钮审批。

- POST /notify        收到 JumpServer 转来的通知，解析直审批 token，发飞书交互卡片（同意/拒绝按钮）；
- POST /lark/callback 飞书事件订阅回调：url_verification 回 challenge；card.action.trigger 点按钮→回调 JumpServer 直审批接口；
- 审批身份由 JumpServer 直审批 token 的 approver_id + has_current_assignee() 保证，中间层不存任何凭据。

环境变量：
    LARK_APP_ID / LARK_APP_SECRET   必填
    LARK_HOST                      默认 https://open.feishu.cn（国际版 https://open.larksuite.com）
    LARK_VERIFICATION_TOKEN        可选（事件订阅若启用校验 token）
    RELAY_SECRET                   与 JumpServer JMS_IM_RELAY_SECRET 一致（可选）
    JMS_SITE_URL                   默认 
    LISTEN                         默认 0.0.0.0:8000
"""

from __future__ import annotations

import ast
import http.cookiejar
import io
import json
import os
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from PIL import Image, ImageDraw, ImageFont

LARK_APP_ID = os.environ.get("LARK_APP_ID", "").strip()
LARK_APP_SECRET = os.environ.get("LARK_APP_SECRET", "").strip()
LARK_HOST = os.environ.get("LARK_HOST", "https://open.feishu.cn").rstrip("/")
LARK_VERIFY_TOKEN = os.environ.get("LARK_VERIFICATION_TOKEN", "").strip()
RELAY_SECRET = os.environ.get("RELAY_SECRET", "").strip()
JMS_SITE_URL = os.environ.get("JMS_SITE_URL", "https://*.*.*.*:8443").rstrip("/")

_URL_RE = re.compile(r"https?://[^\s)>）\"']+/tickets/direct-approve/([A-Za-z0-9]+)/?")

_token_cache: str | None = None
_token_expire_at = 0.0


def _opener(verify: bool = True, no_redirect: bool = False, cookies=None):
    handlers = []
    ctx = ssl.create_default_context() if verify else ssl._create_unverified_context()
    handlers.append(urllib.request.HTTPSHandler(context=ctx))
    if no_redirect:
        handlers.append(_NoRedirect())
    if cookies is not None:
        handlers.append(urllib.request.HTTPCookieProcessor(cookies))
    return urllib.request.build_opener(*handlers)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _lark_token() -> str:
    global _token_cache, _token_expire_at
    if _token_cache and time.time() < _token_expire_at - 60:
        return _token_cache
    url = f"{LARK_HOST}/open-apis/auth/v3/tenant_access_token/internal/"
    body = json.dumps({"app_id": LARK_APP_ID, "app_secret": LARK_APP_SECRET}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    with _opener(verify=True).open(req, timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8", "replace"))
    if data.get("code") != 0:
        raise RuntimeError(f"lark token failed: {data}")
    _token_cache = data["tenant_access_token"]
    _token_expire_at = time.time() + int(data.get("expire", 7200))
    return _token_cache


# ---- 工单信息 → 图片（PNG）----
_FONT_REG = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"
_FONT_BOLD = "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"


def _parse_fields(md: str) -> list:
    fields = []
    for line in (md or "").splitlines():
        # 实际 markdown 是 `* **key:** value`（冒号在 ** 内）
        m = re.match(r"^\s*\*\s*\*\*(.+?)[:：]\*\*\s*(.+)$", line)
        if not m:
            m = re.match(r"^\s*\*\s*\*\*(.+?)\*\*\s*[:：]\s*(.+)$", line)
        if m:
            fields.append((m.group(1).strip(), m.group(2).strip()))
            continue
        m2 = re.match(r"^\s*(时间|申请原因|原因|说明)[:：]\s*(.+)$", line)
        if m2:
            fields.append((m2.group(1).strip(), m2.group(2).strip()))
    return fields


def _build_card(fields: list):
    info = {}
    for k, v in fields:
        if k not in info:
            info[k] = v

    snap = info.get("工单快照") or info.get("申请详情") or info.get("申请内容")
    sa, sn, sac = [], [], []
    if snap:
        try:
            d = ast.literal_eval(snap.replace(r"\_", "_").replace(r"\*", "*"))
            if isinstance(d, dict):
                sa = [str(x) for x in (d.get("apply_assets") or []) if x]
                sn = [str(x) for x in (d.get("apply_nodes") or []) if x]
                sac = [str(x) for x in (d.get("apply_accounts") or []) if x]
        except Exception:
            pass

    assets = info.get("资产") or ("、".join(sa) if sa else None)
    nodes = info.get("节点") or ("、".join(sn) if sn else None)
    accounts = info.get("申请账号") or info.get("账号") or ("、".join(sac) if sac else None)
    start, end = info.get("开始日期"), info.get("失效日期")
    valid = (f"{start} 至 {end}" if (start and end) else (start or end or None))

    box = [
        ("授权规则", info.get("授权规则名称")),
        ("组织", info.get("组织")),
        ("授权账号", accounts),
        ("动作", info.get("动作")),
        ("涉及资产", assets),
        ("涉及业务", nodes),
        ("生效日期", valid),
    ]
    box = [(k, v) for k, v in box if v]

    top = [
        ("标题", info.get("标题")),
        ("编号", info.get("序列号") or info.get("编号")),
        ("类型", info.get("类型")),
        ("申请人", info.get("申请人")),
    ]
    top = [(k, v) for k, v in top if v]

    bottom = [
        ("备注", info.get("备注") or info.get("申请原因") or info.get("原因")),
    ]
    bottom = [(k, v) for k, v in bottom if v]

    status = info.get("审批状态") or info.get("状态") or "审批中"
    return top, box, bottom, status


def _wrap(text: str, font, max_w: int) -> list:
    lines, cur = [], ""
    for ch in text:
        if font.getlength(cur + ch) <= max_w:
            cur += ch
        else:
            if cur:
                lines.append(cur)
            cur = ch
    if cur:
        lines.append(cur)
    return lines or [""]


def _dashed_rect(d, x1, y1, x2, y2, color, width=2, dash=9, gap=6):
    for x in range(x1, x2, dash + gap):
        d.line([(x, y1), (min(x + dash, x2), y1)], fill=color, width=width)
        d.line([(x, y2), (min(x + dash, x2), y2)], fill=color, width=width)
    for y in range(y1, y2, dash + gap):
        d.line([(x1, y), (x1, min(y + dash, y2))], fill=color, width=width)
        d.line([(x2, y), (x2, min(y + dash, y2))], fill=color, width=width)


def _render_image(subject: str, fields: list) -> bytes:
    W, pad = 660, 28
    f_title = ImageFont.truetype(_FONT_BOLD, 30)
    f_badge = ImageFont.truetype(_FONT_BOLD, 17)
    f_key = ImageFont.truetype(_FONT_BOLD, 18)
    f_val = ImageFont.truetype(_FONT_REG, 18)
    f_foot = ImageFont.truetype(_FONT_REG, 14)
    line_h = 28

    top_rows, box_items, bottom_rows, status = _build_card(fields)

    def block_h(items, key_font, val_font):
        h = 0
        for k, v in items:
            kw = int(key_font.getlength(k + "："))
            vw = W - 2 * pad - 40 - kw
            h += max(1, len(_wrap(v, val_font, max(40, vw)))) * line_h + 6
        return h

    head_h = 20 + 42 + 16
    top_h = (block_h(top_rows, f_key, f_val) + 6) if top_rows else 0
    box_h = (14 + block_h(box_items, f_key, f_val) + 16) if box_items else 0
    bottom_h = (10 + block_h(bottom_rows, f_key, f_val)) if bottom_rows else 0
    H = head_h + top_h + box_h + bottom_h + 52

    img = Image.new("RGB", (W, H), (8, 16, 30))
    top, bottom = (10, 22, 40), (26, 60, 90)
    d = ImageDraw.Draw(img)
    for y in range(H):
        t = y / max(1, H - 1)
        d.line([(0, y), (W, y)], fill=tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)))

    d_rgba = ImageDraw.Draw(img, "RGBA")
    for gx in range(0, W, 44):
        d_rgba.line([(gx, 0), (gx, H)], fill=(120, 200, 255, 8))
    for gy in range(0, H, 44):
        d_rgba.line([(0, gy), (W, gy)], fill=(120, 200, 255, 8))

    d = ImageDraw.Draw(img)
    L = 22
    for (cx, cy, dx, dy) in ((10, 10, 1, 1), (W - 10, 10, -1, 1), (10, H - 10, 1, -1), (W - 10, H - 10, -1, -1)):
        d.line([(cx + dx * L, cy), (cx, cy), (cx, cy + dy * L)], fill=(90, 210, 235), width=3)

    y = 20
    d.rounded_rectangle([pad, y + 3, pad + 6, y + 34], radius=3, fill=(64, 210, 232))
    d.text((pad + 16, y), "工单审批", font=f_title, fill=(255, 255, 255))

    # 状态徽章（右上角，只显示状态内容，按状态配色）
    if "通过" in status or "批准" in status or "同意" in status:
        sc = (110, 220, 160)
    elif "拒绝" in status or "驳回" in status:
        sc = (255, 120, 120)
    else:
        sc = (90, 210, 235)
    bw = int(f_badge.getlength(status)) + 26
    bx1, by1 = W - pad - bw, y + 2
    d_rgba.rounded_rectangle([bx1 - 3, by1 - 3, bx1 + bw + 3, by1 + 30], radius=16, fill=sc + (44,))
    d.rounded_rectangle([bx1, by1, bx1 + bw, by1 + 24], radius=13, outline=sc, width=2)
    d.text((bx1 + 13, by1 + 2), status, font=f_badge, fill=sc)
    y += 44

    # 顶部：编号 / 类型 / 申请人
    for k, v in top_rows:
        d.text((pad, y), k + "：", font=f_key, fill=(120, 205, 225))
        kw = int(f_key.getlength(k + "："))
        vw = W - 2 * pad - 40 - kw
        vy = y
        for vl in _wrap(v, f_val, max(40, vw)):
            d.text((pad + kw, vy), vl, font=f_val, fill=(230, 240, 245))
            vy += line_h
        y = vy + 6

    # 申请内容（虚线框，深色底，非红色，带立体阴影，无标题）
    if box_items:
        px, py = pad - 8, y + 2
        ph = 12 + block_h(box_items, f_key, f_val) + 14
        x2 = W - pad + 8
        d.rectangle([px + 5, py + 7, x2 + 5, py + ph + 7], fill=(0, 0, 0))
        d.rectangle([px, py, x2, py + ph], fill=(24, 38, 60))
        _dashed_rect(d, px, py, x2, py + ph, (110, 190, 225), width=2)
        yy = py + 12
        for k, v in box_items:
            d.text((px + 18, yy), k + "：", font=f_key, fill=(130, 205, 225))
            kw = int(f_key.getlength(k + "："))
            vw = W - 2 * pad - 16 - 18 - kw
            vy = yy
            for vl in _wrap(v, f_val, max(40, vw)):
                d.text((px + 18 + kw, vy), vl, font=f_val, fill=(235, 242, 247))
                vy += line_h
            yy = vy + 6
        y = py + ph + 16

    # 底部：备注
    for k, v in bottom_rows:
        d.text((pad, y), k + "：", font=f_key, fill=(120, 205, 225))
        kw = int(f_key.getlength(k + "："))
        vw = W - 2 * pad - 40 - kw
        vy = y
        for vl in _wrap(v, f_val, max(40, vw)):
            d.text((pad + kw, vy), vl, font=f_val, fill=(230, 240, 245))
            vy += line_h
        y = vy + 6

    d.line([(pad, H - 42), (W - pad, H - 42)], fill=(80, 140, 165), width=1)
    d.text((pad, H - 34), "JumpServer · 工单审批", font=f_foot, fill=(140, 175, 195))

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _upload_lark_image(png: bytes) -> str:
    boundary = "----jmslark" + uuid.uuid4().hex
    parts = [
        ("--" + boundary).encode(),
        b'Content-Disposition: form-data; name="image"; filename="card.png"',
        b"Content-Type: image/png",
        b"",
        png,
        ("--" + boundary).encode(),
        b'Content-Disposition: form-data; name="image_type"',
        b"",
        b"message",
        ("--" + boundary + "--").encode(),
    ]
    multipart = b"\r\n".join(parts) + b"\r\n"

    url = f"{LARK_HOST}/open-apis/im/v1/images"
    req = urllib.request.Request(url, data=multipart, method="POST")
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    req.add_header("Authorization", f"Bearer {_lark_token()}")
    with _opener(verify=True).open(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8", "replace"))
    if data.get("code") != 0:
        raise RuntimeError(f"上传图片失败: {data}")
    return data["data"]["image_key"]


def _lark_send_card(user_id: str, subject: str, text: str, token: str) -> dict:
    fields = _parse_fields(text)
    png = _render_image(subject, fields)
    image_key = _upload_lark_image(png)

    card = {
        "config": {"wide_screen_mode": True, "enable_forward": False},
        "elements": [
            {"tag": "img", "img_key": image_key, "alt": {"tag": "plain_text", "content": "工单详情"}},
            {"tag": "action", "actions": [
                {"tag": "button", "text": {"tag": "plain_text", "content": "✅  同意"},
                 "type": "primary", "value": {"action": "approve", "token": token}},
                {"tag": "button", "text": {"tag": "plain_text", "content": "❌  拒绝"},
                 "type": "danger", "value": {"action": "reject", "token": token}},
            ]},
        ],
    }
    body = {
        "receive_id": user_id,
        "msg_type": "interactive",
        "content": json.dumps(card, ensure_ascii=False),
    }
    url = f"{LARK_HOST}/open-apis/im/v1/messages?receive_id_type=user_id"
    req = urllib.request.Request(
        url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {_lark_token()}"},
        method="POST",
    )
    with _opener(verify=True).open(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def forward(chat_ids: list[str], subject: str, text: str) -> int:
    sent = 0
    for user_id in chat_ids:
        user_id = user_id.strip()
        if not user_id:
            continue
        m = _URL_RE.search(text or "")
        if not m:
            continue
        token = m.group(1)
        try:
            _lark_send_card(user_id, subject, text, token)
            sent += 1
            print(f"[token] {token} -> user {user_id}", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[Lark发送失败] user={user_id} {exc}", flush=True)
    return sent


def direct_approve(token: str, action: str) -> tuple[bool, str]:
    """调 JumpServer 直审批接口，返回 (是否成功, 说明)。"""
    url = f"{JMS_SITE_URL}/core/tickets/direct-approve/{token}/"
    cj = http.cookiejar.CookieJar()
    op = _opener(verify=False, no_redirect=True, cookies=cj)

    # 1) GET 表单，拿到 csrfmiddlewaretoken
    try:
        r = op.open(urllib.request.Request(url), timeout=15)
        html = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError:
        return False, "token 已失效或工单已处理"

    m = re.search(r'name=["\']csrfmiddlewaretoken["\'][^>]*value=["\']([^"\']+)["\']', html) or \
        re.search(r'value=["\']([^"\']+)["\'][^>]*name=["\']csrfmiddlewaretoken["\']', html)
    if not m:
        return False, "未找到 csrfmiddlewaretoken"
    csrf = m.group(1)

    # 2) POST 提交表单（带 Referer，HTTPS 下 Django 强制校验 Referer）
    data = urllib.parse.urlencode({"csrfmiddlewaretoken": csrf, "action": action}).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    req.add_header("Referer", url)
    try:
        r = op.open(req, timeout=15)
        r.read()
    except urllib.error.HTTPError as e:
        if e.code == 403:
            return False, "CSRF 校验失败"

    # 3) 再次 GET：成功则 token 已被清除（302），失败则仍是 200 表单
    try:
        r = op.open(urllib.request.Request(url), timeout=15)
        r.read()
        return False, "审批未生效：无权限或工单状态已变化"
    except urllib.error.HTTPError:
        return True, "已提交"


def _handle_lark_callback(body: dict) -> dict:
    if body.get("type") == "url_verification":
        return {"challenge": body.get("challenge", "")}

    header = body.get("header") or {}
    if header.get("event_type") != "card.action.trigger":
        return {}

    if LARK_VERIFY_TOKEN and header.get("token") and header.get("token") != LARK_VERIFY_TOKEN:
        return {}

    event = body.get("event") or {}
    value = (event.get("action") or {}).get("value")
    if isinstance(value, dict):
        act, tok = value.get("action"), value.get("token")
    else:
        m = re.match(r"^(approve|reject):([A-Za-z0-9]+)$", str(value or ""))
        act, tok = (m.group(1), m.group(2)) if m else (None, None)

    if act not in ("approve", "reject") or not tok:
        return {}

    ok, note = direct_approve(tok, "approve" if act == "approve" else "reject")
    if ok:
        label = "✅ 已同意" if act == "approve" else "✅ 已拒绝"
    else:
        label = "❌ " + note
    print(f"[callback] {act} {tok} -> {label}", flush=True)
    return {"toast": {"type": "info", "content": label}}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _json(self, code: int, obj) -> None:
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):  # noqa: N802
        if self.path.split("?", 1)[0] == "/healthz":
            self._json(200, {"ok": True})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        path = self.path.split("?", 1)[0]
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            self._json(400, {"error": "bad json"})
            return

        if path == "/lark/callback":
            self._json(200, _handle_lark_callback(body))
            return

        if path != "/notify":
            self._json(404, {"error": "not found"})
            return

        if RELAY_SECRET and self.headers.get("X-Relay-Secret") != RELAY_SECRET:
            self._json(401, {"error": "bad secret"})
            return

        recipients = body.get("recipients")
        if isinstance(recipients, str):
            recipients = [recipients]
        if not isinstance(recipients, list) or not recipients:
            self._json(400, {"error": "recipients required"})
            return

        subject = str(body.get("subject") or "")
        print(f"[notify] recipients={recipients} subject={subject[:60]!r}", flush=True)
        sent = forward([str(i) for i in recipients], subject, str(body.get("message") or ""))
        self._json(200, {"ok": True, "sent": sent})

    def log_message(self, *args):
        pass


def main() -> None:
    host, _, port = (os.environ.get("LISTEN", "0.0.0.0:8000")).rpartition(":")
    ThreadingHTTPServer((host or "0.0.0.0", int(port)), Handler).serve_forever()


if __name__ == "__main__":
    main()
