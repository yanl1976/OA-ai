// 全局 API 封装：统一处理会话(cookie)与错误。前端只调相对路径 /api/*，
// 开发(dev server 代理)与生产(Flask 同源托管)两种环境都适用。
async function req(method, url, body, isForm) {
  const opts = { method, credentials: "include" };
  if (body !== undefined) {
    if (isForm) {
      opts.body = body; // FormData 自带 contentType
    } else {
      opts.headers = { "Content-Type": "application/json" };
      opts.body = JSON.stringify(body);
    }
  }
  const resp = await fetch(url, opts);
  let data = null;
  try {
    data = await resp.json();
  } catch (e) {
    data = {};
  }
  if (!resp.ok) {
    const err = new Error(data.error || `请求失败(${resp.status})`);
    err.status = resp.status;
    // 保留完整响应体：上传分类校验冲突(409)时，前端需要读取 conflicts 清单
    // 以便弹窗请用户确认，仅有 message 不足以支撑交互。
    err.data = data;
    throw err;
  }
  return data;
}

export const api = {
  get: (u) => req("GET", u),
  post: (u, b) => req("POST", u, b),
  put: (u, b) => req("PUT", u, b),
  del: (u, b) => req("DELETE", u, b),
  postForm: (u, form) => req("POST", u, form, true),

  login: (username, password) => api.post("/api/auth/login", { username, password }),
  logout: () => api.post("/api/auth/logout"),
  me: () => api.get("/api/auth/me"),

  categories: () => api.get("/api/kb/categories"),
  categoriesAll: () => api.get("/api/kb/categories_all"),
  documents: (params) => {
    const q = new URLSearchParams();
    Object.entries(params || {}).forEach(([k, v]) => {
      if (v !== undefined && v !== null && v !== "") q.set(k, v);
    });
    return api.get("/api/kb/documents?" + q.toString());
  },
  document: (docId) => api.get("/api/kb/document?doc_id=" + encodeURIComponent(docId)),
  search: (q, topK, category) => {
    let url = "/api/kb/search?q=" + encodeURIComponent(q) + "&top_k=" + (topK || 5);
    if (category) url += "&category=" + encodeURIComponent(category);
    return api.get(url);
  },
  accessibleCategories: () => api.get("/api/kb/accessible-categories"),

  createCategory: (b) => api.post("/api/kb/category", b),
  updateCategory: (id, b) => api.put("/api/kb/category/" + id, b),
  deleteCategory: (id) => api.del("/api/kb/category/" + id),
  upload: (files, category, confirmCategory) => {
    const fd = new FormData();
    // 支持批量：files 为 File 数组，统一以 files[] 提交；兼容单个 File
    const arr = Array.isArray(files) ? files : [files];
    arr.forEach((f) => fd.append("files", f));
    fd.append("category", category);
    // confirm_category=1：用户已确认「仍按所选分类上传」，后端不再拦截
    if (confirmCategory) fd.append("confirm_category", "1");
    return api.postForm("/api/kb/upload", fd);
  },
  uploadZip: (file, parent, confirmCategory, conflictPolicy) => {
    const fd = new FormData();
    fd.append("file", file);
    if (parent) fd.append("parent", parent);
    if (confirmCategory) fd.append("confirm_category", "1");
    // 冲突文件处置策略：skip(跳过) / suggest(改用建议分类) / keep(仍按原分类)
    if (conflictPolicy) fd.append("conflict_policy", conflictPolicy);
    return api.postForm("/api/kb/upload-zip", fd);
  },
  deleteDocument: (id) => api.del("/api/kb/document/" + id),
  // 原版文档 PDF 预览（inline=true 内联，供 <iframe> 预览）
  docPdfUrl: (docId, inline) =>
    "/api/kb/document/" + encodeURIComponent(docId) + "/pdf" + (inline ? "?inline=1" : ""),
  // 上传文件管理
  uploads: (params) => {
    const q = new URLSearchParams();
    Object.entries(params || {}).forEach(([k, v]) => {
      if (v !== undefined && v !== null && v !== "") q.set(k, v);
    });
    return api.get("/api/kb/uploads?" + q.toString());
  },
  reclassifyDocument: (id, category) =>
    api.put("/api/kb/document/" + id, { category }),
  deleteUpload: (id) => api.del("/api/kb/uploads/" + id),
  deleteUploadsBatch: (docIds) => api.del("/api/kb/uploads/batch", { doc_ids: docIds }),
  batchReclassify: (docIds, category) =>
    api.post("/api/kb/uploads/batch/reclassify", { doc_ids: docIds, category }),
  // 上传后查询后台识别进度（轮询），ids 为逗号分隔的 doc_id 列表
  uploadStatus: (ids) => api.get("/api/kb/upload-status?ids=" + encodeURIComponent(ids.join(","))),
  // 回收站
  listTrash: (params = {}) => {
    const q = new URLSearchParams();
    Object.entries(params || {}).forEach(([k, v]) => {
      if (v !== undefined && v !== null && v !== "") q.set(k, v);
    });
    return api.get("/api/kb/trash?" + q.toString());
  },
  restoreUpload: (docId) => api.post("/api/kb/trash/" + docId),
  purgeUpload: (docId) => api.del("/api/kb/trash/" + docId),
  purgeUploadsBatch: (docIds) => api.del("/api/kb/trash/batch", { doc_ids: docIds }),
  // 系统初始化（清除文档 / 提取内容 / 重建索引）
  initClear: (includeTrash) =>
    api.post("/api/admin/init/clear", { include_trash: includeTrash !== false }),
  initExtract: () => api.post("/api/admin/init/extract"),
  initExtractOne: (docId) => api.post("/api/admin/init/extract-one", { doc_id: docId }),
  initClearExtract: () => api.post("/api/admin/init/clear-extract"),
  initAbort: () => api.post("/api/admin/init/abort"),
  initIndex: () => api.post("/api/admin/init/index"),
  // 标签
  listTags: () => api.get("/api/kb/tags"),
  setDocTags: (docId, tags) => api.put("/api/kb/document/" + docId + "/tags", { tags }),
  docsByTag: (tag, params = {}) => {
    const q = new URLSearchParams();
    Object.entries(params || {}).forEach(([k, v]) => {
      if (v !== undefined && v !== null && v !== "") q.set(k, v);
    });
    return api.get("/api/kb/tag/" + encodeURIComponent(tag) + "/documents?" + q.toString());
  },
  // 文档在线编辑
  updateDocText: (docId, text) => api.put("/api/kb/document/" + docId + "/text", { text }),
  // 门户概览
  kbOverview: () => api.get("/api/kb/overview"),

  permissions: () => api.get("/api/admin/permissions"),
  roles: () => api.get("/api/admin/roles"),
  createRole: (b) => api.post("/api/admin/roles", b),
  updateRole: (id, b) => api.put("/api/admin/roles/" + id, b),
  deleteRole: (id) => api.del("/api/admin/roles/" + id),
  users: () => api.get("/api/admin/users"),
  createUser: (b) => api.post("/api/admin/users", b),
  updateUser: (id, b) => api.put("/api/admin/users/" + id, b),
  deleteUser: (id) => api.del("/api/admin/users/" + id),
  features: () => api.get("/api/admin/features"),
  setFeature: (key, enabled) =>
    api.put("/api/admin/features/" + key, { enabled: enabled ? 1 : 0 }),
  reindex: () => api.post("/api/admin/reindex"),
  stats: () => api.get("/api/admin/stats"),
  vectorStats: () => api.get("/api/admin/vector_stats"),
  health: () => api.get("/api/health"),
  systemInfo: () => api.get("/api/system/info"),
  setSystemName: (name, copyright) => api.put("/api/system/info", { system_name: name, copyright: copyright || "" }),

  // 会议纪要二次生成（衍生版本）
  derivedList: (sourceDocId) =>
    api.get("/api/derived/list" + (sourceDocId ? "?source_doc_id=" + encodeURIComponent(sourceDocId) : "")),
  derivedGet: (id) => api.get("/api/derived/" + id),
  derivedCreate: (b) => api.post("/api/derived", b),
  derivedUpdate: (id, b) => api.put("/api/derived/" + id, b),
  derivedDelete: (id) => api.del("/api/derived/" + id),
  // 将会议纪要正文按标准模板解析为结构化字段（议题级截取用）
  derivedParse: (text) => api.post("/api/derived/parse", { text }),
  // 生成并下载二次生成纪要的 PDF（同源，浏览器会话 cookie 自动携带）
  derivedPdfUrl: (id) => "/api/derived/" + id + "/pdf",
  // 衍生版本 PDF 内联预览（供 <iframe>）
  derivedPdfPreviewUrl: (id) => "/api/derived/" + id + "/pdf-preview",
  // 预览/下载该衍生版本所关联的「原版 PDF」（二次生成前的来源文件）
  derivedSourcePdfUrl: (id, inline) =>
    "/api/derived/" + id + "/source-pdf" + (inline ? "?inline=1" : ""),
  // 衍生版本父子血缘（来源纪要 + 祖先链 + 下游子版本）
  derivedLineage: (id) => api.get("/api/derived/" + id + "/lineage"),
  // 允许二次生成的分类集合（id+name，后端按 id 规则动态计算，改名自动同步）
  derivedAllowedCategories: () => api.get("/api/derived/allowed-categories"),

  // ============ 云之家审批单据拉取任务 ============
  yzjTemplates: () => api.get("/api/yzj/templates"),
  yzjTasks: () => api.get("/api/yzj/tasks"),
  yzjTaskCreate: (task) => api.post("/api/yzj/tasks", { task }),
  yzjTaskUpdate: (id, task) => api.put("/api/yzj/tasks/" + id, { task }),
  yzjTaskDelete: (id) => api.del("/api/yzj/tasks/" + id),
  yzjTaskRun: (id, opts) => api.post("/api/yzj/run/" + id, opts || {}),
  yzjTaskAbort: (id) => api.post("/api/yzj/abort/" + id),
  yzjTaskStatus: (id) => api.get("/api/yzj/status/" + id),
  yzjPulledDocs: (params) => {
    const q = new URLSearchParams();
    Object.entries(params || {}).forEach(([k, v]) => {
      if (v !== undefined && v !== null && v !== "") q.set(k, v);
    });
    return api.get("/api/yzj/pulled-docs?" + q.toString());
  },

  // ============ 对话式智能问答 ============
  chatScope: () => api.get("/api/kb/chat/scope"),
  chatSessions: () => api.get("/api/kb/chat/sessions"),
  chatCreateSession: (title) => api.post("/api/kb/chat/sessions", { title }),
  chatSessionMessages: (sid) => api.get("/api/kb/chat/session/" + sid),
  chatDeleteSession: (sid) => api.del("/api/kb/chat/session/" + sid),
  chatRenameSession: (sid, title) => api.post("/api/kb/chat/session/" + sid + "/rename", { title }),
  chatSend: (sessionId, question, topK, category) =>
    api.post("/api/kb/chat", {
      session_id: sessionId || undefined,
      question,
      top_k: topK,
      category: category || undefined,
    }),
};
