<script setup>
import { ref, onMounted } from "vue";
import { api } from "../api.js";

const emit = defineEmits(["ok"]);
// 登录 / 注册 双模式：注册需工号+姓名命中用户池，提交后等管理员审批
const mode = ref("login");

const username = ref("");
const password = ref("");
const err = ref("");
const busy = ref(false);

// 注册表单
const reg = ref({ emp_no: "", name: "", password: "", confirm: "" });
const regErr = ref("");
const regOk = ref("");
const regBusy = ref(false);
// 注册开关（系统设置 → 自助注册）；未开启时不展示注册入口
const registerEnabled = ref(false);
const minPwd = ref(6);

onMounted(async () => {
  try {
    const r = await api.registerInfo();
    registerEnabled.value = !!r.enabled;
    minPwd.value = r.min_password_length || 6;
  } catch (e) {
    registerEnabled.value = false;
  }
});

function switchTo(m) {
  mode.value = m;
  err.value = "";
  regErr.value = "";
  regOk.value = "";
}

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

async function submitRegister() {
  regErr.value = "";
  regOk.value = "";
  const f = reg.value;
  if (!f.emp_no.trim() || !f.name.trim()) {
    regErr.value = "请输入工号和姓名";
    return;
  }
  if (!f.password) {
    regErr.value = "请设置登录密码";
    return;
  }
  if (f.password.length < minPwd.value) {
    regErr.value = `密码至少 ${minPwd.value} 位`;
    return;
  }
  if (f.password !== f.confirm) {
    regErr.value = "两次输入的密码不一致";
    return;
  }
  regBusy.value = true;
  try {
    await api.register(f.emp_no.trim(), f.name.trim(), f.password);
    regOk.value = "注册申请已提交，请等待管理员审批通过后登录。";
    reg.value = { emp_no: f.emp_no, name: "", password: "", confirm: "" };
  } catch (e) {
    regErr.value = e.message;
  } finally {
    regBusy.value = false;
  }
}
</script>

<template>
  <div class="login-wrap">
    <div class="login-card">
      <h1>OA 知识库门户</h1>
      <div class="sub">请登录以继续</div>

      <div v-if="registerEnabled" class="login-tabs">
        <span :class="{ on: mode === 'login' }" @click="switchTo('login')">登录</span>
        <span :class="{ on: mode === 'register' }" @click="switchTo('register')">注册</span>
      </div>

      <!-- 登录 -->
      <div v-if="mode === 'login'">
        <div class="login-err">{{ err }}</div>
        <div class="field">
          <label>用户名 / 工号</label>
          <input
            class="input"
            v-model="username"
            placeholder="用户名或工号"
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
        <div v-if="registerEnabled" class="login-tip">
          还没有账号？<a @click="switchTo('register')">点此注册</a>（需管理员审批）
        </div>
      </div>

      <!-- 注册 -->
      <div v-else>
        <div class="login-err">{{ regErr }}</div>
        <div v-if="regOk" class="login-ok">{{ regOk }}</div>
        <div class="field">
          <label>工号</label>
          <input class="input" v-model="reg.emp_no" placeholder="如 E1001" />
        </div>
        <div class="field">
          <label>姓名</label>
          <input class="input" v-model="reg.name" placeholder="须与用户池登记姓名一致" />
        </div>
        <div class="field">
          <label>登录密码</label>
          <input
            class="input"
            type="password"
            v-model="reg.password"
            :placeholder="`至少 ${minPwd} 位`"
          />
        </div>
        <div class="field">
          <label>确认密码</label>
          <input
            class="input"
            type="password"
            v-model="reg.confirm"
            placeholder="再次输入密码"
            @keyup.enter="submitRegister"
          />
        </div>
        <button class="btn primary" style="width: 100%; justify-content: center"
          :disabled="regBusy" @click="submitRegister">
          {{ regBusy ? "提交中…" : "提交注册申请" }}
        </button>
        <div class="login-tip">
          工号与姓名须与用户池一致；提交后由管理员审批，通过后即可登录。
          <a @click="switchTo('login')">返回登录</a>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.login-tabs {
  display: flex;
  border-bottom: 1px solid var(--border, #e5e7eb);
  margin-bottom: 18px;
}
.login-tabs span {
  flex: 1;
  text-align: center;
  padding: 8px 0;
  cursor: pointer;
  color: var(--muted, #6b7280);
  font-size: 14px;
  border-bottom: 2px solid transparent;
}
.login-tabs span.on {
  color: #2563eb;
  font-weight: 600;
  border-bottom-color: #2563eb;
}
.login-tip {
  margin-top: 14px;
  font-size: 12px;
  color: var(--muted, #6b7280);
  line-height: 1.7;
}
.login-tip a {
  color: #2563eb;
  cursor: pointer;
}
.login-ok {
  color: #16a34a;
  font-size: 13px;
  margin-bottom: 12px;
  line-height: 1.6;
}
</style>
