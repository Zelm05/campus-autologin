package com.campus.autologin.ui.viewmodel

import android.app.Application
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.campus.autologin.CampusApp
import com.campus.autologin.data.model.ConnectionState
import com.campus.autologin.data.model.LoginConfig
import com.campus.autologin.data.model.LoginResult
import com.campus.autologin.data.model.UserInfo
import com.campus.autologin.service.AutoLoginGate
import com.campus.autologin.service.AutoLoginService
import com.campus.autologin.service.KeepAlive
import com.campus.autologin.util.ErrorText
import kotlinx.coroutines.Job
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch

data class MainUiState(
    val config: LoginConfig = LoginConfig(),
    val connection: ConnectionState = ConnectionState.UNKNOWN,
    val loading: Boolean = false,
    /**
     * 仅由"用户手动下拉刷新"驱动，与自动刷新/登录中的 loading 解耦。
     * 旧版本下拉指示器直接绑定 loading：网络回调一触发自动刷新（中间大图标
     * 转圈）时下拉指示器也跟着出现/被顶掉，用户手动下拉反而看不到反馈。
     */
    val userRefreshing: Boolean = false,
    val lastResult: LoginResult? = null,
    val passwordSet: Boolean = false,
    val userInfo: UserInfo = UserInfo(),
    /** 免认证网络下展示的**本机**网络信息（网关查不到本账号会话时用它）。 */
    val localNet: UserInfo = UserInfo(),
    val greeting: String = greetingForHour(java.util.Calendar.getInstance().get(java.util.Calendar.HOUR_OF_DAY))
)

/** 按时段返回问候语。 */
fun greetingForHour(hour: Int): String = when (hour) {
    in 5..8 -> "早上好"
    in 9..10 -> "上午好"
    in 11..12 -> "中午好"
    in 13..16 -> "下午好"
    else -> "晚上好"
}

/** 非在线时清空会话数据（IP/MAC/hex），保留姓名用于问候展示。 */
private fun UserInfo.clearedSession(): UserInfo = copy(
    online = false,
    ip = "",
    mac = "",
    service = "",
    wlanHex = ""
)

class MainViewModel(app: Application) : AndroidViewModel(app) {

    private val repo = (app as CampusApp).repository

    private val _uiState = MutableStateFlow(MainUiState())
    val uiState: StateFlow<MainUiState> = _uiState.asStateFlow()

    /**
     * 刷新任务的句柄。网络回调（onAvailable/onLost/onCapabilitiesChanged）会连着
     * 触发好几次 refresh，旧版本没有互斥，多个协程并发跑「探测 + 自动登录」，
     * 谁最后写完谁生效 —— 这就是"有时候状态显示是错的、但不是每次都错"的根因。
     * 现在每次刷新先取消上一次，并在每个挂起点后检查自己是否已被取代。
     */
    private var refreshJob: Job? = null

    /**
     * 自动登录协程：独立于刷新协程运行，refresh 的 cancel 不会打断它，
     * 保证"需登录/认证 → 自动登录"这条链路稳定执行。
     */
    private var autoLoginJob: Job? = null

    init {
        // 后台服务自动登录成功后，主界面立即刷新（浏览器下线等场景，
        // 系统回调可能长期不触发，UI 状态不能滞后）
        AutoLoginService.onAutoLogin = { refresh() }
        refresh()
    }

    /**
     * WiFi 重新出现（断网重连 / App 重启后第一次收到网络）。
     * 刷新状态；若处于"需登录/认证"且开启自动登录，doRefresh 会自动补登。
     */
    fun onNetworkAvailable() {
        refresh()
    }

    /**
     * @param byUser 是否为用户手动下拉触发。
     *        true 时驱动下拉刷新指示器（userRefreshing）；
     *        网络回调触发的自动刷新传默认 false，只转中间大图标，不打扰下拉指示器。
     */
    fun refresh(byUser: Boolean = false) {
        refreshJob?.cancel()
        refreshJob = viewModelScope.launch {
            if (byUser) _uiState.update { it.copy(userRefreshing = true) }
            try {
                doRefresh()
            } finally {
                if (byUser) _uiState.update { it.copy(userRefreshing = false) }
            }
        }
    }

    /**
     * 取「要展示的会话信息」。
     *
     * 规则（用户明确）：**查不到完整会话（IP 为空）就是未认证**——
     * 1) 有 freshInfo（网关确认了完整会话）→ 用它；
     * 2) 否则退回持久层，但持久层的会话也必须"完整"（IP 非空）才可用，
     *    因为登录成功后 persist 的可能是不完整响应；
     * 3) 都不行 → 清空会话字段（IP/MAC 显示为空 = 未认证，状态由 connection 决定）。
     * 不再做 hex IP 兜底：那会把"未认证"伪装成有数据，制造状态与数据不一致。
     */
    private fun resolveUserInfo(freshInfo: UserInfo?, fallback: UserInfo): UserInfo {
        if (freshInfo != null) return freshInfo
        return repo.loadUserInfo().takeIf { it.online && it.ip.isNotBlank() }
            ?: fallback.clearedSession()
    }

    private suspend fun doRefresh() {
        val cfg = repo.loadConfig()
        val info = repo.loadUserInfo()
        _uiState.update {
            it.copy(
                config = cfg,
                passwordSet = repo.getPassword().isNotBlank(),
                userInfo = info,
                greeting = greetingForHour(
                    java.util.Calendar.getInstance().get(java.util.Calendar.HOUR_OF_DAY)
                )
            )
        }

        _uiState.update { it.copy(loading = true) }
        val state = runCatching { repo.probeState() }.getOrDefault(ConnectionState.UNKNOWN)
        if (!currentCoroutineContext().isActive) return

        // 每次刷新若处于"需登录/认证"且开启了自动登录，触发一次自动登录。
        // 已按用户要求取消"手动下线冷却期"：任何需登录状态都会自动补登。
        //
        // 关键：登录放到**独立协程**（autoLoginJob）里跑，绝不能被网络回调 /
        // 30 秒周期刷新的 cancel 打断——旧版本直接在 doRefresh 里登录，
        // 刷新一多登录请求就被取消，于是"永远不会自动登录"。
        // 与后台服务共用 AutoLoginGate 节流闸门（5 秒），避免重复打网关。
        if (state == ConnectionState.CAPTIVE && cfg.autoLogin && AutoLoginGate.tryAcquire()) {
            _uiState.update { it.copy(loading = true) }
            if (autoLoginJob?.isActive != true) {
                autoLoginJob = viewModelScope.launch { doAutoLogin(state) }
            }
            return
        }

        // probeState 已经完成"会话确认"：ONLINE 时必已查到完整会话并持久化，
        // 这里直接复用持久层结果，不再重复请求网关（快）；
        // 非 ONLINE（需登录/未连接）时界面本来就显示对应状态，无需再查会话。
        val fresh = if (state == ConnectionState.ONLINE) {
            repo.loadUserInfo().takeIf { it.online && it.ip.isNotBlank() }
        } else null
        // 免认证网络：网关里没有本账号会话，信息卡改显示设备在本网络拿到的地址
        val local = if (state == ConnectionState.OPEN) repo.localNetInfo() else UserInfo()
        if (!currentCoroutineContext().isActive) return
        _uiState.update {
            it.copy(
                loading = false,
                connection = state,
                // 进入"已连接"（认证过的 ONLINE 或免认证的 OPEN）都清掉旧的失败提示，
                // 避免"已连接 + 登录失败"并存
                lastResult = if (state == ConnectionState.ONLINE ||
                    state == ConnectionState.OPEN
                ) null else it.lastResult,
                // 非在线（断开/需登录）：不再显示旧的 IP/MAC/hex
                userInfo = resolveUserInfo(fresh, it.userInfo),
                localNet = local
            )
        }
    }

    /**
     * 自动登录（独立协程，不随刷新取消）：
     * 1. 调用网关登录；
     * 2. 登录成功后重试拉取会话信息（网关同步有延迟）；
     * 3. 3 秒后再补刷一次，确保 IP/MAC 显示；
     * 4. 登录成功但连 IP 都没有 → 不显示"已连接"，保持"需登录/认证"，
     *    绝不出现"已连接 + 空 IP/MAC"。
     */
    private suspend fun doAutoLogin(state: ConnectionState) {
        val result = runCatching { repo.login() }.getOrElse {
            LoginResult.Error(ErrorText.of(it), it)
        }
        if (!currentCoroutineContext().isActive) return

        val freshInfo = if (result is LoginResult.Success) {
            runCatching { repo.fetchOnlineInfoRetry() }.getOrNull()
        } else null
        if (!currentCoroutineContext().isActive) return

        if (result is LoginResult.Success) {
            // 网关会话同步有延迟：3 秒后再刷一次，把 IP/MAC 补上
            viewModelScope.launch {
                delay(3000)
                if (currentCoroutineContext().isActive) refresh()
            }
        }

        val resolved = resolveUserInfo(freshInfo, _uiState.value.userInfo)
        val showOnline = result is LoginResult.Success && resolved.ip.isNotBlank()
        _uiState.update {
            it.copy(
                loading = false,
                connection = if (showOnline) ConnectionState.ONLINE else state,
                // 登录成功：清掉旧的"登录失败"提示；失败/未拿到会话：保留原因
                lastResult = if (showOnline) null else result,
                userInfo = resolved
            )
        }
    }

    /**
     * 登录。网络选择与 WiFi socket 绑定已下沉到仓库/引擎层，
     * 这里不再做进程级 bindProcessToNetwork（部分 ROM 无效且有副作用）。
     */
    fun login() = runAttempt { repo.login() }

    fun logout() = runAttempt(isLogout = true) { repo.logout() }

    private fun runAttempt(isLogout: Boolean = false, block: suspend () -> LoginResult) {
        // 手动操作期间取消正在跑的自动刷新，避免两者结果互相覆盖；
        // 同时放行自动登录闸门，保证用户点"立即登录"永远立刻生效
        refreshJob?.cancel()
        AutoLoginGate.release()
        refreshJob = viewModelScope.launch {
            _uiState.update { it.copy(loading = true, lastResult = null) }
            val result = runCatching { block() }.getOrElse {
                LoginResult.Error(ErrorText.of(it), it)
            }
            if (!currentCoroutineContext().isActive) return@launch

            val freshInfo = if (result is LoginResult.Success) {
                runCatching { repo.fetchOnlineInfoRetry() }.getOrNull()
            } else null
            if (!currentCoroutineContext().isActive) return@launch

            // 会话信息：登录成功但网关未同步时退回持久层，别把刚存好的 IP/MAC 清掉
            val prev = _uiState.value
            val resolved = if (isLogout && result is LoginResult.Success) {
                prev.userInfo.clearedSession()
            } else {
                resolveUserInfo(freshInfo, prev.userInfo)
            }
            // 注销成功 → 直接判"需登录/认证"（用户主动下线 = 明确离线），
            // 不依赖注销后的复查——网关可能按 IP 匹配返回旧会话导致误判在线。
            // 登录成功但会话不完整（IP 空）→ 也不显示"已连接"（未认证状态）；
            // 这里**主动重置为 CAPTIVE**，绝不保留旧的 ONLINE 与空 userInfo
            // 组合，避免出现"已连接+空 IP/MAC"的中间态。
            val newConnection = when {
                isLogout && result is LoginResult.Success -> ConnectionState.CAPTIVE
                freshInfo?.online == true && freshInfo.ip.isNotBlank() -> ConnectionState.ONLINE
                result is LoginResult.Success && resolved.ip.isNotBlank() -> ConnectionState.ONLINE
                result is LoginResult.Success -> ConnectionState.CAPTIVE
                else -> _uiState.value.connection
            }
            _uiState.update {
                it.copy(
                    loading = false,
                    // 下线是用户主动操作（不显示"登录成功已下线"那种 ResultCard）；
                    // 手动登录成功：清掉之前的失败提示；失败：保留原因
                    lastResult = when {
                        result is LoginResult.Success -> null
                        else -> result
                    },
                    userInfo = resolved,
                    connection = newConnection
                )
            }
        }
    }

    fun toggleAutoLogin(on: Boolean) {
        val cfg = repo.loadConfig().copy(autoLogin = on)
        repo.saveConfig(cfg, repo.getPassword())
        _uiState.update { it.copy(config = cfg) }
        applyServiceState()
    }

    /** 是否显示"监控中"常驻通知。 */
    fun toggleShowNotification(on: Boolean) {
        val cfg = repo.loadConfig().copy(showMonitorNotification = on)
        repo.saveConfig(cfg, repo.getPassword())
        _uiState.update { it.copy(config = cfg) }
        AutoLoginService.refreshNotification(getApplication())
    }

    /** 是否始终在后台运行（从最近任务划掉也能保活）。 */
    fun toggleAlwaysBackground(on: Boolean) {
        val cfg = repo.loadConfig().copy(alwaysRunInBackground = on)
        repo.saveConfig(cfg, repo.getPassword())
        _uiState.update { it.copy(config = cfg) }
        applyServiceState()
    }

    /** 统一按「自动登录 || 始终在后台运行」决定后台服务与兜底任务是否常驻。 */
    private fun applyServiceState() {
        KeepAlive.sync(getApplication())
    }
}
