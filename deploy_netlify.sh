#!/usr/bin/env bash
# Install Netlify CLI, login, and deploy ATOS Dashboard
# Usage: bash deploy_atos_netlify.sh

set -e

echo "=== ATOS Dashboard - Netlify 部署 ==="

# 检查是否登录
if ! netlify status 2>/dev/null | grep -q "Authenticated"; then
    echo "请先登录 Netlify"
    echo "在浏览器中打开以下链接并授权："
    netlify login --new 2>&1
fi

cd /Users/benson/ATOS_PRO/netlify-deploy

# 获取当前 Tunnel URL
TUNNEL_URL=$(cat /tmp/cloudflared_url.txt 2>/dev/null || echo "https://tariff-explains-heath-oil.trycloudflare.com")
echo "Tunnel URL: $TUNNEL_URL"

# 创建 Netlify Functions 目录
mkdir -p netlify/functions

# 创建 API 代理函数
cat > netlify/functions/api-proxy.js << EOF
// Netlify Function: proxy /api requests to Cloudflare Tunnel
const https = require('https');
const http = require('http');

exports.handler = async (event, context) => {
  const tunnelUrl = '${TUNNEL_URL}';
  const url = new URL(tunnelUrl);
  
  const path = event.path.replace('/.netlify/functions/api-proxy', '/api');
  
  return new Promise((resolve, reject) => {
    const lib = url.protocol === 'https:' ? https : http;
    const req = lib.get({
      hostname: url.hostname,
      port: url.port || 443,
      path: path,
      headers: { 'Accept': 'application/json' },
      timeout: 15000,
    }, (res) => {
      let data = '';
      res.on('data', chunk => data += chunk);
      res.on('end', () => {
        resolve({
          statusCode: 200,
          headers: { 
            'Content-Type': 'application/json',
            'Access-Control-Allow-Origin': '*',
          },
          body: data,
        });
      });
    });
    req.on('error', (e) => {
      resolve({
        statusCode: 500,
        body: JSON.stringify({ error: e.message }),
      });
    });
    req.on('timeout', () => {
      req.destroy();
      resolve({
        statusCode: 504,
        body: JSON.stringify({ error: 'Tunnel timeout' }),
      });
    });
  });
};
EOF

# 创建 netlify.toml
cat > netlify.toml << 'TOM'
[build]
  functions = "netlify/functions"

[[redirects]]
  from = "/api"
  to = "/.netlify/functions/api-proxy"
  status = 200
TOM

# 修改 index.html 恢复相对路径
sed -i '' 's|fetch('"'"'https://.*\.trycloudflare\.com/api|fetch('\''/api|g' index.html

# 部署
netlify deploy --prod --dir=. 2>&1

echo ""
echo "=== 部署完成 ==="
echo "如果域名没变还是匿名部署，可以直接用之前的密码访问"
