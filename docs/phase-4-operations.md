# Phase 4：持续运行与治理

## 1. 运行节奏

| 频率 | 工作 | 输出 |
|---|---|---|
| 每日 | CUR 新鲜度、API 采集、异常和建议刷新 | 采集状态、异常、建议快照 |
| 每周 | 工程评审高价值建议 | 有负责人和期限的任务清单 |
| 每月 | 账单核对、预算、承诺和实际节省复盘 | 管理层 FinOps 摘要 |
| 每季度 | IAM、阈值、数据保留和恢复演练 | 治理检查记录 |

## 2. 建议任务状态机

```text
NEW → TRIAGED → APPROVED → IN_PROGRESS → IMPLEMENTED
  ↘ DISMISSED ←───────────────┘
```

API：

```http
GET /api/v1/tasks
PUT /api/v1/tasks/{recommendation_id}
```

新任务必须从 `NEW` 开始。每次更新记录负责人、预计节省、实际节省、备注、操作者和时间。`IMPLEMENTED` 不允许直接退回，确需重开时应创建新的评审记录。

## 3. 节省验证

预计节省不能直接视为实际节省。建议实施后：

1. 记录实施日期和受影响资源。
2. 选择实施前、实施后可比窗口。
3. 排除业务量变化、价格变化、Credit、Refund 和 Tax。
4. 使用 CUR/CE 重新计算实际月度节省。
5. 将证据链接和结果写入任务备注。

## 4. 运行监控

至少监控：

- 两个账号的 CUR 最后交付时间。
- S3 Replication FailedOperations。
- Athena 查询失败和扫描字节数。
- CE/CO/CW 请求失败、限流和延迟。
- 单账号 `PARTIAL` 持续时间。
- SPICE 最近摄取状态。
- DeepSeek 错误率和 Token 用量。
- 容器健康、CPU、内存、磁盘和 SQLite 大小。

## 5. 备份与恢复

备份对象：

- `/data/finops.db`
- 非敏感配置摘要
- 每周/月报告

不要把 `/etc/finops-ai` 下的明文密钥复制到普通备份桶。生产环境应使用 Secrets Manager 或等效受控密钥系统，并单独设计恢复流程。

恢复验证：

1. 在隔离主机启动同版本镜像。
2. 恢复 SQLite 数据库。
3. 重新注入 secrets 和 Instance Profile。
4. 执行 Smoke Test。
5. 验证任务、审计记录和 Dashboard 可用。

## 6. 月度运营会议

建议议程：

1. 实际成本与预算差异。
2. 主要服务和账号成本变化。
3. 新异常及关闭情况。
4. 新增、完成和驳回建议。
5. 预计节省与实际节省差异。
6. RI/SP 覆盖率、利用率和到期计划。
7. 下月负责人和目标日期。
