<script setup>
import { ref, computed, watch } from "vue";

const props = defineProps({
  show: Boolean,
  url: String,
  title: String,
});
defineEmits(["close"]);

const frameRef = ref(null);

// 预览时禁用浏览器内置 PDF 查看器的工具栏：
// 直接用 <iframe src=...pdf> 时，Chrome/Edge 会显示自带的 PDF 工具栏，
// 其中含「转换」「拆分」「转图片」等与业务无关的功能，且为浏览器语言（常为英文）。
// 这里通过 URL 片段参数隐藏内置工具栏，改由自定义头部提供仅「打印 / 下载」两个中文按钮。
const viewerUrl = computed(() => {
  if (!props.url) return "";
  const sep = props.url.includes("#") ? "&" : "#";
  return props.url + sep + "toolbar=0&navpanes=0&scrollbar=0&statusbar=0&view=FitH";
});

// 下载用地址：去掉 inline=1，让后端以 attachment 方式返回，保证是下载而非预览
const downloadUrl = computed(() => {
  if (!props.url) return "";
  return props.url.replace(/([?&])inline=1&?/, "$1").replace(/[?&]$/, "");
});

function printPdf() {
  const f = frameRef.value;
  try {
    // 同源 iframe 可直接调 print()；失败则新开窗口打印（跨域/被浏览器限制时）
    if (f && f.contentWindow && typeof f.contentWindow.print === "function") {
      f.contentWindow.print();
      return;
    }
  } catch (e) {
    /* 跨域或被限制，走下面的兜底 */
  }
  const w = window.open(props.url, "_blank");
  if (w) {
    w.focus();
    // 新窗口加载完再打印，避免打印空白页
    setTimeout(() => { try { w.print(); } catch (e) {} }, 800);
  }
}

// 每次打开时重置滚动位置，避免沿用上一次的浏览进度
watch(() => props.show, (v) => {
  if (v && frameRef.value) frameRef.value.src = viewerUrl.value;
});
</script>

<template>
  <div class="modal-mask" v-if="show" @click.self="$emit('close')">
    <div class="modal pdf-modal">
      <div class="pdf-head">
        <span class="pdf-title">{{ title || "PDF 预览" }}</span>
        <div class="pdf-actions">
          <button class="btn sm primary" @click="printPdf">打印</button>
          <a class="btn sm" :href="downloadUrl" target="_blank" download>下载</a>
          <button class="btn sm" @click="$emit('close')">关闭</button>
        </div>
      </div>
      <iframe
        v-if="url"
        ref="frameRef"
        class="pdf-frame"
        :src="viewerUrl"
      ></iframe>
      <div v-else class="loading">无预览内容</div>
    </div>
  </div>
</template>

<style scoped>
.pdf-modal {
  width: 90vw;
  height: 88vh;
  display: flex;
  flex-direction: column;
  padding: 0;
  overflow: hidden;
}
.pdf-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 10px 14px;
  border-bottom: 1px solid var(--line);
  background: #f8fafc;
}
.pdf-title { font-weight: 600; font-size: 14px; }
.pdf-actions { display: flex; gap: 8px; }
.pdf-actions .btn { text-decoration: none; display: inline-flex; align-items: center; }
.pdf-frame {
  flex: 1;
  width: 100%;
  border: none;
  background: #525659;
}
</style>
