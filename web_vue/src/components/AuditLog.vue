<script setup>
import { ref, onMounted } from "vue";
import { api } from "../api.js";
import { inject } from "vue";

const notify = inject("notify");
const items = ref([]);
const total = ref(0);
const page = ref(1);
const page_size = 50;
const q = ref("");
const action = ref("");
const actions = ref([]);

async function load() {
  try {
    const r = await api.get("/api/admin/audit", {
      params: { q: q.value, action: action.value, page: page.value, page_size },
    });
    items.value = r.items || [];
    total.value = r.total || 0;
  } catch (e) {
    notify("加载审计日志失败：" + (e.response?.data?.error || e.message), "err");
  }
}

async function loadActions() {
  try {
    const r = await api.get("/api/admin/audit/actions");
    actions.value = r.actions || [];
  } catch (e) { /* ignore */ }
}

function fmt(ts) {
  return ts || "";
}

onMounted(() => { load(); loadActions(); });
</script>

<template>
  <div class="audit">
    <h2 class="t-title">操作审计日志</h2>

    <div class="bar">
      <input v-model="q" class="fz" placeholder="搜索用户/对象/详情" @keyup.enter="load" />
      <select v-model="action" class="fz" @change="load">
        <option value="">全部动作</option>
        <option v-for="a in actions" :key="a.action" :value="a.action">
          {{ a.action }}（{{ a.c }}）
        </option>
      </select>
      <button class="btn" @click="load">查询</button>
      <span class="muted">共 {{ total }} 条</span>
    </div>

    <div v-if="!items.length" class="empty">暂无日志记录</div>
    <table v-else class="tbl">
      <thead>
        <tr><th>时间</th><th>用户</th><th>动作</th><th>对象</th><th>详情</th></tr>
      </thead>
      <tbody>
        <tr v-for="r in items" :key="r.id">
          <td class="nowrap">{{ fmt(r.ts) }}</td>
          <td>{{ r.username || "—" }}</td>
          <td><span class="act">{{ r.action }}</span></td>
          <td class="mono">{{ r.target }}</td>
          <td class="detail">{{ r.detail }}</td>
        </tr>
      </tbody>
    </table>
  </div>
</template>

<style scoped>
.audit { padding: 8px 4px; }
.t-title { margin: 4px 0 14px; font-size: 20px; }
.bar { display: flex; gap: 8px; align-items: center; margin-bottom: 12px; flex-wrap: wrap; }
.fz { border: 1px solid #dcdcdc; border-radius: 6px; padding: 6px 10px; }
.btn { border: 1px solid #2b6cb0; background: #2b6cb0; color: #fff; border-radius: 6px;
  padding: 6px 14px; cursor: pointer; }
.muted { color: #999; font-size: 13px; }
.tbl { width: 100%; border-collapse: collapse; background: #fff; }
.tbl th, .tbl td { text-align: left; padding: 8px 10px; border-bottom: 1px solid #f0f0f0; vertical-align: top; }
.nowrap { white-space: nowrap; color: #888; font-size: 13px; }
.mono { font-family: monospace; font-size: 12px; color: #555; word-break: break-all; }
.detail { color: #666; font-size: 13px; }
.act { background: #eef4fb; color: #2b6cb0; border-radius: 4px; padding: 2px 8px; font-size: 12px; }
.empty { color: #aaa; padding: 30px 0; text-align: center; }
</style>
