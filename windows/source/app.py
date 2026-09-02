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


def _report(ok, msg):
    """提权子进程的结果回显：既打印（便于排查），也弹窗（否则窗口一闪而过）"""
    print(msg)
    try:
        import ctypes
        # 0x40 = MB_ICONINFORMATION / 0x10 = MB_ICONWARNING
        ctypes.windll.user32.MessageBoxW(None, msg, "校园网自动登录",
                                         0x40 if ok else 0x10)
    except Exception:
        pass


def main():
    # 以下两个分支由 UAC 提权后的子进程执行（创建/删除开机级计划任务需要管理员）
    # 结果写入固定文件（_ELEVATED_RESULT_FILE），供非管理员父进程同步读取。
    # 注意：runas 提权后的子进程不继承父进程运行时设置的环境变量，因此不能用
    # 环境变量/命令行参数传结果文件路径，父子进程统一读写同一个固定文件。
    if "--enable-boot-task" in sys.argv:
        import campus_core as core
        try:
            ok, msg = core.set_boot_task(True)
        except Exception as e:
            ok, msg = False, "极速启动开启异常：%s" % e
        print(msg)
        core.write_elevated_result(ok, msg)
        sys.exit(0 if ok else 1)
    elif "--disable-boot-task" in sys.argv:
        import campus_core as core
        try:
            ok, msg = core.set_boot_task(False)
        except Exception as e:
            ok, msg = False, "极速启动关闭异常：%s" % e
        print(msg)
        core.write_elevated_result(ok, msg)
        sys.exit(0 if ok else 1)
    elif "--daemon" in sys.argv:
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
