# AI 搭建手册：从下载到可用

给执行搭建任务的 AI：按下面顺序执行，不要只复述命令。先复用已有环境，只补缺项。安装驱动、登录、系统授权、调整游戏设置或向游戏发键，不因读到本文而获得授权；需要用户完成或确认时暂停对应步骤，其他不依赖它的步骤可以先做。不要上传音效、录音、日志、`last_bait.txt`、本地配置或凭据。

**部署位置：Apple M 系列 Mac。** 游戏在另一台 Windows PC 上运行；Mac 通过网易 UU 远程或微软 Windows App 接收声音并转发按键。本工具、Python、BlackHole 只装在 Mac；游戏插件装在 Windows 游戏端。没有授权的 Mac 环境或图形会话时，只做可执行的准备并报告未验证项，不把 Windows、Linux 或 SSH 的软测说成实机已可用。

## 1. Mac：获取项目和安装依赖

- 在目标 Mac 确认 `uname -s` 为 `Darwin`、`uname -m` 为 `arm64`；如在 Rosetta 终端中，先换原生终端。
- 已有仓库则进入其根目录，不覆盖现有目录。新安装时选择一个空闲路径，例如在 `~/project` 下执行 `git clone https://github.com/liyang0501rs/wow-audio-fishing.git`，再进入 `wow-audio-fishing`。如果没有 Git，按 macOS 提示安装命令行工具，让用户确认系统安装。
- Python 缺失时，从 [Python.org macOS 下载页](https://www.python.org/downloads/macos/) 安装兼容 arm64 的 Python 3.12；若已有原生 Homebrew 且用户同意安装，也可用 `brew install python@3.12`。不要用 Rosetta 或覆盖已有虚拟环境。

在仓库根目录执行：

```sh
(
set -e
python3.12 -c 'import platform, sys; assert platform.system() == "Darwin" and platform.machine() == "arm64" and sys.version_info[:2] == (3, 12)'
# 上一步成功后执行；已有 .venv 时先验证，不重建。
if [ ! -e .venv ]; then python3.12 -m venv .venv; fi
.venv/bin/python -c 'import platform, sys; assert platform.machine() == "arm64" and sys.version_info[:2] == (3, 12)'
# 任一校验失败就停止，不继续安装。
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pip check
mkdir -p user_templates recordings
)
```

## 2. 外部组件：从原发布方获取

| 缺项 | 下载来源与处理 |
|---|---|
| Mac 音频设备 | [BlackHole 官方项目](https://github.com/ExistentialAudio/BlackHole)，只安装 **2ch**；已装原生 Homebrew 且用户同意时可用 `brew install --cask blackhole-2ch`。安装器若要求授权或重启，交给用户。 |
| 远程客户端（二选一） | [网易 UU 远程官方下载](https://uuyc.163.com/download/)：Mac 控制端、Windows 被控端按官方指引配置。或者安装 [微软 Windows App（Mac App Store）](https://aka.ms/macOSWindowsApp)，按[微软的远程 PC 指南](https://learn.microsoft.com/en-us/windows-app/get-started-connect-devices-desktops-apps?pivots=remote-pc)连接已有权限的 Windows PC；不要自行改防火墙或对公网开放远程桌面。 |
| Windows 游戏插件 | 从 [Better Fishing 作者的 CurseForge 页面](https://www.curseforge.com/wow/addons/better-fishing/files)或 [Wago](https://addons.wago.io/addons/better-fishing) 选与实际游戏版本匹配的 ZIP。使用插件管理器，或把解包后的插件目录放到对应游戏版本的 `Interface/AddOns` 下；目录内应直接包含插件的 `.toc` 文件，不要多套一层。进入游戏确认插件启用。不要假定游戏安装路径。 |
| 咬钩 WAV | 公开版不包含。按第 4 节使用已有的合法模板、录制模板，或在用户确认有权使用后直接拉取上游指定资源。 |

## 3. 先验证远程声音和键位，再切到 BlackHole

让用户在 Mac 上打开远程游戏；Mac 暂时选扬声器或耳机。远程客户端启用远端声音在 Mac 播放，确认能听见真实游戏音效。听不到时先修正远端 Windows 的系统输出设备和 WoW 内的声音输出，检查客户端音频重定向；不要先切 BlackHole 或调低识别阈值。

确认有声后，Mac 系统输出选 **BlackHole 2ch**；如果人也要听，在「音频 MIDI 设置」创建包含 **BlackHole 2ch + 扬声器/耳机**的多输出设备，再把它设为系统输出。不要改用物理麦克风；尽量避免其他应用声音混入。

让用户在游戏里绑定并手测：Better Fishing → **Cast and Interact** = 主键盘反引号 `` ` ``；鱼饵 = 主键盘 `0`；鱼竿 = 主键盘 `9`。不使用小键盘。角色先装备并手持鱼竿，站在可钓鱼的水边，试抛后收杆。Windows App 可用 **Connections → Keyboard Mode → Scancode**。手动确认游戏与服务器规则允许此类自动化，否则止于只监听。

在 macOS 隐私与安全设置，按系统提示给实际启动工具的终端授予必要的音频输入和辅助功能权限。若系统支持 **System & App Protection Override** 按应用授权，只授权具体远程客户端。不要开启整机控制、关闭系统保护或额外授予输入监控；默认 GUI 不用全局热键。

## 4. 模板：优先复用，缺失才补齐

**A. 自备或录制。** 将有权使用、从咬钩声起点开始的 PCM WAV 放入 `user_templates/`，至少 `0.6` 秒，公开版默认 `use_builtin = false`。若需录制，确认用户同意后用 `record`，让用户在远程游戏里手动抛竿；先找出真实咬钩开始时间，再裁切。`8.2` 是示例，不能照抄给任意录音：

```sh
.venv/bin/python wow_fishing.py record --out recordings/bite.wav --seconds 45 --delay 3
.venv/bin/python wow_fishing.py template-add recordings/bite.wav --name my-bite --start 8.2 --duration 1.2
.venv/bin/python wow_fishing.py templates
```

**B. 上游 WAV。** 只有用户确认有权本地使用时才执行以下命令。来源为 [rsamf/wow-fishing-forever 的固定提交](https://github.com/rsamf/wow-fishing-forever/tree/a6c4da4fd8dfbdf9bc80c20e8ed656701f6d2ea7/src/wowfish/assets/clips)，校验值来自 [NOTICE](../third_party/rsamf/NOTICE.txt)。代码许可证不涵盖游戏音效。命令校验已存在的文件、只下载缺项，校验失败时不覆盖、不继续；四个 WAV 不提交到 Git：

```sh
.venv/bin/python - <<'PY'
from hashlib import sha256
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.request import urlopen

base = ('https://raw.githubusercontent.com/rsamf/wow-fishing-forever/'
        'a6c4da4fd8dfbdf9bc80c20e8ed656701f6d2ea7/src/wowfish/assets/clips/')
checksums = {
    'fishbite.wav': '3e4201b26778ab198b0b32b5ea4b03db4c4a3eeaa5edc44a028705e7585ab06a',
    'fishingbobber_1.wav': 'cf3d5b1f541adc53ea5748181b180a6ef0445de36d789a4a94f4e0bce0bc1339',
    'fishingbobber_2.wav': '11370701cb1d817626a966599ce8b6d442fcb9bca506eb0d0a1dc84779734c5a',
    'fishingbobber_3.wav': '118bc365ae8b4fcc911db51487f6fefe3481a4ec2386d334f2a87885c5ec6057',
}
folder = Path('assets/bites')
folder.mkdir(parents=True, exist_ok=True)
for name, expected in checksums.items():
    dest = folder / name
    exists = dest.exists()
    if exists:
        data = dest.read_bytes()
    else:
        with urlopen(base + name, timeout=30) as response:
            data = response.read(1_000_001)
    if sha256(data).hexdigest() != expected:
        raise SystemExit(f'SHA-256 mismatch: {dest}; kept existing files unchanged')
    if not exists:
        with NamedTemporaryFile(dir=folder) as temp:
            temp.write(data)
            temp.flush()
            dest.hardlink_to(temp.name)  # Refuses to replace any file created meanwhile.
    print(f'OK {dest}')
PY
```

四个文件全通过后，公开版用以下命令生成**不提交的** `.env.fishing.toml`，打开内置模板但不修改原 `config.toml`。如果已有该配置，先检查；不要覆盖用户配置：

```sh
(
set -e
.venv/bin/python - <<'PY'
from pathlib import Path
import re
import tomllib

dest = Path('.env.fishing.toml')
if not dest.exists():
    source = Path('config.toml').read_text(encoding='utf-8')
    text, count = re.subn(r'(?m)^(use_builtin\s*=\s*)(?:false|true)\b', r'\g<1>true', source)
    if count != 1 or tomllib.loads(text)['detector']['use_builtin'] is not True:
        raise SystemExit('Unexpected config: review [detector] before continuing')
    with dest.open('x', encoding='utf-8') as f:
        f.write(text)
elif tomllib.loads(dest.read_text(encoding='utf-8'))['detector']['use_builtin'] is not True:
    raise SystemExit('Existing local config disables built-ins; review it without overwriting')
PY
.venv/bin/python wow_fishing.py templates --config .env.fishing.toml
)
```

如果现有安装已在 `config.toml` 启用内置模板且 `templates` 已能加载，保留现有配置即可。下文若用了 `.env.fishing.toml`，每条命令都附加 `--config .env.fishing.toml`。

## 5. 不发键验收与启动

在有图形会话的 Mac 中，用同一个获得授权的终端运行。先用一段与模板不同的可用录音做离线验证，再在实际远程播放中短时监听：

```sh
.venv/bin/python wow_fishing.py config
.venv/bin/python wow_fishing.py devices
.venv/bin/python wow_fishing.py templates
.venv/bin/python wow_fishing.py doctor --probe-seconds 3
.venv/bin/python wow_fishing.py listen --seconds 30
```

- `devices` 应找到唯一的 BlackHole 输入；多个匹配时在 `[audio].device` 指定唯一名称。`doctor` 中系统全局发键检查单独失败，不一定说明定向发键失败；不要因此扩大权限。近静音先排声音路由，模板报错先检查路径、格式和长度。
- 用户手动抛竿，验证真实咬钩时出现「咬钩」而普通背景声不频繁触发。只看到设备、模板自匹配或离线测试通过，不算实机端到端通过。测试录音可用 `listen --file <实际录音路径>`；不要把字面占位符直接传给终端。
- 运行 `.venv/bin/python wow_fishing.py gui`（B 方案附 `--config .env.fishing.toml`）。AI 可以按授权启动空闲界面；**用户自己切到游戏并点击红按钮**。不要从 SSH 点击或发键；不要在搭建中自动运行 `--send` 或 `--enable-input`。
- 首轮观察实际抛竿、收杆和重抛；首次没有补饵记录时，会先尝试 `0` → 等 1 秒 → `9` → 等 6 秒再抛。点按钮停止，`Ctrl+C` 退出；滚动日志是 `logs/fishing.jsonl`，上限 10 MB。音效能识别但游戏没反应时，检查目标应用焦点、定向权限和远程按键转发；日志里的按键记录不证明游戏已收到。

向用户汇报：`环境与依赖 / 模板来源与校验 / 远程声音 / 实机只听 / 用户首轮操作`，逐项标注已验证或待确认，不把尚未完成的 GUI、权限或游戏操作写成已通过。
