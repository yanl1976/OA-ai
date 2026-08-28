#!/usr/bin/env bash
###############################################################################
# 知识库服务：一行命令启动 + 进程守护（崩溃自动重启）
#
# 用法（一条命令即可）：
#   bash scripts/start.sh              # 启动（自带守护，崩溃自动重启）
#   bash scripts/start.sh stop         # 停止
#   bash scripts/start.sh restart      # 重启
#   bash scripts/start.sh status       # 查看状态
#   bash scripts/start.sh logs         # 实时跟踪日志
#
# 两种运行模式（二选一，绝不重复守护）：
#   1) systemd 模式（默认，若本机已装 kb.service）：
#      委托给 systemctl。systemd 本身就有崩溃重启 + 开机自启，最省心。
#   2) standalone 模式（--standalone，或本机无 kb.service 时自动使用）：
#      本脚本自带守护循环，后台常驻，检测到应用退出即自动拉起。
#      适合容器、无 systemd 的环境，或临时手动运行。
#
# 崩溃重启限流（重要）：
#   连续快速崩溃时不会无限重启。窗口内崩溃达上限即停止并保留现场，
#   便于排查，避免刷爆日志与空转（历史上曾因此重启过上万次）。
###############################################################################

# 不使用 set -e：守护循环需要容忍应用非零退出
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KB_ROOT="${KB_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"

# ---------- 可配置项（也可用环境变量覆盖） ----------
SERVICE_NAME="${SERVICE_NAME:-kb}"
APP_NAME="${APP_NAME:-kb}"
VENV_PY="$KB_ROOT/venv/bin/python"
LOG_DIR="${KB_DIR_LOG:-$KB_ROOT/logs}"
PID_DIR="${KB_DIR_PID:-$KB_ROOT/run}"
APP_LOG="$LOG_DIR/${APP_NAME}.log"
GUARD_LOG="$LOG_DIR/${APP_NAME}-guardian.log"
APP_PID_FILE="$PID_DIR/${APP_NAME}.pid"
GUARD_PID_FILE="$PID_DIR/${APP_NAME}-guardian.pid"

# 守护行为
RESTART_ALWAYS="${RESTART_ALWAYS:-yes}"   # yes=正常退出也重启；no=仅异常退出才重启
RESTART_DELAY="${RESTART_DELAY:-5}"       # 每次重启前等待秒数
MAX_CRASH="${MAX_CRASH:-5}"               # 限流窗口内允许的崩溃次数
CRASH_WINDOW="${CRASH_WINDOW:-300}"       # 限流窗口（秒）；稳定运行超过它则计数清零
MAX_LOG_SIZE="${MAX_LOG_SIZE:-10485760}"  # 单日志文件上限（默认 10MB）

# ---------- 基础函数 ----------
_ts() { date '+%Y-%m-%d %H:%M:%S'; }

_log() {
  # $1=标记(G=守护/A=应用) 其余=内容
  local tag="$1"; shift
  printf '[%s] [%s] %s\n' "$(_ts)" "$tag" "$*"
}

_guard_log() { _log "G" "$@" >> "$GUARD_LOG" 2>&1; }

_rotate() {
  # 日志超过上限则轮转，保留一份旧文件，避免无限增长
  local f="$1"
  [ -f "$f" ] || return 0
  local sz
  sz=$(stat -c%s "$f" 2>/dev/null || echo 0)
  if [ "${sz:-0}" -gt "$MAX_LOG_SIZE" ]; then
    mv "$f" "$f.1" 2>/dev/null || true
    : > "$f" 2>/dev/null || true
  fi
}

_is_alive() {
  local pf="$1" pid
  [ -f "$pf" ] || return 1
  pid=$(cat "$pf" 2>/dev/null || echo "")
  [ -n "${pid:-}" ] || return 1
  kill -0 "$pid" 2>/dev/null
}

_pid_of() { cat "$1" 2>/dev/null || echo ""; }

_uptime_of() {
  local pid="$1"
  [ -n "${pid:-}" ] || { echo "-"; return; }
  ps -o etime= -p "$pid" 2>/dev/null | tr -d ' ' || echo "-"
}

_pick_python() {
  if [ -x "$VENV_PY" ]; then echo "$VENV_PY"; return; fi
  command -v python3 || command -v python || echo ""
}

_start_app() {
  local py
  py="$(_pick_python)"
  if [ -z "$py" ]; then
    _guard_log "未找到 python（venv 与系统 python 均不可用），无法启动"
    return 1
  fi
  _rotate "$APP_LOG"
  # shellcheck disable=SC2093
  KB_ROOT="$KB_ROOT" exec "$py" "$KB_ROOT/scripts/serve.py"
}

# ---------- systemd 模式判定 ----------
_have_systemd_unit() {
  command -v systemctl >/dev/null 2>&1 || return 1
  systemctl list-unit-files "${SERVICE_NAME}.service" >/dev/null 2>&1
}

_can_control_systemd() {
  # 是否真能操作 systemd 服务。
  #
  # 【坑】systemctl start/stop 需要 root 权限。非 root 用户即使能查到 unit，
  # 也可能无权启停（sudo 若需密码且无 tty，会报
  # "a terminal is required to read the password" 并失败）。
  # 这里用「能否免密执行一条只读命令」来判定，避免脚本走进必然失败的分支。
  local out
  if [ "$(id -u)" -eq 0 ]; then
    systemctl is-active "$SERVICE_NAME" >/dev/null 2>&1
    return $?
  fi
  out=$(sudo -n systemctl is-active "$SERVICE_NAME" 2>&1)
  case "$out" in
    *"a terminal is required"*|*"a password is required"*|*"not allowed"*)
      return 1 ;;
    *"inactive"*|*"active"*|*"failed"*|*"activating"*|*"unknown"*)
      return 0 ;;
    *)
      return 1 ;;
  esac
}

_use_systemd() {
  # 强制 standalone 时不走 systemd；
  # 否则仅当「unit 存在」且「当前用户确实能控制它」时才委托 systemd。
  [ "${FORCE_STANDALONE:-no}" = "yes" ] && return 1
  _have_systemd_unit || return 1
  _can_control_systemd
}

# ---------- systemd 模式操作 ----------
_sys() {
  # root 直接执行；非 root 加 sudo -n（免密方式，失败由调用方处理）
  if [ "$(id -u)" -eq 0 ]; then
    systemctl "$@"
  else
    sudo -n systemctl "$@"
  fi
}

# ---------- standalone 守护核心 ----------
_guardian_shutdown() {
  _guard_log "收到停止信号，正在关闭"
  if [ -n "${APP_PID:-}" ]; then
    kill -TERM "$APP_PID" 2>/dev/null || true
    # 等待最多 30 秒优雅退出
    local waited=0
    while kill -0 "$APP_PID" 2>/dev/null && [ "$waited" -lt 30 ]; do
      sleep 1; waited=$((waited + 1))
    done
    kill -KILL "$APP_PID" 2>/dev/null || true
  fi
  rm -f "$APP_PID_FILE" "$GUARD_PID_FILE" 2>/dev/null || true
  _guard_log "已停止"
  exit 0
}

__guardian() {
  mkdir -p "$LOG_DIR" "$PID_DIR" 2>/dev/null || true
  echo "$$" > "$GUARD_PID_FILE"
  trap _guardian_shutdown TERM INT

  local crash_count=0 last_crash=0
  _guard_log "守护进程启动 (pid=$$, 应用=$APP_NAME, 延迟=${RESTART_DELAY}s, 限流=${MAX_CRASH}次/${CRASH_WINDOW}s)"

  while true; do
    # 每条日志都带时间戳，便于事后统计重启次数
    echo "[$(_ts)] [A] ---- 应用启动 ----" >> "$APP_LOG" 2>&1
    _start_app >> "$APP_LOG" 2>&1 &
    APP_PID=$!
    echo "$APP_PID" > "$APP_PID_FILE"
    _guard_log "应用已启动 (pid=$APP_PID)"

    # 记录启动时刻，用于识别「刚启动就退出」（典型：端口被占用，code=1）
    local started_at
    started_at=$(date +%s)

    wait "$APP_PID"
    local rc=$?
    local lived=$(( $(date +%s) - started_at ))
    rm -f "$APP_PID_FILE" 2>/dev/null || true
    _guard_log "应用退出 (pid=$APP_PID, code=$rc, 存活 ${lived}s)"

    # 正常退出且未配置强制重启 -> 不再拉起
    if [ "$rc" -eq 0 ] && [ "$RESTART_ALWAYS" != "yes" ]; then
      _guard_log "应用正常退出(0)，按配置不重启"
      break
    fi

    # 崩溃限流：窗口内崩溃过多则停止，避免无限重启风暴
    local now
    now=$(date +%s)
    if [ "$last_crash" -gt 0 ] && [ $((now - last_crash)) -gt "$CRASH_WINDOW" ]; then
      crash_count=0
      _guard_log "距上次崩溃已超过 ${CRASH_WINDOW}s，崩溃计数清零"
    fi
    crash_count=$((crash_count + 1))
    last_crash=$now

    if [ "$crash_count" -ge "$MAX_CRASH" ]; then
      _guard_log "窗口内连续崩溃 ${crash_count} 次，达到上限 ${MAX_CRASH}，停止自动重启（需人工排查）"
      _guard_log "常见原因：端口被占用、配置错误、依赖缺失。请查看 $APP_LOG"
      break
    fi

    # 刚启动就退出（存活 < 3 秒）通常是端口冲突等硬错误，
    # 退避时间翻倍，避免高频空转刷日志
    local delay="$RESTART_DELAY"
    if [ "$lived" -lt 3 ]; then
      delay=$(( RESTART_DELAY * crash_count ))
      [ "$delay" -gt 60 ] && delay=60
      _guard_log "应用存活不足 3s（疑似端口冲突等硬错误），延长退避至 ${delay}s"
    fi

    _guard_log "${delay}s 后重启 (崩溃计数 ${crash_count}/${MAX_CRASH})"
    sleep "$delay"
  done

  rm -f "$GUARD_PID_FILE" 2>/dev/null || true
  _guard_log "守护进程退出"
}

_standalone_start() {
  if _is_alive "$GUARD_PID_FILE"; then
    _log A "服务已在运行 (守护 pid=$(_pid_of "$GUARD_PID_FILE"))，无需重复启动"
    _log A "如需重启请执行: bash $0 restart"
    return 0
  fi

  # 【防双重守护·必须成功否则中止】
  # 若 systemd 已在运行同一服务，必须先把它停掉再启动脚本守护。
  #
  # 【踩过的坑】曾在这里只打印警告就继续启动，结果 systemd 与脚本同时拉起
  # 应用：端口被 systemd 那份占着，脚本启动的应用立刻以 code=1 退出，
  # 守护又马上重启，形成崩溃风暴，最终留下 4 个 serve.py 进程。
  # 因此：停不掉就【直接中止】，绝不带冲突启动。
  if _have_systemd_unit; then
    local st
    st=$(systemctl is-active "$SERVICE_NAME" 2>/dev/null || echo unknown)
    if [ "$st" = "active" ] || [ "$st" = "activating" ]; then
      if ! _sys stop "$SERVICE_NAME" 2>/dev/null; then
        _log A "错误: systemd 服务 ${SERVICE_NAME} 正在运行，且当前用户无权停止它"
        _log A "      若强行以 standalone 启动，会造成双重守护与端口冲突，故已中止。"
        _log A "      请选择其一："
        _log A "        (1) 用 systemd 管理: sudo systemctl restart $SERVICE_NAME"
        _log A "        (2) 先交给 systemd 停止: sudo systemctl stop $SERVICE_NAME"
        _log A "           再执行: bash $0 --standalone"
        return 1
      fi
      _log A "已停止 systemd 服务 ${SERVICE_NAME}，改由脚本守护接管（避免双重拉起）"
      # 等待端口释放
      local waited=0
      while [ "$waited" -lt 15 ]; do
        if ! systemctl is-active "$SERVICE_NAME" 2>/dev/null | grep -q '^active$'; then
          break
        fi
        sleep 1; waited=$((waited + 1))
      done
    fi
  fi

  mkdir -p "$LOG_DIR" "$PID_DIR" 2>/dev/null || true
  # setsid 脱离终端，nohup 免疫挂断，保证 start 命令返回后守护仍在后台
  setsid nohup bash "$0" __guardian > /dev/null 2>&1 < /dev/null &
  disown 2>/dev/null || true
  sleep 2
  if _is_alive "$GUARD_PID_FILE"; then
    _log A "已启动 [standalone 守护模式]"
    _log A "  守护进程 pid : $(_pid_of "$GUARD_PID_FILE")"
    _log A "  应用进程 pid : $(_pid_of "$APP_PID_FILE")"
    _log A "  守护日志     : $GUARD_LOG"
    _log A "  应用日志     : $APP_LOG"
    _log A "崩溃将自动重启：延迟 ${RESTART_DELAY}s，限流 ${MAX_CRASH} 次 / ${CRASH_WINDOW}s"
  else
    _log A "启动失败，请查看: $GUARD_LOG"
    return 1
  fi
}

_standalone_stop() {
  local gpid apid
  gpid=$(_pid_of "$GUARD_PID_FILE")
  apid=$(_pid_of "$APP_PID_FILE")
  if [ -n "${gpid:-}" ] && kill -0 "$gpid" 2>/dev/null; then
    _log A "停止守护进程 (pid=$gpid)"
    kill -TERM "$gpid" 2>/dev/null || true
    local waited=0
    while kill -0 "$gpid" 2>/dev/null && [ "$waited" -lt 35 ]; do
      sleep 1; waited=$((waited + 1))
    done
    kill -KILL "$gpid" 2>/dev/null || true
  fi
  if [ -n "${apid:-}" ] && kill -0 "$apid" 2>/dev/null; then
    _log A "停止残留应用进程 (pid=$apid)"
    kill -TERM "$apid" 2>/dev/null || true
    sleep 2
    kill -KILL "$apid" 2>/dev/null || true
  fi
  rm -f "$GUARD_PID_FILE" "$APP_PID_FILE" 2>/dev/null || true
  _log A "已停止 [standalone 守护模式]"
}

_status_standalone() {
  local gpid apid
  gpid=$(_pid_of "$GUARD_PID_FILE")
  apid=$(_pid_of "$APP_PID_FILE")
  echo "模式      : standalone（脚本自带守护）"
  if [ -n "${gpid:-}" ] && kill -0 "$gpid" 2>/dev/null; then
    echo "守护进程  : 运行中 pid=$gpid  已运行 $(_uptime_of "$gpid")"
  else
    echo "守护进程  : 未运行"
  fi
  if [ -n "${apid:-}" ] && kill -0 "$apid" 2>/dev/null; then
    echo "应用进程  : 运行中 pid=$apid  已运行 $(_uptime_of "$apid")"
  else
    echo "应用进程  : 未运行"
  fi
  if [ -f "$GUARD_LOG" ]; then
    local n
    n=$(grep -c '应用已启动' "$GUARD_LOG" 2>/dev/null || echo 0)
    echo "累计启动  : ${n} 次"
    echo "最近守护日志:"
    tail -n 5 "$GUARD_LOG" 2>/dev/null | sed 's/^/    /'
  fi
}

# ---------- 命令分发 ----------
_usage() {
  cat <<'USAGE'
用法:
  bash scripts/start.sh               启动（自带守护，崩溃自动重启）
  bash scripts/start.sh stop          停止
  bash scripts/start.sh restart       重启
  bash scripts/start.sh status        查看状态
  bash scripts/start.sh logs          实时跟踪应用日志

选项:
  --standalone    强制使用脚本自带守护（不委托 systemd）
  -h, --help      显示本帮助

环境变量（可选覆盖）:
  RESTART_DELAY   重启延迟秒数（默认 5）
  MAX_CRASH       限流窗口内最大崩溃次数（默认 5）
  CRASH_WINDOW    限流窗口秒数（默认 300）
USAGE
}

cmd_start() {
  if _use_systemd; then
    _log A "使用 systemd 模式启动 ${SERVICE_NAME}.service"
    if _sys start "$SERVICE_NAME" 2>/dev/null; then
      sleep 2
      _sys is-active "$SERVICE_NAME" 2>/dev/null
      _log A "已启动 [systemd 模式]（systemd 自带崩溃重启与开机自启）"
      _log A "  查看状态: bash $0 status"
      _log A "  实时日志: bash $0 logs"
      return 0
    fi
    # 启动失败（多为权限不足）时降级为脚本守护，保证一行命令总能起服务
    _log A "systemd 启动失败，自动降级为脚本自带守护"
  fi
  _standalone_start
}

cmd_stop() {
  if _use_systemd; then
    _sys stop "$SERVICE_NAME"
    _log A "已停止 [systemd 模式]"
  else
    _standalone_stop
  fi
}

cmd_restart() {
  if _use_systemd; then
    _sys restart "$SERVICE_NAME"
    _log A "已重启 [systemd 模式]"
  else
    _standalone_stop
    sleep 1
    _standalone_start
  fi
}

cmd_status() {
  if _use_systemd; then
    echo "模式      : systemd（委托 systemctl，自带崩溃重启）"
    _sys status "$SERVICE_NAME" --no-pager 2>&1 | head -12
  else
    _status_standalone
  fi
}

cmd_logs() {
  if _use_systemd; then
    _sys_dash() {
      if [ "$(id -u)" -eq 0 ]; then
        journalctl -u "$SERVICE_NAME" -f
      else
        sudo journalctl -u "$SERVICE_NAME" -f
      fi
    }
    _sys_dash
  else
    _log A "跟踪应用日志: $APP_LOG （Ctrl+C 退出）"
    tail -f "$APP_LOG"
  fi
}

main() {
  # 隐藏的内部子命令：真正执行守护循环（由 _standalone_start 调用）
  if [ "${1:-}" = "__guardian" ]; then
    __guardian
    return 0
  fi

  # 解析选项
  while [ $# -gt 0 ]; do
    case "$1" in
      --standalone) FORCE_STANDALONE=yes; shift ;;
      -h|--help)    _usage; return 0 ;;
      start|stop|restart|status|logs) break ;;
      *) echo "未知参数: $1"; _usage; return 1 ;;
    esac
  done

  local cmd="${1:-start}"
  case "$cmd" in
    start)   cmd_start ;;
    stop)    cmd_stop ;;
    restart) cmd_restart ;;
    status)  cmd_status ;;
    logs)    cmd_logs ;;
    *)       _usage; return 1 ;;
  esac
}

main "$@"
