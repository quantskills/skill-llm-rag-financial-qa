#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
skill-llm-rag-financial-qa · BUILD 主入口（#42）
================================================================================
工具定位（BUILD开发与生产规则V2 §3）：分析报告型 BUILD（公告问答）。
把"财报/公告 RAG 问答"封装成可被 agent 调用的工具：三路路由（数字精确算 / 底仓文本检索 /
官方全文按需）+ 引用纪律 + 拒答。数据源 PandaData 优先，网页为官方披露次级源。

标准入口 run(input_data, config=None)：
  input_data = "问题串" 或 {"question","symbols"?,"date_range"?,"doc_types"?,"docs"?,"cache"?}
  · 直连：传 docs(+cache) → 离线问答（可测，不联网）
  · 生产：从 database.parquet 加载底仓语料；指定 symbol 缺语料时实时补
返回 qa 结果 dict（answer/evidence/citations/route/mode）。

生产产物：database.parquet = **底仓 doc 语料**（结果型，每日增量）。
免责：仅研究/教育示例，不构成投资建议。
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import pandas as pd

import qa

BUILD_ID = "42"
BUILD_NAME = "财报公告RAG问答"
DATA_VERSION = "rag-financial-qa-v1"

DEV_ROOT = Path(__file__).resolve().parents[1]
BUILD_ROOT = DEV_ROOT.parent
DEFAULT_CORPUS = BUILD_ROOT / "生产产物" / "database.parquet"

_DOC_COLS = ["doc_id", "symbol", "name", "info_date", "source_api", "doc_type", "field", "text", "url", "numeric_ctx"]


def _today() -> str:
    return datetime.now().strftime("%Y%m%d")


# ============================================================================
# 输入校验
# ============================================================================
def validate_input(input_data: Any) -> dict:
    if input_data is None:
        raise ValueError("input_data 不能为 None")
    if isinstance(input_data, str):
        if not input_data.strip():
            raise ValueError("问题串为空")
        return {"question": input_data}
    if isinstance(input_data, dict):
        if not input_data.get("question"):
            raise ValueError("dict 输入必须含非空 question")
        return dict(input_data)
    raise ValueError(f"不支持的 input_data 类型：{type(input_data)}")


# ============================================================================
# 语料库 parquet（底仓）
# ============================================================================
def save_corpus(docs: list[dict], out: Path = DEFAULT_CORPUS) -> Path:
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    for d in docs:
        r = {k: d.get(k) for k in _DOC_COLS if k != "numeric_ctx"}
        r["numeric_ctx"] = json.dumps(d.get("numeric_ctx", {}), ensure_ascii=False)
        r["build_id"] = BUILD_ID
        r["data_version"] = DATA_VERSION
        r["update_time"] = datetime.now().isoformat(timespec="seconds")
        rows.append(r)
    new = pd.DataFrame(rows)
    try:
        if out.exists():
            old = pd.read_parquet(out)
            new = pd.concat([old, new], ignore_index=True)
        new = new.drop_duplicates(subset=["doc_id"], keep="last")
        new.to_parquet(out, index=False)
        return out
    except ImportError:
        alt = out.with_suffix(".csv")
        new.drop_duplicates(subset=["doc_id"], keep="last").to_csv(alt, index=False)
        return alt


def inspect(symbol: str, path: Path = DEFAULT_CORPUS) -> dict:
    """核验入口：某 symbol 在底仓的 doc_type 分布 / 日期范围 / 条数（artifact P2#11）。"""
    from collections import Counter
    docs = [d for d in load_corpus(path) if d.get("symbol") == symbol]
    dates = sorted(str(d.get("info_date")) for d in docs if d.get("info_date"))
    return {"symbol": symbol, "in_corpus": bool(docs), "n_docs": len(docs),
            "doc_types": dict(Counter(d.get("doc_type") for d in docs)),
            "date_range": [dates[0], dates[-1]] if dates else None}


def brief(symbol: str, config: Optional[dict] = None) -> dict:
    """综合简报：一次把某票各 doc_type 的最新证据聚起来，agent 不用逐条猜措辞（artifact P2#11/#13）。"""
    from collections import defaultdict
    cfg = config or {}
    docs = [d for d in load_corpus(cfg.get("corpus_path", DEFAULT_CORPUS)) if d.get("symbol") == symbol]
    g: dict = defaultdict(list)
    for d in docs:
        g[d.get("doc_type")].append(d)
    by_type = {}
    for dt, ds in g.items():
        ds = sorted(ds, key=lambda x: str(x.get("info_date", "")), reverse=True)
        by_type[dt] = [{"info_date": x.get("info_date"), "text": x.get("text", "")[:140]} for x in ds[:2]]
    return {"symbol": symbol, "n_docs": len(docs), "by_doc_type": by_type}


def load_corpus(path: Path = DEFAULT_CORPUS) -> list[dict]:
    path = Path(path)
    if not path.exists():
        return []
    df = pd.read_parquet(path)
    docs = []
    for _, r in df.iterrows():
        d = {k: r.get(k) for k in _DOC_COLS if k != "numeric_ctx"}
        try:
            d["numeric_ctx"] = json.loads(r.get("numeric_ctx") or "{}")
        except Exception:  # noqa: BLE001
            d["numeric_ctx"] = {}
        docs.append(d)
    return docs


# ============================================================================
# 主入口
# ============================================================================
def run(input_data: Any, config: Optional[dict] = None) -> dict:
    cfg = config or {}
    spec = validate_input(input_data)
    question = spec["question"]
    filters = {k: spec[k] for k in ("symbols", "date_range", "doc_types") if spec.get(k)}

    docs = spec.get("docs")
    if docs is None:
        docs = load_corpus(cfg.get("corpus_path", DEFAULT_CORPUS))
        if filters.get("symbols") and cfg.get("live_fill", True):
            have = {d.get("symbol") for d in docs}
            need = [s for s in filters["symbols"] if s not in have]
            if need:
                import ingest
                api = cfg.get("api") or ingest.init_panda()
                docs = docs + ingest.build_corpus(need, spec.get("start", "20240101"),
                                                  spec.get("end", _today()), api)

    cache = spec.get("cache")
    if cache is None and "numeric" in qa.classify_route(question) and filters.get("symbols"):
        try:
            import ingest
            api = cfg.get("api") or ingest.init_panda()
            cache = ingest.load_numeric_cache(filters["symbols"], api)
        except Exception:  # noqa: BLE001
            cache = None

    return qa.answer(question, docs, cache, filters, cfg)


# ============================================================================
# 生产维护
# ============================================================================
def maintain_daily(date: Optional[str] = None, config: Optional[dict] = None) -> Path:
    """全市场当日增量语料（shareholder_change/lhb）→ 追加底仓 parquet。"""
    import ingest
    api = (config or {}).get("api") or ingest.init_panda()
    docs = ingest.build_corpus_market(date or _today(), api)
    return save_corpus(docs)


def backfill(symbols: list[str], start: str, end: str, config: Optional[dict] = None) -> Path:
    """给定 symbols 建底仓语料（7 源）→ 落 parquet。"""
    import ingest
    api = (config or {}).get("api") or ingest.init_panda()
    docs = ingest.build_corpus(symbols, start, end, api)
    return save_corpus(docs)


# ============================================================================
# CLI
# ============================================================================
def _cli():
    ap = argparse.ArgumentParser(description="财报公告 RAG 问答 BUILD #42")
    ap.add_argument("--question", default="盾安环境回购的目的是什么")
    ap.add_argument("--symbols", nargs="*", default=None)
    ap.add_argument("--backfill", nargs=2, metavar=("START", "END"), default=None)
    ap.add_argument("--inspect", metavar="SYMBOL", default=None, help="核验某票在底仓的覆盖")
    ap.add_argument("--brief", metavar="SYMBOL", default=None, help="某票各 doc_type 最新证据综合简报")
    args = ap.parse_args()
    if args.inspect:
        print(json.dumps(inspect(args.inspect), ensure_ascii=False, indent=2))
        return
    if args.brief:
        print(json.dumps(brief(args.brief), ensure_ascii=False, indent=2))
        return
    if args.backfill and args.symbols:
        print("语料落地:", backfill(args.symbols, args.backfill[0], args.backfill[1]))
        return
    res = run({"question": args.question, "symbols": args.symbols})
    print(f"路由 {res['route']} | mode {res['mode']} | 证据 {len(res['evidence'])} | "
          f"拒答={res['answer'] == 'insufficient_evidence'}")
    if res.get("numeric"):
        n = res["numeric"]
        print(f"数字路：{n['metric']} {n['quarter']} = {n['value']}  {n['cite']}")
    for e in res["evidence"][:3]:
        print(f"  {e['cite']} {e['text'][:60]}")


if __name__ == "__main__":
    _cli()
