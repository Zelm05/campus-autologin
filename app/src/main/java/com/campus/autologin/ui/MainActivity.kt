package com.campus.autologin.ui

import android.Manifest
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat
import com.campus.autologin.service.KeepAlive
import com.campus.autologin.ui.navigation.AppRoot
import com.campus.autologin.ui.theme.CampusTheme

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        requestPostNotifications()

        setContent {
            CampusTheme {
                AppRoot()
            }
        }

        // 每次打开 App 都重新对齐后台状态：
        // 前台服务 + WorkManager 兜底任务一起按最新配置同步，
        // 顺带把"服务被杀了但用户又打开了 App"这种情况自动修复回来。
        KeepAlive.sync(this)
    }

    private fun requestPostNotifications() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            if (ContextCompat.checkSelfPermission(
                    this,
                    Manifest.permission.POST_NOTIFICATIONS
                ) != PackageManager.PERMISSION_GRANTED
            ) {
                ActivityCompat.requestPermissions(
                    this,
                    arrayOf(Manifest.permission.POST_NOTIFICATIONS),
                    1
                )
            }
        }
    }
}
