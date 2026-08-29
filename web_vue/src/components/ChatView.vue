<script setup>
import { ref, reactive, onMounted, nextTick, inject } from "vue";
import { api } from "../api.js";

const notify = inject("notify");

const scope = ref([]);            // 可对话范围（域名称）
const accessCats = ref([]);       // 可对话域（含 node/label，供下拉）
const selScope = ref("");         // 当前选中的检索域（空=全部授权域）
const sessions = ref([]);
const activeId = ref(null);
const messages = ref([]);          // {role, content, refs}
const input = ref("");
const sending = ref(false);
const loadingSessions = ref(false);
const scrollRef = ref(null);

async function loadScope() {
  try {
    const r = await api.chatScope();
    scope.value = r.domains || [];
  } catch (e) {
    scope.value = [];
  }
  try {
    const c = await api.accessibleCategories();
    accessCats.value = c.categories || [];
  } catch (e) {
    accessCats.value = [];
  }
}

async function loadSessions() {
  loadingSessions.value = true;
  try {
    const r = await api.chatSessions();
    sessions.value = r.sessions || [];
  } catch (e) {
    notify("加载会话失败：" + (e.response?.data?.error || e.message), "err");
  } finally {
    loadingSessions.value = false;
  }
}

async function openSession(sid) {
  activeId.value = sid;
  messages.value = [];
  try {
    const r = await api.chatSessionMessages(sid);
    messages.value = (r.messages || []).map((m) => ({
      role: m.role,
      content: m.content,
      refs: m.refs || [],
    }));
  } catch (e) {
    notify("加载消息失败：" + (e.response?.data?.error || e.message), "err");
  }
  scrollBottom();
}

async function newSession() {
  try {
    const r = await api.chatCreateSession("新对话");
    await loadSessions();
    openSession(r.session_id);
  } catch (e) {
    notify("新建会话失败：" + (e.response?.data?.error || e.message), "err");
  }
}

async function removeSession(sid) {
  if (!confirm("确定删除该会话？删除后不可恢复。")) return;
  try {
    await api.chatDeleteSession(sid);
    if (activeId.value === sid) {
      activeId.value = null;
      messages.value = [];
    }
    await loadSessions();
    notify("已删除会话", "ok");
  } catch (e) {
    notify("删除失败：" + (e.response?.data?.error || e.message), "err");
  }
}

async function renameSession(s) {
  const t = prompt("重命名会话：", s.title);
  if (t === null) return;
  const title = t.trim();
  if (!title) return;
  try {
    await api.chatRenameSession(s.id, title);
    await loadSessions();
  } catch (e) {
    notify("重命名失败：" + (e.response?.data?.error || e.message), "err");
  }
}

async function send() {
  const q = input.value.trim();
  if (!q || sending.value) return;
  sending.value = true;
  // 本地先展示用户消息 + 占位助手
  messages.value.push({ role: "user", content: q, refs: [] });
  messages.value.push({ role: "assistant", content: "正在分析…", refs: [], loading: true });
  input.value = "";
  scrollBottom();
  try {
    const r = await api.chatSend(activeId.value, q, 5, selScope.value || undefined);
    if (!activeId.value) {
      activeId.value = r.session_id;
      await loadSessions();
    }
    // 替换占位助手消息
    const last = messages.value[messages.value.length - 1];
    last.content = r.answer;
    last.refs = r.refs || [];
    last.loading = false;
    if (scope.value.length === 0) scope.value = r.scope || [];
  } catch (e) {
    const last = messages.value[messages.value.length - 1];
    last.content = "对话失败：" + (e.response?.data?.error || e.message);
    last.loading = false;
    last.error = true;
  } finally {
    sending.value = false;
    scrollBottom();
  }
}

function onKey(e) {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    send();
  }
}

function scrollBottom() {
  nextTick(() => {
    if (scrollRef.value) scrollRef.value.scrollTop = scrollRef.value.scrollHeight;
  });
}

// 参考来源弹窗
const activeRef = ref(null);          // 当前弹窗展示的来源对象
function openRef(rf) {
  activeRef.value = rf;
}
function closeRef() {
  activeRef.value = null;
}

onMounted(async () => {
  await loadScope();
  await loadSessions();
  if (sessions.value.length) openSession(sessions.value[0].id);
});
</script>

<template>
  <div class="chat-layout">
    <!-- 左侧会话列表 -->
    <aside class="chat-side">
      <div class="side-head">
        <span>对话历史</span>
        <button class="btn-new" @click="newSession">＋ 新对话</button>
      </div>
      <div class="side-list">
        <div
          v-for="s in sessions"
          :key="s.id"
          class="side-item"
          :class="{ active: s.id === activeId }"
          @click="openSession(s.id)"
        >
          <div class="side-title">{{ s.title }}</div>
          <div class="side-meta">
            <span>{{ s.updated_at }}</span>
            <span class="side-actions">
              <a @click.stop="renameSession(s)">改名</a>
              <a class="danger" @click.stop="removeSession(s.id)">删除</a>
            </span>
          </div>
        </div>
        <div v-if="!sessions.length" class="side-empty">暂无会话，点击「新对话」开始</div>
      </div>
    </aside>

    <!-- 右侧对话区 -->
    <section class="chat-main">
      <div class="chat-scope" v-if="scope.length">
        对话范围：
        <select v-model="selScope" class="scope-select" title="选择对话范围">
          <option value="">全部（我有权限的）</option>
          <option v-for="c in accessCats" :key="c.node" :value="c.node">{{ c.label }}</option>
        </select>
        <span class="scope-note">· 仅基于所选范围的文档作答</span>
      </div>
      <div class="chat-scope denied" v-else>
        当前账号无对话权限，请联系管理员开通「对话」权限。
      </div>

      <div class="chat-scroll" ref="scrollRef">
        <div v-if="!messages.length" class="chat-empty">
          <p>👋 您好，我是企业知识库智能助手。</p>
          <p>请用自然语言提问，我将基于授权范围内的文档进行检索与汇报式分析，并标注引用来源。</p>
        </div>

        <div
          v-for="(m, i) in messages"
          :key="i"
          class="msg"
          :class="m.role"
        >
          <div class="msg-role">{{ m.role === "user" ? "我" : "助手" }}</div>
          <div class="msg-body">
            <div class="msg-text" :class="{ error: m.error }" v-if="!m.loading">{{ m.content }}</div>
            <div class="msg-text loading" v-else>{{ m.content }}</div>

            <div class="refs" v-if="m.role === 'assistant' && m.refs && m.refs.length">
              <div class="refs-title">参考来源</div>
              <div
                class="ref-card"
                v-for="(rf, j) in m.refs"
                :key="j"
                @click="openRef(rf)"
                title="点击查看来源详情"
              >
                <div class="ref-name">📄 {{ rf.filename }}</div>
                <div class="ref-cat" v-if="rf.category">{{ rf.category }}</div>
                <div class="ref-snippet" v-if="rf.snippet">{{ rf.snippet }}</div>
              </div>
            </div>
          </div>
        </div>
      </div>

      <div class="chat-input">
        <textarea
          v-model="input"
          placeholder="输入您的问题，Enter 发送 / Shift+Enter 换行"
          @keydown="onKey"
          :disabled="!scope.length"
        ></textarea>
        <button class="btn-send" @click="send" :disabled="sending || !scope.length">
          {{ sending ? "分析中…" : "发送" }}
        </button>
      </div>
    </section>

    <!-- 参考来源弹窗 -->
    <div class="ref-modal-mask" v-if="activeRef" @click.self="closeRef">
      <div class="ref-modal">
        <div class="ref-modal-head">
          <span class="ref-modal-name">📄 {{ activeRef.filename }}</span>
          <button class="ref-modal-close" @click="closeRef" title="关闭">✕</button>
        </div>
        <div class="ref-modal-body">
          <div class="ref-modal-row" v-if="activeRef.category">
            <span class="ref-modal-label">分类</span>{{ activeRef.category }}
          </div>
          <div class="ref-modal-row" v-if="activeRef.score != null">
            <span class="ref-modal-label">相关度</span>{{ activeRef.score.toFixed(2) }}
          </div>
          <div class="ref-modal-section">原文相关片段</div>
          <div class="ref-modal-snippet">{{ activeRef.content || activeRef.snippet || "（无片段预览）" }}</div>
          <div class="ref-modal-tip">提示：以上为回答所依据的文档片段，关闭弹窗可返回对话继续提问。</div>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.chat-layout {
  display: flex;
  height: calc(100vh - 120px);
  background: #fff;
  border: 1px solid #e4e7ed;
  border-radius: 8px;
  overflow: hidden;
}
.chat-side {
  width: 260px;
  border-right: 1px solid #e4e7ed;
  display: flex;
  flex-direction: column;
  background: #fafbfc;
}
.side-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 12px;
  font-weight: 600;
  border-bottom: 1px solid #eee;
}
.btn-new {
  border: none;
  background: #409eff;
  color: #fff;
  border-radius: 4px;
  padding: 4px 8px;
  cursor: pointer;
  font-size: 12px;
}
.side-list {
  flex: 1;
  overflow-y: auto;
  padding: 8px;
}
.side-item {
  padding: 8px 10px;
  border-radius: 6px;
  margin-bottom: 6px;
  cursor: pointer;
  border: 1px solid transparent;
}
.side-item:hover { background: #eef4ff; }
.side-item.active { background: #e6f0ff; border-color: #b3d4ff; }
.side-title {
  font-size: 13px;
  color: #303133;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.side-meta {
  display: flex;
  justify-content: space-between;
  font-size: 11px;
  color: #909399;
  margin-top: 4px;
}
.side-actions a { margin-left: 8px; color: #409eff; cursor: pointer; }
.side-actions a.danger { color: #f56c6c; }
.side-empty { color: #b0b3b8; text-align: center; font-size: 12px; margin-top: 20px; }

.chat-main { flex: 1; display: flex; flex-direction: column; min-width: 0; }
.chat-scope {
  padding: 8px 16px;
  background: #f0f7ff;
  font-size: 13px;
  color: #205081;
  border-bottom: 1px solid #e4e7ed;
}
.chat-scope.denied { background: #fef0f0; color: #c0392b; }
.scope-note { color: #8a9bb0; margin-left: 4px; }

.chat-scroll { flex: 1; overflow-y: auto; padding: 18px 22px; }
.chat-empty { color: #8a9099; text-align: center; margin-top: 60px; }
.chat-empty p { margin: 6px 0; }

.msg { display: flex; margin-bottom: 18px; }
.msg.user { flex-direction: row-reverse; }
.msg-role {
  width: 40px; height: 40px; line-height: 40px; text-align: center;
  border-radius: 50%; background: #409eff; color: #fff; font-size: 13px; flex-shrink: 0;
}
.msg.user .msg-role { background: #67c23a; }
.msg-body { max-width: 78%; margin: 0 14px; }
.msg.user .msg-body { text-align: right; }
.msg-text {
  background: #f4f6f8; padding: 10px 14px; border-radius: 8px;
  white-space: pre-wrap; word-break: break-word; font-size: 14px; color: #303133;
  text-align: left; line-height: 1.7;
}
.msg.user .msg-text { background: #e6f7ee; }
.msg-text.error { background: #fef0f0; color: #c0392b; }
.msg-text.loading { color: #909399; font-style: italic; }

.refs { margin-top: 10px; text-align: left; }
.refs-title { font-size: 12px; color: #909399; margin-bottom: 6px; }
.ref-card {
  border: 1px solid #e4e7ed; border-radius: 6px; padding: 8px 10px;
  margin-bottom: 6px; cursor: pointer; background: #fff; transition: .15s;
}
.ref-card:hover { border-color: #409eff; background: #f5faff; }
.ref-name { font-size: 13px; color: #303133; font-weight: 600; }
.ref-cat { font-size: 11px; color: #909399; margin-top: 2px; }
.ref-snippet {
  font-size: 12px; color: #606266; margin-top: 4px;
  max-height: 48px; overflow: hidden; text-overflow: ellipsis;
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical;
}

.chat-input {
  display: flex; padding: 12px 16px; border-top: 1px solid #e4e7ed; gap: 10px;
  background: #fafbfc;
}
.chat-input textarea {
  flex: 1; height: 56px; resize: none; border: 1px solid #dcdfe6; border-radius: 6px;
  padding: 8px 10px; font-size: 14px; font-family: inherit;
}
.btn-send {
  width: 80px; border: none; background: #409eff; color: #fff; border-radius: 6px;
  cursor: pointer; font-size: 14px;
}
.btn-send:disabled { background: #a0cfff; cursor: not-allowed; }

/* 参考来源弹窗 */
.ref-modal-mask {
  position: fixed; inset: 0; background: rgba(0,0,0,.45);
  display: flex; align-items: center; justify-content: center; z-index: 1000;
}
.ref-modal {
  width: 560px; max-width: 92vw; max-height: 80vh; background: #fff;
  border-radius: 10px; box-shadow: 0 10px 40px rgba(0,0,0,.25);
  display: flex; flex-direction: column; overflow: hidden;
}
.ref-modal-head {
  display: flex; justify-content: space-between; align-items: center;
  padding: 14px 18px; border-bottom: 1px solid #eee; background: #f5f8ff;
}
.ref-modal-name { font-size: 15px; font-weight: 600; color: #303133; }
.ref-modal-close {
  border: none; background: transparent; font-size: 18px; color: #909399;
  cursor: pointer; line-height: 1; padding: 2px 6px; border-radius: 4px;
}
.ref-modal-close:hover { background: #e6e6e6; color: #303133; }
.ref-modal-body { padding: 16px 18px; overflow-y: auto; }
.ref-modal-row { font-size: 13px; color: #606266; margin-bottom: 8px; }
.ref-modal-label {
  display: inline-block; min-width: 56px; color: #909399; margin-right: 8px;
}
.ref-modal-section {
  font-size: 13px; font-weight: 600; color: #303133; margin: 12px 0 6px;
  border-left: 3px solid #409eff; padding-left: 8px;
}
.ref-modal-snippet {
  background: #f7f8fa; border-radius: 6px; padding: 12px 14px;
  font-size: 13px; line-height: 1.8; color: #303133; white-space: pre-wrap;
  word-break: break-word; max-height: 280px; overflow-y: auto;
}
.ref-modal-tip {
  margin-top: 12px; font-size: 12px; color: #a0a4ab; text-align: right;
}
</style>
