# IB Gateway 安装与配置

这份文档手把手把 IB Gateway 装好、API 设置好，让本工具能从它读你的持仓和期权链。

## 前置

- 一个 Interactive Brokers（盈透）账号
- macOS 或 Windows（Linux 也行，本文以 macOS 截图为主，步骤一致）

## 1. 下载

打开 <https://www.interactivebrokers.com/en/trading/ibgateway-stable.php>，
选 "Stable" 版本，下载对应你系统的安装包：

| 系统 | 文件名样式 |
|---|---|
| macOS | `ibgateway-stable-macosx-x64.dmg` |
| Windows | `ibgateway-stable-windows-x64.exe` |
| Linux | `ibgateway-stable-linux-x64.sh` |

安装就是一路下一步，没坑。

## 2. 启动并登录

启动 IB Gateway，登录界面里有两个选项：

| 模式 | 用途 |
|---|---|
| **Live** | 真实账户 |
| **Paper** | 模拟账户（推荐先用这个测试） |

填用户名 + 密码登录。登录成功后，主窗口会显示 **API** 状态，类似：

```
API: Listening on port 4001 (or 4002 for paper)
```

> 如果你要同时连两个账户：开两个 IB Gateway 实例（不同的安装目录或不同的
> 账号 profile），每个用不同端口。本工具的 `config/accounts.yaml` 支持
> 多 entry。

## 3. 配置 API（关键步骤）

主菜单 → **Configure** → **Settings** → **API** → **Settings**：

按下面这张表勾选：

| 选项 | 设置值 | 为什么 |
|---|---|---|
| **Enable ActiveX and Socket Clients** | ✅ 勾选 | 不勾本工具连不上 |
| **Read-Only API** | ✅ 勾选（**强烈推荐**） | 哪怕本工具有 bug 也下不了单 |
| **Socket port** | `4001`（Live）/ `4002`（Paper） | 默认值，本工具默认连 4001 |
| **Master API client ID** | 留空 | 本工具用 7878 / 7879 等独立 client_id |
| **Trusted IPs** → 加一行 | `127.0.0.1` | 只允许本机连 |
| **Allow connections from localhost only** | ✅ 勾选 | 双重保险 |
| **Bypass Order Precautions for API Orders** | ❌ 不勾 | 本工具是 Read-Only，不需要 |

点 **OK** 保存。

> ⚠️ 如果 **Read-Only API** 勾不上（灰色），说明你账户被设了"完全 API
> 权限"。Read-Only 是更严格的限制，**勾上更安全**。

## 4. 关闭自动登出（可选但推荐）

主菜单 → **Configure** → **Settings** → **Lock and Exit**：

| 选项 | 推荐值 |
|---|---|
| Auto restart | ✅ 启用，时间设晚上 11:55（IBKR 服务器 daily reset 之前） |
| Auto logoff | 关掉或者设成同一时间 |

IBKR 服务器每天会强制断一次连接，开 Auto restart 让 Gateway 自己重连。

## 5. 验证连接

回到本工具目录，跑：

```bash
uv run python scripts/test_ib_connect.py
```

预期输出（实际账号代码会不同）：

```
✓ Connected to 127.0.0.1:4001 (account UXXXXXXX)
  Server version: 178
  Connection time: 2026-04-27 12:00:00
```

报错的常见情况：

| 报错 | 原因 | 解决 |
|---|---|---|
| `Connection refused` | Gateway 没开 / 没登录 | 启动 Gateway 并登录 |
| `Couldn't connect after 1 second` | API 没开 | 回到 §3 检查 |
| `clientId X is already in use` | 端口被另一个程序占了 | 改 `config/accounts.yaml` 里 `client_id` 换个值（比如 7980） |
| `Not connected: account code is invalid` | 账号代码填错 | 在 Gateway 主界面右上角能看到真实账号代码（U 开头），填进 `config/accounts.yaml` |

## 6. 日常使用注意

- **每天交易前**：先开 Gateway 并登录，再启动本工具
- **Gateway 偶尔会要求二次验证（IBKey）**：这是正常的安全措施，按提示在
  手机 IBKey App 上点确认就行
- **看到 "Daily reset at HH:MM"**：那是 IBKR 服务器的例行重启，Gateway
  会自动重连，本工具会断开后自动重试

## 7. 常见问题

**Q: 我可以不开 Gateway，直接用 TWS 吗？**
A: 可以。TWS 默认端口 7496（Live）或 7497（Paper）。把
`config/accounts.yaml` 里 `port` 改成对应端口即可。但 TWS 内存占用比
Gateway 大很多，建议长跑用 Gateway。

**Q: 为什么需要 client_id？**
A: IBKR 允许多个程序同时连一个 Gateway，client_id 是身份识别。本工具
默认用 7878，如果你同时还在用 ib_insync / ib_async 的别的脚本，给它们
不同的 client_id 避免冲突。

**Q: Paper Trading 账户够用吗？**
A: 期权链数据 Paper 账户也能拉，但会有 15 分钟延迟。要实时报价必须
开 Live + 订阅期权数据包（Options Bundle，约 $4.5/月）。
