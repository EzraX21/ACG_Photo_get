"""
依赖： requests
"""

download_path = "~/ACG_Photo_get"
import os
import logging
import requests
from io import BytesIO
import time
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock
import argparse
import json
import sys
import android_store

# --- 配置参数---
CONFIG = {
    "SAVE_DIR": os.path.expanduser(download_path),   # 保存路径
    "TAGS": [],                                               # 标签列表
    "TARGET_COUNT": 30,                                       # 最终必须下载到的张数
    "BATCH_SIZE": 20,                                         # 每次请求的数量（最大 100）
    "MAX_WORKERS": 5,                                         # 并发下载线程数
    "R18": False,                                             # True → explicit, False → safe
    "API_SOURCE": "nekos",                                      # "nekos" or "lolicon"
    "API_ENDPOINT": {"nekos": "https://api.nekosapi.com/v4/images", "lolicon": "https://api.lolicon.app/setu/v2"},
    "REQUEST_DELAY": 0.5,                                     # 每批请求间隔（秒）
    "MAX_LOOP": 50,                                           # 最大尝试轮次
}

# --------------------------------

# ---------- Path resolution ----------

def resolve_path(path: str) -> str:
    """Resolve user (~) and ${ENV} in a path string."""
    return os.path.expandvars(os.path.expanduser(path))

# Apply early resolution and ensure the directory exists.
# If creation fails (e.g., due to permission issues), fall back to a user‑writable folder.
def reinit_save_dir():
    """解析并创建 SAVE_DIR，同时把日志文件挪到新目录。

    GUI 在启动前可能改过 CONFIG["SAVE_DIR"]，所以要能重复调用。
    解析或建目录失败时回落到用户主目录，保证一定有地方写日志。
    """
    fallback_dir = os.path.join(os.path.expanduser("~"), "ACG_Photo_get")
    try:
        resolved_dir = resolve_path(CONFIG["SAVE_DIR"])
        os.makedirs(resolved_dir, exist_ok=True)
        # 目录建得出来不代表能写，试探一下
        probe = os.path.join(resolved_dir, ".write_test")
        with open(probe, "w") as f:
            f.write("")
        os.remove(probe)
    except Exception as e:
        logging.warning(
            f"Failed to use SAVE_DIR ({CONFIG['SAVE_DIR']}), "
            f"falling back to {fallback_dir}: {e}"
        )
        try:
            os.makedirs(fallback_dir, exist_ok=True)
        except Exception:
            pass
        resolved_dir = fallback_dir

    CONFIG["SAVE_DIR"] = resolved_dir

    # 日志文件跟着换目录，否则新目录里没有 photo_get.log
    new_handler = logging.FileHandler(os.path.join(resolved_dir, "photo_get.log"))
    new_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    for h in list(logging.getLogger().handlers):
        if isinstance(h, logging.FileHandler):
            logging.getLogger().removeHandler(h)
            h.close()
    logging.getLogger().addHandler(new_handler)
    return resolved_dir

reinit_save_dir()

# ---------- Logging Setup ----------


# Ensure log directory exists before configuring handlers (already ensured above)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(CONFIG["SAVE_DIR"], "photo_get.log")),
        logging.StreamHandler()
    ]
)



# ---------- PID Persistence ----------
PID_LOG = os.path.join(CONFIG["SAVE_DIR"], ".downloaded_pids.txt")

def load_seen_pids() -> set:
    """Load already downloaded PIDs from PID_LOG if it exists."""
    if os.path.isfile(PID_LOG):
        try:
            return {line.strip() for line in open(PID_LOG, encoding="utf-8") if line.strip()}
        except Exception as e:
            logging.warning(f"Failed to read PID log: {e}")
    return set()

def register_pid(pid: str):
    """Append a PID to the persistent log file."""
    try:
        with open(PID_LOG, "a", encoding="utf-8") as f:
            f.write(pid + "\n")
    except Exception as e:
        logging.warning(f"Failed to write PID {pid} to log: {e}")

def safe_print(*args, **kwargs):
    # Preserve original behaviour but route through logging
    message = " ".join(str(a) for a in args)
    logging.info(message)

# ---------- CLI 参数解析 ----------
def parse_args():
    parser = argparse.ArgumentParser(description="ACG Photo Downloader")
    parser.add_argument("-t", "--target", type=int, help="目标下载数量")
    parser.add_argument("-w", "--workers", type=int, help="并发线程数")
    parser.add_argument("-r", "--r18", action="store_true", help="下载 R18 内容")
    parser.add_argument("-c", "--config", type=str, help="自定义 JSON 配置文件路径")
    parser.add_argument("-e", "--export-config", type=str, help="导出当前运行时配置到指定 JSON 文件路径")
    parser.add_argument("--api", choices=["nekos", "lolicon"], help="切换使用的 API 源（默认 nekos）")
    return parser.parse_args()


def setup_directory():
    try:
        os.makedirs(CONFIG["SAVE_DIR"], exist_ok=True)
        test_file = os.path.join(CONFIG["SAVE_DIR"], ".write_test")
        with open(test_file, 'w') as f:
            f.write('test')
        os.remove(test_file)
        print(f"📁 图片将保存至：{CONFIG['SAVE_DIR']}")
    except (PermissionError, OSError) as e:
        print(f"❌ 目录不可写：{CONFIG['SAVE_DIR']}，请修改配置")
        raise

def get_file_extension(content_type, url):
    ext_map = {
        'image/jpeg': '.jpg', 'image/png': '.png', 'image/gif': '.gif',
        'image/webp': '.webp', 'image/bmp': '.bmp',
    }
    if content_type in ext_map:
        return ext_map[content_type]
    path = urlparse(url).path
    if '.' in path:
        ext = os.path.splitext(path)[1].lower()
        if ext in ['.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp']:
            return '.jpg' if ext == '.jpeg' else ext
    return '.jpg'

# ===================== NekosAPI 源函数 =====================

_nekos_offset = 0

def fetch_from_nekosapi():
    global _nekos_offset

    params = {
        "limit": min(CONFIG["BATCH_SIZE"], 100),
        "offset": _nekos_offset,
        "sort": "created_at",
        "order": "desc",
    }

    if CONFIG["TAGS"]:
        params["tags"] = ",".join(CONFIG["TAGS"])

    params["rating"] = "explicit" if CONFIG["R18"] else "safe"

    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

    try:
        retry = 0
        while True:
            resp = requests.get("https://api.nekosapi.com/v4/images", params=params, headers=headers, timeout=15)
            if resp.status_code == 429:
                wait = min(2 ** retry, 30)
                safe_print(f"⚠️ NekosAPI 触发限流，等待 {wait}s 后重试...")
                time.sleep(wait)
                retry += 1
                continue
            resp.raise_for_status()
            break
        data = resp.json()
        items = data.get("items", [])

        if not items:
            _nekos_offset = 0
            safe_print("🔄 已到达末尾，重置 offset 重新获取")
            return []

        _nekos_offset += len(items)

        formatted = []
        for item in items:
            pid = item.get("id")
            if not pid:
                continue
            title = item.get("title") or f"Image_{pid}"
            artist = item.get("artist")
            author = artist.get("name") if artist and isinstance(artist, dict) else "Unknown"
            img_url = item.get("url")
            if not img_url:
                continue

            formatted.append({
                "pid": pid,
                "title": title,
                "author": author,
                "urls": {"original": img_url}
            })

        safe_print(f"✅ 从 NekosAPI 获取 {len(formatted)} 张图片 (offset={_nekos_offset - len(formatted)})")
        return formatted

    except requests.exceptions.RequestException as e:
        safe_print(f"❌ NekosAPI 请求失败：{e}")
        return []
    except Exception as e:
        safe_print(f"❌ NekosAPI 数据解析失败：{e}")
        return []

def fetch_one_batch():
    if CONFIG.get("API_SOURCE") == "lolicon":
        return fetch_from_loliconapi()
    return fetch_from_nekosapi()

# ===================== 单个图片下载 =====================

def download_single_image(image_info):
    """下载单张图片并返回状态字符串。
    返回值:
        "success"   - 下载并保存成功
        "duplicate" - 文件已存在或 PID 已记录
        "error"     - 其它异常或 HTTP 错误
    """
    pid = image_info.get("pid", "unknown")
    title = image_info.get("title", "无标题")
    urls = image_info.get("urls", {})
    img_url = urls.get("original", "")
    if not img_url:
        return "error"

    safe_title = "".join(c if c.isalnum() else "_" for c in title)[:30] or str(pid)

    try:
        # ---- 请求并流式读取 ----
        retry = 0
        while True:
            resp = requests.get(img_url, timeout=20, stream=True)
            if resp.status_code == 429:
                wait = min(2 ** retry, 30)
                safe_print(f"⚠️ 图片请求触发限流，等待 {wait}s 重试...")
                time.sleep(wait)
                retry += 1
                continue
            resp.raise_for_status()
            break

        content_type = resp.headers.get('Content-Type', '')
        if 'image' not in content_type:
            return "error"
        ext = get_file_extension(content_type, img_url)
        filename = f"{pid}_{safe_title}{ext}"
        base_path = os.path.join(CONFIG["SAVE_DIR"], filename)


        # ---- 下载内容 ----
        img_bytes = BytesIO()
        for chunk in resp.iter_content(chunk_size=8192):
            if chunk:
                img_bytes.write(chunk)
        img_bytes.seek(0)

        # ---- 冲突命名（添加序号） ----
        final_path = base_path
        if os.path.exists(final_path):
            base, ext_part = os.path.splitext(base_path)
            counter = 1
            while os.path.exists(final_path):
                final_path = f"{base}_{counter}{ext_part}"
                counter += 1
        # 写入文件
        with open(final_path, "wb") as f:
            f.write(img_bytes.getbuffer())
        # Android 上通知媒体库，这样相册能立刻看到新图
        android_store.notify_media_scan([final_path])
        safe_print(f"  ✅ 下载成功：{os.path.basename(final_path)}")
        # 记录 PID
        register_pid(str(pid))
        return "success"

    except Exception as e:
        safe_print(f"  ⚠️ 下载失败 {pid}: {e}")
        return "error"

# ===================== Lolicon API 源函数 =====================

def fetch_from_loliconapi():
    """Fetch images from lolicon API.
    Returns list of formatted dicts similar to NekosAPI output.
    """
    params = {
        "r18": 1 if CONFIG.get("R18") else 0,
        "num": min(CONFIG.get("BATCH_SIZE", 20), 20),
        "size": "original",
    }
    if CONFIG.get("TAGS"):
        params["tag"] = ",".join(CONFIG["TAGS"])
    # API endpoint may be overridden via CONFIG["API_ENDPOINT"]["lolicon"]
    url = CONFIG.get("API_ENDPOINT", {}).get("lolicon", "https://api.lolicon.app/setu/v2")
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        # lolicon returns {'error': '', 'data': [ ... ]}
        items = data.get("data", []) if not data.get("error") else []
        formatted = []
        for item in items:
            pid = item.get("pid")
            if not pid:
                continue
            title = item.get("title") or f"Image_{pid}"
            author = item.get("author") or "Unknown"
            img_url = item.get("url") or (item.get("urls", {}) or {}).get("original")
            if not img_url:
                # try “original” in urls dict if present
                img_url = item.get("urls", {}).get("original")
            if not img_url:
                continue
            formatted.append({
                "pid": pid,
                "title": title,
                "author": author,
                "urls": {"original": img_url},
            })
        safe_print(f"✅ 从 Lolicon 获取 {len(formatted)} 张图片")
        return formatted
    except requests.exceptions.RequestException as e:
        safe_print(f"❌ Lolicon 请求失败：{e}")
        return []
    except Exception as e:
        safe_print(f"❌ Lolicon 数据解析失败：{e}")
        return []

# ===================== 单个图片下载（无分辨率检查） =====================

def main():
    print("=" * 50)
    print("🚀 二次元图片自动搬运工（NekosAPI 版 · 无分辨率限制）")
    print("=" * 50)
    setup_directory()

    # ---------- 参数覆盖 ----------
    args = parse_args()
    if args.target is not None:
        CONFIG["TARGET_COUNT"] = args.target
    if args.workers is not None:
        CONFIG["MAX_WORKERS"] = args.workers
    if args.r18:
        CONFIG["R18"] = True
    if args.api:
        CONFIG["API_SOURCE"] = args.api
    if args.config:
        try:
            with open(args.config, "r", encoding="utf-8") as f:
                custom = json.load(f)
                CONFIG.update(custom)
            safe_print(f"⚙️ 已加载自定义配置文件: {args.config}")
        except Exception as e:
            safe_print(f"⚠️ 加载配置文件失败 {args.config}: {e}")
    # 导出当前配置（包括命令行覆盖后的）
    if args.export_config:
        try:
            with open(args.export_config, "w", encoding="utf-8") as f:
                json.dump(CONFIG, f, ensure_ascii=False, indent=2)
            safe_print(f"✅ 已导出运行时配置至: {args.export_config}")
        except Exception as e:
            safe_print(f"⚠️ 导出配置失败 {args.export_config}: {e}")
        # 导出后直接结束程序
        sys.exit(0)

    target = CONFIG["TARGET_COUNT"]
    print(f"🎯 目标下载数量：{target} 张")
    print(f"🧵 并发线程数：{CONFIG['MAX_WORKERS']}")
    if CONFIG["TAGS"]:
        print(f"🏷️  标签：{', '.join(CONFIG['TAGS'])}")
    print(f"🔞 当前 rating：{'explicit' if CONFIG['R18'] else 'safe'}")

    success_count = 0
    loop_count = 0
    max_loop = CONFIG["MAX_LOOP"]
    # Load already processed PID set from persistent log
    seen_pids = load_seen_pids()
    # 统计信息
    stats = {"success": 0, "duplicate": 0, "error": 0}

    while success_count < target and loop_count < max_loop:
        loop_count += 1
        safe_print(f"\n--- 第 {loop_count} 轮尝试（当前成功 {success_count}/{target}）---")

        batch = fetch_one_batch()
        if not batch:
            safe_print("⚠️ 本轮未获取到任何图片，等待后重试...")
            time.sleep(CONFIG["REQUEST_DELAY"] * 2)
            continue

        new_images = []
        for img in batch:
            pid = img.get("pid")
            if pid and pid not in seen_pids:
                seen_pids.add(pid)
                new_images.append(img)

        if not new_images:
            safe_print("🔄 本轮图片均已处理过，跳过")
            time.sleep(CONFIG["REQUEST_DELAY"])
            continue

        safe_print(f"📥 获取到 {len(new_images)} 张新图片，开始并发下载...")

        with ThreadPoolExecutor(max_workers=CONFIG["MAX_WORKERS"]) as executor:
            futures = [executor.submit(download_single_image, img) for img in new_images]
            for future in as_completed(futures):
                try:
                    status = future.result()
                    if status == "success":
                        success_count += 1
                        stats["success"] += 1
                        safe_print(f"📈 当前成功总数：{success_count}/{target}")
                    elif status == "duplicate":
                        stats["duplicate"] += 1
                    else:
                        stats["error"] += 1
                except Exception as e:
                    safe_print(f"⚠️ 下载线程异常：{e}")
                    stats["error"] += 1

        if success_count >= target:
            break

        if loop_count < max_loop:
            time.sleep(CONFIG["REQUEST_DELAY"])

    print("\n" + "=" * 50)
    if success_count >= target:
        print(f"🎉 任务完成！成功下载 {success_count} 张图片，达到预设目标！")
    else:
        print(f"⚠️ 已达到最大尝试次数 {max_loop}，当前成功 {success_count} 张，未达目标。")
        print("   你可以增加 MAX_LOOP 或检查网络/API 状态。")
    print(f"   📁 保存位置：{CONFIG['SAVE_DIR']}")
    # 打印统计信息
    print("统计信息:")
    for k, v in stats.items():
        print(f"  {k}: {v}")
    print("=" * 50)
    return 0 if success_count >= target else 1

if __name__ == "__main__":
    sys.exit(main())
