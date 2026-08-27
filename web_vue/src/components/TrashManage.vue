<script setup>
import { ref, onMounted } from "vue";
import { api } from "../api.js";
import { inject } from "vue";

const notify = inject("notify");
const items = ref([]);
const total = ref(0);
const q = ref("");
const page = ref(1);
const page_size = 20;
const selected = ref([]);

async function load() {
  try {
    const r = await api.listTrash({ q: q.value, page: page.value, page_size });
    items.value = r.items || [];
    total.value = r.total || 0;
    selected.value = [];
  } catch (e) {
    notify("加载回收站失败：" + (e.response?.data?.error || e.message), "err");
  }
}

function allSelected() {
  return items.value.length && selected.value.length === items.value.length;
}
function toggleAll(e) {
  selected.value = e.target.checked ? items.value.map((x) => x.doc_id) : [];
}

async function restore(id) {
  try {
    await api.restoreUpload(id);
    notify("已恢复：" + id, "ok");
    load();
  } catch (e) {
    notify("恢复失败：" + (e.response?.data?.error || e.message), "err");
  }
}

async function purge(id) {
  if (!confirm("彻底删除后不可恢复，确认？")) return;
  try {
    await api.purgeUpload(id);
    notify("已彻底删除", "ok");
    load();
  } catch (e) {
    notify("删除失败：" + (e.response?.data?.error || e.message), "err");
  }
}

async function batchPurge() {
  if (!selected.value.length) return;
  if (!confirm("确认彻底删除选中的 " + selected.value.length + " 项？不可恢复。")) return;
  try {
    await api.purgeUploadsBatch(selected.value);
    notify("已彻底删除选中项", "ok");
    load();
  } catch (e) {
    notify("删除失败：" + (e.response?.data?.error || e.message), "err");
  }
}

onMounted(load);
</script>

<template>
  <div class="trash">
    <h2 class="t-title">回收站 <span class="muted">（软删除文档，可恢复）</span></h2>

    <div class="bar">
      <input v-model="q" class="fz" placeholder="搜索文件名/分类" @keyup.enter="load" />
      <button class="btn" @click="load">查询</button>
      <button class="btn danger" :disabled="!selected.length" @click="batchPurge">
        彻底删除（{{ selected.length }}）
      </button>
      <span class="muted">共 {{ total }} 项</span>
    </div>

    <div v-if="!items.length" class="empty">回收站为空</div>
    <table v-else class="tbl">
      <thead>
        <tr>
          <th><input type="checkbox" :checked="allSelected()" @change="toggleAll" /></th>
          <th>文件名</th><th>分类</th><th>删除时间</th><th>操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="d in items" :key="d.doc_id">
          <td><input type="checkbox" :value="d.doc_id" v-model="selected" /></td>
          <td>{{ d.filename }}</td>
          <td>{{ d.category }}</td>
          <td>{{ d.deleted_at }}</td>
          <td>
            <button class="link" @click="restore(d.doc_id)">恢复</button>
            <button class="link danger" @click="purge(d.doc_id)">彻底删除</button>
          </td>
        </tr>
      </tbody>
    </table>
  </div>
</template>

<style scoped>
.trash { padding: 8px 4px; }
.t-title { margin: 4px 0 14px; font-size: 20px; }
.muted { color: #999; font-size: 13px; font-weight: 400; }
.bar { display: flex; gap: 8px; align-items: center; margin-bottom: 12px; }
.fz { border: 1px solid #dcdcdc; border-radius: 6px; padding: 6px 10px; }
.btn { border: 1px solid #2b6cb0; background: #2b6cb0; color: #fff; border-radius: 6px;
  padding: 6px 14px; cursor: pointer; }
.btn:disabled { opacity: .5; cursor: not-allowed; }
.btn.danger { background: #c0392b; border-color: #c0392b; }
.tbl { width: 100%; border-collapse: collapse; background: #fff; }
.tbl th, .tbl td { text-align: left; padding: 9px 10px; border-bottom: 1px solid #f0f0f0; }
.link { background: none; border: none; color: #2b6cb0; cursor: pointer; margin-right: 10px; }
.link.danger { color: #c0392b; }
.empty { color: #aaa; padding: 30px 0; text-align: center; }
</style>
