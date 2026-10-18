# Haier（海尔智家）Home Assistant 集成 — 热水器调温修复版

本仓库是 [banto6/haier](https://github.com/banto6/haier) `1.3.0` 的分支
并已按 [HACS 官方对集成仓库的规范](https://www.hacs.xyz/docs/publish/integration/) 组织好目录结构，
可作为 **HACS 自定义仓库**安装。

在原版 1.3.0 上增加热水器「设定目标水温、切换加热模式」

## 二、仓库结构（符合 HACS 集成仓库规范）

```
haier-hacs-repo/
├── custom_components/
│   └── haier/                     ← 一个仓库只允许一个集成，且代码必须全部在此目录内
│       ├── brand/
│       │   └── icon.png           ← 品牌图标（HACS 发布要求提供）
│       ├── manifest.json          
│       ├── water_heater.py        ← 已修复
│       ├── core/attribute.py      ← 已修复
│       └── ... 其余原版文件
├── hacs.json                      ← HACS 识别文件
├── LICENSE
└── README.md
```

---
- 上游项目：https://github.com/banto6/haier
- 反馈问题时请附上：热水器型号、`.storage/haier/device_<deviceId>.json` 缓存内容（可脱敏）、
  以及开启 `custom_components.haier: debug` 后的日志。

## 许可

沿用上游 MIT License，见 `LICENSE`。
