# -*- coding: utf-8 -*-
"""
campus_core.py —— CQUST 校园网 eportal 自动登录核心模块
====================================================
职责：
  1. 网络状态探测（generate_204 探针，识别是否被门户劫持）
  2. 从门户重定向中捕获 queryString（登录必需的加密参数）
  3. 调用 InterFace.do?method=login 完成登录
  4. 统一日志输出（供远程排查 bug 使用）

全部使用 Python 标准库，无需安装任何第三方依赖。
"""

import base64
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

APP_VERSION = "1.1.0"   # 应用版本号（显示在设置页脚与 exe 文件属性里）
APP_NAME = "校园网自动登录"

# 数据目录：源码运行时 = 本文件所在目录；
# 打包成 exe 后 = exe 所在目录（onefile 的临时解压目录不可用，必须用 sys.executable 定位）
APP_DIR = os.path.dirname(os.path.abspath(__file__))
if getattr(sys, "frozen", False):
    APP_DIR = os.path.dirname(sys.executable)
CONFIG_PATH = os.path.join(APP_DIR, "config.json")
LOG_DIR = os.path.join(APP_DIR, "logs")
STATUS_PATH = os.path.join(APP_DIR, "status.json")
PID_PATH = os.path.join(APP_DIR, "daemon.pid")

# Windows 探针地址：联网时返回 204；被门户劫持时返回 302 跳转到认证页
DEFAULT_PROBE_URL = "http://connect.rom.miui.com/generate_204"
# 联网探针链（依次尝试，任一有效即出结果；实测校园网 DNS 解析不了微软 captive 探针）
DEFAULT_PROBE_URLS = [
    "http://connect.rom.miui.com/generate_204",
    "http://www.msftconnecttest.com/redirect",
    "http://www.baidu.com/",
]
# 在线状态下允许的跳转目标（跳微软官网说明已联网，而非门户劫持）
_ONLINE_REDIRECT_HOSTS = ("go.microsoft.com", "microsoft.com", "msftconnecttest.com")
# 认证服务器（可被 config.json 中的 portal_base 覆盖）
DEFAULT_PORTAL_BASE = "http://aaa.cqust.edu.cn"
DEFAULT_SERVICE = "互联网"


# ----------------------------------------------------------------------
# 配置读写（密码做 base64 混淆存储，防止被随手看到，不是加密）
# ----------------------------------------------------------------------
def load_config():
    """读取 config.json，缺失项用默认值补齐"""
    cfg = {
        "username": "",
        "password_b64": "",
        "service": DEFAULT_SERVICE,
        "interval": 15,                      # 探测间隔（秒）
        "portal_base": DEFAULT_PORTAL_BASE,  # 认证服务器地址
        "probe_urls": [],                    # 联网探针链（空则用内置默认链）
        "max_retries": 5,                    # 单轮登录最多重试次数
        "retry_delay": 3,                    # 重试间隔（秒）
        "autostart": False,
        "campus_ssid": "",                   # 校园网 WiFi 名称（SSID），空则不自动连
    }
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg.update(json.load(f))
        except Exception:
            pass  # 配置损坏时使用默认值，由 GUI 重新保存覆盖
    return cfg


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def get_password(cfg):
    try:
        return base64.b64decode(cfg.get("password_b64", "")).decode("utf-8")
    except Exception:
        return ""


def set_password(cfg, plain):
    cfg["password_b64"] = base64.b64encode(plain.encode("utf-8")).decode("ascii") if plain else ""


# ----------------------------------------------------------------------
# HTTP 基础设施（禁用代理、禁用自动重定向 —— 两者都会干扰探测）
# ----------------------------------------------------------------------
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """禁止 urllib 自动跟随 302，以便捕获门户跳转地址"""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


# 不吃系统代理（VPN/Clash 等会导致探测结果失真），不自动重定向
_OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({}),
    _NoRedirect(),
)

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) CampusAutoLogin/1.0",
    "Accept": "*/*",
}


def _decode_body(raw):
    """门户返回内容可能是 UTF-8 或 GBK，依次尝试解码"""
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def http_get_no_redirect(url, timeout=6):
    """GET 请求，不跟随重定向。返回 (状态码, 响应头, 正文, 异常或None)"""
    req = urllib.request.Request(url, headers=_HEADERS)
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), _decode_body(resp.read() or b""), None
    except urllib.error.HTTPError as e:
        # 302 且无重定向处理器时会以 HTTPError 形式抛出，同样携带头和正文
        try:
            body = _decode_body(e.read() or b"")
        except Exception:
            body = ""
        return e.code, dict(e.headers or {}), body, None
    except Exception as e:
        return None, {}, "", "%s: %s" % (type(e).__name__, e)


def http_post(url, body, timeout=8):
    """POST 原始表单体（body 为已拼好的字符串，值已按需编码）。
    返回 (状态码, 正文, 异常或None)"""
    req = urllib.request.Request(
        url,
        data=body.encode("utf-8"),
        headers=dict(_HEADERS, **{"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"}),
    )
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            return resp.status, _decode_body(resp.read() or b""), None
    except urllib.error.HTTPError as e:
        try:
            return e.code, _decode_body(e.read() or b""), None
        except Exception:
            return e.code, "", "%s: %s" % (type(e).__name__, e)
    except Exception as e:
        return None, "", "%s: %s" % (type(e).__name__, e)


# ----------------------------------------------------------------------
# 探测：判断联网状态并捕获门户 queryString
# ----------------------------------------------------------------------
class ProbeResult(object):
    def __init__(self):
        self.online = False       # 是否已联网
        self.portal_url = ""      # 被劫持时跳转到的门户完整地址
        self.query_string = ""    # 门户地址 '?' 后的参数串（登录必需）
        self.portal_base = ""     # 从门户地址推出的服务器根，如 http://aaa.cqust.edu.cn
        self.detail = ""          # 调试信息：状态码 / 异常等


def _probe_once(url, timeout):
    """单次探测一个探针地址。返回 ProbeResult（未确定时 online=None 表示该探针无效）"""
    r = ProbeResult()
    code, headers, body, err = http_get_no_redirect(url, timeout)
    r.detail = "%s -> HTTP %s" % (urllib.parse.urlsplit(url).netloc, code if code else ("异常 %s" % err))

    if code == 204:
        r.online = True
        r.detail += " (已联网)"
        return r

    portal_url = ""
    if code in (301, 302, 303, 307, 308):
        loc = headers.get("Location", "") or headers.get("location", "")
        if loc:
            if "://" not in loc:
                loc = urllib.parse.urljoin(url, loc)
            loc_host = urllib.parse.urlsplit(loc).netloc.lower()
            if any(h in loc_host for h in _ONLINE_REDIRECT_HOSTS):
                # 跳微软官网 = 该探针正常行为，说明已联网
                r.online = True
                r.detail += " → %s (已联网)" % loc_host
                return r
            portal_url = loc
    elif code == 200:
        if "eportal" in body or "index.jsp" in body:
            # 部分 AC 用页面 meta-refresh 跳转而非 302
            m = re.search(r"""https?://[^"'\s<\\]+index\.jsp\?[^"'\s<\\]+""", body)
            if m:
                portal_url = m.group(0)
        elif "baidu" in body or "Baidu" in body:
            # 最后兜底探针：百度首页正常返回 → 已联网
            r.online = True
            r.detail += " (百度正常)"
            return r

    if portal_url:
        r.portal_url = portal_url
        parts = urllib.parse.urlsplit(portal_url)
        r.query_string = parts.query
        r.portal_base = "%s://%s" % (parts.scheme, parts.netloc)
        r.online = False
        r.detail += " → 门户 %s" % parts.netloc
    else:
        r.online = None  # 本探针无法判定（超时/DNS失败/未知响应）
    return r


def probe(probe_url=None, timeout=6):
    """按探针链依次探测网络，返回 ProbeResult。
    - 任一探针 204 / 正常跳微软 / 百度正常 → 已联网
    - 任一探针 302 跳到非微软地址 → 未认证，Location 即门户地址（含 queryString）
    - 全部失败 → 网络不可达（WiFi 断开等），不尝试登录
    probe_url 参数兼容旧调用（字符串或列表均可）
    """
    if isinstance(probe_url, str):
        urls = [probe_url]
    elif isinstance(probe_url, (list, tuple)):
        urls = [u for u in probe_url if u]
    else:
        urls = list(DEFAULT_PROBE_URLS)
    if not urls:  # 空列表/空字符串 → 使用默认探针链
        urls = list(DEFAULT_PROBE_URLS)

    final = ProbeResult()
    final.detail = ""
    for u in urls:
        r = _probe_once(u, timeout)
        if r.online is True:
            return r
        if r.online is False and r.query_string:
            return r  # 明确检测到门户，直接返回用于登录
        if final.detail:
            final.detail += "；"
        final.detail += r.detail
    final.online = False
    return final


# ----------------------------------------------------------------------
# 登录
# ----------------------------------------------------------------------
def _q(s):
    """与前端一致的双重 URL 编码"""
    return urllib.parse.quote(urllib.parse.quote(str(s), safe=""), safe="")


def eportal_login(portal_base, query_string, username, password, service=""):
    """调用 eportal 登录接口。返回 (是否成功, 服务器原始响应, 解析后的dict)"""
    url = portal_base.rstrip("/") + "/eportal/InterFace.do?method=login"
    # 严格模拟 login_bch.js 中 doauthen() 拼的表单体
    body = (
        "userId=" + _q(username) +
        "&password=" + _q(password) +
        "&service=" + _q(service) +
        "&queryString=" + _q(query_string) +
        "&operatorPwd=" + _q("") +
        "&operatorUserId=" + _q("") +
        "&validcode=" +
        "&passwordEncrypt=" + _q("false")
    )
    code, resp_text, err = http_post(url, body)
    data = {}
    try:
        data = json.loads(resp_text)
    except Exception:
        pass
    ok = isinstance(data, dict) and data.get("result") == "success"
    return ok, resp_text, data, code, err


# ----------------------------------------------------------------------
# WiFi 操作（通过 netsh，不依赖额外库）
# ----------------------------------------------------------------------
def _netsh(args, timeout=15):
    """运行 netsh 命令并返回输出（自动尝试多种编码）。失败/超时返回 ('err', message)。"""
    import subprocess
    try:
        r = subprocess.run(["netsh"] + args, capture_output=True, timeout=timeout,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        out = r.stdout
        for enc in ("utf-8", "gbk", "cp936", "utf-16", "latin1"):
            try:
                return ("ok", out.decode(enc).strip())
            except Exception:
                continue
        return ("ok", out.decode("utf-8", errors="ignore").strip())
    except subprocess.TimeoutExpired:
        return ("err", "timeout")
    except FileNotFoundError:
        return ("err", "netsh 不存在")
    except Exception as e:
        return ("err", str(e))


def get_connected_ssid():
    """返回当前已连接的 WiFi SSID，没有就返回 None。"""
    status, out = _netsh(["wlan", "show", "interfaces"])
    if status != "ok":
        return None
    for line in out.splitlines():
        s = line.strip()
        if s.startswith("SSID") and ":" in s and "BSSID" not in s:
            v = s.split(":", 1)[1].strip()
            return v or None
    return None


def scan_ssids(timeout=15):
    """返回当前可见的 SSID 列表（去重）。"""
    status, out = _netsh(["wlan", "show", "networks", "mode=bssid"], timeout=timeout)
    if status != "ok":
        return []
    seen, sids = set(), []
    for raw in out.splitlines():
        line = raw.strip()
        if line.lower().startswith("ssid ") and ":" in line:
            sid = line.split(":", 1)[1].strip()
            if sid and sid not in seen:
                seen.add(sid); sids.append(sid)
    return sids


def connect_ssid(ssid, timeout=30):
    """连接指定 SSID 的 WiFi。返回 (ok: bool, msg: str)。
    Windows 可能弹「SSID 没有」」错误的弹窗（即使 netsh 本身调用成功），那弹窗是系统级的，本程序无法关闭。"""
    if not ssid:
        return False, "SSID 为空"
    status, out = _netsh(["wlan", "connect", f"name={ssid}"], timeout=timeout)
    ok = (status == "ok") and ("已成功完成请求" in out or "已成功" in out or "successfully" in out.lower() or "completed successfully" in out.lower())
    return ok, out or status


def ensure_campus_wifi(cfg):
    """守护启动时调用：若配置了 campus_ssid 且当前没连，自动连。返回 (是否已就绪, 说明)。"""
    ssid = (cfg.get("campus_ssid") or "").strip()
    if not ssid:
        return True, "未配置校园网 SSID，跳过自动连接"
    cur = get_connected_ssid()
    if cur == ssid:
        return True, f"已连接校园网：{ssid}"
    log.info("[WiFi] 当前=%s，目标=%s", cur or "(无)", ssid)
    # 先确认该 SSID 在可见列表里（没看到说明附近没这网络，跳过）
    visible = scan_ssids(timeout=10)
    if visible and ssid not in visible:
        return False, f"附近未发现校园网 WiFi「{ssid}」（可见：{len(visible)} 个网络）"
    ok, msg = connect_ssid(ssid)
    if ok:
        log.info("[WiFi] 已发起连接：%s", ssid)
    else:
        log.warning("[WiFi] 连接请求失败：%s", msg)
    return ok, msg


# ----------------------------------------------------------------------
# 状态文件（daemon 写、GUI 读）
# ----------------------------------------------------------------------
def write_status(**kw):
    status = {}
    if os.path.exists(STATUS_PATH):
        try:
            with open(STATUS_PATH, "r", encoding="utf-8") as f:
                status = json.load(f)
        except Exception:
            status = {}
    status.update(kw)
    status["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    tmp = STATUS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(status, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATUS_PATH)


def read_status():
    try:
        with open(STATUS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def mask_secret(s):
    """日志中打码密码：保留首尾各1位"""
    if not s:
        return "<空>"
    if len(s) <= 2:
        return "***"
    return s[0] + "***" + s[-1]


def mask_username(u):
    """账号脱敏显示（界面用）：保留前2位与后2位，中间打码。空则返回空"""
    u = str(u or "").strip()
    if not u:
        return ""
    if len(u) <= 4:
        return u[0] + "***"
    return u[:2] + "***" + u[-2:]


# ----------------------------------------------------------------------
# 后台守护进程控制（GUI 与 exe 入口共用）
# ----------------------------------------------------------------------
def is_daemon_running():
    """判断后台守护是否在运行（读 pid 文件 + 校验进程存在）"""
    if not os.path.exists(PID_PATH):
        return False
    try:
        pid = int(open(PID_PATH).read().strip())
    except Exception:
        return False
    try:
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if h:
            k32.CloseHandle(h)
            return True
        return False
    except Exception:
        return False


def write_pid():
    with open(PID_PATH, "w") as f:
        f.write(str(os.getpid()))


def remove_pid():
    try:
        os.remove(PID_PATH)
    except OSError:
        pass


def start_daemon():
    """后台静默启动守护进程（无窗口）。
    - exe 模式：用当前 exe 加 --daemon 参数重启自身
    - 源码模式：用 pythonw 跑 daemon.py
    返回 (是否成功, 说明)"""
    if is_daemon_running():
        return False, "后台服务已在运行中"

    import subprocess
    # DETACHED + 尝试脱离 Job 对象：保证 GUI 进程/其所在 Job 结束时，后台守护不被连带杀掉
    flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
    BREAKAWAY = getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x01000000)
    devnull = subprocess.DEVNULL

    def _spawn(cmd):
        # 先尝试带 BREAKAWAY 启动；若所在 Job 不允许脱离（极少见）则退回普通启动
        try:
            return subprocess.Popen(cmd, cwd=APP_DIR, creationflags=flags | BREAKAWAY,
                                    stdin=devnull, stdout=devnull, stderr=devnull, close_fds=True)
        except OSError:
            return subprocess.Popen(cmd, cwd=APP_DIR, creationflags=flags,
                                    stdin=devnull, stdout=devnull, stderr=devnull, close_fds=True)

    if getattr(sys, "frozen", False):
        # 打包后：同一 exe 以 --daemon 模式静默运行
        _spawn([sys.executable, "--daemon"])
    else:
        exe = sys.executable
        pythonw = os.path.join(os.path.dirname(exe), "pythonw.exe")
        if os.path.exists(pythonw):
            exe = pythonw
        daemon_path = os.path.join(APP_DIR, "daemon.py")
        _spawn([exe, daemon_path])

    # 最多等 3.5 秒确认 pid 文件就绪（进程冷启动需要 1-2 秒）
    for _ in range(7):
        time.sleep(0.5)
        if is_daemon_running():
            return True, "已启动"
    return False, "启动失败，请查看日志"


def stop_daemon():
    """停止后台守护进程。返回 (是否成功, 说明)"""
    if not os.path.exists(PID_PATH):
        return False, "后台服务未在运行"
    try:
        pid = int(open(PID_PATH).read().strip())
    except Exception:
        return False, "pid 文件损坏"
    import subprocess
    subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                   capture_output=True, creationflags=0x08000000)
    remove_pid()
    return True, "已停止 (pid=%s)" % pid
