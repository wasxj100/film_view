import sys
import os
import subprocess
import random
from pathlib import Path
from pathlib import Path
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
                             QTreeWidget, QTreeWidgetItem, QPushButton, QLabel, QLineEdit,
                             QProgressDialog, QMessageBox, QGroupBox, QSizePolicy, QFileDialog)
from PyQt6.QtGui import QPixmap, QImage
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QUrl
from PyQt6.QtGui import QDesktopServices

if getattr(sys, "frozen", False):
    APP_DIR = Path(sys.executable).resolve().parent
else:
    APP_DIR = Path(__file__).resolve().parent

# ===================== 配置区 =====================
FFMPEG_PATH = APP_DIR / "ffmpeg.exe"
FFPROBE_PATH = APP_DIR/ "ffprobe.exe"
VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".wmv", ".flv", ".webm", ".m4v"}
PREVIEW_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".gif"}
# ==================================================

def get_video_duration(video_path: Path) -> float:
    """使用 ffprobe 获取视频总时长(秒)"""
    cmd = [
        FFPROBE_PATH, "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(video_path)
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    return float(result.stdout.strip())

def extract_frame(video_path: Path, out_img: Path, time_sec: float):
    """ffmpeg 截取单个时间点帧"""
    cmd = [
        FFMPEG_PATH, "-y", "-ss", f"{time_sec:.3f}",
        "-i", str(video_path),
        "-vframes", "1",
        "-q:v", "2",
        str(out_img)
    ]
    subprocess.run(cmd, capture_output=True, timeout=30)

def combine_images_vertical(img_list: list[Path], out_path: Path):
    """多张图片垂直拼接，用ffmpeg拼接"""
    inputs = []
    filter_parts = []
    for idx, p in enumerate(img_list):
        inputs.extend(["-i", str(p)])
        filter_parts.append(f"[{idx}:v]scale=width=-2:height=-2[v{idx}]")
    join_expr = "".join([f"[v{i}]" for i in range(len(img_list))]) + f"vstack=inputs={len(img_list)}[out]"
    cmd = [
        FFMPEG_PATH, "-y",
        *inputs,
        "-filter_complex", ";".join(filter_parts + [join_expr]),
        "-map", "[out]",
        "-q:v", "2",
        str(out_path)
    ]
    subprocess.run(cmd, capture_output=True, timeout=60)

def generate_preview_image(video_path: Path, preview_path: Path):
    """生成3帧垂直拼接预览图"""
    duration = get_video_duration(video_path)
    if duration < 1:
        raise ValueError("视频过短")
    times = sorted(random.uniform(0, duration) for _ in range(3))
    tmp_imgs = []
    for t in times:
        tmp = Path(f"{preview_path}.tmp_{t:.2f}.jpg")
        extract_frame(video_path, tmp, t)
        tmp_imgs.append(tmp)
    combine_images_vertical(tmp_imgs, preview_path)
    for f in tmp_imgs:
        try:
            f.unlink()
        except Exception:
            pass

def find_video_preview(video_path: Path) -> Path | None:
    """查找视频对应的预览图（同名，任意图片后缀）"""
    stem = video_path.with_suffix("")
    for ext in PREVIEW_EXTS:
        candidate = stem.with_suffix(ext)
        if candidate.exists():
            return candidate
    return None

def delete_all_preview_of_video(video_path: Path):
    """删除该视频所有同名预览图（所有图片后缀）"""
    stem = video_path.with_suffix("")
    for ext in PREVIEW_EXTS:
        p = stem.with_suffix(ext)
        if p.exists():
            p.unlink()


class PreviewGenWorker(QThread):
    """后台批量生成预览图线程"""
    sig_progress = pyqtSignal(int, str)
    sig_done = pyqtSignal(int, int, int)  # success, skip, fail
    sig_cancel = pyqtSignal()

    def __init__(self, video_list: list[Path], mode: str):
        super().__init__()
        self.video_list = video_list
        self.mode = mode  # "skip_exist" / "force_all"
        self._abort = False

    def run(self):
        success = 0
        skip = 0
        fail = 0
        total = len(self.video_list)
        for idx, vp in enumerate(self.video_list):
            if self._abort:
                self.sig_cancel.emit()
                return
            self.sig_progress.emit(idx, f"处理：{vp.name}")
            preview_file = vp.with_suffix(".jpg")
            if self.mode == "skip_exist":
                if find_video_preview(vp) is not None:
                    skip += 1
                    continue
            else:
                # force 模式：先删掉旧预览
                delete_all_preview_of_video(vp)
            try:
                generate_preview_image(vp, preview_file)
                success += 1
            except Exception as e:
                print(f"FAIL {vp}: {e}")
                fail += 1
        self.sig_done.emit(success, skip, fail)

    def cancel(self):
        self._abort = True


class VideoBrowser(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("PyQt6 电影浏览器 (ffmpeg版)")
        self.resize(1050, 720)
        self.base_folder = ""
        self.file_map: dict[str, QTreeWidgetItem] = dict()  # abs_path -> item
        self.all_video_paths: list[Path] = []
        self.current_selected_video: Path | None = None

        self.init_ui()

    def init_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QHBoxLayout(central)

        # ========== 左侧 ==========
        left_widget = QWidget()
        left_layout = QVBoxLayout(left_widget)

        # 顶部按钮行
        btn_layout = QHBoxLayout()
        self.btn_select_root = QPushButton("选择根目录")
        self.btn_select_root.clicked.connect(self.on_select_folder)
        btn_layout.addWidget(self.btn_select_root)

        self.btn_gen_skip = QPushButton("生成缺失预览图")
        self.btn_gen_skip.clicked.connect(lambda: self.start_batch_gen("skip_exist"))
        self.btn_gen_skip.setEnabled(False)
        btn_layout.addWidget(self.btn_gen_skip)

        self.btn_gen_force = QPushButton("全部重新生成预览图")
        self.btn_gen_force.clicked.connect(lambda: self.start_batch_gen("force_all"))
        self.btn_gen_force.setEnabled(False)
        btn_layout.addWidget(self.btn_gen_force)
        left_layout.addLayout(btn_layout)

        # 搜索框
        search_layout = QHBoxLayout()
        self.edt_search = QLineEdit()
        self.edt_search.setPlaceholderText("搜索文件夹名 / 视频文件名...")
        self.edt_search.textChanged.connect(self.on_search_text)
        search_layout.addWidget(self.edt_search)
        self.btn_clear_search = QPushButton("清空")
        self.btn_clear_search.clicked.connect(lambda: self.edt_search.clear())
        search_layout.addWidget(self.btn_clear_search)
        left_layout.addLayout(search_layout)

        # 树控件
        self.tree = QTreeWidget()
        self.tree.setColumnCount(1)
        self.tree.setHeaderLabel("目录 / 视频")
        self.tree.itemSelectionChanged.connect(self.on_tree_select)
        self.tree.itemDoubleClicked.connect(self.on_double_play)
        left_layout.addWidget(self.tree)

        main_layout.addWidget(left_widget, stretch=1)

        # ========== 右侧 ==========
        right_widget = QWidget()
        right_layout = QVBoxLayout(right_widget)

        self.lbl_preview_title = QLabel("电影预览")
        self.lbl_preview_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        f = self.lbl_preview_title.font()
        f.setBold(True)
        f.setPointSize(14)
        self.lbl_preview_title.setFont(f)
        right_layout.addWidget(self.lbl_preview_title)

        self.lbl_img = QLabel()
        self.lbl_img.setFixedSize(300, 400)
        self.lbl_img.setStyleSheet("background-color:#cccccc;")
        self.lbl_img.setAlignment(Qt.AlignmentFlag.AlignCenter)
        right_layout.addWidget(self.lbl_img, alignment=Qt.AlignmentFlag.AlignCenter)

        info_group = QGroupBox("视频信息")
        info_layout = QVBoxLayout(info_group)
        self.lbl_name = QLabel("文件名：-")
        self.lbl_size = QLabel("大小：-")
        self.lbl_dir = QLabel("目录：-")
        info_layout.addWidget(self.lbl_name)
        info_layout.addWidget(self.lbl_size)
        info_layout.addWidget(self.lbl_dir)
        right_layout.addWidget(info_group)

        # 预览管理按钮组
        manage_layout = QHBoxLayout()
        self.btn_regen_single = QPushButton("重新生成当前预览图")
        self.btn_regen_single.clicked.connect(self.regen_current_preview)
        self.btn_regen_single.setEnabled(False)
        manage_layout.addWidget(self.btn_regen_single)

        self.btn_del_preview = QPushButton("删除当前预览图")
        self.btn_del_preview.clicked.connect(self.del_current_preview)
        self.btn_del_preview.setEnabled(False)
        manage_layout.addWidget(self.btn_del_preview)

        # ===== 新增：删除视频按钮 =====
        self.btn_del_video = QPushButton("删除视频文件")
        self.btn_del_video.clicked.connect(self.delete_video_file)
        self.btn_del_video.setEnabled(False)
        self.btn_del_video.setStyleSheet("color:red;")
        manage_layout.addWidget(self.btn_del_video)

        right_layout.addLayout(manage_layout)

        self.btn_play = QPushButton("▶ 播放选中视频")
        self.btn_play.clicked.connect(self.play_selected)
        self.btn_play.setEnabled(False)
        right_layout.addWidget(self.btn_play)

        right_layout.addStretch()
        main_layout.addWidget(right_widget, stretch=1)

    def on_select_folder(self):
        desktop = str(Path.home() / "Desktop")
        sel = QFileDialog.getExistingDirectory(self,"选择电影根目录" ,desktop)
        if not sel:
            return
        self.base_folder = sel
        self.build_tree()
        self.btn_gen_skip.setEnabled(True)
        self.btn_gen_force.setEnabled(True)

    def build_tree(self, filter_keyword: str = ""):
        self.tree.clear()
        self.file_map.clear()
        self.all_video_paths.clear()
        self.current_selected_video = None
        self.clear_preview_panel()

        base = Path(self.base_folder)
        root_item = QTreeWidgetItem(self.tree, [base.name])
        root_item.setData(0, Qt.ItemDataRole.UserRole, None)
        kw = filter_keyword.lower().strip()

        # 遍历一级子目录
        for subdir in sorted([p for p in base.iterdir() if p.is_dir()]):
            dir_node = QTreeWidgetItem(root_item, [subdir.name])
            dir_node.setData(0, Qt.ItemDataRole.UserRole, None)
            dir_name_match = kw in subdir.name.lower()

            videos_in_dir = []
            for fp in subdir.rglob("*"):
                if fp.is_file() and fp.suffix.lower() in VIDEO_EXTS:
                    videos_in_dir.append(fp)
            videos_in_dir.sort(key=lambda x: x.name)

            has_show = False
            # 遍历视频，判断是否保留
            for vp in videos_in_dir:
                rel = vp.relative_to(subdir)
                display = str(rel).replace("\\", " / ")
                vid_name_match = kw in display.lower()

                if not kw or dir_name_match or vid_name_match:
                    vid_item = QTreeWidgetItem(dir_node, [display])
                    vid_item.setData(0, Qt.ItemDataRole.UserRole, str(vp))
                    self.file_map[str(vp)] = vid_item
                    self.all_video_paths.append(vp)
                    has_show = True

            if has_show:
                dir_node.setExpanded(True)

        root_item.setExpanded(True)

    def on_search_text(self, text: str):
        self.build_tree(filter_keyword=text)

    def on_tree_select(self):
        items = self.tree.selectedItems()
        if not items:
            self.clear_preview_panel()
            return
        item = items[0]
        path_str = item.data(0, Qt.ItemDataRole.UserRole)
        if path_str is None:
            self.clear_preview_panel()
            return
        vp = Path(path_str)
        self.current_selected_video = vp
        self.update_info_and_preview(vp)
        self.btn_play.setEnabled(True)
        self.btn_regen_single.setEnabled(True)
        self.btn_del_preview.setEnabled(True)
        self.btn_del_video.setEnabled(True)

    def update_info_and_preview(self, vp: Path):
        self.lbl_name.setText(f"文件名：{vp.name}")
        size = vp.stat().st_size
        if size < 1024:
            s = f"{size} B"
        elif size < 1024*1024:
            s = f"{size/1024:.1f} KB"
        elif size < 1024*1024*1024:
            s = f"{size/(1024*1024):.1f} MB"
        else:
            s = f"{size/(1024*1024*1024):.1f} GB"
        self.lbl_size.setText(f"大小：{s}")
        self.lbl_dir.setText(f"目录：{vp.parent.name}")

        preview = find_video_preview(vp)
        if preview is not None and preview.exists():
            pix = QPixmap(str(preview))
            pix = pix.scaled(300, 400, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            self.lbl_img.setPixmap(pix)
        else:
            self.lbl_img.clear()

    def clear_preview_panel(self):
        self.lbl_img.clear()
        self.lbl_name.setText("文件名：-")
        self.lbl_size.setText("大小：-")
        self.lbl_dir.setText("目录：-")
        self.btn_play.setEnabled(False)
        self.btn_regen_single.setEnabled(False)
        self.btn_del_preview.setEnabled(False)
        self.btn_del_video.setEnabled(False)
        self.current_selected_video = None

    def on_double_play(self, item, col):
        path_str = item.data(0, Qt.ItemDataRole.UserRole)
        if path_str is not None:
            self.play_video(Path(path_str))

    def play_selected(self):
        if self.current_selected_video:
            self.play_video(self.current_selected_video)

    def play_video(self, vp: Path):
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(vp)))

    def start_batch_gen(self, mode: str):
        if not self.all_video_paths:
            QMessageBox.information(self, "提示", "没有找到视频文件")
            return
        worker = PreviewGenWorker(self.all_video_paths, mode)
        self.progress_dlg = QProgressDialog("正在生成预览图", "取消", 0, len(self.all_video_paths), self)
        self.progress_dlg.setWindowModality(Qt.WindowModality.WindowModal)
        self.progress_dlg.canceled.connect(worker.cancel)
        worker.sig_progress.connect(self.on_progress_update)
        worker.sig_done.connect(self.on_gen_done)
        worker.sig_cancel.connect(self.on_gen_cancel)
        worker.start()
        self.worker = worker
        self.progress_dlg.show()

    def on_progress_update(self, val, msg):
        self.progress_dlg.setLabelText(msg)
        self.progress_dlg.setValue(val)

    def on_gen_done(self, success, skip, fail):
        self.progress_dlg.close()
        QMessageBox.information(self, "完成", f"成功：{success}\n跳过：{skip}\n失败：{fail}")
        self.on_tree_select()

    def on_gen_cancel(self):
        self.progress_dlg.close()
        QMessageBox.information(self, "取消", "任务已取消")

    def regen_current_preview(self):
        if not self.current_selected_video:
            return
        vp = self.current_selected_video
        try:
            delete_all_preview_of_video(vp)
            generate_preview_image(vp, vp.with_suffix(".jpg"))
            QMessageBox.information(self, "成功", "预览图已重新生成")
            self.update_info_and_preview(vp)
        except Exception as e:
            QMessageBox.critical(self, "错误", f"生成失败：{str(e)}")

    def del_current_preview(self):
        if not self.current_selected_video:
            return
        vp = self.current_selected_video
        delete_all_preview_of_video(vp)
        QMessageBox.information(self, "完成", "预览图已删除")
        self.update_info_and_preview(vp)

    # ========== 新增：删除视频文件函数 ==========
    def delete_video_file(self):
        if not self.current_selected_video:
            return
        vp = self.current_selected_video
        ret = QMessageBox.question(
            self,
            "确认删除",
            f"确定要删除【{vp.name}】？\n此操作会同时删除视频文件和预览图，无法恢复！",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if ret != QMessageBox.StandardButton.Yes:
            return
        try:
            # 先删预览图
            delete_all_preview_of_video(vp)
            # 删除视频本体
            vp.unlink()
            QMessageBox.information(self, "已删除", "视频和预览图删除成功")
            # 重新刷新树列表
            self.build_tree(self.edt_search.text())
        except Exception as e:
            QMessageBox.critical(self, "删除失败", f"删除文件出错：{str(e)}")
            print("c")

#==========================================================================
######################无打开文件夹记录功能版###################################
#========================= 新增：删除视频文件按钮   ===========================

if __name__ == "__main__":
    app = QApplication(sys.argv)
    w = VideoBrowser()
    w.show()
    sys.exit(app.exec())
