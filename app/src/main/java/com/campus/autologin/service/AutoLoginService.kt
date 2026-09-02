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
import com.campus.autologin.util.ErrorText
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
 *
 * 注意：Android 12+ 禁止从后台启动前台服务，国产 ROM 还会主动清理长驻服务。
 * 这个服务**不再被当作唯一保活手段**——它活着时负责即时响应，
 * 一旦被杀由 MonitorWorker（WorkManager 加急任务）接管并把它拉回来。
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
        isRunning = true
        // 兜底任务常驻：服务被杀后由它接管（幂等，重复 enqueue 会 UPDATE 而不是叠加）
        MonitorWorker.enable(this)
        if (!startForegroundSafe()) return
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
            runCatching {
                val mgr = getSystemService(NOTIFICATION_SERVICE) as NotificationManager
                mgr.notify(NOTIF_ID, buildNotification())
            }
            return START_STICKY
        }
        if (repo.loadConfig().alwaysRunInBackground) {
            startForegroundSafe()
        }
        return START_STICKY
    }

    /**
     * @return 是否成功进入前台。false 表示被系统拒绝（Android 12+ 后台启动限制），
     *         此时必须主动 stopSelf，否则系统会在几秒后以
     *         "Context.startForegroundService() did not then call Service.startForeground()"
     *         为由杀掉整个进程——这正是"开了自启动却还是掉后台"的典型表现。
     */
    private fun startForegroundSafe(): Boolean {
        val notification = buildNotification()
        return try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                startForeground(
                    NOTIF_ID,
                    notification,
                    ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC
                )
            } else {
                startForeground(NOTIF_ID, notification)
            }
            lastStartError = null
            true
        } catch (t: Throwable) {
            lastStartError = ErrorText.of(t)
            // 进不了前台就别硬撑：交给 WorkManager 复活，避免进程被系统判死
            MonitorWorker.scheduleRevive(this, delayMinutes = 1)
            stopSelf()
            false
        }
    }

    private fun registerNetworkCallback() {
        val cm = getSystemService(CONNECTIVITY_SERVICE) as ConnectivityManager
        // 只监听 WiFi 网络：连上即回调（未认证的校园 WiFi 可能没有
        // INTERNET capability，所以不能按 INTERNET 过滤，否则会漏掉重连事件）
        val request = NetworkRequest.Builder()
            .addTransportType(NetworkCapabilities.TRANSPORT_WIFI)
            .build()

        callback = object : ConnectivityManager.NetworkCallback() {
            override fun onAvailable(network: Network) {
                // 新网络出现（含 WiFi 重连/切换）：视为一次全新的连接。
                // 等 DHCP/DNS 稳定后，先看状态：已经在线的（如断网重连，
                // 会话还在）不重复登录，避免把在线会话踢掉重建
                loginJob?.cancel()
                loginJob = scope.launch {
                    delay(2500)
                    val state = runCatching { repo.probeState() }
                        .getOrDefault(com.campus.autologin.data.model.ConnectionState.UNKNOWN)
                    if (state == com.campus.autologin.data.model.ConnectionState.CAPTIVE) {
                        runLogin()
                    }
                }
            }

            override fun onCapabilitiesChanged(network: Network, caps: NetworkCapabilities) {
                // 网络能力变化（如浏览器手动下线/会话过期）：
                // 若之前"已联网校验通过"现在不再通过，触发一次重登
                if (!caps.hasTransport(NetworkCapabilities.TRANSPORT_WIFI)) return
                val validated = caps.hasCapability(NetworkCapabilities.NET_CAPABILITY_VALIDATED)
                if (lastValidated && !validated && repo.loadConfig().autoLogin) {
                    loginJob?.cancel()
                    loginJob = scope.launch {
                        delay(3000)
                        // 先确认状态，再决定要不要登录。
                        // 这里原本是直接 runLogin()：免认证网络（OPEN）的校验状态
                        // 只要抖一下（validated 由 true 变 false），就会在一个根本
                        // 不需要认证的网络里尝试登录，然后报错。
                        // 现在与 onAvailable 保持一致：只有真的"需认证"才补登。
                        val state = runCatching { repo.probeState() }
                            .getOrDefault(
                                com.campus.autologin.data.model.ConnectionState.UNKNOWN
                            )
                        if (state == com.campus.autologin.data.model.ConnectionState.CAPTIVE) {
                            runLogin()
                        }
                    }
                }
                lastValidated = validated
            }
        }
        try {
            cm.registerNetworkCallback(request, callback!!)
        } catch (_: Exception) {
            // 部分 ROM 限制；手动登录与 WorkManager 兜底仍然可用
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
        // 已取消"手动下线冷却期"：任何需登录状态都会自动补登
        // 与 WorkManager 兜底链路共用节流闸门，避免同一时刻重复打网关
        if (!AutoLoginGate.tryAcquire()) return

        val result = runCatching { repo.login() }.getOrElse {
            LoginResult.Error(ErrorText.of(it))
        }
        when (result) {
            is LoginResult.Success -> {
                runCatching { repo.fetchOnlineInfoRetry() }
                postNotice(this, "已自动登录，监控中…")
                // 通知主界面立即刷新（浏览器下线/会话过期等场景，
                // 系统可能长时间不回调能力变化，UI 需要主动拉一把）
                onAutoLogin?.invoke()
            }
            else -> {
                if (cfg.notifyOnFailure) {
                    val text = when (result) {
                        is LoginResult.Failure -> "自动登录未成功：${result.message}"
                        else -> "自动登录异常：${result.message}"
                    }
                    postNotice(this, text)
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
            // Android 12+ 上面那步多半会被拒，再用加急任务兜底（允许从后台启动）
            MonitorWorker.scheduleRevive(this, delayMinutes = 0)
        }
        super.onTaskRemoved(rootIntent)
    }

    override fun onDestroy() {
        isRunning = false
        loginJob?.cancel()
        periodicJob?.cancel()
        scope.cancel()
        callback?.let {
            runCatching {
                (getSystemService(CONNECTIVITY_SERVICE) as ConnectivityManager)
                    .unregisterNetworkCallback(it)
            }
        }
        callback = null
        // 不是用户主动关闭（配置仍要求常驻）→ 安排一次快速复活
        if (shouldRun()) MonitorWorker.scheduleRevive(this, delayMinutes = 1)
        super.onDestroy()
    }

    private fun shouldRun(): Boolean {
        val cfg = runCatching { repo.loadConfig() }.getOrNull() ?: return false
        return cfg.autoLogin || cfg.alwaysRunInBackground
    }

    override fun onBind(intent: Intent?): IBinder? = null

    private fun buildNotification(text: String = ""): Notification {
        val cfg = repo.loadConfig()
        return buildNotification(this, cfg, text)
    }

    private fun createChannel() {
        createChannel(this, getSystemService(NOTIFICATION_SERVICE) as NotificationManager)
    }

    companion object {
        private const val NOTIF_ID = 1001
        private const val CHANNEL_ID = "auto_login_channel"
        private const val ACTION_REFRESH = "com.campus.autologin.action.REFRESH_NOTIF"
        private const val PERIODIC_CHECK_MS = 60_000L  // 每 60 秒兜底探一次

        /** 服务是否正在运行（进程内标志，供 WorkManager 判断要不要拉起）。 */
        @Volatile
        var isRunning: Boolean = false
            private set

        /** 自动登录成功后的进程内回调（主界面注册，用于即时刷新 UI）。 */
        @Volatile
        var onAutoLogin: (() -> Unit)? = null

        /** 上一次进入前台失败的原因，设置页会显示出来。 */
        @Volatile
        var lastStartError: String? = null
            private set

        fun start(ctx: Context) {
            runCatching {
                ContextCompat.startForegroundService(
                    ctx.applicationContext,
                    Intent(ctx.applicationContext, AutoLoginService::class.java)
                )
            }
        }

        fun stop(ctx: Context) {
            val app = ctx.applicationContext
            MonitorWorker.disable(app)
            runCatching {
                app.stopService(Intent(app, AutoLoginService::class.java))
            }
        }

        fun refreshNotification(ctx: Context) {
            runCatching {
                val app = ctx.applicationContext
                val intent = Intent(app, AutoLoginService::class.java).setAction(ACTION_REFRESH)
                app.startService(intent)
            }
        }

        /** 从任意上下文更新常驻通知（加急任务里也要用）。 */
        fun postNotice(ctx: Context, text: String) {
            runCatching {
                val app = ctx.applicationContext
                val cfg = AuthRepository(app).loadConfig()
                val mgr = app.getSystemService(NOTIFICATION_SERVICE) as NotificationManager
                createChannel(app, mgr)
                mgr.notify(NOTIF_ID, buildNotification(app, cfg, text))
            }
        }

        private fun buildNotification(
            ctx: Context,
            cfg: com.campus.autologin.data.model.LoginConfig,
            text: String
        ): Notification {
            val showFull = cfg.showMonitorNotification
            val contentIntent = PendingIntent.getActivity(
                ctx,
                0,
                Intent(ctx, MainActivity::class.java),
                PendingIntent.FLAG_IMMUTABLE
            )
            val builder = NotificationCompat.Builder(ctx, CHANNEL_ID)
                .setSmallIcon(R.drawable.ic_stat_wifi)
                .setContentIntent(contentIntent)
                .setOngoing(true)
                .setOnlyAlertOnce(true)
                .setSilent(!showFull)
            if (showFull) {
                builder.setContentTitle("校园网自动登录")
                builder.setContentText(text.ifBlank { "监控中…" })
            } else {
                builder.setContentTitle(ctx.getString(R.string.app_name))
                builder.setContentText(" ")
            }
            return builder.build()
        }

        private fun createChannel(ctx: Context, mgr: NotificationManager) {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                val channel = NotificationChannel(
                    CHANNEL_ID,
                    ctx.getString(R.string.notification_channel_name),
                    NotificationManager.IMPORTANCE_LOW
                )
                channel.setShowBadge(false)
                mgr.createNotificationChannel(channel)
            }
        }
    }
}
