"""用户池（自助注册白名单）——以 `config/user_pool.json` 为唯一数据源。

设计要点
--------
1. **名册与代码分离**：工号 / 姓名 / 预设角色全部落在这个 JSON 文件里，
   `app/admin.py` 只负责「读它、用它」，名单不再硬编码进源码。
   后期 HR 提供真实花名册：直接替换本文件，或经「用户管理 → 用户池 → 批量导入」
   写回本文件（后者会原子覆盖并保留一份 `.bak`）。
2. **文件热生效**：每次查询都重新读盘，改完文件无需重启服务。
3. **不落库、不存「是否已注册」**：注册占用状态由 `users` 表按工号派生
   （见 `admin.list_user_pool`）。这样删除账号后工号自动恢复可注册，
   不存在「两处状态不一致 / 占用标记忘记清」的问题。
4. **本模块不碰数据库**（角色名 → 角色 id 的解析由 admin.py 完成），
   保证它是纯粹的文件读写层，便于单独测试与替换。

文件格式
--------
```json
{
  "version": 1,
  "source": "来源说明（如「2026-09 人力花名册」）",
  "updated_at": "2026-08-31 12:00:00",
  "entries": [
    {"emp_no": "E1001", "name": "张伟",
     "role": "viewer", "status": 1, "note": ""}
  ]
}
```
`role` 存【角色名】（人类可读、便于编辑），运行时由 admin.py 解析为角色 id。
"""
import os
import json
import shutil
import tempfile
from datetime import datetime

# 与 admin.py / serve.py 口径一致：根目录取 KB_ROOT（serve.py 启动时会注入），
# 回退到「本文件上级目录」，保证单独运行本模块时也指向正确的 config/。
KB_ROOT = os.environ.get("KB_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_DIR = os.path.join(KB_ROOT, "config")
POOL_FILE = os.path.join(CONFIG_DIR, "user_pool.json")
BACKUP_FILE = POOL_FILE + ".bak"

# 文件缺失时自动生成的初始内容（虚拟用户池，仅用于功能联调）
DEFAULT_DOC = {
    "version": 1,
    "source": "虚拟用户池（示例数据，待替换为真实花名册）",
    "updated_at": "",
    "entries": [
        {"emp_no": "E1001", "name": "张伟",   "role": "viewer", "status": 1, "note": ""},
        {"emp_no": "E1002", "name": "李娜",   "role": "editor", "status": 1, "note": ""},
        {"emp_no": "E1003", "name": "王强",   "role": "viewer", "status": 1, "note": ""},
        {"emp_no": "E1004", "name": "刘洋",   "role": "editor", "status": 1, "note": ""},
        {"emp_no": "E1005", "name": "陈静",   "role": "viewer", "status": 1, "note": ""},
        {"emp_no": "E1006", "name": "赵敏",   "role": "秘书",   "status": 1, "note": ""},
        {"emp_no": "E1007", "name": "孙建国", "role": "领导",   "status": 1, "note": ""},
        {"emp_no": "E1008", "name": "周涛",   "role": "viewer", "status": 1, "note": ""},
    ],
}


def set_root(kb_root: str):
    """重新绑定根目录（由 admin.py 调用，确保两个模块指向同一个部署根）。

    【为什么需要】本模块的路径在 import 时按 KB_ROOT 推导一次并缓存；
    若调用方（如 serve.py）之后才确定根目录，或测试里临时切到别的根，
    就会出现「admin 连 A 库、user_pool 读写 B 目录」的分裂。显式调用本函数即可对齐。
    """
    global KB_ROOT, CONFIG_DIR, POOL_FILE, BACKUP_FILE
    KB_ROOT = kb_root
    CONFIG_DIR = os.path.join(KB_ROOT, "config")
    POOL_FILE = os.path.join(CONFIG_DIR, "user_pool.json")
    BACKUP_FILE = POOL_FILE + ".bak"


def _now() -> str:
    """当前时间（本地时区，与 admin._now 一致）。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _norm(v) -> str:
    return ("" if v is None else str(v)).strip()


def _norm_entry(raw) -> dict:
    """把任意来源的一行数据规范成标准条目。"""
    if not isinstance(raw, dict):
        return None
    emp_no = _norm(raw.get("emp_no"))
    if not emp_no:
        return None
    return {
        "emp_no": emp_no,
        "name": _norm(raw.get("name")),
        "role": _norm(raw.get("role") or raw.get("role_name")),
        "status": 1 if int(raw.get("status", 1) or 0) else 0,
        "note": _norm(raw.get("note")),
    }


# ============ 文件读写 ============
def _ensure_dir():
    os.makedirs(CONFIG_DIR, exist_ok=True)


def load_doc() -> dict:
    """读取用户池文件；文件不存在时按模板创建。

    文件损坏（JSON 解析失败）时回退到 `.bak` 备份；
    备份也不可用时抛错，由调用方转成明确的中文提示——
    绝不能静默返回空池，否则「所有人都注册不了」且毫无报错。
    """
    _ensure_dir()
    if not os.path.exists(POOL_FILE):
        doc = json.loads(json.dumps(DEFAULT_DOC, ensure_ascii=False))
        doc["updated_at"] = _now()
        save_doc(doc)
        return doc
    for path, is_backup in ((POOL_FILE, False), (BACKUP_FILE, True)):
        if not os.path.exists(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                doc = json.load(f)
            if isinstance(doc, dict):
                doc.setdefault("entries", [])
                if not isinstance(doc["entries"], list):
                    doc["entries"] = []
                if is_backup:
                    # 备份可用：立即把它恢复成正式文件，避免下次仍读到坏文件
                    doc["updated_at"] = _now()
                    save_doc(doc)
                return doc
        except (ValueError, OSError):
            continue
    raise ValueError("用户池文件损坏且备份不可用：%s（请修复或删除该文件及 .bak）" % POOL_FILE)


def save_doc(doc: dict):
    """原子写入用户池文件：先备份旧文件，再写临时文件后 os.replace。

    原子写保证「写一半断电/异常」不会留下半截 JSON 把注册功能整体搞瘫。
    """
    _ensure_dir()
    if os.path.exists(POOL_FILE):
        try:
            shutil.copy2(POOL_FILE, BACKUP_FILE)
        except OSError:
            pass
    doc = dict(doc)
    doc["updated_at"] = _now()
    fd, tmp = tempfile.mkstemp(dir=CONFIG_DIR, prefix=".user_pool_", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, POOL_FILE)
    except Exception:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
        raise


# ============ 查询 ============
def list_entries(only_enabled: bool = False) -> list:
    """返回规范化后的池条目（按工号排序）。"""
    doc = load_doc()
    out = []
    for raw in doc.get("entries", []):
        item = _norm_entry(raw)
        if not item or not item["name"]:
            continue
        if only_enabled and item["status"] != 1:
            continue
        out.append(item)
    out.sort(key=lambda x: x["emp_no"])
    return out


def get_entry(emp_no: str):
    """按工号取条目（未命中返回 None）。"""
    target = _norm(emp_no)
    for item in list_entries():
        if item["emp_no"] == target:
            return item
    return None


# ============ 写入 ============
def create_entry(emp_no, name, role="", status=1, note="") -> dict:
    emp_no, name = _norm(emp_no), _norm(name)
    if not emp_no or not name:
        raise ValueError("工号与姓名不能为空")
    doc = load_doc()
    for raw in doc["entries"]:
        e = _norm_entry(raw)
        if e and e["emp_no"] == emp_no:
            raise ValueError("工号「%s」已存在于用户池" % emp_no)
    doc["entries"].append({
        "emp_no": emp_no, "name": name,
        "role": _norm(role), "status": 1 if status else 0, "note": _norm(note),
    })
    save_doc(doc)
    return get_entry(emp_no)


def update_entry(emp_no, data: dict) -> dict:
    """更新条目；允许改工号（改名同步更新，冲突则报错）。

    data 中未出现的字段保持原值。
    """
    old_no = _norm(emp_no)
    doc = load_doc()
    target = None
    for raw in doc["entries"]:
        e = _norm_entry(raw)
        if e and e["emp_no"] == old_no:
            target = raw
            break
    if target is None:
        raise ValueError("工号「%s」不在用户池中" % old_no)

    new_no = _norm(data.get("emp_no")) if "emp_no" in data else old_no
    if "emp_no" in data and not new_no:
        raise ValueError("工号不能为空")
    if "name" in data and not _norm(data.get("name")):
        raise ValueError("姓名不能为空")
    if new_no != old_no:
        for raw in doc["entries"]:
            e = _norm_entry(raw)
            if e and raw is not target and e["emp_no"] == new_no:
                raise ValueError("工号「%s」已存在于用户池" % new_no)

    if "emp_no" in data:
        target["emp_no"] = new_no
    if "name" in data:
        target["name"] = _norm(data["name"])
    if "role" in data:
        target["role"] = _norm(data["role"])
    if "status" in data:
        target["status"] = 1 if data["status"] else 0
    if "note" in data:
        target["note"] = _norm(data["note"])
    save_doc(doc)
    return get_entry(new_no)


def delete_entry(emp_no) -> dict:
    old = _norm(emp_no)
    doc = load_doc()
    kept, removed = [], None
    for raw in doc["entries"]:
        e = _norm_entry(raw)
        if e and e["emp_no"] == old and removed is None:
            removed = e
            continue
        kept.append(raw)
    if removed is None:
        raise ValueError("工号「%s」不在用户池中" % old)
    doc["entries"] = kept
    save_doc(doc)
    return removed


def import_entries(items, mode="merge", valid_roles=None, source: str = None) -> dict:
    """批量导入花名册。

    items       : [{emp_no, name, dept, role, note}]，role 为角色名
    mode        : merge   同工号更新、新工号新增
                  replace 先清空全部条目再导入（清空的判断在 admin 层做：
                          已注册工号由 admin 决定是否保留，本函数只管写文件）
    valid_roles : 合法角色名集合；提供时校验 role，非法则记入 errors 并跳过该行
    source      : 写入文件的来源说明
    返回 {created, updated, skipped, errors:[{row, msg}]}
    """
    if not isinstance(items, list):
        raise ValueError("导入数据必须是数组")
    doc = load_doc()
    existing = {}
    order = []
    for raw in doc.get("entries", []):
        e = _norm_entry(raw)
        if e:
            existing[e["emp_no"]] = e
            order.append(e["emp_no"])

    result = {"created": 0, "updated": 0, "skipped": 0, "errors": []}
    for i, it in enumerate(items):
        if not isinstance(it, dict):
            result["errors"].append({"row": i + 1, "msg": "数据格式不正确"})
            continue
        emp_no, name = _norm(it.get("emp_no")), _norm(it.get("name"))
        if not emp_no or not name:
            result["skipped"] += 1
            result["errors"].append({"row": i + 1, "msg": "工号或姓名为空"})
            continue
        role = _norm(it.get("role") or it.get("role_name"))
        if role and valid_roles is not None and role not in valid_roles:
            result["skipped"] += 1
            result["errors"].append({"row": i + 1, "msg": "角色「%s」不存在" % role})
            continue
        entry = {
            "emp_no": emp_no, "name": name,
            "role": role, "note": _norm(it.get("note")),
        }
        if emp_no in existing:
            old = existing[emp_no]
            # 未提供的字段保留原值；status 沿用原状（导入不擅自改启用状态）
            entry["status"] = old["status"]
            if not entry["role"]:
                entry["role"] = old["role"]
            if not entry["note"]:
                entry["note"] = old["note"]
            existing[emp_no] = entry
            result["updated"] += 1
        else:
            entry["status"] = 1
            existing[emp_no] = entry
            order.append(emp_no)
            result["created"] += 1

    doc["entries"] = [existing[k] for k in order if k in existing]
    if source:
        doc["source"] = source
    save_doc(doc)
    return result


def clear_entries(keep_emp_nos=None) -> int:
    """清空用户池，仅保留 keep_emp_nos 中的工号（替换模式用）。返回被清除条数。"""
    keep = {_norm(x) for x in (keep_emp_nos or [])}
    doc = load_doc()
    kept, removed = [], 0
    for raw in doc.get("entries", []):
        e = _norm_entry(raw)
        if e and e["emp_no"] in keep:
            kept.append(e)
        else:
            removed += 1
    doc["entries"] = kept
    save_doc(doc)
    return removed


def pool_info() -> dict:
    """文件级元信息（供界面展示来源与更新时间）。"""
    doc = load_doc()
    return {
        "file": POOL_FILE,
        "source": doc.get("source", ""),
        "updated_at": doc.get("updated_at", ""),
        "count": len(doc.get("entries", [])),
    }
