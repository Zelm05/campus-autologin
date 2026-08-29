# -*- coding: utf-8 -*-
"""
app.py —— exe 统一入口
================================================
双击 exe           → 打开桌面设置窗口
exe --daemon       → 后台静默守护（开机自启用，无窗口）
exe --once         → 单次调试模式
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def main():
    if "--daemon" in sys.argv:
        # 后台守护：绝不弹窗
        import daemon
        daemon.main_loop()
    elif "--once" in sys.argv:
        import daemon
        daemon.run_once()
    elif "--stop-daemon" in sys.argv:
        # 停止后台守护（供安装/卸载流程调用，无窗口、无副作用）
        import campus_core as core
        ok, msg = core.stop_daemon()
        print(msg)
        sys.exit(0)
    else:
        import gui
        gui.main()


if __name__ == "__main__":
    main()
