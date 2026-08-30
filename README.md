# OA-ai 本地知识库系统

## 更新日志

### 2026-08-30 · 云之家审批单据自动拉取（重大功能 + 全链路排障）

新增从云之家按模板自动拉取会议纪要等审批附件入库，并修复 10 类问题：

| 问题 | 根因与修复 |
|---|---|
| 只拉到 7 条 | 模板只取首个命中 + `find_flows` 不翻页 → 改为匹配所有模板并逐模板翻完 |
| 2024 年度拉不到 | 附件在 `Kg_0`（dict）控件，旧代码只扫 list 型 → 新增 `Kg_0` 支持 |
| 自愈不生效，记录恒为 `ver=None` | 自愈清记录后未跳过 alive 判断被误判跳过 → 置 `_rechecking` 直接重拉 |
| 拉取到 110 条无进展 | `synced` 整轮才保存一次，中断全丢 → 每条增量保存 + 原子写 + finally 兜底 |
| 切页任务终止 | 同步阻塞 → 后台线程 + 轮询 + `/abort` 中止，切页不影响后端 |
| docx 无法读取 | 历史「`.docx` 名 + PDF 内容」名实不符 → 自动按 PDF 解析 + 修复脚本 |
| 32 份同名文件 | 同类单据附件名相同 → **仅重名时**加「单据日期_」前缀 |
| 全部文件被加时间戳 | 初期无差别加前缀 → 改为仅冲突时加 |
| 配置改了生产机不生效 | `yzj_pull_tasks.json` 被 `.gitignore` 排除 → 纳入版本库 |
| `.doc`（OLE）无法提取 | `extract_text` 不支持 → 新增 `_extract_doc()`（antiword/catdoc + 内置兜底） |

下载策略最终定为：**优先盖章版 PDF；仅当无盖章 PDF 时回退原文件 docx/doc，扩展名按实际内容定**。

新增接口参考文档 **[`docs/yunzhijia_api.md`](docs/yunzhijia_api.md)**：记录云之家各接口的请求/响应结构、
鉴权三 scope（含易遗漏的 `resGroupSecret`）、附件**两种控件字段对照表**（`Ps_0` list / `Kg_0` dict）、
下载策略、时间字段、去重记录字段语义与排查命令。

详见下方「云之家审批单据自动拉取」与「云之家拉取排障」两节。

---

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

### 云之家审批单据自动拉取（2026-08-30 新增）
从云之家（云之家开放平台）按模板自动拉取审批完成的会议纪要等单据附件入库。

- **任务化配置**：`config/yzj_pull_tasks.json`（**已纳入 git**，可随 pull 同步到各环境），支持模板、状态、时间范围、目标分类、定时（每日/每周）、限流间隔、单次条数上限。
- **多模板匹配**：模板名关键词匹配**所有**命中模板（早期只取第一个，导致漏拉）；`findFlows` 逐模板翻完全部页。
- **附件识别**：支持两种载体
  1. `Ps_0`/`Od_0` 等普通附件控件（`value` 为 list，含 `sealedFileId`/`wpsFileId`）
  2. **`Kg_0` 金山在线文档控件**（`value` 为 dict，含 `pdfFileId`/`fileId`）—— 2024 年度那批纪要走的就是它
- **下载策略**：优先盖章版 PDF；**仅当该单据无盖章 PDF 时**才回退下载原文件 docx/doc，扩展名按实际内容定（绝不把 docx 改名成 pdf）。下载后校验 `%PDF` 头，不符则自动回退原文件。
- **文件名规则**：不重名保持原文件名；**仅当重名时**才加「单据日期_」前缀（同类单据附件名常完全相同，实测 32 份同名）；重名且同日再加 `(2)` 序号。
- **异步可中止**：`/api/yzj/run` 后台线程执行并立即返回，前端轮询 `/api/yzj/status`，可 `/api/yzj/abort` 终止；**切换页面不影响拉取**（后端继续跑，返回页面自动续接进度）。
- **去重与断点续传**：每条处理完即**增量保存**去重记录（原子写），中断不丢进度，下次续拉不重复；异常时 `finally` 兜底保存。
- **自愈**：旧版本误判为「无附件」的记录会重新校验一次（用 `ver` 标记避免重复校验）。

> **接口参考**：云之家 API 的请求/响应结构、附件两种控件（`Ps_0` list / `Kg_0` dict）字段对照、
> 下载策略与排查命令，见 **[`docs/yunzhijia_api.md`](docs/yunzhijia_api.md)**。

**从零重拉（数据混乱时最省事的办法）**：`scripts/reset_yzj_pull.py`
删除云之家文档（物理文件 + 条目 + 重建索引）并清空去重记录，之后重新拉取即得干净数据。
**不会碰手动上传的文档**。

```bash
# 预览（默认只删今天拉取的）
/opt/OA-ai/venv/bin/python scripts/reset_yzj_pull.py \
  --meta /opt/OA-ai/knowledge_base/uploads/user_documents.json
# 确认后执行
/opt/OA-ai/venv/bin/python scripts/reset_yzj_pull.py \
  --meta /opt/OA-ai/knowledge_base/uploads/user_documents.json --apply
# 删除全部云之家文档（不限今天）：加 --all
# 只删文档但保留去重记录：加 --keep-synced
```

运维脚本 `scripts/fix_yzj_ext.py`：修复历史「名实不符」文件（`.docx` 名 + PDF 内容等）。
```bash
/opt/OA-ai/venv/bin/python scripts/fix_yzj_ext.py \
  --files-dir /opt/OA-ai/knowledge_base/uploads/files \
  --meta /opt/OA-ai/knowledge_base/uploads/user_documents.json            # 预览
# 确认无误后加 --apply 执行
```

### 其他
- **3D 知识图谱**：Three.js 渲染文档/分类/标签关联网络，节点可点击下钻。
- **多格式文档**：PDF、DOCX、DOC、XLSX、PPTX、TXT、HTML 上传、解析、入库。
- **派生分析**：会议纪要结构化解析（议题切分）、二次生成 PDF。
- **权限与角色**：用户/角色/权限管理，细粒度权限点（查看、上传、删除、图谱、派生分析等）。

### 系统设置与界面（2026-08-29 更新）
- **统一卡片网格**：原「管理功能」「系统维护」两个分组合并为统一卡片网格（入口卡片带「进入 →」，维护卡片带状态）。「系统初始化」高危卡片红框置底。
- **系统名称设定**：系统设置新增「🏷 系统名称」卡片，可设定系统名称并查看版本号。
  - **版本号**：由 Git 提交次数派生（每次 Git 更新即重新定义），规则为 `1.{commits//10}.{commits%10}`（每位满 10 进位，初始基准 1.0.0）。当前 55 次提交 → `v1.5.5`。
  - 系统名称持久化于 `config/system_settings.json`（默认 "OA-AI 知识库"）。
  - **首页左上角品牌名已变量化**：自动读取系统设置中的系统名称；管理员在系统设置修改并保存后，首页品牌名立即同步（全局响应式，无需刷新）。
- **页面水印**：所有内容显示页叠加全局半透明水印（使用者账号 + 姓名，如 `zhangsan（张三）`），用于信息流向追踪与防泄露。
  - 开关位于系统设置「🌐 页面水印」专用卡片（需 `system.manage` 权限切换），默认开启。
  - 后端以功能开关 `watermark_enabled` 持久化（`app/admin.py` 的 `DEFAULT_FEATURES`，默认 1）；`list_features()` 幂等补种，保证旧库升级后开关可用且可持久化切换。
- **概览首页联动**：首页「最近更新」列表点击文档 → 跳转「知识浏览」页并在右侧详情区打开该具体文档（与中间列表联动），不再跳独立详情组件。
- **纪要二次生成按钮**：知识浏览页右侧详情区对「纪要」类文档（分类名以"纪要"结尾，如"总经理会议纪要"）显示「纪要二次生成」按钮（需 `derived.manage` 权限）；无权限时显示禁用提示按钮，便于区分"功能丢失"还是"账号未授权"。

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
│   ├── admin.py              # 用户 / 角色 / 权限 + 功能开关（含 watermark_enabled）
│   ├── chat_store.py         # 对话会话持久化
│   ├── extract_text.py       # 多格式文档解析（PDF/Office/HTML，含 .doc OLE 兜底）
│   ├── yzj_pull.py           # 云之家审批单据拉取引擎（多模板/多控件/去重/自愈）
│   ├── yunzhijia_client.py   # 云之家开放平台 API 封装（token/模板/流程/下载）
│   ├── pdf_make.py           # PDF 生成（reportlab）
│   ├── derived_store.py      # 会议纪要结构化解析与派生
│   ├── generate_data.py      # 原始数据导入
│   └── crypto.py             # 加密（secret.key）
├── scripts/
│   ├── serve.py              # ✅ 启动入口（Flask，默认 8080）
│   ├── fix_yzj_ext.py        # 修复云之家历史「名实不符」文件（预览 / --apply）
│   ├── start.sh              # ✅ 一行命令启动 + 进程守护（崩溃自动重启）
│   └── install.sh            # 首次安装脚本（systemd 服务）
├── web_vue/                  # 前端（Vue 3 + Vite + Three.js）
│   ├── dist/                 # 生产构建（serve.py 直接托管，无需 dev server）
│   └── src/components/       # SearchView / KbBrowse / ChatView / MeetingDerived ...
├── docs/
│   └── yunzhijia_api.md      # 云之家接口参考（字段结构/附件控件/下载策略/排障）
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

### 系统设置
| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/system/info` | 系统信息（登录可见）：`system_name` + `version`（Git 提交数派生）+ `commits` |
| PUT | `/api/system/info` | 更新系统名称（需 `system.manage`，1~60 字符），请求体 `{ "system_name": "..." }` |
| GET | `/api/admin/features` | 功能开关列表（含 `watermark_enabled`）；旧库首次访问自动补种缺失开关 |
| PUT | `/api/admin/feature` | 切换功能开关（见上方"页面水印"） |

### 派生分析（会议纪要）
| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/derived/parse` | 纪要结构化解析 |
| GET | `/api/derived/list` | 派生列表 |
| GET | `/api/derived/<id>/pdf` | 生成 PDF |

### 云之家拉取（需 `system.manage` 权限）
| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/yzj/templates` | 云之家审批模板列表 |
| GET | `/api/yzj/tasks` | 拉取任务列表 |
| POST/PUT | `/api/yzj/tasks` | 新建 / 更新任务 |
| DELETE | `/api/yzj/tasks/<id>` | 删除任务 |
| **POST** | `/api/yzj/run/<id>` | **启动拉取（后台线程，立即返回）** |
| POST | `/api/yzj/abort/<id>` | 请求终止（协作式，下一条前退出） |
| GET | `/api/yzj/status/<id>` | 查询进度（`running` / `processed` / `stats` / `new_docs`） |
| GET | `/api/yzj/progress` | 全部任务的进度快照 |
| GET | `/api/yzj/pulled-docs` | 已拉取文档清单（`page=`、`page_size=`，固定 `source=yunzhijia`） |

> 前端「立即拉取」为异步：触发后轮询 `/api/yzj/status`，**切换页面不影响后端执行**；
> 返回页面时 `onMounted` 自动续接进度。详见 [`docs/yunzhijia_api.md`](docs/yunzhijia_api.md)。

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

**实战案例（2026-08-29，真实踩坑记录）**：

生产执行 `git pull origin main` 被拒绝，报两类冲突：

```
error: Your local changes to the following files would be overwritten by merge:
        app/admin.py  app/search.py  scripts/serve.py
        web_vue/src/api.js  web_vue/src/components/ChatView.vue
        web_vue/src/components/RoleManage.vue  web_vue/src/components/SearchView.vue
error: The following untracked working tree files would be overwritten by merge:
        scripts/start.sh
```

**排查与处理（零丢失）**：

1. 先对所有冲突文件做哈希比对，确认生产工作区内容与 `origin/main` **逐一一致**（假脏，非真改动）：

   ```bash
   for f in app/admin.py app/search.py scripts/serve.py \
            web_vue/src/api.js web_vue/src/components/ChatView.vue \
            web_vue/src/components/RoleManage.vue web_vue/src/components/SearchView.vue \
            scripts/start.sh; do
     printf "%-45s local=%s remote=%s\n" "$f" \
       "$(git hash-object "$f")" "$(git rev-parse origin/main:"$f")"
   done
   # 输出：8 个文件 local 与 remote 哈希全部相等 → 可安全丢弃工作区
   ```

   > 哈希一致即证明这些"未提交改动"纯属 `deploy_from_local.py` 整目录直传造成的假脏，
   > 实际内容相同，**丢弃零损失**。若任一文件哈希不一致，则应先 `git stash` 或人工 diff 保留真改动。

2. 确认一致后，强制同步（fetch 确保 origin/main 最新，再 reset）：

   ```bash
   git fetch origin main && git reset --hard origin/main
   # HEAD is now at f4d31a9 ...
   git status          # On branch main / Your branch is up to date with 'origin/main'
   git pull origin main # Already up to date.
   ```

3. 重启服务（按需）：

   ```bash
   sudo systemctl restart kb && sleep 8 && systemctl is-active kb   # → active
   ```

**结论**：假脏冲突的根因是"先 deploy 后 commit/push"，让生产工作区文件比 git HEAD 新。
根治办法仍是**部署前先 `git commit && git push`**，使本地、生产、远端三者同源，此后 `git pull` 不再报冲突。
哈希比对这一步是关键安全阀——务必先确认一致再 `reset --hard`，切勿对未知改动盲 reset。

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

- 服务在运行中执行 `start.sh` → **自动终止并重新启动**（pid 132132 → 132238）✅
- `kill -9` 应用进程 → 5 秒后自动拉起，服务恢复 ✅
- 连续崩溃 2 次 → 限流计数正常递增（1/5 → 2/5）✅
- 停止 → 进程干净退出，无残留 ✅

**权限配置（让脚本能自动停止/重启 systemd 服务）**：

脚本需要 root 权限才能启停 systemd 服务。若未配置，`start.sh` 会提示
"systemd 服务 kb 正在运行" 并中止。配置免密 sudo 即可解决：

```bash
sudo tee /etc/sudoers.d/kb-service > /dev/null <<'EOF'
# 仅放行 kb 服务的 systemctl 操作
yanl ALL=(root) NOPASSWD: /usr/bin/systemctl start kb
yanl ALL=(root) NOPASSWD: /usr/bin/systemctl stop kb
yanl ALL=(root) NOPASSWD: /usr/bin/systemctl restart kb
yanl ALL=(root) NOPASSWD: /usr/bin/systemctl is-active kb
yanl ALL=(root) NOPASSWD: /usr/bin/systemctl is-enabled kb
yanl ALL=(root) NOPASSWD: /usr/bin/systemctl status kb *
yanl ALL=(root) NOPASSWD: /usr/bin/systemctl show kb *
EOF
sudo chmod 440 /etc/sudoers.d/kb-service
sudo visudo -c          # 必须校验通过
```

> ⚠️ 修改 sudoers 前**务必用 `visudo -c` 校验**，语法错误会导致 sudo 完全不可用。
> 生产已配置此文件，位于 `/etc/sudoers.d/kb-service`。

**三级提权回退**（脚本内建，保证任何环境都能真正把服务拉起来）：

1. root → 直接执行
2. `sudo -n` → 免密 sudo（配了上面的 sudoers 时走这条）
3. `echo 密码 | sudo -S` → 从 `.env` 读取 `SUDO_PASSWORD`（无则回退 `SSH_PASSWORD`）

三条都不通才会中止并提示，此时会明确告诉你该怎么配。

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

> ⚠️ **别在脚本里快速连打启停命令**。限流是 300 秒 5 次，若自动化脚本在几秒内
> 连续 `stop`+`start`+`restart`，会触发 `start-limit-hit` 让服务进入 `failed`。
> （真实踩过：验证 sudoers 时按秒连打启停，服务直接 failed，
> 日志显示 `Start request repeated too quickly`。）
> 批量操作之间请留出 ≥8 秒间隔。

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

**Q：新增的系统名称 / 页面水印 / 卡片整理等前端改动没生效？**
A：前端改动需 `npm run build` 重建 `web_vue/dist`，而 **`dist/` 被 `.gitignore` 忽略，`git pull` 拉不到**。生产机更新必须另行同步 dist：开发机 `python deploy_dist.py`（走既定部署脚本，不触碰 git 跟踪文件），或生产机 `cd /opt/OA-ai/web_vue && npm run build`。后端 `scripts/serve.py`、`app/admin.py` 等会随 `git pull` 更新。

**Q：升级后系统设置里「页面水印」开关切换不生效？**
A：旧库缺少 `watermark_enabled` 这一行时，`setFeature` 的 UPDATE 会因找不到行而静默失效。已在 `admin.list_features()` 增加幂等补种（首次访问 `/api/admin/features` 自动 `INSERT OR IGNORE` 补入缺失开关），`git pull` + 重启后前端首次加载即补入，开关即可持久化切换。

**Q：系统名称改了但首页左上角没变？**
A：首页品牌名在页面加载时读取 `/api/system/info`；管理员在系统设置保存后全局立即同步。若未登录或非管理员，左上角显示的是最近一次读取到的名称（默认 "OA-AI 知识库"）。确认系统设置卡片已保存成功。

---

## 云之家拉取排障（实战记录）

> 接口字段结构与排障前置知识见 **[`docs/yunzhijia_api.md`](docs/yunzhijia_api.md)**。

### Q1：只拉取到 7 条，明明云之家有很多纪要

两个原因叠加，均已修复：

1. **模板只匹配第一个**：旧代码 `next(t for t in templates if kw in title)` 只取首个命中模板。
   实测「会议纪要」关键词可匹配 3 个模板（7 + 154 + 0 条），只取第一个就只有 7 条。
   现已改为匹配**所有**命中模板。
2. **`find_flows` 不翻页**：只取首页，单据超过 `page_size` 后永久丢失。现已逐模板翻完全部页。

### Q2：某批单据一直显示「无附件」而跳过

附件控件有**两种**，必须都支持：

| 控件 | value 类型 | 文件 id 字段 | 典型年份 |
|---|---|---|---|
| `Ps_0` / `Od_0` 普通附件 | **list** | `sealedFileId` / `wpsFileId` | 2025 / 2026 |
| **`Kg_0` 金山在线文档** | **dict** | `pdfFileId` / `fileId` | **2024** |

旧代码只扫 `value` 为 list 的控件，导致 2024 年度那批（`Kg_0`）被整体误判为"无附件"。
现已支持 `Kg_0`；对历史误判记录有**自愈**：无 `ver` 标记的 `no-attachment` 记录会重新校验一次。

### Q3：自愈已部署，但那批记录仍是 `ver=None`、数量不减

检查自愈分支后是否**漏了跳过 alive 判断**：

```python
if note == "no-attachment":
    if rec.get("ver") is None:
        del synced[inst_id]      # 清记录准备重拉
        ...
# ⚠️ 若此处继续往下走 alive 判断：
doc_ids = rec.get("doc_ids") or []          # 空 → fallback
alive = inst_id[-6:] in alive_suffix        # 用旧文件名后缀兜底
if alive:
    continue                                 # 误判"已落盘"→ 自愈失效
```

`no-attachment` 记录没有 `doc_ids`，会 fallback 用「inst_id 末 6 位是否出现在旧文件名后缀里」判断，
一旦撞上就跳过，表现为"跑满整轮却只多落盘 1 条"。
现已在自愈分支置 `_rechecking=True`，**跳过 alive 判断直接重拉**。

### Q4：拉取到一半停住，反复重拉已完成的单据

根因：去重记录 `synced` **只在整轮结束后保存一次**，中断即全部丢失
（文档已入库但去重没写，下次重跑会重复拉取）。

修复：
- 每处理完一条即**增量保存**（原子写：临时文件 + `os.replace`）
- 调度器与接口层均加 `finally` 兜底 `flush_task_synced()`
- 新增整轮运行时长上限 `max_runtime_sec`（默认 3600s）有序退出

### Q5：切到别的页面，拉取就中断

`/api/yzj/run` 改为**后台线程**执行并立即返回；前端轮询 `/api/yzj/status`，
`onUnmounted` 只停前端定时器（后端继续跑），`onMounted` 自动续接进度。终止走 `/api/yzj/abort`。

### Q6：落盘的 docx 文件无法读取 / 提取失败

`extract_text` **严格按扩展名路由**（`.docx` → OOXML 解析器，`.pdf` → PDF 解析器），名实不符必然失败。

已加兜底：`.docx` 名但内容为 `%PDF` 时**自动按 PDF 解析**（实测提取 1156 字，与正确命名结果一致）。

> ⚠️ **注意**：兜底只对**新提取**生效。历史落盘时已写入空 `text` 的文档不会被自动重跑，
> 这些才是「在系统里看不了」的文件。

**甄别要诀**：先确认文件是否**真有问题**，再决定处理。实测 36 号文件存在 3 份（md5 相同）：

| 文件 | text | 创建时间 | 判断 |
|---|---|---|---|
| `...36号_0b58e5.docx` | **0 字** | 07:53:31 | ❌ **旧残留，系统里确实看不了** |
| `20260731_...36号.pdf` | 910 字 | 11:38:49 | ⚠️ 多带日期前缀的重复 |
| `...36号.pdf` | 910 字 | 12:19:27 | ✅ 正常（新文件） |

**能正常查看的是右边两份新的 `.pdf`**，而非待处理的 `_0b58e5.docx`。
这类「旧残留 + 已有对应新版本」的情况，正确处理是**删除冗余**而非改名：

```bash
# 预览（推荐先看）
/opt/OA-ai/venv/bin/python scripts/fix_yzj_ext.py \
  --files-dir /opt/OA-ai/knowledge_base/uploads/files \
  --meta /opt/OA-ai/knowledge_base/uploads/user_documents.json --dedupe
# 确认无误后加 --apply
```

`--dedupe` 按内容 **md5** 分组，每组保留 1 份、删除其余。

**保留判据（只用这两条，不使用文件名前缀）**：
1. `text` 非空的优先（旧残留通常 `text` 为空）
2. 创建时间更早的优先（后拉的是重复）

> ⚠️ **为什么不用「有无 `YYYYMMDD_` 前缀」当判据**：
> 带前缀的文件**未必是冗余**——它们可能是「重名但内容不同」的真实文档
> （如 `20240929_` / `20240923_` / `20240918_` 是不同日期的天传所例会，
> md5 各不相同，本就不会进入 dedupe 流程）。用前缀当判据会误导，
> 也会让人误以为「带前缀的就要删」。同组内 md5 已相同，按创建时间删较晚那份最稳妥。

**判定对照表**：

| 情形 | md5 | 处理 |
|---|---|---|
| 同份文件被重复拉取（跨天、或重名后加前缀） | 相同 | ✅ 保留较早那份，删其余 |
| 不同日期的例会（文件名重名 → 加前缀区分） | **不同** | ❌ 不参与，全部保留 |
| 旧残留（`text` 为空）+ 已存在新版本 | 相同 | ✅ 删旧残留 |

输出中会打印每组的 md5 前 10 位与「共 N 份（内容完全相同）」字样，便于人工核对。


仅改名（不含 `--dedupe`）用于处理「名实不符且无对应新版本」的文件：

```bash
/opt/OA-ai/venv/bin/python scripts/fix_yzj_ext.py \
  --files-dir /opt/OA-ai/knowledge_base/uploads/files \
  --meta /opt/OA-ai/knowledge_base/uploads/user_documents.json --apply
```

### Q7：同名文件无法区分（实测 32 份同名）

同类单据的附件名常完全相同（如「天传所集团生产经营工作例会会议纪要」）。
规则：**不重名保持原名；仅当重名时**才加「单据日期_」前缀（取 `Da_0`，回退 `_S_DATE`/`createTime`）；同日仍重名再加 `(2)`。

> 注意：不是给所有文件加时间戳——只有真正冲突的才加。

### Q8：`fix_yzj_ext.py` 报 `uploads 条数: 0`

`kb_store` 路径拼接为 `KB_ROOT + "knowledge_base/uploads"`，而生产机 `.env` 的
`KB_ROOT` **已含** knowledge_base，导致拼出重复路径、读到空数组：

```
拼出: /opt/OA-ai/knowledge_base/knowledge_base/uploads/user_documents.json ← 不存在
实际: /opt/OA-ai/knowledge_base/uploads/user_documents.json
```

脚本已支持从 `--files-dir` 父目录推断元数据，但**建议显式指定 `--meta` 最稳妥**（见 Q6 命令）。
保存时会写回**实际读到的那个**文件并自动备份 `.bak`。

### Q9：改了任务配置但生产机不生效

`config/yzj_pull_tasks.json` 曾被 `.gitignore` 排除，`git pull` 同步不过去
（表现为"开发机拉全部、生产机只拉 N 条"）。现已**纳入版本库**。

> `config/.yzj_pull_synced.json`（去重记录）保持忽略——它是运行时本地状态，各环境应独立。

### Q10：旧版 `.doc`（OLE 格式）无法提取

`extract_text` 原不支持 `.doc`。现已加入允许列表并实现 `_extract_doc()`：
优先调用系统 `antiword` / `catdoc`，未安装则用内置 OLE 兜底。建议生产机安装以提升准确率：

```bash
sudo apt-get install -y antiword
```

### Q11：「文件列表都在，但看不到文件」——先查软删，别急着重下载

**实测结论（2026-08-30）**：生产机 344 条元数据中，**物理文件缺失 0 个**。
「列表有条目但界面看不到」几乎都不是文件丢失，而是 **`deleted=1` 软删**——
所有接口均以 `not u.get("deleted")` 过滤，软删文档界面完全不显示，但**物理文件与正文都在**。

自查（生产机）：

```bash
python3 -c "
import json, os
base='/opt/OA-ai/knowledge_base/uploads'
ups=json.load(open(base+'/user_documents.json',encoding='utf-8'))
if isinstance(ups,dict): ups=ups.get('items') or []
miss=[u for u in ups if u.get('stored_path') and not os.path.exists(os.path.join(base,u['stored_path']))]
print('元数据',len(ups),'| 物理文件缺失',len(miss),'| 软删',len([u for u in ups if u.get('deleted')]))
"
```

- `物理文件缺失 = 0` → 是软删，**用恢复脚本**，无需重新下载
- `物理文件缺失 > 0` → 才是真丢失，需从备份或重新上传

恢复脚本 `scripts/restore_deleted_docs.py`（默认预览，`--apply` 执行）：

```bash
# 按文件名关键词
/opt/OA-ai/venv/bin/python scripts/restore_deleted_docs.py \
  --meta /opt/OA-ai/knowledge_base/uploads/user_documents.json \
  --match "2025.3.11" --match "呆滞物料" --rebuild-index --apply

# 按 doc_id（可重复）
/opt/OA-ai/venv/bin/python scripts/restore_deleted_docs.py \
  --meta /opt/OA-ai/knowledge_base/uploads/user_documents.json \
  --doc-id up_142770527602 --doc-id up_414452157707 --rebuild-index --apply

# 恢复全部云之家文档，跳过正文异常巨大的（如 .doc 解析出上百万字）
/opt/OA-ai/venv/bin/python scripts/restore_deleted_docs.py \
  --meta /opt/OA-ai/knowledge_base/uploads/user_documents.json \
  --source yunzhijia --max-text 1000000 --rebuild-index --apply
```

> `--rebuild-index` 会在恢复后重建检索索引，否则界面可能仍检索不到（推荐加上）。
> 执行前自动备份元数据到 `user_documents.json.bak`。

**为什么不能「按列表自动从云之家重新下载」**：
落盘元数据只记录 `source: "yunzhijia"`，**不保存 `formInstId` / `fileId`**，
无法从元数据反查云之家单据。若确需重下，只能用拉取任务的 `synced` 记录
（`config/.yzj_pull_synced.json` 中的 `formInstId`）定位，或清去重后整轮重拉。

### Q12：去重记录状态速查（排查第一手依据）

`config/.yzj_pull_synced.json` 的每条记录按 `<formInstId>` 索引：

```json
{
  "ts": 1756530000,
  "files": 1,
  "doc_ids": ["up_xxx"],
  "note": "no-attachment",
  "ver": 2
}
```

| 记录状态 | 含义 | 下次行为 |
|---|---|---|
| 有 `doc_ids` | 已落盘 | 校验文档是否仍存活，存活则跳过 |
| `note=no-attachment` **且无 `ver`** | **旧版误判记录** | **触发自愈，重新校验一次** |
| `note=no-attachment` 且 `ver=2` | 已用新逻辑校验，确无附件 | 永久跳过 |
| `dry: true` | 试跑记录（未落盘） | 正式拉取时仍会处理 |

一键查看分布：

```bash
python3 -c "
import json, collections
d = json.load(open('/opt/OA-ai/config/.yzj_pull_synced.json', encoding='utf-8'))
c = collections.Counter()
for k, v in d.items():
    if v.get('note') == 'no-attachment': c['no-attachment(ver=%s)' % v.get('ver')] += 1
    elif v.get('doc_ids'): c['已落盘'] += 1
print('总数', len(d), dict(c))
"
```

> **判读要点**：如果 `no-attachment(ver=None)` 长期不减少，说明自愈没生效
> （先查 Q3）；拉取停止时先看文件 `mtime` 判断是"结束了"还是"卡住"。
