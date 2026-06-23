// ATOS Dashboard — Cloudflare Worker 反向代理
// 把请求转发到本地 Dashboard 的 Cloudflare Tunnel
// 部署后得到固定 URL: https://atos-dashboard.darkside7313.workers.dev

// ⚡ 这个 URL 由 tools/tunnel_url_watchdog.py 自动更新，不要手动改
const TUNNEL_ORIGIN = "https://guides-fold-florida-lip.trycloudflare.com";

// 最后一份成功获取的数据缓存（Tunnel 断连时用）
let lastGoodData = null;

// 降级 HTML — Tunnel 断连时返回给用户
const FALLBACK_HTML = `<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="utf-8"><title>ATOS Dashboard — 离线</title>
<style>
body{font-family:-apple-system,sans-serif;display:flex;flex-direction:column;align-items:center;justify-content:center;height:100vh;margin:0;background:#0a0a0c;color:#e0e0e0;text-align:center;padding:20px}
.card{background:#141416;border:1px solid #2a2a2e;border-radius:12px;padding:40px;max-width:500px}
h1{font-size:24px;margin-bottom:8px}
.status{color:#f0ad4e;font-size:18px;margin:16px 0}
p{color:#888;line-height:1.6;font-size:14px}
.retry-btn{margin-top:20px;padding:10px 24px;background:#d4a853;color:#0a0a0c;border:none;border-radius:8px;cursor:pointer;font-weight:600}
.footer{margin-top:32px;color:#555;font-size:12px}
</style></head>
<body>
<div class="card">
<h1>ATOS PRO 交易系统</h1>
<div class="status">⏳ 本地服务连接中...</div>
<p>Dashboard 正在等待后端数据。<br>
请确保你的 Shadow Trader 和 Dashboard 正在运行。</p>
<p style="color:#666;font-size:13px">Cloudflare Worker 代理正常运行，等待 Tunnel 连接...</p>
<button class="retry-btn" onclick="location.reload()">刷新重试</button>
<div class="footer">atos-dashboard.darkside7313.workers.dev</div>
</div>
<script>setTimeout(function(){location.reload()},15000)</script>
</body>
</html>`;

export default {
  async fetch(request) {
    const url = new URL(request.url);
    const targetUrl = TUNNEL_ORIGIN + url.pathname + url.search;

    try {
      const newRequest = new Request(targetUrl, {
        method: request.method,
        headers: request.headers,
        body: request.body,
      });

      const response = await fetch(newRequest, { timeout: 10 });

      // 如果 Tunnel 返回了正常数据
      if (response.ok) {
        // 如果是 API 请求，缓存数据
        if (url.pathname === '/api') {
          try {
            lastGoodData = await response.clone().text();
          } catch(e) {}
        }

        const newHeaders = new Headers(response.headers);
        newHeaders.set("Cache-Control", "no-store, no-cache");
        // 允许跨域
        newHeaders.set("Access-Control-Allow-Origin", "*");

        return new Response(response.body, {
          status: response.status,
          headers: newHeaders,
        });
      }

      // Tunnel 返回了错误状态
      throw new Error(`Tunnel returned ${response.status}`);

    } catch (err) {
      // Tunnel 连接失败 — 返回降级页面或缓存数据
      if (url.pathname === '/api') {
        // API 请求：返回缓存数据或错误
        if (lastGoodData) {
          return new Response(lastGoodData, {
            status: 200,
            headers: {
              "Content-Type": "application/json",
              "Cache-Control": "no-store, no-cache",
              "Access-Control-Allow-Origin": "*",
              "X-Data-Source": "cache",
            },
          });
        }
        return new Response(JSON.stringify({
          error: "Tunnel disconnected",
          short: { pv: 0, cash: 0, pos: [], cnt: 0 },
          long: { pv: 0, cash: 0, pos: [], cnt: 0 },
        }), {
          status: 200,
          headers: {
            "Content-Type": "application/json",
            "Cache-Control": "no-store",
            "Access-Control-Allow-Origin": "*",
          },
        });
      }

      // 页面请求：返回降级 HTML
      return new Response(FALLBACK_HTML, {
        status: 200,
        headers: {
          "Content-Type": "text/html; charset=utf-8",
          "Cache-Control": "no-store, no-cache",
        },
      });
    }
  },
};
