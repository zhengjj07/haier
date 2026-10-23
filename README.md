# Haier 集成 1.3.11（热水器修改版）

基于 banto6/haier 1.3.0 修改，增加电热水器温度/模式控制问题。

1.打开了热水器控制温度调节的模式选择开关，包括整胆、半胆、增容、四季温水、智能抑菌

2.打开了预约用水功能。

3.修改字段显示名称以便于理解

<img width="501" height="865" alt="屏幕截图 2026-10-23 125733" src="https://github.com/user-attachments/assets/3128bb26-09df-4414-acba-dfafbc138a6d" />



## 安装
1. 把 `custom_components/haier` 放到配置目录，或通过 HACS 添加自定义仓库。
2. 重启 HA。
3. **删除旧 Haier 集成再重新添加**
