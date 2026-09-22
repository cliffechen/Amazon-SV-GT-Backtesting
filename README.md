# Amazon-SV-GT-Backtesting

把亚马逊站内搜索量（**SV，Search Volume**）和 Google 趋势（**GT，Google Trends**）放在一起，研究一个成分相关的需求未来会怎样变化，再用过去的数据检验这个方法到底靠不靠谱。

目前预测的是 **Amazon 美国站的关键词搜索需求**，新闻模块面向北美。搜索需求不等于产品销量、利润，更不是“必成爆款”的承诺。

**先说结果：程序已经能跑，但第一批回测还没有证明模型能稳定超过简单方法。** 报告会把失败结果一起展示，不只挑好看的曲线。

## 用大白话讲，方法是什么？

### 1. 先确定我们研究的是哪个词

例如研究尿石素 A，就使用美国站的 `urolithin a`。中文名、英文别名、不同剂型不能随便算成几个完全独立的成分。相关查询通过 `family_id` 归组，防止同一个东西一边当练习题、一边当考试题。

`ingredient_db.json` 是**候选成分知识库**，包含 22 类、184 条记录。里面有重复、宠物用途、剂型技术，以及未经核实的市场描述。它能帮助列研究名单，但不是可以直接训练的历史数据。首批人工核对了 40 个核心查询词。

### 2. 把不同来源放到同一张周表里

| 数据 | 大白话解释 | 当前用途 |
|---|---|---|
| Amazon 搜索量 | 一个词一周被搜索多少次；使用卖家精灵的估计值 | 主要预测目标和历史特征 |
| ABA 排名 | 这个词相对其他词有多热门，数字越小越靠前 | 检查是否有额外预测帮助 |
| Google 趋势 | 在 Google 上的相对热度，范围 0–100 | 检查站外关注是否有帮助 |
| PPC、ABA 集中度 | 广告建议出价、头部商品的点击和购买份额 | 当前只作竞争参考 |
| 新闻与品牌动态 | 最近发生了什么、品牌在推广什么 | 当前只作调研背景 |

**这些数字不能直接相加。** Google 的 80 不等于 80 次搜索，ABA 排名也不是搜索量。模型比较的是各自过去的水平、变化和波动，不是拍脑袋设置“亚马逊占 60%、Google 占 40%”。

目前统一使用周数据。Amazon 标签按周六结束；Google 的周边界仍需供应商进一步确认，因此按披露的映射规则处理，再额外滞后一周。缺失数据保留缺失，不填成“需求为零”。原始响应按采集日期保存，以便追查口径变化。

### 3. 明确要预测什么

目标是：**从最后一个有数据的周开始，未来 13 周或 26 周的平均每周搜索量**，大约对应 3 个月和 6 个月。

例如“未来 13 周平均每周 2 万次”，说的是总体平均水平，不是每周恰好 2 万次，也不是销量 2 万件。增长幅度与过去 13 周均值比较。来源数据落后于今天时，预测窗口也从数据截止周起算，不假装数据已经更新到今天。

### 4. 把未来遮住，反复模拟考试

假设站在过去某个周末：

1. 只让模型看这个周末及以前的数据。
2. 让它预测后面 13 周或 26 周的平均搜索量。
3. 再揭开真实数据，比较错了多少。
4. 把时间向后移动，反复做同样的事。

这叫**滚动回测**。当前每 4 周做一次。预测窗口会重叠，所以 300 条记录不等于 300 次完全独立的考试。

还有一个容易漏掉的地方：用来训练的旧预测题，其后续答案也必须在预测当天之前已经发生。不能把还没发生的答案偷偷拿去训练。所有成分遵守同一个历史截止日期。

### 5. 先和两个朴素的方法比

- **最近均值法**：未来大致和最近 13 周一样。
- **去年同期法**：未来大致和去年对应的那段时间一样。

然后尝试带约束的回归模型（Ridge）：先只看 Amazon，再分别加入 Google、ABA，最后一起加入。它会约束系数，减少为了贴合旧数据而做出夸张解释的机会。

新增数据源必须在**同样一批考试题**上比较，才能说有没有帮助。把更多列塞进去，或者看到两条曲线一起涨，不等于更准，也不代表因果关系。

### 6. 防止“练习题很好，换题就不会”

这就是过拟合。项目采用这些检查：

- 用较早的验证结果选方法，再用后面一段时间检验；后期结果不参与自动选模或区间校准。
- 留出一组成分族，训练时不让模型见到，看看换成分能不能预测。
- 特征、参数和选择门槛尽量简单固定，不根据后期成绩反复调到好看。
- 标准化只使用训练数据；缺失值、未来数据和当前新闻不能偷偷混进历史特征。

**多拿一些成分有帮助，但不是越多越准。** 要有不同类别、增长、下降、平稳的案例，还要有足够长的历史。如果只挑已经火了的成分，或者把同义词算成独立样本，效果会被高估。当前候选库偏向新兴成分，仍然有选择偏差。

## 第一批真实结果

下面是 2026-09-23 首次运行记录，数据截止周为 2026-09-12；不是实时更新的成绩。

| 检验 | 13 周 | 26 周 |
|---|---:|---:|
| 较早验证选择的方法 | 最近 13 周均值 | 去年同期均值 |
| 所选方法的后期加权误差 | 33.20% | 50.16% |
| 同期最近均值法的误差 | 33.20% | 34.53% |
| 所选方法对未见成分的后期误差 | 20.33% | 27.99% |
| 未见成分上的最近均值法误差 | 20.33% | 21.88% |

加权误差（WAPE）就是“所有预测错的量加起来，除以所有真实搜索量”，**越小越好**。不能把 `100% − 误差` 叫作准确率。它更偏重搜索量大的词，所以还要结合单成分表现看。

26 周方法在后期比简单基准更差。即使某个词显示很大的增长预测，也只能作为探索线索。报告区间来自较早预测误差的经验分布，不保证未来有 80% 的覆盖率，更不是成为爆款的概率。

另外，现在下载的旧数据可能已经被供应商修订，无法完全复原过去当时能看到什么；开发时也已查看后期成绩，不能称为从未看过的盲测。后续需要持续保存快照并做前瞻验证。详见[详细方法](docs/MODELING.md)和[首批验证记录](docs/FIRST_RUN.md)。

## 它以什么形式运行？

- **Skill**：告诉 Codex 怎样采集、查预算、运行程序和解释结果，是操作说明。
- **Python**：清洗数据、拟合、回测和生成报告；预测数字由程序计算。
- **HTML**：选择成分、拖动时间条、切换 13/26 周、比较预测和实际、筛选新闻。图表程序和数据已内嵌，不需要另装前端框架。

在已配置好的 Codex 中可以说：

> 用 $ingredient-forecast 查看尿石素 A 的预测和 Timeline 近三个月动态，优先使用缓存。

Skill 在 [skills/ingredient-forecast/SKILL.md](skills/ingredient-forecast/SKILL.md)。克隆仓库不会自动安装它，需要放入自己的技能目录。文件保留原开发机器的默认路径；换电脑时，要明确告诉 Codex 使用当前克隆目录，或修改这个默认路径。

**Python 不会自动继承 Codex 的 MCP 登录。** 采集新数据需要已连接的卖家精灵 MCP；也可以按[数据结构](docs/CONTRACT.md)导入自己合法取得的历史数据。

## 运行依赖

| 依赖 | 用途 | 要求 |
|---|---|---|
| Python | 运行后端 | 推荐 3.12，首批验证为 3.12.10 |
| NumPy、pandas | 数值计算、整理周表 | 随依赖文件安装 |
| scikit-learn、SciPy | 回归模型与数值运算 | 随依赖文件安装 |
| Plotly | 生成交互图表 | 随依赖文件安装 |
| requests | HTTP 工具依赖 | 随依赖文件安装 |
| pytest | 自动检查 | 随依赖文件安装 |
| Node.js | 执行报告 JavaScript 的一项测试 | 可选；不装会跳过该测试，日常报告不依赖 Node |
| 现代浏览器 | 查看 HTML | Edge、Chrome 等 |
| 卖家精灵账号及 MCP | 获取新 Amazon / Google 数据 | 只有采集时需要，可能消耗点数 |
| 联网搜索工具 | 搜索并核验新新闻 | 只有更新新闻时需要 |

`requirements-lock.txt` 固定了已验证的主要依赖版本，不是包含全部传递依赖的完整环境锁。`pyproject.toml` 声明最低 Python 3.11，但复现这批固定依赖建议使用已验证的 Python 3.12。无需 GPU；预算账本使用 Python 自带的 SQLite，不需要数据库服务器。

## 第一次在新电脑运行

以下以 Windows PowerShell 为例，在项目根目录执行。Mac/Linux 将 `.venv\Scripts\python.exe` 换成 `.venv/bin/python`；附带的 `.ps1` 脚本是 Windows 用法。

### 1. 克隆、安装、检查

```powershell
git clone https://github.com/cliffechen/Amazon-SV-GT-Backtesting.git
cd Amazon-SV-GT-Backtesting
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-lock.txt
.\.venv\Scripts\python.exe -m pytest -q
```

私有仓库需要对应 GitHub 权限。代码可以直接从项目根目录运行，无需安装成系统命令。

### 2. 创建自己的预算配置和成分目录

```powershell
if (-not (Test-Path config/budget.json)) {
    Copy-Item config/budget.example.json config/budget.json
}
.\.venv\Scripts\python.exe -m predictor audit
.\.venv\Scripts\python.exe -m predictor budget
```

模板默认可用余额为 **0**，不会允许付费采集。采集前按实际情况填写 `config/budget.json`：

- `period`：当前月份，例如 `2026-09`。
- `screenshot_available`：已经核对过的账户余额。
- `project_limit`：本项目最多允许使用多少点。
- `account_reserve`：留着不用的点数。
- `per_minute`：每分钟调用上限。

到新月份需重新核对；程序不会购买点数或转移下月额度。`audit` 会重新生成目录，已有人工查询映射时不要随意重跑覆盖，应先保存和迁移自己的映射。

### 3. 准备历史数据

```powershell
.\.venv\Scripts\python.exe -m predictor plan --ids urolithin-a
```

这只生成清单，不调用 MCP、不扣点。真正采集时，由 Codex 按 Skill 执行“预约预算 → 调用 MCP → 保存完整原始响应 → 完成登记”。每个词首次通常需要 Amazon 和 Google 两个历史接口，已有可用缓存就复用。失败尝试保守计入预算，真实扣费以供应商账单为准。

**新克隆的仓库没有付费原始历史，因此不能直接生成真实预测报告。** 需要先采集，或将自己的 `data/raw/` 缓存按原目录结构放回来。历史不足时显示数据不足或回退基线，不会编造预测值。

### 4. 从缓存生成报告

```powershell
$reportDate = Get-Date -Format 'yyyy-MM-dd'
.\.venv\Scripts\python.exe -m predictor run --as-of $reportDate
```

完成后打开 `outputs/latest/report.html`。也可以执行 `scripts/rebuild_report.ps1`，默认使用本机日期。

`run` 只使用**已保存的缓存**，不会扣新点数或搜索新新闻。把日期改成今天，不等于旧数据自动更新到今天。只改 UI、已有 `analysis.json` 时，可以只执行：

```powershell
.\.venv\Scripts\python.exe -m predictor report
```

## 新闻怎样更新？

```powershell
.\.venv\Scripts\python.exe -m predictor news-queries --ids urolithin-a
```

这条命令只生成检索词。由 Codex 搜索并打开原文，核验发布日期、北美相关性和来源，整理成 JSON 后导入：

```powershell
.\.venv\Scripts\python.exe -m predictor news-import --input data/news/new_research.json
.\.venv\Scripts\python.exe -m predictor enrich
.\.venv\Scripts\python.exe -m predictor report
```

最后两步要求已有 `outputs/latest/analysis.json`。导入格式见[新闻说明](docs/NEWS.md)。近三个月按**三个日历月**计算；品牌营销与独立报道分开标注，旧文章不伪装成新新闻，今天的新闻不塞回过去的训练中。

自带的 `verified_seed.json` 是有日期的首批资料，不是实时新闻流。首批重点覆盖尿石素 A / Timeline，并补充肌酸与南非醉茄；加拿大和墨西哥尚未系统检索。新闻空白不代表市场没有活动。

## 文件放在哪里？

```text
predictor/                       数据、预算、模型、报告程序
tests/                           自动检查，不调用付费接口
config/budget.example.json       可分享的预算模板
config/budget.json               自己的真实配置，不上传
ingredient_db.json               原始候选成分知识库
data/news/verified_seed.json     首批带日期和来源的新闻资料
data/raw/                       原始 MCP 响应，不上传
data/processed/                 清洗目录和周表，不上传
data/budget.sqlite              用量账本，不上传
outputs/latest/                 HTML、分析结果和模型，不上传
skills/ingredient-forecast/     Codex 操作技能
docs/                           详细方法、字段和验收记录
```

仓库保存代码、文档、测试和候选库，不上传本地环境、付费原始数据、个人预算配置、账本和带数据的报告。**GitHub 仓库不是完整数据备份**：换电脑时，原始数据和预算账本需要另外保留，不能把删除账本当成恢复额度的方法。

输出里的 `analysis.json` 保存结果，`training_manifest.json` 记录输入/代码摘要和训练审计，`models/*.joblib` 保存模型。只加载自己信任的模型文件。PPC、集中度、TikTok 当前未进入需求训练；竞争指标只有已核对的月份快照，不是完整周趋势。
