# 钉钉通知后端 → 中间层 → Telegram（只改通知入口，不动 SDK）

把 JumpServer 的**钉钉通知后端**接到一个中间层，由中间层转发到 Telegram。

关键点：

- 只改 **1 个文件**：`apps/notifications/backends/dingtalk.py`（通知发送入口）；
- **不动** `apps/common/sdk/im/dingtalk/__init__.py`（钉钉 SDK 和认证代码保持原样）；
- 未配置中间层地址时，后端行为与官方钉钉完全一致（可随时回退）。

```text
JumpServer 钉钉后端 dingtalk.py
  → 若配置 JMS_IM_RELAY_URL：POST 收件人/标题/消息 给中间层
  → dingtalk-relay/relay.py
  → 解析正文里的直审批链接 → Telegram inline URL 按钮
  → 审批人点「打开审批」→ JumpServer 审批页
```

---

## 1. 改动（只 1 个文件）

`apps/notifications/backends/dingtalk.py` 用本仓库
[`patch/apps/notifications/backends/dingtalk.py`](patch/apps/notifications/backends/dingtalk.py) 覆盖即可。

核心逻辑：

```python
class DingTalk(BackendBase):
    account_field = "dingtalk_id"        # 存 Telegram chat_id
    is_enable_field_in_settings = "AUTH_DINGTALK"

    def send_msg(self, users, message, subject=None):
        accounts, __, __ = self.get_accounts(users)
        if not accounts:
            return
        if RELAY_URL:                     # 有中间层就走中间层
            return self._send_relay(accounts, subject, message)
        return self.dingtalk.send_markdown(accounts, subject, message)  # 否则走原钉钉
```

`_send_relay` 发：

```json
POST <JMS_IM_RELAY_URL>
{"recipients": ["<chat_id>", ...], "subject": "标题", "message": "正文(含直审批链接)"}
```

---

## 2. 配置（JumpServer）

`config.txt`（Docker）或环境变量：

```ini
AUTH_DINGTALK=true
JMS_IM_RELAY_URL=http://<中间层IP>:8000/notify
# 可选：中间层鉴权，与 relay 的 RELAY_SECRET 一致
JMS_IM_RELAY_SECRET=<随机串>
```

> `JMS_IM_RELAY_URL` / `JMS_IM_RELAY_SECRET` 在 `dingtalk.py` 里直接用
> `os.environ.get(...)` 读取，**不需要改 settings 文件**。

重启：

```bash
docker restart jms_core jms_celery
```

---

## 3. 中间层启动

```bash
export TELEGRAM_BOT_TOKEN=123456789:AAH...
export LISTEN=0.0.0.0:8000
export RELAY_SECRET=<与上面一致，可选>
python3 relay.py
```

健康检查：

```bash
curl http://127.0.0.1:8000/healthz   # {"ok": true}
```

---

## 4. 绑定审批人（把 chat_id 填进 dingtalk_id）

```bash
cd /opt/jumpserver/apps
python manage.py shell
```

```python
from users.models import User
from notifications.models import UserMsgSubscription

u = User.objects.get(username='zhangsan')
u.dingtalk_id = '123456789'          # 审批人的 Telegram chat_id
u.save(update_fields=['dingtalk_id'])

sub, _ = UserMsgSubscription.objects.get_or_create(user=u)
if 'dingtalk' not in (sub.receive_backends or []):
    sub.receive_backends = list(sub.receive_backends or []) + ['dingtalk']
    sub.save(update_fields=['receive_backends'])
```

审批人私聊 Bot 发 `/start` 可回显 chat_id（见 `jumpserver-telegram-channel/bot/bind_bot.py`）。

---

## 5. 工单通知来源（只发通知，不改）

消息内容由 JumpServer 工单通知生成，来源：

- `apps/tickets/notifications.py` → `TicketAppliedToAssigneeMessage`
- `apps/tickets/utils.py` → `send_ticket_applied_mail_to_assignees`

其中 `get_dingtalk_msg()` 返回的 Markdown 正文里已经带了：

```text
https://<SITE_URL>/tickets/direct-approve/<32位token>/
```

中间层用正则提取该链接，作为 Telegram 的「打开审批」按钮 URL。审批身份仍由 JumpServer
的直审批 token + 登录态（或“审批无需登录”）保证，审计是本人。

---

## 6. 节点切换

仍由 JumpServer `_on_step_approved()` 自动给下一级审批人发通知 → 走后端 → 中间层 → Telegram，
中间层不需要轮询。

---

## 7. 后续：要“在 Telegram 里直接点按钮批”

当前是**链接审批**（点按钮打开 JumpServer 页面）。若要 TG 内直接批，需要再加：

1. 中间层把直审批链接的 token 换成 `approve:<token>` / `reject:<token>` 回调按钮；
2. 中间层收到 Telegram 回调后，POST 回 JumpServer 的直审批接口（带 token）；
3. JumpServer 用一个 csrf-exempt 端点按 token 反查 approver_id 并 approve（这步才需要碰 JumpServer 审批接口）。

这三步属于第二期；本仓库当前先覆盖“通知转发”这一段。

---

## 8. 目录

```text
dingtalk-relay/
├── relay.py                                  # 中间层（/notify -> Telegram）
├── patch/apps/notifications/backends/dingtalk.py   # 覆盖原后端
└── README.md
```
