package com.campus.autologin.data.remote

import android.net.Network
import com.campus.autologin.util.WifiNet
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.OkHttpClient
import okhttp3.Request
import java.net.URI

/** 一次探测的结论。 */
sealed interface ProbeOutcome {
    /** 抓到门户地址（通常带 wlanuserip 等参数）。 */
    data class Portal(val url: String, val trace: String) : ProbeOutcome

    /** 确认真的能上外网。 */
    data class Online(val trace: String) : ProbeOutcome

    /** 既没抓到门户，也没能证明在线（DNS 不通 / 全部超时等）。 */
    data class Inconclusive(val trace: String) : ProbeOutcome
}

/**
 * 校园网门户探测。
 *
 * 相比上一版的三处关键改动：
 *  1. **所有请求都钉在传入的 WiFi Network 上**（socket 级绑定），双网卡不再走 5G；
 *  2. **前三个探针用 IP 字面量**，认证前 DNS 常被劫持或不通，用 IP 才能稳定触发网关劫持；
 *  3. 锐捷网关经常返回 **HTTP 200 + JS/meta 跳转**（而不是 302），
 *     上一版只看 3xx，于是把这种情况误判成"已联网"。现在会解析响应体里的跳转地址。
 */
object NetworkProbe {

    private data class Probe(val url: String, val expect204: Boolean)

    private val PROBES = listOf(
        // —— IP 字面量：不依赖 DNS，未认证时必被网关劫持 ——
        Probe("http://223.5.5.5/generate_204", true),
        Probe("http://119.29.29.29/", false),
        Probe("http://114.114.114.114/", false),
        // —— 各家系统探针 ——
        Probe("http://connectivitycheck.gstatic.com/generate_204", true),
        Probe("http://connect.rom.miui.com/generate_204", true),
        Probe("http://connect.hicloud.com/generate_204", true),
        Probe("http://www.msftconnecttest.com/connecttest.txt", false),
        // —— 国内站点：几乎一定被校园网关拦 ——
        Probe("http://www.baidu.com/", false),
        Probe("http://www.qq.com/", false)
    )

    /** 响应体里的门户跳转：优先找 index.jsp?...，再找通用 location.href。 */
    private val INDEX_JSP = Regex(
        """https?://[^'"\s>)]*index\.jsp\?[^'"\s>)]+""",
        RegexOption.IGNORE_CASE
    )
    private val JS_REDIRECT = Regex(
        """(?:location(?:\s*\.\s*(?:href|replace))?\s*(?:=|\(\s*)|url\s*=\s*)['"]?(https?://[^'"\s>)]+)""",
        RegexOption.IGNORE_CASE
    )

    private const val BODY_LIMIT = 8_192L

    /** 全部探针的总时间预算。 */
    private const val TOTAL_BUDGET_MS = 14_000L

    /**
     * 依次打探针，第一个有结论的即返回。
     * @param net 绑定用的 WiFi 网络；null 时走默认网络。
     */
    suspend fun detect(net: Network?): ProbeOutcome = withContext(Dispatchers.IO) {
        val client = WifiNet.client(net, timeoutMs = 4_000L, followRedirects = false)
        val trace = StringBuilder()
        var sawOnline = false
        // 总时长上限：WiFi 完全不通时 9 个探针串行超时会让界面转圈太久
        val deadline = System.currentTimeMillis() + TOTAL_BUDGET_MS

        for (probe in PROBES) {
            if (System.currentTimeMillis() > deadline) {
                trace.append("(超时中止)")
                break
            }
            val tag = shortHost(probe.url)
            val step = runCatching { probeOne(client, probe) }
                .getOrElse { Step.Nothing(it.javaClass.simpleName) }
            trace.append(tag).append('→').append(step.note).append(' ')
            when (step) {
                is Step.PortalFound -> return@withContext ProbeOutcome.Portal(
                    step.url, trace.toString().trim()
                )
                is Step.OnlineSignal -> {
                    // 拿到一次在线信号先记下，但继续再打 1 个探针交叉确认，
                    // 避免个别站点未被拦截造成误判
                    if (sawOnline) return@withContext ProbeOutcome.Online(trace.toString().trim())
                    sawOnline = true
                }
                is Step.Nothing -> Unit
            }
        }
        if (sawOnline) ProbeOutcome.Online(trace.toString().trim())
        else ProbeOutcome.Inconclusive(trace.toString().trim())
    }

    /** 旧接口兼容：只要门户 URL。 */
    suspend fun detectCaptivePortal(net: Network? = null): String? =
        (detect(net) as? ProbeOutcome.Portal)?.url

    // ------------------------------------------------------------------

    private sealed interface Step {
        val note: String

        data class PortalFound(val url: String) : Step {
            override val note = "门户"
        }

        data class OnlineSignal(override val note: String) : Step

        data class Nothing(override val note: String) : Step
    }

    private fun probeOne(client: OkHttpClient, probe: Probe): Step {
        val request = Request.Builder().url(probe.url).get()
            .header("User-Agent", UA)
            .build()
        client.newCall(request).execute().use { response ->
            val code = response.code

            // 1) 标准重定向
            if (code in 300..399) {
                val loc = response.header("Location")
                if (loc.isNullOrBlank()) return Step.Nothing("3xx无Location")
                val reqHost = response.request.url.host
                val locHost = runCatching { URI(loc).host }.getOrNull() ?: reqHost
                val looksPortal = loc.contains("index.jsp", true) ||
                    loc.contains("eportal", true) ||
                    loc.contains("wlanuserip", true) ||
                    !locHost.equals(reqHost, ignoreCase = true)
                return if (looksPortal) Step.PortalFound(loc) else Step.Nothing("同站跳转")
            }

            // 2) 204：真正在线
            if (code == 204) return Step.OnlineSignal("204")

            // 3) 200：可能是网关的"伪 200 + JS 跳转"页
            val body = runCatching {
                response.peekBody(BODY_LIMIT).string()
            }.getOrDefault("")
            extractPortal(body)?.let { return Step.PortalFound(it) }

            if (code == 200) {
                // 期望 204 却拿到 200，且体内没有跳转 → 内容被替换，仍按门户可疑处理
                return if (probe.expect204) Step.Nothing("200≠204")
                else Step.OnlineSignal("200")
            }
            return Step.Nothing(code.toString())
        }
    }

    private fun extractPortal(body: String): String? {
        if (body.isBlank()) return null
        INDEX_JSP.find(body)?.value?.let { return it }
        val m = JS_REDIRECT.find(body)?.groupValues?.getOrNull(1) ?: return null
        return m.takeIf {
            it.contains("eportal", true) || it.contains("wlanuserip", true) ||
                it.contains("index.jsp", true) || it.contains("login", true)
        }
    }

    private fun shortHost(url: String): String =
        runCatching { URI(url).host.orEmpty() }.getOrDefault("").ifBlank { url }
            .removePrefix("www.").removePrefix("connectivitycheck.")

    private const val UA =
        "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 (KHTML, like Gecko) " +
            "Chrome/120.0.0.0 Mobile Safari/537.36"
}
