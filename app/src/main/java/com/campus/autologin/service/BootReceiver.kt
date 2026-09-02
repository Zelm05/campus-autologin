package com.campus.autologin.service

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

/**
 * 开机 / 覆盖安装后恢复后台监控。
 *
 * 两条腿走路：
 *  1. 直接拉起前台服务（Android 12+ 明确允许在 BOOT_COMPLETED 接收器里启动前台服务）；
 *  2. 重新注册 WorkManager 兜底任务——它会持久化在自己的数据库里，
 *     即使这次开机广播被厂商 ROM 拦掉，WorkManager 也会自行恢复。
 */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        when (intent.action) {
            Intent.ACTION_BOOT_COMPLETED,
            Intent.ACTION_MY_PACKAGE_REPLACED,
            ACTION_QUICKBOOT_POWERON -> KeepAlive.sync(context)
        }
    }

    private companion object {
        /** 部分 MTK / 旧 HTC 设备开机只发这个非标准广播。 */
        const val ACTION_QUICKBOOT_POWERON = "android.intent.action.QUICKBOOT_POWERON"
    }
}
