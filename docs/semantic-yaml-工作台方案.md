# 语义 YAML 工作台方案（浏览 Ossie / 编辑语义 / 重建向量）

> 状态：方案，未改代码。调研基于当前容器内运行版本（2026-11-12）。
> 结论先给：**"改 Ossie YAML → 导入 → 向量生效"这条路在语义层面不成立**，不是 UI 工程量问题，是转换层单向性 + 导入只新建不更新决定的。方案据此改为「Ossie 只读浏览 + 语义对象编辑 + 精确重建向量」。

---

## 一、三条实测发现（决定了方案形态）

### 1. 改 Ossie 的 `field.description`，导入后被静默丢弃

`POST /model/export?format=ossie` 导出的文件根上带

```yaml
custom_extensions:
- vendor: NVIDIA_AUTO_ONTOLOGY
  data: {"native_document": {...整份原生文档...}}
```

反向转换时，列描述的**唯一**来源是这份冻结副本：

```python
# ossie_nvidia_auto_ontology/native_converter.py:986
"description": str(preserved_column.get("description") or ""),
```

实测（retail_churn，88 个表/列对象）：

| 编辑对象 | 是否写回 catalog |
|---|---|
| `datasets[].fields[].description` | ❌ 否（保留 `custom_extensions` 时） |
| `datasets[].fields[].description` | ❌ 否（**删掉** `custom_extensions` 时也否，反向根本不读这个字段） |
| `datasets[].description` | ❌ 否（`preserved_table` 存在时优先用它） |

即：Ossie 文档对描述是**只写不读**的渲染结果。

### 2. Ossie 里的描述不是 catalog 原文，是转换器拼装的英文串

| 列 | catalog 实际描述 | Ossie 里的 description |
|---|---|---|
| `signup_date` | `''`（空） | `signup_date (date) records the calendar date when each customer account was created. — format: YYYY-MM-DD` |
| `contract_type` | 中文业务口径（monthly/annual/spot 各自节奏，含"不应据此判断流失"的判断规则） | `contract_type (character varying) defines the agreement frequency, with sample values monthly, annual, and spot.` |

所以即使将来打通回写，也是**用英文样板覆盖中文口径**——语义只会变差。这点必须在做 UI 之前就想清楚。

### 3. 导入口径是"只新建、不更新"，向量也只嵌新建的

```python
# auto_ontology/dal/model_interchange.py:816-828
live_id = existing.get(imported_id)
if live_id is None:
    to_create.append(...)      # 没有 → 建
    continue
result[imported_id] = (live_id, False)   # 有 → 只打 imported_id 戳，属性一个不改
```

```python
# auto_ontology/dal/model_interchange.py:1142 / 1162
if col_created: embed_buffer.data_rows.append(...)
if tbl_created: embed_buffer.data_rows.append(...)
```

叠加 `PostgresVDB.write_to_index` 用的是 `store.add_embeddings`（插入，非 upsert，`vdb/postgres.py:387`），**重复重建会产生同一节点的多行向量**。

> 结论：YAML 通道是"导入"，不是"编辑"。要支持"改了 YAML 就生效"，必须新增 upsert 语义或走另一条编辑路径。

---

## 二、现状能力盘点（真正的缺口只有一个）

| 编辑入口 | 写 catalog | 重建向量 |
|---|---|---|
| 数据页改**表/列描述** | ✅ | ✅ 自动（先嵌成功再删旧行，`server/datasources/service.py:165`） |
| 改**列样例值** | ✅ | ✅ 数据层 + 相关 ColumnAttribute |
| 改 **Term / ColumnAttribute / SqlAttribute / CustomAnalysis 描述** | ✅ | ❌ `_get_node_ids_for_embedding_update` 对非 Table/Column 直接 `return []`（`service.py:144`） |
| YAML 导入（新对象） | ✅ | ✅ |
| YAML 导入（改已有对象） | ❌ 属性不更新 | ❌ |
| 编辑 Ossie 的 field.description | ❌ | ❌ |

**唯一真正的缺口：语义层对象的描述改了，向量不重建。** 而这恰好是"提升语义理解"最该修的地方——`semantic_layer` 的 675 行才是跨语言命中的主力（中文问法 vs 英文列名、样例值反查列、逾期/沉睡类同义表述）。

顺带一个已有隐患：`/api/semantic-compilation/reset` 会**重编译语义层并覆盖人工编辑**。它和"重建索引"是两件完全不同的事，UI 上必须分开。

---

## 三、方案形态

新增页面 `/settings/semantic-yaml`（Settings 第 11 项）。三个 tab，职责不重叠。

### Tab A —— 浏览 Ossie（只读，零风险，先做）

- 库选择：复用 `datasources.getDBs()`
- 文档切换：`semantic_model.yaml` / `ontology.yaml` / `mapping.yaml`（三件套走 `include_ontology=true` 的 zip，前端用 `fflate` 解压后分文件展示）
- 代码视图：行号 + YAML 语法高亮 + 折叠 + 关键字搜索 + 复制 + 下载
- 结构树侧栏：datasets / fields / relationships / metrics 计数，点击跳行
- **一致性体检**（这块比看文本有用得多）：
  - 关系引用的字段是否存在（实测 retail_churn / WWI 均 0 悬空，可作为回归断言）
  - metrics 是否重名（重名会导致导出 422）
  - 哪些 term 因"代表多张表"被压平（`_project_terms_onto_one_table` 的 warning 回显）
  - 本库有哪些对象 **Ossie 表达不了**（SqlAttribute、semantic_fk、zones）

### Tab B —— 编辑语义对象（真源在 catalog，不在 YAML）

**B1 表单化编辑（推荐主线）**

按对象列出可编辑字段，保存走已有的 catalog patch API（**它自带向量重建**，只是目前只覆盖 Table/Column）：

| 可编辑 | 影响哪些向量 |
|---|---|
| Term 描述 | Term 行 + 该 Term 下所有 ColumnAttribute 行（attr 文本含 `term_name`） |
| ColumnAttribute 描述 | 该 attr 行 |
| CustomAnalysis 名称/描述/SQL | 该行（也是 few-shot 候选） |
| 列描述 / 样例值 | Column 行 + 父 Table 行（已有能力） |

配套：每行一个「重建该条向量」按钮 + 全局「重建全部」。

**B2 原生 YAML 批量编辑（P3，可选）**

用 `format=auto_ontology` 的原生 YAML 做批量兜底。前提是补两件事：

1. `import` 新增 upsert 语义（按 id 更新 description/name/type），否则只能新建；
2. 导入必须 `replace=false`（`replace=true` 会删掉 payload 里没有的语义数据）+ `embed=false`（避免只嵌新建的、留下重复行），再显式触发重建。

原生 YAML 的 id 是 catalog 真 uuid（实测 169 个 id 往返 0 丢失 / 0 新增），所以 upsert 能原位更新，不会打断 zones / tags / PQL 引用。

### Tab C —— 重建索引

- **预览**：`dry_run` 返回将重建的对象数与按 label 拆分（data_rows / semantic_rows）
- **执行**：逐条 `delete_by_id` → `embed` → `write`。顺序必须是**先嵌入成功、再删旧行**（沿用 `_refresh_vdb_embeddings` 的做法：嵌失败时旧向量还在）
- **回执**：总数 / 成功 / 失败 / 耗时 / 是否跳过
- **绝不静默**：`EMBED_API_KEY` 缺失时现在返回 `{"skipped": true}`，UI 要显示成红色阻断态，而不是"完成"
- **行数校验**：重建前后 `vdb.semantic_layer` / `data_objects_layer` 行数必须不变，这是 upsert 正确性的硬指标

---

## 四、后端改动清单

| # | 改动 | 位置 | 说明 |
|---|---|---|---|
| 1 | 新增 `POST /api/semantic-index/reindex` | 新 router，`server/__main__.py` 注册 | body `{databases[], labels[]?, node_ids[]?, dry_run}` → 预览或执行结果 |
| 2 | 语义层"按库枚举 → embed rows" | `semantic/embed.py`（`_build_rows` 已有，缺批量入口） | Term / ColumnAttribute / SqlAttribute / CustomAnalysis / PqlAnalysis 五类 |
| 3 | 补 `_get_node_ids_for_embedding_update` 的语义层分支 | `server/datasources/service.py:144` | Term 改动要连带其 ColumnAttribute；attr 改动要连带所属 Term 的聚合文本（若有） |
| 4 | `import` 支持 upsert | `dal/model_interchange.py:_resolve_entities_batch` | 现有匹配到的行只打 `imported_id` 戳，需补属性更新；仅 P3 需要 |
| 5 | 导入返回 `updated` 变更集 | 同上 | 现在只有 `created` / `skipped`，前端无法知道"该重建谁" |
| 6 | 导出可选剥离 `native_document` | `server/model_interchange/service.py` | `?include_native_document=false` 产出"干净的对外 Ossie"，同时在 UI 上把"编辑 Ossie 无效"的真相说清楚 |

前端：新增页面 + `api/semanticYaml.ts`；YAML 高亮建议 **CodeMirror 6**（`@uiw/react-codemirror` + `@codemirror/lang-yaml`，约 60 KB gzip），P0 可先用等宽 `<textarea>` + 行号，零依赖。

---

## 五、分阶段与工作量

| 阶段 | 内容 | 估算 | 风险 |
|---|---|---|---|
| **P0** | Tab A 只读浏览 + 一致性体检 | 1~2 天 | 无（只读） |
| **P1** | Tab C 重建索引 + 后端 #1 #2 | 2~3 天 | 中（写向量库，需行数校验） |
| **P2** | Tab B1 表单化编辑 + 后端 #3 | 3~4 天 | 中（改 catalog，需事务与回滚） |
| **P3** | Tab B2 原生 YAML 批量编辑 + #4 #5 | 4~5 天 | 高（upsert 语义、diff、误删） |
| **可选** | #6 干净 Ossie 导出 | 0.5 天 | 低 |

**建议顺序 P0 → P1 → P2**。P1 即使不做任何编辑也有独立价值：可以把被 `retail_churn_asof_20240630` 那次污染过的向量、以及历次导入遗留的重复行清干净。

---

## 六、验收标准（必须能在 UI 上看见效果）

1. **召回变化**：给 `contract_type` 的 ColumnAttribute 补一句"spot 客户流失率高"，问"哪些客户要流失" → 改前该列不在 top-k，改后进入 top-k。
2. **few-shot 变化**：新增/改写一条 PqlAnalysis 描述后，`PQL_MAX_EXAMPLES=5` 选中的例子是否换成了更相关的那条。
3. **不膨胀**：重建前后两层向量行数完全一致。
4. **可回滚**：故意让嵌入服务失败，旧向量仍然可检索（不是删了写不进去）。
5. **不误删**：编辑路径全程 `replace=false`，zones / tags / PQL 语料零丢失。

---

## 七、风险与取舍

| 风险 | 应对 |
|---|---|
| 同一节点多行向量 | 重建前 `delete_by_id`；用"行数不变"断言 |
| `replace=true` 误删语义 | 编辑路径硬编码 `replace=false`，UI 不暴露该开关 |
| 嵌入服务不可用被当成功 | `skipped` 一律渲染成失败态 |
| 语义重编译覆盖人工编辑 | 与"重建索引"分开放，reset 加二次确认 + 明确文案 |
| Ossie 压平多表 term | 浏览时标黄提示"代表 N 张表，Ossie 只能表达 1 张" |
| 把英文样板写回中文口径 | 编辑面只在 catalog/原生 YAML 上做，Ossie 只读 |

---

## 八、需要你拍板的三件事

1. **编辑面用哪个**：表单化（B1）还是原生 YAML（B2）？前者安全、可审计，后者适合批量迁移。我的意见是先 B1，B2 作为 P3。
2. **要不要让 Ossie 描述可回写**：需要改 `native_converter.py`（让 `field.description` 优先于 `preserved`）+ 解决"英文拼装串污染中文口径"。技术上可行，但**不建议**——收益是格式洁癖，代价是语义退化。
3. **重建粒度**：只做"整库重建"，还是支持"单节点重建"？后者体验好但要按 label 精确构造 embed 文本，工作量约多 40%。
