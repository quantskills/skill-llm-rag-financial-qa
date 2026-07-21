---
name: skill-llm-rag-financial-qa-production
description: 当需要读取"财报公告 RAG 问答"（#42）的生产底仓语料时，使用此 skill。读取已 ingest 的 database.parquet 文本语料做检索问答，不重复拉取全部披露源。
tags: [quant, build, production, rag, financial-qa]
---

# 财报公告 RAG 问答生产底仓（#42）

## 工具定位

- 工具类型：分析报告型 BUILD 的生产底仓（**文本检索语料**，非结果面板）
- 服务对象：复盘 agent / 投研问答 agent / 人工尽调 / 组合 Alpha
- 是否可被 Alpha 调用：是（证据计数、`doc_type` 分布、数字/同比可作特征）

## 结果文件

- 路径：`database.parquet`
- 格式：Parquet（无 pyarrow 时开发脚本降级 CSV）
- 角色：**底仓 doc 语料**——ingest 后供 `retrieve/qa` 检索；数字问题仍实时查 `get_fina_reports`（PIT），不落在本文件。
- 更新频率：`backfill` 建历史底仓 + `maintain_daily` 每日增量（全市场增减持/龙虎榜按日期段）
- 生成任务：`scripts/build.py`（`backfill` / `maintain_daily` → `save_corpus`）

## 当前内容（随包样例，真实 ingest）

- **溯源**：由 `scripts/build.py --backfill` 从 **真实 PandaData** 拉取生成（非合成/非测试桩），
  `update_time = 2026-07-11T17:18:06`，`data_version = rag-financial-qa-v1`。可用同一命令重建。
- 规模：**154 条 doc / 11 票**（002011.SZ 盾安环境、002217.SZ *ST合泰、300750.SZ 宁德时代、000001.SZ 平安银行、
  002415.SZ 海康威视、002594.SZ 比亚迪、600519.SH 贵州茅台、300502.SZ 新易盛、002456.SZ 欧菲光、
  688347.SH 华虹公司、688981.SH 中芯国际）
- doc_type 分布：`lhb 62 / audit 41 / shareholder_change 17 / repurchase 14 / restricted 8 / forecast 7 / status_change 5`
- 公告日范围：`20240105 ~ 20260708`
- 扩容：`backfill(更多 symbols, start, end)` 追加；`save_corpus` 按 `doc_id` 去重（`keep=last`）。

> 随包样例仅为**可跑通的演示底仓**，覆盖票数有限；生产使用请按自己的股票池重跑 `backfill`。

## 示例样本

- `sample_quarterly_688347.html` — 单季拆解 + SVG 柱状图样例（688347.SH 华虹公司，正绿负红），由 `scripts/render.py` 生成。

## 主键 / schema

doc 唯一键 `doc_id`（= `source_api:symbol:info_date:seq`）。列：

| 字段 | 类型 | 说明 |
|---|---|---|
| doc_id | string | 唯一键（去重用） |
| symbol / name | string | 代码 / 名称（部分源无名，代码必有） |
| info_date | string(YYYYMMDD) | 官方公告日（PIT / 过滤用） |
| source_api / doc_type / field | string | 来源接口 / 类型 / 抽取字段 |
| text | string | 带头部前缀 `[代码 名称 公告日 类型]` 的自足正文 chunk |
| url | string/null | 官方全文 URL（结构化源为空） |
| numeric_ctx | string(JSON) | 该 doc 关联的少量结构化上下文 |
| build_id / data_version / update_time | string | "42" / "rag-financial-qa-v1" / 生成时间 |

## 读取规则

```python
import pandas as pd
from scripts.build import load_corpus, run

docs = load_corpus()                       # 读底仓（随包样例 154 条）
res = run({"question": "盾安环境回购的目的是什么", "symbols": ["002011.SZ"], "docs": docs})
for e in res["evidence"]:
    print(e["cite"], e["text"][:60])       # 每条带官方出处四元组

# 数字问题（同比/绝对值）——不在本文件，run 会实时查 get_fina_reports 并 PIT 计算
res_num = run({"question": "宁德时代2024q4归母净利润增速", "symbols": ["300750.SZ"]})
print(res_num["numeric"])                  # is_growth=True → 同比%，带算式引用
```

默认使用最新底仓；某票缺语料时可 `run` 时开 `live_fill`（默认开）实时补，或先 `backfill`。
回答时必须带边界：**仅基于公开披露、不构成投资建议**；核对以官方原文为准。

## 禁止行为

- 不允许 agent 查询时重新 ingest 全部披露源（数字路实时查 `get_fina_reports` 是设计内、轻量）。
- 不允许手工修改 Parquet。
- 拒答两层要如实转达：硬拒答（`symbol` 不在底仓）由引擎保证；话题不覆盖由 agent 按 `answer_contract` 声明，**不编造**。
- 结果异常时须提示底仓日期范围与异常原因。
