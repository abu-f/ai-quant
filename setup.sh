#!/usr/bin/env bash
# aquant 一键安装脚本（Ubuntu/Debian WSL）
# 只安装 Python 依赖，不改动系统软件包，不需要 sudo。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="${AQUANT_VENV:-$HOME/.venvs/aquant}"
MIRROR="${AQUANT_PIP_MIRROR:-https://mirrors.aliyun.com/pypi/simple/}"

echo "==> 项目目录：$ROOT"
echo "==> 虚拟环境：$VENV"

if ! command -v python3 >/dev/null 2>&1; then
  echo "未找到 python3，请先执行：sudo apt install -y python3 python3-venv" >&2
  exit 1
fi
echo "==> Python：$(python3 --version)"

# 1) 创建虚拟环境（Ubuntu 精简镜像可能缺 ensurepip，这里手动引导 pip）
if [ ! -x "$VENV/bin/python" ]; then
  mkdir -p "$(dirname "$VENV")"
  python3 -m venv --without-pip "$VENV"
fi
PY="$VENV/bin/python"

if ! "$PY" -m pip --version >/dev/null 2>&1; then
  echo "==> 引导 pip"
  tmp="$(mktemp -d)"
  curl -fsSL https://bootstrap.pypa.io/get-pip.py -o "$tmp/get-pip.py"
  "$PY" "$tmp/get-pip.py" -i "$MIRROR" --quiet
  rm -rf "$tmp"
fi

# 2) 安装依赖
echo "==> 安装依赖（镜像：$MIRROR）"
"$PY" -m pip install --upgrade pip -i "$MIRROR" --quiet || true
"$PY" -m pip install -r "$ROOT/requirements.txt" -i "$MIRROR"

# 3) 初始化目录与数据库
chmod +x "$ROOT/quant" 2>/dev/null || true
echo "==> 初始化"
cd "$ROOT"
"$ROOT/quant" init

cat <<'EOF'

安装完成。常用命令：
  ./quant doctor                     环境自检
  ./quant sync                       同步自选股日线
  ./quant quote                      看自选股实时行情
  ./quant ta 600519                  技术指标
  ./quant fund 600519                基本面
  ./quant screen --preset value      选股
  ./quant alert check                预警检查
  ./quant backtest 600519 --strategy ma_cross
  ./quant web                        本地可视化（浏览器打开 http://127.0.0.1:8770）
EOF
