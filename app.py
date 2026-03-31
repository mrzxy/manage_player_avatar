"""
选手头像管理工具
- 输入比赛ID，从接口拉取选手列表
- 生成 xlsx（id / name / avatar）
- 下载头像到 avatar/ 子目录
- 重复执行时先删除旧目录
"""

import hashlib
import os
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import openpyxl
import requests

API_URL = "https://applyv3.ymq.me/api/v1/player/getlist"
AVATAR_PREFIX = "https://aijignsai.oss-cn-hangzhou.aliyuncs.com/playerphoto/2026/"
DOWNLOAD_WORKERS = 5


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

    # 兼容多种常见响应结构
    data = body.get("data") or body.get("result") or body
    if isinstance(data, dict):
        raw = data.get("list") or data.get("data") or data.get("players") or []
    elif isinstance(data, list):
        raw = data
    else:
        raw = []

    # 过滤 team_id <= 0
    return [p for p in raw if isinstance(p, dict) and int(p.get("team_id", 0)) > 0]


def download_image(url: str, dest: Path) -> tuple[bool, str]:
    try:
        r = requests.get(url, timeout=30)
        r.raise_for_status()
        dest.write_bytes(r.content)
        return True, dest.name
    except Exception as e:
        return False, str(e)


DEFAULT_OUTPUT = Path.cwd() / "outputs"
DEFAULT_OUTPUT.mkdir(parents=True, exist_ok=True)


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("选手头像管理工具")
        self.root.resizable(True, True)
        self._build_ui()

    # ------------------------------------------------------------------ UI --

    def _build_ui(self):
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(2, weight=1)

        # --- 配置区 ---
        cfg = ttk.LabelFrame(self.root, text="配置", padding=10)
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

        # --- 操作区 ---
        act = ttk.Frame(self.root, padding=(10, 0))
        act.grid(row=1, column=0, sticky="ew", padx=10, pady=5)

        self.start_btn = ttk.Button(act, text="开始生成", command=self._start)
        self.start_btn.pack(side=tk.LEFT)

        self.progress_var = tk.DoubleVar()
        ttk.Progressbar(
            act, variable=self.progress_var, mode="determinate", length=320
        ).pack(side=tk.LEFT, padx=10)

        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(act, textvariable=self.status_var, width=18).pack(side=tk.LEFT)

        # --- 日志区 ---
        log_frame = ttk.LabelFrame(self.root, text="日志", padding=5)
        log_frame.grid(row=2, column=0, sticky="nsew", padx=10, pady=(0, 10))
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)

        self.log_box = tk.Text(log_frame, state=tk.DISABLED, wrap=tk.WORD, font=("Courier", 11))
        self.log_box.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(log_frame, orient=tk.VERTICAL, command=self.log_box.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.log_box.configure(yscrollcommand=sb.set)

        self.root.geometry("680x520")

    # --------------------------------------------------------------- helpers --

    def _browse(self):
        d = filedialog.askdirectory(initialdir=self.out_dir_var.get())
        if d:
            self.out_dir_var.set(d)

    def _log(self, msg: str):
        def _do():
            self.log_box.config(state=tk.NORMAL)
            self.log_box.insert(tk.END, msg + "\n")
            self.log_box.see(tk.END)
            self.log_box.config(state=tk.DISABLED)

        self.root.after(0, _do)

    def _set_status(self, pct: float, text: str):
        def _do():
            self.progress_var.set(pct)
            self.status_var.set(text)

        self.root.after(0, _do)

    def _set_btn(self, enabled: bool):
        self.root.after(0, lambda: self.start_btn.config(state=tk.NORMAL if enabled else tk.DISABLED))

    # ----------------------------------------------------------------- task --

    def _start(self):
        game_id = self.game_id_var.get().strip()
        if not game_id:
            messagebox.showerror("错误", "请输入比赛 ID")
            return
        self._set_btn(False)
        self._set_status(0, "启动中…")
        threading.Thread(target=self._run, args=(game_id,), daemon=True).start()

    def _run(self, game_id: str):
        try:
            out_root = Path(self.out_dir_var.get())
            out_root.mkdir(parents=True, exist_ok=True)
            game_dir = out_root / game_id
            avatar_dir = game_dir / "avatar"

            # 删除旧目录（幂等）
            if game_dir.exists():
                self._log(f"[清理] 删除旧目录: {game_dir}")
                shutil.rmtree(game_dir)

            game_dir.mkdir(parents=True)
            avatar_dir.mkdir()

            # 1. 拉取数据
            self._log(f"[API] 请求比赛 {game_id} 的选手数据…")
            self._set_status(5, "拉取数据…")
            players = fetch_players(game_id)
            self._log(f"[API] 过滤后有效选手: {len(players)} 人")

            if not players:
                self._log("[警告] 没有 team_id > 0 的选手，任务结束。")
                self._set_status(100, "完成（空）")
                return

            # 2. 生成 Excel
            self._log("[Excel] 生成 xlsx…")
            self._set_status(15, "生成 Excel…")
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
            self._log(f"[Excel] 已保存: {xlsx_path}")

            # 3. 下载头像
            self._log(f"[下载] 开始下载 {len(players)} 张头像（并发={DOWNLOAD_WORKERS}）…")
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
                        self._log(f"  [失败] user_name={user_name}  idcard={idcard}  原因: {info}")
                    pct = 15 + done / total * 82
                    self._set_status(pct, f"下载 {done}/{total}")

            self._log(
                f"[完成] 成功 {done - failed}/{total}  失败 {failed}\n"
                f"       输出目录: {game_dir}"
            )
            self._set_status(100, "完成")

        except Exception as exc:
            self._log(f"[错误] {exc}")
            self._set_status(0, "出错")
        finally:
            self._set_btn(True)


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
