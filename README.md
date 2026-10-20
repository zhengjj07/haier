# Haier 集成 1.3.11（热水器修复版）

基于 banto6/haier 1.3.0 修改，修复电热水器温度/模式控制问题。

## 1.3.11 更新

1. **移除「自动关机」功能**：该机型云端数字模型的 16 个属性里不含此字段，先后尝试 `autoOffStatus`、`autoPowerOffStatus` 等多个候选字段名下发命令均未生效（设备到温不停机）。本版按用户要求彻底放弃该功能：
   - 不再生成「自动关机」开关实体；
   - 删除全部 fallback 候选字段逻辑（避免向设备下发无效命令）。
2. **关闭调试日志，运行日志保持干净**：
   - 属性 dump 日志由 `WARNING` 降为 `DEBUG`——默认不再输出任何调试内容；如需排查，把 `custom_components.haier` 的日志级别临时设为 `debug` 即可恢复；
   - 删除 `Sending fallback command ...`、`water heater feature ... not found` 等提示性 WARNING；
   - `set_temperature called without temperature value` 由 `WARNING` 降为 `DEBUG`。
3. **保留并继承 1.3.8 ~ 1.3.10 的全部修复**：
   - 加热模式列表已清理（不含「未设置 / 0 / 1 / 2 / 3」等脏值）；
   - 四季温水（绑定真实字段 `heatKeepingStatus`）、智能抑菌开关正常；
   - 预约实体命名：预约开关 / 预约时间（时）/ 预约时间（分）/ 预约温度；
   - 「预约1每日循环」不再生成（该开关在设备端无法关闭，海尔智家 APP 也无对应设置）；
   - WaterHeater 卡片「当前温度」取自真实字段 `currentTemperature`，不再等于目标温度。

## 安装
1. 把 `custom_components/haier` 放到配置目录，或通过 HACS 添加自定义仓库。
2. 重启 HA。
3. **删除旧 Haier 集成再重新添加**（本版移除了「自动关机」实体与「预约1每日循环」，需重装集成才会从实体注册表里彻底清除残留）。
