<script setup>
import { ref, onMounted, inject } from "vue";
import { api } from "../api.js";
import Modal from "./Modal.vue";

const notify = inject("notify");
const roles = ref([]);
const catalog = ref([]); // 权限目录 [{key,label,group}]
const categories = ref([]); // 分类树（含 id/parent_id/name）
const showModal = ref(false);
const editing = ref(null);
const form = ref({ name: "", description: "", permissions: [] });
const saving = ref(false);

// 非分类权限（普通网格展示）
const normalCatalog = () => catalog.value.filter((p) => p.group !== "分类权限");
// 分类权限 key 查找：kb.cat.<id>.<action>
function catPermKey(catId, action) {
  return `kb.cat.${catId}.${action}`;
}
// 分类树（带 depth），仅展示分类权限涉及的节点
const catTree = () => {
  const cats = categories.value || [];
  const byParent = {};
  cats.forEach((c) => (byParent[c.parent_id || 0] = byParent[c.parent_id || 0] || []).push(c));
  const out = [];
  const walk = (pid, depth) => {
    (byParent[pid] || []).forEach((c) => {
      out.push({ ...c, depth });
      walk(c.id, depth + 1);
    });
  };
  walk(0, 0);
  return out;
};
const ACTIONS = [
  { key: "view", label: "浏览" },
  { key: "search", label: "查询" },
  { key: "download", label: "下载" },
];

async function load() {
  try {
    const [r, p, c] = await Promise.all([
      api.roles(),
      api.permissions(),
      api.categoriesAll(),
    ]);
    roles.value = r.roles || [];
    catalog.value = p.permissions || [];
    categories.value = c.categories || [];
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
// 分类权限级联勾选：勾选某节点的某操作，同时勾选其全部后代节点的同操作
function toggleCatPerm(cat, action, cascade) {
  const key = catPermKey(cat.id, action);
  const has = form.value.permissions.includes(key);
  if (cascade) {
    const ids = [cat.id];
    // 收集后代
    const byParent = {};
    (categories.value || []).forEach((c) => (byParent[c.parent_id || 0] = byParent[c.parent_id || 0] || []).push(c));
    const stack = [cat.id];
    while (stack.length) {
      const cur = stack.pop();
      (byParent[cur] || []).forEach((ch) => { ids.push(ch.id); stack.push(ch.id); });
    }
    if (has) {
      // 取消：移除该操作的所有相关 key
      form.value.permissions = form.value.permissions.filter(
        (k) => !ids.some((id) => k === catPermKey(id, action))
      );
    } else {
      ids.forEach((id) => {
        const k = catPermKey(id, action);
        if (!form.value.permissions.includes(k)) form.value.permissions.push(k);
      });
    }
  } else {
    togglePerm(key);
  }
}
function catChecked(cat, action) {
  return form.value.permissions.includes(catPermKey(cat.id, action));
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

      <!-- 分类权限矩阵：按分类树层级展示，每个节点一行含 浏览/查询/下载 -->
      <div class="cat-matrix">
        <div class="cat-matrix-head">
          <span class="cat-col-name">分类（细化到子类）</span>
          <span class="cat-col-act">浏览</span>
          <span class="cat-col-act">查询</span>
          <span class="cat-col-act">下载</span>
        </div>
        <div v-for="c in catTree().filter(t => t.depth === 0)" :key="'cat-' + c.id" class="cat-matrix-row">
          <span class="cat-col-name">{{ c.name }}</span>
          <span class="cat-col-act">
            <input type="checkbox" :checked="catChecked(c, 'view')"
                   @change="toggleCatPerm(c, 'view', true)" title="勾选将级联应用到其下全部子类" />
          </span>
          <span class="cat-col-act">
            <input type="checkbox" :checked="catChecked(c, 'search')"
                   @change="toggleCatPerm(c, 'search', true)" title="勾选将级联应用到其下全部子类" />
          </span>
          <span class="cat-col-act">
            <input type="checkbox" :checked="catChecked(c, 'download')"
                   @change="toggleCatPerm(c, 'download', true)" title="勾选将级联应用到其下全部子类" />
          </span>
        </div>
        <p class="muted cat-hint">权限仅需在顶层分类设置：勾选某顶层分类的某操作会自动级联应用到其全部子类（子类无需单独设置，自动继承父级权限）。新增顶层分类会自动出现在此矩阵中。</p>
      </div>

      <!-- 其他（非分类）权限网格 -->
      <div class="perm-grid" style="margin-top: 14px">
        <label v-for="p in normalCatalog()" :key="p.key" class="perm-item">
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

<style scoped>
.cat-matrix {
  border: 1px solid #e4e7ed; border-radius: 6px; overflow: hidden;
}
.cat-matrix-head, .cat-matrix-row {
  display: flex; align-items: center; padding: 7px 10px;
  border-bottom: 1px solid #f0f0f0;
}
.cat-matrix-head {
  background: #f5f7fa; font-weight: 600; font-size: 13px; color: #606266;
}
.cat-matrix-row:nth-child(even) { background: #fafbfc; }
.cat-col-name { flex: 1; font-size: 13px; color: #303133; }
.cat-col-act {
  width: 56px; text-align: center; font-size: 13px; color: #606266;
}
.cat-hint { font-size: 12px; padding: 8px 10px; margin: 0; }
</style>
