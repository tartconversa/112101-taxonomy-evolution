# 治理分类演进数仓

可离线运行的 Python 数仓项目：分类目录随时间演进（拆分、合并、改名、部分适用、退役），
内容审核事件与人工复核记录共同入库，治理人员可按**事件发生时口径**或**当前可比口径**
查询历史趋势，并将每个汇总结果追溯到事件原始类别、所用定义版本和人工复核状态。

项目仅使用 Python 标准库（sqlite3 / csv / argparse），无需网络或第三方包。

## 目录

- `sources/` 基线源数据（UTF-8 CSV，虚构样例）。
  - `taxonomy.csv` 分类目录版本（`valid_from`/`valid_to` 均含当日，`valid_to` 为空表示当前有效）。
  - `category_mappings.csv` 分类维护方提供的类别映射（见下表）。
  - `content_events.csv` 内容审核事件（含平台提交的原始类别与其声称的目录版本）。
  - `classifications.csv` 人工复核记录（`confirmed` / `changed` / `needs_review`）。
- `src/governance_taxonomy/`
  - `schema.py` 数仓表结构（SQLite DDL）。
  - `synth.py` 确定性合成数据生成器（覆盖全部映射情形与切换期滞后提交）。
  - `etl.py` 加载、维度/事实构建、双口径解析、月度汇总、数据质量检查。
  - `queries.py` 面向治理人员的查询函数（趋势、待审、无法转换、世系下钻、对账）。
  - `cli.py` 命令行入口。
- `tests/` 源文件格式检查与数仓核心不变量测试。
- `data/` 运行期产物（合成数据与 SQLite 数仓，已 gitignore）。

## 数据模型分层

```
源 CSV ──> stg_*（原样加载，保留源 record_id）
        ──> dim_category_version（分类版本，按有效期划分的 SCD 式定义）
            dim_category_map（维护方映射：rename/split/merge/partial/none）
        ──> fact_event（事件 + 事件时有效的定义版本 + 版本匹配方式）
            fact_review（全部复核历史，标记每事件最新一次）
        ──> bridge_event_classification（每事件恰好一行：双口径解析结果）
        ──> mart_trend_aswas / mart_trend_asis（两种口径的月度汇总）
            dq_issue（目录与引用级数据质量问题）
```

## 两种分析口径

- **as-was（事件发生时口径）**：平台提交的原始类别 + 接收时刻有效的定义版本。
  切换期仍提交旧代码的事件保留原样，并标记 `def_version_match = 'lag'`。
- **as-is（当前可比口径）**：最新人工复核优先，其次维护方映射的确定性转换。
  无法确定性转换的事件**不会**落入任何默认类，而是进入显式桶：
  - `PENDING_REVIEW`：拆分/部分适用/无映射/复核请求，需要人工裁决；
  - `UNMAPPABLE`：维护方声明无后继的退役类别，历史数据无法可靠转换。

转换规则（as-is 解析优先级）：

| 情形 | 结果 |
| --- | --- |
| 最新复核为 `changed` 且指定类别有效 | 采用复核指定类别（`reviewed`） |
| 最新复核为 `needs_review` | `PENDING_REVIEW`（`review_requested`） |
| 最新复核为 `confirmed` | 对确认类别按下述映射规则解析；确定性则 `reviewed`，否则待审/无法转换 |
| 无复核，类别当前有效 | 原类别（`identity`） |
| 无复核，映射为 rename/merge 且全覆盖 | 自动转换（`auto_mapped`） |
| 无复核，映射为 split/partial | `PENDING_REVIEW`（`ambiguous_split_or_partial`） |
| 无复核，维护方声明无后继 | `UNMAPPABLE`（`retired_without_successor`） |
| 无复核，类别无任何映射 | `PENDING_REVIEW`（`no_mapping_defined`） |

注意：人工 `confirmed` 确认的是事件发生时的分类；拆分场景下确认旧类并不能替代
在多个新类之间的裁决，因此此类事件仍标记为待审——分类映射不能替代对实际事件的审核，
历史数据也不会被无条件重写成当前分类。

桥表每事件恰好一行，两个口径的汇总都由桥表聚合得到，因此切换期间同一事件
不会在新旧类别中重复计入；`queries.reconciliation` 逐月校验两口径合计等于事件数。

## 运行

```bash
# 一键演示：生成合成数据 -> 构建数仓 -> 打印治理报表（Linux/macOS）
PYTHONPATH=src python -m governance_taxonomy demo

# 分步执行
PYTHONPATH=src python -m governance_taxonomy synth --out data/synthetic
PYTHONPATH=src python -m governance_taxonomy build --sources data/synthetic --db data/warehouse.db
PYTHONPATH=src python -m governance_taxonomy report --db data/warehouse.db

# 也可直接用基线源数据构建
PYTHONPATH=src python -m governance_taxonomy build --sources sources --db data/baseline.db
```

```powershell
# Windows PowerShell
$env:PYTHONPATH = "src"; python -m governance_taxonomy demo
```

报表包含：月度总览、两种口径的趋势矩阵、逐月对账、切换期滞后提交清单、
待审事件清单、无法可靠转换的历史记录、世系钻取示例与数据质量问题。

## 测试

```powershell
python -m unittest discover -s tests -v
```

除源文件格式检查外，`tests/test_warehouse.py` 验证核心不变量：
每事件仅一行桥表记录、两口径逐月对账一致、合并类自动转换、
拆分/部分映射不落默认类、复核改派优先生效、滞后提交保留原类别并标记、
退役无后继类别进入 UNMAPPABLE、世系下钻行数与汇总单元格一致、合成数据可复现。
