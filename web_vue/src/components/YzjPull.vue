<script setup>
import { ref, onMounted, inject } from "vue";
import { api } from "../api.js";

const notify = inject("notify");
const loading = ref(false);
const tasks = ref([]);
const jobs = ref([]);
const templates = ref([]);
const pulledDocs = ref([]);

const showEditor = ref(false);
const editing = ref(null); // 当前编辑的任务副本
const saving = ref(false);
const runningId = ref(null);
const lastStat = ref(null);
const forceNow = ref(false);

const SCHEDULE_OPTS = [
  { v: "manual", t: "手动触发" },
  { v: "daily", t: "每日" },
  { v: "weekly", t: "每周" },
];
const WEEKDAYS = [
  { v: 0, t: "周一" }, { v: 1, t: "周二" }, { v: 2, t: "周三" },
  { v: 3, t: "周四" }, { v: 4, t: "周五" }, { v: 5, t: "周六" }, { v: 6, t: "周日" },
];
const STATUS_OPTS = [
  { v: "", t: "全部" },
  { v: "FINISH", t: "已完成" },
  { v: "RUNNING", t: "进行中" },
];
const TIME_OPTS = [
  { v: "all", t: "全部时间" },
  { v: "recent_days", t: "近 N 天" },
  { v: "custom", t: "指定起止" },
];

async function load() {
  loading.value = true;
  try {
    const [t, tp, pd] = await Promise.all([
      api.yzjTasks(),
      api.yzjTemplates(),
      api.yzjPulledDocs({ page_size: 100 }),
    ]);
    tasks.value = (t.tasks || []).map((x) => ({ ...x, _busy: false }));
    jobs.value = t.jobs || [];
    templates.value = tp.templates || [];
    pulledDocs.value = (pd.items || []);
  } catch (e) {
    notify(e.message, "err");
  } finally {
    loading.value = false;
  }
}

function blankTask() {
  return {
    id: "task_" + Date.now(),
    name: "",
    enabled: true,
    form_code_id: "",
    template_name: "",
    status: "FINISH",
    time_range: "all",
    recent_days: 7,
    start_date: "",
    end_date: "",
    target_category: "会议纪要",
    download_attachments: true,
    index_into_kb: true,
    batch_size: 10,
    interval_sec: 3,
    schedule: "daily",
    schedule_hour: 2,
    schedule_minute: 0,
    schedule_weekday: 0,
  };
}

function openCreate() {
  editing.value = blankTask();
  showEditor.value = true;
}

function openEdit(t) {
  editing.value = JSON.parse(JSON.stringify(t));
  showEditor.value = true;
}

async function saveTask() {
  if (!editing.value.name) { notify("请填写任务名称", "err"); return; }
  if (!editing.value.template_name && !editing.value.form_code_id) { notify("请选择云之家模板或填写模板名", "err"); return; }
  saving.value = true;
  try {
    if (tasks.value.some((x) => x.id === editing.value.id)) {
      await api.yzjTaskUpdate(editing.value.id, editing.value);
    } else {
      await api.yzjTaskCreate(editing.value);
    }
    notify("已保存", "ok");
    showEditor.value = false;
    await load();
  } catch (e) {
    notify(e.message, "err");
  } finally {
    saving.value = false;
  }
}

async function removeTask(t) {
  if (!confirm("确定删除拉取任务「" + t.name + "」？")) return;
  try {
    await api.yzjTaskDelete(t.id);
    notify("已删除", "ok");
    await load();
  } catch (e) {
    notify(e.message, "err");
  }
}

async function toggleEnabled(t) {
  t._busy = true;
  const next = { ...t, enabled: !t.enabled };
  try {
    await api.yzjTaskUpdate(t.id, next);
    t.enabled = !t.enabled;
    notify(t.enabled ? "任务已开启" : "任务已关闭", "ok");
  } catch (e) {
    notify(e.message, "err");
  } finally {
    t._busy = false;
  }
}

async function runTask(t, dry) {
  runningId.value = t.id;
  lastStat.value = null;
  try {
    const r = await api.yzjTaskRun(t.id, { dry_run: dry, limit: dry ? 3 : null, force: forceNow.value });
    lastStat.value = { ...r.stats, _dry: !!dry };
    notify((dry ? "试跑完成：" : "执行完成：") + JSON.stringify(r.stats), "ok");
  } catch (e) {
    notify(e.message, "err");
  } finally {
    runningId.value = null;
  }
}

function scheduleText(t) {
  if (t.schedule === "manual") return "手动";
  if (t.schedule === "daily") return "每天 " + String(t.schedule_hour).padStart(2, "0") + ":" + String(t.schedule_minute).padStart(2, "0");
  if (t.schedule === "weekly") {
    const wd = WEEKDAYS.find((w) => w.v === t.schedule_weekday);
    return (wd ? wd.t : "周?") + " " + String(t.schedule_hour).padStart(2, "0") + ":" + String(t.schedule_minute).padStart(2, "0");
  }
  return "-";
}

onMounted(load);
</script>

<template>
  <div class="yzj-pull">
    <div class="toolbar">
      <button class="btn primary" @click="openCreate">+ 新建拉取任务</button>
      <button class="btn" @click="load">刷新</button>
    </div>
    <div v-if="loading" class="loading">加载中…</div>
    <template v-else>
    <div class="force-row">
      <label><input type="checkbox" v-model="forceNow" /> 强制重拉（忽略去重记录，手动删除的文件也会重新拉回）</label>
    </div>
    <table class="tbl">
      <thead>
        <tr>
          <th>任务名</th><th>模板</th><th>状态</th><th>时间范围</th>
          <th>计划</th><th>目标分类</th><th>入索引</th><th>开关</th><th>操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="t in tasks" :key="t.id">
          <td>{{ t.name }}</td>
          <td>{{ t.template_name || t.form_code_id }}</td>
          <td>{{ (STATUS_OPTS.find(s=>s.v===t.status)||{}).t }}</td>
          <td>{{ (TIME_OPTS.find(s=>s.v===t.time_range)||{}).t }}<span v-if="t.time_range==='recent_days'"> ({{t.recent_days}}天)</span></td>
          <td>{{ scheduleText(t) }}</td>
          <td>{{ t.target_category }}</td>
          <td>{{ t.index_into_kb ? "是" : "否" }}</td>
          <td>
            <button class="btn sm" :class="t.enabled ? 'primary' : ''" :disabled="t._busy" @click="toggleEnabled(t)">
              {{ t.enabled ? "● 开" : "○ 关" }}
            </button>
          </td>
          <td class="ops">
            <button class="btn sm" :disabled="runningId===t.id" @click="runTask(t, false)">立即拉取</button>
            <button class="btn sm" :disabled="runningId===t.id" @click="runTask(t, true)">试跑</button>
            <button class="btn sm" @click="openEdit(t)">编辑</button>
            <button class="btn sm danger" @click="removeTask(t)">删除</button>
          </td>
        </tr>
        <tr v-if="!tasks.length"><td colspan="9" class="loading">暂无拉取任务</td></tr>
      </tbody>
    </table>
    </template>

    <div v-if="lastStat" class="stat-box">
      <b>上次执行结果（{{ lastStat._dry ? "试跑" : "正式" }} · {{ lastStat.task }}）：</b>
      <template v-if="lastStat._dry">
        发现 {{ lastStat.found }} · 试跑命中 {{ lastStat.tried }}（未落盘）· 跳过 {{ lastStat.skipped }} · 失败 {{ lastStat.failed }}
      </template>
      <template v-else>
        发现 {{ lastStat.found }} · 落盘 {{ lastStat.downloaded }} · 跳过 {{ lastStat.skipped }} · 失败 {{ lastStat.failed }}
      </template>
      <span v-if="lastStat.errors && lastStat.errors.length">；错误：{{ lastStat.errors.slice(0,3).join("; ") }}</span>
    </div>

    <div class="pulled">
      <h3>已拉取文档（云之家来源，共 {{ pulledDocs.length }} 条）</h3>
      <table v-if="pulledDocs.length" class="tbl">
        <thead>
          <tr><th>文件名</th><th>分类</th><th>年代</th><th>字符数</th><th>入索引</th><th>拉取时间</th></tr>
        </thead>
        <tbody>
          <tr v-for="d in pulledDocs" :key="d.doc_id">
            <td>{{ d.filename }}</td>
            <td>{{ d.category }}</td>
            <td>{{ d.year || "-" }}</td>
            <td>{{ d.chars }}</td>
            <td>{{ d.indexed ? "是" : "否" }}</td>
            <td>{{ (d.created_at || "").replace("T", " ").slice(0, 19) }}</td>
          </tr>
        </tbody>
      </table>
      <div v-else class="loading">暂无云之家拉取文档</div>
    </div>

    <div v-if="jobs.length" class="jobs">
      <b>调度器已注册任务：</b>
      <span v-for="j in jobs" :key="j.id" class="job">{{ j.id }} → {{ j.next_run }}</span>
    </div>

    <!-- 编辑器 -->
    <Modal :show="showEditor" :title="(tasks.some(x=>x.id===editing?.id)?'编辑':'新建')+'拉取任务'" @close="showEditor=false">
      <div v-if="editing" class="form">
        <div class="row"><label>任务名</label><input class="inp" v-model="editing.name" placeholder="如：会议纪要" /></div>
        <div class="row">
          <label>云之家模板</label>
          <select class="inp" v-model="editing.template_name">
            <option value="">-- 手动填写模板名 --</option>
            <option v-for="tp in templates" :key="tp.formCodeId" :value="tp.name">{{ tp.name }}（{{ tp.typeName }}）</option>
          </select>
        </div>
        <div class="row"><label>或模板ID</label><input class="inp" v-model="editing.form_code_id" placeholder="留空则按模板名匹配" /></div>
        <div class="row">
          <label>拉取状态</label>
          <select class="inp" v-model="editing.status"><option v-for="s in STATUS_OPTS" :key="s.v" :value="s.v">{{ s.t }}</option></select>
        </div>
        <div class="row">
          <label>时间范围</label>
          <select class="inp" v-model="editing.time_range"><option v-for="s in TIME_OPTS" :key="s.v" :value="s.v">{{ s.t }}</option></select>
        </div>
        <div class="row" v-if="editing.time_range==='recent_days'"><label>近 N 天</label><input class="inp" type="number" v-model.number="editing.recent_days" /></div>
        <div class="row" v-if="editing.time_range==='custom'">
          <label>起 / 止</label>
          <input class="inp" type="date" v-model="editing.start_date" /> ~ <input class="inp" type="date" v-model="editing.end_date" />
        </div>
        <div class="row"><label>目标分类</label><input class="inp" v-model="editing.target_category" placeholder="知识库分类名" /></div>
        <div class="row">
          <label>下载附件</label>
          <input type="checkbox" v-model="editing.download_attachments" />
        </div>
        <div class="row">
          <label>入知识库索引</label>
          <input type="checkbox" v-model="editing.index_into_kb" />
        </div>
        <div class="row">
          <label>每次拉取数量</label>
          <input class="inp" type="number" min="1" v-model.number="editing.batch_size" />
          <span class="hint">单次最多处理几条流程（防 IP 被封）</span>
        </div>
        <div class="row">
          <label>拉取间隔(秒)</label>
          <input class="inp" type="number" min="0" step="0.5" v-model.number="editing.interval_sec" />
          <span class="hint">每条流程处理完后的等待秒数（限流）</span>
        </div>
        <div class="row">
          <label>计划</label>
          <select class="inp" v-model="editing.schedule"><option v-for="s in SCHEDULE_OPTS" :key="s.v" :value="s.v">{{ s.t }}</option></select>
        </div>
        <div class="row" v-if="editing.schedule!=='manual'">
          <label>执行时间</label>
          <template v-if="editing.schedule==='weekly'">
            <select class="inp" v-model.number="editing.schedule_weekday"><option v-for="w in WEEKDAYS" :key="w.v" :value="w.v">{{ w.t }}</option></select>
          </template>
          <input class="inp" type="number" min="0" max="23" v-model.number="editing.schedule_hour" /> 时
          <input class="inp" type="number" min="0" max="59" v-model.number="editing.schedule_minute" /> 分
        </div>
      </div>
      <template #actions>
        <button class="btn" :disabled="saving" @click="showEditor=false">关闭</button>
        <button class="btn primary" :disabled="saving" @click="saveTask">{{ saving ? "保存中…" : "保存" }}</button>
      </template>
    </Modal>
  </div>
</template>

<style scoped>
.toolbar { display: flex; gap: 8px; margin-bottom: 12px; }
.tbl { width: 100%; border-collapse: collapse; font-size: 13px; }
.tbl th, .tbl td { border: 1px solid #e5e7eb; padding: 6px 8px; text-align: left; }
.tbl th { background: #f8fafc; }
.ops { display: flex; gap: 4px; flex-wrap: wrap; }
.stat-box { margin-top: 12px; padding: 10px; background: #f0fdf4; border: 1px solid #bbf7d0; border-radius: 6px; font-size: 13px; }
.force-row { margin: 10px 0; font-size: 13px; color: #475569; }
.pulled { margin-top: 20px; }
.pulled h3 { font-size: 14px; margin: 0 0 8px; color: #334155; }
.jobs { margin-top: 8px; font-size: 12px; color: #475569; }
.job { display: inline-block; margin-right: 12px; background: #f1f5f9; padding: 2px 6px; border-radius: 4px; }
.form .row { display: flex; align-items: center; gap: 10px; margin-bottom: 10px; }
.form .row label { width: 100px; flex: none; color: #475569; }
.form .row .hint { font-size: 12px; color: #94a3b8; flex: none; }
.inp { flex: 1; padding: 6px 8px; border: 1px solid #cbd5e1; border-radius: 4px; }
</style>
