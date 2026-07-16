# 市场状态与板块热度（Market Regime）

- **核心目标**：基于共享市场数据层，独立计算市场趋势、波动、广度、板块轮动与板块热度。
- **策略边界**：不读取 Pool/Quant/Bloom 输出，不改写模型二 VCP 结构、评分或买点。
- **数据源**：通达信行情服务器的宽基/个股日线、`tdxhy.cfg + zhb.zip/incon.dat` 行业映射，以及 `block_zs.dat`、`block_fg.dat`、`block_gn.dat` 板块文件。
- **策略配置**：`strategies/market-regime.json`。
- **主脚本**：`scripts/market_regime.py`；基础数据由 `scripts/data/market_data_service.py` 管理。

## 运行方式

```bash
# 首次初始化：通过共享数据层创建本地 SQLite，并回填每只全 A 标的近 300 日线
python3 scripts/market_regime.py init --lookback 300

# 盘后增量更新：刷新板块快照、证券母体与最近日线
python3 scripts/market_regime.py update

# 仅读取共享数据库，生成市场状态、板块阶段数据与报告数据
python3 scripts/market_regime.py run

# 检查数据库、覆盖率和最近完整交易日
python3 scripts/market_regime.py status

# 直接双击打开本地页面
# dashboard/index.html
```

`init` 支持 `--resume`；初始化中断或局部失败后，不重复请求已完成标的。`--max-codes` 仅用于小样本接口验证，不能产生正式市场状态。

## 数据持久化与输出

```text
cache/market_data/market_data.sqlite
cache/market_data/blocks/<YYMMDD>/
market/market_regime_<YYMMDD>.json
market/market_regime_<YYMMDD>.md
market/data/market_context_<YYMMDD>.json
market/data/latest.json
dashboard/index.html
dashboard/data/market_context.js
market/sector_heat_<YYMMDD>.csv
market/stock_strength_<YYMMDD>.csv
market/concept_strength_<YYMMDD>.csv
```

共享 SQLite 保存原始日线、证券母体、日度母体成员、板块快照和任务日志；市场状态库额外保存每日板块排名、广度、量能与相对强度，作为阶段判断依据。`market/` 保存某日计算结果与供页面读取的 JSON；三者均为本地运行数据，不提交 Git。市场模块不自行下载或维护基础数据。

`stock_strength` 对每只有效 A 股输出 5/20/60 日全市场 RPS、申万二级行业 RPS、趋势位置、量能状态与强度分；`concept_strength` 为“股票 × 通达信概念”关系表，输出概念热度与概念内 20 日 RPS。RPS 只使用当日收盘前已经完成的横截面收益排名。

## 页面数据与状态

页面位于本地运行目录 `dashboard/`，是全系统统一数据分析入口。市场模块通过 `dashboard/data/market_context.js` 发布数据包；双击 `dashboard/index.html` 即可查看，不需要 HTTP 服务或公网发布。页面不得重新计算指标、访问通达信或写入数据库。当前 Markdown 保留为简要归档。

“板块相对强度排名”支持 `20日` 与 `5日` 两种视图切换；两者均按日期展示当日 Top20，横轴为名次、纵轴为日期、单元格为板块名称。

页面的板块排名以类别内 **20 日相对强度** 为主，5 日相对强度和排名变化用于观察强化或转弱。相对强度采用板块成员等权收益中位数减全 A 等权收益中位数；绝对收益与广度、量能、连续上榜天数只用于解释状态，不替代主排名。

市场状态包括“趋势扩散、结构性强势、震荡轮动、修复观察、弱势下行”。板块状态包括“持续主线、新晋强化、高位分歧、轮动脉冲、弱势退潮”。状态必须引用最近阶段的指数、排名、广度和量能证据；阶段数据不足时，输出“历史积累中”。

运行时，脚本自动检查最近 20 个交易日的板块阶段指标。缺失日期使用**当前板块成分快照**和已持久化的个股日线做基线回填，输出标记为“当前成分快照回填（非严格点时）”；已存在的每日板块快照及其计算结果不得被该回填覆盖。日报和页面必须展示历史口径，严格点时回测只能使用每日快照积累后的数据。

## 兼容指标

模块输出四个独立分数：

- `trend_score`：六个宽基的 MA20/MA60、MA20 斜率和 20 日收益。
- `volatility_score`：ATR14、10 日实现波动与 10 日振幅；分高代表风险高。
- `breadth_score`：全 A 上涨比例、站上 MA20/MA60 比例与 60 日创新高减创新低比例。
- `rotation_score`：行业、概念、风格、指数成分集合的强势排名重合度与持续性；分高代表轮动快。

状态为 `OFFENSIVE`、`SELECTIVE`、`DEFENSIVE`、`RECOVERY_WATCH`。状态切换需满足配置中的多日确认规则。申万一级、二级、三级行业，概念、风格、指数四类热度分别排名；每类热度由相对强度、成交活跃、上涨扩散、强势股密度和持续性组成。

## 历史边界

个股日线可在初始化时回填；板块成分严格点时可得仅从首次保存每日快照开始。缺少目标日期快照时，脚本不得将当前成分伪装为历史成分。
