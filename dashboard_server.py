#!/usr/bin/env python3
"""Dashboard Web服务器 — 多线程持久化版本

- ThreadingHTTPServer: 每个请求独立线程
- 并发上限: 最多10个并发, 超限返回503
- 请求超时守护: /api/data 超过5秒自动返回500
"""

import os
import sys
import json
import signal
import threading
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).parent))

DASHBOARD_DIR = Path(__file__).parent / "data"
PORT = 8899
MAX_CONCURRENT = 10


class DashboardHandler(SimpleHTTPRequestHandler):
    """多线程安全 — 每个请求独立实例"""

    _concurrency = threading.Semaphore(MAX_CONCURRENT)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(DASHBOARD_DIR), **kwargs)

    def do_GET(self):
        acquired = self._concurrency.acquire(blocking=False)
        if not acquired:
            self._safe_json_response(503, '{"error":"too many requests"}')
            return
        try:
            self._do_GET()
        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
            pass
        finally:
            self._concurrency.release()

    def _do_GET(self):
        path = urlparse(self.path).path

        # /api/data — JSON 数据端点 (5s 超时守护)
        if path == '/api/data':
            self._handle_api_data()
            return

        # 根路径重定向
        if path == '/' or path == '':
            self.send_response(302)
            self.send_header('Location', '/dashboard.html')
            self.end_headers()
            return

        # 静态文件
        file_path = os.path.join(str(DASHBOARD_DIR), path.lstrip('/'))
        if not os.path.realpath(file_path).startswith(str(DASHBOARD_DIR)):
            self.send_response(403)
            self.end_headers()
            return
        if not os.path.isfile(file_path):
            self.send_response(404)
            self.end_headers()
            return

        ext = os.path.splitext(file_path)[1].lower()
        mime_map = {'.html': 'text/html; charset=utf-8', '.css': 'text/css',
                    '.js': 'application/javascript', '.json': 'application/json',
                    '.png': 'image/png', '.svg': 'image/svg+xml',
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
        try:
            self.send_response(code)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(body.encode('utf-8'))
        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
            pass

    def _handle_api_data(self):
        """安全处理 /api/data，带超时"""
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
            self._safe_json_response(500, json.dumps(
                {"error": "timeout", "message": "Data fetch took too long"}))
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
            raise

    def do_OPTIONS(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, format, *args):
        if args[1] not in ('200', '302', '304'):
            super().log_message(format, *args)


def main():
    DASHBOARD_DIR.mkdir(parents=True, exist_ok=True)

    server = ThreadingHTTPServer(('0.0.0.0', PORT), DashboardHandler)

    def _shutdown(sig, frame):
        print("\nServer shutting down...")
        server.shutdown()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    print(f"Dashboard (threaded) :{PORT}")
    print(f"Concurrency limit: {MAX_CONCURRENT}")
    print(f"Doc root: {DASHBOARD_DIR}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        print("Stopped")


if __name__ == "__main__":
    main()
