#!/usr/bin/env python3
"""Dashboard Web服务器 — 多线程持久化版本

- ThreadingHTTPServer: 每个请求独立线程，一个卡住不影响其他
- 请求超时守护: /api/data 超过5秒自动返回500不会卡死
- 自动重启: 异常退出由 systemd 拉起
"""

import os
import sys
import json
import signal
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).parent))

DASHBOARD_DIR = Path(__file__).parent / "data"
PORT = 8899


class DashboardHandler(SimpleHTTPRequestHandler):
    """多线程安全 — 每个请求独立实例"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(DASHBOARD_DIR), **kwargs)

    def do_GET(self):
        try:
            self._do_GET()
        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
            pass  # 客户端断开连接，静默忽略

    def _do_GET(self):
        parsed_path = urlparse(self.path)

        # /api/data — JSON 数据端点 (5s 超时守护)
        if parsed_path.path == '/api/data':
            self._handle_api_data()
            return

        # /api/signals — 最近信号日志
        if parsed_path.path == '/api/signals' or parsed_path.path.startswith('/api/'):
            if parsed_path.path == '/api/signals':
                self._handle_api_signals()
            else:
                self._safe_json_response(404, '{"error":"unknown endpoint"}')
            return

        # 根路径重定向
        if parsed_path.path == '/' or parsed_path.path == '':
            self.send_response(302)
            self.send_header('Location', '/dashboard.html')
            self.end_headers()
            return

        # 静态文件: 禁止缓存
        file_path = os.path.join(str(DASHBOARD_DIR), parsed_path.path.lstrip('/'))
        # 防止路径穿越
        if not os.path.realpath(file_path).startswith(str(DASHBOARD_DIR)):
            self.send_response(403); self.end_headers(); return
        if not os.path.isfile(file_path):
            self.send_response(404); self.end_headers(); return
        # MIME type
        ext = os.path.splitext(file_path)[1].lower()
        mime_map = {'.html': 'text/html; charset=utf-8', '.css': 'text/css', '.js': 'application/javascript',
                    '.json': 'application/json', '.png': 'image/png', '.svg': 'image/svg+xml',
                    '.ico': 'image/x-icon', '.woff2': 'font/woff2'}
        content_type = mime_map.get(ext, 'application/octet-stream')
        self.send_response(200)
        self.send_header('Content-Type', content_type)
        self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
        self.send_header('Pragma', 'no-cache')
        self.send_header('Expires', '0')
        self.end_headers()
        with open(file_path, 'rb') as f:
            self.wfile.write(f.read())

    def _safe_json_response(self, code, body):
        """安全的 JSON 响应，捕获客户端断开"""
        try:
            self.send_response(code)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(body.encode('utf-8'))
        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
            pass

    def _handle_api_signals(self):
        """从日志中提取最近 50 条信号行"""
        import re
        log_path = DASHBOARD_DIR.parent / "data" / "live_trading.log"
        signals = []
        try:
            if log_path.exists():
                lines = log_path.read_text(errors='ignore').splitlines()
                for line in lines:
                    m = re.search(
                        r'(\d{2}:\d{2}:\d{2}).*📊\s*\$\s*([\d,]+).*Eq=\$?([\d,]+)',
                        line
                    )
                    if not m:
                        continue
                    sig_m = re.search(r'SIG=(\w+)', line)
                    k_m = re.search(r'K#(\d+)', line)
                    signals.append({
                        "time": m.group(1),
                        "price": int(float(m.group(2).replace(',', ''))),
                        "equity": int(float(m.group(3).replace(',', ''))),
                        "signal": sig_m.group(1) if sig_m else "?",
                        "kline": int(k_m.group(1)) if k_m else 0,
                    })
        except Exception:
            pass

        body = json.dumps(signals[-50:], ensure_ascii=False)
        self.send_response(200)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.end_headers()
        self.wfile.write(body.encode('utf-8'))

    def _handle_api_data(self):
        """安全处理 /api/data，带超时"""
        import threading
        result = [None]

        def fetch():
            try:
                from dashboard_data import get_all_data
                result[0] = json.dumps(get_all_data(), ensure_ascii=False)
            except Exception as e:
                result[0] = json.dumps({"error": str(e)})

        t = threading.Thread(target=fetch, daemon=True)
        t.start()
        t.join(timeout=5)

        if t.is_alive():
            # 超时 — 不阻塞主线程
            err = json.dumps({"error": "timeout", "message": "Data fetch took too long"})
            self.send_response(500)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(err.encode())
            return

        if result[0] is None:
            body = json.dumps({"error": "internal"})
            self.send_response(500)
        else:
            body = result[0]
            self.send_response(200)

        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', len(body.encode('utf-8')))
        self.end_headers()
        self.wfile.write(body.encode('utf-8'))

    def end_headers(self):
        try:
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Access-Control-Allow-Methods', 'GET, OPTIONS')
            self.send_header('Access-Control-Allow-Headers', 'Content-Type')
            super().end_headers()
        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
            raise  # re-raise to be caught by do_GET wrapper

    def do_OPTIONS(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, format, *args):
        """只记录错误，减少日志噪音"""
        if args[1] != '200' and args[1] != '302' and args[1] != '304':
            super().log_message(format, *args)


def main():
    DASHBOARD_DIR.mkdir(parents=True, exist_ok=True)

    # 优雅退出
    server = ThreadingHTTPServer(('0.0.0.0', PORT), DashboardHandler)

    def _shutdown(sig, frame):
        print("\n👋 Server shutting down...")
        server.shutdown()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    print(f"🚀 Dashboard (threaded) :{PORT}")
    print(f"📁 {DASHBOARD_DIR}")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        print("🛑 Stopped")


if __name__ == "__main__":
    main()