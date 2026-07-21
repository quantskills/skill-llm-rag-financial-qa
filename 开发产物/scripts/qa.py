#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
skill-llm-rag-financial-qa · 问答层（三路路由 + 引用纪律 + 拒答）
================================================================================
三路路由（"数字绝不进检索层"是防幻觉红线）：
  ① 数字路：净利/营收/增速等 → 查数字缓存(get_fina_reports PIT)，pandas 精确计算
  ② 底仓文本路：原因/条款/目的等 → retrieve BM25/直读底仓 doc
  ③ 全文路：点名要原文 或 底仓证据不足 → webfetch 官方披露渠道（可选，失败降级声明）

答案强制带引用：PandaData 用 [source_api|field|info_date|symbol]，网页用 [url|title|date|fetched]。
LLM 默认"调用方 agent 即 LLM"（零 key，返回证据包+契约）；证据不足→拒答。免责：仅研究/教育示例。
"""
from __future__ import annotations

import re
from typing import Any, Optional

import pandas as pd

from retrieve import retrieve, HashEmbedder

# 路由词表
NUM_WORDS = ["多少", "增速", "净利", "归母", "营收", "营业收入", "收入", "毛利", "每股", "eps",
             "几倍", "增长", "利润", "roe", "市值", "同比"]
FULLTEXT_WORDS = ["原文", "条款", "具体内容", "全文", "细则", "详细内容", "约定", "公告全文"]
METRIC_MAP = {
    "归母": "is_n_income_attr_p", "净利": "is_n_income_attr_p", "营业收入": "is_total_revenue",
    "营收": "is_revenue", "收入": "is_total_revenue", "毛利": "is_gross_profit",
    "营业利润": "is_operate_profit", "每股": "is_basic_eps", "eps": "is_basic_eps",
}
METRIC_ZH = {"is_n_income_attr_p": "归母净利润", "is_total_revenue": "营业总收入", "is_revenue": "营业收入",
             "is_gross_profit": "毛利", "is_operate_profit": "营业利润", "is_basic_eps": "基本每股收益"}
# 增速意图词：命中则数字路算"同比"（YoY），而非返回绝对值（防"增速被当绝对值答"）
GROWTH_WORDS = ["增速", "同比", "增长", "yoy"]
_SYM = re.compile(r"\d{6}\.(?:SZ|SH|BJ)", re.I)
_Q = re.compile(r"20\d{2}q[1-4]", re.I)


def _prev_year_q(quarter: str) -> Optional[str]:
    """同比基准：'2025q3' → '2024q3'（上年同季）。"""
    m = re.match(r"(20\d{2})(q[1-4])", str(quarter).lower())
    return f"{int(m.group(1)) - 1}{m.group(2)}" if m else None


def _prev_q_same_year(quarter: str) -> Optional[str]:
    """单季拆解基准：'2025q3'→'2025q2'；'2025q1'→None（Q1 累计即单季）。"""
    m = re.match(r"(20\d{2})q([1-4])", str(quarter).lower())
    if not m:
        return None
    n = int(m.group(2))
    return f"{m.group(1)}q{n - 1}" if n > 1 else None


# 自然语言"年报/中报/一季报…"→季度（防"2025年"命中不了 q 正则而静默取最新一期）
_Q_MID = re.compile(r"(20\d{2})\s*年?\s*(?:中报|半年报|半年度|中期)")
_Q_Q1 = re.compile(r"(20\d{2})\s*年?\s*(?:一季报|1季报|一季度|第一季)")
_Q_Q3 = re.compile(r"(20\d{2})\s*年?\s*(?:三季报|3季报|三季度|第三季)")
_Q_ANNUAL = re.compile(r"(20\d{2})\s*年报")
_Q_BARE_YEAR = re.compile(r"(20\d{2})\s*年")   # "2025年"无报种 → 默认年报(q4)


def resolve_quarter(question: str) -> tuple[Optional[str], bool]:
    """把问题里的期间措辞解析成 'YYYYqN'。返回 (quarter, explicit)。
    explicit=True 表示用户确实指定了期间（含"2025年/2025年报/中报"等自然语言）；
    None 表示完全没提期间——由上层显式声明"按最新一期作答"，绝不静默换季。"""
    ql = str(question).lower()
    m = _Q.search(ql)                                  # 精确 2025q4
    if m:
        return m.group().lower(), True
    for rx, suf in ((_Q_MID, "q2"), (_Q_Q1, "q1"), (_Q_Q3, "q3"), (_Q_ANNUAL, "q4"), (_Q_BARE_YEAR, "q4")):
        mm = rx.search(ql)
        if mm:
            return f"{mm.group(1)}{suf}", True
    return None, False


# 问题意图 → doc_type：命中则检索时软加权对应类型（修正混合类型下"公司名盖过主题"的排序问题）
DOCTYPE_INTENT = {
    "回购": "repurchase",
    "减持": "shareholder_change", "增持": "shareholder_change", "股东": "shareholder_change",
    "解禁": "restricted", "限售": "restricted",
    "预告": "forecast", "业绩预告": "forecast",
    "退市": "status_change", "st": "status_change", "重整": "status_change", "风险警示": "status_change",
    "龙虎榜": "lhb", "审计": "audit", "审计意见": "audit",
}


def intent_doc_types(question: str) -> set:
    ql = str(question).lower()
    return {dt for kw, dt in DOCTYPE_INTENT.items() if kw in ql}


def classify_route(question: str) -> list[str]:
    q = str(question).lower()
    routes = []
    if any(w in q for w in NUM_WORDS):
        routes.append("numeric")
    if any(w in question for w in FULLTEXT_WORDS):
        routes.append("fulltext")
    if not routes or "numeric" not in routes:
        routes.append("text")
    return routes


def _cite_doc(d: dict) -> str:
    if d.get("url"):
        return f"[{d['url']}|{d.get('name','')}{d.get('doc_type','')}|{d.get('info_date','')}|{d.get('symbol','')}]"
    return f"[{d.get('source_api','')}|{d.get('field','')}|{d.get('info_date','')}|{d.get('symbol','')}]"


# ============================================================================
# 数字路（精确计算，绝不进检索层）
# ============================================================================
def resolve_quarters(question: str) -> tuple[list[str], bool]:
    """解析问题里的**全部**期间（跨期问答用），如"2022 2023 2024 三年"→ 三个季度。

    单期沿用 resolve_quarter 的语义；多期时按出现顺序去重返回。
    返回 (quarters, explicit)；explicit=False 表示完全没提期间。
    """
    ql = str(question).lower()
    found: list[str] = []
    for m in _Q.finditer(ql):                          # 精确 2025q4 可能多个
        q = m.group().lower()
        if q not in found:
            found.append(q)
    if found:
        return found, True
    # 自然语言期间：同一后缀规则下可能出现多个年份（如"2023年和2024年报"）
    for rx, suf in ((_Q_MID, "q2"), (_Q_Q1, "q1"), (_Q_Q3, "q3"), (_Q_ANNUAL, "q4"), (_Q_BARE_YEAR, "q4")):
        for mm in rx.finditer(ql):
            q = f"{mm.group(1)}{suf}"
            if q not in found:
                found.append(q)
        if found:
            return found, True
    return [], False


def answer_numeric(question: str, cache, filters: dict) -> Optional[dict]:
    """数字路入口。支持**跨公司 / 跨期**：多票或多期时逐个精确计算，
    结果放在 `items`，绝不静默只答一个（清单 #42 要求「支持跨公司/跨期问答」）。"""
    if cache is None or getattr(cache, "empty", True):
        return None
    ql = str(question).lower()
    syms = (filters or {}).get("symbols") or [s.upper() for s in _SYM.findall(question)]
    metric = next((col for kw, col in METRIC_MAP.items() if kw in ql), None)
    if not syms or metric is None or metric not in cache.columns:
        return None
    quarters, q_explicit = resolve_quarters(ql)
    # 单票单期 → 原样返回（向后兼容既有调用方与引用格式）
    if len(syms) <= 1 and len(quarters) <= 1:
        return _numeric_one(ql, cache, syms, metric, quarters[0] if quarters else None, q_explicit)
    items, missing = [], []
    for sym in syms:
        got = 0
        for tq in (quarters or [None]):
            one = _numeric_one(ql, cache, [sym], metric, tq, q_explicit)
            if one:
                items.append(one)
                got += 1
        if got == 0:
            missing.append(sym)                        # 底仓无该票 → 记下来，绝不静默丢
    if not items:
        return None
    if len(items) == 1 and not missing:
        return items[0]
    # 多结果：顶层保留首条以兼容旧读法，但显式标 multi + note，逼调用方读全 items
    out = dict(items[0])
    out["multi"] = True
    out["items"] = items
    out["symbols"] = list(dict.fromkeys(i.get("symbol") for i in items if i.get("symbol")))
    out["quarters"] = list(dict.fromkeys(i.get("quarter") for i in items if i.get("quarter")))
    out["cite"] = " ; ".join(i["cite"] for i in items if i.get("cite"))
    note = (f"跨公司/跨期问题，共 {len(items)} 条结果，完整数据在 items[]；"
            f"顶层字段仅为 items[0]（{out.get('symbol')} {out.get('quarter')}），"
            "作答须逐条引用 items，不得只答其中一条")
    if missing:
        out["missing_symbols"] = missing
        note += f"；另有 {len(missing)} 只无数据须明说：{', '.join(missing)}"
    out["note"] = note
    return out


def _numeric_one(ql: str, cache, syms: list, metric: str,
                 tq_in: Optional[str], q_explicit: bool) -> Optional[dict]:
    """单票单期的精确取数（原 answer_numeric 主体）。"""
    sub = cache[cache["symbol"].isin(syms)].copy()
    if sub.empty:
        return None
    # PIT：同 quarter 取"原始公告(if_adjusted==0)优先、其内最新"的一行——不采后来的追溯调整版，
    # 引用落在原始年报公告日而非重述日（与 #48 datasource 口径一致）。保留全季，供同比取上年同期。
    sub["_pref"] = (pd.to_numeric(sub.get("if_adjusted", 0), errors="coerce").fillna(0) != 0).astype(int)  # 0=原始优先
    piv = (sub.sort_values(["quarter", "_pref", "date"], ascending=[True, True, False])
              .groupby("quarter", as_index=False).first().sort_values("quarter"))
    tq, explicit = (tq_in, q_explicit) if tq_in else resolve_quarter(ql)
    avail = list(piv["quarter"])
    if explicit and tq:
        row = piv[piv["quarter"].str.lower() == tq.lower()]
        if row.empty:
            # 用户明确指定了季度但底仓没有 → 明确告知，绝不静默换季（防"2025年报被答成2026Q1"）
            return {"metric": METRIC_ZH.get(metric, metric), "metric_field": metric, "is_growth": False,
                    "quarter": tq, "value": None, "quarter_available": False, "symbol": syms[0] if syms else None,
                    "note": f"底仓无 {tq} 数据（现有 {avail[0]}~{avail[-1]}）；未静默改用其它季度"}
        r = row.iloc[-1]
    else:
        r = piv.iloc[-1]                                # 未提期间 → 用最新一期，但下方显式声明
    val = r.get(metric)
    cur = None if pd.isna(val) else float(val)
    out = {
        "metric": METRIC_ZH.get(metric, metric), "metric_field": metric, "is_growth": False,
        "quarter": r.get("quarter"), "value": cur, "date": r.get("date"), "symbol": r.get("symbol"),
        "quarter_inferred": not explicit,
        "cite": f"[get_fina_reports|{metric}|{r.get('date')}|{r.get('symbol')}]",
    }
    if not explicit:
        out["note"] = f"问题未指明期间，已按最新一期 {r.get('quarter')} 作答；如需年报请写明「XXXX年报」或「XXXXq4」"
    # 单季拆解（财报为累计口径 → 单季 = 本季累计 − 上季累计；Q1 累计即单季）
    pq = _prev_q_same_year(r.get("quarter"))
    q_single = cur
    if pq and cur is not None:
        prow = piv[piv["quarter"].str.lower() == pq.lower()]
        pv = prow.iloc[-1].get(metric) if not prow.empty else None
        q_single = round(cur - float(pv), 4) if (pv is not None and not pd.isna(pv)) else None
    out["quarterly_value"] = q_single      # 单季值（None=缺上季累计无法拆）
    # 重述稳定性：该季共几个披露版本、核心值是否被后续修正过
    vers = sub[sub["quarter"].astype(str).str.lower() == str(r.get("quarter")).lower()]
    vals = pd.to_numeric(vers.get(metric), errors="coerce").dropna().round(2).unique()
    out["restated_count"] = int(len(vers))
    out["value_stable"] = bool(len(vals) <= 1)
    # 增速/同比：算真实 YoY（上年同季），负基数带符号；绝不拿绝对值冒充增速
    if any(w in ql for w in GROWTH_WORDS):
        out["is_growth"] = True
        out["metric"] = METRIC_ZH.get(metric, metric) + "同比增速"
        out["abs_value"] = cur
        pyq = _prev_year_q(r.get("quarter"))
        prow = piv[piv["quarter"].str.lower() == str(pyq).lower()] if pyq else piv.iloc[0:0]
        pv = None if prow.empty else prow.iloc[-1].get(metric)
        pv = None if (pv is None or pd.isna(pv)) else float(pv)
        if cur is not None and pv not in (None, 0):
            yoy = (cur / abs(pv) - 1.0) * 100.0 * (1 if pv > 0 else -1)
            out["value"] = round(yoy, 2)
            out["unit"] = "%"
            out["base_quarter"], out["base_value"] = prow.iloc[-1].get("quarter"), pv
            out["cite"] = (f"[get_fina_reports|{metric} yoy|{r.get('date')}|{r.get('symbol')}]"
                           f"（{r.get('quarter')}={cur:.4g} ÷ {pyq}={pv:.4g}）")
        else:
            # 无上年同期 → 显式标注不可算，而非误报绝对值为增速
            out["value"], out["growth_available"] = None, False
            out["note"] = f"缺上年同期({pyq})，无法计算同比；当期绝对值={cur}"
    return out


# ============================================================================
# 全文路（可选，webfetch 官方披露渠道；失败降级）
# ============================================================================
def _fetch_fulltext(question, filters, config) -> list[dict]:
    if not (config or {}).get("enable_web"):
        return []
    try:
        import webfetch
        syms = (filters or {}).get("symbols") or [s.upper() for s in _SYM.findall(question)]
        return webfetch.fetch_docs(syms, question, config=config)
    except Exception:  # noqa: BLE001
        return []


# ============================================================================
# 主入口
# ============================================================================
def answer(question: str, docs: Optional[list[dict]] = None, cache=None,
           filters: Optional[dict] = None, config: Optional[dict] = None) -> dict:
    """三路路由问答。返回证据包 + 引用（默认不生成成品答案，交调用方 agent 按契约写）。"""
    cfg = config or {}
    docs = docs or []
    route = classify_route(question)
    result: dict[str, Any] = {"question": question, "route": route, "mode": None,
                              "numeric": None, "evidence": [], "citations": [], "answer": None, "degraded": False}

    if "numeric" in route:
        num = answer_numeric(question, cache, filters or {})
        if num:
            result["numeric"] = num
            # 跨公司/跨期：逐条引用都要进 citations，防"只引一条"
            for it in num.get("items") or [num]:
                if it.get("cite"):
                    result["citations"].append(it["cite"])

    if "text" in route or "fulltext" in route:
        emb = HashEmbedder() if cfg.get("enable_vector") else None
        r = retrieve(question, docs, filters, top_k=cfg.get("top_k", 8), embedder=emb,
                     boost_types=intent_doc_types(question))
        result["mode"] = r["mode"]
        # 枚举类问题防静默截断：命中的候选总数 vs 实际返回数，截断则显式告知（artifact P1#8）
        result["total_matched"] = r["n_candidates"]
        result["returned"] = len(r["hits"])
        if r["mode"] in ("bm25", "hybrid") and r["n_candidates"] > len(r["hits"]):
            result["truncated"] = True
            result["truncated_note"] = f"命中 {r['n_candidates']} 条，仅返回相关性前 {len(r['hits'])} 条；枚举类问题请调大 top_k 或加 doc_types 过滤"
        for h in r["hits"]:
            d = h["doc"]
            result["evidence"].append({"text": d["text"], "symbol": d["symbol"], "name": d.get("name"),
                                       "info_date": d["info_date"], "source_api": d["source_api"],
                                       "doc_type": d["doc_type"], "score": h["score"], "cite": _cite_doc(d)})
            result["citations"].append(_cite_doc(d))

    # 全文路（底仓证据不足 或 点名要原文）
    if "fulltext" in route or (not result["evidence"] and not result["numeric"]):
        web_docs = _fetch_fulltext(question, filters, cfg)
        for d in web_docs:
            result["evidence"].append({"text": d.get("text", ""), "symbol": d.get("symbol"),
                                       "info_date": d.get("info_date"), "url": d.get("url"),
                                       "source_api": "web", "doc_type": "fulltext",
                                       "score": 1.0, "cite": _cite_doc(d)})
            result["citations"].append(_cite_doc(d))
        if cfg.get("enable_web") and not web_docs:
            result["degraded"] = True  # 网页通道失败 → 降级声明

    # 拒答
    if not result["evidence"] and not result["numeric"]:
        result["answer"] = "insufficient_evidence"
        result["degraded"] = True
        return result

    # LLM 生成（默认不配 → 交调用方 agent；配了才内部生成）
    if cfg.get("llm"):
        result["answer"] = _llm_answer(question, result, cfg["llm"])
    else:
        contract = ("请逐条基于 evidence/numeric 作答，每个结论后附 cite 引用；"
                    "语料未覆盖必须明说'不确定/超出语料边界'，禁止编造。")
        if (result.get("numeric") or {}).get("multi"):
            n = result["numeric"]
            contract += (f" 本题为跨公司/跨期问题：numeric.items 共 {len(n['items'])} 条，"
                         "必须逐条列出并各自引用，不得只答其中一条")
            if n.get("missing_symbols"):
                contract += f"；无数据的标的须明确说明：{', '.join(n['missing_symbols'])}"
            contract += "。"
        result["answer_contract"] = contract
    return result


def _llm_answer(question: str, result: dict, llm_cfg: dict) -> str:
    """可选：配置了 OpenAI-compatible endpoint 时内部生成带引用答案（默认路径不走此处）。"""
    try:
        import requests  # noqa: F401
        # 组织 prompt（证据 + 引用契约），调用 llm_cfg["endpoint"]。留作扩展；社区版默认不启用。
        return "[LLM 生成未启用：请配置 config.llm.endpoint]"
    except Exception:  # noqa: BLE001
        return "[LLM 不可用]"


if __name__ == "__main__":
    demo = [
        {"doc_id": "1", "symbol": "002011.SZ", "name": "盾安环境", "info_date": "20250730",
         "source_api": "get_repurchase", "doc_type": "repurchase", "field": "purpose",
         "text": "[002011.SZ 盾安环境 20250730 回购] 回购注销激励对象已离职未解除限售的限制性股票，回购目的为股权激励调整。", "numeric_ctx": {}},
    ]
    print("路由(回购目的):", classify_route("盾安环境回购的目的是什么"))
    print("路由(净利多少):", classify_route("宁德时代2025q3归母净利润多少"))
    a = answer("盾安环境回购的目的是什么", demo, filters={"symbols": ["002011.SZ"]})
    print("证据数:", len(a["evidence"]), "| 引用:", a["citations"][:1], "| mode:", a["mode"])
    b = answer("某不存在公司的董事长是谁", demo, filters={"symbols": ["999999.SZ"]})
    print("拒答:", b["answer"])
