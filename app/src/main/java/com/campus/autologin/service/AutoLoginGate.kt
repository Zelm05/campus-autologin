package com.campus.autologin.service

/**
 * 自动登录的节流闸门。
 *
 * 前台服务（网络回调 + 60 秒自检）、WorkManager 兜底任务、主界面周期刷新
 * 是三条独立的触发链路，断网重连时很容易在同一瞬间同时发起登录，向网关
 * 连打多次请求——网关对短时间重复认证很敏感。
 *
 * 这里给「自动」路径加一道最小间隔（5 秒），防抖的同时不挡正经的自动登录
 *（主界面 30 秒周期 / 服务 60 秒自检都远大于 5 秒）。用户手动点登录不受限制。
 */
object AutoLoginGate {

    private const val MIN_INTERVAL_MS = 5_000L

    @Volatile
    private var lastAt = 0L

    /** 距上次自动登录是否已超过最小间隔。通过则自动占位。 */
    @Synchronized
    fun tryAcquire(): Boolean {
        val now = System.currentTimeMillis()
        if (now - lastAt < MIN_INTERVAL_MS) return false
        lastAt = now
        return true
    }

    /** 手动登录成功后放行，避免用户点了立即登录却被闸门挡住。 */
    @Synchronized
    fun release() {
        lastAt = 0L
    }
}
