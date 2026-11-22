"""Flask 接口层测试：导入、批量更正、失败回滚、下载、批次交错与并发。"""

import io
import json
import sqlite3
import threading

import pytest

from app.app import create_app, sample_edges
from app.db import get_db, load_accum, load_edges
from app.flow import compute_accumulation, dump_flow_csv


@pytest.fixture()
def app(tmp_path):
    app = create_app(db_path=str(tmp_path / "test.db"))
    app.config.update(TESTING=True)
    return app


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def imported(client):
    data = dump_flow_csv(sample_edges()).encode("utf-8")
    resp = client.post(
        "/api/import",
        data={"file": (io.BytesIO(data), "sample.csv")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 200, resp.get_json()
    return resp.get_json()


def test_import_and_state(client, imported):
    assert imported["cell_count"] == 16
    assert imported["outlet_count"] == 1
    state = client.get("/api/state").get_json()
    assert state["cell_count"] == 16
    outlet_rows = [c for c in state["cells"] if c["down_row"] is None]
    assert len(outlet_rows) == 1
    assert outlet_rows[0]["row"] == 3 and outlet_rows[0]["col"] == 3
    assert outlet_rows[0]["accum"] == 16


def test_import_rejects_cycle_and_keeps_old_data(client, imported):
    bad = dict(sample_edges())
    bad[(0, 1)] = (0, 0)  # 与 (0,0)->(0,1) 成环
    resp = client.post(
        "/api/import",
        data={"file": (io.BytesIO(dump_flow_csv(bad).encode()), "bad.csv")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400
    assert "环" in resp.get_json()["error"]

    # 旧数据完好且可下载
    flow_csv = client.get("/download/flow.csv").data.decode()
    assert "3,3,," in flow_csv
    accum_csv = client.get("/download/accumulation.csv").data.decode()
    assert "3,3,16" in accum_csv


def test_multi_cell_correction_incremental(client, app, imported):
    changes = [
        {"row": 0, "col": 1, "down_row": 1, "down_col": 1},
        {"row": 1, "col": 0, "down_row": 2, "down_col": 0},
    ]
    resp = client.post("/api/correct", json={"changes": changes})
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    assert body["changed_count"] == 2
    assert body["affected_count"] == len(body["affected"])

    # 数据库中的结果必须与整图重算逐格一致
    conn = get_db(app.config["DB_PATH"])
    edges, accum = load_edges(conn), load_accum(conn)
    assert edges[(0, 1)] == (1, 1)
    assert accum == compute_accumulation(edges)
    conn.close()

    # 下载的汇水表与库一致
    accum_csv = client.get("/download/accumulation.csv").data.decode()
    for cell, value in accum.items():
        assert f"{cell[0]},{cell[1]},{value}" in accum_csv


def test_correction_rejects_cycle_and_rolls_back(client, app, imported):
    before = _snapshot(app)
    resp = client.post(
        "/api/correct",
        json={"changes": [{"row": 0, "col": 1, "down_row": 0, "down_col": 0}]},
    )
    assert resp.status_code == 400
    assert "环" in resp.get_json()["error"]
    after = _snapshot(app)
    assert after == before  # 原流向和汇水表照常可用

    # 失败后仍可正常下载
    assert client.get("/download/flow.csv").status_code == 200
    assert client.get("/download/accumulation.csv").status_code == 200


def test_correction_rejects_non_neighbor_and_missing_cell(client, app, imported):
    before = _snapshot(app)
    resp = client.post(
        "/api/correct",
        json={"changes": [{"row": 0, "col": 0, "down_row": 0, "down_col": 2}]},
    )
    assert resp.status_code == 400 and "相邻" in resp.get_json()["error"]

    resp = client.post(
        "/api/correct",
        json={"changes": [{"row": 9, "col": 9, "down_row": None, "down_col": None}]},
    )
    assert resp.status_code == 400 and "不在当前格点集合" in resp.get_json()["error"]

    # 半填写下游
    resp = client.post(
        "/api/correct",
        json={"changes": [{"row": 0, "col": 0, "down_row": 1, "down_col": None}]},
    )
    assert resp.status_code == 400 and "同时" in resp.get_json()["error"]

    # 同批重复格
    resp = client.post(
        "/api/correct",
        json={
            "changes": [
                {"row": 0, "col": 0, "down_row": 1, "down_col": 0},
                {"row": 0, "col": 0, "down_row": 0, "down_col": 1},
            ]
        },
    )
    assert resp.status_code == 400 and "多次" in resp.get_json()["error"]
    assert _snapshot(app) == before


def test_interleaved_batches_later_includes_earlier(client, app, imported):
    """两批更正交错：第二批基于第一批提交后的状态计算。"""
    batch1 = [{"row": 3, "col": 2, "down_row": None, "down_col": None}]  # 新出口
    # 第二批把 (3,0) 改汇到上方 (2,0)，脱离新出口的集水区
    batch2 = [{"row": 3, "col": 0, "down_row": 2, "down_col": 0}]

    r1 = client.post("/api/correct", json={"changes": batch1})
    assert r1.status_code == 200
    mid = _snapshot(app)
    assert mid["edges"][(3, 2)] is None
    # 新出口带着上游链 (3,0)->(3,1)->(3,2)，共 3 格
    assert mid["accum"][(3, 2)] == 3
    assert mid["accum"][(3, 3)] == 13

    r2 = client.post("/api/correct", json={"changes": batch2})
    assert r2.status_code == 200, r2.get_json()
    final = _snapshot(app)
    # 第二批必须看到第一批已生效的新出口 (3,2) 才能正确计算
    assert final["edges"][(3, 0)] == (2, 0)
    assert final["accum"] == compute_accumulation(final["edges"])
    assert final["accum"][(3, 2)] == 2  # 只剩 (3,1) 与自身
    assert final["accum"][(3, 3)] == 14

    history = client.get("/api/state").get_json()["corrections"]
    kinds = [h["kind"] for h in history]
    assert kinds.count("correction") == 2


def test_save_failure_rolls_back(client, app, imported, monkeypatch):
    """模拟保存阶段数据库失败：旧流向和汇水表必须照常可用。"""
    import app.app as app_module
    import app.db as db_module

    before = _snapshot(app)
    real_get_db = db_module.get_db

    class FailingConnection:
        """仅让更正事务的 UPDATE cells 失败，其余调用原样转发。"""

        def __init__(self, real):
            self._real = real

        def execute(self, sql, *args, **kwargs):
            if isinstance(sql, str) and sql.lstrip().startswith("UPDATE cells"):
                raise sqlite3.OperationalError("simulated disk I/O error")
            return self._real.execute(sql, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._real, name)

        def __enter__(self):
            return self._real.__enter__()

        def __exit__(self, *exc):
            return self._real.__exit__(*exc)

    # app.py 通过 from .db import get_db 绑定了名字，需 patch 它的绑定
    monkeypatch.setattr(
        app_module, "get_db", lambda path: FailingConnection(real_get_db(path))
    )
    resp = client.post(
        "/api/correct",
        json={"changes": [{"row": 0, "col": 0, "down_row": 1, "down_col": 0}]},
    )
    assert resp.status_code == 400
    assert "保存失败" in resp.get_json()["error"]
    monkeypatch.undo()

    assert _snapshot(app) == before
    # 失败之后服务仍可继续工作：再提交一个合法批次应成功
    ok = client.post(
        "/api/correct",
        json={"changes": [{"row": 0, "col": 0, "down_row": 1, "down_col": 0}]},
    )
    assert ok.status_code == 200


def test_concurrent_batches_are_serialized(client, app, imported):
    """并发提交多批更正：每请求独立连接，写事务由 BEGIN IMMEDIATE +
    busy_timeout 串行化，不允许脏写/丢失更新；最终汇水表必须与整图重算一致。"""
    # 6 个批次作用于 6 个互不相同的格点；每条新边都让该格更靠近出口
    # (3,3)（曼哈顿距离严格减小），因此任意提交顺序都不会成环，全部应成功。
    batches = [
        [{"row": 0, "col": 0, "down_row": 1, "down_col": 0}],
        [{"row": 0, "col": 2, "down_row": 1, "down_col": 2}],
        [{"row": 1, "col": 1, "down_row": 2, "down_col": 1}],
        [{"row": 2, "col": 0, "down_row": 3, "down_col": 0}],
        [{"row": 1, "col": 3, "down_row": 2, "down_col": 3}],
        [{"row": 2, "col": 2, "down_row": 3, "down_col": 2}],
    ]
    results = []
    lock = threading.Lock()

    def worker(batch):
        resp = client.post("/api/correct", json={"changes": batch})
        with lock:
            results.append((batch, resp.status_code, resp.get_json()))

    threads = [threading.Thread(target=worker, args=(b,)) for b in batches]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 距离严格减小 => 任意顺序都不会成环，所有批次都应成功
    assert all(code == 200 for _, code, _ in results), results
    succeeded = [batch[0] for batch, code, _ in results if code == 200]
    assert len(succeeded) == len(batches)

    conn = get_db(app.config["DB_PATH"])
    edges, accum = load_edges(conn), load_accum(conn)
    # 每个批次的改动都必须在最终流向里
    for change in succeeded:
        cell = (change["row"], change["col"])
        if change["down_row"] is None:
            assert edges[cell] is None
        else:
            assert edges[cell] == (change["down_row"], change["down_col"])
    # 最终图无环且汇水表与整图重算一致——串行化正确性的核心断言
    assert accum == compute_accumulation(edges)
    conn.close()


def test_downloads_require_data(tmp_path):
    fresh = create_app(db_path=str(tmp_path / "empty.db"))
    c = fresh.test_client()
    assert c.get("/download/flow.csv").status_code == 409
    assert c.get("/download/accumulation.csv").status_code == 409


def test_bad_json_and_empty_batch(client, imported):
    assert client.post("/api/correct", data="not-json").status_code == 400
    assert client.post("/api/correct", json={"changes": []}).status_code == 400


def _snapshot(app):
    conn = get_db(app.config["DB_PATH"])
    data = {"edges": load_edges(conn), "accum": load_accum(conn)}
    conn.close()
    return data
