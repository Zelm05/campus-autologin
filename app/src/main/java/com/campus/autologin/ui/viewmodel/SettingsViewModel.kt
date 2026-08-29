package com.campus.autologin.ui.viewmodel

import android.app.Application
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.campus.autologin.CampusApp
import com.campus.autologin.data.model.LoginConfig
import com.campus.autologin.data.model.LoginResult
import com.campus.autologin.service.AutoLoginService
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch

class SettingsViewModel(app: Application) : AndroidViewModel(app) {

    private val repo = (app as CampusApp).repository

    private val _config = MutableStateFlow(repo.loadConfig())
    val config: StateFlow<LoginConfig> = _config.asStateFlow()

    private val _password = MutableStateFlow(repo.getPassword())
    val password: StateFlow<String> = _password.asStateFlow()

    private val _testResult = MutableStateFlow<LoginResult?>(null)
    val testResult: StateFlow<LoginResult?> = _testResult.asStateFlow()

    private val _testing = MutableStateFlow(false)
    val testing: StateFlow<Boolean> = _testing.asStateFlow()

    fun update(block: LoginConfig.() -> LoginConfig) {
        _config.update(block)
    }

    fun setPassword(value: String) {
        _password.value = value
    }

    fun save() {
        repo.saveConfig(_config.value, _password.value)
        // 保存后按最新开关状态同步后台服务与通知样式
        val cfg = _config.value
        if (cfg.autoLogin || cfg.alwaysRunInBackground) {
            AutoLoginService.start(getApplication())
        } else {
            AutoLoginService.stop(getApplication())
        }
        AutoLoginService.refreshNotification(getApplication())
    }

    fun testLogin() {
        viewModelScope.launch {
            _testing.value = true
            try {
                _testResult.value = runCatching { repo.login() }
                    .getOrElse { LoginResult.Error("测试失败：${it.message}", it) }
            } finally {
                _testing.value = false
            }
        }
    }

    fun clearTest() {
        _testResult.value = null
    }
}
