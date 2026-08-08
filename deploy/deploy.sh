#!/usr/bin/env bash
# 智批π Demo 一键部署 / 更新（Linux 服务器，如阿里云轻量应用服务器）
#
#   git clone <仓库> && cd zhipi-submission/deploy
#   cp .env.example .env && vi .env      # 至少填 ZHIPI_ACCESS_CODE
#   bash deploy.sh
#
# 做四件事：环境自检 → 构建镜像 → 起容器 → 冒烟测试（探针 + 口令闸 + 四屏接口）。
# 冒烟没过就以非零码退出，不会留下一个「看起来起来了、其实点不开」的服务。

set -euo pipefail

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd .. && pwd)"
SERVICE="zhipi-demo"

c_red()  { printf '\033[31m%s\033[0m\n' "$*"; }
c_grn()  { printf '\033[32m%s\033[0m\n' "$*"; }
c_ylw()  { printf '\033[33m%s\033[0m\n' "$*"; }
step()   { printf '\n\033[1m[%s]\033[0m %s\n' "$1" "$2"; }
die()    { c_red "✗ $*"; exit 1; }

# ---------------------------------------------------------------- 1. 环境自检
step 1/5 "环境自检"

command -v docker >/dev/null 2>&1 || die "未找到 docker。Ubuntu/Debian 可执行：curl -fsSL https://get.docker.com | sh"
docker info >/dev/null 2>&1 || die "docker 守护进程没在跑，或当前用户不在 docker 组。试：sudo systemctl start docker；sudo usermod -aG docker \$USER 后重新登录"

if docker compose version >/dev/null 2>&1; then
  DC="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
  DC="docker-compose"
else
  die "未找到 docker compose 插件。Ubuntu/Debian：sudo apt-get install -y docker-compose-plugin"
fi
echo "  docker      $(docker version --format '{{.Server.Version}}' 2>/dev/null || echo '?')"
echo "  compose     $($DC version --short 2>/dev/null || echo '?')"

[ -f .env ] || die ".env 不存在。先执行：cp .env.example .env 然后填写（至少 ZHIPI_ACCESS_CODE）"

# 口令是公网部署的第一道闸，缺了要显式警告——但不阻断，
# 内网试跑或临时演示确实可能不需要。
# 末尾的 \r 要剥掉：.env 常被 Windows 编辑器改过，带 CR 会让冒烟测试
# 拿着「口令\r」去请求，明明配对了却判成错误口令。
CODE_LINE="$(grep -E '^ZHIPI_ACCESS_CODE=' .env | head -n1 | tr -d '\r' || true)"
if [ -z "${CODE_LINE#ZHIPI_ACCESS_CODE=}" ]; then
  c_ylw "  ⚠ ZHIPI_ACCESS_CODE 为空：任何拿到链接的人都能直接打开。"
  c_ylw "    公网部署强烈建议设置。继续请按回车，中止按 Ctrl+C。"
  read -r _ || true
else
  echo "  访问口令    已设置"
fi

# 未配 VLM Key 却设了日配额是无害的；反过来（配了 Key 不设配额）才危险。
if grep -qE '^ZHIPI_VLM_API_KEY=.+' .env && ! grep -qE '^ZHIPI_VLM_DAILY_LIMIT=[1-9]' .env; then
  c_ylw "  ⚠ 配了 ZHIPI_VLM_API_KEY 但 ZHIPI_VLM_DAILY_LIMIT 未设或为 0："
  c_ylw "    公网链接被脚本刷一晚上就能把 API 余额打空。建议设一个可承受的数字。"
fi

PUBLIC_PORT="$(grep -E '^ZHIPI_PUBLIC_PORT=' .env | head -n1 | cut -d= -f2 | tr -d '\r' || true)"
PUBLIC_PORT="${PUBLIC_PORT:-8010}"
echo "  对外端口    ${PUBLIC_PORT}"

# ---------------------------------------------------------------- 2. 构建
step 2/5 "构建镜像（首次约 3-5 分钟，之后走缓存）"
$DC build

# ---------------------------------------------------------------- 3. 启动
step 3/5 "启动容器"
$DC up -d
$DC ps

# ---------------------------------------------------------------- 4. 等健康
step 4/5 "等待健康检查通过"
BASE="http://127.0.0.1:${PUBLIC_PORT}"
for i in $(seq 1 30); do
  if curl -fsS -m 3 "${BASE}/healthz" >/dev/null 2>&1; then
    c_grn "  ✓ 探针通过（${i}s）"
    break
  fi
  sleep 1
  [ "$i" = 30 ] && { $DC logs --tail 40; die "30 秒内探针未通过，日志见上"; }
done

# ---------------------------------------------------------------- 5. 冒烟
step 5/5 "冒烟测试"
FAIL=0
chk() { # chk 描述 期望码 实际码
  if [ "$2" = "$3" ]; then printf '  ✓ %-34s %s\n' "$1" "$3"
  else printf '  ✗ %-34s 期望 %s，实际 %s\n' "$1" "$2" "$3"; FAIL=1; fi
}
code() { curl -s -o /dev/null -m 8 -w '%{http_code}' "$@"; }

chk "/healthz 免口令" 200 "$(code "${BASE}/healthz")"

CODE_VAL="${CODE_LINE#ZHIPI_ACCESS_CODE=}"
JAR="$(mktemp)"; trap 'rm -f "$JAR"' EXIT

if [ -n "$CODE_VAL" ]; then
  chk "无口令访问 /api 应拒绝" 401 "$(code "${BASE}/api/demo/config")"
  chk "错误口令应拒绝"        401 "$(code "${BASE}/?code=__wrong__")"
  chk "正确口令 303 去明文"   303 "$(code -c "$JAR" "${BASE}/?code=${CODE_VAL}")"
else
  chk "无口令模式首页可达" 200 "$(code -c "$JAR" "${BASE}/")"
fi

chk "首页"             200 "$(code -b "$JAR" "${BASE}/")"
chk "运行配置"         200 "$(code -b "$JAR" "${BASE}/api/demo/config")"
chk "样例图库"         200 "$(code -b "$JAR" "${BASE}/api/sample-images")"
chk "教师工作台数据"   200 "$(code -b "$JAR" "${BASE}/api/teacher/results")"
chk "班级看板数据"     200 "$(code -b "$JAR" "${BASE}/api/analytics/class")"
chk "静态样式"         200 "$(code -b "$JAR" "${BASE}/static/css/app.css")"
chk "静态脚本"         200 "$(code -b "$JAR" "${BASE}/static/js/app.js")"

echo
if [ "$FAIL" = 0 ]; then
  MODE="$(curl -fsS -m 5 "${BASE}/healthz" | grep -o '"mode":"[a-z]*"' | cut -d'"' -f4 || echo '?')"
  c_grn "════════════════════════════════════════════════════════"
  c_grn " 部署成功 · 运行模式：${MODE}"
  c_grn "════════════════════════════════════════════════════════"
  IP="$(curl -fsS -m 5 https://api.ipify.org 2>/dev/null || echo '<服务器公网IP>')"
  echo " 本机自测：  ${BASE}/"
  if [ -n "$CODE_VAL" ]; then
    echo " 对外分享：  http://${IP}:${PUBLIC_PORT}/?code=${CODE_VAL}"
    echo "             （服务端校验后会跳转去掉明文口令）"
  else
    echo " 对外分享：  http://${IP}:${PUBLIC_PORT}/"
  fi
  echo
  c_ylw " 别忘了在云控制台安全组放行 TCP ${PUBLIC_PORT}，否则外网连不上。"
  echo " 看日志：    cd deploy && ${DC} logs -f"
  echo " 停服务：    cd deploy && ${DC} down"
  echo " 更新代码：  git pull && cd deploy && bash deploy.sh"
else
  c_red "════════════════════════════════════════════════════════"
  c_red " 冒烟测试未全部通过，服务可能不可用。最近日志："
  c_red "════════════════════════════════════════════════════════"
  $DC logs --tail 40
  exit 1
fi
