<script setup>
import { ref, computed } from "vue";

// 递归分类树节点。缩进克制(每层约 22px)、名称完整显示、支持折叠。
const props = defineProps({
  node: { type: Object, required: true },
  depth: { type: Number, default: 0 },
  selectedName: { type: String, default: "" },
});
const emit = defineEmits(["select"]);

const hasKids = computed(() => (props.node.children || []).length > 0);
const open = ref(true);

function toggle() {
  if (hasKids.value) open.value = !open.value;
}
function onRow() {
  emit("select", props.node.name);
}
</script>

<template>
  <div class="cat-node">
    <div
      class="cat-row"
      :class="{ active: selectedName === node.name }"
      @click="onRow"
    >
      <span
        class="twisty"
        :class="{ empty: !hasKids }"
        @click.stop="toggle"
        >{{ hasKids ? (open ? "▾" : "▸") : "" }}</span
      >
      <span class="cat-label" :title="node.name">{{ node.name }}</span>
      <span class="cnt">{{ node.doc_count }}</span>
    </div>

    <div class="cat-children" v-if="hasKids && open">
      <CatTreeNode
        v-for="c in node.children"
        :key="c.id"
        :node="c"
        :depth="depth + 1"
        :selected-name="selectedName"
        @select="(n) => emit('select', n)"
      />
    </div>
  </div>
</template>
