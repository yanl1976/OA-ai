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
import Dashboard from "./components/Dashboard.vue";
import TagBrowse from "./components/TagBrowse.vue";
import TrashManage from "./components/TrashManage.vue";
import AuditLog from "./components/AuditLog.vue";
import DocDetail from "./components/DocDetail.vue";
import ChatView from "./components/ChatView.vue";

const user = ref(null);
const loading = ref(true);
const current = ref("Dashboard");
const toast = reactive({ show: false, msg: "", type: "" });
let toastTimer = null;

// 跨页跳转：从衍生版本页跳回原版文档 / 跳到某衍生版本
const pendingDocId = ref(null);
const pendingDerivedId = ref(null);
// 跨页跳转：从知识浏览页直接打开某文档的「纪要二次生成」编辑页（作为来源纪要）
const pendingDerivedSourceId = ref(null);
const detailDocId = ref("");

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
// 从知识浏览页点击「纪要二次生成」：以指定文档作为来源纪要，直接打开二次编辑页
function openDerivedForDoc(docId) {
  pendingDerivedSourceId.value = docId;
  current.value = "MeetingDerived";
}
function openDocDetail(docId) {
  detailDocId.value = docId;
  current.value = "DocDetail";
}
function navigate(key) {
  if (key === "DocDetail") return;
  current.value = key;
}
window.addEventListener("nav", (e) => navigate(e.detail));
provide("notify", notify);
provide("user", user);
provide("openDocInBrowse", openDocInBrowse);
provide("openDerivedInManage", openDerivedInManage);
provide("openDocDetail", openDocDetail);
provide("pendingDocId", pendingDocId);
provide("pendingDerivedId", pendingDerivedId);
provide("openDerivedForDoc", openDerivedForDoc);
provide("pendingDerivedSourceId", pendingDerivedSourceId);

// 导航：按权限过滤。perm 为所需权限 key，无 perm 表示人人可见。
const groups = [
  {
    title: "知识库",
    items: [
      { key: "Dashboard", label: "概览首页", icon: "🏠", perm: "cat:view" },
      { key: "KbBrowse", label: "知识浏览", icon: "📚", perm: "cat:view" },
      { key: "TagBrowse", label: "标签浏览", icon: "🏷", perm: "cat:view" },
      { key: "SearchView", label: "全文检索", icon: "🔍", perm: "cat:search" },
      { key: "ChatView", label: "智能对话", icon: "💬", perm: "cat:search" },
      { key: "UploadView", label: "上传文档", icon: "⬆", perm: "kb.doc.upload" },
      { key: "MeetingDerived", label: "会议纪要二次生成", icon: "✂", perm: "derived.manage" },
    ],
  },
  {
    title: "管理",
    items: [
      { key: "CategoryManage", label: "分类管理", icon: "🗂", perm: "kb.category.manage" },
      { key: "UploadManage", label: "上传管理", icon: "🗃", perm: "kb.upload.manage" },
      { key: "TrashManage", label: "回收站", icon: "🗑", perm: "kb.upload.manage" },
      { key: "UserManage", label: "用户管理", icon: "👤", perm: "user.view" },
      { key: "RoleManage", label: "角色管理", icon: "🎭", perm: "role.manage" },
      { key: "PermissionView", label: "权限目录", icon: "🔐", perm: "permission.view" },
    ],
  },
  {
    title: "系统管理",
    items: [
      { key: "SystemManage", label: "系统设置", icon: "🛠", perm: "system.manage" },
      { key: "AuditLog", label: "操作日志", icon: "📜", perm: "system.manage" },
    ],
  },
];

const visibleItems = computed(() => {
  const perms = user.value?.permissions || [];
  const hasCatView = perms.some((p) => p.startsWith("kb.cat.") && p.endsWith(".view"));
  const hasCatSearch = perms.some((p) => p.startsWith("kb.cat.") && p.endsWith(".search"));
  return groups
    .map((g) => ({
      title: g.title,
      items: g.items.filter((it) => {
        if (!it.perm) return true;
        if (it.perm === "cat:view") return hasCatView;
        if (it.perm === "cat:search") return hasCatSearch;
        return perms.includes(it.perm);
      }),
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
  Dashboard,
  KbBrowse,
  TagBrowse,
  SearchView,
  UploadView,
  UploadManage,
  TrashManage,
  CategoryManage,
  UserManage,
  RoleManage,
  PermissionView,
  SystemManage,
  MeetingDerived,
  AuditLog,
  GraphView,
  DocDetail,
  ChatView,
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
        <component :is="views[current]" :docId="detailDocId" />
      </div>
    </div>

    <div v-if="toast.show" class="toast" :class="toast.type">{{ toast.msg }}</div>
  </div>
</template>
