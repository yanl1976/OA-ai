<script setup>
defineProps({ title: String, show: Boolean, wide: Boolean });
const emit = defineEmits(["close"]);
</script>

<template>
  <div class="modal-mask" v-if="show" @click.self="emit('close')">
    <div class="modal" :class="{ 'modal-wide': wide }">
      <h3>{{ title }}</h3>
      <slot />
      <div class="actions" v-if="$slots.actions">
        <slot name="actions" />
      </div>
    </div>
  </div>
</template>
