"""Command line entry points. Listening and offline commands never send a key."""
from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime
import json
import math
from pathlib import Path
import platform
import re
import sys
import time
from typing import Any

import numpy as np

from . import __version__
from .config import AppConfig, ConfigError, PROJECT_ROOT, load_config, validate_config
from .monitor import NearMissMonitor
from .session_log import SessionLog


def _cfg(args: argparse.Namespace) -> AppConfig:
    cfg = load_config(args.config)
    if getattr(args, "threshold", None) is not None:
        cfg = replace(cfg, detector=replace(cfg.detector, threshold=args.threshold))
    limits = cfg.limits
    if getattr(args, "minutes", None) is not None:
        limits = replace(limits, duration_minutes=args.minutes)
    if getattr(args, "max_reels", None) is not None:
        limits = replace(limits, max_reels=args.max_reels)
    return validate_config(replace(cfg, limits=limits))


def _detector(cfg: AppConfig):
    from .templates import make_detector
    return make_detector(template_dir=cfg.templates_path,
                         use_builtin=cfg.detector.use_builtin,
                         threshold=cfg.detector.threshold,
                         onset_seconds=cfg.detector.onset_seconds,
                         refractory_seconds=cfg.detector.refractory_seconds)


def _source(cfg: AppConfig):
    from .audio import LiveAudioSource
    return LiveAudioSource(device=cfg.audio.device, capture_rate=cfg.audio.capture_rate,
                           sample_rate=22050, block_seconds=cfg.audio.block_seconds,
                           read_timeout=cfg.audio.read_timeout,
                           queue_seconds=cfg.audio.queue_seconds)


def _finite(value: float, name: str, lo: float, hi: float) -> float:
    if not math.isfinite(value) or not lo <= value <= hi:
        raise ValueError(f"{name} 必须在 {lo:g}～{hi:g} 之间")
    return value


def _choose_window(args: argparse.Namespace, cfg: AppConfig):
    from .macos import list_windows
    windows = list_windows()
    if getattr(args, "window_id", None) is not None:
        found = [window for window in windows if window.window_id == args.window_id]
    else:
        app_filter = cfg.target.app_contains.casefold()
        title_filter = cfg.target.title_contains.casefold()
        found = [window for window in windows
                 if app_filter in window.app_name.casefold() and title_filter in window.title.casefold()]
    if not found:
        raise RuntimeError("找不到目标窗口；请用 windows 查找窗口编号")
    if len(found) != 1:
        text = "; ".join(f"{w.window_id}: {w.app_name} / {w.title}" for w in found)
        raise RuntimeError("目标不唯一，请用 --window-id 指定窗口：" + text)
    return found[0]


def _focused_target(cfg: AppConfig):
    from .macos import accessibility_status, focused_window_info
    target = focused_window_info()
    if target is None:
        trusted, detail = accessibility_status()
        hint = "macOS 未提供可核验的前台窗口；请先切到目标窗口再启动。" if trusted else detail
        raise RuntimeError(f"无法证实当前前台窗口的 ID 和 PID；未发键。{hint}")
    if (cfg.target.app_contains.casefold() not in target.app_name.casefold()
            or cfg.target.title_contains.casefold() not in target.title.casefold()):
        raise RuntimeError("前台窗口不符合应用或标题筛选；未启动")
    return target


def _countdown(seconds: float, stopped=lambda: False, *, consume=None, show=None) -> bool:
    end = time.monotonic() + seconds
    announced = None
    while (left := math.ceil(end - time.monotonic())) > 0:
        if stopped():
            return False
        if left != announced:
            print(f"{left} 秒：请切到目标窗口", flush=True)
            if show is not None:
                show(f"{left} 秒")
            announced = left
        if consume is not None:
            consume()  # A live source is already open: drain it, never queue countdown audio.
        else:
            time.sleep(min(0.1, max(0.0, end - time.monotonic())))
    return not stopped()


def _monitor_line(text: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {text}", flush=True)


def cmd_config(args: argparse.Namespace) -> int:
    print(json.dumps(_cfg(args).to_dict(), indent=2, ensure_ascii=False))
    return 0


def cmd_devices(args: argparse.Namespace) -> int:
    from .audio import list_devices
    devices = list_devices()
    for device in devices:
        print(f"{device.name} | id={device.id} | 输入通道={device.channels}")
    if not devices:
        raise RuntimeError("没有可用输入设备；请在 Mac 安装 BlackHole 2ch 并重启相关应用。")
    print("配置 audio.device 使用唯一名称；程序不会自动退回物理麦克风。")
    return 0


def cmd_windows(args: argparse.Namespace) -> int:
    from .macos import accessibility_status, focused_window_info, list_windows
    focus = focused_window_info()
    windows = list_windows()
    identity = (focus.window_id, focus.pid) if focus else None
    marked = False
    for window in windows:
        is_current = (window.window_id, window.pid) == identity
        marked |= is_current
        flag = "*" if is_current else " "
        print(f"{flag} id={window.window_id:<8} pid={window.pid:<7} {window.app_name} | {window.title}")
    if marked:
        print("* = 本次核验的前台窗口")
    else:
        trusted, detail = accessibility_status()
        print(f"[WARN] 未核验到前台窗口。{'AX 检查已通过，但找不到一致的窗口身份。' if trusted else detail}")
    print("只列窗口，不发键。")
    if not windows:
        print("未能列出窗口：请检查窗口是否可见以及系统隐私权限。")
        return 2
    return 0


def cmd_templates(args: argparse.Namespace) -> int:
    from .templates import list_templates
    from .wav_io import read_wav
    cfg = _cfg(args)
    paths = list_templates(template_dir=cfg.templates_path, use_builtin=cfg.detector.use_builtin)
    for path in paths:
        samples, rate = read_wav(path)
        print(f"{path.name}: {len(samples) / rate:.2f}s @ {rate}Hz | {path}")
    detector = _detector(cfg)
    print(f"模板 {len(detector.fingerprints)} 个；基础阈值={cfg.detector.threshold:.3f}；"
          f"使用开头 {cfg.detector.onset_seconds:.2f}s；分数不是概率。")
    for fp in detector.fingerprints:
        threshold = detector.threshold_for(fp.name)
        if threshold > detector.threshold:
            print(f"{fp.name} 生效阈值={threshold:.3f}（模板最低分保护）")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    cfg = _cfg(args)
    ok = True
    print(f"UU Audio Fishing {__version__} | Python {platform.python_version()} | "
          f"{platform.system()} {platform.release()} / {platform.machine()}")
    print(f"项目: {PROJECT_ROOT}\n配置基准: {cfg.base_dir}\n目标: 启动时锁定的前台窗口")
    try:
        detector = _detector(cfg)
        print(f"[OK] 模板已加载: {len(detector.fingerprints)}")
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"[FAIL] 模板: {exc}")
        ok = False
    if sys.platform != "darwin":
        print("[INFO] 当前不是 macOS；只支持离线 WAV 识别、配置和单元测试，不会尝试操作当前桌面。")
        return 2
    if platform.machine() != "arm64":
        print("[WARN] 当前 Python 不是原生 arm64；M4 建议使用原生 Python 3.12，避免 Rosetta 混装依赖。")
    from .macos import accessibility_status, input_monitoring_status, keyboard_post_status, list_windows
    if args.request_permissions:
        print("[INFO] 正在向系统请求权限；不保证弹窗或立即生效。设置后请重开终端，运行 doctor --no-audio 复查。")
    for name, check, required in (("前台窗口控制（AX）", accessibility_status, True),
                                  ("Quartz 发键权限", keyboard_post_status, True),
                                  ("F8/F9 输入监控（仅 --hotkeys）", input_monitoring_status, False)):
        missing = "FAIL" if required else "INFO"
        try:
            permitted, detail = check(request=args.request_permissions and required)
            print(f"[{'OK' if permitted else missing}] {name}: {detail}")
            ok = ok and (permitted or not required)
        except (OSError, RuntimeError) as exc:
            print(f"[{missing}] {name}: {exc}")
            ok = ok and not required
    try:
        choices = [w for w in list_windows() if cfg.target.app_contains.casefold() in w.app_name.casefold()]
        print(f"[{'OK' if choices else 'WARN'}] 符合应用筛选的窗口: {len(choices)}；详见 windows 命令")
    except (OSError, RuntimeError) as exc:
        print(f"[WARN] 窗口枚举: {exc}")
    if not args.no_audio:
        try:
            with _source(cfg) as source:
                peak = -120.0
                start = time.monotonic()
                while time.monotonic() - start < args.probe_seconds:
                    chunk = source.read()
                    rms = float(np.sqrt(np.mean(np.asarray(chunk.samples, dtype=np.float64) ** 2)))
                    peak = max(peak, 20 * math.log10(rms + 1e-12))
                print(f"[OK] 音频输入: {source.name}；探测最大块电平 {peak:.1f} dBFS")
                if peak < -75:
                    print("[WARN] 输入近静音。请让 UU 播放声音，检查多输出设备是否包含 BlackHole。静音不等于断流。")
                elif peak < -35:
                    print("[WARN] 本次短时探测电平较低；可能只是未播放咬钩声。"
                          "这不是识别失败阈值，请在实际咬钩时录音验证，不要仅据此调整音量或检测阈值。")
        except (OSError, ValueError, RuntimeError) as exc:
            print(f"[FAIL] 音频: {exc}")
            ok = False
    print("不会发键；BlackHole 可能混入其他应用声音，锁定窗口不会隔离音频。")
    print("doctor 只检查，不启动运行，也不会驻留。")
    if ok:
        print("要启动真实循环，在同一 Mac 终端运行：.venv/bin/python wow_fishing.py run --enable-input --minutes 3 --max-reels 3")
    return 0 if ok else 2


def cmd_record(args: argparse.Namespace) -> int:
    from .wav_io import write_wav
    cfg = _cfg(args)
    seconds = _finite(args.seconds, "录音秒数", 0.5, 600)
    output = args.out.expanduser().resolve()
    if output.exists() and not args.force:
        raise FileExistsError(f"文件已存在，未覆盖: {output}（显式 --force 才可覆盖）")
    delay = cfg.input.start_delay_seconds if args.delay is None else _finite(args.delay, "倒计时", 0, 30)
    print("仅录制 BlackHole，不发送任何按键。请手动抛竿，录下咬钩声。")
    _countdown(delay)
    # Fail before recording if the destination directory cannot be created.
    output.parent.mkdir(parents=True, exist_ok=True)
    blocks, total = [], 0
    count = math.ceil(seconds * 22050)
    try:
        with _source(cfg) as source:
            print(f"录音: {source.name}")
            while total < count:
                block = source.read().samples
                block = block[:count - total]
                blocks.append(block)
                total += len(block)
    except KeyboardInterrupt:
        print("已中止；有数据时保存已录制的部分。")
    if not blocks:
        print("没有音频数据，未写文件。")
        return 130
    write_wav(output, np.concatenate(blocks), 22050, overwrite=args.force)
    print(f"已保存 {total / 22050:.2f}s PCM WAV: {output}")
    return 0


def cmd_template_add(args: argparse.Namespace) -> int:
    from .detection.features import DEFAULT_MEL
    from .detection.fingerprint import build_fingerprint
    from .wav_io import read_wav, resample, write_wav
    cfg = _cfg(args)
    if not cfg.templates_path:
        raise ConfigError("请先在配置中设置 detector.templates_dir")
    if not re.fullmatch(r"[\w-]{1,80}", args.name, flags=re.UNICODE):
        raise ValueError("模板名称限 1～80 个文字/数字/下划线/连字符，不可含路径或扩展名")
    start = _finite(args.start, "片段开始时间", 0, 86400)
    duration = _finite(args.duration, "片段长度", cfg.detector.onset_seconds, 30)
    samples, rate = read_wav(args.file)
    begin, end = round(start * rate), round((start + duration) * rate)
    if begin >= len(samples) or end > len(samples):
        raise ValueError(f"裁剪范围超出录音 {len(samples) / rate:.3f}s；不会自动补静音")
    excerpt = resample(samples[begin:end], rate, 22050)
    # Validate before writing; a bad/silent selection must not pollute the template directory.
    onset = excerpt[:int(cfg.detector.onset_seconds * 22050)]
    if float(np.std(onset)) < 1e-5:
        raise ValueError("所选片段的分析开头近静音，不能作为咬钩模板；请重新对齐开始位置")
    fingerprint = build_fingerprint(args.name, excerpt, DEFAULT_MEL, onset_s=cfg.detector.onset_seconds)
    if float(np.linalg.norm(fingerprint.feats)) < 0.5:
        raise ValueError("所选片段开头没有可用的声谱变化，未写入模板")
    cfg.templates_path.mkdir(parents=True, exist_ok=True)
    output = cfg.templates_path / (args.name + ".wav")
    write_wav(output, excerpt, 22050, overwrite=args.force)
    print(f"已添加: {output}\n请使用另一份 UU 录音执行 listen --file 验证，不能只用模板自身证明效果。")
    return 0


def cmd_listen(args: argparse.Namespace) -> int:
    from .wav_io import iter_wav_chunks
    cfg = _cfg(args)
    seconds = _finite(args.seconds, "监听秒数", 0, 86400)
    detector = _detector(cfg)
    log = SessionLog(PROJECT_ROOT / "logs")
    count = 0
    processed = 0.0
    near_miss = NearMissMonitor()
    _monitor_line("仅监听，不发键")
    log.write("listen_start", version=__version__, config=cfg.to_dict(),
              file=str(args.file) if args.file else None)

    def report_miss(peak) -> None:
        if peak is not None:
            log.write("near_miss", clip=peak.clip, score=peak.score, threshold=peak.threshold,
                      processed_seconds=processed)
            _monitor_line(f"未达标 {peak.score:.2f}")

    def process(chunk) -> bool:
        nonlocal count, processed
        processed += len(chunk.samples) / chunk.sample_rate
        events = detector.feed(chunk)
        if events:
            near_miss.discard()
            detector.take_peaks()
        else:
            report_miss(near_miss.observe(processed if args.file else time.monotonic(), detector))
        for event in events:
            count += 1
            log.write("bite", clip=event.clip, score=float(event.score), onset=float(event.t),
                      processed_seconds=processed)
            _monitor_line(f"咬钩 {event.score:.2f}")
        return seconds <= 0 or processed < seconds

    try:
        if args.file:
            for chunk in iter_wav_chunks(args.file, sample_rate=22050, block_seconds=cfg.audio.block_seconds):
                if not process(chunk):
                    break
        else:
            with _source(cfg) as source:
                _monitor_line(f"音频输入：{source.name}")
                while process(source.read()):
                    pass
    except KeyboardInterrupt:
        pass
    finally:
        report_miss(near_miss.finish(processed if args.file else time.monotonic()))
        _monitor_line(f"结束｜咬钩 {count} 次")
        log.write("listen_summary", events=count, processed_seconds=processed)
    return 0


def cmd_test_key(args: argparse.Namespace) -> int:
    from .macos import MacKeyboard, frontmost_app_info
    cfg = _cfg(args)
    target = _choose_window(args, cfg) if args.window_id is not None else None
    expected_app = args.frontmost_app.strip() if args.frontmost_app is not None else None
    if expected_app == "" or (args.system_event and not expected_app):
        raise ValueError("系统事件测试必须用 --frontmost-app 指定预期的前台应用名称")
    mode = "系统事件" if args.system_event else "定向发键"
    if not args.send:
        label = expected_app or (target.label if target else "启动时核验的前台窗口")
        print(f"预览：{mode}｜{label}｜{cfg.input.action_key!r}。未发送；加 --send 才执行。")
        return 0
    delay = cfg.input.start_delay_seconds if args.delay is None else _finite(args.delay, "倒计时", 0, 30)
    print("仅测试一次完整按下/松开；不会启动钓鱼循环。请松开所有修饰键。")
    _countdown(delay)
    if expected_app:
        target = frontmost_app_info()
        if target is None or target.app_name.casefold() != expected_app.casefold():
            actual = target.app_name if target else "无法确认"
            raise RuntimeError(f"预期前台为 {expected_app}，实际为 {actual}；未发键")
    elif target is None:
        target = _focused_target(cfg)
    print(f"{mode}｜目标：{target.app_name}")
    keyboard = MacKeyboard(target, key=cfg.input.action_key, hold_seconds=cfg.input.hold_seconds)
    if args.system_event:
        keyboard.press(targeted=False, allow_app_system_event=True)
    else:
        keyboard.press(targeted=True)
    print("已向 macOS 提交一次按键；请观察远端是否响应。")
    return 0


def _listen_without_target(source, detector, controls, emit) -> None:
    """Keep the start button session useful, without simulating or sending any key."""
    from .engine import Stats

    monitor = NearMissMonitor()
    stats = Stats()
    started = time.monotonic()

    def report_miss(peak) -> None:
        if peak is not None:
            emit("near_miss", dict(state="listen", clip=peak.clip, score=peak.score,
                                   threshold=peak.threshold))

    try:
        while not controls.stop_event.is_set():
            chunk = source.read()
            if controls.stop_event.is_set():
                break
            events = detector.feed(chunk)
            if events:
                monitor.discard()
                detector.take_peaks()
                for event in events:
                    stats.bite_events += 1
                    emit("bite", dict(state="listen", clip=event.clip, score=float(event.score)))
            else:
                report_miss(monitor.observe(time.monotonic(), detector))
    finally:
        report_miss(monitor.finish(time.monotonic()))
        stats.elapsed_seconds = time.monotonic() - started
        stats.stop_reason = controls.stop_reason or "监听中止"
        emit("summary", dict(state="stopped", **vars(stats)))


def cmd_run(args: argparse.Namespace, *, controls=None, show_button=None) -> int:
    from .bait import bait_history
    from .engine import ActionBlocked, Controls, FishingEngine
    from .macos import AppInfo, FocusError, MacKeyboard, frontmost_app_info, is_focused
    from .overlay import StatusOverlay
    cfg = _cfg(args)
    select_at_deadline = args.window_id is None
    target = None if select_at_deadline else _choose_window(args, cfg)
    real_input = bool(args.enable_input)
    auto_start = select_at_deadline or args.auto_start or not args.hotkeys
    if controls is None:
        controls = Controls(paused=real_input and not auto_start)
    keyboard = None  # Without --enable-input there is no path to a CGEvent.
    bait_keyboards: dict[str, MacKeyboard] = {}
    bait = None
    target_problem = None
    log_dir = getattr(args, "log_dir", None) or PROJECT_ROOT / "logs"
    log = SessionLog(log_dir)
    path = log.path
    hotkeys = None
    mode = "定向发键" if real_input else "模拟，不发键"
    _monitor_line(f"{mode}｜日志：{path}")
    if args.hotkeys:
        _monitor_line("F8 启停｜F9 退出")

    bite_score: float | None = None

    def emit(event: str, data: dict[str, Any]) -> None:
        nonlocal bite_score
        log.write(event, **data)
        if event == "action":
            label = "抛竿" if data["action"] == "cast" else "收杆"
            _monitor_line(label if real_input else "模拟" + label)
        elif event == "bite":
            _monitor_line(f"咬钩 {data['score']:.2f}")
        elif event == "near_miss":
            _monitor_line(f"未达标 {data['score']:.2f}")
        elif event == "timeout":
            _monitor_line("超时")
        elif event == "bait_start":
            _monitor_line("补鱼饵")
        elif event == "bait_rod":
            _monitor_line("已按 9｜等待抛竿")
        elif event == "bait_error":
            _monitor_line(f"鱼饵记录写入失败：{data['detail']}")
        elif event == "state" and data["state"] == "paused":
            _monitor_line(f"暂停：{data.get('reason', '已暂停')}")
        elif event == "summary":
            _monitor_line(f"结束｜抛竿 {data['casts']}｜收杆 {data['reel_actions']}")
        # An action event follows a completed (or simulated) tap. A bite is not a reel.
        if show_button is not None:
            if event == "action":
                if data["action"] == "cast":
                    bite_score = None
                    show_button("green")
                else:
                    show_button("orange", score=bite_score, reeled=real_input)
            elif event == "bite":
                bite_score = data["score"]
            elif event == "state" and data["state"] == "paused":
                controls.stop(data.get("reason", "暂停"))
            elif event == "state" and data["state"] in ("ready", "resync", "retry_wait"):
                show_button("orange")
            return
        if event == "action":
            if data["action"] == "cast":
                hud.show("等待上钩")
            elif data["action"] == "reel":
                hud.show("等待抛竿")
        elif event == "state":
            if data["state"] == "paused":
                hud.show("已暂停")
            elif data["state"] in ("bait_wait_rod", "bait_settle"):
                hud.show("补鱼饵")
            elif data["state"] in ("ready", "resync", "retry_wait"):
                hud.show("等待抛竿")

    def send_key(keyboard: MacKeyboard | None, on_submitted=None) -> None:
        if controls.stop_event.is_set() or controls.snapshot()[0]:
            raise ActionBlocked("已暂停/停止")
        if keyboard is not None:
            try:
                keyboard.press(targeted=True, cancelled=controls.stop_event.is_set,
                               on_submitted=on_submitted)
            except FocusError as exc:
                raise ActionBlocked(str(exc)) from exc

    def action() -> None:
        send_key(keyboard)

    def bait_action(key: str, on_submitted) -> None:
        send_key(bait_keyboards[key], on_submitted)

    phase = "F8/F9 热键"
    try:
        log.write("startup", version=__version__, real_input=real_input, auto_start=auto_start,
                  hotkeys=args.hotkeys, targeted=real_input)
        # If explicitly requested, start the listener before opening audio.
        if args.hotkeys:
            from .hotkeys import GlobalHotkeys
            hotkeys = GlobalHotkeys(controls.toggle, lambda: controls.stop("F9 停止"))
            hotkeys.start()
        if controls.stop_event.is_set():
            return 0
        phase = "状态条"
        from contextlib import ExitStack
        with ExitStack() as stack:
            hud = stack.enter_context(StatusOverlay()) if show_button is None else None
            if show_button is not None and select_at_deadline:
                phase = "锁定前台目标"
                try:
                    if cfg.target.title_contains:
                        target = _focused_target(cfg)
                    else:
                        front = frontmost_app_info()
                        if front is None:
                            raise RuntimeError("无法确认前台应用")
                        if cfg.target.app_contains.casefold() not in front.app_name.casefold():
                            raise RuntimeError("前台应用不符合筛选条件")
                        target = front
                except Exception as exc:
                    target_problem = str(exc)
                    log.write("target_unavailable", detail=target_problem)
                    _monitor_line("目标未确认｜仅监听，不发键")
            phase = "加载音效模板"
            detector = _detector(cfg)
            phase = "BlackHole 音频"
            source = stack.enter_context(_source(cfg))
            source.read()  # Verify actual frame delivery, not just the existence of a device.
            if auto_start:
                phase = "启动倒计时"
                if not _countdown(0.0 if show_button is not None else cfg.input.start_delay_seconds,
                                  controls.stop_event.is_set, consume=source.read,
                                  show=hud.show if hud is not None else None):
                    return 0
            phase = "锁定前台窗口"
            if target is None and target_problem is None:
                target = _focused_target(cfg)
            if controls.stop_event.is_set():
                return 0
            source.flush()  # Never pass preflight/countdown audio to the detector.
            if target is None:
                log.write("run_start", version=__version__, real_input=False, target=None,
                          target_scope="listen_only", config=cfg.to_dict())
                phase = "仅监听"
                _listen_without_target(source, detector, controls, emit)
                phase = "关闭音频和状态条"
                return 0
            if real_input:
                keyboard = MacKeyboard(target, key=cfg.input.action_key, hold_seconds=cfg.input.hold_seconds)
                bait_keyboards = {key: MacKeyboard(target, key=key, hold_seconds=cfg.input.hold_seconds)
                                  for key in ("0", "9")}
                bait = (bait_history(cfg.base_dir / "last_bait.txt"), bait_action)
            _monitor_line(f"目标：{target.app_name}")
            if real_input and not auto_start:
                _monitor_line("切回目标窗口，按 F8 开始")
            log.write("run_start", version=__version__, real_input=real_input,
                      target=vars(target), target_scope="app" if isinstance(target, AppInfo) else "window",
                      config=cfg.to_dict())
            def preview_score(score: float) -> None:
                show_button(score=score)

            engine = FishingEngine(cfg, source, detector, action, lambda: is_focused(target),
                                    controls, emit=emit, bait=bait,
                                    preview_score=preview_score if show_button is not None else None)
            phase = "钓鱼循环"
            engine.run()
            phase = "关闭音频和状态条"
    except (OSError, RuntimeError, ValueError) as exc:
        log.write("run_error", phase=phase, detail=str(exc))
        raise RuntimeError(f"{phase}：{exc}（日志：{path}）") from exc
    finally:
        reason = controls.stop_reason
        controls.stop("会话已结束")
        try:
            if hotkeys is not None:
                hotkeys.stop()
        finally:
            log.write("run_exit", phase=phase, reason=reason)
    return 0


def cmd_gui(args: argparse.Namespace) -> int:
    from .button_ui import run_button_ui

    def run_session(controls, show):
        # The button click is the opt-in; merely starting `gui` opens no device.
        run_args = argparse.Namespace(config=args.config, window_id=None,
                                      log_dir=getattr(args, "log_dir", None),
                                      enable_input=True, targeted=True, auto_start=True,
                                      hotkeys=False, minutes=0.0, max_reels=0)
        cmd_run(run_args, controls=controls, show_button=show)

    run_button_ui(run_session)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="M4 macOS + UU 远程 + Better Fishing 纯音频钓鱼")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--config", type=Path, default=None, help="TOML；相对路径以该文件目录为基准")
    subs = parser.add_subparsers(dest="command", required=True)

    def command(name: str, help_text: str, func):
        sub = subs.add_parser(name, help=help_text)
        sub.add_argument("--config", type=Path, default=argparse.SUPPRESS)
        sub.set_defaults(func=func)
        return sub

    command("config", "验证并显示解析后的配置（不写文件）", cmd_config)
    command("devices", "列出 Mac 音频输入", cmd_devices)
    command("windows", "列出 Mac 窗口与明确前台标记", cmd_windows)
    command("templates", "检查内置/自定义音效模板", cmd_templates)
    doctor = command("doctor", "检查环境/权限/模板/BlackHole，不发键", cmd_doctor)
    doctor.add_argument("--request-permissions", action="store_true", help="请求系统显示权限提示，不自动授权")
    doctor.add_argument("--no-audio", action="store_true")
    doctor.add_argument("--probe-seconds", type=float, default=1.0)
    record = command("record", "录制 BlackHole 为 PCM WAV，不发键", cmd_record)
    record.add_argument("--out", type=Path, required=True)
    record.add_argument("--seconds", type=float, default=15)
    record.add_argument("--delay", type=float, default=None)
    record.add_argument("--force", action="store_true")
    add = command("template-add", "裁剪实际 UU 录音为自定义模板", cmd_template_add)
    add.add_argument("file", type=Path)
    add.add_argument("--name", required=True)
    add.add_argument("--start", type=float, default=0)
    add.add_argument("--duration", type=float, default=1.2)
    add.add_argument("--force", action="store_true")
    listen = command("listen", "纯监听；--file 可以跨平台离线验证", cmd_listen)
    listen.add_argument("--file", type=Path)
    listen.add_argument("--seconds", type=float, default=0)
    listen.add_argument("--threshold", type=float)
    key = command("test-key", "预览单次按键；--send 才实际发送", cmd_test_key)
    key_target = key.add_mutually_exclusive_group()
    key_target.add_argument("--window-id", type=int)
    key_target.add_argument("--frontmost-app", help="仅当前台应用名称与指定名称一致时测试")
    key.add_argument("--send", action="store_true")
    key_mode = key.add_mutually_exclusive_group()
    key_mode.add_argument("--targeted", action="store_true", help="定向发键（默认）")
    key_mode.add_argument("--system-event", action="store_true", help="测试系统事件；必须指定 --frontmost-app")
    key.add_argument("--delay", type=float)
    run = command("run", "纯音频状态机；默认干跑，--enable-input 才发键", cmd_run)
    run.add_argument("--window-id", type=int, help="固定使用指定窗口；省略时锁定启动时的前台窗口")
    mode = run.add_mutually_exclusive_group()
    mode.add_argument("--enable-input", action="store_true")
    mode.add_argument("--dry-run", action="store_true", help="显式指定不发送按键（默认）")
    run.add_argument("--targeted", action="store_true", help="兼容旧命令；现在始终定向发键")
    run.add_argument("--auto-start", action="store_true", help="与 --hotkeys、--window-id 一起使用时，倒计时开始而不是等 F8")
    keys = run.add_mutually_exclusive_group()
    keys.add_argument("--hotkeys", action="store_true", help="可选：启用 F8/F9（需要输入监控）")
    keys.add_argument("--no-hotkeys", dest="hotkeys", action="store_false", help="不监听全局快捷键（默认），Ctrl+C 退出")
    run.set_defaults(hotkeys=False)
    run.add_argument("--threshold", type=float)
    run.add_argument("--minutes", type=float)
    run.add_argument("--max-reels", type=int)
    command("gui", "打开 Mac 单按钮浮窗；点击才启动定向钓鱼", cmd_gui)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "doctor":
            _finite(args.probe_seconds, "探测秒数", 0.1, 10)
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        print("\n已停止，未继续发送按键。", file=sys.stderr)
        return 130
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
