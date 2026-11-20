"""通知后端：钉钉（可配置转发到中间层，再由中间层发 Telegram）。

- 未配置 JMS_IM_RELAY_URL：行为与官方钉钉后端完全一致；
- 配置 JMS_IM_RELAY_URL：把收件人(user.dingtalk_id)、标题、消息 POST 给中间层。
  钉钉 SDK / 认证代码不参与转发路径，保持原样。

中间层约定（本仓库 relay.py）：
    POST <JMS_IM_RELAY_URL>
    Headers: Content-Type: application/json  [X-Relay-Secret: ...]
    Body: {"recipients": ["chat_id", ...], "subject": "...", "message": "..."}
"""

import json
import os
import urllib.request

from django.conf import settings

from common.sdk.im.dingtalk import DingTalk as Client
from .base import BackendBase

RELAY_URL = (
    os.environ.get("JMS_IM_RELAY_URL", "") or getattr(settings, "JMS_IM_RELAY_URL", "")
).strip()
RELAY_SECRET = (
    os.environ.get("JMS_IM_RELAY_SECRET", "") or getattr(settings, "JMS_IM_RELAY_SECRET", "")
).strip()


class DingTalk(BackendBase):
    account_field = "dingtalk_id"
    # 不复用 AUTH_DINGTALK 作为唯一开关，避免误开钉钉 SSO 登录。
    # 配置了中间层地址即启用；否则回退到官方钉钉开关。
    is_enable_field_in_settings = "AUTH_DINGTALK"

    @classmethod
    def is_enable(cls) -> bool:
        return bool(RELAY_URL) or bool(getattr(settings, "AUTH_DINGTALK", False))

    def __init__(self) -> None:
        if not RELAY_URL:
            self.dingtalk = Client(
                appid=settings.DINGTALK_APPKEY,
                appsecret=settings.DINGTALK_APPSECRET,
                agentid=settings.DINGTALK_AGENTID,
            )

    def send_msg(self, users, message, subject=None):
        accounts, __, __ = self.get_accounts(users)
        if not accounts:
            return
        if RELAY_URL:
            return self._send_relay(accounts, subject, message)
        return self.dingtalk.send_markdown(accounts, subject, message)

    def _send_relay(self, accounts, subject, message) -> None:
        payload = {
            "recipients": list(accounts),
            "subject": subject or "",
            "message": message or "",
        }
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if RELAY_SECRET:
            headers["X-Relay-Secret"] = RELAY_SECRET
        request = urllib.request.Request(
            RELAY_URL, data=data, headers=headers, method="POST"
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            response.read()


backend = DingTalk
