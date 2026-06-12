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
# 也检查 /tmp 的（兼容旧版）
if not os.path.exists(URL_FILE):
    URL_FILE = "/tmp/cloudflared_url.txt"
WORKER_SCRIPT = os.path.expanduser("~/ATOS_PRO/cloudflare-worker.js")
WORKER_NAME = "atos-dashboard"

def get_current_tunnel_url() -> str | None:
    if not os.path.exists(URL_FILE):
        return None
    try:
        with open(URL_FILE) as f:
            return f.read().strip()
    except Exception:
        return None

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
