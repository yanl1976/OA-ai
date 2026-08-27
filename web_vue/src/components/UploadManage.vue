<script setup>
import { ref, onMounted, onUnmounted, inject, computed } from "vue";
import { api } from "../api.js";

const notify = inject("notify");
const openDocDetail = inject("openDocDetail");

const items = ref([]);
const total = ref(0);
const page = ref(1);
const pageSize = 20;
const q = ref("");
const cats = ref([]);
const loading = ref(false);

// 后台提取轮询：当列表中存在「未识别」(indexed=0) 的活跃文档时，自动刷新列表，
// 使「重新提取」后状态/字数能即时从「已识别/字数」→「未识别/0」→回填 可视化。
const pollingTimer = ref(null);
function hasExtracting() {
  return items.value.some((d) => !d.deleted && d.indexed === 0);
}
function startPolling() {
  if (pollingTimer.value) return;
  pollingTimer.value = setInterval(async () => {
    if (hasExtracting()) {
      try { await load(); } catch (e) { /* 忽略轮询错误 */ }
    } else {
      stopPolling(); // 全部识别完成，停止轮询
    }
  }, 2500);
}
function stopPolling() {
  if (pollingTimer.value) { clearInterval(pollingTimer.value); pollingTimer.value = null; }
}

// 批量删除
const selected = ref(new Set());
const batchBusy = ref(false);
function toggleSelect(id) {
  const s = new Set(selected.value);
  if (s.has(id)) s.delete(id); else s.add(id);
  selected.value = s;
}
function isSelected(id) { return selected.value.has(id); }
const allChecked = computed({
  get() { return items.value.length > 0 && items.value.every(d => selected.value.has(d.doc_id)); },
  set(v) {
    const s = new Set(selected.value);
    items.value.forEach(d => { if (v) s.add(d.doc_id); else s.delete(d.doc_id); });
    selected.value = s;
  },
});
function clearSelect() { selected.value = new Set(); }

// 当前正在调整归类的行
const reclassId = ref(null);
const reclassCat = ref("");
// 单篇内容提取中标记（doc_id，避免重复点击）
const busyExtractId = ref(null);

// 标签编辑
const tagEditId = ref(null);
const tagEditText = ref("");
function openTagEdit(d) {
  tagEditId.value = d.doc_id;
  tagEditText.value = (d.tags || []).join(", ");
}
async function saveTags() {
  const tags = tagEditText.value.split(",").map((s) => s.trim()).filter(Boolean);
  try {
    await api.setDocTags(tagEditId.value, tags);
    notify("标签已更新", "ok");
    tagEditId.value = null;
    await load();
  } catch (e) { notify(e.message, "err"); }
}

async function loadCats() {
  try {
    const r = await api.categories();
    cats.value = r.categories || [];
  } catch (e) { notify(e.message, "err"); }
}

async function load() {
  loading.value = true;
  try {
    const r = await api.uploads({ q: q.value, page: page.value, page_size: pageSize });
    items.value = r.items || [];
    total.value = r.total || 0;
  } catch (e) {
    notify(e.message, "err");
  } finally {
    loading.value = false;
  }
}

function search() { page.value = 1; load(); }
function goPage(p) { page.value = p; load(); }
const totalPages = () => Math.max(1, Math.ceil(total.value / pageSize));

function openDoc(d) { openDocDetail(d.doc_id); }

function startReclass(d) {
  reclassId.value = d.doc_id;
  reclassCat.value = d.category;
}
async function saveReclass(d) {
  if (!reclassCat.value) { notify("请选择分类", "err"); return; }
  try {
    await api.reclassifyDocument(d.doc_id, reclassCat.value);
    notify("已更新归类", "ok");
    reclassId.value = null;
    await load();
  } catch (e) { notify(e.message, "err"); }
}
function cancelReclass() { reclassId.value = null; }

async function remove(d) {
  if (!confirm("确认将「" + d.filename + "」移入回收站？可在回收站恢复。")) return;
  try {
    await api.deleteUpload(d.doc_id);
    notify("已移入回收站", "ok");
    await load();
  } catch (e) { notify(e.message, "err"); }
}

async function extractOne(d) {
  if (busyExtractId.value === d.doc_id) return;
  busyExtractId.value = d.doc_id;
  try {
    const r = await api.initExtractOne(d.doc_id);
    notify((r.note || "已提交单篇提取，稍候自动刷新") + "", "ok");
    await load();
  } catch (e) {
    notify(e.message, "err");
  } finally {
    busyExtractId.value = null;
  }
}

async function batchRemove() {
  const ids = Array.from(selected.value);
  if (!ids.length) { notify("请先勾选要删除的文件", "err"); return; }
  if (!confirm(`确认将选中的 ${ids.length} 个文件移入回收站？可在回收站恢复。`)) return;
  batchBusy.value = true;
  try {
    const r = await api.deleteUploadsBatch(ids);
    notify(`已删除 ${r.deleted || ids.length} 个文件` + (r.not_found && r.not_found.length ? `，${r.not_found.length} 个未找到` : ""), "ok");
    clearSelect();
    await load();
  } catch (e) { notify(e.message, "err"); }
  finally { batchBusy.value = false; }
}

onMounted(() => { loadCats(); load(); startPolling(); });
onUnmounted(() => stopPolling());
</script>

<template>
  <h2>上传文件管理</h2>
  <p class="muted">查看、调整归类或删除已上传的文档（仅作用于上传文档，不影响原始文档库）。</p>

  <div class="toolbar">
    <input class="input" v-model="q" placeholder="搜索文件名 / 分类…" style="max-width:300px"
           @keyup.enter="search" />
    <button class="btn" @click="search">搜索</button>
    <button class="btn" @click="load">刷新</button>
    <button class="btn danger" :disabled="!selected.size || batchBusy" @click="batchRemove">
      批量删除{{ selected.size ? `（${selected.size}）` : "" }}
    </button>
    <span class="spacer"></span>
    <span class="muted">共 {{ total }} 个上传文件</span>
  </div>

  <div class="card card-pad">
    <table class="table">
      <thead>
        <tr><th style="width:40px"><input type="checkbox" v-model="allChecked" title="全选本页" /></th><th>文件名</th><th>归类</th><th>标签</th><th>年代</th><th>字数</th><th>原文件</th><th>识别状态</th><th>上传时间</th><th>操作</th></tr>
      </thead>
      <tbody>
        <tr v-for="d in items" :key="d.doc_id">
          <td class="mid"><input type="checkbox" :checked="isSelected(d.doc_id)" @change="toggleSelect(d.doc_id)" /></td>
          <td class="mid">
            <a class="link" @click="openDoc(d)">{{ d.filename }}</a>
            <span class="badge" style="margin-left:6px">上传</span>
          </td>
          <td class="mid">
            <template v-if="reclassId === d.doc_id">
              <select class="input" v-model="reclassCat" style="max-width:180px">
                <option v-for="c in cats" :key="c.id" :value="c.name">{{ c.name }}</option>
              </select>
            </template>
            <template v-else>{{ d.category }}</template>
          </td>
          <td class="mid">
            <span v-for="t in (d.tags || [])" :key="t" class="mini-tag">{{ t }}</span>
            <span v-if="!(d.tags && d.tags.length)" class="muted">—</span>
          </td>
          <td class="mid muted">{{ d.year || "—" }}</td>
          <td class="mid">{{ d.chars }}</td>
          <td class="mid">
            <span v-if="d.stored" class="badge role" :title="d.storage_path">已归档</span>
            <span v-else class="badge danger" title="原始二进制文件缺失，预览将回退为文本重排 PDF">原文件不存在</span>
          </td>
          <td class="mid">
            <span v-if="d.indexed" class="badge role" title="后台已提取文本并入索引，可检索">✅ 已识别</span>
            <span v-else class="badge warn" title="后台正在提取文本/建索引，稍候即可检索">🔄 识别中</span>
          </td>
          <td class="mid muted">{{ (d.created_at || "").slice(0, 19).replace("T", " ") }}</td>
          <td class="mid">
            <template v-if="reclassId === d.doc_id">
              <button class="btn sm primary" @click="saveReclass(d)">保存</button>
              <button class="btn sm" @click="cancelReclass">取消</button>
            </template>
            <template v-else>
              <button class="btn sm" @click="startReclass(d)">调整归类</button>
              <button class="btn sm" @click="openTagEdit(d)">标签</button>
              <button class="btn sm" :disabled="busyExtractId === d.doc_id" @click="extractOne(d)">
                {{ busyExtractId === d.doc_id ? '提取中…' : '内容提取' }}
              </button>
              <button class="btn sm danger" @click="remove(d)">删除</button>
            </template>
          </td>
        </tr>
        <tr v-if="!items.length"><td colspan="9" class="loading">暂无上传文件</td></tr>
      </tbody>
    </table>

    <div class="pager toolbar" v-if="totalPages() > 1">
      <button class="btn sm" :disabled="page <= 1" @click="goPage(page - 1)">上一页</button>
      <span class="muted">第 {{ page }} / {{ totalPages() }} 页</span>
      <button class="btn sm" :disabled="page >= totalPages()" @click="goPage(page + 1)">下一页</button>
    </div>
  </div>

  <div class="modal-mask" v-if="tagEditId" @click.self="tagEditId = null">
    <div class="modal">
      <h3>编辑标签</h3>
      <p class="muted">多个标签用逗号分隔。</p>
      <input class="input full" v-model="tagEditText" placeholder="标签1, 标签2" />
      <div class="modal-acts">
        <button class="btn sm" @click="tagEditId = null">取消</button>
        <button class="btn sm primary" @click="saveTags">保存</button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.link { color: var(--primary); cursor: pointer; text-decoration: underline; }
.link:hover { color: var(--primary-d); }
.mini-tag { display: inline-block; background: #f0f4f8; color: #555; border-radius: 4px;
  padding: 1px 6px; font-size: 11px; margin: 0 4px 2px 0; }
.modal-mask { position: fixed; inset: 0; background: rgba(0,0,0,.35); display: flex;
  align-items: center; justify-content: center; z-index: 100; }
.modal { background: #fff; border-radius: 10px; padding: 20px 24px; width: 360px; box-shadow: 0 8px 30px rgba(0,0,0,.2); }
.modal h3 { margin: 0 0 6px; }
.modal .input.full { width: 100%; box-sizing: border-box; padding: 8px 10px; border: 1px solid #dcdcdc; border-radius: 6px; }
.modal-acts { display: flex; justify-content: flex-end; gap: 10px; margin-top: 14px; }
</style>
