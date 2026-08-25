# AWS 中国区 FinOps 客户部署手册

本文是本项目的主部署手册。客户可以按顺序完成双账号 CUR 汇聚、Athena 数据模型、QuickSight CID、FinOps API、优化建议和 FinOps AI 部署。

所有 `<...>` 均为客户环境参数。不要把真实账号、主机地址、用户名、Bucket 或密钥提交到 Git。

## 1. 部署完成后得到什么

```text
Linked Account A CUR ─┐
                      ├─ S3 Replication ─ Central S3 ─ Glue/Athena ─ QuickSight CID
Linked Account B CUR ─┘                         │
                                               └─ FinOps API / AI

PoC Container Host
  └─ finops-aws-china container
       ├─ Cost Explorer / Pricing
       ├─ Compute Optimizer / CloudWatch
       ├─ Cost Anomaly Detection
       ├─ CUR reconciliation
       ├─ QuickSight registered-user embedding
       └─ DeepSeek FinOps AI
```

## 2. 自动化边界和 Stack 输出

本仓库包含 5 个 CloudFormation 模板，不是单一总 Stack。

| 顺序 | 模板 | 部署位置 | 自动创建 | 输出 |
|---:|---|---|---|---|
| 1 | `phase1-central-account-a.yaml` | Account A / 北京 | 中央 S3、Bucket Policy、Glue Database、Athena Workgroup | `CentralBucketName`、`GlueDatabaseName`、`AthenaWorkGroupName` |
| 2 | `phase1-source-cur.yaml` | Account A / 宁夏 | A 的 CUR Bucket、Billing Policy、复制角色和规则 | `CurBucketName`、`ReplicationRoleArn` |
| 3 | `phase1-source-cur.yaml` | Account B / 宁夏 | B 的 CUR Bucket、Billing Policy、跨账号复制角色和规则 | `CurBucketName`、`ReplicationRoleArn` |
| 4 | `phase2-member-read-role.yaml` | Account B | 跨账号只读采集角色 | `MemberRoleArn` |
| 5 | `phase2-central-collector-role.yaml` | Account A | Collector Role 和 EC2 Instance Profile | `CollectorRoleArn`、`InstanceProfileName` |
| 6 | `phase1-cid-deployment.yaml` | Account A / 北京 | AWS 官方 CID 嵌套 Stack | `CostIntelligenceDashboardURL`、`DeploymentRegion`、`OfficialCIDTemplate` |

仍需按本文手工完成：创建 CUR Definition、等待首次交付、创建 Athena 原始表、关联 Instance Profile、配置 Secrets，以及完成 QuickSight 和应用验收。

中央和源 S3 Bucket 使用 `DeletionPolicy: Retain`。删除 Stack 不会自动删除成本数据。

## 3. 前置条件

### 3.1 工具

- AWS CLI v2、Git、OpenSSL 和 Bash（Linux、macOS 或 WSL）。
- 应用宿主机安装 Docker Engine 24+ 和 Docker Compose v2。

### 3.2 AWS 条件

- 两个 AWS 中国区账号均能管理 CUR、S3、IAM 和 CloudFormation。
- Account A 能管理 Glue、Athena、QuickSight 和 EC2 Instance Profile。
- QuickSight Enterprise 已在北京区启用，并存在一个 Active Admin/Author。
- 应用宿主机能访问 AWS API 和 DeepSeek API。

### 3.3 AWS Profiles

本文使用：

```text
account-a = 中央账号 / Linked Account A
account-b = 成员账号 / Linked Account B
```

验证两个 Profile：

```bash
aws sts get-caller-identity --profile account-a --query Account --output text
aws sts get-caller-identity --profile account-b --query Account --output text
```

两个命令必须返回不同账号。

## 4. 设置部署变量

```bash
export ACCOUNT_A_ID="$(aws sts get-caller-identity --profile account-a --query Account --output text)"
export ACCOUNT_B_ID="$(aws sts get-caller-identity --profile account-b --query Account --output text)"

export CENTRAL_REGION="cn-north-1"
export CUR_REGION="cn-northwest-1"

export CENTRAL_BUCKET="customer-finops-central-${ACCOUNT_A_ID}-${CENTRAL_REGION}"
export CUR_BUCKET_A="customer-cur-a-${ACCOUNT_A_ID}-${CUR_REGION}"
export CUR_BUCKET_B="customer-cur-b-${ACCOUNT_B_ID}-${CUR_REGION}"

export CUR_REPORT_A="finops-cur-account-a"
export CUR_REPORT_B="finops-cur-account-b"
export EXTERNAL_ID="$(openssl rand -hex 24)"
```

Bucket 名称必须全局唯一。真实变量只保存在客户受控终端和配置中。

## 5. 部署中央数据层

在 Account A 北京区执行：

```bash
aws cloudformation deploy \
  --profile account-a --region "${CENTRAL_REGION}" \
  --stack-name finops-central-data \
  --template-file infra/cloudformation/phase1-central-account-a.yaml \
  --parameter-overrides \
    CentralBucketName="${CENTRAL_BUCKET}" \
    SourceAccountAId="${ACCOUNT_A_ID}" \
    SourceAccountBId="${ACCOUNT_B_ID}" \
    SourceReplicationRoleName=FinOpsCURReplicationRole \
    DatabaseName=finops WorkGroupName=finops \
  --capabilities CAPABILITY_NAMED_IAM \
  --no-fail-on-empty-changeset
```

查看输出：

```bash
aws cloudformation describe-stacks \
  --profile account-a --region "${CENTRAL_REGION}" \
  --stack-name finops-central-data \
  --query 'Stacks[0].Outputs' --output table
```

| Output | 后续用途 |
|---|---|
| `CentralBucketName` | CUR 汇聚、Athena Results、CID |
| `GlueDatabaseName` | Athena 和应用配置 |
| `AthenaWorkGroupName` | Athena、CID 和应用配置 |

验证 Versioning 和 Public Access Block：

```bash
aws s3api get-bucket-versioning --profile account-a --bucket "${CENTRAL_BUCKET}"
aws s3api get-public-access-block --profile account-a --bucket "${CENTRAL_BUCKET}"
```

Versioning 应为 `Enabled`，四项 Public Access Block 应全部为 `true`。

## 6. 部署两个 CUR 源端

### 6.1 Account A

```bash
aws cloudformation deploy \
  --profile account-a --region "${CUR_REGION}" \
  --stack-name finops-cur-source-a \
  --template-file infra/cloudformation/phase1-source-cur.yaml \
  --parameter-overrides \
    CurBucketName="${CUR_BUCKET_A}" CurPrefix=cur \
    CentralBucketName="${CENTRAL_BUCKET}" \
    CentralAccountId="${ACCOUNT_A_ID}" \
    ReplicationRoleName=FinOpsCURReplicationRole \
    IsCrossAccount=false \
  --capabilities CAPABILITY_NAMED_IAM \
  --no-fail-on-empty-changeset
```

### 6.2 Account B

```bash
aws cloudformation deploy \
  --profile account-b --region "${CUR_REGION}" \
  --stack-name finops-cur-source-b \
  --template-file infra/cloudformation/phase1-source-cur.yaml \
  --parameter-overrides \
    CurBucketName="${CUR_BUCKET_B}" CurPrefix=cur \
    CentralBucketName="${CENTRAL_BUCKET}" \
    CentralAccountId="${ACCOUNT_A_ID}" \
    ReplicationRoleName=FinOpsCURReplicationRole \
    IsCrossAccount=true \
  --capabilities CAPABILITY_NAMED_IAM \
  --no-fail-on-empty-changeset
```

### 6.3 验证

```bash
aws cloudformation describe-stacks \
  --profile account-a --region "${CUR_REGION}" \
  --stack-name finops-cur-source-a \
  --query 'Stacks[0].Outputs' --output table

aws cloudformation describe-stacks \
  --profile account-b --region "${CUR_REGION}" \
  --stack-name finops-cur-source-b \
  --query 'Stacks[0].Outputs' --output table

aws s3api get-bucket-replication --profile account-a --bucket "${CUR_BUCKET_A}"
aws s3api get-bucket-replication --profile account-b --bucket "${CUR_BUCKET_B}"
```

两个 Stack 都应输出 `CurBucketName` 和 `ReplicationRoleArn`；复制规则应为 `Enabled`，Destination 应为中央 Bucket。

## 7. 创建两个 CUR Definition

CloudFormation 只创建 Bucket 和复制链路；CUR Definition 由脚本创建。

```bash
AWS_PROFILE=account-a CUR_REGION="${CUR_REGION}" \
CUR_REPORT_NAME="${CUR_REPORT_A}" CUR_BUCKET="${CUR_BUCKET_A}" \
CUR_PREFIX=cur bash scripts/create-cur.sh

AWS_PROFILE=account-b CUR_REGION="${CUR_REGION}" \
CUR_REPORT_NAME="${CUR_REPORT_B}" CUR_BUCKET="${CUR_BUCKET_B}" \
CUR_PREFIX=cur bash scripts/create-cur.sh
```

两个 CUR 使用相同口径：Hourly、Parquet、Overwrite、Resource IDs、Split Cost Allocation Data、Athena artifact、Refresh closed reports。

验证：

```bash
aws cur describe-report-definitions --profile account-a --region "${CUR_REGION}" \
  --query "ReportDefinitions[?ReportName=='${CUR_REPORT_A}']" --output table

aws cur describe-report-definitions --profile account-b --region "${CUR_REGION}" \
  --query "ReportDefinitions[?ReportName=='${CUR_REPORT_B}']" --output table
```

首次交付不是即时完成。等待两个源 Bucket 都出现 Manifest、Parquet 和 Athena artifact：

```bash
aws s3 ls "s3://${CUR_BUCKET_A}/cur/${CUR_REPORT_A}/" --profile account-a --recursive
aws s3 ls "s3://${CUR_BUCKET_B}/cur/${CUR_REPORT_B}/" --profile account-b --recursive
aws s3 ls "s3://${CENTRAL_BUCKET}/cur/" --profile account-a --recursive
```

中央 Bucket 应出现两个报告目录。Live Replication 只复制规则启用后的对象；历史对象需要 S3 Batch Replication。

## 8. 创建 Athena 数据模型

### 8.1 原始表

使用两个 CUR 交付目录中的 Athena 建表 artifact 创建：

```text
finops.cur_account_a_raw
finops.cur_account_b_raw
```

修改每份建表 SQL：

1. 表名分别设为 `cur_account_a_raw`、`cur_account_b_raw`。
2. `LOCATION` 指向中央北京 Bucket 中对应报告的真实 Parquet 路径。
3. 两张表保持相同 CUR 版本和字段口径。
4. 原始表不直接授权给 QuickSight 客户用户。

Athena 执行环境：北京区、`AwsDataCatalog`、Database `finops`、Workgroup `finops`。

### 8.2 统一视图

在 Athena 执行 `infra/sql/phase1-athena-views.sql`，生成：

- `finops.cur_unified`
- `finops.v_dashboard_cost_daily`

验证：

```sql
SELECT linked_account_alias,
       COUNT(*) AS line_count,
       MIN(line_item_usage_start_date) AS first_usage,
       MAX(line_item_usage_start_date) AS last_usage,
       SUM(line_item_unblended_cost) AS unblended_cost
FROM finops.cur_unified
GROUP BY linked_account_alias;
```

必须同时返回 A/B 且两边行数大于 0。相同账期、币种和 Metric 下，CUR 与 CE 差异应不超过 1%，或可由 Credit、Refund、Tax、摊销等口径解释。

## 9. 部署跨账号采集权限

### 9.1 Account B Read Role

```bash
aws cloudformation deploy \
  --profile account-b --region "${CUR_REGION}" \
  --stack-name finops-member-read-role \
  --template-file infra/cloudformation/phase2-member-read-role.yaml \
  --parameter-overrides \
    CentralAccountId="${ACCOUNT_A_ID}" \
    CentralCollectorRoleName=FinOpsCollectorRole \
    MemberRoleName=FinOpsReadOnlyRole \
    ExternalId="${EXTERNAL_ID}" \
  --capabilities CAPABILITY_NAMED_IAM \
  --no-fail-on-empty-changeset

export MEMBER_ROLE_ARN="$(aws cloudformation describe-stacks \
  --profile account-b --region "${CUR_REGION}" \
  --stack-name finops-member-read-role \
  --query "Stacks[0].Outputs[?OutputKey=='MemberRoleArn'].OutputValue | [0]" \
  --output text)"
```

### 9.2 Account A Collector Role

```bash
aws cloudformation deploy \
  --profile account-a --region "${CENTRAL_REGION}" \
  --stack-name finops-central-collector \
  --template-file infra/cloudformation/phase2-central-collector-role.yaml \
  --parameter-overrides \
    CollectorRoleName=FinOpsCollectorRole \
    MemberAccountRoleArn="${MEMBER_ROLE_ARN}" \
    CentralBucketName="${CENTRAL_BUCKET}" \
    AthenaDatabaseName=finops AthenaWorkGroupName=finops \
  --capabilities CAPABILITY_NAMED_IAM \
  --no-fail-on-empty-changeset

export INSTANCE_PROFILE_NAME="$(aws cloudformation describe-stacks \
  --profile account-a --region "${CENTRAL_REGION}" \
  --stack-name finops-central-collector \
  --query "Stacks[0].Outputs[?OutputKey=='InstanceProfileName'].OutputValue | [0]" \
  --output text)"
```

### 9.3 关联宿主 EC2

先检查现有配置：

```bash
export HOST_REGION="<HOST_EC2_REGION>"
export HOST_INSTANCE_ID="<HOST_EC2_INSTANCE_ID>"

aws ec2 describe-iam-instance-profile-associations \
  --profile account-a --region "${HOST_REGION}" \
  --filters "Name=instance-id,Values=${HOST_INSTANCE_ID}" --output table
```

仅当 EC2 没有现有 Instance Profile 时执行：

```bash
aws ec2 associate-iam-instance-profile \
  --profile account-a --region "${HOST_REGION}" \
  --instance-id "${HOST_INSTANCE_ID}" \
  --iam-instance-profile "Name=${INSTANCE_PROFILE_NAME}"
```

如果已有 Instance Profile，不要直接替换；应合并只读权限、使用专用宿主机，或在维护窗口评估更换。

在宿主机验证本账号和成员账号。将占位符替换成前面得到的实际值：

```bash
aws sts get-caller-identity
aws sts assume-role \
  --role-arn "<MEMBER_ROLE_ARN>" \
  --role-session-name finops-deployment-test \
  --external-id "<EXTERNAL_ID>" \
  --query 'AssumedRoleUser.Arn' --output text
```

## 10. 部署 QuickSight CID

### 10.1 获取用户

```bash
aws quicksight list-users \
  --profile account-a --region "${CENTRAL_REGION}" \
  --aws-account-id "${ACCOUNT_A_ID}" --namespace default \
  --query 'UserList[].{UserName:UserName,Role:Role,Active:Active}' --output table

export QUICKSIGHT_USER="<ACTIVE_ADMIN_OR_AUTHOR>"
```

### 10.2 部署 CID

```bash
aws cloudformation deploy \
  --profile account-a --region "${CENTRAL_REGION}" \
  --stack-name finops-cost-intelligence-dashboard \
  --template-file infra/cloudformation/phase1-cid-deployment.yaml \
  --parameter-overrides \
    QuickSightUser="${QUICKSIGHT_USER}" ShareDashboard=yes \
    CURBucketPath="s3://${CENTRAL_BUCKET}/cur" \
    CURDatabaseName=finops CURTableName=cur_unified \
    AthenaWorkgroup=finops AthenaQueryResultsBucket="${CENTRAL_BUCKET}" \
    CurrencySymbol=JPY \
    QuickSightDataSourceRoleName=CidQuickSightDataSourceRole \
  --capabilities CAPABILITY_NAMED_IAM CAPABILITY_AUTO_EXPAND \
  --no-fail-on-empty-changeset
```

`JPY` 用于 AWS 官方中国区 CID 的 `¥` 符号；实际币种以 CUR 字段为准。

```bash
aws cloudformation describe-stacks \
  --profile account-a --region "${CENTRAL_REGION}" \
  --stack-name finops-cost-intelligence-dashboard \
  --query 'Stacks[0].Outputs' --output table
```

预期得到 `CostIntelligenceDashboardURL`。在 QuickSight 中确认 SPICE ingestion 为 `COMPLETED` 且 Rows dropped 为 0。

### 10.3 获取 Dashboard ID 和 User ARN

```bash
aws quicksight list-dashboards \
  --profile account-a --region "${CENTRAL_REGION}" \
  --aws-account-id "${ACCOUNT_A_ID}" \
  --query 'DashboardSummaryList[].{Name:Name,DashboardId:DashboardId}' --output table

aws quicksight list-users \
  --profile account-a --region "${CENTRAL_REGION}" \
  --aws-account-id "${ACCOUNT_A_ID}" --namespace default \
  --query "UserList[?UserName=='${QUICKSIGHT_USER}'].Arn | [0]" --output text
```

记录 Dashboard ID 和 User ARN。不要为了嵌入创建额外 Admin；Admin 和 Author 都按 Author 席位收费。

## 11. 部署 FinOps Web/API/AI

### 11.1 盘点宿主机

```bash
docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Ports}}'
docker system df
df -h
free -h
```

### 11.2 获取代码

```bash
sudo install -d -m 0755 /opt/finops-aws-china
sudo chown "$(id -u):$(id -g)" /opt/finops-aws-china
git clone https://github.com/zjinsong/FinOPS-AWS-China.git /opt/finops-aws-china
cd /opt/finops-aws-china
```

若目录已存在，先执行 `git status --short`，不要覆盖客户修改。

### 11.3 配置 `.env`

```bash
cp .env.example .env
chmod 600 .env
```

| 变量 | 填写来源 |
|---|---|
| `FINOPS_ACCOUNT_B_ROLE_ARN` | `MemberRoleArn` |
| `FINOPS_ACCOUNT_B_EXTERNAL_ID` | 第4节 External ID |
| `FINOPS_ATHENA_DATABASE` | `GlueDatabaseName` |
| `FINOPS_ATHENA_WORKGROUP` | `AthenaWorkGroupName` |
| `FINOPS_ATHENA_VIEW` | `cur_unified` |
| `FINOPS_QUICKSIGHT_DASHBOARD_ID` | `list-dashboards` 输出 |
| `FINOPS_QUICKSIGHT_USER_ARN` | `list-users` 输出 |
| `FINOPS_QUICKSIGHT_ALLOWED_DOMAINS` | PoC 为 `http://localhost:8080`；生产为批准的 HTTPS 域名 |
| `FINOPS_SECURE_COOKIE` | localhost PoC 为 `false`；HTTPS 为 `true` |

检查占位符：

```bash
if grep -Eq '<[A-Z0-9_]+>' .env; then
  echo 'ERROR: unresolved placeholders in .env'
  exit 1
fi
```

### 11.4 创建 Secrets

```bash
read -rsp 'DeepSeek API Key: ' DEEPSEEK_KEY
printf '\n'
printf '%s\n' "${DEEPSEEK_KEY}" | sudo bash scripts/bootstrap-secrets.sh
unset DEEPSEEK_KEY
```

生成 `/etc/finops-ai/` 下的 DeepSeek Key、Session Secret、Pseudonym Secret 和管理员密码，文件权限为 root-only。

### 11.5 构建并启动

```bash
docker build -t finops-aws-china:2.0.0 -f deploy/Dockerfile .
sudo bash scripts/hash-admin-password.sh
sudo test -s /etc/finops-ai/admin_password_hash
sudo bash scripts/deploy.sh
```

```bash
docker compose --env-file .env -f deploy/docker-compose.yml ps
docker compose --env-file .env -f deploy/docker-compose.yml logs --tail 100 finops-api
curl -fsS http://127.0.0.1:8080/api/v1/auth/status
```

预期容器为 `healthy`，端口只绑定 `127.0.0.1:8080`。

## 12. 用户登录

管理员密码只保存在宿主机：

```bash
sudo cat /etc/finops-ai/admin_password
```

通过 SSM 或 SSH 建立转发：

```bash
ssh -L 8080:127.0.0.1:8080 <HOST_USER>@<HOST_ADDRESS>
```

访问 `http://localhost:8080`，用户名为 `finopsadmin`。

CID 使用 Registered User 临时嵌入会话，客户不需要再次输入 QuickSight 密码。它不是 Anonymous Embedding，也不需要购买读者会话容量套餐。

## 13. 端到端验收

```bash
sudo python3 tests/smoke-test.py
```

验收项目：

1. 页面只显示 Linked Account A/B。
2. 两个账号都可采集；单账号失败时整体为 `PARTIAL`。
3. CE/CUR 差异不超过 1%或有解释。
4. CO、Idle、RDS/Aurora 建议有来源、证据、风险和成本。
5. 未配置异常监控器时显示 `NOT_CONFIGURED`。
6. CID 在 FinOps 页面中加载。
7. AI 回答带数据来源、时间范围和币种。
8. API、HTML 和日志无账号 ID、AK/SK 或 DeepSeek Key。

## 14. 输出到应用配置映射

| 输出 | 下游用途 |
|---|---|
| `CentralBucketName` | CID CUR 路径、Athena Results、Collector S3 权限 |
| `GlueDatabaseName` | `FINOPS_ATHENA_DATABASE` |
| `AthenaWorkGroupName` | `FINOPS_ATHENA_WORKGROUP` |
| `CurBucketName` | CUR Definition `S3Bucket` |
| `ReplicationRoleArn` | 复制链路审计，不写入应用配置 |
| `MemberRoleArn` | `FINOPS_ACCOUNT_B_ROLE_ARN` |
| `CollectorRoleArn` | IAM 审计 |
| `InstanceProfileName` | 关联应用宿主 EC2 |
| `CostIntelligenceDashboardURL` | CID 人工验收入口 |
| QuickSight Dashboard ID | `FINOPS_QUICKSIGHT_DASHBOARD_ID` |
| QuickSight User ARN | `FINOPS_QUICKSIGHT_USER_ARN` |

## 15. 常见故障

### CUR 没有文件

检查 Definition 的 Bucket、Region、Prefix 和 Billing Reports Bucket Policy。首次交付需要等待。

### 中央 Bucket 没有 Account B 数据

检查 Replication Rule、复制角色名称和对象创建时间。规则启用前的历史对象需要 Batch Replication。

### Athena 为 0 行或 Schema 错误

检查原始表 `LOCATION`、A/B CUR 版本，并确认查询位于北京区 `finops` Database 和 Workgroup。

### STS AssumeRole AccessDenied

检查 `MemberRoleArn`、External ID 和 EC2 当前实际 Instance Profile。

### CID 无法显示

检查 QuickSight Region、用户状态、SPICE ingestion、Athena 权限、Dashboard ID、User ARN 和 Allowed Domains。

### Anonymous API 报错

本仓库默认使用 Registered User。`GenerateEmbedUrlForAnonymousUser` 需要购买会话容量；未开通会返回 `UnsupportedPricingPlanException`。

## 16. 更新和删除保护

更新前记录当前 Git commit、镜像摘要，并备份 `/data/finops.db`。新镜像通过 Smoke Test 后再切换。

删除 Stack 前必须人工确认。S3 Bucket 会保留并继续产生存储费用；CUR Definition、QuickSight 订阅、SPICE 和 CID 也要分别检查，不能把“Stack 已删除”等同于“全部费用已停止”。
