<script setup>
import { ref, onMounted, inject } from "vue";
import { api } from "../api.js";
import Modal from "./Modal.vue";

const notify = inject("notify");
const users = ref([]);
const roles = ref([]);
const showModal = ref(false);
const editing = ref(null);
const form = ref({ username: "", display_name: "", role_id: "", password: "", status: 1 });
const saving = ref(false);

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
  } catch (e) {
    notify(e.message, "err");
  }
}

const roleName = (id) => roles.value.find((r) => r.id === id)?.name || "—";

onMounted(load);
</script>

<template>
  <h2>用户管理</h2>
  <div class="toolbar">
    <button class="btn primary" @click="openCreate()">+ 新建用户</button>
    <button class="btn" @click="load">刷新</button>
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
</template>
