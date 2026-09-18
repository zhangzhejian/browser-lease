# Browser Lease

单机、跨项目的 Python 浏览器任务启动器，第一版支持 macOS / Linux。
已有 Codex、Claude Code 进程在需要浏览器时调用它。启动器登记任务并启动专属 Chrome，
不负责启动智能体、不替用户登录、不操作业务数据。

## 安装与使用

```bash
cd /Users/zhejianzhang/browser-lease
uv sync --locked
```

`browser-lease` 是本目录的薄命令入口。已安装到 `~/.local/bin/browser-lease` 时可以从任意目录调用。
也可以使用 `uv run --project /Users/zhejianzhang/browser-lease browser-lease ...`。

```bash
browser-lease register --file /绝对路径/task.json
browser-lease list --active --probe
browser-lease list --environment cn-test --slot worker-a
browser-lease show qa-001 --probe
browser-lease reconnect qa-001 --agent-pid 1111
browser-lease events qa-001
browser-lease stop qa-001
browser-lease cleanup                 # 只列原执行者已退出的任务
browser-lease cleanup --apply         # 关闭这些任务的浏览器并释放占用
browser-lease purge-profile qa-001    # 停止后显式删除登录资料；保留证据
```

申报示例见 `examples/task.json`。PID 和示例账号、学习实例必须替换为实际值。
`agent_pid` 是长期执行任务的进程，不是短命的工具调用 shell 或启动器进程；
通过宿主提供的进程信息或核对父进程链确定。第一版需要此进程存活且属于当前系统用户。
`task` 唯一，同一执行者以完全相同申报重试会返回已有浏览器；已结束任务使用新编号。
账号不涉及登录时可声明统一的 `visitor` 标识，保守地独占它，或为真正独立访客槽位分配不同标识。
不涉及学习实例时显式填写 `course_run: null`。

## 申报、冲突与持久化

标准流程：校验 → 全局文件锁 → 检测冲突 → SQLite 登记 starting → 建目录 → 启动 Chrome
→ 核对专属调试端点 → ready → 返回连接。全局锁覆盖整个启动段，避免两次并发申报都通过检查。
启动可等待 20 秒，全局锁最多等待 30 秒，超过返回结构化错误，智能体可稍后重试。

唯一生产注册表是 `~/.local/state/browser-lease/registry.sqlite3`。
CLI 不提供切换注册表参数，避免两个项目分别登记而漏掉跨项目冲突。
目录权限 700，数据库、标记与日志权限 600。记录 PID + 进程启动时间，避免 PID 复用误判。

冲突规则：

| 资源 | 范围与判定 |
| --- | --- |
| slot | 本机独占 |
| 浏览器目录、证据目录 | 规范化真实路径；相同、父子嵌套和跨类型重叠均冲突 |
| account | 同 environment 独占，即使任务声明只读也不复用同一账号 |
| course_run | 非空时，同 environment 独占 |
| resources | 同 environment 下 kind + id 相同，任一为 write 则冲突；read/read 可并发 |
| environment_access=exclusive | 与同 environment 的所有活动任务冲突，适合部署、故障注入 |
| control_mode=desktop | 与注册表内所有活动任务冲突；browser 也会被已有 desktop 阻止 |

所有项目必须使用相同环境、账号和对象的规范标识，别名不会自动归并。
同一业务对象须使用相同 `kind`、`id`；未申报的服务端共享状态无法由本工具发现。
同一 profile 目录保留 environment/account 归属，后续即使没有活动任务，也不允许改成另一个账号。
若需要换账号，使用新目录或显式删除旧目录后再申报。首次只能接收空目录，拒绝接管日常 Chrome 数据。

冲突返回退出码 3；不创建任务、不启动 Chrome、不自动抢占：

```json
{
  "schema_version": "browser-lease/0.1",
  "ok": false,
  "code": "resource_conflict",
  "error": {
    "message": "资源被占用，请修改申报后重试",
    "detail": {
      "conflicts": [{"task": "qa-000", "slot": "worker-a", "status": "ready", "fields": ["slot"]}]
    }
  }
}
```

其他失败退出码 2，成功为 0。所有命令结果为 JSON，命令用法通过 `--help` 查看。
查询只报告事实，不悄悄重启浏览器或释放资源。`list` 支持分页，默认 50，最多 100 项。

## 连接与重新接管

成功响应中的 `data.connection` 包含：

```json
{
  "cdp_url": "http://127.0.0.1:动态端口",
  "websocket_url": "ws://127.0.0.1:动态端口/devtools/browser/实例ID",
  "browser_version": "Chrome/版本",
  "transport": "cdp"
}
```

CDP 是 Chrome DevTools Protocol（Chrome 开发者工具协议）。Chrome 使用独立
`--user-data-dir`、动态分配的调试端口和 `about:blank` 启动，不访问业务网站。
Apple Silicon 上即使 Python 运行于 x86 转译模式，也显式启动 ARM Chrome。
下载目录固定为本任务 `artifacts_dir/downloads`，返回字段为 `downloads_dir`。
连接前核对进程身份、目录 inode 和 `DevToolsActivePort`，并读 `/json/version` 比对实例ID。
健康检查直接连接本机，不使用系统 HTTP 代理。

执行者使用已有 Playwright 工具连接返回的 **websocket_url**，例如：

```python
browser = playwright.chromium.connect_over_cdp(connection["websocket_url"])
context = browser.contexts[0]
page = context.pages[0]
```

需要 Playwright MCP 时，使用自行安装并固定版本的服务：

```text
npx @playwright/mcp@<固定版本> --cdp-endpoint <websocket_url> --output-dir <artifacts_dir>
```

由调用者接入专属工具。第一版不会修改 Codex / Claude Code 全局 MCP 配置，也不能让已经运行的
智能体自动热加载新 MCP。无法动态接入时，使用已授权的 Playwright 脚本/命令连接；
只接受原生扩展控制的任务不适用这个 CDP 路径，应改用独占桌面并明确边界。
MCP 的显式截图文件名可能按工作目录解析，调用者仍需确保写入申报的证据目录。

浏览器 ready 只表示连接就绪。响应始终提示 `verification_required`：
环境、实际登录账号、学习实例需要执行者在页面核验。申报不会创建账号、授权或课程。

丢失上下文：先 `list` 找回任务 → `show --probe` → `reconnect --agent-pid`。
同一个进程可重连；换进程时必须确认原 PID + 启动时间对应进程已退出。
原执行者存活时返回 `owner_alive`，不会双重接管。接管成功记录审计事件。
浏览器已退出时返回错误，不把全新浏览器伪装成恢复的页面；清理后新申报。

## 生命周期与回收

状态：starting → ready → stopping → stopped。
启动失败且进程已确认退出：failed；无法确认完成回收：cleanup_required，继续占用资源。
查询额外返回 owner_alive、browser_alive、orphaned、health。
孤立是可恢复状态：执行者退出后不会立即清理，可先由新进程 reconnect 接管。

`stop` 仅终止匹配本工具记录及专属启动参数的浏览器和已识别子进程。
每次启动另带唯一 launch_id，跨进程恢复和清理据此识别本次启动。
先温和停止，5 秒后按进程身份终止剩余进程。没有全局 kill Chrome 命令。
启动器中断后，starting 记录仍会占用；可查找匹配专属目录的浏览器恢复或清理。
`cleanup` 默认只展示候选，`--apply` 才执行；有任一回收失败就返回失败，继续保留占用。

默认保留登录 profile、下载和证据。`purge-profile` 必须显式调用，且要求任务停止、
没有其他任务占用、没有该目录的浏览器进程、目录归属和 inode 均匹配；不会删除 artifacts。
任务审计历史保留。不要在申报、描述、命令行或报告中填写密码和登录令牌。

## 给智能体的使用约定

可将本段作为个人规则入口的引用内容；本次没有自动改写全局技能或 AGENTS.md。

1. 浏览器操作前，通过 browser-lease register 申报全部资源。冲突后修改真实任务资源并重试，
   不通过改账号/环境别名绕过冲突。
2. 只连接任务返回的 websocket_url。并发浏览器模式禁止用 getApp('Google Chrome')、
   自动寻找现有 Chrome、切换其他 profile 或操作共享桌面。
3. 页面核验账号、环境、学习实例后再执行已获授权的业务操作。
4. 状态丢失先查询、探测和重连，不能猜测端口、复用其他任务标签页。
5. 任务结束 stop。不要全局杀进程、删除其他任务目录或自动清空登录资料。

这些规则和注册表用于可信智能体之间的运行协调。相同系统用户、完整 shell 权限仍可绕开申报，
本工具不构成安全沙箱。CDP 仅供本机可信进程使用，具有浏览器控制能力。
desktop 的互斥只覆盖已申报任务，不能阻止用户或未申报程序抢焦点。

## 验证

```bash
uv run pytest -q
uv run pytest -q -m chrome          # 两个真实专用 Chrome；不使用现有登录资料
uv run ruff check browser_lease tests
```

真实浏览器测试在临时目录、无头 Chrome、本地 HTTP 页面中验证隔离和回收，
不访问 Course 环境，不登录，不产生真实 Agent 调用。

本机验证记录见 `verification.json`：30 项测试通过；含双 Chrome 的 Cookie、
本地存储、下载隔离，存储复用、进程接管、启动中断恢复、真实并发申报，
以及独立命令进程执行的 12 步申报/冲突/查询/重连/清理验证。
当时已运行的 3 个日常 Chrome 主进程均保留。可见窗口人工登录和 Linux 尚未实测。

参考：[Chrome 调试独立目录要求](https://developer.chrome.com/blog/remote-debugging-port)、
[Playwright 持久上下文](https://playwright.dev/docs/api/class-browsertype#browser-type-launch-persistent-context)、
[Playwright MCP 参数](https://github.com/microsoft/playwright-mcp#configuration)。
