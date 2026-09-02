# -*- coding: utf-8 -*-
"""
gui.py —— 校园网自动登录 设置界面
================================================
- 源码模式：双击 启动设置.bat 运行本文件；优先用 pywebview 打开原生桌面窗口，
  失败时兜底打开浏览器设置页（127.0.0.1:8765，仅本机可访问）。
- exe 模式：桌面 App 主界面（见 app.py 入口）。

页面功能：
  - 修改账号 / 密码 / 服务 / 检测间隔 / 认证服务器
  - 极速启动开关（计划任务：开机即启动、锁屏时也在后台运行，需管理员授权一次）
  - 立即测试登录（探测 + 掉线时走完整登录流程，显示服务器原始响应）
  - 启动 / 停止后台服务、查看实时日志
  - 30 分钟无操作自动退出，不留后台残留
"""

import json
import logging
import os
import secrets
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import campus_core as core  # noqa: E402
APP_DIR = core.APP_DIR

# ----------------------------------------------------------------------
# 本地端口：动态分配，不再固定 8765
# ----------------------------------------------------------------------
# 问题背景（用户反馈"本地部署了其他网站，打开本软件却显示那个网站的内容"）：
#   Windows 上 SO_REUSEADDR 的语义是"允许抢占已被绑定的端口"（与 Unix 的
#   TIME_WAIT 复用语义不同）。而 Python 的 HTTPServer 默认 allow_reuse_address=1。
#   于是当用户本地部署的其他网站先占用了同一端口时，本程序"绑定成功"不报错，
#   但发往该端口的连接被先绑定者抢走，WebView2 渲染出来的就成了别人的网站。
# 三重对策：
#   ① 端口交给系统分配（bind 到 0），从根本上不与任何本地站点冲突；
#   ② 关闭 SO_REUSEADDR，真冲突时能立刻报错，而不是静默串台；
#   ③ 每次启动生成随机令牌，页面与所有接口都校验，杜绝被其他站点顶替。
GUI_PORT_FILE = os.path.join(core.APP_DIR, "gui.port")
GUI_URL_FILE = os.path.join(core.APP_DIR, "gui.url")   # 纯文本 URL，供 启动设置.bat 直接读取
_ACCESS_TOKEN = secrets.token_hex(16)   # 每次启动随机，仅本进程知晓

log = logging.getLogger("gui")
_last_activity = time.time()
_main_window = None  # /api/quit 用来关闭 webview 窗口，避免直接 os._exit 导致 _MEI 临时目录无法清理
_QUITTING = False    # 标记程序是否已进入退出流程；进入后关闭回调不再拦截/弹窗


# ----------------------------------------------------------------------
# 本地服务的单实例管理
# ----------------------------------------------------------------------
def _instance_url(port):
    return "http://127.0.0.1:%s/?k=%s" % (port, _ACCESS_TOKEN)


def _write_port_file(port):
    """记录本实例端口，供 启动设置.bat 与其他实例复用。返回本实例 URL"""
    url = _instance_url(port)
    try:
        with open(GUI_PORT_FILE, "w", encoding="utf-8") as f:
            json.dump({"port": port, "token": _ACCESS_TOKEN, "pid": os.getpid()}, f)
        # 单独写一份纯文本 URL，批处理脚本无需解析 JSON 即可直接取用
        with open(GUI_URL_FILE, "w", encoding="utf-8") as f:
            f.write(url)
    except Exception as e:
        log.warning("[启动] 写入端口文件失败：%s", e)
    return url


def _read_port_file():
    try:
        with open(GUI_PORT_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _clear_port_file():
    for p in (GUI_PORT_FILE, GUI_URL_FILE):
        try:
            if os.path.exists(p):
                os.remove(p)
        except Exception:
            pass


def _api_is_ours(port, token):
    """确认该端口上的服务确实属于本程序。

    仅"能连上"是不够的：用户本地部署的其他网站也可能对 /api/status 返回 200
    （例如 SPA 开发服务器把所有路径都回落到 index.html），因此必须校验身份标识。
    """
    try:
        import urllib.request
        url = "http://127.0.0.1:%s/api/status?k=%s" % (port, token)
        req = urllib.request.Request(url, headers={"Cache-Control": "no-cache"})
        with urllib.request.urlopen(req, timeout=1.5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return isinstance(data, dict) and data.get("app_name") == core.APP_NAME
    except Exception:
        return False


def _focus_existing():
    """已有设置窗口在运行时把它提到前台复用。返回 True 表示本实例应直接退出。"""
    d = _read_port_file()
    if not d or not _api_is_ours(d.get("port"), d.get("token")):
        return False
    try:
        import ctypes
        user32 = ctypes.windll.user32
        user32.FindWindowW.restype = ctypes.c_void_p
        user32.FindWindowW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]
        hwnd = user32.FindWindowW(None, "校园网自动登录 - 设置")
        if hwnd:
            if user32.IsIconic(hwnd):
                user32.ShowWindow(hwnd, 9)      # SW_RESTORE
            user32.SetForegroundWindow(hwnd)
        else:
            # 原生窗口没找到（源码模式可能是浏览器页面），直接把页面再打开一次
            import webbrowser
            webbrowser.open(_instance_url(d["port"]))
    except Exception:
        pass
    log.info("[启动] 已有设置窗口在运行（端口 %s），已将其置前", d.get("port"))
    return True


# ----------------------------------------------------------------------
# 开机自启（委托 campus_core 的计划任务双轨实现）
# ----------------------------------------------------------------------
def autostart_enabled():
    """登录级自启是否已开启（免管理员）"""
    return core.autostart_enabled()


def set_autostart(enable):
    """开启/关闭登录级自启（免管理员）"""
    ok, msg = core.set_autostart(enable)
    if not ok:
        raise OSError(msg)
    log.info("[自启] 登录级（登录后立即启动）-> %s", "开启" if enable else "关闭")


def boot_task_enabled():
    """开机级自启是否已开启（开机即启动、锁屏时也运行）"""
    return core.boot_task_enabled()


def set_boot_task(enable):
    """开启/关闭开机级自启。返回 (ok, 说明)

    开机级任务以 SYSTEM 身份运行，创建/删除都需要管理员权限。
    当前进程不是管理员时，弹 UAC 提权重启自身处理，界面只需提示用户确认。
    """
    ok, msg = core.set_boot_task(enable)
    if ok:
        log.info("[自启] 开机级（开机即启动、锁屏也运行）-> %s", "开启" if enable else "关闭")
        return True, msg
    if not core.is_admin():
        arg = "--enable-boot-task" if enable else "--disable-boot-task"
        if core.elevate_self(arg):
            return True, "已请求管理员授权，请在弹出的 UAC 窗口中确认"
        return False, "无法获取管理员授权"
    return False, msg


# ----------------------------------------------------------------------
# API 实现
# ----------------------------------------------------------------------
def api_status():
    cfg = core.load_config()
    st = core.read_status()
    username = cfg.get("username", "")
    campus_ssid = cfg.get("campus_ssid", "").strip()
    cur_ssid = core.get_connected_ssid()
    wifi_status = "off"  # off / wrong / right / unknown
    if cur_ssid:
        if campus_ssid and cur_ssid == campus_ssid:
            wifi_status = "right"
        elif campus_ssid:
            wifi_status = "wrong"   # 连着 WiFi 但不是校园网
        else:
            wifi_status = "other"   # 任意 WiFi
    return {
        "app_version": core.APP_VERSION,
        "app_name": core.APP_NAME,
        "state": st.get("state", "未知"),
        "updated_at": st.get("updated_at", "--"),
        "last_login": st.get("last_login", "--"),
        "last_error": st.get("last_error", ""),
        "login_count": st.get("login_count", 0),
        "daemon_running": core.is_daemon_running(),
        "autostart": autostart_enabled(),
        "boot_task": boot_task_enabled(),   # 开机级自启（开机即启动、锁屏也运行）
        "is_admin": core.is_admin(),        # 前端据此决定是否需要提示 UAC
        "username": username,
        "username_masked": core.mask_username(username),
        "has_password": bool(core.get_password(cfg)),
        "service": cfg.get("service", core.DEFAULT_SERVICE),
        "interval": cfg.get("interval", 15),
        "portal_base": cfg.get("portal_base", core.DEFAULT_PORTAL_BASE),
        "campus_ssid": campus_ssid,
        "wifi_ssid": cur_ssid or "",
        "wifi_status": wifi_status,
    }


def api_save(data):
    cfg = core.load_config()
    if "username" in data:
        cfg["username"] = str(data.get("username", "")).strip()
    if "password" in data and str(data.get("password", "")) != "":
        core.set_password(cfg, str(data["password"]))
    if "service" in data:
        cfg["service"] = str(data.get("service", "")).strip() or core.DEFAULT_SERVICE
    if "interval" in data:
        try:
            cfg["interval"] = max(5, int(data["interval"]))
        except (ValueError, TypeError):
            return {"ok": False, "error": "检测间隔必须是数字"}
    if "portal_base" in data:
        cfg["portal_base"] = str(data.get("portal_base", "")).strip() or core.DEFAULT_PORTAL_BASE
    if "campus_ssid" in data:
        cfg["campus_ssid"] = str(data.get("campus_ssid", "")).strip()
    core.save_config(cfg)
    log.info("设置已保存：用户名=%s 服务=%s 间隔=%ss 校园网SSID=%s",
             cfg["username"], cfg["service"], cfg["interval"], cfg.get("campus_ssid", ""))
    return {"ok": True}


def api_test():
    """探测 + 必要时登录。返回给前端的完整结果"""
    result = {"online": False, "detail": "", "login_tried": False,
              "response": "", "message": "", "ok": False}
    cfg = core.load_config()
    username = cfg.get("username", "")
    password = core.get_password(cfg)
    service = cfg.get("service", "")

    r = core.probe(cfg.get("probe_urls"))
    result["detail"] = r.detail
    result["online"] = bool(r.online)

    if r.online:
        result["message"] = "当前已联网，无需登录。如需测试完整流程：断开 WiFi 重连（或在自助服务页下线本机）后再点测试。"
        return result
    if not r.query_string:
        result["message"] = "未联网且未捕获到门户跳转（WiFi 可能未连接）。"
        return result
    if not username or not password:
        result["message"] = "请先填写并保存用户名、密码，再测试登录。"
        return result

    result["login_tried"] = True
    log.info("[网页测试] 检测到未联网，开始测试登录（用户名=%s）", username)
    ok, resp_text, data, code, err = core.eportal_login(
        r.portal_base or cfg.get("portal_base", ""), r.query_string, username, password, service)
    result["response"] = "HTTP %s\n%s" % (code, resp_text[:600])
    result["ok"] = ok
    if ok:
        time.sleep(2)
        r2 = core.probe(cfg.get("probe_urls"))
        result["online"] = bool(r2.online)
        result["message"] = "登录成功，复测：%s" % ("已联网" if r2.online else "仍未联网")
        log.info("[网页测试] 登录成功，复测=%s", r2.detail)
    else:
        msg = data.get("message", "") if isinstance(data, dict) else ""
        result["message"] = "登录失败：%s" % (msg or "详见服务器响应")
        log.warning("[网页测试] 登录失败：%s", resp_text[:300])
    return result


def api_log():
    logfile = os.path.join(core.LOG_DIR, "daemon-%s.log" % datetime.now().strftime("%Y-%m-%d"))
    if not os.path.exists(logfile):
        return {"log": ""}
    with open(logfile, "r", encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
    return {"log": "".join(lines[-150:])}


# ----------------------------------------------------------------------
# HTTP 服务
# ----------------------------------------------------------------------
class LocalServer(ThreadingHTTPServer):
    """本地设置服务。

    关键：关闭 SO_REUSEADDR。
    在 Windows 上开启该选项意味着"允许抢占别人已绑定的端口"，本程序会以为自己
    绑定成功，实际连接却被真正的占用者（用户本地部署的其他网站）截走。
    关掉之后，端口真被占用时会立刻抛 OSError，我们能明确报错而不是静默串台。
    """
    allow_reuse_address = False
    daemon_threads = True


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # 静默，不写控制台
        pass

    def _authorized(self):
        """校验随机令牌：确认是本程序自己的页面在访问，而非本地其他站点"""
        q = parse_qs(urlsplit(self.path).query)
        return q.get("k", [""])[0] == _ACCESS_TOKEN

    def _deny(self):
        """令牌不匹配时返回明确的拒绝页，绝不渲染成其他内容"""
        body = (
            "<!DOCTYPE html><html lang='zh-CN'><meta charset='utf-8'>"
            "<title>403 拒绝访问</title>"
            "<body style='font-family:Microsoft YaHei;padding:40px;line-height:1.8'>"
            "<h3>403 拒绝访问</h3>"
            "<p>此地址仅限「校园网自动登录」程序自身访问。</p>"
            "<p>请直接双击桌面快捷方式打开本程序。</p>"
            "</body></html>").encode("utf-8")
        self.send_response(403)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        global _last_activity
        _last_activity = time.time()
        path = urlsplit(self.path).path
        if path == "/" or path.startswith("/index"):
            if not self._authorized():
                return self._deny()
            # 注入本次启动的随机令牌，页面内所有请求会自动带上
            body = PAGE_HTML.replace("__TOKEN__", _ACCESS_TOKEN).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            # 强制禁用缓存：每次启动都取最新 HTML（避免 WebView2 缓存旧主题/旧 logo）
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/status":
            if not self._authorized():
                return self._json({"error": "forbidden"}, 403)
            self._json(api_status())
        elif path == "/api/log":
            if not self._authorized():
                return self._json({"error": "forbidden"}, 403)
            self._json(api_log())
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        global _last_activity
        _last_activity = time.time()
        path = urlsplit(self.path).path
        if not self._authorized():
            return self._json({"ok": False, "error": "forbidden"}, 403)
        try:
            length = int(self.headers.get("Content-Length", "0"))
            data = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        except Exception:
            return self._json({"ok": False, "error": "请求体解析失败"}, 400)

        if path == "/api/save":
            self._json(api_save(data))
        elif path == "/api/test":
            self._json(api_test())
        elif path == "/api/daemon/start":
            ok, msg = core.start_daemon()
            self._json({"ok": ok, "message": msg})
        elif path == "/api/daemon/stop":
            ok, msg = core.stop_daemon()
            self._json({"ok": ok, "message": msg})
        elif path == "/api/boottask":
            # 开机级自启：开机即启动、锁屏状态下也在后台运行（需管理员授权）
            ok, msg = set_boot_task(bool(data.get("enable", False)))
            self._json({"ok": ok, "message": msg})
        elif path == "/api/openlog":
            try:
                os.makedirs(core.LOG_DIR, exist_ok=True)
                os.startfile(core.LOG_DIR)
                self._json({"ok": True})
            except OSError as e:
                self._json({"ok": False, "error": str(e)})
        elif path == "/api/quit":
            self._json({"ok": True})
            global _QUITTING
            _QUITTING = True  # 进入退出流程：关闭回调不再拦截/弹窗，让窗口正常销毁
            # 先 destroy 窗口让界面立即消失，再稍等 WebView2 子进程正常退出，最后兜底 os._exit。
            # 本项目为 onedir 部署，运行依赖位于安装目录（非临时目录），不会触发 _MEI 退出警告。
            def _quit():
                try:
                    if _main_window is not None:
                        _main_window.destroy()
                except Exception:
                    pass
                time.sleep(0.4)
                os._exit(0)
            threading.Thread(target=_quit, daemon=True).start()
        else:
            self._json({"error": "not found"}, 404)


# ----------------------------------------------------------------------
# 前端页面（单文件内嵌，无外部依赖）
# ----------------------------------------------------------------------
PAGE_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>校园网自动登录 - 界面预览 v2</title>
<style>
  /* === 主色：冷色蓝白（呼应新图标：黑发 + 蓝灰围巾 + 白蓝冰晶背景） ===
     强调色用偏冷的"霜蓝"作主，原强调按钮的粉色改为呼应原图眼睛的绿色。 */
  :root {
    --bg-solid: #eef4fa;
    --card: #ffffff;
    --text: #1a2433;          /* 深海蓝灰，呼应发色 */
    --muted: #6b7a8f;
    --line: #d4dde8;
    --line2: #b9c4d2;
    --accent: #2c5784;        /* 钢蓝 / 海军蓝 */
    --accent-soft: #dbe7f3;
    --pink: #16a34a;          /* 强调色改绿色（呼应原图眼睛）；变量名保留以减少改动面 */
    --pink-soft: #e2f4ea;
    --ok: #16a34a;
    --ok-soft: #e7f7ec;
    --warn: #d97706;
    --warn-soft: #fdf3e2;
    --code-bg: #14192a;
    --code-fg: #d8e3d8;
    --err: #c0392b;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg-solid: #101822;
      --card: #1a2330;
      --text: #e6ecf2;
      --muted: #8696a8;
      --line: #2a3548;
      --line2: #354055;
      --accent: #5e8cc7;
      --accent-soft: #1d2c40;
      --pink: #4ade80;
      --pink-soft: #143623;
      --ok: #4ade80;
      --ok-soft: #143623;
      --warn: #fbbf24;
      --warn-soft: #3a2c12;
      --code-bg: #0e0f1c;
      --code-fg: #cfe0d2;
      --err: #f87171;
    }
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  html, body { height: 100%; overflow: hidden; }
  body {
    font-family: "Microsoft YaHei", system-ui, -apple-system, "Segoe UI", sans-serif;
    background: var(--bg-solid); color: var(--text); line-height: 1.55; font-size: 14px;
  }
  .app { height: 100vh; max-width: 1180px; margin: 0 auto;
         display: grid; grid-template-rows: auto 1fr auto; padding: 14px 16px; gap: 12px; }
  .header {
    display: flex; align-items: center; gap: 14px;
    padding: 10px 16px;
    background: linear-gradient(95deg, #f4f9fd 0%, #e3eef7 50%, #d5e6f3 100%);
    border: 1px solid var(--line); border-radius: 14px;
    box-shadow: 0 1px 3px rgba(60,100,160,.08);
  }
  .header img { width: 56px; height: 56px; border-radius: 12px; flex-shrink: 0; }
  .header .title { font-size: 19px; font-weight: 600; letter-spacing: .5px; }
  .header .sub { color: var(--muted); font-size: 12px; margin-top: 2px; }
  .header .ver { background: var(--accent-soft); color: var(--accent);
    font-size: 12px; font-weight: 600; padding: 4px 10px; border-radius: 999px; }
  /* 头部右侧：版本号 + 开源地址 / 联系邮箱 */
  .header .hdr-right { margin-left: auto; align-self: flex-start;
    display: flex; flex-direction: column; align-items: flex-end; gap: 4px; }
  .header .hdr-links { display: flex; align-items: center; gap: 8px;
    font-size: 11px; color: var(--muted); white-space: nowrap; }
  .header .hdr-links a { color: var(--accent); text-decoration: none; }
  .header .hdr-links a:hover { text-decoration: underline; }
  .main { display: grid; grid-template-columns: 1.05fr 1fr; gap: 14px; min-height: 0; }
  .col { display: flex; flex-direction: column; gap: 12px; min-height: 0; }
  .card {
    background: var(--card); border: 1px solid var(--line); border-radius: 14px;
    padding: 13px 16px; box-shadow: 0 1px 3px rgba(40,80,130,.05);
    display: flex; flex-direction: column; min-height: 0;
  }
  .card h2 {
    font-size: 14px; font-weight: 600; color: var(--text);
    padding-left: 10px; border-left: 3px solid var(--accent);
    margin-bottom: 8px; flex-shrink: 0;
  }
  .card.test h2 { border-left-color: var(--pink); }
  .card.daemon h2 { border-left-color: #f59e0b; }
  .card.status h2 { border-left-color: var(--warn); }
  .card.log h2 { border-left-color: var(--pink); }
  /* 左列：账号自然高度 + 测试填满剩余 */
  .card.account { flex: 0 0 auto; min-height: 0; }
  .card.test { flex: 1 1 auto; min-height: 0; }
  .acct-row { display: flex; align-items: center; gap: 10px; margin-bottom: 8px; flex-shrink: 0; }
  .acct-badge { display: inline-flex; align-items: center; gap: 6px;
    font-size: 12px; padding: 4px 10px; border-radius: 999px;
    background: var(--accent-soft); color: var(--accent); }
  .row { display: flex; align-items: center; gap: 10px; margin: 5px 0; }
  /* 只约束行首的字段名 label（直接子元素）；否则会误伤选项用的 <label class="chk">，
     把复选框文字压进 96px 里导致每行只挤两三个字 */
  .row > label { width: 96px; font-size: 13px; color: var(--muted); flex-shrink: 0; }
  .row input[type=text], .row input[type=password] {
    flex: 1; min-width: 0; padding: 7px 11px;
    border: 1px solid var(--line2); border-radius: 8px;
    font-size: 14px; background: var(--card); color: var(--text);
  }
  .chkrow { align-items: flex-start; }
  .chk { display: flex; align-items: flex-start; gap: 6px; font-size: 13px; line-height: 1.5; }
  .chk input { width: 15px; height: 15px; accent-color: var(--accent); flex-shrink: 0; margin-top: 3px; }
  .chkrow > div { flex: 1; min-width: 0; }
  .card .actions { display: flex; gap: 8px; margin-top: 10px; flex-shrink: 0; }
  .btn { padding: 8px 16px; border: none; border-radius: 8px; font-size: 13px;
    cursor: pointer; font-weight: 500; }
  .btn.primary { background: linear-gradient(95deg, var(--accent) 0%, #3a73ab 100%); color: #fff; }
  .btn.pink { background: linear-gradient(95deg, var(--pink) 0%, #15803d 100%); color: #fff; }
  .btn.amber { background: linear-gradient(95deg, #fbbf24 0%, #f59e0b 100%); color: #fff; }
  .btn.warm { background: #94a3b8; color: #fff; }
  .btn.warm-outline { background: transparent; color: #b45309; border: 1.5px solid #f59e0b; }
  .btn.brown-outline { background: transparent; color: #78350f; border: 1.5px solid #92400e; }
  pre { background: var(--code-bg); color: var(--code-fg);
    font-size: 11px; line-height: 1.45; padding: 8px; border-radius: 8px;
    overflow: auto; font-family: Consolas, monospace; white-space: pre-wrap; word-break: break-all; }
  pre.out { background: var(--bg-solid); border: 1px solid var(--line);
    color: var(--text); min-height: 0; flex: 1; }
  /* 右列：运行情况自然 + 后台服务自然 + 日志填满(与左边测试结果一致，随窗口缩放) */
  .card.status { flex: 0 0 auto; }
  .card.daemon { flex: 0 0 auto; }
  .card.daemon .hint { font-size: 12px; color: var(--muted); margin-bottom: 6px; }
  .card.daemon .actions { display: grid; grid-template-columns: repeat(4, 1fr);
    gap: 8px; margin-top: 0; }
  .card.daemon .btn { height: 38px; padding: 0 6px; font-size: 13px; white-space: nowrap; }
  .card.log { flex: 1 1 auto; min-height: 0; }
  .logbar { display: flex; align-items: center; justify-content: space-between; margin-bottom: 6px; flex-shrink: 0; }
  .logbar .l { font-size: 12px; color: var(--muted); }
  .logpre { flex: 1; min-height: 0; }
  .chips { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-bottom: 6px; }
  .chip { background: var(--bg-solid); border: 1px solid var(--line);
    border-radius: 10px; padding: 6px 10px; display: flex; align-items: center; gap: 8px; }
  .chip .ico { width: 24px; height: 24px; border-radius: 7px;
    display: flex; align-items: center; justify-content: center; font-size: 13px; flex-shrink: 0; }
  .chip .k { font-size: 11px; color: var(--muted); }
  .chip .v { font-size: 12px; font-weight: 600; display: flex; align-items: center; gap: 5px; }
  .dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; }
  .dot.on { background: var(--ok); } .dot.off { background: var(--err); } .dot.unk { background: var(--warn); }
  .ico.net { background: var(--accent-soft); color: var(--accent); }
  .ico.wifi { background: var(--accent-soft); color: var(--accent); }
  .ico.ssid { background: var(--ok-soft); color: var(--ok); }
  .ico.daemon { background: var(--ok-soft); color: var(--ok); }
  .ico.auto { background: var(--pink-soft); color: var(--pink); }
  .ico.count { background: var(--warn-soft); color: var(--warn); }
  .lastinfo { font-size: 11px; color: var(--muted); margin-top: 2px; }
  /* 自定义退出模态框（替代丑的浏览器 confirm） */
  .modal { position: fixed; inset: 0; z-index: 1000;
           display: none; align-items: center; justify-content: center; }
  .modal.show { display: flex; }
  .modal-mask { position: absolute; inset: 0;
                background: rgba(15,25,40,.45); backdrop-filter: blur(3px); }
  .modal-box { position: relative; background: var(--card);
               border: 1px solid var(--line); border-radius: 18px;
               padding: 24px 26px; min-width: 360px; max-width: 90%;
               box-shadow: 0 14px 44px rgba(0,0,0,.18); text-align: center;
               animation: modalPop .18s ease-out; }
  @keyframes modalPop { from { opacity: 0; transform: scale(.94); }
                        to { opacity: 1; transform: scale(1); } }
  .modal-icon { font-size: 32px; line-height: 1; margin-bottom: 10px; }
  .modal-title { font-size: 16px; font-weight: 600; color: var(--text);
                 margin-bottom: 4px; }
  .modal-subtitle { font-size: 12px; color: var(--muted); margin-bottom: 16px; }
  .modal-list { display: flex; flex-direction: column; gap: 8px;
                margin-bottom: 18px; text-align: left; }
  .modal-item { display: flex; align-items: center; gap: 10px;
                padding: 10px 12px; border-radius: 10px;
                background: var(--bg-solid); border: 1px solid var(--line); }
  .modal-item .dot { width: 9px; height: 9px; border-radius: 50%; flex-shrink: 0; }
  .modal-item.on .dot { background: var(--ok); box-shadow: 0 0 0 3px var(--ok-soft); }
  .modal-item.off .dot { background: #cbd5e1; }
  .modal-item .txt { flex: 1; font-size: 13px; color: var(--text); }
  .modal-item .tag { font-size: 11px; font-weight: 600; padding: 3px 8px;
                     border-radius: 999px; }
  .modal-item.on .tag { background: var(--ok-soft); color: var(--ok); }
  .modal-item.off .tag { background: #eef2f6; color: #64748b; }
  @media (prefers-color-scheme: dark) {
    .modal-item.off .dot { background: #475569; }
    .modal-item.off .tag { background: #334155; color: #94a3b8; }
  }
  .modal-hint { font-size: 12px; color: var(--muted); line-height: 1.6;
                margin-bottom: 18px; }
  .modal-actions { display: flex; gap: 10px; justify-content: center; }
  .modal-actions .btn { flex: 1; max-width: 140px; }
  .modal-cancel { background: transparent; color: var(--muted);
                  border: 1px solid var(--line2); }
  .modal-cancel:hover { background: var(--bg-solid); }
  .modal-confirm { background: linear-gradient(95deg, var(--pink) 0%, #15803d 100%);
                   color: #fff; }
  /* 页脚：免责声明 */
  .footer {
    padding: 8px 16px;
    background: var(--card);
    border: 1px solid var(--line); border-radius: 14px;
    font-size: 12px; color: var(--muted);
    box-shadow: 0 1px 3px rgba(40,80,130,.04);
  }
  .footer .disclaimer {
    text-align: center; line-height: 1.5;
    font-size: 11px; color: var(--muted);
  }
</style>
</head>
<body>
<div class="app">
  <div class="header">
    <img src="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAQAAAAEACAYAAABccqhmAAEAAElEQVR4nOS9Bbxc1dU+/Jxxue6W3NzojbsQIQnBKW6FAsVLqbe8LW9L5a28tKWFtziF4lDcPUCUJBB3udHr7uNyvt9ae++5ZyZzJQbl/21+h7mZOXPmnC1rL3nWszR8zZrPr+vQgKgGhCNAJALABIR1HZGwjkg0Aj0aRUTXEdXFd3RdA2CDZtIAs7wQfTcURVq6Cdu278SbL76GVKsFoWAEW7dvRVN1Dew2G6Li5wAdCNPXLYCm8z/5ffVq18RrXJO/H2uHnXCU5yQ7byDnHOd7oq4Pqo+TnKPrOswmMyIRE8LhKKwWDRkZaSgZVoZLrrgMs+ZMRdhkQkeXj/s1LcXJP636NNntqD63ArAkOXcgLe6+Expdzy6m1FE1p6Yd6e18pe0/+mb1kM5jT/8LRgGdDh0IRnQEwlFEoxFEI7oQBtDlRJGrM+7paDhtAI2NWXwYDUWR4jRj165dePHFl5BusyMnPRsfL/kIVTXVSLe5gUgULDvoUkYBgCQC4D+9M49zo2cNq4WUbMVqYrw0HjMdZqsZDqcd4VAQFqcT46ZOw9yFczF1+iTk5WSizeNHJBJCWmoKj5Pxkskub5WHfiz3jeMvAA5rYcBp/c8VCsftOY9X0w0ttlsbBppeI5EwIqEA7+D+cBQhFgZy74ibEZphmaoWRDTqRYoTaK6pxD8fvA+2aBQjR47A4qWLUXnwEDLtbuiRiFj8es9EMatdXk9yJH2YE3ROsvO+yntKnN4GoSm+H0Ew0gGTOQq7wwazScehij3Yv2sPNqzfjIM11ch1O2Cz29Ha3sGC3ZTw07098pG2o/nOsTZfSNfp8MoN7T+pkRb1lTde7MkaTSSzeLFoQIR24CjtwlZo9F84DFNE44Uak7H9yloNJpMZuh7GXX//K6LhMObOmYMnHvsXag9WIcOdjijZFpp22M5m/I0BiXTtP+yc43gtpRn1ukWTLCbhSX2l0/hZ0e3xIi0tnTUCq9mEres3wuVyw+m0IOjzonz4SNhNGro6u+BwueCw2RABmWDJb4jejR6h5vVlb8V6wubVEdD1CGlGADKdX71m8JVqALGdvq8mN3ESAGRnm8yA2QZYbRZoNhM0i9iiebH2eZGIsFuDZmSkOfDQgw9j//4D+M6NN+OJJ59E5f6DyHSlQCcfgroeqbFJb1xMbtYO5GtITsb/p5p6RhKGpJYnDBVNHlaSIlKvVh9TR5AMVSdqZiBiZRvK4/UhGAzCpOvw+rqxcd16dLf5UFfTgK07dsFltSI9NRXerm4+1wwN0Zh51/MTSkP4yldQP43M04A0OXiOyMVPrc2n63Tg/08CIE7FP5ImZxupLDYNcFkAl9kKl8UEq6mvS9FnZEvobDqkus34fMUmvPPm27jzT3fisX/9C3v27EF6WhpCEZrFclLT4k9iVah5TrKbz5ECKpJMAHzdTQD5bKFwGD6fDxarVXZKwrYmTzXpJph5yRqWJfcfSVQL9KgmDh18vXAoAn8wgE8WL4a/K4hApxe7du2Hy2xGZmYGPD4vfF4f7KS16Tp5cdg+Vzb6QB4l2aN9mc2UxAhNbCQEWn263kkO7v+XBcARL/rERuaAGbCZAacJcJs0uDUbnGYTTCa9Dy0gCj1qgtmiI+gP4p9334M///p/8NZb72DFZ58hLz0beigidjq+zyT3Ll95oRtGNPE3B2yJfE0aDZnJYsGDjz6EU049BR6PFyaTKfaArC2ZaGfT4fV60drSikAgwBEX6kf+XJwJTYsiEiURqqHL60FXdzf8fj9am5uxffNWaGENeiiMin2VcJpMyM/Ogs/nRbjTAzebZDpPWDVpLYbDNEBpkMRSwZEKkSNpLBj7Ocf4u+QrwP9rAuCodvzemvLrmQCLGXDYNDgtVlhpwqnVGPdTNBEpNBhEbpoVf7vzr1iwcD5qqqvw4jNPoyQrF3rAB+hhgCYnG67xo6J+krZ4WxQwkRSg02RkQm399ELTW2nExr/p9evWSCBGIhFkZGTgwIFDWLp0GZwuJ6Kkx1LTAXJwR/QoLC4Lps6chhtvuQFDhg9hIWAy9egCYkjMHKKlL5pMFrhdbmSmpiI9JR3VNbXYsXMngv4QIsEA9u3bjxRoKMjKRiAcgqerC3Zphiidzmo4yDwkDcHWT3RAl5NehREtCYd1IIsi2sdhcBqrmzAlUwmUo1T6t+j+LfLEL9NheEI3quO26A+7cE9ns52uAb5gFJ3+MHzkvdeNE0/I9uwMF+7+0934/IvP8aMf/hA/+8H3keVOF0JDD0qdns61QydHg1EAJIm56zRYKkohnV3q1uLu09AcxwsrkOy8gZxzNL9HmItoFN0eD9xuN8wmEwsG1UhABKJRfPcn38PZZ5+B7Ox0PProU/jLn+9FblY2TDQehp+JmqIcDsxIz4DN5oDZbEJ2bg6cqSmMuygrK8PkKeWwOKyw2u0YVTaMBWhLYzOcdhvS09MY82Hpw3/WV5y/v0en1uuklYI/salZTrdkJS1VE7+vhELc/RjMK/KdUGTJTtMt2rvkOZFhRNPXbvHHfqDnT+oel8OETJcNKRYLLCZdhvCEty4t1YX/u+tePPHwI5g1ZToeffRRWMw2aGYb9KhyVfeEEpSANrwV3+h9GeNW5xk+Ovz7vV3nsC8M8Jxe7umortXfOTp40Wemp8NCix8aAsEA2+40xLS8//qX/8VVV12K9DQ3f72rswOaZu4ZIqWY0S5ttsJitrBtT9d3p6Syk88fCCAciaKxqQnbt++BBTZ4u704VFPLu3JebiZ8Pj+6u7t58fc1vUguKyWuryPa+yae3DSgC5sO/8xsEgufTFNa0InmRFw/KJtAaQfq/D6W+Ik0C0xfy8XfS7PbgEy3Fek2G2xmHWE9ivR0Fx576FG8/NzzOGvhqVi75nMc3HcAae5URMKRHocVCwv64wiErXFH+Ep9uSewkQZAZkyUQFEa/AE/hg4fjm9fdy3b8eQYHD22nLUCi9WCu/5yD1547lWkpbkQpXiXQUCyumuxIuALIEr4jUAAKS4n/B4v2pqa0d3RgebGZrQ1dWL3jv3ISMlGQ2092ltbYIKOgvxc+LxeeLzepFEJY1OmgTILjOZBb5/ZkpxHf1OgiYNIUuAbz7GZxMIn9Z0WcxIFKrmSJU0FEh7GPuqteYIK1/ofLACOxdZn226gX5X4gNhh6DwLQUpdZqRbLCjKdmPZspW495678etf/RrhqI6DB/YhhZxWIT/tZcL2D1OYipCCdoAcXAOJL6mRDR9FMPpr1Ch05YcIZdHhjUTZFAgFQohCg88fxK3f/QEeffgx3PnHv+KFZ1+EFg7AoUVg0YPQIyHqbHGEg/D7uhAO+BD0e+D3dqO+pgrdne2wm00I+XzwtrUjQDt9Zzc2rN+CQUXFqNi9H5EoiQAgJzcXbW2t7HDsSwiYpU1vnCbqPUsvn5mTnMe+BlLtKQRN/p+o4RzCpZj6dzTG/D8xu1T8Td8dyAKk061WDfUtHfprb757XAWB9nXf9ZM1nTHDGqPKTj7tLJwx/zTccuN3cNUll8Fs0Xg3odvlh2fb3yFeSdQniu9efyRhQJV7Wuv91K8bXJjuOxDnyReN4LzhoB8Ou5s/CPi7EfAHYDKbkJKS0rMw5ZRQXcq+A7n1kellMltht9nZx5DiTmF/gN3mRPHgwSgaPAhWlxNlQ0owe9ZUVFZXY8rUiWKjiERR31CH7Jxc2O32LxcPEDXEe9VGlNBiNr88N5Bsj9DlfBjgjbNfIRDAkk+XYtzo4Rg2fLj2H6MB6JR5M4BGA02HUVawHRmNYOXKlXjsscfYe3yssoTnmEnDg3ffB2sggF/81w/xt7/9EV6fv2cSHgbn6yWe11tLDO4mM/xOdIzpODYtmYLTy8oiNd7psEFHCLoehMPhQGZWJtLT0/nznvGjyIwAEcUchywcNGgmE/RwhME+pJl1e7rR0trGP9re3oru9k7UVFZi2YrPsGffXqSmpmJfxQG+TshiRlZePg5UVSIUCp34xa/LVR1Jol4kaf0Bwrhb+/ILJbumDoZKn3HmGcgrKEFtXZP+HyEAIuSgUJ6T/n7MRPF64UUm7zE1+pt2j+bmZkybOg379u7j92KhpiNs9D36jb0V+/DYo4/h9p//AitWLsXSxR8hMyNLZPLJzpc6QPyQKaeMVAZ6k0Uqxn2YA8fo5U3icEsaBehPSAxEmAxU4CQ5RzdYMgzio8QnQk3GhFzCl3TCVYgvazAjqtN4RmNjGvspTfQwYQR4PPmrhAfo6WuK7VPYL+DzIaqH0dhUj9qaGnz+xRp4Ojpg0UzYsH4bOyI7OzrQ5fHK5CIrCgsKsH3PTkQSNpXj3rSEv/tZuL3eyTHcovJp0/hYHXbk5OUcF637mAQAxSpJ1SEVJ0TptVEhqRIb3SdNgLffeht33XUXGhsbYTabY5+5XC6MHDES1dXVwjt8HNrDjzyCIUOGYvyE8bjvnnuQlZFPmgpIWsXAPrGNSj5FOEBYYYljFRO2XzMAh0NgY4eKC8uVpbAB/0nKQFja+MGkB2Xl6dAUkCF2mKCTvwQ26BGz8KFQqmQs/1q8kIOVxp3UfKfDJcSF2RIzD9iDbqYNQZfAIYoG+NHV5eENwW61obSoGKnuNOzetQ9lQ4egvq4eDqnFpaekso9gR8Wufp2Cx9zMh/ubVItKgRmiTFVapOQsNEBDExNuWChK/0qvv5WgYaioBf9NvxfR4Q1G4fGTuP4KBIBPF1n2xnlPKbuBiDhCChwTjfLgbN68GR3tHVgwfwH++Mc/4qmnnoozB0aNHIWsrCyUjy4XN0bOuKNBrZlMaGlqxdtvvomLLrkQ777/NhprGmGzpCKqyyltzPHt+bY4aPRYCAR7hECfP9rH++Q5lgf9W9mGCht+pEGH49oSNJK4TY41JBKUQWjSnxJ3qFgWgSF0C6CrQPbh42GxWGC2mBAKBVkQuFNSWCOgMGFqShpMtBGQ+1yCC0lAEMBrxNDh6GhvR4rTBbvNCrvVicaGJtjtVjQ1NzMAieZPdmY20lJSsXv/3hMvBHpp4aiY7/QaNQj+RNmRGOrrVceNJq544aumeRM2DJLJYuLDcwxhQtPRLn7jv42TSO2u1Bl0KHXPZrOhqKgI02dMx7333otDhw7B0+2JLXTKF589ZzbcKSKWfDRNmQ1vv/s2q/e5+Xl4+413kZleiEg4BI22sphHS+rwiY0fgMD9uhQC/fRtXwtY/lRMk1b58cbBH+DQ8XekyZEsVt1XLDvp/cowVAycYpy0/G8Shgbmk14bTcnA4YAIw43TuwQBjiKKYCAEi9nMGqDdbkNaahpczhQmDmHPuGZild7j6URbext27t7DTr+KigrUVjexcK+ureX8BGUqlhYPQjQSwYHqyq9ECJgV1qMXnwBkyDDRbdSvz0ENYET6mBXKUP4WI7Lp1Qz4BuiHO2YBQHYHKX/c+pgYYtDpfBFWGzFiBNo72+HxeLBm1RqMGTMGKam0G/TIwUQH4ZE2JUzeevstjCsfiz3bd6OrrYMnm6YJhUtk+gnwT29OGN4BabGG2Y0lhUWSc9QC7+06/IM9wCG1Olmlpt1CnWQ4kuJ5JAMS24ByEz5swSds0nReUmCL8l3I9GpSXeNMl5i6T6FROxBNMqsNz62ZwtA4Rzu5OsEsTNzXJl6k4XCI5YrFakNKaiprfQV5+TCbrTz+BAYiodvQWI9gwIdDlYdQW12L9vY2VFXVwO8JoaOtA51dnXFZiqOHj2TfQUtb65cuBExCgYkDC8Udsr8ShUC/yp8aF9kYkyAjD5QQp/AIugxP6kRfdaT3fiQnK6eDin70tsuoqBgLCrqxsA6b1YaRw0fiueeeQ1VNFc4/7/zDVH3lIDyaxmE9TUNjfRNqqmsQDoax5vM1cDhcCEUCCGoR6BaN04dj6b69X61npMJSaWej7ijuTV0n8aukNkYAv8FkoiMobcm4I5rgdDzCI2SI4wcobKcDAWmukcraG6MPf9DbI+tq5uliBSQJfmu6hnA4DL8/wGp/OBJBJEqRgygsZhMCAT//u6mlCaFISC5o0t3IJwB0dnWgqaEOS5Yu4Yvu3LkLTY0tqDxQhe3bd7Lj0RhdmDZpMrZs24pgqFfr+oQ0Tcb0Y4sjiQBAglnPoKhkK9AokI02gtz1+VBymszKBHjykToGj2hGJ16cdxe5k7G2aPhUknAZvgtoJAJPUCMPNO3077z9Dm778W3Ipgw/7hwz/NxdGqtKNroPssv7IvWILQgVMiA8LL1SOqwcAWNPHIMdP+DhsvSzfSS7jnEnNv5e5Ahuu08TgDrSgLqPCU1274kogIW0OokBSLgoTadwKAwzobdIjrAu3SPMqZFp0Nnlwcjy0SgfOxqNjc0484xFsLosmH/KfIYFUxhSfaemrh6VVZU4acYM5nM0fUmcG2EpaLV+sAMhgx3PO3ri0MWMfNmkACFFmnIGTHKtqU1B/Zs0ApP0NmpHsIsOWANIJllMCgctb44gkQrbnNjYcUvbGyXdsW2AE9I2btgIC+Wl62ZpbwsiEOotkpZE9iMeqA+DTDky1OJXPgHyxCQ6PAZs2PXyU4Y0BJbuFsOrOmSCSUztTmYqGK+TeI5OKDw/EPFDi5Bjz2CeJIteGKMYvWoAhkWvEh74WmLxE6qSNABWppjjT4x9zO6R79sdTs4NENEBcVPG+RsmDodUJ/bt2Y1wMISmpkZUVOxnyHBneye6CD9gUPuLCwtgsZix/8BBXvz/Qfg0JHalsrZiUzFZyqj0tbKvlP4tM1DZ2UiRN9qbpBJ2NFrAgARAbxc0+Chi40oeXBFjP3xdcKiHbaaefPLj1ZQpUVVZBYvFJrL8iCNAOmnMJo3hnDy3lFpmTM00xnd5g5IrSKkKMUeZjBSoQzFfxkHEjrIZnXGJHr3E9/vzAhrPITteJ+1FJsEazZK+popyBnIkwPC8SmKwkKT3gjKd2ni9EHTK/1dmBpMo9HyuG/9PLM4xXdYkYeE946Cy5jasX4/Jkyfh1dfeQDgUxbrP1yI3O4dNgbrGxpg/acqkSdizt4JzBvj6J0AI6Em6eiDfUdNOuUw4fGjcmIyNFj6Z0rTJwhCkipGtCguM04gT1tNAhUC/AqCvC2nxfuBY/JjuWnmsk37HaOMch7FR6h+RS1BeOSWlkABgbd0C2CwaayectEE+ADOppuL1MBA437f8jyaichawVItKwysMXQ9A14LQEeAjSYBPJSPGjuT3bjh6SUXjNUbrLWwIKyYcepIj9jltIdQRoF3WFOdPiAk+g82qGw+6EOEjyBcSVSOsnj3EPAuM96dZKScxLXwSAKSCh8gPwEAghazq6U76YWICZup1TVCI26x2mE3EHiTNRn5uHXabAwcPVcLX7UFxSZEgEz1YhYbGRuTl5CDV7cb+QwfR1t7O15k0cSI+W73qhDkEowlzvlcPkZIUstEoCASFPDhDUmQTGi9A40MLW/ETqI8iJhmgosxDmttqL00WgBnAg/evAcQmUfKPNflAiSQMNMCKfjnZQc4tThbr49pH2jo7O9HR1sYqpdQm45rygrNWb1RRjK/JPJq9NdXzsXAixz7ihEC/LdGk6O2cZJrKEZ0jH86o5cT8CUlsh9j7yfJsjTdNGoUNMFljBROEeUHkqxocUgBrtIWrDBsVbRD2Kjv8RJqtxmo7kbYymUgsDCmAZG6HEyuWr8TcObOxdt06Ohvr167n66S4XCgdNBi1dXXYuWcPCvLymOZt+86drB1+maaAfrRTWqkGMpLA/Whc/ZSURAlEchxIgByrIt2nAAhHdBK+AhqqinD00tTYqsZavrn3g3YXWiYcZTrGp1CDW1tTi67OTliY0KP3Iegr9/rYmxHnNQDp1pfwSTzneLSkzstw/KEb/qbd3KizGi/EGrtVFkuQagQPvOAJ00xSEzMIJVIoyIHFSUaGnZEgwlarRYZshUbXQz8oTEe3zYrWpkZ0tLVjdHk5Vn32OeprmzgzkD63Wa0YO3o0C4sv1q3FjGnTUVVdxWQmx7uZDF599qnIw5hJmEhtPqCWKMCTzFXSGMjnFgs5KgcxjlwL6FMAROQ0poMWbCI4T+9nPnOc2XBEDAeZCHTPHAI7Rr4s9YxfrF3HUGLlD+iNzKMnzNXPhQdyTnJvh0FJTHTrHmVLNA36+7zXYTfaFurfh+lnAOEmmClJGuCHzRRjx5AAsAMWeZjFK3tgjIpHRKBFGTUscSLCxBHU36FAGJ1d3ewkJjxAAJqAKcvxjSCKtFQ3Pl38EaZPm4a9+/airb0TBw4IAJDCkYwtL0debh5WrvoMpaVDsHHzpmPKL0nWtAQ6MnUk8g0kJpgOpPFaIxW/l80xzu96jNk8vX7dq+tE4MoCnW6G1hQt3MQNRPHgqameeL99aqTyDqJ07WN5CJOJHUHLli7lXYJ555I5xaQzitVOykhLVAVijm35nzonyYLqeS7xX7IWi2v3kfmVqH0P5Jxk5/V9jh7L6te0IDQtIA+VBRBvOwhy1J5+SnpPIpDHyUAaTfdkhqgxFCFxDpxgJBsJAiLCFRgF8geE2akVCorXOJo1ORctNgva21rQWF+HcePG4p2330dtVb2hv4XNP6S0FKPLKWzYgL379rGv4CtDCR5Jk8/JTr/jpKn2pQUkFQAEK2Q/jnDK8qApXnNO/DEsevVvaarFO/h6OwwgB2WPq2scaVO5BuvWrsPWDZs4cSQSpZxxiZ01hrR6czom2rnKIZboIMRR3uRhhveAt+vj1OJCAkf22/1NQo6KSFc/karG/hYHzyM5BrQYbKo7pSamRyRVOJV7I4oWmmsaZSFICnDOmyfiUR2+qA5fhKoLpWDxR59gzqyT0NTYiPXrNsLT3UMQol5zsrMxe+YspKS48Ppbbx53LeCwluTShNDrL9iiGstaYhhKCOudSCGQXAMwLlYyA8OCCZceBtInoAphiDhzAtRVzjNODkp2yLRGcWc9vXM0QkA917IVyxEiam+bQ5ih8krGEHXCzyX3BXDcMAmaK8ZH3Ysq0GdTnRPo40icPX15/BI/7+sGlGPySL/XX5N2FEnaaEBECPgg9p8gNDooWkIaF/MuEgGICBPbLIDd0jPJeax4lYgMwZhlorARrCFq8FKOAJGMWsxobW9H5aGDmDVjBt568x20NLXHzQclBCgZ6dILL+GF/8nSJexjSExbPm6tF32aws+qrFxfW4ASkMd78R/xLcfyQOQrHRzlkaEoVqVJKPDJQtirVGDWEghqmkQRSNyDEiWjWiIDHR7OCzeb4ff5sWTJMjhoZkXCCEeZ7At+6AytjS1qMlVlEZFekYDsvOhFa2HWoBBgkdj3GGlAX3cZMZBq9fok8fY3H8HjcCidrS9Ez1E0xaVADj7CdjA3lgadDrbvaQePIqAJ9Z7I18JSn1cFV1T+ggi6SmFEKrwRYiDHmCNGoMIgGh+0mImefMmy5Zg3dw46Wlvx5uvvIByOxHn8jYCim264AR8uXozP163jOXNCNQFjk7a8ctwpDL9Vhvmo64whwS9z8ScVAG0BXSfHXFCCE9TfKs2XsOu+sHgvZt7RgqdcaAO6KdrPXmMUAomNowMDuHk1iBs3bsTe3bvhsNmZsZY5SqQdy5NVCjCB3REFJmhXimlFxkWcuOiNwsAY7FWsIn1upMYPejvJKAqNFQWMRzKY3kDPOcrdLjFEmvhkjCXowe7zU7BzV4ef8+PJZUcZe+J9FQ9R3aU2AlmKJZYzrSIGTLLJkFeNKz/T52Zdg1kml1F2KWUFVuyrwMyZM/Daq6+xKUAef6Otr9R+q9mCU09ZhL3792H152tYUES/LCEQ6zQhDChowjUIJI04oWmZW/AEL/7e0LzxrZc+4U1UxiGt8oYZbCfVG5OBNTXRSZVsDiWblsbzjsR3vnbdFiBshx8OeGHheyAOfpa4JrmfStMjEKXS4gROoeKi0h192P3K/ziElXwFxIBCfZCZis8EBVbvQCAt7rxkAf2ea/QcR3ZO4j0N4L61BCBU4jlq4cd+S+3smlB3NSssmg12sy4OQmUmmeBCKEgBoNR+2hlpKpp0dhBzRSBOzxTFX0hoEETY4jTzjj577hwu77Zr6y4c2l8Jn580rp6mNIHhw4Zh2NChvPg/+OjDr0YIqKay6agZyx19yS3uZ9t6qU3GMVapwliUAFApjlIAsPoSlWGRhAsnqvrG/c74npGpNWmtvcSbN5k42+yzFZ/BbnGwqiiSsjVGl/Huz8yrsoCoZKgl32BI4Nhiz5e09abeD2RjP+wi/QX5+zvnuMUBB9aMikmSg6MDtDtHRIn2QETnV2qKI5/rM8S2fRFNEShE+T6H/2h/p1UuUsekK6An/GxELarf5zTmMOxOOw4cOIC2tjZMnTIFr7zyOkODN23aEufwUxrB4EEl2LVrF2ZOn8F05m+/995XKwS+gkWfqAXE30Ivc4YFlCEEGBNccvKzR5cSgiR2WS3mvqagZgwfyoQGY/yUrhMagPefSkjt3rUDdqeVgK6Ug8YTjLh/ghTKjgihJRhZhD1g1OIHrHap84w04GqBfMl225fa9MMtDPLYK/hxlBKNNPK20K5L6b7CSawZwnIR8smEIwhQbF+mIZNGppFDTy0+BrX3+Gb4J9lcow95q+m5JS43boFNt8FqtmLj5s04+5yzsebzL7B7ZwWKCwuxYcOmOH8AOwTNFgwfOhS79uzBqQtPgc1mxTvvvfvVCoGvuA1IBvWaXChXOKM8E9SYRJMm2SWUHUgtsfvN/aTXqIHdtnUrV4sxE4osViRSrEyGIktvk8i9JtezggmLCHbfD97LjRvVk4HGeAYiJL42gkQ8tJgXlGREZhdh+K0C1y7BgLHFR0yMumAEiuVF0CunuFlFFClGHJLgK1D/k2Ma+22J06D6gps3beayYmPHjMFDDz+GwaWDUFdXj9q6+rjQILXBgwZj566d/N4Zp57GTLufLl0isSRfxwqOx9ZiS5bKE+NIFEfpA+AmnRkkBAxvxa2NI1VIVSSuN1+AGtA1a1YL7YS9xkasO+HKBRTATyFJtb5MYtkrbUHdq3HxCcfU4WCh2F9sTmjikMCipPdo/K83QI2BNrvPcxL+6/M6Rhrufq6VvPUdKhTOezG6VuLyt5Ctb4qlgydehROKdArk9YSNqVEuv9OksSectQqpXSWWWuvB2sr0bE4vluKAMAPBILZu24azzzkLa1atxv5dB3DKwvlYvmwFL2qjACgqLER7ZxfTjkWjUZy+6FQEQmGsXLVShAj/f6AJGM2AmABIfGwFL2YnX6JBL7H8kYQvJhLDGKdQssxb42EMD/JN9uELUOSfBP4ge8/hdCHKCdLy1+VFY/uFAcxn5klngoNCQRxSMmTbyR+K3QMJNM0Ai1bhK3lNTs2MGLJkE6RcnNkcE0x9HP1oWn22I5S0x+ghEHY8J++Qg87g4JPhO2bKjYtDGLD9UlskBUCjzEz+EokjCvbpSTwZRkEU30lcVCQlBatXfY5BxYPgSnHh2Wde5DDhmDHlWL7is5iKTwfhAooLClDf0BDb9c867TQ0Nrdg7fp1TD/+/ydzILa0jflrsYQGmapIr7FuN3CchcjDbhAOjBNQfUeIJvLGK0RXLwd9rkLqMYpxeT8KWpyoBSgBVllZjYaGDljNzj4fsmfqiN2io6MDnZ4uThdW4CS/PJT9qryR5FMM0Xn0rFKQUDSBQbSMWpMhUroOAeEMc0fAgMXBrS90ZH8Ok7435Xh/RD/+RPbsJ0Q44i5vjHAmXkdXCSgiu4/8Kgrzr8vwsJ8YbMMq6YccfqQaOIT5xVaYqIkVJPRfWKD+xCUGGAvT4/+kRU20Ys3NTZg3Zy5efvUV1NfUY8KE8ehsb0dNTU2cPyAzIwOVlZVyjISJcNF556Ni3z7s2r37/1c+AV6+dQFdZx+szPlQ7hbqLxpIiv1LB29ceFmTCybWVTR2sQSPI9xlDAZ/Yg6dsSiLuC/x6YYNm+H1+Fhqx09lw7nqQamiTDCI0tJBuOyqb+L0s89CKCRES8zPZBA+pBEoMiCSBQTP5IgH2bhykcU2bcXWqxmSmyTMNUpQVzYV9KTwYmbUlhrVYTEYtWPqfeT6K8ecoZBJ0vWv7G4SSMp84cwscb8K56FwH+rgfk/kdpDIT+NBwp8EY5wVTbs6J5BIhIu5Z/ycdjsTw4TYmSsffqCaTgIjEl2PbPnlK1Zi3LhxaG1txROPP86nz507B6tWfy74CGTLy8tDfb3IHzAKgcsuugRr161FbW3tl55C/FWZAT1kpr3sKuxBT5aAItlJmCMj4TtGMNARtT68fpFe7H+zRiWqeu4p6SyiewqHubb8I48+gF//4Zf46z/uxIIFC5mlmARIXHiSdnYJcVdYB3JsEYwVUghQ5pdMf49bccyDoOojRCk8FoE/qiOYwJCS1BRK7GODRXMYO1Di38adO9l4yR8hPwNl31E5tqhOXvuo4DgxHAr5yTJLhXRiua89vxmVYDESICw0jQtTrEy5RUvqGqldUYg2GAwhKyeLzYdoJMylwpT3pddm0HCUWSW6QEdKCtGG7URubhamT5uOl158BZUHDiI7JxvFhUVYv2FjrBgNCQAKI1Iz+kqISuz8887D+4s/RFdX11dWZ+BEtkT8B898m2JfkX3MTCdyt+KJL4VA3IVUNMzQP8xTTtl9cifoT5kTk0HS7fUhBAyKBd8oSWdimq3Yuw82i03E/NXsim2DMYAp/59UOqpAlJ2XzWYA2X7X3/RtziFnRhrDbxHwhEOJXIhSqDjG3AXe0Cw6bCYdZsa4EyRZ5n9LX1WiVk+XCRoWTM+h8xGhqkV8xPsklHOTGIzYXiZAjWIyMmgUDMohdCMnQtGiEhyI6ggamIf5WZSPRJp5TgvgkK8E3HFYBICHzRcjhyDZ9qTpEA+SpiOk6QjLSRAXEFGLlRa//EB4+AVRJ9X0I/ae4pJi/neU6jbQUqYEIsPYid+NgQ9iN0HzjPvcInxUNr5mEFu2bMWM6dPRVN+KZ55+mm9l5szp2L93H7q6uoX24XAgxZWKYEjUFuB7l4ud6hRQdODlV15msNH/SwJAOUONgo0FQMzyMlBCEQJLOXP6MSljdr9Sh3lA+okxcnhZspvS5Izr5yTOtNjmI0/cV7EP9bUNsNFWHPMuJb9Lelirzca24IbP1zOMlHaDNetWI6LTxOvxKivBxt8jwBATghrMHrmZcXcpjDc9Lx0E8SR1Xt20yqWW3mvlSAwnUH5zH0gfBKMVpTqtYuaxo7fkKsN5ZK7xdeS1gvLg+DwfogwXPT9RZ3F1GaVpGF9Zi6AlKX0z6t4YRHV4MlfMBaEnYxDqeYs1kDBtKBYEfFQTsgWDSgdxZWE9HIBGtOE9A9czsdSfUs1g/1QEsIflBhaOwmV3Ye3aDSgbWoaCgmK89857qDpwCGaLGZMnTcLKlZ/FFrzVakcwGJ+focBDJcUlmDJpCt55553/Z/wBMdr8xkbs3SuqKCGOxMcgctVuGLOF6USpCRhPZ9NAmgIcFpRfNBuiBbHRTNgS6TsMBlH/lupmbBJFDq+Nph6E2radu5gDQNB/GV3oSYSAknwmE35zx+8x+5TZaOtox6eLP4HdYRflxIWfWgCJGWkmElsinNCiwgp6bPHz40mNR2lP9D+FkqTdnVTTWAKhDF0ll6Q9M9zojzDcfvKvxJ0U//ZhtPSxRBP6Q2Lue1G6jaXTIgTkUQUNDSaJOCeeYozH30hioe7bGCFQvhUKIVqsHMlps7WjtGwI9u7aCatU0/n0WBGnhPCSkliSPJSfgfwAVjvXhQj4/RgxajjWrluN5559Frf/+lcYWT4CGzdtRkNDI/LzcrkuQVj6gIxNRQYmTZ6EhoYGrFq1GrNnnxQrOvt1XvxVVVXYunUrPwsV5KVmovh/b84j6maa0ALWaXhTqp7M5Rb7ESk/VOjMWAEhwXPNUM8EocoZo8bTlBAwaheKURhAfX0THHZHT9w2juQPh0t2Xeda8vXNLXj2yRfxwRsfwaa5YILVME81gWdgG58Wu5j1FGKkuoK0oGn3ZoCR3MH5WwmCk+YnpbsSdDp2aLpgjTEnOwgrr8Fh0vgczhiLY5ghRuOEw8A8w4emsRps42uIw5qQ2Ux9qcJsvTVhv6vIBZF00CKjnHDCVho8MYlFQ9SYG+O9SMhbkpuJ6isaE9LGWlva0dXRheGjRvGurOmc/mMotSXGVv1Hvy0SvWSYhsuQk+S1IxIOYv3atRg9egwGlQzBu++9j8a6Bl4ElDm4dNkKllTRcO/ZJipbkEpx79+3jyMGX3enIPk01q5di1MXnYozzzyTwU8+r1c3qeQexUCq4v9kArDzi8I2CfZm3CBLDzaFyKixkEysJpVg13NWbYIwVcUOVMmAqEEfJy+zSk8m9TVCsFJ/gF/7KoEQ0xoM5oPVakVmZgbS0tOlE8lQJFzWNFQbmyrlpFIBZcBLvNLOCMV4m1AUUpkIqlQXCUUpMOPCbDLqYuKiJWTbR/nVZBIH2fsWM+2UlM0WhVWLwmpShx5/aFFYTFGYNdqpovGMx8bomqLsS6JEMM+DcupJk4KgujoVS2VB24uppXjpkLw6ctxgGJoYkyjXCKyva+CKt4PLhrF/gEhBmZIqdqZAc1otJNysMJORabZBs9CrHZrFztci+37vvv3IyExn1F9HexfeevMNHq+ikkLYLGY2Owg7YmGvbvKmVOQLLrwAn37yKYKB4NdSAKjdv7Ojk+f+hg0b8Nrrr2H23Lkw22zQfKqyqNLoyP6XHH5q7gzoh2Rcny4TZ1mpmB5Jf7kD0i/6pK3bh7bITiljLXmS/LRLNjY14//ueQjvvvUuLAztTXQa9Dg7lIMvzq6UOQF8Wamax2xY5fRW/zApamth7/Tcn+GOpW5PC5mFm8GnwgqLcREYrRXjgzMHlsiI6/kV9fzJQmS92ACxjwXQSYG1qHyfKj4Sw2rIW49lPCtBLbtHefK5QrBGoVZRyruHFFiFCVTxlITnS+zYZM1A/RUMBTF+wjg0NtZzCTCLmSI84tnJ2WozE/VYYrHt+N+ikCJpEWedczo6Orrg9XZi/8FdeOPtt+BOTUFTQwOWLlmKjNwcLFywAFaqStRLU2o/JRBt3boNl156ydfaFKCxjYQiMDOjqMCyMCaD1F7e5S1i52fP8BEmKxnT5WNzPmHTMFLsJxIfJNuRjGaCyOkXF6itqUNnexdCMQ3A8CNy8VPYz+vzxRZ4zw8ZPF2Gz4xzlWk0yPanakI6qdVCTZeRLDkpFf44JNXjAKKUDCNJLJkIIyIBMWEKA+oIE3rSDPEqj7BZFweHTy2IaBZEafHKIwIzvx/W4o8IfWY44j+zIKBr7LgjTZfDkspskYk47Cw08D6okKfqT2ECSNitxcxkH0bfgagJqAZGLn41kMZB7Wvx8zk9yD/yCezetRvFJYPhcrqYIzD+q0IH66vGAl2JSEV37dyD1tY2zJw1C5VVNfjk4095LuQVFCAUDWPP7t28+PvC/yt/QHl5OZ9L9vPX1SmotBcz2fOsaYtnYAGgqIiM/P5HquzQXFBdGRMcejzKUIZ6Y9VN6Xd7c/5rhtCZmkOqtHZNbQOC4SCrp0JV64lpqcWfmZmJ8WPHIRwiL3/PNeNmjyH8p35fyROGCRPOXEYp2K6nsCjP+54rcsiPyS9o0QjdV8BkhR1B3Ux3QE5FpVqrV3FohgPH5RBFPwnwE++C4XsxViU2gJniNTHVp/IkYyWlZJTPHMul8mMG3S9Rpeu1KUFKJo+JCUEpZDd+4gTuV+N8UBdOeklpUgmWKBMniDU01LO3f/rUGXjqyccRDIj7GzJkKDzebvG1fsroUXSCTAuym4l30uuR1YaOeIV8tY2f0zCOhH1JTNuPqb9HU8NTCXuO6St1QOIBOMeDdjwDOpAjZEbHYC/XZLYhgpZyQU8NfmLksto4uB1LJVUnyz9IapOdP2HieBYGidqFSIhJ3mJeeIO2wuhA6QknjZGdcxL/rsMKs+aAiWixmbCKDYEe7zpvolFolAyjR6FFE49I/DGQc/jo6xzqaMmGlNCfcc9tcFzGCf2Y+SMdGUZGpJ5wTE8CBINqSFAY1Ol+F37cgAhNQ4/CZrOjtaUNzU3NmDhxAjmq2B9AxUKE/dJHSFp+QMVGvR4/ujo6UF1ViYsvuQSrVq3B1o1bWDvoaGvBsLJhqDx4qF/nnhpHh9OBGTNn4MMPBZEI4S2+7s0o0+MaDePRVB1Ru7ZS/Y07L+cN0Oe9OF/7+y2+rq7B2+WRw2LgfqP/yV1El97+ffsP4LnnnufKsfG+OVFCjAWDwf6PC1YYygdx4pHyaxlCXRbytjP7DZUYIzXZJNVjY3UdZfdImhu6Z/YEKk+cfN94xIprHMvRU/880ccSo2FOqFcWH6xRXky5M0vYt3DDRAWxSiQgDlaXyBSjQiDmOBBPYm20xH4+/BwRcXE6Xdi1cydSUtMwdPhw3rktZqozcDjJq2pxa1gCjQKBEDZv3oqp06ajbEgZHnr4ISxZvBhTJk/BnJlzsGXLdvndvhczRZ5I7R87dix8Ph8OUuFRMgWMlU2+pq1XM990lIvfZyheyKAXw2CHKbGGQRvJ/QB9Xp8WtgWM8/b5vEwAKXxUBmVMLjwG/lgscLnccSYo7SRURebmG27ExAkT4KUiIioBPbEl2q5SE1AVkriYBd0T+QhYD5A14mmTiqUnJ6jUcZdMthx665CBnHf4Oer34x7FuLvHeUeNl5JqPan3PcF48RnTfSfwN5Nzjnb/2Ao9wmZ0KkpNwGF3Yu3aLzB8+FBkZ2aLiI+8jb5+IcY1QNdwOFFRUYHNGzbgpJNOwieLP4PZ7EBBcTHyi/JZSPAcGIBTT6HnzjzrTKxes/pr6QdI1vp88t7mh2rUBQr3LxhcDDulTCQxdhM72DgLTIb5jqAxo4sJDB8Nhfzo9nT1VADqOSkOdKMAPjEHX1Rn4M+SZUu4ijCFn1j692kPxDsIFbKOtRlJLUYbJcfiiayS+O/J607mgjwIT8DwYrXRyXsj2Ks4whLGi14OPcnRxzlyFcTS6BPHsVfdz9CPLBglz7+xl2mxWxyi+o9J6omsmvezMvsc3J4OVvdKtnfAF8SOnTsxbeZUGSGRNG99zB1jOUNmjPYGYbaa8f3v/wBWkxMrlq3mz+j62dlZ2LRhE/+7vwWtBEBWVhYfuylrUPv6aAHGzUAp6ZH+BIAyA5L1dyQhdZeZfGWcm17J082e5QSSWqYZl0LgSB+AWltbB9uJ5OQxmQWlhx7n3Y+PVav9UJ1nMZtRUbEXLe1tPEEGIoeMzmwtoQQoRwxktiTdAmkBBA22GyDC9sSDwT8a7BTTplci1VCgIEuSIxl4KOk5Ws8rmSiy6qxxA495AxPQfckfXJklidqQKR7mHIsZHmUzdDArHJyQJEy56upqNLU0YuqMKfAHRFSnr3s2JhVylqDNjmVLV2LoqKGYOHkS3nrnTTTUCmDQ5MkT0VDfIG5hAPdPC14AiuZhzZo1Uis5huc+gc24OasSf8YqFF559Kv7KCxA4kKRpnBSpVRVOIlNPBVRkgOkUH9H1FREIRRAiiuFawGwBqA8+TJJiPDtfSnINIAOp5NNBAZJDPDnD3OWGbxmrBnIaIGxGArRY6saCUwmIk0E0XlCWmixgyroJilX3ktx1T7P4VwOjbkKYrh94wP0a3GoRW3Adx82Hgbb/VgWf5J+VnONUniJ2IPAK2kZGRg5eqR0CooS573dumoiGpSBlqY2zju4+JJzUF9fg38/9wIv+NT0NPZdVFfXDCzzT5qXLrcLgwYNimHq/xPNAZXDEducEz5XgqFfAaBYfo3IXiP4KzFkSBOQMDN0UMgxWZeyH0AVhzR+t5d7EDu3+Num++G0m5n6WaXxijmocdy/o7MzzsGXeLB5IItIoo9zjEfcZ7GbVXEnyWWXhJU/Sk4teRCeQCX9xJJ7mGtBNxxafPJPLMFHO+xIfo6OgHwNhqOsgalcjtjC0nr3E/QINwNYylCtp9c+SvJ5f/2o93E/Rtlk0jW4dAu+WLESo8eOQVZeDvM6kD8n6XAYlUEmJI0gHAxj5/bdOPMbp6CkpBgvv/wmJ5NRG1U+Agf2H+S/B4L0U5rC5MmTsWnTpgFrD19F60vOK7KeAfv6Etl9lBkZF0tUvyY1Q7nJHRaKEjZw7xgAY2N7UAMcgjWabS66RiBAxS2lY016fadOm4TTz1iErOxshCPSaZS0Z/qoxHkMW9ZhbjrjylN0dsqjrl65dJY6DJ8ZjwTyjd7PESE7vhZTbPUsiiNyuSSApb7MFmdysgyKwmq3o621HTu3bcfcebPZ8ogygMcAClLKCL8KHAaZeOS1d7kd2LtrLzKysjFu3AS0NXfiiX8+zai4oUPL0FBfL1mmB+4MzM7OZiqytpY2udkcoVPrK27Gjfyovqz8AyYjrZf8nJ0McuFT2uZhC1ru/kZTINk0U7uBwyqSXbgRmQWVj5a7PDUCfpB3/9xzz8T9D92NcRPKEQj4Y4lDJ7wZFllybUFOUPV3X8dAzhngccQL/6tq8iYZjCatoJg3X5aTd6ZkomL3Pvi9Psw4aSZXgIopD3JSsWlE1eEIjCVpxulv8hU1NbbyNc886zTOt2hqasfi9z+F3elgjYIYgoxMxn3erjxn8ODB2Ll7p3jva4oLOOoVoiWgBhVhjOoGo3/A6ENQ4yUy6UQzCg7je/Q9p0040hT6Lhgiq1Zh/IXKSMAfksb33Xc/zjrjLKxYvgIupxsRA5LsK29ycfcqJGTrU5D0cd7XtcVknoGDMKaAxM4S2zsJ+s9Xr0ZhQSGGDS/jhDAz5yjIfogh3aQ5wX4hM0eOujrbUVdVj0WLToHDbUXUFEXloSo2BWbNnMn8kvxLAxAAKvo0uHQw04f9J5sB/bVj2iIVf6DaqZW/gM2CHvM4aYtzKid5j5rNKrzmSp2mXZ9UQqGGJQgLsxmd7X5UHWyCiavMSO74fmyh3jbh/+RzcJTnxJ1nAOcknv9lPlusclASSRa3IVCI1ergBJ+tW7Zh0pTJSEl1IyD9AcbfI9+QoIoX2Z+NjXUIhrtQeWgfMnKyMG3aFKxevZox/iuWrOIM05pqIQAG2uja6enpSEtLY2DZ11YAqKKZcbRcyVovW41FmgCWXkgoGAasQlH9XF690i7gkKEtYyMCB5b4LATE2bHJJktB2x2Ovh8kAZ32tTwn2XkDOcd4nqKG6kM4fBnPppKOOC1bJhsx6lM26ebloFYkGmJk5949u5msY/acmbBZiHiVMjEM1zQ8EYV9CUlI8ODamhp+78wzT2OY8Pad2zFn7kn48P0PmY6M/EoDzfRTmkJqaipamlri3jvadqzfN9azHeiVTF1BwKOSSCRyL2nrY6vRDPDh+KuL7DckMsUkNDUJVSMOAuKmO2woZEVaGnBC8HGpL+PH5PwhadZLQcv/pPaVqe4xd7mOUDgk+nGAhLwnrKmgA+FDpO8oxi5FDk1OtRIBLU0Tr3t27kQgFMHU6VM5/ddEs8VYrUn+Qc69QYPL0N7Rgfb2Dn7WGdNnoKS4GG+88QayctKRmpaGNauIOVgUETmShUgaRk1tzVEvYAXe4rs1zGe6FuEMBtpUnF+F/RQCt7/GDFfEcsON0kePYWIqQWD8PtFp83t9CAH1FvsSKFOwn2wkWvgWkhK97jz4Wix8oSX1RuN7Au9Bqsb5+fnw+wkK2wOv+6q7TqUb60l2HxYFVIDWakNLaxNqqyrhdLsxduwoeL3dMEshQJEioVGIvI8hgwbD5w0ws09HazuKSosxa9ZM7Nu7Fxs2rMcZZ54On8eLL9asHXBcXy1WMgNaWoQGcKSNTNrOzk5OfKqroxT3TvZrUMZhS3MLmhqb0N0lshb7a/oAzcLEJqKpcuEL931y4MBAm2KQVo0c8cynr97oRQjQ/KPF7ySkaS/XZqdfVExep9MZW/9f9aQ9ktaXPT7gUTvGRru+z+9DQUEByoYO41CZQkX+p1iyye5DaAYyQYuIYRrq0NbcjJGjRiA3R7A9U/iTct0F05NIDbc5HOwU7ursQntHO19r3vzZTHDyycdLkJ2XgzPPPgPvv/0uqg5VxyjBBtJSU1KPGgjEpcjCEbS1t3GOS1NzEwsDGhsSLFnZWQxsO5ETnDUA6myCsvJjSJ+AUiP0o/A8K2qxWKNdXTIFq39zyEYyENNBaba08/dlgRGZASEByXJ1OVyxuzhsJ+0PePJVnqN2WiYbUfh2te0N7DrJfm/Azy+p0ZwOJ5rrazC+fAQKC/IYRMUFVmL38+X00eE5gj1zLbGJT4XJQmSwHZ0daG1txt79BzD35HlcX4A+J01A7NBCWJDvgLAhQX8Yba2tfJXJUydhUEkJVixfhcb6esyeN4/teWKZamttGzDxh81uY7/E0TS6x4ysDIwcNRKjy0cztoCo64kMhTRcurbT7TyhUtkUY/GQXPQchpHeBArVKby7wv4bj2AfDofD4MMUypO59Fz5V8YHGTtPmHWp9vdWJYskOjn5yAEYleAQZbrwSz9S8rA+PEaHy7E2QqilpKZynXrFYvRl+C7UMouEwxg3chjC3k4sOnkO0twO6VE30hz/ZzalBVitFjQ1NaKrvQONzQ2YP38eOjs7uE9VXEAImShyc7PR3u6Bt1sQepSWlWLq1EloamzG6s9WITMrE06XAxlZ6XjtlTdY9R4IESgJCRrLo23K6UjJSRmZGUjPTOcciC8rx4AKs/R4/hTHg/ybE7CkeUBCoC+88UCnriYTVDhcKBmHSctR9Qh7Kw4UK2SgiaIgTpe7h6O2n0FSUSYVLYiDun7ZTTrdrGYN5UNKmGosttP0Uh34eDZhG5sYT9Hl6caM6dNQeWAfLrvoAhABGaVZixRpQ62xE9iO6olJU6IsSyoQ6/UgGglxFaCcvByUDR3CoDAWqDrNMxP8Hd0oKRmEmrpqNNbXxS4zecpEvsbijz7lc8eMG4thZWVIS0/Fm2+8BU+3p1+fAJlPxysEqIrekjA4mo45mpESNPdyhagQXFwdLgU7NaSWxsWO5XqKFbowyJNen4Er68hEM3mSoM7q3Qmp5iF1dnt7O1JSXKJGn+GavTW1+Jm2yyTVw+OcxDKwJglK9SgsVjNOnT8XGakORAzB8C/jjngMiSe+sgoerxdnn3M2Pv98FW667lpEQn5pa/eU4j6RzTjW/SncMU1PBTKigla8rqEemelpWL1mDU5btIhLlquUaLtmQmdbJwYNKmFm34MHD8WuN3XGFE4W2r59J9rb2nhKjJkwjs+lilFvv/mOyDpNogmof5MTj+jnjkc7VkFyNN82BSxAUJJYKDWf8t25yKX0C3DNOAPnXMhQ4iqxmCSHE2XSSzKHgUpgS2zRBFjxYU2l95rMaGxs4JJg5CCJ2axJpEaPsOoBvRC1U+LnJwrkkuwcapSnkJmeipNmT8Pw4UME666cAAP15h7NORI6IXy90Qjbm3v27MLCBXMwdvRQbN60Dj+85WZ4ujpkgpMQlOr6J7KP6J5oPrEZSj4iOZ5R46I3dqL8W3BAhtDR1QWzrmN3xS6cfcZZ6GjvZHAYeZa7PF5kZWUjPSMNdbIoKM2bUaNGYfSYUWhuaMTmDVuRkZ7Oqv/0GdNhc9hhsVnx0r9fRkd7R6+aAL1HzlQ1fl92U1r40eppIgxoWKexTDZS/aUPQJXtDkuW2ZA86G8yf8j3Ynyf+PM43z/xSFLp19gUmKivbiSWHwKBaGaLKCcVUw36AsKIhcXqlUkM5GFswV8KgEfcAyUuFeXmoLAwF2VlpTI1uY/97zgCgYRvTCTKeAMCRbd06Uf49Z/+CK+3AxV7d+O7N92A9pYmvleFulRm1AnpI8VArCjcDRKC5iClBXO6jbT9Y2MuP2PegMoqZGRkYu0Xa1BYXICJ48eh2+OByWpBV3c3l4bLSM9kwUCN6b0tZsyYNYM1g6WfLoWNuCY1nUuJnX3OWdw3GekZeP65f6OpQfSHYhFWi500kJzsnLj3vsym/HNHLQAsoQgckviVWqzvjaFpMgOIxkuZA2EiixFF7ug1Go5wOCMSjor3Q0BUEnlyiWklXXqZ4/QR+QD7igCozi0ePIiTgYTvZGCPLea8mPQWs/WrKfAQ60yqShPE0NIC2GzAzEnjUFyYF0tuOtFTSA2F2UwLw4eobsK2bdsR8fvwyONPYd++Q2htbsT3b7kJrS1NgutPWUwn4oYOkyw9v6PoFJke3mh+GlQSlcBjsmioa6jjPIGXXnkJF154PmxUNk7TuAJ0JBRGWloqFwjlr8rxmDlzBlLTU7Bz1042LZVHn0LN3zj3HGaQys7KxtNPPY19e/eJ0B1lmkr26ZbWFj7nq2hqLI9lXExEbpm0NLixKTZfDhFGEdADCEQD8EfFa1D9pwfFe3qAO4nUOab2ViXEk21cycKGSZoaMKr2QmWdmOJZpQP38131szSZ43DbX6Yg4IrDYrabtQiTVJJdVVZaiskTxomSascgyQd+Hz1h087ubni8QXR2edBUUwOXKw1PPPskNqzbhFDAj+9952a0tDXLIis993Zc77GPi8XClqpoidIOEr7DTlWrhcFBtFjbm5ux6rNVuPjiC9HR1clqLvFHUJiPgE/8+NL7PmbcWI4IVFZVomL3HqS4U+U1hX/hrG+cidy8XOTm5eHfz/8b69aujZUZJ7BOZkbmV0YKciygPdVMxEZjcPzHXpNemCMClGBJ1NNxAd+exBvWdKMIyZraSruLoQ0T2kAWvzEPOz0zg1liyGnjsBNTbGx593sN+j6VkP5KWqweXhS6zYTc4nyucpOSmo5Rw4cgKyMzrobBiW4EmaaJTD4VXbdg+86d0E1RpGWm4/Fnn8CH73+MkN+D7990E8fblXZyrDvOkTSjn+AwLSDxeWSR0Nq6WowcWY733nsX+Xk5mDJ1Elo7Oxg/4nA5YlyUoqhtlHdvYvtt6WjBxi2bmD+QP5ebBKn9CxctYE1haNkwPP6vJ/HsU8/FIgCDSgbFTMrD7+nEiXNlrh/tWKila+KQnOGgsJyq+htzTivdnAA7ugUmUKUYaTfIz2LRBI5gCIIEDh0qyW2s9yebfYCLXzUlZTOyMlFdU82TN+6RegGeqE6ixBCi3krWEScC5JIM5kvVbtIz0lFaWoxQxA+H24kpE8dh8vhxHJtnj7MUaycGCGRYxpoJTU1NsFvt2L17DzSrCUFPELmDCvD0C8/gww8+RjTowy03XI+uLmE7x6izEkhDjrmP5NLu7RxREUidEz+G6lYIJ0IqududguysTPz7+Rdw/jfOZW6Kri4P+woYWZewQKdNm86hT5pTjXXCSWjc0dlhOHoULrjwfNx8801MUfb97/0AKz9biZTUFIOvRIR01XdPpE/gWEWLKipr4opPCQfXvLdINh8WE2J8CLFHC8gOO2zMit/7YqKVH1blm43BfemFjtXPO6ImvjFh/Hh0dbXESD96JnbvTal0VvIBxF/uS2s0ISj+PmTwIBQV5nIpMZPDgbz8XAwdko/UtFRJd34CfQGGCCgtqLrGWlBdo8pDzQh3eMQOqAHFwwfhqWefwiuvvoE1ny3H+NGjGPCihNRXE0btvSlNkDSb+vpaRvnt378X69etw4Xnn4va6mq+77TUtB5mKU3c/7QZ05GTmQu/z4ed23ccdm21uF0pLkyaMgl3/+PvuOD8C7Bv/37cf//9WLZsGUN56TzhaKb6hEHU1daxw/eEPC8JG4OGMRBGIuWGU5gb5XRP2qhvKM5OUoIgvKruvYrjWQyTNHl4h5DWSt8SP85ATVVM5igeWvHATZk2BUWF+fBItFZftry6R97zyIvLRHk9XuQvsynHUdmQQXC5nKI0eURHVm4uRo8ux+hyyWTEWsAJkk+GvqKh8PoDsNodnApbW3kIZpcZh/YewCsvvIiX33wB1994DdpaW1BUkI0f3/odziAMBkPCDv4S0wj77Aup/tP/KVGouakFdrsT+Xn5bAq4HE6MmzAOdbW1GDN2jPhKVIBuqBUU5aO0tBTtrR2ob0jOEmzc4en5C4sK8dvf/AYXXHAB9u/fz07Cxx9/HC+88ALefPNNfLJkCfxBKmgiQ9VH2RKzE9WiN2saHNI7K/g5+4hqGbJ1jfye6n30KQTUnJGLX5TQ1hHhwKD4oLcftirKeGlSULiQ6gJYZJEQ1j4Oe+LeR1s5W4j9p3xMORqbPzeoj8mb2qg0gwD58sM1PX4KmnhFBfkwmSwIExqKuPXtVhQVFWBI6SCs37RZqJAxp+EJaMq+1UzwBYJoa2tFcVEJXn/rPTiXLEN3VxfGTRqPSy65GIOHDcclV16Ka668gqv3/v6OX+Dv/3gA7R1dPA4k0GiAe4FiHJ+mxjCBX7LnecRJum6DRoVSwz40NzcyN4Q1EMbrb76LS664mCsOnXb6qYINWs6FSCTCwmzChLFYt/4LbN62BRcFL4HVJqJFiXOFhMbBA/tRVFzE41RSUoLrrruOP6Nko66uLi5RnpKdFasoZTmaRzZUuI77fdYiRfSho6ODk5GoaC7Bnymp6bAU+YTq7XEfcCVrc//qojHPmope0I9EhCdAVpM+fNUKS43Yd3tgjZp05rCpQWAhKWDMyVAhvcx9JQ1POmkWNm7YiVAoHCOITHxAMSkTLiY5smP255fSpANNj8Jus2Js+XB2a5stTmiU1hyJYMiQwZg0dhT27NmHdRs3IjUlRcScj/c9yl1DsDXRBLNi567dbPsfqqrDn/7ye0ycPk1uExFEA15kF+Th+Zdfxbcu+yaee/4p/Or2n+DhR5/Crt37GElHwCbVyydUIehLLYqFKXWYrTZ4PJTtmIfu7gBDx39zx//gJz/5EYeBjYtKk39TX1MIcM+uvTh0qBLDRwyLEwDq786OThYe9Nxqd1Ylw8mEowMKN0O1DY5y/JjROBLh1GBCI4YjYYTCUXR0tvP7/kCQnzkQCHJ4k/IIkuFI+/t1QbXX22HoXPXKEoUnjxlmWOFgj4D4v/E/GxfK7KkGapwYjPiSgoVChAQwiqst38ddqwGZMnkyvF6K76oS4XK7lweX50r4XRqoMAEUjKX7kj12AoDlqM+J60BiLg4iOzsN5cOHceaayWKVWpUOZ3YucnMzMH3SeDhsPQhHNan7+71kQKBe7zthQFraOzGyfDhGjByJQYMGM9gjEuiGHgoymo4cgenZaXj5zTeQmpqOP/zpD7jmykswb84MtLS28q6UGCE44n6Uo9WbOZnMnRnXw4bxpsVIdSOoNqDVorFTcOPm7ViydBnqamrxxaovhDapi4VLXACjx4xBZmYhujpCqKupSzrvSPWvqqxBWdmQuB2aoylyEtJY+iSVOv8GjrzRPKWCJZWHKplzkCIblCbc2toiaiU4XSgsKEBeXi4yMzLY7DlsUAfY+uc/Miz+mEnPdN+UxSNAIgqQEe/0Jt3fctiC0FTVElk3jEwD0goChhLzfTXV0fkF+UhxO9kpFXMy0IDIqsMWAv5Q2iirjqLePUlupn6O9dMJNmDjEIA0gXwYWlaKvLw8QCN1VRa4IDbvKKQZUMRRAfK6x9JzT2CSkLi+hc06zRTC8hVLocuKPz2hMBICQTidFjz172dw5hln4rf/83tMnTQR3/rmxSyIyRRgXpFj9F3wNEuoRjbgL8ZUEA26yQJ/IITU1BQEg13IyErBy6++wk65dZ+vx7rP10kHso7a2jpMnjQZhQX56GhvR2NDU3z/MB1dCHt270Fp2WBYLLTge7kPQ78d7ewiZCKBl9ihaDEjxZ3CDMRDyoYiJycHbrebBY3b5WJIN29s1P8KSZmkW45KAMi+TPKQYmGx/81Q1PZwUkCTyPdP0hmMQZY7P/nlqOy3SjP2GxKKemuEBXC7XSyVVe43rXhOOWbuAYpWSMeHmbz/NoYOq93qS2kKqCQdSFTaqnzEcFjsKYA5lQgOOJRpsjv53krHjoPTacNpC0+B22ntiS+rLKzj3GgIzCYN4VAQO3bvQVFxNjas/zyG5zA29p/wuOr43Z//hO999yd44P7HEAlEccO3r0IKj0VYCK0kfTCgmzF6bI/2geQfhHT0ePyw25wwW6jOaQgdQS/eeu8DlA4ejLdffwsVO/cI7EB1NQaVDsLw4cMZKFZdVRe3w5OJs3P3XhSXFCMlxS2FQvLnMsJy2e80kNs2OgmJD9PpQFlZGdeuLKZCpoX58Ab8qKsX90XhTqfdDn+AzJsgzxMulkK+gCTXT3qn8s0+789sYPeN+6J0yLCZ2JcJIQuEDKSRczEYlOaAxDgn1KA9jCB02LChGD9hHPzSc04E8pGIqMBD90ycAzxUCQCEnjDgiRcFynGZnZuDzKxsLFq0CJozBdGoBZ4OD5pqG7F/zz7s2r4D+/bt47AgZTqeuvAUeH2U1ioJ0U+QFsD3p5nQ3NKK3OxspqZqqauFyWo3kJdQtVMbag81YNuGbfjgzXdRPmYUrr/hGrzwwr+x5ONPMX70SKS6HZwdF+e4kppZvxJMynA+s68Z24+NqJyRIuQaRIgYgWwO6JEoUlxObN6yGbV19bDbXXjn7fexbtVaNnsIEDN58gRWsXfv3IXODgF+8ni8qKjYh8GDS2J2//F2IsddT9R2QdikoaCwkMvGNXe0w5XixvChQ9nxShpXW0cH+wKcTjtSU9y8sRET0pHemaU3Aaxi9TwFjKvQ1JO6x1Vwkz2QrAJsLA2e9MET35BCwEYYBJLa8qdUERJjI6w2eWLnn3wKF2eIJfhEdISJFsrwRGaEONZNOQScD98frDWJJ/VIz+lxS4jkkqKCIni62/DF56uw7vPPUF3TiFAwDFdqCpr93fB66PDAabGio6EF7tRU5k2QfLk9wPje7ikZEq3f+yZfCZkZZng8YbR2dsNuM2Hzpk045ayzEQ0FhMZkc6D2QA22bd7B6De7y4Zw0I8ZM6dixpzH8dwTz2H5shW48sor0NrRhQ8+/gR2q9C4eC5RqEqBgRLV1ITJr/cpHRIVXKMzL/FdcTGKx9tsTgS8Pljl5F388ac4/xvnYOWqlbxnf+uab/E3yoaVwuGwcyiwsaGRcRL0d+ngEs6cHOjiH6gCo65HGALKQiSaO8KquDPSee6TExE2O0oHDxHPFIkymCklJRVpGoU8rewg9Mgy9y0trcjOzYZG9FoDbBYS7lR2m36LKMIJ/KPKrtELV701fsPg1WDVv48EH+PfiUqEsXR44pBSNiE9Ai8ASS2eyBNKzqm01BQU5OdjxvTpWLp0GTLTM2JxfkaNyflCdfmi0gn35VVzpklvYjVtzKihaKw7hNLBxTj1zDORmZkGlzuFocxmu00wonCSUAChYATrlqzA3kP7kFeQjbffW4q0tMxjYp1JfnsqhCsEVHtnF6qqGpGTm49VK9eyAGCnnNWOrvY27Ntbwd7m2QvmICUjJe5SJ8+fi7/d+Xc8+eQzuOKbl+G6K6/Aq++8wwSXJARIHQ/4/bxKSX0dYPeJCSL1aBpPCqGSH6dH0U3MgxM7k/CfCu4Hvy8Adxqp7bRjUSJPFPVNjdi9pwLDhw3Hlk3bMHLEWpxzwTlsZ5P2RWG27Vt34OQFORgxYpjgSxzg4jcWxOnNUDCaF5RPUFVdJQBKGuCm3ZxMXFb6aCMTY8UKtdnEc4bsfSIrIQ2H5heBn+jVbncwoxAzZx0RJZjC7Mu6czFiT0kTdtjAGI2cXi/c05KdJquFx0oYG2U7Cx7KJqRIBy1g6RswZqTR5CopKUJDUx3OP/cbHA4UHS4XvpkmgIGbjNmOjr/61ltT/my655NnzoCvux3nnnUWRo4bh9ziQrgzUmC2W6BHw9BDAeghQjWCw4RzTl0Il8OC8y84E6kp5OgRBJfH1WepwBGkodBC8fsQCIZRkFeE/XsPwtfm4ShFJBTCzq1bWN2cQMU4MlIQDUblEULY34Uo/Ljt1/+Fn//ip3jxxZfwweIPMH/BAlZhyVQjpxX9nZubo37S2FGJHdfT5LykJd+TRk5XUDNUeXnUlxQEVz0iCf0IQsEQTHBInIAdejSCTVs2we1Ohd1mx2crVuHdN9+F0+ZCZlYWmptbuKBoVk5WTMYMdN6omLtC22l9mIUU4iOnJN0DJSrl5uby4jVm5vKTUqRB/j71J/m9aENoa+/g73t8fjYLrBYzn6cc7bGYSR9U56yZ0c5PB/3NWb7GXbK/5+7l8/6cH4nugmRaApnuJAR4UyfNwBApoB2F0FiNTU2YcdIUDBtezPzwsfrxymaWqiftQrT798kDcBxa7Jkk8+7IYWWcTEMSe8HJ83ihRwkeSmEQubBjgA+KKUdCgN2O8vLR8Ld14ZILz0NbR7tA3sUSNI7j/Uq1nHwoeyoOoa3Nh+6uTmzZuBWa2Y5d27fj0P6DmDRlIlwZNui0mCTcVYMJFhuRs9rwxWdfIBAOomxkCerqqvDpWx+jraGF75tos8mxRaW0yLRRMlrcQC/PYyi+TL3UQyTTF3BFNhWtYu5JnReMMEdoAzAhGKAKUyZ89tkqzJp1Enbt3o3169ejsvIgxo4p5xBidU2NNFeMN3tsjR3B/gBDiAk+XF1N+Swaax7ZOdn9IgbpU7vdCk93F2sB6WnpOHDgALq7uxiQVFwyCHabjYlRSDRSYjOPTgKgyLiHm5La71FxGCu0JG3S0ZcMVNyXYE/2YMnOEc4vIBASKEIlBOg1HNGRmpKG9NQ0lqbf/ObF6Pa0c9hEfVENoK6H0dnawlhvI8PN8ZYERi2GC25EI5g75yRs3bwZ48eNQ8GgYo73iaKlycMrHHcP+TBu6gw01jXg1JNnID8/hyW9mIzHVxOI+XssFjS3tqLL60FOdjZWrFiBkNePjxcvxqSpJyEjL4/xABRSpcY+F5sZXd1evP/2O3jnrQ/x1DPPo76xHhl5qdDTfGgLtvCic9gd2LVzFz5e/Ana21o5jBaLnffiPOG+pKHkBDQSMUIMkPrf73o0OLO4lgDbfT2zzEx+lo4udHZ3Yc+ePZg6ZSoqD1VjxcoVnOuiR0Oo2CMiBIwYPA79rRY3OSX9Xj+mTJ7CbMBUm4HVfkVV189j0XXS0tKRlZnF/o3SIWWYMW0qBpeUwGkXICfSAmhZ+nwBNLe2ccJXY0srPF4/Iz/VdahXkhpkyvY39mW/TQmAXr7X13UG8hvkFyBoscWssdlgslsZaz102BAsW7oCF110Kf756L/YzldMQZawiTkKbE4d37rkfGzesQf7qqpgIQAOKwlSwie0REk8oFRPxZzDMX0BMBk+rAxupxPbdmzHAw88wLznuhyAw1uPDsQoSrsdEydNRMWe3bjxmm/iT3fdh5wsUfbcuEh6vZ8B3rfSAMwWggX7UFVTg+GDi7F92zY8+8TTTFc9Ylw5osQexHnwQn012UyorqzEiqVLsHrV51i+Zgt0cxS6I4S0LCdyclPg7gyhvqIVpoAdC+bPR0ZmOpYtW8GgFro1d0qKASsvxKcxhq6g6JyUxslo5MlRjP+Jz3V4j5LwYH8Tq7fyWTnhTWNnZklJMTZs2oRvXn45Nm/Zwubu9m1b4LY50FRVB7/Hx9mayX/xyBs7/FpaObzX7elm5CFRf/c2VolNkdoQ83FDczPysrMwYexodHR5UVt3EE3NLXA4KAAOtHd0cn6H0HrMsmaCzr4em82KSy88n7EEvScDDWRhJhL89cHn1Z8GkIgVML7HigaTQArfAL1HHdfQ1Mi8bjt37mJG2DPPPBNdXR0MUqL+ZBhmJIA7fvlz/PaeO7HgpOkI+b08kZU3ur97G2gzev3JU0tC6OQ587Bj22aUDinF3IULoJOJQrNZNmPqKM1WsrNVnfpoyI9xJ81hD+/UCeUYP64cXuntVR6G49nI3DLpGg4eOMjkqZQAtHPXXpx+zrls73PaKEG/o0Fe/Du3bcfbr7+OTxYvwaeLlyOidyEt14S8UTkoGFuMweVDMG3hVJhzw0jLc/G9jxw5DK++/gKefPpRfO8H32UOPtJsOIRrwDqo8RCLX4NuNkEz0c6vEJL9DEJioSW6oHQikI+LNwcblfSqQ1ZWDj744AOcecYZbBKcc9bZDPrxdHSilSr+KDX0GBst8OUrPmMzlVKIyeNP+QbJ8P4KXmy03dXrlm3b8fGST9FQX4/de/fiw48/QU1NNapra5GW6kZJUTGGlI3A9GkzcMqChTh90SKcfsoCnDxnDvtgykoHYUjpYOw/WMm+JdOxP1mCEEiM1BjeHsilVKMbozlHWYiKp8BGCEQiciAGGKcTHZ2dyM7LQlp6OqqranDD9dcjNd3JKbXk2e4MNmPu7Kk4e94cRGqqkZ+VCasBrmZ0Oh7TELNfgbz+5EvRkWo3Y3RpEUoK8vDRR+/j+muvgznFgSjBkCURBaGoTDYnTGRDEykLJaXY3fxvUeNQh24xY87ck7F7+zb88OZvIxCWPo5+Mr+O+PYZfaczTt7j6UY4qsHpsjNJCXnOSfCKyajBZHNj1bIl+OCN9/Hx4mX4dNlyuDNcGDtnNEYvLMeIiaUoLs1mLSAzy4UxI8oQCLTg1h/egK3bt+P2X9yBYNCPn9z2Pdx7/90ij0Bl80m/TcwtQLo/O3Ft0HVKzrHK7LL+bcnDNAIlXCTxKDk+vR4PbFY7DlZXs22ekZbBWIzSshK0d3nQ2tomvnMMnS1IaEJYuXo17A4rBg8ehNTUNEZX0m8mnsu3KtOKkwkHAgYRL6bX52cK8wXz5mFM+UjMnzMbY8rLkZOdxf4WMnOCER2BYATbd+3C5q1bOEqQmpWJgBZlAci/MxDbPFlTvjbxD4MJoP7WjjI2Kl8JC0CLnkKUMQYv+buMh+bKLcQ9GMGYMaPx7tvvo2zYUJw8bw66u8hpZkFED3ENOC0a5tsZNHhQLH1Y5dwbtYyjatKtzWWoIhGkprrhctlw5qmL8OnHH/KAnXXONxAN0M5ugWa18iInJ+b+Pbvw5msv46EHH8Rf/vIX/OG3v8KOLZs49EbCQIsEUTZxIps1g/OzccE3zuIilxbWiY+sX/vsc6lVkGZEpK47du7myjS0ywe7gjBZaQc2Iwoz3nvjTaxa8Rk+/HQxtu7ejvJZI3DypSchZ3gOvAEPCvMLkZ6eBrvdzBENAmvtP7QXTW3V+Md9d+F7378ZL7/8Ai668ALsO7AbF1zwDXR7OnlBCNSDGg/anaQ/XTOubCPxfJKxMB7JTlFkN1EyeyxMAlKUX4xXX32NNchVK1ahu9MjaOrJ7jyWfqWqRJEIlixbzll7ZUOGoqOjEz6fnxN76PdFyLpn4RMScTc5JTesx959e1FRUSH8P9K7T0uL8gAuPPccjBoxgs0Jo8ZAkG7aIDkgStB3kwnulDTGF2hmMzo8HlhsTqRnZLDD30IU3nxhmaarHOgcQRPgusPtLflK59Nw8Odq0dOHlCZA4URZbZiJRPvoqETvPzXqe/U7NN9JE4i1CE1WjSVdQ10j5s2djT8uXYFgIIRbb70Vy5avYqpt8vR2t7VCCwnwD8Va7VYzTHRz8te4DJpEDsZ+fyAhH6NUkrYrhWZKivIxrLiAyBbxzvtv4eEHH4EzN1PMYbMdzQ0NWL9+HXbt3MHqfHtnB4aPHIWZM2ajvq4Ojz72T540F198CWaedBIcTifOOO8CvP3SC/jpd6/Dmi/WorPDyxx4ZMP1COEBirAk58U8DxyftqOz04cZMybi0w+WYOumzZg6bzp8XX68+9brqKmuxYfvLUdl80GMmjEUxcPy0B1owqfvr0ZzUxNMl5yNaSdNBnQ/UmypCKRS4Qwd1dVVPElPmjODj61bt+Ddd99BS1stMrLSECTTTBP2MGMTNDM0FeKLcY+oqpUq0KUMBbssMy4FoyH0G9NQlRmghEOE/Ekmjs4MLirGoYP7sWnTJlx0/gV44eWXkJ2Tyck3fD8DVAESsQIqujJ6zGgG7BBilRyi3V4vY/hJyFrNqZLgFjh06BBWr16NkuISDgtSTgCFCJs6Ohj2nu508aIkE4IWtgK/GTUFZvOiTZLxL0KzcrlS4AmG0OJrgc3pwuCcPJgJ6ckhf1KJTKKoB3UMu8fkrkv9Fhnggo39w2BYW6W5FmICgP532UQ0ovE1WSssKmFY59RBhRhaNoS1gAsvOQ9nn3k2F3UgN3J1TS2r2iFvgLPC3C4HPKEoq4DsETZoAr2ZAv0FZ2iQqbbeiJHDkOZOwUmzTsKf7/ozTl10Gs675BLuz5bGJixdugTbtmxBVmYmiksGY9r06SgqKmYOetXOPu98vP/uO3j9tdfw8eKP8I1zL8DM2XOQXVCMg/v24Ne/+BG++6NfICszl506R6vBJT9HsCbV1zciGA7C6XZg9YpVGDdtAl558Xl0d3bjtVffRXNTF8omF6BkRAbcqWZkZRcixbEFe1sPorulA9kp6ejs9MNmscJhdzJijUpziYy6IDuyxo+fwIfP68V9/3iACTUI5EITl+YemSMiktObbKOZae2ZdMnUOCMOxAgqMmwmEehobG1GUUEB3nnvA/zuV/+NkkGDEPB48ewzT2P+ogUszPtyBBqBPUYhoAqK5GRm8uImrY+KlFKkivwMAb8P7ZEobDYLnE4HE5icdeZZjJvg6tcAO0wrKg9h/BhBZOJ2OTFmVDn7mfpytgv0uyisnpbiRpDYuzUTXE4nZxq6rVYUZKXDRGE14QCSmAfZn2pDH8iiTdq0nqQhkuPSgdxnS/pxsmpEMs2S2FoPVlXyexdfcj5WrvyMa7/95Kc/QH5+HkIRHUF/kHHg9IwUJjFxVVLDvSumI1UZqZ/njfuyHGxS0fJyc3gSzZw2DYtXfIq6jmb8/n//F/5oCO+/9x7+9Jc/YevO7Zi3cCGuvfFGXHL55RgydChsNOklsCYSiEAPRdlk+PNdf2dyy6effhLPPfE4Fp1xPtau3Ym508bh8stOQ2t7c4yVR/ipjtEYYE2P/hfhOVBRUY1JUyZgxWdL8dZLr8HbGcIzz7yEiupDmHr6UCy8YDZGjCnHiFFjUFxchIsuPw033ngZzjjtFDhtFhTmDcKgwlIuqkG25+RJ07ivSMAoVl1qREl+8skno7C4mFVjDmOxw68nqtLzZEpHVQEsa1IHYGzXV4wcahej7qLxJtWXcsdAZqYJfo+X02rb2zuwbOVnKCoswv6Dh6BFTHjvnXfFQlZsosm6TqrutbX17OyktmfvPuzbfyD2zES44vMHUFNbg00bN6CtrQ2bt2zFa2+9iS/Wb0BrWztrtMQXSYufdneqa7B7335MHT8RKQ7CBlIgycrY/2Rrk8PkpNESxETR8IXJMWXC6XNPQo4rFXX7KzFmUDEc0PDUMy/AEonoiJg02AgGLP0w1DEcautnzvS5YJQEIYeLVE2IBYg1DYU8HEiTJgglOtBsYGw67SQR8IJb0d6OkC+E7LxszJo+C4888hh+8rMf4r9/+XN85+ZbsWffPoQCAU72oOQgUi2hC9uOJ5l0NDLj8ZFk3iqTQeaVT5gwBrmZmejuasPLr72Me+75B3wBP+6+88+A04JvnHs+e2VVIxRdJBJkv4DJZI3RU/FngSgcNheuvu4GjJs4EW+8+ir+/dK/kV88GK+++hZ++18/w45t+1FxoBkuhy3WN8ejkVlB4SnyA4wcUcpY+C2bt3L4LqKHMX5CObp97cjLzUdjSwus6QQGAiZOHY85804GIiHOyCQnbEZ6DrZu2YmpE2dgxLAxHJaiVltTi81bNuLQ/gMoG1yGltZ2fPPCi/Hksy+gu9sLBxVMOLyrData1ZEyTCKl+iv+SlnDQgkDxUtjo8UvNzqNL25CxO9nvxJtKJ8sWYqLzzsPWblZaG9tw44t2zF+wgSUDS+LhXf55ww7fVVVFVpbO5GRngYHh/V05n18/8PFKC4q5P4kD/2WLY3wB7zISM/iGgSbNm/iHAMiLqEkH6OpQb+T4nZjzqwZfY+Xsm4klF9tmLE+EQ/JGsDo0SMxa+o4VFbX4vW33sVppyyABXoAWtSEoNkCW9TEAkPVDO8PNh/p640kIorxQjQ4ZlmOPDH2Z2ykldB5oQgimhlplONLCEBJJkKxXbfDiqLcfNTXN6BkSDEuveJC/ORHt2Hxhx/jtDNOxR/+9D/46x9+j+raAxgybDgcNgdnVbV4vQxGEdBgMWnYXJG1DdlJZLQLkvr9hNPM2+3BlCnjoetUpyCEJ555Ebd851YMLirB+jVfIC8/H95ICLu370D1wYNcpJLIJwqLipgTMNbCBHYSzhK141CRlclTpjEq8KP338WhAzU4sH0Xpk7ej7/84fe44trvi4iHmYSQ1AKOQRCoxyXwi9cXwoFDVSgZUoJX3ngLmdlp+Odj/4Q35MdpF8zBmKmjMHvBXARCXXC7HTBbrbDYCVZsR2d7Fxr3NmL7hiVYu2QLTp9/Bh544EHs2r2LUYHE3U98B2VFxehypeG6665nFbG2pgovvfwGzM5U5nnQNOVhMoJqE8NOhsUv344R0Sp1X25qLFaMiWC6+reGtuZmFjxE6bVt+zYOUdY1NKK9tRNLFy9B2bCyw3d9nx87duxgHD7J4NSYbS5MKcqu3LJtG2ZMm8bmzYL585CWmoqDlVV49/33MW7cOEyeNAkZaQK/n+jxp+Qeoi0nQZB0vAgkJ314nE1r2LRpw1WKFgl1Cqem22z4eMky7K44wDwOOVlZsAQYZUU8/1GEKH5OoRaZIKSSgo54FhlfE1rMtDD1LWDEWohi15bNSM/MRHtHC9LSMlBWOpg7VzUic1y7fi0GlZXwTvyr3/wSP/3xz7ik0xVXXY7uzjZ8sWEjho4eA7tdQxpJ2npB/KhWuqDH6tF8GCBkEAIx288YjSCCjwDlI5TAnUJpsO1YtnQJstMz4LDaeOds6m7HZ6tW4dMln3DF2rTUdGYFprAlYQNGjhjO6u/8k+djSFkZ75pGLYDsTjILnA43zr/4Muyr2IU3QxH89e8P4MEH78Mffn0bfvrf/8MsPWztqWw5/ciAQImN+tFhs2PrtgqBwjfb8H/33oeJkyczBPWyC76F++5+GAUF+Zg5ewq6uzvR1eDBzgO7UX2gAVm2PORnFGK4qxx//+3fMGLkcCxdsgRDCotwwVlnY3/FXsyYMRMTZ01H475DiHR3wex246Jzz8V7730ggTs9ursomxafE9rDAiUXsUEm8Ecykcyk8ATJpqQmXijJhtTzMKJwOF3YuHU7Jk0Yj8suuxxPPPkkho4Yig3rNmDK9CnsoCUBTU69nTt2wWK2wOvx8ZgOHlTEnyl8By1q2pwYG+JwwG02Y8WqVRz+O/WUBVxTgEg9EseHxmTDpi3Izs1Fenp6LytEzk25ifKaMkDlmedF7rApdhOa2zrw54cewsTxE3HLzdfy2gvQPVJMNEBU3ywxIwwBJsBFRDdx/P2ohEBvTUlo+ikyOZKEsmKBBC4ZruGL5auQS6ytw0rR2d2JqtpaDB0ymKMAkaCOEeUjsW7TRgS9QVjsZuTkZuF//vA7/P5//oirOjtw8eWX4aXHHkc0pMFi1Vjq6dGKHqC5er5EZ5FRAzBONoO3kLDwbrcNWdnp2FfRhKsuvwIXXXwx7rzrr/jtnX9ku9DtdMHjEzXpm1qa+ZVyA6qqKrFyxQo8/vgT7Nih3WDOnDk4+8yzMGXqdGRm91ScDfsJu65j2Ihy3PrjUvzrIRt+fvsv8be/3YXv3nAN7vvnk8jOymJtID4+e5SNns2kIRQIISs7B+FAM1NTRfXJPKkfuPtRHDpwAPf+/kFceukl3I/F6YMxedx8fGN6KSIEudYiGDZxgljEvgCmTp8qkjrMFiw6/TTA60O0sQ15ublMPQZPN4YOLcPIESOwfecBRrQJWUXPI0hSe4VBy20vZlbK5zd6CWJfSwR/aIYPyC9AJqGNuBCAy795OZ557jls37cXFrcL4yaO5+iLsOvtcDrdqKmpYuh0XkFBnBuGBAGp+Xk5OXw+IR5JsJFfYML4cSgfOUr8qiEEqF4J30ICo7S4sM8xIi2aH0HB9hWul5+DAGkaA6cJ8LTks5WYP38OFsyZx9WoQ/IeLeQg03QTomS3RczQZblQjbywx5PtNUnoIO66hveoU6xmDR3tHlRV1+BHP/0+2CmssgcpzsmJS1HYLWaOAFD4ZP6i+YJ2e2gp/n73X/CPu+/HylWr0d7QwPnr5FjNy8vmwTCOltrtmeREhk/ifAHKgclOFTpfMPbSZWxmHTs3b0F+YSHmn7IINXX1qKmv48VP3m9a/ASznX7GdBQMz+f3w94Ammpa0VbTie72LrQ2tOLzL9bycfc9/8eswQtPWYhLL7kM8+bOQ1auKD6pB3VYTTZ876c/xJP//BeeffY5XPet83Cocj/e+mAZq9bM0isTi47aHDCEOCn5pKikiL30511wDioq9mDl8pU4fc4idE2YhpOnn4q5CxahqfIAivLzYM3MhN7Rwf6PSGuz3KXofrzcwZT9yFSUBITSotD9Xu5PMtqdVgfjOIgrgYAuzEIUK2WW5JESBbQKBrBWm3zzisnvJOm9VtlvKU4Htmzdip17duHb3/4WHvrX45gxfRq++GIt5s6dzX1M8fdAKACX2w2b3cHRJXLeqetu2rSZf23Y8BGcsFZdXYPGxkaMHzOWPfrGEF58spAfe/cdxPBhQ+W1DIvD2AhLph5ICi6O2tF3zBqcJg0tHe3Yum0bPF1enHXqacjOzIj5MZSpYKHON1ORhIiwXShEESVnWyTKefQkHGJxfqP07Em3j+/d3pqKw/bWDOAhVp80Mz5buRJeXzeamlqQk083TwAhEyc78EBSeEbXMX3mNLz47xeZljklLZW/n5Wdhf/502+wd+8+/PIXt2PHzp2YOH828vOyRBlo2qVUM/BtkEOQJCo5K9U6inlEVa5nVIMejqIgNwsLZ8/FGaeegtaOTjzwz4fxxPPP8iXJ003RgfETxmPohCHIHJ+KkiFFvKtGiZ1AtyHEmU0meFq7sX/bXtTsqUXjoTrU1jfguedf4IPU7IsuuBBXXHkl5pw0N2YmXHHVlXjk/gfR0e3B7377X6hvbMK6zXuQmZ7eIwSOtsnwIlFN084/pGwI5wi8/OJrKMjPxcghI3DphefxLmOy2BhbUVo6mLMc9a5uhjK7HG4hSdmukuq8rmYuMaGGGNXH8RwSCqx5hZCfmY2xo0di4+adiERFVIDvRi6GOCGQzA2u5mYvc1FcQ3AgRMnUkN8xXkow1Wl48onH8Kc//RF333s/mlpa+LbJ3idPPFcLGjmcvfcUNqWEHtUIQfjO2+8iOzsTH3/8CdcPvPybl/HO3xcfgtIspk4mzUncZ5DC6b1gcdiVHSPTFYLHbdHQ7fNgZ3UVdlVUID0lA2eduoh9XmrxG/l3TYSkI0IBkTLLuVPsMaR8+1BI57ACj1GQnFKqUiONn44QpVWy67z3xW/86IhCbAB27N7NWXTE1067tmKVkX67mCSnvIDZc+bgg3ffi71HgxuN6Ni6ZRsq9u3H+h27oTlSkJ2djfzcXA41JdrDMY0ztq2o2G7P7q9Kq2hmnUEaY8eMRYhsTbsNew8dECWmJc30tddei1UrVyHTnYoNq9ZyaWpif+nq6ERXWzOiwS601FZxvkB+WSbOuGo+zr/5TFx889mYNG8MMrJT2YZ88OGHMe/kkzFvwRw8/q/H0FhbD7vLiW+c/w28+t67SM8pwN1//S3KR5Shq9sTN8kOG5rD2VuTzcQY+w7lVbS1tjEN9apVq1jLmnPKPKS40+B0pTPnHmgekG1M80cJ+pBc2LHFT2g1KUmjRMwivcBcA8GECKefRlCQnYnykcMwadJYtpWF190u4daKLtvgPVI2sHFTIi2un8kmuFa1pJ9x7YlUB5avWI4uTzcuuvB8xkNQefHVq1ZLO1/nZKbOzm6kprljCWZ0f1lZmbjj17/E4NIhuOD88/H979/K5g0TdfQTriV/RDASEYubTAfiJGQm7YTdU+747KszAw7CyUci2H/gIIcXKdIytKQM0ydNZE1KgZKor7jOomyWUCQsePTCwrnB3I9msv+tPPhEjEBZYgTqoAlP1YRpYfm8fnbOOBwuDmnRuWarZLk1aGaCQlxCelXHy2xDgVTqvVFd96yCdHS0t2H3vn0YNSyeq52acroMGzEUjY0N+OD9D3DmWWfyD/3pj3/DE088xR7YF958EwtPncOUSZwzbTYhFJbXMtxz3CRRfW00g9hHQn0gHmDN52vQ4enAq+++hV179/Ipw4YMwZ/v/DMu+ebl/O90qxvtB1qQm52DSDjEHm4yvRwmM7at2YBNn25F27BaXHT5WXDlp6KkJAOjp5bB0+XB0rfWw4F0NokqdlfghptuwqJTFuDbV12D886/AGPHTsKbr7+C8y+/Evf+5Q786Pb/RcW+g1xXgKDJicCqxNbrdOQFIvqHiCssmgUrlq/EgR27MXhYGTSCp3ISD/UPQa1pQUt6Yw5HcQ15zh8g04pQkuTLoxBhhCvFEggrFCv9q/gQhg4qxtbdu5mh101FL8IEg7XHQEEUoiodXMosOsb1S+PBOzs5smnCywjPYc8nyT1itSS05H1C5l1nux8vv/warrn2Krww/2zYrA62+SmcydBaudhtVgvvsKqp+XnOOWcddu3esv4U0GjX7gpELWYMKxuC5vYO1NfXsyY5ZeIEDvORCcURA0LuqgCJrqO2rpFh4rUN9ZyGTrgDShWmSJnxudi8Naw7k8NmhZ24yBwuLqdks7thtTo5fEK7P4W67A4XTERFZBaEm6SW2N1OpLHk0xC1WNhsoMFU4RaK2tklnp/V6lgHSNpuw3u9zcSMjHR8suRTztR66fkXxGlJJKgSAifNOYkdc2tXr8fzz76Ax598Ajk52cjLycPmLduwee1GFBYWwJ7qgCstRVKKi8433gxBj+0SFRkzc+KcRwJsHQiHYHc6YHU4UVhYwrHcH//oBywUaPEzCEnXcdkVV8PT6sOh7fs4SpDuTkFWegZyCwox5+SZcGWZUDioAMNGjeJdo6hwEDPzzJg5EyWlOZg6dTLuvec+3H7bf+F7t9zCCUevvPEag4QWLjoVTa2t2LD6M5SNG4t//PmXGDlyONo72kWq7eGlUQbW+Dl7NAFC5nV5fHjm+ecEjZmEPzNpFTn3aMulCRqKQA/6oIfDQrOn5ALCWPjDwncSJs2Mzif5EDWMqc55HTTmgwoL4LLbMWvGLOYbEPToYly6fV7klBSiZEipSHGVQigtLY0doSzYGTvft3AT9jd6bdGoFS53Jt59+0Pk5xdg3JhybP5iHfKLirFp01a+523bduDDjz5AS2sbh9oS56aKBgwESqzJIh81dbWoq6nGmjVrUF/XgMyMLJQUFmHpihX4691/51wC1Wc0PKRpNhDxismMkcOHsQlYOqgUw4cO51wM0lSohVWxEunrUs3kdLtFXrLVyskJIjXTBF2zIhSNIKiTQ9DM6hJFDNgkMJNqIoEHXDdQmA+q/hj7zPqZcaoIaWygDMVBVHdNmDAea9auRXtbJ+bNPRkPPfyosGES1SEpBOj9M846HS0t7fjrXfciIyOT+d2CYS8y0zKwbtsuZObnIys/B8WlJTyBjAAcMRISNGIRnIQWY2kqaQrwRmSzoKWjA29/9Ak2b9uBGdNnYcWny3HP/92LnLx8Fi429mTrmD17Hs467Twsf+MT2GBGZko6stIzkeJyYcKUifjtn36O62++Brn5ecgfVIyi0kEYPGQwMrKyUF1/AFEEMHHiRNz6vR/g57f9HL/++e245IKLkJdfgKbGBlx93U345JPFOFhRgbKRw/HgX3+H8ePGck44haniHN5HghhUwo5zJiJIcafinQ8+xsGd22FiqjKp4kdk8gbJgiDt+mRv0nyJQIsIPINGzmb2nZD7OsKpx1TtRkmmCKsH4jdHDh0KX3cHpkwcj5zsXARZC5C8BVYTdu6vQH7pIESsJoQjIYbXTpo4CQtPno+T583HkNJSngtJp6DhTb0PtUiYlnYuBLJu/WZ889tX4OP3PsLQwUNQU1PD84aKogwbOoy5KQkTkChxYsxJA8wtsdhsmDptJoaWDceMabMwbmw5SovyEdKjWL7qM5w8dw6ys7N6oMfS10SEMRSd2LV7D0oKi1FSVASzWRfmgfSE8lJV5pmqz0ldLqRUXA2LWE+Ru4Aq2ASiQQGsSHS6yn8ofFas8nYfvABxHUQ5+7JGoNplSciQakzovkxXGsaWl+O2227HgoXzWJX/9wsv8UMnEwKqo7ds28pxd9o5TBqlRIaRlpWB1954G93eALIyM1BcWNwzARK3C/m3ApyqGDKrTpKk0AxRfaagqBgXX3QZ/vz3OzFp+mS+L+FkMtjhuo6//PFv0PwWfPL6RygqLoTdboHdqcOGMMaPGIHSQXnIzLJjSH4aSjJTMbQoH3UHKrB3y0HMmizQYGRnDh5ahlMWnYnLL7oMl3/zChTlFcJudeCab9+El156Ga0tbSgelIv7//YbzJ8zEW3tbYJ9J+bjOFJdQDjhODLD/oBO/OPeB3igyARkIUA7AQkAisyEaFe2SNue2I/MoqIwbd8EbmLoMs1MMHiIHY6MRZclyyI6MtPSWQPraG3GN84+B55uUQSWvmmzONBMO2NaKk46+WSkZGYiP68Q+cVFcOflYfTUiRg5fgyXBCfG42SNI0h0L5oqbXfYI8deaGd/89U3MGP2TNTX16Guug6dXV1YtnwF+zEo0lNdW4MuokNPCDscaVFQOp1yAooHFSEgLYoNm7fg5ZdfwvXfvhZzZs2K8VrStYnUZP36jXjmuX9j5649aG5uZfPERcAsA9yar02bd1BChUkboDocIR4G6eUXp/V4+TiNgBhYhL0U5Mhh7y0WflYeuiNobCbEHDg6axcNtQ1wmq0478yzsX7DRtzz9/tx3XXXsHPj6WeeizG19hAmCIlLBJArV67i7CmryQS7xcrXcztcTHm1+JOljPsmNZESVYzEID0PQ0AgibIw0SIlQWDwQvOC0BAIhpiaibjZ66rr2c5NVkySfqOoqAQPPfA0Vry/Bqs/+gxDy4YhPyefpbcpDHTUd2HXpkqsXb4N61duwN6Nh7DknQ04ZfapmDdrHprrGzg7LeIVjlebw42oVzjYogEf8gcNxgUXXYZH/vUvdPnDyMrNxl3/+xtcdv4CtLU1sWeenahxRSgGUPZMJcHwookyau29D5fjgzffhjktBQGvn9mCyATkKAAJZvLwEw07OZhpIVK/cIYZWQuivDjNKQauhCOImgjiLbQA+rXUtDTezWurD3LYNiWNaN+EwKcAljkaxcH9+3H+uedi/qmnYtSY0ejs9qDT50WHt5s1KQKIUUg7RriihpbStjlUXMaee4oGBLmqkVE1kC9RHS63C+vXrkd6ZhZGTxiN5Z8sxfix4zkrcuSoURg+fBgmjickKIQWELcmBiZs1arjDTocZRM7zWxCZU0d3nv/fcyfOxelBQWC91869OjaO3buweat29He1o7m5iaef8T2I9ZF/G8o3k8SaOTD4+6kjEjSE9TuSym00MluTYiPkuKqk1kgEXxkr3Bl0QRQZhKU5kAb+VAs8iapBfQAuv3duOTyi/Dc888z5TThpn/2sx/j3vvuxz/uewA/+sH3xMPxZBJlv2pq6tHa1gaX3RkrGGcyEdNOGJkZOXjz7ffwrSu+CU2rRFp6KiO6eHdKGJFYvFjq/jZdwKQpcic0lSicNic2btiCKVMmYG/FLhSWFMRdRlX5pZlOk+6UhafhzVc+wu/+8CtYoy6OXuzeuRetTS2wRh1Ic6Zi/MTJsNhSsOtQIy4542pk52ThrbfewoXnXcgi2W4Ste70MC0aKxCxwKSFEe3yYOT48Tjb58MjjzyMm797K1JTMvDH//4l8nPy8OATL8HuSGGHVazoqPKsD2B8OG+CZQOFqlz4x0OPYuKEschOy0KYOPfdLoR9AY6FE5OwsM17cPMMyZVv8Rwipx3zolL0wCpwAdGoyGsgs8ViQmpGGtZtXY+sonzUV1fDSUkHehQuOxXx3AWLyYRFp56CkM/PrFCdvm4U5RYyNqS1th7RcJSLeZInP/YcGuAnoQMNZUNHoKGxgTPzKEmHUIBcO0KNsabzDk/YjqUfr8AV37wSH324GGPHjUVeQR6XqFctKzuTz1WNIj60MKmUWG+U4ow6JaeySVTDok3HbTehpcuDdz9dCpfdih/98EdIdTkQJJNEbiZ0rZrqOi4lP2Z0OYoLCjj5jUzOxEZjFghH4Q+L8B8BLGn98oYpowhiYBkYpwLxPenWJAXD5OWNWNnO5xuWahQtBpuqEGSEAB9tCFoCcahlpmdi+5adOGneLFxw/rl48unn8PAjj6Kq8hB+/bs78OHixfjvX92B737nO8yywrFvSp9saYbP64OTC2wqMokoe8RLigqx+vO1GFRSirJhQ5CelcEhLofLkVRdM4D+EJWFlE3mgMBHUOc5rWhsakB7aysOHgxihmcGOrs8fD2qEmv0L9Df69Z+jnXrNyLFlY0XH38b806ag9FDJyGUE8CVl30TGQ4nNu/cgbLhw9g+zkhxM2XV7PGTRLEHQjLRALA9re6O1GjyrkcQ8XgwccYMdtz+88EHcNON30G6MxU/vOUWjBo5Cr+760G0tXWyzcrcgkdQ5Ub9Ggk1UlMr9lbhwYcfwx/++Cf4PD7YqJJL1MfVjhnHT5gSE5GykIdfznSaNBRFIo2AhADvaJT5FmFzjePUFiv0cIRTpjMy09AWjmLilCK0NDUgGhI7IGFVPJ2d2Ld3L849/zw0NzfjlPz5qG1oQFZ2NhOPkiDNzsxBxd4KbN+6FX5KQ2bVOQqrw46de/bg1AULEQgFGZW35vMvcODgfjhcbnF/cgencaOF/Y+778Pll16EOfNPhqZFMaikMG7OKHi6WqAUElW7cbLGbhMOcOrwU9jdrCHbrOGLDZux7LM1WDBvNqZMGs8uFk8wCqv0mqvxKijMR3FJPFIwEVXIv6Pr8IY0IfQoXqg05ojw41nIW8/hWHZsUzDHyjhdXaNJRtx6lEdMJkAA1gip01TTvgc9RzFwZcNLNHEPcu5oGiW2hHTmZCdIZGtjG77zvZvx6dJlaGvuwMdvfohdm7bht3f+DiOvuhoPPfJPTJw4HpddcqkswBlAJOInGgTJnCdscqqguvfAfsZfEz0S7f5DhgxBxc7dcGqC+LGviS/+ssAGK9uXvHMxa42OQ3sPITN1FDavWoWIycRUzXwfNit7jale/Csvv4IPP3wfN994PYozsnDl7Xfggm9dhY4DRObYiiHDRwBeP+YvXCBx0haAVGp/QOymET90Gg+Tw1AlSCbL0JhoggYt6unG+KlTeTd77NHHcPmVV6KkoBBnnH46c8Hd8b/3YMPmncjKyGDMP9mxiiq9ryGLORIpDs/e+ky8+ubHmDVhKs751qUINjbHnEIcd46GmXSCcuB58pJHiWjPiPWYyDDMAmwjrs32hUCeQkcw1I7iocWoe6UeDU0diJi7YbXaEQ4QK7EJmh5hhh1izrkqzQU9mgWbzYzcgjw0NbfB6U7BoEFlDNUls4UKflL9P2UOkFOb7ququppr/XkCEVz0rWuZ82Df7u1sgtC9Ep2419uG4UOL8f0ffBdjJoxjVCTPBlmk5LB+kovP5SATVAjnw3wDrOFK84s0OmLuCYdR1dKKQ3UNuPySCzG4MA/eoBgbYmRKnJ0U7aAWTciSFcaX0M5pKdLiD0TCCFIRUZUZJX1ZggZAAglog7FRToDJArtmg8NkZyyATSO/tdDrQwgxyy45amI3JKNA9KuKrKmP1OkBNZ6OGjBtxhS8/da7SElLwa9++Qvo4RCy07PQWNOAG6+8Aa+99BquufIqFmr/+5e/cOgkKyMLTruNvdB8JV3ETVvbOuFKTcX7H76H0884E5u3bUdufo5YXAO/MYZIm01WWM12Riump6Ux8CLF5kb1oUruKYfZivETJqJiyxY8+ejDaGlqZI3kvrvuwu3//WtccMbZOO+c86C3tjO1+fBhIxBt74AeDiJK+Ap/AFGPBzqp0hRDlUUaRaxc6nDqhqjXiSZLRlFMmgWRrm6MGDMa37z6Wrz43LPYsHEjwfpYC3ji3r/iigvPRmdnCyJhIucgcMpAzYAeYBHtpDa7E7/7yz3YtnEjbG6qdygmA5lHFJbSSUiys1A4+YQvQGwuapeg6/Cyl74BVQzZ7HYxg87BQwc5vDp9+nRJGxaFWdc4iaazvZ0jHdk5KbxLOxwWrhhNYCyb3cIJOiNHjcPkKdNRVDyIC4GkpWfCYjIzocahQwewY9sWrNu4AeFwFN+59YcYMnQEcwXSQ44YOgR3/OoXuP/BB3Dq2afz4qcMvYb6Jl50TS1tbMocPFgJj1fke1C5emokjCm6xjBoQ7+x050EBJX20jRU1ddjxboN+GzV5/D4Ajj7jNPZvOgkItYYL2BP+fPEJrA74jeCyqxgAQN0BykNI8pFcwJRDUE6IgLlGolSAFf4XuInOO3ujHTT+JVECQkBi0zJDCPCdfZUVIB/WO8hIgjJf1OaIoPAjkIY0KSnjho9phwWhxnvvfUeZs+fje/96CbUN9fDRbXR0tLx6vOv4Hs33Iq2ulbMmzMHFTt244O33ub66ZTMpOKK5IDq7OrEw//6F1q7ujFywljUNNSxVpqRlSGhs4bf5xCL+s/QDNskh0U1CyxWN5qbWhkYRXBZ4h648IorkJeRhYXzFuCVl97AZRdcih9cez3OOeUURKqrMX/eXJgohk2586ShRHz8StyFJloQIQ2msInDZrJInFCfRQ4XKIU7vsaZFArSdDNrFkQ7PCgeNBi3/vBnWLduPd54/Q32fNOO9/tf/Ah/+fVtSHea0NXVxrkKqm6f7IC4I7bolaNQuMdhtZrhC4dx6/d/it079sCRkRMrwmFiUgsyTUgNFmYAyVpyQtEC5GuZ1eSWPgJOiSVLx8aTKRSJorO9gwuOEt5i6tQpDEqiaxIqj4gvq6vrEAzSbmxGOBJFGmkEICJOUVbKZk/BiPJJWLDgdKSmpcNqdSAYJm+KCJHZzRbUUrRlF0WO2nDH73+N7LwcTgsuHz0M37z6MpSUDUJtdR12765AU0MzRyXoPh0WK+rrm5jh126xC5SzLD9PxCBkijLwMarDF4mKdF3KJPR6sXHbdrzx1lvYuGkjCnNyMHv6LAwfMoiBOr4w9YfcmAwsZ1ovZoQ6YhW2aP2FCbYchT8UhDeswUcEoeEI9xHJaTqf5bJxcA87eMcTqZZWjVyBVraEhae4Z01QYxJFSUpAGioNPv/7KLUBBe751lVXYNeuHXj/nbdx1fXX4rJrLkN9Qz17kQljbYpqeOKhf+GPv/wDqvZXoSC/gGGqnORElrtFVKahDDti4KlvakLZsOEYNWo0KqsqOa6q/AdH0lQaJu9qVjvWbNjEXG5+r4/FRkFWFs448yzora2ItLWgbNgI6P4gzFYzazKIBEW1kwj5V+hVVj+h93mBU4xdgg5iP8h/JJGqScBRFCXxetn8uPkHP+LssofvvxcVe/ex3X3BJZfin/f+A/OmT4Sns419AqwN8OVUPPjw62qy4gwDpSIRpLkcTJ/937++A/v27YUzPZ37hFqU4k4kTAl2TTyOFpvgojdbZa1DhcQUFXt4hzdRzQErYV+Z45E0tAnjxqOxuQVTp0/j+o+ZmTnMWEx1EijcS5wRhPegOUGFN4oK8hDwh5imjaoABQJh5OYXY+jw0bS7sBNVzdEogFSHA2uWfQJvdye6PV345W/uwPz5J+Ocb3wDr738Gn75X3fgww8/QcAX4PmdJZOuKAxHJLD5+bmw0LhysVWgra0dBw8e4rnm84ukJhdBfH1+LF25Eu+89y4/+8knL8Q3zjyLU9xJmyToPVe0NvpliKvCyH1kCNQZ0elq8dM+4AtzAib8wRC6QkA3OVdBpK5SSyDzJRoBJUCbyHXR26GYUxQ4htQ6Ngk0AgYdPv1MCRBfVlClI7y/lrSwg9yVfnzbT7Fr1248+I97cdvtP8eV11zJWVhMsOjp5rJK0UAEr7/8Bh689xH427vhklh/k9mChsZmXHr5ZcI7DZ1jpaefdRZ279mLmuoazuyKm+wy/JXkjuLvWUbVKFRE7C5FhVRoYgMCzS2wWuyIdrdA07tghge6pzWuZp3wkBuDb1LiS7BGNBRENBIQAiHm8OsFs2xMhzO+y4wqEeiebpx1+mm46NJLse7z1Xjn3bdRtXcPRk2egEcfvA+/+MEtcNmA7u4WLrxCizGxGrFqykkrMkU19ipnuDPR2NaJv/3fP7B1205YKMzKi188I4dsZYiUsAwivGzQImV2pWbVBH5Cc/JC7WjrQHVdtdAozCbY3C7MnXsyIxwzMjOQ6nLh4P4D8Af9CAao+KgJLS1tjBgcNqKMhUFzawvzDu49eAjpmXkoHz0WRYUlcFLKryYq8JLga29r46xOT2c38gsKMHnGVHz/B7fhwQf+BQ/zSbqwp6IC+w/s4+uSWUmLm4hcVdeTPe/xeNDe1sGh5iFDBguHaUUF3vvwI7z4wgscxibNcPK4cUh3pSLoJdo6aVJLcI9VsVRRtqmRDkWt9Ejykad1548A/hDgC4bRGdbhYdNcY0AfpaEJr1UUWakaQlYf+2LikWKGqa/qfkQpeUs5+ozVgQ1JgkpBpb/JFCCAj3ymPh1MtCPsq9iPYYQvTwjHqUQc6tif3HYb3nr9Tfzujt/hvAvOY/uqvaWdudVWf7aKbS4q7EgtQruqJuwnImuwuZyYNfdkpKdn4FBVJXLIAYYoZs2egy9WrkBXp0cCJ+TCNzL+sv4lbW9B7RsHFOF+MlHqchc2bt2GstJh2LJxI6bPmSPMc4mnFxl2KqtN9los1C7EOj0/e5GptHowDIvDgmiAKLZo4Gy8G7I6za8CcMMLi+sGKrvaoClIcAbV8wv6fcyedMWVV2H79m0Msbav+gwLFi7Ct2+9hfML7rzr7/hw2Qq4U3MYGSoqLfdMTKG29+iZ7MaSgxsOhFFd14K3PliM9q5OzJszk6m2IqEgzOQN58rMUZjtouAoqcqahRyKQmUUsiKEqJlqD9jQ1tyEiv37YLeRHR1l3oANWzZh3qxZaO1qZxODCFlISBD4acTwQahvaOPoy8GqGgwfOgRjR4/G4rrV2F9dA6fDhub6evg83eju6uAwo40kQFRHKBpm0pmWhiZEfH4cPHgQJy88BZ+uWA6zZsLEiZM47t/R1cb+COLjb2io48y9ir17YbJY2O9AxKcqJEhO4DfffhMHDh5iqH1qehpGjB7FfhhySHsDokSciVa7oTHxlcnAoGVMf0wIs8dMMrn4PWSC0+4fjqI7HIZXMsUpazFsiiDM85E0AcJimKH5Qn3sz0ZQj1zdbGqSqoUIrPxfT5EKpWLQvx0kyOXfJM3YEWxoKlxCBRKpqMfMWTN6MpaSCQqZynjoYCVe+vcr6OroRkNtHQYPKmFAzbov1jHlVIDqwdutwg62AdW1lRg1biJefOUVzmUn7y0NyPYdO1CxezdWLV6K5sZWdiKR0wpRKrdsyHVWoTK59pXEM0YNVLVXCgXd9pPvo6WmCtdcfiVC4W52rrIg411cSlKKcxNKTsKXOdJCtrvVhiiBUmy0SMIS8RgVZgMxF8kkEPaoU1KIldhlRVird+51DWEPDbeGsM/LarbF6eTF+MWGtdi5YwecDhfmzJ2LktIheP3Nt/DQY0/iUE0jO81UCXi20c2iHq/oAhlyUiXGTSZ4PD6MHzsOZ5y6EKaQHxdcfBEcTjuCXV3sDKT7s5CDNhgRUSRKDApFELUSCYeVzRZSVc1p6VizbDnOvehiZOYX4fzzLsGUabPw4Ucf4tvXfBvbduzA3t27Oa/CYndg2pTJWLDoJHR7PaiubkCKOx11dQ0cqmv3B/H2G+9h55aNXFaqrbYefqrGTAIUPT4PmtNWuwsTxk3AjbfejJAJ6OzowJ/v/Cvy0jMRDgZQVJyPiEnHggVzmeh02pRpeOW1V/m5cnJyMXhwCc8DqsFHJd1IBS8bORTDRoxgLSMzM4MZf7lLE1ddkrEjU6u3ptaaAPcIc9tDvoZgGP5QCKGwJrTxKPlUzBzxYYyNmtqSfrn/Yu2xbU6mcjOAwAxzlMIx8cQZ9GCksqj1o1pfJgBNoMrKQ5h10kyRYdeLU14tltIhg/Ff//1TrPtiPT54dzFef+1V9vyfPPtkTJo4EQcOHsTefRVoa23nEAfZgmPHj4PbDhw4WMVFFBvrG1E+ciSeefop1DW1MMCEwSqSqEKk/CWztQ19oqrYSNucVEIKW3788RKMKx+OvXv3YMSIMhDML8LZksIkYZ83w68jrFgw94Gs/EvJMLy7cwxd5KubbTb2kNO9EKmJ3ZXC6jX5NojQlTzAlDFnJjhlMj2OSDCtZgS9fpgcDt4iIuEA/9bM2XMxc+p07Ni+FRu3bMKnny7GuAmT8dvbb8Mb73yApau+ECn9XMBCzLQY2jP2KwIpSPdCavLGzZuQkZWJyy+6gElETj/zbAwZXoZwewtn87DZxKXbySyIMlkr1Xk0MSxYalpmC7bv2A6XOwVzZ5+MVEqeysjAxAmTWBucNZOKvVAIE0hxu7i2QlVlA0rLCpCS0glvdxfzRuzasw85xUW4/Ior8M+WRiz56D2kmO3sjORxi0qtlngNeH4FsXXrVqxevhKN3e2YOm06Y0xaahuQkZomNoaohtrqBhSXFDBt+qjycsFvWFbKkQQeuxQLTjv1NGZLjjAhqWB/ZO2dfzaB2URh9BNQtL5QGN0dXaL+g2bi5DtK3mNXsFT3RYagMLUDEQ0+YpgmJ6JGi54elHw7ugBnStPcaG70KwDUlGJGNgpHsyohWF4UKChuoSbhX0vG/ad2zeKSYqZTpqIMkyZNinGuJQOoGOG/02ZM5ePiyy7APx/+J958901OMBlaNhTFJSXIzc1DY0M9hycaGhtxoKoJc2fNxeovvkBxYREOHapmvndfMACXiyig1B3bRRz+8DuWTxXfk4ya5B08yuSNn6/fgJzcbGzcvhUjRwzhnZwWdUSnxBiTqD7LrC1m4YxRWgDlipMWYDZzFqHVaZW7hBAWbB4weIfiuRTvsyJKbMdsNgiSFEGqb1j8ckDEdWS4iMJxEfp9C0JcdtyCMRMmYczsOWivrsIBwiXUV2PGxFHMJ//mB4vR1tqBzJQUFpRh+m1CkMR+gqIhQgiQwKJsvsWLF7MJ8aPvfRfvvPU6ctfl4rzzvwFrqhOR7jZE6Pu0MKImmO2OmIkkTAGijg5gyadLWKsYVFKMQDAAp8POmZ2E7CstGYSZs2Zh+bLlyMwsZoxATVUzV9otG1yE7Tv2M8y3pbUThyqq4Uz34Hs/+jGqDx3Ank1bkJWRwjUmNblR0StRyLtsFLqzsV1fNGQQo0RvvOF6PP/kM4KWWzOjo6MNu7dUwKSbGexTWjqInYEjR40QAoUiYqGgqHloJopzDaRk86yRxUlVbCkOSSsLoAaNs42LgLhhNRCG+gnYoxNnB/UfbRICPky+DHL+EfVdWLMhROoV+WkshHykMDGtqXhwm7qHXhc+m3sc2uihV+KbFoJQSDS1WRququahukai+m8UArQYrr76W7j7nn+wR3f27Nni92W5I3Ve3L1RQllYqMijx5bjnvvuRndXNz5dvATvvfE+9h08wGy9bH64HZg9ew6am9uQMigdOdk5zDJEH5JGYCE+dI3UfgmPZWTd4RmCsVHqsQF6Po5tvFQKKoiNlC4a9DMHYFZGKtt6gZAfFpM9xrZDCy8cDvHuH6JX6lmuWqbq4QmgCe3U3KcUdrNY2TlICTZMwEGTi7IOraQsJln86p8UeiPVO0BxfyLfIO+6xqYF96MeQailGRlZ2ZicmYXJJ81CoNuDRd4gvnH+BdiyYxv+/exLaGhoZK93mEutSf+CETdNvoBwmItQvvveu1x15+//uAtLP/oQj/7zYcyYNwvTpk9huzrq6SKbR8QGJUaYtSKXG5vXb0ZbdwDXXH0TwhGiF09FYVEhjw+p1w6HnX8jO5vKppNDzoqDlQfgcFowZUo5hpaVYe36rSguKsbatZvR3tXFlOZ33vUPfO/aa9DaVA+3087ziyKsNH7Ec2GxOli4VuzdjwWLTkeDtwOjx49izWPd55+zs7iru4tTo8n3JDYrYd5x2q8MLZIJajfAcplqzODAN7p+jfPrsNgOwZaDIXj8AQQ5eckEs9PNY0fhPBHWE/kV9EobSpiiCarcjXJdJeOJlGN2eG1AztMW/Ol0WGlXOpyMROT9U4hCcq7DUMSTPIuk1fK/aV734gFUC5u8+Lff/nOOV99//0PYtmWbKBBhFnXkVVqlOmgSKxplol/atnUb9h84gMGDSpnNlSqsBMN+dHS3Y8r0qTj11FMZvMGx4lAE+XkFaO/oQm1lFTw+qpVGYDo/q+u8w3OlWsPRA+Po6QFjnFxKQFK57LZUVNc2Ye3mHVi3dTs7wGhRiAER5ykaLFKBlRZFeHTSCggLTw5NkuokOCKhgPSiiwxDUVlHoPFUYgYtqB41y9jZYmfl8Bv5FKyUHy6y8Lj2HtUjsFhZK2FTggRBJISI38tMP1n5hZg8czq+ffN1ePCJh5A3pJiLVVCKMcfuWfglRB5k3kNubi6Wr1yOn/3wNpx1yUW48rs3csnxx/75L2zasAmaww1zRjo0hgRS2I/YqEn4mrF01RoMGT4K2XmD4XJmMAiIMkEp5ZXCmYTzoMSbWTOno7m9DV6fFx5PJ2eBtneJxKRxo8uxb28lRo0chsaaWv7NPQcO4c/33AOLI4V9K5qkGyO1nJx7LBwjQHdXAHt2HoLHF8G+mnbMWbQITc2NOFRZxbRzHR0dzPHX1eVhgU0CiedlQkHPRC4AtW56S5ZVTnTD+hREO04HdJsN3qCOjtYudLcF4PFG0BEIwhsKwk8Lnw6mtQ/LfB7KLggIvoZkITv1G81dQV1UzOkRAAw+S7xDdffG9dCDLFRMWUfVjMkSWzdvxfKVKwWjrtvFZAzkVaWQH2GraQHTAFCMlxZQd7eHd9C2tlasWbEOe/fu505zOR2oravk0lznX3Q56uqbOZ+6o8ODpuZ6DB82GN+6+locqtyLDDd5mgOwmSnZxOC9ND4824xigsbEuPEcaRzTQs4vzIDVZkJZQR7+9MufcuCFbDiRm0DUZpScJEAjhNUmNZ6ei+C7pD5SklCYdmsrPW+I88TDkSCsNgcngJATkwpXktnAHPRuggcnFgcxRANINfQHmbODoggcVjKZEQgHGWATDgR4VyN2KKvTDj0UholIYcgxSv6KSBSW9BRUVlbjputuQosEv6iU7N7yCUhwETPu3Dmzcd9Dd8PsdKF272588fkaXoBZmTmYMHEyMtJyyCcNzWlDU2MT7v/no0hLy0Jebik71txpLkYtEvKwvbMTs2ZORnubB4WFKVi6fAM+X7sWKU436hrqMXXyFJx19ino9kXR2NyJjz9czMy8S5at4GH7zo03oLG2Fr/5r58iI9XBCV6ksaa63Rw94np9FjemTJ2DCbOmIjMvA9Omj8JDf38YyxZ/zM/k83nYFLrxO9fivPPPZ1gxMVaTqULmFVVDys3J4WIivKT6MGuNTW4/MTAP478YVBdlnr/2Dg8s9hSeQ6wdkBmuliVpAAnXS/ZriSYAW/WEl+YPTGIHN/f2Tabk7SHuCAciCPhCHAKK+sMIB4k/gKQejqgpU4Ak5viJ4/G9730X1177bZSXj+JyTStXrMSTTz6JB+5/EM8+9zzWrl2Hg/sPMkEjFV48dOAgnnr6GezcewBmRyp0E+1mlKKuY+Tw4ewVJpWbCByJZ91itqO+vhn/fv45TJowHfkFJUjNLODwiQByiVzxHi1AIi+YCUgSICiHjaEaDX2TcAedna2YN28mVq1dj+Wrv2AvPWWmMTiH4LFEW0Vf4wVMbkFi3FGFSsg3InwE7AQkRF2EsPNWNgdIuLGzlGM/YVkpOLEcTMIUMOkwOeyM4dRsgvSVEot0kvT8vKSTiGsKTYXUOMrQo3s0Md16xBvkxXjP3+5Cp6eD493MM2BEECZO6DBlYGZi+fLP8POf/AoRnwdFxJP3rWtwzpnnIC+nEK2tHub+Y/eizYJPVy5j4Z6dnY+Ro0aiZHAB+1EoIkH5BxRqI7uaoLwdnWHMmjmRTYeWtlb2O3zy6Sc4eKgWXiock5WBwpJi1DU24MzTFsHX2Yk77/wzxo4fj+tuvoUrEqW6XYLANRLmQqykkbW2t6CFoNKRKKoOHGJmo/KJUxjZR+nmxAdAnJn33fsAnn76aTY/91ZUoK62jiv6VtVUYsVnK/DA/Q+xqaD6SbV+WYJoJ48A3jDQGgY8ASLoNcGdmQ6by4aoQ0OQtEBpipPQiEvWN8BYjPu1ekNhvFgf9XFNYXAdcUIqkZqV4iScey9FFg0HZXqRLUIDRJOWM61IgtqszNxyNE3Z/UnDgdL2J7ipoom65+778eprrzBNVSRiQ3dnFyaMLecFu2zlUrz16ltIzy9AZWUVXO5UdhBSpdTmxmbkpafyZF+zehWyM9Nx+y9uQ1aaW+DZY8vHkNmkkimMwAlTgk2lmdDR1YQzTluIsiHDsPjt1/Hivx4WzCxUHpwwChx1IIFLvIQiHEXAIepPEg408RhKG47AYjGxum9xuBDwdjOmQVTNoTAP8QI4OKbdg9BJVE3EjRH9FlNGc6jVwpoF+QBImAgbkRB7IvtNoL5s8poqBi3uw+yyY+mHH+Lnv/g17FaX0Ei47+V1jBON+0cXuRit7bjs4gvwmz/+Dyf1WEi6OB3i9ohMxww0NzbikcceQWZ2LmbMmIuMTELcRXjRZWZlwJXiQnNLKwaXFqK91cNluNLTKF23BX+75z4GA3naOtgB/MPbf4mIyYxd23cgIzUFH76/GHNnz8azzz/P9NyPPPAQ7vnrn7Di0w+Rk5vLSELaHIIBH1raOzGsfCxOOec8BCNBzD95IUKhAH7xnRvg6/ZwliKxQXV1eKCZQ/jZL36Cq6++8rDpuvaLdXjyqSdw+mmncyEUAgcR0zJFZdRcj5UaIy++hP0GQwJaT1Ww6DUUIlMxRCX+ENaIL4B8AAa5r8B2CQGgWCDL4HCI/VMiCUWaEW0IVhOXIbJQDDrBZGAHAyFXqUxXhDIBKKFDxH5tFrLLiD/NgRQKU7jtAlveX12xXpqy85VGQL+nWHY4jmy1wOV2siPqBz+8DY8/9QJgdcHrC8Js0jFh3Gh855Yb0NBcz4uxpLSEU4RpEtbV18CR4kJbVxeKSgejtqmVB97hToXF4kJqSiYQJu8p4brVQf9Wh1z8KtShZIN6j2nxwkhLS2HikVGjRqFg8DA89Ni/YElL4VgyaQgEsKHXcFhQkjFxJmkDvIhMHPahi5FdzpxuXMeACpxSNIBgtULd53UrQ4jxi58kETlfyJyhABS9ki9FRgCYGlppJDrMNgsLA+ZFYI1HSTXjRBAlyMLdHVhw5un45e0/wogRQ5nYpLtLAqkSNjWegJIQNCsnAy+88ir+duddsLgoVyOKqDcA3U9ZaVFQZfDPVq1EOBpBQVEhiopLoDGbkCC5YKcaCS9NQ3enDw66Z0oL9lJqbjYuOu9c7NqzB93tndiydiM+fPVtZKZa0N7ZjZEjh2L23Ln4ZOkS/OwnP8XQoUNw06234Iqrr2N4sM9LQpi4BIOwO5xsDpCgJsdtSkYmdu/fC3dGBlwkIIIB9vpff831GF42GqaoG3+98x/49a9+x/1Ajfw0dEyfMQ0//dFPGSJOyMW29na88OKL2LBxE5tGap5zbEma0rTzc15NWIB66KDEnWDUgmCU8h00Poede0ojVyapmpMWQ4hBvRq11RiltqID4p2L8pcFmQNRNDOBoBxI2pQJVBAOi3hvhCrZUn6xZF7lyWMR7K9Gbr9jaSoXWzkCVdiQLtzZ2YXv3nIr1qxcjQxnCpwmC3Iys/HbO/4L37nlahw6uI+zvEaMpmQiB1qam9ifkJWZjYbaegwZNAh79+/DsDGjRHZXQx2XaiZiSYFRlx5UefQgBFWHyDURWxxxIoyDpmazA08/9Sxu/c4teGvFZ3j//Q/gyMxg4A4xsojvkfpPTlMKAcZwsbIgBr2KoqhEfEGxfovNwfY5xdMpLEjmQQ9O1Kiy2Yi0kEOFvJObrNAs5CewwOyw867KNNaaCVZZZlrBn7kMF0OBE7xSKnRldTDvwAUXXYgxY4bg7HNOxdBhQ3lRsABJFAKc8afzfKEKOk8+8zQeuPt+mF2UtEOO0AhMThMzHpMTj75RXDgIDoebo0KUvETMu6QFkBAPU2y82yvCXuzXMKGtI4J586fiogsvRGVdLaw2M15/+Vkc3H4ApcVFaOz0o2T4UMyYOwcvvPQifvrjH+K8c8/G3ffegykzZiKsE2+kn7MPXSkpyCsZjNSsfDhtbujBKCr27IGuh5CZk8v8Ad2hAGYuPAXX/+A7mD5jPNIdqXjmyedw5WVXYuPazQwIU/wAw0YO46pIBAkuLCrARZdewr/x5nvvYcXq1TyvKTWYNl2S6wTooWS6UJQo+Vk5ZzYugt7R2EhC5RgGh/8pS6Alqujx/+oB7NErVUe2x76nwrqU4eS0sfoeNWIAiHecsgKtJljNZlZhCIiiKrfEDboqnnF0FkCvTdlM5Pz6+c9/gU0bN8FuNmHqhLE47ZSFuP/+v2PCxNGIRkM4VHkQpyw6A7PmzGXnCaGvyIakSe8NBFDfUIchQ0qwY+cOjJs6lp1q5B8g0kvm1Fe52n2kyfI6kRI4MXWAdnS3Ow3r1q/H5o0b8bff/hl3/ulu7N2+C9b0FEQp8YdJMQRCixYyh+MsFg7rcS4OvTKCUJRtJoIQZtuJBNk8YNQkhVhiN6nulHZ+JYmle1ZKfJNGcGIbzDYHLBz6kNoFxd5JCJGgcKYI1b/X8s0UnbGxNvitK69AW1sjfvj9W3gDIcFizH1P5Fgg4UpouEcefRzPPf4MzES+QZFvDfjwww8QCIWRnVuAQYPLeLck4hK32wkrOUHDUa4WRI5QckDSDkuhPIa5ms1o64ji21dfjYWnLUJzRxt8gQ7c87c/c46I02VDY2szyspH4fwrL8ULL7+BiePGYOZJc7BnfyWKhwxDdm4R0tKzkJKSgaJBw5GSkQO/j5x6BNgSeaHp2dnwBnzIyM5COlGVZWZi0aL5+PerT+LXd/ySSURvvPEm/PbX/4P33/0AX6xZiz279vG9yonBDt5RI4bj21deifrGZmzatp0xNnpIePFp8ZNpF9KDCEaDCESCIlzJXH6U1CMXv27Y9CmMSfyasMPG/1evttghWL16lAQO7ZMGYKcvSzpvMWg95BD0YwF50COoTCVyIlEuMZck5pFNgvbp2+F5xE3ZS3+/+x4sWbaM1cOLL70ECxYtwFnnnY6xY0dxGSQq1kB24pTJk7hcF00kyogT0k1QoDfWt3NMPjc/Fweq6lA+ZgzjyjOzMpGdk8NRBeKpo3LmXHTS8KrWVEwCK+arBAc8mS3pGdl49F//Qm66GzdefRVu/tHPUVFxEM6MDN5taLGItUthPYFrIOoyCkcxPFh6j2MKCO3gVjs7NEl9Z9SiktyMNXOIXZ+b0gOlikD3SBzx7NwhDYe0AwI9kcCg9+3QHC4c2rUXPkokZ/Mh3ryJMb+y4NdQOHgQJowage6WJvzw1u+y0KK4PJsaEiHIr1zMQ2hWNHHJRPrLXffg+cef4Zp7y5ctxZ6KPazeulzp8PmFdmkjbYUcoITbt9vZPCL+RYJBE214c7uXkXa8a0ZM6A7quP2O/+Y8jPbuEA4c2INH7r+P5yc5+Ch0mJWfj4u/dRmWfrYWI0eNwc9+eRsWnXU+Bo0oR0ZOPvcvja3b5eaQKJkflHjW1tGF1HRKHY/gqqtuZMxBbl4h6pqbuDDtDd+9Dq+88RJefOXfKB9TzhWVCIBG5jKNlVDTxdry0e6uA2efdTbWb9wYy+eX4ESOTJCJRp4ogoyzYCCbXQHvZESZRpqEh4NC7hrgRBguhONe1WFDCFQ7hBiIbHQBuieLqAMSo/M2FKs2zuUYQxiziar8f1mOmA6KOHK1kb5Kfx1DU0wyK1asxPP/fp5VoeuvvZ6hmikZKZg+cwp70MkRRTnbK1euhM/n5d2VQDNU0KSprQVZ+UWwOlwYPXIYtm3aiZIiIlqMwOv18GIkfPeo8tEIBoKMViM8eBxqQ5kFUiCQs5xyOUiI9mDkeyIChO3v8gVx98P/wDW3fIe55L7/s1+w/edMS2XnKcXiFYuwgHyKHZlse1o4VHOALsp8c1RKi14ppETqvxI6/CrxCzFhpEpjClWf/AsdRGAR0qE5nQYNTezTwUgYT/zzYVTW1sBE6jlViVbUT8rnQVuGJIwgiHLY42ca7rrKg5g+cSzu+NXPeUIJISAgzYQTIC99WmomYxtE7kMUKSku3Hv/Q7j3b/+HJZ98wjuvyepARlYBvL4AO+SojBplbqrut1mpaG2YUY/NHT44Mlwi5ZzmHdWxANAZAK6/+TuYOX8hPAEde3dvxGP3PQi71Q6n3Q5PG1G6WXD1jVdj5779OFjTjElTpiNKVOaahYE3ZOd3d3Yg6A+xyUvkscQBQA68m275AZODVOxrREFRCnSbAzt37oz5qYaPGIYrrrgcN9x0Pc497xyUl4/gCA9tnuTUo3ulFHYqjkLchZREtHjlaoGnUbwJfBDXY0+0nRO1GXtBKcMUNRJ1EkyyPqXFJGI5SQ+NzEgR6YuyTRvlsKtuskgfgHRmHRb+Ns6vw9dCHJqJbRVOFEoGaTp6n4BKECIiiHv+7/+YqOGbl1+OhaecjIPV+/CNC88UhSBMGgL+CP729/sYIeZyOZl3n3rRGwogatIR8nsYt+1Mt6OgMBdr12zByBElCEQi8IcDPPglI4bBlOZkp0ei+a+KU6j87Fiqpuw7Q3HWmNbiSsvCklXr8MmH7+Kvf/5f9vJ/57u/Rn1dG+zu1Bg01Op0sklAURQeWIuNCS+UKm+yUsIPqWvyMDI5qNplJhLPh3tfdVh4d920fiNee+UVfL5iOSJyN2I/jt2O7Zs3YuS4MZh3+ulcUIKq/QgUmNQelEMpJgTIpUA+BRtmzZzB4bcZEyfgzj/8lgWxin1zUZBwBO4UFy695CIEAl2s5ZCWQJrMO+99iGCAUszdsJkdTAZCXyOCFTJ7yHlod1CuvcbhQdI6azo7kJqXLnQMWVaQwDAUHqRcfEdaBu747W8wde58WFwpeP+N5/DPe/7OFaEyU+zwewOIRky48MJz0dTUytmDdpcbFjsdDnR5vcwhSFBgihgQHTgRfBSWlqJ4VDkrTpQi7PUAI0ZNxLLPPpPamygEEglGuKw7hX7Jh0NOu4DBVBSbBLHz6Ljs8su4cI2XCqeQz4t3dkrSscKq0f9FeTAbVesiAhp6X6fxIQCXhR2zwtdD2hzNkSSvGgGViOGLrkMAPQvsFhtfs8dSl0U6ehMC/TVlMrAQUOanUX04SgGgqqguWbIE6zesx0mzTsIN113PpApXf/tbAkREgAhdZ1AQTZxTFp0KbzCItHThee32BeByulgbIFrr5sZOlAwuht1qxe5t1Rg9fhy6/SHeJakYyoiR5Qj4RGHKRFPGTNlVlK9CJknPCov1gVFpEIIjCpszE39/+FG23//2h9/hQE0VfvDT25nTzpKVy/nZlPDD0p98EKR+Eycgl0RmadCzG8eBSQxuXzqXc3VVRzOVkKj0TEVV7U7MP+10LDz9TCbaqDxwkLUIvlwwgPHjJmDO3HmIRonaikwAXWWAGLQK6g+ZFk2wVCv5AkJsQlHkZ9eeXVhw6kL8/o6fIxSg9F8aG7I8HJwdN3JUOf7rv36Bjo4u3gkJ0ZeZmc1hXXqO9NQMpm/nzECTCalpKZz84vVSwhRFMDS0d3ahoKhIsCWFEFOPOZEzCnZGkibX6Y/ghz/+CU4990KUDB2J9cs/wZP3P4DK/QeQQqhC+SjzF8ziRKqhI0dyvQYiG+EiOWYLfF4PiosKMHpkOZsg5eVj0dbRwV1Mzsm9+6swddpENLa0w8d5GVQtghCBgv+AvHqUp6HgxnTQc3OxJDoiGoJ+YGz5MNgcVrbRSU3nVAku/WVm7YHeE5RwFqa6p0NUptZgIWcvAf7plzm9OckhST3IB0SmL1f+JuhzoquO5o+6gaMVAtRILSOpx4f0bNJxNE3FlpcsW8Jq8O2/+AXee+8DXHjhRewM4kUm+fKoivC0adMwsnwUGltakJ2RxYANciBRPlZXZzfS01OZXpxAG+MnjkZdTR1L6szsPKRnpKBizy6MGTeOvb1WmMhPLZ2kpJ6HETTrDMIgSrIeCq2eF7tizJGsOTY9ilQbRSK8+PPf7sLY2VPx859ci7qGavzjkX9hyUefQnOlQrM7EQ3TTmcT7L8k2SmcSio/CwVVPEP1jIG6hex5ngRWA1+BgjVT1ovM648C2fm5mHfGIgwZMoJpnWlS0cyzUBUjyjMgxlOeL4pwirxSUrrzK90DXVdoBzo5hVPdGD1yBCorD8LXWoOzzl2EH3//u4InT2Ll7RYHnn/ueVzxravwu9/9WtRtsBI3Hzn4IlzurLm1DdU1lcjMSEVGJoVlXaxRCqg1aTGUbJTFnICU30K7KlPwkUBmDTAEm9nJCT2dbV0w2dw4+6ILcfV3v4cLr7waa1ctx1//+mcsX7oUnR1drEHQdYaUlcGa4oTDRQLHhoz0DHbUdnYScCyNQ5BNjY0Ci+BOQacHzAXZ1FqHlFR6Bhe+WLueu43mjTfkhyfkgzcY4NoRFFKMhkVokPxLbAZFSS+j+ohRBLxULVpA611EMGORBy9w4euxa1FYTSFYTYTaDMCmEZ1ciOhtpBHuR9REr4qKvgety5yxUoDrSqNT0YDEBcesTHL3Zq6Go1i0CjSY+CZJac5aPQoBQMQL559/HoeG0jPTMXrMqFg9AIoHr1u7nivpOl0uWJ0OrmVI4TOaJCkuN9c9JAinzx+G02FhaHFVdR1mzJqG3bv2MpOs1W5DyONHTm4eL3AS1RZzD1sL1wPSiGI5iNzCIkTqKCRJNfiS51RJ1w7v7ukp6fjokzWY9MzTuORbl+LQwSpU1Tdgy9bNOHSoEgtPWYDSESMAjwdRKoVlVQVb1GAqKaPAn7IjlZGY7NeVeccVIQVWTA9p0ENERKFmgYr3JQB5koxf7M9Y3XqKMhJ/XxDDhw7D4k8/RUv7HBS5HLjqqku5zPmzL73EWhc573bursDnq9fjkiu+iQ8Xf4SuzgBSUzO5wGxKahoTttCFqchryaBBYgNjk5Icz+RTsTIgKhQRue49t0YITMoLaYHD6kIooHOiFZkadlca8osJTpyGsZOnYvPmTfhiwxfYsmsHRo0YhZzcArjTiNYrH4HWDlFcpLOTuQAIUUoRj2gkKFKuzWb2BXg8IaSlWjlKUdvYhbHjJ2HDps2YP3c2AlQTkTQRojNDBE4n5XcIvgdeVmQSUEq2SZRsI78CO0sDgN1mYiFAFh4rgKQJhDR4NSv8UVqsgu9Xre2eKIta6SamhxfFbmz8LuNoiH9BhpfNZvI5SY9+r8E6skMMdN9H05L5ChieGzk6ZOCUKVO4WOShg4e4ToAwDcTtUyz/xZdextQpU7F562be8QmfvWv3boQCfg4dUpdk5ucxmopUsNy8TCbwrG2sx8QJI7jTm9vauKOobFhxQSFzKznkomfTV3oByZ3i0yIYPmYMX5uJIPvpDVVb7/5HnsPOjRW45ZZbOcGFhNKkCRPw0eKP8cJTT6O92wNTRpYABjEWXMZvZZUiEauXJcv7GwBVsIHlhWBlIdQgWxScO2DkXxiYqGehS0Y6M/TqnMxDfgSC3OYXFGFvdRPgyOTn+sH3b+LkLJVARLvqpx8vwcvPv4BJkyajdMgQpKZlcsiUGH/KSksxedIkBANRtDS1w+cJcdoua5GkxZHJQejJhCJVgo8A8PuDyMxMRVeXjynBKXzIJCZ2F+cbONLycO4Fl+Ca667FyQvmIq8gCw6nxg41u4UIXk2cXEZmIz+b0w5HKuUIkCJGOl0UHl8nOjqbQfAJl0tQk0+ZOgNV9U2oqW9EY30bwgEzPJ4wPN0EcTTB5w2x2eXzeOElDEMoyr4KCj0T0xA5oQmEROcQ9QP1rJM0SC77Dbit4CpZJvbfEx+A8ELR38I9aJf/JqDQ/8fcX4DZVZ5dwPA6LuPu7pnIxD0hgrsUKaVAgfrb9qUKlbelTmmhCpQWSinu7iHuybi7u585Lv+17r33ZDJJkLbf93+ba0gyc+acLY/csiQEl8cNh3NC6i1u9xS8Lgf8Pg+8bjc8M04F1afO7A8duZIjfISV/Cc5tEVAwrZPuLJsPWuLtFYSElngY5VaAa0wV7z397/Hxk3bkZqehmiGZkPDWLhgEeoaGyV8S05Okqqy5GZEwrEV4wkiLSNNJKCHxsexefNaREREobWtXcQnCkpKZQeQ7hnvxRywBTEQI/0DyMrIhC0iUnTXP+rCpYBIzLk3hJ/d/QdBAt503bUYGxnGwcP7RIUmNS8bTz39FF588gmMjk9BT9HT6GhJEWCx04lECjs6cugpnKmFerozTf7Zuy5yW6pm87zmrsZA//BDKXAFYLCEScvS5XCccP5h29JuRUZqBoaGRqCXgibRexZ85/avCraCBdao8HA88+wzmJhwYMOGDahrqJfaREJymvT+GdmxG8LiKMNxb1CPGTex904BdJHSLK7jYm+lFtxYbDSEMDExJbUSa5hOdfmxitQXF2emIKmp8cjISMWMCLPakZaWjaycfCSnpkvdhZvJ2rXrBN1qNRFfEIJ7ZkZayC63U6JXp9MtcN4WaVkCiQmJokocFkZpI+CNN9/AyNgIevp60NjQKE6+VDRm4ZnMQceMW4qHhEXzvUZGx6X9ycWfCkbULmQ7WrG5A2xs7RmV7q2NbFu280LKdOf+rrT06KXJyMILl3tC6kwWkxk2u0UwB2x/Uk8gEPDBF6CoKbscaqowCwU+w6CVyc/8jLriLrdCQf0Pi/scjhrL6eP8skY2yczMxO49u8X5RRuQnFDPv/AiBkfGsWHbVnT19OKc7Wdj1we7sG7tWkTHKtBNkTDz+uFxuuB0OhRQjVGHwdFRZGRmYWBwBJPTM1i1crXkafUNDbjgkkslX2LoqdUypVnCjZSh4NiY9HjL1qwUOioHzinHPCYGq+MEtlCx6Bd3/wbFy0px7vZNaGttxdMPP4pNZ2/BrZ/7PBISkvHck8/gn39+AEd37cVAVy+cU9PwuT1wu/zo7+5FbWXlrAb8KTf4I9Os060apzl9ItTY3mLB02yXnPqlF57DBRdeLDUWyqJr4iY84uNiMDgwILuFyWoW4M7C5ctx682fwTjFR0wmQVxmZWaILuHE5DiC+iDikpJgMBJCbhZHoIiYWBjCLTBYFPlwFxfpgWE4ZzxSFrHbdYiK0iMiQo/oSAPCrDoMjYwhMj4G09MB0U2whVlknHGxoiSWh+1PvQFJyQmIjSOvIAx0xjZbbbDbFaekmKQYJKWmYmRwCJ7pGUQTjSjwbXZn9CJGEp+QhMHBQbjdIUREWKTQPDbuRUFBEcorKjE6Nozunk5hp7a0tqKpqQnT0w4MDQ+J/ffIyASmJvjvMVm0GJJTt7KpqRmj49OYcjhkrEpThyUgRuNMy0XFiJJwOrUxo5ScRVdQIB5m4UqYwy0w2o0wMpcg61SvaEsSom+2GmAyK45FVoF+A0YRoyACzDBnZVUfvKJzrvSjiULTrIa1MaSxBmd9JD/BoakNz2HdnPbQ8NI0e/zhnd/H8OCQoOa4q3R39uKxfz6FW774FZlE3D0uvuQSdPT04IP3d+D8s89Da3sbbOHjSItMEL4++QIePwEeRsRGxYpIaF5eLsaHRxEZEyu1gvfefRdnn38+1m06C/vefwv2yEiRoGKVVtZN4c4D1VW12H7h+Th68JDC558fJs27PtmLCRCKDBNrs8ysJNz2eRYEx9DY3IA9r7yJjReejw1nbcOGjZvR1taMro527NrxPmZm2D4LIiwiHOkZmVi8dKkCGNKE3tQPEMZfUHHn+XcqOHMNV6lNaFDRIQf378X999+P8bEx/OrXdyMrJw8hHzslXBG5AyrCnWOHDiunwp2c4JtpB2648TM4erwCtfVNsNlt2LV7F3r7u+RejE9NC6vTY/MiXMfJaIVbtPRVrDoFNyPC0N8/gI7eHvHym3Y4ZY3zzLjQPzws4K3NZ29HTLQNw12TiIuLU3rnbJMRRETMgqpqS+lt6h9Yw62CEuzt65fBy3RhxumHPSYKM84pzLicyCopRFhUJCZampGanCxiICxChkUMCRsy3B4ubsXNrS3yPPbvfR+vvvaypAv0oSQHwDEzhZHRYcRExcIx7VTAPIQvjxGt6JHwn5vhjNOFwaFhuJ1R0gSMiQ4XFiaBZpqXn/Y8T9dll81atA2oGMUIgXbjRthCBol8+atMo2inzpI4QVEeXxBG5m8O17QCMgjpBBjDFohSNNDLm7C5IQUFLR/ljkhoptul6Lkb9CKKgDligx/7mMs3YbFEWichWXBmx7UaBZQtWzrLCOT37r77d7jjuz8UeG9Ha4cMjpyMdHzli1/BXT//GYaGR3DuhRdDb7FieHwEyYkpIiPu9XsRCNhgN5lFKbexuQVLy4pkV4hPSYUx5BdU4XU3fAbHD+0RgopVLaoZSYwS5qoR/R19MBkjsX7zdrzz8rOIjYxWuQRznszcvqp6PYEg9d2i8LeHn0RGRjYuvuRSPP7E4zhwcD9yioqQlklDSS9yF5fIlxwuHwIUA+F94b3mCuT3nNjt1Z3fYAxT/u1zSaSj6O2r5cg5z+WEq3Jojp+dAToiAtUTHRsexN59+/DmW29J/nj99deLxp/8ns85W4NRRp8JEdFRMim0zyKxTGkvK+0sRpA0sezs7pZiGtut7OS4XTOIz89HcWHGLPlMFgBhrAURMBkRl5wMP4Jwd3ejq7MdQ919mB4ZR1pBFjZdeYkC6mKlTBdAZAwXbObNBmlaMMkRRrfIOrAoy9MKYXxiCtFRUbJLU9STKSWFUSeYP7udCpWYpDKrCUGjHjNj04iLjUFkRARGR0eESpyamoGammPYetZGjI2Nobt7EpPTU7IwLC9bLvbxfF96CVBkRYBFZkXbYpRuz8LK1MHlsmBwYAzTVp90BfS5ekTGhCNoouqTMubF7UpavfNmv1ZgVBs4arVKUITEDJg8SsKnqHnS6SskKEOzIUitCbMMWobJPCk+VFI8ORFJG+W/2WKRm0c8sioRRpQWpav5O3wtFWFlpQkFJff7yEVgbgqgThQOEBYxWGSaX1c/QQZSGIGPPPIPZOfmITs/DzV19YiOjYXRahLkHJsrn7v5Ntz7lz8KCDI5PR0LFy1CWGYaRobdMNn08Fp88BuMsEWFI2omGseO12Pd2hIsWLgI5fv3it97VHgYzr/wYjz99JMwSWsooFSm1eIXCy31VRVYsWKVtJjcVL81El2lWolrRftZW/G5hwlh9mj84pf34Q+/+xm2bd2C997bgZdeeBa3fOUrglEIORX0Fvd1vTEEA6tBnBSy8xoFCDJ7I4n0CgFP/uPvyMsvwOoNG2E8SXKa/bQTJg2ivaeZvKiHz+lAb1+HUKeHRobR1NgsA/6bt38D+YULlEflp0fhCRrriQekCKrSsksBuyjec6bwSFx3xRXo6exDamqqCHjyVOLjEmGJihOHn8jwKGRnJktaRnafl07UAnsIwesNwOP3YHx0TEA1C0oWID09TQQ6czMykJmdALdX0cIngtBis8BEuXF2KhmcaHfoBNZdvk+KrcvtlF2dakD5+VmwmA1imMFCrN1mQ9GiUqVfTliyziB1BWIESNHh8x2fdIgfYaiG3hDhwuOoq68V0VYW9KYmJ7Bt63bZWJkaMdVITcmQlMBsNMExOSVKQ/HxsRgdGUNU5CRyMnJlbnGyLQjLkyIw9QrZ1VDNObQbfuIPFXRion6/4EaYICgwcbEAkzGrvlTV92N3wxg0MQIISh9W1GmNIYFbGo1haiFCERLUDkEpcfIHAavdAnu4BS6KOYgslcImJEtNa3l86HEaujAnNsFv8vtUNqVU1Zxxpu1gHKA7d+7HT372a5TX1UhRsL2zE5FhFmkTcRNmzkeKJrutlABjKsCVlBxy3sxJhnNxsVIMTEpJks5BZVUXFi9djjdeeBY5ySlo7+tH2aq12LN7J4bHJgSgorRYlHKaJ+RHc3Mtlq1YhpVrN+Kt115AmDlSzCsFyitgEO3cT75Wgf+S5OM34M4f/hL3//EeLFu6FEeqK/Hqqy/j6uuvR5A7waxFlNrwlsKkBruafTeFUkuhk8Fh/OLu3yIlMx0b16/D8iVlyMvNkdya4BWRvWLUN61IWzFKGhgcFNVidjuYEqanpmHT+o246qpPqSAkuvwoVFcRMzlp8odE3FNnNmJgaBh2e5iMNObfZrsdP77je3j5tVfR2tKJP/7+T3jhxRdQtrhMMPkdfcMwGG0oK1sMs8GAsVGXom/ArEOlubGISzKLzWIVJiDbfan6DBEEYSdz3KF4CzLKIUWYhC/JihgNzSWmzrn/PH3HpBNmixXesTEsWFAkitA8OMEZjm/fthXRSfGCX6nqPILE6HhxnBIZutEJFBbYMD45hsTkSOk+cNPMys7Fu2+8jOTkZIk06FE5NDAgxCsyTXt6uuBxeuDx+OCYcSEpMQGtLa1ob29DakqqnOiAWTG8IeQ5qPMht6hA0jpqFnIXV/4vZzpPbDMInaj1qlR8re40R7LxRE3Kj1BAkdQzEkZLVRnKTgntVlaPMxwMgVWciYL4CwmqSgRp1Q/7pNgB1hu4U9C91ON0ywAkjJeVdoMoryqqNXxTrfD3hz/+EVdedo14x3HV9TI8Qki8+WyWcCQkpEkPmhZgRyvKYYuKQMCgFzNJm9kKN/nVHreg+ii06PGEREOe7cTYiDBExcahe7APixcvwdEjh7Fx81Y88+zTKsVTo/KGRJ+uubkBdbVVWLVqDeprytHd3Y0oApT4uo/QRCAtmEAYDoaf/Pwe/PzHd2Da5URlSyNqy6tRunSRRAE00FB9l874XtKKDHhx+3e/h//52v8ItbatrVV2nt7OLlkgWGWneQq5EV63Bz6fW4pYC0tLRUeRct5CH5bDL+akQZ9HJpdizHmGQaEuRlNuF9ZuWi8Rgtkehif++Q/85Fe/xruvvo6EhFgcO3ZEdj5GnEkJiahp6sDSJUuQmBiNiUkXDDYzAiRjzsuieBCebLUoPWwRZvIH4VMp49rrFDvuk89KmTRz7xPgmaYselDak1T1tdnMcDj8sNsNyrjQ6bDtnPPhdPtkXHAhGp8Yhz0yArG0lB8nl9+AseEJmBYqbzo4OCp2c8pjZxvahcHhQUGnxsUlYPGiJYJLIbyYkcrAQCcmJgbQ1dOF0ZFRbP3W91FdUw27LUrmVbQ/iDffeAPnmy5AXmkxHG5qFphOqFLPPzScyFynnpNxaqdi+rnparn7JzlY9CUclqENW9NaeDunHPWRh9QUJLUlCozcJyBgZLXVhJCR+vGK+Kd2EUKbNRikQt/fM4Bzz9+KozWNiI2KxPiU4nnvdjoExJGVVSBecoQ8xsTFYmBsGNHxMbJDFeTmYXx4QhYWFkIouCgQykAIcfGxiI4ANm45Gy++8QrWbjkLucWFcE5PIS8nG13dPVIsUvJmRUOBYemxo4eQmZ2Dc867CA8/dL9C6piX95/24McGAopYxPgUHvnXC7jmyvMxNT2JHbveR25BLqxs/33cmgrPxzMDk8WI5SvXyNepd10bHfPfUxJNdadXbb9UYdaPOvRGA9yOSZkgeUUFsolUHjuK62+8GV+87TZsv+gC/ODb38WO3TuwYskqmUxkQ5aWlsoC4HKFYLXbMO6cgd5skfrK/NOWSX/iMk+rGKU54PDn2uSff/hcVJQisVaRMI9OiILLG4KFgqN24K133sZlF16CMFJ+I2Ml4k3PykQ4hUgJ99WbRYCUSszEKLicEPwDW3qZmYoG4NDgkHIeBh2cDgdGRkZFzoznbDI6JUVNSolDT083An4XBge60NbWgsqqchlPmWn5iIyIRMCvw9/+ej9+/ec/KmKxcs2GM28Ecwvr8mBOCRRPeez/NmufkYAGJvt36s0CVGVvnDfeYkZ4mAUxkTbYbRbZzbijUzVGY7OKDh6Afz32L6xatRYWqwKuoRAjARUsquXlFWB6xiU+aIlpSdL/TElMhtvpFhwAfeJYcOGNINWUWG+2WNjqE2W+QBAOJ3D2eRdiZnQKR/fsQ05mHlas24TsvGKJAOYa6HKis4jV3NIsZJqc3DxsOvtsIZPMTpyP0R3hIhAZHYP2vmHsOliFnIxsQYTt3b1T/PJEFehjHqIgzE6O1ymLgfbFf7NqH6Irjs8lE/3E910y+XlwZ/s4ApYnHSzaGg1YuLhUWkvDA/249IorkJaeit/ddy8qjx3H3X+4T+omFN7s7R+GJ2jARRddBLNFhynPFLqH+qVLYzodqlIdYF7u3hTDnOOjMQsy44Sf8/O5YGmNuCW4J6rsGkzQhfSIjI6QmhOZdCZ48OyT7yKoM+CSa66F1R4BQygIj4fIOgUIRJQh3y3AOggh3hHhmJkJIjExSaDFiUkZyukydTFZYTJwvOhknNHmvKmpARMTY6iprkJra6toCQ4PD8r5V1YchNWsx7597yMmJlJIb+kZ2Thy6CiaxL2J78Wr1BRgTjN1tbV6jr3YSS+TX9NYSaf++JMfZ9I3/oiDQQyHmwYwEqbXjB9TDg+mHWTsTQtQgjZfPAT8SsMMr09EPC64+BIMjboknGO3sr+vB16vXyS5zRY7RsgIC48QZWD2Xt0uh+SJcdxpJx2wmi0ik81JT/UjvpGid6CH1x1ETk488rPz0N/ZhamBCSSnpOHcSz+FuKR0KQTOF3hkKnD48H5UVZejbPVqpOTmSG3k404iFn2oQ+cOhHC0phnlNS3ISEhCQ309hnv7BMv9oSKSH6GmpEmra7u65sQ79/v/0UGCIqmtFouQgK699jqR0L7/wfths4fhK1/9qhTGuJC5XU54/QEhbKUkxaOiogrdA72whFmlrnTa69EcdU5kX7MKurNfapQwN8bR1LBmKVOMHAxUJ/KL0rBela6z6PUYGJkUANgtt94KW3gk7KzY+7yYnpienagGsusClH8PSPGQfAC2A5nfs9MRGRajCIuyeOnzYmJsTOpjZUuXwzE9he6uLkw7pmSC039Q0XRghysoERF5KowGKF3X3NwonIT0tEy88vzzCLebEBAOhsY4Ofn+n959Z56eJ/X9pR3CAYWPAAJ93GOOZuYnPWajZNYWzHopskVTCjw2Ggkx4Qi3K+5ozCk5SA8cPAKjMQIF+WnoHxjF0PCwfLbb48X09ITk0suWroTX5cHY0DCm/D5YoyNl9SadmG2dvv4+REZFCLCJxUau3OyshfSKLJNUiEMhXHDRpdh/4CBsUXY0tHWgdGkJrr/pVvhJ35BuxAk4Lltc45NTOHpoHzra27FtyzlC5TxN6f+UQ8Q94YdzZhz9nY0Y6GrBiy+9ipbmbiRERGLHzh0ySCiC8UmxFv9vHaIry9qP0YRbvnArduzahVtvvAEXX3AJfnnXz7Dv4AFJ7bj7dw4NIjMnV4pue49VwhBhR14eXYhipLg5fy3ixKcNlrZh8NAML2cJZ1pEPL/QOufbXDw437nYmu0WmAivCwJWoSuz9GERRiSRe4wMpqYmERURITUpq9UEl8sjAB2K4Zh1ij8D++l0AuZCIJGs1YbIiOgTluesi0yOo7LiKDziy6jDyOiggHFYmObzJBuSz5ebHjsnNLSZnp4U1uPk5ARKSxdh3+498HkUNemTeCHaIZP/QwaHZA4q9Fv0ITjhVLCfylr8z9aBucK5H/FS3Rn+znyfaCtWO+cPAq39t2fXHixbsgKTk+TuzwgYiAVD5lfMyew0AAR77BHCIyfkISYmFnFRMRgYGBCaqCgLEfpJ5LSOevy03z5xNtwdZ2aAstWL4fYGsXf/bpQUpaKioQ1rN6/HjZ//EpweuvOccD7mzsaqdGd7F6qOHMLYyChWr9uIGerkEch0phujVWh5zdyMPB5MjA3C7XHimRdfRVtLJ6oOH8a+HTuFp67VDP6/drCQazTb8O1v3Y7H/vk4MjLTRbnp2IH9+NFdP5HUicVmdgiuuuY6qbJHR4Vh0+Z1yC/KlwEk+fu8By/imKqIhkS/c++hOp5nvVu0Y25rWX1LLujMLBglctLRB4O/bxIPDB2mJqalONzT04fomFgFoUcNCpdbJp3NalOdmwOiN8iOBDUELTajTFIRJ52ZltdQ0ViBqissRi4GhPiyXkSKMKPVqqpyDA0NY3x8BP29vTJGiIpkpMGDXSoWItkuLCkuxWD/IA7t3Y/wcLMiGHu6DGAWWXeGQzAbcyR/VNyI3kkSBZVINfDFv3mIOo5asP+wRSB0grR64nVqRDLrcD0XHDRn2xsZGcaWc7ehtbNXAD1sXRn0ZkRHxmFwYBR2S7gguKLiEuEJBQVlZQ9TcApkZjlnHNKL7u3tkcKby0FeegAzRJbRoIYPTSCXPMkQrrz2Wjz0p/thkLQgFS29g7jkqitx2dU3YWp6ZrYQpSz4BC+Z0d3ajuPHD8FsD0d+UbHAhEXy6TRtQI2wd2Kw6iQ35TfJD393x17UVjfhxz/6Me791W8UD0BLmKor/x88rP/iIVqGZjvuu/ce3PPb++R7v7/3tzLZr7/xRtnVuFsSaPP3fzyGRYuWYMXylUhPjxftO497bp/q5EPY0GRCz1Vg1qrH86vac0yb+BrWC9gVI/6eC+wY4bejo8K4mxXT1ANTLq/8mwrNZotRIhUSychQZA2JaEI+Z7b0fF5CkXWwWm2iABxmjxBAE9vjHq8bOgOZfh5JCzRhV238Dg4NwO12SQTCXZ6flxCXIL9P/MvoyAhCOitiExJFmlxZNL1ClCJd+Z233hCKsHQ/DIBXq4/P4mlEM1zNk+YVCbV7NoviU/ImDiG9MK1U1V+R9foPxhWL9uaPsQictFapD0/m3GkqEtKrVKvCxaWlyMrJgdtL7L0BKclJMqlpb+31UBDEjb7+ftjC7aKmQ84/FyZ6v7P11TfUj0hCOycmBDdNoBPbUiPDQ/IQBdwxPikyVG63Dpu3bUKkNQwPP/APxMdbkZCYgBGHCzd+8VasO+tczMwoHIAT10obLqM8zMqaciQkpSEuPkEQeSc9DymaKTj6WbMR6bGqLxAnbSLzTJiYdEhR6Xd3/xab1p+FXR98ILh8nYnedqpc+v+fDn4+bbbKjxzEXx+6X7533aevxuVXXI1bb7lJEJZcYDdv2oRdtPu+8DJsOescXHbZlXC4CcnWzFVP//6aCNHsz+eY0px6Mid+Jng0hETth3z83t4BybGTkxPZ6BeMgmhn6EMYHRsXCjmVphPiEzA+MSY7Nz0QNbo56znETlBCntgN7sKOqRlpGxIAJ48x6BfCDdOAufZgJ+ouOsE4SN5PaXPHNOrr6pGQlIj0zGykpWdiy+atiIpMUI1FFRoxHbJoWb5n9y5MTit+GKIgrLb7VZb6iUM2BlZFTuPnd1LIrciK6T2BoOz+DLeEqfefjAg10hB/wI/MfdXFWi1gWLSdd94xMjKCjrZOebAlxYWwW23SGuMEZ9hNWGZKSjKKi0sECtzT3SNgizWr18lqzTqiy+2WfixXT95Qas739Q8IqMTnYb87gOkJh6KyYrWio6NbiCeWMB2+ePv/4vknH0flgSZERxkQZjbLAPvyd29HXHqK7G6aqKN2n3mug30s5DTAFhmnqPzMGbQcQGabFQnJKeLKRDhokJxt3jw1NCLrTarrBgtcnhC2XXA1nL4wXH7tzbj9u99C/8CAkHPEQeg/Cd3+zYP5PBeiivIjYk55zZVXITcnE48+/CgeeeghPPrE0xIR/fyXd+Od9z5Abk4xptwhJGVkIjYlCQ6vovBzpkPb1IW3NldqXiabEjWc1Amc5V1w0njQ2dIBsyygk6IenJRIsVc6GKtrrR6YmXAIOYsA9+joaEyMT4gTFa3EwsMNUkD2ig6AovJDpp3GmRka7gflGsk1YIpiJgBNxzY2QXQKl8ZqJ9nIipi4OEXJ2O0WrMXipSuwceNWlJUtR2FBCZaWLRcmakjvRSDEDoPihSEcCY8HBYULMDI4jMOHjiAsnFGiQuOeFdvi3KHIj+jCcIHhInTSqnBCLIpYMhKq9H5EGENcAHzwBgIinDHrRHKy+e0nPkSxVBXMnNuWPHE6iuiB1qKZO/mpIqM9YNoqvfbq64Ld5xtlZ2YLMM1ioXW2X0BA/CouKsDw0IBIVCcmJmJ4YAjxsXZYbWEC2oiLofccrZ9tGB4dRkJctHDxp+gjb7EgNzsL3R2dSrvIbkFKRhoayM4adWPTOcux9byL8Is77oR72gOrVQ+vx4+klEjc8pWvw+M7tdqv4epHx0bQ398tMlFzJbJZNJym6KTXjczsbKRm50i13EmDDaF+nRDg4QJJdBrxBzfe9Dlc85kvYseeevzPN+/E8y89L8ASTkRlUv4XDBk+bthvCUdnWwvu+fUvBfxUUFCIl55/Do31DfjCl78s9/6pp5/Dnd/7NhXH4JhWTU8UkbJZgssJ4bKTvzTSskRDc9Y3YciZlToRd9/ZxU+FHxO2PtQ7gLS0ZCn2JiYnSwRIqLCKjlU2Jw5ynQERUZFS+CM6j4xFovPIGKVKGTcLakaILLhOJ7UARvbchEQynNJgYXZlAbDYhFFIpaOsrGxJ/5aUlWHd+vVYvWYtLrzkctENWLViI5ITswWxGBsXj4jwCFRWHENjQw3aWlqEzu6lhiUXAVVj0GYNR0ZaFg4f2AO7YIGUaxaeQygIl9cJl8clkQq1AGacM5iecWDG4xTPQna6JFKc0wFk+ixOUETFuaQGEJJagNuvfHnUheA/OWRH/ZCUQGvVUAKbR3d3L/bu2a/0/WliaTCioKAA+bn5ClORF+z1KpBWVbaaD4lFm472DokUkhMTMNDXIztoTGwSZqZdiIiIQGJ8GibHxwWySW46zSao/BMeQS8AvVR821u7JHphCpFXkIOG+iYMj8zgez/5DsLDzPjpHd8V/AGlrKam/Thr20ZsOvd8GUCiNaBdl3A3ldFKVRoXF1hVhFPGKtlnehNGh4YkRI1Pz8O2iy5HXlEJxoaJVSAcW3xdFYnoQACOiVFh4l1w3sX4wQ9+iiXLzsXOfU34w4OPY8+e3TKhWB/gB3N3/n9iIZBqP5mY1nC89fpLeOD+P2Hzpo3SEiMTMD0tA7fedhviE5Pw9NPP4MrLL8bElA9On06EPBTEp5JuagOY4fjpvsQ7ZU5KqqU6bA13d3ehvPwIJsaGRXORLlXc4fhsxofHFUl1Fv50IQFF0UREpLPJqRdePXNpnzhMjY9Owic095BwDNgtam/vltoDEXszMw5JFRmWW63s7XOjDCiS5T7IhOdBdV+vLyDpAyXpMtLTBZlICvjE+Di87hCiY2hxrsfY+CiGBoeFVrzjg/fQ1d0pxWh+VnhEuOgaajOV70eFo4Wly7B/1y7hPbB4LVNGjQLEok88qYiK5YZOAZwQdAFGCzoh7VFQFarCnMxJrmQMJAI6/mIIroBOWQACgMsPOEmuUBeC/zgl4NeZYAOarC6osxaBs7dvm+1j799/AKNjY4ofvU6Hru5uYXCFRUTJSkwBSbrqcjXOz8+XkI0rMf3Ux8Y9SIiNQkJcEjy+AFJTE5CSlCo3eto5IwseCRz2MAPaO3oQExMlqrGDfSOyy1jDw1BUUoT6hk5MTk3i9/f/AR1Nzfj+128XO3EWiDwuP278/BcRk5goBRsBMJFb4XTB4Ockp4urEqfO92HnA2Khp6+nF/29Xejs6sFlV9+AK6/9rPgaMAykEYukSjSsHOzDxGA3ampqRVBy61mbsH79FqRmLcX7eyrw5f+5Hc8884wy8OkgKyHrf69GQPwDByMn/0MP3I877rhTqt0rlq9GSnIqVqxcix/95KfIzCvC8y+/josvvgCjk354guxfK4sZNy7NDl3mNzdhLa+f/6VNfnXAmM2kpENcnIisy8jMRGZ2pgirWMwUvAyiq6MbfZQ1p72cyl2RdpwYe5wonjNoYBRpJGhpeFBwHJxR3OHZCVi6dKHiz+d1i54EjXC4G5PbwPTPzeerZySoyHcr5CiDIkMWCqChoUGYgeSsNDY1oa6uVr7GR8cl5ZicGpVa09Ejh0QARMh2MigU7UayI8nPkUhfp5dUoLCoVCDdXZ0DAhjiCskxRaau1RQmXya9GXq/DmEmKyLNdkQazQi3WKVzwU9gQdo14xISn6ZQrPfArN5vv1gRaZ5k/JNhExcDrtr/ySGprRoNUChT09CfT/ljgY55OQfu6OgoNm/ZgvPPP0/alJ3t3fDMuIXEIq5EfPAWuwB3eBuIytL7ZkRnLyknFyOTI+JeQ/4zudecScS7m/RWsflmPjY2QqIGBH/PPjBbij1dPTDSdsrlg9FuQWFxIRpa+zDhC+GP//gnqmsa8K1bboXbMQWjxYj0jCRcf9PnpPLLym5Rfh5WL18Gs16HmdFxGP0B2I0GWDUTkDkHHwIJLL0tdXBPDMhOvnjdJtzxq7vhZ9ow7YJFrKB1cHt8aKyuwGhnIxwzExidGEV+URYK8rNx2ZU3YM2Wq/HevgZ85ub/xcN//7u0nhgR0EdA/Bz/zTqBsuv7oOd76Y341u3fxOe/9GXERCWItPYrr7wq8ltHqxqRW7IMv7zvQSxYvgR9UwF4Q0YFwCOuNicX6pU3P8Of6t8VYA/9AIPo6uoSlR1O6pLiIiQlJ8HjoXZFEFNTDlRW1qCxsVVcoJKSU0RQlIxBpRs/75pE1UmHvr5hCcGlHUzBGLcHRUXFsmsznFcs6fTSPuZGEzQbxIhEwD/hkTJHmJoJTJcW72qNaWJyAp1dnVIU9HsppOODyzOBlrZKHD9+AN2dLejuapgtbGhrNPkDCiFLYfOYDEqq4/e5ERsTC7PZhrbWBnC9MgVCMDP1YCvTr4PRz7llQmRYOCxGi9AfPcGARC5BTmSfHxY9CX+kaZ8g2en54ZRe8nHnCnngCQUkJdDagx717//pIjC7/KpOVCJiO+/QbJMPHTqC2tp60cinGhElmH5810/w6GOPoLenCzZ6dgaDmJx2wOMNwmohHoAyWTp0d7cj3GrFxPAIXG4XQqr+mcPhgtloQG5WLiLDWaghCYjCkQpBhIOcVeu09DQ51/7uHqn0sjVUsmgBegfGMDg2gX88/TT01nB87lNXY/fb70oods1Nl2PTedul7z8yPIyiVcvx5Z/+GJfedD2MZgsmJyZUHLlifDq/zcmcsqa6Whawt19/DfaoWPzp748gLCERExQJJYSWC4rXh8qjBzDc2YbhkWGpYLtcU+js7BBG45e/+nVcc9OXUN86gC99/bt49B//xPjIiCwErBNoElofFRUoxqwBxYWYrTBrBGprq3HuOdvx23t/Jzsq5dYqquvwy9/cg0cfe1yKXNffcKNoKkxO0DvScMLhVnWQ0uoaGhJ09jTmtvO0zxcjUEX6enxiEgadAQV5+cjMyoLJbAfhGxIhIYCR4VHZGPLzs5GblyNmK5IFiSqz8nXi4hQFYboPeTxuZGSlYHBwSBaFyOgwES/t6x8U6TgSkFiboaGI0+2UMcLNaGJ8DBZbmGL1RTp8UPFxpPnGxOiIqkJkxMToOEaHR6S/T8IdJ3h7a6tAg0+HvqTXoWDzJOKdY0vPBUuvE02BmqpK0QqUNvA8aofU+KipoXY5qLBksdoRHR2J+JhIRIaHwy5pjFIgFD8Vnd8DXdAjkssCugjRitAvKqSc+EwFuIDMMCKQn3/SmX/ioc4esqpR4ODkVUVrl4i0d2ERZqYdYgRZV1uL+sYGQeZRX54RhdtN1VUdxlUABfMve6QV08O9sIkAJW8ai20OgWoy71JUYpQ+MElQ7PUKPZqFo+FBUaMdGx2Vu5eZmYbenh5MjE1IlFBIyqjeiMbmNtzz59/js7fdhvt+fTe+etOteO2lt3DhFVchPiEOg8MjePudDzA64cD1n/8q/vCvf+Gaz30JCSnpUrDj7sHBxZrBbLtIPOOs2LVvL6xBL95++TW09PXjj488jIT0NDhIkjEY4Ncp5hh73n8HVp1e4KS0qo6Ji4bJYsLA0BBs4WHYeuGnsPm861HZOo6bv3QH7vnNPaiqOC6LgBIVWCSkZz7Pr8C8v3PSs8NgsIZLVfynP7sLq1evwbvv75L2JcNdMt1eeuUllC5agmtv+gIKFi/HtIdai6yA6wWhx41M6jbiPDQHuaeCe+bv+Nquz13KylARPhw9dlQW1fj4RIR0hINzcVLaJETPdfd0iWT3gpISZOdmwyvFPRX3oiqpnzzXlMna2d2D1LQE6RC5fS7ExMXLBtU/NCgKQ1PU8aPuIandMs6mpOLPsJvRAck6/Jni20cHKh3cDgcmxoakzz86PIwZx/TsRO9oaxeDU+XfisDN/INqwSICK3kSLcVOiGYwysjPK0J1+VE1Yp+n2Ku+VHea7wsniGOOEQu7AF7l2fC5GAWtpIFZ5IPIANOagQa4KChA0IC0X9Tqvgr6+SQK3/Px87zx3DW0UERTpSFwgz1SoqumZ5x44YXn8OJLL0kBbGSoHx0dbYqpgt8vFVjquI+MjEm7JC4+CVVHyxVjB71edNnCwyMwMz0pn9U7MIzszAQ4J12q7XJAJJ7jYmNFry4hYVxaiVE9MUhNT0JRYQEaGpsFv56aloqcnCyMDYdj157D2Hbhhdh2zjn4598ewcO/f0i0DOKi4zEzOYPu9g688/xz1OjAqg0bcMs3b8al116N8r17sPPdt1DfWIfRiUlE2cJlF2LqIQYaNhve/eB9bN28BQfe+0AUjX//wAP4+he+iP6+XgHXEIo6ONiLg3t2YfP55+Po0WosLisTX5AZ57SIPpLpmJ2bLpLnbKM+9cTj+P2DT8Ck+zs2r1+DzZs2IDUj54xEkMmxYbS3d+DlV17B3x/5B7q7e+T7BMlwkeCzCo+Oxle/9h18+Wvfgt5iwsQ0B6wS8ivjTjEFoVY/Q3HF2uxDsGqyECtfzPHZtg0PC0NxYQnCwsNmUwkOVJsV6Osblfw6Iy0Dy5YuE/ksakDQpXquj8ncg90lm12H2vpO+XdCnA0HDtZLRd5q12NsdFrqSLExVrS1jcu5cNdnzYnQYNqLiWqvieAyavIHlTw9GILJaENHe728jlHeie7EHJTIvEmvsS211zL1YIdDNKxZ2NQrmAO+jlyKzIwsfLC3GdMObm5qS0Nr752SX564r+qHzSIDWI8Tl+E50JPZ18oPgrQj4mrFTNQouRyr6gx/TBqEUiyGlH7qx2EUsYCXlpoqF8wHHEsFH1Zh5kx+kn0eeOAvSE/PwGWXXyF1gJ7+ATQ1NcpuTgWXjtY2iSGZX5HSGR8fI1qAYQyR4pKkPzs0NCBST1wAsnPSUFXVj5zsXHR3dWNiMlyqT6y08jWEh3KxcRGeGhkpNzo83IrW1i7k52diAQuBjc3wtHdIyy42Pg6REWGoqGxAZnoqvv2jr2Hsy1/GoX0HxLCysb4eT/7jb+ior8X+2EhYoyIwOZOP/MxkbLr0PKzcthXN7Y04+MFuvPf8S1i6dAWOlh+FyaqYX1JR5/0P3sfmdRtRvucgRoZG8ONf/wbf+8bXMDk+JmIRZpsN+/bsxPI1azEyOA6TwYKIKBssNrP4Gjock5KHcjEjejIjOxvnn/8VHDm8F2/teAe/vvcBJMWHY9uWswQcY7dY1ep6r5hVHjxwQLTytDGkAbGYT/K49Iqr8Y3v/xz5BfmYchDCrFBVVf9PdSLT9SaAyTFi5aPVePMM9DN1MLIF7HY6xfuBuTl3WSGB0aaNk0UlzvT1j4qybmFeIZJTkhQkHoti8vPTO1wxarBZdWhq7pXdsGxRNoaGvbLJ5OfnyeLCyCM6Jgp9fRNyrUT6udwOCam5qCbFxWDG4RWHYjos82KYZrIlbTBYxORULN9FROX0OTPfh8VfUtG1roq2OXKx0enCFE1O1RJPmZdUBfIhPjEOrmknxobHEJcSB59PkeQ77Q1lsSJI9SKlxsDFWEoLfDZ8Rt4QAnolzZp9BrOLgORuNMDg3xUHEuKppwOAQ+0SuOd0CsSa6TSnoa1sNbW12Lt//6y2H1tGnPwaYEKsv3buwk9++lO8+vprIsTR0dYhYhw1lRUIDwtHV3eXrOD9g4OYnA7CHTRgaHwaUfFJcDMMMxiQlJguTi7HjhwUD3aTyYzhEQdSUpLQ2taC7Owc9PYNyrOZZjch3I7wcJukD1PT02JF1UYRUasFdrMRjQ1tEkqWLiDH3YSa6jrZIUxWK5YtK8PE5DT2HWgWm+6zztkIU2QkipevwNI1a2WgDvb2Yai7EwO9/aioqhMQij8URNBkwZU334yrPvc5ZC8oxedv/yYcHjoYKRp+bFvuObAHg52taDpWjjfeegs3f/7zyg5MAI5AhkN4/aUXxbb9sUf+jrHREfQPDmNy0gGj0Ya4uCQsWURw1AQSE0iJdiEvbxm+fee9eOixN5CQsRzf/d6duPGmW/Gp6z6Dz9z4Odzxgx/iuedfQI86+Tn5OPm19GDhsjV49Jm38ddHn0ZGWj7GRxWHG0FDBlXYayCI6ckZ9PUMSiU8IiJS8mSNsaYx9rSxxvadgFR0IcFscIRm52QiNi5GaaNKB0CRpaOAycjoqBBwcnKzpe4x67unaLVJj990mslvt+nQ3t6P8bFJlJZkY2DEKYKwbo9L7iuHakRUuLTsxLWIgjS0B3N5pHPE6Cwqhg5BgwJEs5itYrCpXIgBXtckDux+V93Qzlww45xgKsCjqKgId95xpyKhp4qViKyeTHlGMoo4D9vKfE/iWFhsHBsdEm7DCV/603ypUHOLSSeUa0UNmJ0UxR2IXTAX4ewfRdLxBnWir64QZpVcjovAtFoTYCXUqX7NBxBpKxvZUp+6/EqFeqrGgdquz0E2MTmFjIwsJCcm4dxzzpPef01tFQ4fOYLj5cfQ2dEug543r6mxQfzfYbTDw4duMUJvNWPC5YIvGMLWbRfgnTdegd1qgcVilLQhISlaVvSpqXHk5GUL65D5HLXW2JoifJOFmcLCNKxZvRYHDx1FTCwFLq2oqW6Sm5SdTeOLRFQeP47J0VHYzEBxUZ4UWCoq6jA+4cGyZcVISkzEuu3nIT49G86RUfS1N2NyuEdw5IeOlCMs0ors9CzU1tVhy2UXI2gzYWBiHL+657diTsFesCgtRUShqq4WbY31GG6jvHQLlq3bqBboCA4xo72tGZHUOejvR01NPdq7yUM4jEOH9+PY8eN4+dW3ceTwQURFRmFwaBwzjim0tNSLDt0Xvvw/eOjRl1C6eKk8Iz4Hs9kiE34ulJUTn/fna9/7OZ56ZTe2nnsOhkZJX2VFWcQUhGLOYh3NJ4nCo+Q1owFiLYjW1NB3HBsCOOOmodrGsQUvfX/oxPMvIjYWHo8yfk0crCGlyMd6THdXj6R+xcX5MBot8Em4egJNKiIt88avGLTadejo7MfA4BBWrVyAwaFJ6Rz4/QGJSsMjwiSF6ujokTCfxT8uPkIXlrkUlGfDLGZocEDRhVAvStSBw204uG8HWppqTg7/T3dIKqP8nB2L1WtWyz3iwY6CwUSpb0bDHnh9PsElkEMgz8FikcVnuL9PUvDQR+EoQ36MjY6hr29QTEu6uSENjGB4gG3Isdn6gHZe889z9q1oxcQVIxQi+omFCiIHA5jyhWQh0B4qIwJGB+pzmRXyFGcYIivUN9QmP/P21pYuTI67YLNG4PIrPo3rrv20aMa3trUKkk4q6G635EfMiVhkaWlpQlg4Q7AAJicmxfiTikAdHV1YXLYC46M9qK6sVvXe/RgZmsbiJaVobm2WIpqF4pp6tn58AkYRDTYWQT30ro8UBmF7J3NuKyLDbTh88LiEUCkp8ViyZBGamltQ39gqoWFaahIyM1JQXVuL7p5RpGcmYOOmDfj057+EKbcPrRVV6GluwtTosPSdX37lTRE/Wbm4DFUVlbjkU5+SVuV7H+zEz391H4oXLJEKmdflkhV/aGwUNdWVqDhwALXlFeKxp5ib6OGbdqClugJr1q4R7YCzz96MspWrULpwMTITErDjnfdx4UUXiRQ3B+r0pAJ17esdQFVlDfSWaPzs7ofxpa/dIZVzDjotJNWis3Vbt+HFdw7iu3feKRLhE2MBKaSK4YYaW8543Gjr7EZNfQu6enpkQpBsxZqADIXTJP68d8I/4WLghYK884Skq8P3nXHMoKq6TmzTaL9GzXyCtqJjYuDxqmHt3Ore6T6Dk9+mR2dnP7p6urF27RIMj4xhaGREtCJGR0eRlJois8DtUtrFhUVZotQj1N8A00OyQ2ckAqBLT19vD4wWm3ALmEqE/B7o/W489/SjZz6R+XNLrQUcP34cl156qfgNnJh3OhmzdCMm/oSCN7JxMhozWGUH7+vtVhYA4ZCcaRlgN8AnjEaPT+n7s34koqDGEEJGHdxMM7STYUipTfxZb8E5lEo+MOZZhA57Al74Al64A17M+PyY8obgUCMCLgAObSFQ3T/aOzpkFdMeCo+uzi6cteUsPPyPx2C3R0FvsEqv//DhQzh+7Jg8HFZ3T5iRMF9R1qvWlibpzbI3zp1L6JME5vg8cDi9OPe8y/HcM4/KLirw38EBUXwlWIhFMWEfhkLobO+QGgalz1llZUEwOSlWQtHE+Dh0dvUiLNyGjIxk7Nt3SHQGuFusWrVSuhgUs5hxeREXFyPyVt29/ejqGoQ9zIzt52zBj+7+LYbGJlFz5Ai6mprgcUxhQX4unnn8aWn1bNmwDlWVVbj2+s+IFPiDjz6Kr37jW7j06hvEs87MCUSxCK8XQzSjmJyUGgV1/4lxiLCH49DOD5BTlIeetlaEvHqctaYAWclJeOPF13HBuefh8isuQElRCdasXo6Vq1dh1Zq1WLlyDS666GxERMbCGzTgF/f8Ai+8eRCbt10i90tETix2/OD/foMnnn4NBQsXY2gsoOSlbEmqyrp07JWFnLuVV6H75mbnIDEhXuoh0u87zWZ46majfIc7Plt7LOQxveIuz+4Hw/yMzFQkxCfBbLaeXsDk5CaTGvbr0dbRjbaOdqxft0I49vWNTVi4IA8tTa2Ij4uTxYrnSJk5tvvoDUBtyNSUJGGJEuHncbkQFxMracLo2BCiIiJFrVinU/r/9/zyZ2iqK5e/s336UYeWWmmiLdr1EIjEFIAkM6HsqzgAApXoz4GgARaTHt0UE+EvBMxAUNM9nnv9tDuzwONmV4cVc53UVYh4nKJs3syMGJ44nNOUODOpq4JByBvEqbMCqRVTTn1g3B108POLdM6AD66ABw4SahgFqAIOziAww2IdDChdWAY3YZJz7L5379mHgwcOYePGTULbdTtn5M/REYomBsRWaXqKZhhzn6zWUmkUiCP1/zjophxOJMTFy45D2a/zzr8SDdVH0dJSh7DwSFkcJiZciI+PE512nxtITE4SFBafF9MuSkPz88PDjALjPHT4sLD/BgfH4HL7sGBBHo4cKcfkGAtxepSWFiE9PVU05RlecldftmShQHsbGtvhcQexbvMK/P6RRzHtcKHiwD601tXJYrRly2a88PxrGBwYwpbN61BVXYuLr7gMl3zqYvzynl8iOycPN3/p61izfhMy0zJEI4/Rkuz7cwaMTu0t73z7DVBV8ZUnn8Uf7nkYd/3fz7B++1ZcetUVGBvxiSEqvyiiyhZwXIJJQuBd77+PrJx0dPR6sGTpErz4xsu44NJrsHjxCrz8+n585RvfgnPGBI8jIBbaHqMOLn0IXiNrQD5MTjvR2zcAx5RT6jRhYeEyUEO+kMLfn7tBqZsKK9CKtZUwruXPcJNOvjc5NoHRoRGp9xAByjHImk5ebi5iIqKktSskqTOo34j9digIb9APm02HlvZO9A30YfOmteIXePQYJdwXixNUdU0tsrIzZfd30rJrxgGLzQ6Xk3wPGyIizCKdTqYg26IpNPmQjZrakXFSF4hPsOLVV5/BA3/+paK2rMrWfdjBSa+1XTVchnaYrWb5OenIjJhjY+Nl4tK+jtFIEB4YDDb0qV0ZkoBO7XRQ5TsIXyAAi9WE2PhYETiJiYuRGldiUgLSM9Ol+JuemQo9T4QVcRa3RNOftkomi9AeZwfanAnIHZlfJ9iFDFk46b2CeWdxkMAPHyuzFFvwh5CQmgyXL4gZfwA+HQ2RgfikJCxcvBS1NTXyoInxn5yaEF04qyVMVqv+gf6Tlh9eHI8D+3YKwIfa62zv0cQhISECcXHxMnAMFjNWrTgLzz35d4GrUnOQtGEFg64Tg43oSAtcXg/GRyagM4SQlJQsiw3zz5ycDKSlZWB8dBR6XUgUdHv7hlC6sAit7V1obGqT9k9aajyKC/MwMNiPtrYuKcxkZ6XDZrOioqoaQ4MOZBWm4w//fEzkr4/s3oma4xVoaWzHFZeej0MHy1FZ1YAtm1ajprYRi5YswY9/dRdeevMV7N+7F8tWrcN5l3wKl195LVatXCnoNiL8uOMyJOOObLBYcPSDXfC7XXjnvbeQkpaJv/3jPlx6zRZhiMVEmhAdTacZI+wWPZyT09izcz++8T9fRWFeHhJSODAs6O7pwf98/dvILVqIl9/ej8KFSzA8zMFJEJIeXonAiFzUwTM9hc62dvQP9MFDarYBiI6OEO8/pcaj1HlUY9+T2lSMNDV5LobURE9Sn6G2vgYjwwMIBtwI+l2Ijg5DSVEe4qKjESKLz8/lT1NgOTN60B/0iDUWo06q7a5bsxozM24cPHwUS8tKERFmwwe79mHBgmLYImzwe4Py+U7XDLIz0zEwOCC1Hjn3IOHYRpH3io+nJdi09P5ZAyB0/Hh5LZ5/4m8SCXycya+lxBs3bsQXvvAF4RZo3+dhs4bJ/ZGFmtBiYvrVDZMy4bxws9kufgOKkAyt3xXEk9zmYEhYrEyfCGNmm5KO1xa7RTa4mOgo8Q0M+fxitzY6yLRUrIONoozCfqbb65bVTfIfk+JVP9spCAalfcGVyqD6wqnrgpyEj8OEYYmq8KLXU21Vh47BMbz8/Av4xpdukdWJ4dcDDz4osN+nnnkCy5evlMoo7Z+YV80QeqrXS2V+bptD+zzKcFPMg5NTwD16A9xexT+QFmEunwvbL7oUd37ni7j4suPIzilGVHSkvDfhlWMT07Da4pCSlIThwUEkp0YjnHbgoZD0ks0mndg6sa/O6IGpCAuZ5JUnJcVietqJqqpaZGSmiaddSfECDA8PoKKqGYsXFiArIwXRkeFoaevA5FQscvNSce/DD+Peu+7Cwfffw9jwiKgGffr6i/DOe0fx9tt7sPWs9WhuboU1LBx/fui3ePyRp/Hcy08jMy0TiYkpWHHWNuSXlaGnvQ09He2yUwb8QQiXg92RlmZExkSj6ugBDA72yc7FNEkKSVMOQaX1D3Sgu78T41NObNt+Aa667mJJ1Z5/9l94+aWncO2Nt+GSiy+FY5wmLUEELQaJMHUWHazExju9YqQ6OTEuOTGLWLEx0coCTrktDe7MlFEkEEJiZqmBXySsNeoErONxe4X1xgWCasILk1Ol2s2IIi42FdFRERJyE9km9tmMftS1RfveSbuTqAaT32FDY10TPE4nVq9ahpkZD8rLK7FqRRni46OwZ+8RhAI+pKSmSBpDsN3w8AgWLy5FT1+fVORZDCVHgUxRFlCPHj+G1atW4tj+oyJAw+Kmd6oPX//Wl+F1TYkJZ0ZmBlqbW8848bXi4Le/823c/eu75fssTD/88MOSBjAS4AbG6IemNDw5RnfsRLi9HpiJ86cwuMmA0QlGxkrn5MTaR9wAEKa2TolGZPSgd3rEWdhr0gvJzCxakGYYaWhqj4AxjDBH2hM7AwjIisLVmaEmOQLK8q3hqXkhFjvzFE3zTPu5hkYkiIGRABVMFVttkSC02LFg4UL4JHf14Y47vov339+BhPh46DCJhsYmee3g4ADCw6ME5trb0y3uLFrvWVsAeCN5vtPjw4iMS4XHR0UXvYCVGJnQ8HFi2oHEtDQsXboajz7yEH7y89/B7/XCSAK32m5xugOiCX/owFGMj8wgMo4QVje6u3pl54lPiIa3zystS9JK62vrERlJtp0BCQkJkgN29QygoCBXjCJSU1KEo3/oyHGsWLZEfmdhaQkam1tx+HAtFi0qwZ2//hEef/BfeOIff8c7b7yGvu5OfOfOr+BoeRveeW8XNq5fjoHBMRw5Uo8bPncNtp19EV558TlU0QjU7YY+qIfD5ZL7aYuNRk97O8IsdtmRSFhhxPL6C8/J5GWqQJYj5bZj45OQmJoq4W18XDpuvu1KnHfhJuzfV45HHvkb9HoLfv3rh5Cal4KRkQBsLPKZ9YqVutePqfEJOMenYNKTmOJDYlIy4uOixfG2v39IHHPscdHCR6f7sQi86HSwc+HQszOgFJRmXB4pxLKYx0HDiZacGCdqO2TWMQJjvUUmuRrqs96hYfeJ2RgZm0BCQoySapy0+SutPiooEdOxYVUZHA4PjpaXYwG5AwlROHK8Xnr2iUlJiImPlyiExcvsbFqxUSw0IHk4o09mnikpKYI9IdksNkaHY0f2IzMvAwGvEz/83tfR0dwkKxKL4osXlaKtlZHhyQmzNrnFxVivR3JSMnbv2o1NmzchNzf3pI2NEvbs9kxN0Ah1WiKPAA1hPV4kmhMVNS1yO0JszSv3houAoqVoEF4Ed/2AYABIhlLMXkhYon6GdBi4mPK+EokqZjwGoxAbFHcVdTeXdm5IbgT/k+yDHgAUPDArVWhZFhh+EPggS76a59HtBl54gmbQVVBQY+FW5JeUoLGnEwc/2IGG+gYpmvX19iI5OU3aGyMjQwK/pGUTVz7mZOzbtzY3n7QICNU2GET58eM45/IiKQByIeHnxMUkwmCwYmysHxNTU9h69vn4vx9+C4cP7sPGTVuV8F9NA2TR0gELF+bjqaefwcVXXIq09Fj09QTQ0dmLrKw0ZKQmYnLKSegX1m9YiYqKWnR292FBSQFS4+OQkZ4oFenEhBjU1jWhsDBXTCb2HziCpWWLBDC0cEEhurr7cOjgUQHOfPYrn0HZspX425/uw8N/+R3amxrw01//EukpSaiqrIUt3IxJpw9vvn0YKUnJWLJ8FVIzssWcs7OlFT5fADazGTkLFmHNxi3Y8dILCIsIgyvow9q1KxCdSI88nygZTc5MY3qGctUpSE5OQVJClvAaGKk98OdH8O77O3DWli24/trPSS48OULCCJWCdYL74M7occ6oRVYTbJERiCRoyu1BTVOTsCmTUxIRZjZieMopeHlGHXFR4fD7guho60X/QJdM/pQU0mMtgtLMSE2WKrzs2mwHqmK3guz3zm43s8VoRmRURhoYGkFqSqLiM6jWqLSOEhmBVZUNkrpuWrcEI8NOsYwrKSpEakqMTH4OHRb2dDozbFYT3K6QAMlos9bd0y0ko+6eXkEg8rPtbNH2TyAnN0sWKEp6X7roXHz327ejvr5WlX7XITu3SGpcTDW5WMw9tInP1jOL2N/85jexdOlSfO1rX8Mjjzwir9E6L7Qen5wck9ePjfdhYiIBwaCiWkW1IpGEo3GL2gJU9U9gg0mwEoq6l2r7bTAiLFwRotFoA5qHoFbgF22EWfywSpM8EdbrpPjkDRng07HyTLMLAj6UhyNhmbx2DsttFhfBdEB1MAkaMOMOIsxmxMN/fwIP3/c76AIhKeCNDPQjMjxaeuQkWNBh1xmcEV5CbGwiBod6BHd90qGe35Ej+3HlDbeJmInBEMLwyCBsFvL+E+FyOTA+OYrElDQsXLAAf73/HqxcvU5SnTAKKwYDkiPpA7yx0bjo/PNQfrQCUVEbUFBYgKbGJnhcPljpiAyIESQC0VixrFTUiGtr6sVEJDU5VYqQVI0tLSlEeVUtFhTnYOXKxThwoEKgw3k5achiBTsuBs0t7TKpli4pwq//dD+2v/46/vnI3/DZa65FfEqS4MBLlpaJoYYBBgEoWVKSUVBciG3nbobPAxEjEQssE/3ogMHONuzdtRNBixF5BUVYuGIFJkccCloz3AaDTY+D+/eho7cdkfGReGfXW6KXQCWfT193HS69+AKMjxMPEYLdbpSQ3OX0Y0JsqhUhlsjoaLlnI2Pj8rRZ5S8siBcn2qnxSXSPjSMqLhaxkRHwul3obG4XAg95JaGgBxFh4cKKC7PbkJebDTN7+IwUZvkfJ3J5LR/mTspdn5/f3UuhVA9yc9NlV+MiLso8BCGJz0MAR4/XID4uFqULMjA4MCUdmmXL6DoUgaqaFlmY+/qGJHW77dZbFHKOLyip4bGj1ViyrEQxk4Gq/ydkIB0Gh4aQnZWNgV63FNXuveeX2LPzTVmEODZJCsrJLZBZxpSBC4B2DZzIDPk/ddWnJJUgCvBPf/wTfnfv73DzzTfPXi/nUUxMNKIjozA2Nirh/8BgH6KjExEdFQ8P559qwMpag+zis/dLD4tBj3Czgq1w+/VCtOI90pCZp8mWZv99CifvJAdZCZd5S9gp8JzgaTPbEsjjvIbOHHFGPiCSimQV9AGxMWFoLq8Ult5JxWFSkN1uASYwLGF/lTeRhQySZ1h3oKkHQRuMCk7UAY5icmoYMBAdZZCdZWCoD6kpWYiJjYfZbMDI8AhKShbj7/ffjUM738DW86+VFdNNtV6jCQNj4wj445CdmwJbZDiqymuxqGwBikqLAFcQwYBOMAY5YTY0tbYjEEiQCvCG9WvR2zMA58yUFB1HR8exYX0ZypYsQl1dI5KS4rB921rsP3BcvPeWli0UHnhp6QL09PRi/8FK5Ofn4ILrLsS68y9EXVUFenp6kJyRhgWli0QIhTeIDmOaz6LboaRcZJmx6MrOBEk5JUuWYd8HO2Dy+JEQHo2VS0tB/01uTswJuestW7wQjY1toiBE2PPoyAC2bz8P551zLppbaLfuUSyjdQEphvncfsQlxMrOO+MNiVQWCTJp6dEyccjVaGMh1E9lpERER0RgcngYzdX1EhVERUeIBwMLtExDwsIiJE3QjJ60NX129MwZkdqmQnGPiYlpKbqyYp0UkyDCGDqzsuNLq9Ksh8vrkhQpMysbeQXJ6GgfknRtzcoViIoJQ3Vtq4yhSHs4/vHam7jm+puFsTjjIotUj/LyGmRmp0pE293ZjYyMTMHZc33y+kOC/Fu7ejnqK+rwyF/vEcluKcqpO21cTIxIh9vsEQizRWFqYkpafIxaucv//K6fCySbNQcev/3db/H+jvdRU1OjXAfdsBFCQlIKQiGTEIJoAU9SGr/f1dmBuLjEWWKTaA/wRqqGs8QjRjAoOFF7FSQk29vU8phr1j134mt/18DGs0+CATz3b+WL/w6JtJdJXkRRCL6rHzoaDJ5OOkx7dzV88/F3qHs+E0JvZ4ey4s1ZZLw+t1Q9x8YnZbGhTj/hmcwvtRYgixkkzcjSo8IsBwZ6MT42JN0Lm9Uqrcms7AxRVwmz2qRDEJcQj+TMXCEJ3fubu+B0TMoNpEAod7eU1ES0drRj0uGTVglbe411zZiadsDAHJg0T4nydGLeSKklv5926iGkZSQjNS0NeTmZ0rI5cqwOdpsBBYX5GBmdRHVNCzZuWI642Gjs2rUPg4PDoLUgU4vSBcXo6e3DsfIWyUlXrCvDZVdfhFUrl8LCvE1VxOFEn31oc3ZGHgqJRI8Fi5ZIq5MDyRP0C0LP4/HD7Q7C5SK2gXz5oOSbP7zj27jpMzehIG8BoLPj2Wdfw/DIgBTFyCFgPB5mZWEvCo4pqtWGkJKciMyMdEHgjfQPoLWxGUGvD7k5eSLK0d/bh+PlFXA6/UhIjBVJdqvZDo/PJzh7qzVc0gIRoGFr2EForeKuO+ttqQ4HPm8uOnpDEI3N7WIukp2VJoOJuAciO/liajuyx88q/5GDx1BSUiyTv6WpF81NTVi7dg2iosPQ2NgBl8eJpKQEPPv8C9i+fRsKCnIw4wjCZtEL34Pux6mpcag8VoeszEzZ2QmYYfQxOjYpZLL4GD3Kj++XyU/ikLYJMaxPTU+Ta+PY0BB92rFy5UpBE1K7QFiDqlJPUlLSCVq2eu0Z6Zli1sp6AqHV3OyoGEwJO3lfpkjS5VCNaciaBMU/jCKTJ4VYZuTqjs+XmI2qqrK6EcyVsp/d4GftU/hCnQ/WkBcWBGCBHxZ4YYUH4fAiRgeEi4WSj0RjhAwKIOPEasJ3Vo0lKfinqWPKp5DGSZafsoN/7tbP45pPf0Z96F7ZEaannFJxJypOJLYMRilACRPK55M8STtrAf94A2htqhUpLxZG+AAiwm0Ij7AL2i8iMkpaKQUlpdhyziXoaGvGvx7+PWJjqYtEWzAnbGG0Dk9BY2MzvJ4grDSpKCqAd8YDH8UldUE0d3XDRZZWmFWKX/QRZPdiYorKwX5hT65avlD6wBVVjRikKWl2tnQcdu8pR15uJtatXYm6hiZUVNZLF4SYgYUlxWJn1t5JX7h+TE764HSGpLfLxc4SUsRTZu+vFsqpT1CBiwKpqZlIik+QnF0fE6W6/uhhpMeCQXH94b/d7hDGxgOIiorFFz5/E84/9xysWLFEMbEcGsTk1BjGRydkEUhKSEZWaqrsJJPDo+jr6IXO60dOWibWLiuFwR/AG2++gWeeeRYNTa3Izc1DUlI87PZIREXHICEpCRmF+cgvzkFUlML4nJyYwmD/kECFLVajFNzGxijLTkadstASsts/MISa2jpYrSy2xoguJHnsUZHhCLIgGAohLFyP5qZ2qSWtWbUCaUnRqDrSiP6uPmxcuw6xkRZUVraK0WdRbg52vrsDxSULkJtfKEmzxaaXCA4BP4oL03DsUB2yMtJFl0ETeOX5OKfHkZyUBK8b2PXBG2q4zshXQUFSYj4hJUlmn9/vEjixMqaVTepXv/oVnnrqKdGV0LgSxK8IOUpb1NXFZEkpo8p+kQRPSU5BWlo6ens7EB0VjsKCPNgoB8/2us8n4CSiSm1GAoOU85Xaq8arYHpE3Q11AeC6SeIe5e74pyo7OSvRMWf3FniXSAYLxcqgAEcYcdA/zSqaapTXZOVRuciTmIjSVxEXttlFQRRdCLZwOQXdx+O9d9/B3t275O+cuCajSaSeSKWkOip/LyoyBpNT07DYbFKAOjXMAPbu3IHEhDjZYRh29faMIDOD7j+9iAyPEPAMB2Ppig0Ij4zDX/9yDw7vq0RycpQ8xPERF5KSYpCYnIiammrZdukvH5sYJ7EPKab0KqyoqFAQVeqiJhsR2D/uVfwJPSEsWlgMq9WMgYE+SWO4ytN88tjxWkzPeHD+eetlodp/4BC6e6lIDKQmJyA7M0cKq5SlZnW3tbMDHd0d0sNnLcSs6tnN8j7m3AP62tHiKoquviYj0jIypaBmVJF0DifhxMqCofEufD6SGVdOBAAAoYdJREFUbgICfy7Mz8ZZG1dj3do1yEhPhc89g47WFrz12os4evigYEIKczJRtjBfFhkStA4cLMfw2DDCwoxYtrwUa9evQHp6smgSJGckIzYxBiarQjwhGKW1pw9NrW2YmnEgNiEBUdEkXHWgp7dbdBZoBmO2KjLZR49XY3pmXIqr1HFgwS47Kws2m13Om+PIYtHh+PEqgTRv2rRe4Np7dlcKZn7t2pWwWCglV4FQwI3crEy8++b7QvdmUSwuMREGow7D/UMYHBjGwtJcfPDBMcTGRiAlOVaIRmE2u6SJ3C1ZpE5KSkFH1xj2E3ui8jC0SUvzWXIHqE/JArY28bU/q6qqcN111+E73/uupF78PVLQb7nlltm2oChEmy3IzS1EU3MDHI4pmFgstVhluJG3waKrxRSmxOBBamWyQqRQ8lVCrSJ1Pm+WzJsus3uITG2VCnxSDUCENuf+xry8gbsyGwlGFgRVp3LlXsy1wOKaYlbJQ6p8swGYmhjHzLSCee7qaJ99fy4MbHdQ3puagGOjDmn/8UIpBiJ4fZZg5xwa2+r44cPwzHigs1iQmpKMppZWJCbGCkiDk4dQUoMxhEVlS7H9wsvx0pN/w4/u/Bqeeu0DpKWnoLm5HSOjVpQsyJQI4/Dh41i5cimCYuPsFGpuclqiPKz6ugYsXrJA5KNIQomOIIc/Hk1NrSgszIPPq0dxYY50SlpaWrB8WZlQcZlWsPA0PRmFxYvzMTWVjsqqOgE5UW8g3B4uBUJO3BDNSyYnUVa8SP6t1wfFdZbFpfCoSOU13EUkGVT6mQGrkmZRryArK1MEM4h14ILV0d0qWgP52YVS2ddQqiJgGlSw91prldiF/OwUSYfaOlow0N+Lw4d3o9xCb/s4REfHCrMvO28xbDaD7HYc55KmSFtJ+WK9hynb8PCwGolFICMjXV7f1dEmNQbCrJOS02TwOlxu9LQR2RZAemoCZpwOdHb2ITcnR/r2DgfrTQTfMOXxyYLKnXrl0iJxiKqoqERqSiwWLKAmZBDvfXBI8vKSggzs2XsI4bFR6Ojrw5ZN22G1GzDQNypafWvWLsfuveWIiY0U8FZFRQPSMzJkokoh1EUjGScyM6Pw9JNPwTlDnr9CvufELV1cBqstCk6XFx6/VxbwufLwcp9V1OZTTz8pBdcNG9fLvWY3SEEEKuM4LS0NOoMP5RX7ZUIxAmZhnJMqOiZJ7Mb8FB9h+3pmCpGxJhBCJDv+rLIPJGpUtqYTeCnpFGrTUk0pZS6r/9ab4ZFQX4HvqMaBs76qJ75CMqn1sDINkPugVvmF463EHtouxfiAr4fBQqC9hCOsqmqLxFyjSvcMZbRGcP11tyIrs1gsv5xuB8YmRgXswxbU/ENWWB3Q292B5sZ62ZHZmqRa8MDAsEB0ie5iGEVhiayMDGw552JkZGTj8MHd+NN9v0FsnAn5efmC866pbkdufjIy0tNw4MAR6II+BLwetDW1yCCPT4pHXn6OhJ3krFOthu3KmKgwpKYmo76eOIYQXM4gCvIzFaloP6Mno/DWN6wrkxvPlIBpw1mbliEnKwttbd3o6KKxqVJvIQWV+PTO3h6YGXGZjKJXz0nTQxqzgZVmnRha6E3sfAzD4/BJ0fTs7echOsoqCycRj6TTckHgTlpTV43hkRHpHJgomKE/0T7TOOdirDITFC39RQtLcM6523HOeRcgMzsXvqAPVTXHUVN3FK3t9aI8xN6+TPoQC5Ie4U/0dg+INyLluWKiY5GVkYVwuic3N6C5sVkW+QXFpcjMSpNFn+jKoYEhxESHC1JteHgSVnMUVq0skwLi6AgVnfVISDIIKIvW8NmZmShbVoSBgXFUlFegqDBXFtbBkRm8v2M38rKzkJqWgqPldSLn5nA5UVxciqzsRAz1jwlhbOXKJThw4CiSEuNQXFyAI0eqkZmViTAiGRVUvNCC6ds35fDjuaf/pWyCcyZ4bBx1IcLF/pswdrGYm3do/X8S2jaftUlUlT7YuRPf+973FHVlVWehpKRMIhLSfGkQwmfCiIhjLy42CWHhUapEmkKJDqc4KSCRoXyOapGmyS1oRkqmubBrmfUnanSMAEh6NDKUV5BWLLLxTyWEPykQUFcW+QABCelYJIef8st8pxAXCNV9dRYSoJfVh6s3NQpJRaTYg1bBVW6ownPmxRKHXV51BD19LTAZuetEiPIKc/3Tgb8Vo4YA3n37VazdUIa6xlEhqoyNjQj/n1VnFrLstgjhpRcvXITtF16FJx/9E/587104e/sFKF68CIUlRairqcHxI01YubJQdoA9uw9hy1lrJMysq27CgkWFsEWEISYmBpU19VKYykhLltA/NjZSUg2yA6kbwCJhfFw8mltaUFJcgPLKakRGhmNBSQ6Gh6fQ2NSMgYEILCjJR3x8LEbHpiVa4aLC25Kano7R4SHUNTcjIzlJBCpSU1MwODqJloY2mdgJBM/YmdMZse/9XXA4/Tj7wsvholIM40I1tWToy1SKAimiQjSejFTWMQxmxETTOVkpvDE60BZk3u2ZGSUyMJrtWLx4oaLB7wxgsL9HpNP27NklHaCk5FSE2cIQFRMtWw1RdNRYYNrX09sl9Y64mCjk5uYgJjZCEbj0UCWI+TXx7T7RCmDRlPeoZEGBLFiPPnA/pqYGhN/B3jrsdqRkFGH92lVSL6qqapZW7/LVyxAbZUJ5dSuaWtqx/ay1GB4aQ2Nzk2LioTMgJjIWixaWor9vVARPigrzsHv3ISxZUiTozn27D0tkp41LLpDcULrb24RMVlfbiMMHds9OBL5m7bqzFBr2yIREhKTcshhtsM1PVU8stPzz6NEj2Lply+zPtM1t+dLVOHbsuPydkRbvHRGc0dYwJCelwm4Ng0Heh6KjMwizpqvzUy1GqgVW79xS0Slnoioiq3JpZi50JMWeyNcZrhAdpP7qrK8ylwulOCWxgbpCWk1mmHQG6EWrKSAnyBORlUUjfZBrHwBsIWB6dEg55zl2LtqK6nRO48jRD/CPx/4og1FujNbolNCSggZEu5lOWYl3vP8Gpp2s6ymFQFJf+/sHkZGVjo6uThTkZ4gLUFpyCspW0IyyCBs3bxEFXsp7kySzqGwhfF433n/vCDLSE7BkyUK8t2Of/K7JokddbZOcT3R8LFYuXyqditq6Zuj0NG4AkpMTZNXu6R2WBTIhIV4x+wxBxD2oA9DdM4L4+EisZqXfwiJVg0CSmfaE2S0i8MmBxKYQc9XsrEx09/VLNZzU0OTEKNEyYNuxqqoO7R19woq87/e/x1XXfwaJ2QkCh569NzTFZGLIDkZaCjZs2iCTaWR8HNU1NWhv75OKMwtvSntJEZFQBqxi1srFwekMStWcqU1WdhbWrl2Fq6/5FM4790KZtLT8aqqvh81sFIEJtu4or16Qn4fFixdJFEJ3nP7+MXR29qK7rw+DE6MYmJpEHwVMJsYQHcGWYRSaGxvx8uN/xWcu2Igfffk2fPbibVi/OA8fvPQvPPbgPXj9hZdFpIUR2cpVS6WLcOhIlbRhzz/3LLQ0t2JgYFDGDwk9DNmXLFooXYfRkUnExMaguroGa9YsE0HYXXuOoKA4V4qghN+y789SM6+Z3pOR4Xa88sI/4ZmZlnBeq/5n5RQiIy0H/f39MFtMMJgVspbGVj1poxJTUSXw1iJfbcHl96OiY2XS7zvwPowmi0x8agkS82+x2EQI1GSzwkeUH6HqHo/UluYeoh+qbdgfwkaWoh9r4FTyUqMBxbcyxNVPLzmm0BlJ5RI4pyLtJFLC4remthdYUTQCYXoTbDozWGcyz131tA/kezDP4DdEnXf++SmvpPwUowESdswmYrFNOH78qNK6IHdZR9FMs1gssUCicdV5bo21x1FfWyk4Az5oSlSzoEjlU9J0+/pHUFxUJOyvC887DwnJ6eK0Ula2DE2tPYiIoL5/ECtWLhZU22uvfSDU4fVrl2PnrsMozM9BdGQYutp7leKnXo/CogIJF8kuY2jNQil3FlqPO2Y8Unnlyj04OIrCghxRGOKi1NzcJZMyPy8DOdk58HkJ2+SuojyILnGK1UmrjBTQBQuLBTbb1NIiTsFMCcoWFwiGYKh/QghE3/nu93DRZedj2hOEzsTBNueBq4sBC2hM33Jzs5GXl4XVa5aJxFRdQy2OHK+A2+uC2aaoxsizZign1lvEnutnBzFbi9PTAZkgJnMYlpaV4fNfuFXacW++/Bp8jmnk5+bIQGZK0NPbi7aONhFyHRkdktYui2ChgAd1lUcw3teE3a89jt//6juoKf8AKUnx0LtGkFdShKjwSKQmZ2LV2i145fnXcMPF52JmuBEfvPkEDux6AxNjYwizm6QLQhbm8SPlkpbxkmntxTZwVmYe4uLCEfAGZDGlruSqVUtRU9OAiqp6QQF2d3YJ8i8ljYQ1gqGoGVgnjLyA14HXnlfCf4H8BoNYtXoj3E43IuyxosLk8TqQnJAojL3T0ZRPkISU6EEpJJ7gR5QULhKswcTEIFJSUjE9PSUsVUKSOYZotsLWNb0VxPVPrZed7pDl59TSvvLpamGTm7RkHmr6YHRNe2Wl1OsCsnJTeICyVdzV/S6fGCL6PC6BbloZy+uVcJ+RZlA02PSiBMQ2BBdAbZE4Mb8V3X4LoZ/amcw7uJNt3rxd+sfHjh5BQmIiBvt7YWDlSAUXUSGYrSOSUAj95apNwgNxT++++QJWrvqJKP0S88/Qjj3knOxMHK+oxsoVi9DG3NQXwFe+9h1cd+1FuPYzX8DQIPPPKJhCRlRXUSU2CZs2rcTevUdQXJSD9auW4ODBY1i5aoUiIOpwwe92IiI2DskpKdLjJ4GHBa+FC4pQXJSP1rY2LF5UguioSMljGTqxQElEGVf02roGpKelIjEhCna7RSr5bDAQCcYWmcsThNvnRm9zL9JSU5CSHi/gIxauKiorxQcxNTUJq1YsEHUbbtwM2c1GxUTkdF6bym6juh9LUKBHQVEWvJ6A4CbqmxplAOdk5cFIsILRKMIoBEx5/X5hVLIIRw6IBAqqthzbkFxwb/viDag63oJnnn4CvX/9I/IKcrF46WqkpGTA6aKy87Sw0GLjEoWz0FBeiYTANG667jrocR4a21tx3/3348/3/QJjg/1IiIrCl++8AxidRGByTJCln7r0MrQN9CG3oADPPfMinnjgPsSnZeGci68QfkJaeoakf+lJadKKzc7OQmFePI5XtuLIkeNYvmwR4uKi8cZbO2RcsjBpsxhRumAJgiEDnO6QwIjZDq6srsbCkhK89/YbGBroUyze1bR1/fqNQgajWQ1TrOHhUfH28xBANa9exSjk85//Ih786/2yoJwEwVGL5yTC7TuopBiMDNvbW+UZMOIl/Dc2NkHs76SIGyKRyiceAcqD1R7wCQj/mQ6JztVG3UmLhsUcgQjRPreLOQL57k7aZlMD3euGWU/5KUUqimG26PmrH8ydTlEHDsGsC8FAiyKqyvi8Ql4xBvyK4y9zW/ZLVWNM7dCAPt09bdLzX1C6WDQAOjs7ERUTqwh/zulvcEByMOYXFGDDxo3IyCKJA3j79ZcwM+2W9giJE/awCNmVyA2Pj0tAU2MXlixagt179mPT2Ztw7nkXY+euN1C2pBivvfE2Gjs6kFeQjc6ObvT2DuOcczejb2AE1XVNUqmvqqyW+gaLSmMT4zDTtstH0Qwbli1dKAWs/YeOw+NxCSd+dHRS4MmkkvLk+dDIfuMikJaaiJHREbS190ilniIYQp31KwAR0kEJG2We397ZidaWbllCCwszxX+PUtWVVTUijOlxAX63CviYw6//ME0aDZ4httzU2i/IlY4F9enGJ0fQ1NSA2upK1Nccx4F9u1BTXY7mpjpUVVehrr5RhFzo00BGZ29PL0ZHKDnlQMnCfPz6tz/CD370c+TmFKGhtgrPPfkohvv6pHBHhB1bZuQxMNK8+NxtUnQO+X0oLirBA3+5H9++7XOICA/HV75/J/7yy19hgrh9EpNsekxODCPMQjShAdd85jr84q4fojgrAa/88wHseO15WfgXU4/B7RFWXUJiDN7beUScfrZv3SxAp9fe2Cm4EYqAJiYmICMrFx6/XtSuWMcmCOn4sWrp0ycnxuPlF59QJgm7JiFO1tWIi0lGajLhxoNYuHCJqPOQHEYMy1zOCg8+38rK47JRzZn1Sr0hRDxIhHBhjh0/KIAjLsycd0KSSk5DdjYXZLpAq5Rgv08AQhFRSgpwEiNS4wdohirqZqABg8T9es7c1Q6j0cQKtNJzDrIbYGUtgDvuhCCdLFazIt/k80qZkDBEo464QAJOFNFB/oKHYU3ADzvNPIJ+hIx6EZFgOsH3pl8cw1quhFr+o7UPfX4npifHUVS4AJ1dLRgbHZYKeoSq0subRe40gUTEZS9csETC2oSEZIwMv4iGmio8+9STuOGmm3HoWDVmjEbZUesbGrFsaQkOHKpBvDdOyBbHj3Xhxz+9F7/65fcRbrNj87p1OHT4CHLSU7BqVSmqqlpw/FgN1qxahq7uHhw/Vo6CwkK4PH6pLxCuTMWirKxcJCXGiPBHdlYGEhMS0dXdKyAewprj4pbIVsnwmROa7cyh4WFZyVNTE2VHpXtMWHiE7CQ2m1HksWg57XRCIhO2MwcGBlDfUC+KNIwkiqPyMDIyJczJgYF+5GVnIiYyUtnh1Whznjv8aVaAE+kBAUI8EpPikZoaj5kZv/S0GYKnJY2JMUl8Srw8j9HRCXg8MxgeH5EQNSI6TAgyrF9UVngEwkoQ1OZtF4muXnP9cbz6yqt47YXHsHzFMpx11nmITY9Gd2cbjh3z4fwrLkFozI2AYwY6ox7XXkWH4Ty8/M7bePWdt7Fi7ToZ8Ext+nu6sPGsrQixfeZmBd2Eiy69BBddcgkOHjiIHW88h53WKFx42WWysdTUtCA3Nx9GUxDdXf2Y9PhxzoVnwz/jRkRkNMxmxd+AuyoHOPN/toWnHVOyWe354G3U1lSo/XoFtbd+7WZpD3MHZgcmOzcLHqcbVr1ZBFEUj8ETGzEXhF27dp36CNTxv3z5GpEfY2svOycffX2K0AfVfOJjkiQFYFdHydN1qmmoX7QzT31PxXlLRELmHUzBz7QpGLlz84RthL6arOJuQ2WgmBir4Ovpl04UV5jFBmu0SSihWpwpeQXRRRR99UE8+FhGESy7KLoqnm0kEoVFRQs6T0IhFQFlIthBF4LP40NrRytycrKFA0BY69TkuAB8mJJEhkXAHmYX0ws6txJWK10XHQ0ew6QN9qd7f4lLr7hKZJ343uwCUFONCkM5mZlSHMrOyUF1VTWKL9iKxUvWYOcHB3H29jWwW8/C4SPHZPCWluZjYsKJlpYuQWFRRourvTXMJgshnYSPHhtReQnRsjuz/8xVmy0pSp9VVNbi2PEqrFm9GH39E7KrETSUk5uHlpZmOJ1uYbVlZqXD6fSIQo3fPS0kn47ObqxYtRqTTr8sJgz5o6JiRNK8pq4OqSnpiI+LlK/xCQd6B4fk9+lqy0GowUQ/hjTdSQsBF1SG9IQ1JyTESsmG55hI9R+1f5yRTSttIL8oX6rx0llKBoqlc+CShYALE8lT1ApYtmwVVq1fheaqRjz/+N/x1duuR0q6otmwNz4eS5csQnJCnIyVEJlO0ZHIycrGz390F2pbmrB33y5s2bYN2dnZMHPccVwFAjL5ucwFPAofYs26NVizehVefPFlPPnI33HNDbeirKwUnZ09GOjvRE9PpwiojvQW4+prroFzhl5+eoHOaorBbS0d0hJm1JaUEI9f/PBhCfsFner3Iy+3EJPT04iMSpCiNBd8Mh6pO+mYIFoxAp6g91Ss/YeIhC5atAjvvf++bKrcCDraW+T7qWkZyMrKEVQlqc2C7DTTjNQrc456i3Of3exE/zj6/PMOSnaQeoFQiIIDZkSFRSHcYlP9xfSICY9BfnYuMtJSBBjDGyJQQwpFqGEHFwErZZ3IOaYtEyvIrHQKvZiwT7Y3okR1R7tBUBVX9ERTWcwYGOxEbDTbPrT1tisUSpMFy5atQ9niVSguWiL5Wt8gMfSHUNNwDDt3v4fR4VF5bXdHM3551/+hdGGhpDAsAhEl2NrahpREYuVDQrRgr7/ieB2uuPJaNDdR0y8kkNwtW86SLgKLQ+Qq5BdkY2KKyipelJTmwe0JyMJkNhlQUlyC9o42DA6NYnhkFDabUjHnJDKZbNiwfoVgElra+iXl4eeyv8/rXrN6Cbx+t4CWautqYQhM4Z9/+CUaj+5HSVo00kxBPHjf3WhqrUd7S730eqPpUZifL4In3b1dqGlowuDIuNpezEVSSprsTDROPekGf4JDq05zgPE6+B7UgPQTWERmoax0IYSo9+YJiRcd5SNY1HTRCNVuQ1p2KkoXFUvHoXjBQji9Xrg8ASQkJSPCHoGrLr8KeWkp0vc/VluBXfv3ora2TlR/6VbUWluLJ556EhMz0yhdvAifuvIafPD+Duzbu0/g21yBQ9KLEqtRhfCkCyDoJdTWh8uvuhxXX7Idrzz/OPp6+2C1mdFQdQDhehfOWbsMR3e8jnt++QuhzrqlABsU6TDSlgWLIcjXEHa8/zoqju2fle/iceNnvwCXa0ZaquxkMV1j/z8nRyn+cgLzOcu4PinNPdX9it+j1kCYPRyNjTWIj0/A0GCf/Dw8MhpFhSVCbiLrr6G+DocOHRBYM/kYLNizPfnJHu6Zx4RiozondBB0F7sB6gqvMxmE2OuiXBbDTGkNqMQUtQUh6j/EchMhRikj1bJZQSWQVGOUhxGbmDB7E3gwN6LcVmx8vPTEWRBbu3ajOPGwErps+QqsX7sFBYWLYDGHSxWZggokJNETj9JIkhupRo6PPnQv3n71VZnkLBqyyk4aaEf3IAoLs9HR2SEXSzDP5KQTq1atxnPPPIPwSLZG9Fi8aBGKigrQ2NSCV159XYoX3b29GJ1yIjraAKtND483gIT4CERHxWBsfFwUazu7BlVehCK24HKFBL/NVhNbQxw4xCb09/WI2cSGtWuFWjzQ14Vvf+N/UZqVittuuRF2nxvrt2zEYHsjnGMDCDMZ8Oc//AkNdXWICqMXfTgWli4U6fG+wX5U1dWhtq5ddqT0rCQh8XDwyQA8s1jsR48XlVYm/vEqXVdx29XNfomGvTq6SJ7xqchCtiLJopN6ETnpkQY89q9HsaQ4G7/41V343S/vwqcuu1iizHse/AtyFy9Ea1cHDh8/LqnYtTd/DtHx8Qgw1I6OwRe+8GUMDw3j4YceEnVbpgO0NZs7shkFsEfudzuwcuUynL1+OXa/8yaqK8pRVlqAz3/uFlx88cX417/+iYbyvWioqkBEGE0/9eju7BOSDyO7yoqjIgTz9BMKT1/D/KdnpMNssSMtNQNt7Yq3X3xsvNSb8vKKMDo5rngLzKeun+nespi4bhOqa6oEJ0OmpVDOdToUFBbLRhkXmyCFbm40pJBzcSGfhh0ypodz3+s/OWYXAKkOzuaHc16h8Y7n8v7n3H7tNZzvbBWG2XSI1ulhI8aZdEW2Hohq0wHp6RqAQV1A2BIJ+DA6MiJ558TkCM4550IsX7kGy1esxto1a+EPONDc3IhgyIu6uhr09w1LSN7fy9bhCY81rVDy9S/egMqjB0Wkg1BUtvwo9sDBXJBfgP7BAeHw19U3oLhkEWxhFlx/3dWYmp4U3XevX4+yJQuRmJAi8t90pd3zwW688uKbOH6kGmHhtIoGCgsKFBvvIFBZWSF5PxXUtJvC00pJShAa89jElNRBklPT0d7eg1ibDoGpSXRUHsX1F5+Hr37uswgO9ULHXu/MJOobarDjxWcQbQkiJRL4/h3/g/179iMw4wZFbOPjo7F40QIh4ZC12NHVjqaWLrgCXlVZl2CtE0XW0z80dQCoIjDan5/ITlyyDaXqpPCUTgCKNIg4pcFIDV65cgUCLj+irfH44Y/vwtsvP4uQz4nb7/wmVm/bjOXr1uDTn78NSWmkzYZgsNvFC5AIz6uvuw6LF5fhbw/ej6GBAegtlpPENLWTYaE64J7Bxk3rUJodg8o9r+KSCy+F18vW2pSc62euuwEH97yPmEhi/bslzdqwYQH27t+DrOQclO/bh4aqI2rlX7kXq1etl7QtKjkVk+5p1NQT3KWQzSIEaDUoyMaPc+8YEXKxyM0pwP6DexAREylANS4e0bEJKCwoRlpqFoxGM1pbGxAKOREdZUdERBjGJ0ZYUpb09r91nJw1fMIcYlaVhBGCw42hgVGMDA4DAa8AhvSM/WlKyMkZCCIpjdTOE9Aj+buqgjo+MYz2jmbYzHasW7dJCm9siYxNjMAxM4KGxhqptk5PTcwiCufecO3vM45JfPmWq/HO6y8hLSVFJin7pjV1DchIi5dCICOPkuJCHDlSge3bz0VxUTGuvuJcTDmmYLMS6x/EokWlEm2wLlCUnysFmSOHD+Gxx57D0MgYkpNsKMjNl/wwKjoadQ0NcLp84mHAdhnHJzkNiQmxSElNRX19gwhpEHBEKvC/HvsnbrvpZpy1YYOIkgY8M9BHxmDf7n3o6O/Dedu2IS06Epecvw2ry4rx4F9+j+ee/Buef/x5DPRoSrwWFORnobC4SJyQ+geH0N7Vg6G+IcxMzSAsjASgE/dHGGmaXoNapW5pahE13plpl1hgEwl3ppbS3IVCMbhQWmeUr1Yp6mr0cSICUVh+YahvbITBYhQRzsDIFBYVFOEfD/1NKLjf+d9vCACKkX2ABQf1A3R6CmH4EPA4sWL1Slxx1dV4+YUXZGc3WMgOPHXHJZKT9/Kiy6/AirLFeP6Zx2E2R6pgGZ3Qt7t7OtHaMSFw7bi4WDQ09IsITGJCEv56/x9mFzO+P3fj0VEagxpEY7+wqATHK8phtdHwRCnQjU9MyFglgefDDoFdh0KymI2Oj2NifFSYl5QhN5iM0rokcjMuLhk9vT2YmByXduMYvS/sVgwN94n4DDtNsw/xPzxOMgYRVC9x9SpNSIP1fpzPYbuGQgtWux0zTpcgq9gzD7mdmBgeEmbXiuWKC422MxG6S3yA3sgK8wgGR7rhcjnFymp8YhzDo0Noa2vG6NiwhEqc3GyFzA60eYeCYuOCMoMff+/LePTB3yIrjdBXkxiEdvX0IT4hVnGr8YeQmpyMmqoGfPmrP8Dll12JK87bBOf0qFTo2YvNy8vBzPQMpmZ4Tkk45+xzBGPw6qtv4p33D8tOvKi0BAuKS6Tn297RJYAfpycgdmnijeANIdpulYVnamICBbm5+OnP/g+33HQNsovykJiagqnpGZiSUzHc24WX3nkb8bEJWF62hK6TMJjNKFtchvTUJMRE2dDedAx/+e2P0dvdhbrGVkxNOuW5JSfHCvchPCoaLp8bx48fQeXxGvGR46Aji47RgXh0qj4P3DGTk1MFjj0yNID+3h6MjowLrHV+dKmg0NiGcks4ynocOQrDfQNwT03D43DCGAiKxHeYSQcbq9LiZgyULl2OXfv3y4dynNBOLDwqHs5JB2674WZs3roVn7v5RoyNj8BgNSAgxHYW1BRQuaASPTNISU/GZ26+SeTgdrz3njgYn3YRoGSY14nP3nyraOzteO9tmKzslFCzIA69vV1Sm/KrE/j1N97ExvVn4c3Xn0NrC919FJg5D9LG/YEZHD66E+EWCxIS0ySSmJycQHomcfuU746TWpFoKnzIoY1ZbnCHDh6UzsrE5KR0zSLCI8X9NyE+BTZ7uMCGe3u7RVszKipCBHa7uzvFU5ObpoCJ/gsrwEl7viYRpMoDfOTba7UFXhapsFylwsPtApAhyoo7g9/nEdJFLAtWxUWC6NPC9aA/gOTkZLmowb5+zMxMo2+gV/JkTmQqEA8PD2FwcBBtzc2i4Ctoqg+9ycp7c7X9429/iof/9AsYg37k5hWirrFBFhiCLIZGhgVUFB4RKTZdt9z6dXz62k/jou3r4JweEb6DLSxcVHy8Li+Ghodk5hSXlGLlyuXiMUe7bO7mSYkKxJeinOTgU8VFkJP0gVN5+xkpacjPi8Nv7v4Vtq5fhpLSBfA7HAgaDDje1IQnHn8Cew8fxqhjGiUF+cLSqGlsQG1NrYCHbBYTCnMz8e1vfh3bN6/HB+++jtzMJNTW1qK1tRVjY1OyC8fEhYu33vr1mxATFYf+/j4coqtQVb3YQtGsM+AjsETZqMMj7aIdn1uQI20tTvyx4SFm9vPvrNw7SoG3NDeivLIK3d3d8PldIloxPNQvdFYClkZGJmVy0cSCHgxFpdnw6ax46rF/whBLzr1RUKIFOQXYs3M3Lrz4Enz2xptxzdWfEtMLRgrEgMwdgRQ5DXk9At757OdukbD55RdfUBYBEb07eVQoVnJefPu7d+KtN15HbW2lTPqISDtiYmOFoJOaGo/KykqkpaQiLjwCf/3Lr5XQXx1hXByTUpLR1taE8uNH4Zjk8zUiLiEBvf3dIpRCkE5qihLZsm51xrmiRqxkHJLdWVtfiei4WEE08mc5+QVISkwT8FR/Xx/6+rsk56dbEReYUCAgCyTrP8o4/wSp2occJ5mD6uZxAD9qAZhLUResAKvFeh2shI/qWA+wIYkqMeFWKQwlpqQimdRHdXuhzRNX24LCIvkwQiuZ95BBxzCHjsJDgyOYnpycvYkf55DaAtlTBgMevP/3+MUPvgzHcC8K8vKld+50TSEhPlYmTlZ6upwnEYM33PZt3Pblb+CH3/kfWE0+6cGyt12yoEjgn/SjpyIxQT3syRcVFqK8ohKvvrFDfla6sFSYcv6AF309/Zgcp69AEO5AADExOjz68AuAZxi33vYl+D0zCrjKSGPRc3HepZeJ2++Lr76Ecy88B+Mz04iJTxIxz2/f/r8oLSkWK6iutg5sOWsLLHCjtbEVmzavlPZqW1srqqsq0dHeg2lCiW165OemiBzZ8lWrpBLP+J/96/a2NjinZuThE4rM4ivl0CnXTQOJzMx0IXOd5Laj00snpyA/FyULSrGgqESEK8LtEdJhmXbMSEoxSc+AzjY0NNSISrLPNS2byTnnXoRjNfXY+c5bGHFMYHzSgQ9278L6LZsQ8Ppw9nnn4n9v/6aQZeqrqyXEp4LO3IN1HLG4CoVw1dVXy0L+9FNPQm+yK8vVnEkhtQjhzgPf+d6duP/Pf8YwXaNj46QvTlj59LRT6jyfufY83P3rHwrBSKvS88/RUWoGLBaLc4be7773JpLjYxEeEYPmtlaJBqllERUVPS+xPfXQxu62reegurpWnKGogckxyhQpLycfYfYoGfe0giPegt12Fqwz0tLR198Dx7RDWIjKpX60D8HHOQSmb1Rpgypob/bro9YYASCpTQEi5SxqHigRvk4nfW1DADByYdABKTGxSElOlglKiaqsrCxMT05jcGAA0TGxAhmdcIwJJyAuJkF20NHhwdkH+4lWPSkwKt2BnR+8j1uuvwhh+iAyM7PhmnFKyy89PQ219fXIzcmSos6hw8dxxTU345LLr8av7/oJrGZyDoIywbgIUKKJE527PP8kSIdQTuaSx4/XyoChi43H5ZbBRFRaR3sHkuMNOLi/HLvfewH33P0LBP2sEwiEUqqnlH6OTUyUxWntqlUoKiiQyMfjdWPK6RJpMgJromMixAiC6j1lC4rwynOPCW2D0Oh165Yiv7AIjhknqqsqUFleibb2fvhdfkTZjEhPjUFaRiIKi/ORX1goQivcpZXnqIORCzcliAjrVvv+3AmDs9j1E/k/O8H2MBPSUqORlRGPskV5WLp4MQrzC5GdmQWbnXbj9HnoE2h0TXWLqEKHRcUhLScXxkgb7JEWXHTFJUhISYRBF4TP5cQFF12E//vxj7B67VpUHi+H0RIu4fHcnUiiO7V+ccGFF4liz5OP/wt6k+2ERI1WzGao7PciPjEZX/ril/GLn/+MqoqCSuTYqqtrwC23XI8d7x/GSy/+U/rxcw0+CORhhLBs2QrZjbmAdLW3iNBIW1ubIDzj4uIQHclJSUi398y7fzCI2Lh45OQUYu/unbDaw4TYQ8h1TnYeEuPZ+89GY2MtOrtaRWGZ4jLEGyQkxEnHiVLz2gLw3zr0rNST8KeF/Ay8POrXbBB2uj4iq/vs/8+RGeKfVA0yqREElcG4MEhtQdRMIZ7uOZnZSE9LQ252tuyonPC82S5qA4wMoLe/F9NTM1i+fMXsDfx3D61FSMTVtZdvR0dTLQqKSzA0NCwOMN6AXwxJeJNpQ0Uo6LpNF2D72efjb39+AEaDQuBg75WFQbZLxsYnBBU4MjosugPRlMFKTIDD4UZffz9SaSoyMyOswOTEWHS2tuG+3/4Ev/vNT0XDn71WpdWmFFpoQBlyu7FixUr885FHkJdXID1/fgYXxd6+HrjcUzBZqCBD45YZrFq9Ft7pMTz/zItob2tBRXmTRFNLlxVi7drViE9KQc9gH2qaa0Wzr766GWNDkyK7TYZteIQeYXa9qrwbEjxFT2cvZqYmxRRGgFpGHWwWnRBkmI7RM6G+tgHVlbWorqwTdaCjx+rR2jYgfnacPI4JBwpy81BSVIzoqAQhd3FypmZEICkrD2+++5aEvqZwO8x2C0KiCc7KuE6ihc/ceDN+/OMfYd2GdThy6DAM5nA1H1djVOb8AQ8MIQKBZrB5y1bxd3j+maeVRYC5hWHOAGc+73aIgMfZZ5+LX/7iLmEFjgwNY9myZbAZ9fjBd7+k9L/nCtwZzGIvzxoUEazcmdPTUvDOu29KPYZpB1NWu9UqilXxCUmS059uvMoCAGD7tgtQ39AEp0upZXHx4KKzZPFKhNljYbXY0NhUjeHRbric0xjs60RSAlWuFTQsWYL8nf/mwQhEKQqd5ofaZVCEkYWZ+aYHc4vFWgoxV5DgpNugaZ8tLhOEIZWAW1pbZAFISEiUcIohEds8ZFiNjtNbPkpkrrSQ7N89+PvM7TraW/GFGy7DE4/8BQtLizE4OITN61egvqFZesxh9jB5yMePV2PBklVi3vDgnx5QbW7Y2w7DoiWLxCCUhbDFixajt69PCmKDA30ifkpdg76+AeTl5YrEV1xcBH5x1//hh9/7HjKzchD0KciuE3dtzr0LAQlJiUjLysCCJcuxcOFiGVwCNAkjCEsvXAjSXacd47jowvPR1VwpSkRcWFqbm3BwX7l0PjIyErFp03IsXLwEmXn5MNnN2H1gL15++SXs3XUAH7y3H8cOV2NqbBL6UFAWQKstXMBEo8PDaGyox+FDx1BdUYvWpnZp1VLoJC09DWkZGfLFnTQinIYqI2htaUZXVyesdiP6+3qlPpCXnYbFC4rR1lCDxx5/BctXr8BLr7yKmqpK6I3m2chCG01sj/ncDtz+re/g9m98DZ+98TM4tP8ADOYwSVVOW+zzOLBuw2ZBHr78/HPQG/jak1MHQfN5HLjgootF0+Hll14Q7kdqcgR+8N3voLbquFT5tT4+aefEW3AR5rFv715kZ+XC62cLziJ+kBQDaWhsELKO1x9EekbOaceolvtHRIQLaejdd4gvgdSfGEFwsY+NSUBeboGAfsgd6OntxMhwv4T5C0oXSGRIsZj29vbZGsB/6xCVIyr2SFvoDG1jFpeo8cbFYp4s3cc+BEIMYNPGDaLEW9/UJNxshkbMXVn44DHYP4Dy8sMSGpGEs2zZcuWj/oMFQNNyU2CZXtz3yzvxg2/cjNhwu4SzZ2/fhIP7DyIiMkwQhKTbVpTXIS2rEEnJCfj5j38Cg441AT90eiPWrF0q1mHkoRcXl4hFOaGag/2Dct4ms16UifKzIvCrn92FKy48H+vWrxGgCrXpThyKnpaOiaqq2EJ0JHdF7m72yGihMs+4XSIvTkZjU2uLqCW1ttUiPSNJ0pGJSa8gF5mbp6VkCGz4vXd24tixRjlHQqozs7KwbdvZ2LxtGwpKilFQXISEZMJaQ/K++w8exIxzSvLZyMgYIR4tWrhQohk68nJpt5qsYtkVHR0plWkuGjm5mSIasmjxIikAt7S0Iz4hDllZSahpqMNbLz2HwugwZEcZ8cz99+HY4cP43K23Ks9ljr69dojevseJn/7iV7jyyivwwx99H7t2fAADyWqzpJoTB3ddVvzPu/AiTDumsfP9d2EwMGo4+bViJuJ14atf+yYyEmJwYO8eHDpQiUf//gdFYZn286IGbJKiG2G3bPuxtTcxMS6Rnkh0RYUJl4FSYo3NdYiOpsMwTVFZtHadkqpqC8D5512C+vp6jI0NKK5Jdmr86bBq5QbYLNGwWc3YtXcXuns6EAh6pV1M6HhqarowUSlOM+OYQE5O7n8NBMSDDlCz6rNzWwLSFuTPjGSbUTzYAFNIcXFlj5+h/nzYAKMIrqGn0/DRTpj9fYZgExMTc3ZCJVTXbtboyLDSHx0bk/CaYc8JDvUnvEC9HsUlxRJqKbBMRRzzrTdfxWXnrsfh/UeQnGIRx9433ngb8YkJGB+fEgMLkoluuOkabD//PDzy0D8QH2OEf9qB9ro2WPR+ZKUni+QVdwErdQmTEwTeW1hYBJfbhWeffxdhpiA+/dlPw+/2wKgpOGr3mIUck1m6G9TCYx6l8D6Ucwz53FI4JREoPT0Ly5atQWZ6LmKjExEeHom+/g4BspCOTC0EnzeAyOgYLC1bgg3rNiIiPBpDA8Ooqa7CwQNHUFNTKTUV3mueC/0W6CdYVJKPJWWMNowClKqtq0Z1VRVaW6gN2C+INwJRbJE2eGjBxiicMGB6/HlCcLpYK9AjNy8LixYtRFNTB8wmu9C7pyZHsOGcDTh/6wX49Q9+jJ9873vo7+nBXT/+EQxG8ykTVa6dxT6/D3fe+QMsLluC++//M3bv2AGjNXwOs+7EIbgLv0fSh7qaahw7eliKiCeDhRTdKxbf7vndfXj8b/fiO1+/eRY5qX2ZTGyNJknKqDyjEGz2MBGNZbGTkZA9zAKHuFcr4CKyQpkCyXibM0a18UxaeNnilXj9jZfl+2HhYRgdG8HmTVugC9mE4NbUXIeeXsrms+6kvMea1RuFG0M6dFNjg3yPReG58+k/PcT5Uwg9zPm5ISnWbieJgc6N9eVz1Q1r7uKtiRNqj4dDfVYkRBEgVxVQIkTkYGRk5JST0VZOOpcSGJSZkYMjR45i5eq1eOuN11QJsY9fCNQqugQQcbAfPXJ01pKZE6yltRafumQzfvTju/Hlb34Vl1y0He++twNbtm4VMww+mF07K3HRhRsw2D+Mxx56CuMDbXj3vZdlBef7LC5bhbMvvAoRMckYGhwUajR1/tiTf+j+3+DRP98ng85gUHPMk26aAT6PG88+8wxuve02tXp60h0BDfeoTBsK6JGUmo346ESxTu8f7IXdYoNRXy/px4qlmRiZgCKJ1diLqKhwRIRHIT2tUAQ5mWrTW49uvFwsHI5JSYm4q3InN1vDJV9PTY1Cfl6BSlH2SxFQIMGCi1fOSZGzn+MerY5FMgsZ2pLkQnaY3+WGXcTsA3APT8OaEIYrLrsA+UW5ePRfT6C+pgolCxdLj19y0ZOeG4leNtz+jf/Fvb//PV5+5WVxul27foOE/Vq+rY0+pjEhvwe3fuFLuOfuXwn3IisnF0GvUxXzVK25vW6kZWXj21//EjZtPeek0F/GnsslNGdGcnSv4u8y/SJPoqamSsg/xHxQX3JkbESMaNkWZD1AG+rzx9+5512E1rYujI72K5gGhKR4uaBkCRzTXECMOHp8H9xuRQyEv0ez2pzcIkHI0qGrovKY4kocHYP/5nHCGESVCZJwX6v7qaAuOTQNYQ3qeoY3PKleOKcowFVM2+XLlpbNqtGe8vtihRzEgQP7kJ2di5aWVunHchX+d3uf1dXVgjWgsIam0aZ5tnm9Lvzgjv/Bjdd+FvYwI845ZxN2734PsbFRAgKh6OfuXbXYcvY2HD6yCx2ddXj0Hw/jg3ffxQN/+TOsJj++942bcXDve1i9ZqkAZTwzo3jrtWdw/RWXIz4hRuDO8xdsuRdGC95+8w1RpDGHRaq724kXatfb29crIilyf/R6CVHzMwuYSYoXwr/+di9uv/374myTnhaLgrwiwVIQxnz48AE0Nbagrq5e2n8EKrndXuk3r1q9HEuXLkN8fLLwDMiEI1GGaZEIiFqMInnFVqWy+PL7iuS3rGWq1v3cZ6dcgk6w/hQGaacEWFsrrLFRCDhcSE3Lkg7Ifffeh9defhljI8PQ07J6XltLqLpeL1LTM3H99Z8Rk9NXX34F5cePnWZ3V3dE2nlbTPjSV7+Gp598EhNj1Im0nPTeUg9wTWPjlrPxs5/8n4jMaAuE9j4EpZEXwrSBb0uuPmG/RIbOOKaExk3oOJGek5PDSElOUIqTs4P+xO4fGRWJ5cvW4YWXnlGuy2gUPcRzz7kQI2SqsvLfXIumllqMjxJ/oeBHiosXCSCOKNnRsUEMD3ULJkFpOf4XU4ATV64UpVUmr9IWnLsAzHndRx1a+iBhgCZVOufYuGmT8rozoPn4Bp2dLQKFTE1Nw7FjR7Fm7fozLhofeZF65uQDorAiSqyafLIqb85o4NUXHsP2DWvR1d2IjZvX4NDhvXIfiNCKjo2W1dvjc6K7swMlJaVyY0oXLsRvf3svnnzicfz9Tz/DM/96BBdfsEUsulvqDuPT116GoN8jXZD5E5s0Z6/Hib888CDOPv980U049do0zr4LyUkJqr4/dywvLGFWEQulfPRN112G9WX5eOXJv6C+rh4JiWbh9y9dWoZlS9cgOSkdaanpiI+PF+0B6hTy2ukcxIIX6xKkK4un4uiYLLptLW3o6ujCYG+/AJxYnO3p6kB7Szv6e4dEfCDCrvAONA6BMtGURYGkqbAoK5ZvOhuvv/8OxkYGxG7eCDPy0nPFCfea6z+DB/70R3EyYhh68ngIiX1bwOdFWdlSbNywXoQ4//mPR6XYaaBRxvxFgDUen0eAPp+65lo8+MD9EuGISvWc9+YiwHrAd777XSxbWjYbEZ60CIyNisALi30swk1Ojkv9h6+lXHhVVaWI6DCSovVZYqLKdJ1tQSoLwIUXXoa62gaMj/dK2uXzeFFauhApKbliZBsTbcee/e+iu6td6kGK8UkkSkvLJMVg3am5uVbeMy01TWTetU3sv3GcMpsotMhFQHzleCGnJPMfDhAQNKFqRXTKh6nfXL5s2SxP+nQXQi15FsMOHtwtBo/VVcdlIaDn/SeJArQb5fP6EBsXK/Di2ZOc8xplABjR3VaHS7duxI43nkdhQRZa2luUfndGmigLJSRmKdJiNMk0GKU7wvem2+v+/fvx4B9/jbamRgFv3PiZT6uAec2hcc5nkgZttOKxfz6CpORkpKRnIehThFJOul8MF31uGcQMM5mc6YwhpWiIoACpGM4Tu71l63asX1WGu3/6Azzz9EsY6B8Q3D899mxhVpn0ZFxGx0ZJJZsPSIOUUvePbbjO9i5pcUZHRiIlOUlw+inpKYiJj5NIKDsnW/50OR1oa2vBsWPVGB4cEhUooj6tVjLsuPsrPvY8zYSYREy5/egf6Ma0eCn2wx0glNiMzOwcnH3+hfjj738Hnd40pyeiPiSxyaLXgBvnXXAx1qxdjfzCfPzx97/H+OiIGHPMHw8SOXhmkJOXj+3bz8aDDzwAvcl6khKVopkQgMliw+/v/Z2E1vN5JY2NjbLz08yGOAyOD1KBWXth25o1n/b2Jvm70WAS8xLtvJW+fwjxCYlYVLoCb7z50qwGIJ/phRdeioG+IZQU5+FY1VFUVpQrz1F9/kStkoPgcrsRDPpRWVUu36cTEY8z6Qv8O8dpt1OR+eJGqe3eH2POzSIJVZmw061P2gDPzsyW1t/8h6f9nC0TAmpGh/sFjcCiYUVFOTZt3vKJowC+nqAdSllRY18TZpx/sBgl7xv04Xtf/yoe/uNvYTXrUV5VJ9cSERmLO75/J2LiU3Fg/0G5NyHy/w2ULvdLcea1l1/CTddfDn1wBlvP2oqAf27L78T56IxmTE2M4Omnn8AXbvu8evGnLkwwmKWuMDQ4DL2Zg9gt3gB0DSIG3hwRiaTEJFGw7evuQknJImzasB6TIyOoOn4E//jH31BRUQ03bbttitml36eIkJ6QpuOgNKO4pBBLlpZJ2tPd3YWW1la0tDajp7tXDC/p0kTJbqPRhqKSXJSUFCIlLUOEQAjTPnzwEI4erkRLUwfGh0fFZqyzfRg5mcloae2EISwClrgIRCcnYvHq1VI4C3gCWLl6NUoWLMbDf/ur9PFPCu0lGgxKBMVK/1lbt6OkuBhZ2Tn485/+qCzCp3mWjGhYJ1i+ag1y8/Lw/NNPwmC2n/TeSvdgBhs2b8FZZ50l42Ku8i+x/hq3hLgI1pH455IlS+V7vHNkEw4NjUhUSRLY7B1Vw/9LLv4UqmvqMDk5qNjZ+f1Yu3Yd7PYYIZ7RPXnnzh3CcWEXgb9Dk1CG/2E2q4xbQuNHhhRFbeoezo6NT3BoBU5ePzkLxNwQpEbuwhln0uwioIXxH0NggGCgDwtMtKIIjTiXrVDae3MniHZh5D/HxsYgPj4O+w8cwIqVK1FbXYGE+ERBQn2SEEijp1KAgVp8Wth/uuNEl8CIJx57GL/5yTfR216H2mrSMkOIiLLixpu+iBdffV15B+a7fkY8RjGvLKWeQE4GbrzuU0Jemm+erFxjUEAmTz/1uLSQWOAM+d0nFcFO3As9WttaZfeee3/k7+olmK0WYbSxBcWBmJGSBJ/PiWWr1mDtug2ihETyzOGDRwXQQ2usuaaU2uF2hUS+mwKoiSnc5V0icmkV4FIIjqlpDA/3o7OjFTWV1AbshcmoR0pKGkoXL8SKFauQm5Mr1loUNx1j25KiJ40tKF64Evf+4Y8wUySVkyxAQwKtjx/AhZdcLC5Ob7z28mnzex4iSOpzSfqYmZUhz/CpJ/51ysSefb0sAk6cf+HForR7+MBeJW2YQx4KqTUMIkK1sTL3/hONSXwBIza6PHHiEA0aGxOHIRF5tcLn9kgqMj4+rL6HEtmmZ2Rj0aIyvPHmi7PjnuH7eedejNqaZmSkp6OpidiJ1pPOOze3SByYaHZLSb6qqmOzP4uPjf1Yk39WE2JubYZdIp9P8WHUUenZIOPlQ7dSLgJsE0qNZC40XC0azh1EXKVniQUfcmjhy7Zt22ZPbv7ByixvcFFhkRBXqPZrC7PhwP792LBx8ydeAPh6qq5s3nSW5LxC9jjDrysrJav2RpQfOYh7f/4d7N35BlpbO0Cy17kXXIa6xmZM9k+I5iHfiwg4g8mAR/76EC655DIUFi9EMOg6yQNBu3aGo309rXjllVdwww03CtCHSi9yzNFk0B4gueZ5BQUn3ysRX1Demx71CmEkFuPjk1hQVCjV8P7BYURERwvteWHpAhE8+esDf4ff5xL57/mREHNWgm3Y5qNV1ZKyJZJC9PT1wDEzJUpI6TTDTEgQk1SGx4wImlsaUVFBYlCHWIK5XG5MjE/D5fOJdDqji63nn43RaS9eeuElmbDKJAypqj4BBDwufPbmm7Fn7z7UVZHqy9ecmnuKe5LJhPUbNiA7Nxfl5ZWorapQX3+GRcPvwQ033YKjR4+ir6tDCq/zJ4f+Q8YSIwEuMBoGobKqQui/XBQY4lusNikIjo0rvpcaFPnmm27D7t07MTmh7P68nvMvvAg+nx4O57RM8tbWJkxO8vcUkVBChfNy82WiE0rNyKCpsXZ2kzx05MhJbk4ftenxYDeLXY0+Eox6+sSSbmBwQOYX08QPfSeZ51SaZbtaneCzyD92qPgzoX0qC8XHObQTW7506RnzGWoEUh2YyCc62ZCrvWLFCrS2NooYKUPAT4oO7OnpRU52sbjjKtf24b8rKQEhxN3dePrRP+CBP9yN6spGFBTGIS07H0cryxEyKwAjHc04R8fFMefm224VsJGCsJp/8Urr7+23diA2NhEbN2wEQsrnzP5ce6m6mhIjkJOdpeT/tHURoIWSw/JgpZofxNYU+Q1TDgcs+iD6e2iN7YfeZERzU5uE9+npGXj/vZ2YHBsVe22BAc9ZbMjt5y310gDabMWixQtQXFSAqakx4dA3NjVKFMAqOT+L9z8ulmKrsXK9xMtT4NLrdcLrmoabUtleH0ZGp3DbV7+OB/7+mOg5iLS8FAyZ9zIi4ucHcMvNN+Nb3/mutMJCojc3P0XUI+h1IzklDenpKSKI8uTjT8hniBXu/BvOicA6glGHa66+Rsw+tfB97v328vc/5sE+vd/vkd9hekZ2IFt11IhUxk0Al1x8DZIT0/HGGy/JZKUoTEpqGjZvPE9EZgsL8qWo2NJSJ6K3bDHyWLiQ3TFljFBgpqa2QtCx2oR++623RMiGrz9d1KM9S07uluYWYYpy4SPpjUVwj8+jYB2MJgEvRcVEfQwJEPUVfEYk+7AoIyepSoaJeccpuN8PeTt19eIOk5iUeMpE1lp03Nm4gw4OD6G7s1MgnBFREdi7Z5d0BBjGfJwFYHYnHRiC2WAT8Ubtcz7qYGjH1/X19GDn2y/iW1//Aurqu3HOBZfh9bcUu+gAQ32DDn/924O48uprBKpL1RaZrKfs/jY01Vdj1869WLtmI6LjEqUvrUlwKSestf9NcEyPYXBoRPLCUNArenCzFEz1/QUuumABcgoLkJqcJEWp9OREHNy7S0w4h0bGJcQ9fqxSXIBpuRYI6LF/72ERb6GnIdt6fDenY0Yp4DKuCZD+G4TJEo5FS5Zg4aIyYcGZjBZY6IZDERCjXhyAteo/j+i4GOTm5aOoaAGWrViJFSuXoyAvDwsXl2DcEcCf/nS/VOFFE0IsiDh3KYftQn5RCVatWoWf/N8PYTApPoenjB/uppIKbCFVSfrlu3Z+IO2+uUSeE+NJLzTiuKRkpGdlYWJ4EDpGgXOiyMmJiZPGypkOvhfTUM3hhx0YXjsXPi1CyMjIxVWX3Yinn3lWGH3KZ4Tw2c9+Ae1tw/KcqQDd0dmMvr6OORqBabIAsMtDBWy2/2prlOKfhmIlf+COO+6YLTSe9hxDOkTaI0V9a0HJAmw/e7tA2levWY2SkhLk5OTIvGN7kunNx9u3tdBU3fFF6181GPykmgTaBCegYes2xSeNPdZTlH2czlk5cMoxNdTXY/NZm4VxR30ADkrtxnycY5BijgYdEtR2zcc9tOLJzNQI6sv34Bv/8yWUFCwQhaHpsSmYrCb0dPWgf3AQ511wgYT+BvZTTzeWQsChg0dgDzNID167H7PplDp+KdBOSlVjbZ3cA7afJMSde6/Vv9M8JSomRt6bijJLC0uQEh8JS8iFN15+XcxKRienRHeuva1dpMwokUYXmrq6WkHlHdh3CIOD3aipKcc777yN2uoqtLe2oLO9DZNj4/B4Q9LCyy/IQVpmFiKiY8X9h+duMRllAkxNOeD2eDAxPonBvl4BxigLKMSWa/euw/jmt76NPz3wV7Q0NUFvYlgcOHliBz349ne+g7ffeU+8CaR6f5pFgKkAI45LLrscMy6yH2sk7ZlfR5l9vWj7BaUITAqxBrjSqT/nWPuwg2MsNSVV8BqUpp+7UDBPZ5uW789Qv6AgHwcO78Seve/MioqSCk1Q2/HK/dLFoABMU0udRLmKnZgOq9euk1SC58gIqLm5TiDIWgqrtSpfefklvPjSyycJlsrQogDNjEcWQWu4VWTDpBUZVKJU7Wu+ZuTHL6droB6V4UeI8L8jQywnq57Eli1bT7z33I8SQIlfilB2mx2xMTFoamwW1h5hlfv27EJRcalIhX9UPWAWXUicthiF/HtsKkX+CTi4730E/G7kFhbjxRdflJv8xBOP45orr1Z7yUElTJ/7u1yozDY01FajrrYWySmpEpIT6quwsbSTPfmcKWDCnvHpDi2FYQ3CbFWKa0FfAAnJychPz0KEEWhuKEdNbb3s+jMeNyKjo4RrQfPS8ckpFBUVS2E0MsIulWaGhWlpKRJi+nweYaRNOyZnAWFcCFhCpRoxOzP28CiYrHZx/ElOSRZ0JBcEs80qwK3oKJPYpz/5zFMoXVCINWtWY/PGzfj6N76mDM45yEhloLNuEIlvfut23HnnHeLBcLpdWVIBn1tawxecdw6qqislSmN35Uy7uLazhkVGSQVV9i6dsqtNqnoTZ3z2YlXvkOImIdvK+FV+xvFJODbfi9EORVH+9fhfmVgoO3tqBq687BZ8sPMDpCTHw2614Hj5IbS2NCqTOBhAfkEhCgtLMDU1iYS4BNFRqKg+cobr0OFXv/qVAq4V8RL1+3oSjOjITQRnAD6PXyzReKV8nfY1Wx9Qx9y/gapRJqykLf8mFkE7kc0bN4k8Fyf7/Aosj7GRMembZmZkiHR4xXFFi43+aMxt1q3b8LEKgtqD7uvrlcrt3M/QzuejD+X1Prcb37z9VpStWIbqhhr0dfdKDrpm/VoE/E7o53JRT/rdEA4fOSzFQgo8GCRkZQ//RDdhrgArF5KKigqULVmkgGK0sv/sNfH/QYQCZDHSgkwHndkoC0JKViHWrVyD9PQkeP0+dHZ3w+vxY9Ixg/DIcAwO9Ijf4eCwIpBKhR72t8PCohAXlyj9Z3ovTJAZODamoBa14pLkufRy8GJibAQ9nR1ob+9Ad08vxsfH4fH7ER0Th7S0SBw+XIW6ujqctXmd3He+39XXfRa79+zGjvffg4FtP8p/qTVBqdwHnLju0zdICvjSC89J5X4+X0B5Zjqp8q/dsBlR0ZGoa2yUwXn6HrkmXc1dS1lweZ90BGM5HbP4kA9LAZjnc7JGRkTOuf+087Kit7dfnJFp4trT3YXpaapVKxPu87d8U5iRTif5JQV4+ul/4eD+XSpxSIFZb96yHYP940iMJ9AnDDt3vQPH5OTs7j+72KsbJ6HgisWbas6hjQku3B6f6FOIAalU5j98VH+iBYD30ONSbK5o/fxx8AGn/VBVbpk70DJVJ3D+JNRyo76+fskRszKzZGXjDs7Ka1tLg4Q7zGk+qiCo/ayto1WEMORa5iwczIUKCwtnz+FM7yU7uU6PA3t34t7f/hpubwD79uzF1Vdfo+TtFFOcN/4Et2CyobOlCa0tbXLO5557rgJyOU3IKq83WzA2xErtMHKIyxer67nnpPSnQz6vhK9UqyH2XoAxZgP0YWFC22WBcOO6JapOoksGTltXj+yy3V2dEtVQ06B/sF9cegf7O9HZ3orCwnxs3LgZK1asRUnRImXHPYk0wyKhWT5j8ZJFWL6iTMhEi8vYDlyA6Ohw7Nx5BMMjY7KAkc9uNlkF2blw0QoUFJbif2+/XbnXIl+sElBoJivheQjf//738fNf/0r08NnvP93klKJ0KIhvf+e7aG5q/pBnJ0AC1dRwzs/1esE9sMWp3fvTjR1GDlycxP1H0/5XxxzbpTs/eE9ydprOKIxCpmxBXH7ZVUhPz0RTKx2GdHjuxWfR2lqviKtIuzAkTkgspE5OTIvuZHVNJTrblehAOx9NokwpYALFxcWKXqdGoFO7QUzBuGEy5eFGw79/1PGJFgCeuIYPN7Ja9B+gEbX8Zcu2rSdVvbVDm6BkDdJ/jjlsXn6eDGLNHXX/vr1iQMHVV3mTD/9MVrBlt5zzGTycLieMZiNKFpRIRPJhUQVzck7cpppKvPjcM2KeWlBcgKBXW4ROHkRK7q7H3n0H5J4xBEzNzDlR/Jt3KCaUBlRVVwsSj8UwVoJPuTiDQSrPDF/psKRpFugIdaaoqd2C8clx0WBcuXyZ+B2sWrkceSIlbsX6DZsQxd0spBMfBbpDl5YuFTAR6w+kGYeHmcWBl/JiVAviF5F+IjDKJoeK/uJH0gaR/o511U14+613RAR1/fo1wo2g1gMHNK2zdGYbvvTVb6KqqhqPPvKIAv4RR2plAaANOXf2des3icX4Qw89KKjJ02nuS27v9yItTdEnOLx/D/TSZjwDUm52EdCevVFEOfn1YQe1+Gml7vV64KYhowZ8k4hIj5Url8pk0yJbRrRLl67FNdd8Du/ufAFHy3fj9defQ2Pd8VmIsDb2Nm26AEMDM7DardLJYedglmSl6gakpKdL+1obW+efd546FpWlgeI9k14vHP4AnNDBzQLlx9ycP1kKoNqF/zdwyNp7MCfkwd1o/qHdJLYw6F5LA0sywoqKCoWnzdyr/Hg5lixdpuycp/Fnn/s+4ySH6Ayi9DL3HBQrbCVHLCgoUKDIH7IIaEQiknTy8/OVB6rjY5ivoUw8uxlu5xQOHDogC+jq1as/9L5oyLYPPtiJFStPr4ikTDyjCGO63F7oDDSQ1HIJ5QiLihT8hGL0TEGKCNGaz85OR0FBNnzBkMiDsfiUmVWIVWtWwmKPlHCaZrDHjh9DVU0dBkcI6OlCc1M7mlu60NrSie7OfsEcTJEa3d6Dyqo6VJRX4tiRQxgZHce552wXfYTW1nYRi41LSITRrJe81DEzg4ycIixfuRJ3fP/7mJ4Yl3t0Coc+6MdPf3IXnnjqKUxPTaqEofltPkb1rOgHpdpNwhAjq1mPi9N90b5rlhykk3Ybd8654+TE22vS84pYCQuIc1/DhSYpKVFQdQy7tUIbC4E3f+5zePa5x/DCs0+hralFinEaRFgL7ePiE5CUmIKJsXHEREfgzbdfm9UL0Op0y1euEC4Ex6xC4zZi48YN8vkGlaAlhinQIdxuh406k+yQ/T+yAPwXD40LsG3LVuQV5p6xos+b4fF6hdlGXbSlq1bCEhaGjZsVQtFAf4/oCtIp+KNSARo/0hSSrbq5B3+PrSBaVxOExEp5YqICVT6Tygt/JzoqWtBvAs6gd9Y86B/DfaL+9uzeI7s41+v1GzfKTkRa6KlHSFpkoYAPlVVVWLmKnYJT74s2KLkAsOg293vaIuANBtDT2wuHgw7EdK5lBZg9b3oAKtfl8oQQCLHCz78zstFJW+/Y8aOYmBwTk0zKjQ0PD6Crsx3jw0OoqawUR6Pdu97Hnl0fYHJsCKODgxjoH8ay5ctFe6G+oUXoyMyJyT40mZWFiCkJOQMBvQnXf+5LmHBM4/4HH5B7NLeiLWPD70FefpHsdhUVx+U1J01QjWymY5/cg7AwCxaXLQN0XjEanZWlOu2XFlRD/Ay1zzz1aSivmpoiTdcriMKTxgHIHBzCwUP75N9Z2dlYv2G9IFf/8qff4tlnHz+pvqWdv/bvJYuXoq+3EwaTF2MT/Tiw/93Z0J+L2oazNktbka+mWxaP9Mws5OTmyv10+3SY8dKRm/qX/DcUNqZy8v/fXgC0CV9RXQt/8Mwi5LO798SEQFpZUElITEJBXgmKShbIzxob6sRQgRp6Z2QYqp0As9WEGM1ffQ5SkPTZ6Um2c1g1NcrDJHz4dIvAidqBXT7zTG6c2ndee/VVFBYVIikxFWGRMdLPPd3rRfTEaEF9XRWsFjPS0ighdmqqMBsZEcmlLiSz+aJ6+aLQOzYsOv8sclnMysCaj7lQdjmtLiMxC/LyC5GTk4+BwUEsLVsqrakFC0qFwbd1+2aYrTRvWYd161YL7Xfd+rU4a/N6lJdXKbwDqxGVlVUierl27SqRi6faFD0BTAbFlTe3oAS28Ei8t+N9tR4yz6NGdrcgPvvZ60WEY+41nqxHrdxpvjZErsTHzEtD6mLd29urft4nj2q5WbAtPTWt4ggQFNWfJ594UuTCtEVl/pjUoNjUBODGwILgm2++ql6JMh43n7UNKUlpogRMToD2HuvWrpUIzeH0w0uphRBhvSaYTFbFIVq9NfxTuqzB/w8uALwY5tF19bV49sV3cecP7lErtR++bDHUqjx8THYVl0eHrVsukAnIxaS5qRExsQkfWtH3qTebsNn5kMlQ0ICN68+GY9oDp4shlVFaW4TFnikSMJpN4qar8nTnX6UU88aH+zExNQmLyYqyxXND+lOvVenT6vDqq69h8eLFggj7sOfX292j5P+nOZiz0qDU4/WIS3JXd58AfhiCagjA+QfPiKxAevPRx5G95OaWFqQmJQo1lag3ogozM5LFnm2gfwhp6Sxc1UoXoDg/Cwf2H8Gbb74trs1lSxfDH9KJ0xJrJzwncfYlqMxkkLZjQ3MbWpsaoaOg55zcXXk2lFOPQVtHhxQ5FcScrCTK11yHKXn9Jx/Ovb29MvnPhCH4sINjbXJqYtYQhPTpsdGx2Z+dFsOgbji0GGftiWa3u/fswNTEpMKNCAZQWLQIZ599Cbp7+qXtTUUn7WDEzKt2c6FgOq7yQsSwm+bJrHeq6QMXCHo2hrQO02nkvv9fXwC0AgjDwwcffBhXXftpnH/xxcjIzle0/D9kAms46D073sXE2CBMBguuuIK0W1p0u0Xp9WSlmJN/lwdhkhER0SedDw+q+PJrzZq10makFJPdFiHW1BnUyZ+zCGi/Q2NTipuenjJ54rXXf/p6hIXZkE/DD47fM/RQiSjkR+zZc1CMPeR7ujOHpmOj44IbV67v5NdQrstutSPgD8FotsEX8KG7m465OowMj6ttvZOxB6wz9fQMSM5N0kvIYJQFtqO7V+zPOImzsjLR2T2A5JRE6T6QEkyeAV2RXn7tDWTnZuPGG2/A2rUrYbZSxIXkKj1GhkckImAdgoUz1nyS4pPR39uL4xUVyi5+0oRRRiwLzsSxk3EpHQMeBrNqZvkfHOr96u3tk3FHltwnPTgWmSopXZmTN5SPouxyDLB+X1l1FNPT47IAaWnQlVd+SmC8zOMZLY6P8TOIvDRh1ep1MrHJc5BjjuaGxvGaOxY493llngDgCTI1PHmk6v//Ffo/+NeHsHbdJUjNTEXIDGzeco78/KNAPfzie7z95ouoqjqCzMwCbDpruxBPqO6jOMqcemjv65ieQdIcNKA2sccnhuBwTonW3eKFy+Rmk+pJaybqtZOLPT+Uo+4f9fKUNzrNhwZ8iE1IwDnnnC2twtj/X3nvAV/nWZ6NX0dn6CztvaxtW7Ik773thNhkkoRAEggQVlMopZRd+NMBhf4+vra08LWUlFkCgZBNlhM78Z7ykjWtvaVzjo509v7/rvs9r3SkyLacmNH24XeQI531jud+7ue+r5GTKeF5Ph6C0i5Mhm10BI7xKWU/S9jNPAYQ0wg2n0+2KbN/q4xQIILh4WHxJpyw2VCYXyhcf8fEFKKIoLm1Tar3ifOI/X1KthF/QFHKZF0Mbn8AyUYDxsZHYLON4fjx44iEAjh7rgndPf3Iy8mWfvqa1avwwQ99CCtXN8BkSRGdQGYbrCswOBPkwh437c7cLh9MyamoLKtGOBhED1f4hMCWeDw8V3TnnUGdvf0CtPq+HINDw/jMn/8Z1tCKbcGYkJkxPDKYsA2ZzcKbb6h/ZzBsaWvGhIOLliJLRn2Hd91zrygAkwRWsqgIZ8+qgKAYliytQWVVFXys4ajEnLgIJ6kh1O4Qe/f4fwtcP/5vZgF8qNCTP0gAUMwktThx4hQcU8Att26HczKK8XEP9tx+rwgzXInkMHfC+gN+HDr8Gp595tfYvGkH7rjzXVITUD/jSkO01TOz3vSebKk999yv4fd7YNAbUFZaJWpApCbHIhpRyS0pISlnZhAdxlabvMc8EUAldlAy2+2ajEfp+e2jlDaXDi+//DKKFuUL0o6qNfOntcrNy7090XCzA2e8ten1opgKwYODyMpJQUtbC5bW1KKtvRMFBdnyvAtnLiLkDSjoTq1G9oxWswU7tm9B/9AIevsH4Pe5MTA8LD18bh0Y9Pr6BkRTn4XY2vp6mfRpmeniLuT1shLOTE7pTzPjJyXZmmKFQadFS0szjEZur4qwpLZGJmJLS9sMJmDWIcZEQv7Tf/4ppe0sS9xsSfW3NLgFJc89EkJ3dxfuuGUXfvrd/wNDXDh2oUGARV7C0t/KtyF4jPLiKqTXkpKK226/G+VlNWht6xIsBbkGg8PDMMbFRvbsuQUmoxZhIvxiihW96swtwj3xCS+wCrVMMrdcIvPwdxQArhb9pgtxXhd++l//hfsf+jDG3VF4Q16x01q1fi2Wr1wjN/LVJnDie/FiXbp0BvtfewW52cW48467pGWitulmv0b5Sb8BFq7e9H2FoxDGM88+jtGxAVmtiorKkJpKmi2RV8SDF6GgUNkOqCwvuhpd6dhlUkZCKCkpETkuBdAzP0lA2G8ADh8+jHUbVivXbT57b9YWFJkcOCccKChU7LRnWprKT+LJdVoDVtZVoP1yG1LTrOjsakNtzVKcv0CI8VJM2SYw0N0jktPEp3OCsxvLesiubRtQXUVwlB5rVq+GyZKBiqoa3P/gveLll5qeJao+0agGbndUthwzVFXlOzAIuKYCophrMiSjsZHKNyEsXrJUsg2aoOp1BnR398LrdQsyMJZkSEjxVdq2uswl1lreeiYgE0erw6TDIUInySYrapYvx6YNa6XfrgaBa7W76c9Auq3yptcXApgBqDh9moxS7IbGoD3do0hLzYTZapJuDJGnala7YuVapSYU1YgNXyASRTASlX/zEYrE3Z8jM3WAuQ/Z9SXUAZI0N0pcTG2PRaKy+nAPzaKbOpSTqsGPf/Rj1K/dCkuuRdxzfb6AOPLk5Fpx9wMfn0VUWGgx8cSJgzh/7oxINj38kU+ivmFt/PMSA4na0pmc1uafNWkTWjSv7X8JHe0XRZkoKzMbJhOloRwi6MgAQMVfteVIrnXCy+f7koJZ4GunkTPzDEWNKIIPP/ww7rv33dDEwldACjKp0GGKmUeSBtmUCouqoqPEhiugKBf38ABSLTqZyL29fQgH/ZicdCA9LU1UeupXNWBq0oGpSaf48A0NUzKcxTYSZKLIzc3B+rUrYEy2YN3aenHHcU2x3RmvbsdhD/NNFmYB5N0MDvShoCAPI6NjYmOuZaEhFhG9vRgiCIYDOH3mNIZ5HgXVxl1rnPhExqOqNS+fJUqkcrySrqh/n+8RmfMIzTxiYZ5EMvJ6xdnHNjqG4e4e3H3Hrdi5YxdWLl89TZy59pb0rclzqe2+Des2447b7kVWRi7Gx8ZhsRjFwZrErYG+PoUIxNkdN0LhpxGKxI0nF40IkpTDYgAIU1dgRqFbdfhilSqoAUKkesdfK0J1mrdSNp118GrrKSYVenKQqaXG1oWCU6Y9tuIoxAMeHxvFubZubNl7F+xuXmqufFpRue3vd6F+1XrkFpZIJXShcUlVGj546FVcaj6Pzo4+fPKRr6JuGYPAmzMBh8Mm6a7sR2W8ucXG15w5exKnzxxBVlaG4L8DgagU3eihl59XIJoEfG57R7v6wqucrGvBpilKoYBU1mzYKHLWXOGveA7EcTiIhoblQsaZFhRJOBZaihHi7I1CYNTU+KMx6NDQgJB5GKQDsYg4L/mck7IXp8U46wQur1eyDHYrfX4lIHs88dRe+OBX7HxOnwp2ExwON4Jhn8haU1uPUtsUC6EqsMVsxq9//QTy8wolK1DbcYQ3y0aVqUhi1Xq6A8j8NqzksVea/ImvSdgnTycSDAAAOjsuCzbB53HJ1mTNihr0dPXi/nc/jI996BPSRbke8ZmFDrVDsHRpLe6+5/3o6xuTYimDSUVFsWxrDx/cPx1c1Hv4/Plzcjx+UpDfdE8o8XF6A52wjqq1XjUuEiXIYqC89/V8cbUAJ2+aUPFUUz/yjMsryqW9wxSHMF72MElQ4HN+8YtfYPOu22A16aAJRcQenMoodGnt7OxCcdEi7L3twVkHfT3f68ypRjz++A/RP9iB973/Yaxes27Wnk7ALz6f4LkZdJTfvfn91Ne0t7fg8JHXkJOXjtQUiyDfbDa7nMyiwhJkZWWj47KCQZ9OWecdC72BNKJjT3jrlV6jBNaoCKRS5JOVndlEKuXnpNcjKkrkg3i8AdQurYHBYBYwVWtbM8rLitE/MABzRhpSLBY4R23o6xtCeWlOXMRjQop3EmDp9ygccBUZd41DisVEELSrs1OuKYt8Bfm5Ul/xB9zQW/Q4dOhVBNxOrFzeAH8wKMF14edrgUw0dfLPw8/guHy5GyaTEatW1ou0Gglnfr8brx46gKr6lfirr/z9tBzbjQoCol4ejUKvN+P+934QFy81KY5MKSlITcvE66+/jkuXGuP27TNbXY7XXnoRdncQ2iQ6CwTFX0A8ZonK1wHBZCCUsHOa9Y3Js4jXCJhkkYPFkbSQVV5N5QWVFwig+VIzDh06hKHBITH0pHEkNceC/qC0e4icYoGMRT32kklMcNjt6OofxuZt2+F3KxVihWYalVWZk4kp2frNN8FoUWSr552dV/2+LDj148c/+Se4PS7cd99DqG9YmZDOKc+bcNpF5Vc9pvmGGgQokPnqay8jJzcTFrNB9stj49R/i6Kqeomw3wRK/HbbUvGxkL0nV0eiGdmZiIaCb+JRcDgmHDBR/VeYn1oRA123dr1g/lnQ6+65LO7MLZc7kV2YBy3x4/4gmi50oTA3jaEIE1OT0hIVvLzTFcf8x4PAFWIdswSTWYPW1m7BW1AijJkYuwE0HkmxZGDEZsdjP/8RvvQXn4ZBS4UanfgfcrxF64d5vshshuV8o69/UPwOK6rKkV9QAJPBAIvFgKGxERw8ehCW9Gy8Y89dNywLUCG+DcuX46H3fUQMcKm0tHRplVyvl1/+Ld5444V4hjAbcszXnj9/Fhebm2AyG8RrIhgJwOVxizU7laB8gcB0rBNQUDThEVFWfa7+/LcqxTDrrn0TFjq+ytNrjrROPrhPKVlUIuw5YvQpWklEGgEnBG3QM48BgzcoMfWMrERMPfXkM1i2fJvAUKfcHhHN5JyhvHNGajp8/gDCkSDKF1fh5jveq+zv52EIXm2o6ftA/wAe+/l/wO3y4j3vfh+2btmN4pJKmC1KNFfsyK+tC6CeeAav1994TRxcQmEfhgapmtshfXAGiICH6r9krOH3Nngmtm3bNnNO6Dwk4V25BVwuN0zmNJG0ZqWd95Neq8WObdtl+0BTU/rQ0W+gs78PGTnZ4t/I1L2pqRPZGWno7e0RRSZW7QNBLzq7eoThRy+AxBVKHfzvZIMGY6NOwRIwa3JNTiLZkCyZXigcRH5xPn7+n99FfWU+li4uQ2FeHupqlqA73gq84Sdp3t8r52hwqF8cqs83NWFobExEOzPTMwT2bB8bRU9fDzZu2SGGr2/XoFYFAKVnpGHN6nUStAdHhpCWnoyBwW48/qufoKuzaRpmPncQJxCORvDKs0/DRCAX3Zp0SQhGQghEAuLxSIs/3oRhzUyaP/3gQh6/N2RaxaeW/GAxYG6U4+QWIcWhIZSWlYrsFN1SmQFwglEGmT1o7i9JoqFBJuWpiTojjp4uPCJCwKzB70dTcztWrt2G4WGnfKjfHUAkEBIKJK8UnXmJg/f5PXjH3rsFRKJGQRIxyGpbSCRWV27q1v/nD78HtyeAHdv24s473i3+bPwsgjeIxFrIUD+Tx9Dc0ixpMT+DenDUCySZhEKLbOHN1wr8XY5kqr4kCoUqEhsyaL0Wi+kUn734MYTDtO5Kxs4duwX80j/QJ2q2rBU4fR6YDcnQ0xbboENf7wDysrLQ19eP7m5yCjzIZKsv4EFza7u0AznZJQbEH1zliYvo6upBQUEubDaHTCpeP2496Pbs97rw6vNP4jN/8jB6e7vEI481I6eAXW5cqj3/UK6PgGhiAYE6a5Ki8Did0g5kWy8vNwdelwOO8VGcPnkCmdk5WLN2w9v+bupra2vrkJqSKZ2RYNiPI4ffwHPPPBWnF1/Z+k4FSb30zNOYcnlFTXnK6ZLMm2CvCGc5SyP0quAqP4cDxfQ3CRpFekEDZJrUsnHCF+SefWpySt40Pz9fVnnumRsbGyUYEPJYvbhaaLOEyDK957tzwpeWl4r/nqz6LP9Km0Qp0O17bR9yCqrECJNVTtopcx9Ow0l+HlGBFEc0myxSTFy+ci3uffAjsj1QlUz27NkjvfHrCQIkWnz3e99CMOQVPcCbdu/B4up6TE25EAxfXQZq1slXi53R2DTsk9+BYosULL3cRWlnBWb7ex3zKOeqkFbbuB0uTxBtTcNIt1Jog9JiGiGOsNe/ddN2keLu7euG3T6KYCiIiI4aAxEYdUZhnVlMVlSVVSAY8sPhmJCJzVORl5stLb2e3n4Y9DPBh9X9C+ebxDnY7fEjNy9PFIfY/Zma8khKPdDbD20ogFSrFXpEMDRqh5c69UHWq+MAlxsx5r0UcTZeLAlRbwg9/f0IBKewefsm4YfwnJWWFItyMh38qNpDBd3tO25624YcfC1hvbQZ33/gVex//Vk8+9QvBb06gzC98vurn93S1oqOlnYBZxmSzDAbM2AxZ8BgsipEsgREPS32iN5kUZ3dFnXnliivMh0AWKxzT7mlWkuHW4IUWAAqLyvH2rVrZaUfGx8Tg01uCfhOItSZmiKpiNz8aksoSSP0XU5mjvON57Bi1UZh9TH8tLa2ifeeze6UdiF7wHRMZWGOvfjsrHTcdvcDMFkVyC6zDmYWO3fuEDlqNQhcLRCoQcDtmsL/+/634fLYMTnpxn3vfi8Q1WHCsTAhyCsNvowXlM7FNG6c9V5XoqLe8DFDZ6ZTiawiuiQM9g9hYGgEd911J46dbMSpUxeQmhZ3YqJqTJBY9BSsWrFKbvyJSSdCQR+c7ikh9vs9HhG/oN22KVmPVLNVKvjpaalwuwPCK8jJzZE9dGd3n3wLbh36eoeFo0/KNS3WBwb65HUTTpcsCnQqJpFoSXUV+kaGxfaK13Z0jN2JoSsc31s8cYnYd1VxSe0AaJPhtLtQXlKIr3zhs4gEoki1psI+YUdWmgK6YVY3YRtFd2crltQsR/XiGvn9W7Gmmz4ajUaMQKj1Ty4Ft8rq1mAhQ7YB4RAutzQJLd4b8GPSPQW70yE1BPvEFDxBPwLRqHAAgpEgApEgglEm/1FFWBZhRBKi4/TRpKamaliIIX2RxRtjslFEC1nlZhWXqzuVSGjsSVQXA0aitnxi5BZNMlFOiaGnuxvhYDIycgql19zVfVloplJcmnAIQYRQ0oKCTDkpZWXlGBwYQFZ2Hm675/3Te6+mi03IzMjFrh17sW7d1ukC5bWCAP9OIssvf/VDXGo5Kz50e/e8E0lJhukgQhGQ672QPLbM7Ezk5eWhu6c3/oeE8uvvJQAkjEi8cKQBjhw5BL3OhLyiNKzatBY9g0N4/cBJWKxKoOa1CvhjKCzIQW5uIaqrlwhuIC83F76ADwZLMsaHR1CQlwvnpBNZGVkIM8WUbo9GMj7WESR4OCfQ0XlZUv+ConxUlJdh8eIy9Pb2S0rr9wVhNJrF+sucrBN/hmX1DVixfDnWrl6DUMArbUAuJiGfW27yGWozr8ts0s8Vx5UwANHZaEvBYxh0eOI3j+Pbf/slVBWUIOqNIisjDeFQUAKfOtG9bidaWi4g2WDFtm2KCMfbyfHYeqUs3SwS2nUsQOprhvsGSERBMKx0AkSkx5gsamecv4FwSGjeyvkjWcwgMz2gjSGAEAotM5N1VjjjPp+VZYpGcn+/qHSRiHAQySaRKkImmU4uFtPxK1Ws2RVg9sB9e3tbJ1IzShDS6ETllEGAppZ9/f1SJyCUsrpyEQaGnFKosphSRFsuNycHD334TxT1WSL4bHacF8klHZZU1+LW2+9FspEUyKsTiNQgwX3m/gMv4mLzGaSlZeEdN++dVv+hqMe10Idz35M20aWLyiRAnhUyS1zdOHHlmfv4HQ81hbx0qUkcZsYcYaRYrVhaWyNAnAOvHoPRrPSbJQgEYigvLRUpcUp70fewuqoc3lAQmXk56OsbREFBMQaHB1FSlI+gLyBAHu6d6V2npO0Ec3nR2zsgWIbMrHQp9GZnZUp1X6vXyZYrIz0NIyPMHPJgn/JgcnwcaZlpqCwvjaPTopINzCb5sPs02zF55mDnrPLzYQASXkbn5SSzGSGE8Hdf+jyS9UFs2rwZIVcIem1YNBh43xO0xeH1uGBJsaCjrVXoyA11a5CTkzctBfZWh7riv5XMU/1UgsAmp9xI0mmQajUjxWIS+rhOZ4RRzzqOfg7hjL1HSq8R7TqHdp34H1zZmYqTAUa540S0Hf9bRAYXELV44mjGyddxD5VVXACX14FJpx1LqhejpaUVxUUluHSpBVUViuDB8NAQqqoWCUWS4pRpKWZYUzKxtGG1FKz4XufPnZaizYjDhqKqatz5ngem/f7muvAkjmnobiiExsaj6O5tRzSWJEqs/BspvQxG6gW61jAYjNiwfifCAaBuWZ20BgcGeqDRGucw2n6Pg+dI3HQD+PkvfoHVK9dibHREVGPCU15sWb0WCEdx4thZMQWRPSVl3vV65OQUSC2Gdt6tXZ2oqi+HOxZGcfkiqW/Qp5FIwuKiIngmJ8UdqKe3D2WLSqRTwzaT1+cTMA+dnFhkYoAsrygV5SAqE2WkpglKNBSKIDU1A3bHJDRGI+667VZ5fm9/n1CIpUelHNCc3WpCXz88Z4Wfe8qnETAKw5CTX5tuRU9PB77wF49gRV0Vdm/dgt7mS9BbkuCacgjrjgYnirQaHZm9Eoxcrkm0d1xCTm4hNm3cEX/bhXlSzH+ZZhCn1/se6mFZsjKRXZADk9mqnJKIghJkl4oUYUn3ExS2lM+iHHoyNNHZ2e6sWTMfLPhKijhXG0xDCJghCrC1owd6a7qk4ZQ/GhgclmIFTSetKRaUl2eipbULedk5MOr1svcqKy1G/+CoFBHvf/Bjs9BvhP3m5OZKVTknbxHueeBhlJVXTk+8a/X1abTZ3UuHoQgWlSySv42P0+O94JrHpv6Ne39KMA+PDOBTn/gTcbuhIKSo0v4+e4EJQwA7OiOefeZJxDQa1K9cKW03FuGYwRHzXlRWKjDo06cvwWLhd1UUZDLp7KNNhk5rlOtmG51EXl6WZA20JWNNKCMjS7zsigoKYB+zoSAvT4IAzyEh176AHxOTbgyPjAlWgG0pvz8i20e+P5V3crLzRFVn3DEpJCt+gUXFRRKk2AZmp2W21nxCjUOd8As8vSLPRd3FZK1M/md//QS+/Y2v4/0P3otVK5bJm7m9Uwj4PWJwUlFRimQDTT4Dwsbj8Lk9CHi9uHjpFNx+L6qra6eFP94uNiAxC1hwMIg/PyMrG55wBMFQAFGtBklUgyaZi1+b2oyaMEI0pyFeMBaWrDtIRapYDBWZsz/ohpOBeOK5TaCx57Fjx6A3Z8BoSZUv4PX70NXbidTUdCkANSxbKhBg8vDLynJwuXtQPOCp2jM6NirCEzt27EWKlSo6dO/VYnhkCK2Xzgqoh3p0Wl063nn7fVi7frN87tUujPq39rZWyXaI6eegiAOLmQut9LK15Zigvn4KzNoYsjPS0d5xWfkjWRhXfGhmHjcwTshx6XQIBfz43Be/jO033QQ9hTj1WtjGx+FnrSYckyLf4uV1sE9NortzCCYje87UUiRGQIdoiP/WobenX1ZVa1oahgeHkJ2ThzNnG4Ve3NrWhrycHNGxIw9hdHRYajomkXcPCoy48WwTDMkaDAwOICc7W67b+NgYPB52mPx48skncKmtDV3t7UhPM+NPP/IwOi53CdIyggQvgMT9/DXOl2DmI0SXEvWiQZLFjKS0FLS3tuHLn/4MLjWewre+9jmsbFgCY3ISPAEPYnSeGh2F0WKE2aQX7ge3NYmaEqxndbQ1o6evW7wFKIpiMpoE9TpX4muhg21R1tK49WQLfdaW4Bo1LQbFxbW1SNJpYUwxIsnAfgUJQRGEo8y+lFYwT5mfXAtNGIFoAL6ID+GkN1Pl3xQAroccpJ50lcKrQoKpbvLLxx7Hq68dQ8PK9VJYoqLq5Y5WZGflCryShJRkg16ENevrl8DtCWPC6UBVZRFa2zqlwESP+aHBAeFJxz9RkRE7ewquCTsy09Iw6ZyAPwisXrMVe297l6SRVwoC6okmxuHE8RPIzi6GwZAsmAaSetT25dWOl4N98nPnG+UsO0ZHsWvrRpw4FXdxZRimPPi8D83sxw0akt3ojGi6cBa93X04ceywmH0Mj9sks7JPOmBKsUIbiaGnoxPbd21Cc3s7PF7CSpVtN2WpmUYak01Ch25qakVRcZaSUobDWN7QgJJFpRgYGML+/a/JORsfsUmNZiRewafaz5TDhkmnDT//2a/RfblT6jWkYJutZoTCMfR2tsE+NoB9Bw4i4PWIX+HXv/I5VCwqxPETp6SOohBgwrJlEAffxP/FCTg8Zrn3wrQmi0GTnAwtra4sVBkO4szpY/jbr30Nv/zRD3DXO7bgS5/9GKxJYfgmXEi3WJCVkipBjx0QzhguUNwCZGakvSna+DxuDA51Y2xsGGvWrZVMtKKiAhs2KviAhXSl5mbIvT29Apa74447cNsdt6O0tFK9ya6ojakAidJRXLoI4UAM2mgSNcGUYrxgQGYvLPyNEB/1OlHOXpIuUKFZ47q5AHLSE9Jt3jRqAa23pxu/feEF/MdPnoIfWdj4zvfCy0qkWHxRO0+HZbV1uNzRIUWk801tUrxITUlGa1sHFldVwulUmITVlcU4f6FD7K5S02Yr+LCae2jfC+hsbYQ2KSRGiy63FwWFZdi9e4/AOq8UndXCJfd8hfmVSEvPlRuA0GW2rOZ7zdwLQeAMv8Pq5WuwZ/su7Ll5h5BJItRlJ6UVrJ/M9zDMPHjxblAaoB7rSy+/glv37oEuGsXxowdgSTbCGwzC53NDa9UjKRqDPhhByBtBxZJq0QbQ0fWVN6UISQTFIpwtRTI0my51oLKqFP5gQFYtsgO3bd8uK/qxo8cQ8nkxPDAEl8eLw0eOYP++F3Hi0Ktov3gG9tER1C+rhcszKTcnawFZmZk4sO95OYenGi9gZNwmSEWSxH7wnX/AvheexZO/+TW0+mRojVYkmczQ6A2iE8Aaj/zU66AxmaWgxwmvtVqgSTZgsLcXLz73LH7x6PfgnhiEzzaEu27agq9+/pNYt7oWEadDsjCTRgttIIJsswV52dkiqUaoMsk4lJ/nwsRgkFhBVPQKIxIA6JnAlPr4sePiynz7HbeLapS6uCykTcjnsdB94cIF/PaF3yISjOHDH/oE7n33A4KtUbtXifeh+r71dXXicxjwh6Tdyj7/tQBoV2Mtzq+fNefF6hfil1AnezgQRHdvD7r6ejE16UP/yLhwwyuqlmLxqlrRUnc47MJ/z8/NRWPjGcHfBwNe0eanIQVTxnRWm3vpVmtGapoZp89cRN2yGnR3s6ccQ0l5VgK+bbaE17HDByRtW1KzQi6ec8opxZrt23fh5Kmjsv9k54Ijsd/Kn80t5/GuO9+D9NR0eDx2cbtlZyPxM+YbrK7W1dXJeyfpIrBYk1Ff1yDoso7WFixdvgJRL4U8Z7j5ygsVcZCZcaOKhYr4Bn8+/fQzkk5PeVz44aP/jhdePoKnnnsRixdXS8+Zoh0uqgINjSIjIx39nd1wu/xISTHCZDQKOo39cHIdsnOyMTA1iNFxO7Lz8+G0TaCgMBfhUAQ379mDMds4igqLpTvT19+LnTuppzgFbSwsdQdKtRMdaramwOt2SQDIyZzA6VOH5fz2DAzi+KlGVJSXC2yZENxv/c1X8JW/+Saee/opbNq6DSvrl4sEGZmDYhMWjUrmQfQlOxEkM/E68BHwuVC/bCnu2rtXAlN9WZFAy0NRjYC3DDQXUS6urIiEtE96AzAajPHtpR49Az3iWSnirAn3C2XfNNGgdAYmHGFs3bwDp06dEOYrsRIbN2xGcXEpGs+eFvzL3Ptt3vsoPrm52L340vMYHBoXwY8/feQzaLp4Dq/se2HW+6jPX711C/QGLaKuAKIi6aS+Y9I17indwn/LbUA4HGEtadakt42P4czpUxged8Hp9MATDiC7iCt0CRo2rJeTTMCOgTJQPjdSLRaxUSaCbHBwBEsWLxUlE9UDIE1MJg3wBb2CLBwecghmnF703D+u37AGnYMT0CWrqfnMhFJPyGuvvCCpe3FJteDWA7og3F4/ltUtl5uYqRb3n4l67Hyfto4W9A92IzurAKPjvVI/4P5OhBmvokrE5xAxSUcYqt8arXq0dvUIv72pRQkAsQjbWYrBh7LicxCAwl769BEotYC3iRMQRV89gS1jGB0Zhh4B3H3nHvz9P38Pzc1tWFZTI6KaxHEUFxRJ4Y/fPTUrG1m52bjc3S3OvZRU80w6RVs+Iz0VLS2XsGnTZhw5elRSXar6uNw+ZOcQrAVkGwzCNc/My0NKuqLITGgwLwshIISmjow7YNJqYZ9yCX/kcuslMQZlezAUDqO7f1hqOhXlVQIvHxsdxscfflB4Fb95+nl87/X9wkzkXp1GJVGeT6FNR5TuRXYWViyrw127t6G8NBfJZgMiPg3sY3a0dHeJ2cuy1GzohDdP9QHl+hu1BgzbnNKmJk+FcZoWX1xLKyvKsGvzWuw/ckpWfgqXMECx9cYJQcu0Tet3oKub93SPEOKeffZ5bNq4FQ++94M4ceoompsvxuXAles8XyBI/B1rC5S3c7tcuPmm2/Hgez8mPJbzF8+IyapkrPF7cvnGTXFdv5hiHDt941xt8mtQm0Fb33nu5yu9RKfTkh8QY6Q8euQYevr64QqFxNJ6Wf1mrMtfBJOVmv3UVncrFkvuKcS4RdBoBPc9Oenk+8DvJdgjKDx6+siRZ02PcpfHg8wMKwbcXnk9XVfKiktxqaUVJSWLhJ6qtxiRFi/QJWbmalTkiXnxt89i5+5bUFaxVFaHlNQ0OCfssp9dVleHkREbxkfHYRsnZl9pmTDqX2hqxOLFS3Di9GsCX2UgoeEoAVBXiuD8vPHxMfk37biJF1+8fC2WVy/B/v0HcO9774/rfsx9bSJZOz7hr4ZxWWBQYHbGFe3Y0RPQRKOoLC/nhhZ/+r4H8L1//kf86BffF+86BsGKiipxoHH7fEianBADz4HeXgR8EdkjDvQ5JRMjiIueB+0dbVi5ogFnG89jw4aVOHW8Uc4tEScpZhOck24kGQzC+COmIhRkS1DJSPw+CovoMDo2hokJG1LMOhw4vE9uXIErh8OYdPG+8SPZYBD9AqvJBO/kJBaVluHL/9+XEPU45TleT1AAY8OjI0jWGlBTvRQ6Al9IUeZ21OsXDonHR7FTo5jJjtlSEQgyW3AiNydbzE5V4c5QICAEJa9PcaAm/4FFaSIdR0dH8OB775UAMK1sxIk/MiKMSmaKZN0RH0HmKVPrUMiHNw6+Am1SDDt27Bb5uJMnT4mEvYJlUDUA5tHnknuZuhVadHU34+V9QMPKVahdsQlrN+/Ejx79jnSZ+DmsGSxdVodgkBZ1C18xajOMV3zyVTcsX//GP+DRHz4Ou0uDuvW34q4HPoXttz+AlKwCTLm9cEyEREabEs+RSFAoihYr3VJpE+2U1Zy8defEhJggTLqmJKJXVZShu7cPS6vK4Jxwiwuw1ZIiQIyRsRFp//GE6ZNNyM1W92RvHtNBIBzCqy8/jwvnTkmbi1sGehcmJ1sRDISQn1eCjZu2Yefum6WeoEbTCxfOo6KyEgaDRSYAASvMBNT3nm/wtUQlclAslKsks5f33303jh05LAQnrY776GtcFRXEcq2/X/URmxa3ePqZZ8UFuLauHqODo9izZQPOHnsdF872Yv261RihGMu5RqRmpgN6DVwel1h2sWjK/n16qqLD7w+G4aJwanaeaNzzOhDU09raieolS3DxUguMBo1MBqJF3R4vUtJSZCLHqNAT1kAbU3QXWNAjFoF6fo6JcRx8/RX5rqrNl1wHkpL6+1FUWAjvlAupyWaM9PUjYrfDO+kQvZsUqw4ZaWaYjDFMTYxiYkxRLh4fHYPT5pB2naT2Wp2itpsUQ3p6ivAbRkaHBasws0fXihMQtRxHx0aRnmIVkgy3CcVFhTAmJ6OhrhZWq3n6PmGmwPoQtx88JmIE6CBVQDp2XOyG9+H+1/fhV79+DJkZ2bjv3vfj/vd+COUVSxMYflexnIuL1/T0NKOrvw1asxG5hUW4+74Hp/fvdavXoLC4RABZb0PLZ9a46rt89Stf1Ky76S4UVDUAeiM8U264xpyyFzNYTNAl64WUQKYYpaFplCFWyxqIlzpJPmOjY6ivq4fZYsLJkydRX1+LM2fPoXZpJcKhqKwQxUUF0BvIJAsIy6y8vFRuTNnahiNS7Z/3y8cvrFowOXnsDbz60lNwOshws8pKJWaJiEiGkZdXgDvuvBelZRUC/LnUfBGxaETkmEiW4fenmAnH1Su6vMk0SEtNQ7I1BeP9/RgdG8ao3S4FThgNiFLqWAp/HFciz88jXTWfok3iY85QDTXYcuUNyUr6aSrHGCKoLM3Es88/A3OaAbt3vhOXLjXD6/dKkAt4fcrKbTRhdGRIPpMUaWZq1C/k8VWWVeLs2fOoqiwVpSAKu7CD09HRj2Q9+f06hIMhSeelAK2SoQg80zDpjmHcZhcGZfOlJgz2sJVGarLyPIdzAhWllbh8uVNu7NEJO1zhkAiEkJCmS9LDQEch7tGhkGn0RgMGxwalzahNiiCmjSCqZ7odip+4IKAJIxKKQqvRKkSzeDoui0U0ApPJLBM+NTVN2pRhqi8lSVItCkF0iaqoKJ++x4gipVAHsxRmIlOTk7IC8/rL1U2ApY8MD+HxXz2Gg4cPoKqqDPff9xBu2n0r1qxej2SD0vJT3/dK49DBV5CRliKYmMVLaqTTwrFrz574YhuXTFvAqM0wXfWZ1wwjWn0SCotzkZGZArfXI2kVtdpVog3li02WZNkfDY+MK9zvVCt6BwZEQKJmaTUmJidFS2B5fT1a29vF7psH2NLWJlJVhuQkuAUnPYSKMhZ9SAWm6o0JeZnJKCtTWiSJCkQchCQTcZhY8Z+wj2PfS8/j2NE3xDEoFIxICkqxR+ckzS2SsLi6BitWrMCiRUXweJ0SGHhR2d6Zqxg87zkR1JURB48d5/KAwZEBlJQWyYr02C8eUwwgBVE2LWZ35TebO8GvpGmnPlRiS1zmWZNkgGt0DIMD/XjH7t2CtKtrWIa12zZj9Zp6/NeP/h88npjs3e+8404cPXpc0J4EY7GQypWNuPFxmxc5WTSr8IgPnd/rR2lxCXKz83Gm8QK2bFyNcxcvirW50+nAhMMFCvWyaMiaAj0NmKkJBJWJQJR69jYpBicbtNId4CqduLUaGhtFYUEBgj6fGGxQQp38duIXplwuESmRnZBGAwPXaWkJsmLP9p8/rn+gtAEVvUClGBqJBGBNMck9ygKiuOYmUewzIj38/v5e4bJkZ2cIjp7ZomgY8/tJV0uH8jJFAZpBp75hRdzTIEk0FamwzOCZl18gtRV18qs/p6YmcOrUQfzwx/+Gru52VFYswY4de/DQQx9HSUkZ9Hp6Ob4ZUqxiUM6eOgGfZ1J0KA3J5mnz27qG5eIJcCMp09cMADtXFGkud/WgqaUZKelmZOXlSu/V7XRCG40iJyMNjvEJ+LxBFBYVSnSiymvdskoUF+ahqakFFpMJVRVVAqFNsaahvDQLre3d8S2CEVOTXnR1dQuqjBeDJ4JAHQo2+v1RpGTkzbG/Uk40s41ly5ZJWy4xCAhff2QI/T2dOH70CM6dPYOjRw4IfoC6akzjeK63b98Gr8ePmqU1skqwvck9nhSGrlLB5c3Am+nZF16BbWgYAU8AO9+5Fw/dezu+/U//CM+4HRqx4hJLhvjMlW+OGzmkmGrQ4dTp04q6rV4n/ocaqsOGyfjLwkhPG37z2M+QkqZBfmEh1q3fgIvnm6CJhhAM+iUbCEXCGBCtQIPoCHDace/NmVyQVwCjwYQLF1uxecs6HD99HNWLl6C5pQ1Bf0hWJBqQBIlMIzklrDRBp5xTEuxJKw8Egjh66DXlDHASx8/t+JhNzELLi0rQ0dGOisoiuL02CUzci/M6RKe1DELIyCRikSm4T74nA5WWUO64EAbrLnL9JZtLFlAhATJkoTJlJk6BBVy6HWVkpqOoqEAxBGEhlZkGkjBuswkidffmjdNtbi5ILOyODA/CoDcLcpTisuQIGJL5OTPTSL03xTx2oB+/fPzHOPDGiyIvRyvwm2+6Fe+5731SK5ivVS0uysEQ2louwsQ2ri+A7Jx8ESWprq2D3/dmncs3D2U1udbqL5+3kBvt3p21mvXr14r08+ioTZhf2elpsrcaHLYL77uyvBgDg0MCrNi1cyN83gCOHmvEqpV1WLpkifT4uWddUlWCzh6boO8ovkDeeG9fP3JzcsXEkv5xRDlxMB0dt3lQHNfjn4vSY6WVnPzNWzfPugDqSVVZe/wdq7gjQ4M423gSr+1/RcwvzjQ2oq+vW9xcWZRkZZntspS4ctC1dO+4R/729x6FyZyCqXEbvvnNr8JqMOBPP/VnSDKnyvdTvnPw2pnAdQ3lmBTD0Sh++9KLyMpMR2FBvkCdScNm/7yrq1fOwb/8n6/CMTYJnzeE7OwCycScDie8Hq8Es5ysHMFBCNc/L18o2ewGaJJ0GB4aRN2yapF76+7sxvKGZbjQdFa2aefON8k2jS1EERJhz55qdaEwWltbpbXHQqxzYkr09hK+vfx0OCbR2tONZTW1mHI4kJmeJtqDgWhIUncKXaiTg+fRbDJJodCSkiKQZTEbVUnu0xmA8p/cGlktJmnT8djon8fzf+EiNfi8KCophNloEuSgkDg1Wnh8frS3dcCQbMLNO7fJOeZrWWBUFhyH1AEyMrNwoemC+FVSyo4Cs4kLRmLrnI+2tma89PIzOHT4DZSWlMois3fPHdi0abtkG3Nfy3Hy+BFRYmJrNi+vSIrVVNuiG5UcZaISS/x8ztxdOtRmmBd0sy24krA0W6PxefwozstFmsUMm8MJjz8o3nAerxunGy9KBXn3znU4eeaCcAD27NmGkdFx9Pb2CpAjJycLQ8PjcpIrKiqRYjXCPTUlcMjMzAzRiBfpY/V8sPer1aN6SY0QXRL7oeq/2YYZGRwR1aLEesBcxpX6e6kCh/xiFX7p4iWcOnNMyEC0C5uKu7+qsOD5HHzUwXSXBKnv/edP8fxrr0thKCczA68+9Sscfv0N/Pt3/hn6jFwkkeAiOmxeRKIKHnuhykHqMaioNwYUpTClESJNcna6rGxHT52QG+LgwcNoqK/D+g3rEHZPoH9wQF4/1N+H73zrb2FJVvQdjeZUbN60RaTYpPqekoKBAdYxvFKMGx0dEzMK9qgzs7PRdKkLa1Y1wOsOYnBgWFpl/cN9yMxOx8VL7eDcNhoo/aYXG/ARu1OIRWz92R0OrFyxQiZK/KRK0Y2DnI/crCyZQPbhUQScXtRUVsMf9MLLQrKYkSjZmKzGInsek5oC9+CRYFCphqsZwGwYnGBEuKgw0GmTDWi93IlzLZdlJS0qK4HT50WEJLFIRBCPNqcbGTkFmPD48OkvfhWF+QVY3rBCCpwyoeNOQNFIGMl6vYB2SPElG/JK1481JXZOiIR94cXf4LUDr4o4K92u9+65Dbt23oTC4mKhlauDx+QYH8HYcC9sYwPo7unCytVrkZychChVPyM0AdQhFtUhGp35SZNdqkBdqeU337iuUmLZonyJ5v2DwwizEGgwwOPxS7FlWe0yEYHYd+A0krR67N69FRcvdQpCLz8/R1ouHp8XqSnpWLtqjawabe29wiIrEUIID4Q3uyIWypWFJ434AZpFpKVlzHuCeWNcuHhBUtmKyoor6gSov5+bIbBKTKxAeWmF3CiKieUCQDoyqSOyIvzd//0Ozrd3IhYzoL62FCf3vYTGE6fxZx//GE4eO41okgHatGxoUy3QGHTxbUpkelLPffD3cgxaIt/0gmvXplmgS7fKz5gmio6Wi/j+d7+Le999j7Quv/z5z+LWW29BfV2tWHzbJiZwsbl1+lgPHXgRdrtH4Lj0FZh0B1BQtAiRiEaq34Res+VLbAO3LMyG2BYjT57+g+0dg1jRUCcgrtFRu2QKDqdDVH47LvdBp2EBTYGeZmSkIVlvEI0BCr5m5+YjMzN7hkkXT2FrqipRWV6JKbcLaSlp0EQjyM3IRHqqBY5JB4aGxgR3IedCVNNDyMnJlao8Ld64BZz2W5gOBEqGoRq3KL9ih1+DNw4exorV67Fk2TJhcMZiQdHJs6anCST8xX2v4gc/+wU+8/mvoL9/HOvXbIBBlywdh/gllw4GOxvpGZlSN6I03NVwI8wgWHhcQzamyNe/ghdfehaNZxvluFjf2r5lp2RiM/Juys/W5otyTsaH+1BT3yDefppYHFUa0yMc1SOgPiI6BKM6+Xk947oCQHWORsOLRVEHi8ksE99A7ydo0TcwgNFxG0pLi0VYorHxEkZGxlBUWIRQJCoaABmpVoyMDWHUNip7OK3RgMqqCmhiOkRFa0Aj3QBu0Jnas31IDEDtsiqs37h1WnFo1jyMT/bOy52yYt98y83TPP+r4bOng0Qsir6+LlSWL5bUmR0NlTl4Ldoz95p8D26JPv/lv4bbF5LVRJ/kxz/81Z/j9pu34Mc/exR33f8efObPP4lnn3xSthz+oA9JKTOTeu6Dv+cKH/D5MGEbR+u5Rjz12M/x11/8Eu65+y7sum0vPvnpT6Gz+RLetX0tfvZPX8eff/T9iHod8HjGYU1hXaAJA4Pj0y2qjo5WHD3xKszZyQiy7yzOxnqYjBYEfGFUVizG4Mgw2lp7JbPp7u2CNcWIgJ/1GAsWlRRh3DaJzPRsFBUWwOvyCLKTPoN2p01EKKgI7Q9RjCIKY3IKSooXiYITqddbdtwi50zFblBF+dU3DmP/oQNyXQn/TWYtJxpFQdYiJBvTxFmYQDK5hAya4QjSxQMyIO061j1mBERmNAB4H7CeQ1ty6iGw5vPLnz+ONWvWYe3aWrgnJ2Cx5CIrvRC6aAz7jxzHex75NL75r49ifMwFoykVSTr6IdrRS4FQu11xk4rfS5GYH/6AEw6HQ7KRaw2v2y1Bb+PGLfLfU5N2HDt2AL964mdCZ+c12LNnb4L1gfKvvt4ewVTQULV++SqwXDFTCJ/9GWr8W5VzfRXC6wsX9CdbX6w5eN4eY9vMZE3GhHNKUuhyZgeTbjgnXbA5JuBzu7FuzSop3hAOTLdTXszSshIRRYzwJrQaxHyCMYQHOj5OYo9PbK24j1xUUgZjshYpGRTAVFLp+YZSBYYEDar0PPi+B7F//370dCtKs+pkvtKE7uxsx8ZNOyRdJAjIlJcn2xKmd9caLNhw23LuQhMe+ujH8eRjj6LvUhu0Bj3ecdMObN+6Fpe7+3HizFm89NyT+P6/fw8h3pxGM9KtaYLAS7NalVXN4xFJdUJouR3x+f0IhILIsFoEultdVYEP33UXqitKUVqyCIb0NMBvw9EjR2HW07UoX/bESXo9jh0/GT92boeYcYTxynO/wqabbkOYbDm2R2XiadHR3YPVK6qli8OCGWs7dKT1eMIwGrU4c7YZ/oAXtUsXI6ZR0JJE7/FcsSim1WtgNiULO5PZH/n/POe0I+PKRhHLFeu2xcEw8dVSEIMR/OVX/wYHnnxCAYJ5g9BrgdzsbAyO2eHxBWGzOZGXnxWXpqffgPL+LN6SPqxuqBTQXTwF4HuHwtLWXVRWit88+Qw2b1yHmg1rEXVPIqhNQnN7Gw68fhRHT51Bc0c7MgsKUFJagVgwgnG7Qyr1bEeyhpGRlgW70y7njEGIatAsJgY5+a8x3dSFhtDhd9y8R84bhVc0miiaLl4UTkBBfgl27Ngh0nunTp6cvl/p2HT82GFhrVYuraEIkFzP+FG+qai8Kuf6BRWvOwBwbFuepTna4opRyJPVVHKoh4btctNywltMNNesxuXuy5Ii6w1GUZrJzUoX5hknfSCqFUUamnz6mXqHQwgEQrICcPLl5BfBaNXCEAMefuARDPS1iKPN1JT7CmdaiY6nTpxCdl4e9tx5O86eaJLq64Rd8e+bWx9Qi4qDw+xrG5CZmSuED49rSvANBLMsRMGFKxoLUs+89BrueeBD+Odvfg3Nl1qQwxan2SCc92XLGvDwuwOw2UbRMzaM5pZWifwOpxMBP0VRQ8jMyoZVW4AUc4pwuletWSUZSXVlNdLSM5R8LcCTF5DUm5gME891WSnsninYXU6ULSrGcFcvLlxqx7Ytm3Dw8FEFVGUwYLCvHx77JLxhot7MaGnvgtXIDC6GqakIltfX4XJXL6wmI7KCflH5jYQ1WLKkGh986FZEPG78y4+eRk5xEULeKLJz0kBRIKE1RSHUWrZ1iYfnOQkGw9L5YRCg6cXS2mVobro47YzDQHPufAs+/tnP486d2zEyNCadIFNyREhi3AvTgbiotHjavpv3CYudvb2DIm1eVVEuN/H0timuncetCveRZ5rPYsfedyDVZMTzjz+Bg6casW//YXQPDqOyqlq0CnLzChAIRmQrWFK0SNSWBRpMPwOdAYWFmdDoNFJQ5LUiTLemdql0MRST72txlZUfLAJu2bwdg8NDCPi98U7BgDyImty2dQfOnD49614juI0ZbV93F6pWrYI3QCSl8pn0B1IsWDVYlXNltN8NDwAcm2pSNMcvO2Ls3fOEk0JKMAUFQonMazxzHtVLKlFXVw+TWScOwJ3do7ISRTVKi4opGuHA4UAIRoMeGTmZ0BkNMKcCFjPQeOICPv/II1haXYqjhw7hlnfcjDfeODztqDrfeeaEffG557Fx5zasX7cFtUvq0Xj+CEaG+qW4NXci873YVmKKzHRsdHRI9ofqhnKh0k1M/XV6LZ5+6XWMOSfxlb/4BE6eOYuammrk5OXDabcj4uZEDwhnYvumDShdVgfBUsfBPIqXWpTLMobpO8B+fSyM8eEBmGKc98G46Cq/XURwFqybMGUftTllRU0pKMA/fuUb0iKnbwADAI+BN1EkFMFgbydWbNwMtpbdXh/SrGQ2+jAwNIqaxYUYGB7A+pWrRLORGHihbRuT4ff60Hb2NJ765Y/xqa/8FVzeGMZsU0izpsSNVYF9r76MFQ31MlnJ5wj5Q+K6nJWVDoc9jNXrt0gAUFBsVKtVtlq/euq3OHW6EY888F587otfhIbkIZsdw7ZRZFjMOHn8BFasbJDjCPrDMOnNUh3XRDSyDSCQTK69+A5G4Xd70NLVg5OnzuBMcztaLvcK8YjBdGZohI1HwNHMvaCTTIQ280SlckvImowvQCs1j/xNQE7hMC7G0aALuT+Eqht3pers6sDSpTXiZZlYqzp58hC2btmMlStX4cyZ09NBkts0BtAXn/4NvrhpFWzOmEzamX5HDKtyrt3uu+EBgGNDVabmSPNEjG0jKsUmG/XQcbWxpKG+YRVIrbbZQnDavGJMyTRXrzXKymLUJCE5FENBPtsogDWJaRswNunH0dcO4fmnH8PR1/bh05/6JD7/uS/K57373nfj9dcPXfkLSYVdqaIe238Qfpcf69Zsx45tt2DKZcPw8AC8Hh8az5ySVgyHmgWcv3gSi6uXonbZSiwqrhSZ8rT0FIzbBqR6zzT7WoNMOabHR4+fxfs/8UV89IMPweXzY2VDBAX5+YjpNfC5lWIPe8nFYxPwulzQJNi18ggIsbUNjyCtIB9RTZLsM0lsUSDSqkoO8es+QEd6dFTEO8sXlaP7Uh9OnbqIjzz8ATT3DMhTxZshEBDDi5ysDNjGJmA1GcS+i59MFCc9A5fXF0ohlIGERUCyOTMo9W7RYuPqLeg4dxoXG0/C7wMyUrQ4tO8whsdH8MgjD8M7BWTnpMqqy8mZlZ0pLSuKpzC7IkFr9zvvxi9/9p/SvlSHZAJarRCDPv8P/4SoXosvfOYvsbiiAh09XSjKz0f/0Ch8wdOoqK1ESX65HPuyxVEpBEa1OjT19qCjoxvEq7R39OD02fNoar0silSJQ/WpILaAnxsOKbZq6iTm4uT1KAU/6heqw+WefPOtdp0S8Opkp//Atm07hO/APf50VooYDh0+gh07d0sAmNZEiS90zz77ND762S+JQGm8ESRjVc7C2n2/kwDAsbk2Q/OT587GaASRRZnwFBOmvCFpQbW1uWVVZTWbf+M+lboYhMpbjUDECwyM+3Dk0BmcPnQA7a0XYB8dQKrVgN27d+GfjhxFySKCgxSn3LvedQ++9OWvCIrranRLFTxy9tRJTIzbsX3HLYKoyswqwu5dq3D/e96P7//Hd0XUgy2wDes3yiSi8Yk/EEJBQZHIXzkcY+gf6Bfk20KHak1OkMi3vv2PWLG8ATdt34xPfPxDKCvOh8/rQQRROL0+RfyByLMk/XRVh9Bk6T9PTqFi6RK00VTTYp5pdcfbiATJmA0Z0jEZHLmM0REH1q7ahC9+6a/F5prKzinjcenzuAoNyVou5yR6R13Iz8uVLg7JW4QDOyY6phNZ9uAJcx0aHkSkgts1YOuum/DjH/xfYcAxiKVl0ktPj3OnXofmkYfjMunUqIsJWYZtQa/bK2w6ug4TR6DVmrB6zWbRKkhkXSrnTDEZ+eLXv40XXz2I2265Ga0dHZjy+mSvb3M40Pf9PlhT0kW6zB/0w2Z3oH9oRGTmWDeaOxJFa0UijI85z4m96R5SMr+ZdvNMwW2h2eDVBt/j0qWL2Llzl4ixshiufCrrBMdQVlYitHRqDaigNv7samvG+cZGrNmxDZOTDJqat7Xy37AAwPGB21dqLo7EeC/B4QpKgSRJQ7XdCqRlGIQRS61VAuNG7B60n23Cwf37cf7UUel3ZmekYuOGdfiTjzyA+mXLsGTp4tn7ax2NLSKiWPzggw/i3/7t3+R3DAzXirg9PZ2YePoxbNq4BWZrtohbsk1Fpdt1GzZiw/pNQESLsQkbHG6XSEEfOnoAY9RtD8Y9EZMMspclcm4hIxEEcu78BXk88cwLuPddt+OOPbtRV1kCp2tSePK8odkKVenKOr0RU1Q5CodlL8o9r9lildoK7z8y57RGPSY9k2jt6ELT+XYRqCAh6dnnXkFFeQWKS4rh8xDSq5/+PoRN2+2T8Ph8iARZyMpHYEpx/KVYi2vSDn+YoB4zBgeHULJ6uaD6fMEIgtAK49GalgqHbUxWx/ycclhT0hBBWHQ0tRFI3YTOQfR5sBrN6HcPSM/b5SKoJ4LJqQnc+a6HcPrUQUS41Zl1zmYw8m8cPymP6xnq6i4pehxt+NaMPGLK/88C57yFt7nKPclJ39XZhb17b8XPf/7TOISBoTOMs2cbYTZZMYEZs1QuEtx2dLScx6abFYDSqpy4Qu8fQwDgqM/XaJoGYrH8LANKS3JBl+exYQ/amwfQMzCAjqYLOHv6GGwj3dBGAlixogGf+uiDWLtuLSor43JIc5SHiOZStQjUFf9zn/scHnvssWtmAer7CILL6cSLLz6PlNRMESblip6k0+Ode+/C6PCQaOjTkbq1uQXtLcrejoOTc9WqdTAYKKR5YsEBQP3sRIUYGp9++5+/i3/990exoaEWK+qXYmVDHe7cu1f22aAcWTCMaDAAczLT+6j0udmZYI+cq3Qo5sfg2LDYng2OjSC/IB8ZGSkwGgyiXltYmIP77r0LP/rpfyEtxQKDIX7uRLk3Ar/PK7f3lMcrOAznpNLylJWbhiEhkrjMMoEZWym7TpBLsjYFWXlZWLJsOdouXcTw8AiW1pRLZ0anMUjgUBZOtu38sv+n1YLNYRPADF2M6fxFfkRpcRU2bNmFwwf2KTf2nFqOWhwURd85uA31Z2IVXHHCUSb7H0iP+boHj+H8hfPYseMmrFy1DmdOn5jOiLhV7O2P+0wkDB7vxbNnpNi6rkDAz/ijCgAcdcXKVfrwp74a6+1sBQJTMLEukGxBRVkpHnnoTqxevRpl5RUze9mECa+umpw0Ki1XHWrhr7y8HF//xtfxZ5/8s2tmAep7q4GC8s98kLJ82613Y2TYBpMpFU1N53D8+BG4XA6lJ63VoqZ2JVav3iAOrgcPPi/6Aguq+M4Z6iok7Rvu5/1+vHGyUR4c+d/4R6yqq8GWDWsFMbekshLlVZWoJfqRQhbRCPp6usQ0ZWR4GMSj1S6rQ8OKlejq7kRqgRVjY+PYsGEV0lJSJTUkLbu8tBgdAyfl2MXiWtSIIlJvIAuPGHZFppveAfRe4HdTroO0JN1+WM1W0UosXbJUSv0btt6Mi2dOCOEq4CPSLg3aqB6TPiBVCpMxaV3y9yKz5nJJrYXdm4nJKTEnDSfp8NFHPoMjr++74tKqSs7NvY7/E0ZMlagPh3DixCls27pLAoB6fPSZKCsvE3m9+J5vWvH6iZ/9RPPEz35yQ7/PDQ0A6vjPf/k7zfmzZ2MVFWWwps7o+SUORTHlyhN+vqFGyU9+4pN47vnn8MpLr1xTwYcjkYLJqir3XyuW10vl+ejRwxgZGZS/Kym4Hg0rVuCu2+9Ga0sz9u9/Ac4J2zWzjWsNJcVVgpySqir7u5ExG17Yf0geHBR9ZDtubX0tFi9djJHhfpQX5WPV8uXYsmULdEREGow49/orSDEzvfagrLxcznM4GIQu4pRaw+JNW/HN7z0q35lIQVk64opGnO4U1CCak5VuopUV4RZFEddsMuJy72VULV6CvgtnUaoBfH5g3dab8R/f+QZCLi9tAGEyEbijKOfG9FrJKtiiUMwzgDH7uICFyDzk1kU+w+/BsoaNkl25XZNv+7z+dxyx+PFebGrEzTftFsah0nIGhocHRUhHnhef+PHn3zgK4O86AHAsX7lSFIVkexMvwMiEZ2FGFIfe2kcr6D3gX//lX7FiuULTXMhNpLZV8vJyUVJcgv949N8xNDAowBf1b3zjTVu3oKi0HBeazuDg6wdl8s/8/e0PyXbmcBSkWBVf+SikQsgqH/K9tUmorqzE4soyVFcsEv46dfRkNQ0HsaahRmoJrNbrky04cuI4Tja147EHP4DfvvIqFtfUICsnBy3SttJKcc+o10Jr0CFEJl2MrSZCTCl9NSkgoozMbLR3tCOmT5KJ6/MEYDQno2rJMhQVlMBjtwmbkjDccCQmAK+ITovUlBQB7CRBD1+AXR0/3O5JOJ0mEW5NTcvA8NAo6mvqsGHjTux7+amrymD/Tx2xaTbrhADnFi2qRlvbBcnC2N1ny3HO839nJ+l3FgASJcYZB96OqeJ8W4HF1Yvxl5/7LL7+t3931SxADQ6cwPS0I1/ghRdeEIKS+ne1FbV6zWosrqoW5NqLL74oZqkzai7XHgvJRuYOdfszX0CQnCESQVt7hzwSB5l3dFeuLCkQp10y5YZtDgyPK0o3NLsor6yWHj7fKxjwISU1A5k5+UDsAoIRYIqyaZnZMge5mk86bII9YGshLTUDPT29IuzS23kZNQ3LkJqVgjUbd+Hppx/Hre/5IIwmEzQ0oQi4ELUYFfScz4v0tFy43T7hC5AVygOhSg5dneyOAbj9Hmzddieazp3G8Gj/DQ2w/12GJn5fklm5ZEmlBICZ4sasf/xOxw01BrlaILgev4FrDfWG+dIXvoCSspJp7P58gyeZae2SpUuxectmnDl9RmEnzvk61BRgG5CV/wOvviKTfyF8AJUzznG9k/9K31dIQgl+C0nxz2AKzZ/CGQ8FxbL71PlmvHbkJJ579Q2cPncRNtu4mLLQdIKZF78+1ZGIyKSuf1pWroi3EGfvsNuFd8F2eV5uPhy2cRhMJukUlJUsQmfHZWnD+j0ueZ9QBNiw7Z1ovXQE/b2XRUxDq2Nf3S8FOPLtw5GoEL7G7Xbk5eZJVsMsgnyJ3NwspGemYmBsCBWVDbj7rg+oZxH/20Ysfl+dO3sSWTmZs34X5zX/Xk7K7yUAqONGBQE1epJG+q2//+a8k1T9qJr6ZVi/ZTt23bRXOOqUd0qc2FKU0SbJ5B8eGsbzzz0rKkcLWfnV76H2/m/Zc4sIN97oQVyDwhxUgoKynZrJFtRuCf/NLRGlut0un0CNU6zpQq9mDaKwtBwaM+nWFPVUUG/cexIMl5tXJGsOFZP435l5FG7ViaKSPxSAa9Ilv69tWCEZQn/PZWqRSG8/6I3EzXuSkKy3wmJJxdhwv3BESBtnpsD47POHYNARFORFODmMJSvWIiWdik5vz3Dzv/M2wG53yLml2Mnvcq//RxEAErOBtxsMRDcgGsX9770fN73jpuk0fvrvei2Ky4pRUlqK7IJiGEwWEZ9UwTSJQ90etLS2KIo4C6gpqM+hUebOnTvld5/9zGelB89xo7Y8VxqJ7S8Ss1QsPIfFkgIDZadiSQL/9biUPWVKZjpiRP9FyTADzClWcV7ihUhNyYDH7ZYbguQtDSEEEeBycysKi4oxMtgvBb/CshJUVC5GT2erwIk1FCL3BeL0bSoEmWE0anDx3EkJMFT3YV2AdGBSyVmEJMlGY9EjZjGirm5DfDL83m/FP5rxxBO/0kw4HL+3VT9x/EHP+tsNBMpKmIRb33mr+n7TKwknYkZqLiJBLQpyC9B06aKs/ok67epzKysqZfJTpWihk5+D4pmv7XtNmIcbN27Enr17BOChfrc/1GAqbrakyk96JUxNKVXltLR8BUeu0VI5HEVZWWhtOiMFPcqFB4IheKZcooDDrIEozqHefhSQ+++wC+DHYE5Cw9pNmBgbAuOtwWiW9p+i38oefgyBGDA8YYd3yoPiwiJ5LbUFiFqkUUjE50OG0QLn6DDuec/7Ubho0bQq7v+mEVOKe3/Q1OeP4oy/1axAvWGeefYZ+akCR7KyM8QUJD+/GJkZmQJueWPfSwkAktnD7rCLGIb6HtcazDT4vA984AOoX14vLc177r7nhtQAbsSgOpHJaBB0IVGG9EDgSLGkixWBSGRE2HJMFeYeoaVms0Zw+6PD/QKH5qFQOTnk9yHgI/89CMe4DfTNXFTdIHLgPFPsMIg5VQwCzqKl+6TTJyq/5ARQAJaKOlnZ2eKPOuV0o6+zE7mZBYiFw8guKcFHP/kFxSvgf8fQvD0rmP+BAeCtBAO18EfO/7Ejx6Z/pzVokZ6ZDr0uGbk5OSILdfbcMYRpDMGAMY/+Gltv1wIUJQ71dUxv2QNnS7OouGi6RfmHGur3InmIhB7q4iuGsspl9nhGiXpGVKvUEHRWC/IK89DW3iqtwOz8Mnj8IUH/ifJ0UhIKSoowarOhuLwM/f090OuA1PQcBGJhRPRA/ZpV0Iryrh9RTRTp+WnobjmD1JQ0McygSenkxKhIvxmsaTBbUuD3T0ktwGhMg41OO7vehV233vembdz/oKH5Y5r0f9QB4ErBYG5QUG/2pktNUvhSV2VrKk0+zMjLWyRKNuP2cbzxKmGnV241Xe8uRF3pf/DoD6Sz8LWvfQ0//Skx3QunD/8uB/fcdvu48MZNFOyLg4COHX4dHl9E2HiREKXHQiIiQiYgpcPzCwoFo08mJAt+sSQtLGlpGLePiUjo0OAgKH9XWl4qvAB2BQoKCwRWTQowQUxmiwaXLp4UdxzHhEPOO1Hr9LurWVYr4iTcC3h8btHLYxFyYsqDhz/2KaW7sxA5tv8+k13zxzjpE8f/Dx4VyiHHDDUnAAAAAElFTkSuQmCC" alt="icon">
    <div>
      <div class="title" id="appname">校园网自动登录</div>
      <div class="sub" id="subtitle">掉线自动重连 · 后台常驻守护 · 配置简洁清晰</div>
    </div>
    <div class="hdr-right">
      <div class="ver" id="ver">v--</div>
      <div class="hdr-links">
        <a href="https://github.com/Zelm05/campus-autologin" target="_blank" rel="noopener">GitHub 开源</a>
        <span class="sep" style="color:var(--line2)">|</span>
        <a href="mailto:yz050930@gmail.com">yz050930@gmail.com</a>
      </div>
    </div>
  </div>

  <div class="main">
    <!-- 左列：账号设置（自然高度，按钮不被裁）+ 测试结果（填满剩余） -->
    <div class="col">
      <div class="card account">
        <h2>账号设置</h2>
        <div class="acct-row">
          <span class="acct-badge empty" id="acct_badge">尚未设置账号</span>
        </div>
        <div class="row"><label>用户名</label><input type="text" id="username" placeholder="统一身份认证账号" autocomplete="username"></div>
        <div class="row"><label>密码</label><input type="password" id="password" placeholder="" autocomplete="current-password"></div>
        <div class="row"><label>服务</label><input type="text" id="service"></div>
        <div class="row"><label>检测间隔</label><input type="text" id="interval" style="max-width:80px"> <span style="color:var(--muted);font-size:12px">秒</span></div>
        <div class="row"><label>认证服务器</label><input type="text" id="portal_base"></div>
        <div class="row"><label>校园网 WiFi</label><input type="text" id="campus_ssid" placeholder="例如：CQUST-T（也可勾选自动连接）"></div>
        <div class="row chkrow"><label>开机自启</label>
          <div>
            <label class="chk"><input type="checkbox" id="boot_task" onchange="onBootTaskChange()"> 极速启动（开机即自动联网，锁屏时也在后台运行）</label>
          </div>
        </div>
        <div class="actions">
          <button class="btn primary" onclick="save()">💾 保存设置</button>
          <button class="btn pink" onclick="testLogin()" id="btn_test">⚡ 立即测试登录</button>
        </div>
        <div class="msg" id="save_msg"></div>
      </div>

      <div class="card test">
        <h2>测试结果</h2>
        <pre class="out" id="test_out">点「立即测试登录」后，这里会显示探测与登录的完整过程。</pre>
      </div>
    </div>

    <!-- 右列：运行情况 → 后台服务（按钮顶部，无留白）→ 今日日志（变大填满） -->
    <div class="col">
      <div class="card status">
        <h2>运行情况</h2>
        <div class="chips">
          <div class="chip"><div class="ico net">📡</div><div><div class="k">网络</div><div class="v"><span class="dot unk" id="d_net"></span><span id="net">检测中…</span></div></div></div>
          <div class="chip"><div class="ico wifi">📶</div><div><div class="k">WiFi</div><div class="v"><span class="dot unk" id="d_wifi"></span><span id="wifi" title="">未连接</span></div></div></div>
          <div class="chip"><div class="ico daemon">⚙</div><div><div class="k">后台服务</div><div class="v"><span class="dot unk" id="d_daemon"></span><span id="daemon">检测中…</span></div></div></div>
          <div class="chip"><div class="ico auto">🚀</div><div><div class="k">极速启动</div><div class="v"><span class="dot unk" id="d_boot"></span><span id="boot_task_state">--</span></div></div></div>
          <div class="chip"><div class="ico ssid">🎯</div><div><div class="k">校园网</div><div class="v"><span id="campus">未配置</span></div></div></div>
          <div class="chip"><div class="ico count">✓</div><div><div class="k">自动登录次数</div><div class="v"><span id="logincnt">0 次</span></div></div></div>
        </div>
        <div class="lastinfo"><span id="lastinfo">最近登录：-- ｜ 最近错误：无</span></div>
      </div>

      <div class="card daemon">
        <h2>后台服务</h2>
        <div class="hint">常驻后台，掉线自动重连；退出设置界面后继续运行</div>
        <div class="actions">
          <button class="btn amber" onclick="daemonCtl('start')">▶ 启动</button>
          <button class="btn warm" onclick="daemonCtl('stop')">■ 停止</button>
          <button class="btn warm-outline" onclick="api('/api/openlog', {})">📂 打开日志</button>
          <button class="btn brown-outline" onclick="quitApp()">✕ 退出</button>
        </div>
        <div class="msg" id="daemon_msg"></div>
      </div>

      <div class="card log">
        <div class="logbar">
          <span class="l">今日日志 · <span id="logsize">0</span> 条</span>
          <label class="chk"><input type="checkbox" id="auto_log" checked> 自动刷新</label>
        </div>
        <pre class="logpre" id="log">暂无日志</pre>
      </div>
    </div>
  </div>

  <!-- 页脚：免责声明 -->
  <div class="footer">
    <div class="disclaimer">
      免责声明：本软件按"现状"提供，仅供学习与个人合法用途使用。校园网账号、密码等数据仅存储于本机配置文件，不会上传至任何服务器。
      因使用本软件造成的封号、断网、违规处罚等后果由使用者本人承担，与作者无关。请遵守所在学校的网络使用规定。
    </div>
  </div>
</div>

<script>
let initialFilled = false;
let logAuto = true;
const $ = id => document.getElementById(id);

// 本次启动的随机令牌（由服务端渲染时注入）。
// 所有请求都带上它：既能防止用户本地部署的其他网站顶替本程序页面，
// 也能阻止外部网页跨域驱动本程序的接口。
const TOKEN = '__TOKEN__';
function tok(p) { return p + (p.indexOf('?') >= 0 ? '&' : '?') + 'k=' + TOKEN; }

async function api(path, body) {
  const opt = body ? {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)} : {};
  const r = await fetch(tok(path), opt);
  return r.json();
}

function setChip(dot, on) {
  dot.className = 'dot ' + (on === true ? 'on' : on === false ? 'off' : 'unk');
}

function refreshStatus() {
  api('/api/status').then(s => {
    $('ver').textContent = 'v' + s.app_version;
    $('appname').textContent = s.app_name;
    const online = s.state.indexOf('联网') >= 0 && s.state.indexOf('未') < 0 && s.state.indexOf('不可达') < 0;
    setChip($('d_net'), online);
    $('net').textContent = s.state;
    setChip($('d_daemon'), s.daemon_running);
    $('daemon').textContent = s.daemon_running ? '运行中' : '未运行';
    setChip($('d_boot'), s.boot_task);
    $('boot_task_state').textContent = s.boot_task ? '已开启' : '未开启';
    // WiFi 状态
    const wifi = $('wifi'), dWifi = $('d_wifi');
    if (s.wifi_status === 'right') { setChip(dWifi, true); wifi.textContent = s.wifi_ssid; wifi.title = '已连到校园网'; }
    else if (s.wifi_status === 'other') { setChip(dWifi, null); wifi.textContent = s.wifi_ssid; wifi.title = '连着其他 WiFi（非校园网）'; }
    else if (s.wifi_status === 'wrong') { setChip(dWifi, false); wifi.textContent = s.wifi_ssid; wifi.title = '连着其他 WiFi（期望：'+s.campus_ssid+'）'; }
    else { setChip(dWifi, false); wifi.textContent = '未连接'; wifi.title = '当前未连 WiFi'; }
    // 校园网配置
    $('campus').textContent = s.campus_ssid || '未配置';
    $('logincnt').textContent = s.login_count + ' 次';
    $('lastinfo').textContent = '最近登录：' + s.last_login + (s.last_error ? ' ｜ 最近错误：' + s.last_error : ' ｜ 最近错误：无');
    if (!initialFilled) {
      $('username').value = s.username;
      $('service').value = s.service;
      $('interval').value = s.interval;
      $('portal_base').value = s.portal_base;
      $('campus_ssid').value = s.campus_ssid || '';
      $('boot_task').checked = s.boot_task;
      initialFilled = true;
    }
    $('password').placeholder = s.has_password ? '已保存（留空则不修改）' : '请输入密码';
    const badge = $('acct_badge');
    if (s.username) {
      badge.textContent = s.username_masked + (s.has_password ? '（账号已保存）' : '（未设密码）');
      badge.className = 'acct-badge';
    } else {
      badge.textContent = '尚未设置账号';
      badge.className = 'acct-badge empty';
    }
  });
}

function refreshLog() {
  api('/api/log').then(d => {
    const el = $('log');
    if (d.log) {
      const atBottom = el.scrollTop + el.clientHeight >= el.scrollHeight - 30;
      el.textContent = d.log;
      $('logsize').textContent = d.log.split('\n').filter(x => x.trim()).length;
      if (atBottom || logAuto) el.scrollTop = el.scrollHeight;
    }
  });
}

function save() {
  const body = {
    username: $('username').value, service: $('service').value,
    interval: $('interval').value, portal_base: $('portal_base').value,
    campus_ssid: $('campus_ssid').value, password: $('password').value
  };
  api('/api/save', body).then(r => {
    const m = $('save_msg');
    if (r.ok) {
      m.className = 'msg ok';
      m.textContent = '已保存 ✓';
      $('password').value = '';
    } else {
      m.className = 'msg err';
      m.textContent = '保存失败：' + r.error;
    }
    refreshStatus();
  });
}

// 极速启动：走独立接口。勾选后会弹 UAC，真正的任务创建/删除由提权子进程
// 异步完成，所以这里轮询真实状态（最多 30 秒）、以任务是否实际存在为准
// 更新「运行情况」卡片，避免"勾了但卡片一直显示未开启"的不同步。
async function onBootTaskChange() {
  const want = $('boot_task').checked;
  const m = $('save_msg');
  const r = await api('/api/boottask', {enable: want});
  if (!r.ok) {                            // 直接失败（如 UAC 被取消）
    m.className = 'msg err';
    m.textContent = '设置失败：' + (r.message || '未知错误');
    $('boot_task').checked = !want;       // 回滚勾选
    refreshStatus();
    return;
  }
  m.className = 'msg ok';
  m.textContent = want ? '正在等待管理员授权…（请在 UAC 窗口点「是」）' : '正在关闭…';
  let last = null;
  for (let i = 0; i < 30; i++) {          // 每秒查一次真实任务状态
    await new Promise(res => setTimeout(res, 1000));
    last = await api('/api/status');
    setChip($('d_boot'), last.boot_task);
    $('boot_task_state').textContent = last.boot_task ? '已开启' : '未开启';
    if (last.boot_task === want) {
      m.className = 'msg ok';
      m.textContent = want ? '极速启动已开启 ✓（重启电脑后生效）' : '极速启动已关闭';
      refreshStatus();
      return;
    }
  }
  // 超时：按真实状态回滚勾选，提示重试
  m.className = 'msg err';
  m.textContent = '等待授权超时，请重试（注意确认弹出的 UAC 窗口）';
  $('boot_task').checked = last ? last.boot_task : !want;
  refreshStatus();
}

function testLogin() {
  const btn = $('btn_test'), out = $('test_out');
  btn.disabled = true; out.textContent = '探测中…（约几秒）';
  api('/api/test', {}).then(r => {
    let s = '▶ 探测：' + (r.online ? '已联网 ✔' : '未联网 ✘') + '\n' + r.detail + '\n\n';
    if (r.login_tried) s += '▶ 已尝试登录：' + (r.ok ? '成功 ✔' : '失败 ✘') + '\n' + r.response + '\n\n';
    s += '▶ ' + (r.message || '');
    out.textContent = s;
  }).finally(() => { btn.disabled = false; });
}

function daemonCtl(action) {
  api('/api/daemon/' + action, {}).then(r => {
    const m = $('daemon_msg');
    m.className = r.ok ? 'msg ok' : 'msg err';
    m.textContent = r.message || (r.ok ? '完成' : '失败');
    setTimeout(refreshStatus, 900);
  });
}

let _quitDialogOpen = false;
let _quitting = false;   // 已进入退出流程，忽略后续关闭触发，避免重复弹窗

function quitApp() {
  tryQuit();
}

// 条件退出：未开启自启动且后台服务未运行，直接退出；否则弹窗说明哪些功能还开着。
async function tryQuit() {
  if (_quitting) return;        // 正在退出，忽略（含窗口销毁触发的 FormClosing 回调）
  if (_quitDialogOpen) return;
  const s = await api('/api/status');
  if (!s.daemon_running && !s.boot_task) {
    _quitting = true;
    _quitDialogOpen = true;
    confirmQuit();
    return;
  }
  _quitDialogOpen = true;
  const rows = [
    { key: '后台服务', on: s.daemon_running,
      onText: '运行中', offText: '已停止',
      desc: '常驻后台、掉线自动登录' },
    { key: '极速启动', on: s.boot_task,
      onText: '已开启', offText: '已关闭',
      desc: '开机即启动，锁屏时也在后台运行' }
  ];
  let html = '';
  for (const r of rows) {
    html += '<div class="modal-item ' + (r.on ? 'on' : 'off') + '">' +
            '<div class="dot"></div>' +
            '<div class="txt"><div>' + r.key + '</div>' +
            '<div style="font-size:11px;color:var(--muted);margin-top:2px">' + r.desc + '</div></div>' +
            '<div class="tag">' + (r.on ? r.onText : r.offText) + '</div></div>';
  }
  $('exit-info-list').innerHTML = html;
  document.getElementById('exit-info-modal').style.display = 'flex';
}

function hideExitInfoModal() {
  document.getElementById('exit-info-modal').style.display = 'none';
  _quitDialogOpen = false;
}

function confirmQuit() {
  _quitting = true;   // 标记正在退出，destroy 触发的关闭回调不再弹窗
  hideExitInfoModal();
  // 交给后端延迟退出（/api/quit 会先销毁窗口，再结束进程）
  api('/api/quit', {});
}

$('auto_log').addEventListener('change', e => { logAuto = e.target.checked; if (logAuto) refreshLog(); });

refreshStatus(); refreshLog();
setInterval(refreshStatus, 5000);
setInterval(() => { if (logAuto) refreshLog(); }, 4000);
</script>

<!-- 退出信息提示模态框（自启动/后台任一开启时显示） -->
<div class="modal" id="exit-info-modal">
  <div class="modal-mask" onclick="hideExitInfoModal()"></div>
  <div class="modal-box">
    <div class="modal-icon">🔒</div>
    <div class="modal-title">退出设置</div>
    <div class="modal-subtitle">以下功能当前处于开启状态</div>
    <div class="modal-list" id="exit-info-list"></div>
    <div class="modal-hint">关闭设置窗口不会停止后台服务。<br>若不需要后台运行，请先在界面内关闭对应开关。</div>
    <div class="modal-actions">
      <button class="btn modal-cancel" onclick="hideExitInfoModal()">留在设置</button>
      <button class="btn modal-confirm" onclick="confirmQuit()">仍要退出</button>
    </div>
  </div>
</div>
</body>
</html>"""


# ----------------------------------------------------------------------
# 入口
# ----------------------------------------------------------------------
def setup_gui_logging():
    # GUI 自己的日志文件，与后台守护的 daemon-日期.log 分离，避免文件锁冲突崩溃
    os.makedirs(core.LOG_DIR, exist_ok=True)
    logfile = os.path.join(core.LOG_DIR, "gui-%s.log" % datetime.now().strftime("%Y-%m-%d"))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.FileHandler(logfile, encoding="utf-8")],
    )


def idle_watchdog():
    """30 分钟无请求则自动退出设置程序（后台守护不受影响）"""
    while True:
        time.sleep(60)
        if time.time() - _last_activity > 1800:
            log.info("设置界面 30 分钟无操作，自动退出（后台服务不受影响）")
            os._exit(0)


def _show_error(title, message):
    """用 Windows 原生消息框提示错误，避免'双击没反应'。"""
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, message, title, 0x10)  # 0x10 = MB_ICONWARNING
    except Exception:
        log.error("[%s] %s", title, message)


def _install_close_guard(window):
    """移除标题栏关闭按钮，并用 FormClosing 兜底拦截 Alt+F4/任务栏关闭。

    背景：pywebview 的 edgechromium 后端（Windows 默认）不会触发
    window.events.closing，因此点 X 会直接关窗。这里先通过系统菜单删除
    SC_CLOSE（去掉标题栏 ×），再挂上 FormClosing，在 Alt+F4 或任务栏关闭
    前调用 JS 的 tryQuit() 做条件判断（未开自启动/后台则直接退出，否则
    弹窗提示用户手动关闭后再退出）。

    注意：FormClosing 是同步 UI 事件，不能在其中直接 evaluate_js，否则
    会阻塞 WinForms 消息循环导致窗口无响应；改为设置 Cancel 后另起线程
    异步调用 evaluate_js。
    """
    import time
    import ctypes
    TITLE = "校园网自动登录 - 设置"
    user32 = ctypes.windll.user32
    user32.FindWindowW.restype = ctypes.c_void_p
    user32.FindWindowW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]

    # 等待窗体出现（webview.start() 内部异步创建）
    hwnd = 0
    for _ in range(200):  # 最多 ~40s
        try:
            hwnd = user32.FindWindowW(None, TITLE)
        except Exception:
            hwnd = 0
        if hwnd:
            break
        time.sleep(0.2)

    if not hwnd:
        log.warning("[关闭防护] 未找到窗体句柄，关闭拦截未生效")
        return

    try:
        import clr
        clr.AddReference('System.Windows.Forms')
        import System.Windows.Forms as WinForms
        from System import IntPtr

        form = None
        for _ in range(20):  # 句柄刚创建时 WinForms 可能尚未登记，稍作重试
            try:
                form = WinForms.Control.FromHandle(IntPtr(int(hwnd)))
            except Exception:
                form = None
            if form is not None:
                break
            time.sleep(0.3)

        if form is None:
            log.warning("[关闭防护] 无法通过句柄获取窗体对象（重试后仍失败）")
            return

        # 移除标题栏的关闭按钮（SC_CLOSE），保留最小化/最大化/图标
        try:
            user32.GetSystemMenu.restype = ctypes.c_void_p
            user32.GetSystemMenu.argtypes = [ctypes.c_void_p, ctypes.c_bool]
            user32.DeleteMenu.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint]
            user32.DrawMenuBar.argtypes = [ctypes.c_void_p]
            hMenu = user32.GetSystemMenu(ctypes.c_void_p(hwnd), False)
            if hMenu:
                # 0xF060 = SC_CLOSE, MF_BYCOMMAND = 0x00000000
                if user32.DeleteMenu(hMenu, 0xF060, 0):
                    user32.DrawMenuBar(ctypes.c_void_p(hwnd))
                    log.info("[关闭防护] 已移除标题栏关闭按钮（SC_CLOSE）")
                else:
                    log.warning("[关闭防护] 移除标题栏关闭按钮失败")
        except Exception as e:
            log.warning("[关闭防护] 移除标题栏关闭按钮异常：%s", e)

        def on_form_closing(sender, e):
            # 已进入退出流程（用户已点“仍要退出”/无功能开启直接退出）：
            # 不再拦截、不再弹窗，直接放行让窗口销毁，避免“退出弹窗出现两遍”。
            if _QUITTING:
                return
            # 取消本次关闭，由 JS 判断是否符合直接退出条件
            try:
                e.Cancel = True
            except Exception:
                pass
            # FormClosing 里直接 evaluate_js 会阻塞 UI 消息循环导致无响应，
            # 必须放到后台线程异步调用。
            def _ask():
                try:
                    if window is not None:
                        window.evaluate_js("tryQuit()")
                except Exception:
                    pass
            threading.Thread(target=_ask, daemon=True).start()

        form.FormClosing += on_form_closing
        log.info("[关闭防护] 已挂接 FormClosing，Alt+F4/任务栏关闭将走条件判断")
    except Exception as ex:
        log.warning("[关闭防护] 初始化失败：%s", ex)


def main():
    """桌面 App 入口：优先 pywebview 原生窗口，失败兜底浏览器"""
    setup_gui_logging()
    log.info("[启动] 校园网自动登录 v%s 开始启动", core.APP_VERSION)

    # 已有设置窗口在跑 → 直接把它提到前台，而不是再开一个（更不会串到别的站点）
    if _focus_existing():
        return

    # 走到这里说明端口文件要么不存在、要么已失效（上次异常退出留下的）。
    # 先清掉，这样 启动设置.bat 的轮询只会读到本实例刚写入的全新地址，
    # 不会误开一个已经打不开的旧地址。
    _clear_port_file()

    # 旧版迁移：早先版本提供「登录级自启」（登录后启动，免管理员），
    # 现已统一为「极速启动」（开机即启动、锁屏也运行）。
    # 检测到旧登录级任务时自动移除，避免两套自启并存导致重复启动守护。
    if core.autostart_enabled():
        try:
            core.set_autostart(False)
            log.info("[自启] 检测到旧版登录级自启任务，已自动移除（现统一使用极速启动）")
        except OSError as e:
            log.warning("[自启] 清理旧版登录级自启任务失败：%s", e)

    try:
        # 端口传 0：由系统分配一个空闲端口。
        # 固定端口会与用户本地部署的其他网站冲突，是本程序"显示成别的网站"的根因。
        server = LocalServer(("127.0.0.1", 0), Handler)
    except OSError as e:
        log.exception("本地服务启动失败")
        _show_error("校园网自动登录",
                    "无法启动本地设置服务。\n\n错误：%s\n\n"
                    "常见原因：安全软件拦截了本机的本地监听。\n"
                    "可尝试把本程序加入杀毒白名单后重试。" % e)
        return
    port = server.server_address[1]
    url = _write_port_file(port)
    log.info("设置界面服务启动：%s", url)
    # 明确不干预后台：检测到后台服务在运行则只提示，绝不停止/重启它
    if core.is_daemon_running():
        log.info("[设置] 检测到后台服务运行中，设置界面不会停止/重启它，两者互不影响")
    else:
        log.info("[设置] 后台服务未运行（如需常驻，请在界面点「启动后台服务」）")
    threading.Thread(target=idle_watchdog, daemon=True).start()
    threading.Thread(target=server.serve_forever, daemon=True).start()

    opened = False
    try:
        # 桌面原生窗口（基于系统 WebView2 渲染）
        import webview

        window = None  # 供 closing 回调使用

        def _on_closing():
            """窗口关闭事件兜底：交给 JS 做条件退出判断。"""
            # 必须在异步线程里 evaluate_js：closing 是同步事件，在事件处理中直接
            # 调用 JS 会阻塞 UI 消息循环，导致窗口无响应（假死）。
            def _ask():
                try:
                    if window is not None:
                        window.evaluate_js("tryQuit()")
                except Exception:
                    pass
            threading.Thread(target=_ask, daemon=True).start()
            return False  # 取消默认关闭，由 JS 决定是否退出

        window = webview.create_window("校园网自动登录 - 设置", url,
                                       width=1180, height=760, min_size=(1000, 680))
        global _main_window
        _main_window = window
        window.events.closing += _on_closing
        # edgechromium 后端不触发 closing 事件，用 WinForms FormClosing 兜底拦截 Alt+F4/任务栏关闭
        threading.Thread(target=_install_close_guard, args=(window,), daemon=True).start()
        # 将 WebView2 用户数据目录固定到 %LOCALAPPDATA%\校园网自动登录_WebView2，
        # 卸载时可统一清理，避免缓存散落在默认 pywebview 目录下。
        webview_storage = os.path.join(
            os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
            "校园网自动登录_WebView2")
        webview.start(storage_path=webview_storage)
        opened = True
        log.info("桌面窗口已关闭；后台服务（如已启动）继续独立运行，不受设置界面关闭影响")
    except Exception as e:
        log.warning("无法打开桌面窗口（%s），改用浏览器兜底", e)
        _show_error("校园网自动登录",
                    "无法启动设置窗口（缺少 WebView2 或被杀毒拦截）。\n\n"
                    "已改用浏览器打开本机设置页。\n\n"
                    "如频繁出现，可安装/更新 Edge WebView2 运行库，或将本程序加入杀毒白名单。")
        try:
            import webbrowser
            webbrowser.open(url)
            opened = True
            server.serve_forever()
        except Exception:
            pass

    if opened:
        try:
            server.shutdown()
        except Exception:
            pass
        # 给 pywebview 一点时间清理浏览器进程，避免关闭时挂住
        import time
        time.sleep(0.3)
    _clear_port_file()


if __name__ == "__main__":
    # 源码模式下由 UAC 提权后的子进程执行（创建/删除开机级计划任务需管理员）
    if "--enable-boot-task" in sys.argv:
        ok, msg = core.set_boot_task(True)
        print(msg)
        sys.exit(0 if ok else 1)
    if "--disable-boot-task" in sys.argv:
        ok, msg = core.set_boot_task(False)
        print(msg)
        sys.exit(0 if ok else 1)
    main()
