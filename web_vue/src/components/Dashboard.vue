<script setup>
import { ref, onMounted } from "vue";
import { api } from "../api.js";
import { inject } from "vue";

const notify = inject("notify");
const openDocInBrowse = inject("openDocInBrowse");
const overview = ref({ doc_count: 0, category_count: 0, tag_count: 0,
  trash_count: 0, total_count: 0, recent: [] });
const tags = ref([]);
const loading = ref(false);

async function load() {
  loading.value = true;
  try {
    const [ov, tg] = await Promise.all([api.kbOverview(), api.listTags()]);
    overview.value = ov;
    tags.value = tg.tags || [];
  } catch (e) {
    notify("加载概览失败：" + (e.response?.data?.error || e.message), "err");
  } finally {
    loading.value = false;
  }
}

function go(key) {
  // 通过自定义事件让父组件切换视图
  window.dispatchEvent(new CustomEvent("nav", { detail: key }));
}

function openDoc(docId) {
  // 跳转到「知识浏览」页并在右侧详情区打开该具体文档（与列表联动），
  // 而非独立的 DocDetail 组件。
  if (openDocInBrowse) openDocInBrowse(docId);
}

onMounted(load);
</script>

<template>
  <div class="dash">
    <h2 class="dash-title">知识库概览</h2>

    <div class="cards">
      <div class="card" @click="go('KbBrowse')" title="浏览全部文档">
        <div class="card-num">{{ overview.doc_count }}</div>
        <div class="card-label">活跃文档</div>
      </div>
      <div class="card" @click="go('CategoryManage')" title="分类管理">
        <div class="card-num">{{ overview.category_count }}</div>
        <div class="card-label">分类数</div>
      </div>
      <div class="card" @click="go('TagBrowse')" title="标签浏览">
        <div class="card-num">{{ overview.tag_count }}</div>
        <div class="card-label">标签数</div>
      </div>
      <div class="card warn" v-if="overview.trash_count" @click="go('TrashManage')" title="回收站">
        <div class="card-num">{{ overview.trash_count }}</div>
        <div class="card-label">回收站待恢复</div>
      </div>
      <div class="card" v-else @click="go('TrashManage')" title="回收站">
        <div class="card-num">0</div>
        <div class="card-label">回收站</div>
      </div>
    </div>

    <div class="dash-grid">
      <div class="panel">
        <div class="panel-head">
          <span>最近更新</span>
          <a @click="go('KbBrowse')">全部 →</a>
        </div>
        <div v-if="!overview.recent.length" class="empty">暂无文档</div>
        <ul v-else class="recent-list">
          <li v-for="d in overview.recent" :key="d.doc_id" @click="openDoc(d.doc_id)">
            <span class="r-name">{{ d.filename }}</span>
            <span class="r-cat">{{ d.category }}</span>
            <span class="r-time">{{ d.updated_at }}</span>
          </li>
        </ul>
      </div>

      <div class="panel">
        <div class="panel-head">
          <span>标签云</span>
          <a @click="go('TagBrowse')">浏览 →</a>
        </div>
        <div v-if="!tags.length" class="empty">暂无标签</div>
        <div v-else class="tag-cloud">
          <span
            v-for="t in tags"
            :key="t.tag"
            class="tag-chip"
            :style="{ fontSize: (12 + Math.min(t.count, 20)) + 'px' }"
            @click="go('TagBrowse')"
          >{{ t.tag }} <em>{{ t.count }}</em></span>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.dash { padding: 8px 4px; }
.dash-title { margin: 4px 0 16px; font-size: 20px; }
.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 14px; }
.card {
  background: #fff; border: 1px solid #e6e8eb; border-radius: 10px;
  padding: 18px; cursor: pointer; transition: .15s;
  box-shadow: 0 1px 3px rgba(0,0,0,.04);
}
.card:hover { transform: translateY(-2px); box-shadow: 0 4px 14px rgba(0,0,0,.1); }
.card-num { font-size: 30px; font-weight: 700; color: #2b6cb0; }
.card-label { color: #666; margin-top: 4px; }
.card.warn .card-num { color: #c05621; }
.dash-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-top: 18px; }
@media (max-width: 880px) { .dash-grid { grid-template-columns: 1fr; } }
.panel { background: #fff; border: 1px solid #e6e8eb; border-radius: 10px; padding: 14px 16px; }
.panel-head { display: flex; justify-content: space-between; align-items: center;
  font-weight: 600; margin-bottom: 10px; border-bottom: 1px solid #f0f0f0; padding-bottom: 8px; }
.panel-head a { color: #2b6cb0; cursor: pointer; font-weight: 400; font-size: 13px; }
.recent-list { list-style: none; margin: 0; padding: 0; }
.recent-list li { display: flex; gap: 10px; align-items: center; padding: 7px 4px;
  border-bottom: 1px dashed #f3f3f3; cursor: pointer; }
.recent-list li:hover { background: #f7faff; }
.r-name { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.r-cat { color: #888; font-size: 12px; }
.r-time { color: #aaa; font-size: 12px; min-width: 110px; text-align: right; }
.empty { color: #aaa; padding: 20px 0; text-align: center; }
.tag-cloud { line-height: 2.2; }
.tag-chip { display: inline-block; margin: 0 8px 0 0; color: #2b6cb0; cursor: pointer; }
.tag-chip em { color: #999; font-style: normal; font-size: 11px; }
</style>
