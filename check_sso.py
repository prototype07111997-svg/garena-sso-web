import requests
import sys
import os
import json
import time
import urllib3
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
urllib3.disable_warnings()

_print_lock = threading.Lock()
_file_lock = threading.Lock()

try:
    from check import check_login
except ImportError:
    check_login = None

# Enable ANSI color support and UTF-8 encoding on Windows console
class C:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    RED = "\033[91m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    CYAN = "\033[96m"
    WHITE = "\033[97m"

if sys.platform == "win32":
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
    except Exception:
        pass
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except Exception:
        pass

# Global statistics counters for report summary
_success_count = 0
_claimed_count = 0
_fail_count = 0
_stats_lock = threading.Lock()

def print_summary(total_lines):
    print("\n" + "=" * 58)
    print(f"║ {C.BOLD}{C.CYAN}                    KẾT QUẢ THỐNG KÊ                    {C.RESET} ║")
    print("=" * 58)
    print(f"║  ► Tổng số tài khoản đã quét :  {C.BOLD}{total_lines:<8}{C.RESET}                    ║")
    print(f"║  ► {C.GREEN}Thành công (+150 QH)      {C.RESET}:  {C.BOLD}{C.GREEN}{_success_count:<8}{C.RESET}                    ║")
    print(f"║  ► {C.BLUE}Đã nhận trước đó          {C.RESET}:  {C.BOLD}{C.BLUE}{_claimed_count:<8}{C.RESET}                    ║")
    print(f"║  ► {C.RED}Lỗi/Thất bại              {C.RESET}:  {C.BOLD}{C.RED}{_fail_count:<8}{C.RESET}                    ║")
    print("=" * 58)

def print_banner():
    banner = [
        "                 ██╗  ██╗████████╗",
        "                 ██║  ██║╚══██╔══╝",
        "                 ███████║   ██║   ",
        "                 ██╔══██║   ██║   ",
        "                 ██║  ██║   ██║   ",
        "                 ╚═╝  ╚═╝   ╚═╝   "
    ]
    colors = [C.RED, C.YELLOW, C.GREEN, C.CYAN, C.BLUE, C.WHITE]
    print()
    for line, color in zip(banner, colors):
        print(f"{C.BOLD}{color}{line}{C.RESET}")
    print(f"{C.BOLD}{C.CYAN}" + "=" * 59 + f"{C.RESET}")
    print(f"║ {C.BOLD}{C.WHITE}     HỆ THỐNG VƯỢT ẢI TỰ ĐỘNG - NHẬN +150 QUÂN HUY     {C.RESET} ║")
    print(f"{C.BOLD}{C.CYAN}" + "=" * 59 + f"{C.RESET}")
    print()

USE_PROXY = True

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
                    "event_link": event_link,
                    "access_token": access_token
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
    if res["status"] == "success" or res["detail"] == "error_session" or not USE_PROXY:
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
                    "event_link": event_link,
                    "access_token": access_token
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
    if res["status"] == "success" or res["detail"] in ("INVALID", "result=3", "result=101", "result=367") or not USE_PROXY:
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

def _run_event_automation_internal(access_token, proxy=None):
    if not access_token:
        return "missing_token"
        
    sess = requests.Session()
    if proxy:
        sess.proxies = {"http": f"http://{proxy}", "https": f"http://{proxy}"}
        
    sess.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://shootingwar.lienquan.garena.vn",
        "Referer": "https://shootingwar.lienquan.garena.vn/"
    })
    
    # 1. Login via callback to get cookies
    callback_url = f"https://shootingwar.lienquan.garena.vn/connect/garena/callback?access_token={access_token}"
    try:
        r = sess.get(callback_url, timeout=10, verify=False)
        if r.status_code not in (200, 302):
            return f"login_failed_status_{r.status_code}"
    except Exception as e:
        return f"login_error_{str(e)}"
        
    # Check if ff_session cookie is set
    cookies = sess.cookies.get_dict()
    if "ff_session" not in cookies:
        return "missing_session_cookie"
        
    # 2. Claim login reward first to get soccer ball tokens
    try:
        sess.post(
            "https://shootingwar.lienquan.garena.vn/api/app/mission/update_status",
            data="",
            timeout=10,
            verify=False
        )
    except Exception:
        pass

    # 3. Call game/start to initialize and get progress
    try:
        r = sess.get("https://shootingwar.lienquan.garena.vn/api/app/game/start", timeout=10, verify=False)
        if r.status_code != 200:
            try:
                err_msg = r.json().get("error", "") or r.json().get("detail", "") or r.text
                return f"start_failed_{r.status_code}_{err_msg[:60]}"
            except Exception:
                return f"start_failed_status_{r.status_code}"
        start_data = r.json()
    except Exception as e:
        return f"start_error_{str(e)}"

    specials = start_data.get("userMissionSpecials", [])
    special_id = None
    already_claimed = False
    progress = 0
    for spec in specials:
        if spec.get("missionId") == 1:
            special_id = spec.get("id")
            progress = spec.get("progress", 0)
            if spec.get("status") == "claimed":
                already_claimed = True

    # 4. Complete stages sequentially only if progress < 4
    played_stages = False
    if progress < 4 and not already_claimed:
        for stage in range(progress + 1, 5):
            payload = {
                "data": [],
                "result": {
                    "levelNum": stage,
                    "second": 20,
                    "complete": True
                }
            }
            max_retries = 3
            for attempt in range(max_retries):
                try:
                    r = sess.post(
                        "https://shootingwar.lienquan.garena.vn/api/app/game/update_status",
                        json=payload,
                        timeout=10,
                        verify=False
                    )
                    if r.status_code == 200:
                        played_stages = True
                        break
                    elif r.status_code == 429:
                        time.sleep(2.5)
                    else:
                        time.sleep(1.0)
                except Exception as e:
                    if attempt == max_retries - 1:
                        return f"stage_{stage}_error_{str(e)}"
                    time.sleep(1.5)
            else:
                return f"stage_{stage}_failed_status_{r.status_code}"
            time.sleep(0.5)

    # Re-fetch start to get the newly generated special mission ID if we played
    if played_stages:
        try:
            r = sess.get("https://shootingwar.lienquan.garena.vn/api/app/game/start", timeout=10, verify=False)
            if r.status_code == 200:
                start_data = r.json()
                specials = start_data.get("userMissionSpecials", [])
                for spec in specials:
                    if spec.get("missionId") == 1:
                        special_id = spec.get("id")
                        if spec.get("status") == "claimed":
                            already_claimed = True
        except Exception:
            pass

    # 5. Claim daily missions/special missions
    token_count = "N/A"
    max_retries = 3
    for attempt in range(max_retries):
        try:
            r = sess.post("https://shootingwar.lienquan.garena.vn/api/app/me/claim_daily_mission", timeout=10, verify=False)
            if r.status_code == 200:
                mission_res = r.json()
                token_count = str(mission_res.get("userExtension", {}).get("tokenNum", "N/A"))
                break
            elif r.status_code == 429:
                time.sleep(2.5)
            else:
                time.sleep(1.0)
        except Exception as e:
            if attempt == max_retries - 1:
                token_count = f"error-{str(e)}"
            time.sleep(1.5)
    else:
        token_count = f"failed-status-{r.status_code}"
        
    # 6. Claim special reward (150 Limited QH)
    qh_status = "no-special-id"
    if special_id:
        if already_claimed:
            qh_status = "already-claimed"
        else:
            payload = {"userMissionSpecialId": special_id}
            max_retries = 3
            for attempt in range(max_retries):
                try:
                    time.sleep(1.5)
                    r = sess.post(
                        "https://shootingwar.lienquan.garena.vn/api/app/me/claim_special_reward",
                        json=payload,
                        timeout=10,
                        verify=False
                    )
                    if r.status_code == 200:
                        qh_status = "claimed"
                        break
                    elif r.status_code == 429:
                        time.sleep(2.5)
                    else:
                        try:
                            err_detail = r.json().get("detail", "")
                            qh_status = f"failed-{err_detail or r.status_code}"
                        except Exception:
                            qh_status = f"failed-{r.status_code}"
                        break
                except Exception as e:
                    if attempt == max_retries - 1:
                        qh_status = f"error-{str(e)}"
                    time.sleep(1.5)
                
    return f"success_tokens_{token_count}_qh_{qh_status}"

def run_event_automation(access_token, proxy=None):
    if not access_token:
        return "missing_token"
        
    if not USE_PROXY:
        return _run_event_automation_internal(access_token, proxy=None)
        
    use_proxy = proxy
    if not use_proxy:
        import random
        kiot_key = load_kiot_key()
        static_proxies = load_static_proxies()
        if kiot_key:
            use_proxy = get_current_kiot_proxy(kiot_key) or get_new_kiot_proxy(kiot_key)
        elif static_proxies:
            use_proxy = random.choice(static_proxies)
            
    # Try with selected proxy (or direct if no proxy configured)
    res = _run_event_automation_internal(access_token, use_proxy)
    if res.startswith("success_") and not any(err in res for err in ("Connection", "ProxyError", "connect", "refused", "Max retries")):
        return res
        
    # If failed due to connection/proxy error, fallback to direct!
    if not res.startswith("success_") or any(err in res for err in ("Connection", "ProxyError", "connect", "refused", "login_error", "Max retries")):
        res_direct = _run_event_automation_internal(access_token, proxy=None)
        return res_direct
        
    return res

def process_and_claim(res, thread_name, proxy=None):
    if res["status"] == "success":
        access_token = res.get("access_token")
        if access_token:
            ev_res = run_event_automation(access_token, proxy)
            if ev_res.startswith("success_"):
                parts = ev_res.split("_")
                token_count = "N/A"
                qh_msg = "Không rõ"
                
                if "tokens" in parts:
                    idx = parts.index("tokens")
                    if idx + 1 < len(parts):
                        token_count = parts[idx+1]
                        
                if "qh" in parts:
                    idx = parts.index("qh")
                    if idx + 1 < len(parts):
                        status = parts[idx+1]
                        if status == "claimed":
                            qh_msg = "Đã nhận +150 QH!"
                        elif status == "already-claimed":
                            qh_msg = "Quân Huy đã nhận trước đó"
                        elif status == "no-special-id":
                            qh_msg = "Không tìm thấy nhiệm vụ"
                        elif status.startswith("failed"):
                            qh_msg = f"Thất bại ({status})"
                        else:
                            qh_msg = f"Trạng thái: {status}"
                    
                res["tokens"] = token_count
                res["qh_msg"] = qh_msg
                res["event_status"] = "success"
            else:
                res["event_status"] = f"failed_{ev_res}"
                res["qh_msg"] = f"Lỗi ({ev_res})"
        else:
            res["event_status"] = "missing_access_token"
            res["qh_msg"] = "Lỗi (missing_access_token)"
    return res

def main():
    global USE_PROXY, _success_count, _claimed_count, _fail_count
    if "--no-proxy" in sys.argv:
        USE_PROXY = False
        sys.argv.remove("--no-proxy")
        
    if len(sys.argv) >= 2:
        arg1 = sys.argv[1]
        
        if arg1 == "--file" and len(sys.argv) >= 3:
            filepath = sys.argv[2]
            threads = 5
            if len(sys.argv) >= 4:
                try:
                    threads = int(sys.argv[3])
                except ValueError:
                    pass
            if not os.path.exists(filepath):
                print(f"Lỗi: Không tìm thấy file {filepath}")
                sys.exit(1)
                
            try:
                import check
                if hasattr(check, "_bulk_tune_connections"):
                    check._bulk_tune_connections(threads)
            except Exception:
                pass

            print_banner()
            print(f"Đang đọc file {filepath} (đa luồng: {threads})...")
            try:
                # Reset counters
                with _stats_lock:
                    _success_count = 0
                    _claimed_count = 0
                    _fail_count = 0
                    
                with open(filepath, "r", encoding="utf-8-sig") as f:
                    lines = [line.strip() for line in f if line.strip()]
                    
                if not lines:
                    print("File trống, chương trình kết thúc.")
                    sys.exit(0)
                    
                print(f"\n[{time.strftime('%H:%M:%S')}] Tìm thấy {len(lines)} dòng. Bắt đầu xử lý đa luồng...")
                
                def worker(line):
                    global _success_count, _claimed_count, _fail_count
                    os.makedirs("Nhan_QH", exist_ok=True)
                    thread_name = threading.current_thread().name
                    splitter = None
                    if "|" in line:
                        splitter = "|"
                    elif ":" in line:
                        splitter = ":"
                        
                    if splitter:
                        parts = line.split(splitter, 1)
                        account, password = parts[0].strip(), parts[1].strip()
                        res = check_account_with_retry(account, password)
                        if res["status"] == "success":
                            process_and_claim(res, thread_name)
                            qh_msg = res.get("qh_msg", "Thất bại")
                            
                            if qh_msg == "Đã nhận +150 QH!":
                                display_msg = "QH: Đã nhận +150 QH!"
                                filename = "Nhan_QH/Da_Nhan_150QH.txt"
                                file_content = f"{line}\n"
                            elif qh_msg == "Quân Huy đã nhận trước đó":
                                display_msg = "đã nhận trước đó"
                                filename = "Nhan_QH/Da_Nhan_Truoc_Do.txt"
                                file_content = f"{line}\n"
                            else:
                                display_msg = f"QH: {qh_msg}"
                                filename = "Nhan_QH/Loi.txt"
                                file_content = f"{line} | {qh_msg}\n"
                                
                            with _file_lock:
                                with open(filename, "a", encoding="utf-8") as out:
                                    out.write(file_content)
                                if filename == "Nhan_QH/Loi.txt":
                                    with open("Nhan_QH/Chay_Lai.txt", "a", encoding="utf-8") as out:
                                        out.write(f"{line}\n")
                                    
                            with _stats_lock:
                                if qh_msg == "Đã nhận +150 QH!":
                                    _success_count += 1
                                    color_tag = f"{C.GREEN}[THÀNH CÔNG]{C.RESET}"
                                elif qh_msg == "Quân Huy đã nhận trước đó":
                                    _claimed_count += 1
                                    color_tag = f"{C.BLUE}[ĐÃ NHẬN TRƯỚC]{C.RESET}"
                                else:
                                    _fail_count += 1
                                    color_tag = f"{C.RED}[THẤT BẠI]{C.RESET}"
                                    
                            with _print_lock:
                                print(f"{color_tag} {account} | {display_msg}")
                        else:
                            with _file_lock:
                                with open("Nhan_QH/Loi.txt", "a", encoding="utf-8") as out:
                                    out.write(f"{line} | {res.get('detail', 'unknown')}\n")
                                with open("Nhan_QH/Chay_Lai.txt", "a", encoding="utf-8") as out:
                                    out.write(f"{line}\n")
                            with _stats_lock:
                                _fail_count += 1
                                color_tag = f"{C.RED}[LỖI ACC]{C.RESET}"
                            with _print_lock:
                                print(f"{color_tag} {account} | {res.get('detail', 'unknown')}")
                        return res
                    else:
                        sso_key = line
                        key_display = f"{sso_key[:10]}..."
                        res = check_sso_with_retry(sso_key)
                        if res["status"] == "success":
                            process_and_claim(res, thread_name)
                            qh_msg = res.get("qh_msg", "Thất bại")
                            
                            if qh_msg == "Đã nhận +150 QH!":
                                display_msg = "QH: Đã nhận +150 QH!"
                                filename = "Nhan_QH/Da_Nhan_150QH.txt"
                                file_content = f"{line}\n"
                            elif qh_msg == "Quân Huy đã nhận trước đó":
                                display_msg = "đã nhận trước đó"
                                filename = "Nhan_QH/Da_Nhan_Truoc_Do.txt"
                                file_content = f"{line}\n"
                            else:
                                display_msg = f"QH: {qh_msg}"
                                filename = "Nhan_QH/Loi.txt"
                                file_content = f"{line} | {qh_msg}\n"
                                
                            with _file_lock:
                                with open(filename, "a", encoding="utf-8") as out:
                                    out.write(file_content)
                                if filename == "Nhan_QH/Loi.txt":
                                    with open("Nhan_QH/Chay_Lai.txt", "a", encoding="utf-8") as out:
                                        out.write(f"{line}\n")
                                    
                            with _stats_lock:
                                if qh_msg == "Đã nhận +150 QH!":
                                    _success_count += 1
                                    color_tag = f"{C.GREEN}[THÀNH CÔNG]{C.RESET}"
                                elif qh_msg == "Quân Huy đã nhận trước đó":
                                    _claimed_count += 1
                                    color_tag = f"{C.BLUE}[ĐÃ NHẬN TRƯỚC]{C.RESET}"
                                else:
                                    _fail_count += 1
                                    color_tag = f"{C.RED}[THẤT BẠI]{C.RESET}"
                                    
                            with _print_lock:
                                print(f"{color_tag} {res.get('username') or key_display} | {display_msg}")
                        else:
                            with _file_lock:
                                with open("Nhan_QH/Loi.txt", "a", encoding="utf-8") as out:
                                    out.write(f"{line} | {res.get('detail', 'unknown')}\n")
                                with open("Nhan_QH/Chay_Lai.txt", "a", encoding="utf-8") as out:
                                    out.write(f"{line}\n")
                            with _stats_lock:
                                _fail_count += 1
                                color_tag = f"{C.RED}[LỖI ACC]{C.RESET}"
                            with _print_lock:
                                print(f"{color_tag} {key_display} | {res.get('detail', 'unknown')}")
                        return res

                with ThreadPoolExecutor(max_workers=threads) as executor:
                    futures = [executor.submit(worker, line) for line in lines]
                    for fut in as_completed(futures):
                        fut.result()
                
                print_summary(len(lines))
            except KeyboardInterrupt:
                print("\nĐã dừng quét file.")
        else:
            # Check single key
            key = arg1
            res = check_sso_with_retry(key)
            process_and_claim(res, "MainThread")
            print(json.dumps(res, indent=2, ensure_ascii=False))
    else:
        print_banner()
        proxy_choice = input("Bạn có muốn sử dụng Proxy không? (y/n, mặc định y): ").strip().lower()
        if proxy_choice == "n":
            USE_PROXY = False
            print("[ ] Chạy trực tiếp (DIRECT) không sử dụng Proxy.")
        else:
            print("[ Cấu hình Proxy ]")
            kiot_key = load_kiot_key()
            static_proxies = load_static_proxies()
            current_config = ""
            if kiot_key:
                current_config = f"KiotProxy Key: {kiot_key[:5]}...{kiot_key[-5:] if len(kiot_key)>10 else ''}"
            elif static_proxies:
                current_config = f"{len(static_proxies)} Static Proxies"
            
            prompt_msg = "Nhập KiotProxy Key hoặc Proxy mới (nhấn Enter để dùng cấu hình cũ"
            if current_config:
                prompt_msg += f" [{current_config}]"
            prompt_msg += "): "
            
            user_proxy_input = input(prompt_msg).strip()
            if user_proxy_input:
                with open("proxy.txt", "w", encoding="utf-8") as f:
                    f.write(user_proxy_input)
                print("[ ] Đã cập nhật proxy.txt mới!")
            print("[ ] Sử dụng Proxy từ cấu hình proxy.txt / proxya.txt.")
            
        while True:
            try:
                user_input = input("\nNhập SSO Key, Account (user:pass) hoặc đường dẫn file .txt (gõ 'q' để thoát): ").strip().strip('"\'')
                if not user_input:
                    continue
                if user_input.lower() in ("q", "exit", "quit"):
                    break
                
                # Check if it is a file
                if os.path.isfile(user_input):
                    threads_str = input("Nhập số luồng chạy song song (mặc định 5): ").strip()
                    threads = 5
                    if threads_str.isdigit():
                        threads = int(threads_str)
                        
                    try:
                        import check
                        if hasattr(check, "_bulk_tune_connections"):
                            check._bulk_tune_connections(threads)
                    except Exception:
                        pass

                    print(f"Đang đọc file {user_input} (đa luồng: {threads})...")
                    try:
                        if not os.path.exists(user_input):
                            print(f"Lỗi: File {user_input} không tồn tại.")
                            break
                            
                        # Reset counters
                        with _stats_lock:
                            _success_count = 0
                            _claimed_count = 0
                            _fail_count = 0
                            
                        with open(user_input, "r", encoding="utf-8-sig") as f:
                            lines = [line.strip() for line in f if line.strip()]
                            
                        if not lines:
                            print("File trống.")
                            break
                            
                        print(f"\n[{time.strftime('%H:%M:%S')}] Tìm thấy {len(lines)} dòng. Bắt đầu xử lý đa luồng...")
                        
                        def worker(line):
                            global _success_count, _claimed_count, _fail_count
                            os.makedirs("Nhan_QH", exist_ok=True)
                            thread_name = threading.current_thread().name
                            splitter = None
                            if "|" in line:
                                splitter = "|"
                            elif ":" in line:
                                splitter = ":"
                                
                            if splitter:
                                parts = line.split(splitter, 1)
                                account, password = parts[0].strip(), parts[1].strip()
                                res = check_account_with_retry(account, password)
                                if res["status"] == "success":
                                    process_and_claim(res, thread_name)
                                    qh_msg = res.get("qh_msg", "Thất bại")
                                    
                                    if qh_msg == "Đã nhận +150 QH!":
                                        display_msg = "QH: Đã nhận +150 QH!"
                                        filename = "Nhan_QH/Da_Nhan_150QH.txt"
                                        file_content = f"{line}\n"
                                    elif qh_msg == "Quân Huy đã nhận trước đó":
                                        display_msg = "đã nhận trước đó"
                                        filename = "Nhan_QH/Da_Nhan_Truoc_Do.txt"
                                        file_content = f"{line}\n"
                                    else:
                                        display_msg = f"QH: {qh_msg}"
                                        filename = "Nhan_QH/Loi.txt"
                                        file_content = f"{line} | {qh_msg}\n"
                                        
                                    with _file_lock:
                                        with open(filename, "a", encoding="utf-8") as out:
                                            out.write(file_content)
                                        if filename == "Nhan_QH/Loi.txt":
                                            with open("Nhan_QH/Chay_Lai.txt", "a", encoding="utf-8") as out:
                                                out.write(f"{line}\n")
                                                
                                    with _stats_lock:
                                        if qh_msg == "Đã nhận +150 QH!":
                                            _success_count += 1
                                            color_tag = f"{C.GREEN}[THÀNH CÔNG]{C.RESET}"
                                        elif qh_msg == "Quân Huy đã nhận trước đó":
                                            _claimed_count += 1
                                            color_tag = f"{C.BLUE}[ĐÃ NHẬN TRƯỚC]{C.RESET}"
                                        else:
                                            _fail_count += 1
                                            color_tag = f"{C.RED}[THẤT BẠI]{C.RESET}"
                                            
                                    with _print_lock:
                                        print(f"{color_tag} {account} | {display_msg}")
                                else:
                                    with _file_lock:
                                        with open("Nhan_QH/Loi.txt", "a", encoding="utf-8") as out:
                                            out.write(f"{line} | {res.get('detail', 'unknown')}\n")
                                        with open("Nhan_QH/Chay_Lai.txt", "a", encoding="utf-8") as out:
                                            out.write(f"{line}\n")
                                    with _stats_lock:
                                        _fail_count += 1
                                        color_tag = f"{C.RED}[LỖI ACC]{C.RESET}"
                                    with _print_lock:
                                        print(f"{color_tag} {account} | {res.get('detail', 'unknown')}")
                                return res
                            else:
                                sso_key = line
                                key_display = f"{sso_key[:10]}..."
                                res = check_sso_with_retry(sso_key)
                                if res["status"] == "success":
                                    process_and_claim(res, thread_name)
                                    qh_msg = res.get("qh_msg", "Thất bại")
                                    
                                    if qh_msg == "Đã nhận +150 QH!":
                                        display_msg = "QH: Đã nhận +150 QH!"
                                        filename = "Nhan_QH/Da_Nhan_150QH.txt"
                                        file_content = f"{line}\n"
                                    elif qh_msg == "Quân Huy đã nhận trước đó":
                                        display_msg = "đã nhận trước đó"
                                        filename = "Nhan_QH/Da_Nhan_Truoc_Do.txt"
                                        file_content = f"{line}\n"
                                    else:
                                        display_msg = f"QH: {qh_msg}"
                                        filename = "Nhan_QH/Loi.txt"
                                        file_content = f"{line} | {qh_msg}\n"
                                        
                                    with _file_lock:
                                        with open(filename, "a", encoding="utf-8") as out:
                                            out.write(file_content)
                                        if filename == "Nhan_QH/Loi.txt":
                                            with open("Nhan_QH/Chay_Lai.txt", "a", encoding="utf-8") as out:
                                                out.write(f"{line}\n")
                                                
                                    with _stats_lock:
                                        if qh_msg == "Đã nhận +150 QH!":
                                            _success_count += 1
                                            color_tag = f"{C.GREEN}[THÀNH CÔNG]{C.RESET}"
                                        elif qh_msg == "Quân Huy đã nhận trước đó":
                                            _claimed_count += 1
                                            color_tag = f"{C.BLUE}[ĐÃ NHẬN TRƯỚC]{C.RESET}"
                                        else:
                                            _fail_count += 1
                                            color_tag = f"{C.RED}[THẤT BẠI]{C.RESET}"
                                            
                                    with _print_lock:
                                        print(f"{color_tag} {res.get('username') or key_display} | {display_msg}")
                                else:
                                    with _file_lock:
                                        with open("Nhan_QH/Loi.txt", "a", encoding="utf-8") as out:
                                            out.write(f"{line} | {res.get('detail', 'unknown')}\n")
                                        with open("Nhan_QH/Chay_Lai.txt", "a", encoding="utf-8") as out:
                                            out.write(f"{line}\n")
                                    with _stats_lock:
                                        _fail_count += 1
                                        color_tag = f"{C.RED}[LỖI ACC]{C.RESET}"
                                    with _print_lock:
                                        print(f"{color_tag} {key_display} | {res.get('detail', 'unknown')}")
                                return res

                        with ThreadPoolExecutor(max_workers=threads) as executor:
                            futures = [executor.submit(worker, line) for line in lines]
                            for fut in as_completed(futures):
                                fut.result()
                        
                        print_summary(len(lines))
                    except KeyboardInterrupt:
                        print("\nĐã dừng quét file.")
                        break
                else:
                    # Check single key or account
                    splitter = None
                    if "|" in user_input:
                        splitter = "|"
                    elif ":" in user_input:
                        splitter = ":"
                        
                    if splitter:
                        parts = user_input.split(splitter, 1)
                        account, password = parts[0].strip(), parts[1].strip()
                        print(f"Đang check account: {account}...")
                        res = check_account_with_retry(account, password)
                    else:
                        print("Đang check SSO Key...")
                        res = check_sso_with_retry(user_input)
                    process_and_claim(res, "MainThread")
                    print(json.dumps(res, indent=2, ensure_ascii=False))
            except (KeyboardInterrupt, EOFError):
                break
        print("\nTạm biệt!")

if __name__ == "__main__":
    main()
