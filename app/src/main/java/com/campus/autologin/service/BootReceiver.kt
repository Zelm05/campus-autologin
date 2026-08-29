package com.campus.autologin.service

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import com.campus.autologin.data.repository.AuthRepository

/**
 * Starts the background auto-login monitor after the device boots (or after the
 * app is updated), but only if the user enabled "开机自启".
 */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        val action = intent.action
        if (action == Intent.ACTION_BOOT_COMPLETED ||
            action == Intent.ACTION_MY_PACKAGE_REPLACED
        ) {
            val repo = AuthRepository(context.applicationContext)
            val cfg = repo.loadConfig()
            // 开机自启开启，或要求"始终在后台运行"时，开机都拉起后台监控
            if (cfg.autoStartOnBoot || cfg.alwaysRunInBackground) {
                runCatching {
                    AutoLoginService.start(context.applicationContext)
                }
            }
        }
    }
}
