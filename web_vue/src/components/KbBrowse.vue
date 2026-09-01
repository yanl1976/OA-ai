<script setup>
import { ref, onMounted, inject, watch, computed } from "vue";
import { api } from "../api.js";
import CatTreeNode from "./CatTreeNode.vue";
import PdfModal from "./PdfModal.vue";
import DocxModal from "./DocxModal.vue";

const notify = inject("notify");
const openDerivedInManage = inject("openDerivedInManage");
const openDerivedForDoc = inject("openDerivedForDoc");
const pendingDocId = inject("pendingDocId");
const user = inject("user");
// 纪要二次生成：① 需 derived.manage 权限；② 当前文档须属于「总经理类会议纪要」分类
// 【范围约束·用 ID 管理】允许二次生成的分类集合由后端按分类 id 规则动态计算
// （会议纪要父分类下、名称含「总经理」的子分类 id 集合），改名/新增/删除子类自动同步。
// 前端拉取该集合判断，不写死任何分类名。
const derivedAllowedNames = ref(new Set());
function isMinutesCategory(cat) {
  if (!cat) return false;
  return derivedAllowedNames.value.has(String(cat));
}
// 当前文档是否为「纪要类」（仅用于判断是否可展示二次生成入口）
const isMinutesDoc = computed(() => isMinutesCategory(docDetail.value?.category));
// 是否有「会议纪要二次生成」权限
const hasDerivedPerm = computed(() => {
  const perms = user.value?.permissions || [];
  return perms.includes("derived.manage");
});
// 二次生成按钮：① 需 derived.manage 权限；② 当前文档须属于「纪要」分类域
const canDerived = computed(() => isMinutesDoc.value && hasDerivedPerm.value);

const roots = ref([]);
const selected = ref(""); // "" 表示全部文档
const docs = ref([]);
const total = ref(0);
const years = ref([]);
const selectedYear = ref(null);
const page = ref(1);
const pageSize = 15;
const loadingDocs = ref(false);
const docDetail = ref(null);
const loadingDoc = ref(false);
// 中间列表搜索（按文档名/分类模糊筛选当前列表）
const search = ref("");
let searchTimer = null;

// 该文档的衍生版本（顺查：原版 -> 衍生）
const derivedForDoc = ref([]);
// PDF 预览
const pdfShow = ref(false);
const pdfUrl = ref("");
const pdfTitle = ref("");

function buildTree(flat) {
  const map = {};
  flat.forEach((c) => (map[c.id] = { ...c, children: [] }));
  const rs = [];
  flat.forEach((c) => {
    if (c.parent_id && map[c.parent_id]) map[c.parent_id].children.push(map[c.id]);
    else rs.push(map[c.id]);
  });
  const sortFn = (a, b) => (a.sort_order || 0) - (b.sort_order || 0) || a.name.localeCompare(b.name, "zh");
  const sortRec = (nodes) => {
    nodes.sort(sortFn);
    nodes.forEach((n) => sortRec(n.children));
  };
  sortRec(rs);
  return rs;
}

async function loadCats() {
  try {
    const r = await api.categories();
    roots.value = buildTree(r.categories || []);
  } catch (e) {
    notify(e.message, "err");
  }
}

async function loadDocs() {
  loadingDocs.value = true;
  docDetail.value = null;
  try {
    const r = await api.documents({
      category: selected.value,
      year: selectedYear.value,
      q: search.value.trim(),
      page: page.value,
      page_size: pageSize,
    });
    docs.value = r.items || [];
    total.value = r.total || 0;
    years.value = r.years || [];
  } catch (e) {
    notify(e.message, "err");
  } finally {
    loadingDocs.value = false;
  }
}

async function loadDerivedForDoc(docId) {
  try {
    const r = await api.derivedList(docId);
    derivedForDoc.value = r.items || [];
  } catch (e) {
    derivedForDoc.value = [];
  }
}

function selectCat(name) {
  selected.value = name || "";
  selectedYear.value = null;
  page.value = 1;
  loadDocs();
}

// 中间列表搜索：防抖 300ms，输入即筛选当前分类/年代下的文档
function onSearch() {
  if (searchTimer) clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    page.value = 1;
    loadDocs();
  }, 300);
}

function clearSearch() {
  search.value = "";
  page.value = 1;
  loadDocs();
}

function selectYear(y) {
  selectedYear.value = selectedYear.value === y ? null : y;
  page.value = 1;
  loadDocs();
}

async function openDoc(d) {
  loadingDoc.value = true;
  try {
    const r = await api.document(d.doc_id);
    docDetail.value = r.document;
    await loadDerivedForDoc(d.doc_id);
  } catch (e) {
    notify(e.message, "err");
  } finally {
    loadingDoc.value = false;
  }
}

function previewPdf() {
  if (!docDetail.value) return;
  pdfUrl.value = api.docPdfUrl(docDetail.value.doc_id, true);
  pdfTitle.value = docDetail.value.label || docDetail.value.filename || "文档";
  pdfShow.value = true;
}
// docx/doc 原版版面预览（浏览器内直接渲染，不转 PDF）
const docxShow = ref(false);
const docxUrl = ref("");
function isDocxDoc() {
  const d = docDetail.value;
  if (!d) return false;
  const ext = d.ext || "";
  if (ext === ".docx" || ext === ".doc") return true;
  return /word/.test(d.mimetype || "");
}
function previewDocx() {
  if (!docDetail.value) return;
  docxUrl.value = api.docPdfUrl(docDetail.value.doc_id, true);
  docxShow.value = true;
}

function openDerived(d) {
  openDerivedInManage(d.id);
}

// 跨页跳转：从衍生版本页跳回原版文档时自动打开
async function applyPendingDoc() {
  const id = pendingDocId.value;
  if (!id) return;
  pendingDocId.value = null;
  const target = docs.value.find((d) => d.doc_id === id);
  if (target) {
    await openDoc(target);
  } else {
    // 列表中找不到则直接拉取详情
    loadingDoc.value = true;
    try {
      const r = await api.document(id);
      docDetail.value = r.document;
      await loadDerivedForDoc(id);
    } catch (e) {
      notify(e.message, "err");
    } finally {
      loadingDoc.value = false;
    }
  }
}

watch(pendingDocId, async () => {
  if (pendingDocId.value) await applyPendingDoc();
});

function goPage(p) {
  page.value = p;
  loadDocs();
}

const totalPages = () => Math.max(1, Math.ceil(total.value / pageSize));

onMounted(async () => {
  loadCats();
  await loadDocs();
  if (pendingDocId.value) await applyPendingDoc();
  // 拉取允许二次生成的分类集合（后端按 id 规则动态计算），用于判断按钮可见性
  try {
    const r = await api.derivedAllowedCategories();
    derivedAllowedNames.value = new Set(r.names || []);
  } catch (e) { /* 非致命：失败则按钮默认不显示 */ }
});
</script>

<template>
  <h2>知识浏览</h2>
  <div class="kb">
    <!-- 分类树 -->
    <div class="col-cats card card-pad">
      <div class="cat-head">
        <span class="muted">全部分类</span>
      </div>
      <div class="cat-tree">
        <div
          class="cat-row"
          :class="{ active: selected === '' }"
          @click="selectCat('')"
        >
          <span class="twisty empty"></span>
          <span class="cat-label">📑 全部文档</span>
          <span class="cnt">{{ total }}</span>
        </div>
        <CatTreeNode
          v-for="r in roots"
          :key="r.id"
          :node="r"
          :depth="0"
          :selected-name="selected"
          @select="selectCat"
        />
      </div>
    </div>

    <!-- 文档列表 -->
    <div class="col-list card card-pad">
      <div class="cat-head">
        <span class="muted">{{ selected || "全部文档" }}（{{ total }}）</span>
      </div>
      <!-- 列表内搜索（按文档名/分类模糊筛选当前列表） -->
      <div class="list-search">
        <input
          class="search-input"
          type="text"
          v-model="search"
          placeholder="搜索文档名 / 分类…"
          @input="onSearch"
        />
        <button v-if="search" class="search-clear" @click="clearSearch">✕</button>
      </div>
      <!-- 年代筛选按钮 -->
      <div class="year-bar" v-if="years.length">
        <button class="chip" :class="{ on: selectedYear === null }" @click="selectYear(null)">全部</button>
        <button
          v-for="y in years"
          :key="y"
          class="chip"
          :class="{ on: selectedYear === y }"
          @click="selectYear(y)"
        >{{ y }}年</button>
      </div>
      <div v-if="loadingDocs" class="loading">加载中…</div>
      <div v-else-if="!docs.length" class="loading">暂无文档</div>
      <div
        v-for="d in docs"
        :key="d.doc_id"
        class="doc-item"
        :class="{ active: docDetail && docDetail.doc_id === d.doc_id }"
        @click="openDoc(d)"
      >
        <div class="t">{{ d.label || d.filename }}</div>
        <div class="m">
          {{ d.category }} · {{ d.pages }} 页 · {{ d.year ? d.year + "年 · " : "" }}{{ d.source === "upload" ? "上传" : "库" }}
        </div>
      </div>

      <div class="pager toolbar" v-if="totalPages() > 1">
        <button class="btn sm" :disabled="page <= 1" @click="goPage(page - 1)">上一页</button>
        <span class="muted">第 {{ page }} / {{ totalPages() }} 页</span>
        <button class="btn sm" :disabled="page >= totalPages()" @click="goPage(page + 1)">下一页</button>
      </div>
    </div>

    <!-- 文档详情 -->
    <div class="col-view card card-pad">
      <div v-if="loadingDoc" class="loading">加载中…</div>
      <div v-else-if="!docDetail" class="loading">请选择左侧文档查看</div>
      <div v-else>
        <h3 style="margin: 0 0 6px">{{ docDetail.label || docDetail.filename }}</h3>
        <div class="muted" style="margin-bottom: 12px">
          {{ docDetail.category }} · 共 {{ docDetail.pages }} 页<span v-if="docDetail.year"> · {{ docDetail.year }} 年</span>
          <span class="badge" style="margin-left:8px">{{ docDetail.source === "upload" ? "上传文档" : "原始库" }}</span>
        </div>
        <div class="toolbar" style="margin-bottom:10px">
          <button class="btn sm primary" @click="previewPdf">预览 PDF</button>
          <button v-if="isDocxDoc()" class="btn sm primary" @click="previewDocx">预览 docx</button>
          <button class="btn sm" v-if="canDerived" @click="openDerivedForDoc(docDetail.doc_id)">纪要二次生成</button>
          <button
            class="btn sm disabled"
            v-else-if="isMinutesDoc && !hasDerivedPerm"
            title="当前账号未授予「会议纪要二次生成」权限"
            disabled
          >纪要二次生成（无权限）</button>
        </div>
        <div class="doc-view" @contextmenu.prevent @copy.prevent @cut.prevent @selectstart.prevent>{{ docDetail.text || docDetail.full_text || "（无正文）" }}</div>

        <!-- 衍生版本（顺查：原版 -> 衍生） -->
        <div class="derived-panel" v-if="derivedForDoc.length">
          <div class="nav-group-title" style="padding-left:0">
            衍生版本（{{ derivedForDoc.length }}）· 由本纪要二次生成
          </div>
          <table class="table">
            <thead><tr><th>标题</th><th>版本</th><th>需求</th><th>去向</th><th>操作</th></tr></thead>
            <tbody>
              <tr v-for="d in derivedForDoc" :key="d.id">
                <td class="mid"><b>{{ d.title }}</b></td>
                <td class="mid">v{{ d.version }}</td>
                <td class="mid">{{ d.requirement || "—" }}</td>
                <td class="mid"><span class="badge role">{{ d.destination || "—" }}</span></td>
                <td class="mid"><button class="btn sm" @click="openDerived(d)">查看 / 预览</button></td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>

    <PdfModal :show="pdfShow" :url="pdfUrl" :title="pdfTitle" @close="pdfShow = false" />
    <DocxModal :show="docxShow" :url="docxUrl" :title="docDetail?.label || docDetail?.filename || 'DOCX 预览'" @close="docxShow = false" />
  </div>
</template>

<style scoped>
.doc-view {
  white-space: pre-wrap;
  word-break: break-word;
  font-size: 13px;
  line-height: 1.75;
  max-height: 68vh;
  overflow: auto;
  background: #fcfdff;
  border: 1px solid var(--line);
  border-radius: 8px;
  /* 知识浏览预览：禁止选择/复制 */
  -webkit-user-select: none;
  -moz-user-select: none;
  -ms-user-select: none;
  user-select: none;
  padding: 14px 16px;
}
.year-bar {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 12px;
}
.list-search {
  position: relative;
  margin-bottom: 12px;
}
.search-input {
  width: 100%;
  box-sizing: border-box;
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 7px 28px 7px 11px;
  font-size: 13px;
  color: var(--text);
  background: #fff;
  outline: none;
  transition: border-color 0.15s;
}
.search-input:focus { border-color: var(--primary); }
.search-clear {
  position: absolute;
  right: 6px;
  top: 50%;
  transform: translateY(-50%);
  border: none;
  background: transparent;
  color: var(--muted, #9aa3b2);
  cursor: pointer;
  font-size: 13px;
  line-height: 1;
  padding: 4px;
}
.search-clear:hover { color: var(--text); }
.chip {
  border: 1px solid var(--line);
  background: #fff;
  color: var(--text);
  padding: 3px 11px;
  border-radius: 999px;
  cursor: pointer;
  font-size: 12px;
  transition: all 0.15s;
}
.chip:hover { border-color: var(--primary); color: var(--primary); }
.chip.on {
  background: var(--primary);
  color: #fff;
  border-color: transparent;
}
.derived-panel { margin-top: 18px; border-top: 1px dashed var(--line); padding-top: 12px; }
</style>
