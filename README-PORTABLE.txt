微信抢活动助手 · 便携版说明
================================

【这是什么】
这个文件夹是完整的便携版：Python 解释器、OCR 识别库、识别模型全都在里面。
拖到任何一台 Windows 10/11（64 位）电脑上都能直接运行，不需要装 Python，
也不需要装任何依赖。

【怎么用】
1. 双击 WeChatSignupAssistant.exe
2. 第一次打开如果 Windows 弹出「已保护你的电脑」：
   点「更多信息」→「仍要运行」（程序没有买数字签名，属于正常提示）
3. 打开微信、进入指尖派小程序，然后在助手里：
   选择目标窗口 → 校准位置（或「学习我的点击」）→ 填开放时间 → 开始等待

【重要】整个文件夹要一起用
不要把 exe 单独拖出来，它旁边的 _internal 文件夹里放着运行库和识别模型。
移动就整个文件夹一起移动。文件夹可以改名字，也可以放到任意位置。

【配置和日志在哪里】
就在 exe 同目录：
  wechat_signup_app.json  设置、位置预设、录制的点击序列
  wechat_signup_app.log   运行日志
复制整个文件夹 = 连配置一起复制；删掉 json 就等于恢复出厂设置。
多账号就复制多份文件夹，每份互相独立。

【实用命令】（在文件夹里打开命令提示符后执行）
  WeChatSignupAssistant.exe --selftest    环境自检，结果写入 selftest_report.txt
  WeChatSignupAssistant.exe --reset-ui    外观设置乱了看不清界面时，用它恢复默认

【如果打不开】
1. 提示缺少 DLL（比如 VCRUNTIME140.dll）：
   装一次「Microsoft Visual C++ 2015-2022 Redistributable (x64)」即可，微软官网免费下载。
2. 被杀毒软件拦：PyInstaller 打包的程序偶尔会被误报，添加信任即可。
3. 双击没反应：确认文件夹完整（exe 旁边应该有 _internal 文件夹）。
