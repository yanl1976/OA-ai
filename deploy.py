#!/usr/bin/env python3
"""Windows 侧部署脚本：将 kb_deploy/ 推送到 Ubuntu /opt/OA-ai 并执行安装。

依赖: pip install paramiko
用法: python deploy.py
"""
import os
import sys
import paramiko

from deploy_common import load_ssh_config

# 连接凭据从 .env 读取（切勿硬编码：本仓库是公开的，明文密码会被推送到公网）
HOST, USER, PASSWORD = load_ssh_config()
REMOTE_ROOT = "/opt/OA-ai"
LOCAL_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__)))

# 需要排除的文件（不传输）—— 这些为运行期产物，由服务器 install.sh 重建
EXCLUDE = {".DS_Store", "Thumbs.db", "__pycache__", "*.pyc", ".git",
           "data", "bm25_index", ".jieba_cache", "uploads", ".venv"}


def run(client, cmd, timeout=600):
    """执行远端命令，返回 (stdout, stderr, exit_code)"""
    stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode("utf-8", "replace")
    err = stderr.read().decode("utf-8", "replace")
    code = stdout.channel.recv_exit_status()
    return out, err, code


def upload_dir(sftp, local, remote):
    for name in sorted(os.listdir(local)):
        if name in EXCLUDE or name.endswith(".pyc"):
            continue
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
            print(f"    -> {rp}")


def main():
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    print(f"==> 连接 {USER}@{HOST} ...")
    client.connect(HOST, username=USER, password=PASSWORD, timeout=30)

    # 探测目标环境
    out, _, _ = run(client, "uname -a; python3 --version; whoami")
    print("==> 目标环境:\n" + out)

    # 创建远端目录并赋权给当前用户（一次性 sudo，避免逐个文件 sudo）
    print(f"==> 准备远端目录 {REMOTE_ROOT} ...")
    _, err, code = run(
        client,
        f"echo '{PASSWORD}' | sudo -S mkdir -p {REMOTE_ROOT} "
        f"&& echo '{PASSWORD}' | sudo -S chown -R {USER}:{USER} {REMOTE_ROOT}",
    )
    if code != 0:
        print("[警告] 远端目录准备返回非零，继续执行:", err)

    # SFTP 上传
    print("==> 上传文件 ...")
    sftp = client.open_sftp()
    try:
        sftp.mkdir(REMOTE_ROOT)
    except IOError:
        pass
    upload_dir(sftp, LOCAL_ROOT, REMOTE_ROOT)
    sftp.close()

    # 赋予脚本可执行权限
    run(client, f"chmod +x {REMOTE_ROOT}/scripts/*.sh {REMOTE_ROOT}/scripts/serve.py")

    # 以 root 运行安装脚本（sudo -S 传密码）
    print("==> 运行 install.sh (sudo) ...  （可能需要几分钟，请耐心等待）")
    out, err, code = run(
        client,
        f"echo '{PASSWORD}' | sudo -S bash {REMOTE_ROOT}/scripts/install.sh",
        timeout=1800,
    )
    print(out)
    if err.strip():
        print("[stderr]", err)
    if code != 0:
        print(f"[错误] install.sh 退出码 {code}")
        client.close()
        sys.exit(1)

    # 验证
    print("==> 验证服务 ...")
    out, _, _ = run(client, "sleep 4; curl -s http://127.0.0.1:8080/api/health || echo '无响应(curl 不可用或服务未起)'")
    print(out)

    client.close()
    print("==> 部署完成。访问 http://<服务器IP>:8080/")


if __name__ == "__main__":
    main()
