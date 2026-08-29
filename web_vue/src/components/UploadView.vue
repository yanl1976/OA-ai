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
    const r = await api.upload(files.value, category.value, false);
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
    // 上传完成的文件进入后台提取队列（异步识别+索引），轮询展示识别进度
    const ids = okList.map((x) => x.doc_id).filter(Boolean);
    if (ids.length) pollRecognition(ids);
  } catch (e) {
    // 409：分类与内容冲突，后端未落盘，弹窗请用户确认后重传
    if (e.status === 409 && e.data && e.data.conflicts) {
      conflicts.value = e.data.conflicts;
      confirmOpen.value = true;
      notify(e.message || "分类与文件内容不符，请确认", "warn");
    } else {
      notify(e.message, "err");
    }
  } finally {
    uploading.value = false;
    progress.value = { done: 0, total: 0 };
  }
}

// ---- 分类冲突确认：由用户决定如何上传（后端不代劳，问题在源头解决）----
const conflicts = ref([]);
const confirmOpen = ref(false);

// 采纳建议分类：把冲突文件改到建议分类后重新上传（其余文件保持原分类）
async function uploadWithSuggested() {
  confirmOpen.value = false;
  const conflictNames = new Set(conflicts.value.map((c) => c.filename));
  const conflictFiles = files.value.filter((f) => conflictNames.has(f.name));
  if (!conflictFiles.length) return;
  uploading.value = true;
  try {
    // 按建议分类逐个提交（不同文件建议分类可能不同）
    const groups = {};
    conflicts.value.forEach((c) => {
      (groups[c.suggested_category] = groups[c.suggested_category] || []).push(c.filename);
    });
    const allIds = [];
    for (const [cat, names] of Object.entries(groups)) {
      const nameSet = new Set(names);
      const gf = conflictFiles.filter((f) => nameSet.has(f.name));
      if (!gf.length) continue;
      const r = await api.upload(gf, cat, true);
      allIds.push(...(r.results || []).map((x) => x.doc_id).filter(Boolean));
    }
    notify("已按建议分类上传 " + allIds.length + " 个文件", "ok");
    files.value = [];
    await loadCats();
    emit("uploaded");
    if (allIds.length) pollRecognition(allIds);
  } catch (err2) {
    notify(err2.message, "err");
  } finally {
    uploading.value = false;
    conflicts.value = [];
  }
}

// 仍按原分类上传：用户知情后自主决定，带 confirm_category=1 重传
async function uploadKeepOriginal() {
  confirmOpen.value = false;
  const conflictNames = new Set(conflicts.value.map((c) => c.filename));
  const conflictFiles = files.value.filter((f) => conflictNames.has(f.name));
  if (!conflictFiles.length) return;
  uploading.value = true;
  try {
    const r = await api.upload(conflictFiles, category.value, true);
    notify("已按原分类上传 " + ((r.results || []).length) + " 个文件（提取规则可能与内容不符）", "warn");
    files.value = [];
    await loadCats();
    emit("uploaded");
    const ids = (r.results || []).map((x) => x.doc_id).filter(Boolean);
    if (ids.length) pollRecognition(ids);
  } catch (err3) {
    notify(err3.message, "err");
  } finally {
    uploading.value = false;
    conflicts.value = [];
  }
}

function cancelUpload() {
  confirmOpen.value = false;
  conflicts.value = [];
  notify("已取消，请重新选择分类后上传", "warn");
}

// ---- 上传后后台识别进度（轻量轮询，不阻塞上传完成提示）----
const recognizing = ref(false);
const recogDone = ref(0);
const recogTotal = ref(0);
let _recogTimer = null;

async function pollRecognition(ids) {
  if (_recogTimer) { clearTimeout(_recogTimer); _recogTimer = null; }
  recognizing.value = true;
  recogTotal.value = ids.length;
  recogDone.value = 0;
  const MAX_POLLS = 40;   // 最多轮询 ~60s（1.5s 一次），超时则停止轮询（后台仍在跑，可去管理页看状态）
  let polls = 0;
  const tick = async () => {
    if (!recognizing.value) return;
    try {
      const r = await api.uploadStatus(ids);
      const results = r.results || [];
      const done = results.filter((x) => x.indexed).length;
      recogDone.value = done;
      if (done >= ids.length) {
        recognizing.value = false;
        notify("后台识别完成，已可检索", "ok");
        emit("uploaded");
        return;
      }
    } catch (e) { /* 忽略单次轮询错误，继续 */ }
    polls += 1;
    if (polls >= MAX_POLLS) {
      recognizing.value = false;
      notify("后台仍在识别（" + recogDone.value + "/" + ids.length + "），稍后可在「上传管理」查看状态", "warn");
      return;
    }
    _recogTimer = setTimeout(tick, 1500);
  };
  tick();
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
    const r = await api.uploadZip(zipFile.value, zipParent.value, false);
    zipResult.value = r;
    const ok = (r.results || []).filter((x) => x.ok).length;
    const created = (r.created_categories || []).join("、");
    notify("目录上传完成：" + ok + " 个文件成功" + (created ? "，新建/复用分类：" + created : ""), "ok");
    zipFile.value = null;
  } catch (e) {
    // 409：zip 内有文件分类与内容冲突，展示清单请用户调整目录结构后重传
    if (e.status === 409 && e.data && e.data.conflicts) {
      zipConflicts.value = e.data.conflicts;
      zipResult.value = e.data;
      notify(e.message || "压缩包内有分类冲突，请调整目录结构后重新上传", "warn");
    } else {
      notify(e.message, "err");
    }
  } finally {
    zipUploading.value = false;
  }
}

// zip 冲突：用户调整目录后重传；确属误报可「忽略并上传」（带 confirm_category=1）
const zipConflicts = ref([]);

async function uploadZipIgnore() {
  if (!zipFile.value) return;
  zipUploading.value = true;
  try {
    const r = await api.uploadZip(zipFile.value, zipParent.value, true);
    zipResult.value = r;
    zipConflicts.value = [];
    notify("已忽略冲突提示完成上传，请留意提取结果是否符合预期", "warn");
    zipFile.value = null;
  } catch (e2) {
    notify(e2.message, "err");
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
    <p class="recog-hint" v-if="recognizing">
      <span class="spin">🔄</span> 后台识别中：{{ recogDone }} / {{ recogTotal }}（识别完成后即可检索，可先去「上传管理」查看）
    </p>
  </div>

  <!-- 分类冲突确认弹窗：上传时校验发现分类与内容不符，交由用户决定（后端不自动纠正） -->
  <div class="modal-mask" v-if="confirmOpen" @click.self="cancelUpload">
    <div class="modal-box">
      <h3 class="modal-title">⚠️ 分类与文件内容不符</h3>
      <p class="modal-desc">下列文件所选分类与内容类型不一致，若直接上传会导致提取结果错乱。请选择处理方式：</p>
      <ul class="conflict-list">
        <li v-for="(c, i) in conflicts" :key="i">
          <div class="conflict-name">{{ c.filename }}</div>
          <div class="conflict-detail">{{ c.warning }}</div>
        </li>
      </ul>
      <div class="modal-actions">
        <button class="btn primary" :disabled="uploading" @click="uploadWithSuggested">
          改用建议分类上传
        </button>
        <button class="btn" :disabled="uploading" @click="uploadKeepOriginal">
          仍按原分类上传
        </button>
        <button class="btn danger" :disabled="uploading" @click="cancelUpload">取消</button>
      </div>
      <p class="modal-foot">提示：改用建议分类可保证提取结构正确；若确认分类无误，可选择「仍按原分类上传」。</p>
    </div>
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
      <!-- zip 内分类冲突：交由用户调整目录结构，不自动纠正 -->
      <div class="cat-warn" v-if="zipConflicts.length">
        <p class="cat-warn-title">⚠️ {{ zipConflicts.length }} 个文件分类与内容不符（未上传）</p>
        <ul class="file-list">
          <li v-for="(c, i) in zipConflicts" :key="i">
            <span class="fname">{{ c.filename }}</span>
            <span class="fsize cat-warn-text">{{ c.warning }}</span>
          </li>
        </ul>
        <p class="cat-warn-foot">请调整 zip 目录结构（如把纪要单独放到「会议纪要」目录）后重新上传。</p>
        <button class="btn sm" :disabled="zipUploading" @click="uploadZipIgnore" style="margin-top:8px">
          忽略提示并上传
        </button>
      </div>
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
.recog-hint { margin-top: 8px; color: #b9770e; font-size: 13px; }
.recog-hint .spin { display: inline-block; animation: recog-spin 1.2s linear infinite; }
@keyframes recog-spin { from { transform: rotate(0); } to { transform: rotate(360deg); } }

/* 分类冲突提示（上传校验拦截，交由用户确认） */
.cat-warn {
  margin-top: 12px; padding: 12px 14px;
  border: 1px solid #fde68a; border-left: 4px solid #f59e0b;
  border-radius: 8px; background: #fffbeb;
}
.cat-warn-title { margin: 0 0 8px; font-size: 13px; font-weight: 700; color: #b45309; }
.cat-warn .file-list { margin: 0; }
.cat-warn .file-list li { flex-wrap: wrap; }
.cat-warn-text { color: #b45309; font-size: 12px; flex-basis: 100%; padding-left: 2px; }
.cat-warn-foot { margin: 8px 0 0; font-size: 12px; color: var(--muted, #888); }

/* 分类冲突确认弹窗 */
.modal-mask {
  position: fixed; inset: 0; z-index: 1000;
  background: rgba(15, 23, 42, .45);
  display: flex; align-items: center; justify-content: center;
  padding: 20px;
}
.modal-box {
  background: var(--panel, #fff); border-radius: 14px;
  box-shadow: 0 18px 50px rgba(0, 0, 0, .22);
  max-width: 620px; width: 100%; max-height: 86vh; overflow: auto;
  padding: 22px 24px;
}
.modal-title { margin: 0 0 8px; font-size: 17px; font-weight: 700; color: #b45309; }
.modal-desc { margin: 0 0 12px; font-size: 13px; color: var(--muted, #666); line-height: 1.6; }
.conflict-list {
  list-style: none; margin: 0 0 16px; padding: 10px 12px;
  background: #fffbeb; border: 1px solid #fde68a; border-radius: 8px;
  max-height: 260px; overflow: auto;
}
.conflict-list li { padding: 7px 0; border-bottom: 1px dashed #fde68a; }
.conflict-list li:last-child { border-bottom: none; }
.conflict-name { font-size: 13px; font-weight: 600; word-break: break-all; }
.conflict-detail { font-size: 12px; color: #b45309; margin-top: 3px; line-height: 1.5; }
.modal-actions { display: flex; gap: 10px; flex-wrap: wrap; }
.modal-foot { margin: 12px 0 0; font-size: 12px; color: var(--muted, #888); line-height: 1.6; }
</style>
