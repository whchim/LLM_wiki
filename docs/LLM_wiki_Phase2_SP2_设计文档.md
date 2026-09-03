# LLM Wiki 知识库平台 Phase 2 SP2「API 与安全」设计文档

> **版本**：v0.1 ｜ **日期**：2026-08-24 ｜ **状态**：草案（待评审）
>
> **定位**：Phase 2 子项目 SP2 的详细设计（可直接编码）。依据 [LLM_wiki_Phase2_路线图.md](LLM_wiki_Phase2_路线图.md) SP2 子项目；需求冲突时以 [LLM_wiki_PRD.md](LLM_wiki_PRD.md) v1.8 为准。
>
> **范围**：FastAPI REST API 化（上传/审核/搜索/管理端点）+ JWT 认证（管理员/普通用户）+ 审计日志中间件 + Streamlit 登录接入。

---

## 1. 目标与范围

**目标**：把「管理层」从 Streamlit 直连文件系统 + PG，升级为 **FastAPI 掌数据、Streamlit 掌表单**的两层结构——全部管理操作可经 REST API 完成，JWT 鉴权生效（越权被拒），写操作落审计日志。

**范围内**：
- FastAPI 应用（独立容器 `api`）：上传 / 审核 / 搜索 / 管理 四类 REST 端点
- JWT 登录 + 管理员（admin）/ 普通用户（user）两角色区分，用户表新增
- 审计日志中间件：写操作（上传/审核通过/驳回/重建索引/任务重试）落 `audit_logs`
- users 表（SP1 未建，SP2 补充 DDL）
- Streamlit 接入 JWT 登录并消费 API；保留 Streamlit 表单 UI（React 划入 Phase 3）

**范围外（后续 SP）**：增量编译 watcher（SP3）、混合检索算法（SP4）、巡检/演进/涌现（SP5）、多用户数据隔离与 RBAC（Phase 3）、React 前端（Phase 3）。

## 2. 技术选型与架构决策

**已拍板决策（路线图第五节，本设计落地）**：
1. **D-前端策略**：迭代 1-3 保留 Streamlit + JWT 登录；React 已划入 Phase 3。
2. **D-多用户写入**：登录 + 角色权限生效；普通用户上传经 API 校验写入 RAW，审核操作限审核者/管理员，越权拒绝。

**本设计补充决定**：

| 决策 | 选择 | 理由 |
|------|------|------|
| Web 框架 | **FastAPI** | PRD 技术选型（412 行）；类型提示 + 依赖注入天然适配 JWT 中间件；OpenAPI 文档免费 |
| 服务形态 | **独立容器 `api`**（uvicorn 运行），Streamlit 容器消费 | SP2 目标是"真正后端"，独立进程可独立扩缩，且 SP3/SP4 的 watcher/检索 API 直接挂此服务 |
| JWT 库 | **PyJWT**（`PyJWT>=2.8`） | PRD 930 行曾提 python-jose；PyJWT 维护活跃、API 简单，HS256 场景足够；不引 python-jose（其依赖 pycryptodome 较重） |
| 密码哈希 | **`pwdlib[argon2]`**（FastAPI 官方推荐现代替代 passlib） | passlib 已停止维护（2023）；argon2 是 OWASP 推荐算法 |
| 用户存储 | PG `users` 表（SP2 补 DDL） | 单库原则：与知识条目同库，无独立用户服务 |
| 认证实现 | FastAPI Dependency（`Depends(get_current_user)`） | 标准模式；无中间件黑盒，每个端点显式声明所需角色 |
| 审计实现 | Python 装饰器/依赖 `audit(action=...)` + 统一写入 | 比 ASGI 中间件更可控：可携带 `operator/action/target/detail` 业务语义；中间件只做请求日志 |
| 流式上传 | `UploadFile`（内存+落盘 RAW） | 复用 ops.py 现有校验/落盘逻辑，保持行为一致 |
| API 与 Claude Code 边界 | API **不调用 LLM**；编译触发仍写 `_triggers/` 触发文件（Claude Code 消费） | 沿用「Claude Code 掌 LLM」架构原则，SP2 不重造 Agent 编排 |

**关键架构约束**：FastAPI 复用 `streamlit_app/db.py` + `ops.py`（数据层与业务逻辑 GoVerned 层不动），只在其上包 HTTP 层。目录更名考虑：把 `streamlit_app/` 保留（Streamlit 仍在），新增 `api/` 目录存放 FastAPI 应用——`db.py`/`ops.py`/`rules.py` 作为共享模块被两方引用（通过 sys.path 或打包为 `llmwiki_core`）。本设计采用**轻量共享方案**：api 应用直接引用 `streamlit_app/` 下模块（同仓库，Python 路径注入，不引入包结构重构）。

## 3. 目录与文件变更

```
├── api/                         # 新增：FastAPI 应用
│   ├── __init__.py
│   ├── main.py                  # FastAPI 实例 + 路由注册 + CORS
│   ├── auth.py                  # JWT 签发/校验、密码哈希、get_current_user 依赖、角色依赖
│   ├── schemas.py               # Pydantic 请求/响应模型
│   ├── audit.py                 # 审计写入函数 + audit 依赖
│   └── routers/
│       ├── __init__.py
│       ├── auth_router.py       # POST /auth/login, GET /auth/me
│       ├── upload_router.py     # POST /uploads（普通用户+管理员）
│       ├── review_router.py     # GET/POST /reviews 系列（审核者/管理员）
│       ├── search_router.py     # GET /search, GET /search/missed
│       └── admin_router.py      # POST /admin/rebuild-index（管理员）
├── streamlit_app/
│   ├── app.py                   # 🔄 增加登录态；未登录跳登录页
│   ├── api_client.py            # 新增：Streamlit→API 的 HTTP 客户端（带 JWT）
│   ├── login.py                 # 新增：登录页（用户名/密码 → /auth/login）
│   └── upload.py / review.py / growth.py  # 🔄 直连 db 改为 api_client 调用
├── schema.sql                   # 🔄 追加 users 表 DDL（幂等）
├── docker-compose.yml           # 🔄 新增 api 服务（uvicorn）；streamlit depends_on api
├── Dockerfile                   # 🔄 相同镜像（依赖含 fastapi/uvicorn/pyjwt/pwdlib）
├── requirements.txt             # 🔄 +fastapi +uvicorn +PyJWT +pwdlib[argon2]
├── .env.example                 # 🔄 +JWT_SECRET +ADMIN_INIT_USER/PASS
└── tests/
    ├── conftest.py              # 🔄 加 JWT_SECRET env + users 表重置
    ├── test_auth.py             # 新增：登录/角色/越权
    ├── test_api_upload.py       # 新增：上传端点 + 审计落库
    ├── test_api_review.py       # 新增：审核端点 + 角色限制
    └── test_audit.py            # 新增：审计中间件
```

## 4. Schema 补充（users 表 + audit_logs 行为）

### 4.1 users 表（追加到 schema.sql）

```sql
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    username      TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,           -- argon2 hash
    role          TEXT NOT NULL DEFAULT 'user',   -- admin / user / reviewer
    display_name  TEXT,
    created_at    TEXT NOT NULL DEFAULT now()
);
```

> 角色说明：Demo→Phase 2 从「视角切换」进化为**真实角色**。角色枚举 `admin / reviewer / user`——映射 PRD 2.1 的 管理员/审核者/普通用户；知识消费者（只读）在 Phase 2 由 `user` 角色覆盖（读端点对全部登录用户开放），完整 RBAC 属 Phase 3。

### 4.2 audit_logs 行为定义（表已建，SP2 落行为）

| 字段 | 写规则 |
|------|--------|
| `operator` | 登录用户名（JWT sub）；未登录的系统动作写 `system` |
| `action` | 受控枚举：`login / upload / review_approve / review_reject / review_resubmit / rebuild_index / retry_compile / trigger_write` |
| `target_path` | 操作对象路径（RAW 路径 / NEXUS 路径 / 触发文件路径） |
| `detail` | JSONB：附加上下文（如驳回原因、上传文件列表、重建条目数） |
| `timestamp` | 默认 now() |

**写入方式**：`api/audit.py::audit_log(operator, action, target_path, detail)`——被各端点显式调用（业务审计），不搞 ASGI 中间件全量请求日志（噪音大、缺语义）。

## 5. API 路由设计

### 5.1 端点总表

| 方法 | 路径 | 角色 | 说明 | 对应现有逻辑 |
|------|------|------|------|-------------|
| POST | `/auth/login` | 公开 | 登录，返回 JWT | 新增 |
| GET | `/auth/me` | 登录用户 | 当前用户信息+角色 | 新增 |
| POST | `/uploads` | user/admin | 上传文档（多文件+分类）→ RAW + 编译任务 + 触发文件 | `ops._process_upload` |
| GET | `/uploads/tasks` | user/admin | 编译任务状态列表 | `db.list_recent_compile_tasks` |
| POST | `/uploads/tasks/{id}/retry` | user/admin | failed 任务重试 | upload.py 重试逻辑 |
| GET | `/reviews/pending` | reviewer/admin | 待审列表 | `db.list_pending_reviews` |
| GET | `/reviews/rejected` | reviewer/admin | 已驳回列表 | `db.list_rejected_reviews` |
| POST | `/reviews/{id}/approve` | reviewer/admin | 通过（移 NEXUS/概念 + 双写） | `ops.approve_entry` |
| POST | `/reviews/{id}/reject` | reviewer/admin | 驳回（status=draft + 原因） | `ops.reject_entry` |
| POST | `/reviews/{id}/resubmit` | reviewer/admin | 重新提交审核 | `ops.resubmit` |
| POST | `/reviews/{id}/retry-ai` | reviewer/admin | 重试 AI 审核（写 review 触发） | `ops.write_trigger` |
| GET | `/search` | 登录用户 | 搜索（grep 语义，落 search_logs） | app.py 搜索逻辑 + `db.insert_search_log` |
| GET | `/search/missed` | 登录用户 | 缺口 Top 20 | `db.top_missed_queries` |
| GET | `/search/stats` | 登录用户 | 搜索统计 | `db.search_stats` |
| GET | `/entries` | 登录用户 | 条目列表（分页/过滤） | `db` 查询 |
| POST | `/admin/rebuild-index` | **admin** | 重建索引 | `db.rebuild_index` |

### 5.2 响应约定

- 成功：`200`，JSON 数据直接返回；创建类返回 `201`。
- 错误：统一 `{"detail": "..."}`（FastAPI 默认）；业务错误用 `HTTPException`（400 参数错 / 401 未登录 / 403 越权 / 404 不存在 / 409 冲突）。
- 分页：`?limit=&offset=`，响应 `{"total": n, "items": [...]}`。

## 6. JWT 认证与权限设计

### 6.1 登录流程

```
POST /auth/login {username, password}
  → users 表查询 → pwdlib 校验 argon2 hash
  → 签发 JWT（HS256，payload: {sub: username, role, exp: now+12h}，JWT_SECRET 签名）
  → 返回 {access_token, token_type: "bearer", expires_in, role, display_name}
```

### 6.2 校验与授权

```python
# api/auth.py
def get_current_user(credentials=Depends(HTTPBearer())) -> User:
    payload = jwt.decode(credentials.credentials, JWT_SECRET, algorithms=["HS256"])
    ... 查 users 表确认仍存在且未禁用 → 返回 User

def require_roles(*roles) -> Dependency:   # 工厂：返回检查角色的依赖
    def checker(user=Depends(get_current_user)):
        if user.role not in roles:
            raise HTTPException(403, "无权限执行此操作")
    return checker
```

用法示例：
```python
@app.post("/admin/rebuild-index", dependencies=[Depends(require_roles("admin"))])
```

- **401**：token 缺失/过期/签名错。
- **403**：角色不符（如 user 调 admin 端点）。
- JWT_SECRET 从环境变量读（`.env.example` 提示生成）；Token 过期时间 12h（Demo/企业内网可接受，SP2 不做刷新机制，过期重新登录）。

### 6.3 初始用户

- 启动/自愈时（`ensure_schema` 扩展）：若 `users` 表为空，按 `ADMIN_INIT_USER/ADMIN_INIT_PASS`（默认 `admin/admin123`，.env 可改）创建初始管理员——保证 clone 即用。
- 密码策略：长度 ≥ 8；SP2 不做找回/改密 UI（管理员可通过直接操作 DB，或后续补）。

## 7. Streamlit 接入设计

### 7.1 登录态

- `streamlit_app/login.py`：登录表单 → 调 `POST /auth/login` → 成功将 `{token, role, display_name}` 存入 `st.session_state["auth"]`。
- `app.py` 启动检查：无 auth → 渲染登录页并停止；有 auth → 加载主界面，侧边栏显示当前用户+角色+[退出登录]。
- 用 `st.rerun()` 处理登录切换；JWT 存 session_state（不进 browser cookie，避免 XSS 面）。

### 7.2 API 客户端（api_client.py）

```python
class ApiClient:
    BASE = os.environ.get("API_BASE", "http://localhost:8000")
    def __init__(self, token): self.token = token
    def _headers(self): return {"Authorization": f"Bearer {self.token}"}
    def login(username, password) -> dict            # 静态方法，无 token 也可调
    def get(self, path, **params) -> dict
    def post(self, path, json=None, files=None) -> dict
```

- 全局唯一实例存 `st.session_state["api"]`。
- 错误处理：403 → 提示无权限；401 → 清理 auth 回登录页；网络错误 → 提示 API 不可达。

### 7.3 页面改造

| 页面 | 现状 | SP2 后 |
|------|------|--------|
| upload.py | 直连 db/ops | 调 `POST /uploads` + `GET /uploads/tasks` + retry 端点 |
| review.py | 直连 db/ops | 调 `/reviews/*` 系列 |
| growth.py | 直连 db | 调 `/search/missed` + `/search/stats` |
| app.py 搜索 | 直连 grep + db | 调 `GET /search` |

> 边界：Streamlit 不再直连 PG/文件系统写操作——统一走 API（审计才能覆盖）；**只读展示**（如看板统计）也可走 API。`db.py`/`ops.py` 变为被 API 引用的共享模块，Streamlit 不再 import 它们（除 api_client）。

## 8. 错误处理与安全边界

| 场景 | 处理 |
|------|------|
| 密码错误 | 401（不分「用户不存在/密码错」，防枚举） |
| token 过期 | 401 → Streamlit 清 auth 回登录页 |
| 上传越权/格式错 | 400 带 PRD 9.3 三要素文案（复用 ops.validate_upload） |
| 审核冲突（条目已被处理） | 409 提示刷新列表 |
| 并发双写 | 沿用 Demo 策略：YAML 权威、文件为准；API 内操作仍双写 YAML+PG |
| CORS | `api` 允许 `http://localhost:8501`（Streamlit 容器 origin），DEV 环境可全开 |
| 审计补漏 | 审计写入失败不阻断主操作（try/except + log），审计是增强不是强一致 |
| JWT_SECRET 缺失 | 启动时 fail-fast（开发）或警告+随机（仅文档用途）——**设计取 fail-fast**，避免生产裸奔 |

## 9. 测试计划

| 测试文件 | 覆盖 |
|----------|------|
| `tests/test_auth.py` | 登录成功/密码错/用户不存在（均 401）；/auth/me；无 token 401；user 调 admin 端点 403 |
| `tests/test_api_upload.py` | 上传成功（文件落 RAW + 任务入库 + 触发文件 + audit 行）；格式/大小校验 400；未登录 401 |
| `tests/test_api_review.py` | approve 流程（文件移动 + YAML 改 + 双写 + 审计）；reject 带原因；resubmit；user 角色调审核端点 403 |
| `tests/test_audit.py` | 各写操作 audit 行断言（operator/action/target/detail） |
| 回归 | 既有 43 用例保持绿（db/ops 未动，仅增 HTTP 层） |

**工具**：`fastapi.testclient.TestClient`（无独立服务器进程，直接跑 app）；conftest 提供：users 表初始化（含 admin）、JWT_SECRET env、测试库重置后注入初始用户。

## 10. 风险与缓解

| 风险 | 影响 | 缓解 |
|------|------|------|
| Streamlit 页面改造回归 | UI 断流 | 页面改造与 API 开发同 PR 合入；先 API 测试绿，再切 Streamlit 消费 |
| JWT 密钥泄露 | 伪造身份 | .env 管理 + fail-fast；HS256 内网可接受，Phase 3 评估 RS256 |
| 与 SP5 并行冲突（同改 YAML/文件） | 互相踩 | 沿用路线图边界：SP5 只走 YAML+审核流+Claude 轨；SP2 仅经 ops.py 既有函数操作文件 |
| 审计日志膨胀 | 表增长 | SP2 不设清理；SP5 顺带整理（done/ 归档、审计归档策略） |
| 上传在 API 容器、RAW 在共享卷 | 文件跨容器 | docker-compose 中 api 挂载同一 `./vault:/app/vault` 卷，路径一致 |

## 11. 退出标准（对齐路线图 SP2）

- [ ] 全部管理操作可经 API 完成（上传/审核/搜索/重建索引）
- [ ] JWT 鉴权生效：未登录 401、越权 403（含 user→admin 端点）
- [ ] 审计日志覆盖核心写操作（上传/审核通过/驳回/重提/重建索引/重试）
- [ ] Streamlit 接入登录并消费 API（不再直连 PG 写操作）
- [ ] 新增测试 + 既有 43 用例全绿

---

## Changelog

- **v0.1.1（2026-09-01）**：修复 `auth.py` 的 import 副作用违例——`_fail_fast_check()` 原在模块顶层执行（import 即抛 RuntimeError），任何未配置 JWT_SECRET 的工具/测试脚本无法导入 api 包（踩了 2026-08-24 故事一"被 import 的模块必须零副作用"的纪律）。改为：JWT_SECRET 运行时读取（函数内动态读 env，沿 KB_ROOT 同款惯例）+ `ensure_ready()` 在 encode/decode 前检查；服务侧 fail-fast 移入 `main.lifespan` 显式调用。行为不变（服务启动缺 key 仍即败），工具脚本不再被 import 阻塞。新增测试 `test_ensure_ready_raises_without_secret`。
- **v0.1（2026-08-24）**：初稿。依据路线图 SP2 + 已拍板决策（D-前端策略=保留 Streamlit+JWT；D-多用户写入=登录+角色权限生效）。决策：PyJWT+pwdlib[argon2]（替代 passlib）、audit 为业务函数非 ASGI 中间件、users 表 SP2 补 DDL、api 独立容器共享 streamlit_app 模块、ensure_schema 扩展初始管理员。错误处理防用户枚举（401 统一）。