"""邮件通知模块（知识库 OA-ai）。

配置来源：部署根 `.env`（经 env_config 安全读写层），键名如下——
  EMAIL_ENABLED        : 0/1，总开关
  EMAIL_TYPE           : smtp（当前仅支持标准 SMTP）
  EMAIL_HOST           : SMTP 服务器，如 smtp.qq.com
  EMAIL_PORT           : 端口，如 465(SSL) / 587(STARTTLS) / 25
  EMAIL_USER           : 发件人账号
  EMAIL_PASSWORD       : SMTP 授权码（敏感，绝不回显明文）
  EMAIL_TO             : 收件人，逗号分隔多个地址
  EMAIL_NOTIFY_PULL    : 0/1，云之家拉取内容更新通知
  EMAIL_NOTIFY_REGISTER: 0/1，注册审核通知

两种通知：
  1) 拉取内容更新：yzj_pull.run_task 完成后调用 notify_pull_update()。
  2) 注册审核：admin.approve_registration / reject_registration 后调用 notify_register()。

为避免阻塞主流程（拉取可能很慢、审批在请求内），两处均采用「尽力发送、吞掉异常」
策略：邮件失败只在日志告警，不影响业务结果返回。
"""
import os
import smtplib
import threading
import logging
from email.mime.text import MIMEText
from email.utils import formataddr

try:
    import env_config as _env
except ImportError:  # 允许从 scripts/ 上下文以模块方式导入
    import sys
    _env = None
    try:
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
        import env_config as _env  # noqa: F401
    except Exception:  # noqa: BLE001
        _env = None

logger = logging.getLogger("kb.notify_mail")

# 配置键（与 .env 一致）
KEYS = [
    "EMAIL_ENABLED", "EMAIL_TYPE", "EMAIL_HOST", "EMAIL_PORT",
    "EMAIL_USER", "EMAIL_PASSWORD", "EMAIL_TO",
    "EMAIL_NOTIFY_PULL", "EMAIL_NOTIFY_REGISTER",
]

# 类型映射：QQ/126/163 等常见邮箱的默认端口/加密方式（仅作 UI 提示，不强制）
TYPE_HINTS = {
    "qq":   {"host": "smtp.qq.com",   "port": "465", "ssl": True},
    "126":  {"host": "smtp.126.com",  "port": "465", "ssl": True},
    "163":  {"host": "smtp.163.com",  "port": "465", "ssl": True},
}


def _env_get(key, default=""):
    if _env is not None:
        return _env.get(key, default)
    return os.environ.get(key, default)


def _env_set_many(values: dict, note: str = None) -> dict:
    if _env is not None:
        return _env.set_many(values, note)
    # 无 env_config 时直接写 os.environ（不落盘，仅兜底）
    for k, v in values.items():
        if v is not None:
            os.environ[k] = str(v)
    return values


def load_config():
    """读取当前邮件配置（敏感字段 EMAIL_PASSWORD 只返回是否设置）。"""
    raw = {}
    for k in KEYS:
        if k == "EMAIL_PASSWORD":
            raw[k] = "已设置" if _env_get(k).strip() else ""
        else:
            raw[k] = _env_get(k)
    # 端口统一为字符串返回，前端数字框用
    if raw.get("EMAIL_PORT"):
        raw["EMAIL_PORT"] = str(raw["EMAIL_PORT"])
    return raw


def save_config(cfg: dict):
    """写回邮件配置。

    cfg 中 EMAIL_PASSWORD 若为「已设置」占位或空串则保留原值（不覆盖）。
    返回写入后的实际配置（同 load_config 口径）。
    """
    values = {}
    for k in KEYS:
        if k not in cfg:
            continue
        v = cfg[k]
        if k == "EMAIL_PASSWORD":
            # 占位或空表示「不改」
            if v in ("已设置", "", None):
                continue
        values[k] = "" if v is None else str(v)
    _env_set_many(values, note="邮件系统配置（知识库 OA-ai）")
    return load_config()


def _is_enabled():
    return _env_get("EMAIL_ENABLED", "").strip() in ("1", "true", "True", "yes")


def _pull_enabled():
    return _env_get("EMAIL_NOTIFY_PULL", "").strip() in ("1", "true", "True", "yes")


def _register_enabled():
    return _env_get("EMAIL_NOTIFY_REGISTER", "").strip() in ("1", "true", "True", "yes")


def _build_message(subject, body, to_list):
    """构造 MIMEText；发件人用 EMAIL_USER 的账号名（无友好名也行）。"""
    user = _env_get("EMAIL_USER").strip()
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = formataddr((user or "OA-ai", user or "OA-ai"))
    msg["To"] = ", ".join(to_list)
    return msg


def _send(subject, body):
    """真正发送（同步）。调用方负责判断是否启用、是否在子线程。

    返回 (ok, detail)。任何异常都转成 detail 字符串，绝不抛出到业务层。
    """
    host = _env_get("EMAIL_HOST").strip()
    port_raw = _env_get("EMAIL_PORT").strip()
    user = _env_get("EMAIL_USER").strip()
    pwd = _env_get("EMAIL_PASSWORD").strip()
    to_raw = _env_get("EMAIL_TO").strip()
    if not host or not user or not pwd:
        return False, "SMTP 主机/账号/授权码未配置完整"
    to_list = [t.strip() for t in to_raw.split(",") if t.strip()]
    if not to_list:
        return False, "收件人(EMAIL_TO)未配置"
    try:
        port = int(port_raw) if port_raw else 465
    except ValueError:
        return False, "端口(EMAIL_PORT)不是合法数字: %r" % port_raw

    msg = _build_message(subject, body, to_list)
    # 优先 SSL（465），其次 STARTTLS（587），否则明文（25，仅内网可用）
    try:
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=15) as s:
                s.login(user, pwd)
                s.sendmail(user, to_list, msg.as_string())
        elif port == 587:
            with smtplib.SMTP(host, port, timeout=15) as s:
                s.ehlo()
                s.starttls()
                s.ehlo()
                s.login(user, pwd)
                s.sendmail(user, to_list, msg.as_string())
        else:
            with smtplib.SMTP(host, port, timeout=15) as s:
                s.ehlo()
                if user:
                    s.login(user, pwd)
                s.sendmail(user, to_list, msg.as_string())
        return True, "已发送至 %s" % ", ".join(to_list)
    except Exception as e:  # noqa: BLE001
        return False, "%s: %s" % (type(e).__name__, e)


def _send_async(subject, body):
    """子线程发送，避免阻塞主流程。异常仅记日志。"""
    def _run():
        ok, detail = _send(subject, body)
        if ok:
            logger.info("邮件通知发送成功: %s", detail)
        else:
            logger.warning("邮件通知发送失败: %s | 主题=%s", detail, subject)
    t = threading.Thread(target=_run, name="mail-notify", daemon=True)
    t.start()


# 邮件正文里最多列出的文件条数，避免一次拉取几百个附件把邮件撑爆
MAX_FILE_LIST = 50


def notify_pull_update(task_name, stats):
    """云之家拉取完成后的内容更新通知。

    task_name: 任务名（如「天传所会议纪要」）；stats: run_task 的返回 dict。
    stats 可含 "files"：[(filename, created_at), ...]，用于列出「文件名 + 入库时间」。
    """
    if not _is_enabled() or not _pull_enabled():
        return
    downloaded = (stats or {}).get("downloaded", 0)
    failed = (stats or {}).get("failed", 0)
    files = (stats or {}).get("files") or []
    subject = "【OA-ai 知识库】云之家拉取内容更新：%s（新增 %d）" % (task_name, downloaded)

    body = (
        "云之家审批单据拉取任务已完成。\n\n"
        "任务名称：%s\n"
        "新增/更新文档数：%d\n"
        "失败数：%d\n"
        "完成时间：%s\n"
        % (task_name, downloaded, failed, _now_str())
    )
    if files:
        body += "\n本次入库文件（文件名 — 入库时间）：\n"
        for i, item in enumerate(files[:MAX_FILE_LIST], 1):
            # 兼容 (文件名, 时间) 二元组与纯文件名字符串
            if isinstance(item, (list, tuple)):
                fn, tm = (item + ("", ""))[:2]
            else:
                fn, tm = item, ""
            body += "  %d. %s%s\n" % (i, fn, ("  —  %s" % tm) if tm else "")
        if len(files) > MAX_FILE_LIST:
            body += "  ……（共 %d 个文件，此处仅列前 %d 个）\n" % (len(files), MAX_FILE_LIST)
    else:
        body += "\n本次没有新增文件。\n"
    body += "\n可登录系统「云之家数据拉取」页面查看明细，或检索最新入库文档。"
    _send_async(subject, body)


def notify_register(action, emp_no, name, reviewer="", note="", registered_at=""):
    """注册审核结果通知。

    action: "approved" / "rejected"。
    registered_at: 用户提交注册的申请时间（user_registrations.created_at）；
                   为空时回退为「—」，注明是审核时间。
    """
    if not _is_enabled() or not _register_enabled():
        return
    verb = "通过" if action == "approved" else "驳回"
    subject = "【OA-ai 知识库】用户注册申请%s：%s（%s）" % (verb, name, emp_no)
    body = (
        "用户自助注册申请已%s。\n\n"
        "注册人：%s\n"
        "工号：%s\n"
        "注册时间：%s\n"
        "审核人：%s\n"
        "审核时间：%s\n"
        "审核备注：%s\n"
        % (verb, name, emp_no, registered_at or "—", reviewer or "—",
           _now_str(), note or "—")
    )
    _send_async(subject, body)


def _now_str():
    import datetime
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def send_test(subject="【OA-ai 知识库】邮件配置测试", body="这是一封来自 OA-ai 知识库的测试邮件，如收到说明配置正确。", force=False):
    """测试发送（同步返回结果，供前端「发送测试邮件」按钮使用）。

    force=True 时忽略总开关检查——测试目的是验证 SMTP 可达性，即使总开关未开也应允许测试。
    实际业务通知（拉取/注册）仍受总开关 + 各自开关双重控制，不受 force 影响。
    """
    if not force and not _is_enabled():
        return False, "邮件总开关(EMAIL_ENABLED)未开启"
    if not _env_get("EMAIL_HOST").strip() or not _env_get("EMAIL_USER").strip() or not _env_get("EMAIL_PASSWORD").strip():
        return False, "SMTP 主机/账号/授权码未配置（请先填写后测试）"
    return _send(subject, body)
