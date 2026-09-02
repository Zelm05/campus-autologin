package com.campus.autologin.service

import android.content.Context
import com.campus.autologin.data.repository.AuthRepository

/**
 * 后台常驻的统一开关。
 *
 * 任何"配置可能变了"的地方（开机、更新包、打开 App、保存设置、切换开关）
 * 都调这里，保证「前台服务」与「WorkManager 兜底任务」始终与配置一致，
 * 不会出现"服务没起来但兜底也没注册"这种两头落空的状态。
 */
object KeepAlive {

    /** 后台监控是否应当处于开启状态。 */
    fun shouldRun(ctx: Context): Boolean {
        val cfg = runCatching { AuthRepository(ctx.applicationContext).loadConfig() }
            .getOrNull() ?: return false
        return cfg.autoLogin || cfg.alwaysRunInBackground
    }

    fun sync(ctx: Context) {
        val app = ctx.applicationContext
        if (shouldRun(app)) {
            MonitorWorker.enable(app)
            AutoLoginService.start(app)
        } else {
            MonitorWorker.disable(app)
            AutoLoginService.stop(app)
        }
    }
}
