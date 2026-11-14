#!/usr/bin/env python3
"""可运行的 API 调用示例（仅用标准库）。

用法：
    python3 examples/demo.py                      # 默认 http://localhost:8000
    python3 examples/demo.py http://localhost:9000

它会：补报 20 个有效小时 → 查询日均 → 撤回 1 小时 → 再次查询（跌破门槛）→
用更大修订号重新报回 → 再次查询（恢复日均）→
查询连续超阈值事件 → 确认事件 → 改写事件成员后再用旧确认（应 409）。
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000").rstrip("/")
STATION = "DEMO-001"
DATE = "2025-03-01"


def post_batch(records: list[dict]) -> tuple[int, dict]:
    req = urllib.request.Request(
        f"{BASE}/api/v1/readings/batch",
        data=json.dumps({"records": records}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.load(resp)
    except urllib.error.HTTPError as exc:
        return exc.code, json.load(exc)


def get_daily() -> dict:
    url = f"{BASE}/api/v1/stations/{STATION}/daily?date={DATE}"
    with urllib.request.urlopen(url, timeout=15) as resp:
        return json.load(resp)


def summarize(label: str) -> None:
    report = get_daily()
    print(
        f"{label}: status={report['status']} valid={report['valid_hours']} "
        f"mean={report['daily_mean']} missing={report['missing_hours']} "
        f"retracted={report['retracted_hours']}"
    )


def get_events(threshold: float) -> dict:
    url = (
        f"{BASE}/api/v1/stations/{STATION}/events"
        f"?start_date={DATE}&end_date={DATE}&threshold={threshold}"
    )
    with urllib.request.urlopen(url, timeout=15) as resp:
        return json.load(resp)


def confirm_event(event: dict, threshold: float) -> tuple[int, dict]:
    payload = {
        "station_id": STATION,
        "start_date": DATE,
        "end_date": DATE,
        "threshold": threshold,
    }
    # 把查询里看到的事件对象整个回传即可，服务端会按提交时刻的数据复核。
    payload["event"] = event
    req = urllib.request.Request(
        f"{BASE}/api/v1/stations/{STATION}/events/confirm",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.load(resp)
    except urllib.error.HTTPError as exc:
        return exc.code, json.load(exc)


def main() -> None:
    readings = [
        {"station_id": STATION, "date": DATE, "hour": h, "revision": 1, "value": 40.0 + h}
        for h in range(20)
    ]
    status, body = post_batch(readings)
    print(f"补报 20 小时 -> HTTP {status} {body}")

    status, body = post_batch([dict(readings[0])])  # 幂等重试
    print(f"重复提交同号同内容 -> HTTP {status} {body}")

    summarize("初始日报")

    status, body = post_batch(
        [{"station_id": STATION, "date": DATE, "hour": 0, "revision": 2, "retracted": True}]
    )
    print(f"撤回 0 点 -> HTTP {status} {body}")
    summarize("撤回后")

    status, body = post_batch(
        [{"station_id": STATION, "date": DATE, "hour": 0, "revision": 3, "value": 66.6}]
    )
    print(f"以修订号 3 补回 -> HTTP {status} {body}")
    summarize("补回后")

    status, body = post_batch(
        [{"station_id": STATION, "date": DATE, "hour": 1, "revision": 1, "value": 0.0}]
    )
    print(f"同号改内容 -> HTTP {status} code={body.get('code')} rolled_back={body.get('rolled_back')}")
    summarize("冲突被拒后（应保持不变）")

    # -- 超阈值事件：42..59 点中 50 点以上的连续段 ----------------------
    threshold = 50.0
    events = get_events(threshold)["events"]
    print(f"超阈值事件 -> {len(events)} 个")
    for event in events:
        print(
            f"  {event['start_time']} ~ {event['end_time']} "
            f"{event['duration_hours']}h peak={event['peak_value']}"
        )
    if not events:
        return

    status, body = confirm_event(events[0], threshold)
    print(
        f"确认事件 -> HTTP {status} "
        f"confirmation={body.get('confirmation_id')} idempotent={body.get('idempotent')}"
    )
    before = get_events(threshold)["events"][0]
    print(f"确认后查询 confirmed={before['confirmed']}")

    # 修订事件中的一小时 → 内容变化，旧确认不能再落到新事件
    member = before["hours"][0]
    status, body = post_batch(
        [
            {
                "station_id": STATION,
                "date": member["date"],
                "hour": member["hour"],
                "revision": member["revision"] + 1,
                "value": float(member["value"]) + 5.0,
            }
        ]
    )
    print(f"改写事件成员 -> HTTP {status}")
    after = get_events(threshold)["events"]
    print(f"改写后 confirmed={after[0]['confirmed'] if after else None}")
    status, body = confirm_event(events[0], threshold)
    print(f"用旧确认再提交 -> HTTP {status} code={body.get('code')}")


if __name__ == "__main__":
    main()
