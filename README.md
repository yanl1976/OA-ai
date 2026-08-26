# OA-ai 本地知识库系统

基于 **BM25 + jieba 中文分词全文检索** 与 **自研向量索引（numpy + jieba，零外部模型依赖）** 的本地知识库管理平台，配备 **Three.js 3D 知识图谱** 可视化。支持 PDF / Word / Excel / PPT / 文本 / HTML 等多格式文档的解析、检索、分类管理与关联关系图谱展示。

> 纯本地部署，无云依赖；检索链路不依赖任何大模型 API 或外部嵌入服务，开箱即用。

---

## 功能特性

- **全文检索**：BM25 算法 + jieba 中文分词，支持标题/正文/标签加权匹配，按文档分类过滤。
- **向量检索**：纯 numpy 实现的向量索引（`vec_store.py`），基于 jieba 词向量构建，零模型下载。
- **3D 知识图谱**：Three.js 渲染文档/分类/标签的关联网络，节点可点击下钻。
- **多格式文档管理**：PDF、DOCX、XLSX、PPTX、TXT、HTML 等上传、解析、入库。
- **权限与角色**：用户/角色/权限管理（`admin` 默认管理员），细粒度权限点（查看、上传、删除、图谱、派生分析等）。
- **派生分析**：`derived` 模块支持基于知识库的二次结构化分析。
- **REST API**：提供检索、上传、登录、健康检查等接口，便于集成。

---

## 目录结构

```
kb_deploy/
├── app/                      # Flask 后端核心
│   ├── serve.py              # ⚠️ 应用入口（实际在 scripts/serve.py 调用）
│   ├── rag_query.py          # 检索链路（BM25 + 向量融合）
│   ├── rag_build_index.py    # BM25 索引构建脚本
│   ├── vec_store.py          # 自研向量索引（numpy + jieba）
│   ├── kb_store.py           # 文档存储 / 元数据
│   ├── admin.py              # 用户 / 角色 / 权限
│   ├── crypto.py             # 加密（secret.key）
│   └── ...
├── scripts/
│   ├── serve.py              # ✅ 实际启动入口（Flask，默认 8080）
│   └── install.sh            # 部署脚本（生成 systemd 服务 / 内网机部署）
├── web_vue/                  # 前端（Vue 3 + Vite + Three.js）
│   ├── dist/                 # 生产构建（由 serve.py 直接托管，无需 dev server）
│   └── vite.config.js
├── knowledge_base/
│   ├── bm25_index/           # BM25 索引缓存（运行时生成，已 gitignore）
│   ├── vec_index/            # 向量索引缓存（运行时生成，已 gitignore）
│   └── uploads/              # 上传文档（运行时，已 gitignore）
├── data/                     # ⚠️ 运行时数据（数据库 + 密钥，已 gitignore，勿入库）
│   ├── secret.key            # 自动生成的 AES 密钥
│   └── kb_admin.db           # SQLite 用户 / 权限库
├── config/
│   └── .env.example          # 配置模板（敏感字段留空/占位）
├── requirements.txt
└── .gitignore
```

> **安全说明**：`data/`（数据库、加密密钥）、真实 `.env`/`.env.bak`、`node_modules`、索引缓存等已在 `.gitignore` 中排除，不会进入版本库。请勿将含明文凭证的文件提交到 Git。

---

## 快速开始（本地）

### 环境要求
- Python 3.9+（开发环境路径：`D:\python\python.exe`）
- 依赖见 `requirements.txt`（Flask、numpy、jieba、pdfplumber、python-docx 等）

### 1. 安装依赖
```powershell
cd e:/tedri_project/2026-06-08-task-1/kb_deploy
pip install -r requirements.txt
```

### 2. 启动服务
```powershell
# 方式一：直接运行（前台，调试用）
python scripts/serve.py

# 方式二：后台隐藏运行（生产常用）
Start-Process -FilePath "D:\python\python.exe" `
  -ArgumentList "scripts/serve.py" `
  -WorkingDirectory "e:/tedri_project/2026-06-08-task-1/kb_deploy" `
  -WindowStyle Hidden
```

启动后访问：
- **管理门户**：http://127.0.0.1:8080/
- **3D 知识图谱**：http://127.0.0.1:8080/graph
- **检索 API**：http://127.0.0.1:8080/api/query?q=关键词
- **健康检查**：http://127.0.0.1:8080/api/health

默认管理员账号：**`admin` / `Admin@123`**

### 3. 停止服务
```powershell
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { $_.CommandLine -match 'serve.py' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
```

---

## 索引与检索

系统启动时 `serve.py` 会**惰性加载**已有的 `bm25_index.pkl` 与 `vec_index.pkl`。

### 首次构建 / 增量重建索引
```powershell
cd e:/tedri_project/2026-06-08-task-1/kb_deploy/app
python rag_build_index.py      # 重建 BM25 索引
```

向量索引由文档上传/变更时自动触发（`kb_store.py` 调用 `vec_store.build_index()`）；也可在代码中手动 `vec_store.get_index().build_index()` 强制重建。

> 索引目录 `knowledge_base/bm25_index/` 与 `knowledge_base/vec_index/` 已被 `.gitignore` 排除，部署到新环境后需重新构建（详见下文部署脚本）。

---

## REST API 速览

| 方法 | 路径 | 说明 |
|------|------|------|
| GET  | `/api/health` | 健康检查（返回 `bm25_ready` / `vec_ready` / 文档数） |
| POST | `/api/auth/login` | 登录（body: `{"username","password"}`） |
| GET  | `/api/query?q=关键词` | 全文 + 向量融合检索 |
| POST | `/api/doc/upload` | 文档上传解析入库 |
| GET  | `/api/graph` | 知识图谱数据 |

前端 `web_vue/` 通过同源 `/api/*` 调用，无需单独配置跨域。

---

## 部署到内网机（Linux）

`scripts/install.sh` 用于在一台内网 Linux 机器（Ubuntu 24.04，IP 段如 `192.168.30.x`）上部署为常驻服务：

- 通过 SSH 将本机文件同步到目标机的 `/opt/<系统名>/`（脚本使用 `sshpass` + `rsync`）。
- 远端生成 `systemd` 服务（`KB_API_PORT=8080`），开机自启。
- 首次部署会自动 `pip install -r requirements.txt` 并构建索引。

关键环境变量（在 `install.sh` 或目标机 `.env` 中配置）：
- `SSH_HOST` / `SSH_USER` / `SSH_PASSWORD`：目标机 SSH 凭据
- `KB_API_PORT`：服务端口（默认 8080）
- **注意**：`install.sh` 中的 `SSH_PASSWORD` 等敏感字段切勿提交到 Git（本仓库已移除含明文密码的 `.env.example`）。

---

## 开发说明

- **前端**：`web_vue/` 为 Vue 3 + Vite 工程。修改前端后需 `npm run build` 生成 `dist/`，`serve.py` 才会托管更新后的页面。
- **后端**：Flask 单进程，`scripts/serve.py` 为入口；所有业务模块在 `app/`。
- **数据持久化**：用户/权限在 `data/kb_admin.db`（SQLite）；文档原文在 `knowledge_base/uploads/`；索引在 `knowledge_base/*_index/`。

---

## 常见问题

**Q：访问页面空白 / 接口 500？**
A：确认 `web_vue/dist/` 已构建存在；检查 `data/secret.key` 与 `data/kb_admin.db` 是否已生成（首次启动自动创建）。

**Q：检索无结果 / 图谱为空？**
A：首次部署需构建索引（见「索引与检索」）。`/api/health` 应返回 `bm25_ready: true`、`vec_ready: true`。

**Q：忘记管理员密码？**
A：删除 `data/kb_admin.db` 重启服务，将重新初始化默认管理员 `admin / Admin@123`（会清空已有用户，谨慎操作）。

**Q：多个 serve.py 进程冲突？**
A：停止所有匹配 `serve.py` 的 python 进程后再启动单个实例（见「停止服务」）。
