# 河海大学考试复习助手

面向河海大学学生的 AI 试卷检索助手。输入“找高数期末试卷”等自然语言，系统理解查询条件，检索本地已登记资料，展示试卷信息并提供 PDF 下载。

项目地址：[minimaod/hhu-exam-agent](https://github.com/minimaod/hhu-exam-agent)

**当前为本地运行的最小版本，尚未部署在线服务。仓库提供源码、试卷目录和运行说明；真实试卷 PDF 与 API 密钥需自行准备。**

## 功能与边界

- 自然语言查询：一次查询一门课程，支持期中、期末或全部类型。
- 手动筛选：按课程名称或别名、考试类型查询。
- 真实资料下载：通过登记 ID 定位本地 PDF，保留文件名，显示下载错误。
- 查询反馈：展示理解、检索进度及结果、澄清、空结果或错误。
- 模型只提取条件；试卷信息、数量和下载入口由程序从目录生成。

当前不支持年份或学年筛选、精确课程版本筛选、多轮会话、学习进度记忆、题型统计和个性化复习计划。

## 资料目录

目录快照（2026-10-24）：

| 课程大类 | 全部 | 期中 | 期末 |
| --- | --- | --- | --- |
| 高等数学 | 14 | 7 | 7 |
| 概率论与数理统计 | 12 | 6 | 6 |
| 几何与线性代数 | 33 | 5 | 28 |
| 复变函数与积分变换 | 12 | 1 | 11 |
| 离散数学 | 5 | 0 | 5 |
| 大学物理 | 12 | 6 | 6 |
| 合计 | 88 | 25 | 63 |

目录维护在 `data/papers/catalog.json`。课程按大类归并，“高数”“线代”“复变”“大物”等别名由 `app/papers/courses.py` 处理。A/B、Ⅰ/Ⅱ 等版本主要保留在标题中；已配置的版本写法也可能归入同一大类，不能据此保证精确版本匹配。上下册等附加条件仍受自然语言支持范围限制。

**目录记录不等于文件已经提供。** 克隆仓库后可以查询已登记信息，但缺少对应 PDF 时下载会返回 404。使用现有目录时，需将文件放到每条 `file_path` 指定的相对位置；也可以维护与自己资料一致的目录。

## 技术栈与架构

- 前端：React 19、TypeScript、Vite、Tailwind CSS。
- 后端：Python 3.12、FastAPI、Pydantic、Uvicorn。
- 模型与流程：DeepSeek 官方接口、LangChain 模型适配、LangGraph 工作流。
- 数据：JSON 目录与本地 PDF，暂未接入数据库或向量检索。
- 通信：普通 HTTP 查询与下载，SSE 反馈自然语言查询进度和结果。

```text
React 页面
  ├─ 自然语言查询 → FastAPI → LangGraph
  │                            ├─ DeepSeek 提取条件并校验
  │                            ├─ 检索 → PaperService → PaperRepository → JSON 目录
  │                            └─ 澄清或范围提示
  │                   ← SSE 进度和最终结果
  ├─ 手动筛选 → FastAPI → PaperService → PaperRepository
  └─ 下载 → FastAPI → 根据登记 ID 检查路径及文件 → 返回 PDF
```

模型理解采用一次调用，程序校验结构后决定分支；检索分支复用现有服务，结果不经过第二轮模型生成。工作流状态只在本次请求内使用。

后端目录：

```text
app/
├─ main.py          # 应用组装、启动配置及统一异常处理
├─ config.py        # 模型配置及启动快照
├─ api/             # 健康检查、目录查询、下载和自然语言接口
├─ papers/          # 试卷模型、别名、仓库、业务服务与异常
└─ querying/        # 理解契约、提示词、模型适配、工作流与 SSE 服务
frontend/
├─ src/components/  # 查询入口、筛选、结果与试卷卡片
├─ src/lib/         # HTTP 请求、下载、SSE 解析与响应校验
└─ vite.config.ts   # 本地开发端口及 /api 代理
data/papers/
└─ catalog.json     # 已登记资料；PDF 不随仓库提供
tests/              # 后端契约、异常、模型替身及真实资料测试
```

## 本地启动

### 环境

项目已使用 Python 3.12.13、Node.js 24.21.0、pnpm 12.5.1、uv 0.11.14 进行本地验证。依赖由 `uv.lock` 和 `frontend/pnpm-lock.yaml` 锁定。

### 1. 准备模型配置

复制项目根目录的 `.env.example` 为 `.env`，在本地填入 DeepSeek 密钥：

```dotenv
DEEPSEEK_API_KEY=your-api-key-here
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-flash
```

- 默认模型为 `deepseek-flash`，使用 JSON Output 并关闭思考模式。
- 配置在后端启动时读取，已有环境变量优先；修改 `.env` 后需重启后端。
- 密钥仅供后端读取，不放入前端 `VITE_*` 变量。
- 缺少或错误配置不阻止健康检查及手动查询；自然语言接口会返回 503。
- `.env` 已被忽略，示例配置只含占位值。

### 2. 启动后端

在项目根目录运行：

```powershell
uv sync
uv run uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

接口文档：<http://127.0.0.1:8000/docs>。

### 3. 启动前端

另开终端：

```powershell
cd frontend
pnpm install
pnpm dev
```

浏览器打开 <http://127.0.0.1:5173>。Vite 在开发时将 `/api` 请求代理到 `http://127.0.0.1:8000`；生产部署入口需另行配置。

### 4. 准备 PDF

按 `data/papers/catalog.json` 中的 `file_path`，在 `data/papers/` 下创建对应子目录并放入 PDF。PDF、密钥、依赖目录、构建产物与缓存不进入版本控制。

## 使用示例

- “我要复习高数”：查询高等数学大类的已登记资料。
- “找大学物理期末试卷”：按课程及期末筛选。
- “找线代期末试卷”：使用课程别名查询。
- “我要复习”：提示补充课程。
- “找大学化学试卷”：当前目录无匹配，返回空结果。
- “只要 2024 年的高数试卷”：提示当前不支持该筛选条件。

每次提交独立理解一句完整需求。澄清后需补充完整问题重新提交。查询过程中可以停止，失败后可重试；点击某条结果的下载按钮，会另外请求对应 PDF。

## 接口

| 接口 | 用途 |
| --- | --- |
| `GET /api/health` | 返回 `{"status":"ok","service":"hhu-exam-review"}` |
| `GET /api/papers` | 按可选 `course`、`exam_type` 查询，返回 `items`、`total` |
| `GET /api/papers/{paper_id}/download` | 按已登记 ID 返回 PDF 附件 |
| `POST /api/query` | 接收 `{"query":"找高数期末试卷"}`，通过 SSE 返回进度及最终结果 |

`exam_type` 接受 `midterm`、`final`；省略或空白表示全部。非法非空值返回 422。列表按学年降序，同学年按考试日期降序，未知日期在后。

自然语言问题去除首尾空白后须为 1–1000 个字符，否则返回 422。SSE 事件为 `progress`、`result` 或 `error`；`result.kind` 区分找到、无匹配、澄清和范围提示。正常连接仅有一个最终事件，HTTP 200 不代表后续业务一定成功，提前断流按未完成处理。

目录整体不可用返回 503；未知 ID、文件缺失、越界、非 PDF 或当前不可读返回 404。下载前检查可预见的读取问题，传输过程中发生的文件变化仍可能导致中断。模型故障通过安全错误信息反馈，不把认证信息或服务端磁盘路径返回页面。

PowerShell 查询示例：

```powershell
curl.exe "http://127.0.0.1:8000/api/health"
curl.exe -G --data-urlencode "course=高数" --data-urlencode "exam_type=final" "http://127.0.0.1:8000/api/papers"
```

中文参数使用 `--data-urlencode`，避免 Windows 控制台直接拼接 URL 时的编码问题。

## 检查与构建

后端：

```powershell
uv run pytest
```

测试覆盖目录校验、课程与类型筛选、排序、异常、模型请求及工作流分支等。**完整后端测试包含真实 PDF 读取和下载用例，未补齐本地资料时不能全部通过。** 部分文件锁异常用例使用 Windows API。

前端：

```powershell
cd frontend
pnpm test
pnpm typecheck
pnpm build
```

前端测试使用 Node 内置测试运行器，覆盖 SSE 分块、中文跨块、CRLF、错误事件及提前断流。

当前版本独立核对过六门课程的目录计数、进程内查询和 88 份登记文件的下载哈希，并用模型替身检查工作流；前端 8 项测试及类型检查通过。此验证范围不等同于完整后端测试、全部真实模型语义或浏览器流程验证。

## 参考项目

- [ecommerce-query-agent](https://github.com/minimaod/ecommerce-query-agent)：参考 React、FastAPI、SSE、LangGraph 及模块分层方式。
- [deepsearch-agents](https://github.com/didilili/deepsearch-agents)：参考 Python 3.12、Agent 工作流及前后端任务交互的组织方式。

实际引用的上游代码需保留对应来源及许可证信息。
