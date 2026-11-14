import threading

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

DAY = "2025-03-01"


@pytest.fixture()
def client(tmp_path):
    app = create_app(Settings(database_path=str(tmp_path / "test.db")))
    with TestClient(app) as c:
        yield c


def rec(hour, value=None, revision=1, retracted=False, station="S1", date=DAY):
    body = {
        "station_id": station,
        "date": date,
        "hour": hour,
        "revision": revision,
        "retracted": retracted,
    }
    if not retracted:
        body["value"] = value
    return body


def post(client, records):
    return client.post("/api/v1/readings/batch", json={"records": records})


def daily(client, station="S1", date=DAY):
    return client.get(f"/api/v1/stations/{station}/daily", params={"date": date})


def full_day(client, value=10.0, station="S1", date=DAY):
    res = post(client, [rec(h, value, station=station, date=date) for h in range(24)])
    assert res.status_code == 200, res.text
    return res


# ---------------------------------------------------------------- 基础写入
def test_healthz(client):
    assert client.get("/healthz").json() == {"status": "ok"}


def test_batch_creates_and_reports_daily_mean(client):
    full_day(client, 10.0)
    body = daily(client).json()
    assert body["status"] == "ok"
    assert body["valid_hours"] == 24
    assert body["daily_mean"] == 10.0
    assert body["missing_hours"] == []
    assert len(body["hours"]) == 24
    assert body["timezone"] == "Asia/Shanghai"


def test_under_20_hours_returns_missing_hours_no_mean(client):
    post(client, [rec(h, 12.0) for h in range(19)])
    body = daily(client).json()
    assert body["status"] == "insufficient_valid_hours"
    assert body["daily_mean"] is None
    assert body["valid_hours"] == 19
    assert body["missing_hours"] == list(range(19, 24))


def test_exactly_20_hours_is_enough(client):
    post(client, [rec(h, 5.0) for h in range(20)])
    body = daily(client).json()
    assert body["status"] == "ok"
    assert body["daily_mean"] == 5.0
    assert body["missing_hours"] == [20, 21, 22, 23]


def test_mean_ignores_missing_hours(client):
    post(client, [rec(h, 10.0) for h in range(20)])
    post(client, [rec(20, 100.0)])
    body = daily(client).json()
    assert body["valid_hours"] == 21
    # (10*20 + 100) / 21 = 14.2857
    assert body["daily_mean"] == 14.29


# ---------------------------------------------------------------- 修订语义
def test_higher_revision_wins_immediately(client):
    full_day(client, 10.0)
    res = post(client, [rec(5, 99.0, revision=2)])
    assert res.status_code == 200
    body = daily(client).json()
    assert body["hours"][5]["value"] == 99.0
    assert body["hours"][5]["revision"] == 2
    assert body["daily_mean"] == round((10 * 23 + 99) / 24, 2)


def test_lower_revision_later_does_not_override(client):
    post(client, [rec(3, 50.0, revision=5)])
    post(client, [rec(3, 10.0, revision=4)])  # 迟到旧修订
    body = daily(client).json()
    assert body["hours"][3]["value"] == 50.0
    assert body["hours"][3]["revision"] == 5


def test_same_revision_same_content_is_idempotent(client):
    first = post(client, [rec(1, 10.0, revision=1)])
    second = post(client, [rec(1, 10.0, revision=1)])
    assert first.status_code == second.status_code == 200
    assert first.json()["created"] == 1
    assert second.json()["created"] == 0
    assert second.json()["unchanged"] == 1


def test_same_revision_same_retraction_is_idempotent(client):
    full_day(client, 10.0)
    a = post(client, [rec(7, revision=2, retracted=True)])
    b = post(client, [rec(7, revision=2, retracted=True)])
    assert a.status_code == b.status_code == 200
    assert b.json()["unchanged"] == 1
    assert daily(client).json()["retracted_hours"] == [7]


def test_batch_response_lists_stations_and_dates(client):
    body = post(
        client,
        [
            rec(0, 1.0, station="A", date="2025-03-01"),
            rec(0, 2.0, station="B", date="2025-03-02"),
        ],
    ).json()
    assert body["received"] == 2
    assert body["stations"] == ["A", "B"]
    assert body["dates"] == ["2025-03-01", "2025-03-02"]


def test_large_batch_across_station_dates(client):
    records = [
        rec(h, 10.0 + h, station=st, date=d)
        for st in ("A", "B", "C", "D", "E")
        for d in ("2025-03-01", "2025-03-02")
        for h in range(24)
    ]
    res = post(client, records)
    assert res.status_code == 200, res.text
    assert res.json()["created"] == 240
    for st in ("A", "B", "C", "D", "E"):
        assert daily(client, st, "2025-03-01").json()["daily_mean"] == 21.5


def test_older_revision_retraction_is_ignored(client):
    full_day(client, 10.0)
    post(client, [rec(7, 42.0, revision=3)])
    res = post(client, [rec(7, revision=2, retracted=True)])
    assert res.status_code == 200
    body = daily(client).json()
    assert body["hours"][7]["state"] == "valid"
    assert body["hours"][7]["value"] == 42.0


def test_openapi_schema_lists_endpoints(client):
    schema = client.get("/openapi.json").json()
    assert "/api/v1/readings/batch" in schema["paths"]
    assert "/api/v1/stations/{station_id}/daily" in schema["paths"]


def test_same_revision_different_content_rejected(client):
    post(client, [rec(1, 10.0, revision=1)])
    res = post(client, [rec(1, 10.5, revision=1)])
    assert res.status_code == 409
    detail = res.json()
    assert detail["code"] == "revision_conflict"
    assert detail["rolled_back"] is True
    assert detail["conflicts"][0]["hour"] == 1
    assert detail["conflicts"][0]["existing"] == {"value": 10.0, "retracted": False}
    assert detail["conflicts"][0]["submitted"] == {"value": 10.5, "retracted": False}
    # 原值不变
    assert daily(client).json()["hours"][1]["value"] == 10.0


def test_conflicting_batch_rolls_back_all_records(client):
    post(client, [rec(0, 1.0, revision=1)])
    # 批次里前两条合法、第三条冲突 → 整批不得落地
    res = post(
        client,
        [
            rec(2, 20.0, revision=1),
            rec(3, 30.0, revision=1),
            rec(0, 999.0, revision=1),
        ],
    )
    assert res.status_code == 409
    body = daily(client).json()
    assert body["hours"][0]["value"] == 1.0
    assert body["hours"][2]["state"] == "missing"
    assert body["hours"][3]["state"] == "missing"
    assert body["valid_hours"] == 1


def test_duplicate_key_in_same_batch_conflicts(client):
    res = post(client, [rec(4, 10.0, revision=1), rec(4, 11.0, revision=1)])
    assert res.status_code == 409
    assert daily(client).json()["hours"][4]["state"] == "missing"


def test_duplicate_identical_key_in_same_batch_ok(client):
    res = post(client, [rec(4, 10.0, revision=1), rec(4, 10.0, revision=1)])
    assert res.status_code == 200
    assert res.json()["created"] == 1
    assert res.json()["unchanged"] == 1


# ---------------------------------------------------------------- 撤回
def test_retraction_recomputes_daily(client):
    full_day(client, 10.0)
    res = post(client, [rec(7, revision=2, retracted=True)])
    assert res.status_code == 200
    body = daily(client).json()
    assert body["hours"][7]["state"] == "retracted"
    assert body["hours"][7]["value"] is None
    assert body["valid_hours"] == 23
    assert body["retracted_hours"] == [7]
    assert body["missing_hours"] == [7]
    assert body["valid_hours"] + len(body["missing_hours"]) == 24
    assert body["daily_mean"] == 10.0


def test_retraction_can_drop_below_threshold(client):
    post(client, [rec(h, 10.0) for h in range(20)])
    post(client, [rec(19, revision=2, retracted=True)])
    body = daily(client).json()
    assert body["status"] == "insufficient_valid_hours"
    assert body["daily_mean"] is None
    assert body["valid_hours"] == 19
    assert body["retracted_hours"] == [19]
    assert body["missing_hours"] == [19, 20, 21, 22, 23]


def test_reinstating_after_retraction_uses_new_revision(client):
    full_day(client, 10.0)
    post(client, [rec(7, revision=2, retracted=True)])
    post(client, [rec(7, 42.0, revision=3)])
    body = daily(client).json()
    assert body["hours"][7]["state"] == "valid"
    assert body["hours"][7]["value"] == 42.0
    assert body["retracted_hours"] == []


def test_retract_then_same_revision_value_conflicts(client):
    post(client, [rec(7, 10.0, revision=2)])
    res = post(client, [rec(7, revision=2, retracted=True)])
    assert res.status_code == 409


# ---------------------------------------------------------------- 校验
@pytest.mark.parametrize("hour", [-1, 24, 99])
def test_hour_out_of_range_rejected(client, hour):
    assert post(client, [rec(hour, 10.0)]).status_code == 422


def test_revision_zero_rejected(client):
    assert post(client, [rec(1, 10.0, revision=0)]).status_code == 422


def test_negative_value_rejected(client):
    assert post(client, [rec(1, -1.0)]).status_code == 422


@pytest.mark.parametrize("literal", ["1e400", "NaN", "Infinity"])
def test_non_finite_value_rejected_with_serializable_error(client, literal):
    """校验失败的错误详情本身也必须是合法 JSON（不能因 inf 而 500）。"""
    payload = (
        '{"records":[{"station_id":"S1","date":"%s","hour":1,"revision":1,"value":%s}]}'
        % (DAY, literal)
    )
    res = client.post(
        "/api/v1/readings/batch",
        content=payload,
        headers={"Content-Type": "application/json"},
    )
    assert res.status_code == 422, res.text
    assert res.json()["code"] == "validation_error"


def test_retracted_with_value_rejected(client):
    batch = [rec(1, retracted=True)]
    batch[0]["value"] = 10.0  # 撤回记录不允许携带浓度
    assert post(client, batch).status_code == 422


def test_non_retracted_without_value_rejected(client):
    assert post(client, [rec(1, None)]).status_code == 422


def test_empty_station_rejected(client):
    assert post(client, [rec(1, 10.0, station="   ")]).status_code == 422


def test_empty_batch_rejected(client):
    assert post(client, []).status_code == 422


def test_unknown_field_rejected(client):
    batch = [rec(1, 10.0)]
    batch[0]["extra"] = "x"
    assert post(client, batch).status_code == 422


@pytest.mark.parametrize(
    "patch",
    [
        {"hour": True},
        {"hour": "3"},
        {"revision": "2"},
        {"value": "10.5"},
        {"value": True},
        {"retracted": 1},
        {"retracted": "false"},
    ],
)
def test_cross_type_coercion_rejected(client, patch):
    """JSON 类型写错（字符串/布尔冒充数字）必须报错，而不是被悄悄转换。"""
    batch = [rec(1, 10.0)]
    batch[0].update(patch)
    assert post(client, batch).status_code == 422


@pytest.mark.parametrize("bad_date", ["2025-03-01T10:00:00", "2025-03-01T00:00:00+08:00"])
def test_date_with_time_component_rejected(client, bad_date):
    batch = [rec(1, 10.0)]
    batch[0]["date"] = bad_date
    assert post(client, batch).status_code == 422


def test_invalid_date_rejected(client):
    batch = [rec(1, 10.0)]
    batch[0]["date"] = "2025-02-30"
    assert post(client, batch).status_code == 422


# ---------------------------------------------------------------- 隔离与持久化
def test_stations_are_isolated(client):
    full_day(client, 10.0, station="A")
    full_day(client, 20.0, station="B")
    assert daily(client, "A").json()["daily_mean"] == 10.0
    assert daily(client, "B").json()["daily_mean"] == 20.0


def test_dates_are_isolated(client):
    full_day(client, 10.0, date="2025-03-01")
    full_day(client, 20.0, date="2025-03-02")
    assert daily(client, date="2025-03-01").json()["daily_mean"] == 10.0
    assert daily(client, date="2025-03-02").json()["daily_mean"] == 20.0


def test_unknown_station_returns_all_missing(client):
    body = daily(client, "NOPE").json()
    assert body["valid_hours"] == 0
    assert body["daily_mean"] is None
    assert body["missing_hours"] == list(range(24))


def test_data_survives_restart(tmp_path):
    db_path = str(tmp_path / "persist.db")
    app1 = create_app(Settings(database_path=db_path))
    with TestClient(app1) as c1:
        full_day(c1, 10.0)

    app2 = create_app(Settings(database_path=db_path))
    with TestClient(app2) as c2:
        body = daily(c2).json()
        assert body["daily_mean"] == 10.0
        assert body["valid_hours"] == 24


# ---------------------------------------------------------------- 并发
def test_concurrent_same_revision_conflicting_contents_one_wins(client):
    """并发提交同号不同内容：只有一个成功，另一个 409，最终值对应成功者。"""
    barrier = threading.Barrier(2)
    results = []

    def submit(value):
        barrier.wait()
        results.append(post(client, [rec(6, value, revision=1)]))

    threads = [threading.Thread(target=submit, args=(v,)) for v in (10.0, 20.0)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    statuses = sorted(r.status_code for r in results)
    assert statuses == [200, 409]
    winner = next(r for r in results if r.status_code == 200)
    assert daily(client).json()["hours"][6]["value"] in (10.0, 20.0)
    assert daily(client).json()["hours"][6]["state"] == "valid"
    assert winner.status_code == 200


def test_concurrent_revisions_latest_wins(client):
    """并发写入不同修订号：无论提交顺序，查询结果始终取最大修订号。"""
    barrier = threading.Barrier(8)
    threads = []

    def submit(rev):
        barrier.wait()
        post(client, [rec(9, float(rev * 10), revision=rev)])

    for rev in range(1, 9):
        threads.append(threading.Thread(target=submit, args=(rev,)))
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    body = daily(client).json()
    assert body["hours"][9]["value"] == 80.0
    assert body["hours"][9]["revision"] == 8


def test_concurrent_conflicting_batches_never_leave_partial_writes(client):
    """两个批次各含冲突项：全部失败时数据库不应出现任一批次的非冲突记录。"""

    def attempt(offset):
        try:
            post(client, [rec(0, 1.0, revision=1)])  # 建立基线
            post(
                client,
                [
                    rec(10 + offset, 5.0, revision=1),
                    rec(0, float(100 + offset), revision=1),  # 与基线冲突
                ],
            )
        except Exception:
            pass

    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    body = daily(client).json()
    assert body["hours"][0]["value"] == 1.0
    for hour in range(10, 14):
        assert body["hours"][hour]["state"] == "missing"
