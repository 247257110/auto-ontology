# Wide World Importers：数据字典、报表问法与 SQL Attribute 用法

> 本文所有行数、金额、时间范围都是在本机 `wideworldimporters` 库里实测的（2026-11-08），不是按样例库文档抄的。

---

## 1. 这是什么业务

**一家做新奇小商品的批发进口商（wholesale novelty goods importer）**，自己不做零售，客户是各类零售商。

它同时是一门"进销存 + 应收应付"的完整生意：

| 环节  | 业务动作                          | 落在哪个 schema                  |
| --- | ----------------------------- | ---------------------------- |
| 采购  | 向 13 家供应商下采购订单、收货、付汇          | `purchasing`                 |
| 仓储  | 227 个 SKU 的收发货、盘点、货位、补货点、冷链温控 | `warehouse`                  |
| 销售  | 663 家零售客户下单、发货、开票             | `sales`                      |
| 收款  | 客户按 EFT 付款，形成应收台账             | `sales.customertransactions` |
| 主数据 | 商品/客户/供应商/人员/地区/字典            | `application`                |

客户结构（实测）：

| 客户类别               | 数量  |
| ------------------ | --- |
| Novelty Shop 新奇商品店 | 459 |
| Supermarket 超市     | 58  |
| Computer Store 电脑店 | 51  |
| Gift Store 礼品店     | 48  |
| Corporate 企业客户     | 47  |

采购集团（buying group，联合采购）：`Wingtip Toys` 201 家、`Tailspin Toys` 201 家、无集团 261 家。

商品结构：新奇商品（91 SKU）、电脑周边玩具（83）、服装（74）、包装材料（67）、马克杯（42）、T恤（26）、毛绒鞋（24）、玩具（21）、USB 小物（14）。其中 **8 个 SKU 是冷藏品**（`ischillerstock`），冷链是这个库的一大特色。

---

## 2. 数据画像（先记住这几个数）

| 指标         | 值                                             |
| ---------- | --------------------------------------------- |
| 时间跨度       | **2013-01-01 ~ 2016-05-31**（3 年 5 个月，之后数据就停了） |
| 客户         | 663 家，**全部在美国**（190 个国家里只用到了美国）               |
| 销售订单 / 订单行 | 73,595 / 231,412                              |
| 发票 / 发票行   | 70,510 / 228,265                              |
| 有发票的订单     | 70,510（**3,085 张订单从未开票**）                     |
| 营收（含税）     | **198,043,439**                               |
| 营收（不含税）    | **172,261,341**                               |
| 税额         | 25,782,098                                    |
| 毛利额        | **85,729,181**                                |
| 毛利率        | **49.77%**（不含税口径）/ 43.29%（含税口径）               |
| 客单价        | 2,808.73                                      |
| 应收未收余额     | 267,011（只有 39 家客户有余额）                         |
| 采购订单 / 采购行 | 2,074 / 8,367                                 |
| 供应商        | 13 家                                          |
| 库存变动流水     | 236,667                                       |
| 冷库温度读数     | 4 条（实时）+ 1,200,000 条（归档）                      |
| 冷藏车温度读数    | 65,998                                        |
| 贷记单（退货）    | **0 张**                                       |

年度营收：

| 年           | 营收（含税）     | 发票数    |
| ----------- | ---------- | ------ |
| 2013        | 52,563,273 | 18,767 |
| 2014        | 57,418,917 | 20,303 |
| 2015        | 62,090,221 | 22,250 |
| 2016（1–5 月） | 25,971,029 | 9,190  |

---

## 3. Schema 分工

| Schema        | 角色                             | 表数              |
| ------------- | ------------------------------ | --------------- |
| `application` | 主数据与字典：地区、人员、配送/付款方式、交易类型、公司参数 | 15（8 主表 + 7 归档） |
| `sales`       | 销售与应收：客户、订单、发票、客户台账、特价         | 12（9 主表 + 3 归档） |
| `purchasing`  | 采购与应付：供应商、采购单、供应商台账            | 7（5 主表 + 2 归档）  |
| `warehouse`   | 仓储与商品：商品主数据、库存、品类、冷链           | 14（9 主表 + 5 归档） |
| `website`     | **3 个视图**，给网站前端用的展平结果，不是事实数据源  | 3               |

四种表类型，读的时候必须先分清，否则数字会算错：

| 类型                  | 特征                                   | 用法                               |
| ------------------- | ------------------------------------ | -------------------------------- |
| **事实表**             | 行数大、有日期、有金额或数量                       | 算指标用这些                           |
| **维表/主数据**          | 几十到几百行，有名称                           | 做分组维度                            |
| **`*_archive` 归档表** | 存**历史版本**（`validfrom`~`validto` 已关闭） | 默认**不要 join**，会重复计数；只有做"变更历史"时才用 |
| **`website.*` 视图**  | 已 join 好的宽视图                         | 一般不用，查客户详情时可以借用                  |

主表与归档表的关系（实测确认）：

```
sales.customers         663 行，validto = '9999-12-31 23:59:59.999999'  → 当前版本
sales.customers_archive  51 行，validto 已关闭（如 2015-07-01）        → 旧版本（同一客户最多 3 个历史版本）
```

> ⚠️ **哨兵值陷阱**：当前版本用的是写死的时间戳 `9999-12-31 23:59:59.999999`，**不是** PostgreSQL 的  
> `'infinity'` 字面量。实测 `WHERE validto = 'infinity'` 在 `sales.customers`（663 行）和  
> `warehouse.stockitems`（227 行）上都返回 **0 行**，看起来像"表是空的"。  
> 正确写法：`WHERE validto > now()` 或 `WHERE validto = '9999-12-31 23:59:59.999999'`。  
> 实际上这两张主表里**每一行都是当前版本**（0 条已关闭），所以直接查主表不需要加时间过滤；  
> 只有 join 归档表时才必须把 `validto` 关掉的那一侧排除。

---

## 4. 表清单：每张表存什么

### 4.1 `sales` — 销售域（做报表主战场）

| 表                      | 行数      | 存什么                                                | 关键字段                                                                                                                                            |
| ---------------------- | ------- | -------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------- |
| `customers`            | 663     | 客户主数据：名称、类别、采购集团、联系人、配送/邮寄地址、信用额度、付款天数、标准折扣、是否信用冻结 | `customerid` `customercategoryid` `buyinggroupid` `creditlimit` `paymentdays` `standarddiscountpercentage` `accountopeneddate` `deliverycityid` |
| `customercategories`   | 8       | 客户类别字典                                             | `customercategoryname`                                                                                                                          |
| `buyinggroups`         | 2       | 采购集团字典                                             | `buyinggroupname`                                                                                                                               |
| `orders`               | 73,595  | 销售订单头：客户、销售员、拣货人、联系人、下单日、要求交付日、是否缺货补单              | `orderid` `customerid` `salespersonpersonid` `pickedbypersonid` `orderdate` `expecteddeliverydate` `backorderorderid`                           |
| `orderlines`           | 231,412 | 订单行：商品、数量、单价、税率、已拣数量                               | `orderid` `stockitemid` `quantity` `unitprice` `pickedquantity`                                                                                 |
| `invoices`             | 70,510  | 发票头：客户、来源订单、销售员、开票日、是否贷记单、干/冷藏品件数、配送签收             | `invoiceid` `customerid` `orderid` `invoicedate` `iscreditnote` `totaldryitems` `totalchilleritems`                                             |
| `invoicelines`         | 228,265 | 发票行：**金额与毛利的唯一来源**                                 | `quantity` `unitprice` `taxamount` `extendedprice`（含税） `lineprofit`（毛利额）                                                                        |
| `customertransactions` | 97,147  | 客户资金台账：开票（正）、收款（负）、应收余额                            | `transactiontypeid` `transactionamount` `outstandingbalance` `isfinalized`                                                                      |
| `specialdeals`         | 2       | 特价协议：可按商品/客户/采购集团/类别/品类定折扣，带有效期                    | `discountpercentage` `discountamount` `startdate` `enddate`                                                                                     |

**金额口径（最容易错的地方，已实测）**

```
extendedprice = quantity × unitprice + taxamount     -- 含税
lineprofit    = 毛利额，税前口径
```

例：`quantity=10, unitprice=230, taxrate=15, taxamount=345, extendedprice=2645`  
→ 2300 + 345 = 2645。

所以：

- **含税营收** = `SUM(extendedprice)`
- **不含税营收** = `SUM(extendedprice - taxamount)`
- **毛利率** = `SUM(lineprofit) / SUM(extendedprice - taxamount)` → 49.77%
- 用 `extendedprice` 做分母得 43.29%，数字会低 6.5 个点

**`invoices` 和 `orders` 都没有金额字段**，必须回到行表汇总。

### 4.2 `purchasing` — 采购域

| 表                      | 行数    | 存什么                                      | 关键字段                                                               |
| ---------------------- | ----- | ---------------------------------------- | ------------------------------------------------------------------ |
| `suppliers`            | 13    | 供应商主数据：名称、类别、联系人、银行账户、付款天数               | `suppliercategoryid` `paymentdays` `bankaccountnumber`             |
| `suppliercategories`   | 9     | 供应商类别：新奇品/玩具/包装/服装/快递/保险/金融/营销           | `suppliercategoryname`                                             |
| `purchaseorders`       | 2,074 | 采购订单头：供应商、下单日、要求到货日、是否已确认                | `supplierid` `orderdate` `expecteddeliverydate` `isorderfinalized` |
| `purchaseorderlines`   | 8,367 | 采购行：订购箱数、已收箱数、期望单价、最后收货日                 | `orderedouters` `receivedouters` `expectedunitpriceperouter`       |
| `suppliertransactions` | 2,438 | 供应商台账：采购发票 2,072 笔（944,343,272）、付款 366 笔 | `transactiontypeid` `transactionamount` `outstandingbalance`       |

### 4.3 `warehouse` — 仓储与商品域

| 表                       | 行数      | 存什么                                                   | 关键字段                                                                                                                         |
| ----------------------- | ------- | ----------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------- |
| `stockitems`            | 227     | 商品主数据：名称、供应商、颜色、内/外包装、品牌、规格、提前期、是否冷藏、税率、售价、建议零售价、单位重量 | `supplierid` `colorid` `unitpackageid` `outerpackageid` `leadtimedays` `ischillerstock` `unitprice` `recommendedretailprice` |
| `stockitemholdings`     | 227     | **库存现状**（一商品一行）：在手数量、货位、上次盘点量、最后成本价、补货点、目标库存          | `quantityonhand` `reorderlevel` `targetstocklevel` `lastcostprice` `binlocation`                                             |
| `stockitemstockgroups`  | 442     | 商品 ↔ 品类 多对多（**一个商品可能属于多个品类，按品类汇总会重复计数**）              | `stockitemid` `stockgroupid`                                                                                                 |
| `stockgroups`           | 10      | 商品品类字典                                                | `stockgroupname`                                                                                                             |
| `colors`                | 36      | 颜色字典                                                  | `colorname`                                                                                                                  |
| `packagetypes`          | 14      | 包装形式字典（纸箱、托盘…）                                        | `packagetypename`                                                                                                            |
| `stockitemtransactions` | 236,667 | **库存变动流水**：出入库与盘点调整，`quantity` 正为入、负为出                | `transactiontypeid` `customerid` `invoiceid` `supplierid` `purchaseorderid` `quantity` `transactionoccurredwhen`             |
| `coldroomtemperatures`  | 4       | 冷库温度实时读数                                              | `coldroomsensornumber` `recordedwhen` `temperature`                                                                          |
| `vehicletemperatures`   | 65,998  | 冷藏车温度读数（2016-01 ~ 05）                                 | `vehicleRegistration` `chillerSensorNumber` `temperature`                                                                    |

> 冷库温度"实时表只有 4 条"是这份样例库的设计：历史读数全在 `coldroomtemperatures_archive`（120 万条，2015-12-20 ~ 2016-05-27）。问"冷库温度趋势"必须查归档表。

### 4.4 `application` — 主数据与字典

| 表                  | 行数     | 存什么                                                                         |
| ------------------ | ------ | --------------------------------------------------------------------------- |
| `cities`           | 37,940 | 城市：所属州省、经纬度、最新人口记录、预计人口                                                     |
| `stateprovinces`   | 53     | 州/省：所属国家、销售区域（sales territory）、人口                                           |
| `countries`        | 190    | 国家：大洲、区域、次区域、官方名称、边境、首都                                                     |
| `people`           | 1,111  | **人员总表**：员工（19 人，其中销售员 10 人）+ 客户/供应商联系人，用 `isemployee` / `issalesperson` 区分 |
| `deliverymethods`  | 10     | 配送方式                                                                        |
| `paymentmethods`   | 4      | 付款方式（实际只用到 EFT）                                                             |
| `transactiontypes` | 13     | 交易类型字典：客户发票/贷记单/收款/退款、供应商发票/贷记单/付款/退款、库存转移/出库/入库/盘点调整、客户冲抵                  |
| `systemparameters` | 1      | 公司自身参数：总部地址、应用设置                                                            |

### 4.5 `website` — 视图（不要当表用）

`website.customers` / `website.suppliers` / `website.vehicletemperatures`：把主表 join 成"给人看"的宽视图（客户名 + 类别名 + 联系人姓名 + 采购集团名…）。做分析请回主表，否则口径和数据都会绕。

---

## 5. 主干关系（join 就靠这几条）

```
sales.customers ──< sales.orders ──< sales.orderlines >── warehouse.stockitems
       │                  │
       │                  └────────< sales.invoices ──< sales.invoicelines >── stockitems
       │                                   │
       └──< sales.customertransactions >───┘（invoiceid）
       
sales.orders.salespersonpersonid ──> application.people.personid
sales.customers.deliverycityid   ──> application.cities.cityid
application.cities.stateprovinceid ──> application.stateprovinces.stateprovinceid
application.stateprovinces.countryid ──> application.countries.countryid

purchasing.suppliers ──< purchaseorders ──< purchaseorderlines >── warehouse.stockitems
purchasing.suppliers ──< suppliertransactions

warehouse.stockitems ──1:1── stockitemholdings
warehouse.stockitems ──< stockitemstockgroups >── stockgroups
warehouse.stockitems ──< stockitemtransactions >── （invoiceid / purchaseorderid / customerid / supplierid）
```

三个关键连接点，问的时候值得固定写法：

| 想算    | 怎么接                                                                   |
| ----- | --------------------------------------------------------------------- |
| 营收/毛利 | `invoicelines` 直接带 `invoiceid`，再 join `invoices` 拿客户/日期/销售员           |
| 订单额   | `orderlines.quantity * unitprice` 自己算，**再乘 (1+taxrate/100) 才和发票口径可比** |
| 商品品类  | 必须经 `stockitemstockgroups`；注意多对多                                      |

---

## 6. 可以问哪些报表问题

下面按业务域分组。**"已验证"表示我在库里真跑出了结果**，括号里是实测答案，可以直接用来校验 Agent 答得对不对。

### 6.1 经营总览（高管）

| 问题                      | 用哪些表                        | 实测                                         |
| ----------------------- | --------------------------- | ------------------------------------------ |
| 三年半总共做了多少营收、多少毛利、毛利率多少？ | `invoicelines`              | 含税 1.98 亿 / 毛利 8,573 万 / 49.77%（不含税口径）✅    |
| 每年营收和发票数是多少？            | `invoices` + `invoicelines` | 2013 5,256 万 → 2015 6,209 万；2016 只有 5 个月 ✅ |
| 月度营收趋势、哪个月最好？           | 同上                          | 发票数峰值 2015-07（2,166 张）✅                    |
| 客单价（每张发票平均金额）多少？        | `invoicelines`              | 2,808.73 ✅                                 |

### 6.2 客户分析

| 问题                                   | 用哪些表                                                | 备注                                          |
| ------------------------------------ | --------------------------------------------------- | ------------------------------------------- |
| 客户数、按客户类别/采购集团分布？                    | `customers` + `customercategories` / `buyinggroups` | 663 家，Novelty Shop 占 69% ✅                  |
| Top 10 客户营收？                         | `invoices` + `invoicelines` + `customers`           | 第一名 Tailspin Toys (Inguadona, MN) 438,690 ✅ |
| 客户地域分布（州）？                           | `customers` → `cities` → `stateprovinces`           | 全国分布，需通过 deliverycityid 走三级 join ✅          |
| 每个客户的首次/末次购买日、购买次数、生命周期营收（RFM 原始数据）？ | `invoices` + `invoicelines`                         | 663 行，可直接算 R/F/M ✅                          |
| 一年以上没下单的流失客户？                        | 同上                                                  | 用 `MAX(invoicedate)` 与 2016-05-31 比         |
| 信用额度使用率高的客户？                         | `customers` + `customertransactions`                | 信用额度 1,100 ~ 4,630，均值 2,615；实际无一家信用冻结       |

### 6.3 商品与品类

| 问题                          | 用哪些表                                                            | 实测                                                                             |
| --------------------------- | --------------------------------------------------------------- | ------------------------------------------------------------------------------ |
| 品类营收排名？                     | `invoicelines` → `stockitemstockgroups` → `stockgroups`         | Packaging Materials 1.158 亿 > Clothing 5,341 万 > Computing Novelties 4,301 万 ✅ |
| 品类毛利率对比？                    | 同上 + `lineprofit`                                               | Computing Novelties 51.1% 最高，Novelty Items 31.3% 最低（含税口径）✅                     |
| Top 20 畅销 SKU（按金额/按件数）？     | `invoicelines` + `stockitems`                                   | —                                                                              |
| 哪些商品在亏钱卖（`lineprofit < 0`）？ | `invoicelines`                                                  | 值得先跑一次存成 SQL Attribute                                                         |
| 建议零售价与实际成交价的折扣率？            | `stockitems.recommendedretailprice` vs `invoicelines.unitprice` | —                                                                              |
| 冷藏品（chiller）销售占比？           | `stockitems.ischillerstock`                                     | 只有 8 个 SKU                                                                     |
| 品牌维度销售？                     | `stockitems.brand`                                              | —                                                                              |

### 6.4 销售团队

| 问题                | 用哪些表                                                    | 陷阱                                                                                    |
| ----------------- | ------------------------------------------------------- | ------------------------------------------------------------------------------------- |
| 每个销售员的营收/客户数/发票数？ | `invoices.salespersonpersonid` → `people`               | ⚠️ **有两个人都叫 Hudson（personid 3 和 13），各约 1,990 万 / 1,970 万。按名字分组会合并成 3,960 万，把他俩当冠军** ✅ |
| 销售员的客户覆盖率？        | `customers` 无销售员字段                                      | 销售员归属只能从订单/发票反推                                                                       |
| 哪个拣货人/打包人处理量最大？   | `orders.pickedbypersonid` / `invoices.packedbypersonid` | —                                                                                     |


### 6.5 订单与履约

| 问题                                    | 用哪些表                                         | 实测                                   |
| ------------------------------------- | -------------------------------------------- | ------------------------------------ |
| 订单到发票的转化率？                            | `orders` vs `invoices.orderid`               | 70,510 / 73,595 = 95.8%，3,085 张未开票 ✅ |
| 下单到开票的平均天数？                           | `orders.orderdate` vs `invoices.invoicedate` | —                                    |
| 缺货补单（backorder）有多少？                   | `orders.isundersupplybackordered`            | —                                    |
| 拣货完成率（`pickedquantity` / `quantity`）？ | `orderlines`                                 | —                                    |
| 各配送方式（delivery run）的订单量？              | `invoices.deliveryrun`                       | —                                    |

### 6.6 应收与资金

| 问题                  | 用哪些表                                         | 实测                         |
| ------------------- | -------------------------------------------- | -------------------------- |
| 当前应收未收总额？           | `customertransactions.outstandingbalance`    | 267,011（仅 39 家客户有余额）✅      |
| 应收账龄分布（30/60/90 天）？ | 同上 + `finalizationdate`                      | —                          |
| 回款率（收款 / 开票）？       | `customertransactions` 按 `transactiontypeid` | 开票 1.98 亿、EFT 收款 1.978 亿 ✅ |
| 每家客户的已开票-已收款差额？     | 同上 group by customer                         | —                          |

### 6.7 采购与供应链

| 问题                                         | 用哪些表                                             | 实测                |
| ------------------------------------------ | ------------------------------------------------ | ----------------- |
| 各供应商采购金额排名？                                | `suppliertransactions`                           | 采购发票合计 9.44 亿 ✅   |
| 采购订单到货率（`receivedouters`/`orderedouters`）？ | `purchaseorderlines`                             | —                 |
| 各供应商付款周期（应付款/日均采购）？                        | `suppliers.paymentdays` + `suppliertransactions` | —                 |
| 各品类的供应商集中度？                                | `stockitems.supplierid`                          | 13 家供应商覆盖 227 SKU |

### 6.8 库存与仓配

| 问题                      | 用哪些表                                                 | 实测                 |
| ----------------------- | ---------------------------------------------------- | ------------------ |
| 当前库存总值（按最后成本价 / 按售价）？   | `stockitemholdings.lastcostprice` × `quantityonhand` | —                  |
| 低于补货点的 SKU 清单？          | `stockitemholdings`                                  | 2 个（商品 86 和 184）✅  |
| 库存周转天数 / 动销率？           | `stockitemtransactions` + `stockitemholdings`        | 227 个 SKU 有出入库记录 ✅ |
| 呆滞库存（90 天无出库）？          | `stockitemtransactions` 负向记录                         | —                  |
| 盘点差异（Stock Adjustment）？ | `transactiontypes` = 12                              | 44 笔调整，净 −18 件 ✅   |

### 6.9 冷链合规（这个库的差异化部分）

| 问题                  | 用哪些表                                                          | 注意                                               |
| ------------------- | ------------------------------------------------------------- | ------------------------------------------------ |
| 冷库温度超阈值时长/次数？       | `coldroomtemperatures_archive`                                | **必须查归档表**（120 万条，2015-12 ~ 2016-05）；实时表只有 4 条 ✅ |
| 各冷藏车温度异常记录？         | `vehicletemperatures`                                         | 65,998 条，2016-01 ~ 05 ✅                          |
| 冷藏品从入库到出库的温度是否全程合规？ | `stockitemtransactions` + 两张温度表 + `stockitems.ischillerstock` | 跨域 join，难度最高                                     |

### 6.10 特价与促销

| 问题                | 用哪些表                                       | 实测                                   |
| ----------------- | ------------------------------------------ | ------------------------------------ |
| 特价协议清单、覆盖哪些客户/品类？ | `specialdeals`                             | 只有 2 条（2016 Q1/Q2 的 USB 小物 10%/15%）✅ |
| 特价期间实际成交价是否等于协议价？ | `specialdeals` vs `invoicelines.unitprice` | 数据量小，可精确核                            |

---

## 7. 这份数据的坑（问之前必须知道）

| 坑                   | 现象                                 | 正确做法                                         |
| ------------------- | ---------------------------------- | -------------------------------------------- |
| **金额含税**            | `extendedprice` 已含税，直接当营收会把税额算进收入  | 用 `extendedprice - taxamount` 做收入，或明确写"含税营收" |
| **头表无金额**           | `invoices`、`orders` 都没有 total 列    | 必须从行表汇总                                      |
| **归档表重复计数**         | join `*_archive` 会让客户/商品翻倍         | 分析一律只用主表；`website` 视图同理                      |
| **冷库温度在归档表**        | 实时表只有 4 条，看起来"没数据"                 | 查 `coldroomtemperatures_archive`             |
| **商品多品类**           | 442 条映射 / 227 个商品，按品类汇总会超过 100%    | 加 DISTINCT 或在 SQL Attribute 里固化规则            |
| **同名销售员**           | 两个 Hudson，按名聚合业绩翻倍                 | 按 `personid` 分组，显示时用 `fullname`              |
| **零贷记单**            | `iscreditnote` 全 false             | 问"退货率"会得到 0，这是数据事实不是 bug                     |
| **客户全在美国**          | 190 个国家只有 1 个被用到                   | 别问"按国家分布"，会是单行                               |
| **时间截止 2016-05-31** | 问"最近 12 个月"要基于 2016-05             | 相对时间(本月/去年)在这份数据上没有意义                        |
| **订单额与发票额不可直接比**    | 订单行 231,412 vs 发票行 228,265，且订单无税额列 | 比的应是"订单数 vs 开票订单数"，不是金额                      |



---

## 8. SQL Attribute 怎么用

### 8.1 它是什么

> 挂在某个 **Term（业务词条）** 下面的、**一条经过校验的 SQL 片段**，用来说明"这个业务指标到底怎么算"。

它不是视图、不是物化字段、不是宏替换。它是**写给 LLM 看的口径说明书**：

```
name:        毛利率
description: 毛利额 / 不含税营收，按发票行口径
expression:  SELECT SUM(il.lineprofit) / NULLIF(SUM(il.extendedprice - il.taxamount), 0) AS gross_margin_rate FROM sales.invoicelines il
```

### 8.2 它在问答链路里怎么起作用

```
用户提问
  → 问题向量化，在语义库里召回 Term / Column / SqlAttribute（sql_attribute 也被嵌入）
  → 命中的属性被格式化进 SQL 生成 prompt，标签是：
     "SQL ATTRIBUTES (derived metrics/formulas — use their expressions and SQL as guidance)"
  → LLM 写 SQL 时参考这些表达式
  → 值校验 / SQL 校验 / 执行
```

关键点：**这是"指导"（guidance），不是"强制替换"**。LLM 会参考你的写法，但不会逐字照抄。所以：

- 表达式写得越完整（含 join、含过滤、含 `NULLIF`），被复用的概率越高；
- 只写 `SUM(x)` 这种片段，LLM 大概率自己重新推导，等于没写；
- 同一个指标有两个不同写法时，谁被召回取决于语义相似度，所以**同名指标不要建两条**。

### 8.3 四条硬规则（都是实测出来的）

| 规则                                        | 说明                                                                                                                                                  |
| ----------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------- |
| **① 必须是完整 SELECT**                        | `SUM(sales.invoicelines.extendedprice)` → 校验报 `SQL doesn't reference any table known to the catalog`。必须写成 `SELECT SUM(...) FROM sales.invoicelines` |
| **② 至少引用一张 catalog 里的表**                  | `SELECT 1` 也过不了                                                                                                                                     |
| **③ 校验只查表、不查列**                           | `SELECT SUM(il.gross_margin) FROM sales.invoicelines il`（列不存在）**能通过校验**。所以创建前必须自己真跑一遍                                                               |
| **④ name 全局唯一、同一 Term 下 expression 不能重复** | 会分别报 `SqlAttribute with name '...' already exists` 和 `SQL snippet already exists`                                                                   |

另外两个流程限制：

- 界面上必须先点 **Validate SQL** 通过，`Create` 按钮才可点；
- `certified` 字段目前只用于展示，**检索打分并未使用**——别指望"认证后优先命中"。

### 8.4 创建步骤

**界面**：`Terms` → 选一个 Term（如 "Invoice Line"） → 右侧 **SQL Attributes** 区块 → `Create New SQL Attribute` → 填 Attribute Name / Description（可选）/ SQL Code → 先 `Validate SQL` → `Create`。

**API**：

```bash
POST http://localhost:3001/api/sql-attributes
{
  "name": "毛利率",
  "description": "毛利额 / 不含税营收，按发票行口径",
  "expression": "SELECT SUM(il.lineprofit) / NULLIF(SUM(il.extendedprice - il.taxamount), 0) AS gross_margin_rate FROM sales.invoicelines il",
  "term_id": "<Term 的 id>"
}
```

> `term_id` 是必填的 —— 属性必须挂在词条下，不能悬空。

### 8.5 十个可直接用的例子（全部已在本库执行通过）

| #  | 名称       | 表达式                                                                                                                                                                                                                                                                                           | 实测结果           |
| -- | -------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------- |
| 1  | 营收（含税）   | `SELECT SUM(il.extendedprice) AS revenue FROM sales.invoicelines il`                                                                                                                                                                                                                          | 198,043,439.45 |
| 2  | 营收（不含税）  | `SELECT SUM(il.extendedprice - il.taxamount) AS net_revenue FROM sales.invoicelines il`                                                                                                                                                                                                       | 172,261,341.20 |
| 3  | 毛利额      | `SELECT SUM(il.lineprofit) AS gross_profit FROM sales.invoicelines il`                                                                                                                                                                                                                        | 85,729,180.90  |
| 4  | 毛利率      | `SELECT SUM(il.lineprofit) / NULLIF(SUM(il.extendedprice - il.taxamount), 0) AS gross_margin_rate FROM sales.invoicelines il`                                                                                                                                                                 | 0.4977         |
| 5  | 客单价      | `SELECT SUM(il.extendedprice) / NULLIF(COUNT(DISTINCT il.invoiceid), 0) AS avg_invoice_value FROM sales.invoicelines il`                                                                                                                                                                      | 2,808.73       |
| 6  | 客户生命周期价值 | `SELECT i.customerid, MAX(i.invoicedate) AS last_invoice_date, COUNT(DISTINCT i.invoiceid) AS invoice_count, SUM(il.extendedprice) AS lifetime_revenue FROM sales.invoicelines il JOIN sales.invoices i ON i.invoiceid = il.invoiceid GROUP BY i.customerid`                                  | 663 行          |
| 7  | 品类毛利率    | `SELECT sg.stockgroupname, SUM(il.extendedprice) AS revenue, SUM(il.lineprofit) AS profit FROM sales.invoicelines il JOIN warehouse.stockitemstockgroups ss ON ss.stockitemid = il.stockitemid JOIN warehouse.stockgroups sg ON sg.stockgroupid = ss.stockgroupid GROUP BY sg.stockgroupname` | 9 个品类          |
| 8  | 补货预警     | `SELECT s.stockitemid, s.stockitemname, h.quantityonhand, h.reorderlevel FROM warehouse.stockitems s JOIN warehouse.stockitemholdings h ON h.stockitemid = s.stockitemid WHERE h.quantityonhand <= h.reorderlevel`                                                                            | 2 个 SKU        |
| 9  | 商品出入库合计  | `SELECT st.stockitemid, SUM(CASE WHEN st.quantity > 0 THEN st.quantity ELSE 0 END) AS received_qty, SUM(CASE WHEN st.quantity < 0 THEN -st.quantity ELSE 0 END) AS issued_qty FROM warehouse.stockitemtransactions st GROUP BY st.stockitemid`                                                | 227 行          |
| 10 | 应收余额     | `SELECT ct.customerid, SUM(ct.outstandingbalance) AS ar_balance FROM sales.customertransactions ct GROUP BY ct.customerid`                                                                                                                                                                    | 267,011 合计     |

三个反例，说明"能写什么"：

```sql
-- ✗ 裸表达式，校验直接失败
SUM(sales.invoicelines.extendedprice)

-- ✗ 列名不存在，但校验会通过（陷阱），执行才报错
SELECT SUM(il.gross_margin) FROM sales.invoicelines il

-- ✓ 带完整 join 和口径注释的写法，最容易被 LLM 复用
SELECT
  i.customerid,
  SUM(il.lineprofit) / NULLIF(SUM(il.extendedprice - il.taxamount), 0) AS gross_margin_rate
FROM sales.invoicelines il
JOIN sales.invoices i ON i.invoiceid = il.invoiceid
GROUP BY i.customerid
```

### 8.6 当前这份本体的状态（2026-11-09 已补齐一版）

| 项               | 数量          | 说明                                                                                                                           |
| --------------- | ----------- | ---------------------------------------------------------------------------------------------------------------------------- |
| Term            | 50          | 都是"表级"术语，**混进了归档表和视图**（Archived City、Customer (wideworldimporters.website.customers)）                                        |
| 5 个主表术语         | 已通俗化 + 认证   | 原名 Invoice Line / Invoice / Customer / Stock Item / Order Line，现名 **发票明细行 / 销售发票 / 客户 / 商品 / 订单明细行**，name 与 description 均已认证 |
| Term 名称已认证      | 5 / 50      | 只有上述 5 个主表                                                                                                                   |
| SQL Attribute   | **9**（全部认证） | 见下表                                                                                                                          |
| Custom Analysis | **6**（已建入）  | 见 §9.5，覆盖排名 / 时间序列 / 帕累托 / 漏斗 / 业绩 / 分桶六类范式                                                                                  |
| PQL Analysis    | **6**（已建入）  | 预测类 few-shot 语料，见 §10.8；镜像已装 client、语料已入库，**只差 `KUMO_RFM_API_URL` 指向的 NIM 端点** |
| Tag / Zone      | 0 / 0       | 没有分类锚点                                                                                                                       |
| Join 路径         | 2,215 条     | 外键 39 条 + 语义外键 85 条推导，已可用                                                                                                    |

已建成的 9 条 SQL Attribute（表达式与口径说明均以 API 实存为准）：

| 挂载术语  | 指标       | 实测值                 |
| ----- | -------- | ------------------- |
| 发票明细行 | 营收（含税）   | 198,043,439.45      |
| 发票明细行 | 营收（不含税）  | 172,261,341.20      |
| 发票明细行 | 毛利额      | 85,729,180.90       |
| 发票明细行 | 毛利率      | 49.77%              |
| 发票明细行 | 客单价      | 2,808.73            |
| 客户    | 客户生命周期价值 | 663 家客户             |
| 客户    | 应收余额     | 267,011.44（39 家有余额） |
| 商品    | 品类营收与毛利率 | 9 个品类               |
| 商品    | 补货预警     | 2 个 SKU（86、184）     |

**效果实测**：问"整体毛利率是多少"，生成的 SQL 与属性表达式逐字一致（仅多一个 `CAST(... AS NUMERIC)` 和库名前缀），返回 `0.4976692989` = 49.77%，不再出现含税/不含税口径漂移。

**术语徽章为什么仍是 `partial`**：徽章是**全体聚合**——Term 自身 2 个 flag + 每条 ColumnAttribute + 每条 SqlAttribute 的 `certified` 全部为 true 才显示 `certified`  
（实现见 `auto_ontology/dal/terms.py:_certification`）。5 个主表下共 81 个列属性，只认了 38 个核心列，所以徽章停在 `partial`，**这是预期结果，不是没生效**。

### 8.7 列属性认证：只认核心列（38 / 81）

筛选标准（四条）：

| 保留                        | 排除                                              |
| ------------------------- | ----------------------------------------------- |
| 金额 / 数量 / 税率（算钱必需）        | 代理主键（`invoicelineid`、`orderlineid`…）            |
| 日期（趋势与时效）                 | 审计列（`lasteditedby`、`lasteditedwhen`）            |
| 外键与分类（join 和分组入口）         | 自由文本（`comments`、`tags`、`deliveryinstructions`…） |
| 决定性标志位（是否冷藏 / 信用冻结 / 贷记单） | 二进制（`photo`）                                    |

各表认证情况：

| 术语    | 已认证 / 总数 | 已认证的核心列                                                                                                                              |
| ----- | -------- | ------------------------------------------------------------------------------------------------------------------------------------ |
| 发票明细行 | 8 / 12   | extendedprice, lineprofit, taxamount, taxrate, unitprice, quantity, stockitemid, description                                         |
| 销售发票  | 5 / 17   | invoicedate, iscreditnote, deliveryrun, totaldryitems, totalchilleritems                                                             |
| 客户    | 8 / 22   | customername, creditlimit, paymentdays, accountopeneddate, isoncredithold, standarddiscountpercentage, validfrom, validto            |
| 商品    | 11 / 22  | stockitemname, brand, size, ischillerstock, recommendedretailprice, unitprice, taxrate, supplierid, leadtimedays, validfrom, validto |
| 订单明细行 | 6 / 8    | quantity, pickedquantity, unitprice, taxrate, pickingcompletedwhen, description                                                      |

两个**认证前必须先核对取值**的反例（都是本轮实测抓到的）：

1. **`customers.deliverylocation` 不是城市名**。列类型是 `text`，内容为 WKT 点串 `POINT (-102.6201979 41.4972022)`（经度在前）。  
   原本按"配送城市"认证，核对取值后**已撤销认证**并改写描述。真正的地域链路是 `deliverycityid → application.cities`，  
   但这两个外键（`deliverycityid` / `postalcityid`）在 catalog 里**没有列属性**，是个已知缺口。
2. **`validto` 的哨兵值不是 `'infinity'`**。实测是写死的时间戳 `9999-12-31 23:59:59.999999`；  
   `WHERE validto = 'infinity'` 在 663 行客户表和 227 行商品表上都返回 0 行，会误判成空表。  
   正确写法 `WHERE validto > now()`。这两张主表当前**全部行都是当前版本**，直接查不需要时间过滤。

其余未认证的 43 个列大多是地址行、电话/传真/网址、备注、`runposition`、`barcode`、`quantityperouter` 等  
报表低频列，保留 `pending` 比强行盖章更诚实。

**还剩的问题**：`Archived *` 和 `website` 视图类术语仍在同一检索池里，容易被归档表带偏；建议加 Zone 隔离或把权重降下去。

---

## 9. Custom Analysis：域级分析范式

### 9.1 它和 SQL Attribute 不是一回事

| | SQL Attribute | Custom Analysis |
|---|---|---|
| 挂载位置 | **必须挂在一个 Term 下** | 全局，不挂 Term |
| 内容粒度 | 一条 SELECT，通常只算**一个指标** | 一段完整查询，示范**一类分析**（多表 join + 维度 + 窗口 + 排序） |
| 回答的问题 | "毛利率怎么算" | "客户排名这类问题该怎么查" |
| prompt 标签 | `SQL ATTRIBUTES (derived metrics/formulas — use their expressions and SQL as guidance)` | `DOMAIN-SPECIFIC CUSTOM ANALYSES (use their SQL patterns as guidance)` |
| 注入位置 | SQL 生成 | SQL 生成 **+ intent 校验**（`sql_parse_validation.py` 会把它一起塞进校验 prompt） |

一句话分工：**SQL Attribute 钉口径，Custom Analysis 教套路**。指标类的用前者，跨表/带排名/带时间序列的分析用后者。

两者都只做**指导**，LLM 会参考不会逐字照抄，所以 SQL 要写成可直接执行的样子。

### 9.2 创建方式

```bash
POST http://localhost:3001/api/custom-analyses/validate   # 先校验
{ "sql": "SELECT ..." }

POST http://localhost:3001/api/custom-analyses            # 再创建
{ "name": "...", "description": "...", "sql": "SELECT ..." }
```

约束（接口层强制）：

- `name` 与 `sql` **各自全局唯一**，重复报 409；
- SQL 必须能解析出 catalog 里的表，否则 422（解析不出表的分析等于对检索不可见）；
- 校验和 SQL Attribute 一样**只到表级、不查列**，所以建之前必须自己在库里跑一遍。

### 9.3 六个例子（全部实测执行通过 + catalog 校验 6/6，均已建入）

#### ① 客户营收排名（多表 join + 窗口排名）

```sql
SELECT c.customername,
       SUM(il.extendedprice) AS revenue,
       RANK() OVER (ORDER BY SUM(il.extendedprice) DESC) AS revenue_rank
FROM sales.invoicelines il
JOIN sales.invoices i ON i.invoiceid = il.invoiceid
JOIN sales.customers c ON c.customerid = i.customerid
WHERE i.iscreditnote = false
GROUP BY c.customername
ORDER BY revenue DESC
LIMIT 10
```

实测 Top1 = Tailspin Toys (Inguadona, MN)，438,689.81。示范 `invoicelines → invoices → customers` 这条主链，以及 `RANK()` 做 Top N。`iscreditnote = false` 是营收口径的固定动作。

#### ② 月度营收趋势与环比（CTE + LAG）

```sql
WITH monthly AS (
  SELECT date_trunc('month', i.invoicedate)::date AS month,
         SUM(il.extendedprice - il.taxamount) AS net_revenue
  FROM sales.invoicelines il
  JOIN sales.invoices i ON i.invoiceid = il.invoiceid
  WHERE i.iscreditnote = false
  GROUP BY 1
)
SELECT month, net_revenue,
       LAG(net_revenue) OVER (ORDER BY month) AS prev_month,
       ROUND((net_revenue - LAG(net_revenue) OVER (ORDER BY month))
             / NULLIF(LAG(net_revenue) OVER (ORDER BY month), 0) * 100, 2) AS mom_pct
FROM monthly
ORDER BY month
```

实测 2016-05 净营收 4,970,932.65，环比 +8.92%。所有时间序列问题都应该复用这个骨架（`date_trunc` + `LAG` + `NULLIF` 防除零）。

#### ③ 商品 ABC 分析（帕累托累计占比）

```sql
WITH item_rev AS (
  SELECT s.stockitemid, s.stockitemname, SUM(il.extendedprice) AS revenue
  FROM sales.invoicelines il
  JOIN warehouse.stockitems s ON s.stockitemid = il.stockitemid
  GROUP BY s.stockitemid, s.stockitemname
)
SELECT stockitemname, revenue,
       ROUND(revenue / SUM(revenue) OVER () * 100, 2) AS pct_of_total,
       ROUND(SUM(revenue) OVER (ORDER BY revenue DESC, stockitemid)
             / SUM(revenue) OVER () * 100, 2) AS cumulative_pct
FROM item_rev
ORDER BY revenue DESC
```

实测头部 5 个 SKU 累计占 21.04%，`cumulative_pct` 跨过 80% 的位置就是 A 类分界。`ORDER BY` 里要带 `stockitemid` 做 tie-break，否则同额商品累计占比会抖。

#### ④ 订单到发票的履约漏斗（LEFT JOIN 找缺口）

```sql
SELECT o.orderid, o.orderdate, c.customername,
       o.expecteddeliverydate, o.isundersupplybackordered
FROM sales.orders o
JOIN sales.customers c ON c.customerid = o.customerid
LEFT JOIN sales.invoices i ON i.orderid = o.orderid
WHERE i.invoiceid IS NULL
ORDER BY o.orderdate DESC
```

实测 3,085 张订单从未开票（占 73,595 的 4.2%）。**必须用 `LEFT JOIN ... IS NULL`**，不能用 `NOT IN`——子查询里有 NULL 时会静默返回空集。

#### ⑤ 销售员业绩排名（按 personid 分组，不是姓名）

```sql
SELECT p.personid, p.fullname,
       COUNT(DISTINCT i.invoiceid) AS invoice_count,
       SUM(il.extendedprice) AS revenue
FROM sales.invoicelines il
JOIN sales.invoices i ON i.invoiceid = il.invoiceid
JOIN application.people p ON p.personid = i.salespersonpersonid
GROUP BY p.personid, p.fullname
ORDER BY revenue DESC
```

实测 Top1 = Archer Lamble 20,629,803.44。**这条的重点是分组键**：库里有两个人同名 Hudson（personid 3 和 13），
`GROUP BY p.fullname` 会把两人业绩并成一个人的，排名结论直接错。

#### ⑥ 订单到开票时效分桶（CASE 分桶 + 长尾）

```sql
WITH lag_days AS (
  SELECT (i.invoicedate - o.orderdate) AS days
  FROM sales.orders o
  JOIN sales.invoices i ON i.orderid = o.orderid
)
SELECT CASE
         WHEN days <= 0  THEN '0天(当日开票)'
         WHEN days <= 2  THEN '1-2天'
         WHEN days <= 7  THEN '3-7天'
         WHEN days <= 30 THEN '8-30天'
         ELSE '超30天'
       END AS bucket,
       COUNT(*) AS invoice_count,
       ROUND(AVG(days), 1) AS avg_days
FROM lag_days
GROUP BY 1
ORDER BY MIN(days)
```

实测 66,037 张当日开票，1,484 张超 30 天且均值 193 天——长尾很明显，这类分桶能把"平均值骗人"的问题暴露出来。

### 9.4 这个库上不该拿来做例子的两类问题

设计和实测时踩到，写下来避免重复：

| 想写的分析 | 为什么放弃 |
|---|---|
| 客户流失 / RFM 分层 | 663 家客户的最近购买全部落在基准日 **61 天内**（均值 9.8 天），分层只会得到一个桶。样例库保证了每家客户都有近期订单 |
| 呆滞库存（90 天无出库）、车厢温度超标 | 前者实测 **0 个 SKU**；后者 65,998 条读数全部落在 3~5 ℃，`<2 OR >8` 命中 0 条。模式正确但本库无数据 |

这两类分析模板在**真实生产库**上很有价值，可以照 9.3 的骨架写，只是别拿这个样例库的结果当"验证通过"。

### 9.5 当前落地状态（已全部创建）

6 条已通过 `POST /api/custom-analyses` 建入，`GET /api/custom-analyses` 返回 `count = 6`。

| # | name | id |
|---|---|---|
| ① | 客户营收排名（Top N + 窗口排名） | `91a603ad-4b55-40ff-af44-1ea009c05c45` |
| ② | 月度营收趋势与环比 | `388ba8f4-074f-4cda-953b-1757018f7a39` |
| ③ | 商品 ABC 分析（帕累托） | `b5a9c1b1-485c-4a5b-8f9e-b93cd3e21e71` |
| ④ | 订单到发票的履约漏斗 | `2231f2a7-87a4-45a3-a2bf-14419531fed6` |
| ⑤ | 销售员业绩排名 | `b71ff726-00f0-4b6b-bc4b-ce4dc4ca2f7b` |
| ⑥ | 订单到开票时效分桶 | `fe73e351-aebc-47a4-a209-2d5a181cadb5` |

**向量落库情况**（`auto_ontology` 库的 `vdb.semantic_layer`）：

```sql
SELECT label, database_name, count(*) FROM vdb.semantic_layer GROUP BY 1,2 ORDER BY 1,2;
-- CustomAnalysis | wideworldimporters | 6
```

`database_name` 必须非空——检索时 `search_semantic_index` 会按库过滤命中，
嵌入时若解析不出库名（SQL 引用了未入 catalog 的表）会**降级为无库名嵌入，等于检索永远搜不到**。
`_embed_analysis()` 里对此有 WARNING 日志，创建后应检查一下有没有这条告警。

**端到端验证**（真实对话，非仅校验）：

| 提问 | 生成的 SQL | 结论 |
|---|---|---|
| 按客户营收排名，给我前 5 名客户 | 与 ① 结构同构（同 join 链、`iscreditnote = false`、`RANK() OVER`），仅 `ORDER BY revenue_rank LIMIT 5` 替代原 `ORDER BY revenue DESC LIMIT 10` | 范式被采用，Top1 = 438,689.81 与实测一致 |
| 每个月的营收和环比增长率 | 与 ② **逐字相同**（仅多 `wideworldimporters.` 前缀与空白差异） | 范式被直接复用 |

**幂等性提醒**：`name` 和 `sql` 各自全局唯一。想改内容只能 `PUT /api/custom-analyses/{id}`，
不能"换个名字重建"——`sql` 相同会被 409 挡掉。

---

## 10. PQL 分析：预测，不是取数

### 10.1 它到底是什么

PQL = **Kumo Predictive Query Language**，是 KumoRFM 图模型的查询语言。写它的目的不是"把数据捞出来"，
而是**给定一个实体和时间窗口，让模型输出概率或预测值**。

三者的分工：

| | SQL / Custom Analysis | PQL / PQL Analysis |
|---|---|---|
| 回答 | 已经发生了什么（聚合、排名、趋势） | **接下来会发生什么**（会不会、多少次、哪个） |
| 输入 | 表 + 列 + 过滤 | 实体表主键 + 时间窗口 `(start, end, unit)` |
| 输出 | 结果行 | **逐实体的概率 / 预测值**，不是明细 |
| 依赖 | 关系库 | KumoRFM 图模型（表要建成图、边要是外键） |
| 注入位置 | SQL 生成 prompt | **PQL 生成 prompt 的 "Verified examples" 段**（few-shot） |

关键差别：PQL **没有 GROUP BY**。它只能给出「每个客户一个概率」，要按区域/品类汇总，
是代码在旁边用一次只读 SELECT 把实体属性 join 回来再聚合（`aggregate_prediction_by`）。

### 10.2 开启条件（2026-11-09 更新：两条路线都可走）

**先纠正一个容易搞错的开关**：代码里的开关是 **URL**，不是 API KEY。
`_prediction_enabled()` 现在认两个变量，任一存在即开启：

```python
# auto_ontology/retrieval/text_to_sql/text_to_sql_graph.py
def _prediction_enabled() -> bool:
    return bool(
        os.environ.get("KUMO_RFM_API_URL") or os.environ.get("KUMO_HOSTED_API_URL")
    )
```

| 变量 | 后端 | 谁解析 PQL | 需要 `--extra kumo` | 数据去向 |
|---|---|---|---|---|
| `KUMO_RFM_API_URL` | 自托管 Universal TFM NIM | 服务端 | 是 | 你自己的网络 |
| `KUMO_HOSTED_API_URL` | build.nvidia.com（填 `public`） | **本地**（见 10.12） | 否 | NVIDIA 的端点 |

两者都设时 NIM 优先。

旧版 README、`.env` 注释、以及 `main.py` 的报错文案都写"设 `KUMO_RFM_API_KEY` 即开启"，
与实现不符。理由在函数 docstring：NIM 按契约不鉴权，`KUMO_RFM_API_KEY` 只在前面挂网关时
携带凭证，所以它是可选的。上述三处文案已一并修正。

开启要同时满足的条件：

| # | 条件 | 自托管 NIM | 托管端点 |
|---|---|---|---|
| 1 | 镜像依赖 | 需 `--extra kumo` | 不需要 |
| 2 | PQL few-shot 语料 | ✅ 已建 6 条（见 10.6） | ✅ 同左 |
| 3 | 端点/凭证 | ⬜ 待提供 NIM 地址 | ✅ `nvapi-…` key 已配置 |

自托管那一列的第 3 项是硬阻断，且只能由部署侧解决：NIM 是自持服务，
`RelationalClient(url)` 需要的是一个 HTTP base URL（health 探针走 `/v1/health/ready`），
client 包里**没有任何默认端点**。装的是 `kumo-relational-client`，但推理在 NIM 侧——
vendor 里的 `kumo_relational_engine` wheel 解包后没有 `.so`，全是 Python，可以确认它不是
本地推理引擎。

**未开启时的具体表现**（可从日志/接口区分）：

- 分类节点（classify）不建进流程图，`/chat` 永远不会走到预测分支，预测类问题回落成 SQL；
- 显式传 `prediction: true` 抛
  `prediction=true was requested but the prediction flow is not configured on this deployment (neither KUMO_RFM_API_URL nor KUMO_HOSTED_API_URL is set)`；
- `_prediction_enabled()` 在**启动时求值一次**，改了 env 必须重启容器才生效，热改无效。

**数据外传**：回答一次预测要读取相关表的受限样本并上传到服务端。预算按
`KUMO_MAX_TABLES`（20）/ `KUMO_MAX_BYTES`（2 GiB）/ `KUMO_MAX_BUILD_SECONDS`（300）执行，
**超预算直接拒绝并说明超了什么，而不是静默截断**（`kumo/budget.py` 的注释明确说截断会
"答另一个问题"）。`KUMO_MAX_ROWS_PER_TABLE` 保留但默认不设，因为它正是被设计成要避免的截断。

> 金融机构落地前，这一项要走数据出域评审。传的是"相关表 + 受限样本"，不是全库。
> 自托管 NIM 不改变这一点（样本仍要发给 NIM），但至少不出自己的网络。


### 10.3 存储与检索的三个约定（容易踩）

```bash
POST http://localhost:3001/api/pql-analyses
{ "name": "...", "description": "...", "pql": "PREDICT ..." }
```

1. **`name` 不是标题，是 few-shot 里的 "Q:"**。检索层固定把
   `name → question`、`description → "Why:"`、`pql → 代码块`（`retrieval/kumo/rag.py`）。
   所以 name 要写成一句自然语言问句（「未来 90 天哪些客户会流失？」），
   写成「客户流失预测」这种标题，送进 prompt 的示范就是残缺的。
2. **没有 SQL 那种校验环节**。`create_pql_analysis` 不解析不校验，存什么样就什么样。
   Custom Analysis 至少会过一遍 catalog 解析，PQL 没有这道闸。
3. **`name` 与 `pql` 各自全局唯一，且没有数据库作用域**。
   Custom Analysis 嵌入时带 `database_name`，检索按库过滤；PQL 分析嵌入时
   `database_name` 是 `None`，`fetch_pql_examples` 也不传库名 —— **所有库共用一套 PQL 语料**。
   默认取 5 条（`KUMO_MAX_PQL_EXAMPLES`）。

UI 位置：`/analysis` 页面上的 **PQL** 标签页（和 Custom Analyses 同页切换）。

### 10.4 语法骨架

```
PREDICT <target> [RANK TOP k] FOR EACH <实体表>.<主键> [WHERE <实体属性过滤>]
PREDICT <target> [RANK TOP k] FOR <实体表>.<主键> = <值>
```

`<target>` 的形态直接决定任务类型：

| target 写法 | 任务 |
|---|---|
| `COUNT/SUM/AVG/MIN/MAX(<事件表>.<列>, start, end, unit)` | 回归（预测一个数值） |
| 上面再套比较，如 `COUNT(...) > 0`、`= 0` | 二分类（概率） |
| 直接一个分类型/ID 列 | 多分类 |
| 直接一个数值列 | 回归 |
| `LIST_DISTINCT(<事件表>.<外键>, start, end, unit) RANK TOP k` | 推荐 / 链接预测 |

窗口 `(start, end, unit)` 是**相对数据锚点时间**的前向窗口，`unit ∈ seconds/minutes/hours/days/weeks/months`。

### 10.5 六个例子（本库结构下静态校验通过）

已用项目自带的 `validate_pql_static` 逐条跑过，且**只喂了源库声明的外键**这一最保守的边集，
所以只要声明外键成立，它们就不会被"没有直接外键"那条规则打回。

```pql
-- ① 客户未来 90 天会不会下单（二分类 → 概率）
PREDICT COUNT(orders.*, 0, 90, days) > 0 FOR EACH customers.customerid
```

```pql
-- ② 客户未来 90 天下单次数（回归 → 数值）
PREDICT COUNT(orders.*, 0, 90, days) FOR EACH customers.customerid
```

```pql
-- ③ 客户流失风险：未来 90 天一张发票都没有（二分类）
PREDICT COUNT(invoices.*, 0, 90, days) = 0 FOR EACH customers.customerid
```

```pql
-- ④ 商品未来 90 天出库次数（事件内过滤 + 回归）
PREDICT COUNT(stockitemtransactions.* WHERE stockitemtransactions.quantity < 0, 0, 90, days)
FOR EACH stockitems.stockitemid
```

```pql
-- ⑤ 客户细分预测（直接分类型列 → 多分类）
PREDICT customers.customercategoryid FOR EACH customers.customerid
```

```pql
-- ⑥ 单客户解释（单实体，不是 FOR EACH）
PREDICT COUNT(orders.*, 0, 90, days) FOR customers.customerid = 1
```

### 10.6 五个反例（必须被拒绝，实测确实被拒）

| 写法 | 为什么错 | 静态校验给的错 |
|---|---|---|
| `SUM(invoicelines.extendedprice, 0, 90, days) FOR EACH customers.customerid` | `invoicelines` 没有到 `customers` 的直接外键（只到 `invoices`），聚合内不跨多跳 | `has no direct foreign key to the prediction entity` |
| `COUNT(orders.* WHERE customers.customercategoryid = 3, 0, 90, days)` | 聚合的 WHERE 只能过滤被聚合的那张表 | `can only filter columns of 'orders'` |
| `... FOR EACH customers.customerid RANK TOP 5` | `RANK TOP` 必须在 target 之后、`FOR EACH` 之前 | `RANK TOP is misplaced` |
| 在 `ASSUMING` 里写 `IN (SELECT ...)` | PQL 没有子查询 | `PQL has no SQL subqueries` |
| `WHERE customers.accountopeneddate < CURRENT_DATE` | 窗口已经用 `(start, end, unit)` 表达，不能用 SQL 时间函数 | `does not support the SQL time expression` |

### 10.7 这个库写 PQL 的三条硬限制

不是语法问题，是**数据模型**决定的，写之前得先认：

1. **「客户未来营收」写不出来。** 金额只在 `invoicelines.extendedprice`，而 `invoicelines`
   只声明了到 `invoices` 的外键，`invoices` 也没有任何金额合计列。
   到 `customers` 是两跳，聚合内不允许 → 只能预测「开票张数」`COUNT(invoices.*, ...)`，
   金额要么把锚定口径下沉成一张客户级日/月汇总表喂进图，要么放弃。
2. **`invoicelines` / `orderlines` 的 `stockitemid` 没有声明外键。**
   所以「按商品预测销量」不能聚合 `invoicelines`；能直接连 `stockitems` 的时序表只有
   `stockitemtransactions`（库存流水），例子④ 就是因为它才成立。
3. **同名表冲突隐患。** catalog 里 `sales.customers` 和 `website.customers` 都叫 `customers`，
   图按裸表名建。本库当前只挂了 50 个术语、`website` 视图基本没进图，短期内不炸，
   但一旦把 `website` 那批表也纳入相关表，`customers` 会指代不明。

另外 `FORECAST n TIMEFRAMES`（多步预测）仓库里**没有一条可参照的实例**，
位置只能从 prompt 描述推断，且必须单实体；我无法离线验证，不建议现在就写进语料。

### 10.8 落地状态：6 条语料已建入（2026-11-09）

上面 10.5 的六条已全部通过 `POST /api/pql-analyses` 建入，`name` 按 10.3 的约定写成了问句。

| # | name（= few-shot 的 "Q:"） | id |
|---|---|---|
| ① | 未来 90 天哪些客户会下单？给每个客户一个概率。 | `40ea7049-598f-41cd-916f-8a1925838118` |
| ② | 未来 90 天每个客户会下多少单？ | `fe911bb9-309c-4d72-9426-0ffaf3e5c5a7` |
| ③ | 未来 90 天一张发票都不会有的客户是哪些？ | `f9412bfe-e6e4-4762-8248-44576d402078` |
| ④ | 未来 90 天每个商品会出库多少次？ | `0040f910-e14b-4052-bddf-d4926a149de8` |
| ⑤ | 预测每个客户属于哪个客户分类？ | `0a9af0e4-12f7-4f25-bc83-f38715226139` |
| ⑥ | 解释一下某个具体客户为什么未来 90 天可能不下单。 | `e2469bfb-0f31-489b-a116-1503b5145da9` |

**向量已落库且符合预期**（`vdb.semantic_layer`）：

```sql
SELECT label, database_name, count(*) FROM vdb.semantic_layer
WHERE label ILIKE '%pql%' GROUP BY 1,2;
-- PqlAnalysis | (NULL) | 6
```

`database_name` 为空是**正确的**——这条链路本来就没有库作用域（见 10.3 第 3 条）。
所以这 6 条会同时作用于这台实例上连接的所有库，不区分 `wideworldimporters`。

`description` 我按"为什么这么写"来写，不是把 name 重复一遍，因为它对应 prompt 里的 `Why:`。
比如 ③ 说明为什么用 `invoices` 而不是 `orders`（已开票口径才算成交），
④ 说明为什么不能用 `invoicelines`（`stockitemid` 没声明外键），
⑥ 说明那个 `customerid = 1` 只是语法占位、必须替换成真实主键。

**尚未验证的一环**：因为没有可用 NIM 端点，这 6 条**还没有经过真实预测链路的端到端验证**。
它们目前只证明了「语法通过静态校验」+「能进向量库并能被 `fetch_pql_examples` 的检索条件命中」。
端点接通后，第一件该做的事是拿 ①②③ 各问一次，确认 few-shot 真的被引用、返回的是概率而不是 SQL。

### 10.9 拿到端点后的三步

镜像已重建好（`uv sync --extra kumo` 生效，client 与 engine 均为 1.0.0），
在旧镜像上验证过 `import kumo_relational_client` 失败、新镜像成功。剩下三步：

**① 验端点，别直接改 env。** 只有两个必要条件：readiness 通过、且服务的是
`kumo-relational` 而不是旧的 `kumo-rfm`（旧模型在 client 启动阶段就会被拒）。

```bash
docker run --rm -e KUMO_RFM_API_URL="$URL" --entrypoint python auto-ontology:latest -c "
from kumo_relational_client import RelationalClient
c = RelationalClient('$URL')
print('ready :', c.health_ready())
print('models:', c.models)
c.close()"
```

**② 填 `.env` 并重启。** 只填 URL，`KUMO_RFM_API_KEY` 留空（NIM 按契约不鉴权）。

```bash
# .env
KUMO_RFM_API_URL=<NIM base url>
docker compose up -d auto-ontology   # _prediction_enabled() 只在启动时求值，必须重启
```

**③ 确认节点真的建进来了。** 重启后看日志有没有 prediction 相关节点，然后问一句
「未来 90 天哪些客户会下单」，返回的应当是逐客户概率，而不是一段 SQL。
如果仍回落成 SQL，说明 env 没进到容器（`docker exec auto-ontology env | grep KUMO`）。

⚠️ 重启会触发一次语义重编译（约 7 分钟），期间问数可能报
`doesn't reference any table known to the catalog`，等它跑完即可，不是回归。

### 10.10 端点从哪来：只有自托管一条路（2026-11-09 实测）

**申请入口只有一个**：<https://build.nvidia.com/nvidia/kumo-relational>

页面右上 **Get API Key** 生成 `nvapi-…`。这个 key 同时是 `nvcr.io` 的登录密码，
不需要另外在 NGC 注册（同一套 NVIDIA 账号）：

```bash
docker login nvcr.io          # Username: $oauthtoken   Password: <那个 nvapi- key>
docker run -it --rm --gpus all --shm-size=16GB -p 8000:8000 \
  nvcr.io/nim/nvidia/kumo-relational:1.1.0
```

**但 build.nvidia.com 上的免费 Prototype 端点接不进这套系统**，这点必须说清楚，
否则会以为拿了 key 就能开。实测：

| 探测 | 结果 |
|---|---|
| `POST https://ai.api.nvidia.com/v1/structured-data/nvidia/kumo-relational/predictions`（无 key） | `401` `Header of type 'authorization' was missing` |
| 同上，带一个无效 key | `403` |
| `GET https://ai.api.nvidia.com/v1/health/ready` | **`404`** |
| `GET …/nvidia/kumo-relational/v1/health/ready` | **`404`** |
| `POST …/v1/sessions`（三个候选路径） | **`404` 全中** |

原因是本项目的预测走 engine 的 **session 协议**，且每次 URL 预测都会强制做一次
`authenticate()`（调用链：`RelationalClient.relational().predict` →
`adapter.predict` → `engine.init_client` → `_configure` → `engine.init`
→ `NimClient.authenticate()`）：

```python
# kumo_relational_engine/client/client.py:386,408
ready  = self._probe('/v1/health/ready')   # 必须 status == 'ready'
models = self._probe('/v1/models')         # 必须列出 id == 'kumo-relational'
```

托管端两处都 404 → 首跳就抛 `NIM at '…' is not ready`，**永远不会走到预测**。
它只提供 `/v1/predictions` 单片调用，请求体是 `task/schema/context/predict` 平铺结构，
与 engine 的 session payload 不是一回事。

所以：**托管 key 不能当 `KUMO_RFM_API_URL`**——但它并不是废的，它本人能正常推理
（见 10.11 实测：官方示例 0.93 秒返回、概率 0.991），只是协议不同、且**中国 IP 拉不到镜像**。

**自托管的硬件门槛**（官方 NGC 列表）：

| 项 | 要求 |
|---|---|
| GPU | A10G / **L4** / L40S / H100（这四款，别的不保证） |
| 操作系统 | **Linux**（macOS/Windows 不支持） |
| CUDA | 12.4+ |
| 容器参数 | 必须 `--gpus all --shm-size=16GB`，默认监听 8000 |
| 参数量 | 3.1×10⁷（很小，L4 24GB 级别足够） |

本机是 **arm64 macOS**，`docker info` 的 runtimes 只有 `runc`（无 `nvidia`），
`nvidia-smi` 不存在 → **本地起不来**，必须放到 Linux GPU 机器或云上。

**License 有个必须核实的分歧**（金融机构尤其要看）：

- NGC catalog 的 kumo-relational 容器页写：*"This model is licensed for
  **non-commercial research or evaluation purposes only**"*
- build.nvidia.com 的 model card 写：*"ready for **commercial or
  non-commercial** use"*（NVIDIA Open Model License）

两处口径不一致。**商用前必须找 NVIDIA 商务书面确认**，别按任一处默认理解。

**数据出域对比**（这是自托管相对托管端点的真正好处）：

| 方式 | 数据是否离开你的环境 |
|---|---|
| build.nvidia.com 托管端点 | 是，要上传样本行 |
| **自托管 NIM** | **否**，容器在你自己的 GPU 上 |
| HuggingFace 权重自家跑 | 否，但要自己承担 serving |

对银行场景，只有后两种可接受——这也是唯一走得通的路。

**建议的验证顺序**（先便宜后贵）：

1. 云上按小时租一台 **L4 或 A10G** 的 Linux 实例，起 1.1.0 容器；
2. `docker exec auto-ontology python /tmp/kumo_smoke.py` 验通（health + models 两项）；
3. 填 `KUMO_RFM_API_URL=http://<gpu-host>:8000` 重启，问 ①②③ 验 few-shot；
4. 效果确认后再谈本地 GPU 采购与商务许可，别反过来。




### 10.11 拿到 key 之后的实测记录（2026-11-09）

这一节是 10.10 的续篇：key 到手后逐项验证，结论有三条，其中第三条是**路线级的坏消息**。

**① key 是真的能用，而且比预期权限大**

| 动作 | 结果 |
|---|---|
| `docker login nvcr.io -u '$oauthtoken'` | **`Login Succeeded`** |
| `GET https://integrate.api.nvidia.com/v1/models` | `200`，84 个模型（**列表里没有 kumo**，它在独立路径下） |
| `POST …/kumo-relational/predictions`（官方示例原样） | **`200`，0.93 秒**，返回 `true: 0.991` |
| `docker manifest inspect nvcr.io/nim/nvidia/kumo-relational:1.1.0` | **`denied`——`CND 必需`** |

最后一行是关键：**NIM 镜像对中国 IP 拒绝下载**，NGC 明写「自 2025-02-15 起，任何来自
中国 IP 的 API/CLI 下载 NIM 镜像与模型的请求都会被拒绝」，要求走
<https://catalog.ngc.nvidia.com/china-nim-distributors> 的中国区合作伙伴。
`api.ngc.nvidia.com` 也返回 `NOT_AVAILABLE_FOR_LEGAL_REASONS`。
所以「登录成功」不等于「拉得到镜像」，这两件事必须分开验。

**② 托管端点的真实协议（逐层探出来的，官方文档没写全）**

```jsonc
{
  "model": "kumo-relational",            // 或 kumo-tabular
  "task": {
    "kind": "binary_classification",      // 或 regression / multiclass / forecasting
    "target": { "column_name": "label", "dtype": "bool",
                "classes": ["false","true"], "positive_class": "true" },
    "entity_table_names": ["customers"],  // 必须是已声明的表
    "anchor_time_column": "anchor_time"
  },
  "schema": {
    "instance_table": { "columns": { "<col>": {"dtype":..., "stype":...} }, "primary_key": "instance_id" },
    "related_tables": { "<name>": { "columns": {...}, "primary_key": [...] } },
    "relationships": [
      { "source_table": "instance_table", "source_columns": ["customerid"],
        "target_table": "customers", "target_columns": ["customerid"] }
    ]
  },
  "context": { "instance_table": {"format":"arrays","columns":[...],"rows":[...]},
               "related_tables": { ... } },
  "predict": { ... },
  "output": { "fields": ["prediction","probabilities"] }
}
```

四个**必须知道的**硬约束，都是报错逼出来的：

1. **每个 related table 都必须带 `instance_id` 列** —— 托管端收的是「按实例物化好的行」，
   不是规范化的外键表。报错原文：`Kumo Relational requires a declared 'instance_id' column`。
   这件事本来就是 SDK 的 `materialize()` 干的，手搓等于重写它。
2. **实体表要同时作为 related table 声明**，并且有一条**直连 instance_table** 的关系边。
   报错原文：`Related entity table 'customers' requires exactly one direct instance_table relationship`。
3. `relationships` 元素需要 `source_table` 区分是哪张表发起的边；**少了它或边写错不会报错，
   会静默退化成只用先验**（这正是下面第 ③ 条排查了半天的东西）。
4. **时间戳必须是 RFC3339 带 `Z`**。踩过的坑：`psql -A` 直接输出的 timestamp 是纯日期
   `2013-01-03`，后面补个 `Z` 变成 `2013-01-03Z`，报
   `timestamp[us] values must be RFC 3339 strings with a Z suffix`。
   正确做法是在 SQL 侧 `to_char(ts,'YYYY-MM-DD"T"HH24:MI:SS') || 'Z'`。

**③ 用两个 sanity check 证明「接线是对的，是任务不可预测」**

真实任务跑出来 AUC 只有 0.44~0.51，概率全挤在 0.32~0.34（等于只输出先验）。为了分清是
「payload 写错」还是「数据没信号」，做了两个标签由特征**直接决定**的对照实验：

| 对照实验 | 结果 |
|---|---|
| 标签 = `creditlimit > 中位数`（特征在 related 表里） | **AUC 1.000，准确率 1.000，概率区间 0.003–1.000** |
| 标签 = 历史发票数 ≥ 中位数（特征在 invoices 表里，需跨表遍历） | **AUC 1.000，概率区间 0.005–0.693** |

两次都满分 → 图遍历、related table、时间窗全部接通。**所以真实任务上的 0.5 是数据结论，不是 bug。**

真实任务：预测「未来 90 天发票数 ≥ 10」，200 个客户、2016-02-01 锚点、真实结果回算 AUC。

| 配置 | AUC |
|---|---|
| 单锚点（200 个实例全在同一时点） | 0.436 |
| 滚动 8 个季度锚点（960 个 context 实例） | 0.506 |
| 再加发票金额、行数特征 | 0.438 |

原因是这个库的客户行为**几乎没有个体差异**：90 天内至少开 1 张票的客户是 **100%**，
发票数区间只有 1–20、中位 8，且每个客户基本按月开票。这种「人人都在稳定下单」的数据上，
任何模型都学不到区分度。

**这条结论的实际价值**：Kumo 要用出效果，目标必须是**客户间行为真有差异**的
（流失/沉睡、下次订单金额、品类偏好迁移、账期违约），而且**必须把金额放进图里**——
只用「有没有、几张」这种计数特征，在这个库上等于白跑。

**④ 官方 SDK 这条路也堵着**

`pip install structured-data-models-client` 能装上（`Schema` / `PredictiveQuery` 都有），
`Schema.from_data()` 还能自动推断出 `invoices.customerid -> customers.customerid`。
但 `PredictiveQuery.materialize()` 需要 `structured-data-models-relational-engine`，
**PyPI 上没有任何版本**（本项目 vendor 的是另一代 `kumo_relational_*` 包，模块名都对不上）。
所以「用 SDK 生成 payload、自己 POST 到托管端点」这条捷径走不通，只能像上面那样手搓。

**⑤ 现在只剩两条路**

| 路线 | 前提 | 代价 |
|---|---|---|
| **A. 自托管 NIM** | 中国区合作伙伴拿到镜像 + 一台 Linux GPU（L4/A10G） | 数据不出域，**银行场景唯一可接受**；要采购/租用 |
| **B. 改造走托管端点** | 无需 GPU | 数据要外传 NVIDIA（合规上基本否）+ 要自己实现物化层（等价重写 SDK） |

`KUMO_RFM_API_URL` 仍然只有一个正确填法：指向**自托管 NIM** 的 `http://<host>:8000`。
托管端点即使能通，engine 的 `authenticate()` 也会因为 `/v1/health/ready` 与 `/v1/models`
都 404 而在第一跳失败。

（路线 B 已落地实现，见下一节。上面这张表里的"要自己实现物化层"就是 10.12 做完的事。）


### 10.12 托管端点改造（2026-11-09 完成，路线 B）

托管端点不接 PQL，只接**已经物化好的 task**。SDK 里做这层翻译的是
`PredictiveQuery.materialize()`，而它依赖的 `structured-data-models-relational-engine`
在 PyPI 上不存在（见 10.11 ④）。所以这一步只能自己实现，就是本节的内容。

#### 四个新模块与改动点

| 文件 | 职责 |
|---|---|
| `retrieval/kumo/pql_compile.py` | PQL → `TaskSpec`（kind / target / entity / anchor / window / 比较），并**拒绝**托管端点表达不了的形状 |
| `retrieval/kumo/hosted.py` | `HostedKumoModel`：物化成 payload、发 HTTP、把结果还原成引擎同形的 DataFrame |
| `retrieval/kumo/framed_graph.py` | 用 pandas 帧直接描述"图"（表/列/stype/主键/时间列），**不 import 引擎包** |
| `retrieval/kumo/predictor.py` | 按后端选 `KumoModel` 或 `HostedKumoModel`；缓存键带后端标识 |

`pql_gen.py`（1850 行）**一行没改**：它只依赖 `kumo_model.validate_pql` 和
`kumo_model.predict` 两个方法，`HostedKumoModel` 实现了同样的签名，于是生成 / 报错回馈 /
修复的循环原样复用。编译失败抛的 `PqlCompileError`（`ValueError` 子类）就是修复信号。

#### 物化的三个关键决定

1. **滚动锚点**。context 实例不是"一张快照"，而是在历史时间轴上等距切 `K` 个锚点
   （`KUMO_HISTORY_ANCHORS`，默认 8），每个 `(实体, 锚点)` 一行，label 由该锚点**之后**的
   窗口算出来。predict 实例的锚点由调用方决定，且在所有 context 窗口关闭之后 ——
   `pql_gen._forecast_anchor` 会把它退到 `数据末端 − (窗口长度 + 7 天)`，让
   `[anchor, anchor + 窗口]` 完整落在已观测数据里；只有调用方不给锚点时，
   `HostedKumoModel._anchors` 才回落到数据末端。**两边都用调用方的值，不许夹取**：
   一旦把退回来的锚点顶到数据末端，窗口就整体落在数据之外，正是退让要避免的退化外推，
   提示语里的日期也会和结果行里的 `anchor_timestamp` 对不上。
2. **不许时间穿越**。相关表的行只有在时间 `<= 该实例的锚点` 时才进入该实例。这条是
   `test_hosted.py::test_related_rows_never_postdate_their_anchor` 守着的。
3. **时间可以继承**。明细表常常没有日期（`sales.invoicelines` 的日期在 `invoices` 上），
   这时沿外键往父表找时间列（最多 3 跳）。没有这一步，明细表的行会全部被当成"永久有效"，
   anchor 切割失效 —— 既泄漏又失真。

#### 托管端点的能力边界（实测，不是推测）

| 能力 | 状态 | 说明 |
|---|---|---|
| `binary_classification`（`COUNT(...) > 0` / `= 0`） | ✅ | 返回 `False_PROB` / `True_PROB` |
| `regression`（`SUM/AVG/MIN/MAX`、无比较） | ✅ | `output.fields` **不能**带 `probabilities`（否则 422） |
| `multiclass_classification`（直接列） | ✅ | 返回每个类一列 `<class>_PROB` |
| `LIST_DISTINCT ... RANK TOP`（推荐） | ❌ | 要求 `entity_table_names` 恰好两个实体，单实体图表达不了 |
| `FORECAST n TIMEFRAMES` | ❌ | 要 `task.step_size`，且输出是多期轨迹 |
| `ASSUMING`（反事实） | ❌ | 无对应字段 |
| 两个目标 `AND`/`OR` | ❌ | 一个 task 只有一个 target |

被拒的四种**不是静默降级**，而是回一句可修复的话给 LLM，让它改写成能答的形状
（例如"哪些客户可能下单"从 `LIST_DISTINCT` 改成 `COUNT(items.*, 0, 90, days) > 0`，
概率本身就能排序）。

#### 三个实测踩到的协议约束

1. `schema.instance_table.primary_key` 必须**字面等于** `instance_id` —— 不能用内部前缀。
   同理 `anchor_time` 由 `task.anchor_time_column` 按名字指过来。
2. 每个 `related_tables` 条目都要带 `instance_id` 列，且实体表**同时**作为 related table
   出现，并有一条直连 `instance_table` 的边。
3. `relationships` 元素必须带 `source_table`；**边写错不报错**，会静默退化成只用先验。
   所以 `HostedKumoModel` 里实体键的传递是由 catalog join paths 显式建出来的。

另外：`int64` 列里混进 `null` 会让 pandas 把整列升为 float，schema 却仍声明 `int64`，
端点直接 422。`_clean()` 用 object dtype 保住 Python 标量类型，就是为了这个。

#### 联调时暴露的四个问题（都已修）

前面几张表是照着端点契约读出来的，真跑起来还有四个只有在真实数据上才会露出来的坑。
它们都不是"配置不对"，而是物化层的逻辑缺陷，值得记住：

**① 明细表的归属算错了（最严重，且是静默错）**

`sales.invoicelines → sales.invoices → sales.customers` 是两跳。`_attach_entity_key`
原来用"上一跳产出的键"去连下一跳，等于把 `invoices.invoiceid` 当成 `customers.customerid`。
后果不是报错，而是：

- 22.8 万行 `invoicelines` 只进去了 **1.6 万行**（恰好是 `invoiceid ≤ 663` 的那些，
  因为发票号碰巧等于某个客户号）；
- 进去的那部分还**记在了错误的客户名下**；
- `PREDICT SUM(invoicelines.extendedprice, 0, 30, days)` 的标签**全部为 0**，
  模型只能学到先验，663 个客户全返回 `0.0`。

修法是每一跳把"下一跳要读的列"一起带过来（`path[i+1][0]`），因为那一列在被连的父表里，
事后再去读就读不到了。修完之后：`invoicelines` 上下文行数 1.6 万 → 32.3 万，
标签均值 7441、最大 49697，预测输出从全 0 变成每客户 6 千上下。
回归测试：`test_hosted.py::test_a_two_hop_detail_table_is_attributed_through_its_parent`。

**② 端点请求体硬上限 30 MiB**

实测 `curl` 直接打端点，32 MB 的 body 返回：

```
413 {"type":"/problems/payload-too-large","detail":"request body exceeds configured limit 31457280 bytes."}
```

走本地代理时报的却是 `Broken pipe`，更容易被误判成网络问题。

于是加了 `KUMO_MAX_PAYLOAD_BYTES`（默认 28 MiB，留 2 MiB 余量）：payload 超预算时按
**均匀抽样**把相关表降下来，并打日志说明这次抽掉了多少行 —— 不像 `tail()` 那样截断
（按行序截断会把后面实体的行整段丢掉，前面几个实体毫发无伤，是最坏的一种截）。

**③ `max_related_rows` 原来用 `tail()` 截断**

同上：`tail()` 只留最后几行，等于只保住最后几个实例的历史。改成等距抽样 + 日志。
实测 10 张表的图会一次冲到 398 MB，抽样两轮到限额内，最终请求 81 MB → 抽样后 111,798 行。

**④ categorical 基数上限 10,000**

```
422 {"title":"Unsupported shape","detail":"categorical cardinality 15035 exceeds limit 10000."}
```

语义层把某个自由文本列标成了 `categorical`（1.5 万个取值）。`_column_spec` 现在会数一遍
基数，超过 10,000 就不再按 categorical 送：

| 列的实际类型 | 降级为 | 为什么 |
|---|---|---|
| 字符串 / 自由文本 | `text` | 它本来就是文本，声明成 text 是诚实的 |
| 数值（`int64`/`float64`） | `numerical` | 送 `text` 会让 dtype 与 stype 互相矛盾（int64 + text），端点只能自己再解释一遍，同一个 422 会再来 |

**⑤ 训练行上限 10,000（N_train）**

```
422 {"title":"Unsupported shape","detail":"N_train 12960 exceeds limit 10000."}
```

context 表是每个 `(实体, 锚点)` 一行，8 个锚点 × 1620 个实体 = 12960 行就超了。
`KUMO_MAX_INSTANCES` 只限制实体数，不限制"实体 × 锚点"。现在 `_fit_context` 会从**最老的锚点**
开始裁，让乘积落进 10,000 内 —— 保留全部实体，只损失最早的历史（最新的一段最有信息量）。

**⑥ 单次预测实体上限 1,000**

```
422 {"title":"Unsupported shape","detail":"predict.instance_table.rows has 1800; maximum is 1000."}
```

默认 `KUMO_MAX_INSTANCES=2000` 本身就越过了这个上限。`_entity_keys` 现在再压一道到 1,000 并打日志，
否则一张 1500 行的表会让任何问题都失败。

**⑦ 形状被拒时的自愈**

上面四个上限里，凡是"请求太宽"这一类（而不是"问错了"），`predict` 不再直接把 422 抛给用户：
`_predict_response` 会按端点回显的 `exceeds limit N` 重算一遍列基数（按**实际发出的行**数，不是仓库表），
把最宽的那几列从 schema 和行里一起摘掉再发一次（最多两轮：先摘 categorical/text，再摘相关表自己的
ID 键）。关系连接用的列永不摘除 —— 摘了会把一个 422 换成另一个。

日志会点名列宽和摘掉的列，因为端点只回基数、不回列名：

```
kumo: the endpoint refused this shape (categorical cardinality 12960 exceeds limit 10000);
widest columns were orders.order_id(ID)=15019, orders.order_total(numerical)=14859, ...;
retrying without orders.order_id
```

`pql_gen` 里这个错误也被归为确定性失败：不再拿剩余 4 次尝试去重生成 PQL（重生成救不回一张太宽的表），
而是直接给一句能读懂的解释。

#### 无 catalog join paths 时的兜底建边

应用内第一次跑，`prepare_candidates` 没给出 `attribute_join_paths`，图就一条边都没有，
PQL 直接报 `'invoices' has no path to the prediction entity 'customers'`。

托管路径没有引擎的 `infer_links()` 可退，所以 `framed_graph.infer_links_by_name()` 自己长了一层，
但比引擎保守得多：

| 条件 | 说明 |
|---|---|
| 父表必须是**单列**主键 | 复合键没法用一列外键表达 |
| 子表要有**同名**列 | 先把候选缩到"名字对得上"的那些 |
| 抽样值重叠 ≥ 80% | 名字对上但值不重叠的（如两个都叫 `id` 的自增表）判否 |
| 不是子表**自己的**单列主键 | 两个独立编号的 `id` 表不该相连 |
| 每个外键**只连一个**父表 | `sales.customers` 与 `website.customers` 是同一维度的两份，都连上会分散模型注意力 |

实测在 10 张表的图上一口气推出 9 条边，问答随即恢复正常。

#### 标签构造的一个致命 bug：零订单被当成了"没有值"

`_aggregate` 用 `groupby(_ENTITY_KEY).size()` 算窗口内的计数，**窗口里没有任何行的实体
根本不在结果里**。取值时 `.get(key)` 拿到 `None`，而 `_compare(None, ...)` 一律返回
`False`。于是一条 `COUNT(...) = 0` 的流失标签，对真正"零订单"的客户判成了
"有订单"—— 正好反了，而且不报错。

回归分支早就处理了这个空窗（`_ZERO_ON_EMPTY`：COUNT/SUM 记 0，AVG/MIN/MAX 记 None），
二元分支漏了。后果用 WWI 实测最清楚：

| PQL | 修复前正例率 | 修复后正例率 |
|---|---|---|
| `COUNT(invoices.*, 0, 90, days) = 0` | **0.0000**（5304 个实例，零正例） | 0.0341 |
| `COUNT(invoices.*, 0, 90, days) > 0` | 0.9659 | 0.9659 |

两条本该互补，修复前加起来只有 0.966 —— 缺的 3.4% 全被静默标成了 `False`。
`> 0` 之所以没受影响，是因为"缺失 → False"对 `> 0` 恰好是正确的语义。

这解释了 §10.12 开头那个现象：**概率全挤在 0.9997 一档，不是 WWI 没信号，
是模型从来没被给过一个正例**，只能输出"没人流失"的先验。修复后在 WWI 上重跑，
`True_PROB` 从恒定的 3e-5 变成 0.0008 ~ 0.0168（87 个不同取值），有了分化 ——
但绝对值仍然极低，因为后期锚点的正例率确实是 0。

同样的问题会发生在任何 `= 0` / `<= 0` 形式的流失、逾期、断货问题里，是这类
PQL 的默认写法。回归测试：`test_hosted.py::test_a_customer_who_did_not_order_is_labelled_true_for_the_zero_case`。

#### 怎么验证"这个预测到底有没有用"：造一个有已知机制的库

WWI 回答不了这个问题——正例 3.4%，且与"流失"不同义。要验证链路能不能学到东西，
需要一批**我们知道自己注入了什么信号**的数据。`scripts/synthetic_churn_probe.py`
做这件事。

三个设计要点：

1. **留出一段模型看不到的未来**。模型只拿到 `CUTOFF = 2016-09-30` 之前的订单，
   评分时用完整历史算"锚点后 90 天是否真的没订单"。AUC 才有样本外含义。
2. **隐藏机制，可观测前兆**。每个客户按 `segment` + `creditlimit` + 一次带噪抽样得到
   风险分；高风险客户先进入衰退期（下单变少、金额变小、开始提工单），再停止下单。
   模型能看到的是前兆，看不到的是那次抽样。
3. **留噪声**。没有噪声的话 AUC 会是 1.0，那只能证明数据是自己造的。

跑法：

```bash
python scripts/synthetic_churn_probe.py              # 真打端点并评分
python scripts/synthetic_churn_probe.py --dry-run    # 只看 payload 大小，不花钱
python scripts/synthetic_churn_probe.py --customers 800
```

400 个客户、7,529 笔订单、580 张工单的规模下，实测结果：

```
context labels by anchor:   0.113 → 0.417（正例率随锚点上升，符合流失机制）
predict 正例率:              0.480
True_PROB 分位:              p10 0.060 / p50 0.215 / p90 0.989
AUC:                        0.961
precision@10%:              1.00（lift 2.08）
payload:                    2.7 MiB，3200 个实例，36,454 行相关数据
耗时:                        12.7 ~ 15.9s
```

**这个 0.96 要打个折看**：信号是我们自己注入的，模型学到是应该的。它证明的是
"物化层没有把信息丢掉、端点确实在做关系型推理"—— 也就是管道的正确性。
它**不**证明业务价值，那要用真实数据跑出来。

同一个脚本把 payload 从 WWI 的 398 MB 压到 2.7 MB，也顺带说明了下面的成本问题
主要来自表的数量，不是模型。

#### 398 MB 从哪来，怎么降到端点收得下

这里有三层，前两层是这次新加的，第三层是原来的兜底。

**① 只送问题真正读到的表（治本，已实现）**

`_related_for` 原来遍历**图里所有表**：只要这张表能连到实体，就整表展开进 payload，
和 PQL 有没有引用它无关。10 张表的图因此一次冲到 398 MB，真正被问到的只有 1~2 张。

现在只送 `spec.target_table`、到实体的路径桥接表、以及实体表本身，并在日志里说明
省掉了哪几张。三张表（customers / invoices / invoicelines）的实测：

| 配置 | payload | 相关行 | 送出的表 |
|---|---|---|---|
| 裁剪前（含 invoicelines） | 25.3 M | 485,000+ | 3 张 |
| 裁剪后 | 11.7 M | 288,413 | 2 张 |
| 裁剪 + 回溯 730 天 | 9.5 M | 244,792 | 2 张 |
| 裁剪 + 回溯 365 天 | 5.9 M | 152,387 | 2 张 |

`invoicelines` 的 22.8 万行 × 8 个锚点被整体排除 —— 它"能连上"不等于"要问它"。

**这一步有代价，而且能量出来**：合成库上把工单表裁掉之后，AUC 从 0.961 掉到 0.885，
precision@10% 从 1.00 掉到 0.975。工单是前兆之一，被裁掉了。所以"少给表"不是免费的：
如果某张表对业务有解释力，就该让它进 PQL（例如把工单条件写进 PQL 的 WHERE），
而不是靠"反正会一起送过去"。

**② 回溯窗口（`KUMO_LOOKBACK_DAYS`，默认 0 = 全历史）**

锚点后 90 天的预测，不太需要三年前的订单。设 365 时上表从 11.7 M 降到 5.9 M。
默认保持 0，因为有些库历史本身就短，砍了没有意义；长历史的库建议设 365~730。

**③ 均匀抽样（原有的兜底，不得已才触发）**

超过 `KUMO_MAX_PAYLOAD_BYTES`（28 MiB）时按等距抽样降下来，并打 WARNING。
它保证请求能发出去，但抽掉的行是随机的，会削弱近期信号 —— 能靠 ①② 解决就不要靠它。

不值得动的两个旋钮：降 `KUMO_HISTORY_ANCHORS` 会让训练样本变少，降
`KUMO_MAX_INSTANCES` 会让评估的客户变少。两者都是"少看一点"，比"少看几张表"更伤。

#### 已验证 / 未验证

已验（真实数据、真实端点、真实应用）：

- `sales.customers` + `sales.invoices` + `sales.invoicelines`（29.9 万行）建图、编译、
  物化、POST、还原 DataFrame 全链路通；
- 三种任务各一条 PQL 拿到合理输出（binary 概率、regression 数值、multiclass 类别+概率）；
- **应用内问答跑通**：问「未来 90 天哪些客户可能不再下单」，日志
  `kumo-hosted: binary_classification: COUNT(orders.*) over (0, 90] days = 0 for each
  customers.customerid answered in 63.99s (context=840, predict=105 instances, 111798 related rows)`，
  telemetry `outcome=answered / entities=105 / rows_returned=50 / 76.1s`，
  返回 `False_PROB/True_PROB` 两列；
- 离线单测 `test_pql_compile.py` 18 例、`test_hosted.py` 15 例、
  `test_framed_graph.py` 7 例全绿；`retrieval` 全量 689 例、`auto_ontology` 全量
  2510 例全绿。

未验 / 已知限制：

- **WWI 仍然不适合做流失验证，但原因和 10.11 ③ 不同**。当时归因于"这个库没有信号"，
  真正的原因下面一节再说：标签被抹平了。修好之后 `= 0` 的正例率是 3.4%，且随锚点
  单调递减（0.069 → 0.000）—— 那些"零订单"多半出现在客户关系的头几个月，
  不是流失。正例太少、且与"流失"不同义，模型学不到可用的排序。
- **一次问答 60~80 秒**，其中大头是上传 + 端点推理。裁剪表 + 回溯窗口之后，
  同样的三表场景 payload 从 25.3 M 降到 5.9 M；400 客户、4000 实体的合成库只要 10~16 秒。
  真正的瓶颈仍是引擎一次调用本身的耗时。
- **容器必须有外网出口**。本次验证的机器上容器直连不通，是在 `.env` 里临时加了
  `HTTPS_PROXY=http://host.docker.internal:<port>` 才打通的；直连环境不用管这一条。

#### 运维

```bash
# .env 里已配好 KUMO_HOSTED_API_URL=public 与 KUMO_HOSTED_API_KEY
docker compose up -d --build auto-ontology   # 改了 kumo/ 下的代码必须重建
```

重启后校验三件事：

```bash
docker exec auto-ontology env | grep -E "^(KUMO_HOSTED_API_URL|KUMO_HISTORY_ANCHORS)="
docker exec auto-ontology python -c "from auto_ontology.retrieval.text_to_sql.text_to_sql_graph import _prediction_enabled as e; print(e())"
docker logs auto-ontology 2>&1 | grep -i "prediction branch"
```

第三条应打印 `Text-to-SQL graph: prediction branch True`。之后问一句预测类问题（例如
"哪些客户未来 90 天可能不再下单"），日志里出现
`kumo-hosted: binary_classification: ... answered in X.XXs` 即链路走通。

**payload 规模**：实例数 = 实体数 × 锚点数，事件行数按实例重复。`KUMO_MAX_INSTANCES`（2000）、
`KUMO_MAX_RELATED_ROWS`（40 万）和 `KUMO_MAX_PAYLOAD_BYTES`（28 MiB）三道闸，**先到先限**。
后两道被触发都会打 WARNING，看到就说明这次的历史是被抽过的。

**缓存**：`PredictionContext` 里带着模型句柄，所以缓存键里加了后端标识
（`hosted@<url>` / `nim@<version>`）。切换后端不会拿到上一个后端的句柄。

---

## 11. 相关文档

| 主题             | 文档                                           |
| -------------- | -------------------------------------------- |
| 使用手册（安装、UI、运维） | [使用手册.md](./使用手册.md)                         |
| 连接与多库          | [connections.md](./connections.md)           |
| 对话 API（SSE 事件） | [conversation-api.md](./conversation-api.md) |
| MCP 工具         | [mcp.md](./mcp.md)                           |
