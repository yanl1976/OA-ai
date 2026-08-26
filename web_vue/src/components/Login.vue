<script setup>
import { ref } from "vue";
import { api } from "../api.js";

const emit = defineEmits(["ok"]);
const username = ref("");
const password = ref("");
const err = ref("");
const busy = ref(false);

async function submit() {
  err.value = "";
  if (!username.value || !password.value) {
    err.value = "请输入用户名和密码";
    return;
  }
  busy.value = true;
  try {
    const r = await api.login(username.value, password.value);
    emit("ok", r.user);
  } catch (e) {
    err.value = e.message;
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <div class="login-wrap">
    <div class="login-card">
      <h1>OA 知识库门户</h1>
      <div class="sub">请登录以继续</div>
      <div class="login-err">{{ err }}</div>
      <div class="field">
        <label>用户名</label>
        <input
          class="input"
          v-model="username"
          placeholder="用户名"
          @keyup.enter="submit"
        />
      </div>
      <div class="field">
        <label>密码</label>
        <input
          class="input"
          type="password"
          v-model="password"
          placeholder="密码"
          @keyup.enter="submit"
        />
      </div>
      <button class="btn primary" style="width: 100%; justify-content: center"
        :disabled="busy" @click="submit">
        {{ busy ? "登录中…" : "登录" }}
      </button>
    </div>
  </div>
</template>
