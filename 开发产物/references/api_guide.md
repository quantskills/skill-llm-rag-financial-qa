# API 指南 · skill-llm-rag-financial-qa（#42）

> 数据接口、字段口径、PIT 规则、流量预算与真机实测结论。数据源 **PandaData**（`panda_data>=0.0.9`），
> 官方披露平台（巨潮/沪深交易所/HKEXnews/SEC EDGAR）为**全文次级源**。凭证走环境变量或
> `~/.pandadata/pandadata.env`，**绝不硬编码**。

## 一、接口清单与用途

### 数字路（1 个接口，精确取数）

| 接口 | 用途 | 关键字段（本 skill 用到） |
|---|---|---|
| `get_fina_reports` | 财报数字，PIT 按公告日 | `symbol, date, quarter, if_adjusted, is_revenue, is_total_revenue, is_n_income_attr_p, is_basic_eps, is_gross_profit, is_operate_profit` |

- 调用（见 `ingest.load_numeric_cache`）：
  ```python
  api.get_fina_reports(symbol="300750.SZ", date="20261231",
                       start_quarter="2023q1", end_quarter="2026q4",
                       is_latest=False, fields=NUM_FIELDS)
  ```
- `is_latest=False` 返回**每季全部披露版本**（原始 + 追溯调整），PIT 去重在 `qa.answer_numeric` 内做（同 `quarter` 取 `if_adjusted==0` 优先、最新公告日）。
- 中文指标映射：`is_n_income_attr_p`=归母净利润，`is_total_revenue`=营业总收入，`is_revenue`=营业收入，`is_gross_profit`=毛利，`is_operate_profit`=营业利润，`is_basic_eps`=基本每股收益。

### 底仓文本路（7 个文本源，ingest → doc 语料）

| 接口 | doc_type | 抽取字段 | 说明 |
|---|---|---|---|
| `get_repurchase` | repurchase | `purpose`（回购目的，最长 500+ 字） | 长文按句号二级切 ≤512 字 |
| `get_stock_shareholder_change` | shareholder_change | `reason` + `shareholder_name` + `direction` | 增减持原因（"自身经营需要"等） |
| `get_stock_status_change` | status_change | `description` + `type` | ST/退市风险/重整等状态变更 |
| `get_fina_forecast` | forecast | `forecast_type` + `forecast_description` + 增速上下限 | 业绩预告方向与净利增速区间 |
| `get_restricted_list` | restricted | `relieve_reason` + `shareholder` + 解禁股数/日 | 限售解禁 |
| `get_lhb_list` | lhb | `reason` + `amount` + `change_rate` | 龙虎榜上榜原因（支持全市场按日期段） |
| `get_audit_opinion` | audit | `opinion` + `audit_type` + `agency` | 审计意见（非 `no_audit_performed` 才入库） |

- doc record 契约：`{doc_id, symbol, name, info_date(YYYYMMDD), source_api, doc_type, field, text, url, numeric_ctx}`
- `text` 统一带头部前缀 `[代码 名称 公告日 类型]`，保证每个 chunk 自足、可回溯。

### 全文路（官方披露平台，可选、可降级）

| 平台 | 域名（白名单） | 覆盖 |
|---|---|---|
| 巨潮资讯网 | `www.cninfo.com.cn` | A 股法定披露 |
| 上交所 / 深交所 | `www.sse.com.cn` / `www.szse.cn` | 交易所公告 |
| 港交所披露易 | `www1.hkexnews.hk` | 港股 |
| SEC EDGAR | `www.sec.gov` | 美股 |

- **只抓官方披露平台**，白名单校验（`webfetch.is_official`）；**不抓研报/Wind/Choice 等付费源**（版权 + 社区规则 §3）。
- 限速 `RATE_LIMIT_SEC=1.0`、每题配额 `FETCH_QUOTA=10`、抓过落 `fulltext_cache/` 惰性积累。
- ⚠️ 具体检索入口（如巨潮公告查询）**需有网实测确定**，社区版默认 `enable_web=False`，未启用/失败返回 `[]`，qa 层据此**降级声明**"仅基于结构化字段"。

## 二、真机实测关键结论（2026-07-11，`python3119`）

以下为本会话真实调用 PandaData 得到、并写进设计的结论（可复现，见 `quality_evidence.md`）：

1. **数字路 PIT 正确**：`get_fina_reports(300750.SZ)` 实测返回 13 个季度（2023q1–2026q1），含追溯版本；PIT 去重后 2024q4 归母净利 = **507.4 亿**（`is_n_income_attr_p=50,744,682,000`，公告日 20260310），与宁德时代 2024 年报一致。
2. **同比可算且符号正确**：2024q4 归母净利同比 = **+15.01%**（507.4 亿 ÷ 2023q4 的 441.2 亿）；2024q4 营收同比 = **−9.7%**（3620 亿 ÷ 4009 亿），负增长正确带号。
3. **`is_total_revenue` 不是"毒字段"**：把它放进 `fields` 与否，`get_fina_reports` 均正常返回 17 行——旧版怀疑的"该字段致缓存全 None"**在当前 API 上不可复现**，故保留。
4. **未来 as-of 日容错**：`date="20261231"`（未来）不报错，等价于"取至今全部"；不造成前视（数字路取的是各季当期原始公告）。
5. **底仓文本源密度**（随包样例 11 票 backfill 实测，见生产产物）：`lhb 62 / audit 41 / shareholder_change 17 / repurchase 14 / restricted 8 / forecast 7 / status_change 5`，共 154 条。

## 三、PIT（时点正确）口径

- 数字路：同一 `quarter` 内，先按 `if_adjusted==0`（当期原始）优先、再按公告 `date` 取最新，**每季一行**；同比取"上年同季"同样口径的行。
- 文本路：doc 的 `info_date` = 官方公告日；元数据过滤支持 `date_range` 卡时点。
- 结论：**不偷看追溯调整、不偷看未来公告**——历史回放与实时问答口径一致。

## 四、流量预算与降级

- `ingest.chunk_pull` 按 31 天分段拉全市场大跨度，命中配额/服务错误（`500009 单日总流量`/`200103`/`权限`/`504`…）自动切 7 天细粒度重试。
- `_safe_call` 包裹每次接口调用：异常/None → 返回默认空，**不使整条 ingest 崩溃**。
- 数字缓存缺失、网页通道未启用/失败 → qa 层降级：能算的算、能引的引，其余显式声明"超出语料边界"，**绝不编造**。

## 五、依赖与运行

```bash
pip install --upgrade panda_data pyarrow          # 必需
pip install beautifulsoup4 pdfminer.six requests  # 可选：全文路 extract / 抓取 / 内置 LLM
export PANDA_USERNAME=<手机号>; export PANDA_PASSWORD=<密码>   # 或 ~/.pandadata/pandadata.env
python scripts/test.py                             # 全离线自测（无 SDK 也全绿）
```
