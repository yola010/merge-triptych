# -*- coding: utf-8 -*-
"""
16:9 横屏拼接窗口：2 张或 3 张竖图 -> 居中裁剪预处理 -> 固定 16:9 横屏成品。

尺寸推导（成品 W:H = 16:9，H 为成品高度，n 为张数）：
    W = 16H/9，每张宽 w = W/n，每张高 h = H
    n=2 -> w:h = 8:9   （w = 8H/9，  H 取 9 的倍数保证整数）
    n=3 -> w:h = 16:27 （w = 16H/27，H 取 27 的倍数保证整数）
    无缝横拼后 n*w = 16H/9，比例精确 16:9。

调用链：
    tkinter 窗口
      -> powershell.exe -EncodedCommand (UTF-16LE，规避中文路径 / 引号 / 编码坑)
        -> resize.py --mode cover --width w --height h   （ImageOps.fit 居中裁剪）
        -> merge.py --preset two_h|three_h --gap-mm 0 --output <成品>
      -> 临时预处理文件用 TemporaryDirectory 自动清理

设计要点：
- 居中由 resize.py 的 ImageOps.fit(centering=(0.5,0.5)) 保证，几何居中不偏移。
- 路径用 PowerShell 单引号字面量注入，内部单引号翻倍，杜绝引号闭合问题。
- 两次 PowerShell 调用，JSON 都在 Python 端解析，绕开 PS 5.1 ConvertFrom-Json 的编码坑。
- 拼接放工作线程，UI 更新用 root.after 回主线程；pythonw 无控制台，异常写 merge_gui.log。
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import tempfile
import threading
import tkinter as tk
import traceback
from tkinter import messagebox, ttk

# ---------------- 配置：环境变了只改这里 ----------------
POWERSHELL = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
PY_IMPL = r"G:\AgentEnv\venv\Scripts\python.exe"  # 装有 Pillow，执行 resize.py / merge.py
HERE = os.path.dirname(os.path.abspath(__file__))
RESIZE_SCRIPT = os.path.join(HERE, "resize.py")  # 本地副本，居中裁剪
MERGE_SCRIPT = os.path.join(HERE, "merge.py")  # 本地副本，无缝拼接
LOG_FILE = os.path.join(HERE, "merge_gui.log")
JPEG_QUALITY = 95  # 中间图质量，减轻二次 JPEG 压缩损失
FORCE_HEIGHT = None  # None=按最小源图高度自适应保分辨率；填 1080 则成品恒为 1920x1080
RATIO_W, RATIO_H = 16, 9
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
IDLE_HINT = (
    "等待输入。支持 Ctrl+Shift+C 复制出来的带引号路径；"
    "三张尺寸可以不同，会自动统一。"
)


def log(msg: str) -> None:
    """pythonw 无控制台，异常落盘方便排查。"""
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(msg + "\n")
    except OSError:
        pass


def ps_literal(s: str) -> str:
    """转成 PowerShell 单引号字面量：内部单引号翻倍，路径里的引号和空格都安全。"""
    return "'" + str(s).replace("'", "''") + "'"


def clean_path(s: str) -> str:
    """去掉 Ctrl+Shift+C 复制出来的外层双引号与首尾空白。"""
    s = (s or "").strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("\"", "'"):
        s = s[1:-1]
    return s.strip()


def compute_layout(n: int, src_sizes: list[tuple[int, int]]) -> tuple[int, int, int]:
    """返回 (每张宽, 每张高, 成品宽)。成品高 = 每张高，比例精确 16:9。"""
    if n not in (2, 3):
        raise ValueError("只支持 2 或 3 张")
    unit = 27 if n == 3 else 9  # 让 w = 16H/(9n) 为整数
    if FORCE_HEIGHT:
        h = max(unit, (int(FORCE_HEIGHT) // unit) * unit)
    else:
        min_h = min(size[1] for size in src_sizes)
        h = max(unit, (min_h // unit) * unit)
    if (RATIO_W * h) % (RATIO_H * n) != 0:
        raise ValueError("高度不满足整数宽度条件")
    w = RATIO_W * h // (RATIO_H * n)
    return w, h, n * w


def ps_header() -> str:
    """PowerShell 通用前缀：UTF-8 输出 + 解释器变量。"""
    return (
        "$ErrorActionPreference = 'Stop'\n"
        "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8\n"
        "$OutputEncoding = [System.Text.Encoding]::UTF8\n"
        "$env:PYTHONIOENCODING = 'utf-8'\n"
        f"$py = {ps_literal(PY_IMPL)}\n"
    )


def ps_array(name: str, paths: list[str]) -> str:
    literals = ",\n    ".join(ps_literal(p) for p in paths)
    return f"${name} = @(\n    {literals}\n)\n"


def build_resize_script(paths: list[str], w: int, h: int, outdir: str) -> str:
    """cover = 等比缩放填满 + 居中裁掉溢出（ImageOps.fit centering=(0.5,0.5)）。"""
    return ps_header() + (
        f"$resize = {ps_literal(RESIZE_SCRIPT)}\n"
        + ps_array("imgs", paths)
        + f"& $py $resize @imgs --width {w} --height {h} --mode cover "
        f"--format jpeg --quality {JPEG_QUALITY} --outdir {ps_literal(outdir)}\n"
        "exit $LASTEXITCODE\n"
    )


def build_merge_script(pre_paths: list[str], n: int, output: str) -> str:
    preset = "two_h" if n == 2 else "three_h"
    return ps_header() + (
        f"$merge = {ps_literal(MERGE_SCRIPT)}\n"
        + ps_array("imgs", pre_paths)
        + f"& $py $merge @imgs --preset {preset} --gap-mm 0 "
        f"--output {ps_literal(output)}\n"
        "exit $LASTEXITCODE\n"
    )


def run_ps(script: str, timeout: int = 300):
    """用 -EncodedCommand 调 PowerShell，UTF-16LE 彻底规避中文/引号/编码坑。"""
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    cmd = [
        POWERSHELL, "-NoProfile", "-NonInteractive",
        "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded,
    ]
    return subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout, creationflags=NO_WINDOW,
    )


def parse_json_array(stdout: str) -> list[dict] | None:
    """两个脚本最后一行都输出紧凑 JSON 数组。"""
    for line in stdout.splitlines():
        line = line.strip()
        if line.startswith("[") and line.endswith("]"):
            try:
                data = json.loads(line)
                if isinstance(data, list):
                    return data
            except json.JSONDecodeError:
                continue
    return None


def unique_output(first: str) -> str:
    """成品落在第一张原图同目录，不与已有文件重名。"""
    folder = os.path.dirname(os.path.abspath(first))
    stem = os.path.splitext(os.path.basename(first))[0]
    base = os.path.join(folder, stem + "_16x9")
    out = base + ".jpg"
    i = 2
    while os.path.exists(out):
        out = "%s_%d.jpg" % (base, i)
        i += 1
    return out


def probe_sizes(paths: list[str]) -> list[tuple[int, int]] | None:
    """预读源图尺寸（含 EXIF 摆正），用于自适应选成品高度。"""
    code = (
        "import sys;from PIL import Image,ImageOps;"
        "[print('%d %d' % ImageOps.exif_transpose(Image.open(p)).size) "
        "for p in sys.argv[1:]]"
    )
    try:
        cp = subprocess.run(
            [PY_IMPL, "-c", code] + paths,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=60, creationflags=NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if cp.returncode != 0:
        return None
    sizes: list[tuple[int, int]] = []
    for line in cp.stdout.splitlines():
        parts = line.split()
        if len(parts) == 2:
            sizes.append((int(parts[0]), int(parts[1])))
    return sizes if len(sizes) == len(paths) else None


def _fail(msg: str, cp=None, **extra) -> dict:
    rec = {
        "ok": False, "error": msg,
        "stdout": (cp.stdout or "") if cp else "",
        "stderr": (cp.stderr or "") if cp else "",
        "code": cp.returncode if cp else -1,
    }
    rec.update(extra)
    return rec


def run_pipeline(paths: list[str]) -> dict:
    """预处理(居中裁剪) -> 无缝拼接 -> 校验 16:9，返回结构化结果。"""
    n = len(paths)
    src = probe_sizes(paths)
    if not src:
        return _fail("读不到源图尺寸，确认文件是有效图片且 Pillow 可用")
    try:
        tile_w, tile_h, canvas_w = compute_layout(n, src)
    except ValueError as exc:
        return _fail(str(exc))

    output = unique_output(paths[0])
    info = {
        "n": n, "tile": [tile_w, tile_h], "canvas": [canvas_w, tile_h],
        "src": src, "output": output, "notes": [],
    }

    # 临时目录放项目内，避免往 C 盘写；退出上下文自动清理中间图。
    with tempfile.TemporaryDirectory(prefix="merge16x9_", dir=HERE) as td:
        cp1 = run_ps(build_resize_script(paths, tile_w, tile_h, td))
        recs = parse_json_array(cp1.stdout)
        if cp1.returncode != 0 or not recs:
            return _fail("预处理失败（resize.py 未返回结果）", cp1, **info)
        errs = [r.get("error") for r in recs if r.get("error")]
        if errs:
            return _fail("预处理失败：" + "；".join(map(str, errs)), cp1, **info)
        pre = [r.get("output", "") for r in recs]
        if any(not p or not os.path.isfile(p) for p in pre):
            return _fail("预处理输出文件缺失", cp1, **info)
        for r in recs:
            if r.get("after") != [tile_w, tile_h]:
                return _fail("预处理尺寸不符：%s != %s" % (r.get("after"), [tile_w, tile_h]), cp1, **info)
            for note in r.get("notes", []) or []:
                info["notes"].append(str(note))

        cp2 = run_ps(build_merge_script(pre, n, output))
        merged = parse_json_array(cp2.stdout)
        if cp2.returncode != 0 or not merged:
            return _fail("拼接失败（merge.py 未返回结果）", cp2, **info)

    data = merged[0]
    size = data.get("size", [0, 0])
    info.update({
        "ok": True,
        "size": size,
        "ratio_ok": bool(size[0]) and size[0] * RATIO_H == size[1] * RATIO_W,
        "count": data.get("count"),
        "gap_px": data.get("gap_px"),
        "format": data.get("format"),
        "merge_notes": data.get("notes") or [],
        "stdout": cp2.stdout or "", "stderr": cp2.stderr or "", "code": cp2.returncode,
    })
    return info


class App:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.vars = [tk.StringVar() for _ in range(3)]
        self.entries: list[ttk.Entry] = []
        self.last_output = ""
        self._build()

    def _build(self) -> None:
        r = self.root
        r.title("16:9 横屏拼接 - 2 或 3 张竖图，自动居中裁剪")
        r.geometry("820x410")
        r.minsize(740, 390)

        top = ttk.Frame(r, padding=(14, 12, 14, 4))
        top.pack(fill="x")
        ttk.Label(
            top, text="第三格留空 = 只拼 2 张；成品固定 16:9，预处理居中裁剪"
        ).pack(side="left")
        ttk.Button(top, text="粘贴 2/3 行", command=self.paste_lines, width=13).pack(side="right")

        grid = ttk.Frame(r, padding=(14, 4, 14, 6))
        grid.pack(fill="x")
        grid.columnconfigure(1, weight=1)
        for i, name in enumerate(("左图", "右图", "第三张(可选)")):
            ttk.Label(grid, text=name, width=14).grid(row=i, column=0, sticky="w", pady=4)
            entry = ttk.Entry(grid, textvariable=self.vars[i])
            entry.grid(row=i, column=1, sticky="ew", pady=4)
            self.entries.append(entry)

        actions = ttk.Frame(r, padding=(14, 4, 14, 8))
        actions.pack(fill="x")
        self.btn = ttk.Button(actions, text="生成 16:9 横屏图", command=self.on_generate)
        self.btn.pack(side="left")
        self.open_btn = ttk.Button(
            actions, text="打开成品", command=self.open_result, state="disabled"
        )
        self.open_btn.pack(side="left", padx=(8, 0))
        self.clear_btn = ttk.Button(
            actions, text="清空", command=self.clear_all, width=9
        )
        self.clear_btn.pack(side="left", padx=(8, 0))

        out = ttk.LabelFrame(r, text="结果", padding=(12, 10))
        out.pack(fill="both", expand=True, padx=14, pady=(0, 12))
        self.status = ttk.Label(
            out, text=IDLE_HINT, wraplength=760, justify="left",
        )
        self.status.pack(anchor="w")

    def set_status(self, text: str, error: bool = False) -> None:
        self.status.config(text=text, foreground="#A32D2D" if error else "#2C2C2A")

    def clear_all(self) -> None:
        """清空三个输入框，并把结果区 / 打开成品按钮复位到初始状态。"""
        for var in self.vars:
            var.set("")
        self.last_output = ""
        self.open_btn.config(state="disabled")
        # 生成中的后台任务拿的是路径快照，清空不影响它；此时别覆盖"正在处理"提示。
        if str(self.btn["state"]) != "disabled":
            self.set_status("已清空。" + IDLE_HINT)
        self.entries[0].focus_set()

    def paste_lines(self) -> None:
        """资源管理器多选 2 或 3 张按 Ctrl+Shift+C，一次填入。"""
        try:
            raw = self.root.clipboard_get()
        except tk.TclError:
            messagebox.showinfo("剪贴板", "剪贴板里没有文本内容。")
            return
        lines = [clean_path(x) for x in raw.splitlines() if x.strip()]
        lines = [x for x in lines if x]
        if len(lines) not in (2, 3):
            messagebox.showwarning(
                "需要 2 或 3 行",
                f"剪贴板里识别到 {len(lines)} 行路径，需要正好 2 行或 3 行。",
            )
            return
        for i, var in enumerate(self.vars):
            var.set(lines[i] if i < len(lines) else "")
        self.set_status("已填入 %d 条路径，核对顺序后点「生成 16:9 横屏图」。" % len(lines))

    def collect(self) -> list[str] | None:
        raw = [clean_path(v.get()) for v in self.vars]
        if not raw[0] or not raw[1]:
            messagebox.showerror("缺少路径", "前两格必填；第三格留空即只用 2 张。")
            return None
        paths = raw[:2] if not raw[2] else raw
        for i, path in enumerate(paths, 1):
            if not os.path.isfile(path):
                messagebox.showerror("文件不存在", f"第 {i} 张找不到：\n{path}")
                return None
            if os.path.splitext(path)[1].lower() not in IMAGE_EXTS:
                messagebox.showerror("不是图片", f"第 {i} 个文件不是支持的图片格式：\n{path}")
                return None
        return paths

    def on_generate(self) -> None:
        for label, path in (("预处理脚本", RESIZE_SCRIPT), ("拼接脚本", MERGE_SCRIPT),
                            ("Pillow 解释器", PY_IMPL)):
            if not os.path.isfile(path):
                messagebox.showerror("缺少依赖", f"找不到{label}：\n{path}")
                return
        paths = self.collect()
        if not paths:
            return
        self.btn.config(state="disabled")
        self.open_btn.config(state="disabled")
        self.set_status("正在居中裁剪并拼接，请稍候...")
        threading.Thread(target=self._worker, args=(paths,), daemon=True).start()

    def _worker(self, paths: list[str]) -> None:
        try:
            res = run_pipeline(paths)
        except Exception as exc:  # noqa: BLE001 - 后台线程异常必须兜住
            log(traceback.format_exc())
            res = _fail(str(exc))
        self.root.after(0, lambda: self.show_result(res))

    def show_result(self, res: dict) -> None:
        self.btn.config(state="normal")
        if not res.get("ok"):
            detail = (res.get("stderr") or res.get("stdout") or res.get("error") or "未知错误").strip()
            log("FAILED code=%s\n%s\n%s" % (res.get("code"), res.get("stdout"), res.get("stderr")))
            self.set_status("生成失败：\n" + detail[-1200:], error=True)
            messagebox.showerror("生成失败", detail[-600:] or "未知错误")
            return

        n = res["n"]
        tile_w, tile_h = res["tile"]
        cw, ch = res["size"]
        ratio_txt = "通过" if res["ratio_ok"] else "未通过"
        lines = [
            "生成成功：%d 张 → 16:9 横屏 %d×%d（比例校验 %s：%d×9 = %d×16）"
            % (n, cw, ch, ratio_txt, cw, ch),
            "预处理：居中裁剪 cover，每张 %d×%d；%d 张无缝横拼，间隙 %spx"
            % (tile_w, tile_h, n, res.get("gap_px", 0)),
            "输出：" + res["output"],
        ]
        notes = list(dict.fromkeys(res.get("notes") or []))
        if notes:
            lines.append("处理提示：" + "；".join(notes[:3]))
        if n == 2:
            lines.append("注意：2 张模式裁切较大（9:16 素材上下各约 29%），几何居中不保证主体不被切。")
        if res.get("merge_notes"):
            lines.append("拼接提示：" + "；".join(map(str, res["merge_notes"]))[:200])
        self.set_status("\n".join(lines))
        self.last_output = res["output"]
        if self.last_output and os.path.isfile(self.last_output):
            self.open_btn.config(state="normal")

    def open_result(self) -> None:
        if self.last_output and os.path.isfile(self.last_output):
            os.startfile(self.last_output)
        else:
            messagebox.showinfo("找不到成品", self.last_output or "尚未生成")


def build_window() -> tk.Tk:
    """建窗口（不进入 mainloop），便于无头验证控件构造。"""
    root = tk.Tk()
    root._app = App(root)  # 保持引用，避免回调对象被回收
    return root


def main() -> None:
    try:
        root = build_window()
        root.mainloop()
    except Exception:  # noqa: BLE001
        log(traceback.format_exc())
        raise


if __name__ == "__main__":
    main()
