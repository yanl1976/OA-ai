<script setup>
import { ref, onMounted, inject, computed } from "vue";
import { api } from "../api.js";

const notify = inject("notify");
const perms = ref([]);

// 后端返回 {key, name, description, group}；按 group 分组展示中文说明
const grouped = computed(() => {
  const g = {};
  perms.value.forEach((p) => {
    const grp = p.group || "其他";
    (g[grp] = g[grp] || []).push(p);
  });
  return Object.entries(g).map(([group, items]) => ({ group, items }));
});

onMounted(async () => {
  try {
    const r = await api.permissions();
    perms.value = r.permissions || [];
  } catch (e) {
    notify(e.message, "err");
  }
});
</script>

<template>
  <h2>权限目录</h2>
  <p class="muted">系统内置权限项（含中文说明）。角色在「角色管理」中按需分配这些权限。</p>
  <div class="card card-pad">
    <div v-for="g in grouped" :key="g.group" style="margin-bottom: 18px">
      <div class="nav-group-title" style="padding-left: 0">{{ g.group }}</div>
      <table class="table">
        <thead>
          <tr><th style="width:34%">权限标识</th><th>名称</th><th>中文说明</th></tr>
        </thead>
        <tbody>
          <tr v-for="p in g.items" :key="p.key">
            <td class="mid"><code>{{ p.key }}</code></td>
            <td class="mid"><b>{{ p.name }}</b></td>
            <td class="mid">{{ p.description || "—" }}</td>
          </tr>
        </tbody>
      </table>
    </div>
    <div v-if="!perms.length" class="loading">加载中…</div>
  </div>
</template>
