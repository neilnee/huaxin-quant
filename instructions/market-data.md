# 共享市场数据层（Market Data）

- **核心目标**：统一准备 A 股市场、指数、行业、概念、风格等基础数据，供市场环境模块及后续策略复用。
- **边界**：只拉取、快照、缓存、校验和提供数据；不计算市场状态、不产生选股或交易结论。
- **主入口**：`scripts/data/market_data_service.py`，当前由 `scripts/market_regime.py` 调用。
- **默认日期**：未指定日期时，统一调用 `scripts.shared.expected_trade_date()`，以交易日 15:00 为日度分隔线。

## 数据范围

- 全 A 证券母体及 ST 标记；
- 个股日线 OHLCV；
- 上证综指、沪深 300、中证 500、中证 1000、创业板指、科创 50 日线；
- 通达信行业映射 `tdxhy.cfg + incon.dat`，并生成 X 层级行业快照；
- `block_zs.dat`、`block_fg.dat`、`block_gn.dat` 的指数、风格与概念成分股快照。

## 持久化与质量

```text
cache/market_data/market_data.sqlite
cache/market_data/blocks/<YYMMDD>/
```

数据层负责首次约 300 日的历史回填、盘后增量更新、局部缺口回补和覆盖率校验。输出快照状态为 `READY`、`PARTIAL` 或 `NOT_READY`；消费者只应使用 `READY` 数据。

首次运行时，数据层自动将旧路径 `cache/market_regime/market_data.sqlite` 及板块快照复制迁移到上述共享路径，不重新请求已存在的历史数据。

## 消费约定

消费者以 `ensure_ready(as_of, profile)` 获取数据快照；当前 `market_regime` profile 要求全 A 日线、六个宽基和全部行业/概念/风格成分快照。Quant 已通过 MarketDataService.get_daily_bars 读取共享库并补齐窗口；Pool 扩展池直接查询共享库。不能再将模型二描述为尚未迁移。

## 价格口径与已知边界

文档核对：2026-09-08。daily_bars 保留原始不复权 OHLCV；corporate_actions 与 adjustment_verifications 分别保存公司行为与核验，不以复权结果覆盖原始行。

| 消费者 | 当前价格口径 |
|---|---|
| Quant | 显式申请 point_in_time_qfq，按 run_date 计算 OHLC；成交量/额不变 |
| Pool 扩展 RS | 直接使用 daily_bars 原始收盘价，尚未统一复权 |
| Market 默认计算 | 未显式申请复权的消费者使用原始价格 |
| Backtest 总回报 | 持有期间送转、分红、配股调整；不直接以原始 close 比值代替 |

Quant 的公司行为状态和核验门槛见 [02-quant.md](02-quant.md)。RS 统一、停牌窗口与跨消费者一致性是 [R3](../docs/IMPROVEMENT_ROADMAP.md) 待实现项，本次文档修订不改变价格口径。

expected_trade_date 当前仅处理 15:00 分界及周末回退，不是完整交易所节假日日历。指定历史日期也不能证明后来补入的证券/板块快照当时可用；保留 snapshot_date、来源与 history_basis，严格点时验证单独处理。
