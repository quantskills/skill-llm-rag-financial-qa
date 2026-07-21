---
name: skill-llm-rag-financial-qa
description: 财报公告 RAG 问答系统——就一家 A 股公司的财报/公告提问，给出带官方引用、可核对、拒绝编造的回答。三路路由（数字精确算 / 底仓文本检索 / 官方全文按需）+ 引用纪律 + 拒答。数据源 PandaData 优先、官方披露网页为次级源。BUILD 型 skill，可被复盘 agent 或投研 agent 调用。
tags: [quant, build, rag, financial-qa, retrieval, citation]
license: GPL-3.0-only
metadata:
  organization: QuantSkills
  organization_url: https://github.com/quantskills
  repository: skill-llm-rag-financial-qa
  repository_url: https://github.com/quantskills/skill-llm-rag-financial-qa
  project_type: skill
  collection: llm-tooling
  license: GPL-3.0-only
  status: community-project
---

# 财报公告 RAG 问答系统（#42）

> **项目状态：Community Project（社区项目）。** 本项目由社区成员创建，**未经 QuantSkills 官方审核、认证、验证或背书**，
> 也非生产可用认证项目。名称中的 `quantskills/` 仅表示托管组织，不代表任何官方身份。

> **一句话**：把"就某公司披露内容的自然语言提问"变成**可信、可核对**的回答——数字精确算、文字带官方引用、语料不覆盖就**拒答**，绝不编造。仅复述与计算公开披露。

## 这个工具做什么

RAG（检索增强生成，通俗）= 先查到相关官方原文，再照着原文回答并注明出处。本 skill 的关键判断是**不是所有问题都该走检索**——用三路路由分而治之：

- **① 数字路**（净利/营收/增速/同比）：查 `get_fina_reports`（PIT 按公告日），pandas **精确计算**，绝不进检索层、绝不让模型心算（防幻觉红线）。
- **② 底仓文本路**（原因/目的/条款/为什么）：对 `database.parquet` 底仓 7 源文本做纯 Python BM25 + 元数据过滤；小料直读全量交 agent。
- **③ 全文路**（点名要"原文/条款" 或底仓不足）：按需抓**官方披露平台**（巨潮/交易所/HKEX/EDGAR）全文（可选、可降级，不抓付费研报）。

每条结论强制附**官方出处四元组** `[接口|字段|公告日|代码]`；同比引用带算式；语料不覆盖 → `insufficient_evidence` 拒答。

## 快速使用

```bash
pip install --upgrade panda_data pyarrow
export PANDA_USERNAME=<手机号>; export PANDA_PASSWORD=<密码>   # 或 ~/.pandadata/pandadata.env

# 问答（数字增速 / 文本原因）
python 开发产物/scripts/build.py --question "宁德时代2024q4归母净利润增速" --symbols 300750.SZ
python 开发产物/scripts/build.py --question "紫金矿业为什么减持盾安环境" --symbols 002011.SZ
# 建底仓语料
python 开发产物/scripts/build.py --symbols 002011.SZ 300750.SZ --backfill 20240101 20260711
# 全离线自测（无 panda_data 也全绿）
python 开发产物/scripts/test.py
```

- 详细文档：[开发产物/SKILL.md](开发产物/SKILL.md)
- 数据接口与真机实测：[开发产物/references/api_guide.md](开发产物/references/api_guide.md)
- 质量证据（真票核验 + 缺陷修复 + 诚实边界）：[开发产物/references/quality_evidence.md](开发产物/references/quality_evidence.md)
- 金标问答集：[开发产物/references/golden_qa.json](开发产物/references/golden_qa.json)
- 生产底仓读取：[生产产物/SKILL.md](生产产物/SKILL.md)

## 边界与免责

数据源 PandaData（结构化字段与财报数字）+ 官方披露平台（全文次级源，不抓付费研报）。**Community Project，未经 QuantSkills 官方审核/认证/背书。仅量化研究与教育示例，不构成投资建议，不承诺收益。** 问答仅复述/计算公开披露，核对以官方原文为准；拒答分"硬拒答（symbol 不在底仓，引擎保证）"与"话题不覆盖（由调用方 agent 按契约声明）"两层，见 quality_evidence.md。数据源/假设/参数/限制/风险边界见开发产物 SKILL.md 与 references/。
