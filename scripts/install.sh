#!/usr/bin/env bash
###############################################################################
# 本地知识库一键安装脚本（Ubuntu 22.04 / 24.04）
# 以 root 运行（由 deploy.py 通过 sudo -S 调用）；最终服务以 KB_USER 运行。
#
# 功能:
#   1. 安装系统依赖 (python3-venv)
#   2. 创建虚拟环境并安装 Python 依赖
#   3. 重建 BM25 索引（避免跨平台 pickle 兼容问题）
#   4. 生成 3D 图谱数据（如缺失）
#   5. 注册 systemd 服务，开机自启 + 崩溃重启
#
# 用法:
#   sudo bash /opt/OA-ai/scripts/install.sh
###############################################################################
set -euo pipefail

KB_ROOT="${KB_ROOT:-/opt/OA-ai}"
KB_USER="${KB_USER:-yanl}"
PYTHON_BIN="$(command -v python3 || true)"

if [ -z "$PYTHON_BIN" ]; then
  echo "[错误] 未检测到 python3，请先安装。" >&2
  exit 1
fi

echo "==> 本地知识库部署: ${KB_ROOT}  (运行用户: ${KB_USER})"

# 1. 系统依赖
echo "==> [1/5] 安装系统依赖..."
apt-get update -y
apt-get install -y python3-venv python3-pip

# 2. 虚拟环境
echo "==> [2/5] 创建虚拟环境 ${KB_ROOT}/venv ..."
rm -rf "${KB_ROOT}/venv"
"$PYTHON_BIN" -m venv "${KB_ROOT}/venv"
# shellcheck disable=SC1091
source "${KB_ROOT}/venv/bin/activate"
pip install --upgrade pip -q
pip install -r "${KB_ROOT}/requirements.txt" -q

# 3. 重建索引
echo "==> [3/5] 构建 BM25 索引..."
cd "${KB_ROOT}"
KB_ROOT="${KB_ROOT}" python app/rag_build_index.py

# 4. 生成图谱数据（缺失时）
if [ ! -f "${KB_ROOT}/knowledge_base/knowledge_graph_data.js" ]; then
  echo "==> [4/5] 生成 3D 图谱数据..."
  KB_ROOT="${KB_ROOT}" python app/generate_data.py
else
  echo "==> [4/5] 图谱数据已存在，跳过。"
fi

# 5. systemd 服务
echo "==> [5/5] 注册 systemd 服务 kb.service ..."
# 注意: 这里【不写】Environment=KB_ROOT / KB_API_HOST / KB_API_PORT。
# 端口与根目录统一由 ${KB_ROOT}/.env 管理(serve.py 以 override=True 加载 .env),
# 若在此处再写一份, 会出现两个配置源互相打架、改一处漏一处的问题。
cat > /etc/systemd/system/kb.service <<EOF
[Unit]
Description=Local Knowledge Base Service (RAG + 3D Graph)
After=network.target

# 崩溃重启限流: 防止程序反复崩溃时被 systemd 无限重启,
# 300 秒内最多重启 5 次, 超出则进入 failed 等待人工介入。
StartLimitIntervalSec=300
StartLimitBurst=5

[Service]
Type=simple
User=${KB_USER}
WorkingDirectory=${KB_ROOT}
ExecStart=${KB_ROOT}/venv/bin/python ${KB_ROOT}/scripts/serve.py

# 仅异常退出时重启(正常退出不拉起; systemctl stop 也不触发)
Restart=on-failure
RestartSec=5
TimeoutStopSec=30
KillSignal=SIGTERM

# 资源上限: 防止内存泄漏拖垮整机
MemoryMax=2G

StandardOutput=journal
StandardError=journal
SyslogIdentifier=kb

# 基础安全加固(/opt 不受 ProtectSystem 影响, 服务可正常读写数据)
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=full

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable kb.service
systemctl restart kb.service

# 目录属主交还运行用户（保证服务可读写）
chown -R "${KB_USER}:${KB_USER}" "${KB_ROOT}"

echo ""
# 端口从 .env 读取(与 serve.py 口径一致), 读不到则提示默认值。
# 【注意】务必写成单行: 本文件经 SFTP 从 Windows 直传后可能带 CRLF 换行,
# 若用反斜杠续行, `\` 后面跟的是 \r\n 而非 \n, bash 会报语法错误。
KB_PORT="$(grep -E '^[[:space:]]*KB_API_PORT=' "${KB_ROOT}/.env" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '[:space:]')"
KB_PORT="${KB_PORT:-8080}"

echo "============================================================"
echo " ✅ 安装完成！"
echo "    管理门户首页:    http://<本机IP>:${KB_PORT}/        (默认管理员 admin / Admin@123)"
echo "    3D 知识图谱:     http://<本机IP>:${KB_PORT}/graph"
echo "    检索 API:        http://<本机IP>:${KB_PORT}/api/query?q=关键词"
echo "    健康检查:        http://<本机IP>:${KB_PORT}/api/health"
echo ""
echo "   端口配置: 改 ${KB_ROOT}/.env 的 KB_API_PORT 后 systemctl restart kb"
echo ""
echo "  ⚠️ 请登录后立即修改默认管理员密码（用户管理 -> 编辑）。"
echo ""
echo " 常用操作:"
echo "    sudo systemctl status kb      # 查看状态"
echo "    sudo systemctl restart kb     # 重启"
echo "    sudo journalctl -u kb -f      # 查看日志"
echo "============================================================"
