"""跨测区测区包与汇总测试。

核心断言：只凭测区包的汇总结果，必须与“把所有测区拼成整图后逐格追流”
的结果逐出口、逐测区一致；每格只按自身所属测区计一次。
"""

import io
import json
import random

import pytest

from app.app import create_app
from app.flow import Cell, dump_flow_csv
from app.regions import (
    PACKAGE_FORMAT,
    PACKAGE_VERSION,
    RegionError,
    build_region_package,
    summarize_packages,
)


# --------------------------------------------------------------------------- #
# 参考实现：拼成整图后逐格追流
# --------------------------------------------------------------------------- #
def stitch_and_count(regions):
    """``regions``: [(region_id, edges)]，返回与 summarize_packages 同口径的
    {(outlet_row, outlet_col): {region_id: cells}}。

    这是“拼成整图后逐格追流”的暴力参考：每格沿全局下游链一直走到出口，
    出发格按自身所属测区记账，因此反复跨区也不会重复计数。
    """
    owner = {}
    full: dict[Cell, object] = {}
    for region_id, edges in regions:
        for cell, downstream in edges.items():
            assert cell not in full, f"测区重叠：{cell}"
            full[cell] = downstream
            owner[cell] = region_id

    counts = {}
    for cell, downstream in full.items():
        cur = cell
        seen = set()
        while downstream is not None:
            assert cur not in seen, "参考图成环"
            seen.add(cur)
            cur = downstream
            downstream = full[cur]
        per_outlet = counts.setdefault(cur, {})
        per_outlet[owner[cell]] = per_outlet.get(owner[cell], 0) + 1
    return counts


def as_reference(summary):
    """把 summarize_packages 的输出转成参考实现的形状。"""
    result = {}
    for outlet in summary["outlets"]:
        result[(outlet["row"], outlet["col"])] = {
            row["region_id"]: row["cells"] for row in outlet["regions"]
        }
    return result


def rect_edges(edges, row_min, row_max, col_min, col_max):
    return {
        cell: downstream
        for cell, downstream in edges.items()
        if row_min <= cell[0] <= row_max and col_min <= cell[1] <= col_max
    }


def package_for(region_id, edges, rect):
    return build_region_package(
        region_id, rect[0], rect[1], rect[2], rect[3], edges
    )


def sample_grid(h=4, w=4):
    """与示例 CSV 相同的整图：每行向右，末列向下，(h-1,w-1) 出口。"""
    edges = {}
    for r in range(h):
        for c in range(w):
            if c < w - 1:
                edges[(r, c)] = (r, c + 1)
            elif r < h - 1:
                edges[(r, c)] = (r + 1, c)
            else:
                edges[(r, c)] = None
    return edges


# --------------------------------------------------------------------------- #
# 测区包生成
# --------------------------------------------------------------------------- #
def test_build_package_contents_and_roundtrip_json():
    full = sample_grid()
    edges = rect_edges(full, 0, 3, 0, 1)  # 左半边 4x2
    package = package_for("R1", edges, (0, 3, 0, 1))

    assert package["format"] == PACKAGE_FORMAT
    assert package["version"] == PACKAGE_VERSION
    assert package["region_id"] == "R1"
    assert package["cell_count"] == 8
    # 4x2 矩形的四周就是全部 8 格
    assert len(package["boundary"]) == 8
    # 每行在 col=1 终结并跨区指向 col=2；共 4 个终结格
    assert len(package["terminals"]) == 4
    assert all(t["down_row"] is not None for t in package["terminals"])
    assert all(t["down_col"] == 2 for t in package["terminals"])
    # 汇总格数之和等于区内格数
    assert sum(t["cells"] for t in package["terminals"]) == 8

    # 单独一个包没有跨区接收格，汇总必须指出断点
    decoded = json.loads(json.dumps(package))
    with pytest.raises(RegionError, match=r"缺少跨区接收格.*\(0,1\).*\(0,2\)"):
        summarize_packages([decoded])

    # 配上接收测区后即可汇总
    peer = package_for(
        "R2", rect_edges(sample_grid(), 0, 3, 2, 3), (0, 3, 2, 3)
    )
    summary = summarize_packages([decoded, peer])
    assert summary["total_cells"] == 16
    assert summary["outlets"][0]["total"] == 16


def test_package_records_scale_with_boundary_and_terminals_not_cells():
    """20x20 测区只有 76 个边界格 + 少量出口，记录数远小于 400 格。"""
    size = 20
    edges = {}
    for r in range(size):
        for c in range(size):
            if r < size - 1:
                edges[(r, c)] = (r + 1, c)  # 全部向下
            else:
                edges[(r, c)] = (r, c + 1) if c < size - 1 else None
    package = package_for("big", edges, (0, size - 1, 0, size - 1))
    records = len(package["boundary"]) + len(package["terminals"])
    assert package["cell_count"] == size * size
    assert len(package["boundary"]) == 4 * size - 4
    assert len(package["terminals"]) == 1
    assert records == 4 * size - 3
    assert records < package["cell_count"]
    # 包内没有任何逐格记录（cell_count 之外不出现每格明细）
    assert "cells" not in package
    assert all(set(item) == {"row", "col", "terminal_row", "terminal_col"}
               for item in package["boundary"])


def test_build_rejects_internal_missing_cell_with_location():
    edges = {
        (0, 0): (0, 1),
        (0, 1): (1, 1),  # (1,1) 在矩形内但 CSV 缺该格
        (0, 2): None,
        (1, 0): (1, 1),
        (1, 2): None,
        (2, 0): (1, 0),
        (2, 1): (1, 1),
        (2, 2): (1, 2),
    }
    with pytest.raises(RegionError, match=r"缺格.*\(0,1\).*\(1,1\)"):
        build_region_package("R", 0, 2, 0, 2, edges)


def test_build_rejects_cycle_with_cell_locations():
    edges = {
        (0, 0): (0, 1),
        (0, 1): (0, 0),  # 2 格环
        (1, 0): (0, 0),
        (1, 1): (1, 0),
    }
    with pytest.raises(RegionError, match=r"环.*\(0,1\).*\(0,0\)"):
        build_region_package("R", 0, 1, 0, 1, edges)


def test_build_rejects_cell_outside_rect_and_non_neighbor_crossing():
    edges = sample_grid()
    with pytest.raises(RegionError, match=r"不在测区范围"):
        build_region_package("R", 0, 1, 0, 1, edges)

    bad = {(0, 0): (0, 3)}
    with pytest.raises(RegionError, match="相邻"):
        build_region_package("R", 0, 2, 0, 2, bad)

    with pytest.raises(RegionError, match="region_id"):
        build_region_package("  ", 0, 1, 0, 1, {(0, 0): None})

    with pytest.raises(RegionError, match="没有任何格点"):
        build_region_package("R", 0, 1, 0, 1, {})


# --------------------------------------------------------------------------- #
# 汇总：与拼图逐格追流一致
# --------------------------------------------------------------------------- #
def test_summary_matches_stitched_trace_for_vertical_split():
    full = sample_grid()
    regions = [
        ("R1", rect_edges(full, 0, 3, 0, 1)),
        ("R2", rect_edges(full, 0, 3, 2, 3)),
    ]
    packages = [
        package_for("R1", regions[0][1], (0, 3, 0, 1)),
        package_for("R2", regions[1][1], (0, 3, 2, 3)),
    ]
    summary = summarize_packages(packages)
    reference = stitch_and_count(regions)

    assert as_reference(summary) == reference
    assert summary["outlets"] == [
        {
            "row": 3,
            "col": 3,
            "total": 16,
            "regions": [
                {"region_id": "R1", "cells": 8},
                {"region_id": "R2", "cells": 8},
            ],
        }
    ]
    assert summary["total_cells"] == 16


def test_flow_reenters_same_region_but_counts_cells_once():
    """流路 A -> B -> A：每格只按自身所属测区计一次。"""
    A = {
        (0, 0): (0, 1),
        (0, 1): (0, 2),  # 跨到 B
        (1, 0): (1, 1),
        (1, 1): None,    # A 内真实出口
    }
    B = {
        (0, 2): (0, 3),
        (0, 3): (1, 3),
        (1, 2): (1, 1),  # 跨回 A
        (1, 3): (1, 2),
    }
    packages = [
        package_for("A", A, (0, 1, 0, 1)),
        package_for("B", B, (0, 1, 2, 3)),
    ]
    # A 有两个终结格：跨区去 B 的 (0,1)（带 (0,0) 共 2 格）
    # 与 A 内真实出口 (1,1)（带 (1,0) 共 2 格）
    assert packages[0]["terminals"] == [
        {"row": 0, "col": 1, "cells": 2, "down_row": 0, "down_col": 2},
        {"row": 1, "col": 1, "cells": 2, "down_row": None, "down_col": None},
    ]
    assert packages[1]["terminals"] == [
        {"row": 1, "col": 2, "cells": 4, "down_row": 1, "down_col": 1}
    ]

    summary = summarize_packages(packages)
    reference = stitch_and_count([("A", A), ("B", B)])
    assert as_reference(summary) == reference
    # 路径 (0,0)->(0,1)->B...->(1,1) 两次进入 A，但 A 只记 4 格、B 只记 4 格
    assert summary["outlets"] == [
        {
            "row": 1,
            "col": 1,
            "total": 8,
            "regions": [
                {"region_id": "A", "cells": 4},
                {"region_id": "B", "cells": 4},
            ],
        }
    ]


def test_summary_rejects_missing_receiver_region():
    A = {(0, 0): (0, 1), (0, 1): (0, 2)}  # 指向区外 (0,2)
    with pytest.raises(RegionError, match=r"缺少跨区接收格.*\(0,1\).*\(0,2\)"):
        summarize_packages([package_for("A", A, (0, 0, 0, 1))])


def test_summary_rejects_missing_boundary_cell_in_target_package():
    A = {(0, 0): (0, 1), (0, 1): (0, 2)}
    B = {(0, 2): None, (0, 3): None}
    package_a = package_for("A", A, (0, 0, 0, 1))
    package_b = package_for("B", B, (0, 0, 2, 3))
    # 人为从 B 包中删掉接收格 (0,2) 的边界记录，制造断点
    package_b["boundary"] = [
        item for item in package_b["boundary"] if (item["row"], item["col"]) != (0, 2)
    ]
    with pytest.raises(RegionError, match=r"缺少跨区接收格.*\(0,1\).*\(0,2\).*断点"):
        summarize_packages([package_a, package_b])


def test_summary_rejects_cycle_and_names_boundary_cells():
    A = {(0, 0): (0, 1)}
    B = {(0, 1): (0, 0)}
    package_a = package_for("A", A, (0, 0, 0, 0))
    package_b = package_for("B", B, (0, 0, 1, 1))
    with pytest.raises(RegionError, match=r"成环.*\(0,0\).*\(0,1\)"):
        summarize_packages([package_a, package_b])


def test_summary_rejects_overlap_and_duplicate_ids():
    full = sample_grid()
    p1 = package_for("R1", rect_edges(full, 0, 1, 0, 1), (0, 1, 0, 1))
    p2 = package_for("R2", rect_edges(full, 1, 2, 1, 2), (1, 2, 1, 2))
    with pytest.raises(RegionError, match="重叠"):
        summarize_packages([p1, p2])

    p3 = package_for("R1", rect_edges(full, 2, 3, 2, 3), (2, 3, 2, 3))
    with pytest.raises(RegionError, match="重复"):
        summarize_packages([p1, p3])


def test_summary_rejects_malformed_packages():
    ok = package_for("R1", sample_grid(), (0, 3, 0, 3))
    for bad, match in [
        ("not a dict", "不是 JSON 对象"),
        ({**ok, "format": "x"}, "格式不正确"),
        ({**ok, "version": 99}, "版本不支持"),
        ({**ok, "cell_count": 0}, "cell_count"),
        ({**ok, "terminals": []}, "terminals"),
        ({**ok, "terminals": [{**ok["terminals"][0], "cells": 0}]}, "cells"),
        (
            {
                **ok,
                "terminals": [
                    {**ok["terminals"][0], "down_row": 9, "down_col": 9}
                ],
            },
            "相邻",
        ),
    ]:
        with pytest.raises(RegionError, match=match):
            summarize_packages([bad])

    with pytest.raises(RegionError, match="至少上传一个"):
        summarize_packages([])


def test_summary_cell_count_must_match_terminals_sum():
    ok = package_for("R1", sample_grid(), (0, 3, 0, 3))
    bad = json.loads(json.dumps(ok))
    bad["cell_count"] += 1
    with pytest.raises(RegionError, match="汇总格数不一致"):
        summarize_packages([bad])


# --------------------------------------------------------------------------- #
# 随机性质测试：随机整图 + 随机矩形切分
# --------------------------------------------------------------------------- #
def random_dag_grid(rng, height, width):
    """随机无环功能图：随机全序，每格只能流向全序更靠后的四邻格。

    全序不是行优先，因此流路可向上/向左，能自然地反复进出同一测区矩形。
    """
    cells = [(r, c) for r in range(height) for c in range(width)]
    order = list(cells)
    rng.shuffle(order)
    rank = {cell: i for i, cell in enumerate(order)}
    edges = {}
    for cell in cells:
        r, c = cell
        neighbors = [
            (r + dr, c + dc)
            for dr, dc in ((1, 0), (-1, 0), (0, 1), (0, -1))
            if 0 <= r + dr < height and 0 <= c + dc < width
        ]
        forward = [n for n in neighbors if rank[n] > rank[cell]]
        if not forward or rng.random() < 0.1:
            edges[cell] = None
        else:
            edges[cell] = rng.choice(forward)
    return edges


def random_rect_partition(rng, height, width):
    """随机生成覆盖整个矩形的非重叠矩形切分（先随机切行，再逐带随机切列）。"""
    def split(total):
        cuts = sorted(rng.sample(range(1, total), rng.randint(0, total - 1)))
        bounds = [0] + cuts + [total]
        return [(bounds[i], bounds[i + 1] - 1) for i in range(len(bounds) - 1)]

    rects = []
    while len(rects) < 2:
        rects = []
        for r0, r1 in split(height):
            for c0, c1 in split(width):
                rects.append((r0, r1, c0, c1))
    return rects


@pytest.mark.parametrize("seed", range(40))
def test_random_partition_summary_matches_stitched_trace(seed):
    rng = random.Random(seed)
    full = random_dag_grid(rng, 6, 8)
    rects = random_rect_partition(rng, 6, 8)
    assert len(rects) >= 2

    regions = []
    packages = []
    for i, rect in enumerate(rects):
        edges = rect_edges(full, *rect)
        if not edges:
            continue
        region_id = f"R{i}"
        regions.append((region_id, edges))
        packages.append(package_for(region_id, edges, rect))

    summary = summarize_packages(packages)
    reference = stitch_and_count(regions)
    assert as_reference(summary) == reference
    # 每格只记一次：所有出口的所有测区格数之和 == 总格数
    assert sum(o["total"] for o in summary["outlets"]) == len(full)
    assert summary["total_cells"] == len(full)


# --------------------------------------------------------------------------- #
# 接口层
# --------------------------------------------------------------------------- #
@pytest.fixture()
def client(tmp_path):
    app = create_app(db_path=str(tmp_path / "test.db"))
    app.config.update(TESTING=True)
    return app.test_client()


def _csv_bytes(edges):
    return dump_flow_csv(edges).encode("utf-8")


def test_api_build_package_download(client):
    full = sample_grid()
    edges = rect_edges(full, 0, 3, 0, 1)
    resp = client.post(
        "/api/region/package",
        data={
            "file": (io.BytesIO(_csv_bytes(edges)), "r1.csv"),
            "region_id": "R1",
            "row_min": "0",
            "row_max": "3",
            "col_min": "0",
            "col_max": "1",
        },
        content_type="multipart/form-data",
    )
    assert resp.status_code == 200, resp.get_json()
    package = json.loads(resp.data)
    assert package["region_id"] == "R1"
    assert package["cell_count"] == 8
    assert "R1.region.json" in resp.headers["Content-Disposition"]


def test_api_build_package_rejects_with_location(client):
    bad = {
        (0, 0): None, (0, 1): None, (0, 2): None,
        (1, 0): (1, 0),  # 自环
        (1, 1): None, (1, 2): None,
        (2, 0): None, (2, 1): None, (2, 2): None,
    }
    resp = client.post(
        "/api/region/package",
        data={
            "file": (io.BytesIO(_csv_bytes(bad)), "r.csv"),
            "region_id": "R",
            "row_min": "0",
            "row_max": "2",
            "col_min": "0",
            "col_max": "2",
        },
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400
    assert "(1,0)" in resp.get_json()["error"]


def test_api_summary_multiple_files(client):
    full = sample_grid()
    packages = [
        package_for("R1", rect_edges(full, 0, 3, 0, 1), (0, 3, 0, 1)),
        package_for("R2", rect_edges(full, 0, 3, 2, 3), (0, 3, 2, 3)),
    ]
    resp = client.post(
        "/api/region/summary",
        data={
            "files": [
                (io.BytesIO(json.dumps(packages[0]).encode()), "r1.json"),
                (io.BytesIO(json.dumps(packages[1]).encode()), "r2.json"),
            ]
        },
        content_type="multipart/form-data",
    )
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    assert body["ok"] is True
    assert body["outlets"][0]["total"] == 16
    assert {r["region_id"]: r["cells"] for r in body["outlets"][0]["regions"]} == {
        "R1": 8,
        "R2": 8,
    }


def test_api_summary_reports_break_and_cycles(client):
    A = {(0, 0): (0, 1)}
    package_a = package_for("A", A, (0, 0, 0, 0))

    resp = client.post(
        "/api/region/summary",
        data={"files": [(io.BytesIO(json.dumps(package_a).encode()), "a.json")]},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400
    assert "缺少跨区接收格" in resp.get_json()["error"]
    assert "(0,1)" in resp.get_json()["error"]

    B = {(0, 1): (0, 0)}
    package_b = package_for("B", B, (0, 0, 1, 1))
    resp = client.post(
        "/api/region/summary",
        data={
            "files": [
                (io.BytesIO(json.dumps(package_a).encode()), "a.json"),
                (io.BytesIO(json.dumps(package_b).encode()), "b.json"),
            ]
        },
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400
    error = resp.get_json()["error"]
    assert "环" in error and "(0,0)" in error and "(0,1)" in error


def test_api_summary_rejects_bad_json(client):
    resp = client.post(
        "/api/region/summary",
        data={"files": [(io.BytesIO(b"not json"), "bad.json")]},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400
    assert "JSON" in resp.get_json()["error"]


def test_existing_import_still_works(client):
    full = sample_grid()
    resp = client.post(
        "/api/import",
        data={"file": (io.BytesIO(_csv_bytes(full)), "full.csv")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 200
    assert resp.get_json()["outlet_count"] == 1
