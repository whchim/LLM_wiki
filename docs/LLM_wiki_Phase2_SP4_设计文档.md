# LLM Wiki 知识库平台 Phase 2 SP4「混合检索」设计文档

> **版本**：v0.1 ｜ **日期**：2026-08-28 ｜ **状态**：草案（待评审）
>
> **定位**：Phase 2 最后一个子项目。依据 [LLM_wiki_Phase2_路线图.md](LLM_wiki_Phase2_路线图.md) SP4；PRD v1.8。
>
> **D-embedding 已拍板（2026-08-28）**：阿里云 DashScope `text-embedding-v4`（1024 维，OpenAI 兼容接口）。

---

## 1. 目标与范围

**问题**：grep 只认字面——用户用"自己的话"提问（如"预警发布后怎么通知到人"）无法命中《叫应体系》；且"零命中"被记入知识缺口造成缺口数据污染（词汇不匹配 ≠ 知识缺失）。

**范围内**：
- `knowledge_entries` 加 `embedding vector(1024)` 列 + HNSW 索引（pgvector）
- `api/embedding.py`：DashScope embedding 客户端（批量、重试、降级）
- 向量回填：`/admin/backfill-embeddings`（全量条目补算，幂等）
- `/search` 升级双通道：grep 精确 + 向量语义 → 权重融合 re-rank
- **降级铁律**：embedding 服务故障 → 自动退化 grep-only（不崩、不阻塞）
- search_logs 记录通道来源（精确/语义/融合），为缺口判据重定义铺数据

**范围外**：图谱通道（Phase 3）、编译入库实时同步向量（本期用回填+定时补，避免拖慢编译）、re-rank 模型（用加权分数融合，无 cross-encoder）。

## 2. 技术决策

| # | 决策 | 选择 | 理由 |
|---|------|------|------|
| 1 | Embedding 模型 | DashScope `text-embedding-v4`（1024 维） | 用户拍板；已实测（维度/usage/区分度 OK）；OpenAI 兼容接口零 SDK 依赖 |
| 2 | Key 管理 | 环境变量 `DASHSCOPE_API_KEY`（.env，gitignore） | key 不进代码库 |
| 3 | 向量存储 | pgvector `vector(1024)` 列挂在 knowledge_entries + HNSW（cosine） | 与元数据同库 Join/过滤一体；不引入独立向量库（路线图决策 2） |
| 4 | 向量语义 | **向量 = 可重建缓存**（沿"数据库是缓存"铁律） | 模型换版/索引损坏 → backfill 全量重算即可，无权威数据风险 |
| 5 | 融合算法 | 加权分数：`score = 0.5×grep_rank + 0.3×vec_rank`（rank 归一化倒数） | 路线图初始权重（0.5/0.3/0.2 图谱留 Phase 3）；实现简单可解释 |
| 6 | 检索入口 | 升级现有 `GET /search`，加 `mode=auto\|grep\|vector` 参数 | 接口兼容（Streamlit 零改动），auto 为默认 |

## 3. Schema 变更

```sql
ALTER TABLE knowledge_entries ADD COLUMN IF NOT EXISTS embedding vector(1024);
CREATE INDEX IF NOT EXISTS idx_entries_embedding
    ON knowledge_entries USING hnsw (embedding vector_cosine_ops);
```

> pgvector 已启用（SP1）；ALTER 幂等（IF NOT EXISTS）。ensure_schema 顺带执行。

## 4. api/embedding.py（客户端）

```python
def embed_texts(texts: list[str]) -> list[list[float]]   # 批量；失败抛 EmbeddingError
def embed_query(text: str) -> list[float]                 # 单条便捷
def is_available() -> bool                                # DASHSCOPE_API_KEY 配置即 True
```
- 接口：`POST https://dashscope.aliyuncs.com/compatible-mode/v1/embeddings`
- 批量上限 10 条/请求（DashScope 限制）；429/5xx 指数退避重试 2 次
- **未配置 key → is_available()=False → /search 自动 grep-only（降级）**

## 5. 检索融合（search_router 升级）

```
GET /search?q=<query>&mode=auto
  1. grep 通道（现状逻辑）：命中文件 → 条目 rank 列表
  2. vector 通道（is_available 且 mode≠grep）：
     query 向量 → pgvector 余弦 Top-K（K=20，且过滤 status='active'）
     → 相似度 0~1 归一化
  3. 融合：score = 0.5×grep_score + 0.3×vec_score（rank 倒数归一化）
  4. search_logs.match_count = 融合后命中数；detail 记通道来源（trace detail 同步）
  5. 响应体加 "channels": {"grep": n1, "vector": n2}（可观测）
```

**缺口判据（SP5 联动）**：~~`match_count=0` 现在意味着"两通道都未命中"~~ → **v0.1.2 已落地修正**：`gap = grep 零命中 且 vector 最高相似度 < τ`（τ=0.52，由黄金集缺口/命中样本标定：缺口样本 max_sim∈[0.360,0.487]、命中下限 0.545，分隔区间中值；向量不可用自动退化为旧语义）。缺口以 `match_count=0` 写入 search_logs，看板缺口查询零改动；响应体与 trace detail 新增 `gap`、`max_sim` 字段。vector 单独命中而 grep 未命中的查询单独统计（detail.channel），为未来"词汇对齐词典"留数据。

## 6. 回填与运维

- `POST /admin/backfill-embeddings`（admin）：扫描 embedding IS NULL 的条目批量补算（每批 10 条），返回补算数；幂等可重复执行
- 响应体含 `remaining`（剩余未向量化条数），前端可轮询至 0

## 7. 测试计划

| 用例 | 断言 |
|------|------|
| embedding 客户端 | mock HTTP：成功返回 1024 维；429 重试；key 缺失 is_available=False |
| backfill | 向量列写入且维度 1024；幂等（二次执行 remaining=0） |
| /search mode=vector | 语义相近查询命中（"怎么通知到人" → 叫应体系类条目）；无关查询零命中 |
| /search 降级 | mock embedding 故障 → 自动 grep-only，不崩，响应 channels.vector=0 |
| 融合排序 | 两通道都命中的条目排前（0.5+0.3 > 单通道 0.5） |
| 回归 | 既有 95 用例绿 |

> 真实 API 的端到端语义验证做 smoke（不进 CI——不依赖外网）。

## 8. 退出标准（对齐路线图）
- [ ] 模糊/概念类查询命中（"怎么通知到人"→ 叫应体系）
- [ ] P99 < 3 秒 @ 1 万条目（pgvector HNSW + 批量 embedding）
- [ ] grep 精确匹配行为不回退（mode=grep 与旧版一致）
- [ ] embedding 故障自动降级 grep-only，服务不崩
- [ ] 缺口感知不稀释（双通道零命中才记缺口）

---

## Changelog

- **v0.1.2（2026-09-01）**：v0.1.1 勘误**落地闭环**。① 缺口判据实现：`/search` 加 `gap` 判定（grep 零命中 且 max_sim < τ），τ=0.52 由 `tools/tune_search.py` 标定（缺口样本 max_sim∈[0.360,0.487]、命中样本下限 0.545，分隔无重叠；取中值），向量不可用自动退化旧语义；缺口以 match_count=0 写日志，看板零改动。② 融合权重标定工具 `tools/tune_search.py`（网格扫描）：结论是 14 条黄金集上**权重不敏感**（MRR 全网格 1.00，vector 排序主导），默认 0.5/0.3 保留并抽为常量 W_GREP_DEFAULT/W_VEC_DEFAULT，扩集后重标定。③ 新增 4 个缺口判据测试（高/低相似度、退化、grep 短路）；`eval_search.py` 缺口检出力与线上判据对齐（输出逐条 max_sim）。评测复跑：缺口识别 3/3，MRR@10=1.00 / Recall@10=0.95 不回退。
- **v0.1.1（2026-09-01）**：新增检索离线评测集 `docs/检索评测_黄金集.md`（14 条：精确 6/语义 5/缺口 3）+ `tools/eval_search.py`（grep/vector/融合三通道对比，MRR@10、Recall@10、缺口检出力）。设计点：直接复用 `search_router` 检索原语、不经 `/search` 端点——评测查询不写 search_logs（不污染知识缺口看板）。为决策 5（融合权重 0.5/0.3）提供第一版离线依据。**完整首跑（PG + DashScope key）**：融合 MRR@10=1.00 / Recall@10=0.95 vs grep 0.22/0.46；语义改写查询 5/5 融合命中（grep 0/5）。**三个发现**：① 语义通道补上 grep 全部盲区；② `_grep` 只匹配正文不匹配文件名，"示例监测产品规格参数"查询漏检；③ **缺口判据被架空**——向量通道对缺口查询 3/3 误报命中，`match_count=0` 判据失效，需改相似度阈值（第 5 节勘误）。
- **v0.1（2026-08-28）**：初稿。D-embedding 拍板阿里云 text-embedding-v4（1024 维，已实测 key 可用、区分度 0.167）；决策：pgvector 同库向量列、向量=可重建缓存、加权融合 0.5/0.3（图谱 Phase 3）、故障降级 grep-only、backfill 幂等补算。