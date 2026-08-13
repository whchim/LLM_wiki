#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
VAULT="$ROOT/vault"

# 1. 目录树（mkdir -p 幂等）
mkdir -p "$VAULT"/RAW/{个人_notes,会议,经验,项目}
mkdir -p "$VAULT"/pending_review
mkdir -p "$VAULT"/NEXUS/{资源,概念,研究}
mkdir -p "$VAULT"/_triggers/done

# 2. Reserved Files（已存在则跳过，不覆盖）
[ -f "$VAULT/NEXUS/index.md" ] || printf '# 知识库索引\n\n（编译时由 Claude Code 逐次更新）\n' > "$VAULT/NEXUS/index.md"
[ -f "$VAULT/NEXUS/log.md" ]   || : > "$VAULT/NEXUS/log.md"

# 3. SCHEMA.md（已存在则跳过）
if [ ! -f "$VAULT/SCHEMA.md" ]; then
  cat > "$VAULT/SCHEMA.md" << 'EOF'
# 知识库 Schema
## 1. 合法 Type 列表
- concept / resource / research / glossary
## 2. 合法 Status 列表
- draft / pending / active / stale / deprecated
## 3. 合法 Tags 命名空间
- 部门: 销售/售前/产品/实施交付/开发/财务/人事/行政/共享层
- 领域: AI/大数据/云计算/安全/项目管理/产品设计/应急管理/智慧城市/物联网/数字孪生
- 类型: 实战经验/技术方案/产品文档/会议纪要/复盘总结/行业研究/标准规范/培训材料
## 4. Frontmatter 字段规范（详见设计文档 4.2）
## 5. 文件名与 Wikilink 约定（详见设计文档 5.4）
## 6. 版本号规则
- 格式 V{major}.{minor}，首次入库 V1.0；正文微调 V1.1；核心定义改写 V2.0
EOF
fi

# 4. SQLite 建表（IF NOT EXISTS 幂等）
sqlite3 "$VAULT/meta.db" < "$ROOT/schema.sql"

echo "✅ 初始化完成。下一步：docker compose up -d 启动 Streamlit；Obsidian 打开 $VAULT"
