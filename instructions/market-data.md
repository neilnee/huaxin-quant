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

消费者以 `ensure_ready(as_of, profile)` 获取数据快照；当前 `market_regime` profile 要求全 A 日线、六个宽基和全部行业/概念/风格成分快照。模型二暂不迁移，后续通过同一入口逐步接入。
