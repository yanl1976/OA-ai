<script setup>
import { ref, onMounted, inject, computed } from "vue";
import { api } from "../api.js";
import Modal from "./Modal.vue";

const notify = inject("notify");
const users = ref([]);
const roles = ref([]);
const showModal = ref(false);
const editing = ref(null);
const form = ref({ username: "", display_name: "", role_id: "", password: "", status: 1 });
const saving = ref(false);

// ============ 三个分页签：用户列表 / 注册审批 / 用户池 ============
const tab = ref("users");
const tabs = computed(() => [
  { key: "users", label: "用户列表" },
  { key: "registrations", label: `注册审批${pendingCount.value ? ` (${pendingCount.value})` : ""}` },
  { key: "pool", label: "用户池" },
]);

// ---- 注册审批 ----
const regs = ref([]);
const pendingCount = ref(0);
const rejectTarget = ref(null);
const rejectNote = ref("");
const rejectBusy = ref(false);

async function loadRegs() {
  try {
    const r = await api.registrations();
    // 行内可改角色：为每行预备可编辑的角色值，默认取池中预设角色
    regs.value = (r.registrations || []).map((x) => ({ ...x, edit_role_id: x.role_id || "" }));
    pendingCount.value = r.pending_count || 0;
    roles.value = r.roles || roles.value;
  } catch (e) {
    notify(e.message, "err");
  }
}

async function approve(row) {
  if (!row.edit_role_id) {
    notify("请先为该工号指定角色（用户池未预设角色时需管理员指定）", "err");
    return;
  }
  if (!confirm(`确认通过「${row.name}（${row.emp_no}）」的注册申请？通过后将立即创建可登录账号。`)) return;
  try {
    const r = await api.approveRegistration(row.id, row.edit_role_id, "");
    notify(`已通过，账号已创建（id=${r.user_id}）`, "ok");
    loadRegs();
    load();
  } catch (e) {
    notify(e.message, "err");
  }
}

function openReject(row) {
  rejectTarget.value = row;
  rejectNote.value = "";
}
async function doReject() {
  const row = rejectTarget.value;
  if (!row) return;
  rejectBusy.value = true;
  try {
    await api.rejectRegistration(row.id, rejectNote.value);
    notify("已驳回", "ok");
    rejectTarget.value = null;
    loadRegs();
  } catch (e) {
    notify(e.message, "err");
  } finally {
    rejectBusy.value = false;
  }
}

// ---- 用户池（数据存于 config/user_pool.json，以工号为唯一键） ----
const pool = ref([]);
const poolInfo = ref({ file: "", source: "", updated_at: "", count: 0 });
const showPoolModal = ref(false);
const poolEditing = ref(null);
const poolForm = ref({ emp_no: "", name: "", dept: "", role: "", status: 1, note: "" });
const poolSaving = ref(false);
const showImport = ref(false);
const importText = ref("");
const importMode = ref("merge");
const importBusy = ref(false);
const importResult = ref(null);

async function loadPool() {
  try {
    const r = await api.userPool();
    pool.value = r.pool || [];
    poolInfo.value = r.info || poolInfo.value;
    roles.value = r.roles || roles.value;
  } catch (e) {
    notify(e.message, "err");
  }
}

function openPoolCreate() {
  poolEditing.value = null;
  poolForm.value = { emp_no: "", name: "", dept: "", role: "", status: 1, note: "" };
  showPoolModal.value = true;
}
function openPoolEdit(p) {
  poolEditing.value = p;
  poolForm.value = {
    emp_no: p.emp_no, name: p.name, dept: p.dept || "",
    role: p.role || "", status: p.status, note: p.note || "",
  };
  showPoolModal.value = true;
}
async function savePool() {
  if (!poolForm.value.emp_no.trim() || !poolForm.value.name.trim()) {
    notify("工号与姓名不能为空", "err");
    return;
  }
  poolSaving.value = true;
  const payload = {
    emp_no: poolForm.value.emp_no.trim(),
    name: poolForm.value.name.trim(),
    dept: poolForm.value.dept,
    role: poolForm.value.role || "",
    status: poolForm.value.status,
    note: poolForm.value.note,
  };
  try {
    if (poolEditing.value) await api.updatePoolEntry(poolEditing.value.emp_no, payload);
    else await api.createPoolEntry(payload);
    notify("已保存到用户池文件", "ok");
    showPoolModal.value = false;
    loadPool();
  } catch (e) {
    notify(e.message, "err");
  } finally {
    poolSaving.value = false;
  }
}
async function removePool(p) {
  if (p.used) {
    if (!confirm(`工号「${p.emp_no}」已注册账号（${p.used_username || "已占用"}）。\n删除池中条目不会删除已有账号，但该账号将失去池记录。确认删除？`)) return;
  } else if (!confirm(`确认从用户池删除「${p.name}（${p.emp_no}）」？删除后该工号将无法自助注册。`)) return;
  try {
    await api.deletePoolEntry(p.emp_no);
    notify("已删除", "ok");
    loadPool();
  } catch (e) {
    notify(e.message, "err");
  }
}

// 批量导入：每行「工号,姓名,部门,角色」（逗号或制表符分隔，部门与角色可省略）
function parsePoolText(text) {
  const items = [];
  (text || "").split(/\r?\n/).forEach((raw) => {
    const line = raw.trim();
    if (!line || line.startsWith("#")) return;
    const parts = line.split(/[,，\t]/).map((s) => s.trim());
    if (parts.length < 2 || !parts[0] || !parts[1]) return;
    items.push({ emp_no: parts[0], name: parts[1], dept: parts[2] || "", role: parts[3] || "" });
  });
  return items;
}
const importItems = computed(() => parsePoolText(importText.value));
async function doImport() {
  if (!importItems.value.length) {
    notify("未解析到有效数据，请检查格式：工号,姓名,部门,角色", "err");
    return;
  }
  if (importMode.value === "replace" && !confirm("替换模式会先清空【未被注册占用】的现有条目，确认继续？")) return;
  importBusy.value = true;
  try {
    const r = await api.importUserPool(importItems.value, importMode.value);
    importResult.value = r.result;
    notify(`导入完成：新增 ${r.result.created}，更新 ${r.result.updated}`, "ok");
    loadPool();
  } catch (e) {
    notify(e.message, "err");
  } finally {
    importBusy.value = false;
  }
}

// ---- 用户列表（原有） ----
async function load() {
  try {
    const r = await api.users();
    users.value = r.users || [];
    roles.value = r.roles || [];
  } catch (e) {
    notify(e.message, "err");
  }
}

function openCreate() {
  editing.value = null;
  form.value = { username: "", display_name: "", role_id: roles.value[0]?.id || "", password: "", status: 1 };
  showModal.value = true;
}
function openEdit(u) {
  editing.value = u;
  form.value = { username: u.username, display_name: u.display_name || "", role_id: u.role_id || "", password: "", status: u.status };
  showModal.value = true;
}

async function save() {
  if (!form.value.username.trim()) { notify("用户名不能为空", "err"); return; }
  if (!editing.value && !form.value.password) { notify("新建用户需设置密码", "err"); return; }
  saving.value = true;
  const payload = {
    display_name: form.value.display_name,
    role_id: form.value.role_id || null,
    status: form.value.status,
  };
  if (form.value.password) payload.password = form.value.password;
  try {
    if (editing.value) await api.updateUser(editing.value.id, payload);
    else await api.createUser({ username: form.value.username.trim(), password: form.value.password, ...payload });
    notify("已保存", "ok");
    showModal.value = false;
    load();
  } catch (e) {
    notify(e.message, "err");
  } finally {
    saving.value = false;
  }
}

async function remove(u) {
  if (!confirm(`确认删除用户「${u.username}」？`)) return;
  try {
    await api.deleteUser(u.id);
    notify("已删除", "ok");
    load();
    loadPool(); // 删除账号会释放用户池占用
  } catch (e) {
    notify(e.message, "err");
  }
}

const roleName = (id) => roles.value.find((r) => r.id === id)?.name || "—";
const statusLabel = (s) => ({ pending: "待审批", approved: "已通过", rejected: "已驳回" }[s] || s);

onMounted(() => {
  load();
  loadRegs();
  loadPool();
});
</script>

<template>
  <h2>用户管理</h2>

  <div class="tabs">
    <span v-for="t in tabs" :key="t.key" class="tab" :class="{ on: tab === t.key }" @click="tab = t.key">
      {{ t.label }}
    </span>
    <button class="btn" style="margin-left: auto" @click="load(); loadRegs(); loadPool()">刷新</button>
  </div>

  <!-- ============ 注册审批 ============ -->
  <div v-if="tab === 'registrations'">
    <div class="card card-pad">
      <table class="table">
        <thead>
          <tr>
            <th>工号</th><th>姓名</th><th>部门</th><th>授予角色</th><th>状态</th>
            <th>申请时间</th><th>审批信息</th><th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="r in regs" :key="r.id">
            <td class="mid">{{ r.emp_no }}</td>
            <td class="mid">{{ r.name }}</td>
            <td class="mid muted">{{ r.dept || "—" }}</td>
            <td class="mid">
              <select v-if="r.status === 'pending'" class="input sm" v-model="r.edit_role_id">
                <option value="">— 未指定 —</option>
                <option v-for="ro in roles" :key="ro.id" :value="ro.id">{{ ro.name }}</option>
              </select>
              <span v-else>{{ roleName(r.role_id) }}</span>
            </td>
            <td class="mid">
              <span class="badge" :class="{ on: r.status === 'approved', off: r.status !== 'approved' }">
                {{ statusLabel(r.status) }}
              </span>
            </td>
            <td class="mid muted">{{ r.apply_at || "—" }}</td>
            <td class="mid muted">
              <span v-if="r.review_at">{{ r.review_at }} · {{ r.reviewer_name || "—" }}</span>
              <span v-else>—</span>
              <div v-if="r.review_note" class="muted">备注：{{ r.review_note }}</div>
            </td>
            <td class="mid">
              <template v-if="r.status === 'pending'">
                <button class="btn sm primary" @click="approve(r)">通过</button>
                <button class="btn sm danger" @click="openReject(r)">驳回</button>
              </template>
              <span v-else-if="r.created_username" class="muted">账号 {{ r.created_username }}</span>
              <span v-else class="muted">—</span>
            </td>
          </tr>
          <tr v-if="!regs.length"><td colspan="8" class="loading">暂无注册申请</td></tr>
        </tbody>
      </table>
    </div>
  </div>

  <!-- ============ 用户池 ============ -->
  <div v-else-if="tab === 'pool'">
    <div class="toolbar">
      <button class="btn primary" @click="openPoolCreate()">+ 新增工号</button>
      <button class="btn" @click="showImport = true">批量导入</button>
      <span class="muted">工号+姓名命中用户池才允许自助注册，并按池中角色授予权限</span>
    </div>
    <div class="pool-meta">
      数据源文件：<code>{{ poolInfo.file || "config/user_pool.json" }}</code>
      <span v-if="poolInfo.source"> · 来源：{{ poolInfo.source }}</span>
      <span v-if="poolInfo.updated_at"> · 更新：{{ poolInfo.updated_at }}</span>
      <span class="muted">（可直接编辑该文件，保存后即时生效，无需重启服务）</span>
    </div>
    <div class="card card-pad">
      <table class="table">
        <thead>
          <tr>
            <th>工号</th><th>姓名</th><th>部门</th><th>预设角色</th>
            <th>状态</th><th>注册情况</th><th>备注</th><th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="p in pool" :key="p.emp_no">
            <td class="mid">{{ p.emp_no }}</td>
            <td class="mid">{{ p.name }}</td>
            <td class="mid muted">{{ p.dept || "—" }}</td>
            <td class="mid">
              {{ p.role || "—" }}
              <span v-if="p.role && !p.role_id" class="badge off" title="系统中无同名角色，审批时需手动指定">角色缺失</span>
            </td>
            <td class="mid">
              <span class="badge" :class="p.status === 1 ? 'on' : 'off'">{{ p.status === 1 ? "启用" : "停用" }}</span>
            </td>
            <td class="mid">
              <span v-if="p.used" class="badge on">已注册 {{ p.used_username || "" }}</span>
              <span v-else-if="p.pending" class="badge">待审批</span>
              <span v-else class="muted">未注册</span>
            </td>
            <td class="mid muted">{{ p.note || "—" }}</td>
            <td class="mid">
              <button class="btn sm" @click="openPoolEdit(p)">编辑</button>
              <button class="btn sm danger" @click="removePool(p)">删除</button>
            </td>
          </tr>
          <tr v-if="!pool.length"><td colspan="8" class="loading">用户池为空</td></tr>
        </tbody>
      </table>
    </div>
  </div>

  <!-- ============ 用户列表 ============ -->
  <div v-else>
    <div class="toolbar">
      <button class="btn primary" @click="openCreate()">+ 新建用户</button>
    </div>
    <div class="card card-pad">
      <table class="table">
        <thead><tr><th>用户名</th><th>显示名</th><th>角色</th><th>状态</th><th>最近登录</th><th>操作</th></tr></thead>
        <tbody>
          <tr v-for="u in users" :key="u.id">
            <td class="mid">{{ u.username }}</td>
            <td class="mid">{{ u.display_name || "—" }}</td>
            <td class="mid">{{ roleName(u.role_id) }}</td>
            <td class="mid"><span class="badge" :class="u.status === 1 ? 'on' : 'off'">{{ u.status === 1 ? '启用' : '停用' }}</span></td>
            <td class="mid muted">{{ u.last_login || "—" }}</td>
            <td class="mid">
              <button class="btn sm" @click="openEdit(u)">编辑</button>
              <button class="btn sm danger" @click="remove(u)">删除</button>
            </td>
          </tr>
          <tr v-if="!users.length"><td colspan="6" class="loading">加载中…</td></tr>
        </tbody>
      </table>
    </div>
  </div>

  <!-- 用户编辑 -->
  <Modal :show="showModal" :title="editing ? '编辑用户' : '新建用户'" @close="showModal = false">
    <div class="field">
      <label>用户名</label>
      <input class="input" v-model="form.username" :disabled="!!editing" placeholder="登录账号" />
    </div>
    <div class="field">
      <label>显示名</label>
      <input class="input" v-model="form.display_name" />
    </div>
    <div class="field">
      <label>角色</label>
      <select class="input" v-model="form.role_id">
        <option v-for="r in roles" :key="r.id" :value="r.id">{{ r.name }}</option>
      </select>
    </div>
    <div class="field">
      <label>{{ editing ? "重置密码（留空则不修改）" : "初始密码" }}</label>
      <input class="input" type="password" v-model="form.password" />
    </div>
    <div class="field" v-if="editing">
      <label>状态</label>
      <select class="input" v-model="form.status">
        <option :value="1">启用</option>
        <option :value="0">停用</option>
      </select>
    </div>
    <template #actions>
      <button class="btn" @click="showModal = false">取消</button>
      <button class="btn primary" :disabled="saving" @click="save">{{ saving ? "保存中…" : "保存" }}</button>
    </template>
  </Modal>

  <!-- 用户池编辑 -->
  <Modal :show="showPoolModal" :title="poolEditing ? '编辑用户池条目' : '新增用户池条目'" @close="showPoolModal = false">
    <div class="field">
      <label>工号</label>
      <input class="input" v-model="poolForm.emp_no" placeholder="如 E1001（注册后即为登录账号）" />
    </div>
    <div class="field">
      <label>姓名</label>
      <input class="input" v-model="poolForm.name" placeholder="须与员工真实姓名一致" />
    </div>
    <div class="field">
      <label>部门</label>
      <input class="input" v-model="poolForm.dept" />
    </div>
    <div class="field">
      <label>预设角色（决定注册后权限）</label>
      <select class="input" v-model="poolForm.role">
        <option value="">— 暂不指定（审批时再定） —</option>
        <option v-for="r in roles" :key="r.id" :value="r.name">{{ r.name }}</option>
      </select>
    </div>
    <div class="field">
      <label>状态</label>
      <select class="input" v-model="poolForm.status">
        <option :value="1">启用</option>
        <option :value="0">停用</option>
      </select>
    </div>
    <div class="field">
      <label>备注</label>
      <input class="input" v-model="poolForm.note" />
    </div>
    <template #actions>
      <button class="btn" @click="showPoolModal = false">取消</button>
      <button class="btn primary" :disabled="poolSaving" @click="savePool">{{ poolSaving ? "保存中…" : "保存" }}</button>
    </template>
  </Modal>

  <!-- 驳回原因 -->
  <Modal :show="!!rejectTarget" title="驳回注册申请" @close="rejectTarget = null">
    <p class="muted">
      驳回「{{ rejectTarget?.name }}（{{ rejectTarget?.emp_no }}）」的注册申请。
      驳回后该工号可重新提交申请。
    </p>
    <div class="field">
      <label>驳回原因（可选，会展示在审批记录中）</label>
      <input class="input" v-model="rejectNote" placeholder="如：工号与花名册不符" />
    </div>
    <template #actions>
      <button class="btn" @click="rejectTarget = null">取消</button>
      <button class="btn danger" :disabled="rejectBusy" @click="doReject">{{ rejectBusy ? "提交中…" : "确认驳回" }}</button>
    </template>
  </Modal>

  <!-- 用户池批量导入 -->
  <Modal :show="showImport" title="批量导入用户池" @close="showImport = false; importResult = null">
    <p class="muted">
      每行一条：<code>工号,姓名,部门,角色</code>（逗号或制表符分隔，部门与角色可省略；
      角色可填角色名或角色 id）。以 # 开头的行为注释。
    </p>
    <div class="field">
      <label>导入模式</label>
      <select class="input" v-model="importMode">
        <option value="merge">合并（同工号更新，新工号新增）</option>
        <option value="replace">替换（先清空未被注册占用的条目，再导入）</option>
      </select>
    </div>
    <div class="field">
      <label>花名册数据（已解析 {{ importItems.length }} 条）</label>
      <textarea
        class="input"
        rows="10"
        v-model="importText"
        placeholder="E1001,张伟,综合管理部,viewer&#10;E1002,李娜,人力资源部,editor"
      ></textarea>
    </div>
    <div v-if="importResult" class="muted">
      结果：新增 {{ importResult.created }}，更新 {{ importResult.updated }}，
      清空 {{ importResult.cleared }}，跳过 {{ importResult.skipped }}
      <span v-if="importResult.errors && importResult.errors.length">
        ；错误 {{ importResult.errors.length }} 条（第 {{
          importResult.errors.slice(0, 5).map((x) => x.row).join("、")
        }} 行）
      </span>
    </div>
    <template #actions>
      <button class="btn" @click="showImport = false; importResult = null">关闭</button>
      <button class="btn primary" :disabled="importBusy" @click="doImport">
        {{ importBusy ? "导入中…" : "开始导入" }}
      </button>
    </template>
  </Modal>
</template>

<style scoped>
.tabs {
  display: flex;
  align-items: center;
  gap: 4px;
  border-bottom: 1px solid var(--border, #e5e7eb);
  margin-bottom: 14px;
}
.tab {
  padding: 8px 14px;
  cursor: pointer;
  font-size: 14px;
  color: var(--muted, #6b7280);
  border-bottom: 2px solid transparent;
}
.tab.on {
  color: #2563eb;
  font-weight: 600;
  border-bottom-color: #2563eb;
}
.input.sm {
  padding: 3px 6px;
  font-size: 12px;
  min-width: 110px;
}
.pool-meta {
  font-size: 12px;
  color: var(--muted, #6b7280);
  margin-bottom: 10px;
  line-height: 1.7;
}
.pool-meta code {
  background: #f3f4f6;
  padding: 1px 5px;
  border-radius: 4px;
  font-size: 12px;
}
</style>
