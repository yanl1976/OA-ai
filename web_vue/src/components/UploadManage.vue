<script setup>
import { ref, onMounted, inject, computed } from "vue";
import { api } from "../api.js";

const notify = inject("notify");
const openDocInBrowse = inject("openDocInBrowse");

const items = ref([]);
const total = ref(0);
const page = ref(1);
const pageSize = 20;
const q = ref("");
const cats = ref([]);
const loading = ref(false);

// 当前正在调整归类的行
const reclassId = ref(null);
const reclassCat = ref("");

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

function openDoc(d) { openDocInBrowse(d.doc_id); }

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
  if (!confirm("确认删除上传文件「" + d.filename + "」？此操作不可撤销。")) return;
  try {
    await api.deleteUpload(d.doc_id);
    notify("已删除", "ok");
    await load();
  } catch (e) { notify(e.message, "err"); }
}

onMounted(() => { loadCats(); load(); });
</script>

<template>
  <h2>上传文件管理</h2>
  <p class="muted">查看、调整归类或删除已上传的文档（仅作用于上传文档，不影响原始文档库）。</p>

  <div class="toolbar">
    <input class="input" v-model="q" placeholder="搜索文件名 / 分类…" style="max-width:300px"
           @keyup.enter="search" />
    <button class="btn" @click="search">搜索</button>
    <button class="btn" @click="load">刷新</button>
    <span class="spacer"></span>
    <span class="muted">共 {{ total }} 个上传文件</span>
  </div>

  <div class="card card-pad">
    <table class="table">
      <thead>
        <tr><th>文件名</th><th>归类</th><th>年代</th><th>字数</th><th>原文件</th><th>上传时间</th><th>操作</th></tr>
      </thead>
      <tbody>
        <tr v-for="d in items" :key="d.doc_id">
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
          <td class="mid muted">{{ d.year || "—" }}</td>
          <td class="mid">{{ d.chars }}</td>
          <td class="mid">
            <span v-if="d.stored" class="badge role" :title="d.storage_path">已归档</span>
            <span v-else class="badge danger" title="原始二进制文件缺失，预览将回退为文本重排 PDF">原文件不存在</span>
          </td>
          <td class="mid muted">{{ (d.created_at || "").slice(0, 19).replace("T", " ") }}</td>
          <td class="mid">
            <template v-if="reclassId === d.doc_id">
              <button class="btn sm primary" @click="saveReclass(d)">保存</button>
              <button class="btn sm" @click="cancelReclass">取消</button>
            </template>
            <template v-else>
              <button class="btn sm" @click="startReclass(d)">调整归类</button>
              <button class="btn sm danger" @click="remove(d)">删除</button>
            </template>
          </td>
        </tr>
        <tr v-if="!items.length"><td colspan="7" class="loading">暂无上传文件</td></tr>
      </tbody>
    </table>

    <div class="pager toolbar" v-if="totalPages() > 1">
      <button class="btn sm" :disabled="page <= 1" @click="goPage(page - 1)">上一页</button>
      <span class="muted">第 {{ page }} / {{ totalPages() }} 页</span>
      <button class="btn sm" :disabled="page >= totalPages()" @click="goPage(page + 1)">下一页</button>
    </div>
  </div>
</template>

<style scoped>
.link { color: var(--primary); cursor: pointer; text-decoration: underline; }
.link:hover { color: var(--primary-d); }
</style>
