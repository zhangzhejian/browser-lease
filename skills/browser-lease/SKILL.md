---
name: browser-lease
description: 在本机启动浏览器自动化（Playwright、CDP、Chrome DevTools、网页操作、登录测试环境）之前，先用 browser-lease 申报任务并获取专属 Chrome 的 websocket_url。当用户要求打开网页、操作浏览器、跑 UI 测试、在测试环境登录账号、录制页面证据，或任务涉及 Chrome/Playwright/CDP 时触发。多个智能体并行使用浏览器时必须使用。
---

# browser-lease

单机浏览器任务协调器。每个任务得到一个独立的 Chrome 实例（独立 user-data-dir、动态调试端口），
注册表按 slot、账号、环境、业务对象做冲突检测，避免多个智能体互相踩踏。

所有命令输出 JSON。退出码：0 成功，2 失败，3 资源冲突（不会启动浏览器）。

## 工作流

### 1. 取得自己的 agent_pid

```bash
browser-lease whoami
```

`data.agent.pid` 就是申报用的 `agent_pid`。它是长期运行的智能体进程（如 claude、codex），
不是当前 shell 或 uv 进程。若 `agent` 为 null，从 `data.chain` 里挑宿主进程，或让用户提供 PID。

### 2. 先查这个账号已有的登录资料

```bash
browser-lease profiles --environment cn-test --account learner-a
```

账号要使用稳定的名字（如 `learner-a`、`teacher-main`），不要带日期或任务号，否则每次都是新 profile、
都要重新登录。`account` 名就是 profile 的钥匙。查到 `default_location: false` 的旧目录且用户希望沿用时，
才在申报里显式填写它的 `browser_data_dir`；其余情况省略该字段。

### 3. 写申报并注册

```bash
cat > /tmp/browser-lease-task.json <<'JSON'
{
  "task": "qa-001",
  "description": "一句话说明要做什么",
  "slot": "worker-a",
  "agent_pid": 12345,
  "environment": "cn-test",
  "account": "learner-a",
  "course_run": null,
  "artifacts_dir": "/Users/me/browser-lease-artifacts/qa-001",
  "control_mode": "browser",
  "headless": false,
  "environment_access": "shared",
  "resources": [{"kind": "course", "id": "course-a", "access": "read"}]
}
JSON
browser-lease register --file /tmp/browser-lease-task.json
```

字段说明：

| 字段 | 说明 |
| --- | --- |
| task | 唯一任务编号。已结束的任务不能复用编号 |
| slot | 本机独占的工位名 |
| agent_pid | 上一步得到的 PID，必须存活且属于当前用户 |
| environment | 环境标识，账号和资源冲突都在同一 environment 内判定 |
| account | 登录账号标识。同环境独占，即使只读。不登录时统一填 `visitor` |
| course_run | 学习实例标识，不涉及填 `null` |
| browser_data_dir | 通常省略，自动使用 `~/browser-lease-data/profiles/<environment>/<account>`，跨任务保留登录 |
| artifacts_dir | 截图、下载等证据目录，绝对路径，不能与 profile 目录相同或嵌套 |
| control_mode | `browser`（默认，CDP 控制）或 `desktop`（独占整个桌面，与所有任务互斥） |
| headless | 需要人工登录时填 false |
| environment_access | `exclusive` 会与同环境所有任务互斥，用于部署、故障注入 |
| resources | 业务对象列表，同 kind+id 任一为 write 即冲突 |

不要在申报里写密码或 token。

### 4. 处理冲突（退出码 3）

响应里 `error.detail.conflicts` 列出占用者和冲突字段。做法只有两种：
换真实的 slot/账号/资源再申报，或等对方 `stop`。
禁止改环境、账号别名绕过冲突，禁止杀别人的 Chrome。

### 5. 连接浏览器

成功响应的 `data.connection.websocket_url` 是唯一入口：

```python
browser = playwright.chromium.connect_over_cdp(connection["websocket_url"])
page = browser.contexts[0].pages[0]
```

或 Playwright MCP：

```bash
npx @playwright/mcp@<固定版本> --cdp-endpoint <websocket_url> --output-dir <artifacts_dir>
```

响应带 `verification_required`：连上后先在页面确认环境、登录账号、学习实例，再做业务操作。
截图和下载写入 `artifacts_dir`（下载目录见 `data.connection.downloads_dir`）。

### 6. 登录

连接后先确认页面是否已登录目标账号。未登录或会话过期时：

1. 确认申报为 `headless: false`，请用户在这个窗口里手动登录，并在 Chrome 提示时保存密码。
2. 等用户说登录完成后继续。下次同账号任务会直接处于登录状态。

智能体不输入、不读取、不导出密码，不在这些 Chrome 里登录 Google 账号。

### 7. 结束

```bash
browser-lease stop qa-001
```

`stop` 只关闭本任务的 Chrome，保留登录 profile 和证据。不要全局杀 Chrome。

## 上下文丢失后的恢复

```bash
browser-lease list --active --probe          # 找回任务
browser-lease show qa-001 --probe            # 看浏览器是否还活着
browser-lease reconnect qa-001 --agent-pid <whoami 得到的 PID>
```

原执行者还活着时返回 `owner_alive`，不会双重接管。浏览器已退出时 reconnect 报错，
应 `stop` 后用新编号重新申报，不要假装恢复了页面状态。

## 其他命令

```bash
browser-lease events qa-001            # 审计记录
browser-lease profiles                 # 各环境账号的登录资料目录和当前占用
browser-lease cleanup                  # 列出原执行者已退出的孤立任务
browser-lease cleanup --apply          # 回收它们
browser-lease purge-profile qa-001     # 已停止任务显式删除登录资料
```

## 禁止事项

- 不用 `getApp('Google Chrome')`、AppleScript 或系统级自动化去找现有 Chrome。
- 不猜调试端口，不复用其他任务的标签页。
- 不删除其他任务的目录，不自动清空登录资料。
- 注册表位于 `~/.local/state/browser-lease/registry.sqlite3`，不要直接改。

设计细节与完整冲突规则见仓库 README。
