"""随机化性质测试：任意合法随机图 + 任意随机更正批次下，
增量重算结果必须与整图重算逐格一致，且只重算受影响集合。"""

import random

import pytest

from app.flow import (
    FlowError,
    affected_cells,
    apply_correction,
    compute_accumulation,
    detect_cycle,
    validate_edges,
)

W, H = 6, 6


def random_dag_edges(rng: random.Random):
    """生成随机无环功能图：编号小的格只能流向编号更大的格（或出口），
    且下游限定四邻域，因此天然无环。"""
    cells = [(r, c) for r in range(H) for c in range(W)]
    index = {cell: i for i, cell in enumerate(cells)}
    edges = {}
    for cell in cells:
        r, c = cell
        neighbors = [
            (r + dr, c + dc)
            for dr, dc in ((1, 0), (-1, 0), (0, 1), (0, -1))
            if 0 <= r + dr < H and 0 <= c + dc < W
        ]
        forward = [n for n in neighbors if index[n] > index[cell]]
        if not forward:
            edges[cell] = None  # 最大编号格必为出口
        elif rng.random() < 0.12:
            edges[cell] = None  # 随机制造多个出口
        else:
            edges[cell] = rng.choice(forward)
    return edges


def random_valid_changes(rng: random.Random, edges):
    """构造一批几何上合法的改动（可能成环，交给被测函数判定）。"""
    cells = list(edges)
    k = rng.randint(1, 5)
    chosen = rng.sample(cells, k)
    changes = {}
    for cell in chosen:
        r, c = cell
        neighbors = [
            (r + dr, c + dc)
            for dr, dc in ((1, 0), (-1, 0), (0, 1), (0, -1))
            if 0 <= r + dr < H and 0 <= c + dc < W
        ]
        target = rng.choice(neighbors + [None])
        changes[cell] = target
    return changes


@pytest.mark.parametrize("seed", range(60))
def test_incremental_equals_full_recompute(seed):
    rng = random.Random(seed)
    edges = random_dag_edges(rng)
    validate_edges(edges)
    accum = compute_accumulation(edges)

    changes = random_valid_changes(rng, edges)
    trial_edges = dict(edges)
    trial_edges.update(changes)

    try:
        detect_cycle(trial_edges, roots=list(changes))
    except FlowError:
        # 随机改动可能成环：apply_correction 必须拒绝且不破坏原数据
        with pytest.raises(FlowError):
            apply_correction(edges, accum, changes)
        return

    new_edges, new_accum, affected = apply_correction(edges, accum, changes)

    # 1) 与整图重算逐格一致
    full_edges = dict(edges)
    full_edges.update(changes)
    full_accum = compute_accumulation(full_edges)
    assert new_edges == full_edges
    assert new_accum == full_accum

    # 2) 只重算受影响集合：集合外逐格保持原值
    assert affected == affected_cells(edges, changes)
    for cell in edges:
        if cell not in affected:
            assert new_accum[cell] == accum[cell]

    # 3) 累积值恒为正，且所有出口累积之和等于总格数（集水区划分守恒）
    assert all(v >= 1 for v in new_accum.values())
    total = sum(new_accum[cell] for cell, d in new_edges.items() if d is None)
    assert total == len(new_edges)


def test_sequential_batches_latest_batch_includes_prior_ones():
    """多批顺序提交：后续批次必须建立在先前已生效批次之上，
    每一步的增量结果都与基于当前状态的整图重算一致。"""
    rng = random.Random(20261122)
    cur_edges = random_dag_edges(rng)
    cur_accum = compute_accumulation(cur_edges)

    for _ in range(10):
        prev_edges, prev_accum = cur_edges, cur_accum
        changes = random_valid_changes(rng, cur_edges)
        trial = dict(cur_edges)
        trial.update(changes)
        try:
            detect_cycle(trial, roots=list(changes))
        except FlowError:
            with pytest.raises(FlowError):
                apply_correction(cur_edges, cur_accum, changes)
            # 失败批次不改变状态（模拟服务端回滚）
            assert cur_edges == prev_edges and cur_accum == prev_accum
            continue

        cur_edges, cur_accum, affected = apply_correction(
            cur_edges, cur_accum, changes
        )
        # 受影响集合口径基于“上一代”边
        assert affected == affected_cells(prev_edges, changes)
        # 与基于当前已生效状态的整图重算一致
        assert cur_accum == compute_accumulation(cur_edges)
        # 上一代数据不被修改
        assert prev_accum == compute_accumulation(prev_edges)
