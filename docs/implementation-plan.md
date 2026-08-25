# AWS 中国区 FinOps 四阶段实施计划

## 1. 总体原则

- 每个阶段独立部署、验证和验收，前一阶段通过后再进入下一阶段。
- AWS API 和应用默认只读，不自动执行关机、删除、缩容或购买承诺。
- 真实账号仅保存在部署参数和服务端配置，UI、API、日志和 AI 使用账号别名。
- 优先使用 AWS 中国区现成 API；没有托管建议 API 的资源才使用 CloudWatch、资源清单和 CUR 规则。

## 2. Phase 1：数据整合

### 输入

- Linked Account A、Linked Account B 的账单管理员权限。
- 两个账号的宁夏区 S3 和 IAM 配置权限。
- 中央账号的北京区 S3、Glue、Athena 和 QuickSight 权限。

### 实施步骤

1. 在中央账号北京区部署 `phase1-central-account-a.yaml`。
2. 在两个源账号宁夏区分别部署 `phase1-source-cur.yaml`。
3. 在两个账号运行 `scripts/create-cur.sh`，创建一致口径的 Hourly Parquet CUR。
4. 等待首次 CUR 交付，验证 Manifest、Parquet 和复制状态。
5. 使用 CUR 生成的 Athena SQL 建立两个原始表。
6. 执行 `infra/sql/phase1-athena-views.sql`，建立账号别名统一视图。
7. 用 Cost Explorer 对完整账期做金额核对。
8. 创建 QuickSight Athena Data Source、SPICE Dataset、Analysis 和 Dashboard。
9. 可选部署 `phase1-cid-deployment.yaml`。

### 验收门

- 两个源端最近交付均成功。
- 中央表同时存在 Linked Account A/B 数据。
- CE 与 CUR 同口径差异不超过 1%，差异项可解释。
- Dashboard 不包含真实账号、Bucket 或资源 ID。

## 3. Phase 2：API 聚合和 COH-lite

### 实施步骤

1. 生成至少 16 字符的随机 External ID。
2. 在成员账号部署 `phase2-member-read-role.yaml`。
3. 在中央账号部署 `phase2-central-collector-role.yaml`。
4. 将 Instance Profile 关联到应用宿主 EC2。
5. 配置 `.env` 中的成员角色、Athena 和 QuickSight 参数。
6. 启动容器并验证 STS、CE、CO、CW、异常、Pricing 和 Athena。
7. 验证单账号故障时整体返回 `PARTIAL`，而不是全局失败。
8. 核对优化建议的来源、时间范围、指标覆盖率和成本证据。

### 验收门

- API 响应只显示账号别名和伪匿名资源标识。
- Compute Optimizer、CE 和自定义规则能够统一显示。
- `NOT_CONFIGURED` 不被错误解释为“没有异常”。
- 所有 AWS 凭据来自 Instance Profile/STS。

## 4. Phase 3：FinOps AI

### 实施步骤

1. 通过 Docker secret 配置 DeepSeek API Key。
2. 在模型配置页选择 `deepseek-chat` 或 `deepseek-reasoner`。
3. 验证 AI 工具路由只查询受控后端接口，不执行客户端 SQL。
4. 验证回答包含数据源、时间范围和币种。
5. 执行 Prompt Injection、敏感信息和超范围问题测试。
6. 验证前端和 API 均不返回模型 Key。

### 验收门

- 成本数字来自工具结果，不由模型自由生成。
- 原始 CUR、账号 ID、ARN 和密钥不会发送给模型。
- API 超时或数据不足时，回答明确说明不确定性。

## 5. Phase 4：持续运行

### 实施步骤

1. 配置每日采集和数据质量检查。
2. 每周评审高优先级建议并更新任务状态。
3. 每月核对预计节省和实际节省。
4. 备份 SQLite、配置摘要和报告到受控 S3。
5. 监控采集失败、Athena 扫描量、CE 请求量和 AI Token。
6. 每季度复核 IAM、规则阈值、价格口径和数据保留策略。

### 验收门

- 建议任务按规定状态机流转并保留审计记录。
- 单账号、单 API 或 AI 故障不会破坏中央成本数据。
- 恢复演练能从备份重建应用状态。

## 6. 推荐暂停点

| 暂停点 | 确认事项 |
|---|---|
| Phase 1 后 | 数据完整、金额一致、Dashboard 脱敏 |
| Phase 2 后 | API 权限只读、建议有证据、Partial 正常 |
| Phase 3 后 | AI 安全、数字可追溯、Key 不外传 |
| Phase 4 后 | 运行制度、任务闭环、备份恢复有效 |
