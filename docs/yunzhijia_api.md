# 云之家开放平台接口参考（实测整理）

> 本文基于本系统对接云之家（金蝶云之家开放平台）的**实测结果**整理，
> 记录各接口的请求/响应结构与**关键字段含义**，用于排查拉取异常。
>
> 代码位置：`app/yunzhijia_client.py`（API 封装）、`app/yzj_pull.py`（拉取引擎）
>
> 最后更新：2026-08-30

---

## 一、鉴权

### 1.1 获取 accessToken

```
POST {API_BASE}/gateway/oauth2/token/getAccessToken
Content-Type: application/json
```

请求体：

| 字段 | 说明 |
|---|---|
| `appId` | `YUNZHIJIA_APP_ID` |
| `secret` | 按 scope 取不同密钥（见下） |
| `eid` | 企业 ID（`YUNZHIJIA_EID`，缺省复用 `YUNZHIJIA_ECP_ID`） |
| `timestamp` | **毫秒**时间戳 |
| `scope` | `team` / `app` / `resGroupSecret` |

响应：`data.accessToken`（有效期 `expireIn`，默认 7200s）、`data.refreshToken`

**三种 scope 的区别（重要）**：

| scope | secret 来源 | 用途 |
|---|---|---|
| `team` | `YUNZHIJIA_APP_SECRET` | 智能审批 cloudflow 接口（拉模板/流程/表单） |
| `app` | `YUNZHIJIA_APP_SECRET` | 普通轻应用级（本系统未用） |
| `resGroupSecret` | **`YUNZHIJIA_RESGROUP_SECRET`** | **文件下载**（docrest） |

> 坑：文件下载用的是**独立的** `resGroupSecret` 密钥，不是 `APP_SECRET`。
> 缺 `YUNZHIJIA_RESGROUP_SECRET` 会直接抛「无法下载附件」。

---

## 二、智能审批 cloudflow 接口

网关前缀：

```
{API_BASE}/gateway/workflow/form/thirdpart
```

统一调用方式：**POST**，`accessToken` 拼在 **QueryString** 上。

```
POST {_CLOUDFLOW_BASE}/{path}?accessToken={tok}
```

### 2.1 响应结构不统一（重要坑）

```python
# _cloudflow_post 的处理逻辑
if isinstance(data, list):
    return data                    # getTemplates 直接返回数组
if not data.get("success"):
    raise RuntimeError(...)
return data.get("data", {})        # 其余接口返回 {success, data, ...}
```

| 接口 | 返回类型 |
|---|---|
| `getTemplates` | **直接是 list**（不是 dict！） |
| `findFlows` / `viewFormInst` / `getRedHeadFile` | `{success, data, ...}` → 取 `data` |

> 坑：曾把 `get_templates()` 当 dict 用 `.get()`，恒为 `None`，导致所有单据被跳过。

---

### 2.2 getTemplates — 获取审批模板

请求体：`{}`（空）

原始响应是一个**分类数组**，本系统做了扁平化：

```json
[
  {
    "name": "分类名",
    "formTemplates": [
      { "codeId": "8dd241155e2b4447a7f557123f08cd8d", "title": "总经理会议纪要线上审批", ... }
    ]
  }
]
```

扁平化后（`get_templates()` 返回）：

```python
[{"formCodeId": ..., "title": ..., "category": ..., "available": ..., "state": ...}, ...]
```

**实测本系统匹配的模板**（关键词「会议纪要」）：

| 模板名 | formCodeId | FINISH 单据数 |
|---|---|---|
| 总经理会议纪要线上审批 | `8dd241155e2b4447a7f557123f08cd8d` | 7 |
| 会议纪要线上审批发布 | `1a26743676364eeea44ee7b60f269187` | 154 |
| 会议纪要线上审批发布-test | `0458bbdd8b904471b199c8c4f2e0a047` | 0 |

> **坑**：早期用 `next(...)` 只取第一个命中模板 → 只拉到 7 条。
> 现改为收集**所有**命中模板。

---

### 2.3 findFlows — 查询流程实例列表

请求体：

| 字段 | 类型 | 说明 |
|---|---|---|
| `pageNumber` | int | 页码，从 1 开始 |
| `pageSize` | int | 每页条数（默认 50） |
| `formCodeIds` | list | 模板 codeId 数组 |
| `status` | list | `FINISH`（已完成）/ `RUNNING`（进行中） |
| `title` | str | 标题关键词搜索（可选） |
| `createTime` | list | `[开始毫秒, 结束毫秒]`（可选） |

响应：

```json
{ "total": 154, "list": [ { ... }, ... ] }
```

`list` 元素关键字段：

| 字段 | 说明 |
|---|---|
| `formInstId` | **表单实例 ID**（后续 `viewFormInst` 用它） |
| `flowInstId` | 流程实例 ID |
| `formCodeId` | 所属模板 |
| `title` | 单据标题 |
| `serialNo` | 流水号（如 `HYJYXSSPFB-20240514-001`） |
| `status` | 状态 |
| `createTime` / `finishTime` | 毫秒时间戳 |

> **坑 1**：返回的 `total` 是**真实总数**。实测 `pageSize` 传 10/100/200，`total` 恒为 7（该模板确实只有 7 条），说明不是分页截断。
>
> **坑 2**：**必须自己翻页**。只调一次 `findFlows` 会漏掉第 N 页之后的全部单据。
> 现实现：逐模板翻页，直到本页不足 `pageSize` 或 `len(flows) >= total`。
>
> **坑 3**：`formCodeIds` 传多个模板时组合过滤**不可靠**，改为**逐模板**拉取后合并。

---

### 2.4 viewFormInst — 读取表单详情

请求体：

```json
{ "formInstId": "...", "formCodeId": "..." }
```

响应结构：

```
data.formInfo.widgetMap   ← 控件字典（核心）
data.formInfo.detailMap
```

`widgetMap` 形如：

```json
{
  "Da_0":     { "value": 1753680000000 },        // 会议/业务日期（毫秒）
  "Ta_0":     { "value": "正文文本" },            // 多行文本
  "Ps_0":     { "value": [ {...} ] },             // 普通附件（list）
  "Kg_0":     { "value": { ... } },               // 金山在线文档（dict）
  "_S_APPLY": { "value": ["申请人ID"] },
  "_S_DEPT":  { "value": ["部门UUID"] },
  "_S_DATE":  { "value": 1753680000000 }          // 提交时间（毫秒）
}
```

> 注：**跨模板查询可行**。实测用 A 模板的 `formCodeId` 也能查到属于 B 模板的单据（5/5 成功），
> 但为稳妥起见，代码仍逐模板处理。

---

## 三、附件控件（最关键部分）

云之家存在**两种**附件载体，结构完全不同。**只支持其一会漏掉整批单据。**

### 3.1 类型一：普通附件控件 `Ps_0` / `Od_0`

`value` 是 **list**，每个元素是一个文件对象：

```json
{
  "Ps_0": {
    "value": [
      {
        "sealedFileId": "671efd06b27a2300011c848b",   // 盖章版 PDF
        "sealedFileName": "xxx.pdf",
        "wpsFileId": "671efcf793dcf50001875b80",      // 原文件（docx/doc）
        "wpsFileName": "国产工程设计软件应用专题 会议纪要.docx",
        "redFileId": "..."                            // 红头文件实例 id
      }
    ]
  }
}
```

| 字段 | 含义 | 下载得到 |
|---|---|---|
| `sealedFileId` | **盖章版 PDF** | PDF 字节 |
| `wpsFileId` | 原文件 | docx/doc 字节 |
| `redFileId` | 红头文件实例 id | 需先调 `getRedHeadFile` 解析 |

**典型**：2025 / 2026 年度单据

### 3.2 类型二：金山在线文档控件 `Kg_0`

`value` 是 **dict**（不是 list！）：

```json
{
  "Kg_0": {
    "value": {
      "fileName": "国产工程设计软件应用专题 会议纪要.docx",
      "fileId": "671efcf793dcf50001875b80",              // 原文件（docx/doc）
      "pdfFileId": "671efd06b27a2300011c848b",            // 盖章版 PDF
      "pdfFileSize": "195137",
      "ofdFileSize": "0",
      "kingGridWidgetFileId": "671623236867b50001ef107d"
    }
  }
}
```

| 字段 | 含义 |
|---|---|
| `pdfFileId` | **盖章版 PDF**（可能为空！） |
| `fileId` | 原文件 docx/doc |
| `kingGridWidgetFileId` | 控件级文件 id |
| `ofdFileSize` / `pdfFileSize` | 文件大小（字符串） |

**典型**：**2024 年度单据**

> **最大坑**：旧代码只扫 `value` 为 list 的控件，`Kg_0`（dict）被整体忽略 →
> 2024 年度 47 条单据被误判为「无附件」而永久跳过。

### 3.3 实测统计（2024 年度 54 条）

| 情况 | 条数 | 处理 |
|---|---|---|
| 有 `pdfFileId`（可下盖章 PDF） | 48 | 下载 PDF，命名 `.pdf` |
| 无 `pdfFileId`（只能下原文件） | 6 | 下载原文件，命名 `.docx` |

---

## 四、文件下载

```
GET {API_BASE}/docrest/doc/user/downloadfile
Header: x-accessToken: {resGroupSecret token}
Params: bizkey=cloudflow & fileId={file_id}
```

**注意**：用的是 `resGroupSecret` scope 的 token，且放在 **`x-accessToken` 头**（不是 `accessToken`）。

### 下载策略（本系统最终定稿）

```
优先 盖章版 PDF   → 校验 %PDF 头 → 命名 .pdf
  └─ 无 PDF / 下载失败 / 头校验不通过
       ↓
     回退 原文件 docx/doc → 命名 .docx / .doc（绝不改名成 .pdf）
```

> **教训**：早期版本把所有下载内容一律命名 `.pdf`，导致 docx 内容被 PDF 解析器处理，
> 实测少提取约 3% 文本，且名实不符。现严格按**实际下载类型**命名。

---

## 五、完整调用链路

```
1. get_access_token(scope="team")
        ↓
2. get_templates()                      → 匹配所有含关键词的 formCodeId
        ↓
3. 逐模板 findFlows(pageNumber=1..N)     → 翻完全部页，合并 flows
        ↓
4. 逐条 viewFormInst(formInstId, formCodeId)
        ↓
5. _collect_attachment_files(widgetMap)  → 同时支持 Ps_0(list) 与 Kg_0(dict)
        ↓
6. get_access_token(scope="resGroupSecret")
        ↓
7. download_file(file_id)                → 优先 PDF，失败回退原文件
        ↓
8. extract_text.extract(raw, filename)   → 按扩展名路由解析
        ↓
9. save_upload_raw() → update_upload_text_async()
```

---

## 六、时间字段

| 字段 | 位置 | 说明 | 用于 |
|---|---|---|---|
| `Da_0` | widgetMap | **会议/业务日期**（毫秒） | 文件名重名时的日期前缀（首选） |
| `_S_DATE` | widgetMap | 提交时间（毫秒） | 日期前缀（次选） |
| `createTime` / `finishTime` | findFlows 返回 | 创建/完成时间（毫秒） | 日期前缀（兜底） |

> 注意：云之家时间戳是**毫秒**（13 位），判断 `ts > 1e12` 后需 `/1000`。
>
> `Da_0` 语义最准：实测每条例会各不相同（`20260824` / `20260817` / `20260803` ...），
> 即使附件名完全相同也能区分。

---

## 七、常见排查命令

```bash
# 1) 测试鉴权与模板命中（自带诊断输出）
cd /opt/OA-ai && /opt/OA-ai/venv/bin/python app/yunzhijia_client.py

# 2) 查看去重记录状态
python3 -c "
import json, collections
d = json.load(open('/opt/OA-ai/config/.yzj_pull_synced.json', encoding='utf-8'))
c = collections.Counter()
for k, v in d.items():
    if v.get('note') == 'no-attachment': c['no-attachment(ver=%s)' % v.get('ver')] += 1
    elif v.get('doc_ids'): c['已落盘'] += 1
print(len(d), dict(c))
"

# 3) 查看实时运行日志
sudo journalctl -u kb.service --since today --no-pager | grep -iE 'yzj|recheck|error' | tail -40
```

### 去重记录字段说明

```json
{
  "<formInstId>": {
    "ts": 1756530000,           // 处理时间
    "files": 1,                 // 附件数
    "doc_ids": ["up_xxx"],      // 落盘文档 id（成功时）
    "note": "no-attachment",    // 无附件标记（可选）
    "ver": 2                    // 自愈校验版本：无此字段=旧记录需重校验
  }
}
```

| 状态 | 含义 |
|---|---|
| 有 `doc_ids` | 已落盘，下次跳过（校验文档是否仍存活） |
| `note=no-attachment` 且**无** `ver` | 旧版误判记录，**下次自愈重校验一次** |
| `note=no-attachment` 且 `ver=2` | 已用新逻辑校验确无附件，**永久跳过** |
| `dry: true` | 试跑记录，未落盘，正式拉取时仍会处理 |

---

## 八、环境配置（.env）

| 变量 | 说明 |
|---|---|
| `YUNZHIJIA_APP_ID` | 应用 ID |
| `YUNZHIJIA_APP_SECRET` | 应用密钥（team/app scope） |
| `YUNZHIJIA_ECP_ID` | 企业 ID |
| `YUNZHIJIA_EID` | 审批 team ID（缺省复用 ECP_ID） |
| `YUNZHIJIA_RESGROUP_SECRET` | **文件服务密钥**（下载必需，易遗漏） |
| `YUNZHIJIA_API_BASE` | 默认 `https://www.yunzhijia.com` |

> 生产机 venv 需安装 `requests`、`python-dotenv`。
> 若报 `ModuleNotFoundError: No module named 'requests'`：
> ```bash
> /opt/OA-ai/venv/bin/pip install requests
> ```
