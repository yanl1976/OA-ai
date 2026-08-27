<script setup>
import { ref, computed, onMounted, inject } from "vue";
import { api } from "../api.js";
const emit = defineEmits(["uploaded"]);

const notify = inject("notify");
const cats = ref([]);
const catOptions = ref([]);   // 树形展开后的可选项（含层级缩进 label）
const files = ref([]);        // 已选文件列表（支持批量）
const category = ref("");
const uploading = ref(false);
const progress = ref({ done: 0, total: 0 });

const LS_KEY = "kb_last_upload_category";
const ACCEPT = ".txt,.md,.csv,.docx,.xlsx,.pptx,.pdf";

// 将扁平分类列表按 parent_id 构造成「树形缩进」下拉项，
// 父子级用 ├─ / └─ / │ 连接符区分，同名子级也能看出归属父级。
function buildTreeOptions(list) {
  const children = {};
  list.forEach((c) => {
    const pid = c.parent_id == null ? "root" : c.parent_id;
    (children[pid] = children[pid] || []).push(c);
  });
  const sortFn = (a, b) => (a.sort_order || 0) - (b.sort_order || 0) || a.id - b.id;
  const out = [];
  const walk = (pid, guide) => {
    const list2 = (children[pid] || []).slice().sort(sortFn);
    list2.forEach((c, i) => {
      const isLast = i === list2.length - 1;
      const conn = guide === "" ? "" : isLast ? "└─ " : "├─ ";
      out.push({ name: c.name, label: guide + conn + c.name });
      const childGuide = guide === "" ? "" : guide + (isLast ? "　　" : "│　");
      walk(c.id, childGuide);
    });
  };
  walk("root", "");
  return out;
}

async function loadCats() {
  try {
    const r = await api.categories();
    // 过滤掉「YYYY年度」年份子节点：它们是年份桶、不是真实分类，
    // 若作为「分类」被选中会导致 files/2024年度/2024年度 这类畸形归档路径。
    const list = (r.categories || []).filter(
      (c) => !/^\d{4}年度$/.test(c.name)
    );
    cats.value = list;
    catOptions.value = buildTreeOptions(list);
    // 保留上次上传选择的分类（刷新/重进仍生效）；否则默认第一个
    const saved = localStorage.getItem(LS_KEY);
    const names = catOptions.value.map((o) => o.name);
    if (saved && names.includes(saved)) {
      category.value = saved;
    }
    // 默认 zip 上传的父分类为「管理标准分类」
    if (!zipParent.value) {
      const m = list.find((c) => c.name === "管理标准分类");
      if (m) zipParent.value = m.name;
    }
    // 默认不强制选择：留空表示由系统按文件名/内容自动识别分类（纪要→会议纪要，标准→管理标准）
  } catch (e) {
    notify(e.message, "err");
  }
}

function onCategoryChange() {
  if (category.value) localStorage.setItem(LS_KEY, category.value);
}

// 文件大小友好显示
function fmtSize(n) {
  const kb = n / 1024;
  return kb < 1024 ? kb.toFixed(1) + " KB" : (kb / 1024).toFixed(2) + " MB";
}

// 选择文件（支持多选 / 重复选择追加）
function onFile(e) {
  const picked = Array.from(e.target.files || []);
  if (picked.length) addFiles(picked);
  e.target.value = "";   // 允许再次选同一文件
}

// 拖拽放入
function onDrop(e) {
  const picked = Array.from(e.dataTransfer.files || []);
  if (picked.length) addFiles(picked);
}
function onDragOver(e) { e.preventDefault(); }

// 去重追加（按 name+size 判定）
function addFiles(list) {
  const seen = new Set(files.value.map((f) => f.name + ":" + f.size));
  for (const f of list) {
    const key = f.name + ":" + f.size;
    if (!seen.has(key)) {
      seen.add(key);
      files.value.push(f);
    }
  }
}

// 移除单个
function removeAt(i) {
  files.value.splice(i, 1);
}

// 清空
function clearAll() {
  files.value = [];
}

async function upload() {
  if (!files.value.length) { notify("请选择文件", "err"); return; }
  // 分类留空即由系统自动识别，无需强制选择
  uploading.value = true;
  progress.value = { done: 0, total: files.value.length };
  try {
    // 后端支持一次性批量上传，仅重建一次索引
    const r = await api.upload(files.value, category.value);
    const results = r.results || [];
    const okList = results.filter((x) => x.ok);
    const failList = results.filter((x) => !x.ok);
    // 真实进度：已处理数（成功+失败）
    progress.value = { done: results.length, total: files.value.length };
    if (failList.length && okList.length) {
      notify("部分上传成功：" + okList.length + " 个成功，" + failList.length + " 个失败", "warn");
    } else if (failList.length) {
      notify("全部失败：" + (failList[0].error || "未知错误"), "err");
    } else {
      const cats = {};
      okList.forEach((x) => { const c = x.category || "未分类"; cats[c] = (cats[c] || 0) + 1; });
      const catMsg = Object.entries(cats).map(([c, n]) => c + " " + n).join("、");
      notify("上传成功（" + okList.length + " 个）：" + catMsg + "，已按类别与年份归档", "ok");
    }
    if (failList.length) {
      // 失败文件保留，便于用户查看：按文件名匹配，避免 results 索引错位
      const failNames = new Set(failList.map((x) => x.filename));
      files.value = files.value.filter((f) => failNames.has(f.name));
    } else {
      files.value = [];
    }
    localStorage.setItem(LS_KEY, category.value);
    await loadCats();   // 刷新分类下拉（可能新建了自动分类）
    emit("uploaded");    // 通知外壳刷新侧栏分类树/文档计数
  } catch (e) {
    notify(e.message, "err");
  } finally {
    uploading.value = false;
    progress.value = { done: 0, total: 0 };
  }
}

// ---- 批量目录上传（zip 压缩包）----
const zipFile = ref(null);
const zipParent = ref("");
const zipUploading = ref(false);
const zipResult = ref(null);

const topCats = computed(() => cats.value.filter((c) => c.parent_id == null));

function onZipFile(e) {
  const f = e.target.files && e.target.files[0];
  zipFile.value = f && f.name.toLowerCase().endsWith(".zip") ? f : null;
  if (!zipFile.value && f) notify("请选择 .zip 压缩包", "err");
  e.target.value = "";
}

async function uploadZip() {
  if (!zipFile.value) { notify("请选择 zip 压缩包", "err"); return; }
  if (!zipParent.value) { notify("请选择目标父分类", "err"); return; }
  zipUploading.value = true;
  zipResult.value = null;
  try {
    const r = await api.uploadZip(zipFile.value, zipParent.value);
    zipResult.value = r;
    const ok = (r.results || []).filter((x) => x.ok).length;
    const created = (r.created_categories || []).join("、");
    notify("目录上传完成：" + ok + " 个文件成功" + (created ? "，新建/复用分类：" + created : ""), "ok");
    zipFile.value = null;
  } catch (e) {
    notify(e.message, "err");
  } finally {
    zipUploading.value = false;
  }
}

onMounted(loadCats);
</script>

<template>
  <h2>上传文档</h2>
  <p class="muted">支持文本与 Office / PDF 格式：.txt / .md / .csv / .docx / .xlsx / .pptx / .pdf。可一次选择多个文件批量上传，不限数量，将纳入对应分类并可被检索。</p>
  <div class="card card-pad" style="max-width: 560px">
    <div class="field">
      <label>选择分类</label>
      <select class="input" v-model="category" @change="onCategoryChange">
        <option value="">自动识别（按文件名/内容归类）</option>
        <option v-for="o in catOptions" :key="o.name" :value="o.name">{{ o.label }}</option>
      </select>
      <p class="file-hint">默认「自动识别」：系统按文件名/正文判定分类（如总经理办公会→会议纪要、标准→管理标准），并归入对应年份目录；也可手动指定分类覆盖自动识别。</p>
    </div>
    <div class="field">
      <label>选择文件（可批量，不限数量）</label>
      <input class="input" type="file" multiple :accept="ACCEPT" @change="onFile" />
      <div class="dropzone" @drop="onDrop" @dragover="onDragOver">
        也可将文件拖拽到此处（支持多选）
      </div>
      <ul class="file-list" v-if="files.length">
        <li v-for="(f, i) in files" :key="f.name + ':' + f.size + ':' + i">
          <span class="fname">{{ f.name }}</span>
          <span class="fsize">{{ fmtSize(f.size) }}</span>
          <button class="btn sm danger" :disabled="uploading" @click="removeAt(i)">移除</button>
        </li>
      </ul>
      <p class="file-hint" v-if="files.length">
        已选 {{ files.length }} 个文件 ·
        <a class="link" @click="clearAll" v-if="!uploading">清空</a>
      </p>
    </div>
    <button class="btn primary" :disabled="uploading || !files.length" @click="upload">
      {{ uploading ? "上传中…" : ("上传（" + files.length + "）") }}
    </button>
    <p class="file-hint" v-if="uploading && progress.total">
      正在处理 {{ progress.done }} / {{ progress.total }}
    </p>
  </div>

  <div class="card card-pad" style="max-width: 560px; margin-top: 18px">
    <h3 style="margin: 0 0 8px">批量目录上传（zip 压缩包）</h3>
    <p class="file-hint">把整个管理标准目录（含子文件夹）打包成 <b>zip</b> 上传，系统自动按 zip 内部文件夹名建立子类，并把文件归入对应分类与年份目录。已存在的同名分类会复用，不会重复创建。</p>
    <div class="field">
      <label>目标父分类</label>
      <select class="input" v-model="zipParent">
        <option v-for="c in topCats" :key="c.id" :value="c.name">{{ c.name }}</option>
      </select>
    </div>
    <div class="field">
      <label>选择 zip 压缩包</label>
      <input class="input" type="file" accept=".zip" @change="onZipFile" />
      <p class="file-hint" v-if="zipFile">{{ zipFile.name }}（{{ fmtSize(zipFile.size) }}）</p>
    </div>
    <button class="btn primary" :disabled="zipUploading || !zipFile" @click="uploadZip">
      {{ zipUploading ? "处理中…" : "上传目录(zip)" }}
    </button>
    <div class="zip-result" v-if="zipResult" style="margin-top:12px">
      <p>成功 <b>{{ (zipResult.results||[]).filter(x=>x.ok).length }}</b> / 共 {{ (zipResult.results||[]).length }} 个文件</p>
      <p v-if="zipResult.created_categories && zipResult.created_categories.length">新建/复用分类：{{ zipResult.created_categories.join("、") }}</p>
      <ul class="file-list" v-if="zipResult.results">
        <li v-for="(x,i) in zipResult.results" :key="i">
          <span class="fname">{{ x.filename }}</span>
          <span class="fsize" v-if="x.ok">→ {{ x.category }}</span>
          <span class="fsize" v-else style="color:#c0392b">{{ x.error }}</span>
        </li>
      </ul>
    </div>
  </div>
</template>

<style scoped>
.dropzone {
  margin-top: 8px;
  border: 1px dashed var(--border, #ccc);
  border-radius: 8px;
  padding: 16px;
  text-align: center;
  color: var(--muted, #888);
  font-size: 13px;
}
.dropzone:hover { border-color: var(--primary, #2b7de9); color: var(--primary, #2b7de9); }
.file-list {
  list-style: none;
  margin: 10px 0 0;
  padding: 0;
  max-height: 240px;
  overflow: auto;
  border: 1px solid var(--border, #eee);
  border-radius: 8px;
}
.file-list li {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 6px 10px;
  border-bottom: 1px solid var(--border, #f0f0f0);
}
.file-list li:last-child { border-bottom: none; }
.fname { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.fsize { color: var(--muted, #888); font-size: 12px; }
.link { color: var(--primary, #2b7de9); cursor: pointer; text-decoration: underline; }
</style>
