<script setup>
// 3D 知识图谱由后端 /graph 提供，这里用 iframe 嵌入。
</script>

<template>
  <h2>3D 知识图谱</h2>
  <iframe class="graph" src="/graph" title="知识图谱"></iframe>
</template>

<style scoped>
.graph {
  width: 100%;
  height: calc(100vh - 160px);
  border: 0;
  border-radius: 12px;
  background: #fff;
}
</style>
