# 资金观测数据层参考口径

## 申万—财富通映射

对同层级的申万行业 `S` 和财富通行业 `B`，以个股近 20 日平均成交额 `w(i)` 计算：

```text
coverage(S,B) = sum(w(i), i in S∩B) / sum(w(i), i in S)
purity(S,B) = sum(w(i), i in S∩B) / sum(w(i), i in B)
core_coverage(S,B) = S成交额Top10中属于B的股票数 / Top10有效股票数
jaccard(S,B) = |S∩B| / |S∪B|
score(S,B) = 0.50*coverage + 0.30*purity + 0.20*core_coverage
```

先剔除纯度低于 35% 的财富通行业，再选得分最高的合格行业；覆盖不足时，按尚未覆盖的申万成交额贡献依次增加映射，单个新增板块至少贡献 10% 覆盖，最多三个，合计覆盖达到 80% 后停止。合成权重使用每个板块新增覆盖的申万成交额占比，避免重复成分被重复计权。纯度门槛不满足时，即使该财富通行业覆盖了较多申万成分，也不得用于板块聚合资金确认；这种情形降级为个股资金验证。

映射质量初始口径：

- `high`：合计覆盖率不少于 70%，核心股覆盖率不少于 80%；
- `usable`：合计覆盖率不少于 50%；
- `weak`：合计覆盖率不少于 35%；
- `unavailable`：低于 35% 或没有有效重合。

这些阈值只约束数据可用性，不是交易阈值。

## 标准化资金字段

板块日度字段：

```text
amount
main_inflow
main_outflow
main_net_inflow
large_inflow
large_outflow
medium_inflow
medium_outflow
small_inflow
small_outflow
return_pct
```

个股日度字段：

```text
amount
main_net_inflow
large_net_inflow
super_large_net_inflow
medium_net_inflow
small_net_inflow
financing_buy
financing_repay
financing_balance
```

字段解析优先使用妙想 `rawTable + nameMap`，同时保存首次成功返回的底层字段编码作为响应契约。金额统一转换为元，百分比保留数值百分比。无法识别的 `-`、空值或未披露值保存为 `null`。

## 返回结构

板块接口返回：

```json
{
  "sw_code": "X30",
  "sw_level": 1,
  "mapping_version": "20260809T120000",
  "mapping_quality": "high",
  "mapped_blocks": [{"bk_code": "BK1200", "weight": 1.0}],
  "rows": [{"trade_date": "2026-08-07", "components": []}],
  "status": "complete"
}
```

个股接口按代码返回标准化日度行，并附 `complete`、`partial`、`unavailable` 或 `error` 状态。调用方必须根据状态降级，不得把数据缺失解释为资金中性。
