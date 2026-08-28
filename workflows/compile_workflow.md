# 批量编译 Workflow

**触发**：/process-triggers 消费 compile_*.md 时执行。

## 输入
- 待编译 RAW 路径列表（来自触发文件）

## 步骤
1. 对每个 RAW 路径：
   a. 计算 SHA256：`sha256sum "vault/<raw_path>"`
   b. 查缓存：`sqlite3 vault/meta.db "SELECT id FROM compile_tasks WHERE raw_path='<path>' AND fingerprint='<hash>' AND status='done' ORDER BY id DESC LIMIT 1"`
      - 命中 → 更新该任务记录为 cached（`INSERT` 新行 status='cached' 亦可），跳过 LLM
   c. 未命中：
      - 非 .md/.txt：用 Python 提取文本（pypdf / python-docx，本机无 Python 时报错并标记 failed）
      - 读全文 → 执行 prompts/compile_prompt.md → 解析 JSON（失败自动重试 1 次）
      - 按设计文档 5.3/5.4 落盘：
        - 资源摘要 → `NEXUS/资源/<标题>.md`（YAML: type=resource, status=active, fingerprint, source=<raw_path>）
          （资源摘要免审直接 active 入库（设计文档 4.4），概念页才进审核）
        - 概念页 → `pending_review/<概念名>.md`（YAML: type=concept, status=pending, source）
      - 更新 index.md（资源节，幂等追加；同时维护头部统计行 `> 资源 N 篇 · 概念 M 个 · 最后更新 YYYY-MM-DD`，见设计文档 5.5）
      - `sqlite3` upsert knowledge_entries（资源 active + 概念 pending）
      - 更新 compile_tasks 状态 done/failed（error_msg 记录失败原因）
2. 全部完成后写 `_triggers/review_<ts>.md`（本批所有概念页路径），供审核阶段消费
3. 返回：编译 N 个、缓存 M 个、失败 K 个

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
- 触发文件处理完毕移入 vault/_triggers/done/
- 编译结果已采集（compile_session trace 落库）
