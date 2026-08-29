<script setup>
import { ref, onMounted, inject, computed } from "vue";
import { api } from "../api.js";
import Modal from "./Modal.vue";
import CategoryManage from "./CategoryManage.vue";
import UploadManage from "./UploadManage.vue";
import TrashManage from "./TrashManage.vue";
import UserManage from "./UserManage.vue";
import RoleManage from "./RoleManage.vue";
import PermissionView from "./PermissionView.vue";
import AuditLog from "./AuditLog.vue";

const notify = inject("notify");
const user = inject("user");
const loading = ref(true);

// 管理功能卡片：点击后进入二级页面（在系统设置内嵌渲染）
const sub = ref(null); // 当前二级页 key，null 表示显示卡片网格
const subViews = {
  CategoryManage,
  UploadManage,
  TrashManage,
  UserManage,
  RoleManage,
  PermissionView,
  AuditLog,
};
const adminCards = [
  { key: "CategoryManage", icon: "🗂", title: "分类管理", desc: "管理知识库分类与层级", perm: "kb.category.manage" },
  { key: "UploadManage", icon: "🗃", title: "上传管理", desc: "查看与管理已上传文档", perm: "kb.upload.manage" },
  { key: "TrashManage", icon: "🗑", title: "回收站", desc: "恢复或彻底删除软删除文档", perm: "kb.upload.manage" },
  { key: "UserManage", icon: "👤", title: "用户管理", desc: "管理系统用户与启用状态", perm: "user.view" },
  { key: "RoleManage", icon: "🎭", title: "角色管理", desc: "管理角色与权限分配", perm: "role.manage" },
  { key: "PermissionView", icon: "🔐", title: "权限目录", desc: "查看系统内置权限项", perm: "permission.view" },
  { key: "AuditLog", icon: "📜", title: "操作日志", desc: "查看系统操作审计记录", perm: "system.manage" },
];
const visibleAdminCards = computed(() => {
  const perms = (user && user.value && user.value.permissions) || [];
  return adminCards.filter((c) => {
    if (!c.perm) return true;
    if (c.perm === "cat:view") return perms.some((p) => p.startsWith("kb.cat.") && p.endsWith(".view"));
    return perms.includes(c.perm);
  });
});
function openSub(key) {
  sub.value = key;
}
function backToCards() {
  sub.value = null;
}

const features = ref([]);
const stats = ref(null);
const health = ref(null);
const vecStats = ref(null);
const sysInfo = ref({ system_name: "", version: "", commits: 0 });

const activeCard = ref(null); // 当前打开的弹窗 key
const reindexing = ref(false);
const me = ref(null); // 当前登录用户

async function load() {
  loading.value = true;
  try {
    const [f, s, h, v, m, si] = await Promise.all([
      api.features(),
      api.stats(),
      api.health(),
      api.vectorStats(),
      api.me(),
      api.systemInfo(),
    ]);
    features.value = f.features || [];
    stats.value = s;
    health.value = h;
    vecStats.value = v;
    me.value = (m && m.user) || null;
    sysInfo.value = si || { system_name: "", version: "", commits: 0 };
  } catch (e) {
    notify(e.message, "err");
  } finally {
    loading.value = false;
  }
}

const isAdmin = computed(() => me.value && me.value.role_name === "admin");

const enabledCount = computed(() => features.value.filter((f) => f.enabled).length);

async function toggleFeature(feat) {
  feat.busy = true;
  try {
    const next = feat.enabled ? 0 : 1;
    await api.setFeature(feat.key, next);
    feat.enabled = next;
    notify("已更新", "ok");
  } catch (e) {
    notify(e.message, "err");
  } finally {
    feat.busy = false;
  }
}

async function reindex() {
  reindexing.value = true;
  try {
    const r = await api.reindex();
    vecStats.value = (r && r.stats) || vecStats.value;
    notify("索引重建完成", "ok");
    await load();
  } catch (e) {
    notify(e.message, "err");
  } finally {
    reindexing.value = false;
  }
}

// ============ 系统初始化（清除文档 / 提取内容 / 重建索引） ============
const busyClear = ref(false);
const busyExtract = ref(false);
const busyClearExtract = ref(false);
const busyIndex = ref(false);
const busyAbort = ref(false);

async function initClear() {
  if (!confirm("确定清空全部文档（含回收站）并重建空索引？此操作不可恢复。")) return;
  busyClear.value = true;
  try {
    const r = await api.initClear(true);
    notify("已清空全部文档（删除文件 %d 条 / 条目 %d 条）".replace("%d", r.removed_files).replace("%d", r.removed_entries), "ok");
    await load();
    activeCard.value = null;
  } catch (e) {
    notify(e.message, "err");
  } finally {
    busyClear.value = false;
  }
}

async function initExtract() {
  busyExtract.value = true;
  try {
    const r = await api.initExtract();
    // 后台异步执行：接口仅完成入队即返回，页面可立即关闭弹窗继续操作
    notify((r.note || "已提交后台重新提取").replace("%d", r.queued || 0), "ok");
    activeCard.value = null;
  } catch (e) {
    notify(e.message, "err");
  } finally {
    busyExtract.value = false;
  }
}

async function initClearExtract() {
  if (!confirm("确定清空全部文档的提取内容（保留文件）？状态将变为「未识别」、字数归 0，需重新提取才能恢复。")) return;
  busyClearExtract.value = true;
  try {
    const r = await api.initClearExtract();
    notify((r.note || "已清空提取内容").replace("%d", r.cleared || 0), "ok");
    activeCard.value = null;
  } catch (e) {
    notify(e.message, "err");
  } finally {
    busyClearExtract.value = false;
  }
}

async function initIndex() {
  busyIndex.value = true;
  try {
    const r = await api.initIndex();
    notify(r.note || "已提交后台重建索引，可在后台执行期间继续操作", "ok");
    activeCard.value = null;
  } catch (e) {
    notify(e.message, "err");
  } finally {
    busyIndex.value = false;
  }
}

async function initAbort() {
  if (!confirm("确定中止后台提取？已提取的文档将保留并重建索引，队列中剩余任务被丢弃。")) return;
  busyAbort.value = true;
  try {
    const r = await api.initAbort();
    notify(r.note || "已中止后台提取", "ok");
  } catch (e) {
    notify(e.message, "err");
  } finally {
    busyAbort.value = false;
  }
}

// ============ 系统名称设定 ============
const nameDraft = ref("");
const nameBusy = ref(false);

async function saveSystemName() {
  const name = nameDraft.value.trim();
  if (!name) { notify("系统名称不能为空", "err"); return; }
  nameBusy.value = true;
  try {
    const r = await api.setSystemName(name);
    sysInfo.value.system_name = r.system_name;
    notify("系统名称已更新", "ok");
    activeCard.value = null;
  } catch (e) {
    notify(e.message, "err");
  } finally {
    nameBusy.value = false;
  }
}

const cards = computed(() => {
  const base = [
    { key: "features", icon: "⚙", title: "功能开关", desc: "开启或关闭系统功能模块" },
    { key: "index", icon: "🔎", title: "检索索引", desc: "全文检索向量索引状态与重建" },
    { key: "stats", icon: "📊", title: "数据统计", desc: "文档、用户与分类统计概览" },
    { key: "info", icon: "💡", title: "系统信息", desc: "运行环境与版本信息" },
    { key: "sysname", icon: "🏷", title: "系统名称", desc: "设定系统名称与查看版本" },
  ];
  if (isAdmin.value) {
    base.splice(2, 0, { key: "init", icon: "🔧", title: "系统初始化", desc: "清除文档 / 提取内容 / 重建索引" });
  }
  return base;
});

function statusText(key) {
  if (key === "features") return `${enabledCount.value}/${features.value.length} 已开启`;
  if (key === "index") return (health.value && health.value.vec_ready) ? "索引就绪" : "索引缺失";
  if (key === "stats") return `${(stats.value && stats.value.total_documents) || 0} 份文档`;
  if (key === "info") {
    const ok = health.value && health.value.status === "ok";
    return ok ? "运行正常" : "需关注";
  }
  if (key === "sysname") return `v${sysInfo.value.version || "—"}`;
  if (key === "init") return "3 项操作";
  return "";
}

onMounted(load);
</script>

<template>
  <div class="sys-manage">
    <h2>系统设置</h2>
    <p class="muted">集中管理系统功能开关、检索索引、数据统计与运行信息，点击卡片进入设置。</p>

    <!-- 二级页面：管理功能 -->
    <div v-if="sub" class="sub-view">
      <button class="btn sm" @click="backToCards">← 返回系统设置</button>
      <component :is="subViews[sub]" />
    </div>

    <!-- 卡片网格 -->
    <div v-else>
      <div v-if="loading" class="loading">加载中…</div>
      <div v-else>
        <!-- 管理功能入口卡片 -->
        <h3 class="sub-title">管理功能</h3>
        <div class="sys-card-grid">
          <div
            v-for="c in visibleAdminCards"
            :key="c.key"
            class="sys-card admin-card"
            @click="openSub(c.key)"
          >
            <div class="sys-card-icon">{{ c.icon }}</div>
            <div class="sys-card-title">{{ c.title }}</div>
            <div class="sys-card-desc">{{ c.desc }}</div>
            <div class="sys-card-go">进入 →</div>
          </div>
        </div>

        <!-- 系统维护卡片 -->
        <h3 class="sub-title">系统维护</h3>
        <div class="sys-card-grid">
          <div
            v-for="c in cards"
            :key="c.key"
            class="sys-card"
            :class="{ warn: c.key !== 'features' && statusText(c.key) === '索引缺失' }"
            @click="activeCard = c.key"
          >
            <div class="sys-card-icon">{{ c.icon }}</div>
            <div class="sys-card-title">{{ c.title }}</div>
            <div class="sys-card-desc">{{ c.desc }}</div>
            <div class="sys-card-status">{{ statusText(c.key) }}</div>
          </div>
        </div>
      </div>
    </div>

  <!-- 功能开关 -->
  <Modal :show="activeCard === 'features'" title="功能开关" @close="activeCard = null">
    <p class="muted">实时切换系统功能模块的启用状态。</p>
    <div class="feat-list">
      <div v-for="f in features" :key="f.key" class="feat-row">
        <div class="feat-meta">
          <div class="feat-name"><code>{{ f.key }}</code></div>
          <div class="muted" style="font-size: 12px">{{ f.label }}</div>
        </div>
        <button
          class="btn sm"
          :class="f.enabled ? 'danger' : 'primary'"
          :disabled="f.busy"
          @click="toggleFeature(f)"
        >
          {{ f.enabled ? "关闭" : "开启" }}
        </button>
      </div>
      <div v-if="!features.length" class="loading">暂无功能项</div>
    </div>
    <template #actions>
      <button class="btn" @click="activeCard = null">关闭</button>
    </template>
  </Modal>

  <!-- 检索索引 -->
  <Modal :show="activeCard === 'index'" title="检索索引管理" @close="activeCard = null">
    <div class="info-rows">
      <div class="info-row"><span>BM25 索引</span><b :class="health && health.bm25_ready ? 'ok' : 'warn'">{{ health && health.bm25_ready ? "就绪" : "缺失" }}</b></div>
      <div class="info-row"><span>向量索引</span><b :class="health && health.vec_ready ? 'ok' : 'warn'">{{ health && health.vec_ready ? "就绪" : "缺失" }}</b></div>
      <div class="info-row"><span>向量分块数</span><b>{{ (vecStats && vecStats.chunk_count) ?? "—" }}</b></div>
      <div class="info-row"><span>文档数</span><b>{{ (vecStats && vecStats.doc_count) ?? "—" }}</b></div>
    </div>
    <p class="muted">重建索引将重新解析全部文档并生成 BM25 与向量索引，可能需要一些时间。</p>
    <template #actions>
      <button class="btn" @click="activeCard = null">关闭</button>
      <button class="btn primary" :disabled="reindexing" @click="reindex">
        {{ reindexing ? "重建中…" : "重建索引" }}
      </button>
    </template>
  </Modal>

  <!-- 系统初始化 -->
  <Modal :show="activeCard === 'init'" title="系统初始化" @close="activeCard = null">
    <p class="muted">一键执行知识库初始化操作。请按需单独执行，避免误清空已上传文档。</p>
    <div class="init-actions">
      <div class="init-item">
        <div class="init-meta">
          <div class="init-name">清除文档</div>
          <div class="muted" style="font-size: 12px">删除全部已上传文档（含回收站）并重建空索引，不可恢复。</div>
        </div>
        <button class="btn danger sm" :disabled="busyClear" @click="initClear">
          {{ busyClear ? "清除中…" : "执行清除" }}
        </button>
      </div>
      <div class="init-item">
        <div class="init-meta">
          <div class="init-name">提取内容</div>
          <div class="muted" style="font-size: 12px">对所有已上传文档重新运行文本提取与结构化，并重建索引（提取规则升级后适用）。</div>
        </div>
        <button class="btn warning sm" :disabled="busyExtract" @click="initExtract">
          {{ busyExtract ? "提取中…" : "重新提取" }}
        </button>
      </div>
      <div class="init-item">
        <div class="init-meta">
          <div class="init-name">提取内容清除</div>
          <div class="muted" style="font-size: 12px">仅清空全部文档的提取文本（保留文件），状态置「未识别」、字数归 0；不触发重提，需手动重新提取才能恢复。</div>
        </div>
        <button class="btn danger sm" :disabled="busyClearExtract" @click="initClearExtract">
          {{ busyClearExtract ? "清除中…" : "执行清除" }}
        </button>
      </div>
      <div class="init-item">
        <div>
          <div class="init-name">中止提取</div>
          <div class="muted" style="font-size: 12px">后台提取进行中时，丢弃队列剩余任务；已提取文档保留并重建索引，可正常检索。</div>
        </div>
        <button class="btn sm" :disabled="busyAbort" @click="initAbort">
          {{ busyAbort ? "中止中…" : "中止提取" }}
        </button>
      </div>
      <div class="init-item">
        <div class="init-meta">
          <div class="init-name">重建索引</div>
          <div class="muted" style="font-size: 12px">仅重新构建 BM25 与向量索引，不改动文档与提取文本。</div>
        </div>
        <button class="btn primary sm" :disabled="busyIndex" @click="initIndex">
          {{ busyIndex ? "重建中…" : "重建索引" }}
        </button>
      </div>
    </div>
    <template #actions>
      <button class="btn" @click="activeCard = null">关闭</button>
    </template>
  </Modal>

  <!-- 数据统计 -->
  <Modal :show="activeCard === 'stats'" title="数据统计" @close="activeCard = null">
    <div class="stat-grid">
      <div class="stat"><div class="n">{{ stats && stats.total_documents }}</div><div class="l">文档总数</div></div>
      <div class="stat"><div class="n">{{ stats && stats.category_count }}</div><div class="l">分类数</div></div>
      <div class="stat"><div class="n">{{ stats && stats.user_count }}</div><div class="l">用户数</div></div>
      <div class="stat"><div class="n">{{ stats && stats.active_users }}</div><div class="l">启用用户</div></div>
      <div class="stat">
        <div class="n">{{ stats && stats.index_ready ? "✓" : "✗" }}</div>
        <div class="l">检索索引</div>
      </div>
    </div>
    <template #actions>
      <button class="btn" @click="activeCard = null">关闭</button>
    </template>
  </Modal>

  <!-- 系统信息 -->
  <Modal :show="activeCard === 'info'" title="系统信息" @close="activeCard = null">
    <div class="info-rows">
      <div class="info-row"><span>服务状态</span><b :class="health && health.status === 'ok' ? 'ok' : 'warn'">{{ health && health.status }}</b></div>
      <div class="info-row"><span>知识库根目录</span><b class="mono">{{ health && health.kb_root }}</b></div>
      <div class="info-row"><span>索引目录</span><b class="mono">{{ health && health.index_dir }}</b></div>
    </div>
    <template #actions>
      <button class="btn" @click="activeCard = null">关闭</button>
    </template>
  </Modal>

  <!-- 系统名称设定 -->
  <Modal :show="activeCard === 'sysname'" title="系统名称设定" @close="activeCard = null">
    <div class="info-rows">
      <div class="info-row">
        <span>系统名称</span>
        <b v-if="!isAdmin" class="mono">{{ sysInfo.system_name || "—" }}</b>
      </div>
    </div>
    <div v-if="isAdmin" class="form-row">
      <label>系统名称</label>
      <input
        v-model="nameDraft"
        class="inp"
        maxlength="60"
        placeholder="请输入系统名称"
        @focus="nameDraft === '' ? (nameDraft = sysInfo.system_name) : null"
      />
    </div>
    <p v-if="!isAdmin" class="muted">仅管理员可修改系统名称。</p>
    <div class="info-rows" style="margin-top: 12px">
      <div class="info-row"><span>版本号</span><b class="mono">v{{ sysInfo.version || "—" }}</b></div>
      <div class="info-row"><span>Git 提交次数</span><b class="mono">{{ sysInfo.commits ?? "—" }}</b></div>
      <div class="info-row"><span>版本规则</span><b class="muted" style="font-weight:normal">基准 1.0.0，每次 Git 提交累加，每位满 10 进位</b></div>
    </div>
    <template #actions>
      <button class="btn" @click="activeCard = null">关闭</button>
      <button v-if="isAdmin" class="btn primary" :disabled="nameBusy" @click="saveSystemName">
        {{ nameBusy ? "保存中…" : "保存" }}
      </button>
    </template>
  </Modal>
  </div>
</template>
