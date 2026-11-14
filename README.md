# PM2.5 站点日报与超阈值事件 API

站点设备 PM2.5 小时值的**补报 / 校准修订 / 撤回**接收服务，按北京时间日期计算日均值，
并支持连续超阈值事件的查询与交接确认。
FastAPI + SQLite，单容器 Docker Compose 启动。

## 业务规则

| 规则 | 实现 |
| --- | --- |
| 每小时以最高 `revision` 为准 | 修订行只追加不覆盖，查询取 `MAX(revision)` |
| 同号同内容重试无副作用 | 幂等，返回 `200` 且 `unchanged` 计数 +1 |
| 同号不同内容拒绝整批 | 返回 `409`，同一事务内回滚，不留下任何部分写入 |
| 日均值门槛 | 有效小时 ≥ 20 才算日均（有效小时数为除数），否则 `daily_mean=null` 并返回缺失小时 |
| 修订/撤回立即生效 | 无缓存，查询直接读最新修订 |
| 并发不倒退 | 读取按最大修订号，旧修订后到也无法覆盖新值；写入用 `BEGIN IMMEDIATE` 串行化 |
| 连续超阈值事件 | 最高修订的有效浓度严格超阈值且连续 ≥ 3 小时；缺测/撤回截断，跨午夜连续 |
| 事件确认绑定查询 | 确认绑定站点、查询区间、阈值和事件内容指纹；事件被改写后旧确认不落到新事件 |

「有效小时」= 最新修订是一条带浓度值的非撤回记录。最新修订为撤回的小时与从未收到数据的小时一样
不计入分母，都会出现在 `missing_hours` 中；`retracted_hours` 是 `missing_hours` 的子集，
用于区分「撤回」和「未报」。因此恒有 `valid_hours + len(missing_hours) == 24`。

时间语义：请求中的 `date` + `hour` 直接表示**北京时间（Asia/Shanghai）**的观测日期和整点小时，
服务不做时区换算，也不会跨日聚合；同一天的所有小时归到同一份日报。

## 启动

```bash
# Docker Compose（推荐）
docker compose up --build -d
curl http://localhost:8000/healthz

# 停止（数据保留在命名卷 pm25-data 中）
docker compose down
# 连数据一起清除
docker compose down -v
```

端口可通过环境变量覆盖：`API_PORT=9000 docker compose up -d --build`。
SQLite 文件位于容器内 `/data/pm25.db`，由命名卷持久化。

本地开发（不装 Docker）：

```bash
python3 -m venv --without-pip .venv && curl -fsSL https://bootstrap.pypa.io/get-pip.py | .venv/bin/python
.venv/bin/python -m pip install -r requirements.txt
DATABASE_PATH=data/pm25.db .venv/bin/python -m uvicorn app.main:app --reload
```

运行测试：

```bash
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

## API

交互式文档：启动后访问 `http://localhost:8000/docs`（OpenAPI JSON：`/openapi.json`）。

### 1. 批量提交小时值

`POST /api/v1/readings/batch`

```bash
curl -X POST http://localhost:8000/api/v1/readings/batch \
  -H 'Content-Type: application/json' \
  -d '{
    "records": [
      {"station_id": "S001", "date": "2025-03-01", "hour": 0, "revision": 1, "value": 35.2},
      {"station_id": "S001", "date": "2025-03-01", "hour": 1, "revision": 1, "value": 41.0},
      {"station_id": "S001", "date": "2025-03-01", "hour": 2, "revision": 2, "retracted": true},
      {"station_id": "S002", "date": "2025-03-01", "hour": 0, "revision": 1, "value": 28.7}
    ]
  }'
```

字段说明：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `station_id` | string | 是 | 1–128 字符，首尾空白会去除，不允许全空白 |
| `date` | `YYYY-MM-DD` | 是 | 北京时间观测日期，不接受带时间部分的值 |
| `hour` | int | 是 | 0–23，必须是 JSON 数字（不接受 `"3"` 或 `true`） |
| `revision` | int | 是 | ≥ 1，同一站点/日期/小时下递增 |
| `value` | float | 非撤回时必填 | PM2.5 浓度 µg/m³，≥ 0 的有限数值 |
| `retracted` | bool | 否（默认 `false`） | `true` 表示撤回该小时，此时不得携带 `value` |

一次最多 1000 条，整批原子提交。成功响应：

```json
{"received": 4, "created": 4, "unchanged": 0, "stations": ["S001", "S002"], "dates": ["2025-03-01"]}
```

重复提交同号同内容会得到 `created: 0, unchanged: 1`，可安全重试。

冲突响应（`409`，整批未生效）：

```json
{
  "code": "revision_conflict",
  "message": "one or more revisions already exist with different content; batch rolled back",
  "conflicts": [
    {
      "station_id": "S001", "date": "2025-03-01", "hour": 1, "revision": 1,
      "existing": {"value": 41.0, "retracted": false},
      "submitted": {"value": 45.0, "retracted": false}
    }
  ],
  "rolled_back": true
}
```

要修正已提交的值，请用**更大的** `revision` 重新提交，而不是改动旧修订号。

### 2. 查询日报

`GET /api/v1/stations/{station_id}/daily?date=YYYY-MM-DD`

```bash
curl 'http://localhost:8000/api/v1/stations/S001/daily?date=2025-03-01'
```

```json
{
  "station_id": "S001",
  "date": "2025-03-01",
  "timezone": "Asia/Shanghai",
  "required_valid_hours": 20,
  "valid_hours": 23,
  "daily_mean": 35.87,
  "status": "ok",
  "missing_hours": [2],
  "retracted_hours": [2],
  "hours": [
    {"hour": 0, "state": "valid", "value": 35.2, "revision": 1},
    {"hour": 2, "state": "retracted", "value": null, "revision": 2}
  ]
}
```

- `state`：`valid` / `retracted` / `missing`
- `status`：有效小时 < 20 时为 `insufficient_valid_hours`，`daily_mean` 为 `null`
- `daily_mean`：有效小时值的算术平均，保留两位小数
- 查询不存在的站点或日期返回 24 个 `missing`，不报 404

### 3. 查询连续超阈值事件

`GET /api/v1/stations/{station_id}/events?start_date=…&end_date=…&threshold=…`

```bash
curl 'http://localhost:8000/api/v1/stations/S001/events?start_date=2025-06-01&end_date=2025-06-02&threshold=75'
```

等价形式：`GET /api/v1/events?station_id=S001&…`（站点在查询参数中）、
`GET /api/v1/events/query?station_id=S001&…`、以及
`POST /api/v1/stations/{station_id}/events/query`（条件放在 JSON 请求体）。
确认同理：`POST /api/v1/events/confirm`（站点在请求体）、
`POST /api/v1/stations/{station_id}/events/{event_id}/confirm`。

规则：

| 规则 | 说明 |
| --- | --- |
| 数据来源 | 每小时**最高 revision** 且有效（非撤回）的浓度 |
| 超阈值 | **严格大于** `threshold`；等于阈值不算 |
| 最短时长 | 连续 ≥ 3 小时才构成事件，不足 3 小时的残段丢弃 |
| 截断 | 缺测（从未上报）或最新修订为撤回的小时截断事件 |
| 跨午夜 | 事件按绝对时间连续，23 点后接次日 0 点仍是同一事件 |
| 查询区间 | 北京时间日期，含两端，最多 3 天；区间外的数据不参与计算 |

```json
{
  "station_id": "S001",
  "start_date": "2025-06-01", "end_date": "2025-06-02",
  "threshold": 75.0, "timezone": "Asia/Shanghai", "min_duration_hours": 3,
  "event_count": 1,
  "events": [
    {
      "event_id": "a54a518b…8ed5",
      "start_time": "2025-06-01T22:00:00+08:00",
      "end_time": "2025-06-02T00:00:00+08:00",
      "start_date": "2025-06-01", "end_date": "2025-06-02",
      "duration_hours": 3, "peak_value": 95.0, "peak_time": "2025-06-01T23:00:00+08:00",
      "peak_revision": 1, "min_value": 88.0, "revisions": [1, 1, 1],
      "hours": [
        {"time": "2025-06-01T22:00:00+08:00", "date": "2025-06-01", "hour": 22, "value": 90.0, "revision": 1},
        {"time": "2025-06-01T23:00:00+08:00", "date": "2025-06-01", "hour": 23, "value": 95.0, "revision": 1},
        {"time": "2025-06-02T00:00:00+08:00", "date": "2025-06-02", "hour": 0, "value": 88.0, "revision": 1}
      ],
      "confirmed": false, "confirmation_id": null
    }
  ]
}
```

- 起止时刻均为北京时间 ISO 8601（`+08:00`）；`peak_value` 取并列最大中的最早一个，
  `peak_revision` 是峰值小时的修订号；`min_value` 是构成小时中的最低浓度。
- `hours` 给出构成小时及其**修订号**（`revisions` 为同样内容的紧凑数组）——
  事件、峰值和修订号来自同一条 `SELECT`，因此一定属于同一份数据库状态。
- `event_id` 是事件内容指纹（站点 + 阈值 + 每个构成小时的时刻与浓度）。
- 查询不存在的站点返回 `event_count: 0`、`events: []`，不报 404。

### 4. 确认事件

`POST /api/v1/stations/{station_id}/events/confirm`

```bash
curl -X POST http://localhost:8000/api/v1/stations/S001/events/confirm \
  -H 'Content-Type: application/json' \
  -d '{
    "start_date": "2025-06-01", "end_date": "2025-06-02", "threshold": 75,
    "event_id": "a54a518b…8ed5"
  }'
```

请求必须同时携带**本次查询的区间与阈值**和**所见事件**，可选的事件标识写法：

| 字段 | 说明 |
| --- | --- |
| `event_id` | 查询响应中该事件的指纹（推荐） |
| `event_ids` | 一次确认多个事件，全部通过才落库 |
| `event_start` + `event_end` | 用事件起止时刻定位（`start_time` / `end_time` 亦可） |
| `event` | 把查询响应里的事件对象整个回传 |
| 全都不给 | 确认本次区间与阈值下的**全部**事件 |
| `hours` / `event.hours` | 可选：把所见的构成小时一并回传，服务端按同一口径复核 |

成功时返回 `200`，顶层是本次确认的字段，`confirmations` 列出本请求覆盖的事件：

```json
{
  "station_id": "S001",
  "start_date": "2025-06-01", "end_date": "2025-06-02", "threshold": 75.0,
  "confirmation_id": "4aedbbfe…ab53",
  "event_id": "a54a518b…8ed5",
  "event_start": "2025-06-01T22:00:00+08:00",
  "event_end": "2025-06-02T00:00:00+08:00",
  "duration_hours": 3, "peak_value": 95.0,
  "confirmed_at": "2025-06-01T12:34:56+00:00",
  "idempotent": false,
  "event": { "…": "确认时所见的事件快照" },
  "confirmations": ["…"]
}
```

同一区间、阈值、事件重复确认是幂等的（返回同一条确认，`idempotent: true`）。
`GET /api/v1/stations/{station_id}/confirmations` 列出历史确认，
`current` 表示该事件在当前数据下是否仍然成立。

**失效与并发**：

- 补报、修订或撤回只要改变了事件的内容（浓度）或边界（起止 / 时长 / 是否成立），
  指纹就会变化，旧确认**不会**落到新事件上：查询响应里 `confirmed` 为 `false`，
  再用旧 `event_id` 确认会返回 `409 stale_confirmation`。
- 未受影响的事件指纹不变，其确认继续有效（同一站点其它事件被改写不影响它）。
- 确认在 `BEGIN IMMEDIATE` 写事务内按**提交时刻的数据库状态**重新计算事件并校验，
  之后才插入确认；因此确认与写入并发时按提交顺序判定：
  若写入先提交，确认看到的是新事件，过期确认被拒；若确认先提交，则它确认的是写入前的事件。
- 过期确认返回 `409` 并整体回滚，**不会**删除或修改任何已有确认。

### 5. 可运行示例

```bash
python3 examples/demo.py http://localhost:8000
```

依次演示：补报 20 小时 → 幂等重试 → 撤回跌破门槛 → 用更大修订号补回 → 同号改内容被 409 拒绝
→ 查询超阈值事件 → 确认 → 改写事件成员后旧确认被拒（409）。

## 错误约定

| 状态码 | 含义 |
| --- | --- |
| `422` | 请求体/参数校验失败（小时越界、撤回带值、非撤回缺值、日期非法、区间超过 3 天、阈值非数值等） |
| `409` | 修订号冲突（整批已回滚）；或确认已过期（`code=stale_confirmation`，未改动任何状态） |
| `503` | 数据库繁忙（写锁等待超时），本次写入未生效，可重试 |

所有错误响应均为 `{"code": "...", "message": "..."}` 结构；`500` 时同样保证没有部分写入。

## 实现要点

- **表结构**：`pm25_readings(station_id, obs_date, hour, revision, ...)` 主键为四元组，
  每个修订独立成行；`CHECK` 约束保证撤回/浓度的互斥关系在数据库层也成立。
  `pm25_event_confirmations` 保存确认，`UNIQUE(station_id, start_date, end_date,
  threshold, event_id)` 保证重复确认不会重复落库。
- **原子性**：写事务用 `BEGIN IMMEDIATE` 先取写锁，再在同一事务里做冲突校验和插入，
  任何冲突都在提交前抛错并 `ROLLBACK`。
- **并发**：SQLite WAL 模式下读不阻塞写，写事务串行执行；查询按 `MAX(revision)` 取值，
  因此即使旧修订在最后到达，也无法覆盖已存在的新修订。
- **一致性读**：日报与事件都由单条 `SELECT` 读取（每小时最高修订），
  WAL 下看到的是一个一致快照，不会读到半批数据，也不会让事件与修订号来自不同状态。
- **确认判定**：确认在写事务内重算事件后比对内容指纹，再插入确认，
  因此写入与确认按 SQLite 写锁的获得顺序（即提交顺序）判定，过期确认整体回滚。
- **幂等**：同号同内容基于 `(value, retracted)` 比较，重复请求只计入 `unchanged`；
  同一事件的重复确认返回已有确认（`idempotent: true`）。

## 目录

```
app/
  main.py          应用工厂、异常映射
  api.py           路由
  models.py        读写请求 / 响应模型与字段校验
  event_models.py  事件查询 / 确认的模型与日期区间校验
  service.py       批量写入 / 日报计算
  events.py        连续超阈值事件计算与确认
  db.py            连接、建表、事务
  config.py        配置与业务常量
tests/
  test_api.py          业务规则与边界
  test_events.py       事件查询 / 确认 / 失效 / 并发
  test_concurrency.py  真实 uvicorn 下的并发竞态
examples/
  demo.py              可直接运行的调用示例
Dockerfile / docker-compose.yml
```
