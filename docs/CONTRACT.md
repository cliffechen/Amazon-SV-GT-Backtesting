# 数据与输出契约

本文记录程序之间必须遵守的格式。它用于防止两家 Amazon 数据被误混、缓存被覆盖，以及回测在换机器后无法追查。

## 成分目录

`config/catalog_mappings.json` 是经过人工复核、可进入研究的 52 个成分映射。每项包含稳定的 `id`、`family_id`、默认关键词和供应商专用查询词。`ingredient_db.json` 只是候选知识库，不能直接当训练样本。

运行 `python -m predictor audit` 后生成：

- `data/processed/catalog.json`：完整派生目录；
- `data/processed/audit.json`：源文件、映射文件的 SHA-256、选择数量、排除原因和警告。

同义词或剂型如果属于同一成分族，必须共用 `family_id`，否则留组验证会把同一种东西同时放进训练和考试。

## 原始缓存

新缓存使用以下隔离目录：

```text
data/raw/{provider}/US/{ingredient_id}/{kind}/{snapshot_date}-{request_hash}.json
```

- `provider`：`sellersprite` 或 `sif`；
- `kind`：`amazon` 或 `google`；Google 目前只来自 SellerSprite；
- `snapshot_date`：采集快照日期，不等于最后一个数据周；
- `request_hash`：规范化请求的摘要，避免不同查询互相覆盖。

SIF 一次最多请求 5 个关键词。完整批次先保存到 `data/raw/sif/US/_batches/history/`，通过关键词集合、周标签和数组长度校验后，再原子拆出逐词缓存。完全相同的同日重复可以去重；“周六紧邻周日且搜索量和排名都完全一致”的已知错位副本也可去重。其他日期错位或数值冲突必须拒绝并保存在 `_quarantine/invalid/`。

旧目录 `data/raw/US/` 只用于向后兼容读取。读取前必须核对信封中的 `source`，不得把兼容目录当作来源证明。程序不再向这个目录写入新 Amazon 缓存。

每个缓存信封至少包含：

```json
{
  "source": "sellersprite | sif",
  "tool": "工具名",
  "request": {},
  "fetched_at": "ISO-8601 时间",
  "result": {}
}
```

SIF 派生逐词缓存还要保存原批次路径、原批次 SHA-256 和去重计数。原始响应永远不能因为拆分成功而删除。

## 预算账本

`config/provider_budgets.json` 为本机配置，不进 Git；模板是 `config/provider_budgets.example.json`。SellerSprite 与 SIF 各自有周期、可用量、保留量、项目上限和每分钟上限。

`data/budget.sqlite` 记录 job 的状态变化。已发送但返回无效的请求仍保守计入额度。允许的主要状态为预约、已发送、已收到、已暂存、已保存和失败。恢复操作只能处理同一个 job，不能删除账本后把已付费调用当作没发生。隔离响应离线重验成功时，状态可从 `failed_invalid_response` 恢复到 `staged`、`saved`，计费单位保持不变。

## 周表与数据清单

每个 Amazon 供应商都有独立数据集：

```text
data/processed/datasets/{provider}/{snapshot_date}/
  panel.csv
  manifest.json
  data_quality.json
```

`panel.csv` 的必需列为：

```text
ingredient_id,family_id,keyword,name_cn,marketplace,week_end,
searches,rank,google_trend,amazon_provider,amazon_source,google_provider
```

- `week_end` 是周六；
- `searches` 是当前 Amazon 供应商的非负估计搜索量；
- `rank` 是正数 ABA 排名，越小越靠前；
- `google_trend` 是 0–100 的相对指数，不是搜索次数；
- 缺失值保留为空，不得填成 0；
- 一个数据集只允许一种 `amazon_provider`。

SIF 的周日标签映射为同一周的周六结束日，即加 6 天。Google 周日起始口径仍待供应商确认，当前先映射到周六，再额外滞后一周；这个假设必须保留在清单与报告警告中。

`manifest.json` 使用 schema `2.0`，记录 provider policy、周口径、面板与目录 SHA-256、行数、成分数、首末周，以及每个输入缓存的路径、哈希、原批次哈希、请求和采集时间。模型必须核对清单版本、面板哈希和单一供应商策略；不符合时停止。

SellerSprite 数据集仍会原子发布兼容别名 `data/processed/panel.csv`、`panel.manifest.json` 和 `data_quality.json`。SIF 不写这些别名。

## 分析结果

主接口为：

```python
run_analysis(panel_path, output_dir, catalog_path=None, panel_manifest_path=None)
```

`analysis.json` 使用 schema `2.0`，至少包含：

- `as_of`、`scope`、`methodology`、`warnings`；
- `provenance`：Amazon provider、来源数量、面板和清单哈希；
- `metrics`：选模、封存测试和匹配样本消融；
- `backtests`：逐起点预测；
- `ingredients`：历史、13/26 周预测、诊断和单成分回测。

每条回测的严格配对键是：

```text
(ingredient_id, horizon_weeks, split, origin, target_end, model)
```

预测目标是未来 13 或 26 个完整周的平均每周 Amazon 估计搜索量，增长率相对最后 13 周均值。区间是早期验证残差形成的经验区间，不是爆款概率。

同时写入 `training_manifest.json` 和 `models/*.joblib`。只加载自己信任的 joblib 文件。

## 报告与发布

报告接口为：

```python
build_report(analysis_path, output_path, comparison_path=None)
```

第二份分析必须显式传入，报告不得扫描相邻目录自动猜测。双供应商比较必须核对 schema、方法、成分身份和来源，并只使用严格键交集。普通封存测试和未见成分族测试分别展示。两家绝对搜索量口径不同，因此图表使用相同 x 轴、独立 y 轴。

一次完整运行写入不可变目录：

```text
outputs/runs/{provider}-{snapshot_date}-{timestamp}-{id}/
```

完成所有文件后才更新 `outputs/latest-{provider}.json` 指针。SellerSprite 另外原子更新兼容目录 `outputs/latest/`；未完成的运行不能成为 latest。

`predictor audit-summary` 可生成不含逐周历史的可分享 JSON。仓库内路径写成相对路径，付费原始数据、面板、模型和 HTML 均不提交 Git。

所有历史下载都可能被供应商追溯修订，所以当前回测只能称为“基于本次下载历史快照的回测”。真正的 point-in-time 验证需要从现在起持续保存每次快照。
