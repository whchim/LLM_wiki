# /ask <问题> —— 检索知识库并生成带引用答案

1. 在 vault/NEXUS/ 下 grep 检索问题关键词，取匹配行数 Top-5 的 .md 文件
2. cat 读取这 5 个文件全文（含 YAML Frontmatter）
3. 按 prompts/answer_prompt.md 执行答案生成
4. 将 (query, match_count) 写入 vault/meta.db 的 search_logs 表（source='claude_code'）
5. 向用户呈现答案（引用来源为可点击的 Vault 相对路径）
