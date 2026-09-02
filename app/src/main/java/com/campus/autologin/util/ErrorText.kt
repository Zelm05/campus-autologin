package com.campus.autologin.util

import java.io.IOException
import java.net.ConnectException
import java.net.NoRouteToHostException
import java.net.PortUnreachableException
import java.net.SocketTimeoutException
import java.net.UnknownHostException
import javax.net.ssl.SSLException

/**
 * 把「异常」和「网关返回的错误串」翻译成用户看得懂、且与真实情况一致的中文。
 *
 * 存在的意义：
 *  1. `Throwable.message` **可能为 null**，模板 `"失败：${e.message}"` 会渲染出
 *     字面量 "null"——这是主界面偶尔出现 "null" 的直接原因；
 *  2. 锐捷网关返回的 message 经常是英文/内部错误码（如 `userid error1`），
 *     直接透传会让用户看不懂、甚至误导（比如其实是欠费却报"认证失败"）。
 */
object ErrorText {

    /**
     * 异常 → 人话。保证返回值既非空、也绝不会是 "null"。
     */
    fun of(t: Throwable?): String {
        t ?: return "发生未知错误"
        val raw = t.message
            ?.trim()
            ?.takeIf { it.isNotBlank() && !it.equals("null", true) && it != t.javaClass.simpleName }
        return when (t) {
            is UnknownHostException ->
                "域名解析失败，连不上认证网关。请确认已连接校园 WiFi（不是 4G/5G，也不是其他 WiFi）。"

            is SocketTimeoutException ->
                "连接认证网关超时。常见于：不在校园网覆盖范围内、校园网信号太差，或网关繁忙。"

            is ConnectException, is NoRouteToHostException, is PortUnreachableException ->
                "无法与认证网关建立连接。请确认已连接校园 WiFi，且没有开着 VPN/代理。"

            is SSLException ->
                "与网关的加密连接异常（${raw ?: "证书或协议不匹配"}）。"

            is IOException ->
                raw?.let { "网络请求失败：$it" } ?: "网络请求失败，请检查 WiFi 连接后重试。"

            is SecurityException ->
                raw?.let { "权限不足：$it" } ?: "缺少必要权限，请在系统设置中允许后台运行与通知。"

            else -> raw?.let { "$it（${t.javaClass.simpleName}）" }
                ?: "发生错误（${t.javaClass.simpleName}）。"
        }
    }

    /**
     * 网关返回的 message → 中文人话。
     * @param raw 网关原始 message，可能为空、英文或错误码。
     * @return 永远非空、且与真实原因相符的说明。
     */
    fun gateway(raw: String?): String {
        val s = raw
            ?.trim()
            ?.takeIf { it.isNotBlank() && !it.equals("null", true) }
            ?: return "网关未给出失败原因（返回的 message 为空）。常见原因是账号或密码不对。"

        val lower = s.lowercase()

        // —— 账号 / 密码 ——
        if (lower.contains("userid error") || lower.contains("user id error") ||
            lower.contains("user not exist") || lower.contains("用户不存在") ||
            lower.contains("not exist") || lower.contains("no such user")
        ) return "学号不正确，或该账号在校园网系统中不存在。请到设置里核对学号。"

        if (lower.contains("password error") || lower.contains("pwd error") ||
            lower.contains("密码错误") || lower.contains("wrong password")
        ) return "密码错误。请到设置里重新填写校园网密码。"

        if (lower.contains("账号") && (lower.contains("停用") || lower.contains("禁用") ||
                lower.contains("冻结") || lower.contains("锁定"))
        ) return "该账号已被停用/冻结，需要联系校园网管理部门（网络中心）处理。"

        if (lower.contains("disabled") || lower.contains("forbidden") ||
            lower.contains("blocked") || lower.contains("blacklist")
        ) return "该账号被网关限制（停用/黑名单），需要联系校园网管理部门处理。"

        // —— 计费 / 套餐 ——
        if (lower.contains("余额") || lower.contains("欠费") || lower.contains("arrearage") ||
            lower.contains("no money") || lower.contains("not enough") ||
            lower.contains("insufficient")
        ) return "账号余额不足或已欠费。充值后再试。"

        if (lower.contains("expire") || lower.contains("过期") || lower.contains("到期") ||
            lower.contains("out of date")
        ) return "账号或套餐已过期。请联系校园网管理部门续期。"

        // —— 并发 / 会话 ——
        if (lower.contains("already online") || lower.contains("已在线") ||
            lower.contains("别处") || lower.contains("其他地方") ||
            lower.contains("重复登录") || lower.contains("login in other")
        ) return "该账号已经在其他设备在线。请先在那台设备下线，或在网关自助下线后再登录。"

        if (lower.contains("exceed") || lower.contains("max") || lower.contains("limit") ||
            lower.contains("超出") || lower.contains("并发") || lower.contains("最多")
        ) return "已超出该账号允许的同时在线设备数。请先在其它设备下线。"

        if (lower.contains("mac") && (lower.contains("bind") || lower.contains("绑定") ||
                lower.contains("change"))
        ) return "账号绑定了设备 MAC 地址。请在网关自助解绑，或联系网络中心。"

        // —— 参数 / 协议 ——
        if (lower.contains("querystring") || lower.contains("wlanuserip") ||
            lower.contains("参数") || lower.contains("invalid param") ||
            lower.contains("param error")
        ) return "认证参数失效（一般是 WiFi 刚重连、参数还没刷新）。稍等几秒再点一次即可。"

        if (lower.contains("service") && (lower.contains("error") || lower.contains("not") ||
                lower.contains("无效"))
        ) return "套餐/服务类型不正确，网关拒绝了这次认证。请联系校园网管理部门。"

        if (lower.contains("timeout") || lower.contains("超时")) return "与网关通信超时，请重试。"

        if (lower.contains("success") && lower.contains("fail")) return "网关返回了自相矛盾的响应，请重试。"

        if (lower.contains("认证失败") || lower.contains("auth fail") ||
            lower.contains("authentication failed") || lower.contains("fail")
        ) return "认证失败：网关拒绝了这次登录（$s）。最常见的原因是学号或密码不对。"

        // 兜底：把原始串也带上，方便用户反馈与自查
        return "登录失败：$s"
    }

    /**
     * 登录/注销响应不是 JSON（通常是网关维护页、代理拦截页）时的说明。
     */
    fun unparsable(body: String): String {
        val head = body.trim().take(120)
        return when {
            head.startsWith("{") -> "网关返回了无法解析的 JSON：$head"
            head.startsWith("<", true) ->
                "网关返回的不是数据，而是一个网页（可能是维护页或代理拦截页）。请确认连的是校园 WiFi、且没开代理/VPN。"
            head.isBlank() -> "网关返回了空响应。可能是网关繁忙或网络不通，请稍后重试。"
            else -> "网关返回了无法识别的内容：$head"
        }
    }
}
