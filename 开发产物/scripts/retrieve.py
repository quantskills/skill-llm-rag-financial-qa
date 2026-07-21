#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
skill-llm-rag-financial-qa · 检索层（轻量，零第三方重依赖）
================================================================================
"RAG 是思路不是枷锁"——以达成"可信公告问答"为准，检索按材料体量分层：
  · 小料直读（material < 阈值 token）→ 原文全量交调用方 agent（胜过一切检索）
  · 中等规模 → 自实现纯 Python BM25 + 元数据过滤（主力）
  · 可选向量通道 → HashEmbedder(确定性,供离线测试) / 外部 embedding，RRF 融合

doc record 契约（ingest.py 产出、qa.py 消费）：
  {doc_id, symbol, name, info_date(YYYYMMDD), source_api, doc_type, field,
   text, url(可空), numeric_ctx(dict)}

纯逻辑零 IO / 零联网，可离线单测。免责：仅研究/教育示例。
"""
from __future__ import annotations

import math
import re
from collections import Counter
from typing import Any, Optional

# 小料直读阈值（候选正文总字数低于此，直接全量返回让 agent 读）
DIRECT_READ_CHARS = 6000
_ZH = re.compile(r"[一-鿿]")
_ALNUM = re.compile(r"[a-z0-9]+")


def tokenize(text: str) -> list[str]:
    """中文 char-bigram + 英文/数字词。零依赖，对金融公告 BM25 足够。"""
    if not text:
        return []
    text = str(text).lower()
    toks = _ALNUM.findall(text)
    zh = _ZH.findall(text)
    if len(zh) == 1:
        toks.append(zh[0])
    toks += [zh[i] + zh[i + 1] for i in range(len(zh) - 1)]
    return toks


# ============================================================================
# 纯 Python Okapi BM25
# ============================================================================
class BM25:
    def __init__(self, docs_tokens: list[list[str]], k1: float = 1.5, b: float = 0.75):
        self.k1, self.b = k1, b
        self.docs = docs_tokens
        self.N = len(docs_tokens)
        self.doc_len = [len(d) for d in docs_tokens]
        self.avgdl = (sum(self.doc_len) / self.N) if self.N else 0.0
        self.tf = [Counter(d) for d in docs_tokens]
        df: Counter = Counter()
        for d in docs_tokens:
            df.update(set(d))
        self.idf = {t: math.log(1 + (self.N - n + 0.5) / (n + 0.5)) for t, n in df.items()}

    def score(self, q_tokens: list[str], i: int) -> float:
        if self.avgdl == 0:
            return 0.0
        s, dl, tf = 0.0, self.doc_len[i], self.tf[i]
        for t in q_tokens:
            if t not in tf:
                continue
            idf = self.idf.get(t, 0.0)
            freq = tf[t]
            s += idf * freq * (self.k1 + 1) / (freq + self.k1 * (1 - self.b + self.b * dl / self.avgdl))
        return s

    def search(self, q_tokens: list[str], top_k: int = 8) -> list[tuple[int, float]]:
        scored = [(i, self.score(q_tokens, i)) for i in range(self.N)]
        scored = [x for x in scored if x[1] > 0]
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:top_k]


# ============================================================================
# 确定性 HashEmbedder（可选向量通道；专供离线测试，无需模型/网络）
# ============================================================================
class HashEmbedder:
    def __init__(self, dim: int = 128):
        self.dim = dim

    def embed(self, text: str) -> list[float]:
        v = [0.0] * self.dim
        for tok in tokenize(text):
            h = hash(tok) % self.dim
            v[h] += 1.0
        norm = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / norm for x in v]


def _cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


# ============================================================================
# 元数据过滤 + 分层检索
# ============================================================================
def _match_filters(doc: dict, filters: Optional[dict]) -> bool:
    if not filters:
        return True
    if filters.get("symbols"):
        if doc.get("symbol") not in set(filters["symbols"]):
            return False
    if filters.get("doc_types"):
        if doc.get("doc_type") not in set(filters["doc_types"]):
            return False
    dr = filters.get("date_range")
    if dr:
        d = str(doc.get("info_date", ""))
        if dr[0] and d < str(dr[0]):
            return False
        if dr[1] and d > str(dr[1]):
            return False
    return True


def _rrf(rank_lists: list[list[int]], k: int = 60) -> dict[int, float]:
    """Reciprocal Rank Fusion 合并多路排名。"""
    fused: dict[int, float] = {}
    for ranks in rank_lists:
        for pos, idx in enumerate(ranks):
            fused[idx] = fused.get(idx, 0.0) + 1.0 / (k + pos + 1)
    return fused


def retrieve(query: str, docs: list[dict], filters: Optional[dict] = None,
             top_k: int = 8, embedder: Optional[Any] = None,
             boost_types: Optional[set] = None) -> dict:
    """分层检索：direct(小料直读) / bm25 / hybrid(BM25+向量 RRF)。
    boost_types：命中查询意图的 doc_type（如问"回购"→{repurchase}）软加权前置——
    修正"公司名 token 盖过主题、混合 doc_type 时错误类型排前"的相关性问题；不删候选，保召回。
    返回 {mode, hits:[{doc, score}], n_candidates}。"""
    cands = [d for d in docs if _match_filters(d, filters)]
    if not cands:
        return {"mode": "empty", "hits": [], "n_candidates": 0}
    boost = {i for i, d in enumerate(cands) if boost_types and d.get("doc_type") in boost_types}

    total_chars = sum(len(d.get("text", "")) for d in cands)
    q_tokens = tokenize(query)
    docs_tokens = [tokenize(d.get("text", "")) for d in cands]
    bm = BM25(docs_tokens)
    # 小料直读：候选正文很少 → 全量返回，但按 query 相关性排序（最相关先给 agent 读）
    if total_chars <= DIRECT_READ_CHARS:
        order = bm.search(q_tokens, top_k=len(cands)) if q_tokens else []
        seen = {i for i, _ in order}
        idxs = [i for i, _ in order] + sorted(
            (i for i in range(len(cands)) if i not in seen),
            key=lambda i: str(cands[i].get("info_date", "")), reverse=True)
        if boost:  # 意图 doc_type 稳定前置（全量仍返回，仅改顺序，保召回）
            idxs = [i for i in idxs if i in boost] + [i for i in idxs if i not in boost]
        return {"mode": "direct", "hits": [{"doc": cands[i], "score": 1.0} for i in idxs],
                "n_candidates": len(cands)}

    bm_ranked = bm.search(q_tokens, top_k=max(top_k, 20))
    if boost and bm_ranked:  # 软加权：意图类型 +0.5×最高分，浮上但不独占（保留强相关的其他类型）
        mx = max(s for _, s in bm_ranked) or 1.0
        bm_ranked = sorted(((i, s + (0.5 * mx if i in boost else 0.0)) for i, s in bm_ranked),
                           key=lambda x: x[1], reverse=True)

    if embedder is not None:
        qv = embedder.embed(query)
        vec_scored = sorted(((i, _cosine(qv, embedder.embed(cands[i].get("text", ""))))
                             for i in range(len(cands))), key=lambda x: x[1], reverse=True)[:max(top_k, 20)]
        fused = _rrf([[i for i, _ in bm_ranked], [i for i, _ in vec_scored]])
        if boost:
            mxf = max(fused.values()) if fused else 0.0
            for i in list(fused):
                if i in boost:
                    fused[i] += 0.5 * mxf
        order = sorted(fused.items(), key=lambda x: x[1], reverse=True)[:top_k]
        hits = [{"doc": cands[i], "score": round(sc, 4)} for i, sc in order]
        return {"mode": "hybrid", "hits": hits, "n_candidates": len(cands)}

    hits = [{"doc": cands[i], "score": round(sc, 4)} for i, sc in bm_ranked[:top_k]]
    return {"mode": "bm25", "hits": hits, "n_candidates": len(cands)}


if __name__ == "__main__":
    demo = [
        {"doc_id": "1", "symbol": "002011.SZ", "name": "盾安环境", "info_date": "20250730",
         "source_api": "get_repurchase", "doc_type": "repurchase", "field": "purpose",
         "text": "[002011.SZ 盾安环境 20250730 回购] 回购注销激励对象已离职未解除限售的限制性股票，回购目的为股权激励调整。", "numeric_ctx": {"value": 5.63e6}},
        {"doc_id": "2", "symbol": "002217.SZ", "name": "ST合泰", "info_date": "20250107",
         "source_api": "get_stock_status_change", "doc_type": "status_change", "field": "description",
         "text": "[002217.SZ ST合泰 20250107 状态变更] 公司因被法院裁定受理重整触及退市风险警示情形已消除，申请撤销退市风险警示。", "numeric_ctx": {}},
    ] * 30
    r = retrieve("回购目的是什么", demo, filters={"symbols": ["002011.SZ"]})
    print("mode:", r["mode"], "| candidates:", r["n_candidates"])
    for h in r["hits"][:2]:
        print(f"  {h['score']} {h['doc']['text'][:40]}")
