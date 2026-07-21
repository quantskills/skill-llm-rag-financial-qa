# skill-llm-rag-financial-qa (#42)

> Financial-report & filing RAG Q&A · BUILD-type skill · Community Project
> **Ask a question about an A-share company's filings/financials and get an answer that is cited to the official source, checkable, and refuses to fabricate.** Numbers are computed exactly, text answers carry provenance, and out-of-corpus questions are declined.

## What it answers

- Numeric: "Ningde 2024Q4 net-profit **growth**?" → exact **YoY +15.01%** from PIT financials (with the arithmetic in the citation), never model mental math.
- Textual: "**Why** did Zijin cut its Dun'an stake?" → retrieves the official filing text ("operational needs of its own"), with a four-part citation.
- Not covered: "Who is the chairman?" (not in corpus) → **refuses**, no fabrication.

**Explicitly does NOT**: value companies, give buy/sell advice, or predict prices. **Restates/computes public disclosure only; not investment advice.**

## Three-way routing ("numbers never enter the retrieval layer" is the anti-hallucination red line)

| Route | Trigger | Source | How it answers |
|---|---|---|---|
| ① Numeric | profit / revenue / margin / EPS / **growth (YoY)** | `get_fina_reports` (PIT by announce date) | pandas exact fetch / YoY, sign-correct on negative base |
| ② Corpus text | reason / purpose / terms / why | `database.parquet` corpus (7 sources) | pure-Python BM25 + metadata filter; direct-read for small material |
| ③ Full text | asks for "original text/clause" or corpus insufficient | official venues (CNINFO / exchanges / HKEX / EDGAR) | on-demand fetch → section chunking (optional, degradable) |

Every conclusion carries an official four-part citation `[api|field|date|symbol]`; insufficient evidence → refuse.

## Quick start

```bash
pip install --upgrade panda_data pyarrow
export PANDA_USERNAME=<phone>; export PANDA_PASSWORD=<password>   # or ~/.pandadata/pandadata.env

python 开发产物/scripts/build.py --question "宁德时代2024q4归母净利润增速" --symbols 300750.SZ
python 开发产物/scripts/build.py --question "紫金矿业为什么减持盾安环境" --symbols 002011.SZ
python 开发产物/scripts/build.py --symbols 002011.SZ 300750.SZ --backfill 20240101 20260711
python 开发产物/scripts/test.py     # fully offline self-test (all green without panda_data)
```

## Layout

```
开发产物/ (development)
  scripts/  qa.py · retrieve.py · ingest.py · webfetch.py · build.py · render.py · test.py (19 cases)
  references/  api_guide.md · quality_evidence.md · golden_qa.json
  SKILL.md / skill.json
生产产物/ (production)
  database.parquet             corpus (154 docs / 11 tickers)
  sample_quarterly_688347.html single-quarter decomposition chart sample
  SKILL.md                     corpus read rules
```

## Data & disclaimer

Source: PandaData (credentials via env vars or `~/.pandadata/pandadata.env`, **never hard-coded**) + official disclosure venues (secondary full-text source). **Only official venues are fetched; no paid research (Wind/Choice) — copyright + community rule §3.** The full-text route is off by default and needs a live-tested entry point.

**Community Project, not reviewed / certified / endorsed by QuantSkills. Research & educational example only; not investment advice, no return promises.** The Q&A only restates/computes public disclosure; verify against the original filing. Refusal has two tiers: hard (symbol not in corpus, engine-guaranteed) / topic-not-covered (declared by the calling agent per contract) — see quality_evidence.md.

License: GPL-3.0-only
