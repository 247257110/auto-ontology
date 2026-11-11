<!--
SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# retail_churn：一份真的会流失客户的业务库

WideWorldImporters 是 OLTP 样本库，不是客户关系史：几乎每个客户每季度都下单，
`COUNT(orders.*, 0, 90, days) = 0` 的正例率只有 3%，而且集中在客户关系的头几个月。
拿它验证流失预测，只能验证出"模型学会了先验"。

这个库补上缺的那部分：600 家 B2B 客户、四年半的订单、发票、回款、工单和网站行为，
以及一个**有四种原因、留下不同痕迹**的流失过程。

```
database  retail_churn
schemas   retail   业务表（可查、可问答）
          scoring  流失真值（评分用，模型不可见）
```

---

## 1. 一句话说清它长什么样

一家做食品原料、包装、耗材、生鲜四类货品的批发分销商，五个大区、十六个城市、
六百家签约客户。客户按规模分 SMB / 中型 / 大型，按合同分月度 / 年度 / 单次采购，
六成开通了线上下单门户。

| 表 | 行数 | 一行是什么 |
|---|---:|---|
| `retail.regions` | 5 | 销售大区 |
| `retail.customers` | 600 | 签约客户主数据 |
| `retail.products` | 140 | 产品目录（四类、牌价、成本、上下架） |
| `retail.orders` | 15,019 | 订单头 |
| `retail.order_lines` | 75,667 | 订单明细行 |
| `retail.invoices` | 14,398 | 应收单（按订单开具） |
| `retail.payments` | 12,880 | 回款记录 |
| `retail.support_tickets` | 9,205 | 服务工单（含 CSAT） |
| `retail.web_sessions` | 12,258 | 门户月度行为汇总 |
| `scoring.churn_events` | 600 | **流失真值**：最后下单日、原因、衰退起点 |

时间跨度 2021-01-01 ~ 2025-06-30。累计营收 2.80 亿（年均 6,200 万），客单价中位数 5,823 元、
P95 77,317 元。回款逾期率 14.3%，平均提前 4.1 天结清。

另有两个视图便于直接查：`v_customer_revenue_monthly`（客户月度营收）、`v_customer_last_order`（最后下单日）。

---

## 2. 流失机制：四种原因，四种痕迹

真实的客户流失不是一种现象。这里按 B2B 分销商的实际构成给了四种：实际流失 207 家，
成因分布如下。

| 原因 | 占比 | 流失前在数据里留下什么 | 可预测性（实测 AUC） |
|---|---:|---|---|
| `competitor` 被竞品替换 | 26% | 报价请求归零、会话量塌方、成单金额腰斩 | 0.63 |
| `demand_decline` 需求萎缩 | 22% | 订单金额缓降、采购品类收窄、回款变慢 | 0.62 |
| `non_renewal` 合同未续 | 43% | 续约窗口前互动变少、订单间隔拉长 | 0.69 |
| `bankruptcy` 经营终止 | 9% | **什么都没有**——账户直接停止 | 0.46 |

最后一列是用同一批预测分做的 one-vs-rest AUC（见 §5）。经营终止的 0.46 不是缺陷，
是这份数据最值钱的性质：它给任何模型划了一个诚实的天花板。

`non_renewal` 占比偏高，是因为它同时吸收了"安静地不再出现"的那部分客户——
这类客户没有一个可命名的原因，从外部看就是"合作自然终止"。

### 三个刻意的设计

**① 不是每个流失都能被看见。** 每个客户有一个 `visibility` 抽样，约七分之一的
流失客户无论什么原因都几乎不留痕迹。这保证 AUC 不会跑到 0.99 那种"数据是自己造的"水平。

**② 十个幸存者里有一个会假摔。** 观测期内经历一段困难期（订单下滑、工单变多），
然后恢复。这些是模型必须扛住的虚警，也是真实客户成功团队每天在处理的噪声。

**③ 注册不等于成交。** 600 家里有 32 家从未下过第一单（签约后没转化），
它们在 `churn_events` 里标 `never_activated = true`，**评估时必须排除**：
未转化不是流失，但它在数据上长得和"最危险的客户"一模一样（没订单、没回款、没工单）。

产品侧现在也按同一口径过滤，且不读真值表：问「窗口内有没有下单」这类
`COUNT(events.*, 0, N, days) = 0` 的问题时，在该事件表里整段历史零行的客户
（可观测意义上的"从未开始"）会从预测人群里剔除，答案里注明剔除了多少家。
按发票口径过滤正好就是这 32 家、且不含任何真流失客户。反向的 `> 0` 问法不剔除——
那里"没成交过"是一个合法的负例。开关是 `KUMO_NEVER_ACTIVE_FILTER`。

### 三条让问题变难的真实规律

**年度合同客户的节奏只有月度的三成。** `annual` 客户的采购频率是月度合同的 30%
（中型一年约四单、大型约八单），所以它的 recency 常年 100 天上下却完全健康。
只看"多久没下单"会误杀一半好客户。

**客户会休眠，然后回来。** 每客户平均一到两段 45~150 天的沉默期，少数超过 180 天。
把 recency 从"完美判据"降级成"参考"的就是这批人。

**每张合同带一个续约时钟。** 间隔越久，下一单的概率越高（`rate *= 1 + 2.6·overdue/cycle`）。
没有这个机制时，年度合同的指数尾部会造出两年不订货、然后又回来的客户——
它们在每个锚点上都像已经死了，标签却是"还在"。这类客户曾占全库 13%，
并且**包揽了模型最自信的头部十名**。现在压到 5%，最长不超过 529 天。

---

## 3. 流失标签怎么定义的

`scoring.churn_events.churn_date` = **该客户最后一次有效下单的日期**。

- 非空 = 到观测末已超过 180 天没有订单，按 `COUNT(orders.*, 0, 180, days) = 0` 算流失；
- 为空 = 观测末仍在合作（右删失）；
- `churn_cause` = 流失原因，`decline_start` = 可观测衰退的起点（`bankruptcy` 为空）。

标签必须和要评估的 PQL 用同一个定义，否则 AUC 没有意义。这一点踩过两次坑：

1. 早期版本里"未计划流失但实际沉默超过 180 天"的客户被留成了负例，
   等于告诉模型"沉默是安全的"，AUC 掉 0.07。
2. 更贵的一次：改完标签定义只重跑了生成器、**没重建数据库**，评估用的还是旧答案。
   旧答案里 churn_date 是"计划流失日"而不是"最后下单日"，于是模型把
   一年半没下单的客户判成高危（它是对的），旧标签却说"这人还在"——
   pooled AUC 0.63 看起来还行，但**头部十分位精确率 7.5%，比 10.3% 的基准率还低**，
   `lift = 0.73`。指标互相矛盾时，先怀疑数据，不要怀疑指标。

---

## 4. 数据长什么样：三个真实客户

**① 被竞品挖走（前兆齐全）**

```
重庆海通建材有限公司  建材批发 / 大型 / 年度合同 / 账期 75 天   授信 71.3 万
季度金额   2022 每季 11.7~23.6 万 → 2023Q1: 18.8 万
           2023Q2: 完全没有订单（年度合同的空档）→ 2023Q3: 13.4 万
           2023Q4: 4.4 万，只有 1 单
网站报价   2022 每季 21~48 次 → 2023Q1: 35 → 2023Q3: 20 → 2023Q4: 1
网站会话   每季 248~279 → 263 → 215 → 48
回款       16 笔，4 笔逾期，平均提前 3.2 天
流失 2023-10-23，原因 competitor，衰退起点 2023-05-09
```

报价从每季 35 次掉到 1 次，是会说话的数字——需求还在，只是给了别人。
衰退起点到真正离网隔了五个半月，足够一个客户成功团队介入。

**② 经营终止（无前兆）**

```
广州正大医药有限公司  医药制造 / 中型 / 月度合同 / 账期 45 天   授信 13.9 万
季度金额   2023Q4: 4.5 万（8 单）→ 2024Q1: 2.3 万（4 单）→ 2024Q2: 1.1 万（4 单）
服务工单   27 件，平均 CSAT 4.1，全程没有一次跌到 3 以下
回款       26 笔，5 笔逾期（19%，全库基准 14%），平均提前 3.7 天
流失 2024-06-09，原因 bankruptcy，无 decline_start
```

上周还在正常下单、按时付款、给四分评价，这周就没了。这类客户占 9%，
是任何模型都拿不到的那部分上限——注意它的逾期率 19% 只是噪声，
不代表"回款变慢"这个信号。

**③ 假阳性风险（看着像走了，其实还在）**

```
成都新元餐饮管理有限公司  连锁餐饮 / 中型 / 年度合同 / 账期 45 天   授信 9.7 万
季度金额   2024Q4: 4.35 万（2 单）→ 2025Q1: 0 → 2025Q2: 2,587 元（1 单）
网站报价   最近两年稳定在 4~9 次/季，从未归零
网站会话   每季 62~80 次，没有下滑
回款       12 笔，2 笔逾期，平均提前 4.2 天
状态       未流失
```

最近两季合计 2,587 元，看着比谁都危险；但它是年度合同客户，一年只集中采购四五次，
空一个季度是常态，而且门户活跃度一直没掉。

和案例①的"网站归零"是相反的走向——**只看金额会误杀，只看行为会漏判**，
两者必须一起读。这也是这个库最有价值的地方：它同时养着这两种相反的轨迹。

---

## 5. 验证结果：管道通了，模型没吃满

评估口径：在每个季度末的锚点上，**只喂锚点之前的数据**，用全量历史算真值；
已流失客户排除（预测已经走掉的人不算预测），未成交客户排除。四个锚点
2023-09-30 ~ 2024-06-30，每锚点约 370 家、正例 6.5%~9.3%。

| 打分方式 | 单锚点 AUC | pooled AUC | 说明 |
|---|---|---:|---|
| 距上次下单天数（单特征） | 0.50 / 0.45 / 0.56 / 0.68 | 0.56 | 客户成功团队已有的规则 |
| 六列线性基准 | 0.71 / 0.70 / 0.69 / 0.79 | **0.72** | recency、近两季单数、两季比值、工单数、CSAT、有无订单史 |
| KumoRFM 托管端点 | 0.61 / 0.59 / 0.70 / 0.73 | 0.65 | `--all-tables`，6 个训练锚点 |

pooled（n=1,487，正例 119 家 = 8.0%）：**AUC 0.651、AP 0.119、
头部十分位精确率 16.6%（lift 2.07）**。

按流失原因拆开看，这张表比总 AUC 有信息量：

| 原因 | 正例数 | one-vs-rest AUC |
|---|---:|---:|
| `non_renewal` | 57 | 0.69 |
| `competitor` | 24 | 0.63 |
| `demand_decline` | 28 | 0.62 |
| `bankruptcy` | 10 | **0.46** |

四条结论：

1. **数据里确实有信号。** 六个常规业务列就能到 0.72，不需要任何关系推理。
2. **管道是通的。** Kumo 稳定跑完，概率有分化（p10/p50/p90 ≈ 0.005 / 0.06 / 0.34），
   不是塌成一个常数——这在 WWI 上做不到（那里全挤在 0.9997）。头部十分位能捞出
   2 倍于基准率的真流失，可以拿去排干预名单。
3. **模型认得可预测的原因，也诚实地栽在不可预测的那个上。** 三种有前兆的流失都在
   0.62~0.69，唯独经营终止是 0.46——和"随机"没有区别。这正是数据的设计意图，
   也是评估一个流失模型该看到的形状。
4. **它仍然没吃满。** 0.65 输给六列线性基准的 0.72。多表（工单、网站、回款）都送进去了，
   权重没学出来。这是物化层和训练样本量的问题——四个锚点、119 个正例、
   每条客户只有 6 个历史锚点，学一条"年度合同客户每季一次集中采购"的规律太难，
   而线性模型把这层结构直接写进了特征。

另一个代价量到了：**只送 PQL 用到的表**（早前为压缩 payload 做的优化）会让
pooled AUC 从 0.651 掉到 0.613，头部十分位从 2.07 倍 lift 掉到 1.25 倍。
图里"能连上"的表对流失预测是有用的，裁表省了带宽，也裁掉了信号。

---

## 6. 怎么用

### 重建

```bash
# 默认 600 客户、seed 20261109，落到 retail_churn
python scripts/retail_churn_dataset.py

# 换规模/种子；--dry-run 只算不落库；--csv 导出一份 CSV
python scripts/retail_churn_dataset.py --customers 1200 --seed 7
python scripts/retail_churn_dataset.py --dry-run
python scripts/retail_churn_dataset.py --csv /tmp/retail_churn_csv
```

生成 600 客户约 15 秒，表结构、外键、索引、注释、视图一并建好。
`--keep` 保留原库不重建。

**改过生成器就必须重建库再评估。** 这个坑代价最大（见 §3）：库里的答案不会自己跟着代码走。

### 直接查

```sql
-- 哪些客户在最近 90 天没有任何订单
SELECT c.customer_id, c.company_name, MAX(o.order_date) AS last_order
FROM retail.customers c
LEFT JOIN retail.orders o ON o.customer_id = c.customer_id AND o.status = 'completed'
GROUP BY 1, 2
HAVING MAX(o.order_date) < DATE '2025-06-30' - 90
ORDER BY last_order;

-- 衰退信号：近 90 天 vs 前 90 天
WITH w AS (
  SELECT customer_id,
         COUNT(*) FILTER (WHERE order_date > DATE '2025-06-30' - 90) AS recent,
         COUNT(*) FILTER (WHERE order_date <= DATE '2025-06-30' - 90
                            AND order_date > DATE '2025-06-30' - 180) AS prior
  FROM retail.orders WHERE status = 'completed' GROUP BY 1
)
SELECT c.company_name, w.recent, w.prior,
       ROUND(w.recent::numeric / GREATEST(w.prior, 1), 2) AS ratio
FROM w JOIN retail.customers c USING (customer_id)
WHERE w.prior >= 3 AND w.recent::numeric / GREATEST(w.prior, 1) < 0.5
ORDER BY ratio;
```

### 跑预测与评分

```bash
# 真打端点，四个锚点，输出 AUC / AP / lift，附单特征与线性基线
python scripts/retail_churn_score.py --all-tables --history-anchors 6

# 只看 payload 规模，不花钱
python scripts/retail_churn_score.py --all-tables --dry-run

# 落盘每个人的打分，便于事后看模型到底在信什么
python scripts/retail_churn_score.py --all-tables --anchors 2023-09-30 --save-scores /tmp/scores
```

PQL 是 `PREDICT COUNT(orders.*, 0, 180, days) = 0 FOR EACH customers.customer_id`。
`--all-tables` 决定是否把图上所有相关表都送过去（不裁表）。
锚点必须比数据末端早至少 180 天，否则那个窗口里不可能有完整标签（脚本会跳过并提示）。

规模参考：单锚点、`--all-tables`、6 个历史锚点，payload 约 9.9 MiB / 109,449 行相关记录
（旧版 WWI 十表图会先冲到 398 MiB 再抽样，所以这个库反而更轻）。

### 接到应用里

库在本地 Postgres 的 `retail_churn` 实例上，和应用自带连接串同源
（容器内主机名是 `postgres`，不是 `127.0.0.1`——后者在容器里指自己）。

**答案表必须挡掉。** `scoring.churn_events` 是标准答案，进了 catalog 就等于把
答案递给模型。Postgres 的 schema 白名单（`schemas`）不生效——它只对
Databricks / Snowflake / Kyuubi / Trino 的 introspection 有效——要用
`table_deny_regex`，这个是在 catalog 抽取处统一执行的，所有连接器都认：

```bash
# 1) 加连接（带答案表屏蔽）。host 用容器内的服务名
curl -X POST http://localhost:3001/api/connections \
  -H 'Content-Type: application/json' \
  -d '{"connection":{"type":"postgresql","host":"postgres","port":"5432",
       "user":"<user>","password":"<password>","database":"retail_churn",
       "table_deny_regex":"^churn_events$"}}'
```

生效标志是日志里这一行——12 张关系只留 11 张：

```
Table filter (deny='^churn_events$') kept 11 of 12 table(s)
```

**两条代码路径的优先顺序相反，这是最容易踩的坑。** `docs/connections.md` 说
"Use one per deployment" 就是指这个：问答用 catalog 里的连接、没有才回落到
`CONNECTION_STRINGS`；而 ingest 和语义编译是反过来的，`CONNECTION_STRINGS`
一非空就只认它。后果是半好半坏——问答那边看得见两个库，语义编译那边只看见
一个，`retail_churn` 的表永远没有 Term，提问会被检索拉去 WWI。

所以接了 UI 连接之后，要把 `.env` 里的两个连接串注释掉，让两边都走 catalog：

```
# CONNECTION_STRINGS=...
# SOURCE_CONNECTION_STRINGS=...
```

再把现有的库也在 UI 里登记一遍（否则提问只剩新库，老库的问答会突然失效），
然后 `docker compose up -d --force-recreate auto-ontology ingestion-service`。

**只做 ingest 还不够。** catalog 里有了表不代表检索能选中它们——检索吃的是
语义层（Term / SqlAttribute）。新库的第一次语义编译要等 scheduler 那一轮
（启动时一轮 + 每 24h 一轮），或手动触发：

```bash
curl -X POST http://localhost:3002/semantic/compile   # 增量，只编译还没有 Term 的表
curl http://localhost:3002/semantic/status            # running / last_success_at
```

就绪的判据是这两条同时成立：

```sql
-- 每个库都有标签，retail_churn 不是 0
select d.name, count(distinct tt.table_id) from catalog_table t
  join catalog_schema s on s.id = t.schema_id
  join catalog_database d on d.id = s.database_id
  left join table__term tt on tt.table_id = t.id group by d.name;
```

### 接入实录：一个算错的答案，和它怎么被修好

接进去之后问的第一句是「2024 年订单金额最高的 5 个行业分别是多少？」。
检索正确选中了 `retail_churn`（日志：`Selected candidate database 'retail_churn'`，
顺手丢弃了 8 个落在别的库上的语义命中），SQL 也对，答案却**系统性偏高 3.5%**：

| 行业 | 模型第一次 | 库里的真值 |
|---|---:|---:|
| 连锁餐饮 | 23,411,304.69 | 22,648,760.21 |
| 建材批发 | 19,844,997.00 | 19,300,345.52 |
| 食品加工 | 17,213,405.97 | 16,761,380.34 |

原因只有一条：SQL 少写了 `o.status = 'completed'`，把 163 张已取消订单
（282 万）当成了营收。这不是幻觉——用同一条不带过滤的 SQL 在 psql 里跑，
数字能逐位复现，说明**数据是对的，缺的是口径**。

口径本来写在源的 `COMMENT ON COLUMN` 里（`status IS 'completed 已完成、
cancelled 已取消'`），但 Postgres 连接器的 introspection 只读 `pg_class` /
`pg_attribute`，不读 `pg_description`；更上游，catalog 的写入路径里
`description` 被明确排除在"源库所有"的字段之外（它是 UI 里手工维护的，
re-ingest 不能覆盖）。所以注释既没被读，也进不了语义层。

修法是把口径写到 catalog 的列描述上（等价于 UI 里编辑那一栏），
剩下交给检索：

```sql
update catalog_column c set description =
  '订单状态：completed 已成交（只有这些计入营收与流失判定）、cancelled 已取消（不计营收）。'
  || '聚合金额或统计下单次数时必须过滤 status = ''completed''。'
from catalog_table t
 join catalog_schema s on s.id = t.schema_id
 join catalog_database d on d.id = s.database_id
where c.table_id = t.id and d.name = 'retail_churn'
  and s.name = 'retail' and t.name = 'orders' and c.name = 'status';
```

改完重问同一句，SQL 自己长出了 `o.status = 'completed'`，五个行业
**逐位对上真值**（22,648,760.21 / 19,300,345.52 / 16,761,380.34 /
8,183,493.53 / 6,448,159.56）。

两件事值得记住：**数字对不上时先看 WHERE 子句，别先怀疑数据**；
**语义层的质量上限由描述决定**，而这份库的描述是自己写的，写得多具体，
问答就多准。

> 这一步是手工 UPDATE，重建库或重装不会带回来。要固化的话，把上面这段
> 写成一个可重放的小脚本（`scripts/retail_churn_semantics.py`）最省事。
> 已经这么做了：13 条列口径全在里面，`--dry-run` 可先看要改什么，幂等。

### 第二个库暴露出来的四处结构性问题

单库（WWI）时代这些问题都被"只有一个库"掩盖了。接进第二个库之后，它们
一个接一个把答案带跑偏，且**每处都只在多库同时在线时才出现**——所以这不是
retail_churn 的私有毛病，任何"一个应用接多个库"的部署都会撞上。

| # | 现象 | 根因 | 修法 |
|---|---|---|---|
| 1 | 预测路径报 `UndefinedTable: sales.invoices` | `_enrich_relevant_tables` 只按**表名**跨库查表，取回的是 WWI 的同名表；`_table_select` 又不返回 `database_name`，下游无从分辨 | 富化改为**按当前库限定**查表，并把 `database_name` 带进表条目；引用了本库不存在表的 PQL 示例直接丢弃 |
| 2 | 明明选了 `retail_churn`，取数却读了 WWI | `_load_frames` 里 `default_connector = connectors[0]`：库名查不到就静默用第一个连接 | 删掉回落，查不到就报错（**回落到错的库比报错更糟**：答案是错的但不报错） |
| 3 | 写 `TO_DAYS(...)` / `EXTRACT(DAY FROM CURRENT_DATE - last_order_date)` | 方言规则里没有 Postgres 日期算术——两个 `date` 相减本身就是整数天，`EXTRACT` 一个整数会直接报 `function pg_catalog.extract(unknown, integer) does not exist` | 在 `_POSTGRES_DIALECT_RULES` 补日期算术规则；**刻意不点名其它方言**（有测试守着"方言规则不许串台"） |
| 4 | 二元预测目标上带 `RANK TOP 20`，端点直接拒 | 提示词已写"只有分类/数值目标能排序"，模型不听，且没有修复步骤 | 生成后做**确定性清洗**：二元/多分类目标剥离 `RANK TOP k`，日志留 `PQL: dropped RANK TOP from a target it cannot rank` |

四处都带了单测（跨库富化 4 例、RANK 剥离若干），`pytest auto_ontology`
2507 passed。第 1、2 条是同一个 bug 的两个面：**跨库串表**——查表不限定库、
连接不校验库，任一处松了都会把别的库的表塞进当前库的执行计划。

### 预测路径实测：链路通了，答案不可用

先说结论：**管道是通的，预测出来的名单不能用于业务**。原因不在数据、不在口径，
在一个可以指名的代码位置上。

要让"以某一天为观察点"这类问题可评分，先得解决一个前提：PQL 的窗口是**相对
数据末端**的偏移，`COUNT(orders.*, 0, 180, days)` 指的是"图里最后一条订单之后
180 天"，不是问题里写的那个日期。所以这句话在预测路径里根本无法表达。唯一诚实
的办法是给它一个最新一行就停在该时点的库：

```bash
python scripts/retail_churn_asof.py --anchor 2024-06-30
# CREATE DATABASE ... TEMPLATE retail_churn 整库复制（索引/约束/视图都带过来），
# 再删掉锚点之后的行、把 support_tickets.closed_at 晚于锚点的置 NULL（否则一张
# 锚点时还开着的工单会从关闭日期泄漏答案）、DROP scoring。
# 视图不用重建——view 会跟着行数自己重算。
# 校验：每张表最大日期 ≤ 锚点，且 scoring 不在 schema 里。
```

口径也要跟着改，否则模型仍拿 2025-06-30 当基准，recency 全废：

```bash
python scripts/retail_churn_semantics.py \
    --database retail_churn_asof_20240630 --data-end 2024-06-30
```

然后问同一句话（不再提日期，因为它表达不了）：「哪些客户最可能在之后 180 天内
不再下单？列出 20 家客户，按风险从高到低排序。」锚点 2024-06-30、窗口 180 天、
基准率 31/355 = 8.7%，三条路径的对比：

| 排序方式 | 命中 | 精确率 | 相对基准 | 无效选项 |
|---|---:|---:|---:|---:|
| 纯 recency（离线复算，风险集内最久未下单） | 6 / 20 | 30.0% | 3.4× | 0 |
| **应用 SQL 路径**（显式限定风险集） | 4 / 20 | 20.0% | **2.3×** | 0 |
| 应用预测路径（KumoRFM） | 1 / 20 | 5.0% | 0.57× | 14 |

最后一列的"无效选项"= 锚点前已流失 + 锚点前从未成交，这两类都不是预测。
预测路径那 20 家里有 **8 家锚点前根本没成交过、6 家锚点前已经流失**——
不是"预测得不准"，是根本没在做预测。

用 SQL 路径问同一件事、但**不**限定风险集，会踩同一个坑：`last_order_date`
为 NULL 的客户在 `ORDER BY ... DESC` 里排最前，20 行全是 null。加上"只看锚点前
180 天内有 completed 订单的客户"之后，SQL 一次就对——顺带说明语义层是好的：
它自己写出了 `(DATE '2024-06-30' - v.last_order_date)`，没用 `CURRENT_DATE`，
也没套 `TO_DAYS`。**风险集这件事，两条路径都不会自己想到。**

根因在 `kumo/hosted.py` 的 `_needed_tables`：它只保留"PQL 点名的表 + 关键路径
上的桥表"。`COUNT(orders.*)` 于是只剩 `customers` 和 `orders`，日志明说：

```
kumo: left 3 table(s) out of the payload as unrelated to the question:
      invoices, v_customer_last_order, v_customer_revenue_monthly
```

被踢掉的恰好是承载 recency 和月度下滑的两张视图——**关系型预测器最该用的旁支
信号，被自己的过滤器滤掉了**。而且不是预算问题：本次 payload 9.9 MB，限额 28 MB，
`max_related_rows` 40 万。

那个过滤器是为了修另一个真问题（十张表时线上 398 MB）加的硬语义过滤，没有中间档。
要修应该改成**按总行数预算收纳可达表**，而不是按 PQL 是否点名。在没改之前，
任何"想让它自己发现旁支信号"的问题都拿不到应有的信息。


---

## 7. 已知局限

- **不是真实业务数据。** 分布、季节性、金额量级都按行业常识设定，但没有经过业务校验。
  用它验证的是"管道能不能学到关系型信号"，不是"这家公司的流失模型值多少钱"。
- **流失率偏高。** 已成交客户四年半累计 36.4%（SMB 48%、中型 32%、大型 12%），
  年化约 9%。SMB 部分接近行业上限。
- **没有宏观冲击。** 没有疫情、没有原材料暴涨、没有大客户并购。
  真实数据里这些会让某一年的流失率整体跳一档。
- **单币种、单公司。** 没有汇率、没有多法人、没有跨境。
- **样本量对训练不友好。** 600 客户、四个可评估锚点、119 个正例。
  学"年度合同的采购节奏"这种事需要更多的客户-锚点组合，这也是 Kumo 输给线性基准
  最可能的原因；试过把客户加到 1000、把历史锚点加到 12，都没有改善。
- **`scoring` schema 必须排除。** 它和业务表在同一个库，接进应用时务必用
  `table_deny_regex="^churn_events$"` 挡掉（Postgres 的 `schemas` 白名单不生效）。

---

## 8. 相关

- 生成器：`scripts/retail_churn_dataset.py`
- 评分器：`scripts/retail_churn_score.py`
- 时点快照：`scripts/retail_churn_asof.py`（让应用的数据末端落在某个锚点上）
- 口径固化：`scripts/retail_churn_semantics.py`（`--database` / `--data-end`）
- 答案打分：`scripts/check_churn_answer.py`（`--from-transcript` 直接读问答转录）
- 不变量测试：`auto_ontology/tests/test_retail_churn_dataset.py`（8 例）
- WWI 上的托管端点联调记录：`docs/wideworldimporters-数据字典与问法.md` §10
