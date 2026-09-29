"""Flet GUI for ACG_Photo_get (正式发布版 v1.0.0)."""
import logging
import threading
import os
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
    def __init__(self, output: ft.Text):  # ✅ 修复：__init__
        super().__init__()                # ✅ 修复：__init__
        self.output = output
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))

    def emit(self, record: logging.LogRecord):
        try:
            msg = self.format(record)
            if self.output.value:
                self.output.value += "\n" + msg
            else:
                self.output.value = msg
            self.output.update()
        except Exception:
            self.handleError(record)

def apply_gui_config(target_tf, workers_tf, r18_dropdown,
                        api_dropdown, save_tf):
    def _int(val, default):
        try:
            return int(val) if val else default
        except Exception:
            return default

    # ✅ 修复：移除键名末尾的空格
    main.CONFIG["TARGET_COUNT"] = _int(target_tf.value, 30)
    main.CONFIG["MAX_WORKERS"] = _int(workers_tf.value, 5)
    main.CONFIG["R18"] = str(r18_dropdown.value).lower() in ("true", "1", "yes")
    main.CONFIG["API_SOURCE"] = api_dropdown.value or "nekos"
    main.CONFIG["SAVE_DIR"] = save_tf.value or default_save_dir()

def main_gui(page: ft.Page):
    page.title = "ACG Photo GET"
    page.window_width = 720
    page.window_height = 560

    target_tf = ft.TextField(label="目标数量", value=str(main.CONFIG.get("TARGET_COUNT", 30)), width=120)
    workers_tf = ft.TextField(label="并发线程", value=str(main.CONFIG.get("MAX_WORKERS", 5)), width=120)

    r18_dropdown = ft.Dropdown(
        label="R18",
        options=[ft.dropdown.Option("True"), ft.dropdown.Option("False")],
        value=str(main.CONFIG.get("R18", False)),
        width=120
    )

    api_dropdown = ft.Dropdown(
        label="API 源",
        options=[ft.dropdown.Option("nekos"), ft.dropdown.Option("lolicon")],
        value=main.CONFIG.get("API_SOURCE", "nekos"),
        width=150
    )

    save_tf = ft.TextField(
        label="保存路径",
        value=str(main.CONFIG.get("SAVE_DIR") or default_save_dir()),
        width=300,
    )

    # 文件夹选择：Android 上系统选择器返回的路径可能不可写，
    # 所以只在成功时采纳，失败则回落到应用私有目录
    def _on_save_path(e):
        if e.path:
            try:
                os.makedirs(e.path, exist_ok=True)
                save_tf.value = e.path
            except Exception:
                save_tf.value = default_save_dir()
        else:
            save_tf.value = default_save_dir()
        save_tf.update()

    file_picker = ft.FilePicker()
    file_picker.on_result = _on_save_path
    if hasattr(page, "services"):
        page.services.append(file_picker)
    else:
        page.overlay.append(file_picker)

    # ✅ 修复：ft.Icons.FOLDER（大写 I）和 get_directory_path
    folder_btn = ft.IconButton(
        icon=ft.Icons.FOLDER,
        tooltip="选择保存文件夹",
        on_click=lambda e: file_picker.get_directory_path()  # ✅ 选择文件夹
    )


    log_text = ft.Text(value="等待开始...", selectable=True, expand=True, size=12)

    logger = logging.getLogger()
    for h in list(logger.handlers):
        if isinstance(h, (logging.StreamHandler, logging.FileHandler)):
            logger.removeHandler(h)
    logger.addHandler(FletLogHandler(log_text))
    logger.setLevel(logging.INFO)

    def _start(e):
        start_btn.disabled = True
        start_btn.update()

        # Android 上要写公共目录得先拿到"所有文件访问"权限，
        # 没权限就退到应用私有目录，保证还能用
        if IS_ANDROID:
            android_store.request_permission(page)
            target = save_tf.value or default_save_dir()
            if not android_store.public_dir_or_none(target):
                save_tf.value = android_store.default_public_dir()
            if not android_store.has_permission():
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

        def worker():
            try:
                exit_code = main.main()
            except Exception as ex:
                logger.exception(f"下载任务异常：{ex}")
                exit_code = 1

            if exit_code == 0:
                logger.info(f"✅ 完成: 下载成功 {main.CONFIG.get('TARGET_COUNT', 0)} 张")
            else:
                logger.warning(f"⚠️ 未完成")

            start_btn.disabled = False
            start_btn.update()

        threading.Thread(target=worker, daemon=True).start()

    start_btn = ft.FilledButton(
        text="开始下载",
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
            border=ft.border.all(1, ft.Colors.BLUE_200),
        )
    else:
        perm_hint = ft.Container(height=0)

    # ✅ 修复：使用 GREY_200 替代不存在的 SURFACE_VARIANT
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
            bgcolor=ft.Colors.GREY_200,  # ✅ 修复
            border_radius=5,
            border=ft.border.all(1, ft.Colors.GREY_400)  # ✅ 修复
        ),
        ft.Row([start_btn], alignment=ft.MainAxisAlignment.CENTER),
    )

# ✅ 修复：__name__ == "__main__"
if __name__ == "__main__":
    ft.app(target=main_gui)  # ✅ 使用 ft.app
