# 牛来量化 完整说明（新功能、插件、接口都在这一份里）

这是牛来量化唯一的一份详细文档。以前分散在 `docs/GUIDE.md 第三部分`（对外接口）和 `docs/GUIDE.md 第四部分`（标准数据接口）里的内容已经并进来，那两个文件只留一行跳转。README 讲怎么装、怎么启动、网页大致怎么用；其余的都在这里。

目录

- 第零部分 网页怎么用（第四轮改版后的导航、每个页面、旧链接对应关系）
- 第一部分 功能说明
  - 1. 买入策略和卖出策略（多套、生效、模板、导出导入）
  - 2. 股池绑定策略（网页上叫「股池分配」）
  - 3. 股池成分股存档和按股池回测
  - 4. 风控：只管买入，卖单不受限；不限笔数
  - 5. 止损不会悄悄跳过
  - 6. 「数据缺失」的统一口径
  - 7. 因子库：12 类
  - 8. 形态信号和自定义公式的关系
  - 9. 外部因子：先选数据源，再选因子
  - 10. 涨跌停规则表
  - 11. 安全边界
- 第二部分 插件：作用和写法
- 第三部分 对外接口（/api/v1）和网页内部接口
- 第四部分 标准数据接口（qfdp v1）：第三方行情和股池怎么接入

---

## 第零部分 网页怎么用（第六轮改版）

网页地址不变（按 `[web]` 里的 host 和 port，自带配置是 http://127.0.0.1:8081，只监听本机）。改版只动了页面的组织方式，配置文件、命令、`QF_*` 环境变量、`config/pipeline.toml` 的路径都没变，旧配置不用重填。

### 新导航

一级导航七个：**首页、买入、卖出、账户、回测、设置、日志**。手机宽度下前四个在底部，后三个收在「更多」里。有子页的大页在标题下面有一排子页按钮，地址写成 `#大页/子页`，可以直接收藏。

| 一级 | 子页 | 地址 |
|---|---|---|
| 首页 | 大盘指数 → 市场情绪 → 股池走势（内嵌在下方，不再单开子页） | `#home` |
| 买入 | 买入策略（含选股与打分）/ 股池分配 / 选股结果（分级股池已并进来） | `#buy/strategy`、`#buy/pools`、`#buy/results` |
| 卖出 | 卖出策略（含卖出默认参数和按股池覆盖）/ 股池分配 / 卖出结果 | `#sell/strategy`、`#sell/pools`、`#sell/results` |
| 账户 | 资金与持仓（资金、持仓、委托、手动下单）/ 仓位与风控 | `#account/overview`、`#account/risk` |
| 回测 | 选策略、选股池回放 | `#backtest` |
| 设置（只留系统配置） | 数据源（股池数据源、行情数据源两块）、股池列表与别名、时段与补录、下单程序、推送、对外 API、插件、首页、运行模式 | `#config/子页名`，如 `#config/sources`、`#config/plugins` |
| 日志 | 推送记录、运行日志、系统信息 | `#notify` |

顶栏一直显示：自动交易开关、当前运行模式，以及小字的「买：某套 · 卖：某套」（当前生效的买入、卖出策略）。

### 旧链接自动跳转

以前收藏的地址照样能打开，会自动换成新地址（浏览器地址栏也会改过来）：

| 旧地址 | 跳到 |
|---|---|
| `#funnel`（今日漏斗）、`#buy/funnel` | `#buy/results`（选股结果） |
| `#tiers`、`#buy/tiers` | `#buy/results` |
| `#strategy`、`#buy` | `#buy/strategy` |
| `#sell` | `#sell/strategy` |
| `#broker`（实盘页） | `#account/overview` |
| `#log`、`#logs`、`#push` | `#notify` |
| `#pools`、`#home/pools`、`#home/overview` | `#home` |
| `#bind`、`#config/bind`（股池绑定策略） | `#buy/pools` |
| `#config/select`（选股与打分） | `#buy/strategy` |
| `#config/exits`（卖出默认参数） | `#sell/strategy` |
| `#config/risk`、`#risk`（仓位与风控） | `#account/risk` |
| `#plugins` | `#config/plugins` |
| `#exits`、`#sell/check` | `#sell/results` |
| 认不出的地址 | 首页 |

### 每个页面怎么用

**首页 → 大盘与股池**：指数、市场情绪、各股池当天走势，和以前一样。

**首页 · 股池走势**（在大盘指数、市场情绪下方）：上面按股池中文名点选切换；左边是股池成分股列表（现价、涨跌幅、入池以来涨跌），点表头排序；点一只票，右边显示详情：分时带均价线（可切日K，日K上标出入池日）、各项指标（拿不到的显示「—」）、它在买入策略里走到哪一层和打分明细。有持仓时还显示离止盈 / 止损还差多少、哪天到期卖出（按来源股池用的那套策略和覆盖参数算）。股池整体走势缩成小图放在标题旁。「入池以来」要等股池存档积累几天才有数。

**买入 → 买入策略**（卖出策略页和它一样）：
- 顶部绿条写「正在生效：某某策略」和启用时间。自动交易开关和生效策略是两回事：开关关着时策略照样运行，只是不下单。
- 左边选要看 / 改的那套策略。改的不是正在生效的那套时，顶部有黄条提醒。
- 每层可以切「全部满足 / 任一满足」，用上移下移按钮或直接拖动调顺序。
- 「加因子」打开因子库弹窗：12 类，可以搜索，可以只看盘中实时、盘后、回测可用或实验性的；每个因子有说明和例子。
- 参数都是点选：数字框、下拉、时间框、多选（如要排除的板块）、字段下拉、行业点选。没填的显示「沿用默认（…）」。每个因子都可以选「数据缺失时：淘汰 / 放行」。
- 外部因子先点选数据源（标出是否可用），再点选这个数据源提供的因子或字段。
- 自定义公式：点字段、点运算符插进去，边写边校验，「预览」显示当天能筛出几只。
- 形态信号：点选拼条件并预览；「管理信号」里可以保存自定义信号、改名、删除（自带的只读）。
- 行业筛选：「点选行业」从行业列表里选。列表来自已经取过资料的票（本地缓存），所以第一次用时要先跑一次选股；列表里没有的可以手动补一个，名字要和数据源给的完全一致。
- 保存前先弹出和上一版的差异；「校验」检查写错的地方。
- 「设为生效」会弹确认框，说明从下一次选股 / 卖出检查起按这套来，原来那套停用不删除；有未保存的修改时不让设为生效。
- 「复制为新策略」「导出文件」「复制到剪贴板」（剪贴板用不了时会弹出一个已全选的文本框，手动复制即可）。「导入」先列出差异，导入后存成新的一套，不会自动生效。
- 模板：自带模板只读；可以把当前策略存成模板、套用前预览差异、设为默认、恢复默认。

**买入 → 选股结果**（原「今日漏斗」）：可以按层看（每层剩几只、淘汰几只），也可以按票看（每只票走到哪一层、为什么被淘汰、打分明细）。写明用的是哪个股池的哪套策略。

**分级股池**：已并进「买入 → 选股结果」页下方的「分级跟踪」，跟着上面选中的股池一起筛。

**选股结果怎么看（第六轮）**：顶部用股池标签点选——「全部股池」、点一个看单个池、再点别的变多选。汇总表列出每个池的成分股、新入池、进入策略、通过因子和入选；下面每个池一张自己的漏斗。报错只显示一次：哪个池、什么问题、现在怎么处理；原始英文报错收在「详情」里。

**卖出 → 卖出策略**：「不同股池用不同的卖出参数」一节说明按股池覆盖是所用那套策略的一部分：股池绑定决定用哪套卖出策略，覆盖只改这套里的数字；页面上按你现有的配置生成例子（如 gushi:gold_230 持有 1 天、14:45 卖）。

**卖出 → 卖出结果**：每次卖出检查（定时的、手动点「立即检查卖出」的）记一条，最新的在上面。每条写明检查时间、来源、用的卖出策略、自动交易开没开；表格逐只列出持仓的现价和浮盈、按哪个股池的哪套策略、结果（不卖 / 该卖·触发了哪一层 / 出问题）、是否被人工保留跳过、下了什么委托（模拟盘或自动交易关着时只记信号）、说明，点「看每一层」能看到每层每个因子满足没有。拿不到价、取价出错、卖出因子出错、止损层缺数据时标红「出问题」（exit_problem），下面单独列出原因：这些票这一轮没有卖出，也不算「没触发」。

**账户**：资金、持仓、委托、手动下单，和原来一样；持仓旁边多了「按某池的某套策略卖」。

**回测**：点选买入策略、卖出策略（已保存的策略和模板都行，默认是正在生效的），点选一个或几个股池（用存档里当天的成分股）。结果按股池分开统计并给合计，列出没有存档的日期。下面是历史回测，可以勾选几次对比。实验性因子在回测里标「回测不可用」并跳过。

**买入 → 股池分配**：每个股池一行，点选它用哪套买入策略（没选的跟随正在生效的那套）、新入池方式（跟随默认 / 每日重算 / 跨日连续 / 全部）。按住左边的 ⠿ 拖动整行调整股池优先顺序，手机上用 ↑ ↓：一只票同时在几个池里时归排在前面的池，今天已经由一个池入选过的票别的池不再重复买。下面还有「送进策略的票」（只送新入池的票 / 每次送全部）和「新入池默认怎么算」两项，以及每套买入策略在哪些股池用。点选后立即保存，写回 config/pipeline.toml。

**卖出 → 股池分配**：每个股池点选卖出策略，旁边列出这个池按股池覆盖了哪些卖出参数；下面是每套卖出策略在哪些股池用，以及「启用卖出检查」开关。股池先后顺序买卖共用，在买入那边调。

**买入策略 / 卖出策略页**：「正在生效」条下面多了一行「在用这一套的股池」。选股与打分（每次最多买几只、打分权重）在买入策略里，卖出默认参数和按股池覆盖在卖出策略里，设置里不再有这几页。

**账户 → 仓位与风控**：写明不限下单笔数。单笔上限、每日金额、黑白名单、同一行业最多买几只归在「只管买入」一组：止损、止盈、到点卖出和手动卖出都不受这些限制。本页改完点「保存并生效」，只提交仓位、风控、模拟盘这几段。

**设置 → 数据源**：分成「股池数据源」（写在 `[pool_sources.名字]`）和「行情数据源」（写在 `[market_sources.名字]`）两块，各自能添加。每个数据源显示状态（可用 / 失败 / 还没用过 / 没启用）、用在哪里（哪些股池、哪类行情）、最近一次成功的时间、最近的报错（中文，原始英文点「详情」展开）。gushi 的「代理」是下拉框：跟随系统（失败再直连）、直连、自定义代理网址。连接常被中途断开（UNEXPECTED_EOF）时，先选「直连」。旧配置里的 `[sources]` 读的时候自动归类，保存时改写成两段，不用重填。

**设置 → 股池列表与别名**：勾选用哪些股池选股，并给每个股池起中文别名，页面上所有显示股池的地方都用它。

**交易日历**（独立插件 `plugins/trade_calendar/`）：把含未来日期的 A 股交易日落盘到公共数据文件 `data/cache/trade_calendar.json`（新浪交易日历，只留 2023 年起）。它是**公共设施**，不注册因子/数据源、不改主程序任何一行；别的插件运行时 `import trade_calendar_lib` 读它，也可以直接按文件顶部写明的数据契约读那个 JSON。日历缺失、超过 30 天、或覆盖不到当年年底时，插件加载时后台重拉一次。之所以要有未来日期：主程序 `Market.is_trading_day()` 的依据全是已发生过的日期，凌晨问「今天是不是交易日」判不出来；插件任务改用这份日历就能判准。手动刷新：`.venv/bin/python plugins/trade_calendar/trade_calendar_lib.py --refresh`。


**设置 → 插件**：列出 plugins/ 目录里每个插件注册了哪些因子、数据源、推送渠道；加载失败的写明报错原因，和内置因子重名的写明「没有注册」。改了插件文件后点「重新加载」。

**其他设置页**：每一项旁边有中文说明（干什么、什么时候生效、留空会怎样），英文键名用小字放在后面。

**日志**：推送记录、运行日志、系统信息。

---

### 第六轮小修：选股结果、卖出结果、首页自动刷新

- **买入 → 选股结果**：打开就是「全部结果」——所有运行记录合成一张表，新的在上面，每行是哪天、几点、手动还是定时、哪个股池、哪只票、走到第几层、入选没有、级别（候选 / 重点 / 买入）。默认全部股池，点股池标签看单个池或多选；「只看重点及以上」一起用。上面的汇总按股池列出运行次数、新入池、进入策略、入选（按「只次」算，同一只票在几次运行里出现就算几次）。
  - 按日期或某一次运行看，在右上角「历史记录」里选，默认不选。选了某一次运行才显示那一次的漏斗；汇总右上角的「看最近一次的漏斗」可以直接跳过去。
  - 原来下面单独的「分级跟踪」去掉了。点表里一只票，详情里有这次的每层路径、跨天的级别变化（哪天升到哪级、哪天降级），以及手动调级按钮（只用于跟踪，不下单）。
  - 「手动运行」收在页面最下面，只有「只选股（不交易）」「选股 + 交易」两个按钮。
  - 旧记录第 0 层的「前池（股池合并去重）」显示时一律换成「进入策略（全部股池）」，并标出每个池各贡献几只；记录文件本身不改。
  - 运行方式按记录里的 mode 显示：paper 模拟盘、dry_run 演练、live 实盘；认不出就写「未记录」，不会当成实盘。记录时的运行方式和现在不一样时，两个都写。
- **卖出 → 卖出结果**：所有卖出检查的每只持仓合成一张表，新的在上面：时间、定时还是手动、股票、股池、持有天数、现价、浮盈、触发的层、结果（继续持有 / 已下卖单 / 已卖出 / 卖出出问题）和中文原因。顶部有股池标签、「只看触发的」「只看出问题的」，按日期或某一次检查在「历史记录」里选。「立即检查卖出」只在这一页顶部，检查完新结果出现在表的最上面并高亮。
- **首页自动刷新**：交易日 9:15–11:30、13:00–15:00 按「设置 → 首页 → 交易时段自动刷新间隔」（默认 15 秒）在后台刷新大盘、市场情绪和股池走势（含选中那只票的分时和现价）；其余时间和休市日 5 分钟一次，角落显示「已收盘」「休市」等。页面切到后台时暂停，回来马上刷一次。只更新数字和图，正在看的股池、选中的票、滚动位置都不变。角落有「最后更新 时:分:秒」和自动刷新开关；拉取失败时保留上一次的数据，小字写「刷新失败，显示的是 某时某刻 的数据」。后端同一份行情按这个间隔只算一次，几个页面同时打开也共用。

## 第一部分 功能说明

### 1. 买入策略和卖出策略

- 一套**买入策略** = 买入漏斗的全部层级 + 打分权重 + 取前几名；一套**卖出策略** = 卖出因子链 + 止盈止损/持有天数/到点卖出等参数 + 按股池覆盖参数。
- 可以存好几套，但同一时间每一侧只有一套「正在生效」。生效的那套写在 `config/pipeline.toml` 里，读的时候以配置文件为准；其余的存在 `data/strategies/`。
- 「设为生效」会写进 pipeline.toml 并推一条钉钉；接口里必须带 `confirm: true` 才执行。它和顶栏的「自动交易」开关是两回事：生效策略决定怎么选、怎么卖；开关决定下不下单。
- 复制、改名、导出（自动去掉 token、口令、网址里的 `?token=` 和 `用户名:密码@`）、导入（先列差异，存成新的一套，不会自动生效）、模板（自带的不可删改；可以另存、套用前预览差异、设为默认/恢复默认）。
- 第一次启动时会把你现在的配置自动搬成「当前买入策略」和「当前卖出策略」，不用重填。
- 老的漏斗接口（`PUT /api/funnel/{side}`、`PUT /api/v1/funnels/{side}`）还在，内部就是「修改正在生效的那套策略的层级」，配置文件和策略文件一起更新，不会各说各的。
- 选股结果、委托记录、卖出检查明细（`data/exit_checks.jsonl`）里都写上用的是哪个股池、哪套策略。

### 2. 股池绑定策略

每个股池可以指定自己的买入策略和卖出策略；没指定的用当前生效的那套。网页「买入 → 股池分配」「卖出 → 股池分配」里点选；配置文件里长这样：

```toml
[pool.bind."gushi:gold_230"]
buy = "s-20261112-ab12"      # 买入策略 id，不写 = 用生效的买入策略
sell = "s-20261112-cd34"     # 卖出策略 id，不写 = 用生效的卖出策略
```

规则（实现在 `qfpy/poolbind.py`）：

1. **先定按哪个池。** 买入看选股时这只票来自哪些池；卖出看买入时记下的来源股池（持仓的 sources）。一只票同时在几个池里时，**按配置顺序取第一个**：先看 `[pool.bind]` 里的顺序；卖出再看卖出策略 `[exits.by_pool]` 里的顺序；买入再看 `[pool].pools` 的顺序。
2. **再定用哪套策略。** 这个池绑了就用绑定的，没绑就用生效的。
3. **最后套覆盖参数。** 「按股池覆盖卖出参数」（`[exits.by_pool]`）是卖出策略内容的一部分，跟着策略走：用哪套卖出策略，就只用那套策略里对这个池的覆盖，不会把生效策略的覆盖混进来。所以两者不会打架：绑定决定「哪套卖出链」，覆盖只改这套里的数字。只想改几个数字用覆盖；卖出逻辑完全不同就绑定另一套策略。

其他细节：

- 选股时把要跑的池按买入策略分组，每组跑自己的漏斗、自己的打分和「取前几名」。一只票只属于一组，不会被两套策略各买一次。「同一行业最多买几只」跨组一起算。漏斗结果里每一层前面写着「【策略名 · 股池】」。
- 绑定的卖出策略里的「到点卖出」时刻也会排进定时任务。
- 绑定的策略不存在时保存配置会报错；还绑着的策略不能删。万一策略文件被手工删了，按生效策略跑，并在选股警告里写明。
- 「账户 → 持仓」和股池走势的持仓详情里，每只持仓都写着「按哪个池的哪套策略在卖」以及最终的止盈/止损/持有天数/到点时刻。
- 接口：`GET/PUT /api/pool-bind`、`GET /api/positions-sell-by`（`/api/v1` 下同名，写操作受 `allow_config_write` 管）。

### 3. 股池成分股存档和按股池回测

**存档**（`qfpy/poolarchive.py`）：

- 每次选股运行拉到股池时顺手存一次；交易日 `[pool].archive_time`（默认 15:10）再定时拉一遍所有股池存一次。`[pool].archive = false` 关掉。
- 存在 `data/pool_archive/<日期>.json`，每条记录：日期、股池、代码、名称、入池字段（数据源给的那些，比如竞价涨幅、换手）、当天第一次出现的时间，以及这份存档来自哪几次（选股 09:26、定时 15:10、补录……）。
- 同一天存好几次取并集：出现过就算当天在池里，字段用最新一次的。
- 只记真实拉到的数据。拉取失败的池当天就没有记录（日志里会写）；开始存档之前的日子永远没有数据，不会倒推。
- 接口：`GET /api/pool_archive`（每个池有几天存档）、`GET /api/pool_archive/<日期>`、`POST /api/pool_archive/run`（立刻存一次，只读数据源，不下单）。

**按股池回测**（`qfpy/backtest.py` 的 `run_pool_backtest`）：

- 回测页选一个或几个股池。每个池单独一份初始资金、单独回放：每天只在当天存档里的成分股中选股，用这个池绑定的买入/卖出策略（也可以每个池手动指定），持仓记成来自这个池，卖出时套这套策略对这个池的覆盖参数。
- 结果按股池分开统计（收益率、最大回撤、成交笔数、胜率、用的哪两套策略、有几天存档），再给一个合计（各池相加）。
- **没有存档的交易日不选股**，列在「没有存档的日子」里，不拿前一天或别的池补。整个区间都没存档的池直接报「没有这个股池的存档」。一只票同时在几个池里时每个池各算各的。成分股太多时只取入池天数最多的 120 只，结果里会写。
- 接口：`GET /api/backtest/pools`（可选的池、存档天数、绑定的策略）；`POST /api/backtest` 带 `{"start", "end", "pools": [...], "pool_strategies": {"池": {"buy": "s:<id>", "sell": "t:<模板id>"}}}`。不带 `pools` 就是原来的按代码回测。每次回测的摘要存进 `data/backtests.jsonl`。

### 4. 风控：只管买入，卖单不受限；不限笔数

- **不再限制每天的委托笔数。** `[risk].max_daily_orders` 这一项还认（旧配置不报错），但填多少都不生效，网页上也不再显示。
- 单笔上限 `max_order_amount`、每日金额 `max_daily_amount`、白名单、黑名单、同一只票每天只买一次、整手、同一行业最多几只：**只管买入**。止损、止盈、到点卖出和手动卖出都不受这些限制，卖出金额也不计入「每日金额」。
- 买卖都查的：一键停止、代码格式、数字限价和整数股数、涨跌停价范围、偏离参考价、交易时段。卖出另查：不超过 T+1 可卖数量；零股只能一次卖完。
- 切到实盘时不弹确认框（保持原样）：在「运行模式」里选实盘、保存即生效，切换时照样记日志、推钉钉。

### 5. 止损不会悄悄跳过

卖出检查时，下面几种情况以前只是「这一轮不判断」，现在一律记日志、写进卖出检查明细的 `problems`，并在自动交易开着时推钉钉（事件名 `exit_problem`，同一只票同一种问题 2 小时内只推一次）：

- 拿不到最新价（或取价报错）、没有成本价；
- 卖出链里某个因子出错；
- 止损层的因子数据缺失（其他层数据缺失只记日志）。

这些情况下这一轮都不会卖，也不会当成「没触发」，推送里会写明按哪套卖出策略、哪一层出了什么问题。

**人工保留和止损**：在账户页持仓表点「保留」时选挡多久——到下一次检查（只挡这一次，挡完标记自动失效）、到该交易日结束（当天剩下的检查都挡，跨到下一个交易日失效）、到手工解除（一直挡到点「取消保留」）。三种都挡不住止损：止损层永远最先检查，碰到止损照卖。后两种由插件 `plugins/hold_policy/` 实现，插件没加载或被禁用时，选了也只会按「到下一次检查」处理。

### 6. 「数据缺失」的统一口径

- 拿不到数据时因子一律报「数据缺失」，不当成 0，也不当成通过。
- 买入漏斗里每个因子都有同一个参数 `missing`：`drop` 按数据缺失淘汰（**默认**），`pass` 放行。旧参数名照样认：`allow_missing = true` 和 `on_error = "pass"` 都等于 `missing = "pass"`，`missing = "fail"` 等于 `drop`。放行的票在结果里会标「（按设置放行）」。
- 卖出链里数据缺失一律「不触发」，不受 `missing` 影响，并按上一节提醒。
- 5 日均量：前 5 天有一天没有成交量就算数据缺失，不再把缺的那天当成 0 拉低均量。
- 自带模板里不再默认放行。你自己配置里写了放行的（比如市值区间对自选股放行）保持不变。

### 7. 因子库：12 类

价格涨跌、量能换手、均线与分时、技术指标、涨停板、形态信号、竞价与资金（实验性）、股池来源、基本面与排雷、持仓与卖出、高级/自定义公式、外部因子。分类只在 `qfpy/catalog.py` 定义一份，网页和接口 `GET /api/catalog` 都从这里取。每个因子标了盘中实时还是盘后更新、回测能不能用、是否实验性。实验性因子在回测里标「回测不可用」并跳过。

排雷类：「排除 ST」和「风险排雷」里的 ST 判断是同一个口径（名字含 ST、*ST 或「退」）。行业排除/只要某些行业改成**完全匹配**：填「银行」不会误伤名字里带这两个字的别的行业。

### 8. 形态信号和自定义公式的关系

两者做的是同一类事：用字段拼条件。

- **形态信号**是点选版：每条「字段 比较符 字段/数字」，可以选「N 天前」，全部满足才算出现信号；能存成信号文件反复用（自带的在 `config/signals/`，只读；自己存的在 `data/signals/`）。格式和 tick-stock-panel 自定义信号 JSON 一样。
- **自定义公式**要手写，但能写 `or`、括号、四则运算和 `close[1]`、`ref(close, 1)` 这样的 N 天前；形态信号能表达的公式都能表达。
- 不想写公式就用形态信号；要「任一满足」或算术（比如 `close / ma20 - 1 < 0.05`）就用公式。

### 9. 外部因子：先选数据源，再选因子

- 每个外部因子都挂在一个已配置的数据源下面：先选数据源，再从它提供的因子或字段里选。`GET /api/ext-sources` 列出每个数据源提供哪些因子和字段、现在可不可用。
- push 数据源：字段就是外部程序推送过的字段；因子就是推送因子结果时带了 `source=<这个数据源>` 的那些。从没收到过推送算不可用。
- http 数据源：可选配置 `factor_url`、`factor_names`、`fields`；`fields_url` 返回过的字段也会自动记下。最近一次请求失败算不可用（「测试连接」也算一次请求）。
- 推送因子结果时没写数据源的，统一归到「@api」。
- 数据源不可用、请求失败、或者这只票没拿到数据，一律按「数据缺失」处理（默认淘汰，可选放行）。

### 10. 涨跌停规则表

只在 `qfpy/limits.py` 写一份，全系统都从这里取。板块判断、ST 判断、新股无涨跌幅窗口也只在这里，`qfpy/util.py` 和 `qfpy/vendor/tsp_price_limits.py` 里的同名函数只是转调。

- 主板 10%；创业板 2020-08-24 起 20%（之前 10%）；科创板 20%；北交所 30%。
- 主板 ST/*ST：2026-07-06 之前 5%，从这天起 10%（依据沪深《交易规则（2026年修订）》）。
- 注册制新股上市前 5 个交易日不设涨跌幅限制，北交所只有首日；这几天连板、炸板类因子标「不适用」。

### 11. 安全边界

- 默认模拟盘。只有运行模式选了实盘（`[broker].mode = "live"`），委托才会发给下单程序；模拟盘的委托只在本地撮合。
- 会调用下单程序交易接口的只有两处：实盘下单调 `/api/order`；实盘里手动撤单、以及关闭自动交易时撤掉系统自己发出的未成交委托，调 `/api/cancel`（只按合同号）。代码里没有调用 `/api/command` 的路径。数据源模块里没有任何交易调用，有单元测试检查。
- 网页只监听 127.0.0.1。另外加了两道防护：只接受 Host 是本机地址的请求（防 DNS 重绑定）；写操作如果带了 Origin/Referer，必须是本机网页，浏览器标了跨站的写请求一律拒绝（防跨站表单）。命令行和脚本不带 Origin，不受影响。
- 对外接口 `/api/v1` 没有下单、撤单、改自动交易开关或改风控的接口。

---

## 第二部分 插件：作用和写法

### 插件能干什么

插件就是放进 `plugins/` 的 Python 文件或文件夹，启动时自动加载。能注册三样东西：

- **因子**（买入或卖出）：继承 `qfpy.factors.Factor`，加 `@register_factor`。注册后出现在因子库里，漏斗里用 `kind` 引用。
- **数据源**（股池、行情、分时、日K）：继承 `qfpy.datasources.DataSource`，加 `@register_source`。
- **推送渠道**：继承 `qfpy.notify.Channel`，设置 `type_name` 和 `fields`，实现 `send(title, text)`，加 `@register_channel`。配置放在 `[notify.<type_name>]`，再把名字加进 `[notify].channels`。

不写 Python 也能自定义因子，按难度从低到高：

| 办法 | 适合 | 怎么做 |
|---|---|---|
| 形态信号 | 不想写公式 | 因子库「形态信号」，点选拼条件 |
| 自定义公式 | 字段比较、简单组合 | 因子选「表达式」，写 `close > ma5 and close[1] <= ma5[1]`。只支持 `abs/min/max/round/ref`，只有日K字段能回看 |
| 外部因子：推送 | 结果在别的程序里算好 | `POST /api/v1/factor-results/<名字>` 推结果，见第三部分「外部因子」。例子：`examples/external_push.py` |
| 外部因子：拉取 | 有现成的 HTTP 服务 | 漏斗里放「外部因子（HTTP 拉取）」。例子：`examples/external_factor_server.py` |
| 插件 | 要写 Python 逻辑 | 见下面 |

### 怎么写

单文件写法（最简单）：复制 `plugins/example_single_file.py`，改名、改 `kind`、改 `evaluate()`。

```python
from qfpy.factors import Factor, Outcome, ParamSpec, register_factor

@register_factor
class MyGap(Factor):
    kind = "my_gap"                 # 唯一的英文名，不能和内置因子重名
    title = "我的高开因子"
    cat = "price"                   # 放进 12 类里的哪一类（见第一部分第 7 节）
    sides = ("buy",)                # 买入用还是卖出用
    params = [ParamSpec("max_gap", 3.0, "高开不超过多少 %", "float")]

    def needs(self):                # 要用到的数据：minute / daily / info
        return {"minute"}

    def evaluate(self, c, ctx):
        gap = c.fields.get("open_change")
        if gap is None:
            return Outcome.missing_data("没有开盘涨幅")   # 拿不到数据：报数据缺失，放不放行由 missing 参数统一管
        ok = gap <= self.p["max_gap"]
        return Outcome(ok, gap, f"高开 {gap:.2f}%")
```

目录写法：`plugins/<名字>/plugin.toml`（写 name、version、description、entry）+ `plugin.py`，参考 `plugins/example_ma_trend/`。

数据源插件参考 `plugins/example_data_provider.py`；如果你的数据在另一个程序里，更简单的办法是按第四部分的标准数据接口实现几个 HTTP 接口。

加载和排错：

- 改完点「日志与系统 → 系统状态 → 重新加载插件」，不用重启。插件列表里能看到每个插件注册了哪些因子/数据源、加载失败的原因。
- **kind 不能和内置的重名**：重名的会被拒绝注册（内置的照常用），插件列表里写明。
- 插件里的代码和主程序同一个进程运行，只放你自己写的或看过的代码。

### 自带的示例插件只是写法演示

`plugins/` 里自带的几个示例**只是演示怎么写，不是推荐策略**，名字里都带「示例插件 …（写法演示）」：

- `example_single_file.py`：小幅高开（kind `small_gap`）、吐回浮盈（`profit_giveback`）、固定代码列表数据源（`static_list`）。
- `example_ma_trend/`：均线趋势（kind `ma_trend`）。内置因子里已经有「均线多头排列」（kind `ma_bull`），两者 kind 不同，不冲突；实际选股请用内置的。
- `example_data_provider.py`：CSV 股池数据源（kind `csv_pool`）。

这些 kind 都和内置的不重名，有单元测试检查。

---

## 第三部分 对外接口（/api/v1）和网页内部接口

机器可读的 OpenAPI 在网页的 `/api/v1/openapi.json`。

### 本轮新增的接口

| 接口 | 作用 |
|---|---|
| `GET /api/pool-bind` | 每个股池用的买入/卖出策略、是否绑定、覆盖参数、优先顺序和判定规则 |
| `PUT /api/pool-bind` | `{"pool": "gushi:x", "buy": "策略id", "sell": "策略id"}`，都留空 = 解除绑定；可带 `order` 调整优先顺序 |
| `GET /api/positions-sell-by` | 每只持仓按哪个池的哪套卖出策略在卖、最终参数 |
| `GET /api/pool_archive` | 每个股池的存档天数 |
| `GET /api/pool_archive/<日期>` | 那天的存档 |
| `POST /api/pool_archive/run` | 立刻存一次 |
| `GET /api/backtest/pools` | 按股池回测可选的池 |
| `POST /api/backtest` 带 `pools` | 按股池回测 |

`pool-bind` 和 `positions-sell-by` 在 `/api/v1` 下也有（要 token，写操作受 `allow_config_write` 管）；存档和回测只给本机网页。策略、模板、因子库、形态信号、公式校验、卖出检查结果、选股结果按层/按票看的接口在 `qfpy/web/strategy_api.py`，`/api/` 和 `/api/v1/` 下各一份，完整列表看 OpenAPI。

### 对外接口完整说明（原 docs/API.md）

给你自己的脚本、别的量化程序、tick-stock-panel 之类的工具用：推送因子结果和数据、读写买入漏斗和卖出链、读状态和持仓、触发一次只选股的运行。

机器可读的文档：`GET /api/v1/openapi.json`（同样要 token）。下面的例子都假设网页跑在 `http://127.0.0.1:8081`。

### 安全边界

- **没有任何交易能力**：这套 API 里没有下单、撤单、改自动交易开关、改风控、改运行模式的接口（测试会扫描所有路由确保这一点）。自动委托只会由内部交易层在定时任务里发出，要过自动交易开关和全部风控；手动下单和撤单只能在本机网页上做（见下面「网页内部接口 → 账户」），同样过全部风控。
- `POST /api/v1/runs` 永远只选股：不生成委托，也不记信号。带 `"trade": true` 会被拒绝（400）。
- 你推进来的因子结果和数据会影响选股结果；自动交易打开时，选出来的票会在定时任务里照常买入。所以 token 要像密码一样保管。
- 网页默认只监听 127.0.0.1，配置成其他地址会拒绝启动。别的机器要调，用 ssh 端口转发或带鉴权的反向代理。

### 鉴权

```
Authorization: Bearer <token>
# 或
X-API-Token: <token>
```

- token 优先读环境变量 `QF_API_TOKEN`（变量名由 `[api].token_env` 决定），其次读 `[api].token`。
- 两处都没配：所有 `/api/v1` 请求一律 **401**（不配 token 就不开放）。token 不对也是 401。
- `[api].enabled = false`：整套接口关闭。
- `[api].allow_config_write = false`：只读，所有会写配置的接口（改漏斗、套模板、增删数据源、重新加载插件）返回 403。推送因子结果和数据不算写配置，仍然可以。

```bash
export QF_API_TOKEN=$(python -c "import secrets;print(secrets.token_urlsafe(24))")
python -m qfpy serve          # 启动网页，/api/v1 就开了
curl -H "Authorization: Bearer $QF_API_TOKEN" http://127.0.0.1:8081/api/v1/status
```

### 错误码

| 状态码 | 含义 |
|---|---|
| 400 | 参数不对或校验没过，`detail` 是中文原因 |
| 401 | 没配 token、没带 token 或 token 不对 |
| 403 | `allow_config_write = false` 时调了写接口 |
| 404 | 没有这次运行 / 这天的结果 / 这个数据源 |
| 409 | 上一次通过 API 触发的运行还没结束 |
| 502 | 数据源取数失败 |

### 接口一览

| 方法 | 路径 | 说明 | 写配置 |
|---|---|---|---|
| GET | `/status` | 运行模式、自动交易是否开、是否处于命令行 `halt` 停止状态、配置版本、插件数（只读） | |
| GET | `/funnel/latest?detail=false` | 最新一次运行的漏斗结果 | |
| GET | `/runs?limit=50` | 运行记录列表 | |
| GET | `/runs/{run_id}?detail=true` | 某次运行的逐层结果 | |
| POST | `/runs` | 触发一次**只选股**的运行（后台执行） | |
| GET | `/runs/current/state` | 上面那次运行是否结束 | |
| GET | `/positions` | 本地持仓（含状态、保留标记）和账户汇总 | |
| GET | `/signals?limit=100&side=&status=` | 买卖信号和委托记录（只读） | |
| GET | `/sell/preview?refresh=false` | 卖出因子链对每只持仓的逐层结果 | |
| GET | `/factors?side=buy\|sell` | 可用因子类型（含插件）和表达式字段表 | |
| GET | `/funnels/{side}` | 读取买入漏斗（`buy`）/ 卖出链（`sell`） | |
| PUT | `/funnels/{side}` | 整体替换层级，校验通过才写入 | 是 |
| POST | `/funnels/{side}/validate` | 只校验不保存 | |
| GET | `/templates?side=buy\|sell` | 模板列表 | |
| POST | `/funnels/{side}/apply-template` | 套用模板 | 是 |
| POST | `/factor-results/{name}` | 推送外部因子结果 | |
| GET | `/factor-results` | 已推送过的外部因子 | |
| GET | `/factor-results/{name}?date=` | 读取某天的外部因子结果 | |
| GET | `/source-types` | 可用数据源类型（含插件） | |
| GET | `/sources` | 已配置的数据源（密钥掩码） | |
| PUT | `/sources/{name}` | 新增或修改数据源 | 是 |
| DELETE | `/sources/{name}` | 删除数据源 | 是 |
| PUT | `/sources/{name}/pools/{pool}` | 推送股池（数据源 `type = "push"`） | |
| GET | `/sources/{name}/pools` | 某数据源的股池列表 | |
| POST | `/sources/{name}/fields` | 推送行情 / 自定义字段（`type = "push"`） | |
| GET | `/plugins` | 已加载的插件 | |
| POST | `/plugins/reload` | 重新扫描 `plugins/` 目录 | 是 |

写配置的接口都会先校验，再写回 `config/pipeline.toml`，旧文件留成 `.bak`。网页里的「买入策略」「卖出策略」页用的是同一套校验和写入逻辑。

### 买入漏斗 / 卖出链的层级格式

```json
{
  "layers": [
    {
      "name": "硬性过滤",
      "mode": "all",
      "enabled": true,
      "factors": [
        {"kind": "exclude_st"},
        {"kind": "min_amount", "params": {"min_yi": 2}}
      ]
    },
    {
      "name": "突破（任一）",
      "mode": "any",
      "factors": [
        {"kind": "expr", "label": "上穿 5 日线", "params": {"expr": "close > ma5 and close[1] <= ma5[1]"}},
        {"kind": "external", "params": {"name": "my_model", "use": "score", "min_score": 0.6}}
      ]
    }
  ]
}
```

- `mode`：`all` 本层所有开启的因子都通过才保留（卖出链：都说该卖才卖）；`any` 任一通过就保留（卖出链：任一说该卖就卖）。
- `enabled`：层和因子都可以单独关掉，默认 `true`。
- `kind` 和 `params` 的可选值从 `GET /factors?side=buy` 拿：每个因子列出了参数名、默认值、类型和说明。
- 卖出链的层多一个 `hold_skippable`（点「保留」能否挡住这层）。含 `stop_loss` 的层永远最先检查，点「保留」也挡不住。挡多久在点「保留」时选：到下一次检查 / 到该交易日结束 / 到手工解除，详见第一部分第 5 节。卖出表达式还能用持仓字段 `pnl_pct`、`held_days`、`avg_cost`、`quantity`、`sellable`、`now_min`、`high_since_buy`、`drawdown_since_buy_pct`。

先校验：

```bash
curl -s -X POST -H "Authorization: Bearer $QF_API_TOKEN" -H "Content-Type: application/json" \
  http://127.0.0.1:8081/api/v1/funnels/buy/validate -d @layers.json
# {"ok": true, "errors": []}
# 写错时：{"ok": false, "errors": ["第 2 层「突破（任一）」第 1 个因子：表达式语法错误 ..."]}
```

再保存（`"dry_run": true` 也可以只校验）：

```bash
curl -s -X PUT -H "Authorization: Bearer $QF_API_TOKEN" -H "Content-Type: application/json" \
  http://127.0.0.1:8081/api/v1/funnels/buy -d @layers.json
```

套用模板（`key` 从 `GET /templates?side=buy` 拿）：

```bash
curl -s -X POST -H "Authorization: Bearer $QF_API_TOKEN" -H "Content-Type: application/json" \
  http://127.0.0.1:8081/api/v1/funnels/sell/apply-template -d '{"key": "classic"}'
```

内置模板：买入 `auction_strong` 竞价强势、`gold_230` 尾盘黄金两点半、`steady_dip` 稳健低吸、`expr_only` 只用表达式；卖出 `classic` 经典三件套、`next_day_close` 隔日尾盘卖、`trend_follow` 趋势跟踪、`intraday_weak` 盘中走弱就卖。

### 外部因子

#### 方式一：你推结果进来

你的程序算好之后推送：

```bash
curl -s -X POST -H "Authorization: Bearer $QF_API_TOKEN" -H "Content-Type: application/json" \
  http://127.0.0.1:8081/api/v1/factor-results/my_model -d '{
    "date": "2026-11-11",
    "results": [
      {"code": "300825", "score": 0.82, "note": "情绪分高"},
      {"code": "603709", "pass": false, "note": "板块走弱"}
    ]
  }'
# {"name": "my_model", "date": "2026-11-11", "count": 2, "updated_at": "..."}
```

- 每条结果 `code` 必填（6 位数字），`pass` 和 `score` 至少一个，`note` 可选。`results` 也可以写成 `{"300825": {"score": 0.82}}`。一次最多 5000 条。
- `date` 不写就是今天。默认整体替换当天的结果，加 `"merge": true` 是合并。
- 漏斗里放一个 `kind = "external"`（网页上叫「外部因子（推送）」）的因子，`name` 填 `my_model`：
  - `use`：`auto`（有 pass 用 pass，否则看 score）/ `pass` / `score`；`min_score`：用 score 时的阈值；
  - `missing`：结果里没有这只票时 `fail` 淘汰（默认）/ `pass` 放行。当天一条都没推过，默认整层淘汰。
- 分数会写进候选的字段 `ext_my_model`，后面的层可以在表达式里用。
- 完整例子：`examples/external_push.py`。

#### 方式二：系统来拉

漏斗里放一个 `kind = "http"`（「外部因子（HTTP 拉取）」）的因子，填 `url`。跑到这一层时，系统把本层所有候选**一次**POST 给你：

```json
{
  "factor": "my_http",
  "side": "buy",
  "trading_date": "2026-11-11",
  "candidates": [
    {"code": "300825", "name": "阿尔特", "sources": ["gushi:gold_230"], "fields": {"price": 14.0, "change_rate": 3.2}}
  ]
}
```

你返回的格式和推送一样：`{"results": [{"code": "300825", "pass": true, "score": 0.7}]}`。

- `timeout_secs` 默认 10 秒；`on_error`：请求失败时 `fail` 整层淘汰（默认）/ `pass` 整层放行；`token` 会作为 `Authorization: Bearer` 发给你。
- 也能放在卖出链里（`side` 是 `sell`，候选是持仓，`pass: true` 表示该卖）。
- 完整例子：`examples/external_factor_server.py`（只用标准库的小服务）。

#### 方式三：写 Python 插件

复制 `plugins/example_single_file.py`，改类名、`kind` 和 `evaluate()`，放进 `plugins/` 就自动注册。改完调 `POST /api/v1/plugins/reload`，或在网页「日志与系统 → 系统状态 → 重新加载插件」，不用重启。插件里的代码和系统在同一个进程里运行，只放你自己写的文件。

### 外部数据源

#### 推送型（`type = "push"`）

1. 建数据源（一次就行，也可以直接在配置文件里写 `[sources.mypush]` + `type = "push"`）：
   ```bash
   curl -s -X PUT -H "Authorization: Bearer $QF_API_TOKEN" -H "Content-Type: application/json" \
     http://127.0.0.1:8081/api/v1/sources/mypush -d '{"type": "push"}'
   ```
2. 推股池：
   ```bash
   curl -s -X PUT -H "Authorization: Bearer $QF_API_TOKEN" -H "Content-Type: application/json" \
     http://127.0.0.1:8081/api/v1/sources/mypush/pools/hot -d '{
       "date": "2026-11-11", "name": "热门",
       "stocks": [{"code": "300825", "name": "阿尔特", "fields": {"ext_hot": 5}}, "603709"]
     }'
   ```
   `stocks` 里可以只写代码。字段名要是合法标识符，值会转成数字。
3. 把 `"mypush:hot"` 加进 `[pool].pools`（网页「配置 → 股池与数据源」），选股时就会读它。
4. 给已有候选补字段（不管它们来自哪个股池）：
   ```bash
   curl -s -X POST -H "Authorization: Bearer $QF_API_TOKEN" -H "Content-Type: application/json" \
     http://127.0.0.1:8081/api/v1/sources/mypush/fields -d '{
       "date": "2026-11-11", "items": [{"code": "300825", "fields": {"ext_sentiment": 0.7}}]
     }'
   ```
   默认合并，`"replace": true` 整体替换当天的字段。

以 `ext_` 开头的字段可以直接写进表达式：`ext_hot >= 3 and ext_sentiment > 0.6`。

#### 拉取型（`type = "http"`）

你有现成的 HTTP 服务时用。配哪个 URL 就有哪种能力：

| 配置项 | 请求 | 你返回 |
|---|---|---|
| `pools_url` | GET | `{"pools": [{"key": "hot", "name": "热门"}]}` |
| `pool_url` | GET，URL 里可写 `{key}` `{date}` | `{"name": "热门", "stocks": [{"code": "600000", "name": "...", "fields": {...}}]}` |
| `fields_url` | POST `{"codes": [...], "date": "..."}` | `{"items": [{"code": "600000", "fields": {...}}]}` |
| `minute_url` | GET，`{code}` `{date}` | `{"preclose": 10.0, "points": [{"t": "09:31", "p": 10.1, "avg": 10.05, "vol": 1200, "amt": 12000}]}` |
| `daily_url` | GET，`{code}` `{start}` `{end}` | `{"bars": [{"date": "2026-11-10", "open": 0, "high": 0, "low": 0, "close": 0, "volume": 0, "amount": 0}]}` |

```toml
[sources.myhttp]
type = "http"
pool_url = "http://127.0.0.1:9000/pool/{key}?date={date}"
fields_url = "http://127.0.0.1:9000/fields"
token = ""          # 可空，作为 Bearer 发给你
```

分时和日K要生效，还要把数据源名加进 `[market].minute_sources` / `daily_sources`；前一个源失败会自动换下一个。

### 触发一次只选股的运行

```bash
curl -s -X POST -H "Authorization: Bearer $QF_API_TOKEN" -H "Content-Type: application/json" \
  http://127.0.0.1:8081/api/v1/runs -d '{"date": "2026-11-10"}'
# {"started": true, "trade": false, "poll": "/api/v1/runs/current/state"}

curl -s -H "Authorization: Bearer $QF_API_TOKEN" http://127.0.0.1:8081/api/v1/runs/current/state
# 跑完：{"running": false, "error": null, "run_id": "20261111-120501"}

curl -s -H "Authorization: Bearer $QF_API_TOKEN" "http://127.0.0.1:8081/api/v1/runs/20261111-120501"
```

`date` 不写就是最新交易日；可以用 `pools` 指定只跑哪几个股池。同一时间只能有一次 API 触发的运行。

### 卖出预览

```bash
curl -s -H "Authorization: Bearer $QF_API_TOKEN" "http://127.0.0.1:8081/api/v1/sell/preview?refresh=true"
```

卖出因子要拉行情，在后台算。接口立刻返回上次的结果和 `job`（`running` 为 true 时过一两秒再取）。`results` 里每只持仓有：

- `pnl_pct`、`held_days`、`sellable`、`price`；
- `layers`：每层的 `triggered` 和各因子的 `checks`（`passed`、`value`、`note`）；
- `decision` / `decision_label`：现在触发的卖出层，没触发是 `null`；`due`：今天到点会触发的层和时刻；
- `hold` / `hold_skippable`：是否已保留、这次能不能跳过。

这只是预览，不会下单。


### 网页内部接口（不需要 token，只给本机网页用）

下面这些是网页自己用的接口，挂在 `/api/` 下、不带 `/v1`。它们**不经过 token 鉴权**，安全靠网页只监听 127.0.0.1；别把网页暴露到别的机器上。外部程序请用上面的 `/api/v1`。

#### 首页：大盘行情和股池走势

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/overview?refresh=0` | 立即返回上次计算的结果；过期时在后台重算。`refresh=1` 强制后台重算 |
| GET | `/api/overview/pool?key=gushi:jjdt` | 某个股池的成分股（现价、涨跌、成交额）、两条走势曲线和近 N 日明细；后台还没算完时返回 `{"pending": true}` |
| GET | `/api/dataproto/openapi.json` | 标准数据接口 qfdp v1 的 OpenAPI 描述（给第三方提供方看，见 `docs/GUIDE.md 第四部分`） |

`/api/overview` 返回示例（节选）：

```json
{
  "market": {
    "indices": [
      {"code": "000001.SH", "name": "上证指数", "price": 3000.12, "preclose": 2985.3, "change": 14.82, "change_pct": 0.4964,
       "amount": 512300000000, "minute": {"date": "2026-11-11", "preclose": 2985.3, "points": [{"t": "09:30", "p": 2986.1}], "source": "akshare"}}
    ],
    "index_source": "akshare",
    "stats": {"date": "2026-11-11", "up": 3120, "down": 1890, "flat": 160, "limit_up": 58, "limit_down": 6, "amount": 1180000000000},
    "trading_date": "2026-11-11", "updated_at": "2026-11-11 15:10:02"
  },
  "market_job": {"running": false, "error": null},
  "pools": {
    "pools": [
      {"key": "gushi:jjdt", "name": "竞价多头", "count": 12, "avg_change": 1.85, "up": 8, "down": 4, "from": "数据源", "trading_date": "2026-11-11",
       "best": {"code": "300825", "name": "阿尔特", "change_pct": 9.98},
       "series": [{"date": "2026-11-10", "count": 10, "avg_entry_change": 3.2, "avg_since_entry": 1.4, "picked": ["300825 阿尔特"], "backfill": false}]}
    ],
    "quote_source": "akshare", "days": 5, "errors": []
  },
  "pools_job": {"running": false, "error": null},
  "refresh_secs": 30
}
```

- 金额单位是元，涨跌幅单位是百分数（1.85 表示 1.85%）。
- `series` 是近 N 个交易日每天的运行记录（含补录）里该股池的成分：`avg_entry_change` 是入池那一刻的平均涨幅，`avg_since_entry` 是从入池价到现价的平均涨跌。
- `from` 说明成分的来源：「数据源」是当前股池，「运行记录」或「运行记录（补录）」是数据源取不到时退回到的最近一次运行。
- **股池走势口径**（每个股池两条曲线）：
  - 当天分时：当前成分股分时涨跌（相对各自昨收，%）的等权平均；个股分时先用腾讯、失败用 akshare，最多取 `[home].pool_curve_members` 只。
  - 近 N 日：用**当前**成分股的日K回看近 `[home].pool_days` 个交易日，每天的等权平均涨跌和累计涨跌。gushi 只能查当天和上一交易日的股池，所以这条线不是当时的股池，**有幸存者偏差**；返回里带口径说明，网页照原样显示。只有一天数据时从 0 起画。
  - 上一交易日的股池也会补进入池记录（`series`）。
- 配置：`[home] indices / pool_days / pool_curve_members / refresh_secs`，行情顺序：`[market] quote_sources`。

#### 顶栏：自动交易开关

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/status` | 运行模式、`trading_on`、连接状态、定时任务等；顶栏用 |
| POST | `/api/trading` | 唯一的自动交易开关：`{"on": true}` 打开（顺带解除命令行 `halt`）；`{"on": false}` 关闭，并撤掉本系统当天还没成交的**自动**委托（实盘、按合同号撤），**手动单不撤**。返回里 `cancelled` 列出撤掉的委托，撤不掉的写在 `cancel_errors` |
| POST | `/api/halt` | 旧接口，等同于 `POST /api/trading {"on": false}` |
| POST | `/api/resume` | 旧接口，只解除 `halt` 停止状态，不打开自动交易 |

原来的「一键停止」已经合并进这个开关。自动交易开关只管系统**自动**发的委托，手动下单不受它限制。

#### 设置页

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/config` | 当前配置（敏感字段掩码）、网页改不了的字段（监听地址、数据目录）、当前运行模式 |
| POST | `/api/config/validate` | 只校验不保存；返回 `changes`（会改哪些项） |
| PUT | `/api/config` | 校验后写回 TOML（留 `.bak`）并立即生效。**切实盘无门槛**：不要求确认文字，也没有三重开关，是否实盘只看 `[execution].mode`；切换时照样记日志、推钉钉 |
| POST | `/api/test/source` | 用请求体里的数据源配置（不用先保存）测试连接：`{"name": "vendor_x", "config": {...}}` |
| POST | `/api/test/xiadan` | 用请求体里的下单程序配置测试连接：`{"live": {...}}`。只调用只读的 `GET /api/status`（连通）和 `GET /api/snapshot?only=money`（验证口令，只读缓存）；403 时提示「口令要和下单程序那边完全一致」 |
| GET | `/api/broker/diag` | 下单程序连接诊断：连接状态、最近调用、对账事件（不含券商资金和持仓） |
| POST | `/api/notify/test` | 用表单当前值发测试推送，不写配置 |

#### 账户：资金、持仓、委托、手动下单

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/account` | 账户页一次拿全：资金、持仓、收益、自动获取持仓开关。开关关闭时只给本地账本，`broker` 为 `null` |
| GET | `/api/positions_mode` | `{"auto": false, "mode": "live", ...}` |
| POST | `/api/positions_mode` | `{"auto": true}` 打开自动获取（**只有实盘能打开**）；`false` 改为手动账本 |
| POST | `/api/funds/manual` | 手动填写资金：`{"available": 50000, "balance": 80000}` |
| POST | `/api/positions/manual` | 手动新增或修改持仓：`{"code": "600036", "name": "招商银行", "quantity": 500, "avg_cost": 39.8, "sellable": 200}` |
| DELETE | `/api/positions/manual/{code}` | 删除手动持仓 |
| GET | `/api/orders?limit=200&signals=1` | 委托记录（当前运行模式那一本，`data/journal/<模式>.json`）：系统委托 + 手动补记。`signals=0` 不含只记信号的 |
| POST | `/api/orders/manual` | 补记一笔委托（不带 `id`）或修改一笔（带 `id`）：`symbol / side / price / quantity / status / filled_qty / fill_price / fee / time / strategy` |
| DELETE | `/api/orders/manual/{id}` | 删除一笔手动补记的委托 |
| GET | `/api/trade/quote/{code}` | 手动下单表单用：名称、现价、涨跌停价、持仓和可卖数量 |
| POST | `/api/trade/order` | 手动下单：`{"symbol": "601988", "side": "buy", "price_type": "limit", "price": 5.10, "quantity": 100}` |
| POST | `/api/trade/cancel/{id}` | 撤一笔未成交 / 部分成交的委托，然后刷新委托状态 |
| POST | `/api/orders/refresh` | 刷新委托状态（实盘）。手动账本模式下只读一次委托表（`GET /api/snapshot?only=orders`），不读持仓和资金 |
| GET | `/api/broker`、POST `/api/broker/sync` | 券商快照和手动同步；自动获取持仓关闭时不返回券商数据，`sync`（除 `how=status`）直接 400 |

规则：

- **手动账本**（自动获取持仓关闭）：后台不读下单程序的资金和持仓，卖出前不对账。资金、持仓、委托手动维护。补记或修改委托时，成交数量比已入账的多出来的部分，按成交均价自动记进持仓和可用资金；已入账的成交不能改小。模拟盘也能手填，手填的持仓和资金当作「转入 / 转出」，初始资金跟着调整，收益率不跳变。有在途委托或待确认状态的票不能手动改。
- **手动下单**：`price_type` 为 `limit` 或 `market`。市价单按现价上下 `[risk].market_protect_pct`（默认 1%）换成数字限价、不超过涨跌停价再发出。照样过风控（买入看单笔上限、每日买入金额；卖出看可卖数量 / T+1；都看涨跌停、偏离参考价、交易时段），被拒时 400 并在 `detail` 里写原因；**不受自动交易开关限制**。实盘发给下单程序 `/api/order`，模拟盘走模拟成交，演练只生成委托请求、不发出。来源记为「手动」，写日志、推钉钉。
- **撤单**：只在实盘可用，调下单程序 `/api/cancel`，只按合同号撤（没有合同号时先读一次委托表对上）。手动补记的委托不是本系统发出的，不能在这里撤。
- 格式不对的数量、价格、成本会被拒绝（400，`detail` 里写原因）；不存在的持仓或委托返回 404。

---

## 第四部分 标准数据接口（qfdp v1，原 docs/DATA_PROVIDER.md）

这份文档写给**数据提供方**：你有一套行情、分时、日K、指数、市场情绪或者选股股池的数据，想让牛来量化直接用上。按下面的规范实现几个 HTTP GET 接口，然后在网页「设置 → 数据源」里添加一个「标准接口」类型的数据源，填 URL 和 token，点「测试连接」就行。

- 协议名：`qfdp`，版本 `1`。规范的代码实现是 `qfpy/dataproto.py`（校验函数、OpenAPI 描述、把任意数据源包装成标准服务的 `make_provider_app()`），这份文档和它一致。
- OpenAPI 3.0 描述：牛来量化网页运行时访问 `http://127.0.0.1:8090/api/dataproto/openapi.json`；用 `make_provider_app()` 起的服务自己也带 `/openapi.json`。也可以直接导出：`python -c "import json,qfpy.dataproto as d;print(json.dumps(d.openapi_spec(),ensure_ascii=False,indent=1))" > qfdp.json`。
- 可以对照的示例提供方：`python examples/data_provider_server.py`（随机演示数据，默认监听 127.0.0.1:9100，token `demo-token`）。

不合规范的返回值会被牛来量化**直接报错**（测试连接和日志里会写哪个字段不对），不会悄悄混进数据；行情顺序里它失败时自动换下一个源。

### 1. 通用约定

#### 1.1 请求

- 全部是 `GET`，参数在 query string 里。接口都是只读的。
- 服务根地址随意，比如 `http://10.0.0.5:9100` 或 `https://data.example.com/qfdp`，下面的路径都拼在它后面。
- **鉴权**：请求头 `Authorization: Bearer <token>`。不需要鉴权的服务可以忽略它；token 不对时返回 HTTP 401 和错误码 `UNAUTHORIZED`。牛来量化里 token 存在配置中，网页上显示为掩码。
- 建议单次请求 10 秒内返回（牛来量化默认超时 10 秒，可在数据源里改 `timeout_secs`）。

#### 1.2 响应信封

所有接口都返回 JSON，外面包一层固定的信封：

```json
{"ok": true, "data": {"code": "600000.SH", "price": 10.52}, "ts": 1762845000000}
```

`data` 里放什么看具体接口（见第 3 节），上面只是示意。

出错时：

```json
{"ok": false, "error": {"code": "BAD_REQUEST", "message": "代码 abc 不是标准格式（如 600000.SH）"}, "ts": 1762845000000}
```

- `ts`：服务端生成响应的时刻，**毫秒 Unix 时间戳**（UTC 纪元）。
- 出错时 HTTP 状态码和错误码对应如下：

| 错误码 | HTTP | 什么时候用 |
|---|---|---|
| `BAD_REQUEST` | 400 | 参数缺失或格式不对（代码、日期、分页） |
| `UNAUTHORIZED` | 401 | 没带 token 或 token 不对 |
| `FORBIDDEN` | 403 | token 对，但没有访问这项数据的权限 |
| `NOT_FOUND` | 404 | 没有这只票 / 这个股池 / 这一天的数据 |
| `RATE_LIMITED` | 429 | 调用太频繁；可在 `error` 里带 `retry_after`（建议等待的秒数） |
| `INTERNAL` | 500 | 其他服务端内部错误 |
| `NOT_SUPPORTED` | 501 | 这个接口没实现（也可以直接不在 `/meta` 里声明这项能力） |
| `UPSTREAM_ERROR` | 502 | 你自己的上游出错 |
| `UNAVAILABLE` | 503 | 暂时不可用（维护中、非交易时段没有数据等） |

上面这 9 个就是规范里的全部错误码（与 `qfpy/dataproto.py` 的 `ERROR_CODES` 一致），请不要自造其他错误码。

#### 1.3 代码、日期、时间

- **代码**统一用「6 位数字 + 点 + 交易所」：`600000.SH`、`000001.SZ`、`430047.BJ`。指数也一样：`000001.SH` 是上证指数，`399001.SZ` 是深证成指（同一个 6 位数字在不同交易所是不同的东西，所以交易所后缀不能省）。牛来量化请求时也只发这种格式。
- **日期**：`YYYY-MM-DD`，指北京时间的自然日。
- **分时时刻**：`HH:MM`，北京时间。
- **行情时间戳**：毫秒 Unix 时间戳。

#### 1.4 单位（必须遵守）

| 字段 | 单位 |
|---|---|
| 价格 `price` `open` `high` `low` `close` `preclose` `p` `avg` | 元（指数为点） |
| 涨跌幅 `change_pct`、股池里的 `change_rate` `open_rise` | 百分数：`1.85` 表示 1.85% |
| 涨跌额 `change` | 元（指数为点） |
| 成交量 `volume` `vol` | **股**（不是手） |
| 成交额 `amount` `amt` | **元**（不是万元、亿元） |
| 股池里的 `market_value` | 亿元 |

数值字段可以是 `null`（不知道），但不能是字符串。

#### 1.5 分页

只有股池成分 `/pools/{key}` 分页：`page` 从 1 开始，`page_size` 1~500（默认 200）。返回里带 `page` `page_size` `total`，牛来量化会自动翻完所有页。

### 2. 接口一览

| 路径 | 能力名 | 参数 | 牛来量化拿它做什么 |
|---|---|---|---|
| `/meta` | （必须） | 无 | 识别服务、看它支持哪些能力 |
| `/quotes` | `quote` | `codes`（逗号分隔，最多 200 个） | 实时行情：首页股池走势、持仓现价 |
| `/minute` | `minute` | `code`、`date`（可选，默认当天） | 个股分时：分时类因子、卖出因子 |
| `/daily` | `daily` | `code`、`start`、`end` | 日K：均线、回看表达式、回测 |
| `/indices` | `index` | `codes` | 首页指数点位和涨跌幅 |
| `/index_minute` | `index_minute` | `code`、`date`（可选） | 首页指数分时图 |
| `/market_stats` | `market_stats` | `date`（可选） | 首页市场情绪 |
| `/pools` | `pool` | 无 | 列出你提供的股池 |
| `/pools/{key}` | `pool` | `date`（可选）、`page`、`page_size` | 股池成分：选股前池、牛来时段 |

只实现你有的那部分就行，比如只提供股池，就只实现 `/meta`、`/pools`、`/pools/{key}`，并在 `/meta` 里只声明 `pool`。

### 3. 各接口的格式和例子

下面的例子都是用示例提供方实际请求出来的（数据是随机演示数据），数组只保留了第一项。

#### 3.1 `GET /meta`

```json
{"ok": true, "data": {"protocol": "qfdp", "version": "1.0", "name": "demo", "title": "演示数据提供方",
  "capabilities": ["quote", "minute", "daily", "index", "index_minute", "market_stats", "pool"]}, "ts": 1762845000000}
```

- `protocol` 必须是 `"qfdp"`，`version` 是 `"1.0"`。
- `capabilities` 只能从这 7 个里选：`quote` `minute` `daily` `index` `index_minute` `market_stats` `pool`。牛来量化只会调用声明了的能力。

#### 3.2 `GET /quotes?codes=600000.SH,000001.SZ`

```json
{"ok": true, "data": [
  {"code": "600000.SH", "name": "浦发银行", "price": 10.52, "preclose": 10.40, "open": 10.41, "high": 10.60, "low": 10.38,
   "change": 0.12, "change_pct": 1.1538, "volume": 52310000, "amount": 548000000.0, "ts": 1762844990000}
], "ts": 1762845000000}
```

必填：`code` `price`；其他可以是 `null`。请求里有、你没有的代码，直接不返回那一项即可。

#### 3.3 `GET /minute?code=600000.SH&date=2026-11-11`

```json
{"ok": true, "data": {"code": "600000.SH", "date": "2026-11-11", "preclose": 10.40, "open": 10.41, "high": 10.60, "low": 10.38,
  "points": [{"t": "09:30", "p": 10.41, "avg": 10.41, "vol": 820000, "amt": 8536200.0}]}, "ts": 1762845000000}
```

- `points` 按时间升序，每分钟一个点；`t` 是 `HH:MM`，`p` 是这一分钟的收盘价，`avg` 是当日均价（均价线），`vol` / `amt` 是**这一分钟**的成交量（股）/ 成交额（元），不是累计值。
- 必填：`code` `date` `points`，每个点的 `t` `p`。
- 这一天没有数据（非交易日、未开盘）返回 `NOT_FOUND`。

#### 3.4 `GET /daily?code=600000.SH&start=2026-11-09&end=2026-11-11`

```json
{"ok": true, "data": {"code": "600000.SH", "bars": [
  {"date": "2026-11-09", "open": 10.30, "high": 10.45, "low": 10.25, "close": 10.40, "volume": 48100000, "amount": 500100000.0}
]}, "ts": 1762845000000}
```

- 不复权或前复权都可以，但同一个服务要始终一致（建议前复权，均线和回测更准）。
- `bars` 按日期升序，只含交易日；必填 `date` `open` `high` `low` `close`。

#### 3.5 `GET /indices?codes=000001.SH,399001.SZ`

格式和 `/quotes` 一样（价格单位是点）：

```json
{"ok": true, "data": [
  {"code": "000001.SH", "name": "上证指数", "price": 3002.15, "preclose": 2985.30, "open": 2986.00, "high": 3008.80, "low": 2980.10,
   "change": 16.85, "change_pct": 0.5644, "volume": 41000000000, "amount": 512300000000.0, "ts": 1762844990000}
], "ts": 1762845000000}
```

首页默认请求：`000001.SH` 上证指数、`399001.SZ` 深证成指、`399006.SZ` 创业板指、`000688.SH` 科创50、`000300.SH` 沪深300。

#### 3.6 `GET /index_minute?code=000001.SH&date=2026-11-11`

格式和 `/minute` 一样（价格单位是点，`avg` 可以是 `null`）。

#### 3.7 `GET /market_stats?date=2026-11-11`

```json
{"ok": true, "data": {"date": "2026-11-11", "up": 3120, "down": 1890, "flat": 160, "limit_up": 58, "limit_down": 6,
  "suspended": 9, "amount": 1180000000000.0}, "ts": 1762845000000}
```

- `up` / `down` / `flat`：沪深京 A 股上涨 / 下跌 / 平盘家数；`limit_up` / `limit_down`：涨停 / 跌停家数；`suspended`：停牌家数；`amount`：两市（含北交所也可以，说清楚即可）成交额，元。
- 必填：`date` `up` `down`；家数必须是非负整数。

#### 3.8 `GET /pools`

```json
{"ok": true, "data": [
  {"key": "demo_hot", "name": "演示热门池", "desc": "随机生成的演示股池"}
], "ts": 1762845000000}
```

- `key` 只用字母、数字、`_`、`-`。在牛来量化里，这个股池的全名是 `<数据源名>:<key>`，例如 `vendor_x:demo_hot`，可以勾进前池或某个牛来时段。
- `desc`（可选）是股池说明，只用来显示。

#### 3.9 `GET /pools/{key}?date=2026-11-11&page=1&page_size=200`

```json
{"ok": true, "data": {"key": "demo_hot", "name": "演示热门池", "trading_date": "2026-11-11", "page": 1, "page_size": 200, "total": 12,
  "stocks": [{"code": "600000.SH", "name": "浦发银行", "fields": {"price": 10.52, "change_rate": 1.15, "open_rise": 0.10, "market_value": 3088.0, "my_score": 87}}]},
  "ts": 1762845000000}
```

- `trading_date`：这批成分属于哪个交易日。当天还没出数据时，返回空的 `stocks` 和当天日期（牛来时段会每 60 秒重试），不要返回前一天的成分充数。
- `fields` 是任意数值字段：约定的 `price`（元）、`change_rate`（涨幅 %）、`open_rise`（开盘涨幅 %）、`market_value`（总市值，亿元）会被内置因子用到；你自己加的字段（比如 `my_score`）可以直接写进买入漏斗的表达式，如 `my_score > 80`。
- 首页「股池走势」里的「入池以来」用 `fields.price` 当入池价，所以尽量提供它。

### 4. 在牛来量化里接入

1. 网页「设置 → 数据源」底部：填一个英文名称（如 `vendor_x`），类型选「标准接口」，点「添加数据源」。
2. 填 `url`（服务根地址）和 `token`，点它旁边的「测试连接」。测试用的是表单里当前的值，不用先保存；它会调用 `/meta`，再把你声明的每项能力各试一次并校验格式，逐项显示 ✓ / ✗ 和原因。
3. 把 `vendor_x` 放进同一页的「行情顺序」：实时行情 / 指数 / 情绪（`market.quote_sources`）、分时（`market.minute_sources`）、日K（`market.daily_sources`）。放在第一位就是主力，失败时自动换下一个。
4. 股池：在「股池」或「牛来时段与补录」里勾选 `vendor_x:<key>`。
5. 点「保存并生效」，写回 `config/pipeline.toml`（留 `.bak`），不用重启。

写在配置文件里是这样：

```toml
[sources.vendor_x]
type = "standard"
enabled = true
url = "http://10.0.0.5:9100"
token = "你的 token"
timeout_secs = 10

[market]
quote_sources = ["vendor_x", "akshare"]
minute_sources = ["vendor_x", "gushi", "akshare"]
```

### 5. 用 Python 快速做一个提供方

不想自己写 HTTP 服务的话，继承牛来量化的 `DataSource`，实现你有的方法，再用 `make_provider_app()` 包装成符合规范的服务（信封、错误码、鉴权、分页、字段校验都由它处理）。完整可运行的例子见 `examples/data_provider_server.py`。

`DataSource` 的方法和能力、HTTP 接口的对应关系：

| 方法 | 能力名 | 对应接口 |
|---|---|---|
| `get_quotes(codes)` | `quote` | `/quotes` |
| `get_minute(code, date)` | `minute` | `/minute` |
| `get_daily(code, start, end)` | `daily` | `/daily` |
| `get_indices(codes)` | `index` | `/indices` |
| `get_index_minute(code, date)` | `index_minute` | `/index_minute` |
| `get_market_stats(date)` | `market_stats` | `/market_stats` |
| `list_pools()`、`get_pool(key, date)` | `pool` | `/pools`、`/pools/{key}` |

方法里个股代码用 6 位，包装时会自动转成 `600000.SH` 这样的标准代码；指数代码始终带交易所后缀。在 `capabilities` 里只写你实现了的能力。

```python
import uvicorn
from qfpy.dataproto import make_provider_app
from qfpy.devtools.demo_provider import DemoSource   # 换成你自己的 DataSource 子类

app = make_provider_app(DemoSource(), token="demo-token")
uvicorn.run(app, host="127.0.0.1", port=9100)
```

也可以反过来：不起服务，直接写一个数据源插件放进 `plugins/`（见 `plugins/example_data_provider.py`）：类上加 `@register_source` 装饰器，`type_name` 就是配置里的 `type`。放好后在「日志与系统」里点「重新加载插件」即可。

```python
from qfpy.datasources import DataSource, register_source

@register_source
class MyIndexSource(DataSource):
    type_name = "my_index"
    capabilities = ("index", "index_minute")

    def get_indices(self, codes=None):
        ...  # 返回 [{"code": "000001.SH", "name": "上证指数", "price": ..., "preclose": ...}]

    def get_index_minute(self, code, date):
        ...  # 返回与 get_minute 相同的结构
```

### 6. 自检清单

- [ ] 所有响应都有 `ok` 和毫秒 `ts`，成功时有 `data`，失败时有 `error.code` 和 `error.message`，HTTP 状态码和错误码对得上。
- [ ] 代码都带交易所后缀（`.SH` / `.SZ` / `.BJ`）。
- [ ] 成交量是股、成交额是元、涨跌幅是百分数。
- [ ] 分时的 `vol` / `amt` 是每分钟的量，不是累计。
- [ ] `/meta` 里声明的能力都真的能用；没实现的不要声明。
- [ ] 在牛来量化里点「测试连接」全部 ✓。
