# 河道流向更正与跨测区汇水汇总（Flask + SQLite）

河道测绘员改正栅格流向后，把新的汇水表交给 GIS 的网页工具。

- **导入**流向 CSV：每行一个格点的行列号 + 一个上下左右相邻的下游格；出口的下游两列留空。
- **存储**用 SQLite：流向（`down_row/down_col`）与沿流路汇入各格的格数（`accum`，**含该格自身**）同表保存。
- **批量更正**：一次提交多格；只重算“更正格本身 + 更正前后能沿下游到达的格”，结果与整图重算逐格一致。
- **批次交错**：后生效的批次读取的是此前已提交批次的最新流向，自动纳入先前更正。
- **失败安全**：整批出现环、下游无效（不存在/非四邻域/自环）或数据库保存失败时，事务回滚，**原流向和汇水表照常可用、可下载**。
- **下载**：流向表 `flow_direction.csv` 与汇水表 `accumulation.csv`。
- **跨测区交接**：各组填矩形行列范围、上传局部流向 CSV，下载 JSON 测区包；
  汇总员一次上传多个互不重叠测区的包，只凭包列出汇入每个真实出口的各测区格数及合计。

## 目录结构

```
app/
  flow.py      纯算法：CSV 解析、合法性/环校验、全量重算、增量更正
  regions.py   纯算法：测区包生成/校验、跨测区压缩图汇总
  db.py        SQLite 持久化：导入/更正均为单事务，进程内写锁串行化
  app.py       Flask 路由与应用工厂
  templates/index.html  单页界面（原生 JS，无外部依赖）
tests/         pytest：算法单测、随机性质测试、接口/回滚/并发测试、跨测区汇总测试
sample/        4×4 示例流向 CSV；regions/ 下拆分左右两测区的示例 CSV
Dockerfile, docker-compose.yml, requirements.txt
```

## 用 Docker Compose 启动

```bash
docker compose up --build
# 打开 http://localhost:8000
```

数据保存在命名卷 `flow-data`（容器内 `/data/flow.db`），容器重建不丢数据。
容器以 gunicorn 单 worker + 8 线程运行；写操作由应用进程锁与
SQLite `BEGIN IMMEDIATE` + `busy_timeout` 双重串行化。

## 本地直接运行（无 Docker 时）

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
FLOW_DB_PATH=data/flow.db .venv/bin/gunicorn \
  --bind 127.0.0.1:8000 --workers 1 --threads 8 app.app:app
```

## CSV 格式

表头固定 `row,col,down_row,down_col`（大小写不敏感，兼容 BOM）：

```csv
row,col,down_row,down_col
0,0,0,1
0,1,0,2
3,3,,            # 出口：下游两列留空
```

## 更正接口

`POST /api/correct`，JSON 体：

```json
{"changes": [
  {"row": 0, "col": 1, "down_row": 1, "down_col": 1},
  {"row": 3, "col": 2, "down_row": null, "down_col": null}
]}
```

`down_row/down_col` 同时为 `null` 表示把该格改为出口；只填一个会被拒绝。
同一格在一批里出现多次会被整批拒绝。

## 增量算法（为什么只重算一部分却与全量一致）

1. 校验所有新下游：格点存在、四邻域相邻、非自环；
2. 套用本批改动得到新图，从改动格出发做迭代 DFS 查环（环必含改动格）；
3. 受影响集合 = 改动格 ∪ 旧边下游链 ∪ 新边下游链；
4. 集合内按新图做局部 Kahn 拓扑重算：
   - 集合外上游格的累积值不变，从新图汇入集合的部分作为**边界输入**计入；
   - 集合内每格初值 = 1（自身）+ 边界输入，再沿拓扑序向下游累加；
5. 集合外格点原样保留。只把受影响格的流向与累积值写进一个事务。

性质测试（`tests/test_random_property.py`，60 个随机种子 × 随机图 + 随机批次）
逐格断言增量结果 == 整图 `compute_accumulation` 重算结果。

## 接口一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/` | 网页 |
| POST | `/api/import` | multipart 上传 CSV，整图校验+全量重算，事务替换 |
| POST | `/api/correct` | 批量更正，增量重算 |
| GET | `/api/state` | 当前网格、累积值、最近 20 条导入/更正历史 |
| GET | `/api/sample` | 下载 4×4 示例 CSV |
| POST | `/api/region/package` | 上传某测区 CSV + 表单范围，下载 JSON 测区包 |
| POST | `/api/region/summary` | 一次上传多个测区包（字段 `files`），返回按出口汇总 |
| GET | `/download/flow.csv` | 下载当前流向表 |
| GET | `/download/accumulation.csv` | 下载当前汇水表 |

业务错误统一返回 `400 {"ok": false, "error": ...}`；无数据时下载返回 `409`。

## 跨测区测区包

### 生成（各组）

网页填测区编号与矩形行列范围（闭区间），上传该测区现有格式的流向 CSV
（全局行列号，无需裁剪成局部坐标）：

```text
POST /api/region/package      multipart: file=CSV, region_id, row_min, row_max, col_min, col_max
```

校验（失败即退回文件并指出位置）：

- CSV 格点必须都在给定矩形内；
- 区内下游必须在该 CSV 中存在，否则报“区内引用缺格：格点 (r,c) 的下游 (r',c') …”；
- 跨区下游必须四邻域相邻（“下游留空 = 真实出口”仍照旧）；
- 区内成环时报出“环上格点：(r,c) -> …”。

成功时返回/下载 `<region_id>.region.json`，结构：

```json
{
  "format": "flow-region-package", "version": 1,
  "region_id": "R1",
  "row_min": 0, "row_max": 3, "col_min": 0, "col_max": 1,
  "cell_count": 8,
  "boundary": [{"row": 0, "col": 1, "terminal_row": 0, "terminal_col": 1}],
  "terminals": [
    {"row": 0, "col": 1, "cells": 4, "down_row": 0, "down_col": 2},
    {"row": 3, "col": 0, "cells": 2, "down_row": null, "down_col": null}
  ]
}
```

- `boundary`：矩形四周的边界格，以及它在**本区内**沿流路最终到达的终结格；
- `terminals`：区内终结格。`down_row/down_col` 填 `null` 表示真实出口，
  否则是跨区去向；`cells` 是本区内最终汇到该终结格的格数（含自身）。
  各 `cells` 之和等于 `cell_count`。

**记录数只随边界格和终结格（跨区出口 + 真实出口）数量增长**，与区内格点数无关；
包内不含任何逐格明细。例如 20×20 全向下流的测区有 400 格，包内只有
76 个边界格 + 1 个终结格。

### 汇总（汇总员）

```text
POST /api/region/summary      multipart: files=包1.json, files=包2.json, ...
```

只凭这些包工作，返回：

```json
{"ok": true,
 "outlets": [
   {"row": 3, "col": 3, "total": 16,
    "regions": [{"region_id": "R1", "cells": 8}, {"region_id": "R2", "cells": 8}]}
 ],
 "regions": [{"region_id": "R1", "cell_count": 8, "row_min": 0, "...": "..."}],
 "total_cells": 16}
```

汇总把每个终结格当成带权节点，指向目标测区接收格的终结格；压缩图上求
最终出口后按“格点自身所属测区”累加，因此**流路反复进出同一测区，每格也只
按自身所属测区计一次**。结果与把所有测区拼成整图后逐格追流一致
（`tests/test_regions.py` 用暴力追流作参考实现逐出口断言）。

整批拒绝并指出位置的情形：

- 缺少跨区接收格：某跨区去向不属于任何已上传测区，或目标测区矩形内没有该接收边界格
  （报“断点”，带两侧格点坐标）；
- 拼接后成环：报出“环上的边界格：区域 'A' 边界格 (r,c) -> 区域 'B' 边界格 …”；
- 测区矩形重叠、测区编号重复、包格式/版本/汇总格数不一致。

失败时不返回任何部分结果。

## 测试

```bash
.venv/bin/python -m pytest
```
