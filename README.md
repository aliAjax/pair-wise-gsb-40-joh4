# 海上搜救协调系统

标准库实现的独立协调原型，使用 SQLite 保存事件、搜救资源、搜索区域、线索、离线批次和时间线。

## 运行

要求 Python 3.11+（在当前 Python 3.9 环境也可运行）。

```bash
python3 app.py --init --seed
python3 app.py
```

默认地址为 `http://127.0.0.1:8206`，数据库默认为 `maritime_sar.db`。`--db`、`--host`、`--port` 可覆盖默认值。

## 主要接口

写操作使用 JSON，并需要 `X-User` 与 `X-Role` 请求头。角色包括 `coordinator`、`operator`、`field`、`analyst`、`viewer`。

- `GET /health`、`GET /api/state`
- `POST /api/incidents`：创建遇险事件并识别重复报警
- `POST /api/assets`：登记资源
- `POST /api/areas`：创建搜索区域
- `POST /api/assignments`：按能力、海况和航程分配资源
- `POST /api/relief/reserve`：为区域预留学生接替船（校验能力、海况、航程与到场时间）
- `POST /api/relief/confirm`：确认接替，在切换时刻把责任转交给接替船
- `POST /api/relief/fail`：接替失败，预留退回原船并保留失败原因
- `POST /api/clues`、`POST /api/clues/verify`
- `POST /api/assets/withdraw`：撤回资源并释放任务
- `POST /api/incidents/transfer`、`POST /api/incidents/close`
- `POST /api/offline/batch`：幂等合并离线记录
- `GET /api/incidents/{id}/timeline`

## 接替交接

搜索船燃油或值守时限临近时，用交接代替“先撤回再重派”，区域中途不留空档：

1. `POST /api/relief/reserve` 传 `area_id`、`relief_asset_id`、`required_by`（原船燃油/值守截止的 ISO 8601 时间），可选 `expected_relief_version`。服务按原区域要求校验接替船能力、当前海况、航程，并按 `距离/航速` 估算到场时间（ETA）；ETA 晚于 `required_by` 即拒绝。预留成功后接替船置为 `reserved`（原子占用，两名协调员无法争用同一条船），**原船在确认前继续担责**。
2. `POST /api/relief/confirm` 传 `handover_id`（或 `area_id`）：记录切换时刻，接替船转为 `assigned`、原船释放为 `available`。
3. `POST /api/relief/fail` 传 `handover_id`（或 `area_id`）和 `reason`：接替船释放，原船继续担责，失败原因随交接记录保留。撤回涉及的原船/接替船、提前结束区域、按误警关闭事件时同样自动作废预留。
4. 服务重启时，仍停留在 `reserved` 的交接自动退回原船，失败原因记为“服务重启，接替确认未完成，预留退回原船”。

`GET /api/state` 的每个区域都带有 `original_asset_id`、`relief_asset_id`、`switched_at`、`current_asset_id`、`handover_status`、`handover_failure_reason`（无交接记录的老区域保持兼容），并额外返回 `area_handovers` 列表；页面可查看原船、接替船、切换时刻与当前责任人。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整协调流程、重复报警、错误位置、资源并发占用、离线幂等和权限拒绝。

## 局限

身份依赖调用方传入的用户和角色头；坐标使用球面距离近似；不会自动计算漂移概率区；文件附件、气象服务、真实通信链路和地理围栏未包含在内。
