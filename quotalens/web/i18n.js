"use strict";

// 页面文字的英文版。写法：t("中文原文 {变量}", { 变量 })；中文原文就是键。
// 界面是中文，或者这里查不到对应英文时，直接用中文原文。
// 加了新的中文后跑 python tools/i18n_keys.py，会列出这里还缺的条目。

const EN = {
  // 时间
  "{d} 天 {h} 小时": "{d}d {h}h",
  "{h} 小时 {m} 分": "{h}h {m}m",
  "{m} 分钟": "{m} min",
  "刚刚": "just now",
  "{n} 分钟前": "{n} min ago",
  "{n} 小时前": "{n} h ago",
  "{n} 天前": "{n} days ago",
  "5 小时": "5-hour",
  "每周": "Weekly",
  "30 天": "30-day",
  "{from}～{to}": "{from} – {to}",

  // 状态和检验章
  "稳定": "Stable",
  "可能被收紧": "may be tightened",
  "可能被放宽": "may be loosened",
  "数据不足": "Not enough data",
  "主力模型变了": "Main model changed",
  "不参与分析": "Not analyzed",
  "未见调整": "NO CHANGE",
  "疑似收紧": "TIGHTENED?",
  "疑似放宽": "LOOSENED?",
  "样本不足": "TOO FEW",
  "换了模型": "NEW MODEL",

  // 连接状态
  "找不到 Claude Code 的登录信息（{where}），所以查不到 Claude 的额度百分比。在终端运行 claude 并登录一次，然后点「立即同步」。":
    "Can't find Claude Code's login ({where}), so Claude quota percentages can't be read. Run claude in a terminal and sign in once, then click \"Sync now\".",
  "Claude Code 的登录已过期，额度百分比暂时查不到。在终端随便运行一次 claude，它会自己续上，然后点「立即同步」。":
    "Claude Code's login has expired, so quota percentages can't be read for now. Run claude once in a terminal (it refreshes itself), then click \"Sync now\".",
  "读取 Claude Code 的登录信息失败（{where}）。": "Failed to read Claude Code's login ({where}).",
  "找不到 Codex 的 ChatGPT 登录（{where}）。用量照样能从日志里读，只是空闲时查不了额度；运行 codex login 可以补上。":
    "Can't find Codex's ChatGPT login ({where}). Usage is still read from the logs, but quota can't be checked while idle; run codex login to fix.",
  "Codex 的登录太久没刷新了。运行一次 codex，它会自己续上。": "Codex's login hasn't been refreshed for a long time. Run codex once and it will renew itself.",
  "读取 Codex 的登录信息失败（{where}）。": "Failed to read Codex's login ({where}).",
  "还没找到 Codex 或 Claude Code 的使用记录": "No Codex or Claude Code usage found yet",
  "这个工具读的是它们在本机留下的日志，自己不登录任何账号、不上传数据。装好 Codex CLI 或 Claude Code、登录并用过一次后，点右上角「立即同步」。":
    "This tool reads the logs those CLIs leave on this computer. It never signs in to anything and uploads nothing. Install Codex CLI or Claude Code, sign in, use it once, then click \"Sync now\" at the top right.",
  "{tool} 的日志目录：{dir}": "{tool} log folder: {dir}",

  // 告警横幅
  "{tool} {window}窗口{state} {pct}%": "{tool} {window} window {state} by {pct}%",
  "折合 {model}：最近 {rn} 个窗口 {recent}，之前 {bn} 个窗口 {baseline}":
    "In {model} terms: recent {rn} windows {recent}, previous {bn} windows {baseline}",

  // 现在
  "已重置，下次使用时开始新窗口": "Reset — a new window starts on next use",
  "剩余 {pct}%": "{pct}% left",
  "预计 {when} 用完": "Runs out ~{when}",
  "照目前速度，重置时约用到 {pct}%": "At this pace you'll reach about {pct}% by reset",
  "预计可撑到重置": "Lasts until reset",
  "这个窗口里本地用掉的模型，按官方 API 价折算；约值 = 整个窗口用满大约值多少":
    "Local model usage in this window at official API prices; worth = roughly what a full window is worth",
  "已花": "Spent",
  "/ 约值 {cap}": "/ worth ~{cap}",
  "本地日志以外的消耗：别的设备、云任务或网页聊天": "Usage not in local logs: other devices, cloud tasks or web chat",
  "外部消耗 {pct}%": "External {pct}%",
  "已用 {pct}%，窗口时间已过 {elapsed}%（小三角）": "{pct}% used, {elapsed}% of the window's time passed (triangle)",
  "，照目前速度到重置时约 {pct}%（斜纹）": ", about {pct}% by reset at this pace (stripes)",
  "{span}后重置": "Resets in {span}",
  "没找到 Claude Code 的登录信息": "Claude Code login not found",
  "Claude Code 登录已过期，运行一次 claude 即可": "Claude Code login expired — just run claude once",
  "读取 Claude Code 登录信息失败": "Failed to read the Claude Code login",
  "等待第一次额度查询…": "Waiting for the first quota check…",
  "用一次 Codex 后就会出现": "Shows up after you use Codex once",

  // 额度有没有被调
  "较之前 {pct}": "{pct} vs before",
  "全用 {model} 时一个窗口约值": "One window is worth about this much if spent all on {model}",
  "（最近 {n} 个窗口中位）": " (median of the last {n} windows)",
  "（最近 1 个窗口）": " (latest window)",
  "已记录 {n} 个窗口，攒到 2 个用量 5% 以上的窗口后开始判断": "{n} windows recorded; judging starts once 2 windows reach 5% usage",
  "还没有足够的窗口数据。": "Not enough window data yet.",
  "按 API 等价金额粗看，一个窗口之前约 {before}、现在约 {after}（{pct}），仅供参考。":
    "Roughly, in API-equivalent dollars, a window was worth about {before} and now about {after} ({pct}) — for reference only.",
  "前后主力模型不同（{before} → {after}），额度变化和模型本身的差异分不开，这次不下结论。":
    "The main model changed ({before} → {after}), so a quota change can't be told apart from the models' own differences. No verdict this time.",
  "等新模型用满几周，会自动和它自己比。": " Once the new model has a few weeks of history, it will be compared with itself automatically.",
  "跨断档对比：中间停了 {days} 天，和停用前（{range}）的 {n} 个窗口比。断档期间如果被调过，这里能看出来。":
    "Across a break: you stopped for {days} days; compared with the {n} windows before it ({range}). A change made during the break shows up here.",
  "近期可比的窗口不够，基线往前取到 {range}。": "Not enough recent windows to compare, so the baseline reaches back to {range}.",
  "◀ 收紧": "◀ tighter",
  "放宽 ▶": "looser ▶",
  "前后模型不同，这是按 API 等价金额的粗比": "Different models before and after; this is a rough API-dollar comparison",
  "最近 {rn} 个窗口中位 {recent}，之前 {bn} 个窗口中位 {baseline}": "Recent {rn} windows median {recent}, previous {bn} windows median {baseline}",
  "粗比": "rough",
  "参与判断的窗口：用量 10% 以上、没被外部消耗污染": "Windows that count: at least 10% used and not polluted by external usage",
  "最近 3 天": "Last 3 days",
  "最新": "Latest",
  "之前": "Before",
  "{title}：{detail}": "{title}: {detail}",
  "你手动排除了，不参与判断": "Excluded by you; not used for judging",
  "外部消耗约 {pct}%，未参与判断": "About {pct}% external usage; not used for judging",
  "用量太少，未参与判断": "Too little usage; not used for judging",
  "断档前的基线窗口": "Baseline window from before the break",
  "{start} 起 · 已用 {pct}%": "From {start} · {pct}% used",
  "参与判断": "Counted",
  "未参与": "Skipped",
  "你排除的": "Excluded",
  "正常波动范围": "Normal range",
  "停用 {days} 天（已折叠）": "Not used for {days} days (folded)",
  "停 {days} 天": "{days}d off",
  "之前中位 {value}": "Previous median {value}",

  // 窗口详情
  "{tool} {window}窗口 · {start} 起": "{tool} {window} window · from {start}",
  "已用 {pct}% · 花费 {cost}": "{pct}% used · spent {cost}",
  " · 约值 {cap}": " · worth ~{cap}",
  " · 外部消耗约 {pct}%": " · ~{pct}% external",
  "恢复参与判断": "Count this window again",
  "不让这个窗口参与判断": "Don't count this window",
  "比如这个窗口里你在别的设备上也用过、或者有别的异常。数据本身不删，随时可以恢复":
    "For example if you also used another device in this window. Nothing is deleted; you can undo this anytime",
  "累计花费": "Cumulative spend",
  "外部消耗跳涨": "External jump",
  "已用 {pct}% · {when}": "{pct}% used · {when}",
  "约 {pct}% 来自本地日志以外": "About {pct}% came from outside the local logs",
  "已用": "Used",

  // 模型汇率
  "当前窗口 {when} 结束，下一个窗口用到 5% 以后就能算出来。": "The current window ends at {when}; rates appear once the next window reaches 5%.",
  "下一个窗口用到 5% 以后就能算出来。": "Rates appear once the next window reaches 5%.",
  "{n} 个窗口": "{n} windows",
  "至少要 2 个窗口才能分出各模型的汇率，现在只有 {n} 个。": "At least 2 windows are needed to estimate per-model rates; there are {n} now.",
  "至少需要 2 个用量 5% 以上的干净窗口才能回归。": "At least 2 clean windows with 5%+ usage are needed.",
  "出现在 {n} 个窗口": "seen in {n} windows",
  "（数据少）": " (little data)",

  // 用量
  "等价 API 花费": "API-equivalent spend",
  "请求数": "Requests",
  "{n} 次无价格": "{n} without a price",
  "缓存命中率": "Cache hit rate",
  "缓存读 {n} token": "{n} cache-read tokens",
  "输出": "Output",
  "其中推理 {n}": "{n} of it reasoning",
  "等价 API 花费（按小时）": "API-equivalent spend (hourly)",
  "等价 API 花费（按天）": "API-equivalent spend (daily)",
  "另有 {n} 次请求转发到第三方或没有价格，未计入。": "{n} more requests went to third-party models or have no price and aren't counted.",
  "工具": "Tool",
  "模型": "Model",
  "计费": "Billing",
  "请求": "Requests",
  "输入": "Input",
  "缓存读": "Cache read",
  "缓存写": "Cache write",
  "命中率": "Hit rate",
  "等价花费": "API-equivalent",
  "订阅": "Subscription",
  "第三方": "Third-party",
  "无价格": "No price",

  // 规则变化记录
  "订阅方案变化": "Plan changed",
  "出现新的限额窗口": "New limit window",
  "窗口提前重置": "Window reset early",
  "上个窗口用到 {pct}%，原定 {due} 重置": "previous window reached {pct}%, was due to reset {due}",
  "，可能用了重置券或切换了账号": " (a reset credit or a switched account?)",
  "额度接口": "quota API",
  "日志": "log",
  "{where}出现新字段": "{where}: new fields",
  "{where}的字段不再出现": "{where}: fields no longer present",
  "（近 90 天 {n} 条）": "({n} in the last 90 days)",
  "近 90 天没有发现规则变化。": "No rule changes found in the last 90 days.",

  // 窗口历史
  "窗口": "Window",
  "开始": "Start",
  "外部消耗": "External",
  "推算上限": "Estimated cap",
  "折合主力模型": "In main model",
  "否（你排除了）": "No (excluded by you)",
  "是": "Yes",
  "否（外部消耗多）": "No (much external usage)",
  "否": "No",
  "数据不删，只是不参与汇率、趋势和告警": "Nothing is deleted; it just stays out of rates, trends and alerts",
  "恢复": "Restore",
  "排除": "Exclude",
  "还没有窗口数据。": "No window data yet.",

  // 数据管理
  "参与分析": "Analyze",
  "只保留": "Keep only",
  "每天自动备份一份，留最近 14 份": " Backed up daily; the last 14 copies are kept",
  "；另有 {n} 份删除数据前的备份": "; plus {n} backups taken before deletions",
  "。": ".",
  "数据库 {mb} MB，永久保存，不会自动清理。": "Database {mb} MB, kept forever and never cleaned up automatically.",
  "Claude Code 默认会删掉 30 天前的日志，这里的副本不受影响。": " Claude Code deletes logs older than 30 days by default; the copy here is not affected.",
  "数据目录：": "Data folder: ",
  "：已存 {n} 次请求": ": {n} requests stored",
  "（{date} 起）": " (since {date})",
  "本机还有 {n} 个更早的日志没导入（最早 {date}）。": " {n} older log files on this computer haven't been imported (oldest {date}).",
  "本机的日志都已导入。": " All logs on this computer are imported.",
  "导入全部历史日志": "Import all past logs",
  "数据": "Data",
  "时间": "Period",
  "要不要留": "Keep it?",
  "未记录方案": "unknown plan",
  "已于 {date} 删除；以后再用这个方案，新数据照常记录": "Deleted on {date}; if you use this plan again, new data will be recorded as usual",
  "数据留着，但不参与汇率、趋势和告警（比如以前用过的别的账号）": "Keep the data but leave it out of rates, trends and alerts (e.g. an old account)",
  "默认": "Default",
  "（排除 {n}）": " ({n} excluded)",
  "删除…": "Delete…",
  "「只保留」：数据不动，只是不参与分析；单个窗口可以在「窗口历史」或窗口详情里排除。「删除」前会自动把整个数据库备份一份。":
    "\"Keep only\" leaves the data untouched but out of the analysis; single windows can be excluded in Window history or the window details. \"Delete\" backs up the whole database first.",
  "它的额度快照，以及只属于这些窗口的请求记录": "its quota snapshots and the requests that belong only to these windows",
  "它的额度快照（Claude 的请求记录不分方案，留在用量统计里）": "its quota snapshots (Claude requests aren't tied to a plan and stay in the usage stats)",
  "删除「{name}」的 {n} 个窗口？": "Delete the {n} windows of \"{name}\"?",
  "会删掉{what}。删除前会自动把整个数据库备份一份。删掉的数据以后重读日志也不会再导回来。":
    "This deletes {what}. The whole database is backed up first. Deleted data won't come back even if the logs are read again.",
  "已删除 {w} 个窗口、{s} 条额度快照、{r} 条请求。删除前的备份：{path}": "Deleted {w} windows, {s} quota snapshots and {r} requests. Backup taken before deleting: {path}",
  "导入中…": "Importing…",
  "导入完成，新增 {n} 次请求。": "Import finished: {n} new requests.",
  "导入失败：": "Import failed: ",

  // 模型价格
  "内置快照": "built-in snapshot",
  "你手填的": "set by you",
  "说明": "Notes",
  "你的请求": "Your requests",
  "没有价格，不计入金额": "No price; not counted in dollars",
  "按 {model} 计价": "priced as {model}",
  "同 {model}": "same as {model}",
  "超过 {n} token 上下文加价": "higher price above {n} tokens of context",
  "fast 模式另价": "separate fast-mode price",
  "，": ", ",
  "没有匹配的模型。": "No matching models.",
  "（{n} 个模型）": "({n} models)",
  "单位：美元 / 百万 token。价格来自 models.dev，每天自动更新（最近一次：{when}）。":
    "USD per million tokens. Prices come from models.dev and refresh daily (last: {when}). ",
  "要改价或给没价格的模型补价，编辑 {path}（格式见项目里的 prices_override.example.json），然后点「立即同步」。":
    "To change a price or add a missing one, edit {path} (see prices_override.example.json in the project for the format), then click \"Sync now\".",

  // 设置
  "界面语言": "Language",
  "发现额度可能被调时弹系统通知": "Show a system notification when quota may have changed",
  "Claude Code 空闲时多久查一次额度（分钟）": "How often to check Claude quota while idle (minutes)",
  "使用中固定 3 分钟一次": "Every 3 minutes while in use",
  "偏离超过多少才判为「被调」（%）": "Minimum change to count as \"adjusted\" (%)",
  "这是下限：你的数据本身波动大时会自动放宽": "This is a floor; it widens automatically if your data is noisy",
  "前后共同在用的模型低于多少，算「换了模型」（%）": "Below this share of shared models, treat it as \"new model\" (%)",
  "按最近窗口的花费占比算": "Measured as a share of recent spend",
  "首次启动导入多久以内的日志（天）": "On first launch, import logs from the last (days)",
  "之后可以在「数据管理」里导入全部": "You can import everything later under Data",
  "跟随系统": "Follow system",
  "中文": "中文",
  "（默认 {v}）": " (default {v})",
  "保存": "Save",
  "已保存": "Saved",

  // 运行状态
  "正常": "OK",
  "没找到登录信息": "login not found",
  "登录过期": "login expired",
  "出错": "error",
  "被限流，稍后再试": "rate limited, will retry",
  "{when}，{status}": "{when}, {status}",
  "（{error}）": " ({error})",
  "还没有": "not yet",
  "日志扫描：": "Log scan: ",
  "{n} 个文件": "{n} files",
  "Claude 额度：": "Claude quota: ",
  "（使用中每 3 分钟查一次）": " (every 3 minutes while in use)",
  "Codex 额度：": "Codex quota: ",
  "（只在本地空闲时查）": " (checked only while idle locally)",
  "{tool} 登录信息：{status}（{where}）": "{tool} login: {status} ({where})",
  "价格表：models.dev，更新于 {when}": "Prices: models.dev, updated {when}",
  "备份：": "Backup: ",
  "没有价格的订阅模型：{models}（可以在「模型价格」说明的文件里补价）": "Subscription models without a price: {models} (add them in the file named under Model prices)",
  "、": ", ",
  "{where}：{error}": "{where}: {error}",
  "扫描 codex 日志": "Scanning Codex logs",
  "扫描 claude 日志": "Scanning Claude logs",
  "刷新 models.dev 价格": "Refreshing models.dev prices",
  "备份数据库": "Backing up the database",
  "后台循环": "Background loop",
  "停止服务": "Stop service",
  "停止后不再采集额度数据；再打开一次 How much my Claude 即可恢复。": "Quota data stops being collected; open How much my Claude again to resume.",
  "停止后台服务？停止期间 Claude 的额度百分比无法补录。": "Stop the background service? Claude quota percentages can't be back-filled for the time it's stopped.",
  "服务已停止。再打开一次 How much my Claude 即可恢复。": "Service stopped. Open How much my Claude again to resume.",
  "同步于 {when}": "Synced {when}",
  "正在首次同步…": "First sync…",
  "同步中…": "Syncing…",
  "立即同步": "Sync now",
  "加载失败：": "Failed to load: ",

  // 页面上的固定文字
  "切换左上角标志": "Switch the corner logo",
  "点击切换 Claude / OpenAI": "Click to switch Claude / OpenAI",
  "现在": "Now",
  "每格 2%。实心格是已经用掉的；斜纹格是照目前的速度、到重置时还会用掉的（变红说明会提前用完）；上方的小三角是窗口时间走到哪了。实心格跑在小三角前面，就是用得比时间快。":
    "Each cell is 2%. Solid cells are used; striped cells are what you'll use by reset at the current pace (red means you'll run out early); the small triangle marks how much of the window's time has passed. Solid cells ahead of the triangle mean you're using it faster than time passes.",
  "额度有没有被调": "Has the quota been changed?",
  "每个窗口都折算成「全用主力模型时值多少钱」，消除模型组合的影响。偏离仪的指针是最近几个窗口比之前变了多少，中间绿色一段是正常波动范围，落到左边红区是收紧、右边黄区是放宽。下面小图里的灰带也是这个范围；最近的窗口偏出灰带才判为变化。空心点是被外部消耗污染（别的设备、云任务、网页聊天）或用量太少的窗口，不参与判断。竖线是规则变化（提前重置、方案变化、接口字段变化）。点一个点可以看那个窗口的详情。":
    "Each window is converted to \"what it's worth if spent all on the main model\", removing the effect of the model mix. The gauge needle shows how much recent windows changed versus before: the green middle is normal noise, red on the left means tighter, yellow on the right means looser. The grey band in the chart is the same range; only recent windows outside it count as a change. Hollow points are windows polluted by external usage (other devices, cloud tasks, web chat) or with too little usage; they don't count. Vertical lines are rule changes (early resets, plan changes, API field changes). Click a point to see that window.",
  "单用一个模型时，一个窗口值多少钱": "What one window is worth on a single model",
  "额度消耗和 API 价格不成正比：同样花 $1，便宜模型往往吃掉更多额度。这是用近 60 天干净窗口回归出的汇率。浅色条表示该模型出现的窗口少，估计不稳。":
    "Quota use isn't proportional to API price: the same $1 on a cheaper model often uses more quota. These rates are fitted from clean windows in the last 60 days. Pale bars mean the model appears in few windows and the estimate is shaky.",
  "用量": "Usage",
  "今天": "Today",
  "近 7 天": "7 days",
  "近 30 天": "30 days",
  "全部": "All",
  "花在哪些模型上": "Spend by model",
  "数据管理": "Data",
  "（存了什么、要不要留）": "(what's stored, what to keep)",
  "模型价格": "Model prices",
  "我用过的": "Used by me",
  "搜模型名": "Search models",
  "规则变化记录": "Rule changes",
  "窗口历史": "Window history",
  "按模型明细": "Per-model details",
  "设置": "Settings",
  "运行状态": "Status",
};

let LANG = "zh";

function setLang(language) {
  const auto = (navigator.language || "").toLowerCase().startsWith("zh") ? "zh" : "en";
  LANG = language === "zh" || language === "en" ? language : auto;
  document.documentElement.lang = LANG === "zh" ? "zh-CN" : "en";
}

function t(text, vars) {
  let s = LANG === "en" && EN[text] !== undefined ? EN[text] : text;
  if (vars) s = s.replace(/\{(\w+)\}/g, (m, k) => (k in vars ? vars[k] : m));
  return s;
}

// 页面上写死的文字：第一次把中文原文记在 data-zh-* 里，之后每次切换语言都从原文翻
function translateStatic(root = document) {
  const attrs = [["data-i18n", null], ["data-i18n-tip", "data-tip"], ["data-i18n-title", "title"],
    ["data-i18n-aria", "aria-label"], ["data-i18n-placeholder", "placeholder"]];
  for (const [src, target] of attrs) {
    root.querySelectorAll(`[${src}]`).forEach((el) => {
      const text = t(el.getAttribute(src));
      if (target) el.setAttribute(target, text);
      else el.textContent = text;
    });
  }
}
