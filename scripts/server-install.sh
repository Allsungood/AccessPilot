#!/usr/bin/env bash
# AccessPilot 服务端一键部署脚本 (Xray VLESS-Vision-Reality)
#
# 适用: Debian 10+ / Ubuntu 20.04+ 的全新 VPS, 以 root 执行
# 作用: 安装 Xray-core, 生成 UUID 与 x25519 密钥, 写入抗封锁的 Reality 配置,
#       启动 systemd 服务, 并打印可直接导入 AccessPilot 的分享链接。
#
# 说明: 本脚本按 XTLS 官方安装流程编写(install-release.sh + xray x25519),
#       但未在真实 VPS 上实测过, 执行前请自行审阅。
#
# 用法:
#   bash server-install.sh              # 默认 443 端口, 伪装 www.microsoft.com
#   PORT=8443 bash server-install.sh
#   DEST=www.cloudflare.com:443 bash server-install.sh

set -euo pipefail

PORT="${PORT:-443}"
DEST="${DEST:-www.microsoft.com:443}"
SNI="${DEST%%:*}"
XRAY_CONF="/usr/local/etc/xray/config.json"

log()  { printf '\033[36m[*]\033[0m %s\n' "$*"; }
ok()   { printf '\033[32m[+]\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[!]\033[0m %s\n' "$*"; }
die()  { printf '\033[31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

# --------------------------------------------------------------------------- #
[[ $EUID -eq 0 ]] || die "请以 root 身份运行"

if ! command -v curl >/dev/null 2>&1; then
  log "安装 curl ..."
  apt-get update -qq && apt-get install -y -qq curl ca-certificates >/dev/null
fi

# --------------------------------------------------------------------------- #
log "安装 Xray-core (官方脚本) ..."
bash -c "$(curl -fsSL https://github.com/XTLS/Xray-install/raw/main/install-release.sh)" @ install
command -v xray >/dev/null 2>&1 || die "Xray 安装失败"

# --------------------------------------------------------------------------- #
log "生成凭据 ..."
UUID="$(xray uuid)"
KEYPAIR="$(xray x25519)"
PRIVATE_KEY="$(echo "$KEYPAIR" | awk -F': *' '/[Pp]rivate/{print $2}')"
PUBLIC_KEY="$(echo "$KEYPAIR" | awk -F': *' '/[Pp]ublic/{print $2}')"
SHORT_ID="$(head -c 8 /proc/sys/kernel/random/uuid | tr -d '-')"

[[ -n "$PRIVATE_KEY" && -n "$PUBLIC_KEY" ]] || die "x25519 密钥生成失败"

# --------------------------------------------------------------------------- #
log "写入配置 $XRAY_CONF ..."
mkdir -p "$(dirname "$XRAY_CONF")"
cat > "$XRAY_CONF" <<JSON
{
  "log": { "loglevel": "warning" },
  "inbounds": [
    {
      "listen": "0.0.0.0",
      "port": ${PORT},
      "protocol": "vless",
      "settings": {
        "clients": [
          { "id": "${UUID}", "flow": "xtls-rprx-vision", "email": "accesspilot@local" }
        ],
        "decryption": "none"
      },
      "streamSettings": {
        "network": "tcp",
        "security": "reality",
        "realitySettings": {
          "show": false,
          "dest": "${DEST}",
          "xver": 0,
          "serverNames": ["${SNI}"],
          "privateKey": "${PRIVATE_KEY}",
          "shortIds": ["${SHORT_ID}"]
        }
      },
      "sniffing": { "enabled": true, "destOverride": ["http", "tls", "quic"] }
    }
  ],
  "outbounds": [
    { "protocol": "freedom", "tag": "direct" },
    { "protocol": "blackhole", "tag": "block" }
  ],
  "routing": {
    "domainStrategy": "IPIfNonMatch",
    "rules": [
      { "type": "field", "ip": ["geoip:private"], "outboundTag": "block" },
      { "type": "field", "protocol": ["bittorrent"], "outboundTag": "block" }
    ]
  }
}
JSON

log "校验配置 ..."
xray -test -config "$XRAY_CONF" || die "配置校验失败"

# --------------------------------------------------------------------------- #
log "启动服务 ..."
systemctl enable xray >/dev/null 2>&1 || true
systemctl restart xray
sleep 1
systemctl is-active --quiet xray && ok "Xray 已运行" || die "Xray 启动失败, 查看 journalctl -u xray"

# 防火墙
if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "Status: active"; then
  ufw allow "${PORT}/tcp" >/dev/null 2>&1 && log "已放行 ufw ${PORT}/tcp"
fi
if command -v firewall-cmd >/dev/null 2>&1 && firewall-cmd --state >/dev/null 2>&1; then
  firewall-cmd --permanent --add-port="${PORT}/tcp" >/dev/null && firewall-cmd --reload >/dev/null
  log "已放行 firewalld ${PORT}/tcp"
fi

# --------------------------------------------------------------------------- #
SERVER_IP="$(curl -fsS4 --max-time 8 https://api.ipify.org || echo '<你的服务器IP>')"
LINK="vless://${UUID}@${SERVER_IP}:${PORT}?encryption=none&security=reality&sni=${SNI}&fp=chrome&pbk=${PUBLIC_KEY}&sid=${SHORT_ID}&type=tcp&flow=xtls-rprx-vision#AccessPilot-VPS"

echo
echo "=============================================================="
ok "部署完成"
echo "  服务器 : ${SERVER_IP}:${PORT}"
echo "  协议   : VLESS + Vision + Reality (TCP)"
echo "  伪装SNI: ${SNI}"
echo
echo "在本地导入节点:"
echo "  accesspilot sub add \"${LINK}\" --name my-vps --use"
echo "  accesspilot start --tun"
echo "  accesspilot test"
echo
echo "分享链接(请妥善保管):"
echo "${LINK}"
echo "=============================================================="
