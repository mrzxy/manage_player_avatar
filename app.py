"""
选手头像管理工具
Tab1 - 生成选手数据: 输入比赛ID，拉取选手列表，生成 xlsx + 下载头像
Tab2 - 上传头像:    选择本地图片，转换为 JPG，上传到阿里云 OSS
Tab3 - 设置:        配置阿里云 AccessKey，持久化到本地
"""

import hashlib
import io
import json
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import openpyxl
import oss2
import requests
from PIL import Image

# ------------------------------------------------------------------ constants --

API_URL = "https://applyv3.ymq.me/api/v1/player/getlist"
AVATAR_PREFIX = "https://aijignsai.oss-cn-hangzhou.aliyuncs.com/playerphoto/2026/"
DOWNLOAD_WORKERS = 5

OSS_ENDPOINT = "oss-cn-hangzhou.aliyuncs.com"
OSS_BUCKET = "aijignsai"
OSS_DIR = "playerphoto/2026/"

DEFAULT_OUTPUT = Path.cwd() / "outputs"
DEFAULT_OUTPUT.mkdir(parents=True, exist_ok=True)

# ------------------------------------------------------------------ config --

CONFIG_FILE = Path.home() / ".manage_player_avatar" / "config.json"


def load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_config(data: dict):
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def mask(value: str, keep: int = 4) -> str:
    """显示首尾各 keep 个字符，中间替换为 ****。"""
    if not value:
        return ""
    if len(value) <= keep * 2:
        return "*" * len(value)
    return value[:keep] + "****" + value[-keep:]


# ------------------------------------------------------------------ helpers --


def md5_hex(text: str) -> str:
    return hashlib.md5(text.encode()).hexdigest()


def avatar_url(idcard: str) -> str:
    return f"{AVATAR_PREFIX}{md5_hex(idcard.lower())}.jpg"


def fetch_players(game_id: str) -> list[dict]:
    resp = requests.post(
        API_URL,
        json={"body": {"game_id": game_id}},
        headers={
            "User-Agent": "Apifox/1.0.0 (https://apifox.com)",
            "Content-Type": "application/json",
            "Accept": "*/*",
            "Host": "applyv3.ymq.me",
            "Connection": "keep-alive",
        },
        timeout=30,
    )
    resp.raise_for_status()
    body = resp.json()

    data = body.get("data") or body.get("result") or body
    if isinstance(data, dict):
        raw = data.get("list") or data.get("data") or data.get("players") or []
    elif isinstance(data, list):
        raw = data
    else:
        raw = []

    return [p for p in raw if isinstance(p, dict) and int(p.get("team_id", 0)) > 0]


def download_image(url: str, dest: Path) -> tuple[bool, str]:
    try:
        r = requests.get(url, timeout=30)
        r.raise_for_status()
        dest.write_bytes(r.content)
        return True, dest.name
    except Exception as e:
        return False, str(e)


def convert_to_jpg(src: Path) -> bytes:
    """将任意格式图片转换为 JPG 字节（透明通道以白色填充）。"""
    with Image.open(src) as img:
        if img.mode in ("RGBA", "LA", "P"):
            bg = Image.new("RGB", img.size, (255, 255, 255))
            bg.paste(img.convert("RGBA"), mask=img.convert("RGBA").split()[3])
            img = bg
        elif img.mode != "RGB":
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=95, subsampling=0)
        return buf.getvalue()


def oss_key(idcard: str) -> str:
    """idcard → md5 → OSS key。"""
    return f"{OSS_DIR}{md5_hex(idcard.lower())}.jpg"


# ------------------------------------------------------------------ log mixin --


class LogMixin:
    """为 Tab 提供线程安全的日志 / 进度方法。"""

    root: tk.Tk
    _log_box: tk.Text
    _progress_var: tk.DoubleVar
    _status_var: tk.StringVar

    def log(self, msg: str):
        def _do():
            self._log_box.config(state=tk.NORMAL)
            self._log_box.insert(tk.END, msg + "\n")
            self._log_box.see(tk.END)
            self._log_box.config(state=tk.DISABLED)
        self.root.after(0, _do)

    def set_status(self, pct: float, text: str):
        def _do():
            self._progress_var.set(pct)
            self._status_var.set(text)
        self.root.after(0, _do)

    def _make_log_area(self, parent: tk.Widget) -> tk.Text:
        frame = ttk.LabelFrame(parent, text="日志", padding=5)
        frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)

        box = tk.Text(frame, state=tk.DISABLED, wrap=tk.WORD, font=("Courier", 11))
        box.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=box.yview)
        sb.grid(row=0, column=1, sticky="ns")
        box.configure(yscrollcommand=sb.set)
        return box

    def _make_progress_bar(self, parent: tk.Widget) -> tuple[tk.DoubleVar, tk.StringVar]:
        pv = tk.DoubleVar()
        sv = tk.StringVar(value="就绪")
        bar_frame = ttk.Frame(parent)
        bar_frame.pack(fill=tk.X, padx=10, pady=(0, 5))
        ttk.Progressbar(bar_frame, variable=pv, mode="determinate", length=400).pack(side=tk.LEFT)
        ttk.Label(bar_frame, textvariable=sv, width=20).pack(side=tk.LEFT, padx=8)
        return pv, sv


# ------------------------------------------------------------------ Tab 1 --


class DownloadTab(LogMixin):
    def __init__(self, root: tk.Tk, notebook: ttk.Notebook):
        self.root = root
        frame = ttk.Frame(notebook)
        notebook.add(frame, text="  生成选手数据  ")
        self._build(frame)

    def _build(self, f: ttk.Frame):
        f.columnconfigure(0, weight=1)
        f.rowconfigure(2, weight=1)

        # 配置
        cfg = ttk.LabelFrame(f, text="配置", padding=10)
        cfg.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 5))
        cfg.columnconfigure(1, weight=1)

        ttk.Label(cfg, text="比赛 ID:").grid(row=0, column=0, sticky="w")
        self.game_id_var = tk.StringVar()
        ttk.Entry(cfg, textvariable=self.game_id_var, width=22).grid(
            row=0, column=1, sticky="w", padx=(5, 0)
        )

        ttk.Label(cfg, text="输出目录:").grid(row=1, column=0, sticky="w", pady=(8, 0))
        dir_row = ttk.Frame(cfg)
        dir_row.grid(row=1, column=1, sticky="ew", pady=(8, 0), padx=(5, 0))
        dir_row.columnconfigure(0, weight=1)
        self.out_dir_var = tk.StringVar(value=str(DEFAULT_OUTPUT))
        ttk.Entry(dir_row, textvariable=self.out_dir_var).grid(row=0, column=0, sticky="ew")
        ttk.Button(dir_row, text="浏览…", command=self._browse, width=6).grid(
            row=0, column=1, padx=(4, 0)
        )

        # 操作
        act = ttk.Frame(f, padding=(10, 0))
        act.grid(row=1, column=0, sticky="ew", padx=10, pady=5)
        self._btn = ttk.Button(act, text="开始生成", command=self._start)
        self._btn.pack(side=tk.LEFT)
        self._progress_var, self._status_var = self._make_progress_bar(act)

        # 日志
        log_wrap = ttk.Frame(f)
        log_wrap.grid(row=2, column=0, sticky="nsew")
        self._log_box = self._make_log_area(log_wrap)

    def _browse(self):
        d = filedialog.askdirectory(initialdir=self.out_dir_var.get())
        if d:
            self.out_dir_var.set(d)

    def _start(self):
        game_id = self.game_id_var.get().strip()
        if not game_id:
            messagebox.showerror("错误", "请输入比赛 ID")
            return
        self.root.after(0, lambda: self._btn.config(state=tk.DISABLED))
        threading.Thread(target=self._run, args=(game_id,), daemon=True).start()

    def _run(self, game_id: str):
        try:
            out_root = Path(self.out_dir_var.get())
            out_root.mkdir(parents=True, exist_ok=True)
            game_dir = out_root / game_id
            avatar_dir = game_dir / "avatar"

            if game_dir.exists():
                self.log(f"[清理] 删除旧目录: {game_dir}")
                shutil.rmtree(game_dir)
            game_dir.mkdir(parents=True)
            avatar_dir.mkdir()

            self.log(f"[API] 请求比赛 {game_id} 的选手数据…")
            self.set_status(5, "拉取数据…")
            players = fetch_players(game_id)
            self.log(f"[API] 过滤后有效选手: {len(players)} 人")

            if not players:
                self.log("[警告] 没有 team_id > 0 的选手，任务结束。")
                self.set_status(100, "完成（空）")
                return

            self.log("[Excel] 生成 xlsx…")
            self.set_status(15, "生成 Excel…")
            xlsx_path = game_dir / f"{game_id}.xlsx"
            wb = openpyxl.Workbook()
            ws = wb.active
            ws.title = "选手列表"
            ws.append(["id", "name", "avatar"])
            for p in players:
                pid = p.get("id", "")
                name = p.get("user_name", "")
                idcard = str(p.get("idcard", ""))
                ws.append([pid, name, avatar_url(idcard)])
            wb.save(xlsx_path)
            self.log(f"[Excel] 已保存: {xlsx_path}")

            self.log(f"[下载] 开始下载 {len(players)} 张头像（并发={DOWNLOAD_WORKERS}）…")
            total = len(players)
            done = 0
            failed = 0

            def _task(p: dict):
                idcard = str(p.get("idcard", ""))
                url = avatar_url(idcard)
                dest = avatar_dir / f"{p.get('id', md5_hex(idcard))}.jpg"
                ok, info = download_image(url, dest)
                return ok, info, p.get("user_name", ""), idcard

            with ThreadPoolExecutor(max_workers=DOWNLOAD_WORKERS) as pool:
                futures = {pool.submit(_task, p): p for p in players}
                for fut in as_completed(futures):
                    ok, info, user_name, idcard = fut.result()
                    done += 1
                    if not ok:
                        failed += 1
                        self.log(f"  [失败] user_name={user_name}  idcard={idcard}  原因: {info}")
                    self.set_status(15 + done / total * 82, f"下载 {done}/{total}")

            self.log(
                f"[完成] 成功 {done - failed}/{total}  失败 {failed}\n"
                f"       输出目录: {game_dir}"
            )
            self.set_status(100, "完成")

        except Exception as exc:
            self.log(f"[错误] {exc}")
            self.set_status(0, "出错")
        finally:
            self.root.after(0, lambda: self._btn.config(state=tk.NORMAL))


# ------------------------------------------------------------------ Tab 2 --


class UploadTab(LogMixin):
    def __init__(self, root: tk.Tk, notebook: ttk.Notebook):
        self.root = root
        self._files: list[Path] = []
        frame = ttk.Frame(notebook)
        notebook.add(frame, text="  上传头像  ")
        self._build(frame)

    def _build(self, f: ttk.Frame):
        f.columnconfigure(0, weight=1)
        f.rowconfigure(2, weight=1)

        # 配置
        cfg = ttk.LabelFrame(f, text="配置", padding=10)
        cfg.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 5))
        cfg.columnconfigure(1, weight=1)

        ttk.Label(cfg, text="比赛 ID:").grid(row=0, column=0, sticky="w")
        self.game_id_var = tk.StringVar()
        ttk.Entry(cfg, textvariable=self.game_id_var, width=22).grid(
            row=0, column=1, sticky="w", padx=(5, 0)
        )

        # 文件选择
        sel = ttk.LabelFrame(f, text="选择图片", padding=10)
        sel.grid(row=1, column=0, sticky="ew", padx=10, pady=5)
        sel.columnconfigure(1, weight=1)

        ttk.Button(sel, text="选择文件…", command=self._pick_files).grid(row=0, column=0)
        self._file_label = ttk.Label(sel, text="未选择任何文件", foreground="gray")
        self._file_label.grid(row=0, column=1, sticky="w", padx=(8, 0))

        act = ttk.Frame(sel)
        act.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        self._upload_btn = ttk.Button(act, text="开始上传", command=self._start)
        self._upload_btn.pack(side=tk.LEFT)
        self._progress_var, self._status_var = self._make_progress_bar(act)

        # 日志
        log_wrap = ttk.Frame(f)
        log_wrap.grid(row=2, column=0, sticky="nsew")
        self._log_box = self._make_log_area(log_wrap)

    def _pick_files(self):
        paths = filedialog.askopenfilenames(
            title="选择图片文件",
            filetypes=[
                ("图片文件", "*.jpg *.jpeg *.png *.bmp *.gif *.tiff *.webp"),
                ("所有文件", "*.*"),
            ],
        )
        if paths:
            self._files = [Path(p) for p in paths]
            self._file_label.config(
                text=f"已选 {len(self._files)} 个文件", foreground="black"
            )

    def _start(self):
        game_id = self.game_id_var.get().strip()
        if not game_id:
            messagebox.showerror("错误", "请输入比赛 ID")
            return
        if not self._files:
            messagebox.showerror("错误", "请先选择图片文件")
            return

        cfg = load_config()
        ak = cfg.get("oss_ak", "").strip()
        sk = cfg.get("oss_sk", "").strip()
        if not ak or not sk:
            messagebox.showerror("错误", "未配置阿里云 AccessKey，请前往「设置」Tab 填写")
            return

        self.root.after(0, lambda: self._upload_btn.config(state=tk.DISABLED))
        threading.Thread(
            target=self._run,
            args=(list(self._files), game_id, ak, sk),
            daemon=True,
        ).start()

    def _run(self, files: list[Path], game_id: str, ak: str, sk: str):
        try:
            auth = oss2.Auth(ak, sk)
            bucket = oss2.Bucket(auth, OSS_ENDPOINT, OSS_BUCKET)

            self.log(f"[API] 拉取比赛 {game_id} 选手数据…")
            self.set_status(0, "拉取数据…")
            players = fetch_players(game_id)
            id_to_idcard: dict[str, str] = {
                str(p.get("id", "")): str(p.get("idcard", ""))
                for p in players
                if p.get("id") and p.get("idcard")
            }
            self.log(f"[API] 获取到 {len(id_to_idcard)} 条 id→idcard 映射")

            total = len(files)
            done = 0
            failed = 0

            self.log(f"[上传] 共 {total} 个文件，目标: oss://{OSS_BUCKET}/{OSS_DIR}")
            self.set_status(0, "上传中…")

            for src in files:
                player_id = src.stem
                idcard = id_to_idcard.get(player_id)
                if not idcard:
                    failed += 1
                    self.log(f"  [失败] {src.name}  原因: 找不到 id={player_id} 对应的 idcard")
                    done += 1
                    self.set_status(done / total * 100, f"上传 {done}/{total}")
                    continue
                key = oss_key(idcard)
                try:
                    jpg_bytes = convert_to_jpg(src)
                    bucket.put_object(key, jpg_bytes)
                    self.log(f"  [OK] {src.name}  (idcard={idcard})  →  {key}")
                except Exception as e:
                    failed += 1
                    self.log(f"  [失败] {src.name}  原因: {e}")
                done += 1
                self.set_status(done / total * 100, f"上传 {done}/{total}")

            self.log(f"[完成] 成功 {done - failed}/{total}  失败 {failed}")
            self.set_status(100, "完成")

        except Exception as exc:
            self.log(f"[错误] {exc}")
            self.set_status(0, "出错")
        finally:
            self.root.after(0, lambda: self._upload_btn.config(state=tk.NORMAL))


# ------------------------------------------------------------------ Tab 3 --


class SettingsTab:
    def __init__(self, root: tk.Tk, notebook: ttk.Notebook):
        self.root = root
        frame = ttk.Frame(notebook)
        notebook.add(frame, text="  设置  ")
        self._build(frame)
        self._load()

    def _build(self, f: ttk.Frame):
        f.columnconfigure(0, weight=1)

        sec = ttk.LabelFrame(f, text="阿里云 OSS AccessKey", padding=15)
        sec.grid(row=0, column=0, sticky="ew", padx=20, pady=20)
        sec.columnconfigure(1, weight=1)

        # AK
        ttk.Label(sec, text="AccessKey ID:").grid(row=0, column=0, sticky="w")
        self._ak_var = tk.StringVar()
        ttk.Entry(sec, textvariable=self._ak_var, width=40).grid(
            row=0, column=1, sticky="ew", padx=(8, 0)
        )

        # SK
        ttk.Label(sec, text="AccessKey Secret:").grid(row=1, column=0, sticky="w", pady=(10, 0))
        sk_row = ttk.Frame(sec)
        sk_row.grid(row=1, column=1, sticky="ew", padx=(8, 0), pady=(10, 0))
        sk_row.columnconfigure(0, weight=1)
        self._sk_var = tk.StringVar()
        self._sk_entry = ttk.Entry(sk_row, textvariable=self._sk_var, show="*", width=40)
        self._sk_entry.grid(row=0, column=0, sticky="ew")
        self._sk_visible = False
        self._toggle_btn = ttk.Button(sk_row, text="显示", width=5, command=self._toggle_sk)
        self._toggle_btn.grid(row=0, column=1, padx=(6, 0))

        # 当前状态提示
        self._hint_var = tk.StringVar()
        ttk.Label(sec, textvariable=self._hint_var, foreground="gray").grid(
            row=2, column=0, columnspan=2, sticky="w", pady=(12, 0)
        )

        # 保存按钮
        btn_row = ttk.Frame(f)
        btn_row.grid(row=1, column=0, sticky="w", padx=20)
        ttk.Button(btn_row, text="保存配置", command=self._save).pack(side=tk.LEFT)
        self._result_label = ttk.Label(btn_row, text="", foreground="green")
        self._result_label.pack(side=tk.LEFT, padx=(10, 0))

    def _load(self):
        cfg = load_config()
        ak = cfg.get("oss_ak", "")
        sk = cfg.get("oss_sk", "")
        self._ak_var.set(ak)
        self._sk_var.set(sk)
        self._update_hint(ak, sk)

    def _update_hint(self, ak: str, sk: str):
        if ak and sk:
            self._hint_var.set(f"当前: ID={mask(ak)}  Secret={mask(sk)}  （已配置）")
        else:
            self._hint_var.set("当前: 未配置")

    def _toggle_sk(self):
        self._sk_visible = not self._sk_visible
        self._sk_entry.config(show="" if self._sk_visible else "*")
        self._toggle_btn.config(text="隐藏" if self._sk_visible else "显示")

    def _save(self):
        ak = self._ak_var.get().strip()
        sk = self._sk_var.get().strip()
        if not ak or not sk:
            messagebox.showerror("错误", "AccessKey ID 和 Secret 均不能为空")
            return
        cfg = load_config()
        cfg["oss_ak"] = ak
        cfg["oss_sk"] = sk
        save_config(cfg)
        self._update_hint(ak, sk)
        self._result_label.config(text="保存成功")
        self.root.after(3000, lambda: self._result_label.config(text=""))


# ------------------------------------------------------------------ main --


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("选手头像管理工具")
        self.root.resizable(True, True)
        self.root.geometry("720x560")
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)

        nb = ttk.Notebook(root)
        nb.grid(row=0, column=0, sticky="nsew", padx=5, pady=5)

        DownloadTab(root, nb)
        UploadTab(root, nb)
        SettingsTab(root, nb)


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
