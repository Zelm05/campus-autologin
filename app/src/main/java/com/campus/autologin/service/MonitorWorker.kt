package com.campus.autologin.service

import android.content.Context
import androidx.work.Constraints
import androidx.work.CoroutineWorker
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.ExistingWorkPolicy
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.OutOfQuotaPolicy
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import com.campus.autologin.data.model.ConnectionState
import com.campus.autologin.data.model.LoginResult
import com.campus.autologin.data.repository.AuthRepository
import com.campus.autologin.util.ErrorText
import java.util.concurrent.TimeUnit

/**
 * 后台兜底任务：前台服务被系统/厂商杀掉之后，由它继续完成「探测 + 补登」。
 *
 * 为什么不能只靠前台服务：
 *  · Android 12+ 禁止从后台启动前台服务，服务一旦被杀就只能等下一次开机或开 App；
 *  · 国产 ROM（MIUI / 鸿蒙 / ColorOS / OriginOS）会主动清理长驻服务，START_STICKY 基本无效。
 *
 * WorkManager 的加急任务（expedited）在 Android 12+ 上由系统以「系统级前台服务」
 * 的形式托管，**不受后台启动限制**，是目前唯一能稳定从后台跑起来的官方通道。
 * 因此这里是：前台服务在的时候由服务即时响应，服务没了这个 Worker 兜底并把它拉回来。
 */
class MonitorWorker(appContext: Context, params: WorkerParameters) :
    CoroutineWorker(appContext, params) {

    override suspend fun doWork(): Result {
        val ctx = applicationContext
        val repo = runCatching { AuthRepository(ctx) }.getOrNull() ?: return Result.retry()
        val cfg = repo.loadConfig()

        // 两个开关都关了：彻底收工，顺手撤掉周期任务
        if (!cfg.autoLogin && !cfg.alwaysRunInBackground) {
            disable(ctx)
            return Result.success()
        }

        // 前台服务响应速度最快（有网络回调），Worker 负责把它拉回来。
        // Android 12+ 后台启动会被拒，失败是预期内的静默行为，不影响 Worker 自己干活。
        if (!AutoLoginService.isRunning) {
            AutoLoginService.start(ctx)
        }

        if (!cfg.autoLogin) return Result.success()
        if (!AutoLoginGate.tryAcquire()) return Result.success()

        return try {
            val state = repo.probeState()
            if (state != ConnectionState.CAPTIVE) {
                AutoLoginGate.release()
                return Result.success()
            }
            val result = repo.login()
            when (result) {
                is LoginResult.Success -> {
                    runCatching { repo.fetchOnlineInfoRetry() }
                    AutoLoginService.postNotice(ctx, "已自动登录，监控中…")
                    Result.success()
                }
                else -> {
                    if (cfg.notifyOnFailure) {
                        AutoLoginService.postNotice(ctx, "自动登录未成功：${result.message}")
                    }
                    Result.success()
                }
            }
        } catch (t: Throwable) {
            if (cfg.notifyOnFailure) {
                AutoLoginService.postNotice(ctx, "自动登录异常：${ErrorText.of(t)}")
            }
            Result.retry()
        }
    }

    companion object {
        private const val UNIQUE_PERIODIC = "campus_monitor_periodic"
        private const val UNIQUE_REVIVE = "campus_monitor_revive"

        /** 注册 15 分钟一次的周期兜底（WorkManager 允许的最小周期）。 */
        fun enable(ctx: Context) {
            val app = ctx.applicationContext
            val request = PeriodicWorkRequestBuilder<MonitorWorker>(15, TimeUnit.MINUTES)
                // 不加任何约束：Doze 下也要能跑，否则熄屏一晚就漏登了
                .setConstraints(Constraints.Builder().build())
                .build()
            runCatching {
                WorkManager.getInstance(app).enqueueUniquePeriodicWork(
                    UNIQUE_PERIODIC,
                    ExistingPeriodicWorkPolicy.UPDATE,
                    request
                )
            }
        }

        fun disable(ctx: Context) {
            val app = ctx.applicationContext
            runCatching {
                val wm = WorkManager.getInstance(app)
                wm.cancelUniqueWork(UNIQUE_PERIODIC)
                wm.cancelUniqueWork(UNIQUE_REVIVE)
            }
        }

        /**
         * 服务被杀后的快速复活：延迟若干分钟跑一次加急任务，
         * 由它把前台服务重新拉起来，不用干等下一个 15 分钟窗口。
         */
        fun scheduleRevive(ctx: Context, delayMinutes: Long = 1) {
            val app = ctx.applicationContext
            val request = OneTimeWorkRequestBuilder<MonitorWorker>()
                .setExpedited(OutOfQuotaPolicy.RUN_AS_NON_EXPEDITED_WORK_REQUEST)
                .setInitialDelay(delayMinutes.coerceAtLeast(0), TimeUnit.MINUTES)
                .build()
            runCatching {
                WorkManager.getInstance(app).enqueueUniqueWork(
                    UNIQUE_REVIVE,
                    ExistingWorkPolicy.REPLACE,
                    request
                )
            }
        }
    }
}
