# DNS-Monitor — 常用网站/App 域名 DNS 解析监测工具

对常用网站、App 对应的域名做 **DNS 解析监测**，解析失败或结果异常时**桌面通知 + 日志提醒**。

核心能力：**系统解析 vs 经代理的干净 DoH 解析对照**，能准确区分
「本地 DNS 被污染/拦截」和「域名真的解析不了」，不误报。
纯只读监测：**不写 hosts、不清缓存、不动系统**，安全无副作用。

## 目录结构

**仓库中（公开，已上传）**：
```
dns-monitor/
├── dns_monitor.py              # 主程序（监测 + 提醒）
├── config.json                 # 运行配置（间隔、代理、DoH、文件路径）
├── .gitignore                  # 屏蔽本地私人清单与运行产物
└── README.md
```

**本地维护（私人清单，被 .gitignore 屏蔽，不上传，需自行创建）**：
```
dns-monitor/
├── domains/                    # 监控域名独立文件夹（仅本地）
│   ├── monitor_domains.txt     #   监测域名清单（每行一个）
│   └── domain_expect_cidr.json #   域名→预期 IP 段映射（判污染用）
└── apps/
    └── apps.txt                #   关注的 App 清单（仅记录，不解析）
```

> 域名、App、预期段都拆成独立文件，方便单独维护，改域名不用动 `config.json`。
> 这些清单属于个人私有配置，**不随仓库上传**。clone 本仓库后，需在本地自行创建 `domains/` 和 `apps/` 目录及对应文件，程序才能正常监测。

## 快速开始

**首次使用（准备私人清单）**：本仓库不含个人域名/App 清单（已屏蔽上传）。clone 后，先在本地创建这两个文件，否则监测列表为空：

```
dns-monitor/domains/monitor_domains.txt   # 每行一个要监测的域名，如 google.com
dns-monitor/domains/domain_expect_cidr.json  # 域名→预期IP段（可选，配了判污染更准）
dns-monitor/apps/apps.txt                 # 关注的App清单（仅记录，可选）
```

**用代理（推荐，能正确判污染）**：先确保本地代理已开启，然后：

```bash
python dns_monitor.py --status      # 只看一次状态（只读，不提醒）
python dns_monitor.py               # 持续监测（默认每 60s 一轮，异常自动提醒）
python dns_monitor.py --no-notify   # 持续监测，只写日志不弹通知
python dns_monitor.py --domains google.com github.com   # 只监测指定域名
python dns_monitor.py --interval 30  # 自定义间隔为 30 秒
```

**代理没开时**，可临时只用系统解析（无法判污染，但仍能抓解析失败/结果变化）：

```bash
python dns_monitor.py --no-doh --status
```

## 它监测什么

**域名（真正做解析监测）**：`domains/monitor_domains.txt` 列表，支持 `*.google.com` 这种通配写法，会自动取基础域 `google.com` 来解析。

**App（仅记录，不解析）**：`apps/apps.txt` 列表。`*.exe` 程序本身不产生 DNS 查询，无法直接解析出 IP，所以只作为「关注的 App」记录下来，方便对着清单核对，**不做实际 DNS 解析**。

## 两套解析如何判定

每个域名同时做两次解析：

| 解析 | 方式 | 作用 |
|------|------|------|
| **系统解析** | 系统 DNS（`socket.getaddrinfo`） | 你实际上网时走的本地 DNS 结果 |
| **干净解析** | 经代理走 DoH（Cloudflare/Google） | 未被污染的基准结果 |

对照判定：

- **疑似污染（POLLUTED，🟠）**：
  - 系统解析失败，但干净解析可用 → 本地 DNS 被污染/拦截；
  - 系统解析成功，但 IP 不在该域名「预期 IP 段」内 → 解析到了错误服务器。
- **真实失败（FAIL，🔴）**：系统与干净解析都失败 → 域名本身真解析不了。
- **需关注（CHECK，🟢）**：未配置预期段的域名，系统解析与干净基准无交集（可能是 CDN 轮询或代理出口差异，不一定是污染，提醒留意）。
- **结果变化（CHANGED，🟡）**：与上一轮系统解析不同（Cloudflare 等 CDN 共享 IP 的抖动不告警）。
- **正常（OK，✅）**：系统解析在预期段内 / 与干净基准一致。

## 提醒规则

- 异常状态（POLLUTED / FAIL / CHANGED / CHECK）触发**桌面通知 + 日志**提醒。
- **提醒冷却**：同一域名默认 15 分钟（`alert_cooldown_sec`）内只提醒一次，避免每轮刷屏。

## 配置项（config.json）

| 配置 | 说明 |
|------|------|
| `monitor_interval_sec` | 监测间隔（秒），默认 60 |
| `resolve_timeout_sec` | 单个域名解析超时（秒），默认 8 |
| `max_rounds` | 最多监测轮数，`0` 表示无限，持续监测用默认 |
| `notify` | 是否允许桌面通知，`true`/`false` |
| `alert_cooldown_sec` | 同一域名提醒冷却时间（秒），默认 900 |
| `log_path` | 日志文件路径，默认同目录 `dns_monitor.log`（已被 .gitignore 屏蔽，不上传） |
| **`proxy`** | 本地代理地址，默认 `http://127.0.0.1:10100`，改成你实际在用的代理 |
| `doh_server` / `doh_fallback` | DoH 服务器（Cloudflare 优先，失败回退 Google） |
| `compare_with_doh` | 是否启用干净 DoH 对照，`true`/`false` |
| `domains_file` | 监测域名清单文件路径（相对脚本目录） |
| `expect_cidr_file` | 域名预期 IP 段 JSON 文件路径 |
| `apps_file` | 关注 App 清单文件路径 |

> **加新域名**：在 `domains/monitor_domains.txt` 加一行即可；建议同时在 `domains/domain_expect_cidr.json` 配预期 IP 段，判污染更准，不配则走「需关注（CHECK）」逻辑。

## 本地化配置（不上传）

`config.json` 里的 `proxy` 是个人代理地址。若要保留个人配置又不上传，可把 `proxy` 等个人项移到 `config.local.json`（已被 `.gitignore` 屏蔽），程序会优先读取它。

## 依赖

- Python 3（建议 3.8+）
- 桌面通知依赖：`winotify`（缺失时自动降级为只写日志，不影响监测）
- 干净基准解析需要**本地代理可用**（走 DoH）；代理不可用时用 `--no-doh` 或关闭 `compare_with_doh`

安装依赖：`pip install winotify`

## 开机自启（可选）

需要长期挂着监测，可在「任务计划程序」里新建一个任务，触发器选「登录时」，操作填：
```
程序: <你的 python 路径>
参数: "<dns-monitor 目录>\dns_monitor.py"
```
