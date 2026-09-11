# 销售事实澄清 Agent：Vue 3 工作台

> 状态：已实现展示层迁移 ｜ 范围：阶段 5 工作台

## 1. 目标

Vue 3 + Vite 前端替换原 Streamlit 的销售事实澄清展示层，提供更适合销售、审核者和负责人的角色化工作台。前端只消费 FastAPI，不直连 PostgreSQL。

## 2. 页面范围

- 登录：JWT 登录，令牌仅保存在浏览器本地存储。
- 总览：待澄清会话、我的会话、待负责人确认建议和近期信号。
- 销售澄清：提交脱敏纪要、查看 Agent 事实与真实追问、按 `question_id` 追加回答。
- 负责人审核：查看建议状态、置信度、风险、证据引用和客户状态事件时间线，确认或驳回建议。

## 3. 边界不变

- Agent 只能抽取事实、识别缺口并提出追问，不能直接更新客户状态。
- 状态机、权限、审计、幂等和敏感数值隔离仍由 FastAPI 与确定性服务负责。
- 原纪要、Agent 输出和销售回答保持追加语义，前端没有静默覆盖入口。
- 精确金额、预算、报价和数量不进入模型上下文，仍由受限数值表管理。

## 4. 启动

```powershell
# API
uvicorn api.main:app --port 8000

# Vue
cd frontend
npm install
npm run dev
```

默认 Vue 地址为 `http://localhost:5173`，API 地址为 `http://localhost:8000`。可通过 `VITE_API_BASE` 覆盖 API 地址。后端默认 CORS 同时允许 `8501` 和 `5173`，生产环境应使用 `CORS_ORIGIN` 收紧来源。

## 5. 当前未宣称的能力

Vue 只改善交互和展示，不代表真实模型供应商已经绑定，也没有将 `ready_for_proposal` 自动转换为 `StateProposal`。真实业务准确率、并发容量和合规仍需试点验证；本项目不使用 synthetic oracle 冒充生产效果。
