#!/usr/bin/env python3
"""在本机构建前端 dist，并提交推送到 git —— 服务器 `git pull` 即可更新前端。

【为什么需要它】
原 deploy_dist.py 走 SFTP 推到远端 /opt/OA-ai/web_vue/dist，依赖部署用户对该目录的
写权限，常因权限不足（Permission denied）或 .env 里 SSH 配置被覆盖导致连错机器而失败。
改为让 dist 随 git 同步后，本机只需构建 + 提交 + 推送，服务器 git pull 即生效。

【流程】
  1) cd web_vue && npm run build        （构建最新 dist）
  2) git add .gitignore web_vue/dist    （.gitignore 已放行 dist）
  3) 有改动才 commit（无改动则跳过）
  4) git push origin <当前分支>

【服务器侧】
  cd /opt/OA-ai && git pull
  serve.py 直接读 web_vue/dist，一般无需重启；若有缓存再 systemctl restart kb。

【用法】
  python sync_dist_git.py             # 构建 + 提交 + 推送
  python sync_dist_git.py --no-build  # 跳过构建，直接提交推送现有 dist
  python sync_dist_git.py --branch main
"""
import argparse
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WEB = ROOT / "web_vue"
DIST = WEB / "dist"


def run(cmd, cwd=None, check=True):
    print("==>", " ".join(cmd))
    r = subprocess.run(cmd, cwd=str(cwd or ROOT), capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    if r.stdout:
        print(r.stdout.rstrip())
    if r.stderr:
        print(r.stderr.rstrip(), file=sys.stderr)
    if check and r.returncode != 0:
        sys.exit("[失败] 命令返回 %d：%s" % (r.returncode, " ".join(cmd)))
    return r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-build", action="store_true",
                    help="跳过 npm build，直接提交推送现有 dist")
    ap.add_argument("--branch", default=None, help="推送分支，默认当前分支")
    args = ap.parse_args()

    if not args.no_build:
        if not (WEB / "package.json").exists():
            sys.exit("[失败] 未找到 web_vue/package.json")
        run(["npm", "run", "build"], cwd=WEB)

    if not DIST.exists():
        sys.exit("[失败] 未找到构建产物 web_vue/dist")

    branch = args.branch or run(["git", "branch", "--show-current"]).stdout.strip() or "main"

    run(["git", "add", ".gitignore", "web_vue/dist"])

    # diff --cached --quiet：返回 0 表示无待提交改动
    st = run(["git", "diff", "--cached", "--quiet"], check=False)
    if st.returncode == 0:
        print("==> 无待提交改动（dist 已是最新）")
    else:
        msg = "前端构建产物 dist 更新 %s" % time.strftime("%Y-%m-%d %H:%M:%S")
        run(["git", "commit", "-m", msg])

    run(["git", "push", "origin", branch])
    print("\n==> 完成：服务器执行 git pull 即可生效")


if __name__ == "__main__":
    main()
