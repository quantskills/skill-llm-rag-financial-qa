---
name: skill-llm-rag-financial-qa
description: 当需要"就一家 A 股上市公司的财报/公告，给出带官方引用、可核对、拒绝编造的问答"时，使用此 skill。三路路由（数字精确算 / 底仓文本检索 / 官方全文按需）+ 引用纪律 + 拒答。数据源 PandaData 优先、官方披露网页为次级源。可被复盘 agent 或投研 agent 调用。
tags: [quant, build, development, rag, financial-qa, retrieval, citation]
---

# 财报公告 RAG 问答 BUILD（#42）

## 工具定位

- 工具类型：分析报告型 BUILD（公告/财报问答）
- 解决问题：把"就某公司披露内容的自然语言提问"变成**可信、可核对**的回答——
  1. 数字类问题（净利/营收/增速）**精确算**，不进检索层、不让 LLM 心算（防幻觉红线）；
  2. 原因/条款/目的类问题走**底仓文本检索**，每条结论强制附**官方出处四元组**；
  3. 点名要"原文/条款"或底仓证据不足时，按需抓**官方披露平台**全文（可选、可降级）；
  4. 语料不覆盖 → **拒答**，绝不编造。
- 使用对象：盘后复盘 agent / 投研问答 agent / 人工尽调 / 组合 Alpha（当特征来源）
- **明确不做**：不做估值、不给买卖建议、不预测股价。仅研究/教育示例，**不构成投资建议**。

## 核心框架：三路路由（"数字绝不进检索层"是防幻觉红线）

> RAG（通俗）：先"检索"到相关的官方原文，再"生成"回答——**答案必须落在检索到的证据上，且注明出处**。
> 本 skill 的关键判断：**不是所有问题都该走检索**。数字必须精确计算，检索只负责"文字性"问题。

| 路 | 触发 | 数据来源 | 怎么答 |
|---|---|---|---|
| **① 数字路** | 净利/营收/毛利/EPS/**增速/同比**/多少 | `get_fina_reports`（PIT 按公告日） | pandas 精确取数/算同比，绝不进检索、绝不让模型心算；**跨公司/跨期逐个算，绝不静默只答一个** |
| **② 底仓文本路** | 原因/目的/条款/为什么 | `database.parquet` 底仓 doc（7 源） | 纯 Python BM25 + 元数据过滤；小料直读全量交 agent |
| **③ 全文路** | 点名"原文/全文/条款" 或 底仓不足 | 官方披露平台（巨潮/交易所/HKEX/EDGAR） | 按需抓取→章节切分（可选，`enable_web`；失败降级声明） |

任一路都产出**证据包 + 引用**；证据不足统一**拒答**（`answer="insufficient_evidence"`）。

## 引用纪律（可核对是底线）

- PandaData 结构化字段：`[source_api|field|info_date|symbol]`，如 `[get_repurchase|purpose|20250730|002011.SZ]`
- 数字路同比：引用带**算式**，如 `[get_fina_reports|is_n_income_attr_p yoy|20260310|300750.SZ]（2024q4=5.074e+10 ÷ 2023q4=4.412e+10）`
- 官方网页全文：`[url|title|date|fetched]`
- 默认**不生成成品答案**：返回 `evidence/numeric/citations` + `answer_contract`（"逐条基于证据作答、每个结论附 cite、未覆盖必须明说"），交调用方 agent（即 LLM）落笔；配了 `config.llm` 才内部生成。

## 输入

数据来自 PandaData（凭证走环境变量/`~/.pandadata/pandadata.env`，**绝不硬编码**），或调用方直接传入 docs+cache。

`run(input_data, config=None)` 两种输入：

| 形态 | 例 | 说明 |
|---|---|---|
| 问题串 | `"盾安环境回购的目的是什么"` | 从 `database.parquet` 底仓取语料；数字问题实时补数字缓存 |
| 结构化 | `{"question":..., "symbols":[...], "date_range":?, "doc_types":?, "docs":?, "cache":?}` | 传 `docs(+cache)` → 纯离线问答（可测、不联网） |

## 输出（qa 结果 dict）

| 字段 | 说明 |
|---|---|
| question / route | 原问题 / 命中的路（`["numeric"]`/`["text"]`/`["fulltext"]` 组合） |
| numeric | 数字路结果：`{metric, metric_field, quarter, value, is_growth, unit?, abs_value?, base_quarter?, base_value?, cite}`；**跨公司/跨期时**追加 `multi=True` + `items[]`（逐票逐期各一条，各带 cite）+ `symbols/quarters`，缺票时另有 `missing_symbols` |
| evidence | 文本/全文证据：`[{text, symbol, info_date, source_api, doc_type, score, cite}]` |
| citations | 全部引用（四元组）列表 |
| mode | 检索模式：`direct`（小料直读）/ `bm25` / `hybrid`（BM25+向量 RRF）/ `empty` |
| answer | 默认 `None`（交 agent）；无证据时 `"insufficient_evidence"`；配 llm 才是成品答案 |
| answer_contract | 未配 llm 时的作答契约（强制引用+不编造） |
| degraded | 网页通道失败 / 拒答时置 True |

## 调用方式

```python
from scripts.build import run, backfill, maintain_daily, save_corpus, load_corpus

# 生产：从 database.parquet 底仓问答（数字问题自动实时补数字缓存）
res = run({"question": "盾安环境回购的目的是什么", "symbols": ["002011.SZ"]})

# 离线/直连：调用方已有语料，纯离线问答（可单测、不联网）
res = run({"question": "宁德时代2024q4归母净利润增速", "symbols": ["300750.SZ"],
           "docs": my_docs, "cache": my_numeric_df})

# 生产维护
backfill(["002011.SZ", "300750.SZ"], "20240101", "20260711")   # 建底仓语料 → parquet
maintain_daily("20260711")                                       # 全市场当日增量（增减持/龙虎榜）
```

命令行：
```bash
export PANDA_USERNAME=<手机号>; export PANDA_PASSWORD=<密码>   # 或 ~/.pandadata/pandadata.env
python scripts/build.py --question "盾安环境回购的目的是什么" --symbols 002011.SZ
python scripts/build.py --symbols 002011.SZ 300750.SZ --backfill 20240101 20260711   # 建底仓
python scripts/test.py                                           # 全离线自测（无 SDK 也全绿）
```

## Agent 执行规则

1. **数字问题**（净利/营收/增速…）看 `result["numeric"]`：`is_growth=True` 是同比%（带算式引用），否则是绝对值。**直接引用 `value`，不要自己心算。**
2. **文字问题**：逐条基于 `evidence` 作答，每条结论后附其 `cite`；语料未覆盖必须明说"超出已披露内容/不确定"，**禁止编造**。
3. **拒答**：`answer=="insufficient_evidence"` → 明确告知"底仓无该标的/该问题的官方依据"，不要强行回答。
4. **两层拒答边界**（务必如实转达，见 references/quality_evidence.md）：
   - 硬拒答：`symbol` 不在底仓 / 无任何候选 → `insufficient_evidence`。
   - 话题不覆盖：`symbol` 在底仓但问题超出已披露内容 → 返回该票**真实证据**（direct 模式给全量），由你按 `answer_contract` 判定"超出语料边界"并声明，不编造。
5. 引用阶段/结论时带边界："仅基于公开披露、不构成投资建议"。
6. 先 `python scripts/test.py` 全绿；真实数据因配额/无 SDK 自动跳过（不判失败）。

## 术语表（学术 → 人话，交付语言规范）

| 学术术语 | 人话解读 |
|---|---|
| RAG（检索增强生成） | 先查到相关官方原文，再照着原文回答并注明出处，不凭记忆瞎编 |
| 路由（route） | 先判断这问题该"精确算数字"还是"检索文字"还是"抓原文" |
| BM25 | 一种经典的关键词相关性打分法，衡量"这段文字和你的问题多贴" |
| 向量检索 / HashEmbedder | 按语义相近找材料；HashEmbedder 是确定性的离线版（供自测，无需联网） |
| RRF（倒数排名融合） | 把"关键词排名"和"语义排名"两张榜合成一张，取两者都靠前的 |
| PIT（时点正确） | 只用当时已公告的信息，同一季度取当期原始公告，不偷看后来的追溯调整 |
| if_adjusted | 财报是否为追溯调整版；数字路取 `==0`（当期原始）优先，保证 PIT |
| 引用四元组 | `[接口\|字段\|公告日\|代码]`，让每个结论都能一键回到官方出处核对 |
| 同比（YoY） | 和"去年同一个季度"比，涨/跌了百分之几；负基数按符号处理 |
| 拒答 | 语料里没有依据时，明说"不知道/超出边界"，而不是编一个像样的答案 |

## 可被 Alpha 调用

- 是。`run()` 返回结构化证据包；`numeric.value`（绝对值/同比）、`evidence` 计数、`doc_type` 分布等可作因子特征或事件信号输入。
- 调用限制：数字路需能拉到该票 `get_fina_reports`；文本路需底仓已 ingest 该票或允许实时补。
- 依赖数据：见 `references/api_guide.md`。

## 是否需要生产结果

- 生成 `database.parquet`：是（结果型底仓语料，盘后统一 ingest，多人复用检索）。
- 更新频率：`backfill` 建历史底仓 + `maintain_daily` 每日增量（增减持/龙虎榜全市场按日期段）。
- 字段结构：见 `../生产产物/SKILL.md`。

## 依赖

- panda_data ≥ 0.0.9（`get_fina_reports`/`get_repurchase`/`get_stock_shareholder_change`/`get_stock_status_change`/`get_fina_forecast`/`get_restricted_list`/`get_lhb_list`/`get_audit_opinion`）
- pandas、pyarrow（生产 parquet；缺 pyarrow 自动降级 CSV）
- 可选：`beautifulsoup4`/`pdfminer.six`（全文路 extract）、`requests`（全文抓取/内置 LLM 生成）——**默认不启用，缺失自动降级**
- 凭证：`PANDA_USERNAME`/`PANDA_PASSWORD` 或 `~/.pandadata/pandadata.env`（**绝不硬编码**）
- 核心检索 `retrieve.py` 纯逻辑零 IO、零联网；`test.py` 全离线可跑（无 panda_data 也全绿）

## 数据边界 / 免责

数据源 PandaData（结构化字段与财报数字），官方披露平台（巨潮/交易所/HKEX/EDGAR）为全文次级源；**不抓研报/Wind/Choice 等付费源**（版权 + 社区规则 §3）。假设：可信问答 = 官方结构化字段 + 官方全文 + 强制引用 + 拒答纪律。已知限制：底仓文本覆盖取决于已 ingest 的 `symbols` 与 7 个文本源的披露密度；"年报/中报"等口径词未映射到季度时数字路回退到最新季度（引用如实标注实际季度）；全文路具体检索入口需有网实测，社区版默认 `enable_web=False`。风险边界：**仅量化研究与教育示例，不构成投资建议，不承诺收益**；问答仅复述/计算公开披露，核对以官方原文为准。
