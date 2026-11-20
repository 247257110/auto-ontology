# PQL 业务预测场景清单

面向 `retail_churn` 库（customers / orders / order_lines / invoices / payments /
products / regions / support_tickets / web_sessions）整理的常用业务预测。
清单里的 28 条 PQL 已全部通过 `compile_pql`、`validate_pql_static` 和托管端点的
`validate_pql` 静态校验；其中 7 条做过真实托管预测冒烟。

## 使用前提

- 预测窗口 `(start, end, unit]` 是**相对锚点**的前向窗口：`0, 90, days` 就是
  「锚点之后 90 天」。锚点默认是今天。
- 任务类型由 target 决定：
  - `COUNT/SUM/AVG/MIN/MAX(...)` → 回归（预测数值）；
  - 再套 `> 0` / `= 0` / `>= N` → 二分类（输出 `True_PROB` / `False_PROB`）；
  - 直接一个分类列 → 多分类（输出每类 `<类别>_PROB` 与 `TARGET_PRED` 类别）；
  - `LIST_DISTINCT ... RANK TOP k`（推荐/链接预测）和 `FORECAST n TIMEFRAMES`
    **托管端点不支持**，清单里不收录。
- 聚合表必须与预测实体有**直接外键**。例如 `order_lines` 不能直接聚合到
  `customers`（要经过 `orders`），但可以聚合到 `orders` 或 `products`。
- 聚合内部的 `WHERE` 只能过滤被聚合的那张表，例如
  `COUNT(orders.* WHERE orders.channel = 'web', ...)`；跨表条件请放到实体筛选 SQL。
- **`FOR EACH ... WHERE <实体属性>` 在托管路径不会下发**。要限定客户群/开放发票/
  在售商品，请用查询页的实体筛选 SQL（或 UI 的范围选择），不要只写在 PQL 的 WHERE 里。
- 单次最多打分 1,000 个实体（`invoices` 14k 行、`orders` 15k 行时需要先限定范围）。
- 回归输出可能为负（例如订单金额预测出现 `-357`），展示时建议截断到 0，或改用
  二分类阈值（`>= 10000`）表达业务问题。

## 一、客户经营（entity: `customers.customer_id`）

| # | 业务问题 | PQL | 输出 |
|---|---|---|---|
| 1 | 未来 90 天是否会下单 | `PREDICT COUNT(orders.*, 0, 90, days) > 0 FOR EACH customers.customer_id` | 下单概率 |
| 2 | 未来 90 天下单次数 | `PREDICT COUNT(orders.*, 0, 90, days) FOR EACH customers.customer_id` | 次数 |
| 3 | 未来 90 天订单金额 | `PREDICT SUM(orders.order_total, 0, 90, days) FOR EACH customers.customer_id` | 金额 |
| 4 | 未来 90 天是否达到大额采购（≥1 万） | `PREDICT SUM(orders.order_total, 0, 90, days) >= 10000 FOR EACH customers.customer_id` | 概率 |
| 5 | 未来 90 天是否走线上渠道下单 | `PREDICT COUNT(orders.* WHERE orders.channel = 'web', 0, 90, days) > 0 FOR EACH customers.customer_id` | 概率 |
| 6 | 未来 90 天零发票（流失/未开票风险） | `PREDICT COUNT(invoices.*, 0, 90, days) = 0 FOR EACH customers.customer_id` | 概率 |
| 7 | 未来 90 天开票金额 | `PREDICT SUM(invoices.amount, 0, 90, days) FOR EACH customers.customer_id` | 金额 |
| 8 | 未来 90 天是否会提工单 | `PREDICT COUNT(support_tickets.*, 0, 90, days) > 0 FOR EACH customers.customer_id` | 概率 |
| 9 | 未来 90 天高优先级工单数 | `PREDICT COUNT(support_tickets.* WHERE support_tickets.priority = 'High', 0, 90, days) FOR EACH customers.customer_id` | 次数 |
| 10 | 未来 90 天平均满意度（CSAT） | `PREDICT AVG(support_tickets.csat, 0, 90, days) FOR EACH customers.customer_id` | 分值 |
| 11 | 未来 90 天是否会访问网站 | `PREDICT COUNT(web_sessions.*, 0, 90, days) > 0 FOR EACH customers.customer_id` | 概率 |
| 12 | 未来 90 天报价请求数 | `PREDICT SUM(web_sessions.quote_requests, 0, 90, days) FOR EACH customers.customer_id` | 次数 |
| 13 | 未来 90 天购物车放弃次数 | `PREDICT SUM(web_sessions.cart_abandonments, 0, 90, days) FOR EACH customers.customer_id` | 次数 |
| 14 | 客户分层识别（分类，不是时间预测） | `PREDICT customers.segment FOR EACH customers.customer_id` | 类别 |

**备注**

- #1 是「下次下单倾向」，适合做销售跟进排序；#6 是「未来一张发票都没有」，
  适合做流失/沉默预警，两者口径不同，不要混用。
- #6 要排除「从未开过发票」的客户（`retail_churn` 里有 32 个）。系统在预测链路里
  会自动排除；自己写 SQL 评估时也要先排除，否则它们会把风险名单占满。
- #14 的标签是当前 `segment`，用于校验/补全分层，不是预测分层变化。

## 二、发票与回款（entity: `invoices.invoice_id`）

| # | 业务问题 | PQL | 输出 |
|---|---|---|---|
| 15 | 未来 90 天该发票是否会被支付 | `PREDICT COUNT(payments.*, 0, 90, days) > 0 FOR EACH invoices.invoice_id` | 回款概率 |
| 16 | 未来 90 天该发票回款金额 | `PREDICT SUM(payments.amount, 0, 90, days) FOR EACH invoices.invoice_id` | 金额 |

**备注**：一定要先把范围限定到**未结清发票**（`invoices.status = 'open'`，当前
1,518 张）。对全部 14k 张历史发票打分时，绝大多数早已付清，未来 90 天自然没有
回款，概率会接近 0——那是范围错了，不是模型坏了。

## 三、订单履约（entity: `orders.order_id`）

| # | 业务问题 | PQL | 输出 |
|---|---|---|---|
| 17 | 未来 90 天该订单是否会被开票 | `PREDICT COUNT(invoices.*, 0, 90, days) > 0 FOR EACH orders.order_id` | 开票概率 |
| 18 | 未来 90 天该订单开票金额 | `PREDICT SUM(invoices.amount, 0, 90, days) FOR EACH orders.order_id` | 金额 |
| 19 | 未来 90 天该订单是否会有退货 | `PREDICT SUM(order_lines.returned_qty, 0, 90, days) > 0 FOR EACH orders.order_id` | 退货概率 |
| 20 | 未来 90 天该订单退货数量 | `PREDICT SUM(order_lines.returned_qty, 0, 90, days) FOR EACH orders.order_id` | 数量 |

**备注**：范围应限定到近期订单（如近 90 天或 `status = 'pending'`），不要对全部
15k 历史订单打分。

## 四、商品（entity: `products.product_id`）

| # | 业务问题 | PQL | 输出 |
|---|---|---|---|
| 21 | 未来 90 天该商品是否动销 | `PREDICT COUNT(order_lines.*, 0, 90, days) > 0 FOR EACH products.product_id` | 动销概率 |
| 22 | 未来 90 天该商品销量 | `PREDICT SUM(order_lines.quantity, 0, 90, days) FOR EACH products.product_id` | 件数 |
| 23 | 未来 90 天该商品销售额 | `PREDICT SUM(order_lines.line_total, 0, 90, days) FOR EACH products.product_id` | 金额 |
| 24 | 未来 90 天该商品退货量 | `PREDICT SUM(order_lines.returned_qty, 0, 90, days) FOR EACH products.product_id` | 件数 |

**备注**：范围限定到在售商品（`discontinued_on IS NULL OR discontinued_on > today`），
停产商品没有未来动销。

## 五、区域（entity: `regions.region_id`）

| # | 业务问题 | PQL | 输出 |
|---|---|---|---|
| 25 | 未来 90 天区域订单量 | `PREDICT COUNT(orders.*, 0, 90, days) FOR EACH regions.region_id` | 单量 |
| 26 | 未来 90 天区域销售额 | `PREDICT SUM(orders.order_total, 0, 90, days) FOR EACH regions.region_id` | 金额 |

## 六、窗口变体

| # | 业务问题 | PQL |
|---|---|---|
| 27 | 未来 30 天是否下单 | `PREDICT COUNT(orders.*, 0, 30, days) > 0 FOR EACH customers.customer_id` |
| 28 | 未来 180 天是否下单 | `PREDICT COUNT(orders.*, 0, 180, days) > 0 FOR EACH customers.customer_id` |

窗口单位还支持 `weeks` / `months`，例如 `0, 1, months` 表示未来一个月。窗口越长，
正例率越高，概率分布越平；做运营名单时建议先看阈值对应的实际覆盖人数。

## 已验证结果

- 28/28 条通过静态校验（`compile_pql` + `validate_pql_static` + 托管 `validate_pql`）。
- 7 条真实托管冒烟：

| 场景 | 实体数 | 输出范围 |
|---|---|---|
| 客户-订单金额 | 600 | -357.7 ~ 280,900 |
| 客户-高优工单数 | 600 | 0.015 ~ 0.685 |
| 客户-分层分类 | 600 | 三类概率 + `TARGET_PRED` 类别 |
| 发票-回款概率（未限定范围） | 1,000（触发单次上限） | 0.00002 ~ 0.0024 |
| 发票-回款金额（未限定范围） | 1,000 | -79.52 ~ 62.81 |
| 商品-销量 | 140 | 1,760 ~ 5,273 |
| 区域-销售额 | 5 | 347 万 ~ 443 万 |

- 「未来 90 天零发票」在 3 个历史锚点回测：AUC 0.830 / 0.840 / 0.834，
  `p >= 0.5` 的精确率 71% / 78% / 74%，基准率 40% / 46% / 42%。

## 示例库

以上 28 条已写入 **PQL Analyses**（页面 `/analysis` 的 PQL 标签页，接口
`/api/pql-analyses`），并进入语义索引作为 PQL 生成的 few-shot 样例。
示例库只保留 retail 口径（`customers.customer_id`），原先 6 条 WWI 口径
（`customerid`、`stockitemtransactions`、`customercategoryid`）已移除，避免
生成时把主键拼写带偏。

检索实测：

| 用户问法 | 命中示例 |
|---|---|
| 未来 90 天一张发票都不会有的客户是哪些？ | 哪些客户未来 90 天会完全没有开票？ |
| 下个季度哪些商品会卖得最多？ | 这个商品未来 90 天预计卖多少件？ |
| 哪些发票下个月能回款？ | 这张发票未来 90 天会不会回款？ |

## 落地建议

1. **先限定实体范围**：开放发票、近 90 天订单、在售商品、活跃客户。范围错了，
   概率再准也没有业务意义。
2. **先回测再上生产**：同一 PQL 在 3–5 个历史锚点上算 AUC / 精确率，和基准率比较；
   概率是排序分数，不是校准过的绝对风险。
3. **阈值按名单容量定**：运营每天能跟进 50 家，就取概率 Top 50，而不是固定 0.5。
4. **二分类优先于回归**：业务问题如果能写成「是否 ≥ N」，通常比回归更稳、更好解释；
   回归结果记得截断负值。
5. **状态字段慎用**：`orders.status` / `invoices.status` 是最终状态，历史回测里
   用它们做标签会引入未来信息；渠道、优先级、品类这类锚点时刻已知的字段更安全。
