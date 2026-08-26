<script setup>
defineProps({
  show: Boolean,
  url: String,
  title: String,
});
defineEmits(["close"]);
</script>

<template>
  <div class="modal-mask" v-if="show" @click.self="$emit('close')">
    <div class="modal pdf-modal">
      <div class="pdf-head">
        <span class="pdf-title">{{ title || "PDF 预览" }}</span>
        <div class="pdf-actions">
          <a class="btn sm" :href="url" target="_blank" download>下载</a>
          <button class="btn sm" @click="$emit('close')">关闭</button>
        </div>
      </div>
      <iframe v-if="url" class="pdf-frame" :src="url"></iframe>
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
.pdf-frame {
  flex: 1;
  width: 100%;
  border: none;
  background: #525659;
}
</style>
