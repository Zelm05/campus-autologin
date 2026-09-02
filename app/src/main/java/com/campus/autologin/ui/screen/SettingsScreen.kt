package com.campus.autologin.ui.screen

import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
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
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.ArrowBack
import androidx.compose.material.icons.filled.Lock
import androidx.compose.material.icons.filled.Visibility
import androidx.compose.material.icons.filled.VisibilityOff
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.runtime.collectAsState
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.text.input.VisualTransformation
import androidx.compose.ui.unit.dp
import androidx.lifecycle.viewmodel.compose.viewModel
import androidx.navigation.NavController
import com.campus.autologin.BuildConfig
import com.campus.autologin.data.model.LoginResult
import com.campus.autologin.data.remote.CampusConfig
import com.campus.autologin.service.AutoLoginService
import com.campus.autologin.ui.viewmodel.SettingsViewModel
import com.campus.autologin.util.BatteryOpt
import kotlinx.coroutines.delay

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun SettingsScreen(nav: NavController, vm: SettingsViewModel = viewModel()) {
    val cfg by vm.config.collectAsState()
    val password by vm.password.collectAsState()
    val testResult by vm.testResult.collectAsState()
    val testing by vm.testing.collectAsState()
    var showPassword by remember { mutableStateOf(false) }

    Scaffold(
        containerColor = MaterialTheme.colorScheme.background,
        topBar = {
            TopAppBar(
                title = { Text("设置", fontWeight = androidx.compose.ui.text.font.FontWeight.Bold) },
                navigationIcon = {
                    IconButton(onClick = { nav.popBackStack() }) {
                        Icon(Icons.Filled.ArrowBack, contentDescription = "返回")
                    }
                },
                actions = {
                    Text(
                        "已保存",
                        color = MaterialTheme.colorScheme.secondary,
                        style = MaterialTheme.typography.labelMedium,
                        modifier = Modifier.padding(end = 16.dp)
                    )
                }
            )
        }
    ) { padding ->
        Column(
            modifier = Modifier
                .fillMaxSize()
                .padding(padding)
                .verticalScroll(rememberScrollState())
                .padding(16.dp)
        ) {
            SectionTitle("账号")
            Card(
                modifier = Modifier.fillMaxWidth(),
                shape = RoundedCornerShape(18.dp),
                colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface)
            ) {
                Column {
                    SettingInput(
                        label = "学号",
                        value = cfg.username,
                        keyboardType = KeyboardType.Number,
                        onValueChange = { vm.update { copy(username = it) } }
                    )
                    SettingInput(
                        label = "密码",
                        value = password,
                        isPassword = true,
                        showPassword = showPassword,
                        onTogglePassword = { showPassword = !showPassword },
                        onValueChange = vm::setPassword
                    )
                    SettingSwitch(
                        label = "保存密码",
                        checked = cfg.savePassword,
                        onCheckedChange = { vm.update { copy(savePassword = it) } }
                    )
                }
            }

            SectionTitle("自动化")
            Card(
                modifier = Modifier.fillMaxWidth(),
                shape = RoundedCornerShape(18.dp),
                colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface)
            ) {
                Column {
                    SettingSwitch(
                        label = "连接校园网时自动登录",
                        checked = cfg.autoLogin,
                        onCheckedChange = { vm.update { copy(autoLogin = it) } }
                    )
                    SettingSwitch(
                        label = "开机自启动后台监控",
                        checked = cfg.autoStartOnBoot,
                        onCheckedChange = { vm.update { copy(autoStartOnBoot = it) } }
                    )
                    SettingSwitch(
                        label = "始终在后台运行",
                        checked = cfg.alwaysRunInBackground,
                        onCheckedChange = { vm.update { copy(alwaysRunInBackground = it) } }
                    )
                    SettingSwitch(
                        label = "登录失败时通知我",
                        checked = cfg.notifyOnFailure,
                        onCheckedChange = { vm.update { copy(notifyOnFailure = it) } }
                    )
                    SettingSwitch(
                        label = "显示\"监控中\"通知",
                        checked = cfg.showMonitorNotification,
                        onCheckedChange = { vm.update { copy(showMonitorNotification = it) } }
                    )
                }
            }

            SectionTitle("后台保活")
            KeepAliveCard()

            SectionTitle("校园网配置")
            LockedConfigCard()

            SectionTitle("关于")
            Card(
                modifier = Modifier.fillMaxWidth(),
                shape = RoundedCornerShape(18.dp),
                colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface)
            ) {
                Column {
                    SettingInfoRow("版本", "v${BuildConfig.VERSION_NAME}")
                    SettingInfoRow("开源许可", "MIT")
                    SettingInfoRow("开发者", "@Zelm05")
                }
            }

            Spacer(Modifier.height(20.dp))
            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                Button(
                    onClick = {
                        vm.save()
                        nav.popBackStack()
                    },
                    modifier = Modifier
                        .weight(1f)
                        .height(50.dp),
                    shape = RoundedCornerShape(14.dp)
                ) { Text("保存") }
                OutlinedButton(
                    onClick = vm::testLogin,
                    enabled = !testing,
                    modifier = Modifier
                        .weight(1f)
                        .height(50.dp),
                    shape = RoundedCornerShape(14.dp)
                ) {
                    if (testing) {
                        CircularProgressIndicator(
                            Modifier.size(18.dp),
                            strokeWidth = 2.dp,
                            color = MaterialTheme.colorScheme.primary
                        )
                        Spacer(Modifier.size(8.dp))
                        Text("测试中…")
                    } else {
                        Text("测试登录")
                    }
                }
            }

            testResult?.let { result ->
                Spacer(Modifier.height(14.dp))
                val color = when (result) {
                    is LoginResult.Success -> MaterialTheme.colorScheme.primary
                    is LoginResult.Failure -> MaterialTheme.colorScheme.error
                    is LoginResult.Error -> MaterialTheme.colorScheme.error
                }
                Card(
                    modifier = Modifier.fillMaxWidth(),
                    shape = RoundedCornerShape(18.dp),
                    colors = CardDefaults.cardColors(containerColor = color.copy(alpha = 0.10f))
                ) {
                    Column(Modifier.fillMaxWidth().padding(16.dp)) {
                        Text(
                            when (result) {
                                is LoginResult.Success -> "测试成功"
                                is LoginResult.Failure -> "测试失败"
                                is LoginResult.Error -> "测试错误"
                            },
                            color = color,
                            fontWeight = androidx.compose.ui.text.font.FontWeight.Bold
                        )
                        Spacer(Modifier.height(4.dp))
                        Text(result.message, style = MaterialTheme.typography.bodyMedium)
                    }
                }
                Spacer(Modifier.height(8.dp))
                OutlinedButton(onClick = vm::clearTest, modifier = Modifier.fillMaxWidth()) {
                    Text("清除测试结果")
                }
            }

            Spacer(Modifier.height(12.dp))
            OutlinedButton(
                onClick = { nav.navigate("about") },
                modifier = Modifier.fillMaxWidth().height(48.dp),
                shape = RoundedCornerShape(14.dp)
            ) {
                Text("关于应用")
            }
        }
    }
}

/**
 * 后台保活状态卡。
 *
 * 这一页不是为了好看，是为了回答"我明明开了自启动，为什么还是掉后台"：
 * 把三条真正决定生死的状态摆出来——前台服务在不在、电池优化有没有豁免、
 * 厂商省电策略有没有放行，并给出直达系统的入口。
 */
@Composable
private fun KeepAliveCard() {
    val context = LocalContext.current
    var serviceOn by remember { mutableStateOf(AutoLoginService.isRunning) }
    var startError by remember { mutableStateOf(AutoLoginService.lastStartError) }
    var ignoring by remember { mutableStateOf(BatteryOpt.isIgnoring(context)) }

    // 设置页停留期间每 2 秒刷一次，从系统设置返回时能立刻看到变化
    LaunchedEffect(Unit) {
        while (true) {
            serviceOn = AutoLoginService.isRunning
            startError = AutoLoginService.lastStartError
            ignoring = BatteryOpt.isIgnoring(context)
            delay(2000)
        }
    }

    Card(
        modifier = Modifier.fillMaxWidth(),
        shape = RoundedCornerShape(18.dp),
        colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface)
    ) {
        Column {
            KeepAliveRow(
                label = "后台监控",
                value = if (serviceOn) "运行中" else "未运行",
                valueColor = if (serviceOn) MaterialTheme.colorScheme.primary
                else MaterialTheme.colorScheme.error
            )
            if (!serviceOn) {
                Text(
                    startError?.let { "上次拉起失败：$it" }
                        ?: "打开 App 后会自动恢复；若反复停止，请完成下面两项设置。",
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    modifier = Modifier.padding(start = 16.dp, end = 16.dp, bottom = 12.dp)
                )
            }
            KeepAliveRow(
                label = "电池优化",
                value = if (ignoring) "已豁免" else "未豁免，点此设置",
                valueColor = if (ignoring) MaterialTheme.colorScheme.primary
                else MaterialTheme.colorScheme.error,
                onClick = { if (!ignoring) BatteryOpt.requestIgnore(context) }
            )
            KeepAliveRow(
                label = "自启动与省电策略",
                value = "去系统设置",
                valueColor = MaterialTheme.colorScheme.primary,
                onClick = { BatteryOpt.openAppDetails(context) }
            )
            Text(
                "国内 ROM（MIUI / 鸿蒙 / ColorOS / OriginOS 等）还有各自的后台冻结策略，" +
                    "需要在上面的系统设置里把本应用加入「自启动」和「后台运行」白名单，" +
                    "否则熄屏一段时间后系统仍会回收监控。",
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                modifier = Modifier.padding(start = 16.dp, end = 16.dp, top = 4.dp, bottom = 14.dp)
            )
        }
    }
}

@Composable
private fun KeepAliveRow(
    label: String,
    value: String,
    valueColor: androidx.compose.ui.graphics.Color,
    onClick: (() -> Unit)? = null
) {
    val modifier = onClick?.let { cb ->
        Modifier
            .fillMaxWidth()
            .clickable { cb() }
            .padding(horizontal = 16.dp, vertical = 14.dp)
    } ?: Modifier
        .fillMaxWidth()
        .padding(horizontal = 16.dp, vertical = 14.dp)
    Row(modifier = modifier, verticalAlignment = Alignment.CenterVertically) {
        Text(label, style = MaterialTheme.typography.bodyLarge, modifier = Modifier.weight(1f))
        Text(
            value,
            style = MaterialTheme.typography.bodyMedium,
            color = valueColor,
            fontWeight = androidx.compose.ui.text.font.FontWeight.SemiBold
        )
    }
}

@Composable
private fun SectionTitle(text: String) {
    Text(
        text,
        style = MaterialTheme.typography.labelMedium,
        color = MaterialTheme.colorScheme.onSurfaceVariant,
        fontWeight = androidx.compose.ui.text.font.FontWeight.Bold,
        modifier = Modifier.padding(start = 4.dp, top = 16.dp, bottom = 8.dp)
    )
}

@Composable
private fun SettingInput(
    label: String,
    value: String,
    isPassword: Boolean = false,
    showPassword: Boolean = false,
    keyboardType: KeyboardType = KeyboardType.Text,
    onTogglePassword: () -> Unit = {},
    onValueChange: (String) -> Unit
) {
    OutlinedTextField(
        value = value,
        onValueChange = onValueChange,
        label = { Text(label) },
        modifier = Modifier
            .fillMaxWidth()
            .padding(horizontal = 14.dp, vertical = 6.dp),
        singleLine = true,
        keyboardOptions = KeyboardOptions(keyboardType = keyboardType),
        visualTransformation = if (isPassword && !showPassword)
            PasswordVisualTransformation() else VisualTransformation.None,
        trailingIcon = if (isPassword) {
            {
                IconButton(onClick = onTogglePassword) {
                    Icon(
                        if (showPassword) Icons.Filled.VisibilityOff else Icons.Filled.Visibility,
                        contentDescription = "显示密码"
                    )
                }
            }
        } else null,
        shape = RoundedCornerShape(12.dp)
    )
}

@Composable
private fun SettingSwitch(label: String, checked: Boolean, onCheckedChange: (Boolean) -> Unit) {
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .padding(horizontal = 16.dp, vertical = 14.dp),
        verticalAlignment = Alignment.CenterVertically
    ) {
        Text(label, style = MaterialTheme.typography.bodyLarge, modifier = Modifier.weight(1f))
        Switch(checked = checked, onCheckedChange = onCheckedChange)
    }
}

@Composable
private fun SettingInfoRow(label: String, value: String) {
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .padding(horizontal = 16.dp, vertical = 14.dp),
        verticalAlignment = Alignment.CenterVertically
    ) {
        Text(label, style = MaterialTheme.typography.bodyLarge)
        Spacer(Modifier.weight(1f))
        Text(
            value,
            style = MaterialTheme.typography.bodyMedium,
            color = MaterialTheme.colorScheme.onSurfaceVariant
        )
    }
}

@Composable
private fun LockedConfigCard() {
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(14.dp))
            .background(androidx.compose.ui.graphics.Brush.verticalGradient(
                listOf(
                    MaterialTheme.colorScheme.primaryContainer.copy(alpha = 0.6f),
                    MaterialTheme.colorScheme.primaryContainer.copy(alpha = 0.35f)
                )
            ))
            .padding(14.dp),
        verticalAlignment = Alignment.CenterVertically
    ) {
        Box(
            Modifier
                .size(40.dp)
                .clip(RoundedCornerShape(12.dp))
                .background(MaterialTheme.colorScheme.surface),
            contentAlignment = Alignment.Center
        ) {
            Icon(
                Icons.Filled.Lock,
                contentDescription = null,
                tint = MaterialTheme.colorScheme.primary,
                modifier = Modifier.size(20.dp)
            )
        }
        Spacer(Modifier.size(12.dp))
        Column(Modifier.weight(1f)) {
            Text(
                "已内置 ${CampusConfig.SCHOOL_NAME} 配置",
                style = MaterialTheme.typography.bodyMedium,
                fontWeight = androidx.compose.ui.text.font.FontWeight.Bold
            )
            Text(
                "网关 · 认证协议 · 字段名 已预置，勿手动修改",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant
            )
        }
    }
}
