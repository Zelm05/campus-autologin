package com.campus.autologin.ui.screen

import android.content.Context
import android.net.ConnectivityManager
import android.net.Network
import android.net.NetworkCapabilities
import androidx.compose.animation.core.RepeatMode
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.tween
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Error
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material.icons.filled.Check
import androidx.compose.material.icons.filled.Login
import androidx.compose.material.icons.filled.Logout
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBar
import androidx.compose.material3.pulltorefresh.PullToRefreshBox
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.collectAsState
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.scale
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.lifecycle.viewmodel.compose.viewModel
import androidx.navigation.NavController
import com.campus.autologin.R
import com.campus.autologin.data.model.ConnectionState
import com.campus.autologin.data.model.LoginResult
import com.campus.autologin.data.model.UserInfo
import com.campus.autologin.data.remote.CampusConfig
import com.campus.autologin.ui.viewmodel.MainViewModel
import com.campus.autologin.ui.viewmodel.MainUiState
import com.campus.autologin.ui.viewmodel.greetingForHour
import java.util.Calendar

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun MainScreen(nav: NavController, vm: MainViewModel = viewModel()) {
    val state by vm.uiState.collectAsState()
    val context = LocalContext.current

    // 每次回到主界面（含从设置页返回）都重新读取配置
    LaunchedEffect(Unit) { vm.refresh() }

    // 监听网络变化：WiFi 断开/重连/能力变化时自动刷新状态，
    // 不再需要手动下拉才能看到"未连接 / 已连接"
    DisposableEffect(Unit) {
        var lastValidated: Boolean? = null
        val cm = context.getSystemService(Context.CONNECTIVITY_SERVICE) as ConnectivityManager
        val callback = object : ConnectivityManager.NetworkCallback() {
            override fun onAvailable(network: Network) {
                vm.refresh()
            }

            override fun onLost(network: Network) {
                vm.refresh()
            }

            override fun onCapabilitiesChanged(network: Network, caps: NetworkCapabilities) {
                // 只在"联网校验通过"状态翻转时刷新，避免频繁回调刷屏
                val validated = caps.hasCapability(NetworkCapabilities.NET_CAPABILITY_VALIDATED)
                if (lastValidated == null || validated != lastValidated) {
                    lastValidated = validated
                    vm.refresh()
                }
            }
        }
        runCatching { cm.registerDefaultNetworkCallback(callback) }
        onDispose {
            runCatching { cm.unregisterNetworkCallback(callback) }
        }
    }

    Scaffold(
        containerColor = MaterialTheme.colorScheme.background,
        topBar = {
            TopAppBar(
                title = {
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        Image(
                            painter = painterResource(id = R.mipmap.ic_launcher),
                            contentDescription = null,
                            modifier = Modifier
                                .size(32.dp)
                                .clip(RoundedCornerShape(8.dp))
                        )
                        Spacer(Modifier.size(10.dp))
                        Text("校园网自动登录", fontWeight = FontWeight.Bold)
                    }
                },
                actions = {
                    IconButton(onClick = { nav.navigate("settings") }) {
                        Icon(Icons.Filled.Settings, contentDescription = "设置")
                    }
                }
            )
        }
    ) { padding ->
        // 下拉刷新：刷新一次 = 重读配置 + 探测状态 + 若需登录则自动登录一次
        PullToRefreshBox(
            isRefreshing = state.loading,
            onRefresh = { vm.refresh() },
            modifier = Modifier
                .fillMaxSize()
                .padding(padding)
        ) {
            Column(
                modifier = Modifier
                    .fillMaxSize()
                    .verticalScroll(rememberScrollState())
                    .padding(16.dp)
            ) {
                HeroCard(state)

                Spacer(Modifier.height(14.dp))
                InfoCard(state.userInfo)

                Spacer(Modifier.height(16.dp))
                Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                    Button(
                        onClick = vm::login,
                        enabled = !state.loading,
                        modifier = Modifier
                            .weight(2f)
                            .height(52.dp),
                        shape = RoundedCornerShape(14.dp)
                    ) {
                        if (state.loading) {
                            CircularProgressIndicator(
                                Modifier.size(18.dp),
                                strokeWidth = 2.dp,
                                color = MaterialTheme.colorScheme.onPrimary
                            )
                        } else {
                            Icon(Icons.Filled.Login, contentDescription = null)
                        }
                        Spacer(Modifier.size(8.dp))
                        Text(if (state.connection == ConnectionState.ONLINE) "重新连接" else "立即登录")
                    }
                    OutlinedButton(
                        onClick = vm::logout,
                        enabled = !state.loading,
                        modifier = Modifier
                            .weight(1f)
                            .height(52.dp),
                        shape = RoundedCornerShape(14.dp)
                    ) {
                        Icon(Icons.Filled.Logout, contentDescription = null)
                        Spacer(Modifier.size(8.dp))
                        Text("下线")
                    }
                }

                Spacer(Modifier.height(14.dp))
                SwitchRow("自动登录", "检测到校园网后自动提交", state.config.autoLogin, vm::toggleAutoLogin)

                state.lastResult?.let { result ->
                    Spacer(Modifier.height(14.dp))
                    ResultCard(result)
                }
            }
        }
    }
}

@Composable
private fun HeroCard(state: MainUiState) {
    // 实时在线只看探测结果；userInfo.online 是旧缓存（浏览器下线后仍为 true），
    // 若参与判定会导致状态永远卡在"已连接"
    val online = state.connection == ConnectionState.ONLINE
    // null 安全：greeting 有默认值但万一空串时回退到按时段再算一次
    val greeting = state.greeting.ifBlank {
        greetingForHour(Calendar.getInstance().get(Calendar.HOUR_OF_DAY))
    }
    // null 安全：name 取自 userInfo.name → username，都为空则不渲染
    val name = state.userInfo.name
        .ifBlank { state.config.username }
        .ifBlank { "" }
        .takeIf { it.isNotBlank() }

    Column(
        modifier = Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(24.dp))
            .background(
                Brush.linearGradient(
                    listOf(Color(0xFF2A2430), Color(0xFF1C1820), Color(0xFF25311F))
                )
            )
            .padding(20.dp),
        horizontalAlignment = Alignment.CenterHorizontally
    ) {
        Column(Modifier.fillMaxWidth(), horizontalAlignment = Alignment.Start) {
            Text(
                "$greeting 👋",
                color = Color.White.copy(alpha = 0.92f),
                style = MaterialTheme.typography.bodySmall
            )
            if (name != null) {
                Spacer(Modifier.height(3.dp))
                Text(
                    "$name 同学",
                    color = Color.White,
                    style = MaterialTheme.typography.titleLarge,
                    fontWeight = FontWeight.Bold
                )
            }
        }

        Spacer(Modifier.height(14.dp))
        StatusOrb(state)
        Spacer(Modifier.height(12.dp))

        val (title, subtitle) = statusText(state, online)
        Text(title, color = Color.White, style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.Bold)
        Spacer(Modifier.height(2.dp))
        Text(
            subtitle,
            color = Color.White.copy(alpha = 0.8f),
            style = MaterialTheme.typography.bodySmall
        )

        Spacer(Modifier.height(14.dp))
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .clip(RoundedCornerShape(12.dp))
                .background(Color.White.copy(alpha = 0.10f))
                .padding(horizontal = 12.dp, vertical = 9.dp),
            verticalAlignment = Alignment.CenterVertically
        ) {
            Box(
                Modifier
                    .size(8.dp)
                    .background(if (online) Color(0xFFA8D49E) else Color(0xFF9CA3AF), CircleShape)
            )
            Spacer(Modifier.size(8.dp))
            Text("当前账号", color = Color.White.copy(alpha = 0.9f), style = MaterialTheme.typography.bodySmall)
            Spacer(Modifier.weight(1f))
            Text(
                state.config.username.ifBlank { "未设置" },
                color = Color.White.copy(alpha = 0.9f),
                style = MaterialTheme.typography.bodySmall,
                fontWeight = FontWeight.Bold
            )
        }
    }
}

@Composable
private fun StatusOrb(state: MainUiState) {
    val online = state.connection == ConnectionState.ONLINE
    val loading = state.loading

    val ringColor = when {
        loading -> Color(0xFFF59E0B)
        online -> Color(0xFFA8C4A3)
        else -> Color(0xFF9CA3AF)
    }
    val innerColor = when {
        loading -> Color(0xFFFDE68A)
        online -> Color(0xFFEAF2E8)
        else -> Color(0xFFE5E7EB)
    }
    val iconColor = when {
        loading -> Color(0xFFB45309)
        online -> Color(0xFF4F7A48)
        else -> Color(0xFF6B7280)
    }

    val transition = rememberInfiniteTransition(label = "orb")
    val pulse by transition.animateFloat(
        initialValue = 1f,
        targetValue = 1.28f,
        animationSpec = infiniteRepeatable(tween(1400), RepeatMode.Restart),
        label = "orbPulse"
    )

    Box(contentAlignment = Alignment.Center, modifier = Modifier.size(124.dp)) {
        Box(
            Modifier
                .size(124.dp)
                .scale(pulse)
                .background(ringColor.copy(alpha = 0.30f), CircleShape)
        )
        Box(
            Modifier
                .size(112.dp)
                .clip(CircleShape)
                .background(ringColor.copy(alpha = 0.16f))
                .padding(6.dp),
            contentAlignment = Alignment.Center
        ) {
            Box(
                Modifier
                    .size(56.dp)
                    .background(innerColor, CircleShape),
                contentAlignment = Alignment.Center
            ) {
                if (loading) {
                    CircularProgressIndicator(
                        Modifier.size(30.dp),
                        strokeWidth = 3.dp,
                        color = iconColor
                    )
                } else {
                    Icon(
                        if (online) Icons.Filled.Check else Icons.Filled.Error,
                        contentDescription = null,
                        tint = iconColor,
                        modifier = Modifier.size(30.dp)
                    )
                }
            }
        }
    }
}

private fun statusText(state: MainUiState, online: Boolean): Pair<String, String> {
    val svc = state.userInfo.service.ifBlank { "互联网" }
    return when {
        state.loading -> "登录中…" to "正在提交认证"
        online -> "校园网已连接" to "服务：$svc"
        state.connection == ConnectionState.CAPTIVE -> "需登录/认证" to "检测到校园网认证门户"
        state.connection == ConnectionState.OFFLINE -> "未联网" to "请检查网络连接"
        else -> "检测中" to "正在检测网络状态…"
    }
}

@Composable
private fun InfoCard(info: UserInfo) {
    Card(
        modifier = Modifier.fillMaxWidth(),
        shape = RoundedCornerShape(18.dp),
        colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface)
    ) {
        Column(Modifier.fillMaxWidth().padding(16.dp)) {
            InfoRow("校园", CampusConfig.SCHOOL_NAME.orEmpty().ifBlank { "校园" }, "🏫")
            InfoRow("网关", CampusConfig.GATEWAY.removePrefix("http://").orEmpty(), "📡")
            InfoRow("本机 IP", info.ip.orEmpty().ifBlank { "—" }, "💻", info.wlanHex.orEmpty().ifBlank { "" })
            if (info.mac.isNotBlank()) InfoRow("MAC 地址", info.mac, "🔗")
        }
    }
}

@Composable
private fun InfoRow(label: String, value: String, emoji: String, tail: String = "") {
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .padding(vertical = 6.dp),
        verticalAlignment = Alignment.CenterVertically
    ) {
        Box(
            Modifier
                .size(36.dp)
                .clip(RoundedCornerShape(10.dp))
                .background(MaterialTheme.colorScheme.primaryContainer),
            contentAlignment = Alignment.Center
        ) {
            Text(emoji, style = MaterialTheme.typography.titleSmall)
        }
        Spacer(Modifier.size(12.dp))
        Column {
            Text(label, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
            Text(
                value.orEmpty().ifBlank { "—" },
                style = MaterialTheme.typography.bodyMedium,
                fontWeight = FontWeight.SemiBold
            )
        }
        if (tail.isNotBlank()) {
            Spacer(Modifier.weight(1f))
            Text(tail, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
    }
}

@Composable
private fun SwitchRow(title: String, desc: String, checked: Boolean, onChange: (Boolean) -> Unit) {
    Card(
        modifier = Modifier.fillMaxWidth(),
        shape = RoundedCornerShape(18.dp),
        colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface)
    ) {
        Row(
            modifier = Modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 14.dp),
            verticalAlignment = Alignment.CenterVertically
        ) {
            Column(Modifier.weight(1f)) {
                Text(title, style = MaterialTheme.typography.bodyLarge, fontWeight = FontWeight.SemiBold)
                Text(desc, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
            Switch(checked = checked, onCheckedChange = onChange)
        }
    }
}

@Composable
private fun ResultCard(result: LoginResult) {
    val (title, color) = when (result) {
        is LoginResult.Success -> "登录成功" to Color(0xFF4F7A48)
        is LoginResult.Failure -> "登录失败" to Color(0xFFDC2626)
        is LoginResult.Error -> "操作错误" to Color(0xFFDC2626)
    }
    Card(
        modifier = Modifier.fillMaxWidth(),
        shape = RoundedCornerShape(18.dp),
        colors = CardDefaults.cardColors(
            containerColor = color.copy(alpha = 0.10f)
        )
    ) {
        Column(Modifier.fillMaxWidth().padding(16.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                if (result is LoginResult.Failure || result is LoginResult.Error) {
                    Icon(Icons.Filled.Error, null, tint = color, modifier = Modifier.size(18.dp))
                    Spacer(Modifier.size(8.dp))
                }
                Text(title, color = color, fontWeight = FontWeight.Bold)
            }
            Spacer(Modifier.height(4.dp))
            Text(result.message.orEmpty().ifBlank { "（无详细信息）" }, style = MaterialTheme.typography.bodyMedium)
            val detail = when (result) {
                is LoginResult.Failure -> result.preview.orEmpty()
                is LoginResult.Success -> result.preview.orEmpty()
                is LoginResult.Error -> result.cause?.message.orEmpty()
            }
            if (detail.isNotBlank()) {
                Spacer(Modifier.height(8.dp))
                Text(
                    detail,
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant
                )
            }
        }
    }
}
