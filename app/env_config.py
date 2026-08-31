"""部署配置（.env）的安全读写。

为什么单独抽这一层
------------------
1. **保留注释**：.env 里大量说明性注释（如 KB_ROOT 的"切勿填成 knowledge_base"），
   用 python-dotenv 的 dotenv.set_key 会打乱/丢失它们，运维再也看不到注意事项。
   本模块逐行处理，**只替换等号右边的取值，原样保留注释与空行**。
2. **原子写 + 备份**：先写临时文件再 os.replace，写前自动备份 .env.bak，
   避免"写一半断电"留下半截配置导致服务起不来。
3. **敏感键不回显**：授权码、口令类配置读取时只返回"是否已设置"，
   明文永不外传（详见 SENSITIVE_KEYS 与 mask()）。
4. **即时生效**：写文件后同步更新 os.environ，无需重启服务。

【注意】serve.py 启动时以 override=True 加载 .env，因此**文件是最高配置源**；
本模块读取时同样以文件为准（否则界面会显示与运行时不一致的旧值）。
"""
import os
import re
import shutil
import tempfile

KB_ROOT = os.environ.get("KB_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_FILE = os.path.join(KB_ROOT, ".env")
ENV_BAK = ENV_FILE + ".bak"

# 敏感键：读取时只返回是否非空，绝不回传明文（前端显示为"已设置"）
SENSITIVE_KEYS = {
    "EMAIL_PASSWORD",      # 邮箱 SMTP 授权码
    "KB_ADMIN_PASS",       # 初始管理员口令
    "WECCORP_CORPSECRET",  # 企微应用凭证
    "WECCORP_AESKEY",      # 企微回调密钥
    "WECCORP_TOKEN",
}

# KEY=value 行；允许 `export KEY=value`，值可带引号，行尾可有注释
_LINE_RE = re.compile(r"^(\s*(?:export\s+)?)([A-Za-z_][A-Za-z0-9_]*)(\s*=\s*)(.*)$")


def set_root(kb_root: str):
    """重新绑定部署根（与 admin / user_pool 的 set_root 保持同一约定）。"""
    global KB_ROOT, ENV_FILE, ENV_BAK
    KB_ROOT = kb_root
    ENV_FILE = os.path.join(KB_ROOT, ".env")
    ENV_BAK = ENV_FILE + ".bak"


def _split_value_comment(raw: str):
    """把 `value # comment` 拆成 (value, comment)；引号内的 # 不算注释。"""
    q = None
    for i, ch in enumerate(raw):
        if q:
            if ch == q:
                q = None
        elif ch in ("'", '"'):
            q = ch
        elif ch == "#":
            return raw[:i].rstrip(), raw[i:]
    return raw.rstrip(), ""


def _quote_if_needed(value: str) -> str:
    """值中含空格、#、引号等特殊字符时加引号，避免 .env 解析歧义。

    邮箱授权码一般不含空格，但收件人若填了多个地址（逗号分隔）或带空格就必需。
    """
    if value == "":
        return ""
    if any(c in value for c in (" ", "#", "'", '"', "\t")):
        return '"%s"' % value.replace("\\", "\\\\").replace('"', '\\"')
    return value


def _unquote(value: str) -> str:
    v = value.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
        return v[1:-1]
    return v


def read_file_text() -> str:
    try:
        with open(ENV_FILE, "r", encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def parse(text: str):
    """解析为 [(raw_line, key_or_None, value_or_None, tail_comment)]。"""
    out = []
    for line in text.splitlines():
        m = _LINE_RE.match(line)
        if not m:
            out.append([line, None, None, ""])
            continue
        val, comment = _split_value_comment(m.group(4))
        out.append([line, m.group(2), _unquote(val), comment])
    return out


def get(key: str, default: str = "") -> str:
    """读取配置值：.env 文件优先，未命中回落到进程环境变量。"""
    for _raw, k, v, _c in parse(read_file_text()):
        if k == key:
            return v
    return os.environ.get(key, default)


def get_many(keys) -> dict:
    text = read_file_text()
    found = {k: v for _r, k, v, _c in parse(text) if k is not None}
    return {k: found.get(k, os.environ.get(k, "")) for k in keys}


def is_set(key: str) -> bool:
    return bool((get(key) or "").strip())


def mask(key: str) -> str:
    """敏感键的回显形式：只提示是否已设置，不泄露任何字符。"""
    return "已设置" if is_set(key) else ""


def set_many(values: dict, note: str = None) -> dict:
    """更新若干键，保留注释与未涉及的行；返回实际写入的键。

    values: {KEY: str}；值为 None 表示【不修改】该键（便于前端"留空不改"语义）。
    """
    values = {k: v for k, v in (values or {}).items() if v is not None}
    if not values:
        return {}
    text = read_file_text()
    lines = parse(text)
    seen = set()
    for item in lines:
        key = item[1]
        if key in values and key not in seen:
            indent, _k, sep, _v = _LINE_RE.match(item[0]).groups()
            new_val = _quote_if_needed(str(values[key]))
            comment = item[3]
            item[0] = "%s%s%s%s%s" % (indent, key, sep, new_val, (" " + comment) if comment else "")
            item[2] = str(values[key])
            seen.add(key)
    # 未出现的键：追加到文件末尾（带分组注释更友好）
    appended = [k for k in values if k not in seen]
    if appended:
        if text and not text.endswith("\n"):
            lines.append(["", None, None, ""])
        if note:
            lines.append(["", None, None, ""])
            lines.append(["# %s" % note, None, None, ""])
        for k in appended:
            lines.append(["%s=%s" % (k, _quote_if_needed(str(values[k]))), None, None, ""])
    new_text = "\n".join(item[0] for item in lines)
    if text and text.endswith("\n"):
        new_text += "\n"

    os.makedirs(os.path.dirname(ENV_FILE), exist_ok=True)
    if os.path.exists(ENV_FILE):
        try:
            shutil.copy2(ENV_FILE, ENV_BAK)
        except OSError:
            pass
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(ENV_FILE), prefix=".env_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(new_text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, ENV_FILE)
    except Exception:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
        raise
    # 同步到进程环境，使改动立即生效（无需重启服务）
    for k, v in values.items():
        os.environ[k] = str(v)
    return values
