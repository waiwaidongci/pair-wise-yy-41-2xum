# 桥梁结构监测与通航净空台账

融合传感、巡检、交通荷载和天气数据，生成限载限行或恢复建议；汛期维护桥孔通航净空台账，判定船舶过桥孔通行请求。

## 模块结构

- `app.py`：参数解析、依赖组装和HTTP服务启动。
- `src/domain.py`：数据结构、错误、状态和基础校验。
- `src/rules.py`：状态机、角色矩阵、优先级、期限和关闭不变量。
- `src/repository.py`：SQLite建表、事务、版本控制和审计链。
- `src/service.py`：权限检查、用例编排、并发控制和审计。
- `src/http_api.py`：JSON路由和统一错误响应。
- `src/audit.py`：UTC时间和SHA-256审计事件。
- `src/nav_domain.py`：通航净空字段校验（吃水、标高、计划时段）。
- `src/nav_rules.py`：净空判定规则（净空计算、409冲突、待复测）。
- `src/nav_repository.py`：桥孔档案与通行单持久化，与监测库共用连接、锁和审计链。
- `src/nav_service.py`：通航用例编排（申报、更正、失效重算）。
- `src/nav_http.py`：通航净空请求入口路由。
- `static/index.html`：最小演示页。
- `tests/`：完整流程、规则和失败测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8318
```

默认端口为`8318`，首次启动自动建库。使用`X-Actor`和`X-Role`请求头传递身份。

## 主要接口

- `GET /health`
- `GET /api/items`
- `POST /api/items`
- `GET /api/items/{id}`
- `POST /api/items/{id}/records`
- `POST /api/items/{id}/transition`，必须提交`expected_version`
- `GET /api/audit`

允许角色：sensor_operator, bridge_engineer, traffic_authority, viewer。监测偏差与预警阈值之比和多条异常记录决定告警等级；限行与封闭决策必须绑定交通通告记录。

## 通航净空台账

每座桥孔登记水位基准、梁底标高、安全富余和水位测点；当前净空 = 梁底标高 −（水位基准 + 水位读数），剩余净空 = 当前净空 − 船舶吃水。船队提交吃水和计划时段申报通行单。

- `GET /api/nav/openings`：台账列表，逐项显示当前净空、剩余净空和待复测标记，并汇总`pending_remeasure_openings`（测点离线或无水位数据的桥孔）。
- `POST /api/nav/openings`：登记桥孔档案（bridge_engineer）。
- `GET /api/nav/openings/{id}`、`GET /api/nav/openings/{id}/passages`、`GET /api/nav/passages/{id}`
- `POST /api/nav/openings/{id}/water-level`：上报水位读数（sensor_operator）。
- `POST /api/nav/openings/{id}/gauge`：测点上线/离线（sensor_operator）。
- `POST /api/nav/openings/{id}/datum`：更正水位基准（bridge_engineer）。
- `POST /api/nav/openings/{id}/passages`：船队提交吃水和计划时段（fleet_operator）。
- `POST /api/nav/passages/{id}/complete`：结束通行单。
- `POST /api/nav/passages/{id}/correct-draft`：更正船舶吃水。

通行请求出现以下情况返回409并说明原因：净空不足（剩余净空低于安全富余）、水位测点离线（含无水位数据）、同一桥孔已有未结束通行单。船舶吃水或水位基准更正后，原通行单失效并按新数据重算：重算通过则签发后继通行单，不通过则保持失效并在响应中给出原因。新增角色：fleet_operator（船队）。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
