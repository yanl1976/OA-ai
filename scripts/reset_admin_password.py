#!/usr/bin/env python3
"""重置 / 巡检管理员口令。

【为什么需要这个脚本】
历史上 app/admin.py 曾把默认管理员口令硬编码在源码里（该文件受 Git 版本控制，
等于口令公开）。现已改为「.env 配置或随机生成」，但**改代码不会改变已存在
数据库里的 admin 账号**——存量环境（开发机、生产机）里那个旧口令仍然有效。
因此存量环境必须显式重置一次，本脚本就是干这个的。

用法（在生产机用 /opt/OA-ai/venv/bin/python 执行）：
    # 1) 巡检：检查是否仍在使用历史遗留默认口令 / 弱口令（只读，不改库）
    python scripts/reset_admin_password.py --check

    # 2) 重置为随机强口令（打印出来 + 写入 data/initial_admin_password.txt）
    python scripts/reset_admin_password.py --random

    # 3) 重置为指定口令（注意：会被记入 shell history，仅临时排查用）
    python scripts/reset_admin_password.py --new '你的新口令'

    # 4) 把 .env 里配置的 KB_ADMIN_PASS 同步到数据库
    python scripts/reset_admin_password.py --from-env

    # 5) 对非 admin 账号操作：加 --user <用户名>
    python scripts/reset_admin_password.py --user zhangsan --random

可选：--root <部署根>（默认自动推导：本脚本上级目录 / .env 的 KB_ROOT）
"""
import os
import sys
import argparse

_HERE = os.path.dirname(os.path.abspath(__file__))
_DEFAULT_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_DEFAULT_ROOT, "app"))


def _resolve_root(cli_root):
    """确定部署根：命令行指定 > .env 的 KB_ROOT > 脚本上级目录。

    与 serve.py 同样的合法性校验：合法部署根必须含 scripts/ 目录，
    避免 .env 里误填成 .../knowledge_base 造成路径二次叠加。
    """
    candidates = []
    if cli_root:
        candidates.append(cli_root)
    env_file = os.path.join(_DEFAULT_ROOT, ".env")
    try:
        with open(env_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("KB_ROOT="):
                    candidates.append(line.partition("=")[2].strip().strip('"').strip("'"))
                    break
    except OSError:
        pass
    candidates.append(_DEFAULT_ROOT)
    for c in candidates:
        if c and os.path.isdir(os.path.join(c, "scripts")):
            if cli_root and os.path.abspath(c) != os.path.abspath(cli_root):
                # 显式指定的路径不合法却静默回退，会让运维以为改的是 A 机、实际改了 B 机
                print("[警告] --root %s 不是合法部署根（缺少 scripts/ 目录），已改用 %s"
                      % (cli_root, os.path.abspath(c)))
            return os.path.abspath(c)
    print("[错误] 无法确定合法部署根（缺少 scripts/ 目录），请用 --root 显式指定")
    sys.exit(2)


# 【历史遗留弱口令清单】仅用于**检测**存量环境是否仍在使用这些口令；
# 不是本系统的配置项，也不用于生成任何账号。
# 这些口令曾出现在源码/文档/安装脚本中，属于已公开口令，必须视为不安全。
LEGACY_WEAK_PASSWORDS = [
    "Admin@123",   # 历史硬编码在 app/admin.py、README、install.sh、旧版登录页
    "admin123", "admin", "123456", "12345678", "password", "Aa123456",
]


def main():
    ap = argparse.ArgumentParser(description="重置 / 巡检管理员口令")
    ap.add_argument("--check", action="store_true", help="只巡检（只读，不改库）")
    ap.add_argument("--random", action="store_true", help="重置为随机强口令")
    ap.add_argument("--new", metavar="PASSWORD", help="重置为指定口令")
    ap.add_argument("--from-env", action="store_true", help="用 .env 里 KB_ADMIN_PASS 的值重置")
    ap.add_argument("--user", default=None, help="目标账号（默认 admin）")
    ap.add_argument("--root", default=None, help="部署根目录（默认自动推导）")
    args = ap.parse_args()

    root = _resolve_root(args.root)
    os.environ["KB_ROOT"] = root
    import admin  # 依赖 KB_ROOT，必须在其确定后再 import

    print("部署根  :", root)
    print("数据库  :", admin.DB_PATH)
    if not os.path.exists(admin.DB_PATH):
        print("[错误] 数据库不存在，请先启动一次服务以初始化")
        sys.exit(2)

    target = args.user or admin.DEFAULT_ADMIN_USER
    conn = admin._conn()
    row = conn.execute("SELECT * FROM users WHERE username=?", (target,)).fetchone()
    if not row:
        print("[错误] 账号「%s」不存在" % target)
        conn.close()
        sys.exit(2)
    print("目标账号: %s（%s）" % (target, row["display_name"] or "—"))

    # ---------- 巡检 ----------
    if args.check:
        hit = None
        for pwd in LEGACY_WEAK_PASSWORDS:
            if admin.verify_password(pwd, row["password_salt"], row["password_hash"]):
                hit = pwd
                break
        conn.close()
        if hit:
            print("\n[危险] 该账号仍在使用历史遗留/弱口令（口令字符串：%s）。" % hit)
            print("       该口令曾出现在受版本控制的源码或文档中，应视为已公开。")
            print("       请立即执行：python scripts/reset_admin_password.py --random")
            sys.exit(1)
        print("\n[通过] 未发现历史遗留/弱口令。")
        sys.exit(0)

    # ---------- 重置 ----------
    if args.from_env:
        new_pwd = admin._env_get("KB_ADMIN_PASS")
        if not new_pwd:
            print("[错误] .env 中未配置 KB_ADMIN_PASS")
            conn.close()
            sys.exit(2)
        source = ".env 的 KB_ADMIN_PASS"
    elif args.new:
        new_pwd = args.new
        source = "命令行指定"
        if len(new_pwd) < 8:
            print("[错误] 口令长度至少 8 位")
            conn.close()
            sys.exit(2)
    elif args.random:
        new_pwd = admin.generate_password()
        source = "随机生成"
    else:
        print("[错误] 请指定 --check / --random / --new <口令> / --from-env 之一")
        conn.close()
        sys.exit(2)

    salt, h = admin.hash_password(new_pwd)
    conn.execute("UPDATE users SET password_salt=?, password_hash=? WHERE id=?",
                 (salt, h, row["id"]))
    conn.commit()
    conn.close()

    print("\n[完成] 口令已重置（来源：%s）" % source)
    print("       账号: %s" % target)
    print("       新口令: %s" % new_pwd)
    if source == "随机生成":
        path = admin.INITIAL_PASS_FILE
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write("账号: %s\n口令: %s\n\n（请登录后立即修改密码，并删除本文件）\n"
                        % (target, new_pwd))
            print("       已写入: %s" % path)
        except OSError as e:
            print("       [警告] 口令文件写入失败: %s" % e)
    print("\n提示：改完请重启/确认服务在线，并用新口令登录验证。")


if __name__ == "__main__":
    main()
