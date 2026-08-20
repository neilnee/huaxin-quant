# 全球宏观与流动性雷达（Global Macro）

- **核心目标**：稳定采集全球宏观官方事件与美元流动性、利率、汇率、波动率代理变量，为后续重大事件识别提供可审计的原始事实。
- **模块边界**：本模块只负责信源健康、字段标准化、缓存和原始事件归档；不生成交易建议，不改写 Pool、Quant、Bloom、Market Regime 或资金观测结果。
- **主脚本**：`scripts/global_macro.py`。
- **策略配置**：`strategies/global-macro.json`。
- **本地缓存**：`cache/global_macro/global_macro.sqlite`。

## 运行方式

```bash
# 探测所有启用信源，记录访问、解析、新鲜度和字段健康
python3 scripts/global_macro.py probe

# 只探测部分信源
python3 scripts/global_macro.py probe --sources nyfed_rates,treasury_yield

# 获取并缓存 P0 数据与官方事件；同一观测可重复执行
python3 scripts/global_macro.py fetch

# 查看最近七天信源状态
python3 scripts/global_macro.py status
python3 scripts/global_macro.py status --days 7
```

`probe` 与 `fetch` 默认串行访问，遵循配置中的重试、退避和请求间隔，不得用并发绕过源站限制。网络失败不得删除或覆盖既有数据。单一信源失败时继续处理其余信源，命令最后以摘要标明 `complete` 或 `partial`。

## 首批数据范围

- NY Fed：SOFR、EFFR 等参考利率，以及 ON RRP 操作结果。
- US Treasury：每日收益率曲线。
- FiscalData：TGA 余额和 Debt to the Penny。
- ECB：EUR/USD 官方参考汇率。
- Cboe：VIX 日线。
- Fed、ECB：官方新闻 RSS。

CFTC COT、Fed H.4.1、Treasury TIC 等低频或 HTML 来源保留为后续适配项，不在首批 P0 采集器中伪装为实时资金流。

## 数据规则

1. 原始数值写入统一 `observations` 表，必须保存来源、序列、观测日期、单位、频率、抓取时间和原始行。
2. 官方 RSS 写入 `events` 表，标题只是官方已发布事实的最小摘要，不得自动扩写因果关系。
3. 同一来源、序列和观测日期必须幂等更新；缺失值保持 `null`，不得写成零。
4. 每次访问均写入 `source_health`，包括 HTTP 状态、延迟、内容哈希、最新观测日期、解析结果和错误。
5. 日频数据的新鲜度按自然日容忍窗口判断，周末和节假日不得简单视为失效。事件 RSS 只检查访问和结构，不要求每日必须有新事件。
6. 价格、流动性代理变量、期货仓位和真实基金申赎必须在语义上分开，具体字段见 `instructions/global-macro-ref.md`。

## 降级原则

- 首次网络或 5xx 错误按配置指数退避重试；4xx 除 408/429 外不盲目重试。
- 最新请求失败时保留数据库中最近成功观测，并在健康状态中明确标记陈旧或失败。
- 解析字段缺失、日期无效或响应格式变化视为失败，不得只因 HTTP 200 写入缓存。
- FRED、IMF DataMapper、OPEC 和商业通讯社当前不属于 P0 主链路，不做无授权网页抓取。

字段结构、信源目录和健康评级口径见 `instructions/global-macro-ref.md`。
