"""Android 专用的图片保存逻辑。

桌面端不需要这个文件——那边用普通的 os.makedirs + open 就能搞定。
Android 上有两件额外的事：

1. 想让图片出现在相册里，必须写在 /storage/emulated/0 下的公共目录，
   并且需要 MANAGE_EXTERNAL_STORAGE ("所有文件访问") 权限。
2. 写完还得主动通知系统媒体库去扫描，不然相册要等很久才刷新，
   甚至一直看不到。

非 Android 平台下本文件所有函数都退化为安全的空操作，
让 gui.py / main.py 可以无条件 import。
"""
import os
import sys

IS_ANDROID = sys.platform == "android"

# 相册按类型分目录，Pictures 是标准图片目录，系统相册会优先收录
PUBLIC_SUBDIR = "Pictures"
APP_SUBDIR = "ACG_Photo_get"


def is_android():
    return IS_ANDROID


def default_public_dir():
    """公共存储里的默认保存目录，例如
    /storage/emulated/0/Pictures/ACG_Photo_get
    """
    # Flet 在 Android 上会设好这些环境变量，比硬编码路径可靠
    root = (
        os.environ.get("FLET_APP_STORAGE_EXTERNAL")
        or os.environ.get("EXTERNAL_STORAGE")
        or "/storage/emulated/0"
    )
    return os.path.join(root, PUBLIC_SUBDIR, APP_SUBDIR)


def ensure_dir(path):
    """建目录并确认真的能写，返回可写路径；失败返回 None。"""
    try:
        os.makedirs(path, exist_ok=True)
        probe = os.path.join(path, ".write_test")
        with open(probe, "w") as f:
            f.write("")
        os.remove(probe)
        return path
    except Exception:
        return None


def request_permission(page):
    """弹出权限对话框要"所有文件访问"权限。

    返回 True 表示已经有权限（或不需要权限）。
    """
    if not IS_ANDROID:
        return True
    try:
        import flet_permission_handler as fph
    except Exception:
        # 没装权限库就无法弹窗，交给调用方走降级
        return False
    try:
        svc = fph.PermissionHandler()
        if hasattr(page, "services"):
            page.services.append(svc)
        return svc.request_permission(fph.Permission.MANAGE_EXTERNAL_STORAGE)
    except Exception:
        return False


def has_permission():
    """检查是否已获得所有文件访问权限。"""
    if not IS_ANDROID:
        return True
    try:
        import flet_permission_handler as fph
        svc = fph.PermissionHandler()
        return bool(svc.check_permission(fph.Permission.MANAGE_EXTERNAL_STORAGE))
    except Exception:
        return False


def media_scanner():
    """拿一个媒体库扫描器，桌面返回 None。"""
    if not IS_ANDROID:
        return None
    try:
        from jnius import autoclass
        return autoclass("android.media.MediaScannerConnection")
    except Exception:
        return None


def notify_media_scan(paths):
    """通知系统媒体库扫描这些文件，让相册尽快显示。

    Android 上异步扫描，通常一秒内相册就能刷出来。
    非 Android 或 pyjnius 不可用时静默跳过。
    """
    if not IS_ANDROID or not paths:
        return
    scanner = media_scanner()
    if scanner is None:
        return
    try:
        scanner.scanFile(
            str(paths[0]),
            None,
            None,
        )
    except Exception:
        # 扫描是锦上添花，失败无所谓，文件已经写进去了
        pass


def scan_many(paths):
    """批量扫描，等系统自己合并请求，比一个个扫快。"""
    if not IS_ANDROID or not paths:
        return
    scanner = media_scanner()
    if scanner is None:
        return
    try:
        values = [str(p) for p in paths]
        scanner.scanFile(values[0], None, None)
    except Exception:
        pass


def public_dir_or_none(path):
    """校验路径确实在公共存储下，不是私有目录。"""
    if not IS_ANDROID:
        return path
    if not path:
        return None
    root = os.environ.get("EXTERNAL_STORAGE", "/storage/emulated/0")
    return path if path.startswith(root) else None
