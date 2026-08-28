# OA-ai 本地知识库系统

基于 **BM25 词法检索 + BGE 语义向量**混合检索的本地知识库管理平台，配备 **MiniMax 大模型智能对话**（含查询改写、跨文档对比分析）、**会议纪要结构化提取与二次生成**、**Three.js 3D 知识图谱**可视化。支持 PDF / Word / Excel / PPT / 文本 / HTML 等多格式文档的解析、检索、分类管理与关联关系图谱展示。

> **混合检索，优雅降级**：向量模型（BGE）缺失时自动回退纯 BM25，不影响服务可用性。
> **纯本地部署**：检索链路不依赖外部嵌入服务；仅「智能对话」「文档排版提取」需调用大模型 API（可关闭）。

---

## 功能特性

### 检索
- **混合检索**：BM25（jieba 分词）+ BGE 语义向量双路召回，RRF 融合。
- **精准匹配优化**（`app/search.py`）：
  - **完整短语加成**：查询原串完整出现 → 强加权，排到最前
  - **句级 IDF 加权覆盖率**：按句子统计加权覆盖，防止长文靠散落关键词刷分
  - **重叠词归并**：`智能/智能化` 视为同一概念，避免 jieba 冗余切分重复计权
  - **质量分档 + 分级门槛**：完整命中存在时，丢弃正文零命中的干扰项（宁缺毋滥）
- **前端高亮**：后端返回 jieba 分词 `terms`，前端按分词定位高亮（长查询如「安全生产责任制度」在原文写作「安全生产责任制」时也能正确命中）。

### 智能对话
- **模型**：`MiniMax-M2.5-highspeed`（229B 参数推理模型，20 万 token 上下文）。
- **查询改写**：多轮对话中自动补全指代（「那它的流程呢？」→「公司安全生产责任制的流程有哪些」）。首轮无历史时跳过，零开销。
- **对比分解**：检测对比意图（含「对比/区别/差异」等），拆解为多个子问题分别检索后合并，支持跨文档对比分析。
- **思考过程剥离**：推理模型的 `<think>` 标签自动剥离，不污染回答。

### 其他
- **3D 知识图谱**：Three.js 渲染文档/分类/标签关联网络，节点可点击下钻。
- **多格式文档**：PDF、DOCX、XLSX、PPTX、TXT、HTML 上传、解析、入库。
- **派生分析**：会议纪要结构化解析（议题切分）、二次生成 PDF。
- **权限与角色**：用户/角色/权限管理，细粒度权限点（查看、上传、删除、图谱、派生分析等）。

---

## 目录结构

```
kb_deploy/
├── app/                      # 后端核心（Flask）
│   ├── search.py             # 混合检索（BM25 + 向量融合、短语加成、质量分档）
│   ├── rag_query.py          # BM25 索引加载与打分
│   ├── rag_build_index.py    # BM25 索引构建脚本
│   ├── vec_store.py          # BGE 语义向量索引（缺失时回退哈希方案）
│   ├── llm.py                # MiniMax LLM 封装（思考剥离、查询改写、问题分解）
│   ├── kb_store.py           # 文档存储 / 元数据
│   ├── admin.py              # 用户 / 角色 / 权限
│   ├── chat_store.py         # 对话会话持久化
│   ├── extract_text.py       # 多格式文档解析（PDF/Office/HTML）
│   ├── pdf_make.py           # PDF 生成（reportlab）
│   ├── derived_store.py      # 会议纪要结构化解析与派生
│   ├── generate_data.py      # 原始数据导入
│   └── crypto.py             # 加密（secret.key）
├── scripts/
│   ├── serve.py              # ✅ 启动入口（Flask，默认 8080）
│   ├── start.sh              # ✅ 一行命令启动 + 进程守护（崩溃自动重启）
│   └── install.sh            # 首次安装脚本（systemd 服务）
├── web_vue/                  # 前端（Vue 3 + Vite + Three.js）
│   ├── dist/                 # 生产构建（serve.py 直接托管，无需 dev server）
│   └── src/components/       # SearchView / KbBrowse / ChatView / MeetingDerived ...
├── knowledge_base/
│   ├── bm25_index/           # BM25 索引（运行时生成）
│   ├── vec_index/            # 向量索引（运行时生成）
│   └── uploads/              # 上传文档 + user_documents.json 元数据
├── data/                     # ⚠️ 运行时数据（数据库 + 密钥，已 gitignore）
│   ├── secret.key            # 自动生成的 AES 密钥
│   └── kb_admin.db           # SQLite 用户 / 权限库
├── fonts/                    # PDF 生成用中文字体（约 29MB）
├── app/pdf_make.py           # 排版引擎
├── deploy_from_local.py      # ✅ 生产部署脚本（内网 SFTP 直传，推荐）
├── deploy_backend.py         # 旧部署脚本（仅传代码，已不推荐）
├── deploy.py / deploy_dist.py / deploy_now.py   # 历史部署脚本
├── config/.env.example       # 配置模板
├── requirements.txt
└── .gitignore
```

> **安全说明**：`data/`（数据库、密钥）、真实 `.env` 及 `.env.bak*`、`node_modules` 已在 `.gitignore` 排除。**请勿将含明文凭证的文件提交到 Git。**

---

## 快速开始（本地开发）

### 环境要求
- Python 3.10+（开发环境路径：`D:\python\python.exe`）
- 依赖见 `requirements.txt`

### 1. 安装依赖
```powershell
pip install -r requirements.txt
```

> 注：`requirements.txt` 仅列核心依赖。文档解析还需 `pdfplumber`/`PyMuPDF`、`python-docx`、`openpyxl`、`python-pptx`、`numpy`、`reportlab`、`python-dotenv`；部署脚本需 `paramiko`。若缺包，按启动报错逐个安装即可。

### 2. 配置 `.env`

复制 `config/.env.example` 为根目录 `.env`，关键项：

| 变量 | 说明 |
|------|------|
| `MINIMAX_API_KEY` | MiniMax API 密钥（智能对话、文档排版用；留空则这两项功能不可用） |
| `MINIMAX_API_URL` | 默认 `https://api.minimax.chat/v1/chat/completions`（国内版） |
| `MINIMAX_MODEL` | 默认 `MiniMax-M2.5-highspeed`（可改 `MiniMax-M2.5` 求更高质量） |
| `MINIMAX_STRIP_THINKING` | 默认 `true`，剥离推理模型 `<think>` 思考过程 |
| `KB_ROOT` | 部署根目录（**含 `scripts/` 的那一层**） |
| `KB_API_HOST` | 监听地址，默认 `0.0.0.0` |
| `KB_API_PORT` | 监听端口，默认 `8080` |
| `KB_TOP_K` | 检索返回条数，默认 `5` |

> ⚠️ **`KB_ROOT` 陷阱**：必须填**部署根**（如 `/opt/OA-ai`），代码会自动在其下拼接
> `knowledge_base/`、`data/`、`web_vue/dist/`。若误填成 `/opt/OA-ai/knowledge_base`，
> 路径会二次叠加成 `.../knowledge_base/knowledge_base`，索引与文档全部找不着。
> `serve.py` 已加防御：检测到目录里没有 `scripts/` 会自动回退并打印警告。
> 本地开发若不填，则自动用 `serve.py` 所在位置推导。

### 端口与监听（统一在 `.env` 管理）

**改端口只需改 `.env` 里一行**，无需动 systemd、无需改代码：

```bash
# /opt/OA-ai/.env
KB_API_PORT=8080      # 改成想要的端口
```
```bash
sudo systemctl restart kb
```

**优先级**：`.env` **高于** systemd `Environment=` 与 shell 环境变量。
`serve.py` 在读取任何配置前就以 `override=True` 加载 `.env`，确保改这里必定生效。

> 生产 systemd unit 已**刻意删除** `KB_ROOT` / `KB_API_HOST` / `KB_API_PORT`
> 三行 `Environment=`，避免同一项配置散落两处、改一处漏一处。

**确认当前生效配置**（无需登录服务器翻配置）：

```bash
curl http://192.168.30.155:8080/api/health
# {"port":8080,"host":"0.0.0.0","kb_root":"/opt/OA-ai",
#  "env_file":"/opt/OA-ai/.env", ...}
```

`env_file` 为 `null` 表示未找到 `.env`，此时回退用环境变量或默认值。

### 3. 启动服务
```powershell
# 前台（调试）
python scripts/serve.py

# 后台隐藏（常用）
Start-Process -FilePath "D:\python\python.exe" `
  -ArgumentList "scripts/serve.py" `
  -WorkingDirectory "e:/tedri_project/2026-06-08-task-1/kb_deploy" `
  -WindowStyle Hidden
```

访问：
- **管理门户**：http://127.0.0.1:8080/
- **3D 知识图谱**：http://127.0.0.1:8080/graph
- **健康检查**：http://127.0.0.1:8080/api/health

默认管理员：**`admin` / `Admin@123`**

### 4. 停止服务
```powershell
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { $_.CommandLine -match 'serve.py' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
```

---

## 索引与检索

### 构建 / 重建索引
```powershell
# BM25 索引（文档变更后必须重建，否则搜不到新文档）
python app/rag_build_index.py

# 向量索引（BGE）
python -c "import sys; sys.path.insert(0,'app'); import kb_store, vec_store; vec_store.rebuild(list(kb_store.iter_all_documents()))"
```

> 独立脚本运行时需确保 `KB_ROOT` 指向正确目录（见上文陷阱说明）。

### 索引说明

| 文件 | 作用 |
|------|------|
| `knowledge_base/bm25_index/bm25_index.pkl` | BM25 算法本体（词频、IDF），负责算分 |
| `knowledge_base/bm25_index/doc_metadata.pkl` | 609 个文本块原文与元数据，负责取内容 |
| `knowledge_base/bm25_index/documents_manifest.json` | 文档清单，供前端浏览 |
| `knowledge_base/vec_index/vec_index.pkl` | BGE 语义向量（512 维） |

**索引不是缓存，是主数据**——删除后检索失效，需重建。

### BGE 启动预热

BGE 模型冷启动约 20-33 秒（sentence-transformers 初始化需联网校验权重）。`serve.py` 在启动时用**后台线程**预热，避免首次检索卡顿：

- 向量索引为空时**自动跳过**预热（避免无谓等待）
- 预热失败不影响服务，自动回退 BM25
- 日志标记：`[信息] BGE 语义向量预热完成` / `[信息] 向量索引为空，跳过 BGE 预热`

---

## REST API 速览

### 检索与文档
| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/health` | 健康检查（`bm25_ready` / `vec_ready` / `kb_root`） |
| GET | `/api/kb/search?q=&top_k=` | 混合检索（BM25 + 向量），返回含 `terms` 分词供前端高亮 |
| GET | `/api/query?q=` | 旧版检索（兼容企微/外部调用） |
| GET | `/api/kb/documents` | 文档列表（支持 `q` 模糊搜索、分类/年份过滤） |
| GET | `/api/kb/document?doc_id=` | 文档详情 |
| POST | `/api/kb/upload` | 文档上传解析入库 |
| POST | `/api/kb/upload-zip` | 批量 ZIP 上传 |
| GET | `/api/kb/categories` | 分类树 |
| GET | `/api/kb/tags` | 标签 |

### 智能对话
| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/kb/chat/scope` | 当前用户可检索的分类范围 |
| GET/POST | `/api/kb/chat/sessions` | 会话列表 / 新建 |
| GET/DELETE | `/api/kb/chat/session/<sid>` | 会话详情 / 删除 |
| POST | `/api/kb/chat` | 提问（自动查询改写 + 对比分解） |

### 权限管理
| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/auth/login` | 登录 |
| GET | `/api/admin/users` | 用户管理（需 `user.view`） |
| GET | `/api/admin/roles` | 角色管理（需 `role.manage`） |
| GET | `/api/admin/permissions` | 权限点列表 |
| GET | `/api/admin/audit` | 审计日志 |

### 派生分析（会议纪要）
| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/derived/parse` | 纪要结构化解析 |
| GET | `/api/derived/list` | 派生列表 |
| GET | `/api/derived/<id>/pdf` | 生成 PDF |

前端通过同源 `/api/*` 调用，无需配置跨域。完整路由见 `scripts/serve.py`（约 80 个）。

---

## 部署到生产（Linux 内网机）

### 当前生产环境

| 项 | 值 |
|---|---|
| 服务器 | `192.168.30.155`（Ubuntu 24.04） |
| 部署目录 | `/opt/OA-ai` |
| 服务名 | `kb`（systemd，开机自启） |
| Python | `/opt/OA-ai/venv/bin/python` |
| 访问 | http://192.168.30.155:8080/ |
| 代码仓库 | `github.com/yanl1976/OA-ai`（公开仓） |
| 端口配置 | `/opt/OA-ai/.env` 的 `KB_API_PORT`（改后 `systemctl restart kb`） |

### 推荐方式：本地直传（`deploy_from_local.py`）

通过内网 SFTP 从开发机直传，**不依赖 GitHub**（外网下载不稳定时的首选）。

```powershell
# 完整部署：备份 → 上传 → 迁移数据 → 重建索引 → 切换 → 重启
python deploy_from_local.py

# 只上传不切换（预检）
python deploy_from_local.py --upload

# 一键回滚
python deploy_from_local.py --rollback
```

**脚本行为**：
1. 停止 `kb` 服务，备份到 `/opt/OA-ai.bak.<时间戳>`
2. 上传代码到 `/opt/OA-ai-new`（排除 `node_modules`、`.jieba_cache`、索引、备份目录）
3. 迁移生产独有数据：`.env`、`data/`（账号权限）、`knowledge_base/uploads/`（文档）、`venv/`
4. 语法检查
5. 重建 BM25 索引（用生产自己的文档）
6. 切换目录、修复属主、启动服务

**传输量**：约 103 MB / 690 文件，内网约 12 秒。

> **为何改用此方式**：原 SFTP 增量上传需在脚本里手工维护文件清单，新增文件（如 `app/llm.py`）漏登记会导致生产 `ModuleNotFoundError` 崩溃。整目录直传保证代码树一致，根治此类问题。

### 备用方式：服务器 git 拉取

`/opt/OA-ai` 已含完整 `.git`（非浅仓库，remote 为 HTTPS），可在服务器增量更新：

```bash
cd /opt/OA-ai
git pull origin main        # 工作区干净时可用
sudo systemctl restart kb
```

**若报 `Your local changes ... would be overwritten by merge`**，改用强制同步：

```bash
cd /opt/OA-ai
git fetch origin main && git reset --hard origin/main
sudo systemctl restart kb
```

**为什么会脏**：`deploy_from_local.py` 是「把本地工作区文件整棵传过去」，不走 git。
若本地有**未提交的改动**就部署，生产文件内容会比它的 git HEAD 新，于是 `pull` 被拒绝。
（典型场景：改了代码先部署、后提交，或提交前就上传。）

**规避方法**：部署前先 `git commit && git push`，再跑 `deploy_from_local.py`。

**已验证的修复流程**（当工作区改动与远端内容实际一致时，丢弃零损失）：

```bash
# 1）先确认工作区文件与 origin/main 内容相同（哈希一致即可安全丢弃）
git hash-object scripts/serve.py
git rev-parse origin/main:scripts/serve.py

# 2）一致则强制同步
git fetch origin main && git reset --hard origin/main
```

> ⚠️ **不要用 `sudo` 跑 git**：root 的 `HOME`/凭据环境与 `yanl` 不同，`git fetch` 会长时间挂起。
> 一律以 `yanl` 身份执行 git 命令，只有 `systemctl` / 写 `/opt` 才需要 `sudo`。
>
> ⚠️ 此方式只更新被 git 跟踪的文件，**索引仍需重建**，且不会迁移 `.env`、`venv` 等未入库数据。

### 首次安装（全新机器）

```bash
bash scripts/install.sh
```

生成 systemd 服务、安装依赖、构建索引。

> ⚠️ **Shell 脚本换行符**：本脚本从 Windows 直传后可能变成 CRLF，在 Linux 上会
> 因 heredoc 结束标记变成 `EOF\r` 而报 `unexpected end of file`，**整个脚本无法运行**。
> `deploy_from_local.py` 已自动把 `.sh` 转为 LF 上传；若手动 `scp` 上传，务必先转 LF：
> ```bash
> sed -i 's/\r$//' scripts/install.sh
> ```

### 一行命令启动 + 进程守护（`scripts/start.sh`）

**启动服务只需一条命令**，脚本自带守护进程，应用崩溃会自动重启：

```bash
bash /opt/OA-ai/scripts/start.sh
```

其他子命令：

```bash
bash scripts/start.sh stop       # 停止
bash scripts/start.sh restart    # 重启
bash scripts/start.sh status     # 查看状态
bash scripts/start.sh logs       # 实时跟踪日志
bash scripts/start.sh --help     # 帮助
```

**两种模式自动切换**（二选一，绝不重复守护）：

| 模式 | 触发条件 | 说明 |
|---|---|---|
| **systemd** | 本机装有 `kb.service` 且当前用户能免密控制它 | 委托 `systemctl`，自带崩溃重启与**开机自启**，最省心 |
| **standalone** | 无 systemd（容器等），或指定 `--standalone` | 脚本自带守护循环，后台常驻，检测到退出即拉起 |

```bash
bash scripts/start.sh --standalone    # 强制用脚本守护
```

**standalone 模式的守护行为**：

| 行为 | 默认 | 可调环境变量 |
|---|---|---|
| 重启延迟 | 5 秒 | `RESTART_DELAY` |
| 崩溃限流 | 5 次 / 300 秒 | `MAX_CRASH` / `CRASH_WINDOW` |
| 正常退出是否重启 | 是 | `RESTART_ALWAYS=no` 可改为不重启 |
| 日志上限 | 10 MB 自动轮转 | `MAX_LOG_SIZE` |

```bash
MAX_CRASH=10 RESTART_DELAY=10 bash scripts/start.sh --standalone
```

**产物**：

```
logs/kb.log              应用日志
logs/kb-guardian.log     守护日志（记录每次崩溃与重启）
run/kb.pid               应用 PID
run/kb-guardian.pid      守护进程 PID
```

守护日志示例（可看出崩溃原因与限流计数）：

```
[2026-08-29 00:59:43] [G] 守护进程启动 (pid=130796, 延迟=5s, 限流=5次/300s)
[2026-08-29 00:59:43] [G] 应用已启动 (pid=130804)
[2026-08-29 00:59:52] [G] 应用退出 (pid=130804, code=137, 存活 9s)
[2026-08-29 00:59:52] [G] 5s 后重启 (崩溃计数 1/5)
[2026-08-29 00:59:57] [G] 应用已启动 (pid=130867)
```

**已实测验证**：

- `kill -9` 应用进程 → 5 秒后自动拉起，服务恢复 ✅
- 连续崩溃 2 次 → 限流计数正常递增（1/5 → 2/5）✅
- 停止 → 进程干净退出，无残留 ✅

> ⚠️ **防双重守护**：若 systemd 已在运行同一服务，脚本会先尝试停止它；
> **停不掉则直接中止**并提示，绝不带冲突启动。
> （早期版本只警告就继续，导致端口冲突、应用反复崩溃，最终留下 4 个进程。）
>
> ⚠️ **崩溃限流很重要**：无限重启会让服务在根本起不来时持续空转刷日志。
> 达到上限后守护会停止并保留现场，便于排查。
> 恢复方式：排除故障后重新执行 `bash scripts/start.sh`。

### 守护进程配置（systemd）

服务由 systemd 托管，unit 文件在 `/etc/systemd/system/kb.service`：

```ini
[Unit]
Description=Local Knowledge Base Service (RAG + 3D Graph)
After=network.target

# 崩溃重启限流：防止反复崩溃时被无限重启
StartLimitIntervalSec=300
StartLimitBurst=5

[Service]
Type=simple
User=yanl
WorkingDirectory=/opt/OA-ai
ExecStart=/opt/OA-ai/venv/bin/python /opt/OA-ai/scripts/serve.py
Restart=on-failure
RestartSec=5
TimeoutStopSec=30
KillSignal=SIGTERM
MemoryMax=2G
StandardOutput=journal
StandardError=journal
SyslogIdentifier=kb
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=full

[Install]
WantedBy=multi-user.target
```

**关键项说明**：

| 配置 | 作用 |
|---|---|
| `Restart=on-failure` | 仅在异常退出时重启（正常退出不拉起；`systemctl stop` 也不触发） |
| `RestartSec=5` | 崩溃后等 5 秒再拉起，避免瞬时反复重启 |
| `StartLimitIntervalSec` + `StartLimitBurst` | **防崩溃风暴**：300 秒内最多重启 5 次，超出则停止尝试并进入 `failed`，等人工介入 |
| `MemoryMax=2G` | 内存上限，防止泄漏拖垮整机（实测常驻约 70MB） |
| `TimeoutStopSec=30` | 停止时先发 SIGTERM，30 秒未退出才强杀（默认 90 秒太久） |
| `ProtectSystem=full` | `/usr` `/boot` `/etc` 只读；`/opt` 不受影响，服务仍可正常读写数据 |
| `WantedBy=multi-user.target` | 开机自启（配合 `systemctl enable`） |

> ⚠️ **务必保留 `StartLimit*`**：此前服务曾因异常崩溃，被 systemd 无限重启
> **17653 次**，刷爆日志并持续占用资源。加上限流后，超限即停止并进入 `failed`，
> 便于人工排查而非任其空转。

**常用操作**：

```bash
systemctl status kb                  # 查看状态
systemctl restart kb                 # 重启
systemctl stop kb / start kb         # 停止 / 启动
systemctl enable kb                  # 设置开机自启
systemctl disable kb                 # 取消开机自启
journalctl -u kb -f                  # 实时跟踪日志
journalctl -u kb -n 100 --no-pager   # 查看最近 100 行
journalctl -u kb --since today       # 只看今天的日志
```

**修改 unit 后必须重载**：

```bash
sudo systemctl daemon-reload && sudo systemctl restart kb
systemd-analyze verify /etc/systemd/system/kb.service   # 改前先验语法
```

**验证守护是否真的生效**（杀掉进程看能否自愈）：

```bash
kill -9 $(systemctl show kb -p MainPID --value)
sleep 8
systemctl is-active kb      # 应仍为 active，且 PID 已变化
```

> 注意：`systemctl show` 里 `StartLimitIntervalSec` 显示为 `StartLimitIntervalUSec`
> （值为 `5min`），这是同一项，只是属性名带 `USec` 后缀。

**服务进入 failed 且不再重启时**（多半是触发了限流）：

```bash
systemctl status kb            # 看失败原因
journalctl -u kb -n 50         # 查日志定位根因
systemctl reset-failed kb      # 清除失败计数
systemctl start kb             # 排除故障后重新启动
```

### 服务运维

```bash
systemctl status kb          # 查看状态
systemctl restart kb         # 重启
journalctl -u kb -n 50 --no-pager   # 查看日志
curl http://127.0.0.1:8080/api/health
```

> 服务监听端口 **8080**（`.env` 的 `KB_API_PORT` 可覆盖）。

### 备份与磁盘管理

每次部署会在 `/opt` 生成两个目录：

| 目录 | 用途 | 何时可删 |
|---|---|---|
| `/opt/OA-ai.bak.<时间戳>` | 部署前的完整拷贝 | **至少保留最新一个**，`--rollback` 依赖它 |
| `/opt/OA-ai.old.<时间戳>` | 切换目录时移走的旧版本 | 确认新版本无误后即可删 |

每份约 460MB（`venv` 293M + `knowledge_base` 118M + `fonts` 37M）。连续部署会快速堆积，需定期清理。

**清理前务必先做文档比对**（确认新版本文档数 ≥ 备份，避免误删唯一副本）：

```bash
# 1）同口径统计文档数（两边用完全相同的 find 表达式）
find /opt/OA-ai/knowledge_base -type f \
  \( -name '*.pdf' -o -name '*.doc*' -o -name '*.xls*' \) | wc -l
find /opt/OA-ai.bak.<时间戳>/knowledge_base -type f \
  \( -name '*.pdf' -o -name '*.doc*' -o -name '*.xls*' \) | wc -l

# 2）确认没有「仅备份才有」的文档（输出应为 0 行）
find /opt/OA-ai/knowledge_base -type f \( -name '*.pdf' -o -name '*.doc*' -o -name '*.xls*' \) \
  -printf '%f\n' | sort -u > /tmp/cur.txt
find /opt/OA-ai.bak.<时间戳>/knowledge_base -type f \( -name '*.pdf' -o -name '*.doc*' -o -name '*.xls*' \) \
  -printf '%f\n' | sort -u > /tmp/bak.txt
comm -13 /tmp/cur.txt /tmp/bak.txt

# 3）确认无误后，按【显式路径】删除，切勿用 rm -rf /opt/OA-ai*
sudo rm -rf /opt/OA-ai.bak.<旧时间戳> /opt/OA-ai.old.<时间戳>
```

> ⚠️ `/opt` 属主为 `root`，删除/创建都需 `sudo`。
> ⚠️ **禁止 `rm -rf /opt/OA-ai*`** —— 会连同正在运行的 `/opt/OA-ai` 一起删除。

---

## 开发说明

- **前端**：Vue 3 + Vite。修改后**必须 `npm run build`** 生成 `dist/`，`serve.py` 托管的是构建产物，dev server 不生效。
- **后端**：Flask 单进程，`scripts/serve.py` 为入口，业务模块在 `app/`。
- **检索调参**：`app/search.py` 顶部常量
  - `TIER_KEEP_WHEN_EXACT = 2` — 存在完整命中时的保留档位（3=最严，1=放宽）
  - `PROX_TIER2 = 0.6` — 聚集度分档阈值
  - `PHRASE_BOOST = 2.5` / `PROX_WEIGHT = 2.0` / `LOW_COVER_PENALTY = 0.35`
- **数据持久化**：用户/权限在 `data/kb_admin.db`；文档原文与元数据在 `knowledge_base/uploads/`；索引在 `knowledge_base/*_index/`。

---

## 常见问题

**Q：页面改动没生效？**
A：前端需 `npm run build`；浏览器 `Ctrl+F5` 强刷。`serve.py` 托管的是 `web_vue/dist`，不是源码。

**Q：`vec_ready: false` / `status: index_missing`？**
A：**正常降级**，不是故障。生产服务器未装 `sentence-transformers`，或向量索引未构建。此时检索走纯 BM25，功能完整可用。日志会显示「向量索引为空，跳过 BGE 预热」。若需语义检索，安装 `sentence-transformers==6.0.0`（需下载约 2GB 的 torch）并重建向量索引。

**Q：检索无结果 / 新文档搜不到？**
A：文档变更后**必须重建索引**（见「索引与检索」）。`/api/health` 应返回 `bm25_ready: true`。

**Q：首次检索很慢（20-30 秒）？**
A：BGE 模型冷启动。`serve.py` 已有后台预热，若仍慢说明预热被跳过（向量索引为空）或独立脚本运行（不享预热）。

**Q：独立脚本读到 0 篇文档？**
A：检查 `.env` 的 `KB_ROOT` 是否指向了生产路径。见上文「KB_ROOT 陷阱」。

**Q：忘记管理员密码？**
A：删除 `data/kb_admin.db` 重启服务，将重新初始化 `admin / Admin@123`（**会清空已有用户**，谨慎）。

**Q：多个 serve.py 进程冲突？**
A：停止所有匹配 `serve.py` 的 python 进程后再启动单实例。
