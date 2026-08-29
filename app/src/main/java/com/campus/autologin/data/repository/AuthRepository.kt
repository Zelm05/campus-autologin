package com.campus.autologin.data.repository

import android.content.Context
import com.campus.autologin.data.local.AppPreferences
import com.campus.autologin.data.model.ConnectionState
import com.campus.autologin.data.model.LoginConfig
import com.campus.autologin.data.model.LoginResult
import com.campus.autologin.data.model.UserInfo
import com.campus.autologin.data.remote.LoginEngine
import com.campus.autologin.data.remote.NetworkProbe
import com.campus.autologin.data.remote.ProbeOutcome
import com.campus.autologin.util.WifiNet
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext

/**
 * 数据仓库：UI 与后台服务共用的唯一入口。
 *
 * 网络的选择集中在这里：每次操作都先取当前 WiFi Network，再传给引擎，
 * 引擎内部用它做 socket 级绑定。这样前台界面、后台服务、设置页测试三条路径
 * 都自动走 WiFi，不会因为开着 5G 而失效。
 */
class AuthRepository(context: Context) {

    private val ctx = context.applicationContext
    private val prefs = AppPreferences(ctx)
    private val engine = LoginEngine()

    fun loadConfig(): LoginConfig = prefs.loadConfig()

    fun getPassword(): String = prefs.getPassword()

    /** App 内手动下线的时刻（毫秒时间戳），冷却期内后台不自动补登。 */
    fun getManualLogoutAt(): Long = prefs.getManualLogoutAt()

    /** 是否处于"手动下线冷却期"：App 内点下线后 5 分钟内不自动补登。 */
    fun isInManualLogoutCooldown(): Boolean {
        val t = prefs.getManualLogoutAt()
        return t > 0 && System.currentTimeMillis() - t < MANUAL_LOGOUT_COOLDOWN_MS
    }

    /** App 重启后引擎内存的 userIndex 会丢，从持久层同步回来，避免探测误判。 */
    private fun syncUserIndex() {
        if (engine.userIndex.isBlank()) engine.userIndex = prefs.getUserIndex()
    }

    fun saveConfig(cfg: LoginConfig, password: String) {
        prefs.saveConfig(cfg)
        // 关闭保存密码 或 密码被清空时，一律清掉已存密码，避免重新打开又显示旧密码
        if (cfg.savePassword && password.isNotBlank()) prefs.setPassword(password)
        else prefs.clearPassword()
    }

    /** 环境诊断串（WiFi / 联网校验 / 移动数据），失败提示里回显。 */
    fun diagnostics(): String = WifiNet.describe(ctx)

    /** WiFi 与移动数据是否同时在线。 */
    fun isDualNetwork(): Boolean = WifiNet.isDualNetwork(ctx)

    /** 当前会话的门户地址（供"浏览器登录"按钮直接打开，比打 baidu 更准）。 */
    suspend fun portalUrl(): String? = withContext(Dispatchers.IO) {
        val net = WifiNet.wifi(ctx)
        (runCatching { NetworkProbe.detect(net) }.getOrNull() as? ProbeOutcome.Portal)?.url
    }

    // ------------------------------------------------------------------
    // 登录
    // ------------------------------------------------------------------
    suspend fun login(): LoginResult = withContext(Dispatchers.IO) {
        val cfg = prefs.loadConfig()
        // 注意：这里不检查 autoLogin 开关——主界面"立即登录"按钮、设置页"测试登录"
        // 都是用户主动操作，不应被"自动登录"开关拦住。开关把关在下游：
        // AutoLoginService.runLogin() 与 MainViewModel.refresh() 的自动登录分支。
        if (cfg.username.isBlank()) return@withContext LoginResult.Failure("未配置账号，请先到设置中填写")
        val password = prefs.getPassword()
        if (password.isBlank()) return@withContext LoginResult.Failure("未保存密码，请先到设置中填写")

        val net = WifiNet.wifi(ctx)
        if (net == null) {
            return@withContext LoginResult.Failure(
                "没有检测到 WiFi 连接，请先连接校园 WiFi",
                WifiNet.describe(ctx)
            )
        }

        val result = engine.login(
            username = cfg.username,
            password = password,
            net = net,
            envInfo = WifiNet.describe(ctx)
        )
        if (result is LoginResult.Success) {
            // 手动下线冷却期到此结束：登录成功即恢复自动补登
            prefs.clearManualLogoutAt()
            if (engine.lastWlanUserIpHex.isNotBlank()) {
                prefs.setWlanUserIpHex(engine.lastWlanUserIpHex)
            }
            engine.fetchOnlineInfo(net)?.let { persistUserInfo(it) }
        }
        result
    }

    // ------------------------------------------------------------------
    // 注销
    // ------------------------------------------------------------------
    suspend fun logout(): LoginResult = withContext(Dispatchers.IO) {
        val result = engine.logout(prefs.getUserIndex(), WifiNet.wifi(ctx))
        if (result is LoginResult.Success) {
            prefs.clearUserInfo()
            // 内存里的 userIndex 一并清掉，避免后续探测用旧索引误判在线
            engine.userIndex = ""
            // 记录手动下线时刻：冷却期内后台不自动补登（浏览器下线无此标记，会立即恢复）
            prefs.setManualLogoutAt(System.currentTimeMillis())
        }
        result
    }

    // ------------------------------------------------------------------
    // 状态 / 用户信息
    // ------------------------------------------------------------------
    suspend fun fetchOnlineInfo(): UserInfo? = withContext(Dispatchers.IO) {
        syncUserIndex()
        engine.fetchOnlineInfo(WifiNet.wifi(ctx))?.also { persistUserInfo(it) }
    }

    private fun persistUserInfo(info: UserInfo) {
        prefs.setUserIndex(engine.userIndex)
        prefs.setUserInfo(info.name, info.ip, info.mac, info.service)
        if (engine.lastWlanUserIpHex.isNotBlank()) {
            prefs.setWlanUserIpHex(engine.lastWlanUserIpHex)
        }
    }

    fun loadUserInfo(): UserInfo = UserInfo(
        online = prefs.getUserInfoName().isNotBlank(),
        name = prefs.getUserInfoName(),
        ip = prefs.getUserInfoIp(),
        mac = prefs.getUserInfoMac(),
        service = prefs.getUserInfoService(),
        wlanHex = prefs.getUserInfoWlanHex()
    )

    companion object {
        /** App 内手动下线后，暂停自动补登的时长。 */
        private const val MANUAL_LOGOUT_COOLDOWN_MS = 5 * 60_000L
    }

    /**
     * 判定当前连接状态。优先级：
     *  1. 网关能查到在线会话 → ONLINE；
     *  2. WiFi 没拿到系统"联网校验通过" → 必然需要认证（最可靠的信号）；
     *  3. 再退回 HTTP 探针。
     */
    suspend fun probeState(): ConnectionState = withContext(Dispatchers.IO) {
        // 手动下线冷却期内：用户明确离线，直接判"需登录/认证"，
        // 防止网关按 IP 匹配返回旧会话导致误判在线
        if (isInManualLogoutCooldown()) return@withContext ConnectionState.CAPTIVE
        val net = WifiNet.wifi(ctx)
        syncUserIndex()
        if (engine.fetchOnlineInfo(net)?.online == true) return@withContext ConnectionState.ONLINE
        if (net == null) return@withContext ConnectionState.OFFLINE
        if (WifiNet.hasCaptiveFlag(ctx, net)) return@withContext ConnectionState.CAPTIVE
        if (!WifiNet.isValidated(ctx, net)) return@withContext ConnectionState.CAPTIVE
        when (runCatching { NetworkProbe.detect(net) }.getOrNull()) {
            is ProbeOutcome.Portal -> ConnectionState.CAPTIVE
            is ProbeOutcome.Online -> ConnectionState.ONLINE
            else -> ConnectionState.OFFLINE
        }
    }
}
