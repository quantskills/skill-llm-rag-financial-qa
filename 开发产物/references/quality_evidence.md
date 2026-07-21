# 质量证据 · skill-llm-rag-financial-qa（#42）

> 本文件记录：真实数据核验、测试覆盖、开发中发现并修复的缺陷、以及**需要如实转达的边界**。
> 所有数字均由本会话真实调用 PandaData（`python3119` 环境）得到，可复现。

## 一、真实数据核验（可复现）

环境：`/Users/zhanglinghao/anaconda3/envs/python3119/bin/python`，凭证 `~/.pandadata/pandadata.env`。

### 1. 数字路（`get_fina_reports`，宁德时代 300750.SZ）

| 问题 | 返回 | 引用 |
|---|---|---|
| 2024q4 归母净利润 | **507.4 亿**（50,744,682,000，绝对值） | `[get_fina_reports\|is_n_income_attr_p\|20260310\|300750.SZ]` |
| 2024q4 归母净利**增速** | **+15.01%**（is_growth=True） | `[…is_n_income_attr_p yoy…]（2024q4=5.074e+10 ÷ 2023q4=4.412e+10）` |
| 2024q4 营收**增速** | **−9.7%**（负增长带号正确） | `[…is_revenue yoy…]（2024q4=3.62e+11 ÷ 2023q4=4.009e+11）` |

507.4 亿归母净利与宁德时代 2024 年报一致；同比口径取"上年同季"、`if_adjusted==0` 优先。

### 2. 文本路（底仓 `database.parquet`，随包样例 11 票 154 条）

| 问题 | 命中证据（真实原文摘录） | 引用 |
|---|---|---|
| 紫金矿业为什么减持盾安环境 | "紫金矿业投资(上海)有限公司 减持：自身经营需要" | `[get_stock_shareholder_change\|reason\|20250121\|002011.SZ]` |
| ST 合泰为什么被实施退市风险警示 | "*ST：…2023 年度末合并口径经审计净资产为负值,触及…上市规则第 9.3…" | `[get_stock_status_change\|description\|…\|002217.SZ]` |
| 盾安环境回购的目的 | "…长期激励计划…限制性股票与股票期权激励计划…"（回购目的） | `[get_repurchase\|purpose\|20250730\|002011.SZ]` |

### 3. 拒答

- 硬拒答：`symbol` 不在底仓（如 888888.SZ/999999.SZ）→ `answer="insufficient_evidence"`，`degraded=True`。

## 二、测试覆盖（`test.py`，全离线，两环境全绿）

`python scripts/test.py` → **18/18 通过**（无 panda_data 也全绿；真实数据用例按配额自动跳过；含 artifact 复盘新增 4 项：年份归一化 / 单季拆解 / 重述稳定性 / ingest 三源修复）：

1. `test_tokenize_bm25` 中文 bigram + 英数词、BM25 命中
2. `test_retrieve_modes` direct（相关性排序）/ bm25 / hybrid（RRF）/ 元数据过滤
3. `test_route_classify` 数字 / 全文 / 文本路由
4. `test_numeric_path` 数字路精确取数 + PIT 引用，绝不进检索
5. `test_pit_original_preferred` **PIT 取原始公告(if_adjusted=0)而非追溯重述，引用落原始年报日**
6. `test_numeric_growth` **增速→真实 YoY=30%**、负基数带号；**缺上年同期显式拒算不冒充绝对值**
7. `test_text_path_and_citation` 文本路证据 + 引用四元组可回溯
8. `test_doctype_intent_boost` **意图 doc_type 前置**：问"回购"→repurchase 排首；无意图不改行为
9. `test_refusal` 无证据 → 拒答，不编造
10. `test_ingest_doc_and_chunk` doc 头部前缀 + 长文切分
11. `test_webfetch_offline` extract 去脚本 / 章节切分 / 白名单 / 未启用降级
12. `test_build_validate_and_run` validate 抛错 + run 直连离线问答
13. `test_golden_qa` 3 条金标：召回命中 + 引用齐全
14. `test_real_data_optional` 真实底仓（实测拉到 18 条）

## 三、开发中发现并修复的缺陷（测试驱动）

### ✅ 缺陷 1（真实、已修）："增速/同比"被当绝对值答

- **现象**：问"营收增速多少"，`answer_numeric` 返回当期**绝对营收**（如 2600 亿），而非同比%。`增速/同比/增长` 只用于路由，从未参与计算。
- **实证**：修复前 probe 返回 `value=260000000000.0`（绝对值），期望 30.0%。
- **修复**：`answer_numeric` 改为——PIT 去重后**保留全季**；命中 `GROWTH_WORDS(增速/同比/增长/yoy)` 时取"上年同季"算真实 YoY，负基数按符号；**无上年同期则 `value=None, growth_available=False` 显式拒算**，绝不拿绝对值冒充增速。引用带算式。
- **回归**：新增 `test_numeric_growth`；修复后 probe 返回 `value=30.0, is_growth=True, unit=%`。

### ✅ 复核 2（旧怀疑，不可复现）：`is_total_revenue` "毒字段"

- 旧版怀疑该字段致数字缓存全 None。本会话对 `get_fina_reports` 分别带/不带该字段实测，**均正常返回 17 行**——当前 API 上不可复现，故保留字段。

### ✅ 复核 3（旧怀疑，非缺陷）：`date="20261231"` 未来 as-of 日

- 实测未来 as-of 日不报错、不前视（数字路取各季当期原始公告）。属容错行为，非缺陷。

### 🔁 artifact 实测复盘轮次（688347 华虹宏力 / 688981 中芯国际 实测抓出，均已修+测）

- **缺陷 6【P0】"2025年/2025年报"匹配不上季度正则→静默取最新一期**：`answer_numeric` 只认 `2025q4` 正则，自然语言年份 fallback 到 `piv.iloc[-1]`。实测华虹宏力"2025年报增速"被答成 2026q1 **+513.1%**（实际 2025 年报 **−1.04%**，方向都反）。修：新增 `resolve_quarter`（年报/中报/一季报/三季报/"2025年"→`YYYYqN`）；未指明期间显式 `quarter_inferred`+note，指定不存在季度不静默换季。测 `test_year_quarter_resolution`。
- **缺陷 7【P1】数字路只给累计、无单季**：加 `quarterly_value`（本季累计−上季累计，Q1即单季）。实测华虹宏力困境反转：2024q4 单季 **−1.97亿** → 2025 逐季 +0.23/+0.52/+1.77/+1.25亿。测 `test_quarterly_decomposition`。
- **缺陷 8【P2】引用只锚单版本、稳定性未暴露**：加 `restated_count`/`value_stable`（该季被重述几次、值是否被修正）。测 `test_restated_stability`。
- **缺陷 9【P1】ingest 三源问题**：① `load_forecast` 不吃时间窗→按 info_date 过滤，与其余 6 源一致；② `load_audit` 加 `raw_count`+覆盖标记，区分"0条=接口无覆盖"vs"有数据但均未审计"；③ `load_restricted` 按解禁日**批次聚合**（华虹宏力 **3758→2 条**，滤掉 IPO 配售机构年金逐户记录）。测 `test_ingest_source_fixes`。
- **增强【P1/P2】枚举防截断 + 核验入口**：文本路加 `total_matched`/`returned`/`truncated`（top_k=8 静默截断可见）；`build.py` 加 `--inspect`（底仓覆盖核验）+ `brief`（各 doc_type 最新证据综合简报）。

### ✅ 缺陷 5（真实、已修）：PIT 去重取了追溯重述版，引用落在重述日而非原始年报

- **现象**（透明追踪抓出）：数字路 PIT 去重 `sort_values([...,"_adj","date"]).groupby.last()` 实际取了 `if_adjusted=1`（最新追溯重述）版——与文档声称的"if_adjusted==0 原始优先"相反。宁德 2024q4 归母净利被引到 **20260310**（2025 年报对 2024 的重述日），而非原始 **20250315** 年报日。值几乎相同（YoY 15.01% 不变），但引用指向错误文档、且对历史/回测是前视（用了后来的重述）。测试夹具全是 if_adjusted=0，从未覆盖到。
- **修复**：改为"原始公告(if_adjusted==0)优先、其内最新"（与 #48 datasource 口径一致）。修后引用落 **20250315**（原始年报）。
- **回归**：新增 `test_pit_original_preferred`（同季原始/重述值不同时必须取原始）。

### ✅ 缺陷 4（真实、已修）：混合 doc_type 下"公司名盖过主题"致错误类型排前

- **现象**（真机实测抓出）：问"盾安环境**回购**的目的"，direct 模式下**减持**doc 排在**回购**doc 前面——因"盾安环境"公司名 token 是高 IDF、且减持 doc 更短（BM25 长度归一偏好短文），把主题正确的回购 doc 压了下去。回购 doc 仍在候选里（召回不丢），但不在最前，LLM 读 top-K 可能读偏。
- **修复**：`qa.intent_doc_types` 从问题识别意图 doc_type（回购→repurchase、减持/增持→shareholder_change、解禁→restricted、预告→forecast、退市/ST→status_change、龙虎榜→lhb、审计→audit），`retrieve` 对命中类型**软加权前置**（direct 稳定前置、bm25/hybrid +0.5×最高分），**不删候选、保召回**。
- **回归**：新增 `test_doctype_intent_boost`；修复后真机"盾安回购"top 证据变为 repurchase。

## 四、需要如实转达的边界（不可回避的诚实）

1. **两层拒答，勿过度承诺**：
   - 硬拒答（可保证）：`symbol` 不在底仓 → `insufficient_evidence`。
   - 话题不覆盖（依赖调用方）：`symbol` 在底仓但问题超出已披露内容时，`direct` 模式会返回该票**全部真实证据**并交 agent 按 `answer_contract` 声明"超出语料边界"。**本层拒答由调用方 LLM 完成，不是引擎硬保证。** 曾评估用"词面无重叠"自动拒答，但"公司"等高频词会造成假重叠→给假信心，故不做，改为如实文档化。
2. **口径词未全覆盖**："2024 年报/中报"未映射到季度时，数字路回退到**最新季度**，引用会**如实标注实际季度**（不谎称是年报）。需要精确季度时请带 `2024q4` 式写法。
3. **底仓覆盖 = 已 ingest 的 `symbols` × 7 文本源披露密度**：未 backfill 的票需实时补或先 `backfill`。
4. **全文路默认关闭**：`enable_web=False`；具体检索入口需有网实测，未落地前不联网、返回 `[]` 并降级声明。
5. **`name` 字段部分为空**：`get_repurchase/get_audit_opinion/get_restricted_list` 等返回行不含公司名，doc 头部显示为 `[代码  日期 类型]`（代码始终在，引用不受影响）。

## 五、演示视频

`demo.mp4`（3–5 分钟跑通）需在有凭证的真机录屏，**由维护者录制**，AI 无法代生成。建议脚本：
① `python scripts/test.py` 全绿 → ② `run` 一个数字增速问题（看带算式引用）→
③ `run` 一个文本问题（看四元组引用）→ ④ 一个拒答 → ⑤ 展示 `database.parquet` 底仓统计。
