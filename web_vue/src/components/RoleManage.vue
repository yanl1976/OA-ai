<script setup>
import { ref, onMounted, inject } from "vue";
import { api } from "../api.js";
import Modal from "./Modal.vue";

const notify = inject("notify");
const roles = ref([]);
const catalog = ref([]); // 权限目录 [{key,label,group}]
const showModal = ref(false);
const editing = ref(null);
const form = ref({ name: "", description: "", permissions: [] });
const saving = ref(false);

async function load() {
  try {
    const [r, p] = await Promise.all([api.roles(), api.permissions()]);
    roles.value = r.roles || [];
    catalog.value = p.permissions || [];
  } catch (e) {
    notify(e.message, "err");
  }
}

function openCreate() {
  editing.value = null;
  form.value = { name: "", description: "", permissions: [] };
  showModal.value = true;
}
function openEdit(role) {
  editing.value = role;
  form.value = {
    name: role.name,
    description: role.description || "",
    permissions: [...(role.permissions || [])],
  };
  showModal.value = true;
}
function togglePerm(key) {
  const i = form.value.permissions.indexOf(key);
  if (i >= 0) form.value.permissions.splice(i, 1);
  else form.value.permissions.push(key);
}

async function save() {
  if (!form.value.name.trim()) { notify("角色名称不能为空", "err"); return; }
  saving.value = true;
  try {
    if (editing.value) await api.updateRole(editing.value.id, { name: form.value.name.trim(), description: form.value.description, permissions: form.value.permissions });
    else await api.createRole({ name: form.value.name.trim(), description: form.value.description, permissions: form.value.permissions });
    notify("已保存", "ok");
    showModal.value = false;
    load();
  } catch (e) {
    notify(e.message, "err");
  } finally {
    saving.value = false;
  }
}
async function remove(role) {
  if (!confirm(`确认删除角色「${role.name}」？`)) return;
  try {
    await api.deleteRole(role.id);
    notify("已删除", "ok");
    load();
  } catch (e) {
    notify(e.message, "err");
  }
}

onMounted(load);
</script>

<template>
  <h2>角色管理</h2>
  <div class="toolbar">
    <button class="btn primary" @click="openCreate()">+ 新建角色</button>
    <button class="btn" @click="load">刷新</button>
  </div>

  <div class="card card-pad">
    <table class="table">
      <thead><tr><th>角色名称</th><th>描述</th><th>权限数</th><th>操作</th></tr></thead>
      <tbody>
        <tr v-for="r in roles" :key="r.id">
          <td class="mid">{{ r.name }}</td>
          <td class="mid muted">{{ r.description || "—" }}</td>
          <td class="mid">{{ (r.permissions || []).length }}</td>
          <td class="mid">
            <button class="btn sm" @click="openEdit(r)">编辑</button>
            <button class="btn sm danger" @click="remove(r)">删除</button>
          </td>
        </tr>
        <tr v-if="!roles.length"><td colspan="4" class="loading">加载中…</td></tr>
      </tbody>
    </table>
  </div>

  <Modal :show="showModal" :title="editing ? '编辑角色' : '新建角色'" @close="showModal = false">
    <div class="field">
      <label>角色名称</label>
      <input class="input" v-model="form.name" />
    </div>
    <div class="field">
      <label>描述</label>
      <input class="input" v-model="form.description" />
    </div>
    <div class="field">
      <label>权限分配（{{ form.permissions.length }} 项）</label>
          <div class="perm-grid">
            <label v-for="p in catalog" :key="p.key" class="perm-item">
              <input type="checkbox" :checked="form.permissions.includes(p.key)" @change="togglePerm(p.key)" />
              <span class="perm-text">
                <span class="perm-name">{{ p.name }}</span>
                <span class="perm-key muted">{{ p.key }}</span>
                <span class="perm-desc muted">{{ p.description || "" }}</span>
              </span>
            </label>
          </div>
    </div>
    <template #actions>
      <button class="btn" @click="showModal = false">取消</button>
      <button class="btn primary" :disabled="saving" @click="save">{{ saving ? "保存中…" : "保存" }}</button>
    </template>
  </Modal>
</template>
