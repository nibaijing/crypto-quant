#!/usr/bin/env python3
"""Dashboard Web服务器 (增强版)

启动一个简单的HTTP服务器来展示Dashboard
- 访问根路径自动重定向到dashboard.html
- 支持CORS
- 移动端适配
"""

import os
import sys
import json
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse

# 添加项目路径
sys.path.insert(0, str(Path(__file__).parent))

# Dashboard目录
DASHBOARD_DIR = Path(__file__).parent / "data"
PORT = 8899


class DashboardHandler(SimpleHTTPRequestHandler):
    """自定义请求处理器，支持CORS和根路径重定向"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(DASHBOARD_DIR), **kwargs)

    def do_GET(self):
        """处理GET请求"""
        # 解析路径
        parsed_path = urlparse(self.path)

        # /api/data — JSON 数据端点
        if parsed_path.path == '/api/data':
            try:
                from dashboard_data import get_all_data
                data = get_all_data()
                body = json.dumps(data, ensure_ascii=False)
                self.send_response(200)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_header('Content-Length', len(body.encode('utf-8')))
                self.end_headers()
                self.wfile.write(body.encode('utf-8'))
            except Exception as e:
                err = json.dumps({"error": str(e)})
                self.send_response(500)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(err.encode())
            return

        # 如果访问根路径，重定向到dashboard.html
        if parsed_path.path == '/' or parsed_path.path == '':
            self.send_response(302)
            self.send_header('Location', '/dashboard.html')
            self.end_headers()
            return

        # 否则正常处理
        super().do_GET()

    def end_headers(self):
        """添加CORS头"""
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        super().end_headers()

    def do_OPTIONS(self):
        """处理OPTIONS请求"""
        self.send_response(200)
        self.end_headers()


def main():
    """启动Web服务器"""
    # 确保Dashboard目录存在
    DASHBOARD_DIR.mkdir(parents=True, exist_ok=True)

    # 启动服务器
    server = HTTPServer(('0.0.0.0', PORT), DashboardHandler)
    print(f"🚀 Dashboard Web服务器已启动")
    print(f"📁 Dashboard目录: {DASHBOARD_DIR}")
    print(f"🌐 访问地址: http://localhost:{PORT}")
    print(f"📱 移动端访问: http://192.168.0.104:{PORT}")
    print(f"\n按 Ctrl+C 停止服务器\n")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n\n👋 服务器已停止")
        server.shutdown()


if __name__ == "__main__":
    main()
