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
const isAdmin = computed(() => user.value?.role_name === "admin");
const loading = ref(true);
const current = ref("Dashboard");
const toast = reactive({ show: false, msg: "", type: "" });
let toastTimer = null;

// 系统名称：与系统设置中的「系统名称」卡片同步（来自 /api/system/info）
const systemName = ref("OA-AI 知识库");
function setSystemName(name) {
  if (name) systemName.value = name;
}

// 页面水印：开关来自功能开关 watermark_enabled；内容 = 使用者账号 + 姓名
const watermarkOn = ref(true);
const watermarkText = ref("");
function setWatermark(text) {
  watermarkText.value = text || "";
}

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
  if (key === "SystemManage" && !isAdmin.value) return; // 系统设置仅管理员可访问
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
provide("systemName", systemName);
provide("setSystemName", setSystemName);
provide("watermarkOn", watermarkOn);
provide("watermarkText", watermarkText);
provide("setWatermark", setWatermark);

// 水印平铺格子（覆盖视口，旋转展示账号+姓名）
const watermarkTiles = computed(() => {
  if (!watermarkOn.value || !watermarkText.value) return [];
  const cols = 5, rows = 8;
  const arr = [];
  for (let r = 0; r < rows; r++) {
    for (let c = 0; c < cols; c++) {
      arr.push({ id: r * cols + c, top: (r * (100 / rows)) + (c % 2 ? 6 : 0), left: c * (100 / cols) });
    }
  }
  return arr;
});

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
    title: "系统管理",
    items: [
      { key: "SystemManage", label: "系统设置", icon: "🛠", adminOnly: true },
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
        if (it.adminOnly) return isAdmin.value; // 系统设置仅管理员可见
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
    if (r.user) {
      user.value = r.user;
      // 水印内容：使用者账号 + 姓名
      const uname = r.user.username || "";
      const dname = r.user.display_name || "";
      watermarkText.value = dname ? `${uname}（${dname}）` : uname;
    }
  } catch (e) {
    /* 未登录 */
  }
  // 加载系统名称（与系统设置同步）
  try {
    const si = await api.systemInfo();
    if (si && si.system_name) systemName.value = si.system_name;
  } catch (e) {
    /* 取不到则用默认 */
  }
  // 加载水印开关（功能开关 watermark_enabled）
  try {
    const f = await api.features();
    const wm = (f.features || []).find((x) => x.key === "watermark_enabled");
    if (wm) watermarkOn.value = !!wm.enabled;
  } catch (e) {
    /* 取不到则用默认开启 */
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
      <div class="brand">{{ systemName }}</div>
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
        <div v-if="current === 'SystemManage' && !isAdmin" class="no-access">
          <h2>无访问权限</h2>
          <p class="muted">系统设置仅限管理员访问。如需使用该功能请联系系统管理员。</p>
        </div>
        <component v-else :is="views[current]" :docId="detailDocId" />
      </div>
    </div>

    <div v-if="toast.show" class="toast" :class="toast.type">{{ toast.msg }}</div>

    <!-- 全局页面水印：覆盖所有内容显示页，内容为使用者账号+姓名 -->
    <div v-if="watermarkOn && watermarkText" class="watermark-layer" aria-hidden="true">
      <span
        v-for="t in watermarkTiles"
        :key="t.id"
        class="watermark-item"
        :style="{ top: t.top + '%', left: t.left + '%' }"
      >{{ watermarkText }}</span>
    </div>
  </div>
</template>
