"""Flet GUI for ACG_Photo_get (flet 1.x)."""
import asyncio
import logging
import os
import queue
import sys
import flet as ft
import main
import android_store

# 容错：如果 main.CONFIG 不存在
if not hasattr(main, 'CONFIG'):
    main.CONFIG = {
        "SAVE_DIR": "",
        "TARGET_COUNT": 30,
        "MAX_WORKERS": 5,
        "R18": False,
        "API_SOURCE": "nekos",
    }

IS_ANDROID = sys.platform == "android"

def default_save_dir():
    """Android 上用公共 Pictures 目录（相册可见），桌面用 home 下的老路径。"""
    if not IS_ANDROID:
        return os.path.expanduser("~/ACG_Photo_get")
    return android_store.default_public_dir()

class FletLogHandler(logging.Handler):
    """把日志行放进队列，由 UI 线程上的任务统一刷新。

    flet 1.x 的事件循环只有一个线程，工作线程不能直接改控件，
    所以日志先入队，再由 _pump_logs 在事件循环里更新 Text。
    """
    def __init__(self, output: "queue.Queue[str]"):
        super().__init__()
        self.output = output
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))

    def emit(self, record: logging.LogRecord):
        try:
            self.output.put_nowait(self.format(record))
        except Exception:
            self.handleError(record)

def apply_gui_config(target_tf, workers_tf, r18_dropdown,
                        api_dropdown, save_tf):
    def _int(val, default):
        try:
            return int(val) if val else default
        except Exception:
            return default

    main.CONFIG["TARGET_COUNT"] = _int(target_tf.value, 30)
    main.CONFIG["MAX_WORKERS"] = _int(workers_tf.value, 5)
    main.CONFIG["R18"] = str(r18_dropdown.value).lower() in ("true", "1", "yes")
    main.CONFIG["API_SOURCE"] = api_dropdown.value or "nekos"
    main.CONFIG["SAVE_DIR"] = save_tf.value or default_save_dir()

def main_gui(page: ft.Page):
    page.title = "ACG Photo GET"
    page.window.width = 720
    page.window.height = 560

    target_tf = ft.TextField(label="目标数量", value=str(main.CONFIG.get("TARGET_COUNT", 30)), width=120)
    workers_tf = ft.TextField(label="并发线程", value=str(main.CONFIG.get("MAX_WORKERS", 5)), width=120)

    r18_dropdown = ft.Dropdown(
        label="R18",
        options=[ft.DropdownOption("True"), ft.DropdownOption("False")],
        value=str(main.CONFIG.get("R18", False)),
        width=120
    )

    api_dropdown = ft.Dropdown(
        label="API 源",
        options=[ft.DropdownOption("nekos"), ft.DropdownOption("lolicon")],
        value=main.CONFIG.get("API_SOURCE", "nekos"),
        width=150
    )

    save_tf = ft.TextField(
        label="保存路径",
        value=str(main.CONFIG.get("SAVE_DIR") or default_save_dir()),
        width=300,
    )

    # flet 1.x：服务自注册，不需要 page.overlay / page.services；
    # get_directory_path() 是 async 的，直接返回路径，不再走 on_result。
    file_picker = ft.FilePicker()

    # 文件夹选择：Android 上系统选择器返回的路径可能不可写，
    # 所以只在成功时采纳，失败则回落到应用私有目录
    async def _on_save_path(e=None):
        path = None
        try:
            path = await file_picker.get_directory_path()
        except Exception:
            # web 模式不支持选择目录，直接回落
            path = None
        if path:
            try:
                os.makedirs(path, exist_ok=True)
                save_tf.value = path
            except Exception:
                save_tf.value = default_save_dir()
        else:
            save_tf.value = default_save_dir()

    folder_btn = ft.IconButton(
        icon=ft.Icons.FOLDER,
        tooltip="选择保存文件夹",
        on_click=_on_save_path
    )


    log_text = ft.Text(value="等待开始...", selectable=True, expand=True, size=12)
    log_queue: "queue.Queue[str]" = queue.Queue()

    logger = logging.getLogger()
    for h in list(logger.handlers):
        if isinstance(h, (logging.StreamHandler, logging.FileHandler)):
            logger.removeHandler(h)
    logger.addHandler(FletLogHandler(log_queue))
    logger.setLevel(logging.INFO)

    async def _pump_logs():
        """把工作线程写进队列的日志搬到 UI 上。"""
        while True:
            await asyncio.sleep(0.2)
            lines = []
            while True:
                try:
                    lines.append(log_queue.get_nowait())
                except queue.Empty:
                    break
            if not lines:
                continue
            current = log_text.value or ""
            if current and current != "等待开始...":
                current = current + "\n" + "\n".join(lines)
            else:
                current = "\n".join(lines)
            parts = current.splitlines()
            if len(parts) > 800:
                current = "\n".join(parts[-800:])
            try:
                log_text.value = current
                log_text.update()
            except Exception:
                # 页面已关闭时 task 会被取消，这里兜底避免刷屏
                return

    async def _start(e):
        start_btn.disabled = True
        start_btn.update()

        # Android 上要写公共目录得先拿到"所有文件访问"权限，
        # 没权限就退到应用私有目录，保证还能用
        if IS_ANDROID:
            await android_store.request_permission(page)
            target = save_tf.value or default_save_dir()
            if not android_store.public_dir_or_none(target):
                save_tf.value = android_store.default_public_dir()
            if not await android_store.has_permission():
                log_text.value = (
                    "⚠️ 没有存储权限，已保存到应用私有目录（相册看不到）\n"
                    "   如需在相册查看，请授权后重试\n"
                )
                save_tf.value = os.environ.get("FLET_APP_STORAGE_DATA", "")
                save_tf.update()

        apply_gui_config(
            target_tf, workers_tf, r18_dropdown, api_dropdown, save_tf,
        )

        # main.py 在导入时就把 SAVE_DIR 解析并建好目录了，用户在界面上
        # 改过路径的话，这里要重新解析一次并重建日志文件
        main.reinit_save_dir()

        log_text.value = "🚀 任务开始...\n"
        log_text.update()

        try:
            # 下载是阻塞 IO，放到线程里跑，事件循环继续处理 UI
            exit_code = await asyncio.to_thread(main.main)
        except (Exception, SystemExit) as ex:
            logger.exception(f"下载任务异常：{ex}")
            exit_code = 1
        finally:
            start_btn.disabled = False
            start_btn.update()

        if exit_code == 0:
            logger.info(f"✅ 完成: 下载成功 {main.CONFIG.get('TARGET_COUNT', 0)} 张")
        else:
            logger.warning("⚠️ 未完成")

    start_btn = ft.FilledButton(
        content="开始下载",
        icon=ft.Icons.DOWNLOAD,
        on_click=_start
    )

    # Android 上需要存储权限才能写进相册，提前说清楚，
    # 免得用户点开始才发现弹了个框
    if IS_ANDROID:
        perm_hint = ft.Container(
            content=ft.Row(
                [
                    ft.Icon(ft.Icons.INFO_OUTLINE, size=16, color=ft.Colors.BLUE_700),
                    ft.Text(
                        "首次开始下载会请求存储权限，授权后图片保存到 "
                        "Pictures/ACG_Photo_get，相册里可以直接看到",
                        size=12,
                        color=ft.Colors.BLUE_900,
                        expand=True,
                    ),
                ],
                spacing=8,
            ),
            padding=10,
            bgcolor=ft.Colors.BLUE_50,
            border_radius=5,
            border=ft.Border.all(1, ft.Colors.BLUE_200),
        )
    else:
        perm_hint = ft.Container(height=0)

    page.add(
        ft.Text("🎨 ACG Photo Downloader", size=24, weight=ft.FontWeight.BOLD),
        ft.Divider(),
        ft.Row([target_tf, workers_tf, r18_dropdown], spacing=10),
        ft.Row([api_dropdown]),
        ft.Row([save_tf, folder_btn], spacing=10),
        perm_hint,
        ft.Divider(),
        ft.Text("📋 日志输出", weight=ft.FontWeight.BOLD),
        ft.Container(
            content=log_text,
            expand=True,
            padding=10,
            bgcolor=ft.Colors.GREY_200,
            border_radius=5,
            border=ft.Border.all(1, ft.Colors.GREY_400)
        ),
        ft.Row([start_btn], alignment=ft.MainAxisAlignment.CENTER),
    )

    page.run_task(_pump_logs)


# flet 1.x：入口模块在模块顶层调用 ft.run()，flet build / flet run 都会从这里启动
ft.run(main_gui)
