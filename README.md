# webgw — 給地端 LLM 的查詢感知網頁讀取工具

以 MCP (streamable-HTTP) 形式暴露 `web_fetch(url, query)`,供 OpenCode 等
agent 接上地端模型使用。上游是自架的 crawl4ai。

## 為什麼需要

地端模型的 context 通常 8k–32k。實測網頁 raw markdown 的中位數是 **14,451 tokens**,
單一網頁就吃掉半個 context;極端值 135,294 tokens (OpenReview) 根本放不進去。

本工具用 BM25 依 query 選出相關章節的**逐字原文**。實測 (12 查詢 / 3 頁面):

| 預算 | BM25 (有 query) | 密度啟發式 | 文件順序 |
|---|---|---|---|
| 2,000 tok | 11/12 | 1/12 | 1/12 |
| 4,000 tok | **12/12** | 1/12 | 3/12 |
| 8,000 tok | 12/12 | 5/12 | 10/12 |

## 設計上的實測依據

- **走 `/crawl/stream` 而非 `/crawl`** — 同一個失敗,`/crawl` 收斂成不透明的 HTTP 500,
  `/crawl/stream` 給出 `"Blocked by anti-bot protection: DataDome captcha"` 這類指名判定。
- **不用 `fit_markdown`** — 上游的 `PruningContentFilter` 是密度啟發式,實測會砍掉文章
  標題卻留下登入元件 (TechNews 的 outline 從 9 節掉到 2 節)。
- **沒有 query 時用文件順序截斷,不做密度過濾** — 密度在每個預算下都輸給單純截斷。
- **不維護 chrome 黑名單** — 導覽列不含查詢詞,BM25 分數為 0 自然出局。
- **PDF 在准入層擋掉** — 實測會讓上游硬崩潰 (`Page.goto: Download is starting`)。
- **404 需自行判定** — 上游對 GitHub 404 回 `success=True` 加完整錯誤頁 2,434 tokens。

## 映像

```
docker pull abc99012/webgw:0.1.0
```

digest `sha256:409e1ccd441c466ad75cea4c369df65de09d30b2222f3c9d58dadbfc894d36e5`

## 本機測試

```bash
cp .env.example .env       # 填入 CRAWL4AI_TOKEN
docker compose up --build
curl http://127.0.0.1:8080/healthz
```

## 設定

| 環境變數 | 預設 | 說明 |
|---|---|---|
| `CRAWL4AI_BASE_URL` | `http://127.0.0.1:11235` | 上游位址。移到區網時只改這個 |
| `CRAWL4AI_TOKEN` | — | 上游 API token |
| `GATEWAY_PORT` | `8080` | 監聽埠 |
| `SELECT_BUDGET_TOKENS` | `4000` | 選節預算 |
| `PASSTHROUGH_MAX_TOKENS` | `4000` | 小於此值直接回全文 |
| `FETCH_TIMEOUT_S` | `30` | 抓取逾時 |

## 中文驗證 (17 個案例)

| 情境 | 案例 | rank@1 | 4k 命中 |
|---|---|---|---|
| 同字集 (繁對繁 / 簡對簡) | 11 | 11/11 | 11/11 |
| 跨字集 (繁體查詢 vs 簡體內容) | 5 | 2/5 | 5/5 |
| 中英混雜 | 1 | 1/1 | 1/1 |

CJK 用字元 bigram 而非分詞器。跨字集靠繁簡同形詞仍可運作,但完全不同形的詞
(「訓練」vs「训练」) 會失效 —— 量化過繁簡轉換的效益是改善 3/7、持平 4/7,
因 4k 命中已達 5/5,暫不引入相依。

## 尚未實作

- raw markdown 快取層 (目前每次請求都重抓)
- 小模型預讀層 — 先驗證主模型能否直接從 BM25 原文段落作答,再決定是否需要
- 中文選節的充分驗證 — 目前只有 1 個中文案例,CJK 用 bigram 代替分詞
