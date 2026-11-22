"""核心算法单元测试：解析、校验、全量重算、增量更正。"""

import pytest

from app.flow import (
    FlowError,
    affected_cells,
    apply_correction,
    compute_accumulation,
    detect_cycle,
    dump_flow_csv,
    parse_flow_csv,
    validate_edges,
)


def grid_4x4():
    """每行向右流，末列向下流，(3,3) 出口。"""
    edges = {}
    for r in range(4):
        for c in range(4):
            if c < 3:
                edges[(r, c)] = (r, c + 1)
            elif r < 3:
                edges[(r, c)] = (r + 1, c)
            else:
                edges[(r, c)] = None
    return edges


def test_parse_csv_and_full_accumulation_4x4():
    edges = grid_4x4()
    text = dump_flow_csv(edges)
    parsed = parse_flow_csv(text)
    assert parsed == edges
    validate_edges(parsed)
    accum = compute_accumulation(parsed)
    # 全部 16 格最终汇入唯一出口
    assert accum[(3, 3)] == 16
    # 每条横向链：第 c 列累积 4-c，末列再沿列叠加
    assert accum[(0, 0)] == 1
    assert accum[(0, 3)] == 4
    assert accum[(2, 3)] == 12
    assert all(v >= 1 for v in accum.values())
    assert sum(1 for d in edges.values() if d is None) == 1


def test_parse_rejects_bad_header_and_columns():
    with pytest.raises(FlowError):
        parse_flow_csv("a,b,c,d\n1,2,3,4")
    with pytest.raises(FlowError):
        parse_flow_csv("row,col,down_row,down_col\n0,0,0")
    with pytest.raises(FlowError):
        parse_flow_csv("row,col,down_row,down_col\n0,0,0,")
    with pytest.raises(FlowError):
        parse_flow_csv("row,col,down_row,down_col\n0,0,x,1")


def test_parse_rejects_duplicate_and_blank_id():
    csv = (
        "row,col,down_row,down_col\n"
        "0,0,0,1\n"
        "0,0,1,0\n"
    )
    with pytest.raises(FlowError, match="重复"):
        parse_flow_csv(csv)
    with pytest.raises(FlowError, match="行列号"):
        parse_flow_csv("row,col,down_row,down_col\n,0,0,1\n")


def test_validate_rejects_missing_downstream_and_non_neighbor():
    edges = grid_4x4()
    edges[(0, 0)] = (9, 9)
    with pytest.raises(FlowError, match="不在导入"):
        validate_edges(edges)

    edges = grid_4x4()
    edges[(0, 0)] = (2, 2)  # 对角格，非四邻域
    with pytest.raises(FlowError, match="相邻"):
        validate_edges(edges)

    edges = grid_4x4()
    edges[(0, 0)] = (0, 0)
    with pytest.raises(FlowError, match="自身"):
        validate_edges(edges)


def test_detect_cycle_two_node_and_long_cycle():
    edges = grid_4x4()
    edges[(0, 0)] = (0, 1)
    edges[(0, 1)] = (0, 0)  # 2 格环
    with pytest.raises(FlowError, match="环"):
        detect_cycle(edges)
    with pytest.raises(FlowError, match="环"):
        compute_accumulation(edges)

    edges = grid_4x4()
    # 长环：(0,0)->(1,0)->(1,1)->(0,1)->(0,0)
    edges[(0, 0)] = (1, 0)
    edges[(1, 0)] = (1, 1)
    edges[(1, 1)] = (0, 1)
    edges[(0, 1)] = (0, 0)
    with pytest.raises(FlowError):
        detect_cycle(edges, roots=[(0, 0), (1, 1)])


def test_apply_correction_matches_full_recompute_basic():
    edges = grid_4x4()
    accum = compute_accumulation(edges)
    changes = {(0, 1): (1, 1), (2, 3): (3, 3)}  # 后者与原边相同，属无改动项

    new_edges, new_accum, affected = apply_correction(edges, accum, changes)

    # 受影响集合 = 更正格 + 旧下游链 + 新下游链
    expected_affected = affected_cells(edges, changes)
    assert affected == expected_affected
    assert (0, 1) in affected and (3, 3) in affected

    full_edges = dict(edges)
    full_edges.update(changes)
    full_accum = compute_accumulation(full_edges)
    assert new_edges == full_edges
    assert new_accum == full_accum
    # 集合外格点逐格不变
    for cell in edges:
        if cell not in affected:
            assert new_accum[cell] == accum[cell]
    # 原始入参不被修改
    assert edges[(0, 1)] == (0, 2)


def test_apply_correction_change_outlet_and_back():
    edges = grid_4x4()
    accum = compute_accumulation(edges)

    # 把 (3,2) 改成新出口：它带着自己的上游链一起脱离老出口
    changes1 = {(3, 2): None}
    edges1, accum1, affected1 = apply_correction(edges, accum, changes1)
    assert edges1[(3, 2)] is None
    full1 = compute_accumulation({**edges, **changes1})
    assert accum1 == full1
    # 新出口的集水含自身及其上游 (3,0),(3,1)；老出口收其余格
    assert accum1[(3, 2)] == 3
    assert accum1[(3, 3)] == 13

    # 再改回去（第二批基于第一批的结果）
    changes2 = {(3, 2): (3, 3)}
    edges2, accum2, _ = apply_correction(edges1, accum1, changes2)
    assert accum2 == accum
    assert edges2 == edges


def test_apply_correction_rejects_cycle_and_keeps_input():
    edges = grid_4x4()
    accum = compute_accumulation(edges)
    # (0,1) 指回 (0,0)，与原边 (0,0)->(0,1) 构成 2 环
    with pytest.raises(FlowError, match="环"):
        apply_correction(edges, accum, {(0, 1): (0, 0)})
    # 入参未被污染
    assert edges[(0, 1)] == (0, 2)
    assert accum == compute_accumulation(edges)


def test_apply_correction_rejects_invalid_downstream():
    edges = grid_4x4()
    accum = compute_accumulation(edges)
    with pytest.raises(FlowError, match="不在格点集合"):
        apply_correction(edges, accum, {(0, 0): (7, 7)})
    with pytest.raises(FlowError, match="相邻"):
        apply_correction(edges, accum, {(0, 0): (0, 2)})
    with pytest.raises(FlowError, match="待更正格点"):
        apply_correction(edges, accum, {(5, 5): None})
    with pytest.raises(FlowError, match="为空"):
        apply_correction(edges, accum, {})


def test_affected_set_is_minimal():
    """无关节点不应进入受影响集合。"""
    edges = grid_4x4()
    changes = {(0, 0): (1, 0)}
    affected = affected_cells(edges, changes)
    # 旧链沿第 0 行向右、新链沿第 0 列向下后再向右；
    # (2,1)、(3,1) 等既不在旧链也不在新链上，应被排除。
    assert (2, 1) not in affected
    assert (3, 1) not in affected
    assert (2, 2) not in affected
    assert (1, 2) in affected  # 新下游链 (1,0)->(1,1)->(1,2)->(1,3)
    assert (0, 0) in affected and (1, 0) in affected and (3, 3) in affected
    assert (0, 1) in affected  # 旧下游链
