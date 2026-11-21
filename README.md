# WoW Audio Fishing · Mac 纯音频钓鱼

> **使用场景：本工具只运行在搭载 Apple M 系列芯片的 Mac 上。** 《魔兽世界》运行在另一台 Windows PC 上，Mac 通过网易 UU 远程或微软 Windows App 连接这台 PC。**本工具不安装在游戏 PC 上，也不在游戏 PC 上直接控制游戏。**

游戏声音由远程客户端传回 Mac，再由 BlackHole 采集。识别到咬钩声后，工具把按键发给 Mac 上的远程客户端，由客户端转发到游戏 PC。工具不截图、不读取游戏进程。请先确认你的游戏和服务器规则允许这种自动化。

> 公开版不包含游戏音效，必须准备有权使用的咬钩 WAV。重新分发录音前请确认授权。

## 1. 安装

以下安装步骤都在 **M 系列 Mac** 上完成。安装原生 **Python 3.12（arm64，不用 Rosetta）**、[BlackHole 2ch](https://github.com/ExistentialAudio/BlackHole)，以及你使用的远程客户端。然后在 Mac 终端执行：

```bash
git clone https://github.com/liyang0501rs/wow-audio-fishing.git
cd wow-audio-fishing
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
mkdir -p user_templates recordings
```

## 2. 声音、权限与游戏键位

1. **先确认远程游戏有声，再切到 BlackHole。** 先让 Mac 用扬声器或耳机输出，在 UU 或 Windows App 中启用远端声音播放，确认在 Mac 上确实能听到《魔兽世界》的音效。如果听不到，先检查远端 Windows PC 的系统音频输出和游戏内的音频输出设备。选错设备时，声音可能没有传到 Mac，切到 BlackHole 也无法让工具识别到声音。
2. 确认有声后，将 Mac 输出切换到 **BlackHole 2ch**。如果自己也要听，在 Mac「音频 MIDI 设置」创建包含 **BlackHole 2ch 和扬声器／耳机**的多输出设备，再选它作为系统输出。不要用物理麦克风代替；同样输出到 BlackHole 的其他应用也可能混入声音。
3. 在「系统设置 → 隐私与安全」给**实际启动工具的终端**（如 Terminal 或 WezTerm）授予音频输入权限和控制目标应用所需的辅助功能权限。新版 macOS 若有按应用授权的 **System & App Protection Override**，只授权实际使用的 UU / Windows App；不要关闭系统防护或开启整机控制。GUI 不需要全局热键的输入监控权限。

### 游戏内准备（启动前必做）

1. **安装并启用 Better Fishing 插件**：插件装在实际运行《魔兽世界》的电脑上，进入游戏后确认已启用。
2. **设置插件快捷键**：在游戏内的按键绑定中找到 Better Fishing 的 **Cast and Interact**，绑定到主键盘反引号 `` ` ``。这个键用于抛竿和收杆。
3. **设置鱼饵和鱼竿快捷键**：把鱼饵拖到绑定主键盘 `0` 的动作条格子，把鱼竿拖到绑定主键盘 `9` 的动作条格子。不要用数字小键盘。
4. **让角色先装备并手持鱼竿**：把鱼竿放到动作条只是绑定快捷键，不等于已经装备。启动工具前确认角色手持鱼竿、站在可钓鱼的水边，且当前没有抛竿。

使用 Windows App 时，建议「Connections → Keyboard Mode → Scancode」。先确认远端能收到这些按键；若手动试抛，收杆后再启动工具。

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

先切到游戏窗口，确认角色已手持鱼竿且尚未抛竿，再点红色按钮。橙色表示准备或等待重抛，绿色表示已抛竿并监听。默认按点击时的前台应用锁定，切到其他应用会停止发键；同一应用内换窗口或标签不会自动暂停，请不要切走。点绿色/橙色按钮停止，终端 `Ctrl+C` 完全退出。

每次抛竿前会查 `last_bait.txt` 对应的缓存记录。没有记录或已过 10 分钟时，依次按 `0`、等 1 秒、按 `9`、等 6 秒再抛。红色按钮显示本次工具启动以来的收杆动作数，绿色显示近期分数，收杆后的橙色保留触发分数。按键被系统接受不等于游戏已完成操作。

## 排查与维护

- 找不到声音：先切回 Mac 扬声器或耳机，确认远程游戏有声。确认后再切到 BlackHole，运行 `.venv/bin/python wow_fishing.py devices` 检查设备；`doctor` 可协助确认设备、模板和权限。定向发键与系统全局发键的授权不是一回事。
- 手按有效、程序按键无反应：先检查实际启动工具的终端是否取得对该远程客户端的定向控制权限。支持仅发一次的 `test-key`；默认预览，只有加 `--send` 才发键。
- 日志：`logs/fishing.jsonl` 为滚动文件，上限 10 MB；`session_id` 区分运行。查看：`tail -F logs/fishing.jsonl`。日志、录音和鱼饵状态不应提交。
- 源码位于 `uu_fishing/`，设置见 `config.toml`，`scripts/` 包含模板测试与 macOS 打包入口。仓库不提供编译好的 App。

## 来源与许可

- 本项目代码使用 [MIT 许可证](LICENSE)。声谱指纹与部分 macOS 输入代码源自 Richard Franklin 的 [rsamf/wow-fishing-forever](https://github.com/rsamf/wow-fishing-forever/tree/a6c4da4fd8dfbdf9bc80c20e8ed656701f6d2ea7)；保留其[原始版权及 MIT 许可证](third_party/rsamf/LICENSE)和[改动与来源记录](third_party/rsamf/NOTICE.txt)。
- [ExistentialAudio/BlackHole](https://github.com/ExistentialAudio/BlackHole) 是单独安装的音频设备驱动，不随本仓库分发；第三方依赖适用各自的许可证。游戏音效不在本项目的 MIT 授权范围内。
