<script setup>
import { ref, reactive, computed, onMounted, inject, watch } from "vue";
import { api } from "../api.js";
import PdfModal from "./PdfModal.vue";

const notify = inject("notify");
const openDocInBrowse = inject("openDocInBrowse");
const pendingDerivedId = inject("pendingDerivedId");
// 从知识浏览页「纪要二次生成」按钮带过来的来源文档 id：加载后自动选为该来源并进入生成 tab
const pendingDerivedSourceId = inject("pendingDerivedSourceId");

// 中文数字（与后端 renumber_items 对应，用于二次生成时章节号自动重编号预览）
const CN = "零一二三四五六七八九";
function num2cn(n) {
  if (n <= 0) return String(n);
  if (n < 10) return CN[n];
  if (n < 20) return "十" + (n % 10 ? CN[n - 10] : "");
  if (n < 100) {
    const t = Math.floor(n / 10), o = n % 10;
    return CN[t] + "十" + (o ? CN[o] : "");
  }
  return String(n);
}
function renumberItems(items, first = 1) {
  return (items || []).map((it, k) => {
    const body = (it.title || "").replace(/^\s*([0-9]{1,2}|[一二三四五六七八九十百零]+)\s*[\.、．]\s*/, "");
    return { ...it, title: num2cn(first + k) + "、" + body };
  });
}

const DEST_OPTIONS = ["董事会", "管理层", "财务部", "人力资源部", "项目组",
  "外部审计机构", "法务部", "其他"];

// 将文本切分为可选中的「段落块」（与后端 derived_store.split_blocks 逻辑保持一致）
// 仅在无法识别为模板时作为回退使用
function splitBlocks(text) {
  if (!text) return [];
  const byBlank = text.split(/\n[ \t]*\n/).map((s) => s.trim()).filter(Boolean);
  let raw;
  if (byBlank.length > 1) raw = byBlank;
  else {
    const lines = text.split(/\n/).map((l) => l.replace(/\s+$/, ""));
    raw = [];
    let buf = [];
    for (const ln of lines) {
      if (ln.trim() === "") {
        if (buf.length) { raw.push(buf.join("\n")); buf = []; }
      } else buf.push(ln);
    }
    if (buf.length) raw.push(buf.join("\n"));
  }
  // 决策/结论性标记句（如「会议决定：…」「经讨论，会议一致同意…」）独立成行
  const DECISION = /(?<=[。])((?:会议决定[：:]|经讨论[，,]?|会议一致通过|会议一致同意|会议认为|会议要求|会议指出))/;
  const out = [];
  for (const b of raw) {
    const b2 = b.replace(DECISION, "\n$1");
    if (b2.includes("\n")) {
      for (const seg of b2.split("\n")) if (seg.trim()) out.push(seg.trim());
    } else out.push(b);
  }
  return out.filter(Boolean);
}

// 将模板结构化字段重排为纯文本（预览/查看用，与后端 render_minutes 对应）
function renderMinutes(st) {
  if (!st) return "";
  const parts = [];
  for (const k of ["org", "doc_no", "office_line", "meeting_name", "meeting_seq"]) {
    const v = (st[k] || "").trim();
    if (v) parts.push(v);
  }
  const intro = (st.intro || "").trim();
  if (intro) parts.push(intro);
  for (const it of (st.items || [])) {
    const t = (it.title || "").trim();
    if (t) parts.push(t);
    const b = (it.body || "").trim();
    if (b) parts.push(b);
    const d = (it.decision || "").trim();
    if (d) parts.push(d);
  }
  const p = (st.present || "").trim();
  if (p) parts.push(p);
  const a = (st.absent || "").trim();
  if (a) parts.push(a);
  return parts.join("\n");
}

const tab = ref("generate");

// ---- 源会议纪要 ----
const sources = ref([]);
const srcSearch = ref("");
const filteredSources = computed(() => {
  const q = srcSearch.value.trim().toLowerCase();
  if (!q) return sources.value;
  return sources.value.filter((s) =>
    (s.filename || "").toLowerCase().includes(q) ||
    (s.category || "").toLowerCase().includes(q));
});

const selectedSource = ref(null);
const sourceText = ref("");

// 模板模式：结构化字段 + 议题级选择
const tpl = ref(null);                 // 解析后的结构化对象（structured=true 时启用模板模式）
const selItems = ref(new Set());       // 选中的议题序号
const meta = reactive({
  org: "", doc_no: "", office_line: "", meeting_name: "",
  meeting_seq: "", intro: "", present: "", absent: "",
});

// 回退模式：段落块选择
const blocks = ref([]);
const selected = ref(new Set());

const mode = computed(() => (tpl.value && tpl.value.structured ? "template" : "block"));

const allItemsSelected = computed(() =>
  tpl.value && tpl.value.items.length > 0 && selItems.value.size === tpl.value.items.length);
const allSelected = computed(() =>
  blocks.value.length > 0 && selected.value.size === blocks.value.length);

const selectedChars = computed(() => {
  if (mode.value === "template") {
    const sel = [...selItems.value].sort((a, b) => a - b)
      .map((i) => tpl.value.items[i]).join("\n");
    return sel.replace(/\s/g, "").length;
  }
  const sel = [...selected.value].sort((a, b) => a - b)
    .map((i) => blocks.value[i] || "").join("\n\n");
  return sel.replace(/\s/g, "").length;
});
const canGenerate = computed(() =>
  !!selectedSource.value && (mode.value === "template" ? selItems.value.size > 0 : selected.value.size > 0));

const destPick = ref("");
const form = reactive({
  title: "", requirement: "", destination: "", parent_id: null, editingId: null,
});

function onDestPick() {
  if (destPick.value) form.destination = destPick.value;
}

function toggleItem(i) {
  const ns = new Set(selItems.value);
  if (ns.has(i)) ns.delete(i); else ns.add(i);
  selItems.value = ns;
}
function selectAllItems() {
  selItems.value = allItemsSelected.value
    ? new Set()
    : new Set(tpl.value.items.map((_, i) => i));
}
function toggle(i) {
  const ns = new Set(selected.value);
  if (ns.has(i)) ns.delete(i); else ns.add(i);
  selItems.value = ns;
}
function selectAll() {
  selected.value = allSelected.value
    ? new Set()
    : new Set(blocks.value.map((_, i) => i));
}

const genPreview = computed(() => {
  if (!selectedSource.value) return "";
  const metaHead = [
    "【二次生成会议纪要】",
    "标题：" + (form.title || "（未命名）"),
    "需求：" + (form.requirement || "—"),
    "去向：" + (form.destination || "—"),
    "────────────────",
  ].join("\n");
  if (mode.value === "template") {
    const st = {
      structured: true, ...meta,
      items: renumberItems(
        [...selItems.value].sort((a, b) => a - b).map((i) => tpl.value.items[i])),
    };
    return metaHead + "\n" + (renderMinutes(st) || "（请在上方选择需要保留的议题）");
  }
  const sel = [...selected.value].sort((a, b) => a - b)
    .map((i) => blocks.value[i]).join("\n\n");
  return metaHead + "\n" + (sel || "（请在上方选择需要保留的段落）");
});

// ---- 衍生版本管理 ----
const derivedList = ref([]);
const filterSource = ref("all");

async function loadSources() {
  try {
    // 【范围约束·用 ID 管理】二次生成仅限「总经理类会议纪要」来源文档。
    // 允许的分类集合由后端按分类 id 规则动态计算（改名/新增/删除子类自动同步），
    // 前端不再写死任何分类名。先取「会议纪要」父级下全部，再按后端下发的名集合过滤。
    const allowed = await api.derivedAllowedCategories();
    const allowedNames = new Set(allowed.names || []);
    const r = await api.documents({ category: "会议纪要", page_size: 300 });
    const all = r.items || [];
    sources.value = all.filter((s) => allowedNames.has(s.category));
  } catch (e) { notify(e.message || "加载来源失败", "err"); }
}
async function loadDerived(sourceDocId) {
  try {
    const r = await api.derivedList(sourceDocId);
    derivedList.value = r.items || [];
  } catch (e) { notify(e.message || "加载衍生版本失败", "err"); }
}

async function selectSource(s) {
  selectedSource.value = s;
  resetGen();
  try {
    const r = await api.document(s.doc_id);
    sourceText.value = r.document.text || "";
    await parseSource(sourceText.value);
    await loadDerived(s.doc_id);
  } catch (e) { notify(e.message || "读取纪要正文失败", "err"); }
}

// 解析源正文：能识别为模板则进入议题级模式，否则回退段落块
async function parseSource(text) {
  try {
    const pr = await api.derivedParse(text);
    const st = pr.struct;
    if (st && st.structured) {
      tpl.value = st;
      // 优先使用后端按内容特征解析出的结构化字段（org/doc_no/office_*/meeting_name/
      // meeting_seq），与 PDF 渲染路径（tpl.meeting_seq）保持一致，避免预览为空。
      // 仅在结构化字段缺失时，才回退到 header_lines 按内容特征定位（不依赖固定行序）。
      const hlines = st.header_lines || [];
      const seqFromHlines = (hlines.find((x) => /[（(][^（）()]*?次[）)]/.test(x)) || "").trim();
      meta.org = st.org || hlines[0] || "";
      meta.doc_no = st.doc_no || hlines[1] || "";
      meta.office_line = st.office_line || hlines[2] || "";
      meta.meeting_name = st.meeting_name || hlines[3] || "";
      meta.meeting_seq = st.meeting_seq || seqFromHlines || hlines[4] || "";
      meta.intro = st.intro || "";
      meta.present = st.present || ""; meta.absent = st.absent || "";
      selItems.value = new Set(st.items.map((_, i) => i));
      blocks.value = []; selected.value = new Set();
      return;
    }
  } catch (e) { /* 解析失败则回退 */ }
  tpl.value = null;
  blocks.value = splitBlocks(text);
  selected.value = new Set();
}

function resetGen() {
  form.title = ""; form.requirement = ""; form.destination = "";
  form.parent_id = null; form.editingId = null;
  destPick.value = "";
  selItems.value = new Set(); selected.value = new Set();
}

async function generate() {
  const source_doc_id = selectedSource.value.doc_id;
  const source_title = selectedSource.value.filename;
  let payload;
  if (mode.value === "template") {
    const items = [...selItems.value].sort((a, b) => a - b).map((i) => tpl.value.items[i]);
    if (!items.length) { notify("请至少选择一个议题", "err"); return; }
    const struct = {
      structured: true,
      org: meta.org, doc_no: meta.doc_no, office_line: meta.office_line,
      meeting_name: meta.meeting_name, meeting_seq: meta.meeting_seq,
      intro: meta.intro, items, present: meta.present, absent: meta.absent,
    };
    payload = {
      source_doc_id, source_title,
      template: struct,
      renumber: true,  // 选中议题自动从「一、」顺延重编号
      selected_blocks: [...selItems.value].sort((a, b) => a - b),
      requirement: form.requirement, destination: form.destination,
      title: form.title, parent_id: form.parent_id || null,
    };
  } else {
    const content = [...selected.value].sort((a, b) => a - b)
      .map((i) => blocks.value[i]).join("\n\n");
    if (!content.trim()) { notify("请至少选择一段内容", "err"); return; }
    payload = {
      source_doc_id, source_title,
      content, selected_blocks: [...selected.value].sort((a, b) => a - b),
      requirement: form.requirement, destination: form.destination,
      title: form.title, parent_id: form.parent_id || null,
    };
  }
  try {
    let created = null;
    if (form.editingId) {
      await api.derivedUpdate(form.editingId, payload);
      notify("已保存修改", "ok");
    } else {
      const r = await api.derivedCreate(payload);
      created = r.derived || null;
      notify("二次纪要已生成", "ok");
    }
    await loadDerived(filterSource.value === "all" ? null : filterSource.value);
    if (selectedSource.value) await loadDerived(selectedSource.value.doc_id);
    resetGen();
    // 方案 A：生成/保存成功后直接弹出该纪要的 PDF 预览界面，最直给
    if (created) {
      previewPdf(created);
    } else {
      // 编辑模式下无新建对象，退回管理标签查看
      tab.value = "manage";
    }
  } catch (e) { notify(e.message || "操作失败", "err"); }
}

async function editDerived(d) {
  tab.value = "generate";
  const restoreFromTemplate = (st) => {
    tpl.value = st;
    // 与 parseSource 保持一致：优先用结构化字段，缺失时回退 header_lines 按内容特征定位
    const hlines = st.header_lines || [];
    const seqFromHlines = (hlines.find((x) => /[（(][^（）()]*?次[）)]/.test(x)) || "").trim();
    meta.org = st.org || hlines[0] || "";
    meta.doc_no = st.doc_no || hlines[1] || "";
    meta.office_line = st.office_line || hlines[2] || "";
    meta.meeting_name = st.meeting_name || hlines[3] || "";
    meta.meeting_seq = st.meeting_seq || seqFromHlines || hlines[4] || "";
    meta.intro = st.intro || "";
    meta.present = st.present || ""; meta.absent = st.absent || "";
    selItems.value = new Set((d.selected_blocks || []).filter((i) => i < st.items.length));
    blocks.value = []; selected.value = new Set();
  };
  const restoreFromBlocks = async () => {
    tpl.value = null;
    sourceText.value = d.content || "";
    blocks.value = splitBlocks(sourceText.value);
    selected.value = new Set(blocks.value.map((_, i) => i));
  };

  if (d.template && d.template.structured) {
    // 模板记录：重新解析来源以还原完整议题列表，再按 selected_blocks 勾选
    const src = sources.value.find((s) => s.doc_id === d.source_doc_id);
    if (src) {
      selectedSource.value = src;
      try {
        const r = await api.document(d.source_doc_id);
        sourceText.value = r.document.text || "";
        const pr = await api.derivedParse(sourceText.value);
        if (pr.struct && pr.struct.structured) restoreFromTemplate(pr.struct);
        else restoreFromBlocks();
      } catch (e) { notify(e.message || "读取来源失败", "err"); return; }
    } else {
      restoreFromTemplate(d.template);
    }
  } else {
    selectedSource.value = { doc_id: d.source_doc_id, filename: d.source_title,
      category: "", year: "" };
    await restoreFromBlocks();
  }
  form.title = d.title; form.requirement = d.requirement;
  form.destination = d.destination;
  form.parent_id = null; form.editingId = d.id;
  destPick.value = "";
  selectedSource.value && await loadDerived(d.source_doc_id);
}

async function reDerived(d) {
  tab.value = "generate";
  tpl.value = null;
  sourceText.value = d.content || "";
  // 再生成：以衍生内容为正文重新解析（可能再次识别为模板）
  await parseSource(sourceText.value);
  selectedSource.value = { doc_id: d.source_doc_id, filename: d.source_title,
    category: "", year: "" };
  form.title = d.title + "（衍生）";
  form.requirement = d.requirement;
  form.destination = d.destination;
  form.parent_id = d.id;
  form.editingId = null;
  destPick.value = "";
  await loadDerived(d.source_doc_id);
}

async function delDerived(d) {
  if (!confirm("确认删除衍生版本「" + d.title + "」？此操作不可撤销。")) return;
  try {
    await api.derivedDelete(d.id);
    notify("已删除", "ok");
    await loadDerived(filterSource.value === "all" ? null : filterSource.value);
    if (selectedSource.value) await loadDerived(selectedSource.value.doc_id);
  } catch (e) { notify(e.message || "删除失败", "err"); }
}

// ---- 查看 / 导出 ----
const viewItem = ref(null);
const lineage = ref(null);
const pdfShow = ref(false);
const pdfUrl = ref("");
const pdfTitle = ref("");

async function openView(d) {
  viewItem.value = d;
  lineage.value = null;
  try {
    const r = await api.derivedLineage(d.id);
    lineage.value = r.lineage;
  } catch (e) { /* 血缘查询失败不影响查看 */ }
}
function previewPdf(d) {
  pdfUrl.value = api.derivedPdfPreviewUrl(d.id);
  pdfTitle.value = d.title || "二次生成会议纪要";
  pdfShow.value = true;
}
// 预览该衍生版本关联的「原版 PDF」（二次生成前的来源文件）
function previewSourcePdf(d) {
  if (!d.has_source_pdf) {
    notify("原文件不存在，无法预览原版 PDF（来源原版可能未上传或已被删除）", "err");
    return;
  }
  pdfUrl.value = api.derivedSourcePdfUrl(d.id, true);
  pdfTitle.value = (d.source_title || "原版文档") + "（原版）";
  pdfShow.value = true;
}

// 跨页跳转：从知识浏览的衍生面板跳转到本衍生版本时自动打开查看
async function applyPendingDerived() {
  const id = pendingDerivedId.value;
  if (!id) return;
  pendingDerivedId.value = null;
  const d = derivedList.value.find((x) => x.id === id);
  if (d) openView(d);
}
watch(pendingDerivedId, async () => {
  if (pendingDerivedId.value) await applyPendingDerived();
});

function derivedFullText(d) {
  if (d.template && d.template.structured) {
    // 模板记录：展示按模板重排的正式纪要正文
    const head = [
      "【二次生成会议纪要（按模板版式）】",
      "标题：" + (d.title || ""),
      "版本：v" + (d.version || 1),
      "需求：" + (d.requirement || "—"),
      "去向：" + (d.destination || "—"),
      "生成时间：" + (d.created_at || ""),
      "────────────────",
    ].join("\n");
    return head + "\n" + (d.template ? renderMinutes(d.template) : (d.content || ""));
  }
  const header = [
    "【二次生成会议纪要】",
    "来源：" + (d.source_title || ""),
    "标题：" + (d.title || ""),
    "版本：v" + (d.version || 1),
    "需求：" + (d.requirement || "—"),
    "去向：" + (d.destination || "—"),
    "生成时间：" + (d.created_at || ""),
    "────────────────",
  ].join("\n");
  return header + "\n" + (d.content || "");
}
function exportTxt(d) {
  const txt = derivedFullText(d);
  const blob = new Blob([txt], { type: "text/plain;charset=utf-8" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = (d.title || "二次纪要") + ".txt";
  a.click();
  URL.revokeObjectURL(a.href);
}

// 生成并下载二次生成纪要的正式 PDF（基于原始会议纪要二次生成）
function exportPdf(d) {
  const a = document.createElement("a");
  a.href = api.derivedPdfUrl(d.id);
  a.download = (d.title || "二次生成会议纪要") + ".pdf";
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
}

onMounted(async () => {
  await loadSources();
  await loadDerived(null);
  if (pendingDerivedId.value) await applyPendingDerived();
  // 从知识浏览页「纪要二次生成」按钮带过来：自动选为该来源纪要并进入生成 tab
  const srcId = pendingDerivedSourceId.value;
  if (srcId) {
    pendingDerivedSourceId.value = null;
    let src = sources.value.find((s) => s.doc_id === srcId);
    if (!src) {
      // 来源不一定属于「会议纪要」分类列表：直接以 doc_id 拉取最小信息构造来源对象
      try {
        const r = await api.document(srcId);
        const d = r.document;
        src = { doc_id: d.doc_id, filename: d.label || d.filename || d.doc_id,
                category: d.category, year: d.year };
      } catch (e) {
        notify("读取来源文档失败：" + (e.message || ""), "err");
        src = null;
      }
    }
    if (src) {
      tab.value = "generate";
      await selectSource(src);
    }
  }
});
</script>

<template>
  <div class="md-root">
    <div class="toolbar">
      <div class="md-tabs">
        <button :class="['md-tab', { active: tab === 'generate' }]" @click="tab = 'generate'">二次生成</button>
        <button :class="['md-tab', { active: tab === 'manage' }]" @click="tab = 'manage'">衍生版本管理</button>
      </div>
    </div>

    <!-- 二次生成 -->
    <div v-if="tab === 'generate'">
      <div class="gen-topbar">
        <button class="btn primary" :disabled="!canGenerate" @click="generate">
          {{ form.editingId ? "保存修改" : "生成二次纪要" }}
        </button>
        <span class="muted" v-if="!canGenerate" style="font-size:12px">请先选择来源纪要并勾选内容</span>
      </div>
      <div class="md-gen">
      <div class="md-left card card-pad">
        <div class="field">
          <label>源会议纪要（按文件名检索）</label>
          <input class="input" v-model="srcSearch" placeholder="搜索文件名…" />
        </div>
        <div class="src-list">
          <div v-for="s in filteredSources" :key="s.doc_id"
               :class="['doc-item', { active: selectedSource && selectedSource.doc_id === s.doc_id }]"
               @click="selectSource(s)">
            <div class="t">{{ s.filename }}</div>
            <div class="m">{{ s.year || "—" }} · {{ s.category }}</div>
          </div>
          <div v-if="!filteredSources.length" class="muted" style="padding:10px">暂无会议纪要</div>
        </div>
      </div>

      <div class="md-right card card-pad" v-if="selectedSource">
        <div class="gen-head">
          <div>
            <div style="font-weight:700">{{ selectedSource.filename }}</div>
            <div class="muted" style="font-size:12px">
              来源：{{ selectedSource.category }} · {{ selectedSource.year || "—" }}
              <span v-if="mode==='template'" class="badge role" style="margin-left:6px">模板识别</span>
              <span v-else class="badge" style="margin-left:6px">普通文本</span>
            </div>
          </div>
          <button class="btn sm" v-if="mode==='template'" @click="selectAllItems">
            {{ allItemsSelected ? "取消全选" : "全选议题" }}
          </button>
          <button class="btn sm" v-else @click="selectAll">
            {{ allSelected ? "取消全选" : "全选" }}
          </button>
        </div>

        <!-- 模板模式：文头元信息 + 议题级选择 -->
        <template v-if="mode==='template'">
          <div class="tpl-meta">
            <div class="field"><label>单位名称</label>
              <input class="input" v-model="meta.org" placeholder="如：XX公司纪要" /></div>
            <div class="field-row">
              <div class="field"><label>文号</label>
                <input class="input" v-model="meta.doc_no" placeholder="天研司会议纪要〔2024〕59 号" /></div>
              <div class="field"><label>落款办公室/日期</label>
                <input class="input" v-model="meta.office_line" /></div>
            </div>
            <div class="field-row">
              <div class="field"><label>会议名称</label>
                <input class="input" v-model="meta.meeting_name" placeholder="总经理办公会会议纪要" /></div>
              <div class="field"><label>会议次数</label>
                <input class="input" v-model="meta.meeting_seq" placeholder="（源文件未标注则留空）" /></div>
            </div>
            <div class="field"><label>导语</label>
              <textarea class="input" rows="2" v-model="meta.intro"></textarea></div>
          </div>

          <div class="blocks">
            <label v-for="(it, i) in tpl.items" :key="i"
                   :class="['block', 'item', { sel: selItems.has(i) }]">
              <input type="checkbox" :checked="selItems.has(i)" @change="toggleItem(i)" />
              <div class="block-txt">
                <div class="item-title">{{ it.title }}</div>
                <div class="item-body" v-if="it.body">{{ it.body }}</div>
                <div class="item-dec" v-if="it.decision">{{ it.decision }}</div>
              </div>
              <span class="block-idx">议题 {{ i + 1 }}</span>
            </label>
            <div v-if="!tpl.items.length" class="muted">未识别到议题。</div>
          </div>

          <div class="field-row" style="margin-top:6px">
            <div class="field"><label>出席人员</label>
              <textarea class="input" rows="2" v-model="meta.present"></textarea></div>
            <div class="field"><label>列席人员</label>
              <textarea class="input" rows="2" v-model="meta.absent"></textarea></div>
          </div>
        </template>

        <!-- 回退模式：段落块选择 -->
        <template v-else>
          <div class="blocks">
            <label v-for="(b, i) in blocks" :key="i" :class="['block', { sel: selected.has(i) }]">
              <input type="checkbox" :checked="selected.has(i)" @change="toggle(i)" />
              <div class="block-txt">{{ b }}</div>
              <span class="block-idx">#{{ i + 1 }}</span>
            </label>
            <div v-if="!blocks.length" class="muted">该纪要正文为空或无法读取。</div>
          </div>
        </template>

        <div class="field" style="margin-top:8px">
          <label>二次纪要标题</label>
          <input class="input" v-model="form.title" placeholder="如：董事会决议摘要-预算部分" />
        </div>
        <div class="field">
          <label>文件需求（该衍生文件为何而生 / 需体现什么）</label>
          <textarea class="input" rows="2" v-model="form.requirement"
            placeholder="例：提交财务部用于年度预算审核，仅保留预算相关决议"></textarea>
        </div>
        <div class="field">
          <label>文件去向（报送 / 分发对象）</label>
          <div style="display:flex; gap:8px">
            <select class="input" v-model="destPick" style="max-width:220px" @change="onDestPick">
              <option value="">自定义…</option>
              <option v-for="d in DEST_OPTIONS" :key="d" :value="d">{{ d }}</option>
            </select>
            <input class="input" v-model="form.destination" placeholder="去向对象" />
          </div>
        </div>

        <div class="preview">
          <div class="preview-head">
            <span>实时预览</span>
            <span class="muted" style="font-size:12px">
              已选 {{ mode==='template' ? selItems.size+'/'+tpl.items.length+' 议题' : selected.size+'/'+blocks.length+' 段' }}
              · {{ selectedChars }} 字
            </span>
          </div>
          <pre class="preview-body">{{ genPreview }}</pre>
        </div>

        <div class="toolbar" style="margin-top:10px">
          <button class="btn primary" :disabled="!canGenerate" @click="generate">
            {{ form.editingId ? "保存修改" : "生成二次纪要" }}
          </button>
          <button class="btn" v-if="form.editingId || form.parent_id" @click="resetGen">取消</button>
          <span class="muted" v-if="!canGenerate" style="font-size:12px">请至少选择内容</span>
          <span class="muted" v-if="form.parent_id" style="font-size:12px">基于上级衍生版本再生成</span>
        </div>
      </div>

      <div class="md-right card card-pad muted" v-else
           style="display:flex;align-items:center;justify-content:center;min-height:300px">
        请选择左侧一份会议纪要开始截取
      </div>
    </div>
    </div>

    <!-- 衍生版本管理 -->
    <div v-else class="md-manage">
      <div class="toolbar">
        <select class="input" v-model="filterSource" style="max-width:300px">
          <option value="all">全部来源</option>
          <option v-for="s in sources" :key="s.doc_id" :value="s.doc_id">{{ s.filename }}</option>
        </select>
        <span class="spacer"></span>
        <span class="muted">共 {{ derivedList.length }} 份衍生版本</span>
      </div>

      <table class="table card" v-if="derivedList.length">
        <thead>
          <tr>
            <th>标题</th><th>版本</th><th>来源纪要</th><th>需求</th><th>去向</th><th>创建</th><th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="d in derivedList" :key="d.id">
            <td class="mid">{{ d.title }}</td>
            <td class="mid">v{{ d.version }}</td>
            <td class="mid muted">
              {{ d.source_title }}
              <span v-if="d.has_source_pdf" class="badge role" title="来源原版 PDF 已归档">原版✓</span>
              <span v-else class="badge danger" title="来源原版 PDF 不存在">原版✗</span>
            </td>
            <td class="mid">{{ d.requirement || "—" }}</td>
            <td class="mid"><span class="badge role">{{ d.destination || "—" }}</span></td>
            <td class="mid muted">{{ (d.created_at || "").slice(0, 10) }}</td>
            <td class="mid">
              <button class="btn sm" @click="openView(d)">查看</button>
              <button class="btn sm" @click="editDerived(d)">编辑</button>
              <button class="btn sm" @click="reDerived(d)">再生成</button>
              <button class="btn sm danger" @click="delDerived(d)">删除</button>
            </td>
          </tr>
        </tbody>
      </table>
      <div v-else class="muted" style="padding:20px">暂无衍生版本，请先在「二次生成」中截取生成。</div>
    </div>

    <!-- 查看弹窗 -->
    <div class="modal-mask" v-if="viewItem" @click.self="viewItem = null">
      <div class="modal" style="width:760px">
        <h3>{{ viewItem.title }} <span class="badge role">v{{ viewItem.version }}</span></h3>
        <div class="info-rows">
          <div class="info-row">
            <span>来源纪要（原版）</span>
            <b>
              <a v-if="lineage && lineage.source" class="link" @click="openDocInBrowse(lineage.source.doc_id)">
                {{ lineage.source.title || viewItem.source_title }} ↗
              </a>
              <template v-else>{{ viewItem.source_title }}</template>
            </b>
          </div>
          <div class="info-row" v-if="lineage && lineage.ancestors && lineage.ancestors.length">
            <span>上级衍生版本</span>
            <b>
              <span v-for="(a, i) in lineage.ancestors" :key="a.id" class="link-chains">
                <a class="link" @click="openDerivedInManage(a.id)">{{ a.title }}</a>
                <span class="muted" v-if="i < lineage.ancestors.length - 1"> → </span>
              </span>
            </b>
          </div>
          <div class="info-row" v-if="lineage && lineage.children && lineage.children.length">
            <span>下游衍生版本</span>
            <b>
              <span v-for="(c, i) in lineage.children" :key="c.id" class="link-chains">
                <a class="link" @click="openDerivedInManage(c.id)">{{ c.title }}</a>
                <span class="muted" v-if="i < lineage.children.length - 1">、</span>
              </span>
            </b>
          </div>
          <div class="info-row"><span>文件需求</span><b>{{ viewItem.requirement || "—" }}</b></div>
          <div class="info-row"><span>文件去向</span><b>{{ viewItem.destination || "—" }}</b></div>
          <div class="info-row"><span>生成时间</span><b class="muted">{{ viewItem.created_at }}</b></div>
          <div class="info-row">
            <span>原版文件</span>
            <b>
              <span v-if="viewItem.has_source_pdf" class="badge role">已归档 · 可预览</span>
              <span v-else class="badge danger">原文件不存在</span>
            </b>
          </div>
        </div>
        <pre class="preview-body" style="max-height:50vh">{{ derivedFullText(viewItem) }}</pre>
        <div class="actions">
          <button class="btn" @click="previewSourcePdf(viewItem)">预览原版 PDF</button>
          <button class="btn primary" @click="previewPdf(viewItem)">预览二次生成 PDF</button>
          <button class="btn" @click="exportPdf(viewItem)">下载二次生成 PDF</button>
          <button class="btn" @click="exportTxt(viewItem)">导出 TXT</button>
          <button class="btn" @click="viewItem = null">关闭</button>
        </div>
      </div>
    </div>

    <PdfModal :show="pdfShow" :url="pdfUrl" :title="pdfTitle" @close="pdfShow = false" />
  </div>
</template>

<style scoped>
.md-tabs { display: inline-flex; gap: 4px; background: #fff; border: 1px solid var(--line); border-radius: 8px; padding: 3px; }
.md-tab { border: none; background: transparent; padding: 5px 14px; border-radius: 6px; cursor: pointer; font-size: 13px; color: var(--muted); }
.md-tab.active { background: var(--primary); color: #fff; font-weight: 600; }

.gen-topbar { display: flex; justify-content: center; align-items: center; gap: 12px; margin-bottom: 10px; }
.gen-topbar .btn.primary { min-width: 160px; }
.md-gen { display: flex; gap: 10px; align-items: flex-start; }
.md-left { width: 260px; flex: 0 0 260px; max-height: calc(100vh - 150px); overflow: auto; }
.md-right { flex: 1; min-width: 0; max-height: calc(100vh - 150px); overflow: auto; }

.src-list { display: flex; flex-direction: column; gap: 5px; margin-top: 4px; }
.gen-head { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 8px; gap: 10px; }

.tpl-meta { background: #f8fafc; border: 1px solid var(--line); border-radius: 8px; padding: 8px 10px; margin-bottom: 8px; }
.tpl-meta .field { margin-bottom: 4px; }
.field-row { display: flex; gap: 8px; }
.field-row .field { flex: 1; min-width: 0; }
.field { margin-bottom: 6px; }
.field label { font-size: 12px; margin-bottom: 2px; display: block; }
.input { padding: 5px 8px; font-size: 13px; }

.blocks { display: flex; flex-direction: column; gap: 5px; }
.block {
  display: flex; gap: 8px; align-items: flex-start; padding: 7px 9px; cursor: pointer;
  border: 1px solid var(--line); border-radius: 7px; background: #fff; position: relative;
}
.block:hover { border-color: var(--primary); }
.block.sel { border-color: var(--primary); background: var(--primary-soft); }
.block input { margin-top: 2px; width: auto; }
.block-txt { flex: 1; white-space: pre-wrap; line-height: 1.55; font-size: 13px; }
.block-idx { position: absolute; top: 4px; right: 6px; font-size: 10px; color: var(--muted); }

.item-title { font-weight: 700; margin-bottom: 2px; white-space: normal; }
.item-body { color: #374151; }
.item-dec { color: #047857; margin-top: 2px; font-size: 12.5px; }

.preview { margin-top: 10px; border: 1px solid var(--line); border-radius: 8px; overflow: hidden; }
.preview-head { display: flex; justify-content: space-between; align-items: center; padding: 6px 10px; background: #f8fafc; border-bottom: 1px solid var(--line); font-weight: 600; font-size: 12.5px; }
.preview-body { margin: 0; padding: 10px; white-space: pre-wrap; line-height: 1.6; font-size: 13px; font-family: inherit; max-height: 300px; overflow: auto; }
.link { color: var(--primary); cursor: pointer; text-decoration: underline; }
.link:hover { color: var(--primary-d); }
.link-chains { white-space: nowrap; }
</style>
