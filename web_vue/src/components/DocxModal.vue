<script setup>
import { ref, watch, onBeforeUnmount } from "vue";
import { renderAsync } from "docx-preview";

const props = defineProps({
  show: Boolean,
  url: String,
  title: String,
});
defineEmits(["close"]);

const renderHost = ref(null);
const loading = ref(false);
const errorMsg = ref("");
let currentUrl = "";

// 用后端 inline 端点拉取 docx 二进制，浏览器内渲染原版版面（无需转 PDF、无需公网）
async function loadDocx() {
  if (!props.url || !renderHost.value) return;
  // 防止同一 URL 重复拉取
  if (currentUrl === props.url && !errorMsg.value) return;
  currentUrl = props.url;
  loading.value = true;
  errorMsg.value = "";
  renderHost.value.innerHTML = "";
  try {
    const resp = await fetch(props.url, { cache: "no-store" });
    if (!resp.ok) throw new Error("HTTP " + resp.status);
    const buf = await resp.arrayBuffer();
    await renderAsync(new Blob([buf]), renderHost.value, null, {
      className: "docx-page",
      inWrapper: true,
      ignoreWidth: false,
      ignoreHeight: false,
      breakPages: true,
      experimental: true,
    });
  } catch (e) {
    errorMsg.value = "docx 预览失败：" + (e && e.message ? e.message : e);
  } finally {
    loading.value = false;
  }
}

watch(
  () => props.show,
  (v) => {
    if (v) {
      // 等待 DOM 渲染后再加载
      requestAnimationFrame(() => loadDocx());
    } else {
      currentUrl = "";
      if (renderHost.value) renderHost.value.innerHTML = "";
    }
  }
);

function downloadDocx() {
  const a = document.createElement("a");
  a.href = props.url.replace(/([?&])inline=1&?/, "$1").replace(/[?&]$/, "");
  a.target = "_blank";
  a.download = (props.title || "document") + ".docx";
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
}

onBeforeUnmount(() => {
  if (renderHost.value) renderHost.value.innerHTML = "";
});
</script>

<template>
  <div class="modal-mask" v-if="show" @click.self="$emit('close')">
    <div class="modal docx-modal">
      <div class="docx-head">
        <span class="docx-title">{{ title || "DOCX 预览" }}</span>
        <div class="docx-actions">
          <button class="btn sm" @click="downloadDocx">下载</button>
          <button class="btn sm" @click="$emit('close')">关闭</button>
        </div>
      </div>
      <div v-if="loading" class="loading">正在渲染文档…</div>
      <div v-else-if="errorMsg" class="preview-error">{{ errorMsg }}</div>
      <div v-show="!loading && !errorMsg" ref="renderHost" class="docx-body"></div>
    </div>
  </div>
</template>

<style scoped>
.docx-modal {
  width: 90vw;
  height: 88vh;
  display: flex;
  flex-direction: column;
  padding: 0;
  overflow: hidden;
}
.docx-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 10px 14px;
  border-bottom: 1px solid var(--line);
  background: #f8fafc;
}
.docx-title { font-weight: 600; font-size: 14px; }
.docx-actions { display: flex; gap: 8px; }
.docx-body {
  flex: 1;
  overflow: auto;
  background: #525659;
  padding: 16px;
}
.docx-body :deep(.docx-page) {
  background: #fff;
  margin: 0 auto 16px;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.3);
}
.loading, .preview-error {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  color: #cbd5e1;
  font-size: 14px;
  background: #525659;
}
.preview-error { color: #fca5a5; padding: 20px; text-align: center; }
</style>
