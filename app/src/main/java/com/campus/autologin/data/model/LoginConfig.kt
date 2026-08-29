package com.campus.autologin.data.model

/**
 * 用户在界面上可配置的项（校园网协议参数已硬编码在 CampusConfig，不在此暴露）。
 */
data class LoginConfig(
    val username: String = "",
    val autoLogin: Boolean = true,            // 连接校园网时自动登录
    val autoStartOnBoot: Boolean = true,      // 开机自启动后台监控
    val savePassword: Boolean = true,         // 保存密码
    val notifyOnFailure: Boolean = false,     // 登录失败时通知
    val showMonitorNotification: Boolean = true,  // 显示"监控中"常驻通知
    val alwaysRunInBackground: Boolean = false  // 始终在后台运行（被杀也能保活）
)
