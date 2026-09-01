<script setup>
import { ref, onMounted, watch } from "vue";
import { api } from "../api.js";
import { inject } from "vue";
import DocxModal from "./DocxModal.vue";

const props = defineProps({ docId: { type: String, default: "" } });
const notify = inject("notify");
const doc = ref(null);
const loading = ref(false);
const editing = ref(false);
const editText = ref("");
const tagInput = ref("");

async function load() {
  if (!props.docId) { doc.value = null; return; }
  loading.value = true;
  try {
    const r = await api.get("/api/kb/document?doc_id=" + encodeURIComponent(props.docId));
    doc.value = r.document;
    editText.value = r.document.text || "";
  } catch (e) {
    notify("加载文档失败：" + (e.response?.data?.error || e.message), "err");
    doc.value = null;
  } finally {
    loading.value = false;
  }
}

function pdfUrl() {
  return api.docPdfUrl(props.docId, true);
}
// docx 原版版面预览（浏览器内直接渲染，不转 PDF）：复用同一下载端点（按 ext 回传二进制）
const docxShow = ref(false);
const docxUrl = ref("");
function isDocx() {
  const ext = (doc.value && doc.value.ext) || "";
  return ext === ".docx" || ext === ".doc" || (doc.value && /word/.test(doc.value.mimetype || ""));
}
function openDocx() {
  docxUrl.value = api.docPdfUrl(props.docId, true);
  docxShow.value = true;
}
function closeDocx() {
  docxShow.value = false;
}
function downloadUrl() {
  return "/api/kb/document/" + encodeURIComponent(props.docId) + "/pdf?download=1";
}

async function saveEdit() {
  try {
    await api.updateDocText(props.docId, editText.value);
    notify("已保存并更新索引", "ok");
    editing.value = false;
    load();
  } catch (e) {
    notify("保存失败：" + (e.response?.data?.error || e.message), "err");
  }
}

function addTag() {
  const t = tagInput.value.trim();
  if (!t || !doc.value) return;
  const set = new Set(doc.value.tags || []);
  set.add(t);
  applyTags([...set]);
  tagInput.value = "";
}
function removeTag(t) {
  if (!doc.value) return;
  applyTags((doc.value.tags || []).filter((x) => x !== t));
}
async function applyTags(tags) {
  try {
    await api.setDocTags(props.docId, tags);
    doc.value.tags = tags;
    notify("标签已更新", "ok");
  } catch (e) {
    notify("标签更新失败：" + (e.response?.data?.error || e.message), "err");
  }
}

onMounted(load);
watch(() => props.docId, load);
</script>

<template>
  <div class="docd">
    <div v-if="!docId" class="empty">请选择文档查看详情</div>
    <div v-else-if="loading" class="empty">加载中…</div>
    <div v-else-if="!doc" class="empty">文档不存在或已被删除</div>
    <div v-else class="detail">
      <div class="head">
        <h2>{{ doc.filename }}</h2>
        <div class="meta">
          <span>分类：{{ doc.category }}</span>
          <span v-if="doc.year">年份：{{ doc.year }}</span>
          <span>字符数：{{ doc.char_count }}</span>
          <span v-if="doc.updated_at">更新：{{ doc.updated_at }}</span>
        </div>
      </div>

      <div class="tags-bar">
        <span class="tl">标签：</span>
        <span v-for="t in doc.tags" :key="t" class="tag">{{ t }}
          <a @click="removeTag(t)">×</a></span>
        <input v-model="tagInput" class="ti" placeholder="加标签后回车" @keyup.enter="addTag" />
      </div>

      <div class="acts">
        <a v-if="doc.can_download" class="lnk" :href="downloadUrl()" target="_blank">⬇ 下载原文件</a>
        <span v-else class="lnk disabled" title="当前账号无该分类的下载权限">⬇ 下载受限</span>
        <button v-if="doc.mimetype === 'application/pdf'" class="lnk" @click="notify('请在浏览中点击 PDF 预览','')">
          预览 PDF
        </button>
        <button class="lnk" @click="editing = !editing">{{ editing ? "取消编辑" : "✎ 在线编辑" }}</button>
      </div>

      <div v-if="editing" class="editor">
        <textarea v-model="editText" class="etx"></textarea>
        <button class="btn" @click="saveEdit">保存</button>
      </div>
      <div v-else class="preview" @contextmenu.prevent @copy.prevent @cut.prevent @selectstart.prevent>
        <pre>{{ doc.text }}</pre>
      </div>
    </div>
  </div>
</template>

<style scoped>
.docd { padding: 8px 4px; }
.empty { color: #aaa; padding: 40px 0; text-align: center; }
.head h2 { margin: 4px 0 8px; font-size: 19px; }
.meta { color: #888; font-size: 13px; display: flex; gap: 16px; flex-wrap: wrap; margin-bottom: 10px; }
.tags-bar { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-bottom: 10px; }
.tl { color: #888; font-size: 13px; }
.tag { background: #eef4fb; color: #2b6cb0; border-radius: 14px; padding: 3px 10px; font-size: 13px; }
.tag a { cursor: pointer; margin-left: 4px; color: #999; }
.ti { border: 1px solid #dcdcdc; border-radius: 14px; padding: 4px 10px; font-size: 13px; }
.acts { display: flex; gap: 14px; margin-bottom: 12px; }
.lnk { background: none; border: none; color: #2b6cb0; cursor: pointer; font-size: 14px; text-decoration: none; }
.editor .etx { width: 100%; height: 460px; border: 1px solid #dcdcdc; border-radius: 8px;
  padding: 10px; font-family: monospace; font-size: 13px; line-height: 1.5; }
.btn { border: 1px solid #2b6cb0; background: #2b6cb0; color: #fff; border-radius: 6px;
  padding: 6px 18px; cursor: pointer; margin-top: 8px; }
.preview { -webkit-user-select: none; -moz-user-select: none; -ms-user-select: none; user-select: none; }
.preview pre { background: #fafbfc; border: 1px solid #eef0f2; border-radius: 8px;
  padding: 14px; white-space: pre-wrap; word-break: break-word; max-height: 620px; overflow: auto;
  font-size: 13px; line-height: 1.6; }
</style>
