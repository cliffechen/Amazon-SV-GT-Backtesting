# Amazon-SV-GT-Backtesting

这是一个用于研究 Amazon 美国站成分关键词需求的回测系统。它把 Amazon 站内搜索量、ABA 排名和 Google Trends 对齐到周，再站到过去反复预测后面的 13 周或 26 周，最后把预测与实际历史比较。

它回答的是：“这个关键词的搜索需求可能往哪里走，过去用同样方法预测得准不准？”它不把搜索量当销量，也不承诺某个产品会成为爆款。

## 现在已经做到什么

- 52 个经过人工复核的核心成分关键词，保留同义词和成分族关系；
- SellerSprite 与 SIF 两套 Amazon 历史完全分开存储、建表、训练和发布；
- 默认使用 SellerSprite，SIF 作为同规则复算与口径对照；
- Amazon 搜索量、ABA 排名和 Google Trends 统一成周数据；
- 13 周和 26 周滚动回测、时间封存测试、未见成分族测试；
- 最近均值、去年同期和固定 Ridge 模型同场比较；
- 供应商预算账本、失败隔离、断点恢复、原始响应和 SHA-256 来源清单；
- 自包含的中文 HTML 报告，断网也能看图、切换成分和检查回测；
- 每次正式运行写入独立目录，完整生成后才更新 latest 指针。

当前没有把 PPC、ABA 集中度、TikTok 热度或新闻直接放进需求模型。原因是项目里还没有足够长、口径稳定、能按历史时点复原的序列。它们适合先做辅助决策；拿到合格历史后，再用同样的封存回测判断是否真的增加预测价值。

## 方法论，用大白话解释

### 1. 先把研究对象说清楚

例如尿石素 A 使用 `urolithin a`。每个成分都有稳定 ID、成分族和两家供应商的查询词。`magnesium` 与 `magnesium l threonate` 可以是两个查询，但属于同一个成分族；做未见成分测试时必须一起留出，避免把近亲词一边当练习题、一边当考试题。

`ingredient_db.json` 是候选知识库。真正可进入研究的 52 个映射在 `config/catalog_mappings.json`，每次构建目录都会记录该文件的哈希。

### 2. 不把不同单位硬加在一起

| 数据 | 含义 | 在模型中的用途 |
|---|---|---|
| Amazon 搜索量 | 供应商估计的每周站内搜索次数 | 预测目标和主要历史特征 |
| ABA 排名 | 关键词在 Amazon 搜索中的相对位置，数字越小越靠前 | 候选特征，必须通过样本外回测 |
| Google Trends | 0–100 的相对热度 | 候选站外特征，不能当搜索次数 |
| PPC、ABA 集中度 | 广告成本和头部商品份额 | 当前只作竞争参考 |
| 新闻、社媒 | 事件和消费者教育线索 | 当前只作解释与调研线索 |

程序不会设置“Amazon 60%、Google 40%”这种主观权重。每类数据先按自己的历史尺度做特征，再看它在没见过的数据上是否比简单基准更准。

### 3. 统一时间口径

- Amazon 使用周六结束的完整周；
- SIF 返回周日标签，映射到同一周的周六结束日，即加 6 天；
- Google 周界仍待供应商进一步确认，当前先映射到周六，再额外滞后一周；
- 缺失值保留为空，不能把“没有数据”写成“需求为 0”。

所有原始历史都是本次下载到的修订后快照。供应商可能追溯修改历史，所以回测能防止普通的时间穿越，却不能还原几年前当时屏幕上究竟显示了什么。从现在开始持续留存快照，才能逐步形成真正的 point-in-time 验证。

### 4. 遮住未来，反复考试

在每个历史预测起点，模型只能看到这个日期及以前的数据。它预测后面 13 周或 26 周的平均每周搜索量，等目标窗口完整发生后再打分。预测起点每 4 周移动一次。

最后 52 周作为封存测试，前面的数据负责选模型；另外按 `family_id` 留出一组成分，检查模型换到没见过的成分族后是否还能工作。旧训练样本的答案也必须在当次预测前已经发生，避免偷偷使用未来标签。

预测窗口会互相重叠，相关成分也会受到同一市场事件影响。因此几百行回测记录不等于几百次完全独立的实验。

### 5. 先打败简单方法

系统先测试两个容易理解的基准：

- `last13mean`：未来大致等于最近 13 周均值；
- `seasonal`：未来大致等于去年同期。

然后才测试固定的 Ridge 模型：只看 Amazon、加入 ABA、加入 Google、同时加入 ABA 与 Google。特征、正则强度和选模门槛事先固定。候选模型只有在相同验证案例上比最近均值的 WAPE 至少相对改善 5%，才允许被选中。

WAPE 是“总共预测错多少，除以总共真实有多少”，越低越好。`100% - WAPE` 不能称为准确率。报告同时给出单成分结果、方向准确率、普通封存测试和未见成分族测试。

### 6. 为什么成分要多，但不能盲目堆数量

更多成分可以增加上涨、下跌、平稳、季节性和突发尖峰等案例，对共享模型有帮助。前提是关键词含义明确、历史足够长、周口径一致，而且不能把同义词伪装成独立样本。

只收集已经走红的成分会产生选择偏差；只增加相似成分会让样本数量看起来很多，实际信息没有增加。当前 52 个成分是一个可运行的起点，仍需继续补充不同品类和持续前瞻验证。

## 2026-09-26 已验证运行

本次运行的数据截止周是 2026-09-19。可分享的审计摘要在 `docs/results/2026-09-26.json`，其中没有逐周付费历史。

| 项目 | SellerSprite 主口径 | SIF 对照口径 |
|---|---:|---:|
| 可用 Amazon 历史 | 52 个成分，12,353 行 | 51 个成分，12,373 行 |
| 本次新缓存 | 52 个成分全部重新采集 | 2 个成分来自完整新批次，其余复用已核验兼容缓存 |
| 13 周自动选择 | 最近 13 周均值 | 最近 13 周均值 |
| 13 周封存测试 WAPE | 24.64% | 17.40% |
| 26 周自动选择 | 去年同期 | 最近 13 周均值 |
| 26 周封存测试 WAPE | 33.94% | 19.94% |

这些百分比只能在各自供应商口径内解释。SIF 搜索量曲线更平滑时，WAPE 天然可能更低，这不证明它更接近真实搜索次数。

SellerSprite 的 26 周结果给出了很有价值的反例：去年同期法在较早验证期达到预设改善门槛，但在封存测试中 WAPE 为 33.94%，明显差于同批最近均值基准的 24.07%。系统保留这个失败结果，不根据后期成绩回头换模型。它说明过拟合和市场结构变化确实存在，报告里的 3–6 个月数值应作为调研线索，而不是自动选品指令。

双源报告对 51 个成分做了严格身份与来源核对。共同的最近均值基准在普通封存测试中得到 440 个 13 周配对窗口和 308 个 26 周配对窗口；未见成分族另有 70 / 49 个配对窗口。配对键包含成分、预测跨度、评估集、预测起点、目标结束日和模型，不用“日期差不多”的记录凑数。

本轮 SIF 预算上限为 11 个批次。旧校验器在保存前拒绝了 10 个含完全一致重复周的已付费响应，这 10 份原响应没有被旧流程保留下来；最后一个批次已从隔离区离线重验并恢复，未再次计费。账本和失败记录被保留，项目没有擅自增加额度。因此当前结果不能宣称“两家都在同一天完成了 52 个词的全量新采集”。

这也意味着当前 SIF 的用途主要是历史回测对照：49 份兼容缓存截止 2026-09-12，只有新恢复的 `vitamin d3` 和 `zinc` 延伸到 2026-09-19。为了避免不同成分使用不同“今天”，模型不会用较旧截止周冒充最新周预测；因此多数 SIF 成分当前显示数据不足。SellerSprite 主报告的 52 个成分都已更新到同一个最新周，不受这个缺口影响。

## 系统以什么形式交付

核心系统是 Python 项目，HTML 是查看结果的界面，Skill 是可选的操作说明：

- **Python** 负责采集校验、数据对齐、建模、回测和生成报告；
- **HTML** 负责选择成分、查看 13/26 周结果、历史回测、竞争参考和来源审计；
- **Skill** 告诉 Codex 按什么顺序查预算、复用缓存、运行程序和解释限制。

所以 Skill 不适合作为模型本体。即使没有 Skill，只要配置好 Python 环境和自己的数据，程序仍能运行。Skill 位于 `skills/ingredient-forecast/SKILL.md`，本机 Codex 使用的版本位于个人技能目录。

## 运行依赖

| 依赖 | 用途 |
|---|---|
| Python 3.11+ | 运行全部后端；当前环境使用 Python 3.12 |
| NumPy、pandas | 数值计算和周表整理 |
| scikit-learn、SciPy | Ridge 模型和指标 |
| Plotly | 把交互图表内嵌进 HTML |
| requests | MCP HTTP/SSE 客户端 |
| pytest | 自动测试 |
| Node.js，可选 | 检查报告内嵌 JavaScript；不影响日常查看 |
| Edge / Chrome 等现代浏览器 | 打开离线报告 |
| SellerSprite / SIF MCP 账号 | 只有采集新数据时需要，可能消耗额度 |

不需要 GPU，也不需要数据库服务器；预算账本使用 Python 自带的 SQLite。

## 安装与检查

Windows PowerShell：

```powershell
git clone https://github.com/cliffechen/Amazon-SV-GT-Backtesting.git
cd Amazon-SV-GT-Backtesting
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-lock.txt
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m predictor audit
```

macOS / Linux 把 `.venv\Scripts\python.exe` 换成 `.venv/bin/python`。

## MCP 与预算配置

完整 MCP URL 可能包含凭据，不能写进仓库、日志或报告。程序按以下顺序读取：显式 `--provider-config`、环境变量、用户配置目录。

推荐使用环境变量：

```powershell
$env:ASVGT_SELLERSPRITE_MCP_URL = "https://example.invalid/mcp?credential=..."
$env:ASVGT_SIF_MCP_URL = "https://example.invalid/mcp?credential=..."
```

也可以创建用户级 `providers.json`：

```json
{
  "providers": {
    "sellersprite": {"url": "https://..."},
    "sif": {"url": "https://..."}
  }
}
```

复制预算模板并填写已核对的当期额度：

```powershell
Copy-Item config\provider_budgets.example.json config\provider_budgets.json
```

`config/provider_budgets.json` 和 `data/budget.sqlite` 都不会上传。SellerSprite 与 SIF 分开记账；失败的已发送请求保守计入用量。删除账本不能恢复供应商额度。

## 先做计划，再采集

以下命令只生成计划，不联网、不扣额度：

```powershell
.\.venv\Scripts\python.exe -m predictor plan --as-of 2026-09-26 --amazon-provider sellersprite --refresh-amazon
.\.venv\Scripts\python.exe -m predictor plan --as-of 2026-09-26 --amazon-provider sif --refresh-amazon
```

确认计划、账号余额和本地上限后，才运行采集脚本：

```powershell
.\.venv\Scripts\python.exe scripts\collect_sellersprite_all.py --snapshot-date 2026-09-26 --max-new-units 52
.\.venv\Scripts\python.exe scripts\collect_sif_all.py --snapshot-date 2026-09-26 --max-new-units 11
```

SellerSprite 每词一次调用；SIF 每批最多 5 词，52 词是 11 个批次。SIF 批次失败后不会悄悄拆成 52 次单词重试。`--max-new-units` 是本轮硬上限，仍受本地预算配置和供应商实际余额共同限制。

查看账本或恢复中断状态：

```powershell
.\.venv\Scripts\python.exe -m predictor collect-status --provider sellersprite --quota-config config\provider_budgets.json
.\.venv\Scripts\python.exe -m predictor collect-status --provider sif --quota-config config\provider_budgets.json
.\.venv\Scripts\python.exe -m predictor collect-recover --provider sif --quota-config config\provider_budgets.json
```

## 从缓存生成双口径报告

先分别准备周表：

```powershell
.\.venv\Scripts\python.exe -m predictor prepare --as-of 2026-09-26 --amazon-provider sellersprite
.\.venv\Scripts\python.exe -m predictor prepare --as-of 2026-09-26 --amazon-provider sif
```

再运行 SIF，记下命令输出的 `analysis` 路径；随后把它显式传给 SellerSprite 主报告：

```powershell
.\.venv\Scripts\python.exe -m predictor run --as-of 2026-09-26 --amazon-provider sif
.\.venv\Scripts\python.exe -m predictor run --as-of 2026-09-26 --amazon-provider sellersprite --comparison-analysis <上一条输出的 analysis 路径>
```

完成后打开 `outputs/latest/report.html`。默认运行目录在 `outputs/runs/`；`outputs/latest-sellersprite.json` 和 `outputs/latest-sif.json` 保存已完成运行的指针。第二份分析必须显式传入，程序不会扫描目录猜测要比较哪一份。

只改报告界面、已有分析结果时可以执行：

```powershell
.\.venv\Scripts\python.exe -m predictor report --analysis outputs\latest\analysis.json --output outputs\latest\report.html
```

`run`、`prepare` 和 `report` 只读本地缓存，不会调用付费 MCP，也不会自动搜索实时新闻。

## 主要目录

```text
config/catalog_mappings.json          人工复核的 52 个成分映射
config/provider_budgets.example.json  双供应商预算模板
predictor/                            对齐、预算、模型、报告程序
scripts/                              有硬上限的采集与迁移脚本
tests/                                不调用付费接口的自动测试
data/raw/{provider}/                  按供应商隔离的原始缓存，不上传
data/processed/datasets/{provider}/   周表、质量记录、来源清单，不上传
outputs/runs/                         每次独立运行，不上传
outputs/latest/                       SellerSprite 兼容发布目录，不上传
docs/CONTRACT.md                      数据与输出契约
docs/MODELING.md                      完整建模和验证规则
docs/results/                         不含逐周历史的可分享审计摘要
```

GitHub 保存代码、测试、方法文档和脱敏审计摘要。付费原始数据、MCP 凭据、真实预算、SQLite 账本、面板、模型和带数据 HTML 都只保存在本地。Git 仓库不是这些本地数据的备份。

## 仍需继续做的事

- 在明确追加 SIF 额度后，重新采集未保留下来的 10 个批次；
- 从现在开始定期保存两家 point-in-time 快照，做真正前瞻验证；
- 扩充不同品类、不同生命周期的成分，减少选择偏差；
- 取得合格的 PPC、ABA 集中度和社媒历史后，以消融回测决定是否加入；
- 观察多个新封存周期，再判断 Ridge 或其他模型是否稳定优于简单基准。

选品时仍要另行检查价格带、毛利、竞争强度、法规、供应链和评论门槛。这个系统负责把“趋势感觉”变成可重复检查的证据，不替代商业判断。
