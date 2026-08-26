<script setup>
import { ref, onMounted, inject, computed } from "vue";
import { api } from "../api.js";
import Modal from "./Modal.vue";

const notify = inject("notify");
const loading = ref(true);

const features = ref([]);
const stats = ref(null);
const health = ref(null);
const vecStats = ref(null);

const activeCard = ref(null); // 当前打开的弹窗 key
const reindexing = ref(false);

async function load() {
  loading.value = true;
  try {
    const [f, s, h, v] = await Promise.all([
      api.features(),
      api.stats(),
      api.health(),
      api.vectorStats(),
    ]);
    features.value = f.features || [];
    stats.value = s;
    health.value = h;
    vecStats.value = v;
  } catch (e) {
    notify(e.message, "err");
  } finally {
    loading.value = false;
  }
}

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

const cards = [
  { key: "features", icon: "⚙", title: "功能开关", desc: "开启或关闭系统功能模块" },
  { key: "index", icon: "🔎", title: "检索索引", desc: "全文检索向量索引状态与重建" },
  { key: "stats", icon: "📊", title: "数据统计", desc: "文档、用户与分类统计概览" },
  { key: "info", icon: "💡", title: "系统信息", desc: "运行环境与版本信息" },
];

function statusText(key) {
  if (key === "features") return `${enabledCount.value}/${features.value.length} 已开启`;
  if (key === "index") return (health.value && health.value.vec_ready) ? "索引就绪" : "索引缺失";
  if (key === "stats") return `${(stats.value && stats.value.total_documents) || 0} 份文档`;
  if (key === "info") {
    const ok = health.value && health.value.status === "ok";
    return ok ? "运行正常" : "需关注";
  }
  return "";
}

onMounted(load);
</script>

<template>
  <h2>系统管理</h2>
  <p class="muted">集中管理系统功能开关、检索索引、数据统计与运行信息，点击卡片进入设置。</p>

  <div v-if="loading" class="loading">加载中…</div>
  <div v-else class="sys-card-grid">
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
</template>
