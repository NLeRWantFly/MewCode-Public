from __future__ import annotations

import asyncio
import logging
import os
import sys

log = logging.getLogger(__name__)

if sys.platform == "win32":
    from textual.drivers.windows_driver import WindowsDriver as _BaseDriver
    from textual.drivers import win32 as _win32
    from textual import events as _events
else:
    from textual.drivers.linux_driver import LinuxDriver as _BaseDriver


# ---------------------------------------------------------------------------
# Windows conhost 兼容层
#
# Textual 原生 WindowsDriver 的两个 conhost 痛点（cmd / Windows PowerShell
# 独立窗口）：
#
# 1. 中文输入完全不可用——win32.enable_application_mode() 把控制台输入模式
#    整体替换为 ENABLE_VIRTUAL_TERMINAL_INPUT，conhost 在该模式下禁用 IME
#    组字。修复：不启用 VT 输入模式，IME 上屏的汉字仍以 UnicodeChar 非零的
#    KEY_EVENT 记录到达，直接透传。
# 2. 关闭 VT 输入模式的两个副作用需要补偿：
#    a. 功能键（方向键/Home/End/PgUp/PgDn/Delete/F1-F4）不再由 conhost 展开
#       为 VT 序列，而是 VK 码 + UnicodeChar=\x00——按虚拟键码手动翻译。
#    b. 鼠标事件不再自动转发。原生 EventMonitor 本来就忽略 MOUSE_EVENT_RECORD，
#       这导致 conhost 下滚轮只能滚动终端 scrollback（看起来就像"UI 没锁定"）。
#       这里把滚轮记录翻译成 Textual 的 MouseScrollUp/Down 事件发给应用。
# ---------------------------------------------------------------------------

if sys.platform == "win32":
    # 虚拟键码 → VT 序列（UnicodeChar 为 \x00 时按此表翻译）
    _VK_SEQUENCES = {
        0x26: "\x1b[A",    # VK_UP
        0x28: "\x1b[B",    # VK_DOWN
        0x27: "\x1b[C",    # VK_RIGHT
        0x25: "\x1b[D",    # VK_LEFT
        0x24: "\x1b[H",    # VK_HOME
        0x23: "\x1b[F",    # VK_END
        0x21: "\x1b[5~",   # VK_PRIOR (PageUp)
        0x22: "\x1b[6~",   # VK_NEXT (PageDown)
        0x2D: "\x1b[2~",   # VK_INSERT
        0x2E: "\x1b[3~",   # VK_DELETE
        0x70: "\x1bOP",    # VK_F1
        0x71: "\x1bOQ",    # VK_F2
        0x72: "\x1bOR",    # VK_F3
        0x73: "\x1bOS",    # VK_F4
    }
    _CTRL_PRESSED = 0x0004 | 0x0008  # RIGHT_CTRL_PRESSED | LEFT_CTRL_PRESSED
    _CTRL_ARROWS = {0x26, 0x28, 0x27, 0x25, 0x24, 0x23}
    _MOUSE_EVENT = 0x0002
    _MOUSE_WHEELED = 0x0004

    class ConhostEventMonitor(_win32.EventMonitor):
        """兼容中文 IME 的输入事件监视器（仅 Windows 使用）。

        与原生 EventMonitor 的差异：功能键按 VK 码翻译成 VT 序列；
        滚轮记录翻译成 MouseScrollUp/Down 转发给应用。其余逻辑保持一致。
        """

        def run(self) -> None:
            exit_requested = self.exit_event.is_set
            parser = _win32.XTermParser(debug=_win32.constants.DEBUG)

            try:
                read_count = _win32.wintypes.DWORD(0)
                hIn = _win32.GetStdHandle(_win32.STD_INPUT_HANDLE)

                MAX_EVENTS = 1024
                KEY_EVENT = 0x0001
                WINDOW_BUFFER_SIZE_EVENT = 0x0004

                arrtype = _win32.INPUT_RECORD * MAX_EVENTS
                input_records = arrtype()
                ReadConsoleInputW = _win32.KERNEL32.ReadConsoleInputW
                keys: list[str] = []
                append_key = keys.append

                while not exit_requested():
                    for event in parser.tick():
                        self.process_event(event)

                    if _win32.wait_for_handles([hIn], 100) is None:
                        continue

                    ReadConsoleInputW(
                        hIn, _win32.byref(input_records), MAX_EVENTS,
                        _win32.byref(read_count),
                    )
                    read_input_records = input_records[: read_count.value]

                    del keys[:]
                    new_size = None

                    for input_record in read_input_records:
                        event_type = input_record.EventType

                        if event_type == KEY_EVENT:
                            key_event = input_record.Event.KeyEvent
                            key = key_event.uChar.UnicodeChar
                            if not key_event.bKeyDown:
                                continue
                            if key and key != "\x00":
                                # 普通字符（含 IME 上屏的汉字）直接透传
                                append_key(key)
                                continue
                            # 功能键：VT 输入模式已关闭，conhost 不再展开为
                            # VT 序列，按虚拟键码手动翻译
                            vk = key_event.wVirtualKeyCode
                            seq = _VK_SEQUENCES.get(vk)
                            if seq is None:
                                continue
                            if (
                                key_event.dwControlKeyState & _CTRL_PRESSED
                                and vk in _CTRL_ARROWS
                            ):
                                seq = f"\x1b[1;5{seq[-1]}"
                            append_key(seq)

                        elif event_type == _MOUSE_EVENT:
                            mouse = input_record.Event.MouseEvent
                            if not (mouse.dwEventFlags & _MOUSE_WHEELED):
                                continue
                            # dwButtonState 高 16 位是带符号滚轮增量（±120 的倍数）
                            delta = mouse.dwButtonState >> 16
                            if delta >= 32768:
                                delta -= 65536
                            if delta == 0:
                                continue
                            cls = (
                                _events.MouseScrollUp
                                if delta > 0
                                else _events.MouseScrollDown
                            )
                            x = mouse.dwMousePosition.X
                            y = mouse.dwMousePosition.Y
                            for _ in range(max(1, abs(delta) // 120)):
                                self.process_event(
                                    cls(None, x, y, 0, 0, 0, False, False, False)
                                )

                        elif event_type == WINDOW_BUFFER_SIZE_EVENT:
                            size = input_record.Event.WindowBufferSizeEvent.dwSize
                            new_size = (size.X, size.Y)

                    if keys:
                        for event in parser.feed(
                            "".join(keys).encode("utf-16", "surrogatepass")
                            .decode("utf-16")
                        ):
                            self.process_event(event)
                    if new_size is not None:
                        self.on_size_change(*new_size)

            except Exception as error:
                self.app.log.error("EVENT MONITOR ERROR", error)


class NoAltScreenDriver(_BaseDriver):
    """跨平台终端驱动。

    - POSIX：跳过备用屏（alternate screen），让输出保留在主终端的
      滚动回看（scrollback）区域中——与 Claude Code 的渲染行为保持一致。
      原理：去掉 alt screen 切换码，并在进入应用模式时输出足够多的空行，
      将已有终端内容推入 scrollback，Textual 在"新页面"上渲染。

    - Windows：必须使用备用屏（conhost 在无备用屏时会随每次重绘滚动
      缓冲区，旧帧堆叠成残影），并启用 IME 兼容的输入模式与功能键/滚轮
      翻译（见文件头部注释），保证 cmd / Windows PowerShell 下中文可输入、
      视口锁定、滚轮滚动聊天区。
    """

    _use_alt_screen = sys.platform == "win32"

    def start_application_mode(self):
        if sys.platform == "win32":
            self._start_windows()
            return
        log.info("driver start: platform=%s alt_screen=False", sys.platform)
        try:
            rows = os.get_terminal_size().lines
        except OSError:
            rows = 24
        # 在 Textual 接管终端之前，用换行把已有内容推入 scrollback
        sys.stdout.write("\n" * rows)
        sys.stdout.flush()
        super().start_application_mode()

    def _start_windows(self) -> None:
        """Windows 启动序列：与 WindowsDriver 相同的输出初始化，但输入模式
        兼容 IME，事件监视器替换为 ConhostEventMonitor。"""
        from textual.drivers._writer_thread import WriterThread

        try:
            terminal_in = sys.__stdin__
            terminal_out = sys.__stdout__
            saved_in = _win32.get_console_mode(terminal_in)
            saved_out = _win32.get_console_mode(terminal_out)

            def restore() -> None:
                _win32.set_console_mode(terminal_in, saved_in)
                _win32.set_console_mode(terminal_out, saved_out)

            self._restore_console = restore
            # 输出：VT 处理（颜色/光标定位/1049h 依赖它）
            _win32.set_console_mode(
                terminal_out, saved_out | _win32.ENABLE_VIRTUAL_TERMINAL_PROCESSING
            )
            # 输入：不设 ENABLE_VIRTUAL_TERMINAL_INPUT（会禁用 IME）。
            # WINDOW_INPUT=窗口尺寸事件；MOUSE_INPUT=鼠标/滚轮记录入队；
            # EXTENDED_FLAGS=设置前两项时必须附带。
            _win32.set_console_mode(
                terminal_in,
                _win32.ENABLE_WINDOW_INPUT
                | _win32.ENABLE_MOUSE_INPUT
                | _win32.ENABLE_EXTENDED_FLAGS,
            )
        except Exception as e:
            # 非控制台句柄（管道/重定向）等场景：回退到原生启动逻辑
            log.warning("console mode setup failed (%s), fallback to default", e)
            super().start_application_mode()
            return

        log.info(
            "driver start: platform=win32 alt_screen=True ime_friendly_input=True"
        )

        loop = asyncio.get_running_loop()
        self._writer_thread = WriterThread(self._file)
        self._writer_thread.start()

        self.write("\x1b[?1049h")    # 备用屏：锁定视口
        self.write("\x1b[2J\x1b[H")  # 兜底清屏：忽略 1049h 的终端也从干净页面开始
        self._enable_mouse_support()
        self.write("\x1b[?25l")      # 隐藏光标
        self.write("\033[?1004h")    # FocusIn/FocusOut
        self.write("\x1b[>1u")       # kitty 键盘协议（支持的终端上）
        self.flush()
        self._enable_bracketed_paste()

        self._event_thread = ConhostEventMonitor(
            loop, self._app, self.exit_event, self.process_message
        )
        self._event_thread.start()

    def write(self, data: str) -> None:
        if not self._use_alt_screen:
            if "\x1b[?1049h" in data:
                data = data.replace("\x1b[?1049h", "")
            if "\x1b[?1049l" in data:
                data = data.replace("\x1b[?1049l", "")
        if data:
            super().write(data)
