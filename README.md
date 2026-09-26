# 桥梁结构监测与限行决策 + 汛期通航净空台账

融合传感、巡检、交通荷载和天气数据，生成限载限行或恢复建议；汛期另设通航净空台账，
每座桥孔登记水位基准、梁底标高和当前净空，船队按吃水和计划时段申报通行。

## 模块结构

- `app.py`：参数解析、依赖组装和HTTP服务启动。
- `src/domain.py`：数据结构、错误、状态和基础校验。
- `src/rules.py`：状态机、角色矩阵、优先级、期限和关闭不变量。
- `src/repository.py`：SQLite建表、事务、版本控制和审计链。
- `src/service.py`：权限检查、用例编排、并发控制和审计。
- `src/http_api.py`：JSON路由和统一错误响应（两类业务的请求入口）。
- `src/audit.py`：UTC时间和SHA-256审计事件。
- `src/clearance_domain.py`：桥孔、通行单数据结构、测点状态和时段校验。
- `src/clearance_rules.py`：净空计算与通行判定规则（409原因在此生成）。
- `src/clearance_repository.py`：桥孔档案、通行单表、活动单唯一约束和净空审计链。
- `src/clearance_service.py`：档案维护、通行受理、更正失效与重算编排。
- `static/index.html`：最小演示页（含剩余净空和待复测桥孔）。
- `tests/`：完整流程、规则、失败和净空台账测试（含HTTP 409测试）。

桥孔档案（repository）、判定规则（rules）和请求入口（http_api）分模块承担。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8318
```

默认端口为`8318`，首次启动自动建库。使用`X-Actor`和`X-Role`请求头传递身份。

## 结构监测接口

- `GET /health`
- `GET /api/items`
- `POST /api/items`
- `GET /api/items/{id}`
- `POST /api/items/{id}/records`
- `POST /api/items/{id}/transition`，必须提交`expected_version`
- `GET /api/audit`

允许角色：sensor_operator, bridge_engineer, traffic_authority, viewer。

## 通航净空台账接口

- `POST /api/clearance/spans`：登记桥孔（span_code、name、bridge_name、
  datum_elevation水位基准、beam_elevation梁底标高、safety_margin安全余量）。
  新孔默认测点离线、待复测。角色：bridge_engineer。
- `GET /api/clearance/spans`：台账列表，每孔给出`current_clearance`（=梁底标高-
  水位基准-最新读数）、`remaining_clearance`（再扣除未结束通行单占用的净空），
  并汇总`pending_resurvey`待复测桥孔。
- `GET /api/clearance/spans/{id}`
- `POST /api/clearance/spans/{id}/readings`：登记水位读数（复测），测点转在线、
  清除待复测标记。角色：sensor_operator。
- `POST /api/clearance/spans/{id}/gauge`：设置测点online/offline；离线后桥孔转入
  待复测。角色：sensor_operator。
- `POST /api/clearance/spans/{id}/datum`：更正水位基准，必须带`expected_version`；
  更正后该孔全部未结束通行单失效待重算，桥孔转入待复测。角色：bridge_engineer。
- `POST /api/clearance/orders`：船队提交span_id、fleet_name、draft吃水、
  planned_start/planned_end计划时段。角色：fleet_operator。
- `GET /api/clearance/orders?span_id=&status=`
- `POST /api/clearance/orders/{id}/complete`：登记通行结束，释放桥孔。
- `POST /api/clearance/orders/{id}/draft`：更正船舶吃水（带`expected_version`），
  原通行单立即失效。角色：fleet_operator。
- `POST /api/clearance/orders/{id}/recalculate`：对失效单按最新基准、水位和吃水
  重新判定，通过恢复active，不通过返回409。角色：fleet_operator, traffic_authority。
- `GET /api/clearance/audit?entity_id=`

### 409冲突规则（响应体`message`说明原因）

1. 净空不足：当前净空 < 吃水 + 安全余量；
2. 水位测点离线（或尚无读数），无法确认当前净空；
3. 同一桥孔已有未结束（active）通行单——数据库对`span_id`活动单建唯一索引兜底。

船舶吃水或水位基准更正后，原通行单状态置为`invalidated`，经复测/重算通过才恢复。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
