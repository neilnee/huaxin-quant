# 全球宏观与流动性雷达参考口径

## 统一观测结构

```json
{
  "source_id": "treasury_yield",
  "series_id": "UST_10Y",
  "observed_at": "2026-08-19",
  "value": 4.65,
  "unit": "percent",
  "frequency": "daily",
  "source_url": "https://...",
  "raw": {}
}
```

`observations` 只保存来源事实。派生的曲线斜率、净流动性或异常波动必须在后续业务层计算，不回写为来源原始值。

## 统一事件结构

```json
{
  "source_id": "fed_press_rss",
  "event_id": "sha256-or-guid",
  "published_at": "2026-08-19T18:00:00+00:00",
  "event_time": null,
  "title": "Official release title",
  "url": "https://...",
  "category": "official_release",
  "official_fact": "Official release title",
  "raw": {}
}
```

`published_at` 是来源发布时间，`event_time` 是正文明确给出的实际发生时间；无法确认时保持为空。媒体解释、市场影响和 LLM 推断不得写入 `official_fact`。

## 首批序列

| 来源 | 标准序列 | 单位 | 频率 | 语义 |
|---|---|---|---|---|
| NY Fed Rates | `NYFED_<TYPE>` | percent/index | daily | SOFR、EFFR 等参考利率；SOFR Index 保持指数单位 |
| NY Fed ON RRP | `NYFED_ONRRP_ACCEPTED` | usd | daily | 当日逆回购接纳金额 |
| Treasury Yield | `UST_1M` 至 `UST_30Y` | percent | daily | 官方期限收益率曲线 |
| FiscalData TGA | `TGA_CLOSING_BALANCE` | million_usd | daily | 财政部一般账户日终余额 |
| Debt to Penny | `US_DEBT_PUBLIC`、`US_DEBT_INTRAGOV`、`US_DEBT_TOTAL` | usd | daily | 联邦债务余额 |
| ECB FX | `ECB_EURUSD` | usd_per_eur | daily | 欧元兑美元官方参考汇率 |
| Cboe VIX | `VIX_OPEN/HIGH/LOW/CLOSE` | index | daily | VIX 日线 |

NY Fed 响应字段存在不同产品结构，参考利率只允许从 `percentRate`、`rate` 或明确的 `index` 字段读取；`percentRate/rate` 使用 `percent`，`index` 使用 `index`，找不到数值字段时整条记录拒绝写入。ON RRP 的 `totalAmtAccepted` 是美元整数，不进行十亿美元缩放。

FiscalData 的 Daily Treasury Statement Table I 当前把各行“今日数值”放在 `open_today_bal`；采集器只接受名称明确包含 `Treasury General Account` 和 `Closing Balance` 的行，并优先兼容未来可能恢复的 `close_today_bal`。不得把 Opening Balance、Deposits 或 Withdrawals 当作 TGA 日终余额。

## 健康评级

按查询窗口内的访问记录聚合：

- `GREEN`：成功率不低于 98%，且最近成功数据未超过该来源的新鲜度容忍窗口；
- `YELLOW`：至少有一次成功，但成功率不足 98%，或最近数据已陈旧；
- `RED`：窗口内没有成功记录；
- `UNKNOWN`：窗口内没有探测记录。

事件型 RSS 不以“今天是否发文”判断新鲜度，只检查响应可解析且至少包含一条有效事件。数据型来源必须解析出最新观测日期。

## 数据语义边界

- SOFR/EFFR、ON RRP、TGA、Fed 资产负债表属于美元流动性或资金价格代理变量。
- Treasury、ECB FX、VIX 属于市场价格或风险定价。
- CFTC COT 属于带发布滞后的期货仓位。
- TIC、BIS 属于月度或季度跨境流量/信贷统计。
- EPFR 等基金申赎才属于真实基金净流量，但需要商业授权。

不得把收益率下跌、VIX 变化、CFTC 仓位或 TGA 变化直接命名为“全球资金净流入”。
