from __future__ import annotations

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


def query(client, start, end, threshold, station="S1"):
    return client.get(
        f"/api/v1/stations/{station}/events",
        params={"start_date": start, "end_date": end, "threshold": threshold},
    )


def confirm(client, start, end, threshold, station="S1", **extra):
    body = {"start_date": start, "end_date": end, "threshold": threshold, **extra}
    return client.post(f"/api/v1/stations/{station}/events/confirm", json=body)


# ---------------------------------------------------------------- 基本事件识别
def test_no_events_when_all_below_threshold(client):
    post(client, [rec(h, 50.0) for h in range(24)])
    res = query(client, DAY, DAY, 75)
    assert res.status_code == 200
    assert res.json()["events"] == []


def test_event_requires_three_consecutive_hours(client):
    post(client, [rec(5, 90.0), rec(6, 92.0), rec(7, 88.0)])
    body = query(client, DAY, DAY, 75).json()
    assert body["event_count"] == 1
    event = body["events"][0]
    assert event["duration_hours"] == 3
    assert event["start_time"] == "2025-03-01T05:00:00+08:00"
    assert event["end_time"] == "2025-03-01T07:00:00+08:00"
    assert event["peak_value"] == 92.0
    assert event["peak_time"] == "2025-03-01T06:00:00+08:00"
    assert [h["revision"] for h in event["hours"]] == [1, 1, 1]
    assert event["confirmed"] is False


def test_two_hours_do_not_form_event(client):
    post(client, [rec(5, 90.0), rec(6, 92.0)])
    assert query(client, DAY, DAY, 75).json()["events"] == []


def test_long_run_is_single_event(client):
    post(client, [rec(h, 80.0) for h in range(4, 12)])
    body = query(client, DAY, DAY, 75).json()
    assert len(body["events"]) == 1
    assert body["events"][0]["duration_hours"] == 8


def test_hour_equal_to_threshold_is_not_exceeding(client):
    post(client, [rec(h, 75.0) for h in range(24)])
    assert query(client, DAY, DAY, 75).json()["events"] == []


# ---------------------------------------------------------------- 修订与截断
def test_highest_revision_only(client):
    """低修订即使后到也不影响事件；高修订会替换事件成员。"""
    post(client, [rec(h, 90.0, revision=1) for h in range(3, 6)])
    post(client, [rec(4, 20.0, revision=2)])  # 中间小时下调
    body = query(client, DAY, DAY, 75).json()
    assert body["events"] == []


def test_late_old_revision_does_not_split_event(client):
    post(client, [rec(h, 90.0, revision=3) for h in range(3, 6)])
    post(client, [rec(4, 10.0, revision=2)])  # 更早的修订，应被忽略
    body = query(client, DAY, DAY, 75).json()
    assert body["events"][0]["duration_hours"] == 3
    assert [h["revision"] for h in body["events"][0]["hours"]] == [3, 3, 3]


def test_missing_hour_truncates_event(client):
    post(client, [rec(h, 90.0) for h in range(0, 3)] + [rec(h, 90.0) for h in range(5, 8)])
    body = query(client, DAY, DAY, 75).json()
    assert body["event_count"] == 2
    assert body["events"][0]["end_time"] == "2025-03-01T02:00:00+08:00"
    assert body["events"][1]["start_time"] == "2025-03-01T05:00:00+08:00"


def test_retraction_truncates_event(client):
    post(client, [rec(h, 90.0) for h in range(4)])  # 0-3
    post(client, [rec(2, revision=2, retracted=True)])
    body = query(client, DAY, DAY, 75).json()
    assert body["events"] == []  # 0-1 与 3 各只剩不足 3 小时


def test_retraction_changes_event_boundary(client):
    post(client, [rec(h, 90.0) for h in range(5)])  # 0-4，一个 5 小时事件
    post(client, [rec(0, revision=2, retracted=True)])
    body = query(client, DAY, DAY, 75).json()
    assert body["event_count"] == 1
    assert body["events"][0]["start_time"] == "2025-03-01T01:00:00+08:00"
    assert body["events"][0]["duration_hours"] == 4


# ---------------------------------------------------------------- 跨午夜与区间
def test_event_continues_across_midnight(client):
    post(client, [rec(23, 90.0, date="2025-03-01"), rec(0, 91.0, date="2025-03-02"),
                  rec(1, 92.0, date="2025-03-02")])
    body = query(client, "2025-03-01", "2025-03-02", 75).json()
    assert body["event_count"] == 1
    event = body["events"][0]
    assert event["duration_hours"] == 3
    assert event["start_time"] == "2025-03-01T23:00:00+08:00"
    assert event["end_time"] == "2025-03-02T01:00:00+08:00"
    assert [h["date"] for h in event["hours"]] == ["2025-03-01", "2025-03-02", "2025-03-02"]


def test_cross_midnight_event_truncated_at_query_boundary(client):
    """区间边界之外的小时不参与，事件在查询区间端点处截断。"""
    post(client, [rec(23, 90.0, date="2025-02-28"), rec(0, 91.0, date="2025-03-01"),
                  rec(1, 92.0, date="2025-03-01")])
    body = query(client, "2025-03-01", "2025-03-01", 75).json()
    assert body["events"] == []


@pytest.mark.parametrize("start,end", [("2025-03-01", "2025-03-01"),
                                       ("2025-03-01", "2025-03-03")])
def test_range_inclusive_both_ends(client, start, end):
    res = query(client, start, end, 75)
    assert res.status_code == 200


def test_range_longer_than_three_days_rejected(client):
    assert query(client, "2025-03-01", "2025-03-04", 75).status_code == 422


def test_end_before_start_rejected(client):
    assert query(client, "2025-03-03", "2025-03-01", 75).status_code == 422


@pytest.mark.parametrize("bad", ["2025-03-01T00:00:00", "2025-3-1", "20250301", ""])
def test_bad_date_format_rejected(client, bad):
    assert query(client, bad, "2025-03-01", 75).status_code == 422


def test_bad_threshold_rejected(client):
    res = query(client, DAY, DAY, "abc")
    assert res.status_code == 422
    assert res.json()["code"] == "validation_error"


def test_threshold_must_be_finite(client):
    assert query(client, DAY, DAY, "inf").status_code == 422
    assert query(client, DAY, DAY, "nan").status_code == 422


# ---------------------------------------------------------------- 确认
def test_confirm_then_query_marks_confirmed(client):
    post(client, [rec(h, 90.0) for h in range(3)])
    event_id = query(client, DAY, DAY, 75).json()["events"][0]["event_id"]
    res = confirm(client, DAY, DAY, 75, event_id=event_id)
    assert res.status_code == 200, res.text
    conf = res.json()["confirmations"][0]
    assert conf["event_id"] == event_id
    assert conf["event_start"] == "2025-03-01T00:00:00+08:00"
    marked = query(client, DAY, DAY, 75).json()["events"][0]
    assert marked["confirmed"] is True
    assert marked["confirmation_id"] == conf["confirmation_id"]


def test_confirm_is_idempotent(client):
    post(client, [rec(h, 90.0) for h in range(3)])
    event_id = query(client, DAY, DAY, 75).json()["events"][0]["event_id"]
    first = confirm(client, DAY, DAY, 75, event_id=event_id).json()
    second = confirm(client, DAY, DAY, 75, event_id=event_id).json()
    assert first["confirmation_id"] == second["confirmation_id"]
    assert second["idempotent"] is True


def test_confirm_unknown_event_rejected(client):
    post(client, [rec(h, 90.0) for h in range(3)])
    res = confirm(client, DAY, DAY, 75, event_id="deadbeef")
    assert res.status_code == 409
    assert res.json()["code"] == "stale_confirmation"
    assert client.get("/api/v1/stations/S1/confirmations").json()["confirmations"] == []


def test_confirm_binds_range_and_threshold(client):
    post(client, [rec(h, 90.0) for h in range(3)])
    event_id = query(client, DAY, DAY, 75).json()["events"][0]["event_id"]
    # 阈值不同 → 不是同一个查询
    res = confirm(client, DAY, DAY, 80, event_id=event_id)
    assert res.status_code == 409


def test_confirm_binds_query_range(client):
    post(client, [rec(h, 90.0) for h in range(3)])
    event_id = query(client, DAY, DAY, 75).json()["events"][0]["event_id"]
    res = confirm(client, DAY, "2025-03-02", 75, event_id=event_id)
    # 区间不同：虽然该事件仍在该区间内，但确认绑定的是原查询；
    # 服务端以新区间重新计算事件，event_id 仍匹配，因此允许——但记录的是新查询。
    assert res.status_code == 200
    assert res.json()["start_date"] == DAY
    assert res.json()["end_date"] == "2025-03-02"


def test_confirm_with_start_end_times(client):
    post(client, [rec(23, 90.0, date="2025-03-01"), rec(0, 91.0, date="2025-03-02"),
                  rec(1, 92.0, date="2025-03-02")])
    res = confirm(
        client,
        "2025-03-01",
        "2025-03-02",
        75,
        event_start="2025-03-01T23:00:00+08:00",
        event_end="2025-03-02T01:00:00+08:00",
    )
    assert res.status_code == 200, res.text
    assert res.json()["duration_hours"] == 3


def test_confirm_by_echoing_event_object(client):
    """把查询响应中的事件对象原样回传即可确认。"""
    post(client, [rec(h, 90.0) for h in range(3)])
    event = query(client, DAY, DAY, 75).json()["events"][0]
    res = confirm(client, DAY, DAY, 75, event=event)
    assert res.status_code == 200, res.text
    assert res.json()["event_id"] == event["event_id"]


def test_confirm_with_stale_hours_snapshot_rejected(client):
    post(client, [rec(h, 90.0) for h in range(3)])
    event = query(client, DAY, DAY, 75).json()["events"][0]
    post(client, [rec(1, 99.0, revision=2)])
    res = confirm(client, DAY, DAY, 75, event_id=event["event_id"], hours=event["hours"])
    assert res.status_code == 409


def test_confirm_with_matching_hours_snapshot_ok(client):
    post(client, [rec(h, 90.0) for h in range(3)])
    event = query(client, DAY, DAY, 75).json()["events"][0]
    res = confirm(client, DAY, DAY, 75, event_id=event["event_id"], hours=event["hours"])
    assert res.status_code == 200, res.text


def test_confirm_cross_midnight_event(client):
    post(client, [rec(23, 90.0, date="2025-03-01"), rec(0, 91.0, date="2025-03-02"),
                  rec(1, 92.0, date="2025-03-02")])
    event = query(client, "2025-03-01", "2025-03-02", 75).json()["events"][0]
    res = confirm(client, "2025-03-01", "2025-03-02", 75, event_id=event["event_id"])
    assert res.status_code == 200
    # 撤回其中跨午夜的一小时 → 两个残段都不足 3 小时，事件消失，旧确认不再成立
    post(client, [rec(0, revision=2, retracted=True, date="2025-03-02")])
    assert query(client, "2025-03-01", "2025-03-02", 75).json()["events"] == []
    history = client.get("/api/v1/stations/S1/confirmations").json()["confirmations"]
    assert history[0]["current"] is False


def test_confirm_all_events_when_identity_omitted(client):
    post(client, [rec(h, 90.0) for h in range(0, 3)] + [rec(h, 90.0) for h in range(6, 9)])
    res = confirm(client, DAY, DAY, 75)
    assert res.status_code == 200
    assert len(res.json()["confirmations"]) == 2
    assert query(client, DAY, DAY, 75).json()["event_count"] == 2
    assert all(e["confirmed"] for e in query(client, DAY, DAY, 75).json()["events"])


def test_confirm_multiple_event_ids_partial_stale_rejected(client):
    post(client, [rec(h, 90.0) for h in range(0, 3)] + [rec(h, 90.0) for h in range(6, 9)])
    ids = [e["event_id"] for e in query(client, DAY, DAY, 75).json()["events"]]
    # 改写第一个事件，第二个不受影响
    post(client, [rec(1, 10.0, revision=2)])
    res = confirm(client, DAY, DAY, 75, event_ids=ids)
    assert res.status_code == 409
    # 整批拒绝：第二个事件未被顺手确认
    assert query(client, DAY, DAY, 75).json()["events"][0]["confirmed"] is False


# ---------------------------------------------------------------- 确认失效
def test_revision_changes_event_invalidates_confirmation(client):
    post(client, [rec(h, 90.0) for h in range(3)])
    event_id = query(client, DAY, DAY, 75).json()["events"][0]["event_id"]
    assert confirm(client, DAY, DAY, 75, event_id=event_id).status_code == 200
    # 修订改写事件内容（峰值变化）
    post(client, [rec(1, 99.0, revision=2)])
    new = query(client, DAY, DAY, 75).json()
    assert new["events"][0]["event_id"] != event_id
    assert new["events"][0]["confirmed"] is False
    # 旧确认仍在历史中，但标记为已不再成立
    history = client.get("/api/v1/stations/S1/confirmations").json()["confirmations"]
    assert history[0]["current"] is False
    # 用旧 event_id 再确认 → 409
    assert confirm(client, DAY, DAY, 75, event_id=event_id).status_code == 409


def test_boundary_change_invalidates_confirmation(client):
    post(client, [rec(h, 90.0) for h in range(5)])  # 0-4
    event_id = query(client, DAY, DAY, 75).json()["events"][0]["event_id"]
    assert confirm(client, DAY, DAY, 75, event_id=event_id).status_code == 200
    post(client, [rec(0, revision=2, retracted=True)])  # 边界收缩为 1-4
    events = query(client, DAY, DAY, 75).json()["events"]
    assert events[0]["event_id"] != event_id
    assert events[0]["confirmed"] is False


def test_backfill_extends_event_invalidates_confirmation(client):
    post(client, [rec(h, 90.0) for h in range(3)])
    event_id = query(client, DAY, DAY, 75).json()["events"][0]["event_id"]
    assert confirm(client, DAY, DAY, 75, event_id=event_id).status_code == 200
    post(client, [rec(3, 90.0, revision=1)])  # 补报延伸边界
    events = query(client, DAY, DAY, 75).json()["events"]
    assert events[0]["duration_hours"] == 4
    assert events[0]["event_id"] != event_id
    assert events[0]["confirmed"] is False


def test_unaffected_event_confirmation_survives(client):
    post(client, [rec(h, 90.0) for h in range(0, 3)] + [rec(h, 90.0) for h in range(6, 9)])
    events = query(client, DAY, DAY, 75).json()["events"]
    assert confirm(client, DAY, DAY, 75, event_id=events[1]["event_id"]).status_code == 200
    post(client, [rec(1, 10.0, revision=2)])  # 只改写第一个事件
    after = query(client, DAY, DAY, 75).json()["events"]
    assert [e["event_id"] for e in after] == [events[1]["event_id"]]
    assert after[0]["confirmed"] is True


# ---------------------------------------------------------------- 并发
def test_stale_confirmation_does_not_change_state(client):
    post(client, [rec(h, 90.0) for h in range(3)])
    event_id = query(client, DAY, DAY, 75).json()["events"][0]["event_id"]
    assert confirm(client, DAY, DAY, 75, event_id=event_id).status_code == 200
    post(client, [rec(2, 80.0, revision=2)])
    res = confirm(client, DAY, DAY, 75, event_id=event_id)
    assert res.status_code == 409
    # 已有确认保持不变，也没有新增确认
    history = client.get("/api/v1/stations/S1/confirmations").json()["confirmations"]
    assert len(history) == 1
    assert history[0]["event_id"] == event_id


def test_concurrent_write_and_confirm_ordered(client):
    """写入与确认并发：最终确认状态必须与最终事件内容一致。"""
    post(client, [rec(h, 90.0) for h in range(3)])
    event_id = query(client, DAY, DAY, 75).json()["events"][0]["event_id"]
    barrier = threading.Barrier(2)
    results = {}

    def do_write():
        barrier.wait()
        results["write"] = post(client, [rec(1, 100.0, revision=2)]).status_code

    def do_confirm():
        barrier.wait()
        results["confirm"] = confirm(client, DAY, DAY, 75, event_id=event_id).status_code

    threads = [threading.Thread(target=do_write), threading.Thread(target=do_confirm)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    final = query(client, DAY, DAY, 75).json()["events"][0]
    if results["confirm"] == 200:
        # 确认先提交：随后写入改写了事件 → 确认不再匹配新事件
        assert final["confirmed"] is False
    else:
        assert results["confirm"] == 409
        assert final["confirmed"] is False


def test_concurrent_confirm_same_event_idempotent(client):
    post(client, [rec(h, 90.0) for h in range(3)])
    event_id = query(client, DAY, DAY, 75).json()["events"][0]["event_id"]
    barrier = threading.Barrier(4)
    ids = []

    def do_confirm():
        barrier.wait()
        res = confirm(client, DAY, DAY, 75, event_id=event_id)
        if res.status_code == 200:
            ids.append(res.json()["confirmation_id"])

    threads = [threading.Thread(target=do_confirm) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # 唯一约束 + 事务内查重：并发重复确认只会留下一条
    assert len(set(ids)) == 1
    history = client.get("/api/v1/stations/S1/confirmations").json()["confirmations"]
    assert len(history) == 1


# ---------------------------------------------------------------- 持久化与契约
def test_confirmations_survive_restart(tmp_path):
    db_path = str(tmp_path / "persist.db")
    app1 = create_app(Settings(database_path=db_path))
    with TestClient(app1) as c1:
        post(c1, [rec(h, 90.0) for h in range(3)])
        event_id = query(c1, DAY, DAY, 75).json()["events"][0]["event_id"]
        assert confirm(c1, DAY, DAY, 75, event_id=event_id).status_code == 200

    app2 = create_app(Settings(database_path=db_path))
    with TestClient(app2) as c2:
        assert query(c2, DAY, DAY, 75).json()["events"][0]["confirmed"] is True


def test_openapi_lists_event_endpoints(client):
    paths = client.get("/openapi.json").json()["paths"]
    assert "/api/v1/stations/{station_id}/events" in paths
    assert "/api/v1/stations/{station_id}/events/confirm" in paths


def test_events_isolated_by_station(client):
    post(client, [rec(h, 90.0, station="A") for h in range(3)])
    assert query(client, DAY, DAY, 75, station="B").json()["events"] == []
    assert query(client, DAY, DAY, 99, station="A").json()["events"] == []
    assert query(client, DAY, DAY, 75, station="A").json()["event_count"] == 1


def test_post_query_form_matches_get(client):
    post(client, [rec(h, 90.0) for h in range(3)])
    get_body = query(client, DAY, DAY, 75).json()
    post_body = client.post(
        "/api/v1/stations/S1/events/query",
        json={"start_date": DAY, "end_date": DAY, "threshold": 75},
    ).json()
    assert get_body["events"] == post_body["events"]


# ---------------------------------------------------------------- 现场演练
def test_handover_workflow_locate_then_confirm(client):
    """交接班：查询定位污染过程 → 确认 → 交接后补报使另一事件消失。"""
    post(
        client,
        [rec(h, 90.0) for h in range(2, 5)] + [rec(h, 88.0) for h in range(20, 23)],
    )
    found = query(client, DAY, DAY, 75).json()
    assert [e["duration_hours"] for e in found["events"]] == [3, 3]

    for event in found["events"]:
        res = confirm(client, DAY, DAY, 75, event=event)
        assert res.status_code == 200, res.text

    # 补报把第二个事件向午夜延伸 → 边界改变
    post(client, [rec(23, 90.0)])
    after = query(client, DAY, DAY, 75).json()
    assert after["events"][0]["confirmed"] is True  # 未受影响
    assert after["events"][1]["confirmed"] is False  # 边界已变
    assert len(after["events"][1]["hours"]) == 4

    history = {
        item["event_id"]: item["current"]
        for item in client.get("/api/v1/stations/S1/confirmations").json()["confirmations"]
    }
    assert history[found["events"][0]["event_id"]] is True
    assert history[found["events"][1]["event_id"]] is False
