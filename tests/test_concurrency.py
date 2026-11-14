"""端到端并发测试：启动真实 uvicorn 服务，用多线程 HTTP 请求验证竞态。

TestClient 的并发能力有限，这里单独起一个进程内 uvicorn，通过真实 socket 压测。
"""

from __future__ import annotations

import concurrent.futures
import json
import socket
import threading
import time
import urllib.error
import urllib.request

import pytest
import uvicorn

from app.config import Settings
from app.main import create_app


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture()
def server(tmp_path):
    port = _free_port()
    app = create_app(Settings(database_path=str(tmp_path / "live.db")))
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", lifespan="on")
    srv = uvicorn.Server(config)
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()

    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            urllib.request.urlopen(base + "/healthz", timeout=0.5).read()
            break
        except Exception:
            time.sleep(0.05)
    else:
        raise RuntimeError("server did not start")

    yield base

    srv.should_exit = True
    thread.join(timeout=10)


def _post(base: str, records: list[dict]) -> int:
    req = urllib.request.Request(
        base + "/api/v1/readings/batch",
        data=json.dumps({"records": records}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code


def _get_json(base: str, path: str) -> dict:
    with urllib.request.urlopen(base + path, timeout=15) as resp:
        return json.load(resp)


def _rec(hour, value=None, revision=1, retracted=False, station="S", date="2025-05-01"):
    body = {
        "station_id": station,
        "date": date,
        "hour": hour,
        "revision": revision,
        "retracted": retracted,
    }
    if value is not None:
        body["value"] = value
    return body


def test_parallel_writers_same_revision_only_one_wins(server):
    """同一修订号、不同内容并发提交：恰好 1 个 200，其余 409，库中只有一个值。"""
    contenders = [float(v) for v in range(10, 26)]
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(contenders)) as pool:
        statuses = list(pool.map(lambda v: _post(server, [_rec(3, v, revision=1)]), contenders))

    assert statuses.count(200) == 1, statuses
    assert statuses.count(409) == len(contenders) - 1, statuses

    body = _get_json(server, "/api/v1/stations/S/daily?date=2025-05-01")
    value = body["hours"][3]["value"]
    assert value in contenders
    assert body["hours"][3]["revision"] == 1


def test_parallel_revisions_converge_to_max(server):
    """不同修订号乱序并发：最终结果一定是最新修订号的值。"""
    revisions = list(range(1, 13))
    records = [_rec(11, float(rev), revision=rev) for rev in revisions]
    barrier = threading.Barrier(len(records))

    def send(record):
        barrier.wait()
        return _post(server, [record])

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(records)) as pool:
        statuses = list(pool.map(send, records))

    assert statuses == [200] * len(records)
    body = _get_json(server, "/api/v1/stations/S/daily?date=2025-05-01")
    assert body["hours"][11]["revision"] == 12
    assert body["hours"][11]["value"] == 12.0


def test_conflicting_batch_never_partially_commits(server):
    """并发提交的冲突批次不得留下任何记录。"""
    assert _post(server, [_rec(0, 1.0, revision=1)]) == 200

    def send(offset):
        return _post(
            server,
            [
                _rec(14 + offset, 5.0, revision=1),
                _rec(0, 100.0 + offset, revision=1),  # 与基线冲突
            ],
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
        statuses = list(pool.map(send, range(5)))

    assert statuses == [409] * 5
    body = _get_json(server, "/api/v1/stations/S/daily?date=2025-05-01")
    assert body["hours"][0]["value"] == 1.0
    for hour in range(14, 19):
        assert body["hours"][hour]["state"] == "missing"


def test_reads_reflect_writes_immediately(server):
    """写入后立即查询即可看到结果；撤回后立即消失。"""
    assert _post(server, [_rec(h, 10.0) for h in range(24)]) == 200
    body = _get_json(server, "/api/v1/stations/S/daily?date=2025-05-01")
    assert body["daily_mean"] == 10.0

    assert _post(server, [_rec(0, revision=2, retracted=True)]) == 200
    body = _get_json(server, "/api/v1/stations/S/daily?date=2025-05-01")
    assert body["valid_hours"] == 23
    assert body["retracted_hours"] == [0]
    assert body["hours"][0]["state"] == "retracted"
