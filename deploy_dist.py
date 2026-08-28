#!/usr/bin/env python3
"""仅推送 web_vue/dist 到远端 /opt/OA-ai/web_vue/dist（前端更新快通道）。"""
import os
import paramiko

from deploy_common import load_ssh_config

# 连接凭据从 .env 读取（切勿硬编码：本仓库是公开的，明文密码会被推送到公网）
HOST, USER, PASSWORD = load_ssh_config()
REMOTE_DIST = "/opt/OA-ai/web_vue/dist"
LOCAL_DIST = os.path.join(os.path.dirname(__file__), "web_vue", "dist")


def upload_dir(sftp, local, remote):
    for name in sorted(os.listdir(local)):
        lp = os.path.join(local, name)
        rp = remote.rstrip("/") + "/" + name
        if os.path.isdir(lp):
            try:
                sftp.mkdir(rp)
            except IOError:
                pass
            upload_dir(sftp, lp, rp)
        else:
            sftp.put(lp, rp)
            print("    ->", rp)


def main():
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(HOST, username=USER, password=PASSWORD, timeout=30)
    sftp = client.open_sftp()
    # 清空远端旧 dist（哈希文件名会变化，避免残留）
    try:
        sftp.rmdir(REMOTE_DIST)
    except IOError:
        pass
    try:
        sftp.mkdir(REMOTE_DIST)
    except IOError:
        pass
    print("==> 上传 dist ...")
    upload_dir(sftp, LOCAL_DIST, REMOTE_DIST)
    sftp.close()
    client.close()
    print("==> 前端部署完成。")


if __name__ == "__main__":
    main()
