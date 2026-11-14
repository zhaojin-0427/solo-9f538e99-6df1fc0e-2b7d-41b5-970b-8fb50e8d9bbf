# PM2.5 站点日报 API

站点设备 PM2.5 小时值的**补报 / 校准修订 / 撤回**接收服务，按北京时间日期计算日均值。
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

### 错误约定

| 状态码 | 含义 |
| --- | --- |
| `422` | 请求体/参数校验失败（小时越界、撤回带值、非撤回缺值、日期非法等） |
| `409` | 修订号冲突，整批已回滚 |
| `503` | 数据库繁忙（写锁等待超时），本批未生效，可重试 |

所有错误响应均为 `{"code": "...", "message": "..."}` 结构；`500` 时同样保证本批没有部分写入。

### 3. 可运行示例

```bash
python3 examples/demo.py http://localhost:8000
```

依次演示：补报 20 小时 → 幂等重试 → 撤回跌破门槛 → 用更大修订号补回 → 同号改内容被 409 拒绝。

## 实现要点

- **表结构**：`pm25_readings(station_id, obs_date, hour, revision, ...)` 主键为四元组，
  每个修订独立成行；`CHECK` 约束保证撤回/浓度的互斥关系在数据库层也成立。
- **原子性**：写事务用 `BEGIN IMMEDIATE` 先取写锁，再在同一事务里做冲突校验和插入，
  任何冲突都在提交前抛错并 `ROLLBACK`。
- **并发**：SQLite WAL 模式下读不阻塞写，写事务串行执行；查询按 `MAX(revision)` 取值，
  因此即使旧修订在最后到达，也无法覆盖已存在的新修订。
- **一致性读**：日报由单条 `SELECT` 读取，WAL 下看到的是一个一致快照，不会读到半批数据。
- **幂等**：同号同内容基于 `(value, retracted)` 比较，重复请求只计入 `unchanged`。

## 目录

```
app/
  main.py       应用工厂、异常映射
  api.py        路由
  models.py     Pydantic 模型与字段校验
  service.py    批量写入 / 日报计算
  db.py         连接、建表、事务
  config.py     配置与业务常量
tests/
  test_api.py          业务规则与边界
  test_concurrency.py  真实 uvicorn 下的并发竞态
examples/
  demo.py              可直接运行的调用示例
Dockerfile / docker-compose.yml
```
