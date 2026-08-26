#!/usr/bin/env python3
"""一键部署：前端构建 + 后端/前端推送远端 155 并重启。
本地手动运行：python deploy_now.py
"""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable


def run(cmd, cwd=None):
    print("==>", cmd, "@", cwd or ROOT)
    subprocess.run(cmd, cwd=cwd or ROOT, shell=True, check=True)


def main():
    # 1) 前端生产构建
    web = os.path.join(ROOT, "web_vue")
    run("rm -rf dist", cwd=web)
    run("npm run build", cwd=web)

    # 2) 推送后端 + 同步 dist 到 155
    run('"%s" deploy_backend.py' % PY, cwd=ROOT)

    print("==> 部署完成")


if __name__ == "__main__":
    main()
