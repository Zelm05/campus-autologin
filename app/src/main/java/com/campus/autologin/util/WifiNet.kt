package com.campus.autologin.util

import android.content.Context
import android.net.ConnectivityManager
import android.net.Network
import android.net.NetworkCapabilities
import android.net.wifi.WifiManager
import okhttp3.ConnectionPool
import okhttp3.Dns
import okhttp3.OkHttpClient
import java.net.InetAddress
import java.util.concurrent.TimeUnit

/**
 * WiFi 网络定位 + **socket 级**绑定。
 *
 * 为什么不用 ConnectivityManager.bindProcessToNetwork：
 *  1. 部分 ROM（华为/荣耀 HarmonyOS、部分 MIUI）对进程级绑定有额外限制，
 *     返回 true 但流量仍走默认网络（移动数据）；
 *  2. 即使绑定成功，OkHttp 的 ConnectionPool 里可能还留着**之前经由 5G 建立**的
 *     keep-alive socket，请求被复用到旧连接上，等于绑定完全失效。
 *
 * 正确做法：用目标 Network 的 socketFactory 建连接、用该 Network 自己的 DNS 解析，
 * 并禁用连接复用。这样每一个 socket 在内核层面就绑在 WiFi 上，与 ROM 策略无关。
 */
object WifiNet {

    /** 当前的 WiFi 网络（排除 VPN）。 */
    fun wifi(ctx: Context): Network? {
        val cm = ctx.getSystemService(ConnectivityManager::class.java) ?: return null
        @Suppress("DEPRECATION")
        return cm.allNetworks.firstOrNull { net ->
            val caps = cm.getNetworkCapabilities(net)
            caps != null &&
                caps.hasTransport(NetworkCapabilities.TRANSPORT_WIFI) &&
                !caps.hasTransport(NetworkCapabilities.TRANSPORT_VPN)
        }
    }

    /** 是否存在正在使用的移动数据（用于提示"双网卡"）。 */
    fun hasCellular(ctx: Context): Boolean {
        val cm = ctx.getSystemService(ConnectivityManager::class.java) ?: return false
        @Suppress("DEPRECATION")
        return cm.allNetworks.any { net ->
            cm.getNetworkCapabilities(net)
                ?.hasTransport(NetworkCapabilities.TRANSPORT_CELLULAR) == true
        }
    }

    fun isDualNetwork(ctx: Context): Boolean = wifi(ctx) != null && hasCellular(ctx)

    /**
     * 系统是否认为这条 WiFi 已经"能上网"。
     * 未认证的校园 WiFi 拿不到 NET_CAPABILITY_VALIDATED —— 这是**最可靠**的
     * "需要登录"信号，比任何 HTTP 探针都准。
     */
    fun isValidated(ctx: Context, net: Network?): Boolean {
        net ?: return false
        val cm = ctx.getSystemService(ConnectivityManager::class.java) ?: return false
        return cm.getNetworkCapabilities(net)
            ?.hasCapability(NetworkCapabilities.NET_CAPABILITY_VALIDATED) == true
    }

    /** 系统已明确标记该网络处于 captive portal 状态。 */
    fun hasCaptiveFlag(ctx: Context, net: Network?): Boolean {
        net ?: return false
        val cm = ctx.getSystemService(ConnectivityManager::class.java) ?: return false
        return cm.getNetworkCapabilities(net)
            ?.hasCapability(NetworkCapabilities.NET_CAPABILITY_CAPTIVE_PORTAL) == true
    }

    // 说明：系统探测器抓到的门户地址（LinkProperties.getCaptivePortalData）是
    // @SystemApi，普通应用无法读取，因此门户地址只能靠 NetworkProbe 自己探。

    /**
     * 构造一个"钉死在指定网络上"的 OkHttpClient。
     *
     * @param net 目标网络；null 时退化为默认网络。
     * @param timeoutMs 连接/读取超时。
     * @param followRedirects 探测时必须为 false（要读 Location），登录时可为 true。
     * @param fallbackDns 是否允许在目标网络自身 DNS 解析失败时回退到系统 DNS。
     *        **探测时必须为 false**：开着移动数据时，系统 DNS 走的是 4G，
     *        会把 baidu 等解析成真实公网 IP，再经 WiFi socket 直连——
     *        若网关只劫持域名、放行 IP 直连，就会把"未认证"误判成"已在线"。
     *        登录/在线查询保持 true（网关域名用 4G DNS 也能解析到正确地址）。
     */
    fun client(
        net: Network?,
        timeoutMs: Long = 8_000L,
        followRedirects: Boolean = false,
        fallbackDns: Boolean = true
    ): OkHttpClient {
        val builder = OkHttpClient.Builder()
            .connectTimeout(timeoutMs, TimeUnit.MILLISECONDS)
            .readTimeout(timeoutMs, TimeUnit.MILLISECONDS)
            .writeTimeout(timeoutMs, TimeUnit.MILLISECONDS)
            .followRedirects(followRedirects)
            .followSslRedirects(followRedirects)
            .retryOnConnectionFailure(true)
            // maxIdleConnections = 0：禁止复用旧 socket（可能是 5G 上建立的）
            .connectionPool(ConnectionPool(0, 1, TimeUnit.SECONDS))

        if (net != null) {
            builder.socketFactory(net.socketFactory)
            builder.dns(object : Dns {
                override fun lookup(hostname: String): List<InetAddress> {
                    // 优先用 WiFi 自己的 DNS（校园网 DNS 在认证前也能解析网关域名）
                    val viaWifi = runCatching {
                        net.getAllByName(hostname).filterNotNull()
                    }.getOrDefault(emptyList())
                    if (viaWifi.isNotEmpty()) return viaWifi
                    if (fallbackDns) {
                        return runCatching { Dns.SYSTEM.lookup(hostname) }.getOrDefault(emptyList())
                    }
                    return emptyList()
                }
            })
        }
        return builder.build()
    }

    // ---- 网络标识：用于记住"免认证网络" ----

    /**
     * 当前 WiFi 名称。
     *
     * Android 8.0+ 起读取 SSID 需要位置权限（Android 10+ 还要求打开位置开关），
     * 没有权限时系统统一返回 "<unknown ssid>"。本 App 不申请位置权限，
     * 所以这里通常拿到 null —— 由 [networkKey] 自动退回用网关 IP 做标识。
     */
    fun ssid(ctx: Context): String? {
        val wm = ctx.getSystemService(WifiManager::class.java) ?: return null
        @Suppress("DEPRECATION")
        val raw = wm.connectionInfo?.ssid ?: return null
        return raw.trim().trim('"')
            .takeIf { it.isNotBlank() && !it.equals("<unknown ssid>", ignoreCase = true) }
    }

    /** 当前 WiFi 的网关地址（来自 DHCP）。读取网关不需要位置权限。 */
    fun gateway(ctx: Context): String? {
        val wm = ctx.getSystemService(WifiManager::class.java) ?: return null
        val gw = runCatching { wm.dhcpInfo?.gateway }.getOrNull() ?: return null
        if (gw == 0) return null
        return formatIp(gw)
    }

    /**
     * 记忆"免认证网络"用的网络指纹。
     *
     * 优先 WiFi 名称（最准确、也最符合直觉），取不到时退回网关 IP
     * （需认证与免认证的校园网通常分属不同网段、网关不同，足以区分）。
     * 两者都拿不到时返回 null —— 此时不记忆，每次仍由探针判定，功能不失效。
     */
    fun networkKey(ctx: Context): String? {
        ssid(ctx)?.let { return "ssid:$it" }
        gateway(ctx)?.let { return "gw:$it" }
        return null
    }

    private fun formatIp(value: Int): String = listOf(
        value and 0xFF,
        value shr 8 and 0xFF,
        value shr 16 and 0xFF,
        value shr 24 and 0xFF
    ).joinToString(".")

    /**
     * 设备在当前 WiFi 下的网络信息（本机 IP / MAC）。
     *
     * 免认证网络在网关里查不到会话，信息卡改显示设备自己拿到的地址。
     * 注意：Android 6.0+ 起普通应用读不到真实 MAC（统一返回 02:00:00:00:00:00），
     * 拿不到就返回空串，交给界面决定不显示该行。
     */
    fun localNet(ctx: Context): Pair<String, String> {
        val wm = ctx.getSystemService(WifiManager::class.java) ?: return "" to ""
        @Suppress("DEPRECATION")
        val info = runCatching { wm.connectionInfo }.getOrNull() ?: return "" to ""
        val ip = info.ipAddress.takeIf { it != 0 }?.let(::formatIp).orEmpty()
        val mac = info.macAddress
            ?.takeIf { it.isNotBlank() && !it.equals("02:00:00:00:00:00", ignoreCase = true) }
            .orEmpty()
        return ip to mac
    }

    /** 一行诊断串，登录失败时展示给用户，便于精确定位。 */
    fun describe(ctx: Context): String {
        val net = wifi(ctx)
        return buildString {
            append("WiFi:").append(if (net != null) "已连接" else "无")
            if (net != null) {
                append(" 已联网校验:").append(if (isValidated(ctx, net)) "通过" else "未通过")
                append(" 门户标记:").append(if (hasCaptiveFlag(ctx, net)) "有" else "无")
            }
            append(" 移动数据:").append(if (hasCellular(ctx)) "开" else "关")
        }
    }
}
