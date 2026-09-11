# 安全审查与修复记录

> 审查日期：2026-09-04  
> 范围：FastAPI、Streamlit 操作层、PostgreSQL 数据层、Claude Code watcher、编译/审核 workflow、Docker 配置。  
> 结论：项目已经具备 Agent Harness 工程化面试展示价值，但仍应定位为“可运行的 LLM 应用 Demo/原型”，不能直接宣称生产级平台。

## 风险分级

- **P0**：未授权访问、任意发布或可直接导致数据泄露/接管的问题。
- **P1**：高概率造成越权、资源耗尽、敏感内容发布或状态破坏的问题。
- **P2**：审计、可观测性、部署加固和一致性缺口。

## 修复记录

| 优先级 | 问题与证据 | 修复状态 | 验证 |
|---|---|---|---|
| P0 | `/search` 原依赖把 `auth.get_current_user` 当普通默认参数，OpenAPI 将 `user` 暴露为 query 且无 security，导致未登录可检索。 | **已修复**：`trace()` 使用 `Depends(auth.get_current_user)`。 | 新增未登录 401 测试；OpenAPI security 由依赖生成。 |
| P1 | watcher 默认 `bypassPermissions`。恶意文档可通过 prompt injection 诱导无人值守 Agent 执行高风险操作。 | **已修复默认值**：默认 `acceptEdits`；仅显式 `WATCHER_PERMISSION_MODE=bypassPermissions` 才放开。生产仍应配置 allowedTools、容器沙箱和最小文件权限。 | watcher 启动配置校验；代码命令参数受环境变量控制。 |
| P1 | JWT secret、管理员初始口令有开发默认值。 | **已加启动门禁**：`APP_ENV=production` 时拒绝默认/过短 JWT secret，拒绝 `admin123` 或少于 12 字符的初始口令。 | auth/db 单元逻辑可独立验证；生产启动会 fail-fast。 |
| P1 | 上传先完整读入内存、批量无上限、文件名可过长、同名文件直接覆盖。 | **已修复**：1MB 分块读取，单文件 10MB、单批 20 个/50MB、文件名长度和控制字符校验；使用 `xb` 原子创建拒绝覆盖。 | 新增同名覆盖回归测试。 |
| P1 | grep 遍历 NEXUS 不过滤 YAML status，draft/stale 文件可被返回。 | **已修复**：只接受可解析且 `status: active` 的 frontmatter。 | 新增 draft 排除测试。 |
| P1 | 搜索 query 无长度/模式约束，可能触发全量扫描和 embedding 外部调用滥用。 | **部分修复**：query 最大 300 字符，mode 枚举约束；仍缺少生产级 rate limit、缓存和倒排索引。 | 新增超长 query 422 测试。 |
| P1 | 资源摘要免审核直接 active，可能绕过敏感检查。 | **流程已加门禁要求**：workflow 明确资源也必须执行完整性/敏感扫描，blocked 不得 active。 | 需在 Claude Code 消费实现中落实并做端到端验收。 |
| P1 | LLM 审核输出只阻断 blocked/insufficient/duplicate/低质量，warning、flagged、多 concerns 仍可 approved。 | **已修复**：输出契约校验增加三类一致性规则，要求人工复核。 | 新增 3 组回归测试。 |
| P1 | rejected 记录也可进入 approve，文件/DB/YAML 操作无状态条件保护。 | **部分修复**：approve/reject 只允许 `human_decision IS NULL + entry_status=pending`；resubmit 只允许 rejected/draft，冲突返回 409。 | 新增 rejected 不可 approve 测试；真正数据库行锁/补偿事务仍待生产化。 |
| P2 | `backfill_embeddings` 不在审计 action 枚举中，日志被静默忽略。 | **已修复**：补入 `ACTIONS`。 | 现有 backfill 审计测试可覆盖。 |
| P2 | API/Streamlit/PG 默认监听并映射公网风险。 | **已加固默认 Compose**：端口绑定 `127.0.0.1`；生产仍应由反向代理提供 HTTPS。 | compose 配置审阅。 |

## 尚未解决的生产化缺口

1. 上传、搜索仍需要网关级认证限流、请求体限制和并发控制；搜索 grep 每次全量扫描，规模扩大后应改为数据库/倒排索引。
2. 审核文件移动、YAML 修改和数据库更新不是跨资源事务；需要数据库条件更新/锁、失败补偿和幂等操作。
3. Claude Code watcher 的 `acceptEdits` 不是完整沙箱。生产环境应限制 allowedTools、工作目录、网络访问和挂载目录，并把不可信文档视为攻击输入。
4. 数据库凭据、TLS、密钥轮换、备份恢复、依赖漏洞扫描和 CI 安全门禁仍属于部署工作。
5. 测试当前依赖 PostgreSQL；本机全量执行曾被 Windows `_isolated`/`__pycache__` 文件占用阻断，不能把该环境问题误报为业务测试失败。

## 校招 Agent 岗位面试定位

**可以满足校招 Agent/LLM 应用工程岗位的面试需求**，建议表述为：

> 我实现了一个带 Agent Harness 的企业知识库应用：用触发文件驱动编译/审核 workflow，确定性规则与 LLM 分工，输出契约校验、重试、断点续跑、审计、trace、混合检索和评测闭环；近期又补齐了鉴权、敏感信息、上传资源限制和审核状态机边界。

面试时应主动说明：这是工程化 Demo/原型，生产化还需要沙箱、限流、事务一致性、密钥治理和可用性建设。README 与历史项目经历文档中的测试/数据规模口径也需要统一后再对外展示。

