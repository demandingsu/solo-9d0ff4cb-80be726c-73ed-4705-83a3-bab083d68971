# 河道流向更正与汇水表系统（Flask + SQLite）

河道测绘员改正栅格流向后，把新的汇水表交给 GIS 的网页工具。

- **导入**流向 CSV：每行一个格点的行列号 + 一个上下左右相邻的下游格；出口的下游两列留空。
- **存储**用 SQLite：流向（`down_row/down_col`）与沿流路汇入各格的格数（`accum`，**含该格自身**）同表保存。
- **批量更正**：一次提交多格；只重算“更正格本身 + 更正前后能沿下游到达的格”，结果与整图重算逐格一致。
- **批次交错**：后生效的批次读取的是此前已提交批次的最新流向，自动纳入先前更正。
- **失败安全**：整批出现环、下游无效（不存在/非四邻域/自环）或数据库保存失败时，事务回滚，**原流向和汇水表照常可用、可下载**。
- **下载**：流向表 `flow_direction.csv` 与汇水表 `accumulation.csv`。

## 目录结构

```
app/
  flow.py      纯算法：CSV 解析、合法性/环校验、全量重算、增量更正
  db.py        SQLite 持久化：导入/更正均为单事务，进程内写锁串行化
  app.py       Flask 路由与应用工厂
  templates/index.html  单页界面（原生 JS，无外部依赖）
tests/         pytest：算法单测、60 组随机性质测试、接口/回滚/并发测试
sample/        4×4 示例流向 CSV
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
| GET | `/download/flow.csv` | 下载当前流向表 |
| GET | `/download/accumulation.csv` | 下载当前汇水表 |

业务错误统一返回 `400 {"ok": false, "error": ...}`；无数据时下载返回 `409`。

## 测试

```bash
.venv/bin/python -m pytest
```
