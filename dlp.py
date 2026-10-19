"""
YouTube 片段下载器
调用本机已安装的 yt-dlp 与 ffmpeg，输出 H.264 + AAC 的 MP4，适合导入剪映 / CapCut。

界面使用 PySide6（不依赖系统 tkinter）。首次运行前请安装：
    pip install PySide6
"""

from __future__ import annotations

import json
import queue
import re
import shutil
import subprocess
import sys
import threading
from pathlib import Path

try:
    from PySide6.QtCore import QTimer, Qt
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtWidgets import (
        QApplication,
        QCheckBox,
        QFileDialog,
        QGroupBox,
        QHBoxLayout,
        QLabel,
        QLineEdit,
        QListWidget,
        QMainWindow,
        QMessageBox,
        QProgressBar,
        QPushButton,
        QPlainTextEdit,
        QVBoxLayout,
        QWidget,
    )
except ImportError:
    print("缺少 PySide6。请先运行：python -m pip install PySide6")
    input("按回车键退出…")
    sys.exit(1)


APP_DIR = Path(sys.argv[0]).resolve().parent
messages: queue.Queue = queue.Queue()
busy = False

DOWNLOAD_PCT_RE = re.compile(r"\[download\]\s+(\d+(?:\.\d+)?)%")
SRT_TIME_RE = re.compile(
    r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*(\d{1,2}):(\d{2}):(\d{2})[,.](\d{3})"
)
SRT_BLOCK_RE = re.compile(
    r"(\d+)\s*\n"
    r"(\d{1,2}:\d{2}:\d{2}[,.]\d{3}\s*-->\s*\d{1,2}:\d{2}:\d{2}[,.]\d{3})\s*\n"
    r"([\s\S]*?)(?=\n\n|\Z)",
)


def parse_time(value: str) -> int:
    """把 80 / 1:20 / 0:01:20 转为秒数。"""
    value = value.strip()
    if not re.fullmatch(r"\d+(?::\d{1,2}){0,2}", value):
        raise ValueError(
            f"时间格式不正确：{value}\n"
            "请使用秒数、分:秒 或 时:分:秒，例如 80、1:20、0:01:20。"
        )
    parts = [int(p) for p in value.split(":")]
    if len(parts) == 1:
        return parts[0]
    if len(parts) == 2:
        if parts[1] >= 60:
            raise ValueError(f"时间格式不正确：{value}")
        return parts[0] * 60 + parts[1]
    if parts[1] >= 60 or parts[2] >= 60:
        raise ValueError(f"时间格式不正确：{value}")
    return parts[0] * 3600 + parts[1] * 60 + parts[2]


def tool_path(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    for candidate in (APP_DIR / f"{name}.exe", APP_DIR / "bin" / f"{name}.exe"):
        if candidate.exists():
            return str(candidate)
    return None


def log(text: str) -> None:
    messages.put(("log", text))


def set_progress(percent: float, label: str = "") -> None:
    messages.put(("progress", (max(0.0, min(100.0, percent)), label)))


def hms_to_ms(h: int, m: int, s: int, ms: int) -> int:
    return ((h * 60 + m) * 60 + s) * 1000 + ms


def ms_to_srt(ms: int) -> str:
    ms = max(0, ms)
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, milli = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{milli:03d}"


def trim_srt_to_section(src: Path, dst: Path, start_sec: float, end_sec: float) -> int:
    """
    把整段视频字幕裁切到 [start, end]，并把时间轴归零到片段起点。
    返回保留的字幕条数。
    """
    text = src.read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
    text = text.strip() + "\n\n"
    start_ms = int(start_sec * 1000)
    end_ms = int(end_sec * 1000)
    kept: list[str] = []
    index = 1

    for match in SRT_BLOCK_RE.finditer(text):
        timing = match.group(2)
        body = match.group(3).strip()
        tm = SRT_TIME_RE.fullmatch(timing.strip())
        if not tm or not body:
            continue
        cue_start = hms_to_ms(
            int(tm.group(1)), int(tm.group(2)), int(tm.group(3)), int(tm.group(4))
        )
        cue_end = hms_to_ms(
            int(tm.group(5)), int(tm.group(6)), int(tm.group(7)), int(tm.group(8))
        )
        # 与片段有交集才保留
        if cue_end <= start_ms or cue_start >= end_ms:
            continue
        new_start = max(cue_start, start_ms) - start_ms
        new_end = min(cue_end, end_ms) - start_ms
        if new_end <= new_start:
            continue
        kept.append(
            f"{index}\n{ms_to_srt(new_start)} --> {ms_to_srt(new_end)}\n{body}\n"
        )
        index += 1

    dst.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
    return len(kept)


def resolve_ffprobe(ffmpeg: str) -> str | None:
    ffmpeg_path = Path(ffmpeg)
    for name in ("ffprobe.exe", "ffprobe"):
        candidate = ffmpeg_path.with_name(name)
        if candidate.exists():
            return str(candidate)
    return shutil.which("ffprobe")


def describe_media(path: Path, ffmpeg: str) -> str:
    """用 ffprobe 读取成品分辨率 / 编码，便于在日志里展示画质。"""
    ffprobe = resolve_ffprobe(ffmpeg)
    if not ffprobe:
        return "（无法检测画质：找不到 ffprobe）"

    cmd = [
        ffprobe,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height,codec_name,avg_frame_rate:format=bit_rate",
        "-of",
        "json",
        str(path),
    ]
    try:
        raw = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace"
        )
        if raw.returncode != 0:
            return f"（画质检测失败：{(raw.stderr or raw.stdout).strip()[-200:]}）"
        data = json.loads(raw.stdout or "{}")
        streams = data.get("streams") or []
        if not streams:
            return "（未检测到视频流）"
        s = streams[0]
        w, h = s.get("width"), s.get("height")
        codec = s.get("codec_name") or "?"
        fps = s.get("avg_frame_rate") or "?"
        if isinstance(fps, str) and "/" in fps:
            num, den = fps.split("/", 1)
            try:
                fps = f"{int(num) / int(den):.2f}".rstrip("0").rstrip(".")
            except (ValueError, ZeroDivisionError):
                pass
        br = (data.get("format") or {}).get("bit_rate")
        br_text = ""
        if br:
            try:
                br_text = f"，约 {int(br) / 1_000_000:.1f} Mbps"
            except ValueError:
                pass
        if h and int(h) >= 1400:
            label = "2K"
        elif h and int(h) >= 1000:
            label = "1080p"
        elif h and int(h) >= 700:
            label = "720p"
        elif h:
            label = f"{h}p"
        else:
            label = "?"
        return f"{w}×{h}（{label}），编码 {codec}，{fps} fps{br_text}"
    except Exception as e:
        return f"（画质检测异常：{e}）"


def probe_selected_format(ytdlp: str, url: str) -> str:
    """下载前查询 yt-dlp 将选用的源画质（上限 1440p）。"""
    cmd = [
        ytdlp,
        "--no-warnings",
        "-f",
        "bv*[height<=1440]+ba/b[height<=1440]",
        "-S",
        "res:1440,br,fps",
        "--print",
        "%(width)sx%(height)s | id=%(format_id)s | vcodec=%(vcodec)s | acodec=%(acodec)s | fps=%(fps)s",
        "--skip-download",
        url,
    ]
    raw = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    if raw.returncode != 0:
        detail = (raw.stderr or raw.stdout or "").strip()
        return f"无法预检源画质：{detail[-300:]}"
    line = (raw.stdout or "").strip().splitlines()
    return line[-1] if line else "未知"


def run_logged(cmd: list[str], progress_base: float = 0.0, progress_span: float = 100.0) -> int:
    """运行命令：写日志，并解析 yt-dlp 下载百分比。"""
    log("命令：" + " ".join(cmd))
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip()
        if not line:
            continue
        m = DOWNLOAD_PCT_RE.search(line)
        if m:
            local = float(m.group(1))
            # 下载约占本片段进度的前 85%，转码阶段再走到 100%
            overall = progress_base + (local / 100.0) * progress_span * 0.85
            set_progress(overall, f"下载 {local:.1f}%")
            # 进度行很密，不全量刷进日志框，只偶尔记一条
            if local >= 99.5 or int(local) % 10 == 0:
                log(line)
        else:
            log(line)
            low = line.lower()
            if "destination" in low or "merging" in low:
                set_progress(progress_base + progress_span * 0.88, "合并中…")
            elif "recode" in low or "convert" in low or "ffmpeg" in low:
                set_progress(progress_base + progress_span * 0.95, "转码 H.264…")
    return proc.wait()


def download_job(
    url: str,
    ranges: list[tuple[str, str, int, int]],
    out_dir: Path,
    want_subs: bool,
) -> None:
    try:
        ytdlp = tool_path("yt-dlp")
        ffmpeg = tool_path("ffmpeg")
        if not ytdlp:
            raise RuntimeError(
                "找不到 yt-dlp。请先安装 yt-dlp，或把 yt-dlp.exe 放到本程序同一文件夹。"
            )
        if not ffmpeg:
            raise RuntimeError(
                "找不到 ffmpeg。请先安装 ffmpeg，并确保 ffmpeg.exe 在 PATH 中，"
                "或放到本程序同一文件夹。"
            )

        out_dir.mkdir(parents=True, exist_ok=True)
        ffmpeg_dir = str(Path(ffmpeg).resolve().parent)
        total = len(ranges)

        log("画质策略：最高 2K（1440p），再转码为 H.264 + AAC（剪映 / CapCut 兼容）。")
        if want_subs:
            log("字幕：开启（下载后会按片段起始时间裁切并对齐到 00:00）。")
        else:
            log("字幕：关闭。")

        log("正在预检可用源画质…")
        source_quality = probe_selected_format(ytdlp, url)
        log(f"将下载的源画质：{source_quality}")

        for idx, (start_text, end_text, start, end) in enumerate(ranges, 1):
            safe_start = start_text.replace(":", "-")
            safe_end = end_text.replace(":", "-")
            stem = f"clip_{idx:02d}_{safe_start}_{safe_end}"
            output_template = str(out_dir / f"{stem}.%(ext)s")
            final_mp4 = out_dir / f"{stem}.mp4"
            progress_base = (idx - 1) / total * 100.0
            progress_span = 100.0 / total

            set_progress(progress_base, f"片段 {idx}/{total}")
            section = f"*{start}-{end}"
            log(f"\n[{idx}/{total}] 正在下载并转码 {start_text}–{end_text} …")

            cmd = [
                ytdlp,
                "--no-warnings",
                "--newline",
                "--progress",
                "--ffmpeg-location",
                ffmpeg_dir,
                "--download-sections",
                section,
                "--force-keyframes-at-cuts",
                # 上限 2K：height<=1440，优先接近 1440p（不回退到 4K）
                "-f",
                "bv*[height<=1440]+ba/b[height<=1440]",
                "-S",
                "res:1440,br,fps",
                "--merge-output-format",
                "mp4",
                "--recode-video",
                "mp4",
                "--postprocessor-args",
                "VideoConvertor:-c:v libx264 -crf 18 -preset medium -c:a aac -b:a 192k "
                "-movflags +faststart",
                "-o",
                output_template,
            ]
            if want_subs:
                cmd += [
                    "--write-subs",
                    "--write-auto-subs",
                    "--sub-langs",
                    "all,-live_chat",
                    "--convert-subs",
                    "srt",
                ]
            cmd.append(url)

            code = run_logged(cmd, progress_base=progress_base, progress_span=progress_span)
            if code != 0:
                raise RuntimeError(f"yt-dlp 处理失败，退出代码：{code}")

            if not final_mp4.exists():
                matches = sorted(out_dir.glob(f"{stem}.*"))
                media = [p for p in matches if p.suffix.lower() in {".mp4", ".mkv", ".webm"}]
                if not media:
                    raise RuntimeError(f"未找到输出文件：{final_mp4}")
                out_file = media[0]
            else:
                out_file = final_mp4

            quality = describe_media(out_file, ffmpeg)
            log(f"完成视频：{out_file}")
            log(f"成品画质：{quality}")

            if want_subs:
                # yt-dlp 写出的字幕仍是整片时间轴，按片段裁切并归零
                raw_srts = sorted(out_dir.glob(f"{stem}*.srt"))
                if not raw_srts:
                    log("未找到字幕文件（该视频可能没有可用字幕）。")
                else:
                    for srt in raw_srts:
                        # 例：clip_01_5-38_6-58.en.srt → 原地覆盖为裁切版
                        count = trim_srt_to_section(srt, srt, start, end)
                        log(f"字幕已裁切：{srt.name}（保留 {count} 条）")

            set_progress(progress_base + progress_span, f"片段 {idx}/{total} 完成")

        set_progress(100.0, "全部完成")
        log("\n全部片段下载完成。")
    except Exception as e:
        log(f"\n错误：{e}")
    finally:
        messages.put(("done", None))


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("YouTube 片段下载器")
        self.resize(900, 780)
        self.setMinimumSize(720, 640)

        self.segments: list[tuple[str, str]] = []
        self._build_ui()

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._poll_messages)
        self._timer.start(150)

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(8)

        # 目的文件夹
        layout.addWidget(QLabel("目的文件夹"))
        folder_row = QHBoxLayout()
        self.out_dir_edit = QLineEdit(str(APP_DIR))
        folder_row.addWidget(self.out_dir_edit, stretch=1)
        browse_btn = QPushButton("浏览…")
        browse_btn.clicked.connect(self._choose_folder)
        folder_row.addWidget(browse_btn)
        layout.addLayout(folder_row)

        # 视频链接
        layout.addWidget(QLabel("视频链接"))
        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText("https://www.youtube.com/watch?v=...")
        layout.addWidget(self.url_edit)

        # 添加片段
        time_box = QGroupBox("添加片段")
        time_layout = QVBoxLayout(time_box)
        time_row = QHBoxLayout()
        time_row.addWidget(QLabel("起始时间"))
        self.start_edit = QLineEdit("0:00")
        self.start_edit.setMaximumWidth(100)
        time_row.addWidget(self.start_edit)
        time_row.addWidget(QLabel("结束时间"))
        self.end_edit = QLineEdit("1:20")
        self.end_edit.setMaximumWidth(100)
        time_row.addWidget(self.end_edit)
        add_btn = QPushButton("添加片段")
        add_btn.clicked.connect(self._add_segment)
        time_row.addWidget(add_btn)
        time_row.addStretch(1)
        time_layout.addLayout(time_row)
        layout.addWidget(time_box)

        # 片段列表
        layout.addWidget(QLabel("待下载片段"))
        list_row = QHBoxLayout()
        self.seg_list = QListWidget()
        self.seg_list.setMaximumHeight(90)
        list_row.addWidget(self.seg_list, stretch=1)
        list_btns = QVBoxLayout()
        remove_btn = QPushButton("删除选中")
        remove_btn.clicked.connect(self._remove_selected)
        clear_btn = QPushButton("清空全部")
        clear_btn.clicked.connect(self._clear_segments)
        list_btns.addWidget(remove_btn)
        list_btns.addWidget(clear_btn)
        list_btns.addStretch(1)
        list_row.addLayout(list_btns)
        layout.addLayout(list_row)

        # 字幕选项
        self.subs_check = QCheckBox("下载字幕（按片段起始时间裁切，时间轴从 00:00 开始）")
        self.subs_check.setChecked(False)
        layout.addWidget(self.subs_check)

        # 开始下载
        self.start_btn = QPushButton("开始下载")
        self.start_btn.setMinimumHeight(32)
        self.start_btn.clicked.connect(self._start_download)
        layout.addWidget(self.start_btn)

        # 百分比进度条（在日志上方）
        progress_row = QHBoxLayout()
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 1000)  # 0.1% 精度
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("%p%")
        self.progress_bar.setAlignment(Qt.AlignmentFlag.AlignCenter)
        progress_row.addWidget(self.progress_bar, stretch=1)
        self.progress_label = QLabel("待命")
        self.progress_label.setMinimumWidth(120)
        progress_row.addWidget(self.progress_label)
        layout.addLayout(progress_row)

        # 日志
        log_header = QHBoxLayout()
        log_header.addWidget(QLabel("进度 / 错误信息"))
        log_header.addStretch(1)
        self.copy_btn = QPushButton("复制全部")
        self.copy_btn.clicked.connect(self._copy_log)
        log_header.addWidget(self.copy_btn)
        layout.addLayout(log_header)

        self.log_box = QPlainTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setMinimumHeight(300)
        layout.addWidget(self.log_box, stretch=3)

    def _choose_folder(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self, "选择目的文件夹", self.out_dir_edit.text() or str(APP_DIR)
        )
        if path:
            self.out_dir_edit.setText(path)

    def _add_segment(self) -> None:
        start_text = self.start_edit.text().strip()
        end_text = self.end_edit.text().strip()
        try:
            start = parse_time(start_text)
            end = parse_time(end_text)
            if end <= start:
                raise ValueError("结束时间必须晚于起始时间。")
        except ValueError as e:
            QMessageBox.critical(self, "时间格式有误", str(e))
            return
        self.segments.append((start_text, end_text))
        self.seg_list.addItem(f"{start_text}  →  {end_text}")
        self.start_edit.setText(end_text)

    def _remove_selected(self) -> None:
        row = self.seg_list.currentRow()
        if row < 0:
            return
        self.seg_list.takeItem(row)
        del self.segments[row]

    def _clear_segments(self) -> None:
        self.seg_list.clear()
        self.segments.clear()

    def _append_log(self, text: str) -> None:
        self.log_box.appendPlainText(text)

    def _set_progress_ui(self, percent: float, label: str) -> None:
        self.progress_bar.setValue(int(round(percent * 10)))
        self.progress_label.setText(label or f"{percent:.1f}%")

    def _copy_log(self) -> None:
        text = self.log_box.toPlainText()
        if not text.strip():
            QMessageBox.information(self, "复制", "日志为空，没有可复制的内容。")
            return
        QGuiApplication.clipboard().setText(text)
        self.copy_btn.setText("已复制")
        QTimer.singleShot(1500, lambda: self.copy_btn.setText("复制全部"))

    def _start_download(self) -> None:
        global busy
        if busy:
            return

        url = self.url_edit.text().strip()
        if not url or ("youtube.com" not in url and "youtu.be" not in url):
            QMessageBox.critical(self, "输入有误", "请填写有效的 YouTube 视频链接。")
            return

        out_dir = Path(self.out_dir_edit.text().strip() or str(APP_DIR))
        if not self.segments:
            QMessageBox.critical(self, "输入有误", "请至少添加一个时间片段。")
            return

        ranges: list[tuple[str, str, int, int]] = []
        try:
            for start_text, end_text in self.segments:
                start = parse_time(start_text)
                end = parse_time(end_text)
                if end <= start:
                    raise ValueError(f"结束时间必须晚于起始时间：{start_text}-{end_text}")
                ranges.append((start_text, end_text, start, end))
        except ValueError as e:
            QMessageBox.critical(self, "时间格式有误", str(e))
            return

        busy = True
        self.start_btn.setEnabled(False)
        self.log_box.clear()
        self._set_progress_ui(0.0, "开始…")
        self._append_log(f"保存到：{out_dir}")
        self._append_log(f"共 {len(ranges)} 个片段，开始下载…")

        threading.Thread(
            target=download_job,
            args=(url, ranges, out_dir, self.subs_check.isChecked()),
            daemon=True,
        ).start()

    def _poll_messages(self) -> None:
        global busy
        try:
            while True:
                kind, value = messages.get_nowait()
                if kind == "log":
                    self._append_log(value)
                elif kind == "progress":
                    percent, label = value
                    self._set_progress_ui(percent, label)
                elif kind == "done":
                    busy = False
                    self.start_btn.setEnabled(True)
        except queue.Empty:
            pass


def main() -> None:
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
