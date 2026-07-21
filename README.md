# skill-llm-rag-financial-qa（#42）

> 财报公告 RAG 问答系统 · BUILD 型 skill · Community Project
> **就一家 A 股公司的财报/公告提问，给出带官方引用、可核对、拒绝编造的回答。** 数字精确算、文字带出处、语料不覆盖就拒答。

## 它回答什么

- 数字类：「宁德时代 2024q4 归母净利润**增速**？」→ 查财报 PIT 精确算**同比 +15.01%**（带算式引用），不让模型心算。
- 文字类：「紫金矿业**为什么**减持盾安环境？」→ 检索官方公告原文："自身经营需要"，附出处四元组。
- 不覆盖：「这家公司董事长是谁？」（底仓无）→ **拒答**，不编造。

**明确不做**：不做估值、不给买卖建议、不预测股价。**仅复述/计算公开披露，非投资建议。**

## 三路路由（"数字绝不进检索层"是防幻觉红线）

| 路 | 触发 | 数据来源 | 怎么答 |
|---|---|---|---|
| ① 数字路 | 净利/营收/毛利/EPS/**增速/同比** | `get_fina_reports`（PIT 按公告日） | pandas 精确取数/算同比，负基数带号 |
| ② 底仓文本路 | 原因/目的/条款/为什么 | `database.parquet` 底仓 7 源 | 纯 Python BM25 + 元数据过滤；小料直读 |
| ③ 全文路 | 点名"原文/条款" 或底仓不足 | 官方披露平台（巨潮/交易所/HKEX/EDGAR） | 按需抓取→章节切分（可选、可降级） |

每条结论强制附**官方出处四元组** `[接口\|字段\|公告日\|代码]`；证据不足统一拒答。

## 快速开始

```bash
pip install --upgrade panda_data pyarrow
export PANDA_USERNAME=<手机号>; export PANDA_PASSWORD=<密码>   # 或 ~/.pandadata/pandadata.env

# 数字增速问答
python 开发产物/scripts/build.py --question "宁德时代2024q4归母净利润增速" --symbols 300750.SZ
# 文本原因问答
python 开发产物/scripts/build.py --question "紫金矿业为什么减持盾安环境" --symbols 002011.SZ
# 建底仓语料 → database.parquet
python 开发产物/scripts/build.py --symbols 002011.SZ 300750.SZ --backfill 20240101 20260711
# 全离线自测（无 panda_data 也全绿）
python 开发产物/scripts/test.py
```

## 目录

```
开发产物/
  scripts/
    qa.py          三路路由 + 引用纪律 + 拒答 + 数字路真实同比
    retrieve.py    检索层（纯 Python BM25 / HashEmbedder / RRF，零 IO 零联网）
    ingest.py      PandaData 7 文本源 → 底仓 doc 语料 + 数字缓存（PIT）
    webfetch.py    官方披露平台全文按需（白名单/限速/降级；默认关闭）
    build.py       run/validate_input/backfill/maintain_daily/save_corpus
    render.py      单季拆解 + SVG 柱状图（正绿负红）
    test.py        全离线夹具（19 用例）
  references/
    api_guide.md        接口 + 字段口径 + 真机实测结论
    quality_evidence.md 真票核验 + 测试覆盖 + 缺陷修复 + 诚实边界
    golden_qa.json      金标问答集（回归用，全部可复现）
  SKILL.md / skill.json
生产产物/
  database.parquet             底仓 doc 语料（154 条 / 11 票，agent 直接读检索）
  sample_quarterly_688347.html 单季拆解图样例（华虹公司）
  SKILL.md                     生产底仓读取规则
```

## 数据与免责

数据源 PandaData（凭证走环境变量或 `~/.pandadata/pandadata.env`，**绝不硬编码**）+ 官方披露平台（全文次级源）。
**只抓官方披露平台、不抓研报/Wind/Choice 等付费源**（版权 + 社区规则 §3）；全文路默认关闭，需有网实测入口。

**Community Project，未经 QuantSkills 官方审核/认证/背书。仅量化研究与教育示例，不构成投资建议，不承诺收益。** 问答仅复述/计算公开披露，核对以官方原文为准。拒答分两层：硬拒答（symbol 不在底仓，引擎保证）/ 话题不覆盖（由调用方 agent 按契约声明），见 quality_evidence.md。

License: GPL-3.0-only
