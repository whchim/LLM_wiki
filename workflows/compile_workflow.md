# 批量编译 Workflow

**触发**：/process-triggers 消费 compile_*.md 时执行。

## 输入
- 待编译 RAW 路径列表（来自触发文件）

## 步骤
1. **断点续跑去重（SP3，先于一切编译动作）**：对纸条内每个 RAW 路径，查
   PostgreSQL（`psql "$DB_DSN"` 或 python psycopg；连接参数同环境变量 DB_HOST/DB_PORT/DB_NAME/DB_USER/DB_PASS）：
   ```sql
   SELECT status, fingerprint, started_at FROM compile_tasks
   WHERE raw_path='<path>' ORDER BY id DESC LIMIT 1;
   ```
   - `done` 且指纹与当前文件 SHA256 相同 → **跳过**（输出"已编译，跳过"，计入 skipped）
   - `cached` → 跳过（计入 cached）
   - `processing` 且 started_at 距今 < 30 分钟 → 跳过（另一会话可能在跑）
   - 无记录 / `failed` / `pending` / `processing` 超时（僵尸） / `done` 但指纹已变 → 进入编译
2. 对每个待编译路径：
   a. 计算 SHA256：`python -c "import hashlib,sys;print(hashlib.sha256(open(sys.argv[1],'rb').read()).hexdigest())" "vault/<raw_path>"`
      （或 `sha256sum`；Windows 用 Python 方式）
   b. 置状态：`UPDATE compile_tasks SET status='processing', started_at=now() WHERE id=<该路径最新任务id>`
   c. 未命中缓存：
      - 非 .md/.txt：用 Python 提取文本（pypdf / python-docx，本机无 Python 时报错并标记 failed）
      - 读全文 → 执行 prompts/compile_prompt.md → 解析 JSON（失败自动重试 1 次）
      - **输出契约校验（质量门禁进 loop）**：把编译 JSON 交给
        `python tools/validate_llm_output.py compile -`（stdin）自检（等价于
        output_schema.validate_compile_output：resource 必填/枚举、summary 章节、
        concepts 标题唯一/四章节）：
        - 合法 → 继续落盘
        - 不合法 → 按违例清单重试 1 次；仍不合法 → 标记 failed + error_msg（不落盘）
      - 按设计文档 5.3/5.4 落盘：
        - 资源摘要 → `NEXUS/资源/<标题>.md`（YAML: type=resource, status=active, fingerprint, source=<raw_path>）
          （资源摘要免审直接 active 入库（设计文档 4.4），概念页才进审核）
        - 概念页 → `pending_review/<概念名>.md`（YAML: type=concept, status=pending, source）
      - 更新 index.md（资源节，幂等追加；同时维护头部统计行 `> 资源 N 篇 · 概念 M 个 · 最后更新 YYYY-MM-DD`，见设计文档 5.5）
      - upsert knowledge_entries（资源 active + 概念 pending；PostgreSQL，经 db.py 或等价 SQL）
      - 更新 compile_tasks 状态 done/failed/cached（error_msg 记录失败原因）
3. 全部完成后写 `_triggers/review_<ts>.md`（本批所有概念页路径），供审核阶段消费
4. 返回：编译 N 个、缓存 M 个、跳过 S 个、失败 K 个

## 可观测性（SP2.5）：采集本次编译结果

编译会话结束时（步骤 2 之后、返回之前），**汇总本批结果并交给采集**：

- 统计：`compiled`（LLM 编译成功页数）、`cached`（指纹缓存命中数）、`failed`（失败数）、
  `files`（本批产出文件路径 JSON 数组）、`latency_ms`（本次编译会话耗时）
- 方式：把上述计数与清单写入一个临时 JSON 文件 `vault/_triggers/.compile_trace_<ts>.json`，
  **/process-triggers 命令的兜底采集会读取/使用它**（若命令侧未自动执行，可手动调用：

  ```
  python tools/record_compile_trace.py --trace-id <uuid> --compiled N --cached M \
      --failed K --files '<json>' --latency-ms <ms>
  ```

- 产出 JSON 格式：
  ```json
  {"trace_id": "<uuid>", "compiled": 5, "cached": 3, "failed": 0,
   "files": ["NEXUS/资源/a.md", "pending_review/概念b.md"], "latency_ms": 12000}
  ```

## 输出验收
- 每个成功文件：NEXUS/资源/ 有 1 个资源摘要；概念页在 pending_review/，YAML 四必填字段齐全
- **每个成功文件均通过 compile 契约校验**；落盘 frontmatter 可用
  `python tools/validate_llm_output.py frontmatter <file>` 复核（type/status/version/tags 命名空间）
- 触发文件处理完毕移入 vault/_triggers/done/
- 编译结果已采集（compile_session trace 落库）
