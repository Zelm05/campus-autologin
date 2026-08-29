# -*- coding: utf-8 -*-
"""
daemon.py —— 后台守护进程（无任何窗口/弹窗）
====================================================================
- 源码模式：由 启动设置.bat 或注册表自启用 pythonw 运行本文件
- exe 模式：同一 exe 加 --daemon 参数运行（见 app.py 入口）

工作循环：
  1. 每隔 interval 秒探测一次联网状态（多级探针链）
  2. 已联网 → 静默跳过（仅状态变化记日志，在线每小时一条心跳）
  3. 掉线 → 从门户重定向捕获 queryString，自动调登录接口
  4. 登录成功 → 复测确认；失败 → 记录服务器原始响应，退避重试

日志：logs/daemon-YYYY-MM-DD.log（按天分文件，自动清理30天前的旧日志）
"""

import logging
import os
import sys
import time
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import campus_core as core  # noqa: E402


# ----------------------------------------------------------------------
# 日志
# ----------------------------------------------------------------------
def setup_logging():
    os.makedirs(core.LOG_DIR, exist_ok=True)
    logfile = os.path.join(core.LOG_DIR, "daemon-%s.log" % datetime.now().strftime("%Y-%m-%d"))
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s.%(msecs)03d [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.FileHandler(logfile, encoding="utf-8")],
    )
    # 清理 30 天前的旧日志
    try:
        cutoff = datetime.now() - timedelta(days=30)
        for name in os.listdir(core.LOG_DIR):
            if name.startswith("daemon-") and name.endswith(".log"):
                try:
                    d = datetime.strptime(name[7:17], "%Y-%m-%d")
                    if d < cutoff:
                        os.remove(os.path.join(core.LOG_DIR, name))
                except ValueError:
                    pass
    except Exception:
        pass


log = logging.getLogger("daemon")


# ----------------------------------------------------------------------
# 登录流程（带重试与完整日志）
# ----------------------------------------------------------------------
def attempt_login(cfg, qs, portal_base, reason="掉线"):
    username = cfg.get("username", "")
    password = core.get_password(cfg)
    service = cfg.get("service", "")
    max_retries = int(cfg.get("max_retries", 5))
    retry_delay = int(cfg.get("retry_delay", 3))

    log.info("=" * 60)
    log.info("[登录] 开始自动登录（触发原因：%s）", reason)
    log.info("[登录] 账号=%s 密码=%s | 服务=%s | 服务器=%s",
             core.mask_username(username) or "<空>", core.mask_secret(password),
             service or "<空>", portal_base)
    log.info("[登录] 初始 queryString=%s", qs[:200] + ("..." if len(qs) > 200 else ""))

    if not username or not password:
        log.error("[登录] 用户名或密码为空，请打开设置界面填写后重试")
        core.write_status(last_error="用户名或密码为空", state="配置缺失")
        return False

    # 关键：每次重试前重新探测，获取最新的 queryString。
    # 原因：服务器对同一 wlanuserip 连续重试会持续返回"用户不存在"，
    # 但 AC 在用户重连后会下发新的 wlanuserip，旧的 QS 已失效。
    current_qs = qs
    current_portal_base = portal_base
    for i in range(1, max_retries + 1):
        # 第 2 次起每次都重新探测
        if i > 1:
            log.info("[登录] 第 %d/%d 次前重新探测网络...", i, max_retries)
            r = core.probe(cfg.get("probe_urls"))
            if r.online:
                log.info("[登录] 第 %d 次：探测显示已联网，无需登录", i)
                core.write_status(state="已联网")
                return True
            if not r.query_string:
                log.warning("[登录] 第 %d 次：未捕获到门户参数，跳过本次", i)
                if i < max_retries:
                    time.sleep(retry_delay)
                continue
            current_qs = r.query_string
            current_portal_base = r.portal_base or cfg.get("portal_base", "")
            log.info("[登录] 第 %d/%d 次：新 queryString=%s",
                     i, max_retries, current_qs[:200] + ("..." if len(current_qs) > 200 else ""))

        ok, resp_text, data, code, err = core.eportal_login(
            current_portal_base, current_qs, username, password, service)
        if err:
            log.warning("[登录] 第 %d/%d 次：请求异常 -> %s (HTTP %s)", i, max_retries, err, code)
        else:
            log.info("[登录] 第 %d/%d 次：HTTP %s 响应=%s", i, max_retries, code, resp_text[:500])
        if ok:
            log.info("[登录] >>> 成功！userIndex=%s keepaliveInterval=%s",
                     data.get("userIndex", ""), data.get("keepaliveInterval", ""))
            # 稍等后复测，确认真正联网
            time.sleep(2)
            r = core.probe(cfg.get("probe_urls"))
            log.info("[登录] 复测结果：%s（%s）", "已联网" if r.online else "仍未联网", r.detail)
            core.write_status(
                state="已联网" if r.online else "登录成功但复测未联网",
                last_login=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                last_error="",
                login_count=int(core.read_status().get("login_count", 0)) + 1,
            )
            return r.online
        # 失败：解析服务器消息并记入状态
        msg = data.get("message", "") if isinstance(data, dict) else ""
        if msg:
            log.warning("[登录] 服务器返回失败原因：%s", msg)
        core.write_status(last_error="登录失败: %s" % (msg or resp_text[:80] or err or code))
        if i < max_retries:
            log.info("[登录] %d 秒后进行第 %d 次重试...", retry_delay, i + 1)
            time.sleep(retry_delay)
    log.error("[登录] 自动登录最终失败，已重试 %d 次，等待下轮探测再试", max_retries)
    return False


# ----------------------------------------------------------------------
# 主循环
# ----------------------------------------------------------------------
def main_loop():
    setup_logging()
    core.write_pid()
    log.info("=" * 60)
    log.info("[系统] %s v%s 守护进程启动 (pid=%s, Python %s)",
             core.APP_NAME, core.APP_VERSION, os.getpid(), sys.version.split()[0])
    cfg0 = core.load_config()
    log.info("[配置] 账号=%s | 服务=%s | 检测间隔=%ss | 服务器=%s",
             core.mask_username(cfg0.get("username")) or "<未设置>",
             cfg0.get("service") or "<空>", cfg0.get("interval", 15),
             cfg0.get("portal_base", core.DEFAULT_PORTAL_BASE))
    log.info("[配置] 探针链：%s", ", ".join(cfg0.get("probe_urls") or core.DEFAULT_PROBE_URLS))
    # 启动时若配置了校园网 SSID 且未连接，自动连接
    ready, wifi_msg = core.ensure_campus_wifi(cfg0)
    log.info("[WiFi] %s", wifi_msg)
    log.info("=" * 60)

    last_state = None          # online / offline / unreachable
    heartbeat_at = 0.0         # 在线心跳：每小时记一条，证明进程活着
    login_ok_until = 0.0       # 登录成功后 60 秒内不再触发重复登录

    try:
        while True:
            cfg = core.load_config()
            interval = max(5, int(cfg.get("interval", 15)))

            r = core.probe(cfg.get("probe_urls"))
            now = time.time()

            if r.online:
                if last_state != "online":
                    log.info("[网络] 已联网（%s），无需登录", r.detail)
                elif now - heartbeat_at > 3600:
                    log.debug("[网络] 在线心跳（每小时一条，仅确认进程存活）")
                    heartbeat_at = now
                core.write_status(state="已联网")
                last_state = "online"
            elif not r.query_string:
                # 连门户参数都拿不到：多半是 WiFi 断开/信号问题
                if last_state != "unreachable":
                    log.info("[网络] 无法联网且未捕获到门户跳转（%s），可能是 WiFi 未连接，继续等待", r.detail)
                core.write_status(state="网络不可达（未捕获门户参数）", last_error=r.detail)
                last_state = "unreachable"
            else:
                if now > login_ok_until:
                    success = attempt_login(cfg, r.query_string, r.portal_base or cfg.get("portal_base", ""))
                    if success:
                        login_ok_until = now + 60
                        last_state = "online"
                else:
                    log.debug("[网络] 登录冷却期内，跳过本轮（60秒防重复）")
                last_state = "offline"

            time.sleep(interval)
    except KeyboardInterrupt:
        log.info("收到退出信号，守护进程结束")
    except Exception:
        # 兜底：任何未预期异常都完整写入日志，绝不弹窗
        log.exception("守护进程发生未捕获异常！")
        core.write_status(state="异常退出", last_error="守护进程发生未捕获异常，详见日志")
    finally:
        core.remove_pid()


def run_once():
    """单次模式：探测一轮 + 必要时登录一次（调试用，exe 的 --once 也走这里）"""
    setup_logging()
    core.write_pid()
    cfg = core.load_config()
    r = core.probe(cfg.get("probe_urls"))
    if r.online:
        log.info("[单次模式] 当前已联网，无需登录")
        core.write_status(state="已联网")
    elif r.query_string:
        attempt_login(cfg, r.query_string, r.portal_base or cfg.get("portal_base", ""), reason="单次测试")
    else:
        log.warning("[单次模式] 未联网且未捕获门户参数：%s", r.detail)
        core.write_status(state="网络不可达（未捕获门户参数）", last_error=r.detail)
    core.remove_pid()


if __name__ == "__main__":
    if "--once" in sys.argv:
        run_once()
    else:
        main_loop()
