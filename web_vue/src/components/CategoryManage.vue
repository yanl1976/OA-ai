<script setup>
import { ref, onMounted, inject, computed } from "vue";
import { api } from "../api.js";
import Modal from "./Modal.vue";

const notify = inject("notify");

const rows = ref([]); // 扁平化(含 depth)后的分类
const loading = ref(false);
const showModal = ref(false);
const editing = ref(null); // null=新建
const form = ref({ name: "", description: "", parent_id: "", sort_order: 0 });
const saving = ref(false);

function flatten(list) {
  const map = {};
  list.forEach((c) => (map[c.id] = { ...c, children: [] }));
  const rs = [];
  list.forEach((c) => {
    if (c.parent_id && map[c.parent_id]) map[c.parent_id].children.push(map[c.id]);
    else rs.push(map[c.id]);
  });
  const out = [];
  const walk = (nodes, d) => {
    nodes.forEach((n) => {
      out.push({ ...n, depth: d });
      walk(n.children, d + 1);
    });
  };
  walk(rs, 0);
  return out;
}

async function load() {
  loading.value = true;
  try {
    const r = await api.categoriesAll();
    rows.value = flatten(r.categories || []);
  } catch (e) {
    notify(e.message, "err");
  } finally {
    loading.value = false;
  }
}

// 可选上级：排除自身及其后代，避免成环
const parentOptions = computed(() => {
  if (!editing.value) return rows.value;
  const forbid = new Set([editing.value.id]);
  const stack = [editing.value.id];
  while (stack.length) {
    const id = stack.pop();
    rows.value.filter((r) => r.parent_id === id).forEach((r) => {
      if (!forbid.has(r.id)) { forbid.add(r.id); stack.push(r.id); }
    });
  }
  return rows.value.filter((r) => !forbid.has(r.id));
});

function openCreate(parentId = "") {
  editing.value = null;
  form.value = { name: "", description: "", parent_id: parentId ? String(parentId) : "", sort_order: 0 };
  showModal.value = true;
}
function openEdit(row) {
  editing.value = row;
  form.value = {
    name: row.name,
    description: row.description || "",
    parent_id: row.parent_id != null ? String(row.parent_id) : "",
    sort_order: row.sort_order || 0,
  };
  showModal.value = true;
}

async function save() {
  if (!form.value.name.trim()) {
    notify("分类名称不能为空", "err");
    return;
  }
  saving.value = true;
  const payload = {
    name: form.value.name.trim(),
    description: form.value.description,
    parent_id: form.value.parent_id || null,
    sort_order: Number(form.value.sort_order) || 0,
  };
  try {
    if (editing.value) await api.updateCategory(editing.value.id, payload);
    else await api.createCategory(payload);
    notify("已保存", "ok");
    showModal.value = false;
    load();
  } catch (e) {
    notify(e.message, "err");
  } finally {
    saving.value = false;
  }
}

async function remove(row) {
  if (!confirm(`确认删除分类「${row.name}」？若有子分类将被阻止。`)) return;
  try {
    await api.deleteCategory(row.id);
    notify("已删除", "ok");
    load();
  } catch (e) {
    notify(e.message, "err");
  }
}

onMounted(load);
</script>

<template>
  <h2>分类管理</h2>
  <div class="toolbar">
    <button class="btn primary" @click="openCreate()">+ 新建一级分类</button>
    <button class="btn" @click="load">刷新</button>
  </div>

  <div class="card card-pad">
    <table class="table">
      <thead>
        <tr><th>分类名称</th><th>上级</th><th>文档数</th><th>状态</th><th>操作</th></tr>
      </thead>
      <tbody>
        <tr v-for="r in rows" :key="r.id">
          <td class="mid">
            <span :style="{ paddingLeft: r.depth * 18 + 'px' }">
              <span v-if="r.depth" style="color: var(--muted)">└ </span>{{ r.name }}
            </span>
          </td>
          <td class="mid muted">{{ r.parent_id ? (rows.find(x => x.id === r.parent_id)?.name || '—') : '（顶级）' }}</td>
          <td class="mid">{{ r.doc_count }}</td>
          <td class="mid">
            <span class="badge" :class="r.status === 1 ? 'on' : 'off'">{{ r.status === 1 ? '启用' : '停用' }}</span>
          </td>
          <td class="mid">
            <button class="btn sm" @click="openCreate(r.id)">+子级</button>
            <button class="btn sm" @click="openEdit(r)">编辑</button>
            <button class="btn sm danger" @click="remove(r)">删除</button>
          </td>
        </tr>
        <tr v-if="!rows.length"><td colspan="5" class="loading">加载中…</td></tr>
      </tbody>
    </table>
  </div>

  <Modal :show="showModal" :title="editing ? '编辑分类' : '新建分类'" @close="showModal = false">
    <div class="field">
      <label>分类名称</label>
      <input class="input" v-model="form.name" placeholder="如：管理标准分类" />
    </div>
    <div class="field">
      <label>上级分类</label>
      <select class="input" v-model="form.parent_id">
        <option value="">（顶级分类）</option>
        <option v-for="o in parentOptions" :key="o.id" :value="String(o.id)">{{ '　'.repeat(o.depth) }}{{ o.name }}</option>
      </select>
    </div>
    <div class="field">
      <label>描述</label>
      <textarea class="input" v-model="form.description" placeholder="可选"></textarea>
    </div>
    <div class="field">
      <label>排序</label>
      <input class="input" type="number" v-model="form.sort_order" />
    </div>
    <template #actions>
      <button class="btn" @click="showModal = false">取消</button>
      <button class="btn primary" :disabled="saving" @click="save">{{ saving ? "保存中…" : "保存" }}</button>
    </template>
  </Modal>
</template>
