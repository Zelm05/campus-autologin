package com.campus.autologin.service

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.net.ConnectivityManager
import android.net.Network
import android.net.NetworkCapabilities
import android.net.NetworkRequest
import android.os.Build
import android.os.IBinder
import androidx.core.app.NotificationCompat
import androidx.core.content.ContextCompat
import com.campus.autologin.R
import com.campus.autologin.data.model.LoginResult
import com.campus.autologin.data.repository.AuthRepository
import com.campus.autologin.ui.MainActivity
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch

/**
 * Foreground service that watches for network changes and triggers a login
 * attempt whenever a new internet-capable network appears (e.g. you join the
 * campus Wi-Fi). Also re-logs in when the user manually logs out in the
 * browser or the network loses validated state.
 */
class AutoLoginService : android.app.Service() {

    private lateinit var repo: AuthRepository
    private var callback: ConnectivityManager.NetworkCallback? = null
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private var loginJob: Job? = null
    private var periodicJob: Job? = null
    @Volatile private var lastValidated: Boolean = false

    override fun onCreate() {
        super.onCreate()
        repo = AuthRepository(applicationContext)
        createChannel()
        startForegroundSafe()
        registerNetworkCallback()
        startPeriodicCheck()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == ACTION_REFRESH) {
            val cfg = repo.loadConfig()
            if (!cfg.autoLogin && !cfg.alwaysRunInBackground) {
                stopSelf()
                return START_NOT_STICKY
            }
            val mgr = getSystemService(NOTIFICATION_SERVICE) as NotificationManager
            runCatching { mgr.notify(NOTIF_ID, buildNotification()) }
            return START_STICKY
        }
        if (repo.loadConfig().alwaysRunInBackground) {
            startForegroundSafe()
        }
        return START_STICKY
    }

    private fun startForegroundSafe() {
        val notification = buildNotification()
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                startForeground(NOTIF_ID, notification, ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC)
            } else {
                startForeground(NOTIF_ID, notification)
            }
        } catch (_: Exception) {
            // 部分 ROM / Android 12+ 后台启动限制下 startForeground 可能抛异常。
        }
    }

    private fun registerNetworkCallback() {
        val cm = getSystemService(CONNECTIVITY_SERVICE) as ConnectivityManager
        val request = NetworkRequest.Builder()
            .addCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET)
            .build()

        callback = object : ConnectivityManager.NetworkCallback() {
            override fun onAvailable(network: Network) {
                // 新网络出现（含 WiFi 重连/切换）：等 DHCP/DNS 稳定后登录
                loginJob?.cancel()
                loginJob = scope.launch {
                    delay(2500)
                    runLogin()
                }
            }

            override fun onCapabilitiesChanged(network: Network, caps: NetworkCapabilities) {
                // 网络能力变化（如浏览器手动下线/会话过期）：
                // 若之前"已联网校验通过"现在不再通过，触发一次重登
                val validated = caps.hasCapability(NetworkCapabilities.NET_CAPABILITY_VALIDATED)
                if (lastValidated && !validated && repo.loadConfig().autoLogin) {
                    loginJob?.cancel()
                    loginJob = scope.launch {
                        delay(3000)
                        runLogin()
                    }
                }
                lastValidated = validated
            }
        }
        try {
            cm.registerNetworkCallback(request, callback!!)
        } catch (_: Exception) {
            // 部分 ROM 限制；手动登录仍可用
        }
    }

    /**
     * 周期自检：每 60 秒探一次状态，若"需登录/认证"且开启自动登录则补登。
     * 兜底 onCapabilitiesChanged 漏掉的情况（部分 ROM 不回调能力变化）。
     */
    private fun startPeriodicCheck() {
        periodicJob?.cancel()
        periodicJob = scope.launch {
            while (isActive) {
                delay(PERIODIC_CHECK_MS)
                // 只对"开启自动登录"的用户补登；仅后台保活时不打扰
                if (!repo.loadConfig().autoLogin) continue
                runCatching { repo.probeState() }
                    .onSuccess { state ->
                        if (state == com.campus.autologin.data.model.ConnectionState.CAPTIVE) {
                            loginJob?.cancel()
                            loginJob = scope.launch { runLogin() }
                        }
                    }
            }
        }
    }

    private suspend fun runLogin() {
        val cfg = repo.loadConfig()
        // 统一入口把关：未开启"自动登录"（只开了后台保活）时不做任何登录动作
        if (!cfg.autoLogin) return
        // 手动下线冷却期：App 内点"下线"后 5 分钟内不自动补登，
        // 让"下线"按钮真正生效；浏览器下线无此标记，会被立即补登
        if (repo.isInManualLogoutCooldown()) return
        val result = runCatching { repo.login() }.getOrElse {
            LoginResult.Error("自动登录异常：${it.message}")
        }
        val mgr = getSystemService(NOTIFICATION_SERVICE) as NotificationManager
        when (result) {
            is LoginResult.Success -> {
                mgr.notify(NOTIF_ID, buildNotification("已自动登录，监控中…"))
            }
            else -> {
                if (cfg.notifyOnFailure) {
                    val text = when (result) {
                        is LoginResult.Failure -> "自动登录未成功：${result.message}"
                        else -> "自动登录异常：${result.message}"
                    }
                    mgr.notify(NOTIF_ID, buildNotification(text))
                }
            }
        }
    }

    override fun onTaskRemoved(rootIntent: Intent?) {
        if (repo.loadConfig().alwaysRunInBackground) {
            runCatching {
                val intent = Intent(this, AutoLoginService::class.java)
                ContextCompat.startForegroundService(this, intent)
            }
        }
        super.onTaskRemoved(rootIntent)
    }

    override fun onDestroy() {
        loginJob?.cancel()
        periodicJob?.cancel()
        scope.cancel()
        callback?.let {
            (getSystemService(CONNECTIVITY_SERVICE) as ConnectivityManager)
                .unregisterNetworkCallback(it)
        }
        callback = null
        super.onDestroy()
    }

    override fun onBind(intent: Intent?): IBinder? = null

    private fun buildNotification(text: String = ""): Notification {
        val cfg = repo.loadConfig()
        val showFull = cfg.showMonitorNotification
        val contentIntent = PendingIntent.getActivity(
            this,
            0,
            Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE
        )
        val builder = NotificationCompat.Builder(this, CHANNEL_ID)
            .setSmallIcon(R.drawable.ic_stat_wifi)
            .setContentIntent(contentIntent)
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setSilent(!showFull)
        if (showFull) {
            builder.setContentTitle("校园网自动登录")
            builder.setContentText(text.ifBlank { "监控中…" })
        } else {
            builder.setContentTitle(getString(R.string.app_name))
            builder.setContentText(" ")
        }
        return builder.build()
    }

    private fun createChannel() {
        val mgr = getSystemService(NOTIFICATION_SERVICE) as NotificationManager
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val channel = NotificationChannel(
                CHANNEL_ID,
                getString(R.string.notification_channel_name),
                NotificationManager.IMPORTANCE_LOW
            )
            channel.setShowBadge(false)
            mgr.createNotificationChannel(channel)
        }
    }

    companion object {
        private const val NOTIF_ID = 1001
        private const val CHANNEL_ID = "auto_login_channel"
        private const val ACTION_REFRESH = "com.campus.autologin.action.REFRESH_NOTIF"
        private const val PERIODIC_CHECK_MS = 60_000L  // 每 60 秒兜底探一次

        fun start(ctx: Context) {
            runCatching {
                ContextCompat.startForegroundService(
                    ctx.applicationContext,
                    Intent(ctx.applicationContext, AutoLoginService::class.java)
                )
            }
        }

        fun stop(ctx: Context) {
            ctx.applicationContext.stopService(
                Intent(ctx.applicationContext, AutoLoginService::class.java)
            )
        }

        fun refreshNotification(ctx: Context) {
            runCatching {
                val intent = Intent(ctx.applicationContext, AutoLoginService::class.java)
                    .setAction(ACTION_REFRESH)
                ctx.applicationContext.startService(intent)
            }
        }
    }
}
