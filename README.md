# 河海大学考试复习助手

面向河海大学学生的复习工具。可以查找和下载历年试卷，也可以围绕真题、题目截图和自己的 PDF 资料开展复习。

**公网邀请内测已开放。** 访客可直接进入试卷库，按课程和考试类型查找、下载原卷。注册需要一次性邀请码。 [打开复习助手](https://hhu-exam.online/)

## 在线版能做什么

| 功能 | 使用方式 |
| --- | --- |
| 历年试卷 | 访客可浏览 13 门课程的 162 份试卷，并下载 PDF |
| 真题检索 | 登录后可检索 1,573 道已审核题目，查看题目出处 |
| 图文辅导与练习 | 输入题目或上传题图，请助手给提示、讲解或生成类似题，提交作答后查看反馈 |
| 错题与学习记忆 | 保存错题、回看历史会话，查看和纠正学习记忆 |
| 个人资料复习 | 上传 PDF，识别扫描页文字，在复习工作台查看笔记、知识图谱和自测 |

可以从“我要复习高数”开始。登录后也可以试试“找一道 Python 循环选择真题，先给提示”，找到题目后继续追问或做一道类似题。真题可以回看出处，生成的练习会单独呈现。遇到公式识别或计算结果有疑问时，可以对照原页核对。

资料来源和下架联系渠道见站内「资料说明」。试卷目录或题目有误时，可以在 GitHub 提交更正依据。 [提交反馈](https://github.com/minimaod/hhu-exam-agent/issues/new)

## 仓库与在线版

GitHub `main` 已同步 F015 内测源码，包含邀请码注册、账号数据隔离、真题检索、个人 PDF OCR 和复习工作台。试卷 PDF、已审核题库和运行配置需另行准备。

## 技术架构

- 前端使用 React、TypeScript、Vite 和 Tailwind CSS，接收 SSE 流式回复。
- 后端使用 Python、FastAPI 和 SQLite，负责试卷下载、会话和学习数据。
- 会话使用 LangGraph 调度，DeepSeek 根据用户请求选择工具，后端校验参数和操作权限，再执行查询或保存。
- 题库使用 SQLite 保存题目和来源，结合 BM25、本地 BGE 向量和 Qdrant 检索，通过 RRF 融合排序。Qdrant 使用本地存储。

一次真题请求从网页进入 FastAPI。Agent 选择检索工具，工具从题库读取已登记的题目和出处，后端再把回复和题目卡片流式发给网页。

## 本地运行

准备 Python 3.12、Node.js 24、uv 和 pnpm。把 [.env.example](.env.example) 复制为根目录的 `.env`，在其中配置 `DEEPSEEK_API_KEY`、`DEEPSEEK_BASE_URL` 和 `DEEPSEEK_MODEL`。密钥保存在本地后端配置中。

在项目根目录启动后端。

```powershell
uv sync
uv run uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

另开一个终端，从项目根目录启动前端。

```powershell
cd frontend
pnpm install --frozen-lockfile
pnpm dev
```

Vite 会将 `/api` 请求转发到本地后端。 [本地页面](http://127.0.0.1:5173) · [OpenAPI 文档](http://127.0.0.1:8000/docs)

首次启动后端后，在项目根目录运行 `uv run python -m app.auth.cli create-invite --label "本地测试" --expires-in-days 7`，再用生成的邀请码在本地页面注册。访客可直接浏览试卷目录。

### 本地数据

| 使用场景 | 数据要求 |
| --- | --- |
| 查询试卷目录 | 仓库自带 [试卷目录](data/papers/catalog.json) |
| 下载试卷 | 按目录中的 `file_path` 准备对应 PDF |
| 检索 Python 真题并回看出处 | 另行准备已审核题库、检索索引、模型缓存和对应的原卷 PDF |
| 图文辅导及练习 | 配置可用的模型，并输入题目或上传题图 |

本地学习库默认为 `data/local/study.sqlite3`，附件位于 `data/local/attachments`。可用 `HHU_STUDY_DB` 和 `HHU_QB_ROOT` 指向独立的学习库和题库。题库的抽取、审核、构建和发布脚本位于 [tools/question_bank](tools/question_bank)。

### 开发检查

在根目录运行 `uv run pytest tests -q`。在 `frontend` 目录运行 `pnpm test`、`pnpm typecheck` 和 `pnpm build`。完整题库回归需要本地试卷、题库和评测资料。
