/* 知识库管理门户 — 主逻辑（原生 JS，零依赖） */
const KB = {
  user: null, perms: [], cats: [], curCat: null, docPage: 1, kw: "",
  searchMode: false, expanded: new Set(), catCollapsed: new Set(),
  _catInit: false, _catMgmtInit: false,
};

const $ = (sel, root = document) => root.querySelector(sel);
const elFromHTML = (html) => {
  const t = document.createElement("template");
  t.innerHTML = html.trim();
  return t.content.firstElementChild;
};
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function toast(msg) {
  let t = $("#toast");
  if (!t) { t = elFromHTML('<div class="toast" id="toast"></div>'); document.body.appendChild(t); }
  t.textContent = msg; t.classList.add("show");
  clearTimeout(t._t); t._t = setTimeout(() => t.classList.remove("show"), 2200);
}

function hasPerm(p) { return KB.perms.includes(p); }

/* ---------------- 模态 ---------------- */
function openModal(title, bodyHtml, actionsHtml) {
  const mask = elFromHTML(`<div class="modal-mask show"><div class="modal">
    <h3>${esc(title)}</h3><div class="modal-body">${bodyHtml}</div>
    <div class="actions">${actionsHtml || '<button class="btn" data-close>关闭</button>'}</div>
  </div></div>`);
  mask.addEventListener("click", (e) => {
    if (e.target === mask || e.target.hasAttribute("data-close")) closeModal();
  });
  document.body.appendChild(mask);
  return mask;
}
function closeModal() { document.querySelectorAll(".modal-mask").forEach((m) => m.remove()); }

/* ---------------- 启动 ---------------- */
async function boot() {
  try {
    const { user } = await KBAPI.me();
    if (user) { KB.user = user; KB.perms = user.permissions || []; return renderApp(); }
  } catch (e) { /* 未登录 */ }
  renderLogin();
}

/* ---------------- 登录 ---------------- */
function renderLogin() {
  document.getElementById("root").innerHTML = `
  <div class="login-wrap">
    <div class="login-card">
      <h1>OA 知识库管理台</h1>
      <p class="sub">本地知识库 · 分类管理与系统控制台</p>
      <label>用户名</label>
      <input id="lu" autocomplete="username" placeholder="admin" />
      <label>密码</label>
      <input id="lp" type="password" autocomplete="current-password" placeholder="请输入密码" />
      <div id="lerr" class="alert err"></div>
      <button class="btn block" id="lbtn">登 录</button>
      <p class="muted" style="margin-top:16px;font-size:12px;text-align:center">
        默认管理员 admin / Admin@123，登录后请尽快修改密码</p>
    </div>
  </div>`;
  const doLogin = async () => {
    const u = $("#lu").value.trim(), p = $("#lp").value;
    $("#lerr").className = "alert err"; $("#lerr").textContent = "";
    try {
      const r = await KBAPI.login(u, p);
      KB.user = r.user; KB.perms = r.user.permissions || [];
      renderApp();
    } catch (e) { $("#lerr").textContent = e.message; $("#lerr").className = "alert err"; }
  };
  $("#lbtn").onclick = doLogin;
  $("#lp").addEventListener("keydown", (e) => { if (e.key === "Enter") doLogin(); });
  $("#lu").focus();
}

/* ---------------- 应用骨架 ---------------- */
function navItems() {
  const items = [];
  if (hasPerm("kb.view")) items.push({ id: "knowledge", ic: "📚", label: "知识浏览" });
  if (hasPerm("graph.view")) items.push({ id: "graph", ic: "🕸", label: "知识图谱" });
  if (hasPerm("system.manage") || hasPerm("user.view") || hasPerm("role.manage") || hasPerm("permission.view") || hasPerm("kb.category.manage")) {
    items.push({ group: "系统管理" });
    if (hasPerm("kb.category.manage")) items.push({ id: "categories", ic: "🗂", label: "分类管理" });
    if (hasPerm("user.view")) items.push({ id: "users", ic: "👤", label: "用户管理" });
    if (hasPerm("role.manage")) items.push({ id: "roles", ic: "🛡", label: "角色管理" });
    if (hasPerm("permission.view")) items.push({ id: "perms", ic: "🔑", label: "权限目录" });
    if (hasPerm("system.manage")) items.push({ id: "system", ic: "⚙", label: "功能与系统" });
  }
  return items;
}

function renderApp() {
  document.getElementById("root").innerHTML = `
  <div class="app">
    <div class="topbar">
      <div class="logo"><span class="dot"></span> OA 知识库</div>
      <div class="spacer"></div>
      <div class="user">你好，<b>${esc(KB.user.display_name || KB.user.username)}</b>
        <span class="badge role">${esc(KB.user.role_name || "无角色")}</span></div>
      <button class="btn ghost" id="logout" style="color:#fff;border-color:#334155">退出</button>
    </div>
    <div class="body">
      <div class="sidebar" id="sidebar"></div>
      <div class="main" id="main"></div>
    </div>
  </div>`;
  $("#logout").onclick = async () => { await KBAPI.logout(); KB.user = null; renderLogin(); };
  renderSidebar();
  navigate(hasPerm("kb.view") ? "knowledge" : "system");
}

function renderSidebar() {
  const sb = $("#sidebar");
  sb.innerHTML = "";
  let lastGroup = false;
  navItems().forEach((it) => {
    if (it.group) {
      sb.appendChild(elFromHTML(`<div class="nav-group">${esc(it.group)}</div>`));
      lastGroup = true; return;
    }
    const node = elFromHTML(`<div class="nav-item" data-view="${it.id}">
      <span class="ic">${it.ic}</span><span>${esc(it.label)}</span></div>`);
    node.onclick = () => navigate(it.id);
    sb.appendChild(node);
  });
}

function navigate(view) {
  document.querySelectorAll(".sidebar .nav-item").forEach((n) =>
    n.classList.toggle("active", n.dataset.view === view));
  const titles = { knowledge: "知识浏览", graph: "知识图谱", categories: "分类管理",
    users: "用户管理", roles: "角色管理", perms: "权限目录", system: "功能与系统" };
  $("#main").dataset.view = view;
  const render = { knowledge: viewKnowledge, graph: viewGraph, categories: viewCategories,
    users: viewUsers, roles: viewRoles, perms: viewPermissions, system: viewSystem }[view];
  if (render) render();
  $("#main").scrollTop = 0;
}

/* ---------------- 知识浏览 ---------------- */
async function viewKnowledge() {
  const main = $("#main");
  main.innerHTML = `<h2>知识浏览</h2>
    <div class="crumb">按分类检索与管理知识内容</div>
    <div class="kb">
      <div class="col-cats card pad" id="catCol"><div class="empty">加载分类…</div></div>
      <div class="col-list card pad" id="listCol">
        <div class="toolbar">
          <input class="search grow" id="docSearch" placeholder="检索关键词（标题/全文）" />
          <button class="btn" id="docSearchBtn">检索</button>
        </div>
        <div id="docList"><div class="empty">选择左侧分类查看文档</div></div>
        <div class="pager" id="docPager"></div>
      </div>
      <div class="col-view card pad" id="viewCol"><div class="empty">点击文档查看内容</div></div>
    </div>`;
  try {
    const { categories } = await KBAPI.categories();
    KB.cats = categories;
    if (!KB._catInit) {
      categories.filter((c) => !c.parent_id).forEach((c) => KB.expanded.add(c.name));
      KB._catInit = true;
    }
    renderCatTree(categories);
  } catch (e) { $("#catCol").innerHTML = `<div class="empty">${esc(e.message)}</div>`; }

  const doSearch = () => {
    const kw = $("#docSearch").value.trim();
    if (!kw) { KB.searchMode = false; loadDocs(); return; }
    KB.searchMode = true; KB.kw = kw; searchDocs(kw);
  };
  $("#docSearchBtn").onclick = doSearch;
  $("#docSearch").addEventListener("keydown", (e) => { if (e.key === "Enter") doSearch(); });
  loadDocs();
}

function renderCatTree(categories) {
  const col = $("#catCol");
  if (!col) return;
  const byParent = {};
  categories.forEach((c) => { (byParent[c.parent_id] = byParent[c.parent_id] || []).push(c); });
  const tops = byParent[null] || [];
  const allOpen = tops.length > 0 && tops.every((c) => KB.expanded.has(c.name));

  col.innerHTML = "";
  col.appendChild(elFromHTML(`<div class="cat-head">
      <span class="muted">全部分类</span>
      <button class="link-btn" id="catToggleAll">${allOpen ? "全部折叠" : "全部展开"}</button>
    </div>`));
  const allRow = elFromHTML(`<div class="cat-row top ${!KB.curCat ? "active" : ""}" data-cat="">
      <span class="twisty-spacer"></span><span class="cat-label">全部文档</span>
      <span class="cnt">${categories.filter((c) => !c.parent_id).reduce((a, c) => a + c.doc_count, 0)}</span></div>`);
  allRow.onclick = () => selectCat(null);
  col.appendChild(allRow);

  const rendered = new Set();
  const build = (parentEl, cs, depth) => {
    cs.forEach((c) => {
      const kids = byParent[c.id] || [];
      const hasKids = kids.length > 0;
      const open = KB.expanded.has(c.name);
      const li = elFromHTML(`<div class="cat-node ${hasKids ? "has-children" : ""} ${hasKids && !open ? "collapsed" : ""}"></div>`);
      const row = elFromHTML(`<div class="cat-row ${KB.curCat === c.name ? "active" : ""}" title="${esc(c.name)}">
        <span class="twisty ${hasKids ? "" : "empty"}">${hasKids ? (open ? "▾" : "▸") : ""}</span>
        <span class="cat-label">${esc(c.name)}</span><span class="cnt">${c.doc_count}</span></div>`);
      row.onclick = (e) => {
        if (e.target.classList.contains("twisty") && hasKids) {
          li.classList.toggle("collapsed");
          row.querySelector(".twisty").textContent = li.classList.contains("collapsed") ? "▸" : "▾";
          return;
        }
        selectCat(c.name);
      };
      li.appendChild(row);
      rendered.add(c.name);
      if (hasKids) {
        const ul = elFromHTML(`<div class="cat-children"></div>`);
        li.appendChild(ul);
        build(ul, kids, depth + 1);
      }
      parentEl.appendChild(li);
    });
  };
  const tree = elFromHTML(`<div class="cat-tree"></div>`);
  col.appendChild(tree);
  build(tree, tops, 0);
  // 兜底：上级缺失的孤立分类平铺到根
  categories.filter((c) => !rendered.has(c.name)).forEach((c) => {
    const li = elFromHTML(`<div class="cat-node"><div class="cat-row" data-cat="${esc(c.name)}" title="${esc(c.name)}">
      <span class="twisty empty"></span><span class="cat-label">${esc(c.name)}</span><span class="cnt">${c.doc_count}</span></div></div>`);
    li.querySelector(".cat-row").onclick = () => selectCat(c.name);
    tree.appendChild(li);
  });

  const ta = $("#catToggleAll");
  if (ta) ta.onclick = () => {
    if (allOpen) tops.forEach((c) => KB.expanded.delete(c.name));
    else tops.forEach((c) => KB.expanded.add(c.name));
    renderCatTree(categories);
  };
}

function selectCat(name) {
  KB.curCat = name; KB.searchMode = false; KB.docPage = 1;
  document.querySelectorAll("#catCol .cat-row").forEach((r) =>
    r.classList.toggle("active", (r.dataset.cat || null) === (name || "")));
  loadDocs();
}

async function loadDocs() {
  const list = $("#docList"); list.innerHTML = `<div class="empty">加载中…</div>`;
  try {
    const r = await KBAPI.documents({ category: KB.curCat, page: KB.docPage, page_size: 15 });
    renderDocList(r, false);
  } catch (e) { list.innerHTML = `<div class="empty">${esc(e.message)}</div>`; }
}

async function searchDocs(kw) {
  const list = $("#docList"); list.innerHTML = `<div class="empty">检索中…</div>`;
  try {
    const r = await KBAPI.search(kw, 20);
    renderDocList({ items: r.results.map((x) => ({
      doc_id: x.doc_id || x.filename, filename: x.filename, category: x.category,
      label: x.label, pages: x.pages, score: x.score, snippet: x.text })), total: r.count }, true);
  } catch (e) { list.innerHTML = `<div class="empty">${esc(e.message)}</div>`; }
}

function renderDocList(r, isSearch) {
  const list = $("#docList");
  if (!r.items.length) { list.innerHTML = `<div class="empty">暂无文档</div>`; $("#docPager").innerHTML = ""; return; }
  list.innerHTML = "";
  r.items.forEach((d) => {
    const node = elFromHTML(`<div class="doc-item" data-id="${esc(d.doc_id)}">
      <div class="t">${esc(d.filename)}</div>
      <div class="m">${esc(d.category || "")} · ${d.pages || "?"}页${d.score != null ? " · 相关度 " + d.score : ""}</div>
      ${isSearch && d.snippet ? `<div class="m" style="margin-top:6px;color:#475569;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden">${esc(d.snippet)}</div>` : ""}
    </div>`);
    node.onclick = () => openDoc(d.doc_id, node);
    list.appendChild(node);
  });
  const total = r.total || r.items.length;
  const pages = Math.max(1, Math.ceil(total / 15));
  $("#docPager").innerHTML = "";
  if (!isSearch && pages > 1) {
    const p = $("#docPager");
    const prev = elFromHTML(`<button class="btn sm ghost">上一页</button>`);
    const nxt = elFromHTML(`<button class="btn sm ghost">下一页</button>`);
    prev.disabled = KB.docPage <= 1; nxt.disabled = KB.docPage >= pages;
    prev.onclick = () => { KB.docPage--; loadDocs(); };
    nxt.onclick = () => { KB.docPage++; loadDocs(); };
    p.appendChild(prev); p.appendChild(elFromHTML(`<span class="muted">${KB.docPage}/${pages}</span>`));
    p.appendChild(nxt);
  }
}

async function openDoc(docId, node) {
  document.querySelectorAll("#docList .doc-item").forEach((n) => n.classList.remove("active"));
  if (node) node.classList.add("active");
  const view = $("#viewCol"); view.innerHTML = `<div class="empty">加载中…</div>`;
  try {
    const { document: doc } = await KBAPI.document(docId);
    const pages = Math.max(1, doc.pages || 1);
    const buckets = chunkByPages(doc.full_text || "(无正文)", pages);
    let pg = 1;
    const render = () => {
      view.innerHTML = `<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">
        <b style="font-size:15px">${esc(doc.filename)}</b>
        <span class="badge role">${esc(doc.category || "")}</span></div>
        <div class="muted" style="font-size:12px;margin-bottom:12px">来源：${doc.source === "upload" ? "用户上传" : "原始库"} · 共 ${pages} 页 · 第 ${pg} 页</div>
        <div class="doc-view">${esc(buckets[pg - 1] || "")}</div>
        <div class="pager">
          <button class="btn sm ghost" id="pgPrev" ${pg <= 1 ? "disabled" : ""}>上一页</button>
          <span class="muted">${pg}/${pages}</span>
          <button class="btn sm ghost" id="pgNext" ${pg >= pages ? "disabled" : ""}>下一页</button>
        </div>`;
      $("#pgPrev").onclick = () => { if (pg > 1) { pg--; render(); } };
      $("#pgNext").onclick = () => { if (pg < pages) { pg++; render(); } };
    };
    render();
  } catch (e) { view.innerHTML = `<div class="empty">${esc(e.message)}</div>`; }
}

function chunkByPages(text, pages) {
  const paras = text.split("\n").filter((p) => p.trim());
  if (!paras.length) return [text];
  const per = Math.ceil(paras.length / pages);
  const out = [];
  for (let i = 0; i < paras.length; i += per) out.push(paras.slice(i, i + per).join("\n"));
  return out;
}

/* ---------------- 知识图谱 ---------------- */
function viewGraph() {
  $("#main").innerHTML = `<h2>知识图谱</h2>
    <div class="crumb">3D 神经网络知识图谱（拖拽旋转 / 滚轮缩放）</div>
    <iframe class="graph" src="/graph"></iframe>`;
}

/* ---------------- 分类管理 ---------------- */
async function viewCategories() {
  const main = $("#main");
  main.innerHTML = `<h2>分类管理</h2>
    <div class="crumb">管理知识分类（新增 / 编辑 / 启停 / 删除）</div>
    <div class="toolbar">
      <div class="grow"></div>
      <button class="btn ghost" id="catToggleAll2">全部折叠</button>
      <button class="btn" id="addCat">+ 新增分类</button>
    </div>
    <div class="card pad"><table>
      <thead><tr><th>ID</th><th>分类名称</th><th>文档数</th><th>排序</th><th>状态</th><th>类型</th><th>上级</th><th>操作</th></tr></thead>
      <tbody id="catBody"><tr><td colspan="8" class="empty">加载中…</td></tr></tbody>
    </table></div>`;
  $("#addCat").onclick = () => catModal(null);
  await refreshCats();
}

async function refreshCats() {
  const body = $("#catBody");
  let counts = {};
  try { const { categories } = await KBAPI.categories(); KB.cats = categories; counts = Object.fromEntries(categories.map((c) => [c.name, c.doc_count])); } catch (e) {}
  const allCats = await fetchAllCategories();
  if (!KB._catMgmtInit) KB._catMgmtInit = true; // 默认全部展开（不在 catCollapsed 中）
  const byParent = {};
  allCats.forEach((c) => { (byParent[c.parent_id] = byParent[c.parent_id] || []).push(c); });
  const parentName = Object.fromEntries(allCats.map((c) => [c.id, c.name]));
  const topIds = (byParent[null] || []).map((c) => c.id);

  const rows = [];
  const makeRow = (c, depth) => {
    const kids = byParent[c.id] || [];
    const hasKids = kids.length > 0;
    const collapsed = KB.catCollapsed.has(c.id);
    const caret = hasKids ? `<span class="twisty ${collapsed ? "" : ""}" data-tw="${c.id}">${collapsed ? "▸" : "▾"}</span>` : `<span class="twisty empty"></span>`;
    const indent = 8;
    rows.push(`<tr data-id="${c.id}" data-parent="${c.parent_id || ""}">
      <td>${c.id}</td>
      <td style="padding-left:${indent}px"><span class="tree-name" title="${esc(c.name)}">${caret}<span class="tree-label">${esc(c.name)}</span></span></td>
      <td>${counts[c.name] || 0}</td>
      <td>${c.sort_order}</td>
      <td><span class="badge ${c.status ? "on" : "off"}">${c.status ? "启用" : "停用"}</span></td>
      <td>${c.builtin ? '<span class="badge builtin">内置</span>' : "自定义"}</td>
      <td class="muted">${c.parent_id ? esc(parentName[c.parent_id] || "—") : "（顶级）"}</td>
      <td>
        <button class="btn sm ghost" data-edit="${c.id}">编辑</button>
        <button class="btn sm ${c.status ? "ghost" : "ok"}" data-toggle="${c.id}">${c.status ? "停用" : "启用"}</button>
        ${c.builtin ? "" : `<button class="btn sm danger" data-del="${c.id}">删除</button>`}
      </td></tr>`);
    if (hasKids) kids.forEach((ch) => makeRow(ch, depth + 1));
  };
  (byParent[null] || []).forEach((r) => makeRow(r, 0));
  // 兜底：上级缺失或损坏的孤立分类平铺
  const renderedIds = new Set(rows.map((h) => { const m = h.match(/data-id="(\d+)"/); return m ? +m[1] : -1; }));
  allCats.filter((c) => !renderedIds.has(c.id)).forEach((c) => makeRow(c, 0));

  body.innerHTML = rows.join("") || `<tr><td colspan="8" class="empty">暂无分类</td></tr>`;
  applyCatCollapse();

  const tg = $("#catToggleAll2");
  if (tg) {
    const allCollapsed = topIds.length > 0 && topIds.every((id) => KB.catCollapsed.has(id));
    tg.textContent = allCollapsed ? "全部展开" : "全部折叠";
    tg.onclick = () => {
      if (allCollapsed) topIds.forEach((id) => KB.catCollapsed.delete(id));
      else topIds.forEach((id) => KB.catCollapsed.add(id));
      applyCatCollapse();
    };
  }

  body.querySelectorAll("[data-edit]").forEach((b) => b.onclick = () => {
    const c = allCats.find((x) => x.id == b.dataset.edit); catModal(c);
  });
  body.querySelectorAll("[data-toggle]").forEach((b) => b.onclick = async () => {
    const c = allCats.find((x) => x.id == b.dataset.toggle);
    await KBAPI.updateCategory(c.id, { status: c.status ? 0 : 1 });
    await refreshCats(); toast("已更新");
  });
  body.querySelectorAll("[data-del]").forEach((b) => b.onclick = async () => {
    if (!confirm("确认删除该分类？文档不会被删除，仅从分类树移除。")) return;
    try { await KBAPI.deleteCategory(b.dataset.del); await refreshCats(); toast("已删除"); }
    catch (e) { toast(e.message); }
  });
  body.querySelectorAll("[data-tw]").forEach((tw) => tw.onclick = () => {
    const id = +tw.dataset.tw;
    if (KB.catCollapsed.has(id)) KB.catCollapsed.delete(id); else KB.catCollapsed.add(id);
    applyCatCollapse();
  });
}

// 纯显示切换：根据祖先链是否被折叠，设置每一行的显示/隐藏，不重建 DOM
function applyCatCollapse() {
  const body = $("#catBody");
  if (!body) return;
  const rows = Array.from(body.querySelectorAll("tr[data-id]"));
  const byId = {};
  rows.forEach((r) => { byId[+r.dataset.id] = r; });
  rows.forEach((r) => {
    let p = r.dataset.parent ? +r.dataset.parent : null;
    let hidden = false;
    while (p) {
      if (KB.catCollapsed.has(p)) { hidden = true; break; }
      p = byId[p] ? (byId[p].dataset.parent ? +byId[p].dataset.parent : null) : null;
    }
    r.style.display = hidden ? "none" : "";
    const tw = r.querySelector("[data-tw]");
    if (tw) tw.textContent = KB.catCollapsed.has(+r.dataset.id) ? "▸" : "▾";
  });
  const tg = $("#catToggleAll2");
  if (tg) {
    const tops = rows.filter((r) => !r.dataset.parent);
    const allCollapsed = tops.length > 0 && tops.every((r) => KB.catCollapsed.has(+r.dataset.id));
    tg.textContent = allCollapsed ? "全部展开" : "全部折叠";
  }
}

async function fetchAllCategories() {
  // 管理接口需要全量（含停用）。扩展：直接读取 categories 接口的 only_enabled=false
  const r = await fetch("/api/kb/categories_all").then((x) => x.ok ? x.json() : null).catch(() => null);
  if (r && r.categories) return r.categories;
  return KB.cats;
}

function catModal(cat) {
  const isEdit = !!cat;
  const roots = (KB.cats || []).filter((c) => !c.parent_id && (!isEdit || c.id !== cat.id));
  const parentOpts = `<option value="">（顶级分类）</option>` + roots.map((c) =>
    `<option value="${c.id}" ${cat && cat.parent_id === c.id ? "selected" : ""}>${esc(c.name)}</option>`).join("");
  openModal(isEdit ? "编辑分类" : "新增分类", `
    <label>分类名称</label>
    <input id="catName" value="${esc(cat ? cat.name : "")}" ${isEdit && cat.builtin ? "disabled" : ""}/>
    <label>上级分类</label>
    <select id="catParent">${parentOpts}</select>
    <label>描述</label>
    <textarea id="catDesc">${esc(cat ? cat.description || "" : "")}</textarea>
    <label>排序（数字越小越靠前）</label>
    <input id="catOrder" type="number" value="${cat ? cat.sort_order : 0}"/>
  `, `<button class="btn ghost" data-close>取消</button>
      <button class="btn" id="catSave">保存</button>`);
  $("#catSave").onclick = async () => {
    const body = { name: $("#catName").value.trim(), description: $("#catDesc").value.trim(),
      sort_order: parseInt($("#catOrder").value || 0, 10),
      parent_id: $("#catParent").value || null };
    try {
      if (isEdit) { await KBAPI.updateCategory(cat.id, body); }
      else { await KBAPI.createCategory(body); }
      closeModal(); await refreshCats(); toast("已保存");
    } catch (e) { toast(e.message); }
  };
}

/* ---------------- 用户管理 ---------------- */
async function viewUsers() {
  const main = $("#main");
  main.innerHTML = `<h2>用户管理</h2>
    <div class="crumb">管理系统用户、角色与启用状态</div>
    <div class="toolbar"><div class="grow"></div><button class="btn" id="addUser">+ 新增用户</button></div>
    <div class="card pad"><table>
      <thead><tr><th>ID</th><th>用户名</th><th>显示名</th><th>角色</th><th>状态</th><th>最近登录</th><th>操作</th></tr></thead>
      <tbody id="userBody"><tr><td colspan="7" class="empty">加载中…</td></tr></tbody>
    </table></div>`;
  $("#addUser").onclick = () => userModal(null);
  await refreshUsers();
}

let _rolesCache = [];
async function refreshUsers() {
  const body = $("#userBody");
  let data;
  try { data = await KBAPI.users(); _rolesCache = data.roles; }
  catch (e) { body.innerHTML = `<tr><td colspan="7" class="empty">${esc(e.message)}</td></tr>`; return; }
  const roleMap = Object.fromEntries(data.roles.map((r) => [r.id, r.name]));
  body.innerHTML = "";
  data.users.forEach((u) => {
    const tr = elFromHTML(`<tr>
      <td>${u.id}</td><td>${esc(u.username)}</td><td>${esc(u.display_name || "")}</td>
      <td><span class="badge role">${esc(roleMap[u.role_id] || "未分配")}</span></td>
      <td><span class="badge ${u.status ? "on" : "off"}">${u.status ? "启用" : "停用"}</span></td>
      <td class="muted">${esc(u.last_login || "—")}</td>
      <td>
        <button class="btn sm ghost" data-edit="${u.id}">编辑</button>
        <button class="btn sm ${u.status ? "ghost" : "ok"}" data-toggle="${u.id}">${u.status ? "停用" : "启用"}</button>
        <button class="btn sm danger" data-del="${u.id}">删除</button>
      </td></tr>`);
    body.appendChild(tr);
  });
  body.querySelectorAll("[data-edit]").forEach((b) => b.onclick = () => {
    const u = data.users.find((x) => x.id == b.dataset.edit); userModal(u);
  });
  body.querySelectorAll("[data-toggle]").forEach((b) => b.onclick = async () => {
    const u = data.users.find((x) => x.id == b.dataset.toggle);
    await KBAPI.updateUser(u.id, { status: u.status ? 0 : 1 });
    await refreshUsers(); toast("已更新");
  });
  body.querySelectorAll("[data-del]").forEach((b) => b.onclick = async () => {
    if (!confirm("确认删除该用户？")) return;
    try { await KBAPI.deleteUser(b.dataset.del); await refreshUsers(); toast("已删除"); }
    catch (e) { toast(e.message); }
  });
}

function userModal(u) {
  const isEdit = !!u;
  const roleOpts = _rolesCache.map((r) => `<option value="${r.id}" ${u && u.role_id == r.id ? "selected" : ""}>${esc(r.name)}</option>`).join("");
  openModal(isEdit ? "编辑用户" : "新增用户", `
    <label>用户名</label>
    <input id="uName" value="${esc(u ? u.username : "")}" ${isEdit ? "disabled" : ""}/>
    <label>显示名</label>
    <input id="uDisp" value="${esc(u ? u.display_name || "" : "")}"/>
    <label>角色</label>
    <select id="uRole">${roleOpts}</select>
    <label>密码${isEdit ? "（留空则不修改）" : ""}</label>
    <input id="uPass" type="password" placeholder="${isEdit ? "留空保持不变" : "请输入密码"}"/>
    ${isEdit ? `<label>状态</label><select id="uStatus">
      <option value="1" ${u.status ? "selected" : ""}>启用</option>
      <option value="0" ${!u.status ? "selected" : ""}>停用</option></select>` : ""}
  `, `<button class="btn ghost" data-close>取消</button><button class="btn" id="uSave">保存</button>`);
  $("#uSave").onclick = async () => {
    const payload = { display_name: $("#uDisp").value.trim(), role_id: parseInt($("#uRole").value, 10) };
    const pw = $("#uPass").value;
    if (pw) payload.password = pw;
    if (isEdit) { if ($("#uStatus")) payload.status = parseInt($("#uStatus").value, 10); }
    try {
      if (isEdit) await KBAPI.updateUser(u.id, payload);
      else { payload.username = $("#uName").value.trim(); await KBAPI.createUser(payload); }
      closeModal(); await refreshUsers(); toast("已保存");
    } catch (e) { toast(e.message); }
  };
}

/* ---------------- 角色管理 ---------------- */
async function viewRoles() {
  const main = $("#main");
  main.innerHTML = `<h2>角色管理</h2>
    <div class="crumb">定义角色并分配权限</div>
    <div class="toolbar"><div class="grow"></div><button class="btn" id="addRole">+ 新增角色</button></div>
    <div class="card pad"><table>
      <thead><tr><th>ID</th><th>角色</th><th>描述</th><th>权限数</th><th>类型</th><th>操作</th></tr></thead>
      <tbody id="roleBody"><tr><td colspan="6" class="empty">加载中…</td></tr></tbody>
    </table></div>`;
  $("#addRole").onclick = () => roleModal(null);
  await refreshRoles();
}

let _permCache = [];
async function refreshRoles() {
  const body = $("#roleBody");
  let roles, perms;
  try { const r = await KBAPI.roles(); roles = r.roles; perms = await KBAPI.permissions(); _permCache = perms.permissions; }
  catch (e) { body.innerHTML = `<tr><td colspan="6" class="empty">${esc(e.message)}</td></tr>`; return; }
  body.innerHTML = "";
  roles.forEach((r) => {
    const tr = elFromHTML(`<tr>
      <td>${r.id}</td><td><b>${esc(r.name)}</b></td><td class="muted">${esc(r.description || "")}</td>
      <td>${r.permissions.length}</td>
      <td>${r.builtin ? '<span class="badge builtin">内置</span>' : "自定义"}</td>
      <td>
        <button class="btn sm ghost" data-edit="${r.id}">编辑</button>
        ${r.builtin ? "" : `<button class="btn sm danger" data-del="${r.id}">删除</button>`}
      </td></tr>`);
    body.appendChild(tr);
  });
  body.querySelectorAll("[data-edit]").forEach((b) => b.onclick = () => {
    const r = roles.find((x) => x.id == b.dataset.edit); roleModal(r);
  });
  body.querySelectorAll("[data-del]").forEach((b) => b.onclick = async () => {
    if (!confirm("确认删除该角色？已绑定用户将变为未分配。")) return;
    try { await KBAPI.deleteRole(b.dataset.del); await refreshRoles(); toast("已删除"); }
    catch (e) { toast(e.message); }
  });
}

function roleModal(role) {
  const isEdit = !!role;
  const checked = new Set(role ? role.permissions : []);
  const permBoxes = _permCache.map((p) => `<label>
    <input type="checkbox" value="${esc(p.key)}" ${checked.has(p.key) ? "checked" : ""}/>
    <span><b>${esc(p.name)}</b><br><span class="muted" style="font-size:11px">${esc(p.description || "")}</span></span>
  </label>`).join("");
  openModal(isEdit ? "编辑角色" : "新增角色", `
    <label>角色名称</label>
    <input id="rName" value="${esc(role ? role.name : "")}" ${isEdit && role.builtin ? "disabled" : ""}/>
    <label>描述</label>
    <textarea id="rDesc">${esc(role ? role.description || "" : "")}</textarea>
    <label>权限分配</label>
    <div class="perm-grid">${permBoxes}</div>
  `, `<button class="btn ghost" data-close>取消</button><button class="btn" id="rSave">保存</button>`);
  $("#rSave").onclick = async () => {
    const perms = Array.from(document.querySelectorAll(".perm-grid input:checked")).map((i) => i.value);
    const payload = { description: $("#rDesc").value.trim(), permissions: perms };
    if (!isEdit || !role.builtin) payload.name = $("#rName").value.trim();
    try {
      if (isEdit) await KBAPI.updateRole(role.id, payload);
      else await KBAPI.createRole(payload);
      closeModal(); await refreshRoles(); toast("已保存");
    } catch (e) { toast(e.message); }
  };
}

/* ---------------- 权限目录 ---------------- */
async function viewPermissions() {
  const main = $("#main");
  main.innerHTML = `<h2>权限目录</h2>
    <div class="crumb">系统支持的权限项（由角色分配）</div>
    <div class="card pad"><table>
      <thead><tr><th>权限标识</th><th>名称</th><th>说明</th></tr></thead>
      <tbody id="permBody"><tr><td colspan="3" class="empty">加载中…</td></tr></tbody>
    </table></div>`;
  try {
    const { permissions } = await KBAPI.permissions();
    $("#permBody").innerHTML = permissions.map((p) => `<tr>
      <td><code>${esc(p.key)}</code></td><td>${esc(p.name)}</td><td class="muted">${esc(p.description || "")}</td></tr>`).join("");
  } catch (e) { $("#permBody").innerHTML = `<tr><td colspan="3" class="empty">${esc(e.message)}</td></tr>`; }
}

/* ---------------- 功能与系统 ---------------- */
async function viewSystem() {
  const main = $("#main");
  main.innerHTML = `<h2>功能与系统</h2>
    <div class="crumb">管理系统功能开关与索引维护</div>
    <div id="statGrid" class="stat-grid" style="margin-bottom:20px"></div>
    <div class="card pad" style="margin-bottom:20px">
      <h3 style="margin-top:0">功能开关</h3>
      <div id="featList"><div class="empty">加载中…</div></div>
    </div>
    <div class="card pad">
      <h3 style="margin-top:0">索引维护</h3>
      <p class="muted">重新构建 BM25 全文检索索引（含用户上传文档）。上传文档或原始数据变更后建议执行。</p>
      <button class="btn" id="reindexBtn">重建索引</button>
      <span id="reindexMsg" class="muted" style="margin-left:10px"></span>
    </div>`;
  try {
    const stats = await KBAPI.stats();
    $("#statGrid").innerHTML = [
      ["文档总数", stats.total_documents], ["分类数", stats.category_count],
      ["用户数", stats.user_count], ["启用用户", stats.active_users],
      ["索引状态", stats.index_ready ? "就绪" : "缺失"],
    ].map(([l, n]) => `<div class="stat"><div class="n">${esc(n)}</div><div class="l">${esc(l)}</div></div>`).join("");
    const fl = $("#featList");
    fl.innerHTML = `<table><thead><tr><th>功能</th><th>说明</th><th>状态</th><th>操作</th></tr></thead><tbody>
      ${stats.features.map((f) => `<tr>
        <td><b>${esc(f.name)}</b><br><code class="muted" style="font-size:11px">${esc(f.key)}</code></td>
        <td class="muted">${esc(f.description || "")}</td>
        <td><span class="badge ${f.enabled ? "on" : "off"}">${f.enabled ? "开启" : "关闭"}</span></td>
        <td><button class="btn sm ${f.enabled ? "ghost" : "ok"}" data-feat="${esc(f.key)}" data-on="${f.enabled}">${f.enabled ? "关闭" : "开启"}</button></td>
      </tr>`).join("")}</tbody></table>`;
    fl.querySelectorAll("[data-feat]").forEach((b) => b.onclick = async () => {
      await KBAPI.setFeature(b.dataset.feat, b.dataset.on === "1" ? 0 : 1);
      viewSystem(); toast("已更新");
    });
  } catch (e) { $("#statGrid").innerHTML = `<div class="empty">${esc(e.message)}</div>`; }
  $("#reindexBtn").onclick = async () => {
    $("#reindexBtn").disabled = true; $("#reindexMsg").textContent = "重建中，请稍候…";
    try { await KBAPI.reindex(); $("#reindexMsg").textContent = "✅ 重建完成"; toast("索引重建完成"); }
    catch (e) { $("#reindexMsg").textContent = "❌ " + e.message; }
    $("#reindexBtn").disabled = false;
    setTimeout(() => viewSystem(), 800);
  };
}

/* 启动 */
document.addEventListener("DOMContentLoaded", boot);
