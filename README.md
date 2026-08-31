<<<<<<< HEAD
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
=======
# crawl-gateway



## Getting started

To make it easy for you to get started with GitLab, here's a list of recommended next steps.

Already a pro? Just edit this README.md and make it your own. Want to make it easy? [Use the template at the bottom](#editing-this-readme)!

## Add your files

* [Create](https://docs.gitlab.com/user/project/repository/web_editor/#create-a-file) or [upload](https://docs.gitlab.com/user/project/repository/web_editor/#upload-a-file) files
* [Add files using the command line](https://docs.gitlab.com/topics/git/add_files/#add-files-to-a-git-repository) or push an existing Git repository with the following command:

```
cd existing_repo
git remote add origin https://gitlab.example.com/Aaron/crawl-gateway.git
git branch -M main
git push -uf origin main
```

## Integrate with your tools

* [Set up project integrations](https://gitlab.example.com/Aaron/crawl-gateway/-/settings/integrations)

## Collaborate with your team

* [Invite team members and collaborators](https://docs.gitlab.com/user/project/members/)
* [Create a new merge request](https://docs.gitlab.com/user/project/merge_requests/creating_merge_requests/)
* [Automatically close issues from merge requests](https://docs.gitlab.com/user/project/issues/managing_issues/#closing-issues-automatically)
* [Enable merge request approvals](https://docs.gitlab.com/user/project/merge_requests/approvals/)
* [Set auto-merge](https://docs.gitlab.com/user/project/merge_requests/auto_merge/)

## Test and Deploy

Use the built-in continuous integration in GitLab.

* [Get started with GitLab CI/CD](https://docs.gitlab.com/ci/quick_start/)
* [Analyze your code for known vulnerabilities with Static Application Security Testing (SAST)](https://docs.gitlab.com/user/application_security/sast/)
* [Deploy to Kubernetes, Amazon EC2, or Amazon ECS using Auto Deploy](https://docs.gitlab.com/topics/autodevops/requirements/)
* [Use pull-based deployments for improved Kubernetes management](https://docs.gitlab.com/user/clusters/agent/)
* [Set up protected environments](https://docs.gitlab.com/ci/environments/protected_environments/)

***

# Editing this README

When you're ready to make this README your own, just edit this file and use the handy template below (or feel free to structure it however you want - this is just a starting point!). Thanks to [makeareadme.com](https://www.makeareadme.com/) for this template.

## Suggestions for a good README

Every project is different, so consider which of these sections apply to yours. The sections used in the template are suggestions for most open source projects. Also keep in mind that while a README can be too long and detailed, too long is better than too short. If you think your README is too long, consider utilizing another form of documentation rather than cutting out information.

## Name
Choose a self-explaining name for your project.

## Description
Let people know what your project can do specifically. Provide context and add a link to any reference visitors might be unfamiliar with. A list of Features or a Background subsection can also be added here. If there are alternatives to your project, this is a good place to list differentiating factors.

## Badges
On some READMEs, you may see small images that convey metadata, such as whether or not all the tests are passing for the project. You can use Shields to add some to your README. Many services also have instructions for adding a badge.

## Visuals
Depending on what you are making, it can be a good idea to include screenshots or even a video (you'll frequently see GIFs rather than actual videos). Tools like ttygif can help, but check out Asciinema for a more sophisticated method.

## Installation
Within a particular ecosystem, there may be a common way of installing things, such as using Yarn, NuGet, or Homebrew. However, consider the possibility that whoever is reading your README is a novice and would like more guidance. Listing specific steps helps remove ambiguity and gets people to using your project as quickly as possible. If it only runs in a specific context like a particular programming language version or operating system or has dependencies that have to be installed manually, also add a Requirements subsection.

## Usage
Use examples liberally, and show the expected output if you can. It's helpful to have inline the smallest example of usage that you can demonstrate, while providing links to more sophisticated examples if they are too long to reasonably include in the README.

## Support
Tell people where they can go to for help. It can be any combination of an issue tracker, a chat room, an email address, etc.

## Roadmap
If you have ideas for releases in the future, it is a good idea to list them in the README.

## Contributing
State if you are open to contributions and what your requirements are for accepting them.

For people who want to make changes to your project, it's helpful to have some documentation on how to get started. Perhaps there is a script that they should run or some environment variables that they need to set. Make these steps explicit. These instructions could also be useful to your future self.

You can also document commands to lint the code or run tests. These steps help to ensure high code quality and reduce the likelihood that the changes inadvertently break something. Having instructions for running tests is especially helpful if it requires external setup, such as starting a Selenium server for testing in a browser.

## Authors and acknowledgment
Show your appreciation to those who have contributed to the project.

## License
For open source projects, say how it is licensed.

## Project status
If you have run out of energy or time for your project, put a note at the top of the README saying that development has slowed down or stopped completely. Someone may choose to fork your project or volunteer to step in as a maintainer or owner, allowing your project to keep going. You can also make an explicit request for maintainers.
>>>>>>> 345d056304c451e00814bf39ebb958f9e08b4503
