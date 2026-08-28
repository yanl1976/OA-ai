#!/usr/bin/env python3
"""推送后端改动文件到远端 /opt/OA-ai 并重启服务。

推送: scripts/serve.py, app/derived_store.py(新增), app/admin.py
重启: 优先 systemctl restart kb，失败则 pkill 由 systemd 自动拉起。
"""
import os
import posixpath
import paramiko

from deploy_common import load_ssh_config

# 连接凭据从 .env 读取（切勿硬编码：本仓库是公开的，明文密码会被推送到公网）
HOST, USER, PASSWORD = load_ssh_config()
REMOTE_ROOT = "/opt/OA-ai"

FILES = [
    ("scripts/serve.py", "scripts/serve.py"),
    ("app/derived_store.py", "app/derived_store.py"),
    ("app/admin.py", "app/admin.py"),
    ("app/extract_text.py", "app/extract_text.py"),
    ("app/pdf_make.py", "app/pdf_make.py"),
    ("app/search.py", "app/search.py"),
    ("app/rag_query.py", "app/rag_query.py"),
    ("app/vec_store.py", "app/vec_store.py"),
    ("app/kb_store.py", "app/kb_store.py"),
]


def put_dir(sftp, local_dir, remote_dir):
    """递归上传目录（含子目录）。"""
    for root, _dirs, files in os.walk(local_dir):
        rel = os.path.relpath(root, local_dir)
        remote_sub = remote_dir if rel == "." else posixpath.join(remote_dir, rel)
        try:
            sftp.stat(remote_sub)
        except IOError:
            sftp.mkdir(remote_sub)
        for fn in files:
            lp = os.path.join(root, fn)
            rp = posixpath.join(remote_sub, fn)
            sftp.put(lp, rp)
            print("    ->", rp)


def main():
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(HOST, username=USER, password=PASSWORD, timeout=30)
    sftp = client.open_sftp()

    local_root = os.path.dirname(__file__)
    for local_rel, remote_rel in FILES:
        lp = os.path.join(local_root, local_rel)
        rp = posixpath.join(REMOTE_ROOT, remote_rel)
        sftp.put(lp, rp)
        print("    ->", rp)

    # 同步前端生产构建产物 web_vue/dist
    local_dist = os.path.join(local_root, "web_vue", "dist")
    if os.path.isdir(local_dist):
        print("==> 同步前端 dist ...")
        put_dir(sftp, local_dist, posixpath.join(REMOTE_ROOT, "web_vue", "dist"))

    sftp.close()

    # 重启服务
    for cmd in ["sudo systemctl restart kb", "systemctl restart kb",
                "pkill -f 'scripts/serve.py'"]:
        try:
            stdin, stdout, stderr = client.exec_command(cmd, timeout=20)
            code = stdout.channel.recv_exit_status()
            if code == 0:
                print("==> 重启命令执行成功:", cmd)
                break
            else:
                print("    (跳过) 命令返回非0:", cmd, "->", stderr.read().decode().strip()[:120])
        except Exception as e:
            print("    (异常) 命令:", cmd, "->", str(e)[:120])

    client.close()
    print("==> 后端部署完成，等待服务拉起…")


if __name__ == "__main__":
    main()
