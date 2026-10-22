# 微信活动报名助手

<img src="app_icon.png" width="128" alt="微信活动报名助手图标">

一个面向 macOS 和 Windows 的桌面辅助工具。它通过窗口截图、OCR 和相对坐标校准，辅助完成微信小程序活动报名页面中的重复操作。

> 本项目仅用于学习自动化与 OCR 技术。请遵守微信及相关活动平台的规则，只操作你有权使用的账号，并在运行前确认目标活动允许相应操作。

## 功能

- 三级 OCR：RapidOCR、macOS Vision、Tesseract。
- 严格锁定微信小程序窗口，降低误操作微信主窗口的风险。
- 支持“报名按钮、身份、表单确认、弹窗确认”四步位置校准。
- 支持“学习我的点击”，手动录制完整流程后按顺序回放。
- 支持提前连点报名按钮、失败报告和倒计时。
- 身份状态无法确认时停止，而不是猜测后提交。
- 支持浅色/深色主题、字体、背景图片和窗口置顶。
- Windows 打包版包含 `--selftest` 环境自检。

完整操作说明见 [docs/使用说明.md](docs/使用说明.md)。

## 环境要求

- Python 3.12 推荐用于 Windows 构建。
- macOS 当前构建环境使用 Python 3.14，并支持 Apple Silicon。
- macOS 需要“辅助功能”和“屏幕录制”权限。
- Windows 10/11 64 位。

## 从源码运行

### macOS

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python wechat_signup_app.py
```

也可以双击 `启动微信报名助手.command`。首次运行会在当前目录创建 `.venv` 并安装依赖。

### Windows

```bat
python -m venv .venv
.venv\Scripts\activate
python -m pip install -r requirements.txt
python wechat_signup_app.py
```

## 构建

### Windows

在 Windows 10/11 的 Python 3.12 环境中双击：

```text
build_windows.bat
```

产物位于 `dist\WeChatSignupAssistant\`。需要复制整个目录，不能只复制 EXE。

构建完成后可运行环境自检：

```bat
dist\WeChatSignupAssistant\WeChatSignupAssistant.exe --selftest
```

### macOS

```bash
python -m pip install -r requirements.txt pyinstaller
python -m PyInstaller --noconfirm --clean 微信活动报名助手.spec
```

## 配置与日志

macOS：

```text
~/Library/Application Support/WeChatSignupAssistant/
```

Windows 打包版：

```text
WeChatSignupAssistant.exe 同目录
```

`wechat_signup_app.json` 保存校准位置、目标窗口和点击序列；`wechat_signup_app.log` 保存运行日志。这些文件包含本机运行数据，不应提交到 Git。

## 注意事项

- 不保证任何活动都能成功提交，最终结果以微信页面显示为准。
- 极速直点不会逐步执行 OCR 校验，窗口尺寸、页面状态和校准位置必须正确。
- 窗口移动通常不影响相对坐标，但窗口尺寸变化后应重新校准。
- 打包程序没有商业代码签名，Windows SmartScreen 或 macOS Gatekeeper 可能显示安全提示。
- 运行前请确认页面上所选身份和最终结果，避免错误提交。

## 目录结构

```text
.
├── .github/workflows/       # 自动语法检查
├── docs/使用说明.md         # 完整使用文档
├── app_icon.*               # 应用图标
├── wechat_signup_app.py     # 主程序
├── requirements.txt         # Python 依赖
├── build_windows.bat        # Windows 构建入口
├── windows_build.spec       # Windows PyInstaller 配置
└── 微信活动报名助手.spec     # macOS PyInstaller 配置
```
