package com.campus.autologin.util

import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.os.PowerManager
import android.provider.Settings

/**
 * 电池优化豁免。
 *
 * 这是"开了自启动还是会掉后台"里最常被忽略的一环：
 * 只要 App 还在电池优化名单里，系统会在熄屏一段时间后冻结进程，
 * 网络回调收不到、周期任务被推迟，看起来就是"自启动没生效"。
 *
 * 国产 ROM 还有各自的「自启动管理 / 后台冻结 / 神隐模式」，
 * 那些没有公开 API，只能把用户送到应用详情页让他手动加白名单。
 */
object BatteryOpt {

    /** 是否已被加入电池优化白名单（Android 6 以下恒为 true）。 */
    fun isIgnoring(ctx: Context): Boolean {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.M) return true
        val pm = ctx.getSystemService(Context.POWER_SERVICE) as? PowerManager ?: return false
        return runCatching { pm.isIgnoringBatteryOptimizations(ctx.packageName) }.getOrDefault(false)
    }

    /** 弹出系统的「允许后台活动」确认框；失败时退回应用详情页。 */
    fun requestIgnore(ctx: Context) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.M || isIgnoring(ctx)) return
        val intent = Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS)
            .setData(Uri.parse("package:${ctx.packageName}"))
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        if (!start(ctx, intent)) openAppDetails(ctx)
    }

    /** 打开本应用的系统详情页（自启动、省电策略、通知都在这里调）。 */
    fun openAppDetails(ctx: Context) {
        val intent = Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS)
            .setData(Uri.parse("package:${ctx.packageName}"))
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        start(ctx, intent)
    }

    private fun start(ctx: Context, intent: Intent): Boolean = runCatching {
        // 先确认真的有 Activity 能接，否则部分 ROM 会直接崩
        if (intent.resolveActivity(ctx.packageManager) == null) return false
        ctx.startActivity(intent)
        true
    }.getOrDefault(false)
}
