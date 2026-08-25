# 安全设计

## 身份与权限

- 宿主 EC2 通过 Instance Profile 获取临时凭据。
- 成员账号通过 STS AssumeRole 和 External ID 访问。
- 成员角色仅提供成本、优化、监控和资源清单读取权限。
- 应用没有资源变更、删除、购买 RI/SP 或更新预算的权限。

## 应用安全

- 登录密码使用 Argon2 哈希。
- Cookie 为 HttpOnly、SameSite=Strict；启用 HTTPS 时设置 `FINOPS_SECURE_COOKIE=true`。
- 登录失败有速率限制。
- 容器只读、删除 Linux capabilities，并启用 `no-new-privileges`。
- 默认端口只绑定 localhost。

## 数据保护

- 客户界面使用 Linked Account A/B。
- 账号、ARN 和资源 ID 在 API 返回前伪匿名化。
- AI 只接收最小必要的聚合数据。
- Docker secrets、`.env`、SQLite 和报告不能提交到仓库。

## QuickSight

- 默认使用 Registered User 临时嵌入 URL。
- Dashboard 使用聚合 Athena 视图，不直接暴露原始 CUR。
- 多客户匿名访问必须使用 Session Tags/RLS，并购买会话容量。
- 允许域名应使用精确 HTTPS 域名；生产环境不要使用通配符。

## 发布检查

提交前至少扫描：AWS Access Key、私钥、DeepSeek Key、12 位账号 ID、EC2 实例 ID、公网 IP、真实邮箱、Bucket 和内部域名。CI 使用 Gitleaks 检查提交历史。
