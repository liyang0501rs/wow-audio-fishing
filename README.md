# WoW Audio Fishing · Mac 纯音频钓鱼

在 Apple Silicon Mac 上通过 BlackHole 采集远程游戏音频，匹配咬钩声并向锁定的前台应用定向发键。支持网易 UU 远程和微软 Windows App 的使用场景。不截图、不读取游戏进程，不需要在远端安装这个工具。请先确认你的游戏和服务器规则允许这种自动化。

> 公开版不包含游戏音效，必须准备有权使用的咬钩 WAV。重新分发录音前请确认授权。

## 1. 安装

安装 Apple Silicon 原生 **Python 3.12（arm64，不用 Rosetta）**、[BlackHole 2ch](https://github.com/ExistentialAudio/BlackHole)，以及你使用的远程客户端。然后在 Mac 终端执行：

```bash
git clone https://github.com/liyang0501rs/wow-audio-fishing.git
cd wow-audio-fishing
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
mkdir -p user_templates recordings
```

## 2. 声音、权限与游戏键位

1. 在 UU 或 Windows App 启用远端声音播放。在 Mac「音频 MIDI 设置」创建包含 **BlackHole 2ch 和扬声器/耳机**的多输出设备，并设为系统输出。要只采集路由到 BlackHole 的声音，不要用物理麦克风代替。其它应用如果也被路由到 BlackHole，声音可能混入。
2. 在「系统设置 → 隐私与安全」给**实际启动工具的终端**（如 Terminal 或 WezTerm）授予音频输入权限和控制目标应用所需的辅助功能权限。新版 macOS 若有按应用授权的 **System & App Protection Override**，只授权实际使用的 UU / Windows App；不要关闭系统防护或开启整机控制。GUI 不需要全局热键的输入监控权限。
3. 在游戏里把 Better Fishing 的 **Cast and Interact** 绑定到主键盘反引号 `` ` ``；把鱼饵放到主键盘 `0` 对应的快捷栏，鱼竿放到 `9`。使用 Windows App 时，建议「Connections → Keyboard Mode → Scancode」。先手按三个键确认远端能收到。

## 3. 准备并验证音效模板

把至少一个有权使用的 PCM WAV 放入 `user_templates/`。片段从咬钩声起点开始，至少 `0.6` 秒，不要把抛竿入水声剪进去。`config.toml` 默认关闭内置模板。

可在[上游音效目录](https://github.com/rsamf/wow-fishing-forever/tree/a6c4da4fd8dfbdf9bc80c20e8ed656701f6d2ea7/src/wowfish/assets/clips)查到 `fishbite.wav` 和 `fishingbobber_1.wav` 至 `fishingbobber_3.wav` 的来源。若你有权本地使用，可将四个文件按原名放入 `assets/bites/`，再设置 `use_builtin = true`；否则继续使用有权使用的自备模板。该链接仅用于溯源，上游代码的 MIT 许可证不授予游戏音效使用或再分发权。

如果有权录制音频，也可以用以下命令录一段、手动抛竿，再按波形实测时间裁切。`8.2` 只是示例起点：

```bash
.venv/bin/python wow_fishing.py record --out recordings/bite.wav --seconds 45
.venv/bin/python wow_fishing.py template-add recordings/bite.wav --name my-bite --start 8.2 --duration 1.2
.venv/bin/python wow_fishing.py templates
.venv/bin/python wow_fishing.py listen --file recordings/bite.wav
```

`listen` 不发键。建议再用一段不同的录音验证是否漏报或把背景声当成咬钩。

## 4. 启动与停止

```bash
.venv/bin/python wow_fishing.py gui
```

先切到游戏窗口，确认未抛竿，再点红色按钮。橙色表示准备或等待重抛，绿色表示已抛竿并监听。默认按点击时的前台应用锁定，切到其他应用会停止发键；同一应用内换窗口或标签不会自动暂停，请不要切走。点绿色/橙色按钮停止，终端 `Ctrl+C` 完全退出。

每次抛竿前会查 `last_bait.txt` 对应的缓存记录。没有记录或已过 10 分钟时，依次按 `0`、等 1 秒、按 `9`、等 6 秒再抛。红色按钮显示本次工具启动以来的收杆动作数，绿色显示近期分数，收杆后的橙色保留触发分数。按键被系统接受不等于游戏已完成操作。

## 排查与维护

- 找不到声音：运行 `.venv/bin/python wow_fishing.py devices` 检查 BlackHole；`doctor` 可协助确认设备、模板和权限。定向发键与系统全局发键的授权不是一回事。
- 手按有效、程序按键无反应：先检查实际启动工具的终端是否取得对该远程客户端的定向控制权限。支持仅发一次的 `test-key`；默认预览，只有加 `--send` 才发键。
- 日志：`logs/fishing.jsonl` 为滚动文件，上限 10 MB；`session_id` 区分运行。查看：`tail -F logs/fishing.jsonl`。日志、录音和鱼饵状态不应提交。
- 源码位于 `uu_fishing/`，设置见 `config.toml`，`scripts/` 包含模板测试与 macOS 打包入口。仓库不提供编译好的 App。

## 来源与许可

- 本项目代码使用 [MIT 许可证](LICENSE)。声谱指纹与部分 macOS 输入代码源自 Richard Franklin 的 [rsamf/wow-fishing-forever](https://github.com/rsamf/wow-fishing-forever/tree/a6c4da4fd8dfbdf9bc80c20e8ed656701f6d2ea7)；保留其[原始版权及 MIT 许可证](third_party/rsamf/LICENSE)和[改动与来源记录](third_party/rsamf/NOTICE.txt)。
- [ExistentialAudio/BlackHole](https://github.com/ExistentialAudio/BlackHole) 是单独安装的音频设备驱动，不随本仓库分发；第三方依赖适用各自的许可证。游戏音效不在本项目的 MIT 授权范围内。
