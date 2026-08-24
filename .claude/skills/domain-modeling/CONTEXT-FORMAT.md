# CONTEXT.md 格式

## 结构

```md
# {上下文名称}

{一两句描述：这个上下文是什么、为什么存在。}

## 语言

**订单（Order）**:
{一两句对该术语的描述}
_避免_：采购单（Purchase）、交易（Transaction）

**发票（Invoice）**:
交付后发送给客户、请求付款的通知。
_避免_：账单（Bill）、付款请求（Payment request）

**客户（Customer）**:
下单的个人或组织。
_避免_：客户（Client）、买家（Buyer）、账号（Account）
```

## 规则

- **要有主见。** 当同一概念有多个词时，选定最佳的一个，把其余列入 `_避免_`。
- **定义要精炼。** 最多一两句。定义它**是什么**，而不是它做什么。
- **只收本项目上下文专属的术语。** 通用编程概念（超时、错误类型、工具模式）即使项目大量使用也不算。加术语前先自问：这是该上下文独有的概念，还是通用编程概念？只有前者才收。
- **当自然成簇时按小标题分组。** 若所有术语属于单一内聚领域，平铺列表即可。

## 单一 vs 多上下文仓库

**单一上下文（多数仓库）：** 仓库根放一个 `CONTEXT.md`。

**多上下文：** 仓库根放 `CONTEXT-MAP.md`，列出各上下文、它们的位置与相互关系：

```md
# 上下文地图

## 上下文

- [Ordering](./src/ordering/CONTEXT.md)：接收并跟踪客户订单
- [Billing](./src/billing/CONTEXT.md)：生成发票并处理付款
- [Fulfillment](./src/fulfillment/CONTEXT.md)：管理仓库拣货与发货

## 关系

- **Ordering → Fulfillment**：Ordering 发出 `OrderPlaced` 事件；Fulfillment 消费后开始拣货
- **Fulfillment → Billing**：Fulfillment 发出 `ShipmentDispatched` 事件；Billing 消费后生成发票
- **Ordering ↔ Billing**：共享 `CustomerId` 与 `Money` 类型
```

本 skill 推断采用哪种结构：

- 若存在 `CONTEXT-MAP.md`，读它以定位上下文
- 若只有根 `CONTEXT.md`，则为单一上下文
- 若两者皆无，在第一个术语敲定时惰性创建根 `CONTEXT.md`

当存在多个上下文时，推断当前主题与哪一个相关。不清楚就问。
