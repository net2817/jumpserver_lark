#!/usr/bin/env python3
"""飞书长连接回调 worker：接收 card.action.trigger（按钮点击），调 JumpServer 直审批。

依赖：pip install lark-oapi
环境变量：LARK_APP_ID / LARK_APP_SECRET / JMS_SITE_URL
"""

from __future__ import annotations

import http.cookiejar
import json
import os
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request

import lark_oapi as lark
from lark_oapi.ws import Client
from lark_oapi.event.callback.model.p2_card_action_trigger import (
    P2CardActionTrigger,
    P2CardActionTriggerResponse,
    CallBackToast,
)

APP_ID = os.environ.get("LARK_APP_ID", "").strip()
APP_SECRET = os.environ.get("LARK_APP_SECRET", "").strip()
LARK_DOMAIN = os.environ.get("LARK_DOMAIN", "https://open.feishu.cn").rstrip("/")
JMS_SITE_URL = os.environ.get("JMS_SITE_URL", "https://*.*.*.*:8443").rstrip("/")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _jms_opener(no_redirect=False, cookies=None):
    handlers = [urllib.request.HTTPSHandler(context=ssl._create_unverified_context())]
    if no_redirect:
        handlers.append(_NoRedirect())
    if cookies is not None:
        handlers.append(urllib.request.HTTPCookieProcessor(cookies))
    return urllib.request.build_opener(*handlers)


def direct_approve(token: str, action: str) -> tuple[bool, str]:
    """调 JumpServer 直审批接口，返回 (是否成功, 说明)。"""
    url = f"{JMS_SITE_URL}/core/tickets/direct-approve/{token}/"
    cj = http.cookiejar.CookieJar()
    op = _jms_opener(no_redirect=True, cookies=cj)

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

    try:
        r = op.open(urllib.request.Request(url), timeout=15)
        r.read()
        return False, "审批未生效：无权限或工单状态已变化"
    except urllib.error.HTTPError:
        return True, "已提交"


def do_card_trigger(data: P2CardActionTrigger) -> P2CardActionTriggerResponse:
    resp = P2CardActionTriggerResponse()
    toast = CallBackToast()
    try:
        ev = data.event
        value = ev.action.value if ev.action else None
        print(f"[ws] value={value!r}", flush=True)
        if isinstance(value, dict):
            act = value.get("action")
            tok = value.get("token")
        else:
            m = re.match(r"^(approve|reject):([A-Za-z0-9]+)$", str(value or ""))
            act, tok = (m.group(1), m.group(2)) if m else (None, None)

        if act not in ("approve", "reject") or not tok:
            toast.type = "error"
            toast.content = "未知操作"
            resp.toast = toast
            return resp

        ok, note = direct_approve(tok, act)
        label = ("已同意" if act == "approve" else "已拒绝") if ok else ("失败: " + note)
        toast.type = "success" if ok else "error"
        toast.content = label
        print(f"[ws] {act} {tok} -> {label}", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[ws error] {exc}", flush=True)
        toast.type = "error"
        toast.content = "处理异常"
    resp.toast = toast
    return resp


def main() -> None:
    handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_card_action_trigger(do_card_trigger)
        .build()
    )
    client = lark.ws.Client(
        APP_ID, APP_SECRET, event_handler=handler, log_level=lark.LogLevel.INFO,
        domain=LARK_DOMAIN,
    )
    client.start()


if __name__ == "__main__":
    main()
