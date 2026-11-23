# 稳健汇总分析

各单位只上报**聚合摘要**（均值向量、标准差、有效数量、取值范围、批次）。少数单位的数据可能损坏：
均值异常、有效数量虚增、或多个单位协同偏移。直接按上报数量加权会被异常大的权重带偏。
本系统接收摘要、比较**封顶加权**与**截尾汇总 / Huber M 估计 / 加权中位数**，
给出**有条件成立的误差范围**，并让每个结果都能追溯到实际参与批次。

纯标准库实现（Python 3.10+，无第三方依赖），前端为原生 JS/SVG 单页应用。

## 运行

```bash
python3 -m robustagg.server --port 8765
# 打开 http://127.0.0.1:8765
```

首次启动会在 `data/robustagg.db` 创建并载入一个虚构样例。可用环境变量或参数改数据库位置：

```bash
ROBUSTAGG_DB=/tmp/demo.db python3 -m robustagg.server --port 8765 --no-seed
```

测试（仅标准库 unittest）：

```bash
python3 -m unittest discover -s tests -v
```

## 界面能做什么

- **载入虚构样例**：六个场景（基线 / 独立损坏 / 协同偏移+虚增数量 / 虚增数量 / 超出上限 / 极小样本极端值），
  真值已知。
- **调整可信假设**：污染上限 α 与单单位权重上限用滑块调整，所有图与误差范围即时重算。
- **切换指标分量**：样例上报的是均值向量（`指标值` 与 `伴随指标`），可分别查看、分别估计。
- **注入损坏摘要**：选择单位与指标，以“更正（替换原贡献）”或“拆报（新批次再报）”方式追加一条报文；
  可从快捷按钮生成“均值上移 3×跨度”或“数量放大 20 倍”。
- **检查某单位影响**：在权重分布或单位取值图上点任一单位，查看其权重份额、
  两个估计之差、若隔离该单位中心会移动多少。
- **两个估计的差异**：直接对比，并判断差异是否落在误差范围内（即是否可由“污染 ≤ α”解释）。
- **追溯参与批次**：从结果或单位详情点“追溯”，列出参与估计的报文及其所在批次的全部报文
  （包括已更正、已被替换的报文）。
- **隔离记录**：隔离必须填写原因，系统记录隔离前后的中心/权重/截尾值；释放也需说明，
  记录保留。**任何路径都不删除原报文。**
- **验证模拟**：用已知真值面板检验稳健收益（p90 误差）与误伤边界（干净面板在改变 α/上限后的漂移），
  并报告误差范围的覆盖率、以及“超出假设时是否撤销保证”。

## 设计要点

### 1. 只接收聚合量

`ALLOWED_RECEIPT_FIELDS` 是白名单；`FORBIDDEN_KEYS` 中的名字（`records`、`rows`、`samples`、
`values`、`person_id` 等）会触发 422 并拒绝整批请求。没有接收逐记录数据的端点。

单分量摘要：

```json
{"unit_id": "单位-07", "batch_id": "批次-甲-01", "period": "2026-W46",
 "metric": "指标值", "mean": 98.2, "std": 11.4, "n_effective": 140,
 "range_low": 60, "range_high": 140}
```

均值向量（服务端按分量展开为多条摘要，共享同一单位与数量）：

```json
{"unit_id": "单位-07", "batch_id": "批次-甲-01", "period": "2026-W46",
 "n_effective": 140,
 "mean_vector": {"指标值": 98.2, "伴随指标": 70.4},
 "std_vector":  {"指标值": 11.4, "伴随指标": 6.1}}
```

### 2. 拆分不能绕过权重上限

同一单位的跨批次报文先汇总，再对单位整体应用上限，最后按申报量比例分配到各报文。
上限定义为“占最终有效权重的份额”，由 `c = s·Σmin(w_i, c)` 二分求解。
求不出正解时（单位太少、又恰好有单位权重过大），系统报告“上限不可行”并撤销保证，
而不是静默压成 0 权重。

### 3. 更正替换而非新增

更正写入新报文并以 `revision_of` 指向旧报文；有效集合自动排除被替换的报文，
旧报文保留在库中并可在批次/追溯视图中看到。

### 4. 误差范围是三档状态

| 状态 | 含义 | 界面表现 |
|---|---|---|
| `guaranteed` | 假设全部在服务端可验证（真实接收路径不会出现） | 显示区间 |
| `conditional` | 至少一个假设无法核验（“实际污染 ≤ α”、“摘要只含聚合量”） | 显示区间并标注前提 |
| `void` | 已知违反假设，或 α ≥ 50%，或单位少于 3 个，或上限不可行 | 区间置空，两处图例都撤掉误差范围，标注“保证已撤销”；两个估计的差异不再判定“是否可由污染解释” |

推导、假设清单与模拟设计见 [`docs/theory.md`](docs/theory.md)。

### 5. 不把“离中心远”自动当坏数据

截尾只削减尾部权重，不产生“坏数据”标签。系统中唯一的坏单位标记来自虚构样例的真值，
仅用于演示与模拟打分。真实的隔离是人的决定，必须附原因，并保留前后影响。

## API 速览

所有接口返回 JSON；错误使用 404（不存在）或 422（违反约束）。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/datasets` | 数据集列表 |
| POST | `/api/datasets/demo` | `{"scenario","seed"}` 生成虚构样例 |
| GET | `/api/dataset/{id}/analysis?alpha&max_share&estimator&metric` | 完整分析结果 |
| GET | `/api/dataset/{id}/compare?...` | 两个估计的差异 |
| GET | `/api/dataset/{id}/batches` / `batch?batch_id=` | 批次与批次内全部报文 |
| GET | `/api/dataset/{id}/unit/{unit}?...` | 单位影响、历史、隔离记录 |
| GET | `/api/dataset/{id}/trace?summary_ids=a,b,c` | 追溯参与报文及所在批次 |
| POST | `/api/dataset/{id}/intake` | `{"receipts":[...],"note":"可选说明"}`（支持 `mean_vector`） |
| POST | `/api/dataset/{id}/revise` | 更正单条摘要 |
| POST | `/api/dataset/{id}/inject` | 演示注入：`{"unit_id","metric","mode":"revision"\|"split",...}` |
| POST | `/api/dataset/{id}/isolate` | `{"unit_id","reason"}` |
| POST | `/api/dataset/{id}/release` | `{"isolation_id","note"}` |
| POST | `/api/dataset/{id}/simulate` | `{"runs","seed","alpha","max_share"}` |
| GET | `/api/dataset/{id}/audit` | 决策与隔离记录 |

## 文件结构

```
robustagg/
  estimators.py   截尾 / Huber / 中位数 / 封顶加权，确定性 RNG
  analysis.py     水填充上限、贡献构建、五个估计量、单位影响、差异比较
  bounds.py       最坏情况误差界 + 假设清单与三档状态
  quantiles.py    加权分位数 / 加权中位数
  synthetic.py    真值已知的虚构面板（均值向量、协同偏移、虚增数量）
  simulation.py   收益模拟与误伤校准
  store.py        SQLite：追加式报文、更正、隔离、审计
  server.py       标准库 HTTP JSON API + 静态文件
  static/         单页界面（原生 JS/SVG，明暗主题）
docs/theory.md    推导、假设、模拟设计、局限
tests/            unittest：估计量、上限不变量、更正、接收策略、误差界、隔离、生成器
```
