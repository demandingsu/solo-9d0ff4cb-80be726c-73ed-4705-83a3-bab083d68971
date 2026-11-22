"""流向与汇水（汇流累积）核心算法。

数据约定
========
- 格点用 (row, col) 行列号表示，行列号均为非负整数。
- ``edges``：dict，格点 -> 下游相邻格点；出口（无下游）的值为 ``None``。
- ``accum``：dict，格点 -> 沿流路汇入该格的格数，**包含该格自身**，
  即该格的上游集水区大小（含自身），出口格的累积值等于总格数。
- 每个格最多一个下游，且必须是上下左右四邻域内的相邻格。

本模块只处理纯数据结构，不接触数据库与 HTTP，便于单元测试与性质测试。
"""

from __future__ import annotations

import csv
import io
from collections import deque
from typing import Optional


class FlowError(ValueError):
    """流向数据非法（格式、邻接关系或环）时抛出。"""


Cell = tuple[int, int]
Edges = dict[Cell, Optional[Cell]]
Accum = dict[Cell, int]


# --------------------------------------------------------------------------- #
# CSV 解析
# --------------------------------------------------------------------------- #
def parse_flow_csv(text: str) -> Edges:
    """解析流向 CSV 文本。

    要求表头为 ``row,col,down_row,down_col``（顺序固定，表头大小写不敏感，
    允许 BOM 与空白）。出口格的下游两列留空。其余常见表头别名也可识别。
    """
    if text is None or not text.strip():
        raise FlowError("CSV 内容为空")

    # utf-8-sig 可吃掉 Excel 常见的 BOM
    reader = csv.reader(io.StringIO(text.lstrip("﻿")))
    try:
        header = next(reader)
    except StopIteration:
        raise FlowError("CSV 内容为空，缺少表头")

    header = [h.strip().lower() for h in header]
    expected = ["row", "col", "down_row", "down_col"]
    aliases = {
        "down_row": ("down_row", "drow", "downstream_row", "next_row"),
        "down_col": ("down_col", "dcol", "downstream_col", "next_col"),
    }
    normalized: list[str] = []
    for name in header:
        if name in ("row", "r"):
            normalized.append("row")
        elif name in ("col", "c", "column"):
            normalized.append("col")
        elif name in aliases["down_row"]:
            normalized.append("down_row")
        elif name in aliases["down_col"]:
            normalized.append("down_col")
        else:
            normalized.append(name)
    if normalized != expected:
        raise FlowError(
            f"表头应为 row,col,down_row,down_col，实际为 {','.join(header)}"
        )

    edges: Edges = {}
    for line_no, row in enumerate(reader, start=2):
        if not row or all(not cell.strip() for cell in row):
            continue  # 跳过空行
        if len(row) != 4:
            raise FlowError(f"第 {line_no} 行应为 4 列，实际为 {len(row)} 列")
        r, c, dr, dc = (cell.strip() for cell in row)
        if not r or not c:
            raise FlowError(f"第 {line_no} 行缺少格点行列号")
        cell = (_parse_int(r, line_no, "row"), _parse_int(c, line_no, "col"))
        if cell in edges:
            raise FlowError(f"第 {line_no} 行格点 {cell} 重复出现")
        if cell[0] < 0 or cell[1] < 0:
            raise FlowError(f"第 {line_no} 行格点行列号必须为非负整数")
        if not dr and not dc:
            edges[cell] = None  # 出口
        elif not dr or not dc:
            raise FlowError(
                f"第 {line_no} 行下游行列号必须同时填写或同时留空（出口留空）"
            )
        else:
            edges[cell] = (
                _parse_int(dr, line_no, "down_row"),
                _parse_int(dc, line_no, "down_col"),
            )
    return edges


def _parse_int(value: str, line_no: int, field: str) -> int:
    try:
        return int(value)
    except ValueError:
        raise FlowError(f"第 {line_no} 行字段 {field} 不是整数：{value!r}")


def dump_flow_csv(edges: Edges) -> str:
    """把流向表导出为 CSV 文本（出口下游两列留空）。"""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["row", "col", "down_row", "down_col"])
    for cell in sorted(edges):
        downstream = edges[cell]
        if downstream is None:
            writer.writerow([cell[0], cell[1], "", ""])
        else:
            writer.writerow([cell[0], cell[1], downstream[0], downstream[1]])
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# 合法性校验
# --------------------------------------------------------------------------- #
def _is_adjacent(a: Cell, b: Cell) -> bool:
    return abs(a[0] - b[0]) + abs(a[1] - b[1]) == 1


def validate_edges(
    edges: Edges, check_neighbor: bool = True
) -> None:
    """校验整张流向图：下游格必须存在；如要求，还必须是四邻域相邻格；最后查环。"""
    for cell, downstream in edges.items():
        if downstream is None:
            continue
        if downstream == cell:
            raise FlowError(f"格点 {cell} 的下游指向自身，形成自环")
        if downstream not in edges:
            raise FlowError(f"格点 {cell} 的下游 {downstream} 不在导入的格点集合中")
        if check_neighbor and not _is_adjacent(cell, downstream):
            raise FlowError(
                f"格点 {cell} 的下游 {downstream} 不是上下左右相邻格"
            )
    detect_cycle(edges)


def detect_cycle(edges: Edges, roots: Optional[list[Cell]] = None) -> None:
    """检测有向环（每点出度 <= 1）。

    ``roots`` 给定时只从这些点出发探查，用于增量场景：环必然包含被改动的格，
    因此从改动格出发即可发现所有新引入的环，而不必遍历整图。
    """
    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[Cell, int] = {}
    starts: list[Cell] = roots if roots is not None else list(edges.keys())

    for start in starts:
        if start not in edges or color.get(start) == BLACK:
            continue
        # 迭代 DFS，栈内存 (当前格, 已处理自身出边标记)
        stack: list[tuple[Cell, bool]] = [(start, False)]
        while stack:
            node, expanded = stack.pop()
            if not expanded:
                state = color.get(node, WHITE)
                if state == GRAY:
                    raise FlowError(f"检测到流向环，环中包含格点 {node}")
                if state == BLACK:
                    continue
                color[node] = GRAY
                stack.append((node, True))
                downstream = edges.get(node)
                if downstream is not None and color.get(downstream) != BLACK:
                    stack.append((downstream, False))
            else:
                color[node] = BLACK


# --------------------------------------------------------------------------- #
# 全量汇水计算
# --------------------------------------------------------------------------- #
def compute_accumulation(edges: Edges) -> Accum:
    """全图重算每格的汇水格数（含自身）。

    用 Kahn 拓扑排序按“下游在自身之后”的顺序累加：
    每个格先计 1（自身），再把自身累积值加给下游格。
    """
    indegree = {cell: 0 for cell in edges}
    for downstream in edges.values():
        if downstream is not None:
            indegree[downstream] += 1

    queue = deque(cell for cell, deg in indegree.items() if deg == 0)
    accum = {cell: 1 for cell in edges}
    visited = 0
    while queue:
        cell = queue.popleft()
        visited += 1
        downstream = edges[cell]
        if downstream is not None:
            accum[downstream] += accum[cell]
            indegree[downstream] -= 1
            if indegree[downstream] == 0:
                queue.append(downstream)
    if visited != len(edges):
        # 正常调用前已做过查环，这里是兜底保护
        raise FlowError("流向图存在环，无法计算汇水表")
    return accum


# --------------------------------------------------------------------------- #
# 增量更正
# --------------------------------------------------------------------------- #
def affected_cells(
    edges: Edges, changes: dict[Cell, Optional[Cell]]
) -> set[Cell]:
    """更正格本身 + 更正前后能沿下游到达的全部格点。"""
    affected: set[Cell] = set()
    for changed in changes:
        affected.add(changed)
        # 更正前的下游链（沿旧边）
        node = edges.get(changed)
        while node is not None and node not in affected:
            affected.add(node)
            node = edges.get(node)
        # 更正后的下游链：起点用新边，链上其余格若也在本批改动中则同样取新边
        node = changes[changed]
        while node is not None and node not in affected:
            affected.add(node)
            node = changes[node] if node in changes else edges.get(node)
    return affected


def apply_correction(
    edges: Edges,
    accum: Accum,
    changes: dict[Cell, Optional[Cell]],
    check_neighbor: bool = True,
) -> tuple[Edges, Accum, set[Cell]]:
    """对一批更正做**内存中**增量计算，返回新流向、新汇水表与受影响集合。

    任何校验失败都会抛出 :class:`FlowError`，且不修改传入对象——
    调用方据此保证数据库里的旧流向/旧汇水表照常可用。

    增量口径：只重算“更正格本身 + 更正前后可到达的下游格”，
    集合外格点的累积值逐格保持不变；集合内按新流向做局部拓扑重算，
    其结果与整图重算一致（由测试 ``test_matches_full_recompute`` 保证）。
    """
    if not changes:
        raise FlowError("更正批次为空，没有需要修改的格点")

    # 1. 改动格必须已存在；新下游必须存在且为四邻域相邻格
    for cell, downstream in changes.items():
        if cell not in edges:
            raise FlowError(f"待更正格点 {cell} 不在当前格点集合中")
        if downstream is None:
            continue
        if downstream not in edges:
            raise FlowError(f"格点 {cell} 的新下游 {downstream} 不在格点集合中")
        if downstream == cell:
            raise FlowError(f"格点 {cell} 的新下游指向自身，形成自环")
        if check_neighbor and not _is_adjacent(cell, downstream):
            raise FlowError(
                f"格点 {cell} 的新下游 {downstream} 不是上下左右相邻格"
            )

    # 2. 构造新流向（拷贝，原对象不动）
    new_edges: Edges = dict(edges)
    new_edges.update(changes)

    # 3. 从改动格出发查环——环必含改动格，无需整图遍历
    detect_cycle(new_edges, roots=list(changes.keys()))

    # 4. 确定受影响集合（改动格 + 旧/新下游链）
    affected = affected_cells(edges, changes)

    # 5. 局部拓扑重算受影响格的累积值。
    #    集合外上游格的累积值保持原值；若它从新流向汇入受影响集合，
    #    其累积值作为“边界输入”计入目标格（这些格的边未改动，旧值即新值）。
    local_indegree: dict[Cell, int] = {cell: 0 for cell in affected}
    boundary: dict[Cell, int] = {cell: 1 for cell in affected}
    for predecessor, downstream in new_edges.items():
        if downstream is None or downstream not in affected:
            continue
        if predecessor in affected:
            local_indegree[downstream] += 1
        else:
            boundary[downstream] += accum[predecessor]

    queue = deque(
        cell for cell, deg in local_indegree.items() if deg == 0
    )
    new_accum: Accum = dict(accum)  # 集合外格点原样保留
    for cell in affected:
        new_accum[cell] = boundary[cell]
    visited = 0
    while queue:
        cell = queue.popleft()
        visited += 1
        downstream = new_edges[cell]
        if downstream is None:
            continue
        if downstream in affected:
            new_accum[downstream] += new_accum[cell]
            local_indegree[downstream] -= 1
            if local_indegree[downstream] == 0:
                queue.append(downstream)
        # 下游落在受影响集合外：不可能发生（集合沿新下游链闭合），
        # 但即使发生，该下游不在重算范围，其值保持原值，由全量测试兜底。
    if visited != len(affected):
        raise FlowError("更正后流向存在环，无法重算汇水表")

    return new_edges, new_accum, affected
