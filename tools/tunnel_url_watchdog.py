#!/usr/bin/env python3
"""
ATOS Tunnel URL 看门狗
每5分钟检查 Tunnel URL 是否变了，变了就自动更新 Cloudflare Worker 并通知。
由 Hermes cron job 触发。
"""
import os
import json
import subprocess
import sys

URL_FILE = os.path.expanduser("~/.atos_tunnel_url")
# 也检查 /tmp 的（兼容旧版）和 cloudflared 日志
def _find_tunnel_url():
    """从多个来源找当前 tunnel URL"""
    # 1. 先从 state 文件读
    for path in [URL_FILE, "/tmp/cloudflared_url.txt", "/tmp/atos_tunnel_url.txt"]:
        if os.path.exists(path):
            try:
                with open(path) as f:
                    url = f.read().strip()
                    if "trycloudflare.com" in url:
                        return url
            except: pass
    # 2. 从 cloudflared 日志找
    try:
        import subprocess
        r = subprocess.run(
            "grep -oE 'https://[a-z0-9-]+\\.trycloudflare\\.com' /tmp/atos_tunnel.log 2>/dev/null | tail -1",
            shell=True, capture_output=True, text=True, timeout=5
        )
        url = r.stdout.strip()
        if "trycloudflare.com" in url:
            return url
    except: pass
    # 3. 从 cloudflared 历史文件
    try:
        hist = os.path.expanduser("~/ATOS_PRO/cloudflared_url_history.txt")
        if os.path.exists(hist):
            with open(hist) as f:
                for line in reversed(list(f)):
                    line = line.strip()
                    if "trycloudflare.com" in line:
                        # 验证是否还活着
                        try:
                            import urllib.request
                            req = urllib.request.Request(line, method="HEAD")
                            urllib.request.urlopen(req, timeout=5)
                            return line
                        except: pass
    except: pass
    return None
WORKER_SCRIPT = os.path.expanduser("~/ATOS_PRO/cloudflare-worker.js")
WORKER_NAME = "atos-dashboard"

def get_current_tunnel_url() -> str | None:
    return _find_tunnel_url()

def get_deployed_tunnel_url() -> str | None:
    """从 Worker 源码中读取当前的 TUNNEL_ORIGIN"""
    if not os.path.exists(WORKER_SCRIPT):
        return None
    try:
        with open(WORKER_SCRIPT) as f:
            for line in f:
                if "TUNNEL_ORIGIN" in line and "https://" in line:
                    return line.split('"')[1]
    except Exception:
        return None
    return None

def update_worker(new_url: str) -> bool:
    """更新 Worker 中的 TUNNEL_ORIGIN 并重新部署"""
    if not os.path.exists(WORKER_SCRIPT):
        return False
    try:
        with open(WORKER_SCRIPT) as f:
            content = f.read()
        
        old_url = get_deployed_tunnel_url()
        if old_url:
            content = content.replace(old_url, new_url)
        
        with open(WORKER_SCRIPT, "w") as f:
            f.write(content)
        
        # 重新部署
        result = subprocess.run(
            ["wrangler", "deploy", WORKER_SCRIPT, "--name", WORKER_NAME,
             "--compatibility-date", "2026-06-12"],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            print(f"Worker 已更新: {new_url}")
            return True
        else:
            print(f"Worker 部署失败: {result.stderr[:200]}")
            return False
    except Exception as e:
        print(f"Worker 更新异常: {e}")
        return False

def main():
    current = get_current_tunnel_url()
    deployed = get_deployed_tunnel_url()
    
    if not current:
        print("Tunnel URL 文件不存在，跳过")
        return
    
    if current == deployed:
        # 没变化，安静退出
        return
    
    print(f"Tunnel URL 变了: {deployed} → {current}")
    if update_worker(current):
        print(f"✅ Worker 已更新至 {current}")
        print(f"Dashboard: https://{WORKER_NAME}.darkside7313.workers.dev")
    else:
        print("❌ Worker 更新失败")
        sys.exit(1)

if __name__ == "__main__":
    main()
