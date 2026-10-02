# How much my Claude

[English](README.en.md)

把 Codex / Claude Code 订阅的实际用量，按官方 API 价折算成美元，推算每个额度窗口（5 小时、每周）值多少钱，并监控厂商有没有悄悄调整额度。

- **本地运行**：只读本机上 Codex CLI、Claude Code 留下的日志，数据存在你自己电脑上，不上传任何地方。
- **不用单独登录**：直接用这两个命令行工具已经存好的登录信息查额度，只发往官方接口，从不刷新或改动它。
- 桌面窗口 + 系统托盘，也可以只开浏览器版。界面支持中文和英文。

> 本项目与 Anthropic、OpenAI 没有任何关联。额度接口是官方 CLI 自己在用的非公开接口，随时可能变化或失效。

## 安装

需要 Python 3.10 或更新版本。

**推荐用 pipx**（装在独立环境里，命令随处可用）：

```bash
pipx install git+https://github.com/Deco025/quota-lens.git
```

```bash
how-much-my-claude-install
```

第二条命令会创建快捷方式：Windows 在桌面，macOS 在「~/Applications」，Linux 在应用菜单。以后双击「How much my Claude」打开，也可以直接运行 `how-much-my-claude`。

**从源码运行**：

```bash
git clone https://github.com/Deco025/quota-lens.git
```

```bash
cd quota-lens && pip install -r requirements.txt
```

```bash
python install.py
```

各系统的额外要求：
- **Windows**：窗口用系统自带的 Edge WebView2，不用另装。
- **macOS**：第一次查 Claude 额度时，系统会问是否允许读取钥匙串里的「Claude Code-credentials」，点「始终允许」。
- **Linux**：窗口需要 GTK 或 Qt 后端，按 [pywebview 的安装说明](https://pywebview.flowrl.com/guide/installation.html) 装好（例如 `pip install "pywebview[qt]"`）。托盘需要桌面环境支持。窗口实在装不上，也可以只用浏览器版。

## 连接你的账号

**不需要在这个工具里登录。** 它读的是 Codex CLI 和 Claude Code 自己留下的东西：

| | 用量（token） | 额度百分比 | 登录信息 |
|---|---|---|---|
| Codex | `~/.codex/sessions/**/rollout-*.jsonl` | 日志里自带；本地空闲时另查 `chatgpt.com/backend-api/wham/usage` | `~/.codex/auth.json` |
| Claude Code | `~/.claude/projects/**/*.jsonl` | 轮询 `api.anthropic.com/api/oauth/usage`（Claude Code 的 `/usage` 用的同一个接口） | Windows / Linux：`~/.claude/.credentials.json`；macOS：钥匙串 |

所以只要装好它们、登录过、用过一次就行。缺什么页面顶部会直接提示，比如「找不到 Claude Code 的登录信息，在终端运行 claude 登录一次」。底部「运行状态」里能看到两边的连接情况。

`CODEX_HOME`、`CLAUDE_CONFIG_DIR` 环境变量改过位置的，这里也会跟着用。

## 使用

- **关掉窗口只是缩到托盘**，后台继续采集。单击托盘图标打开窗口，右键菜单有「立即同步」「开机自启」「退出」。macOS 上不放托盘，关窗口就退出。
- 已经在运行时再打开一次，会把现有窗口调到前面，不会多开。
- **开机自启**：托盘菜单里勾选，或运行 `how-much-my-claude-install --startup`。登录后在后台运行，不弹窗口。Claude 的额度百分比只能在运行时采集，建议开启。
- 退出：托盘菜单「退出」，或页面底部「运行状态」里的「停止服务」。
- 删除快捷方式：`how-much-my-claude-install --remove`。
- 只要浏览器版：`how-much-my-claude-server --open`（源码里是 `python run.py --open`），然后访问 http://127.0.0.1:8787 。

首次启动导入近 90 天的日志（几 GB 日志大约 10 秒），之后每 60 秒增量扫描一次。更早的日志可以在「数据管理」里一键导入。

**设置**（页面底部「设置」）：界面语言、要不要弹系统通知、Claude 空闲时多久查一次额度、判为「被调」的最小幅度、「换了模型」的判定比例、首次导入多久的日志。

## 隐私与安全

- 服务只监听 127.0.0.1，并拒绝别的网站发来的请求（检查 Host 和 Origin）。
- 只往这几个地方发请求：`api.anthropic.com`、`chatgpt.com`（查额度，带你本机已有的登录 token）、`models.dev`（下载公开的价格表）。没有统计上报，也没有其他联网。
- 登录 token 只读不写、从不刷新（刷新会让 CLI 掉登录），不写进日志和数据库。归档的接口原始响应里去掉了邮箱、账号 ID。
- 图表库已经打包在项目里，页面不从网上加载任何东西。

## 数据存在哪

| 系统 | 位置 |
|---|---|
| Windows | `%LOCALAPPDATA%\how-much-my-claude` |
| macOS | `~/Library/Application Support/how-much-my-claude` |
| Linux | `~/.local/share/how-much-my-claude` |

设置环境变量 `QUOTALENS_DATA_DIR` 可以改到别处。里面有数据库 `quotalens.db`、每日备份 `backups/`、日志 `quota-lens.log`、手填价格 `prices_override.json`。旧版本放在源码目录 `data/` 下的数据，第一次启动新版本时会自动复制过来，原来的不删。

- 数据库**永久保存**，不会自动清理。每天自动备份，保留最近 14 份。
- 页面底部「数据管理」可以：
  - 按订阅方案选**参与分析 / 只保留**：只保留的数据不动，但不参与汇率、趋势和告警，适合以前用过的别的账号。
  - **删除**某个方案的全部窗口数据：删除前自动整库备份到 `backups/before-delete-*.db`（不参与轮换）。删掉的数据以后重读日志也不会再导回来；以后再用这个方案，新数据照常记录。
  - **导入全部历史日志**。
- 单个窗口可以在「窗口历史」或窗口详情里**排除**（比如那个窗口里你在别的设备上也用过），随时可以恢复。

## 怎么判断有没有被调

页面上的结论、数字和模型名都是程序每次从你自己的数据里算出来的，前端只负责把它们填进句子里。

**1. 等价花费**：新鲜输入 × 输入价 + 缓存读 × 缓存读价 + 缓存写 × 缓存写价 + 输出 × 输出价。价格来自 [models.dev](https://models.dev)，每天刷新，含长上下文分档价和 fast 模式价；`quotalens/prices_seed.json` 是离线兜底快照。所有历史用量都用同一张价格表重新计价，价格更新不会造成趋势假跳变。
- Claude 的 1 小时缓存写入按输入价 2 倍计（日志里 `cache_creation.ephemeral_1h_input_tokens`），5 分钟档按 1.25 倍。
- Codex 的 `input_tokens` 包含缓存部分，先扣掉再按输入价算。

**2. 归属到窗口**
- Codex：每次请求和它所消耗窗口的 `resets_at` 写在同一条日志里，按此精确归属。切换账号、用重置券都不会串。
- Claude：日志里没有这层信息，按时间归属（窗口起点 = 重置时间 − 窗口长度）。前提是只登录一个 Claude 账号，并且没有用 API key 跑 `claude-*` 模型。

**3. 识别外部消耗**：本地日志之外的消耗（别的设备、云任务、claude.ai 网页聊天）会让百分比上涨，本地却没有花费。如果不排除，会被误判成「额度收紧」。
- 把每个窗口按百分比上涨拆成若干步，用汇率算出每一步的预期涨幅。
- 某一步涨幅 ≥ 3% 且超过预期的 3 倍再加 2 个点，或者本地完全没花钱却涨了 ≥ 2%，就判为外部消耗，从已用百分比里扣掉。
- 外部消耗占到 15% 以上的窗口，不参与汇率拟合和趋势判断。

**4. 模型汇率**：额度消耗和 API 价格不成正比（例如作者的 Codex Plus 账号上，同样 $1，gpt-6-luna 吃掉的额度约是 gpt-6-astra 的 3 倍）。所以用近 60 天的干净窗口做非负最小二乘：已用% ≈ Σ 汇率[模型] × 花费[模型]。每个人的汇率都从自己的数据里算出来。

**5. 趋势和告警**：每个窗口折算成「全用主力模型时值多少钱」，消除模型组合的影响。
- 5 小时窗口：最近 72 小时的窗口和之前 3 周对比，最近至少 3 个、之前至少 5 个。
- 每周窗口：最新 1 个和之前 2～4 个对比。
- 用中位数之比。阈值取「设置」里的最小幅度（默认 20%）和「2.5 × 基线离散度 × 样本量修正」中较大的一个，所以数据本身波动大时会自动放宽。超过阈值就判为收紧或放宽，页面横幅加系统通知提醒，同一方向 24 小时内只提醒一次。
- 作者的数据里单个窗口的正常波动约 ±20%：50% 以上的调整 1～2 个窗口就能看出来，25% 左右的调整要 1～2 天。
- **跨断档**：停用或退订一段时间再续上时，近几周没有可比的窗口，就拿停用前最后几个窗口当基线（5 小时窗口取 10 个，每周窗口取 4 个），卡片上注明「跨断档对比，中间停了 N 天」。不然断档期间发生的调整会被当成新常态吸收掉。趋势图把断档折叠成一小截，前后的点都看得到。

**6. 换了模型**：从日志里的模型名自动判断，不用改代码。最近窗口的花费里，如果来自基线期也在用的模型（基线期占比 ≥10% 才算「在用」）不到一半（比例可在「设置」里改），前后就没法比：汇率回归会把额度变化当成「新模型本来就贵」吸收掉。这时检验章显示「换了模型」，不下结论、不报警，只给按 API 等价金额的粗略对比。新模型用满几周后，会自动和它自己比。

**7. 规则变化记录**：接口 / 日志的字段增减、订阅方案变化、新增限额窗口、窗口提前重置，都从数据里自动找出来，作为解读趋势跳变的背景。

## 自定义价格

页面底部「模型价格」列出你用过的模型查到的价格（也可以看价格表里的全部模型），单位是美元 / 百万 token。
没有价格的模型，在数据目录里建一个 `prices_override.json`（格式见项目里的 `prices_override.example.json`）补上，或用 `{"same_as": "某个模型"}` 让它按别的模型计价。改完点「立即同步」生效。

## 已知局限

- 本地日志看不到的消耗只能识别、扣除，无法精确还原；被它污染的窗口不参与判断。
- Claude 日志不记录账号和 key：切换 Claude 账号，或者用 Anthropic API key 跑 `claude-*` 模型，这些用量都会混进当前账号的窗口。
- 经 CC Switch 等工具转发到第三方的模型（deepseek、glm 等）会自动识别为「第三方」，不计入额度、不计价。
- 百分比只有整数精度，窗口刚开始时推算区间很宽。
- 额度接口是非公开的，随时可能变；接口字段一变化，规则变化记录里会出现提示。
- macOS 和 Linux 上的窗口、托盘、快捷方式还没在真机上充分测试，遇到问题欢迎提 issue。

## 开发

```bash
pip install -r requirements.txt httpx
```

```bash
python -m unittest discover tests
```

- 页面文字加了新的中文后，运行 `python tools/i18n_keys.py`，它会列出 `quotalens/web/i18n.js` 里还缺英文的条目。
- 应用图标是代码画的：`python tools/make_icon.py`。

```
quotalens/
  collect_codex.py   Codex 日志 → usage / quota_snapshot / usage_window
  collect_claude.py  Claude Code 日志 → usage
  claude_quota.py    查 Claude 额度百分比（含 macOS 钥匙串）
  codex_quota.py     本地空闲时查 Codex 额度百分比
  pricing.py         models.dev 价格、模型名规范化、计价
  calibrate.py       窗口实例、外部消耗识别、模型汇率、趋势、跨断档与换模型判断
  events.py          规则变化记录
  service.py         后台定时任务（自适应轮询、分析缓存、告警、备份）
  api.py             本地接口
  settings.py        页面「设置」里的选项
  paths.py           数据目录、旧数据迁移
  desktop.py         桌面版：原生窗口 + 系统托盘，单实例
  server.py          浏览器版
  install.py         快捷方式与开机自启（Windows / macOS / Linux）
  notify.py          系统通知（Windows / macOS / Linux）
  i18n.py            托盘、通知等后端文字的英文
  web/               页面（ECharts 已打包在 vendor/）
desktop.pyw / run.py / install.py   从源码运行时的入口
tools/               图标生成、翻译检查
```

## 许可

[MIT](LICENSE)。第三方内容见 [THIRD_PARTY.md](THIRD_PARTY.md)。
