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
cat > /etc/systemd/system/kb.service <<EOF
[Unit]
Description=Local Knowledge Base Service (RAG + 3D Graph)
After=network.target

[Service]
Type=simple
User=${KB_USER}
WorkingDirectory=${KB_ROOT}
Environment=KB_ROOT=${KB_ROOT}
Environment=KB_API_HOST=0.0.0.0
Environment=KB_API_PORT=8080
ExecStart=${KB_ROOT}/venv/bin/python ${KB_ROOT}/scripts/serve.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable kb.service
systemctl restart kb.service

# 目录属主交还运行用户（保证服务可读写）
chown -R "${KB_USER}:${KB_USER}" "${KB_ROOT}"

echo ""
echo "============================================================"
echo " ✅ 安装完成！"
echo "    管理门户首页:    http://<本机IP>:8080/        (默认管理员 admin / Admin@123)"
echo "    3D 知识图谱:     http://<本机IP>:8080/graph"
echo "    检索 API:        http://<本机IP>:8080/api/query?q=关键词"
echo "    健康检查:        http://<本机IP>:8080/api/health"
echo ""
echo "  ⚠️ 请登录后立即修改默认管理员密码（用户管理 -> 编辑）。"
echo ""
echo " 常用操作:"
echo "    sudo systemctl status kb      # 查看状态"
echo "    sudo systemctl restart kb     # 重启"
echo "    sudo journalctl -u kb -f      # 查看日志"
echo "============================================================"
