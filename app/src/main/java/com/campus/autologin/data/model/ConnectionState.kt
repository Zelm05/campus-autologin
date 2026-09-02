package com.campus.autologin.data.model

/**
 * Coarse network state used by the UI and the background monitor.
 */
enum class ConnectionState {
    UNKNOWN,
    OFFLINE,    // 没有可用的网络
    ONLINE,     // 已认证：网关确认了本账号的在线会话
    OPEN,       // 免认证网络：能上外网，但网关查不到本账号会话（这个网络本就不需要认证）
    CAPTIVE     // 需要认证：被门户拦截，等待登录
}
