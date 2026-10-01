# 额度透视（quota-lens）

把 Codex / Claude Code 订阅的实际用量，按官方 API 价折算成美元，推算每个额度窗口（5 小时、每周）值多少钱，并监控厂商有没有暗调额度。

本地运行，只读本机日志，只监听 127.0.0.1。

## 运行

```bash
pip install -r requirements.txt
python run.py --open
```

打开 http://127.0.0.1:8787 。首次启动会导入近 90 天的日志（几 GB 日志大约 10 秒），之后每 60 秒增量扫描一次。

参数：
- `--port`
- `--poll-minutes`：Claude Code 空闲时查额度的间隔，默认 30；使用中固定每 3 分钟查一次
- `--history-days`：首次导入多久以内的日志，默认 90
- `--no-notify`：发现额度变化时不弹 Windows 通知

测试：`python -m unittest discover tests`

## 数据从哪来

| | 用量（token） | 额度百分比 |
|---|---|---|
| Codex | `~/.codex/sessions/**/rollout-*.jsonl` 的 `token_count` 事件（`last_token_usage`） | 同一事件里的 `rate_limits`（5 小时 / 周窗口的 `used_percent`、`resets_at`、`plan_type`）；本地空闲时另查 `chatgpt.com/backend-api/wham/usage` |
| Claude Code | `~/.claude/projects/**/*.jsonl` 里 `type=assistant` 的 `message.usage` | 轮询 `api.anthropic.com/api/oauth/usage`（Claude Code 的 `/usage` 用的同一个非公开接口） |

两个额度接口都用对应 CLI 已存的登录 token（`~/.codex/auth.json`、`~/.claude/.credentials.json`），只发往官方域名。程序绝不刷新 token：刷新会轮换 refresh token，让 CLI 掉登录。token 过期时请运行一次对应的 CLI。

价格来自 [models.dev](https://models.dev)，每天刷新一次，含长上下文分档价和 fast 模式价；`quotalens/prices_seed.json` 是离线兜底快照。所有历史用量都用同一张价格表重新计价，价格表更新不会造成趋势上的假跳变。

## 怎么判断有没有被暗调

**1. 等价花费**：新鲜输入 × 输入价 + 缓存读 × 缓存读价 + 缓存写 × 缓存写价 + 输出 × 输出价。
- Claude 的 1 小时缓存写入按输入价 2 倍计（日志里 `cache_creation.ephemeral_1h_input_tokens`），5 分钟档按 1.25 倍。
- Codex 的 `input_tokens` 包含缓存部分，先扣掉再按输入价算。

**2. 归属到窗口**
- Codex：每次请求和它所消耗窗口的 `resets_at` 写在同一条日志里，按此精确归属。切换账号、用重置券都不会串。
- Claude：日志里没有这层信息，按时间归属（窗口起点 = 重置时间 − 窗口长度）。前提是只登录一个 Claude 账号，并且没有用 API key 跑 `claude-*` 模型。

**3. 识别外部消耗**：本地日志之外的消耗（别的设备、云任务、claude.ai 网页聊天）会让百分比上涨，本地却没有花费。如果不排除，会被误判成「额度收紧」。
- 把每个窗口按百分比上涨拆成若干步，用汇率算出每一步的预期涨幅。
- 某一步涨幅 ≥ 3% 且超过预期的 3 倍再加 2 个点，或者本地完全没花钱却涨了 ≥ 2%，就判为外部消耗，从已用百分比里扣掉。
- 外部消耗占到 15% 以上的窗口，不参与汇率拟合和趋势判断。

**4. 模型汇率**：实测额度消耗和 API 价格不成正比。在 Codex Plus 上，同样 $1，gpt-6-luna 吃掉的额度约是 gpt-6-astra 的 3 倍。所以用近 60 天的干净窗口做非负最小二乘：已用% ≈ Σ 汇率[模型] × 花费[模型]。

**5. 趋势和告警**：每个窗口折算成「全用主力模型时值多少钱」，消除模型组合的影响。
- 5 小时窗口：最近 72 小时的窗口和之前 3 周对比，最近至少 3 个、之前至少 5 个。
- 每周窗口：最新 1 个和之前 2～4 个对比。
- 用的是中位数之比。阈值取 20% 和「2.5 × 基线离散度 × 样本量修正」中较大的一个，超过阈值就判为收紧或放宽，页面横幅加 Windows 通知提醒，同一方向 24 小时内只提醒一次。
- 实测单个窗口的正常波动约 ±20%：50% 以上的调整 1～2 个窗口就能看出来，25% 左右的调整要 1～2 天。

**6. 规则变化记录**：接口 / 日志的字段增减、订阅方案变化、新增限额窗口、窗口提前重置，都从数据里自动找出来，作为解读趋势跳变的背景。

## 证据留存

- `data/quotalens.db` 保留全部解析后的用量和额度快照，不随 CLI 清理日志而丢失。
- 额度接口的原始响应归档在 `raw_response` 表里（已去掉邮箱、账号 ID；内容不变时只延长时间范围）。
- 每天自动备份到 `data/backups/`，保留最近 14 份。
- Claude Code 默认删除 30 天前的会话记录。想多留原始日志，可在 `~/.claude/settings.json` 里设置 `"cleanupPeriodDays"`。

## 已知局限

- 本地日志看不到的消耗只能识别、扣除，无法精确还原；被它污染的窗口不参与判断。
- Claude 日志不记录账号和 key：切换 Claude 账号，或者用 Anthropic API key 跑 `claude-*` 模型，这些用量都会混进当前账号的窗口。
- 经 CC Switch 转发到第三方的模型（deepseek、glm 等）会自动识别为「第三方」，不计入额度、不计价。
- 百分比只有整数精度，窗口刚开始时推算区间很宽。
- 额度接口是非公开的，随时可能变；接口字段一变化，规则变化记录里会出现提示。

## 自定义价格

复制 `prices_override.example.json` 为 `prices_override.json`，补上没价格的模型，或用 `{"same_as": "某个模型"}` 让它按别的模型计价。改完点页面上的「立即同步」，或重启服务生效。

## 结构

```
quotalens/
  collect_codex.py   Codex 日志 → usage / quota_snapshot / usage_window
  collect_claude.py  Claude Code 日志 → usage
  claude_quota.py    轮询 Claude 额度百分比
  codex_quota.py     本地空闲时查询 Codex 额度百分比
  pricing.py         models.dev 价格、模型名规范化、计价
  calibrate.py       窗口实例、外部消耗识别、模型汇率、趋势与变化判断
  events.py          规则变化记录
  notify.py          Windows 桌面通知
  stats.py           今天 / 7 天 / 30 天汇总
  service.py         后台定时任务（自适应轮询、分析缓存、告警、备份）
  api.py             FastAPI 接口
web/                 页面（ECharts）
```
