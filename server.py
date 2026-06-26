import http.server
import socketserver
import json
import os
import sys

# Add current directory to path to import check_sso
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
try:
    from check_sso import check_sso_with_retry, check_account_with_retry
except ImportError:
    print("Lỗi: Không thể tìm thấy file check_sso.py.")
    sys.exit(1)

PORT = 8080
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
web_dir = os.path.join(BASE_DIR, "web")
if os.path.exists(web_dir):
    DIRECTORY = web_dir
    SERVE_FROM_ROOT = False
else:
    DIRECTORY = BASE_DIR
    SERVE_FROM_ROOT = True

class MyHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=DIRECTORY, **kwargs)

    def do_GET(self):
        # Prevent access to source files if running from root
        if SERVE_FROM_ROOT:
            path = self.translate_path(self.path)
            basename = os.path.basename(path)
            if basename.endswith('.py') or basename.endswith('.txt') or basename.startswith('.'):
                self.send_error(403, "Access Denied")
                return
        super().do_GET()

    def do_POST(self):
        if self.path == '/api/convert':
            content_length = int(self.headers.get('Content-Length', 0))
            post_data = self.rfile.read(content_length)
            
            try:
                payload = json.loads(post_data.decode('utf-8'))
                sso_key = payload.get('sso_key', '').strip()
                account = payload.get('account', '').strip()
                password = payload.get('password', '').strip()
                
                if sso_key:
                    # Run the robust SSO check with automatic proxy support
                    result = check_sso_with_retry(sso_key)
                elif account and password:
                    # Run Garena Account check
                    result = check_account_with_retry(account, password)
                else:
                    self.send_error_response("invalid_input_data")
                    return
                
                if result.get("status") == "success":
                    self.send_success_response(result)
                else:
                    self.send_error_response(result.get("detail", "unknown_error"))
                    
            except Exception as e:
                self.send_error_response(str(e))
        else:
            self.send_error(404, "Not Found")

    def send_success_response(self, data):
        self.send_response(200)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-cache')
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode('utf-8'))

    def send_error_response(self, detail):
        self.send_response(400)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-cache')
        self.end_headers()
        response = {"status": "fail", "detail": detail}
        self.wfile.write(json.dumps(response, ensure_ascii=False).encode('utf-8'))

    # Override log_message to reduce spam
    def log_message(self, format, *args):
        try:
            req_line = str(args[0]) if len(args) > 0 else ""
            status_code = 0
            if len(args) > 1:
                try:
                    status_code = int(args[1])
                except (ValueError, TypeError):
                    pass
            
            # Only log API calls or errors
            if "api/convert" in req_line or status_code >= 400:
                sys.stdout.write("%s - - [%s] %s\n" % (self.address_string(), self.log_date_time_string(), format%args))
        except Exception:
            pass

def main():
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding='utf-8')
            sys.stderr.reconfigure(encoding='utf-8')
        except Exception:
            pass

    print("==================================================")
    print("🚀 LOCAL WEB SERVER CHUYỂN ĐỔI GARENA SSO ĐANG CHẠY")
    print(f"👉 Địa chỉ: http://localhost:{PORT}")
    print("👉 Hãy mở địa chỉ trên bằng trình duyệt của bạn.")
    print("👉 Để tắt server: Nhấn Ctrl + C")
    print("==================================================")
    
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("", PORT), MyHandler) as httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nĐã dừng Server. Hẹn gặp lại!")

if __name__ == "__main__":
    main()
