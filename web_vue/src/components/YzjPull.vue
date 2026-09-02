<script setup>
import { ref, onMounted, onUnmounted, inject } from "vue";
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
const runningId = ref(null);   // 当前正在后台跑的任务 id
const lastStat = ref(null);
const forceNow = ref(false);
const pollTimer = ref(null);
const pollErr = ref("");

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
    batch_size: 0,
    interval_sec: 3,
    schedule: "daily",
    schedule_hour: 2,
    schedule_minute: 0,
    schedule_weekday: 0,
    schedule_times: ["02:00"],
  };
}

function openCreate() {
  editing.value = blankTask();
  showEditor.value = true;
}

function openEdit(t) {
  editing.value = JSON.parse(JSON.stringify(t));
  normalizeScheduleTimes(editing.value);
  showEditor.value = true;
}

// 统一 schedule_times：<任务>可配置一天多个执行时段；旧配置只有单一
// schedule_hour/schedule_minute，这里补成等价的单元素数组，保证界面一致、后端兼容。
function normalizeScheduleTimes(t) {
  if (!t) return;
  if (!Array.isArray(t.schedule_times) || t.schedule_times.length === 0) {
    const h = String(t.schedule_hour ?? 2).padStart(2, "0");
    const m = String(t.schedule_minute ?? 0).padStart(2, "0");
    t.schedule_times = [h + ":" + m];
  }
  // 同步回旧字段，便于后端/旧逻辑回退时取值一致
  const first = t.schedule_times[0] || "02:00";
  const mm = /^(\d{1,2}):(\d{1,2})$/.exec(first);
  if (mm) {
    t.schedule_hour = parseInt(mm[1], 10);
    t.schedule_minute = parseInt(mm[2], 10);
  }
}

// 时段增删
function addScheduleTime() {
  if (!Array.isArray(editing.value.schedule_times)) editing.value.schedule_times = [];
  editing.value.schedule_times.push("09:00");
}
function removeScheduleTime(i) {
  if (!Array.isArray(editing.value.schedule_times)) return;
  editing.value.schedule_times.splice(i, 1);
  if (editing.value.schedule_times.length === 0) editing.value.schedule_times.push("02:00");
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
  // 触发后端异步执行（立即返回，任务在后台线程跑，切换页面/路由不影响）
  runningId.value = t.id;
  lastStat.value = null;
  pollErr.value = "";
  try {
    const r = await api.yzjTaskRun(t.id, { dry_run: dry, limit: dry ? 3 : null, force: forceNow.value });
    if (r.already_running) {
      notify("该任务已在后台运行，已切换到进度跟踪", "ok");
    } else {
      notify(dry ? "试跑已启动（后台执行）" : "拉取已启动（后台执行）", "ok");
    }
    startPoll(t.id);
  } catch (e) {
    notify(e.message, "err");
    runningId.value = null;
  }
}

async function abortTask(t) {
  try {
    await api.yzjTaskAbort(t.id);
    notify("已发送终止请求，任务将尽快停止", "ok");
  } catch (e) {
    notify(e.message, "err");
  }
}

function stopPoll() {
  if (pollTimer.value) {
    clearInterval(pollTimer.value);
    pollTimer.value = null;
  }
}

async function startPoll(id) {
  stopPoll();
  const tick = async () => {
    try {
      const r = await api.yzjTaskStatus(id);
      const p = r.progress || {};
      const st = p.stats || {};
      // 把最新进度同步给展示区（即使切换页面后重新进入也能续显）
      lastStat.value = { ...st, _dry: st.get ? false : (lastStat.value && lastStat.value._dry) };
      // 「拉一个刷一个」：把本次运行新落盘的文档即时并入列表顶部（按 doc_id 去重）
      const nd = p.new_docs || [];
      if (nd.length) {
        const seen = new Set(pulledDocs.value.map((d) => d.doc_id));
        const add = nd.filter((d) => !seen.has(d.doc_id));
        if (add.length) pulledDocs.value = [...add, ...pulledDocs.value];
      }
      if (!p.running) {
        // 完成（含被中止）：停轮询、全量刷新一次文档列表（补齐 chars/indexed 等异步字段）
        stopPoll();
        runningId.value = null;
        if (st.aborted) notify("任务已中止", "ok");
        await loadPulledDocsOnly();
      }
    } catch (e) {
      pollErr.value = e.message;
    }
  };
  await tick();
  pollTimer.value = setInterval(tick, 1500);
}

async function loadPulledDocsOnly() {
  try {
    const pd = await api.yzjPulledDocs({ page_size: 100 });
    pulledDocs.value = pd.items || [];
  } catch (e) { /* 忽略 */ }
}

function scheduleText(t) {
  if (t.schedule === "manual") return "手动";
  // 多执行时段：优先按 schedule_times 展示（如「每天 09:00、14:30、21:00」）
  const times = Array.isArray(t.schedule_times) && t.schedule_times.length
    ? t.schedule_times
    : [String(t.schedule_hour ?? 0).padStart(2, "0") + ":" + String(t.schedule_minute ?? 0).padStart(2, "0")];
  const txt = times.join("、");
  if (t.schedule === "daily") return "每天 " + txt;
  if (t.schedule === "weekly") {
    const wd = WEEKDAYS.find((w) => w.v === t.schedule_weekday);
    return (wd ? wd.t : "周?") + " " + txt;
  }
  return "-";
}

onMounted(async () => {
  await load();
  // 恢复轮询：若组件挂载时后端仍有正在跑的任务，自动续接进度显示
  for (const t of tasks.value) {
    try {
      const r = await api.yzjTaskStatus(t.id);
      if (r.progress && r.progress.running) {
        runningId.value = t.id;
        lastStat.value = { ...(r.progress.stats || {}), _dry: false };
        startPoll(t.id);
        break;
      }
    } catch (e) { /* 忽略 */ }
  }
});

// 切换页面时仅停止前端轮询定时器；后端任务在后台线程继续跑，
// 再次进入本组件时 onMounted 会自动恢复进度跟踪，故「切页不会终止拉取」。
onUnmounted(stopPoll);
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
            <button class="btn sm warn" v-if="runningId===t.id" @click="abortTask(t)">终止</button>
            <button class="btn sm" @click="openEdit(t)">编辑</button>
            <button class="btn sm danger" :disabled="runningId===t.id" @click="removeTask(t)">删除</button>
          </td>
        </tr>
        <tr v-if="!tasks.length"><td colspan="9" class="loading">暂无拉取任务</td></tr>
      </tbody>
    </table>
    </template>

    <div v-if="lastStat" class="stat-box">
      <b>执行结果（{{ lastStat._dry ? "试跑" : "正式" }} · {{ lastStat.task }}）：</b>
      <template v-if="runningId">
        <span class="running">后台运行中…</span>
        已处理 {{ lastStat.processed || 0 }} / {{ lastStat.total || lastStat.found || "?" }}
      </template>
      <template v-else>
        <template v-if="lastStat._dry">
          发现 {{ lastStat.found }} · 试跑命中 {{ lastStat.tried }}（未落盘）· 跳过 {{ lastStat.skipped }} · 失败 {{ lastStat.failed }}
        </template>
        <template v-else>
          发现 {{ lastStat.found }} · 落盘 {{ lastStat.downloaded }} · 跳过 {{ lastStat.skipped }} · 失败 {{ lastStat.failed }}
        </template>
        <span v-if="lastStat.aborted" class="aborted">（已中止）</span>
      </template>
      <span v-if="lastStat.errors && lastStat.errors.length">；错误：{{ lastStat.errors.slice(0,3).join("; ") }}</span>
      <span v-if="pollErr" class="poll-err">；进度获取失败：{{ pollErr }}</span>
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

    <!-- 编辑器：自包含弹窗（Teleport 到 body，不依赖外部组件） -->
    <Teleport to="body">
      <div v-if="showEditor" class="yzj-modal-mask" @click.self="showEditor=false">
        <div class="yzj-modal">
          <div class="yzj-modal-head">
            <span>{{ (tasks.some(x=>x.id===editing?.id) ? '编辑' : '新建') }}拉取任务</span>
            <button class="yzj-modal-x" @click="showEditor=false">×</button>
          </div>
          <div v-if="editing" class="yzj-modal-body form">
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
              <input class="inp" type="number" min="0" v-model.number="editing.batch_size" />
              <span class="hint">0 或留空 = 不限（拉取全部）；设正整数则单次最多处理该条数</span>
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
              <label>执行时段</label>
              <template v-if="editing.schedule==='weekly'">
                <select class="inp" v-model.number="editing.schedule_weekday"><option v-for="w in WEEKDAYS" :key="w.v" :value="w.v">{{ w.t }}</option></select>
              </template>
              <div class="times-wrap">
                <div v-for="(tm, i) in editing.schedule_times" :key="i" class="time-row">
                  <input class="inp time-inp" type="time" v-model="editing.schedule_times[i]" />
                  <button class="btn sm danger" @click="removeScheduleTime(i)" :disabled="editing.schedule_times.length<=1">删除</button>
                </div>
                <button class="btn sm" @click="addScheduleTime">+ 增加时段</button>
                <span class="hint">可配置一天内多个自动拉取时段（如 09:00、14:30、21:00），每个时段各注册一个调度作业</span>
              </div>
            </div>
          </div>
          <div class="yzj-modal-foot">
            <button class="btn" :disabled="saving" @click="showEditor=false">关闭</button>
            <button class="btn primary" :disabled="saving" @click="saveTask">{{ saving ? "保存中…" : "保存" }}</button>
          </div>
        </div>
      </div>
    </Teleport>
  </div>
</template>

<style scoped>
.yzj-modal-mask {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.45);
  display: flex;
  align-items: flex-start;
  justify-content: center;
  padding: 6vh 16px 16px;
  z-index: 1000;
  overflow: auto;
}
.yzj-modal {
  background: #fff;
  width: min(680px, 100%);
  border-radius: 10px;
  box-shadow: 0 12px 40px rgba(0, 0, 0, 0.25);
  display: flex;
  flex-direction: column;
}
.yzj-modal-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 14px 18px;
  border-bottom: 1px solid #eee;
  font-size: 16px;
  font-weight: 600;
}
.yzj-modal-x {
  border: none;
  background: transparent;
  font-size: 22px;
  line-height: 1;
  cursor: pointer;
  color: #888;
}
.yzj-modal-x:hover { color: #333; }
.yzj-modal-body {
  padding: 16px 18px;
  max-height: 64vh;
  overflow: auto;
}
.yzj-modal-foot {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
  padding: 12px 18px;
  border-top: 1px solid #eee;
}
</style>

<style scoped>
.toolbar { display: flex; gap: 8px; margin-bottom: 12px; }
.tbl { width: 100%; border-collapse: collapse; font-size: 13px; }
.tbl th, .tbl td { border: 1px solid #e5e7eb; padding: 6px 8px; text-align: left; }
.tbl th { background: #f8fafc; }
.ops { display: flex; gap: 4px; flex-wrap: wrap; }
.stat-box { margin-top: 12px; padding: 10px; background: #f0fdf4; border: 1px solid #bbf7d0; border-radius: 6px; font-size: 13px; }
.stat-box .running { color: #2563eb; font-weight: 600; margin-right: 6px; }
.stat-box .aborted { color: #d97706; font-weight: 600; }
.stat-box .poll-err { color: #dc2626; }
.btn.warn { background: #fef3c7; color: #92400e; border-color: #fcd34d; }
.btn.warn:hover { background: #fde68a; }
.force-row { margin: 10px 0; font-size: 13px; color: #475569; }
.pulled { margin-top: 20px; }
.pulled h3 { font-size: 14px; margin: 0 0 8px; color: #334155; }
.jobs { margin-top: 8px; font-size: 12px; color: #475569; }
.job { display: inline-block; margin-right: 12px; background: #f1f5f9; padding: 2px 6px; border-radius: 4px; }
.form .row { display: flex; align-items: center; gap: 10px; margin-bottom: 10px; }
.form .row label { width: 100px; flex: none; color: #475569; }
.form .row .hint { font-size: 12px; color: #94a3b8; flex: none; }
.inp { flex: 1; padding: 6px 8px; border: 1px solid #cbd5e1; border-radius: 4px; }
.times-wrap { display: flex; flex-direction: column; gap: 6px; }
.time-row { display: flex; align-items: center; gap: 8px; }
.time-inp { flex: none; width: 130px; }
.times-wrap .hint { flex: none; font-size: 12px; color: #94a3b8; }
</style>
