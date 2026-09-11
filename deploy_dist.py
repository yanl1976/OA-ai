#!/usr/bin/env python3
"""仅推送 web_vue/dist 到远端 /opt/OA-ai/web_vue/dist（前端更新快通道）。"""
import os
import stat
import paramiko
import sys

from deploy_common import load_ssh_config

# 连接凭据从 .env 读取（切勿硬编码：本仓库是公开的，明文密码会被推送到公网）
HOST, USER, PASSWORD = load_ssh_config()
REMOTE_DIST = "/opt/OA-ai/web_vue/dist"
LOCAL_DIST = os.path.join(os.path.dirname(__file__), "web_vue", "dist")


def mkdir_p(sftp, remote):
    """递归创建远端目录（等价 mkdir -p）。权限不足时给出明确指引而非静默失败。"""
    cur = ""
    for part in [p for p in remote.rstrip("/").split("/") if p]:
        cur = "/" + part if not cur else cur + "/" + part
        try:
            sftp.stat(cur)
        except IOError:
            try:
                sftp.mkdir(cur)
            except IOError as e:
                sys.stderr.write(
                    "\n[错误] 无法创建远端目录 %s：%s\n\n"
                    "部署用户对上级目录无写权限。请先在开发机用 sudo 建好并赋权：\n\n"
                    "    sudo mkdir -p %s\n"
                    "    sudo chown -R %s:%s %s\n\n"
                    "然后再运行本脚本。\n"
                    % (cur, e, remote, USER, USER, os.path.dirname(remote))
                )
                sys.exit(1)


def rmtree(sftp, remote):
    """递归删除远端目录【内容】（paramiko 无 rmtree）。不删除 remote 自身。"""
    try:
        names = sftp.listdir(remote)
    except IOError:
        return
    for name in names:
        rp = remote.rstrip("/") + "/" + name
        try:
            st = sftp.stat(rp)
        except IOError:
            continue
        if stat.S_ISDIR(st.st_mode):
            rmtree(sftp, rp)
            try:
                sftp.rmdir(rp)
            except IOError:
                pass
        else:
            try:
                sftp.remove(rp)
            except IOError:
                pass


def upload_dir(sftp, local, remote):
    for name in sorted(os.listdir(local)):
        lp = os.path.join(local, name)
        rp = remote.rstrip("/") + "/" + name
        if os.path.isdir(lp):
            mkdir_p(sftp, rp)
            upload_dir(sftp, lp, rp)
        else:
            sftp.put(lp, rp)
            print("    ->", rp)


def main():
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(HOST, username=USER, password=PASSWORD, timeout=30)
    sftp = client.open_sftp()
    # 确保远端 dist 目录存在（缺失会在此明确报错并提示 sudo 建目录）
    mkdir_p(sftp, REMOTE_DIST)
    # 清空旧 dist 内容（哈希文件名会变化，避免残留）。只删内容、不删 dist 目录本身，
    # 以兼容「部署用户只能写已存在的 dist、不能在 /opt 下新建目录」的权限场景。
    rmtree(sftp, REMOTE_DIST)
    print("==> 上传 dist ...")
    upload_dir(sftp, LOCAL_DIST, REMOTE_DIST)
    sftp.close()
    client.close()
    print("==> 前端部署完成。")


if __name__ == "__main__":
    main()
