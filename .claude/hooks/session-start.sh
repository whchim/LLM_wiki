#!/usr/bin/env bash
TRIGGERS="$(find vault/_triggers -maxdepth 1 -name '*.md' 2>/dev/null | wc -l | tr -d ' ')"
if [ "$TRIGGERS" -gt 0 ]; then
  echo "【知识库触发队列】vault/_triggers/ 下有 $TRIGGERS 个未处理触发文件。请优先执行 /process-triggers 处理队列，处理完成后将触发文件移入 vault/_triggers/done/。"
fi
