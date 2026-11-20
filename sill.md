[SKILL.md](https://github.com/user-attachments/files/35157351/SKILL.md)
---
name: jumpserver-lark-approval
description: 部署与排查 JumpServer 工单审批对接 Lark（飞书/Lark 国际版）：把工单渲染成图片卡片并带「同意/拒绝」按钮，点按钮直接在飞书里审批。涵盖 Lark 应用配置、JumpServer 钉钉后端转发补丁与配置、中间层 relay.py/lark_ws.py 部署与验证。当用户要给 JumpServer 接入 Lark 图片卡片审批、或排查该链路时报错时使用。
whenToUse: 用户提到 JumpServer 工单审批、Lark/飞书审批、图片卡片审批、按钮直接审批、钉钉通道转发、直审批 token、或该链路部署/报错时。
---

# JumpServer → Lark 图片卡片审批

JumpServer v4.10.20 的工单审批，审批人在 Lark 里收到一张**图片卡片**（深色科技风 + 红色/虚线框高亮申请内容 + 右上角状态徽章），点「✅同意 / ❌拒绝」按钮直接审批，无需打开网页、不存任何人的 JumpServer 凭据。

## 架构

```text
JumpServer 钉钉后端 dingtalk.py（复用，改成转发）
  → POST /notify 到中间层（JMS_IM_RELAY_URL）
  → relay.py 解析工单 markdown → Pillow 生成图片 → 上传 Lark → 发图片卡片(按钮)
  → 用户点按钮 → Lark 长连接(WebSocket) → lark_ws.py 收 card.action.trigger
  → 调 JumpServer 直审批 POST /core/tickets/direct-approve/<token>/
```

- 身份来自直审批 token 内置的 `approver_id` + JumpServer `has_current_assignee()` 再校验。
- 前置：打开「审批无需登录」`TICKETS_DIRECT_APPROVE=true`。
- 本仓库代码：`code/relay.py`、`code/lark_ws.py`、`code/dingtalk.py`。

## 部署步骤

### 1. Lark 应用配置（后台点选）
1. 创建企业自建应用（`open.larksuite.com` 国际版 / `open.feishu.cn` 中国版），记 App ID/Secret。
2. 权限管理开通：`im:message`、`contact:user.employee_id:readonly`、`im:resource:upload`。
3. 启用「机器人」能力（否则 230006）。
4. 事件与回调：订阅方式=**使用长连接接收事件**；在「回调」区添加 `card.action.trigger`（新版）。
5. 可用范围加审批人；发布版本 + 管理员审核通过。
6. 拿审批人 `user_id`（管理后台成员详情，非工号）。

### 2. JumpServer 代码修改（只改 1 文件）
把 `apps/notifications/backends/dingtalk.py` 替换为 `code/dingtalk.py`（复用钉钉后端做转发，未配中间层时保持原钉钉行为）。

```bash
docker exec jms_core cp /opt/jumpserver/apps/notifications/backends/dingtalk.py{,.bak}
docker cp dingtalk.py jms_core:/opt/jumpserver/apps/notifications/backends/dingtalk.py
docker cp dingtalk.py jms_celery:/opt/jumpserver/apps/notifications/backends/dingtalk.py
```

> `docker cp` 是临时的，`jmsctl.sh restart/upgrade` 重建会丢；生产要镜像重打包或 bind mount。

### 3. JumpServer 配置
`config.txt` 追加：
```ini
JMS_IM_RELAY_URL=http://<中间层IP>:8000/notify
JMS_IM_RELAY_SECRET=<随机串>
```

DB 设置（SITE_URL/TICKETS_DIRECT_APPROVE 会被自动覆盖，必须写 DB）：
```python
from settings.models import Setting
s = Setting.objects.get_or_create(name='SITE_URL')[0]
s.cleaned_value = 'https://<对外地址>'; s.save(); Setting.refresh_item('SITE_URL')
s = Setting.objects.get_or_create(name='TICKETS_DIRECT_APPROVE')[0]
s.cleaned_value = True; s.save(); Setting.refresh_item('TICKETS_DIRECT_APPROVE')
```

绑定：`User.dingtalk_id = Lark user_id`，并把 `dingtalk` 加进 `receive_backends`（`dingtalk_id` 唯一，一个 user_id 只能绑一个 JumpServer 用户）。

### 4. 中间层部署
依赖：`apt-get install -y python3-pip fonts-noto-cjk; pip install Pillow lark-oapi`。
文件：`relay.py`（主服务）、`lark_ws.py`（回调）、`env`。
env 关键项：`LARK_APP_ID`、`LARK_APP_SECRET`、`LARK_HOST`/`LARK_DOMAIN`（国际版 open.larksuite.com）、`RELAY_SECRET`、`JMS_SITE_URL`、`LISTEN=0.0.0.0:8000`。
两个 systemd 服务：`jms-tg-relay`（relay.py）、`jms-lark-ws`（lark_ws.py），都 `EnvironmentFile=/opt/jms-tg-relay/env`。

### 5. 验证
提交 3 级工单 → 第 1 级收到图片卡 → 点同意 → 逐级到 approved。
排查：`journalctl -u jms-tg-relay -f` / `journalctl -u jms-lark-ws -f`。

## 关键坑（务必记住）

1. Lark 域名：国际版 `open.larksuite.com`，中国版 `open.feishu.cn`，用错报 `Incorrect domain name`。
2. 直审批 URL 必须带 `/core/`：`/core/tickets/direct-approve/<token>/`。
3. CSRF cookie 名是 `jms_csrftoken`（非 `csrftoken`）；HTTPS 下 POST 必须带 `Referer`，否则 `csrf_failure=1`。
4. JumpServer 工单 markdown 字段格式是 `* **字段:** 值`（冒号在 `**` 内），解析正则必须匹配它。
5. `SITE_URL`/`TICKETS_DIRECT_APPROVE` 写 DB 设置，否则被 `update_site_url()` 覆盖成内网 `http://<HOST_IP>`。
6. `im:resource:upload` 权限缺了图片上传 99991672；`contact:user.employee_id:readonly` 缺了发消息 99991672；机器人能力没开 230006。
7. `dingtalk_id` 唯一约束：同一 Lark 账号不能同时绑多个 JumpServer 用户（admin= 要切绑定）。
8. `TICKETS_DIRECT_APPROVE`（审批无需登录）等同"拿到 token 链接就能批"，正式环境评估是否保留。
9. 卡片正文是图片，中文渲染依赖 `fonts-noto-cjk`。
10. JumpServer 容器重建会丢 `docker cp` 的补丁。
