#!/usr/bin/env python3
"""部署脚本公共配置：从 .env 读取服务器连接凭据。

【为什么要有这个模块】
此前 HOST / USER / PASSWORD 以明文硬编码在 deploy*.py 里，而这些脚本被 git
跟踪并推送到了【公开仓库】，等于把服务器 IP、用户名、密码暴露在公网。

现在统一改为从 .env 读取，且 .env 已被 .gitignore 排除，永不入库。

【设计要点】
1. 手写解析而非依赖 python-dotenv：部署脚本常在任意机器上运行，
   不假设目标环境已装该依赖。
2. 缺配置时【直接报错退出】，绝不用默认值兜底 —— 宁可让部署失败，
   也不能悄悄连到错误的机器或用错误凭据。
3. 本模块自身不含任何凭据，可安全入库。
"""
import io
import os
import sys
from pathlib import Path

# .env 位于项目根目录（与本模块同级的上级，即 kb_deploy/.env）
_ENV_PATH = Path(__file__).resolve().parent / ".env"

# 需要的连接配置项 -> .env 中的键名
_REQUIRED = {
    "host": "SSH_HOST",
    "user": "SSH_USER",
    "password": "SSH_PASSWORD",
}


def _parse_env(path):
    """解析 .env 为 dict。支持 KEY=VALUE、注释、空行、export 前缀。

    值两侧的同引号会被剥离；不做变量展开（本项目 .env 无此需求）。
    """
    data = {}
    if not path.exists():
        return data
    for raw in io.open(str(path), encoding="utf-8").read().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip()
        # 剥离成对引号
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        if key:
            data[key] = val
    return data


def load_ssh_config(env_path=None):
    """读取 SSH 连接配置，返回 (host, user, password)。

    优先使用 .env；若 .env 缺失或某项为空，回退到环境变量
    （便于 CI 等不便放文件的场景）。

    Raises:
        SystemExit: 必需配置缺失时打印明确指引并退出，不使用默认值。
    """
    data = _parse_env(Path(env_path) if env_path else _ENV_PATH)

    values = {}
    missing = []
    for name, key in _REQUIRED.items():
        # .env 优先，其次环境变量
        val = data.get(key) or os.environ.get(key) or ""
        if not val.strip():
            missing.append(key)
        values[name] = val.strip()

    if missing:
        sys.stderr.write(
            "\n[错误] 缺少服务器连接配置: %s\n\n"
            "请在项目根目录的 .env 中配置（该文件已被 .gitignore 排除，不会入库）：\n\n"
            "    SSH_HOST=192.168.30.155\n"
            "    SSH_USER=yanl\n"
            "    SSH_PASSWORD=你的密码\n\n"
            "或用环境变量提供后重试。\n"
            % ", ".join(missing)
        )
        sys.exit(1)

    return values["host"], values["user"], values["password"]


def env_path():
    """返回 .env 的路径（便于脚本报错时提示）。"""
    return _ENV_PATH


if __name__ == "__main__":
    # 自检：只打印主机与用户名，密码以 *** 代替，避免回显泄露
    h, u, _p = load_ssh_config()
    print("配置文件: %s" % _ENV_PATH)
    print("SSH_HOST : %s" % h)
    print("SSH_USER : %s" % u)
    print("SSH_PASSWORD: %s" % ("*" * 8))
