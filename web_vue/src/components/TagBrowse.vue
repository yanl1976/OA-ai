<script setup>
import { ref, onMounted, watch } from "vue";
import { api } from "../api.js";
import { inject } from "vue";

const notify = inject("notify");
const openDocDetail = inject("openDocDetail");
const tags = ref([]);
const activeTag = ref("");
const docs = ref([]);
const q = ref("");
const loading = ref(false);

async function loadTags() {
  try {
    const r = await api.listTags();
    tags.value = r.tags || [];
  } catch (e) {
    notify("加载标签失败：" + (e.response?.data?.error || e.message), "err");
  }
}

async function loadDocs(tag) {
  if (!tag) { docs.value = []; return; }
  loading.value = true;
  try {
    const r = await api.docsByTag(tag, { q: q.value, page: 1, page_size: 200 });
    docs.value = r.items || [];
  } catch (e) {
    notify("加载文档失败：" + (e.response?.data?.error || e.message), "err");
  } finally {
    loading.value = false;
  }
}

function selectTag(t) {
  activeTag.value = t;
  loadDocs(t);
}

function goBrowse() {
  window.dispatchEvent(new CustomEvent("nav", { detail: "KbBrowse" }));
}

onMounted(loadTags);
watch(q, () => { if (activeTag.value) loadDocs(activeTag.value); });
</script>

<template>
  <div class="tagb">
    <h2 class="t-title">标签浏览</h2>

    <div class="tag-bar">
      <span
        v-for="t in tags"
        :key="t.tag"
        class="tag-chip"
        :class="{ active: t.tag === activeTag }"
        @click="selectTag(t.tag)"
      >{{ t.tag }} <em>{{ t.count }}</em></span>
      <span v-if="!tags.length" class="empty">暂无标签，可在上传管理中为文档打标签。</span>
    </div>

    <div v-if="activeTag" class="doc-area">
      <div class="doc-head">
        标签「{{ activeTag }}」下的文档（{{ docs.length }}）
        <input v-model="q" class="fz" placeholder="过滤文件名/分类" />
      </div>
      <div v-if="!docs.length" class="empty">无匹配文档</div>
      <table v-else class="doc-tbl">
        <thead><tr><th>文件名</th><th>分类</th><th>标签</th></tr></thead>
        <tbody>
          <tr v-for="d in docs" :key="d.doc_id" @click="openDocDetail(d.doc_id)">
            <td>{{ d.filename }}</td>
            <td>{{ d.category }}</td>
            <td><span v-for="t in d.tags" :key="t" class="mini-tag">{{ t }}</span></td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>

<style scoped>
.tagb { padding: 8px 4px; }
.t-title { margin: 4px 0 14px; font-size: 20px; }
.tag-bar { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 16px; }
.tag-chip { cursor: pointer; background: #eef4fb; color: #2b6cb0; border: 1px solid #d6e4f5;
  border-radius: 16px; padding: 4px 12px; font-size: 13px; }
.tag-chip.active { background: #2b6cb0; color: #fff; border-color: #2b6cb0; }
.tag-chip em { font-style: normal; opacity: .7; margin-left: 3px; }
.doc-area { background: #fff; border: 1px solid #e6e8eb; border-radius: 10px; padding: 12px 14px; }
.doc-head { font-weight: 600; margin-bottom: 10px; display: flex; justify-content: space-between; align-items: center; }
.fz { border: 1px solid #dcdcdc; border-radius: 6px; padding: 5px 8px; font-size: 13px; }
.doc-tbl { width: 100%; border-collapse: collapse; }
.doc-tbl th, .doc-tbl td { text-align: left; padding: 8px 10px; border-bottom: 1px solid #f0f0f0; }
.mini-tag { display: inline-block; background: #f0f4f8; color: #555; border-radius: 4px;
  padding: 1px 6px; font-size: 11px; margin-right: 4px; }
.empty { color: #aaa; padding: 16px 0; }
</style>
