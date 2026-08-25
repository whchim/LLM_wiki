# LLM Wiki Phase 2 SP2「API 与安全」实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 以 FastAPI 建立「真正后端」：REST API 化上传/审核/搜索/管理端点 + JWT 认证（admin/user/reviewer 角色）+ 审计日志（audit_logs），并让 Streamlit 管理台接入登录、消费 API。Claude Code 仍是唯一 LLM 引擎（API 不调用 LLM，编译触发仍写触发文件）。

**Architecture:** 新增 `api/` FastAPI 应用（uvicorn，独立容器），复用 `streamlit_app/db.py` + `ops.py`（共享模块，不重构）；PyJWT HS256 + pwdlib[argon2]；审计为业务函数显式调用（非 ASGI 中间件）；users 表追加进 schema.sql；docker-compose 新增 `api` 服务并挂载同一 `./vault` 卷；Streamlit 经 `api_client.py` 消费 API。

**Tech Stack:** FastAPI、uvicorn、PyJWT、pwdlib[argon2]、Python 3.11+、psycopg3（复用）、Streamlit、pytest + TestClient、Docker Compose。

**Spec:** [LLM_wiki_Phase2_SP2_设计文档.md](LLM_wiki_Phase2_SP2_设计文档.md)（v0.1，本文档实现依据）；范围与需求依 [LLM_wiki_Phase2_路线图.md](LLM_wiki_Phase2_路线图.md)、[LLM_wiki_PRD.md](LLM_wiki_PRD.md) v1.8。

## Global Constraints

- 所有知识文件、目录名、Prompt 输出使用中文（UTF-8 全链路）；代码内注释保持中文
- 数据库是缓存，YAML 是规范源；API 内任何状态变更继续双写（YAML + PG），不一致时 YAML 为准
- `db.py` / `ops.py` 接口签名**保持不变**（共享模块，SP2 只在其上加 HTTP 层；Streamlit 不再直接调用写操作）
- 开发范式：SDD（API 输入输出可形式化）+ TDD（auth/审计/越权测试）；不引入 BDD
- 每次提交中文或英文 conventional commit 均可，提交信息描述实际变更
- 测试前置：真实 PostgreSQL（`docker compose up -d db`）——沿用 SP1 的 llmwiki_test 隔离库机制
- JWT_SECRET 必填（fail-fast）；初始管理员自动创建（ADMIN_INIT_USER/ADMIN_INIT_PASS）
- 错误文案复用 PRD 9.3 三要素（问题描述/可能原因/建议操作）

---

### Task 1: 依赖、Schema 与配置

**Files:**
- Modify: `requirements.txt`（+fastapi +uvicorn +PyJWT +pwdlib[argon2]）
- Modify: `schema.sql`（追加 users 表 DDL，幂等）
- Modify: `streamlit_app/db.py`（ensure_schema 增加建 users 表 + 初始管理员注入：users 空则按 ADMIN_INIT_USER/PASS 创建，argon2 哈希）
- Modify: `.env.example`（+JWT_SECRET +ADMIN_INIT_USER +ADMIN_INIT_PASS）

**Interfaces:**
- Produces: `users` 表可用、初始 admin 存在、依赖就绪

- [ ] **Step 1**: requirements.txt 追加 `fastapi`、`uvicorn[standard]`、`PyJWT>=2.8`、`pwdlib[argon2]`
- [ ] **Step 2**: schema.sql 追加 users 表（见设计文档 4.1 DDL）
- [ ] **Step 3**: db.py `ensure_schema()` 尾部加 `_ensure_admin()`：users 空 → 读 env（默认 admin/admin123）→ pwdlib 哈希插入
- [ ] **Step 4**: .env.example 补充 JWT_SECRET / ADMIN_INIT_USER / ADMIN_INIT_PASS 说明
- [ ] **Step 5**: 测试：conftest 重置后 assert users 有初始 admin；`docker compose up -d db` 环境下 pytest 相关用例绿

---

### Task 2: 认证模块 api/auth.py

**Files:**
- Create: `api/__init__.py`
- Create: `api/auth.py`

**Interfaces:**
- `create_access_token(username, role, expires_h=12) -> str`
- `verify_password(plain, hash) -> bool` / `hash_password(plain) -> str`
- `get_user(username) -> dict | None`（查 users 表）
- `get_current_user(credentials) -> User`（FastAPI Dependency；401 未登录/过期）
- `require_roles(*roles)`（返回 Dependency；403 越权）
- Pydantic: `User(id, username, role, display_name)`

- [ ] **Step 1**: api/auth.py 实现哈希（pwdlib argon2）与 JWT（PyJWT HS256，JWT_SECRET env，exp 12h）
- [ ] **Step 2**: 实现 get_user（psycopg 查 users）、get_current_user（decode → 查库确认存在）、require_roles 工厂
- [ ] **Step 3**: 单元测试挂接：test_auth.py——login 成功签发、密码错/用户不存在 401、/auth/me 回显角色

---

### Task 3: FastAPI 骨架 + 审计 api/main.py + api/audit.py

**Files:**
- Create: `api/schemas.py`（Pydantic：LoginRequest/LoginResponse/TaskOut/ReviewOut/SearchResult/...）
- Create: `api/audit.py`（`audit_log(operator, action, target_path, detail)` + FastAPI 依赖 `AuditDependency`）
- Create: `api/main.py`（FastAPI 实例、CORS、路由注册、healthz）

**Interfaces:**
- `GET /healthz`（无鉴权，容器健康检查用）
- 审计函数：写 audit_logs（operator 从当前用户取；失败 try/except 不阻断）

- [ ] **Step 1**: api/schemas.py 定义全部请求/响应模型（对齐设计文档 5.1 端点表）
- [ ] **Step 2**: api/audit.py 实现写入（psycopg INSERT audit_logs）
- [ ] **Step 3**: api/main.py：FastAPI 实例 + CORS（8501）+ 挂载 routers + /healthz
- [ ] **Step 4**: 冒烟：uvicorn 起服务，/healthz 200、/docs 可访问

---

### Task 4: 业务路由（upload / review / search / admin）

**Files:**
- Create: `api/routers/__init__.py`
- Create: `api/routers/auth_router.py`（POST /auth/login、GET /auth/me）
- Create: `api/routers/upload_router.py`（POST /uploads、GET /uploads/tasks、POST /uploads/tasks/{id}/retry）
- Create: `api/routers/review_router.py`（GET pending/rejected、POST approve/reject/resubmit/retry-ai）
- Create: `api/routers/search_router.py`（GET /search、/search/missed、/search/stats、/entries）
- Create: `api/routers/admin_router.py`（POST /admin/rebuild-index）

**Interfaces:**
- 全部复用 `streamlit_app/db.py` + `ops.py` 现有函数；写操作端点内调用 `audit_log`
- 上传：`ops._process_upload` 的等价实现（FastAPI 多文件 → RAW + 任务 + 触发文件）
- 审核 approve：`ops.approve_entry(review_id, old_path, new_path)`；reject：`ops.reject_entry`；resubmit：`ops.resubmit` + `ops.write_trigger`
- 搜索：请求参数 query → grep 语义（复用 app.py 逻辑）+ `db.insert_search_log`
- 角色：upload=user/admin；review=reviewer/admin；admin 端点=admin；search/entries=登录用户

- [ ] **Step 1**: auth_router（登录校验 + 签发；/auth/me）
- [ ] **Step 2**: upload_router（含 multipart 多文件、校验复用 ops.validate_upload、审计 action=upload）
- [ ] **Step 3**: review_router（approve/reject/resubmit/retry-ai，审计 action=review_approve/review_reject/...）
- [ ] **Step 4**: search_router（/search 落 search_logs；missed/stats 读聚合）
- [ ] **Step 5**: admin_router（rebuild-index，审计 action=rebuild_index）
- [ ] **Step 6**: TestClient 集成测试：test_api_upload.py / test_api_review.py / test_audit.py（覆盖 200/400/401/403/409）

---

### Task 5: Streamlit 接入（登录 + api_client + 页面改造）

**Files:**
- Create: `streamlit_app/api_client.py`
- Create: `streamlit_app/login.py`
- Modify: `streamlit_app/app.py`（登录态守卫 + 侧边栏用户/角色/退出）
- Modify: `streamlit_app/upload.py`、`streamlit_app/review.py`、`streamlit_app/growth.py`（直连 db/ops → api_client）

**Interfaces:**
- `ApiClient.login(username, password) -> dict`（静态）
- `ApiClient.get/post(...)`（带 Bearer token；403 抛权限异常、401 触发重新登录）
- `render_login()` → 登录表单；成功后写 `st.session_state["auth"]` + rerun

- [ ] **Step 1**: api_client.py（BASE 从 API_BASE env 读，默认 http://localhost:8000）
- [ ] **Step 2**: login.py 登录页 + app.py 守卫（无 auth → 登录页；有 → 主界面 + 退出按钮）
- [ ] **Step 3**: upload.py 改走 API（上传/任务表/重试）
- [ ] **Step 4**: review.py 改走 API（待审/已驳/操作）
- [ ] **Step 5**: growth.py + app.py 搜索改走 API
- [ ] **Step 6**: 手动链路验证：浏览器 8501 → 登录 → 上传 → 审核 → 搜索；审计表可见对应记录

---

### Task 6: 部署接线与回归

**Files:**
- Modify: `docker-compose.yml`（新增 api 服务：uvicorn 起 api.main:app，挂载 ./vault，depends_on db healthy；streamlit depends_on api）
- Modify: `Dockerfile`（requirements 已含新依赖，无实质改动；CMD 不变）
- Modify: `README.md`（API 服务说明 + 登录说明）

**Interfaces:**
- `api` 服务：端口 8000（本机映射）；环境变量 DB_* + JWT_SECRET + ADMIN_INIT_*
- Streamlit 容器：API_BASE=http://api:8000

- [ ] **Step 1**: docker-compose 新增 api 服务（镜像同 streamlit build；command=uvicorn；healthcheck /healthz）
- [ ] **Step 2**: streamlit 服务 env 加 API_BASE；depends_on 补 api
- [ ] **Step 3**: 全量测试：`python -m pytest tests -q`（既有 43 + SP2 新增全绿）
- [ ] **Step 4**: `docker compose up -d --build` 端到端：db healthy → api healthy → streamlit 可登录操作
- [ ] **Step 5**: README 更新（新增 API 服务、登录方式、初始管理员说明）

---

## 验收（对齐设计文档第 11 节退出标准）

- [ ] 全部管理操作可经 API 完成（上传/审核/搜索/重建索引）
- [ ] JWT 鉴权生效：未登录 401、越权 403（user→admin 端点）
- [ ] 审计日志覆盖核心写操作（upload/review_approve/review_reject/resubmit/rebuild_index/retry_compile）
- [ ] Streamlit 接入登录并消费 API（不再直连 PG 写操作）
- [ ] 新增测试 + 既有 43 用例全绿