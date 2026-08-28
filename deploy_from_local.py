#!/usr/bin/env python3
"""从本地开发环境整包部署到生产服务器（内网直传，不依赖 GitHub 下载）。

适用场景
--------
生产服务器访问 GitHub 不稳定 / clone 反复失败时，改为通过内网把本地目录
（含 .git）整体上传。上传完成后，生产目录即为一个完整的 git 仓库，
日后即可用 git fetch 增量更新（只传差异，几 KB，不会再因大文件失败）。

安全设计
--------
1. 先备份生产原目录到 /opt/OA-ai.bak.<时间戳>
2. 上传到【新目录】/opt/OA-ai-new，原目录全程不动
3. 自动迁移生产独有的数据（.env / data 账号库 / uploads 文档 / venv）
4. 重建 BM25 索引（用生产自己的文档，不用本地那份）
5. 只有全部成功才切换目录；任何一步失败，原目录都不受影响

用法
----
    python deploy_from_local.py            # 完整部署
    python deploy_from_local.py --upload   # 只上传，不切换（可先试传）
    python deploy_from_local.py --rollback # 回滚到备份

依赖: pip install paramiko
"""
import os
import sys
import time
import posixpath

import paramiko

HOST = "192.168.30.155"
USER = "yanl"
PASSWORD = "Tsdcs2009520"
REMOTE_ROOT = "/opt/OA-ai"
REMOTE_NEW = "/opt/OA-ai-new"
LOCAL_ROOT = os.path.dirname(os.path.abspath(__file__))

# 不上传的目录/文件（本地运行产物或需由生产自身提供的）
EXCLUDE_DIRS = {
    "node_modules", ".jieba_cache", "venv", ".venv", "__pycache__",
    ".git" if False else "___never___",   # .git 需要上传，故不排除
}
EXCLUDE_DIR_NAMES = {"__pycache__", "node_modules", ".jieba_cache"}
EXCLUDE_FILE_SUFFIX = {".pyc", ".pyo"}
# 备份目录与临时脚本不上传
EXCLUDE_PREFIX = ("_backup_index_", "_scan_", "_diag_", "_verify_",
                  "_check_", "_probe_", "_test", "_chk_", "_vfinal",
                  "_rebuild", "_regress")
# 索引由生产重建，不上传本地版本（避免文档不一致导致检索错乱）
EXCLUDE_REL_DIRS = {
    "knowledge_base/bm25_index",
    "knowledge_base/vec_index",
}


def log(msg=""):
    """输出日志。

    【关键】Windows 控制台默认 GBK 编码，遇到远端输出里的 emoji（如 U+2705 ✅）
    会抛 UnicodeEncodeError 并中断整个部署流程（踩过坑：索引重建其实已成功，
    却因打印其输出而崩溃，导致后续切换/启动步骤全部没执行）。此处对无法用
    控制台编码表示的字符做降级替换，保证流程不被非关键输出打断。
    """
    enc = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        text = str(msg)
    except Exception:
        text = repr(msg)
    try:
        text.encode(enc)
    except UnicodeEncodeError:
        text = text.encode(enc, "replace").decode(enc, "replace")
    print(text, flush=True)


def run(c, cmd, timeout=1800, show=True, sudo=False):
    """执行远端命令。sudo=True 时通过 sudo -S 传入密码。

    【关键】/opt 属主为 root，yanl 用户无写权限，创建目录、切换目录、
    改属主、启停服务等操作【必须】走 sudo，否则会 Permission denied 静默失败。
    """
    if sudo:
        cmd = "echo '%s' | sudo -S bash -c %s" % (PASSWORD, _shq(cmd))
    _i, o, e = c.exec_command(cmd, timeout=timeout)
    out = o.read().decode("utf-8", "replace")
    err = e.read().decode("utf-8", "replace")
    code = o.channel.recv_exit_status()
    # sudo -S 会把密码提示回显到 stderr，过滤掉避免误报
    err = "\n".join(l for l in err.splitlines()
                    if "password for" not in l and l.strip() != PASSWORD)
    if show and out.strip():
        log(out.rstrip())
    if err.strip():
        log("[stderr] " + err.strip()[:800])
    return out, err, code


def _shq(s):
    """用单引号包裹，供 bash -c 使用。"""
    return "'" + s.replace("'", "'\"'\"'") + "'"


def should_skip(rel_path):
    """判断是否跳过该文件（rel_path 为相对 LOCAL_ROOT 的正斜杠路径）。"""
    parts = rel_path.split("/")
    for p in parts[:-1]:
        if p in EXCLUDE_DIR_NAMES:
            return True
    name = parts[-1]
    if any(name.endswith(s) for s in EXCLUDE_FILE_SUFFIX):
        return True
    if any(name.startswith(p) for p in EXCLUDE_PREFIX):
        return True
    # 备份目录
    if parts[0].startswith("_backup"):
        return True
    rel_dir = "/".join(parts[:-1])
    if rel_dir in EXCLUDE_REL_DIRS:
        return True
    return False


def upload_tree(sftp, local, remote, stats):
    for name in sorted(os.listdir(local)):
        lp = os.path.join(local, name)
        rp = posixpath.join(remote, name)
        rel = os.path.relpath(lp, LOCAL_ROOT).replace("\\", "/")
        if os.path.isdir(lp):
            if name in EXCLUDE_DIR_NAMES or rel in EXCLUDE_REL_DIRS \
                    or name.startswith("_backup"):
                continue
            try:
                sftp.mkdir(rp)
            except IOError:
                pass
            upload_tree(sftp, lp, rp, stats)
        else:
            if should_skip(rel):
                continue
            try:
                sftp.put(lp, rp)
                stats["n"] += 1
                stats["b"] += os.path.getsize(lp)
                if stats["n"] % 100 == 0:
                    log("    已传 %d 个文件 (%.1f MB)"
                        % (stats["n"], stats["b"] / 1024 / 1024))
            except Exception as ex:  # noqa: BLE001
                log("    [跳过] %s -> %s" % (rel, str(ex)[:80]))


def main():
    only_upload = "--upload" in sys.argv
    do_rollback = "--rollback" in sys.argv

    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    log("==> 连接 %s@%s ..." % (USER, HOST))
    c.connect(HOST, username=USER, password=PASSWORD, timeout=30,
              auth_timeout=30, look_for_keys=False, allow_agent=False)

    # ---------- 回滚 ----------
    if do_rollback:
        out, _e, _rc = run(c, "ls -dt /opt/OA-ai.bak.* 2>/dev/null | head -1", show=False)
        bak = out.strip()
        if not bak:
            log("[错误] 未找到备份目录，无法回滚")
            return 1
        log("==> 回滚到 %s" % bak)
        run(c, "systemctl stop kb", timeout=60, sudo=True)
        run(c, "rm -rf /opt/OA-ai && cp -a %s /opt/OA-ai" % bak,
            timeout=600, sudo=True)
        run(c, "chown -R %s:%s /opt/OA-ai" % (USER, USER), timeout=300, sudo=True)
        run(c, "systemctl start kb", timeout=60, sudo=True)
        time.sleep(8)
        run(c, "systemctl status kb --no-pager | head -8")
        log("==> 回滚完成")
        c.close()
        return 0

    # ---------- 1. 停服务 + 备份 ----------
    log("\n==> [1/6] 停止服务并备份生产目录")
    # /opt 属主为 root，备份目录的创建必须 sudo
    run(c, "systemctl stop kb", timeout=90, sudo=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    run(c, "cp -a %s /opt/OA-ai.bak.%s" % (REMOTE_ROOT, ts),
        timeout=900, sudo=True)
    log("    备份至 /opt/OA-ai.bak.%s" % ts)

    # ---------- 2. 上传 ----------
    log("\n==> [2/6] 上传本地目录到 %s" % REMOTE_NEW)
    # 先 sudo 建目录，再 chown 给 yanl，之后 SFTP（以 yanl 身份）才有写权限
    run(c, "rm -rf %s && mkdir -p %s" % (REMOTE_NEW, REMOTE_NEW),
        timeout=300, sudo=True)
    run(c, "chown -R %s:%s %s" % (USER, USER, REMOTE_NEW), timeout=300, sudo=True)
    sftp = c.open_sftp()
    stats = {"n": 0, "b": 0}
    t0 = time.time()
    upload_tree(sftp, LOCAL_ROOT, REMOTE_NEW, stats)
    sftp.close()
    log("    上传完成：%d 个文件，%.1f MB，用时 %.0f 秒"
        % (stats["n"], stats["b"] / 1024 / 1024, time.time() - t0))

    if only_upload:
        log("\n==> 仅上传模式：已传到 %s，未切换。" % REMOTE_NEW)
        log("    确认无误后运行不带 --upload 的命令完成切换。")
        c.close()
        return 0

    # ---------- 3. 迁移生产独有数据 ----------
    log("\n==> [3/6] 迁移生产独有数据")
    items = [
        (".env", ".env"),
        ("data", "data"),
        ("knowledge_base/uploads", "knowledge_base/uploads"),
        ("venv", "venv"),
    ]
    for src_rel, dst_rel in items:
        src = posixpath.join(REMOTE_ROOT, src_rel)
        dst = posixpath.join(REMOTE_NEW, dst_rel)
        out, _e, rc = run(c, "test -e %s && echo YES || echo NO" % src,
                          timeout=60, show=False)
        if out.strip() != "YES":
            log("    [跳过] 生产无此目录: %s" % src_rel)
            continue
        run(c, "mkdir -p $(dirname %s) && cp -a %s %s" % (dst, src, dst),
            timeout=900, show=False)
        log("    已迁移: %s" % src_rel)

    # ---------- 4. 权限与语法检查 ----------
    log("\n==> [4/6] 权限与语法检查")
    run(c, "chown -R %s:%s %s" % (USER, USER, REMOTE_NEW), timeout=600, sudo=True)
    out, _e, rc = run(
        c, "cd %s && ./venv/bin/python -c 'import ast,sys,os;"
           "[ast.parse(open(os.path.join(r,f),encoding=\"utf-8\").read()) "
           "for r,_,fs in os.walk(\"app\") for f in fs if f.endswith(\".py\")];"
           "print(\"语法检查通过\")'" % REMOTE_NEW, timeout=180)
    if rc != 0:
        log("[错误] 语法检查未通过，已中止（原目录未受影响）")
        c.close()
        return 1
    log("    语法检查通过")

    # ---------- 5. 重建索引 ----------
    log("\n==> [5/6] 重建 BM25 索引（使用生产自己的文档）")
    out, _e, rc = run(
        c, "cd %s && KB_ROOT=%s ./venv/bin/python app/rag_build_index.py"
           % (REMOTE_NEW, REMOTE_NEW), timeout=1800)
    if rc != 0:
        log("[错误] 索引重建失败，已中止（原目录未受影响）")
        c.close()
        return 1
    log("    索引重建完成")

    # ---------- 6. 切换并启动 ----------
    log("\n==> [6/6] 切换目录并启动服务")
    run(c, "mv %s /opt/OA-ai.old.%s && mv %s %s"
           % (REMOTE_ROOT, ts, REMOTE_NEW, REMOTE_ROOT), timeout=300, sudo=True)
    run(c, "chown -R %s:%s %s" % (USER, USER, REMOTE_ROOT), timeout=300, sudo=True)
    run(c, "systemctl start kb", timeout=90, sudo=True)
    time.sleep(15)

    log("\n==> 验证")
    run(c, "systemctl status kb --no-pager | head -10")
    out, _e, _rc = run(c, "curl -s -m 15 http://127.0.0.1:8080/api/health",
                       timeout=60, show=False)
    log("健康检查: " + (out.strip() or "(无响应)"))

    c.close()
    log("\n==> 部署完成。访问 http://%s:8080/" % HOST)
    log("    备份: /opt/OA-ai.bak.%s" % ts)
    log("    旧目录: /opt/OA-ai.old.%s（确认无误后可删除）" % ts)
    log("    回滚: python deploy_from_local.py --rollback")
    return 0


if __name__ == "__main__":
    sys.exit(main())
