<script setup>
import { ref, inject, nextTick, computed } from "vue";
import { api } from "../api.js";

const notify = inject("notify");
const kw = ref("");
const results = ref([]);
const searching = ref(false);
const done = ref(false);

// 选中文件后右侧展示详情
const selected = ref(null); // 选中的检索结果项
const docText = ref(""); // 选中文档全文
const loadingDoc = ref(false);
const occurrences = ref([]); // 关键词在全文中所有出现位置
const active = ref(0); // 当前定位到的关键词序号
const activeRegion = ref(0); // 当前定位到的命中区域序号
const regions = ref([]); // 命中内容区域 [[s,e],...]
const renderedHtml = ref("");

// 关键词拆分。
// 优先使用【后端返回的分词 terms】：后端有 jieba 分词能力，而前端没有。
// 若前端只拿整个查询串去 indexOf，则「安全生产责任制度」这类在原文中实际
// 写作「安全生产责任制」的长查询会永远匹配不到，用户就会看到
// 「正文未精确匹配关键词」。按后端分词匹配即可正确标出命中位置。
// 无后端 terms 时（兼容旧接口）退化为按分隔符 + 整串兜底。
function splitTerms(q, backendTerms) {
  if (Array.isArray(backendTerms) && backendTerms.length) {
    return backendTerms
      .map((t) => String(t || "").trim().toLowerCase())
      .filter((t) => t.length >= 1)
      .filter((t, i, arr) => arr.indexOf(t) === i)
      .sort((a, b) => b.length - a.length); // 长词优先
  }
  const raw = (q || "").trim().toLowerCase();
  const terms = [];
  if (!raw) return terms;
  raw.split(/[\s,，。、;；:：.!?！？()（）"'《》<>\[\]【】/\\|]+/).forEach((t) => {
    if (t.length >= 2 && !terms.includes(t)) terms.push(t);
  });
  if (!terms.length && raw.length >= 1) terms.push(raw);
  return terms;
}

function escapeHtml(s) {
  return (s || "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

// 在全文 text 中找出所有查询关键词的出现位置（大小写不敏感，去重叠）
// backendTerms：后端 jieba 分词结果，命中率远高于整串匹配（见 splitTerms 说明）
function computeOccurrences(text, query, backendTerms) {
  const terms = splitTerms(query, backendTerms);
  const lower = text.toLowerCase();
  const hits = [];
  const seen = new Set();
  for (const t of terms) {
    let i = lower.indexOf(t);
    while (i >= 0) {
      const key = i + ":" + (i + t.length);
      if (!seen.has(key)) {
        seen.add(key);
        hits.push({ start: i, end: i + t.length });
      }
      i = lower.indexOf(t, i + t.length);
    }
  }
  hits.sort((a, b) => a.start - b.start);
  return hits;
}

// 整篇渲染：关键词 <mark class="kw"> 强高亮，命中内容区域 <span class="region"> 底色高亮
function renderFull(text, kwHits, regs) {
  if (!text) {
    return '<span class="muted">（无正文内容）</span>';
  }
  // 合并重叠区域区间
  const merged = [];
  for (const [s, e] of [...regs].sort((a, b) => a[0] - b[0])) {
    const last = merged[merged.length - 1];
    if (last && s <= last[1]) last[1] = Math.max(last[1], e);
    else merged.push([s, e]);
  }
  // 收集边界点
  const pts = new Set([0, text.length]);
  for (const h of kwHits) {
    pts.add(h.start);
    pts.add(h.end);
  }
  for (const [s, e] of merged) {
    pts.add(Math.max(0, s));
    pts.add(Math.min(text.length, e));
  }
  const bounds = [...pts].sort((a, b) => a - b);
  let html = "";
  for (let i = 0; i < bounds.length - 1; i++) {
    const a = bounds[i];
    const b = bounds[i + 1];
    if (a >= b) continue;
    const inKw = kwHits.some((h) => a >= h.start && b <= h.end);
    const inReg = merged.some(([s, e]) => a >= Math.max(0, s) && b <= Math.min(text.length, e));
    let part = escapeHtml(text.slice(a, b));
    if (inKw) part = '<mark class="kw">' + part + "</mark>";
    if (inReg) part = '<span class="region">' + part + "</span>";
    html += part;
  }
  return html;
}

// 渲染整篇，并给"当前命中"加 id 用于滚动定位
function renderWithActive() {
  const text = docText.value;
  const hits = occurrences.value;
  if (!text) {
    renderedHtml.value = '<span class="muted">（无正文内容）</span>';
    return;
  }
  const merged = [];
  for (const [s, e] of [...regions.value].sort((a, b) => a[0] - b[0])) {
    const last = merged[merged.length - 1];
    if (last && s <= last[1]) last[1] = Math.max(last[1], e);
    else merged.push([s, e]);
  }
  const pts = new Set([0, text.length]);
  for (const h of hits) {
    pts.add(h.start);
    pts.add(h.end);
  }
  for (const [s, e] of merged) {
    pts.add(Math.max(0, s));
    pts.add(Math.min(text.length, e));
  }
  const bounds = [...pts].sort((a, b) => a - b);
  let html = "";
  for (let i = 0; i < bounds.length - 1; i++) {
    const a = bounds[i];
    const b = bounds[i + 1];
    if (a >= b) continue;
    const activeHit = hits[active.value];
    const isActive = activeHit && a >= activeHit.start && b <= activeHit.end;
    const inKw = hits.some((h) => a >= h.start && b <= h.end);
    const inReg = merged.some(([s, e]) => a >= Math.max(0, s) && b <= Math.min(text.length, e));
    let part = escapeHtml(text.slice(a, b));
    if (inKw) {
      part = isActive
        ? '<mark id="kb-hit" class="kw active">' + part + "</mark>"
        : '<mark class="kw">' + part + "</mark>";
    }
    if (inReg) part = '<span class="region">' + part + "</span>";
    html += part;
  }
  renderedHtml.value = html;
}

function scrollToHit() {
  const el = document.getElementById("kb-hit");
  if (el && el.scrollIntoView) el.scrollIntoView({ block: "center", behavior: "smooth" });
}

async function doSearch() {
  if (!kw.value.trim()) return;
  searching.value = true;
  done.value = false;
  selected.value = null;
  docText.value = "";
  renderedHtml.value = "";
  try {
    const r = await api.search(kw.value.trim(), 50);
    results.value = r.results || [];
    done.value = true;
    // 默认选中第一个结果并加载内容
    if (results.value.length) await selectResult(results.value[0]);
  } catch (e) {
    notify(e.message, "err");
  } finally {
    searching.value = false;
  }
}

async function selectResult(res) {
  selected.value = res;
  loadingDoc.value = true;
  try {
    const r = await api.document(res.doc_id);
    const doc = r.document || {};
    const text = doc.text || doc.full_text || "";
    docText.value = text;
    occurrences.value = computeOccurrences(text, kw.value, res.terms);
    regions.value = res.regions || [];
    // 初始定位到离检索命中点(char_start)最近的那个关键词
    let a = 0;
    if (occurrences.value.length && typeof res.char_start === "number") {
      let best = Infinity;
      occurrences.value.forEach((h, idx) => {
        const d = Math.abs(h.start - (res.char_start | 0));
        if (d < best) {
          best = d;
          a = idx;
        }
      });
    }
    active.value = a;
    renderWithActive();
    await nextTick();
    scrollToHit();
  } catch (e) {
    notify(e.message, "err");
  } finally {
    loadingDoc.value = false;
  }
}

function gotoOccurrence(delta) {
  const L = occurrences.value;
  if (!L.length) {
    // 无精确关键词：在命中区域间跳转
    if (regions.value.length > 1) {
      activeRegion.value = (activeRegion.value + delta + regions.value.length) % regions.value.length;
      scrollToRegion();
    }
    return;
  }
  active.value = (active.value + delta + L.length) % L.length;
  renderWithActive();
  nextTick(scrollToHit);
}

function scrollToRegion() {
  const reg = regions.value[activeRegion.value];
  if (!reg) return;
  // 定位到区域中点附近的关键词；没有则滚动到该区域起点
  const mid = (reg[0] + reg[1]) / 2;
  let target = null;
  let best = Infinity;
  for (const h of occurrences.value) {
    const d = Math.abs((h.start + h.end) / 2 - mid);
    if (d < best) {
      best = d;
      target = h;
    }
  }
  if (target) {
    active.value = occurrences.value.indexOf(target);
    renderWithActive();
    nextTick(scrollToHit);
  }
}

const hitInfo = computed(() => {
  if (occurrences.value.length)
    return `关键词命中 ${occurrences.value.length} 处，当前第 ${active.value + 1} 处`;
  if (regions.value.length)
    return `正文未精确匹配关键词，已标出 ${regions.value.length} 处相关段落`;
  return "无匹配内容";
});

const selectedLabel = computed(() => {
  const s = selected.value;
  if (!s) return "";
  return s.label || s.filename || s.doc_id;
});
</script>

<template>
  <h2>全文检索（BM25 + 向量混合）</h2>
  <div class="toolbar">
    <input
      class="input"
      style="max-width: 460px"
      v-model="kw"
      placeholder="输入关键词或语义描述，回车检索"
      @keyup.enter="doSearch"
    />
    <button class="btn primary" :disabled="searching" @click="doSearch">
      {{ searching ? "检索中…" : "检索" }}
    </button>
    <span class="legend">
      <i class="lg kw"></i>关键词
      <i class="lg reg"></i>命中内容
    </span>
  </div>

  <div v-if="searching" class="loading">检索中…</div>
  <div v-else-if="done && !results.length" class="loading">未找到相关文档</div>

  <div v-else class="search-split">
    <!-- 左侧：符合要求的文件列表 -->
    <div class="pane left">
      <div class="pane-title">匹配文件（{{ results.length }}）</div>
      <div class="file-list">
        <div
          v-for="(r, i) in results"
          :key="i"
          class="file-item"
          :class="{ active: selected && selected.doc_id === r.doc_id }"
          @click="selectResult(r)"
        >
          <div class="t">
            <span class="rk">#{{ i + 1 }}</span>
            {{ r.label || r.filename }}
          </div>
          <div class="m">
            {{ r.category }}
            <span v-if="r.year" class="badge">{{ r.year }}年</span>
          </div>
          <div class="m dim">相关度 {{ (r.score || 0).toFixed(3) }}</div>
          <div class="snippet" v-html="r.snippet"></div>
        </div>
      </div>
    </div>

    <!-- 右侧：文件内容 + 高亮 -->
    <div class="pane right">
      <template v-if="selected">
        <div class="pane-title doc-head">
          <strong>📄 {{ selectedLabel }}</strong>
          <div class="loc-meta">
            <span>{{ selected.category }}</span>
            <span v-if="selected.year">· {{ selected.year }}年</span>
            <span class="dot">·</span>{{ hitInfo }}
          </div>
        </div>
        <div class="loc-nav" v-if="occurrences.length > 1 || regions.length > 1">
          <button class="btn sm" @click="gotoOccurrence(-1)">‹ 上一处</button>
          <button class="btn sm" @click="gotoOccurrence(1)">下一处 ›</button>
        </div>
        <div v-if="loadingDoc" class="loading">加载文档…</div>
        <div v-else class="doc-view" v-html="renderedHtml"></div>
      </template>
      <div v-else class="empty">请选择左侧文件查看内容</div>
    </div>
  </div>
</template>

<style scoped>
.search-split {
  display: flex;
  gap: 14px;
  align-items: stretch;
  margin-top: 6px;
}
.pane {
  border: 1px solid var(--line);
  border-radius: 10px;
  background: #fff;
  display: flex;
  flex-direction: column;
  min-height: 62vh;
  max-height: 70vh;
}
.pane.left {
  width: 360px;
  flex: 0 0 360px;
}
.pane.right {
  flex: 1;
  min-width: 0;
}
.pane-title {
  padding: 10px 14px;
  font-weight: 600;
  border-bottom: 1px solid var(--line);
  background: #f7f9fc;
  border-radius: 10px 10px 0 0;
  font-size: 14px;
}
.doc-head {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.file-list {
  overflow: auto;
  padding: 6px;
  flex: 1;
}
.file-item {
  padding: 10px 12px;
  border: 1px solid transparent;
  border-radius: 8px;
  cursor: pointer;
  margin-bottom: 4px;
}
.file-item:hover {
  background: #f4f7ff;
  border-color: var(--primary-soft);
}
.file-item.active {
  background: #eef4ff;
  border-color: var(--primary);
}
.file-item .t {
  font-weight: 600;
  font-size: 14px;
  word-break: break-all;
}
.file-item .m {
  font-size: 12px;
  color: var(--primary);
  margin-top: 2px;
}
.file-item .m.dim {
  color: var(--muted);
}
.file-item .snippet {
  font-size: 12px;
  color: var(--muted);
  margin-top: 6px;
  line-height: 1.6;
  max-height: 76px;
  overflow: hidden;
}
.doc-view {
  white-space: pre-wrap;
  word-break: break-word;
  font-size: 13px;
  line-height: 1.85;
  overflow: auto;
  padding: 14px 16px;
  flex: 1;
  background: #fcfdff;
}
.empty {
  margin: auto;
  color: var(--muted);
}
.loc-meta {
  font-size: 12px;
  color: var(--muted);
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  align-items: center;
}
.loc-meta .dot {
  opacity: 0.5;
}
.loc-nav {
  display: flex;
  gap: 8px;
  padding: 8px 14px 0;
}
.rk {
  display: inline-block;
  min-width: 22px;
  color: var(--primary);
  font-weight: 700;
}
.badge {
  font-size: 11px;
  background: var(--primary-soft);
  color: var(--primary);
  padding: 1px 7px;
  border-radius: 999px;
  margin-left: 6px;
}
.legend {
  font-size: 12px;
  color: var(--muted);
  display: inline-flex;
  align-items: center;
  gap: 6px;
  margin-left: 4px;
}
.legend .lg {
  width: 12px;
  height: 12px;
  border-radius: 3px;
  display: inline-block;
  margin-right: 2px;
}
.legend .lg.kw {
  background: #fde68a;
  border: 1px solid #f59e0b;
}
.legend .lg.reg {
  background: #dbeafe;
  border: 1px solid #93c5fd;
}
.snippet :deep(mark),
.doc-view :deep(mark),
.snippet mark,
.doc-view mark {
  background: #fde68a;
  color: #7c2d12;
  padding: 0 2px;
  border-radius: 3px;
}
.doc-view :deep(mark.kw.active),
.doc-view mark.kw.active {
  background: #f59e0b;
  color: #fff;
  box-shadow: 0 0 0 2px rgba(245, 158, 11, 0.3);
}
.doc-view :deep(span.region),
.doc-view span.region {
  background: rgba(59, 130, 246, 0.12);
  border-radius: 3px;
  box-shadow: 0 0 0 1px rgba(59, 130, 246, 0.25);
}
</style>
