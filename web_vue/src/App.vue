<script setup>
import { ref, reactive, onMounted, provide, computed } from "vue";
import { api } from "./api.js";
import Login from "./components/Login.vue";
import KbBrowse from "./components/KbBrowse.vue";
import SearchView from "./components/SearchView.vue";
import UploadView from "./components/UploadView.vue";
import UploadManage from "./components/UploadManage.vue";
import CategoryManage from "./components/CategoryManage.vue";
import UserManage from "./components/UserManage.vue";
import RoleManage from "./components/RoleManage.vue";
import PermissionView from "./components/PermissionView.vue";
import SystemManage from "./components/SystemManage.vue";
import MeetingDerived from "./components/MeetingDerived.vue";
import GraphView from "./components/GraphView.vue";

const user = ref(null);
const loading = ref(true);
const current = ref("KbBrowse");
const toast = reactive({ show: false, msg: "", type: "" });
let toastTimer = null;

// 跨页跳转：从衍生版本页跳回原版文档 / 跳到某衍生版本
const pendingDocId = ref(null);
const pendingDerivedId = ref(null);

function notify(msg, type = "") {
  toast.msg = msg;
  toast.type = type;
  toast.show = true;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (toast.show = false), 2600);
}
function openDocInBrowse(docId) {
  pendingDocId.value = docId;
  current.value = "KbBrowse";
}
function openDerivedInManage(derivedId) {
  pendingDerivedId.value = derivedId;
  current.value = "MeetingDerived";
}
provide("notify", notify);
provide("user", user);
provide("openDocInBrowse", openDocInBrowse);
provide("openDerivedInManage", openDerivedInManage);
provide("pendingDocId", pendingDocId);
provide("pendingDerivedId", pendingDerivedId);

// 导航：按权限过滤。perm 为所需权限 key，无 perm 表示人人可见。
const groups = [
  {
    title: "知识库",
    items: [
      { key: "KbBrowse", label: "知识浏览", icon: "📚", perm: "kb.view" },
      { key: "SearchView", label: "全文检索", icon: "🔍", perm: "kb.search" },
      { key: "UploadView", label: "上传文档", icon: "⬆", perm: "kb.doc.upload" },
      { key: "MeetingDerived", label: "会议纪要二次生成", icon: "✂", perm: "derived.manage" },
    ],
  },
  {
    title: "管理",
    items: [
      { key: "CategoryManage", label: "分类管理", icon: "🗂", perm: "kb.category.manage" },
      { key: "UploadManage", label: "上传管理", icon: "🗃", perm: "kb.upload.manage" },
      { key: "UserManage", label: "用户管理", icon: "👤", perm: "user.view" },
      { key: "RoleManage", label: "角色管理", icon: "🎭", perm: "role.manage" },
      { key: "PermissionView", label: "权限目录", icon: "🔐", perm: "permission.view" },
    ],
  },
  {
    title: "系统管理",
    items: [
      { key: "SystemManage", label: "系统设置", icon: "🛠", perm: "system.manage" },
    ],
  },
];

const visibleItems = computed(() => {
  const perms = user.value?.permissions || [];
  return groups
    .map((g) => ({
      title: g.title,
      items: g.items.filter((it) => !it.perm || perms.includes(it.perm)),
    }))
    .filter((g) => g.items.length);
});

async function logout() {
  await api.logout().catch(() => {});
  user.value = null;
  current.value = "KbBrowse";
}

onMounted(async () => {
  try {
    const r = await api.me();
    if (r.user) user.value = r.user;
  } catch (e) {
    /* 未登录 */
  } finally {
    loading.value = false;
  }
});

const views = {
  KbBrowse,
  SearchView,
  UploadView,
  UploadManage,
  CategoryManage,
  UserManage,
  RoleManage,
  PermissionView,
  SystemManage,
  MeetingDerived,
  GraphView,
};
</script>

<template>
  <div v-if="loading" class="loading">加载中…</div>

  <Login v-else-if="!user" @ok="(u) => (user = u)" />

  <div v-else class="app-shell">
    <div class="topbar">
      <div class="brand">OA 知识库门户</div>
      <div class="user">
        <span>👤 {{ user.display_name || user.username }}</span>
        <span class="muted">（{{ user.role_name || "—" }}）</span>
        <a class="btn sm" href="/graph" target="_blank">3D 图谱</a>
        <button class="btn sm" @click="logout">退出</button>
      </div>
    </div>

    <div class="body">
      <div class="sidebar">
        <template v-for="g in visibleItems" :key="g.title">
          <div class="nav-group-title">{{ g.title }}</div>
          <div
            v-for="it in g.items"
            :key="it.key"
            class="nav-item"
            :class="{ active: current === it.key }"
            @click="current = it.key"
          >
            <span>{{ it.icon }}</span><span>{{ it.label }}</span>
          </div>
        </template>
      </div>

      <div class="content">
        <component :is="views[current]" />
      </div>
    </div>

    <div v-if="toast.show" class="toast" :class="toast.type">{{ toast.msg }}</div>
  </div>
</template>
