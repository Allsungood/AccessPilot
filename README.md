# AccessPilot

> 让 ChatGPT / Discord / X（Twitter）等平台在国内网络下**能打开、能登录、能长期正常用**的客户端管理器。

AccessPilot 不是又一个"翻墙内核"，而是一层**面向可用性的控制与管理平面**：代理协议栈复用成熟开源内核
[mihomo](https://github.com/MetaCubeX/mihomo)（Clash.Meta），本工具负责内核不擅长的事 —— 订阅转换、
平台定向分流、DNS 防污染、系统集成、连通性诊断与图形化控制台。

全部代码 **零第三方依赖**（只用 Python 标准库），因为目标用户往往连 PyPI 都装不上。

---

## 目录

- [为什么是这个方案](#为什么是这个方案)
- [快速开始](#快速开始)
- [⚠️ 系统代理的风险与恢复](#-系统代理的风险与恢复务必先读)
- [命令速查](#命令速查)
- [针对目标平台的分流设计](#针对目标平台的分流设计)
- [免节点直连加速(可选, 有明确边界)](#免节点直连加速可选-有明确边界)
- [你还需要一个"节点"](#你还需要一个节点)
- [红杏 · 桌面客户端（一键开关 + 系统托盘）](#红杏--桌面客户端一键开关--系统托盘)
- [红杏 · 安卓端](#红杏--安卓端)
- [图形控制台](#图形控制台)
- [验证情况（实测数据）](#验证情况实测数据)
- [常见问题](#常见问题)
- [项目结构](#项目结构)
- [参考的开源项目](#参考的开源项目)
- [合规声明](#合规声明)

---

## 为什么是这个方案

"加速访问 ChatGPT/Discord/X"要真正可用，需要同时解决 5 件事，缺一不可：

| 问题 | 只做代理的结果 | AccessPilot 的做法 |
|---|---|---|
| 加密与协议 | ✅ 内核解决 | 复用 mihomo，支持 SS/SSR/VMess/VLESS-Reality/Trojan/Hysteria2/TUIC |
| 流量分流 | ❌ 全局代理导致国内站点变慢 | 8 个策略组 + 内联平台规则 + 远程规则集，AI/社交/流媒体分组独立 |
| DNS 污染 | ❌ 能连上但登录失败、资源加载不出来 | fake-ip + 境外 DoH + `nameserver-policy` 对目标平台强制境外解析 |
| 应用覆盖 | ❌ 只代理浏览器，Discord 桌面端/Steam 走直连 | TUN 模式接管全部流量（含 UDP） |
| 可用性排查 | ❌ 打不开也不知道哪一环坏了 | 出口 IP/机房属性/ChatGPT 区域检测 + 10 个目标平台连通性体检 |

一句话：**内核负责"通"，AccessPilot 负责"能用"和"好用"。**

---

## 快速开始

前置：Windows 10/11 + Python 3.9+（Linux/macOS 亦可，TUN 需 root）。

**第 0 步（重要）：把 `accesspilot` 注册成全局命令。** 否则下面所有命令都要写成
`python -m accesspilot ...`，直接敲 `accesspilot` 会提示"不是内部或外部命令"。

```powershell
cd AccessPilot
python -m accesspilot install-cmd     # 往 PATH 目录写一个包装器
accesspilot --version                 # 验证: 应输出 AccessPilot 1.0.0
```

> 这一步刻意不用 `pip install -e .`：本机 Python 布局特殊（scripts 目录被重定向）时，
> pip 会报 `No pyvenv.cfg file` 而失败。写一个几行的包装器更可靠，也不需要构建工具链。
> 如果提示找不到命令，关掉重开一个终端窗口（PATH 需要刷新）。

```powershell
# 1) 安装内核(下载 mihomo + wintun + GeoIP 数据, 自动多镜像回退)
accesspilot init

# 2) 零成本方案: 抓公开免费节点并测速(不碰系统代理)
accesspilot free auto

# 3) 先只启动内核, 不动系统代理 —— 确认没问题再开
accesspilot start --no-sysproxy
accesspilot test                      # 看 Discord / X 是否通过

# 4) 确认可用后, 再打开系统代理(此时浏览器才会走代理)
accesspilot proxy on
```

有订阅的话把第 2 步换成 `accesspilot sub add "https://你的订阅地址"` 即可。

Windows 用户也可以直接运行 `scripts\install.ps1` 完成安装 + 注册全局命令 + 创建桌面快捷方式。

### 不想装 Python？直接下安装包

上面那套需要机器上有 Python。给"只想双击一下"的人，发布页提供了打包好的安装程序
（**用户级安装，不需要管理员权限，不会弹 UAC**）：

```powershell
红杏-Setup-v1.0.0.exe                 # 双击: 图形向导(选目录 -> 安装 / 卸载 / 检查更新)
红杏-Setup-v1.0.0-full.exe            # 同上, 但包里带着内核, 装完即用、不用再跑 init
红杏-Setup-v1.0.0.exe --silent        # 静默安装(装完不启动)
红杏-Setup-v1.0.0.exe --dir D:\Apps   # 指定安装目录
红杏-Setup-v1.0.0.exe --uninstall     # 卸载(保留节点/配置)
红杏-Setup-v1.0.0.exe --uninstall --purge   # 卸载并删除全部用户数据
红杏-Setup-v1.0.0.exe --update        # 检查 GitHub 最新版并就地更新
```

* **装到哪**：程序在 `%LOCALAPPDATA%\Programs\Hongxing\`，
  数据（节点/订阅/配置/内核）在 `%LOCALAPPDATA%\AccessPilot\`。
* **程序和数据是刻意分开的**：更新只换程序、绝不动数据；卸载默认也只删程序，
  重装一次不会把你攒下来的节点和配置清掉。想连数据一起删才加 `--purge`。
* 装完会创建开始菜单和桌面快捷方式，并注册到**设置 → 应用**，在那里可以直接卸载。
* 更新会在替换主程序前校验 SHA256（发布方没提供校验值时**默认拒绝更新**），
  详见 [packaging/README.md](packaging/README.md) §11.3。

`test` 的输出长这样：

```
出口信息
  IP      : 203.0.113.7  Japan Tokyo
  运营商  : Example Cloud Inc.
  ChatGPT : 可用
  CF trace: ip=203.0.113.7 loc=JP warp=off

目标平台连通性
平台           结果  延迟      说明
ChatGPT 网页版 通过  312 ms    HTTP 200
OpenAI API     通过  288 ms    HTTP 401
Discord        通过  265 ms    网关可用 (wss://gateway.discord.gg)
X (Twitter)    通过  301 ms    HTTP 200
Google         通过  180 ms    HTTP 204
...
```

---

## ⚠️ 系统代理的风险与恢复（务必先读）

`accesspilot start` 默认会开启**系统代理**（把 Windows 的代理指向 `127.0.0.1:7890`）。
这是代理工具的正常行为，但有一个必须知道的失效模式：

> **如果内核因为任何原因退出（崩溃、被杀、被杀毒软件拦截），而系统代理还开着，
> 这台机器上所有网站都会打不开** —— 连本来就该直连的 B 站、百度也一样，
> 因为浏览器把全部流量丢给了一个已经不存在的端口。

本项目为此加了三道防护，并且都用测试锁死了：

| 防护 | 行为 | 实测 |
|---|---|---|
| **自愈** | 只要再调用一次任意 `accesspilot` 命令（或重启后第一次调用），就会检测到"内核已死但代理还开着"并自动还原 | 1.0 秒内恢复 ✅ |
| **看门狗** | `start` 会附带启动一个监控进程，内核一消失就立刻还原系统代理 | 2 秒内恢复 ✅ |
| **不擅自改动** | `free auto` 等自动化命令一律**不碰**系统代理，只在你明确执行 `proxy on` / `start` 时才改 | 已用测试锁定 ✅ |

手动恢复（任何时候都可用）：

```powershell
accesspilot stop          # 停内核 + 还原系统代理
accesspilot proxy off     # 只还原系统代理
```

如果连 `accesspilot` 都跑不起来，用系统设置手动关：
**设置 → 网络和 Internet → 代理 → 手动设置代理 → 关闭**。

> 建议：第一次用先用 `accesspilot start --no-sysproxy` 只启动内核，确认节点可用后
> （`accesspilot test`）再执行 `accesspilot proxy on` 打开系统代理。

---

## 命令速查

| 命令 | 说明 |
|---|---|
| `init` | 下载内核、TUN 驱动、GeoIP 数据 |
| `core install\|update\|version\|path` | 内核管理（支持 `--version` 指定版本） |
| `sub add <url> [name]` | 添加订阅（自动识别 Clash YAML / base64 分享链接） |
| `sub list\|show\|use\|rm\|update` | 订阅管理（含流量与到期时间） |
| `config build\|test\|show\|path` | 生成 / 校验 / 查看 mihomo 配置 |
| `config set-port 7897` | 端口冲突时换端口 |
| `start [--tun] [--no-sysproxy]` | 启动（默认开系统代理，`--tun` 全局接管） |
| `stop` / `restart` / `status` / `log -f` | 生命周期与日志 |
| `proxy on\|off\|status\|env` | 系统代理与终端环境变量 |
| `tun on\|off\|status` | TUN 模式 |
| `node list\|groups\|delay [--select-first]\|use <组> <节点>` | 策略组与测速 |
| `test [--site chatgpt] [--json]` | 目标平台连通性诊断 |
| `ip` | 当前出口 IP / 机房属性 |
| `doctor` | 环境自检（14 项） |
| `dashboard --open` | 图形控制台 |
| `ui install\|open` | 下载并使用内核自带面板 metacubexd |
| `mirror jsdelivr\|raw\|ghproxy` | 规则集下载镜像 |
| `accel on\|off\|status` | 免节点直连加速开关（IP 优选，面向 GitHub 系） |
| `free sources\|fetch\|test\|clean\|auto` | 公开免费节点：抓取 + 并发测速 + **平台级验证** + 只留真正可用的（无需 VPS / 信用卡） |
| `accel bench [域名]` | 实测优选效果，并给出"能否靠换 IP 加速"的结论 |
| `warp register` | 免费注册 Cloudflare WARP 出口（无需账号 / 邮箱 / 信用卡） |
| `warp status\|endpoint\|license\|rm` | WARP 配置管理 |
| `warp chain <节点名>` | 让 WARP 握手借道某个节点（本机 UDP 被墙时的补救手段） |
| `ensure` | 确保内核在运行（已在运行则直接返回，供保活/开机自启调用） |
| `autostart on\|off\|status` | Windows 计划任务保活：内核不在就自动拉起（默认每 5 分钟，不会碰系统代理） |

---

## 免节点直连加速（可选, 有明确边界）

这是把 [FastGithub](https://github.com/creazyboyone/FastGithub) 的思路工程化实现的一部分：
**不修改系统 DNS、不安装根证书、不做中间人**，而是靠"选一个真实可用的 IP"来绕过封锁。

```
应用 --SOCKS5--> [accesspilot accel] --TCP--> 选出的最优真实 IP
                     ↑ DoH 取真实 IP + TCP/TLS 并发探测排序 + 结果缓存
```

### 它能做什么、不能做什么（实测结论）

网络封锁有三种手段，**只有第一种能靠换 IP 绕过**：

| 封锁手段 | 表现 | 换 IP 能否解决 | 本机实测（2026-09） |
|---|---|---|---|
| ① 特定 IP 段被丢弃 / DNS 污染 | 有的 IP 通、有的不通 | ✅ 能 | `raw.githubusercontent.com` 的 .108 不可达，.109/.110/.111 正常 |
| ② SNI 阻断 | TCP 能连，TLS 握手被切断 | ❌ 不能 | `chatgpt.com` 用 Cloudflare 真实 IP + 正确 SNI，TLS **全部失败**；`x.com` 6 个候选各试 3 次仅 1 次偶然握手成功，随后 HTTP 立刻超时 |
| ③ 应用层地区封禁 | TLS 正常但服务端返回 403 | ❌ 不能 | OpenAI 按出口 IP 封禁；你的出口是中国 IP，必然被拒 |

对应的工具行为：

```powershell
accesspilot accel bench
# 域名                       结论    最优 IP          TCP   TLS
# raw.githubusercontent.com  可加速  185.199.109.133  78ms  208ms
# github.com                 可加速  20.205.243.168   80ms  378ms
# github.githubassets.com    可加速  185.199.111.215  78ms  217ms
# codeload.github.com        可加速  20.205.243.165   71ms  269ms
# ghcr.io                    可加速  20.205.243.164   80ms  391ms
```

**因此：ChatGPT / Discord / X 不在加速范围内，`accel` 对它们无效** —— 这不是没实现，
而是物理上做不到。这三个平台必须有境外节点。

### 什么情况下值得开启

只有在 **GitHub 资源对你确实不可用** 时才值得开。本机实测（各 5 次请求）：

| 域名 | 直连成功率 / 中位延迟 | 经 AccessPilot | 结论 |
|---|---|---|---|
| raw.githubusercontent.com | 5/5, 611ms | 5/5, 788ms | 直连本来就通，加速反而多一跳 |
| github.githubassets.com | 5/5, 250ms | 5/5, 357ms | 同上 |
| codeload.github.com | 5/5, 1063ms | 5/5, 1021ms | 基本持平 |

也就是说：**如果你的网络能直连 GitHub，就不要开 `accel`**（默认关闭是对的）。
它真正的用武之地是 GitHub 被完全阻断的网络环境。

### 免节点模式

即使一个节点都没有，AccessPilot 仍然能提供价值（分流 + 19 万条广告拦截规则）：

```powershell
accesspilot accel on      # 开启 IP 优选
accesspilot start         # 会自动创建"仅直连加速"配置档, 默认全部直连
```

此时 `🚀 节点选择` 默认指向 `DIRECT`，只有命中 `⚡ 直连加速` 规则的域名才走 IP 优选 ——
不会越权代理你的其它流量。

---

## 针对目标平台的分流设计

### 策略组

| 策略组 | 类型 | 默认 | 承载 |
|---|---|---|---|
| 🚀 节点选择 | select | ♻️ 自动选择 | 兜底、被墙的通用站点 |
| ♻️ 自动选择 | url-test | 最快节点 | 每 300s 自动测速切换 |
| 🤖 AI 服务 | select | 🚀 节点选择 | ChatGPT / OpenAI / Claude / Gemini / Grok |
| 💬 社交平台 | select | 🚀 节点选择 | Discord / X / Telegram |
| 📺 流媒体 | select | 🚀 节点选择 | YouTube / Netflix / Spotify |
| 🎯 全球直连 | select | DIRECT | 国内站点、局域网 |
| 🛑 广告拦截 | select | REJECT | 广告与追踪域名 |
| 🐟 兜底分流 | select | 🚀 节点选择 | 未命中任何规则的流量 |

> AI 与社交独立成组的意义：ChatGPT 对出口 IP 地区/信誉敏感，Discord 语音需要 UDP 友好的节点，
> 你可以给它们单独挑节点，而不影响其它流量的速度。

### 规则优先级（自上而下）

1. **内联平台规则**（约 150 条，写死在配置里）
   `AI_EXTRA + OpenAI` → 🤖 / `Discord + X + Telegram` → 💬 / 常见被墙站点 → 🚀
   内联的意义：**即使所有远程规则集都下载失败，目标平台依然分流正确。**
2. **远程规则集**（rule-provider）
   `openai / discord / twitter / telegram / google / youtube / netflix / spotify / github / microsoft`
   来源 [blackmatrix7/ios_rule_script](https://github.com/blackmatrix7/ios_rule_script)（完整分类）
   与 [Loyalsoldier/clash-rules](https://github.com/Loyalsoldier/clash-rules)（`reject` / `direct` / `proxy` / `gfw` / `cncidr`）。
3. **尾部兜底**：广告拦截 → 局域网直连 → 国内直连 → `GEOIP,CN,DIRECT` → `MATCH,🐟 兜底分流`

### DNS 防污染（关键）

```yaml
dns:
  enhanced-mode: fake-ip            # 假 IP 应答, 避免真实 DNS 泄漏
  default-nameserver: [223.5.5.5, 119.29.29.29]      # 只用于解析 DoH 服务器域名
  nameserver: [阿里 DoH, 腾讯 DoH]                    # 国内域名
  fallback: [Cloudflare DoH, Google DoH]             # 境外域名
  fallback-filter: {geoip: true, geoip-code: CN}     # 污染结果自动丢弃
  nameserver-policy:
    "geosite:cn":                 国内 DoH
    "geosite:geolocation-!cn":    境外 DoH
    "+.openai.com/+.chatgpt.com/+.discord.com/+.x.com/...": 境外 DoH   # 显式强制
```

同时启用 **sniffer**（TLS SNI / HTTP Host / QUIC 嗅探），确保即使客户端只给了 IP，
也能按域名走对策略组。

### 内核兼容性细节

- mihomo ≥ 1.19 已移除 `global-client-fingerprint`，改为**逐节点**注入 `client-fingerprint: chrome`，
  提升 TLS 握手兼容性（对 ChatGPT 尤其明显）。
- 策略组之间禁止环状引用（`🎯 全球直连` 不能引用 `🚀 节点选择`），否则内核直接拒绝加载。
- rule-provider 的 `format` 必须与文件真实格式一致。Loyalsoldier 的文件虽以 `.txt` 结尾，
  内容却是 Clash YAML payload —— 标成 `text` 会让内核把 `payload:` 当规则解析，
  **静默丢弃整份规则集**。项目里有单元测试锁定这一约定。

---

## 你还需要一个"节点"

AccessPilot 是客户端管理器，**它本身不提供节点**。要让 ChatGPT / Discord / X 真正可用，
必须有境外出口。按"无信用卡"这个前提排序：

**A. 零成本：Cloudflare WARP（先试这个）**

WARP 的注册接口不需要邮箱、账号、付款方式，一条命令搞定：

```powershell
accesspilot warp register     # 注册并作为节点 ☁️ WARP 加入配置
accesspilot start
accesspilot node delay        # 看 ☁️ WARP 是"xx ms"还是"超时"
```

**本机实测结果（2026-09）：WARP 不可用。** 内核日志显示握手包发出去后
再无回应（`Sending handshake initiation` 之后没有任何后续），说明 UDP 被网络丢弃 ——
这是 WARP 免费版在中国大陆的常见状况。你的网络很可能同样如此，但**值得花 10 秒试一次**，
因为不同地区/运营商差异很大。

如果握手被墙、而你又已经拿到了别的节点，可以借道：

```powershell
accesspilot warp chain "香港 01"    # WARP 握手先经该节点出去, 从而拿到 Cloudflare 出口 IP
```

**B. 支付宝 / 微信付款的境外 VPS**

没有信用卡也能买：Vultr、搬瓦工(BandwagonHost) 等面向中国用户的厂商通常支持支付宝/微信
（以各站下单页实际显示为准）；腾讯云/阿里云的香港、新加坡轻量应用服务器也支持微信/支付宝支付，
但需要实名认证。买到 Ubuntu 实例后：

```bash
bash scripts/server-install.sh                       # 在 VPS 上执行
accesspilot sub add "vless://..." --name my-vps --use   # 本机执行
accesspilot start --tun && accesspilot test
```

**C. 有订阅/共享节点**
直接 `sub add`，支持 Clash YAML 与 base64 分享链接两种格式。

**D. 零成本：公开免费节点（`accesspilot free`）**

GitHub 上有一批每天更新的公开免费节点仓库。本项目把它们做成了"抓取 → 并发测速 → 只留可用的"一条命令：

```powershell
accesspilot free auto      # 抓取 9 个公开源 + 369 个节点并发测速 + 自动选最快
```

本机实测（2026-09）：

| 环节 | 结果 |
|---|---|
| 抓取 9 个公开源 | 616 个节点 → 去重 **369** 个 |
| 并发测速（48 并发） | 用时 **32 秒** |
| 真正可用 | **16 个（4%）** |
| Discord / X / Google / OpenAI API | **✅ 全部可用** |
| **ChatGPT 网页版** | **❌ 全部 403** |

**为什么 ChatGPT 不行**：这些节点的出口要么其实在中国（节点标签是假的），要么是韩国机房
被成千上万人共用的 IP，OpenAI 直接以 HTTP 403 拒绝 —— 这是 **IP 信誉封禁，不是地区限制**，
换节点解决不了。免费节点里不存在"干净"的出口 IP。

所以结论是：**没有 VPS / 没有信用卡时，Discord 和 X 可以免费搞定；ChatGPT 不行**，
它需要一个你自己独占的、没被滥用的出口 IP（VPS 或付费机场的 ChatGPT 解锁节点）。

> ⚠️ **安全提醒**：节点运营方能看到你的全部流量去向（HTTPS 内容看不到，但访问了哪些站点一清二楚）。
> **不要在用免费节点时登录 ChatGPT / 邮箱 / 银行账号** —— 这类共享节点上，你的账号被风控甚至被盗的风险显著升高。
> 自建 VPS 是唯一能完全掌控的一条路。

---

## 红杏 · 桌面客户端（一键开关 + 系统托盘）

给"不想知道什么是代理"的人用的版本。一个大圆钮，开就是开。

```powershell
python -m accesspilot gui          # 源码运行: 主窗口 + 系统托盘
python -m accesspilot gui --no-tray  # 只开窗口(怀疑托盘把主循环搞挂时用来排错)
```

打包成单文件 exe（用户机器上不需要装 Python）：

```powershell
python packaging/build.py          # 产物: dist/红杏.exe
```

### 界面

主窗口左侧是大圆钮（已连接 / 未连接）、当前节点与延迟、工作模式三选一
（智能分流 / 全局 / 直连）、设置三项（开机自启 / TUN 全局接管 / 自动切换节点）；
右侧两个标签页：「节点」列出节点、延迟、是否为当前出口、**是否实测能上 ChatGPT**，
双击某一行就切过去；「平台自检」跑 10 个平台的真实连通性。

### 设计取舍

* **托盘是可选增强**。托盘走 Win32 原生接口，在远程桌面、被安全软件拦截、
  Explorer 没在跑的环境下会挂不起来 —— 那不该让整个客户端打不开，所以
  托盘失败就退化成普通窗口。
* **关窗口 = 隐藏到托盘**（有托盘时）。用户以为关掉了，其实还在后台保护网络，
  这是这类客户端该有的行为。没有托盘时必须真退出，否则会留下一个用户杀不掉的幽灵进程。
* **单实例**用 Windows 命名互斥体。双击两次图标，第二次只会把已有窗口叫到前台 ——
  两个客户端同时改系统代理设置是真的会互相打架。
* **窗口关闭时的系统代理一定会被还原**。这是底线：任何异常路径下都不能给用户
  留下一份指向已经退出的代理的设置。

### 窗口守护（一个兜底，不是修复）

双击 exe 之后，窗口正常出现约 5 秒，然后会被最小化 —— 而**进程还活着**。
用户看到的是"双击了没反应"，对一键客户端来说这是最致命的失败模式。

已经排除的：裸 Tk 窗口稳定不动；裸 Tk + 同样的 DPI 感知 + 同样的几何也稳定不动；
`--no-tray` 一样复现；插桩包住 `geometry/state/iconify/withdraw/lift/focus_force`
证明红杏自己一次都没调过。**根因至今没有定位。**

所以现在的做法是启动后 15 秒内每 250ms 检查一次，被最小化就恢复回来，
每次恢复打一行日志 —— **如果哪天根因修掉了，这里应该永远是 0 次**。

⚠️ **已知且无法消除的取舍**：用户在启动后 15 秒内**主动**按最小化，会被弹回来。
因为"用户按的最小化"和"这个故障"在系统看来**完全一样**，实测：

```
正常           iconic=False  rect=(200,200)      816x639
Tk iconify()   iconic=True   rect=(-32000,-32000) 237x39
SW_MINIMIZE    iconic=True   rect=(-32000,-32000) 237x39   ← 点最小化按钮
SW_RESTORE     iconic=False  rect=(200,200)      816x639
```

两条最小化路径给出的 `iconic` + `rect` **逐字节相同**。本进程是 per-monitor-v2
DPI 感知、缩放 1.5，所以 `-32000/1.5 = -21333`、`237/1.5 = 158`、`39/1.5 = 26`
—— 排查时如果看到 `rect=(-21333,-21333) 158x26`，那**就是一次普通的最小化**，
不是别的什么异常状态。想靠"最近有没有键鼠输入"区分也不行：移动鼠标同样算输入，
那样反而会在真故障时误判成用户干的而袖手旁观。

### 已知限制

* **高 DPI 屏已做 per-monitor-v2 DPI 感知**（`theme.enable_dpi_awareness()`，
  三级降级：`SetProcessDpiAwarenessContext` → `shcore.SetProcessDpiAwareness`
  → 放弃），画布坐标与 Treeview 行列都按 `dpi/96` 缩放，文字不再被拉伸。
  **提醒排查的人**：声明感知之后，进程看到的就是**物理**分辨率了，而截图工具
  未必 —— 量窗口位置时别把两套坐标混用（这一条曾经让我误判过一次）。
* **保活任务不会推翻用户主动关闭。** 计划任务 `AccessPilotEnsure` 每 5 分钟跑一次
  `accesspilot ensure`；它会读 `runtime/user_intent.json`，若本次开机内用户主动
  关过就什么都不做（逃生口 `accesspilot ensure --force`）。判据只在这**一次开机内**
  有效，重启后失效 —— 否则「开机自启」会被误伤。详见 `accesspilot/intent.py`。
* **启动耗时约 1 秒。** 实测窗口 0.5~1.2 秒出现、完整骨架 0.75~2.0 秒、数据填满 2~3 秒。
  物理下限约 1.1 秒：Python 解释器 ~0.15s + 首次加载 Tcl/Tk ~0.28s + 构建骨架 ~0.25s
  + 布局首绘 ~0.4s。`tk.Tk()` 那 0.28 秒是加载 DLL，代码层面绕不过去。
* **开启/重启内核时会多跑一次配置校验**（约 2~3 秒，6000 节点规模）。
  `config.render()` 现在自带"写候选 → 校验 → 通过了才原子替换"，而
  `process.start()` 之后还会再校验一次同一份文件。这是刻意留的冗余：
  校验的对象是刚生成的文件，代价是几秒，收益是**磁盘上永远不会躺着一份坏配置**
  （见过一次：坏配置在盘上、内核用的还是内存里的旧配置，表面正常；等内核一重启
  就直接断网，而现象离起因差了好几个小时）。
* **exe 未做代码签名**，Windows SmartScreen 会提示“未知发布者”，需要用户点“仍要运行”。

---

## 红杏 · 安卓端

Kotlin + Compose 的 Android 客户端，和桌面端同一套节点、同一个 mihomo 内核，
界面也是一样的一键开关。代码在 `android/`，构建说明与真机实测结论见
**[android/README.md](android/README.md)**。

架构一句话：`VpnService` 建 TUN → 把文件描述符交给随包的 mihomo 子进程
（配置里写 `tun.file-descriptor`）→ 转发与分流全归内核。

值得单独说的一点：**`ProcessBuilder` 拿不到这个 fd**（libcore 在 execve 前
无条件关闭所有 ≥4 的 fd，而且**不靠 `FD_CLOEXEC`**，所以 `detachFd()` 无效），
因此有一个自写的 native 启动桥 `app/src/main/cpp/hongxing_launcher.c`。
三判据真机实测与完整证据都在 `android/README.md` 里。

---

## 图形控制台

```powershell
python -m accesspilot dashboard --open     # 内置控制台, http://127.0.0.1:9099
python -m accesspilot ui install           # 可选: metacubexd 专业面板, http://127.0.0.1:9090/ui
```

内置控制台提供：运行状态、系统代理/TUN 开关、出口 IP 与 ChatGPT 区域、策略组切换与一键测速、
10 个平台的一键体检、订阅管理（添加/切换/更新/删除）、内核日志。

安全设计：只监听 `127.0.0.1`；所有写操作要求自定义头 `X-AccessPilot: 1` 并校验 Host/Origin，
防止恶意网页通过 CSRF 操纵本机代理设置。

---

## 验证情况（实测数据）

本机（Windows 10 19045 / Python 3.11.9 / mihomo v1.19.31）实测结果：

**单元测试 421 项**（25 个测试文件，全程离线，不打真实网络）

```powershell
python runtests.py                       # 计数随 tests/ 增长, 以本次输出为准
python -m unittest discover -s tests     # 等价写法
```

> **判定标准是退出码和 `OK` / `FAILED`，不是上面这个数字**：`tests/` 一直在长，
> README 里先后出现过 308 / 219 / 380 / 421 四个数，互相矛盾。
> 顺带一条真实观察：本机内存被挤到只剩 1 GB 左右时，`tests/test_installcmd.py`
> 里那个"从别的目录调用包装器"的用例会因为子进程 90 秒超时而偶发失败；
> 单独跑它 0.5 秒就过 —— 那是机器负载，不是回归。

测试隔离靠 `tests/__init__.py` 把数据目录重定向到临时目录（`ACCESSPILOT_HOME`），
所以跑测试**不会**动到你正在用的 `runtime/` 和系统代理。

**端到端测试 31 项全部通过**（`python tests\e2e_check.py`）

用一个本地 SOCKS5 服务器充当"节点"，跑通真实完整链路：

| 验证项 | 实测结果 |
|---|---|
| 内核安装（含多镜像回退） | mihomo v1.19.31 + wintun.dll + GeoIP/GeoSite |
| 配置经内核自检 `mihomo -t` | successful，无 error / warning |
| 规则集真实加载 | `reject` 190037 条、`direct` 111169 条、`proxy` 27092 条、`cncidr` 9741 条、`openai` 35 条 … 共 20 个 provider，零解析告警 |
| 代理链路 | 经 `127.0.0.1:7890` 取回 617 KB HTTPS 内容，HTTP 200 |
| 分流决策（读内核日志） | `x.com` → 💬 社交平台、`discord.com` → 💬 社交平台、`chatgpt.com` → 🤖 AI 服务 |
| 失效节点处理 | url-test 未选中 Dead-Node |
| 系统代理 | 开启/关闭/还原注册表均正常 |
| 节点测速（内核→节点→探测地址） | 60~126 ms |
| TUN | `[TUN] Tun adapter listening at: AccessPilot([198.18.0.1/30]), mtu: 9000, ip stack: Mixed` |
| 清理与还原 | 进程停止、系统代理无残留 |

> TUN 测试为**创建适配器但不接管路由**（`auto-route: false`），以免测试期间中断当前网络连接。
> 日常使用请以管理员身份运行 `accesspilot start --tun`。

**修复记录**（都是先用真实内核跑出来的，不是纸上推演）：

1. mihomo ≥1.19 移除 `global-client-fingerprint` → 改为逐节点注入。
2. 策略组环状引用（🎯 全球直连 ↔ 🚀 节点选择）→ 内核拒绝加载，已拆环。
3. `fallback-filter.geosite` 已废弃 → 迁移到 `nameserver-policy`。
4. **Loyalsoldier `.txt` 实为 YAML payload，误标 `format: text` 导致整份规则集被静默丢弃** → 修正并加防回归测试。
5. SSR 分享链接字段错位（obfs/protocol/method）→ 修正，被单元测试捕获。
6. 残留内核进程占用 9090 端口导致新实例"启动成功但接口 401" → 增加启动前端口预检与残留自动清理。
7. 直连加速器冷启动要 10~16 秒，而内核给上游的拨号超时只有 5 秒 → 改为"内置 IP 池快速路径 + 后台异步完整优选 + 启动预热"，首包从超时变为可用。
8. 配置里无条件写 `external-ui` 会让内核在启动时尝试联网下载面板并刷错误日志 → 改为仅当面板已存在于本地时才声明。
9. WARP 的 WireGuard 需要 X25519，而 Python 标准库没有椭圆曲线运算 → 用纯 Python 实现 RFC 7748 的 Montgomery 阶梯，并用官方向量 + 1000 次迭代测试 + DH 对称性三重校验，避免为单个功能引入第三方密码学依赖。
10. 单元测试会读写用户**真实**的 `~/.accesspilot`：本机注册 WARP 后，"策略组只应包含订阅节点"等 5 项断言立刻失败 → 增加 `tests/__init__.py` 强制把数据目录隔离到临时目录，并让每个测试模块显式导入它（`unittest discover -s tests` 不会自动执行包的 `__init__`，这个坑很隐蔽）。
11. **严重事故**：`free auto` 擅自开启了系统代理，而它在测速过程中内核退出，系统代理仍指向死端口 7890 → 用户整台机器断网（连 B 站都打不开）。修复：`free auto` 改为显式 `system_proxy=False` 绝不触碰系统代理；并新增**自愈**（任意命令调用时检测并还原）与**看门狗**（内核退出即还原）两道防护，全部用单元测试锁死。
12. 内核对外的 `/group/{name}/delay` 返回的是 `{"节点": 151}` 扁平整数格式，而代码只解析 `{"delay": 151}` 对象格式 → 369 个节点的测速结果被全部丢弃，表现为"节点全挂了"，Web 控制台的测速按钮同样是坏的。已兼容两种格式，并改用自研并发测速（内核那个接口实测只返回部分节点）。
13. **"测速快" ≠ "能用"**：`test` 曾显示 X 通过(HTTP 200)但浏览器里永远打不开 —— 因为节点慢到每个请求 15 秒, 资源域根本加载不动。修复：`test` 的 X 检查增加静态资源(`abs.twimg.com`)请求；`free auto` 增加**平台级验证**阶段(对最快的节点逐个实测 X 主页+静态资源+Discord, 只保留全通过的), 并加 `--min-ms` 阈值。
14. **一颗老鼠屎坏一锅汤**：新源里一个带 `auth_aes128_md5` 密码的 SSR 节点让 1242 个节点的整个配置热重载失败(HTTP 400)。修复：抓取时过滤内核不支持的节点参数；`reload_config` 改为**先本地校验、失败绝不提交**(正在运行的内核不受影响)；`build_config` 再做一层防御性过滤。
15. `free auto` 在内核已运行时不会热重载新节点 → 新抓的节点一个都测不到。已修复, 并补上 `ensure`(幂等启动)与 `autostart`(Windows 计划任务每 5 分钟保活, 应对本机环境下内核随会话结束被回收的问题)。
16. **端到端测试会真改系统代理注册表**：测试被外部超时打断时来不及还原，用户整台机器断网（真实发生两次）→ e2e 改为默认**绝不碰真实注册表**（`E2E_TOUCH_SYSPROXY=1` 才能开启真机验证），并在测试前后断言注册表保持原样。
17. 1235 节点配置热重载时内核会阻塞控制接口 10~30 秒，客户端 20 秒超时就误判失败 → `reload` 改为"提交 + 轮询恢复"，且 `reload_config` **先本地校验、失败绝不提交**。
18. `free auto` 选中"测速最快"而非"平台验证最快"的节点（出现过 14 秒延迟的节点被选中）→ 按验证实测延迟排序，并把策略组改成"🚀→♻️自动选择(每5分钟重选)→各子组"的链条。
19. **新开源渠道**：GitHub 聚合源之外，还有一批"每日更新的免费节点网站"（freeclashnode.com / clashnode.cc / nodefree.org / v2rayshare.com 等）。实测这些站点**从国内可直连**，文件按日期发布、URL 可预测。接入后节点池 3,353 → 6,574、存活 7 → 48。抓取分两级：直接按日期拼文件地址 + 抓文章页正则捞订阅文件（换站点也能自适应）。
20. 大源动辄上万个节点，全塞进内核会让配置加载变慢、内存暴涨 → 新增 **TCP 预筛**：先用裸连接淘汰"服务器都连不上"的（14,621 → 6,574，125 秒，淘汰 8,047 个），UDP 类协议（hysteria2/tuic）跳过预筛以免误杀。
21. 系统代理开着但**一个可用节点都没有**时，它只会拖慢本来能直连的站点（实测 GitHub 8.5s → 直连 0.7s）→ `free auto` 现在会在"无验证通过节点"时**自动关闭系统代理**（关闭是安全方向，只恢复连通性），并在日志里说明原因。

---

## 常见问题

**Q: `test` 显示 ChatGPT 失败，提示"IP 所在地区不受 OpenAI 支持"**
换节点。OpenAI 不支持中国大陆/香港/澳门/俄罗斯/伊朗/朝鲜/古巴/叙利亚/白俄罗斯/委内瑞拉。
`accesspilot ip` 可看出口地区，控制台会直接标红。

**Q: 网页能打开但登录时转圈 / 报错**
多为 DNS 污染或 IP 信誉问题。依次试：`accesspilot node delay` 换个节点 →
确认 `config show` 里 `nameserver-policy` 包含目标域名 → 换住宅 IP 节点（`ip` 命令会提示是否机房 IP）。

**Q: Google 登录时提示"此电话号码无法用于进行验证"**
**这不是网络问题**（代理是通的：实测 `accounts.google.com` 正常返回 200）。
Google 长期不接受中国大陆 +86 号码用于**新账号**验证，这是它的账号政策，任何代理都改不了。

可行的路：用已有的老 Google 账号（老账号一般不要求重新验证）／用受支持地区的号码／
注册时点"尝试其他方式"，有时会给出备用验证途径。
不推荐接码平台：那些号码基本已被用烂，即使注册成功，之后被 Google 封号的概率极高，也违反其服务条款。

另外要注意：免费节点的出口 IP 是成千上万人共用的，Google 的反滥用系统会因此**升级验证要求**
（更容易强制要手机号、甚至直接拒绝）。所以——

> ⚠️ **不要用免费节点登录重要账号**（Google / 邮箱 / 银行 / ChatGPT）。
> 节点由陌生人运营，能看到你的全部流量去向；共享 IP 也会让风控升级甚至导致账号被标记。
> 要登录重要账号时：先 `accesspilot proxy off` 用回自己的网络，或换成自建的干净节点。

**Q: 用免费节点会不会影响我的账号安全**
会。免费节点运营方能看到你访问了哪些站点（HTTPS 内容看不到，但域名/时间/流量一目了然），
且共享 IP 会被各种风控系统打上标记。所以：浏览、看视频、刷 X 没问题；
**登录敏感账号不要用**。详见上一问。

**Q: Discord 能发消息但语音不通**
语音走 UDP。请用 `start --tun`（TUN 才能接管 UDP），并确保节点本身支持 UDP（SS/VLESS-Reality/Hysteria2 一般支持）。

**Q: 开了代理后国内网站变慢**
规则未命中。检查 `doctor` 的"规则集镜像"是否可达；不可达时执行
`accesspilot mirror ghproxy` 后 `accesspilot config build && accesspilot restart`。
即使规则集全部失败，内联平台规则与 `GEOIP,CN` 仍能保证基本分流。

**Q: 端口 7890/9090 被占用**
`accesspilot config set-port 7897 --api-port 9097`，然后 `accesspilot restart`。
若占用者是残留内核，`start` 会自动清理。

**Q: 系统代理关了但浏览器还是上不了网**
`accesspilot proxy off` 会还原注册表原值；若曾异常退出，执行 `accesspilot stop` 清理残留内核。

**Q: 不想装 Python**
用发布页上的安装包：`红杏-Setup-v1.0.0.exe`（轻量）或 `红杏-Setup-v1.0.0-full.exe`
（自带内核，装完即用），双击安装、创建快捷方式、可在"设置 → 应用"里卸载。

要自己从源码打包也可以 —— 但**必须走项目自己的打包链路**：

```powershell
python packaging/build.py     # 产物: dist/红杏.exe（版本资源、图标、资源收集都在 spec 里）
```

> 不要写 `pyinstaller -F -n accesspilot accesspilot\cli.py` 这种一行命令。它绕开了
> `packaging/entry.py`：没有那句修复 `sys.stdout` 的代码，`accesspilot/util.py:23` 在
> **import 阶段**就会 `AttributeError: 'NoneType' object has no attribute 'isatty'`
> （双击后"闪一下，什么都没发生"）；也没有 `-m accesspilot` 参数归一化（看门狗/计划
> 任务靠它）、没有版本资源、没有资源收集，产物里 `dashboard.html` 和托盘图标都是缺的。
> 详见 [packaging/README.md](packaging/README.md)。

---

## 项目结构

```
AccessPilot/
├── accesspilot/
│   ├── cli.py            # 命令入口与终端渲染(含计划任务注册)
│   ├── control.py        # ★ 界面与引擎之间**唯一**的接缝, 见文件头三条硬约定
│   ├── intent.py         # 记录"用户最后一次点的是开还是关", 供保活让位
│   ├── config.py         # mihomo 配置生成(DNS/TUN/策略组/规则) + 校验后原子替换
│   ├── rules.py          # 平台定向规则、规则集目录、镜像策略
│   ├── freenodes.py      # 公开免费节点抓取/测速/平台级验证
│   ├── health.py         # 节点失效自动切换 + 单实例(命名互斥体)
│   ├── sharelink.py      # 7 种协议分享链接解析
│   ├── subscription.py   # 订阅抓取/解析/消重名/本地存储
│   ├── miniyaml.py       # 零依赖 YAML 子集解析器
│   ├── coreinstall.py    # 内核下载安装(多镜像回退/解压/GeoIP)
│   ├── process.py        # 进程守护、端口预检、日志
│   ├── sysproxy.py       # 系统代理、终端环境变量、TUN 前置检查
│   ├── accel.py          # 免节点直连加速(本地 IP 优选 + SOCKS5 出站)
│   ├── api.py            # mihomo REST API 客户端
│   ├── diag.py           # 平台体检、出口 IP、ChatGPT 区域、DNS 污染检测
│   ├── webgui.py         # 本地控制台(含 CSRF 防护)
│   ├── gui/              # 红杏桌面端(Tkinter, 零依赖)
│   │   ├── app.py        #   主窗口 + 窗口守护
│   │   ├── theme.py      #   配色/缩放/DPI 感知
│   │   ├── tray.py       #   纯 ctypes 托盘(自有消息泵线程)
│   │   └── icon.py       #   手写 ICO 生成(不依赖 Pillow)
│   └── assets/dashboard.html
├── android/              # 红杏安卓端(Kotlin + Compose), 见 android/README.md
│   ├── app/src/main/java/com/accesspilot/hongxing/
│   │   ├── core/         #   VpnService / 内核管理 / 配置组装 / REST 客户端
│   │   └── ui/           #   Compose 界面(含 FakeEngine 以便预览)
│   ├── app/src/main/cpp/hongxing_launcher.c   # fork+execve 启动桥(为了拿到 TUN fd)
│   └── tools/            #   fetch_core.py / build_assets.py(补齐未入库的产物)
├── packaging/            # 打包链路（主程序 exe + Setup 安装程序）
│   ├── hongxing.spec / entry.py / build.py        # 主程序 -> dist\红杏.exe
│   ├── installer.spec / installer.py / build_installer.py   # 安装程序 -> dist\红杏-Setup-v<版本>[-full].exe
│   └── smoke_test.py / README.md
├── scripts/
│   ├── install.ps1         # Windows 一键安装(内核 + 全局命令 + 桌面快捷方式)
│   └── server-install.sh   # VPS 服务端一键部署(Xray VLESS-Reality)
├── tests/                  # 测试文件(离线可跑, 不碰真实网络; 数量随开发增长)
│   ├── test_control.py / test_intent.py / test_config_dedupe.py / ...
│   └── e2e_check.py        # 端到端实测(真实内核 + 真实链路)
└── accesspilot.cmd       # 免安装启动器(CRLF, 见 .gitattributes)
```

运行时数据（可通过 `ACCESSPILOT_HOME` 重定向）：
Windows `%LOCALAPPDATA%\AccessPilot\`，Linux/macOS `~/.accesspilot/`
—— `core/` 内核、`profiles/` 订阅档、`runtime/config.yaml` 生成配置、
`runtime/user_intent.json` 开关意图、`logs/core.log` 日志。

---

## 参考的开源项目

本项目的"代理能力"完全来自以下项目，AccessPilot 只做编排与体验层：

- [MetaCubeX/mihomo](https://github.com/MetaCubeX/mihomo) — 代理内核（Clash.Meta），本工具的运行主体
- [MetaCubeX/metacubexd](https://github.com/MetaCubeX/metacubexd) — 可选的专业 Web 面板
- [MetaCubeX/meta-rules-dat](https://github.com/MetaCubeX/meta-rules-dat) — GeoIP / GeoSite 数据
- [Loyalsoldier/clash-rules](https://github.com/Loyalsoldier/clash-rules) — 分流规则集（reject/direct/proxy/gfw/cncidr）
- [blackmatrix7/ios_rule_script](https://github.com/blackmatrix7/ios_rule_script) — 平台级规则集（OpenAI/Discord/Twitter/…）
- [XTLS/Xray-core](https://github.com/XTLS/Xray-core) — 自建节点服务端（VLESS-Vision-Reality）
- [SagerNet/sing-box](https://github.com/SagerNet/sing-box) — 同级别的另一内核，设计上大量借鉴
- [WireGuard/wintun](https://www.wintun.net/) — Windows TUN 驱动
- [Dreamacro/clash](https://github.com/Dreamacro/clash) — 原版 Clash，本项目的配置格式源于此

---

## 合规声明

本项目是一个**网络代理客户端管理工具**，与浏览器、VPN 客户端属同类软件。请知悉：

- 请遵守你所在国家/地区的法律法规以及所访问平台的服务条款；
- 请勿用于任何未经授权的网络访问、攻击或数据窃取行为；
- 各平台的账号风控由其自行判定，因出口 IP 信誉导致的封号风险由使用者自行承担；
- 本项目不含任何内置节点或订阅，作者不提供、不运营任何代理服务。

MIT License.
