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
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime

APP_VERSION = "1.2.0"   # 正式版号（内部迭代 v1.2.1~v1.2.13 的修复已全部并入本版）
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


def daemon_pid():
    """读取 pid 文件中的守护进程号，不存在/损坏返回 None"""
    try:
        return int(open(PID_PATH).read().strip())
    except Exception:
        return None


# ----------------------------------------------------------------------
# 开机自启管理（计划任务双轨制）
# ----------------------------------------------------------------------
# 为什么要从「注册表 Run 键」换成「计划任务」：
#   Run 键要等用户登录完成、桌面就绪后才触发，且 Windows 10/11 对启动项有
#   内建错峰延迟，用户体感就是"开机半天才连上网"。计划任务的 ONLOGON 触发器
#   在登录流程早期就触发，且可用 /delay 0000:00 显式取消延迟，免管理员权限。
#
# 两档：
#   登录级 AUTOSTART_TASK（/sc ONLOGON）—— 免管理员，登录后立即启动。默认档。
#   开机级 BOOT_TASK     （/sc ONSTART）—— 需管理员一次性授权，开机即启动，
#                                          锁屏状态下就已经在后台运行。
# ----------------------------------------------------------------------
AUTOSTART_TASK = "CampusAutoLogin"       # 登录级计划任务名（v1.2.12 起改用 Startup 文件夹方案，此名仅作历史兼容保留）
BOOT_TASK = "CampusAutoLoginBoot"        # 开机级任务名
_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_RUN_NAME = "CampusAutoLogin"            # 旧版（<= v1.1.0）遗留的注册表项名

# 「登录后自动运行后台服务」的启动器：放在用户级 Startup 文件夹，
# 登录 Windows 后由 Windows 自动执行。比注册表 Run 早（不经过 Windows 启动项延迟），
# 比 schtasks ONLOGON 可靠（无需 UAC/无需 schtasks，普通用户权限即可读写）。
_STARTUP_DIR = os.path.join(os.environ.get("APPDATA", ""),
                            r"Microsoft\Windows\Start Menu\Programs\Startup")
_STARTUP_AUTOSTART_CMD = os.path.join(_STARTUP_DIR, "校园网自动登录.cmd")

# 极速启动（开机级）的开关状态文件：由创建/删除成功的提权子进程（管理员）写入。
# 为什么不用 schtasks /query 作主判据？——Windows Task Scheduler 服务在不同完整性级别间
# 存在同步/可见性差异，非管理员父进程的 `schtasks /query` 往往查不到刚由管理员创建的
# SYSTEM 任务，导致指示灯/勾选框被反复刷回"未开启"（v1.2.9 之前的表象）。本程序是任务的
# 唯一管理方，因此以"自己写入的开关状态"为权威来源，schtasks 仅在状态文件缺失时回退查询。
BOOT_TASK_STATE = os.path.join(APP_DIR, "boot_task.json")


def _save_boot_state(enable):
    """把极速启动开关状态写入状态文件（供父进程 UI 查询）。失败静默忽略。"""
    try:
        with open(BOOT_TASK_STATE, "w", encoding="utf-8") as f:
            json.dump({"enabled": bool(enable),
                       "updated_at": time.strftime("%Y-%m-%d %H:%M:%S")}, f,
                      ensure_ascii=False)
    except Exception:
        pass


def _short_path(path):
    """把长路径转成 Windows 8.3 短路径名，避免中文/空格在计划任务 SYSTEM 权限下解析异常。
    取不到短路径时原样回退。"""
    try:
        import ctypes
        from ctypes import wintypes
        buf = ctypes.create_unicode_buffer(wintypes.MAX_PATH)
        if not path:
            return path
        if not os.path.exists(path):
            return path
        # GetShortPathNameW(h_lpszLongPath, lpszShortPath, cchBuffer)
        sz = ctypes.windll.kernel32.GetShortPathNameW(path, buf, wintypes.MAX_PATH)
        if sz and sz <= wintypes.MAX_PATH:
            return buf.value
    except Exception:
        pass
    return path


def daemon_command():
    """后台守护启动命令（计划任务与旧注册表项共用）"""
    if getattr(sys, "frozen", False):
        exe = _short_path(sys.executable)
        return '"%s" --daemon' % exe
    exe = sys.executable
    pythonw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if os.path.exists(pythonw):
        exe = pythonw
    return '"%s" "%s"' % (_short_path(exe), _short_path(os.path.join(APP_DIR, "daemon.py")))


def _run_cmd(cmd, timeout=25):
    """执行命令（隐藏窗口），返回 (退出码, 合并输出文本)"""
    import subprocess
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        out = (r.stdout or b"") + (r.stderr or b"")
        for enc in ("utf-8", "gbk", "cp936", "utf-16", "latin1"):
            try:
                return r.returncode, out.decode(enc).strip()
            except Exception:
                continue
        return r.returncode, out.decode("utf-8", errors="ignore")
    except Exception as e:
        return -1, "%s: %s" % (type(e).__name__, e)


def is_admin():
    """当前进程是否以管理员身份运行"""
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _task_exists(name):
    rc, _ = _run_cmd(["schtasks", "/query", "/tn", name], timeout=12)
    return rc == 0


def _task_delete(name):
    rc, _ = _run_cmd(["schtasks", "/delete", "/tn", name, "/f"], timeout=15)
    return rc == 0


def _cleanup_legacy_run_key():
    """清理旧版遗留的注册表 Run 键，避免与计划任务双重启动守护进程"""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            try:
                winreg.DeleteValue(k, _RUN_NAME)
            except FileNotFoundError:
                pass
    except Exception:
        pass


def autostart_enabled():
    """登录级自启（免管理员）是否已开启：通过启动文件夹里的启动器是否存在判定。

    实现方式：把 `校园网自动登录.cmd` 放进用户的「启动」文件夹
    （%APPDATA%\\Microsoft\\Windows\\Start Menu\\Programs\\Startup），
    登录后 Windows 会自动执行。这是用户级操作，无需 schtasks、不弹 UAC，
    比注册表 Run 早（不经过 Windows 启动项延迟）。"""
    return os.path.exists(_STARTUP_AUTOSTART_CMD)


def set_autostart(enable):
    """开启/关闭登录级自启（通过「启动」文件夹的 .cmd 启动器）。免管理员。返回 (ok, 说明)

    实现：写/删一份指向本程序 exe 的 .cmd 启动器到用户的 Startup 文件夹。
    登录后由 Windows 自动执行，启动器调用 `CampusLogin.exe --daemon`，
    daemon 单实例保护拦重复。"""
    if not enable:
        try:
            os.remove(_STARTUP_AUTOSTART_CMD)
        except OSError:
            pass
        return (not autostart_enabled()), "已关闭开机自启"
    try:
        os.makedirs(os.path.dirname(_STARTUP_AUTOSTART_CMD), exist_ok=True)
    except OSError as e:
        return False, "创建启动目录失败：%s" % e
    # 写 .cmd 启动器：用 8.3 短路径规避中文/空格在 cmd 解析异常
    try:
        if getattr(sys, "frozen", False):
            exe = _short_path(sys.executable)
        else:
            exe = _short_path(sys.executable)
        content = '@echo off\r\nstart "" "%s" --daemon\r\n' % exe
        with open(_STARTUP_AUTOSTART_CMD, "w", encoding="gbk") as f:
            f.write(content)
    except OSError as e:
        return False, "写入启动器失败：%s" % e
    if not autostart_enabled():
        return False, "启动器写入后仍不可见"
    return True, "已开启开机自启（登录 Windows 后自动运行）"


def boot_task_enabled():
    """开机级自启（需管理员）是否已开启。

    权威来源是本程序自维护的 boot_task.json 状态文件（创建/删除成功时由提权进程写入）。
    为什么不用 schtasks /query 作主判据：Windows Task Scheduler 服务在不同完整性级别间存在
    可见性差异，非管理员父进程的 /query 往往查不到刚由管理员创建的 SYSTEM 任务——若以它
    为准，指示灯/勾选框会被反复刷回"未开启"（v1.2.9/1.2.10 持续出现的表象）。
    状态文件缺失（首次使用 / 旧版升级尚未写入）时才回退一次 schtasks 查询。
    """
    try:
        with open(BOOT_TASK_STATE, "r", encoding="utf-8") as f:
            return bool(json.load(f).get("enabled", False))
    except Exception:
        pass
    return _task_exists(BOOT_TASK)


def set_boot_task(enable):
    """开启/关闭开机级自启：开机即启动、锁屏状态下也运行。

    需要管理员权限；非管理员进程调用会返回失败，由调用方负责 runas 提权后重入。
    用 /ru SYSTEM 而非存储用户密码：密码变更不会导致任务失效，也不用存明文口令。
    任务创建/删除成功（schtasks 双重验证通过）后，把开关状态写入 boot_task.json，
    供父进程（非管理员）的 UI 查询——避免 schtasks 跨完整性级别查询不可靠导致状态闪烁。
    返回 (ok, 说明)
    """
    if not enable:
        _task_delete(BOOT_TASK)
        ok = not _task_exists(BOOT_TASK)
        if ok:
            _save_boot_state(False)
        return ok, "已关闭极速启动"
    if not is_admin():
        return False, "需要管理员权限"
    cmd = ["schtasks", "/create", "/tn", BOOT_TASK, "/tr", daemon_command(),
           "/sc", "ONSTART", "/ru", "SYSTEM", "/f"]
    rc, out = _run_cmd(cmd)
    if rc != 0:
        return False, out
    # 二次验证：schtasks /create 可能返回 0 但任务实际未生效（受限环境/策略拒绝）
    # 只有真正查询到任务存在，才避免“提示已开启但勾选消失/图标不亮”的误报。
    if _task_exists(BOOT_TASK):
        _save_boot_state(True)
        return True, "已开启极速启动（开机即运行，锁屏时也在后台）"
    # 把 schtasks 的真实输出一并带出，便于定位（路径/权限/策略/安全软件拦截等均会体现在 out 里）
    detail = ("；schtasks 原始输出：%s" % out) if out else ""
    return False, "计划任务创建失败：schtasks 返回成功但任务不存在；%s" % detail


# 提权子进程把「极速启动」创建/删除结果写到这里，供非管理员父进程读取。
# 用用户级临时目录（父子进程同为当前用户，均可读写），避免安装到
# Program Files 时父进程（非管理员）无写权限的问题。
_ELEVATED_RESULT_FILE = os.path.join(tempfile.gettempdir(), "campus_autologin_boot_result.json")


def _elevated_target(arg):
    """返回 (exe, params)：以管理员身份重启动本程序并带上 arg（供 UAC 提权）。

    结果文件路径由固定的 _ELEVATED_RESULT_FILE 约定，父子进程都读写同一个文件，
    不再通过环境变量或命令行参数传递——因为 runas 提权后的子进程不会继承父进程
    在运行时设置的环境变量，命令行传含中文/空格的路径又容易解析出错。"""
    if getattr(sys, "frozen", False):
        # 打包成 exe：直接以自身 + 参数提权
        return sys.executable, arg
    # 源码模式：优先用 python.exe（pythonw 无控制台，UAC 提权后看不到回显）
    exe = sys.executable
    py = os.path.join(os.path.dirname(exe), "python.exe")
    if os.path.exists(py):
        exe = py
    return exe, '"%s" %s' % (os.path.join(APP_DIR, "gui.py"), arg)


def elevate_self(arg):
    """以管理员身份重新启动本程序并带上指定参数（UAC 提权）。
    返回 True 表示已成功发起提权请求（调用方随后应自行退出）。"""
    try:
        import ctypes
        exe, params = _elevated_target(arg)
        rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, APP_DIR, 1)
        return rc > 32   # ShellExecute 返回值 >32 表示成功
    except Exception:
        return False


def run_elevated_and_wait(arg, timeout_ms=90000):
    """以管理员身份运行本程序并带上 arg，阻塞等待其结束，并读取结果文件。

    专用于「极速启动」这类：需要管理员权限、且必须拿回真实执行结果（成功 /
    schtasks 报错 / 用户取消 UAC）的场景。与 fire-and-forget 的 elevate_self
    不同，这里用 ShellExecuteEx + WaitForSingleObject 真正等到子进程退出。

    结果通过【固定文件】_ELEVATED_RESULT_FILE 在父子进程间传递：不再用环境变量
    （runas 提权后的子进程不继承父进程运行时设置的环境变量，会导致读不回结果）、
    也不再每次用随机文件名（容易与子进程写回的路径对不上）。

    返回 (launched, exit_code, result)：
      - launched=False  → 用户拒绝 UAC 或提权失败（子进程根本没启动）
      - launched=True   → 子进程已退出，exit_code 为其退出码（0=成功）
      - result          → 子进程写回的结果字典（读不到则为 None）
    """
    # 先清掉上次可能残留的结果文件，避免读到陈旧结果
    try:
        os.remove(_ELEVATED_RESULT_FILE)
    except Exception:
        pass
    try:
        import ctypes
        from ctypes import wintypes
        exe, params = _elevated_target(arg)

        class SHELLEXECUTEINFOW(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD),
                ("fMask", wintypes.ULONG),
                ("hwnd", wintypes.HWND),
                ("lpVerb", wintypes.LPCWSTR),
                ("lpFile", wintypes.LPCWSTR),
                ("lpParameters", wintypes.LPCWSTR),
                ("lpDirectory", wintypes.LPCWSTR),
                ("nShow", ctypes.c_int),
                ("hInstApp", wintypes.HANDLE),
                ("lpIDList", ctypes.c_void_p),
                ("lpClass", wintypes.LPCWSTR),
                ("hKeyClass", wintypes.HKEY),
                ("dwHotKey", wintypes.DWORD),
                ("hIconOrMonitor", wintypes.HANDLE),
                ("hProcess", wintypes.HANDLE),
            ]

        SEE_MASK_NOCLOSEPROCESS = 0x00000040
        sei = SHELLEXECUTEINFOW()
        sei.cbSize = ctypes.sizeof(sei)
        sei.fMask = SEE_MASK_NOCLOSEPROCESS
        sei.hwnd = None
        sei.lpVerb = "runas"
        sei.lpFile = exe
        sei.lpParameters = params
        sei.lpDirectory = APP_DIR
        sei.nShow = 1
        if not ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(sei)):
            # 返回 0 = 用户取消 UAC 或提权失败
            return False, None, None
        h_proc = sei.hProcess
        ctypes.windll.kernel32.WaitForSingleObject(h_proc, timeout_ms)
        code = wintypes.DWORD()
        ctypes.windll.kernel32.GetExitCodeProcess(h_proc, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(h_proc)
        # 读固定结果文件（父子进程共用同一路径 _ELEVATED_RESULT_FILE）
        res = read_elevated_result()
        return True, code.value, res
    except Exception:
        return False, None, None


def _result_file(path=None):
    """结果文件路径：显式 path 优先；否则用固定文件 _ELEVATED_RESULT_FILE。

    早期版本用过环境变量 CAL_RESULT_FILE / 随机文件名在父子进程间传结果，但
    runas 提权后的子进程不继承父进程运行时设置的环境变量，导致父进程读不回结果、
    前端永远落到“真实状态验证不符”的兜底提示。现统一用固定文件，父子都读写同一路径。"""
    if path:
        return path
    return _ELEVATED_RESULT_FILE


def write_elevated_result(ok, msg, path=None):
    """提权子进程把执行结果写到固定文件，供父进程（非管理员）读取。

    path 为 None 时写到固定的 _ELEVATED_RESULT_FILE（父子进程共用同一路径）。"""
    path = _result_file(path)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"ok": bool(ok), "msg": msg}, f, ensure_ascii=False)
    except Exception:
        pass


def read_elevated_result(path=None):
    """读取提权子进程写入的结果（读后删除）。读不到返回 None。

    path 为 None 时读取固定的 _ELEVATED_RESULT_FILE。"""
    path = _result_file(path)
    try:
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        try:
            os.remove(path)
        except Exception:
            pass
        return data
    except Exception:
        return None
