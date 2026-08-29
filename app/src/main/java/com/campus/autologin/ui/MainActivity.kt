package com.campus.autologin.ui

import android.Manifest
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat
import com.campus.autologin.CampusApp
import com.campus.autologin.service.AutoLoginService
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

        // Start the background monitor if auto-login or always-run-in-background is enabled.
        val repo = (application as CampusApp).repository
        val cfg = repo.loadConfig()
        if (cfg.autoLogin || cfg.alwaysRunInBackground) {
            runCatching { AutoLoginService.start(this) }
        }
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
