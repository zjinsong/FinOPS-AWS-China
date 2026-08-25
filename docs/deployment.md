# 部署手册

## 1. 前置条件

- AWS CLI v2，使用 AWS 中国区凭据或 SSO。
- Docker Engine 和 Docker Compose v2。
- 一台已有 Instance Profile 的 Linux EC2 宿主机。
- Phase 1 中央 Athena 视图已经可查询。
- QuickSight Enterprise 和已发布 Dashboard（可选）。
- DeepSeek API Key（启用 AI 时）。

## 2. 部署基础设施

### 2.1 中央数据账号（北京）

```bash
aws cloudformation deploy \
  --region cn-north-1 \
  --stack-name finops-central-data \
  --template-file infra/cloudformation/phase1-central-account-a.yaml \
  --parameter-overrides \
    CentralBucketName=<GLOBALLY_UNIQUE_BUCKET> \
    SourceAccountAId=<ACCOUNT_A_ID> \
    SourceAccountBId=<ACCOUNT_B_ID> \
  --capabilities CAPABILITY_NAMED_IAM
```

### 2.2 两个 CUR 源账号（宁夏）

分别使用对应账号身份执行：

```bash
aws cloudformation deploy \
  --region cn-northwest-1 \
  --stack-name finops-cur-source \
  --template-file infra/cloudformation/phase1-source-cur.yaml \
  --parameter-overrides \
    CurBucketName=<SOURCE_CUR_BUCKET> \
    CentralBucketName=<CENTRAL_BUCKET> \
    CentralAccountId=<CENTRAL_ACCOUNT_ID> \
    IsCrossAccount=true \
  --capabilities CAPABILITY_NAMED_IAM
```

中央账号自己的源栈将 `IsCrossAccount=false`。

随后设置环境变量并创建 CUR：

```bash
export CUR_REPORT_NAME=finops-cur-account-a
export CUR_BUCKET=<SOURCE_CUR_BUCKET>
export CUR_REGION=cn-northwest-1
bash scripts/create-cur.sh
```

### 2.3 Phase 2 跨账号角色

先生成随机 External ID，并在成员账号部署：

```bash
aws cloudformation deploy \
  --region cn-northwest-1 \
  --stack-name finops-member-read-role \
  --template-file infra/cloudformation/phase2-member-read-role.yaml \
  --parameter-overrides \
    CentralAccountId=<CENTRAL_ACCOUNT_ID> \
    CentralCollectorRoleName=FinOpsCollectorRole \
    ExternalId=<RANDOM_EXTERNAL_ID> \
  --capabilities CAPABILITY_NAMED_IAM
```

再在中央账号部署 Collector Role，并把输出的 Instance Profile 关联到宿主 EC2：

```bash
aws cloudformation deploy \
  --region cn-north-1 \
  --stack-name finops-central-collector \
  --template-file infra/cloudformation/phase2-central-collector-role.yaml \
  --parameter-overrides \
    MemberAccountRoleArn=<MEMBER_ROLE_ARN> \
    CentralBucketName=<CENTRAL_BUCKET> \
  --capabilities CAPABILITY_NAMED_IAM
```

## 3. 配置应用

```bash
cp .env.example .env
chmod 600 .env
```

替换 `.env` 中所有 `<...>`。不要把 `.env`、Key、密码、ARN 清单或客户数据提交到 Git。

创建 secrets：

```bash
sudo bash scripts/bootstrap-secrets.sh
docker build -t finops-aws-china:2.0.0 -f deploy/Dockerfile .
sudo bash scripts/hash-admin-password.sh
```

首次生成的管理员密码保存在 `/etc/finops-ai/admin_password`。只通过授权的主机管理会话读取，不写入文档。

## 4. 启动

```bash
sudo bash scripts/deploy.sh
curl -fsS http://127.0.0.1:8080/api/v1/auth/status
```

端口必须保持 `127.0.0.1:8080`。推荐通过 SSM 端口转发；SSH 隧道示例：

```bash
ssh -L 8080:127.0.0.1:8080 <HOST_USER>@<HOST_ADDRESS>
```

## 5. QuickSight CID

在北京区部署 `phase1-cid-deployment.yaml`，参数指向已脱敏的中央 Athena 视图。应用使用 `GenerateEmbedUrlForRegisteredUser`，因此 `.env` 中必须配置 Dashboard ID、QuickSight User ARN 和允许域名。

匿名嵌入需要额外购买读者会话容量并改用 `GenerateEmbedUrlForAnonymousUser`；本仓库默认不启用匿名公开分享。

## 6. 验证

```bash
python -m compileall -q app
docker compose --env-file .env -f deploy/docker-compose.yml config
sudo python3 tests/smoke-test.py
```

人工验证：

- 页面只显示 Linked Account A/B。
- 一个成员角色故障时另一个账号仍返回。
- CE/CUR 对账、CO 建议、异常状态、CID 和 AI 均有明确来源。
- 页面源代码和 API 响应不包含账号 ID、AK/SK 或模型 Key。

## 7. 更新和回滚

更新前备份 `/data/finops.db`，记录当前镜像摘要。构建新版本并执行 Smoke Test 后再切换。回滚时恢复上一镜像和兼容的 SQLite 备份；不要使用 `git reset --hard` 或覆盖运行数据。
