#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
skill-llm-rag-financial-qa · 底仓语料 ingest（PandaData 文本源 → doc records）
================================================================================
职责：把 PandaData 7 个"带文本的公司行为/披露"接口，规范化为统一 doc record 底仓语料
（每日增量 / 3 年 backfill），供 retrieve/qa 检索；另建结构化"数字缓存"供数字路精确查询。

D1 实测确认的优质文本字段（2026-07-11）：
  get_repurchase.purpose(最长573字) · get_stock_shareholder_change.reason(144字,含direction)
  · get_stock_status_change.description(167字,ST/重整) · get_fina_forecast(预告方向,短)
  · get_restricted_list.relieve_reason(短) · get_lhb_list.reason(短) · get_audit_opinion(枚举)

doc record：{doc_id,symbol,name,info_date,source_api,doc_type,field,text,url,numeric_ctx}
数据源 PandaData；凭证走环境变量/官方 env 文件（**绝不硬编码**）。免责：仅研究/教育示例。
"""
from __future__ import annotations

import os
from datetime import timedelta
from pathlib import Path
from typing import Any, Optional

import pandas as pd

DOC_TYPE_ZH = {
    "repurchase": "回购", "shareholder_change": "股东增减持", "status_change": "状态变更",
    "forecast": "业绩预告", "restricted": "限售解禁", "lhb": "龙虎榜", "audit": "审计意见", "ir": "投资者关系",
}


# ============================================================================
# 客户端（内联精简，不跨仓库共享）
# ============================================================================
def _read_env_file() -> dict:
    env, p = {}, Path.home() / ".pandadata" / "pandadata.env"
    if p.exists():
        for line in p.read_text().splitlines():
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


def init_panda() -> Any:
    try:
        import panda_data
    except ModuleNotFoundError as exc:
        raise RuntimeError("无法导入 panda_data，请先 `pip install --upgrade panda_data`（需 ≥0.0.9）") from exc
    envf = _read_env_file()
    user = os.getenv("PANDA_USERNAME") or os.getenv("PANDA_DATA_USERNAME") or envf.get("DEFAULT_USERNAME", "")
    pwd = os.getenv("PANDA_PASSWORD") or os.getenv("PANDA_DATA_PASSWORD") or envf.get("DEFAULT_PASSWORD", "")
    base = os.getenv("PANDA_BASE_URL") or envf.get("JAVA_SERVICE_BASE_URL")
    if not (user and pwd):
        raise RuntimeError("缺少 PANDA 凭证（环境变量或 ~/.pandadata/pandadata.env）")
    panda_data.init_token(username=user, password=pwd, **({"base_url": base} if base else {}))
    return panda_data


def _compact(s: Any) -> str:
    return str(s).strip().replace("-", "").replace("/", "")[:8]


def _is_quota_or_service_error(exc: Exception) -> bool:
    return any(k in str(exc) for k in ("500009", "单日总流量", "200103", "权限", "ServiceError",
                                        "空 detail", "504", "Gateway Time-out", "600003"))


def _safe_call(fn, default=None):
    try:
        r = fn()
        return r if r is not None else default
    except Exception:  # noqa: BLE001
        return default


def chunk_pull(fetch_fn, start: str, end: str, chunk_days: int = 31) -> pd.DataFrame:
    """按 chunk_days 分段拉取（全市场大跨度），超限自动降级。"""
    cur, end_d = pd.to_datetime(_compact(start)).date(), pd.to_datetime(_compact(end)).date()
    frames, retry = [], 7
    while cur <= end_d:
        seg = min(cur + timedelta(days=chunk_days - 1), end_d)
        try:
            f = fetch_fn(cur.strftime("%Y%m%d"), seg.strftime("%Y%m%d"))
            if f is not None and not f.empty:
                frames.append(f)
        except Exception as exc:  # noqa: BLE001
            if _is_quota_or_service_error(exc):
                sub = cur
                while sub <= seg:
                    se = min(sub + timedelta(days=retry - 1), seg)
                    ff = _safe_call(lambda: fetch_fn(sub.strftime("%Y%m%d"), se.strftime("%Y%m%d")))
                    if ff is not None and not ff.empty:
                        frames.append(ff)
                    sub = se + timedelta(days=1)
            else:
                raise
        cur = seg + timedelta(days=1)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


# ============================================================================
# doc 规范化
# ============================================================================
def _mk_doc(symbol, name, info_date, source_api, doc_type, field, text, numeric_ctx=None, url=None, seq=0) -> dict:
    head = f"[{symbol} {name or ''} {info_date} {DOC_TYPE_ZH.get(doc_type, doc_type)}]"
    body = str(text or "").strip()
    return {
        "doc_id": f"{source_api}:{symbol}:{info_date}:{seq}",
        "symbol": symbol, "name": name or "", "info_date": _compact(info_date),
        "source_api": source_api, "doc_type": doc_type, "field": field,
        "text": f"{head} {body}".strip(), "url": url, "numeric_ctx": numeric_ctx or {},
    }


def _chunk_long(text: str, limit: int = 512) -> list[str]:
    """>limit 字长文按句号二级切，避免单 chunk 过长。"""
    text = str(text or "")
    if len(text) <= limit:
        return [text]
    parts, buf = [], ""
    for seg in re_split_sentences(text):
        if len(buf) + len(seg) > limit and buf:
            parts.append(buf); buf = ""
        buf += seg
    if buf:
        parts.append(buf)
    return parts or [text]


def re_split_sentences(text: str) -> list[str]:
    import re
    return re.findall(r"[^。；\n]+[。；\n]?", text) or [text]


# ============================================================================
# 7 源 loader（→ doc records）
# ============================================================================
def _rows(df):
    return df.to_dict("records") if df is not None and not df.empty else []


def load_repurchase(sym, start, end, api) -> list[dict]:
    df = _safe_call(lambda: api.get_repurchase(symbol=sym, start_date=_compact(start), end_date=_compact(end), fields=[]))
    out = []
    for i, r in enumerate(_rows(df)):
        purpose = r.get("purpose")
        if not purpose:
            continue
        for j, chunk in enumerate(_chunk_long(purpose)):
            out.append(_mk_doc(r.get("symbol", sym), r.get("name"), r.get("date"), "get_repurchase",
                               "repurchase", "purpose", chunk,
                               {"buy_back_value": r.get("buy_back_value"), "procedure": r.get("procedure")}, seq=f"{i}_{j}"))
    return out


def load_shareholder_change(sym, start, end, api) -> list[dict]:
    df = _safe_call(lambda: api.get_stock_shareholder_change(symbol=sym, start_date=_compact(start), end_date=_compact(end), fields=[]))
    out = []
    for i, r in enumerate(_rows(df)):
        reason = r.get("reason")
        if not reason:
            continue
        txt = f"{r.get('shareholder_name','')} {r.get('direction','')}：{reason}"
        out.append(_mk_doc(r.get("symbol", sym), r.get("name"), r.get("info_date"), "get_stock_shareholder_change",
                           "shareholder_change", "reason", txt,
                           {"direction": r.get("direction"), "ratio_up_limit": r.get("ratio_up_limit")}, seq=i))
    return out


def load_status_change(sym, start, end, api) -> list[dict]:
    df = _safe_call(lambda: api.get_stock_status_change(symbol=sym, start_date=_compact(start), end_date=_compact(end), fields=[]))
    out = []
    for i, r in enumerate(_rows(df)):
        desc = r.get("description")
        if not desc:
            continue
        txt = f"{r.get('type','')}：{desc}"
        out.append(_mk_doc(r.get("symbol", sym), r.get("name"), r.get("date"), "get_stock_status_change",
                           "status_change", "description", txt, {"type": r.get("type")}, seq=i))
    return out


def load_forecast(sym, start, end, api) -> list[dict]:
    # get_fina_forecast 不吃时间窗，这里按 info_date 后置过滤，与其余 6 源 [start,end] 口径一致
    df = _safe_call(lambda: api.get_fina_forecast(symbol=sym, fields=[]))
    out = []
    s, e = _compact(start), _compact(end)
    for i, r in enumerate(_rows(df)):
        info = _compact(r.get("info_date"))
        if info and not (s <= info <= e):
            continue
        typ, desc = r.get("forecast_type"), r.get("forecast_description")
        if not (typ or desc):
            continue
        lo, hi = r.get("forecast_growth_rate_floor"), r.get("forecast_growth_rate_ceiling")
        txt = f"业绩预告 {typ or ''}，{desc or ''}，净利增速区间 {lo}~{hi}%"
        out.append(_mk_doc(r.get("symbol", sym), None, r.get("info_date"), "get_fina_forecast",
                           "forecast", "forecast_type", txt,
                           {"growth_floor": lo, "growth_ceiling": hi, "type": typ}, seq=i))
    return out


def load_restricted(sym, start, end, api) -> list[dict]:
    """按解禁日聚合成"批次摘要"，不逐户建 doc——避免 IPO 配售机构年金逐户 3000+ 条把检索层撑爆。"""
    df = _safe_call(lambda: api.get_restricted_list(symbol=sym, start_date=_compact(start), end_date=_compact(end), fields=[]))
    rows = _rows(df)
    if not rows:
        return []
    frame = pd.DataFrame(rows)
    frame["_sh"] = pd.to_numeric(frame.get("relieve_shares"), errors="coerce").fillna(0.0)
    frame["_rd"] = frame.get("relieve_date").astype(str) if "relieve_date" in frame else ""
    out = []
    for i, (rdate, g) in enumerate(frame.groupby("_rd")):
        total, n = float(g["_sh"].sum()), int(len(g))
        top = g.sort_values("_sh", ascending=False).iloc[0]
        reasons = [str(x) for x in pd.Series(g.get("relieve_reason")).dropna().unique()][:2]
        ann = str(g.get("date").iloc[0]) if "date" in g else rdate
        many = f"，最大解禁方 {top.get('shareholder', '')}（{top['_sh']:.0f} 股）" if n > 1 else ""
        txt = (f"限售解禁批次 解禁日 {rdate}：合计解禁 {total:.0f} 股，涉及 {n} 个股东/产品{many}；"
               f"原因：{'、'.join(reasons)}")
        out.append(_mk_doc(sym, None, ann, "get_restricted_list", "restricted", "relieve_batch", txt,
                           {"total_shares": total, "n_holders": n, "relieve_date": rdate}, seq=i))
    return out


def load_lhb(sym, start, end, api) -> list[dict]:
    df = _safe_call(lambda: api.get_lhb_list(symbol=sym or "", start_date=_compact(start), end_date=_compact(end), type="", fields=[]))
    out = []
    for i, r in enumerate(_rows(df)):
        reason = r.get("reason")
        if not reason:
            continue
        txt = f"上榜原因：{reason}，成交额 {r.get('amount','')}，涨跌幅 {r.get('change_rate','')}"
        out.append(_mk_doc(r.get("symbol", sym), None, r.get("date"), "get_lhb_list",
                           "lhb", "reason", txt, {"amount": r.get("amount")}, seq=i))
    return out


def load_audit(sym, start_q, end_q, api) -> list[dict]:
    """实质性审计意见逐条建 doc；若接口返回了行但均为未审计(季报常态)，补一条覆盖标记，
    让下游能区分「0 条=接口无覆盖」与「有数据但均未审计」（artifact P1#5）。"""
    df = _safe_call(lambda: api.get_audit_opinion(symbol=sym, start_quarter=start_q, end_quarter=end_q, fields=[]))
    rows = _rows(df)
    raw_count = len(rows)
    out = []
    for i, r in enumerate(rows):
        op = r.get("opinion")
        if not op or op == "no_audit_performed":
            continue
        txt = f"{r.get('quarter','')} {r.get('audit_type','')} 审计意见：{op}，事务所 {r.get('agency','')}"
        out.append(_mk_doc(r.get("symbol", sym), None, r.get("date"), "get_audit_opinion",
                           "audit", "opinion", txt, {"opinion": op, "raw_count": raw_count}, seq=i))
    if raw_count and not out:   # 有行但均未审计（季报）→ 明确标注，而非静默 0 条
        out.append(_mk_doc(sym, None, "", "get_audit_opinion", "audit", "coverage",
                           f"审计意见：接口覆盖 {raw_count} 期，区间内均为未单独审计（季报常态），无非标意见记录。",
                           {"raw_count": raw_count, "opinion": "no_substantive"}, seq=0))
    return out


# ============================================================================
# 语料装配 + 数字缓存
# ============================================================================
def build_corpus(symbols: list[str], start: str, end: str, api=None,
                 start_q: str = "2023q1", end_q: str = "2026q4") -> list[dict]:
    """给定 symbols 建底仓 doc 语料（每票 7 源）。全市场增量见 maintain_daily。"""
    if api is None:
        api = init_panda()
    docs: list[dict] = []
    for sym in symbols:
        docs += load_repurchase(sym, start, end, api)
        docs += load_shareholder_change(sym, start, end, api)
        docs += load_status_change(sym, start, end, api)
        docs += load_forecast(sym, start, end, api)
        docs += load_restricted(sym, start, end, api)
        docs += load_lhb(sym, start, end, api)
        docs += load_audit(sym, start_q, end_q, api)
    return docs


def build_corpus_market(date: str, api=None) -> list[dict]:
    """全市场当日增量（shareholder_change/lhb 支持全市场按日期段）。"""
    if api is None:
        api = init_panda()
    d = _compact(date)
    docs: list[dict] = []
    docs += load_shareholder_change("", d, d, api)
    docs += load_lhb("", d, d, api)
    return docs


NUM_FIELDS = ["symbol", "date", "quarter", "if_adjusted", "is_revenue", "is_total_revenue",
              "is_n_income_attr_p", "is_basic_eps", "is_gross_profit", "is_operate_profit"]


def load_numeric_cache(symbols: list[str], api=None, start_q="2023q1", end_q="2026q4") -> pd.DataFrame:
    """数字路缓存：财报关键数字（营收/归母净利/EPS/毛利/营业利润），PIT 按公告日。
    数字问题走这里精确计算，绝不进检索层（防幻觉）。"""
    if api is None:
        api = init_panda()
    frames = []
    for sym in symbols:
        df = _safe_call(lambda s=sym: api.get_fina_reports(symbol=s, date=_compact(end_q[:4] + "1231"),
                        start_quarter=start_q, end_quarter=end_q, is_latest=False, fields=NUM_FIELDS))
        if df is not None and not df.empty:
            frames.append(df)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=NUM_FIELDS)


if __name__ == "__main__":
    api = init_panda()
    docs = build_corpus(["002011.SZ", "002217.SZ", "300750.SZ"], "20250101", "20260711")
    print(f"底仓语料 {len(docs)} 条")
    from collections import Counter
    print("doc_type 分布:", dict(Counter(d["doc_type"] for d in docs)))
    for d in docs[:3]:
        print(" ", d["doc_id"], "|", d["text"][:70])
    cache = load_numeric_cache(["300750.SZ"], api)
    print(f"\n数字缓存 {cache.shape}，列 {list(cache.columns)}")
