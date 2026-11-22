"""跨测区测区包（region package）与汇总。

背景
====
多个测绘组各测一块矩形测区，只交换“测区包”。测区包不携带区内每一格，
只包含：

- 矩形范围（行/列闭区间）；
- 边界格（矩形四周的格点）及其在区内的最终去向（区内终结格）；
- 区内终结格：一是跨区去向（下游指向区外的四邻格），二是真实出口
  （下游留空）；每个终结格附带“有多少本区格点最终汇到它”的汇总格数；
- 全区格点数，用于完整性校验。

因为区内每格只有一个下游，区内无环时每格恰好汇到一个终结格。于是
包的大小只随“边界格数 + 终结格（跨区出口/真实出口）数”增长，与区内
格点总数无关。

汇总
====
汇总员拿到若干互不重叠的测区包后，把每个“区内终结格”当成一个节点：
节点带着本区格数，指向目标测区中接收格的终结格（或真实出口）。在这张
压缩图上求每个节点的最终出口，再把格数按“格点自身所属测区”汇总即可。
流路反复进出同一测区不会重复计数：每个区的终结格只在自己的包里出现
一次，区内每格也只归属一个终结格。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Any, Optional

from .flow import Cell, Edges, FlowError

PACKAGE_FORMAT = "flow-region-package"
PACKAGE_VERSION = 1

Rect = tuple[int, int, int, int]  # row_min, row_max, col_min, col_max


class RegionError(FlowError):
    """测区包生成或跨区汇总失败。"""


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def _adjacent(a: Cell, b: Cell) -> bool:
    return abs(a[0] - b[0]) + abs(a[1] - b[1]) == 1


def _contains(rect: Rect, cell: Cell) -> bool:
    r0, r1, c0, c1 = rect
    return r0 <= cell[0] <= r1 and c0 <= cell[1] <= c1


def _on_perimeter(rect: Rect, cell: Cell) -> bool:
    r0, r1, c0, c1 = rect
    return _contains(rect, cell) and (
        cell[0] in (r0, r1) or cell[1] in (c0, c1)
    )


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _fmt_cell(cell: Cell) -> str:
    return f"({cell[0]},{cell[1]})"


def _cycle_in_functional_graph(
    edges: dict[Cell, Optional[Cell]], nodes: Optional[set[Cell]] = None
) -> list[Cell]:
    """在“每点出度 <= 1”的图上找一条环，返回环上的格点（按流向顺序）。

    出边目标不在 ``nodes`` 中（例如真实出口/区外）时视为链的终点。
    没有环时返回空列表。
    """
    allowed = nodes if nodes is not None else set(edges)
    finished: set[Cell] = set()
    for start in allowed:
        if start in finished:
            continue
        path: list[Cell] = []
        position: dict[Cell, int] = {}
        cur: Optional[Cell] = start
        while cur is not None and cur in allowed and cur not in finished:
            if cur in position:
                return path[position[cur]:]
            position[cur] = len(path)
            path.append(cur)
            cur = edges.get(cur)
        finished.update(path)
    return []


# --------------------------------------------------------------------------- #
# 生成测区包
# --------------------------------------------------------------------------- #
def build_region_package(
    region_id: str,
    row_min: int,
    row_max: int,
    col_min: int,
    col_max: int,
    edges: Edges,
) -> dict:
    """由局部流向（全局行列号）生成一个可下载的 JSON 测区包。

    校验规则：

    - CSV 中的每个格点都必须落在给定矩形范围内；
    - 区内下游必须存在于 CSV 中（否则报“区内引用缺格”并指出位置）；
    - 跨区下游必须是四邻域相邻格；
    - 区内不得成环（报出环上格点）；
    - 下游留空即真实出口。

    失败抛 :class:`RegionError`，不产生包。
    """
    region_id = (region_id or "").strip()
    if not region_id:
        raise RegionError("测区编号 region_id 不能为空")
    for value, label in (
        (row_min, "row_min"),
        (row_max, "row_max"),
        (col_min, "col_min"),
        (col_max, "col_max"),
    ):
        if not _is_int(value):
            raise RegionError(f"测区范围 {label} 必须是整数")
        if value < 0:
            raise RegionError(f"测区范围 {label} 必须为非负整数")
    if row_min > row_max:
        raise RegionError("测区范围行号不合法：row_min 不能大于 row_max")
    if col_min > col_max:
        raise RegionError("测区范围列号不合法：col_min 不能大于 col_max")

    rect: Rect = (row_min, row_max, col_min, col_max)
    if not edges:
        raise RegionError("测区 CSV 中没有任何格点记录")

    for cell in edges:
        if not _contains(rect, cell):
            raise RegionError(
                f"格点 {_fmt_cell(cell)} 不在测区范围"
                f"（行 {row_min}..{row_max}，列 {col_min}..{col_max}）内"
            )

    # 逐格校验区内引用与跨区邻接
    inside_edge: dict[Cell, Optional[Cell]] = {}
    for cell, downstream in edges.items():
        if downstream is None:
            inside_edge[cell] = None
            continue
        if downstream == cell:
            raise RegionError(f"格点 {_fmt_cell(cell)} 的下游指向自身，形成自环")
        if _contains(rect, downstream):
            if downstream not in edges:
                raise RegionError(
                    "区内引用缺格："
                    f"格点 {_fmt_cell(cell)} 的下游 {_fmt_cell(downstream)} "
                    "在测区矩形范围内，但 CSV 中没有该格"
                )
            inside_edge[cell] = downstream
        else:
            if not _adjacent(cell, downstream):
                raise RegionError(
                    f"格点 {_fmt_cell(cell)} 的区外下游 {_fmt_cell(downstream)} "
                    "不是上下左右相邻格"
                )
            inside_edge[cell] = None  # 区内链在此终结，沿跨区边出去

    # Kahn 拓扑排序：同时计算“汇入每个区内终结格的格数”并检测区内的环
    indegree = {cell: 0 for cell in edges}
    for downstream in inside_edge.values():
        if downstream is not None:
            indegree[downstream] += 1
    queue = deque(cell for cell, deg in indegree.items() if deg == 0)
    accum = {cell: 1 for cell in edges}
    order: list[Cell] = []
    while queue:
        cell = queue.popleft()
        order.append(cell)
        downstream = inside_edge[cell]
        if downstream is not None:
            accum[downstream] += accum[cell]
            indegree[downstream] -= 1
            if indegree[downstream] == 0:
                queue.append(downstream)
    if len(order) != len(edges):
        cycle = _cycle_in_functional_graph(inside_edge, set(edges))
        detail = " -> ".join(_fmt_cell(cell) for cell in cycle) if cycle else "（未能定位）"
        raise RegionError(f"测区内检测到流向环，环上格点：{detail}")

    # 每格最终落到哪个区内终结格。逆拓扑序保证下游先算。
    terminal_of: dict[Cell, Cell] = {}
    for cell in reversed(order):
        downstream = inside_edge[cell]
        terminal_of[cell] = cell if downstream is None else terminal_of[downstream]

    terminal_cells = sorted(cell for cell, d in inside_edge.items() if d is None)
    boundary = [
        {
            "row": cell[0],
            "col": cell[1],
            "terminal_row": terminal_of[cell][0],
            "terminal_col": terminal_of[cell][1],
        }
        for cell in sorted(edges)
        if _on_perimeter(rect, cell)
    ]
    terminals = []
    for cell in terminal_cells:
        downstream = edges[cell]
        terminals.append(
            {
                "row": cell[0],
                "col": cell[1],
                "cells": accum[cell],
                "down_row": None if downstream is None else downstream[0],
                "down_col": None if downstream is None else downstream[1],
            }
        )

    return {
        "format": PACKAGE_FORMAT,
        "version": PACKAGE_VERSION,
        "region_id": region_id,
        "row_min": row_min,
        "row_max": row_max,
        "col_min": col_min,
        "col_max": col_max,
        "cell_count": len(edges),
        "boundary": boundary,
        "terminals": terminals,
    }


# --------------------------------------------------------------------------- #
# 解析并校验测区包
# --------------------------------------------------------------------------- #
@dataclass
class ParsedRegion:
    region_id: str
    rect: Rect
    cell_count: int
    boundary: dict[Cell, Cell]          # 边界格 -> 本区终结格
    terminals: dict[Cell, tuple[int, Optional[Cell]]]  # 终结格 -> (格数, 区外下游)


def _field_int(raw: Any, where: str, name: str) -> int:
    if not _is_int(raw):
        raise RegionError(f"{where}的 {name} 必须是整数")
    if raw < 0:
        raise RegionError(f"{where}的 {name} 必须为非负整数")
    return raw


def _parse_package(payload: Any, index: int) -> ParsedRegion:
    where = f"第 {index} 个测区包"
    if not isinstance(payload, dict):
        raise RegionError(f"{where}不是 JSON 对象")
    if payload.get("format") != PACKAGE_FORMAT:
        raise RegionError(f"{where}格式不正确：format 应为 {PACKAGE_FORMAT}")
    if payload.get("version") != PACKAGE_VERSION:
        raise RegionError(
            f"{where}版本不支持：期望 version={PACKAGE_VERSION}，"
            f"实际为 {payload.get('version')!r}"
        )

    region_id = payload.get("region_id")
    if not isinstance(region_id, str) or not region_id.strip():
        raise RegionError(f"{where}缺少非空的 region_id")
    region_id = region_id.strip()

    row_min = _field_int(payload.get("row_min"), where, "row_min")
    row_max = _field_int(payload.get("row_max"), where, "row_max")
    col_min = _field_int(payload.get("col_min"), where, "col_min")
    col_max = _field_int(payload.get("col_max"), where, "col_max")
    if row_min > row_max:
        raise RegionError(f"{where}的行范围不合法：row_min > row_max")
    if col_min > col_max:
        raise RegionError(f"{where}的列范围不合法：col_min > col_max")
    rect: Rect = (row_min, row_max, col_min, col_max)

    cell_count = payload.get("cell_count")
    if not _is_int(cell_count) or cell_count <= 0:
        raise RegionError(f"{where}的 cell_count 必须是正整数")

    boundary_raw = payload.get("boundary")
    terminals_raw = payload.get("terminals")
    if not isinstance(boundary_raw, list):
        raise RegionError(f"{where}缺少 boundary 数组")
    if not isinstance(terminals_raw, list) or not terminals_raw:
        raise RegionError(f"{where}缺少非空的 terminals 数组")

    terminals: dict[Cell, tuple[int, Optional[Cell]]] = {}
    for item in terminals_raw:
        if not isinstance(item, dict):
            raise RegionError(f"{where}的 terminals 中存在非对象记录")
        row = _field_int(item.get("row"), where, "terminals[].row")
        col = _field_int(item.get("col"), where, "terminals[].col")
        cell = (row, col)
        if cell in terminals:
            raise RegionError(f"{where}的终结格 {_fmt_cell(cell)} 重复出现")
        if not _contains(rect, cell):
            raise RegionError(
                f"{where}的终结格 {_fmt_cell(cell)} 不在测区范围内"
            )
        cells = item.get("cells")
        if not _is_int(cells) or cells <= 0:
            raise RegionError(
                f"{where}的终结格 {_fmt_cell(cell)} 的 cells 必须是正整数"
            )
        dr, dc = item.get("down_row"), item.get("down_col")
        if dr is None and dc is None:
            downstream: Optional[Cell] = None
        elif dr is None or dc is None:
            raise RegionError(
                f"{where}的终结格 {_fmt_cell(cell)} 的 down_row/down_col "
                "必须同时填写或同时留空"
            )
        else:
            if not _is_int(dr) or not _is_int(dc):
                raise RegionError(
                    f"{where}的终结格 {_fmt_cell(cell)} 的区外下游必须是整数"
                )
            downstream = (dr, dc)
            if _contains(rect, downstream):
                raise RegionError(
                    f"{where}的终结格 {_fmt_cell(cell)} 的下游 "
                    f"{_fmt_cell(downstream)} 仍在测区内，包结构不正确"
                )
            if not _adjacent(cell, downstream):
                raise RegionError(
                    f"{where}的终结格 {_fmt_cell(cell)} 的区外下游 "
                    f"{_fmt_cell(downstream)} 不是四邻域相邻格"
                )
        terminals[cell] = (cells, downstream)

    boundary: dict[Cell, Cell] = {}
    for item in boundary_raw:
        if not isinstance(item, dict):
            raise RegionError(f"{where}的 boundary 中存在非对象记录")
        row = _field_int(item.get("row"), where, "boundary[].row")
        col = _field_int(item.get("col"), where, "boundary[].col")
        cell = (row, col)
        if cell in boundary:
            raise RegionError(f"{where}的边界格 {_fmt_cell(cell)} 重复出现")
        if not _on_perimeter(rect, cell):
            raise RegionError(
                f"{where}的边界格 {_fmt_cell(cell)} 不在测区矩形四周"
            )
        tr = _field_int(item.get("terminal_row"), where, "boundary[].terminal_row")
        tc = _field_int(item.get("terminal_col"), where, "boundary[].terminal_col")
        terminal = (tr, tc)
        if terminal not in terminals:
            raise RegionError(
                f"{where}的边界格 {_fmt_cell(cell)} 指向的终结格 "
                f"{_fmt_cell(terminal)} 不在 terminals 中"
            )
        boundary[cell] = terminal

    if sum(cells for cells, _ in terminals.values()) != cell_count:
        raise RegionError(
            f"{where}的汇总格数不一致：各终结格 cells 之和 "
            f"{sum(cells for cells, _ in terminals.values())} "
            f"不等于 cell_count {cell_count}"
        )

    return ParsedRegion(
        region_id=region_id,
        rect=rect,
        cell_count=cell_count,
        boundary=boundary,
        terminals=terminals,
    )


def _find_target_region(
    regions: list[ParsedRegion], cell: Cell
) -> Optional[ParsedRegion]:
    for region in regions:
        if _contains(region.rect, cell):
            return region
    return None


def _rect_overlap(a: Rect, b: Rect) -> Optional[Rect]:
    r0 = max(a[0], b[0])
    r1 = min(a[1], b[1])
    c0 = max(a[2], b[2])
    c1 = min(a[3], b[3])
    if r0 <= r1 and c0 <= c1:
        return (r0, r1, c0, c1)
    return None


def _nodes_in_cycle(
    successors: dict[tuple, tuple], nodes: set[tuple]
) -> list[tuple]:
    """在“终结格节点 -> 终结格节点/出口”的压缩图上找环。"""
    finished: set[tuple] = set()
    for start in nodes:
        if start in finished:
            continue
        path: list[tuple] = []
        position: dict[tuple, int] = {}
        cur = start
        while cur in nodes and cur not in finished:
            if cur in position:
                return path[position[cur]:]
            position[cur] = len(path)
            path.append(cur)
            cur = successors[cur]
        finished.update(path)
    return []


# --------------------------------------------------------------------------- #
# 汇总多个测区包
# --------------------------------------------------------------------------- #
def summarize_packages(payloads: list) -> dict:
    """只凭测区包计算每个真实出口的各测区格数与合计。

    返回::

        {"outlets": [{"row": r, "col": c, "total": n,
                      "regions": [{"region_id": "...", "cells": n}, ...]}],
         "regions": [{"region_id": ..., "cell_count": n,
                      "row_min": ..., ...}],
         "total_cells": n}

    任一包结构非法、测区重叠、跨区接收格缺失或拼接后成环时，抛
    :class:`RegionError`，不返回部分结果。
    """
    if not isinstance(payloads, list) or not payloads:
        raise RegionError("请至少上传一个测区包")

    regions = [_parse_package(payload, i) for i, payload in enumerate(payloads, 1)]

    seen_ids: dict[str, int] = {}
    for region in regions:
        if region.region_id in seen_ids:
            raise RegionError(
                f"测区编号 {region.region_id!r} 重复出现"
                f"（第 {seen_ids[region.region_id]} 个与后续包）"
            )
        seen_ids[region.region_id] = len(seen_ids) + 1

    for i, left in enumerate(regions):
        for right in regions[i + 1:]:
            overlap = _rect_overlap(left.rect, right.rect)
            if overlap is not None:
                r0, r1, c0, c1 = overlap
                raise RegionError(
                    f"测区 {left.region_id!r} 与 {right.region_id!r} 的矩形范围"
                    f"重叠（行 {r0}..{r1}，列 {c0}..{c1}），测区必须互不重叠"
                )

    # 节点 = (region_id, 终结格)；出口键 = ("outlet", row, col)
    weights: dict[tuple, int] = {}
    region_of_node: dict[tuple, str] = {}
    successors: dict[tuple, tuple] = {}
    outlets: set[Cell] = set()

    for region in regions:
        for cell, (cells, downstream) in region.terminals.items():
            node = (region.region_id, cell)
            weights[node] = cells
            region_of_node[node] = region.region_id
            if downstream is None:
                outlets.add(cell)
                successors[node] = ("outlet", cell[0], cell[1])
                continue

            target_region = _find_target_region(regions, downstream)
            if target_region is None:
                raise RegionError(
                    "缺少跨区接收格："
                    f"区域 {region.region_id!r} 的边界格 {_fmt_cell(cell)} "
                    f"指向区外 {_fmt_cell(downstream)}，"
                    "但该格不属于任何已上传测区（断点）"
                )
            entry_terminal = target_region.boundary.get(downstream)
            if entry_terminal is None:
                raise RegionError(
                    "缺少跨区接收格："
                    f"区域 {region.region_id!r} 的边界格 {_fmt_cell(cell)} "
                    f"指向区外 {_fmt_cell(downstream)}；"
                    f"该格位于区域 {target_region.region_id!r} 的矩形范围内，"
                    "但该区域的测区包里没有这个边界格（断点）"
                )
            successors[node] = (target_region.region_id, entry_terminal)

    nodes = set(weights)
    cycle = _nodes_in_cycle(successors, nodes)
    if cycle:
        parts = [
            f"区域 {region_id!r} 边界格 {_fmt_cell(cell)}"
            for region_id, cell in cycle
        ]
        parts.append(parts[0])
        raise RegionError(
            "拼接后成环，环上的边界格：" + " -> ".join(parts)
        )

    # 无环：求每个终结格节点最终汇入的真实出口
    sink_of: dict[tuple, tuple] = {}
    for node in nodes:
        if node in sink_of:
            continue
        path: list[tuple] = []
        cur = node
        while cur in nodes and cur not in sink_of:
            path.append(cur)
            cur = successors[cur]
        final = sink_of.get(cur, cur)
        for item in path:
            sink_of[item] = final

    counts: dict[Cell, dict[str, int]] = {cell: {} for cell in outlets}
    for node in nodes:
        sink = sink_of[node]
        if sink[0] != "outlet":  # 无环且有限，必定落到出口；防御性检查
            raise RegionError("内部错误：存在未到达真实出口的流路")
        outlet = (sink[1], sink[2])
        rid = region_of_node[node]
        per_region = counts.setdefault(outlet, {})
        per_region[rid] = per_region.get(rid, 0) + weights[node]

    outlet_rows = []
    for outlet in sorted(counts):
        per_region = counts[outlet]
        region_rows = [
            {"region_id": rid, "cells": per_region[rid]}
            for rid in sorted(per_region)
        ]
        outlet_rows.append(
            {
                "row": outlet[0],
                "col": outlet[1],
                "total": sum(per_region.values()),
                "regions": region_rows,
            }
        )

    return {
        "outlets": outlet_rows,
        "regions": [
            {
                "region_id": region.region_id,
                "row_min": region.rect[0],
                "row_max": region.rect[1],
                "col_min": region.rect[2],
                "col_max": region.rect[3],
                "cell_count": region.cell_count,
            }
            for region in regions
        ],
        "total_cells": sum(region.cell_count for region in regions),
    }
