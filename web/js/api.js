/* 全局 API 封装：统一处理会话与错误 */
(function (global) {
  async function req(method, url, body, isForm) {
    const opts = { method: method, credentials: "same-origin" };
    if (body !== undefined) {
      if (isForm) {
        opts.body = body;
      } else {
        opts.headers = { "Content-Type": "application/json" };
        opts.body = JSON.stringify(body);
      }
    }
    const resp = await fetch(url, opts);
    let data = null;
    try { data = await resp.json(); } catch (e) { data = {}; }
    if (!resp.ok) {
      const err = new Error(data.error || ("请求失败(" + resp.status + ")"));
      err.status = resp.status;
      throw err;
    }
    return data;
  }

  const API = {
    get: (u) => req("GET", u),
    post: (u, b) => req("POST", u, b),
    put: (u, b) => req("PUT", u, b),
    del: (u, b) => req("DELETE", u, b),
    postForm: (u, form) => req("POST", u, form, true),

    login: (username, password) => API.post("/api/auth/login", { username, password }),
    logout: () => API.post("/api/auth/logout"),
    me: () => API.get("/api/auth/me"),

    categories: () => API.get("/api/kb/categories"),
    documents: (params) => {
      const q = new URLSearchParams();
      Object.entries(params || {}).forEach(([k, v]) => {
        if (v !== undefined && v !== null && v !== "") q.set(k, v);
      });
      return API.get("/api/kb/documents?" + q.toString());
    },
    document: (docId) => API.get("/api/kb/document?doc_id=" + encodeURIComponent(docId)),
    search: (q, topK) => API.get("/api/kb/search?q=" + encodeURIComponent(q) + "&top_k=" + (topK || 5)),

    createCategory: (b) => API.post("/api/kb/category", b),
    updateCategory: (id, b) => API.put("/api/kb/category/" + id, b),
    deleteCategory: (id) => API.del("/api/kb/category/" + id),
    upload: (file, category) => {
      const fd = new FormData();
      fd.append("file", file);
      fd.append("category", category);
      return API.postForm("/api/kb/upload", fd);
    },
    deleteDocument: (id) => API.del("/api/kb/document/" + id),

    permissions: () => API.get("/api/admin/permissions"),
    roles: () => API.get("/api/admin/roles"),
    createRole: (b) => API.post("/api/admin/roles", b),
    updateRole: (id, b) => API.put("/api/admin/roles/" + id, b),
    deleteRole: (id) => API.del("/api/admin/roles/" + id),
    users: () => API.get("/api/admin/users"),
    createUser: (b) => API.post("/api/admin/users", b),
    updateUser: (id, b) => API.put("/api/admin/users/" + id, b),
    deleteUser: (id) => API.del("/api/admin/users/" + id),
    features: () => API.get("/api/admin/features"),
    setFeature: (key, enabled) => API.put("/api/admin/features/" + key, { enabled: enabled ? 1 : 0 }),
    reindex: () => API.post("/api/admin/reindex"),
    stats: () => API.get("/api/admin/stats"),
  };

  global.KBAPI = API;
})(window);
