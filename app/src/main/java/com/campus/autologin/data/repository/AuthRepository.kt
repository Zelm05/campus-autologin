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
import kotlinx.coroutines.delay
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
            // 登录成功 = 这个网络其实是**需要认证**的：把它从免认证名单里移除。
            // 否则下次连上会被 probeState 第 3 步直接判成"已连接"而不再登录。
            // 与"免认证时记入名单"配合，形成双向修正：判错一次，登录成功立刻纠正。
            WifiNet.networkKey(ctx)?.let { prefs.removeOpenNetwork(it) }
            if (engine.lastWlanUserIpHex.isNotBlank()) {
                prefs.setWlanUserIpHex(engine.lastWlanUserIpHex)
            }
            // 网关会话表有同步延迟，用重试查询把姓名/IP/服务取回来，
            // 否则界面上会短暂显示空白（旧版本表现为 "null"）
            fetchOnlineInfoRetry()
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

    /**
     * 带重试的在线信息查询。
     *
     * 登录刚成功时，网关的在线会话表还没同步完，立刻查会拿到空响应，
     * 于是界面上的姓名 / IP / 服务全部回落为空——这就是"自动登录成功了，
     * 但界面上仍是一片空白（甚至显示 null）"的根因。这里做几次短间隔重试。
     */
    suspend fun fetchOnlineInfoRetry(
        times: Int = 4,
        delayMs: Long = 700L
    ): UserInfo? = withContext(Dispatchers.IO) {
        for (attempt in 0 until times) {
            syncUserIndex()
            val info = runCatching { engine.fetchOnlineInfo(WifiNet.wifi(ctx)) }.getOrNull()
            if (info != null) {
                persistUserInfo(info)
                return@withContext info
            }
            if (attempt < times - 1) delay(delayMs)
        }
        null
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

    /**
     * 免认证网络下展示用的**本机**网络信息。
     *
     * 这类网络在网关里查不到本账号会话（它本就不需要认证），
     * 信息卡改显示设备自己在这个 WiFi 下拿到的 IP/MAC。
     */
    fun localNetInfo(): UserInfo {
        val (ip, mac) = WifiNet.localNet(ctx)
        return UserInfo(online = false, ip = ip, mac = mac)
    }

    /**
     * 判定当前连接状态。
     *
     * 三种"能上网"的情形必须分清：
     *  · **ONLINE**：网关确认了本账号的在线会话（有 IP）——真正认证过的校园网；
     *  · **OPEN**：能上外网，但网关查不到本账号会话 —— 说明这是**免认证网络**。
     *    学校有部分网络连上就能上网、从不需要 portal 登录，会话表里自然永远
     *    查不到本账号。旧版本把它当成"需认证"，于是不断自动登录，
     *    登录与下线全都报错；现在它显示"已连接"，且不触发自动登录；
     *  · **CAPTIVE**：被门户拦截，确实需要认证，会自动补登。
     *
     * 用户明确要求：
     *  · 不出现"已连接 + 空 IP/MAC"——查不到本账号会话就不按 ONLINE 处理；
     *  · **任何"需登录/认证"状态都会自动补登**（已取消手动下线冷却期）；
     *  · 没下线时断开重连，会话还在，应直接显示"已连接"——所以会话查询
     *    必须放在最前面，不能等系统校验/探针（重连瞬间它们都还没就绪）。
     *
     * 判定顺序：
     *  1. 连 WiFi 都没有 → 未连接（最快，不发请求）；
     *  2. 先查网关会话：**有完整本账号会话（IP 非空）→ ONLINE 已连接**。
     *     这覆盖"断网重连"：userIndex 在本地缓存里，重连后立刻能查到会话。
     *  3. 查不到会话，但这是**记住过的免认证网络**（且系统没打门户标记）→ OPEN；
     *  4. 系统信号（门户标记 / 未校验通过）→ 需登录；
     *  5. 探针兜底：被劫持 → 需登录；**能上网但无会话 → OPEN 免认证**（并记住）。
     */
    suspend fun probeState(): ConnectionState = withContext(Dispatchers.IO) {
        // 1) 没有可用的 WiFi（关了 WiFi、不在覆盖范围、只开着移动数据）→ 未连接
        val net = WifiNet.wifi(ctx)
        if (net == null) return@withContext ConnectionState.OFFLINE

        // 2) 直接查网关会话。必须拿到**完整的本账号会话（IP 非空）**才算在线——
        //    网关只回了用户名、没给 IP 也是"查不到"，一律按未认证处理。
        syncUserIndex()
        val info = runCatching { engine.fetchOnlineInfo(net) }.getOrNull()
        if (info?.online == true && info.ip.isNotBlank()) {
            persistUserInfo(info)
            return@withContext ConnectionState.ONLINE
        }

        // 3) 记住过的免认证网络 → 连上就直接判定，跳过系统校验与探针（最快最稳）。
        //    仍检查门户标记：万一学校后来给这个网络加了认证，系统标记会先发现，
        //    于是继续往下走、由探针重新判定，不会被旧的记忆锁死。
        val netKey = WifiNet.networkKey(ctx)
        if (prefs.isOpenNetwork(netKey) && !WifiNet.hasCaptiveFlag(ctx, net)) {
            return@withContext ConnectionState.OPEN
        }

        // 4) 系统明确打上门户标记 / 没拿到"联网校验通过" → 需登录
        if (WifiNet.hasCaptiveFlag(ctx, net)) return@withContext ConnectionState.CAPTIVE
        if (!WifiNet.isValidated(ctx, net)) return@withContext ConnectionState.CAPTIVE

        // 5) 探针兜底
        when (runCatching { NetworkProbe.detect(net) }.getOrNull()) {
            // 被劫持到门户 → 需要认证
            is ProbeOutcome.Portal -> ConnectionState.CAPTIVE

            // 能上外网，但网关查不到本账号会话 → 免认证网络，显示"已连接"。
            // 顺手记住这个网络，下次连上走第 3 步直接判定，不用再等探针。
            is ProbeOutcome.Online -> {
                netKey?.let { prefs.addOpenNetwork(it) }
                ConnectionState.OPEN
            }

            // 既没抓到门户、也证明不了能上网（DNS 不通 / 全部超时）→ 未连接
            else -> ConnectionState.OFFLINE
        }
    }
}
