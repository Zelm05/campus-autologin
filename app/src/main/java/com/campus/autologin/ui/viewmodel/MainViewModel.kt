package com.campus.autologin.ui.viewmodel

import android.app.Application
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.campus.autologin.CampusApp
import com.campus.autologin.data.model.ConnectionState
import com.campus.autologin.data.model.LoginConfig
import com.campus.autologin.data.model.LoginResult
import com.campus.autologin.data.model.UserInfo
import com.campus.autologin.service.AutoLoginService
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch

data class MainUiState(
    val config: LoginConfig = LoginConfig(),
    val connection: ConnectionState = ConnectionState.UNKNOWN,
    val loading: Boolean = false,
    val lastResult: LoginResult? = null,
    val passwordSet: Boolean = false,
    val userInfo: UserInfo = UserInfo(),
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

    init {
        refresh()
    }

    fun refresh() {
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
        viewModelScope.launch {
            _uiState.update { it.copy(loading = true) }
            val state = runCatching { repo.probeState() }.getOrDefault(ConnectionState.UNKNOWN)
            // 每次刷新若处于"需登录/认证"且开启了自动登录，直接尝试登录一次；
            // 手动下线冷却期内（点过"下线"后 5 分钟内）不自动登回去
            if (state == ConnectionState.CAPTIVE && cfg.autoLogin && !repo.isInManualLogoutCooldown()) {
                val result = runCatching { repo.login() }.getOrElse {
                    LoginResult.Error("自动登录异常：${it.message}", it)
                }
                val freshInfo = if (result is LoginResult.Success) {
                    runCatching { repo.fetchOnlineInfo() }.getOrNull()
                } else null
                _uiState.update {
                    it.copy(
                        loading = false,
                        connection = if (result is LoginResult.Success)
                            ConnectionState.ONLINE else state,
                        lastResult = result,
                        // 非在线：清掉会话数据（IP/MAC/hex），只保留姓名用于问候
                        userInfo = freshInfo ?: it.userInfo.clearedSession()
                    )
                }
            } else {
                val fresh = runCatching { repo.fetchOnlineInfo() }.getOrNull()
                _uiState.update {
                    val newState = if (fresh?.online == true) ConnectionState.ONLINE else state
                    it.copy(
                        loading = false,
                        connection = newState,
                        // 非在线（断开/需登录）：不再显示旧的 IP/MAC/hex
                        userInfo = fresh ?: it.userInfo.clearedSession()
                    )
                }
            }
        }
    }

    /**
     * 登录。网络选择与 WiFi socket 绑定已下沉到仓库/引擎层，
     * 这里不再做进程级 bindProcessToNetwork（部分 ROM 无效且有副作用）。
     */
    fun login() = runAttempt { repo.login() }

    fun logout() = runAttempt(isLogout = true) { repo.logout() }

    private fun runAttempt(isLogout: Boolean = false, block: suspend () -> LoginResult) {
        viewModelScope.launch {
            _uiState.update { it.copy(loading = true, lastResult = null) }
            val result = runCatching { block() }.getOrElse {
                LoginResult.Error("操作失败：${it.message}", it)
            }
            val freshInfo = if (result is LoginResult.Success) {
                runCatching { repo.fetchOnlineInfo() }.getOrNull()
            } else null
            // 注销成功 → 直接判"需登录/认证"（用户主动下线 = 明确离线），
            // 不依赖注销后的复查——网关可能按 IP 匹配返回旧会话导致误判在线
            val newConnection = when {
                isLogout && result is LoginResult.Success -> ConnectionState.CAPTIVE
                freshInfo?.online == true -> ConnectionState.ONLINE
                else -> _uiState.value.connection
            }
            _uiState.update {
                it.copy(
                    loading = false,
                    lastResult = result,
                    // 注销成功 / 查不到会话：清掉会话数据（IP/MAC/hex），只保留姓名
                    userInfo = if (isLogout && result is LoginResult.Success)
                        it.userInfo.clearedSession()
                    else freshInfo ?: it.userInfo.clearedSession(),
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

    /** 统一按「自动登录 || 始终在后台运行」决定后台服务是否常驻。 */
    private fun applyServiceState() {
        val cfg = repo.loadConfig()
        if (cfg.autoLogin || cfg.alwaysRunInBackground) {
            AutoLoginService.start(getApplication())
        } else {
            AutoLoginService.stop(getApplication())
        }
    }
}
