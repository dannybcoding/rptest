"""
rptest_gui.py  -  PyQt5 GUI launcher for rptest_run.py
Run with:  python3 rptest_gui.py
Requires:  pip install PyQt5 pyserial
"""

import sys
import os
import subprocess
import threading
import time
import re
from datetime import datetime

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QTabWidget,
    QVBoxLayout, QHBoxLayout, QGridLayout, QGroupBox,
    QLabel, QLineEdit, QComboBox, QSpinBox, QCheckBox,
    QPushButton, QTextEdit, QFileDialog, QSplitter,
    QFrame, QProgressBar, QStatusBar, QScrollArea
)
from PyQt5.QtCore import Qt, QThread, pyqtSignal, QTimer, QProcess
from PyQt5.QtGui import QFont, QColor, QPalette, QTextCursor, QIcon

# -----------------------------------------------------------------------
# Dark palette
# -----------------------------------------------------------------------
def apply_dark_theme(app):
    app.setStyle("Fusion")
    palette = QPalette()
    bg       = QColor(30,  30,  35)
    mid      = QColor(45,  45,  52)
    light    = QColor(60,  60,  68)
    accent   = QColor(0,  160, 200)
    text     = QColor(220, 220, 220)
    subtext  = QColor(150, 150, 160)
    danger   = QColor(220,  60,  60)
    ok       = QColor(60,  200, 100)

    palette.setColor(QPalette.Window,          bg)
    palette.setColor(QPalette.WindowText,      text)
    palette.setColor(QPalette.Base,            mid)
    palette.setColor(QPalette.AlternateBase,   bg)
    palette.setColor(QPalette.ToolTipBase,     mid)
    palette.setColor(QPalette.ToolTipText,     text)
    palette.setColor(QPalette.Text,            text)
    palette.setColor(QPalette.Button,          light)
    palette.setColor(QPalette.ButtonText,      text)
    palette.setColor(QPalette.BrightText,      Qt.red)
    palette.setColor(QPalette.Link,            accent)
    palette.setColor(QPalette.Highlight,       accent)
    palette.setColor(QPalette.HighlightedText, bg)
    palette.setColor(QPalette.Disabled, QPalette.Text,       subtext)
    palette.setColor(QPalette.Disabled, QPalette.ButtonText, subtext)
    app.setPalette(palette)
    app.setStyleSheet("""
        QGroupBox {
            border: 1px solid #3a3a44;
            border-radius: 5px;
            margin-top: 8px;
            padding-top: 6px;
            font-weight: bold;
            color: #00c8e0;
        }
        QGroupBox::title {
            subcontrol-origin: margin;
            left: 10px;
            padding: 0 4px;
        }
        QPushButton {
            border: 1px solid #505060;
            border-radius: 4px;
            padding: 5px 14px;
            background-color: #3c3c48;
        }
        QPushButton:hover  { background-color: #4a4a58; }
        QPushButton:pressed { background-color: #2a2a34; }
        QPushButton#btnStart {
            background-color: #1a6630;
            border-color: #2a9940;
            font-weight: bold;
            font-size: 13px;
        }
        QPushButton#btnStart:hover { background-color: #227a3a; }
        QPushButton#btnStop {
            background-color: #6a1a1a;
            border-color: #993030;
            font-weight: bold;
            font-size: 13px;
        }
        QPushButton#btnStop:hover { background-color: #7a2020; }
        QComboBox, QLineEdit, QSpinBox {
            background-color: #3a3a44;
            border: 1px solid #505060;
            border-radius: 3px;
            padding: 2px 6px;
            color: #dcdcdc;
        }
        QTabBar::tab {
            background: #3a3a44;
            color: #aaaaaa;
            padding: 6px 16px;
            border-top-left-radius: 4px;
            border-top-right-radius: 4px;
            border: 1px solid #505060;
            margin-right: 2px;
        }
        QTabBar::tab:selected { background: #1e1e23; color: #00c8e0; }
        QTextEdit {
            background-color: #1a1a20;
            border: 1px solid #383844;
            color: #cccccc;
            font-family: monospace;
        }
        QProgressBar {
            border: 1px solid #505060;
            border-radius: 3px;
            text-align: center;
            background-color: #2a2a34;
        }
        QProgressBar::chunk { background-color: #00a0c8; }
        QScrollArea { border: none; }
    """)


# -----------------------------------------------------------------------
# Worker thread — runs the subprocess
# -----------------------------------------------------------------------
class TestRunner(QThread):
    log_line   = pyqtSignal(str)   # raw output line
    finished   = pyqtSignal(int)   # exit code
    stats_update = pyqtSignal(dict) # parsed stats

    def __init__(self, cmd):
        super().__init__()
        self.cmd     = cmd
        self.process = None
        self._stop   = False

    def run(self):
        self.process = subprocess.Popen(
            self.cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1
        )
        for line in self.process.stdout:
            line = line.rstrip('\n')
            self.log_line.emit(line)
            self._parse_stats(line)
            if self._stop:
                self.process.terminate()
                break
        self.process.wait()
        self.finished.emit(self.process.returncode)

    def stop(self):
        self._stop = True
        if self.process:
            self.process.terminate()

    def _parse_stats(self, line):
        stats = {}
        # Iteration result
        m = re.search(r'Iteration (\d+): (PASS|FAIL)\s+\((\d+) pass / (\d+) fail\)', line)
        if m:
            stats['iteration'] = int(m.group(1))
            stats['result']    = m.group(2)
            stats['pass']      = int(m.group(3))
            stats['fail']      = int(m.group(4))
        # Sent / Received / Dropped
        ms = re.search(r'Sent:\s+(\d+)\s+bytes', line)
        if ms:
            stats['last_sent'] = int(ms.group(1))
        mr = re.search(r'Received:\s+(\d+)\s+bytes', line)
        if mr:
            stats['last_recv'] = int(mr.group(1))
        md = re.search(r'Dropped:\s+(\d+)\s+bytes', line)
        if md:
            stats['last_drop'] = int(md.group(1))
        if stats:
            self.stats_update.emit(stats)


# -----------------------------------------------------------------------
# Main Window
# -----------------------------------------------------------------------
class MainWindow(QMainWindow):
    VALID_BAUDS = [50, 75, 110, 300, 600, 1200, 9600, 14400,
                   19200, 28800, 57600, 115200, 230400]

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Digi RealPort Serial Stress Tester")
        self.setMinimumSize(980, 720)
        self.runner    = None
        self.run_start = None

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(10, 8, 10, 8)
        root.setSpacing(6)

        # Title bar
        title_bar = QLabel("Digi RealPort Serial Stress Tester")
        title_bar.setAlignment(Qt.AlignCenter)
        title_font = QFont()
        title_font.setPointSize(14)
        title_font.setBold(True)
        title_bar.setFont(title_font)
        title_bar.setStyleSheet("color: #00c8e0; padding: 4px 0;")
        root.addWidget(title_bar)

        # Main splitter: config left, output right
        splitter = QSplitter(Qt.Horizontal)
        root.addWidget(splitter, 1)

        # ---- LEFT: config tabs ----
        left_panel = QWidget()
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(0, 0, 0, 0)
        tabs = QTabWidget()
        tabs.addTab(self._build_ports_tab(),   "Ports")
        tabs.addTab(self._build_serial_tab(),  "Serial Settings")
        tabs.addTab(self._build_timing_tab(),  "Timing & Buffer")
        tabs.addTab(self._build_flow_tab(),    "Flow Control")
        left_layout.addWidget(tabs)
        splitter.addWidget(left_panel)

        # ---- RIGHT: log + stats ----
        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(4)

        self._build_stats_bar(right_layout)
        self._build_log_panel(right_layout)
        splitter.addWidget(right_panel)

        splitter.setSizes([420, 560])

        # ---- Bottom: command preview + Start/Stop ----
        self._build_control_bar(root)

        # Status bar
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage("Ready")

        # Refresh timer for elapsed time
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(1000)

        self._refresh_command()

    # -------------------------------------------------------------------
    # Tab builders
    # -------------------------------------------------------------------

    def _build_ports_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setSpacing(10)

        # Port range group
        grp = QGroupBox("Port Designation")
        gl = QGridLayout(grp)

        gl.addWidget(QLabel("Bidirectional (-bxp):"), 0, 0)
        self.bxp_edit = QLineEdit("0-3")
        self.bxp_edit.setPlaceholderText("e.g. 0-3 or 0,1,2,3")
        self.bxp_edit.textChanged.connect(self._refresh_command)
        gl.addWidget(self.bxp_edit, 0, 1)

        gl.addWidget(QLabel("TX-only (-txp):"), 1, 0)
        self.txp_edit = QLineEdit()
        self.txp_edit.setPlaceholderText("e.g. 0-1")
        self.txp_edit.textChanged.connect(self._refresh_command)
        gl.addWidget(self.txp_edit, 1, 1)

        gl.addWidget(QLabel("RX-only (-rxp):"), 2, 0)
        self.rxp_edit = QLineEdit()
        self.rxp_edit.setPlaceholderText("e.g. 2-3")
        self.rxp_edit.textChanged.connect(self._refresh_command)
        gl.addWidget(self.rxp_edit, 2, 1)

        gl.addWidget(QLabel("DUT device ID (-dut):"), 3, 0)
        self.dut_edit = QLineEdit("e8")
        self.dut_edit.setPlaceholderText("e.g. e8")
        self.dut_edit.textChanged.connect(self._refresh_command)
        gl.addWidget(self.dut_edit, 3, 1)

        gl.addWidget(QLabel("AUX device ID (-aux):"), 4, 0)
        self.aux_edit = QLineEdit("TS")
        self.aux_edit.setPlaceholderText("e.g. TS")
        self.aux_edit.textChanged.connect(self._refresh_command)
        gl.addWidget(self.aux_edit, 4, 1)

        note = QLabel("Ports map to: /dev/tty<ID><nn>\n"
                      "  e.g. DUT=e8, port 0 → /dev/ttye800\n"
                      "  e.g. AUX=TS, port 0 → /dev/ttyTS00")
        note.setStyleSheet("color: #888898; font-size: 11px;")
        gl.addWidget(note, 5, 0, 1, 2)
        lay.addWidget(grp)  # ← this line was accidentally commented out on GitHub

        # Iteration group
        grp2 = QGroupBox("Iterations")
        gl2 = QGridLayout(grp2)

        gl2.addWidget(QLabel("Repetitions (-rep):"), 0, 0)
        self.rep_spin = QSpinBox()
        self.rep_spin.setRange(-1, 99999)
        self.rep_spin.setValue(1)
        self.rep_spin.setSpecialValueText("∞ (infinite)")
        self.rep_spin.valueChanged.connect(self._refresh_command)
        gl2.addWidget(self.rep_spin, 0, 1)

        gl2.addWidget(QLabel("Sleep between reps (-slp, ms):"), 1, 0)
        self.slp_spin = QSpinBox()
        self.slp_spin.setRange(0, 60000)
        self.slp_spin.setValue(0)
        self.slp_spin.valueChanged.connect(self._refresh_command)
        gl2.addWidget(self.slp_spin, 1, 1)

        gl2.addWidget(QLabel("Close ports between reps (-clo):"), 2, 0)
        self.clo_combo = QComboBox()
        self.clo_combo.addItems(["Yes (1)", "No (0)"])
        self.clo_combo.currentIndexChanged.connect(self._refresh_command)
        gl2.addWidget(self.clo_combo, 2, 1)

        lay.addWidget(grp2)

        # Verify & verbose
        grp3 = QGroupBox("Output")
        gl3 = QGridLayout(grp3)
        self.ver_check = QCheckBox("Verify received data (-ver)")
        self.ver_check.setChecked(True)
        self.ver_check.stateChanged.connect(self._refresh_command)
        gl3.addWidget(self.ver_check, 0, 0)
        self.dex_check = QCheckBox("Verbose / debug log (-dex)")
        self.dex_check.stateChanged.connect(self._refresh_command)
        gl3.addWidget(self.dex_check, 1, 0)
        lay.addWidget(grp3)

        lay.addStretch()
        return w

    def _build_serial_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)

        grp = QGroupBox("Serial Port Settings")
        gl = QGridLayout(grp)

        gl.addWidget(QLabel("Baud rate (-bps):"), 0, 0)
        self.bps_combo = QComboBox()
        for b in self.VALID_BAUDS:
            self.bps_combo.addItem(str(b))
        self.bps_combo.setCurrentText("115200")
        self.bps_combo.currentIndexChanged.connect(self._refresh_command)
        gl.addWidget(self.bps_combo, 0, 1)

        gl.addWidget(QLabel("Data bits (-dbs):"), 1, 0)
        self.dbs_combo = QComboBox()
        self.dbs_combo.addItems(["8", "7", "6", "5"])
        self.dbs_combo.currentIndexChanged.connect(self._refresh_command)
        gl.addWidget(self.dbs_combo, 1, 1)

        gl.addWidget(QLabel("Parity (-par):"), 2, 0)
        self.par_combo = QComboBox()
        self.par_combo.addItems(["0 – None", "1 – Odd", "2 – Even",
                                  "3 – Mark", "4 – Space"])
        self.par_combo.currentIndexChanged.connect(self._refresh_command)
        gl.addWidget(self.par_combo, 2, 1)

        gl.addWidget(QLabel("Stop bits (-sbs):"), 3, 0)
        self.sbs_combo = QComboBox()
        self.sbs_combo.addItems(["0 – 1 bit", "1 – 1.5 bits", "2 – 2 bits"])
        self.sbs_combo.currentIndexChanged.connect(self._refresh_command)
        gl.addWidget(self.sbs_combo, 3, 1)

        lay.addWidget(grp)

        grp2 = QGroupBox("Test Pattern")
        gl2 = QGridLayout(grp2)

        gl2.addWidget(QLabel("Pattern (-tbp, hex):"), 0, 0)
        self.tbp_edit = QLineEdit()
        self.tbp_edit.setPlaceholderText("e.g. 0xAA  (blank = 0x00-FF cycle)")
        self.tbp_edit.textChanged.connect(self._refresh_command)
        gl2.addWidget(self.tbp_edit, 0, 1)

        gl2.addWidget(QLabel("Pattern from file (-ctx):"), 1, 0)
        ctx_row = QHBoxLayout()
        self.ctx_edit = QLineEdit()
        self.ctx_edit.setPlaceholderText("Optional binary file path")
        self.ctx_edit.textChanged.connect(self._refresh_command)
        ctx_btn = QPushButton("Browse…")
        ctx_btn.clicked.connect(self._browse_ctx)
        ctx_row.addWidget(self.ctx_edit)
        ctx_row.addWidget(ctx_btn)
        gl2.addLayout(ctx_row, 1, 1)

        lay.addWidget(grp2)
        lay.addStretch()
        return w

    def _build_timing_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)

        grp = QGroupBox("Timing")
        gl = QGridLayout(grp)

        gl.addWidget(QLabel("Time to run (-ttr, ms, -1=forever):"), 0, 0)
        self.ttr_spin = QSpinBox()
        self.ttr_spin.setRange(-1, 9999999)
        self.ttr_spin.setValue(60000)
        self.ttr_spin.setSpecialValueText("Forever (-1)")
        self.ttr_spin.valueChanged.connect(self._refresh_command)
        gl.addWidget(self.ttr_spin, 0, 1)

        gl.addWidget(QLabel("Sleep between I/O ops (-btw, ms):"), 1, 0)
        self.btw_spin = QSpinBox()
        self.btw_spin.setRange(0, 60000)
        self.btw_spin.setValue(0)
        self.btw_spin.valueChanged.connect(self._refresh_command)
        gl.addWidget(self.btw_spin, 1, 1)

        gl.addWidget(QLabel("Read timeout constant (-rtc, ms):"), 2, 0)
        self.rtc_spin = QSpinBox()
        self.rtc_spin.setRange(0, 99999)
        self.rtc_spin.setValue(15600)
        self.rtc_spin.valueChanged.connect(self._refresh_command)
        gl.addWidget(self.rtc_spin, 2, 1)

        gl.addWidget(QLabel("Write timeout constant (-wtc, ms):"), 3, 0)
        self.wtc_spin = QSpinBox()
        self.wtc_spin.setRange(0, 99999)
        self.wtc_spin.setValue(15600)
        self.wtc_spin.valueChanged.connect(self._refresh_command)
        gl.addWidget(self.wtc_spin, 3, 1)

        lay.addWidget(grp)

        grp2 = QGroupBox("Buffer")
        gl2 = QGridLayout(grp2)

        gl2.addWidget(QLabel("Buffer/chunk size (-bss, bytes):"), 0, 0)
        self.bss_spin = QSpinBox()
        self.bss_spin.setRange(1, 65536)
        self.bss_spin.setValue(1152)
        self.bss_spin.valueChanged.connect(self._refresh_command)
        gl2.addWidget(self.bss_spin, 0, 1)

        gl2.addWidget(QLabel("Max buffers per port (-nob, -1=inf):"), 1, 0)
        self.nob_spin = QSpinBox()
        self.nob_spin.setRange(-1, 999999)
        self.nob_spin.setValue(-1)
        self.nob_spin.setSpecialValueText("∞ (infinite)")
        self.nob_spin.valueChanged.connect(self._refresh_command)
        gl2.addWidget(self.nob_spin, 1, 1)

        gl2.addWidget(QLabel("Input queue size (-qui, bytes):"), 2, 0)
        self.qui_spin = QSpinBox()
        self.qui_spin.setRange(0, 65536)
        self.qui_spin.setValue(4096)
        self.qui_spin.valueChanged.connect(self._refresh_command)
        gl2.addWidget(self.qui_spin, 2, 1)

        gl2.addWidget(QLabel("Output queue size (-quo, bytes):"), 3, 0)
        self.quo_spin = QSpinBox()
        self.quo_spin.setRange(0, 65536)
        self.quo_spin.setValue(4096)
        self.quo_spin.valueChanged.connect(self._refresh_command)
        gl2.addWidget(self.quo_spin, 3, 1)

        lay.addWidget(grp2)
        lay.addStretch()
        return w

    def _build_flow_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)

        grp = QGroupBox("Flow Control")
        gl = QGridLayout(grp)

        gl.addWidget(QLabel("RTS (-rts):"), 0, 0)
        self.rts_combo = QComboBox()
        self.rts_combo.addItems(["0 – Disable", "1 – Enable",
                                  "2 – Handshake", "3 – Toggle"])
        self.rts_combo.currentIndexChanged.connect(self._refresh_command)
        gl.addWidget(self.rts_combo, 0, 1)

        gl.addWidget(QLabel("CTS (-cts):"), 1, 0)
        self.cts_combo = QComboBox()
        self.cts_combo.addItems(["-1 – Disable",
                                  "0 – hsIfRtsToggle",
                                  "1 – Handshake"])
        self.cts_combo.currentIndexChanged.connect(self._refresh_command)
        gl.addWidget(self.cts_combo, 1, 1)

        gl.addWidget(QLabel("DTR (-dtr):"), 2, 0)
        self.dtr_combo = QComboBox()
        self.dtr_combo.addItems(["0 – Disable", "1 – Enable", "2 – Handshake"])
        self.dtr_combo.currentIndexChanged.connect(self._refresh_command)
        gl.addWidget(self.dtr_combo, 2, 1)

        gl.addWidget(QLabel("Xon/Xoff (-xon):"), 3, 0)
        self.xon_combo = QComboBox()
        self.xon_combo.addItems(["0 – Disable", "1 – Enable"])
        self.xon_combo.currentIndexChanged.connect(self._refresh_command)
        gl.addWidget(self.xon_combo, 3, 1)

        hint = QLabel("Hardware flow: set RTS=Handshake + CTS=Handshake")
        hint.setStyleSheet("color: #888898; font-size: 11px;")
        gl.addWidget(hint, 4, 0, 1, 2)

        lay.addWidget(grp)

        grp2 = QGroupBox("On-Error Behavior")
        gl2 = QGridLayout(grp2)
        self.res_check = QCheckBox("Retry send on timeout (-res)")
        self.res_check.stateChanged.connect(self._refresh_command)
        gl2.addWidget(self.res_check, 0, 0)
        self.flb_check = QCheckBox("Flush/purge port when done (-flb)")
        self.flb_check.stateChanged.connect(self._refresh_command)
        gl2.addWidget(self.flb_check, 1, 0)
        lay.addWidget(grp2)

        lay.addStretch()
        return w

    # -------------------------------------------------------------------
    # Stats bar
    # -------------------------------------------------------------------
    def _build_stats_bar(self, parent_layout):
        grp = QGroupBox("Live Stats")
        gl = QGridLayout(grp)
        gl.setSpacing(6)

        lbl_style = "color: #aaaaaa; font-size: 11px;"
        val_style = "color: #e0e0e0; font-weight: bold; font-size: 13px;"

        def make_pair(label, col):
            l = QLabel(label)
            l.setStyleSheet(lbl_style)
            v = QLabel("—")
            v.setStyleSheet(val_style)
            gl.addWidget(l, 0, col)
            gl.addWidget(v, 1, col)
            return v

        self.stat_iter   = make_pair("Iteration",  0)
        self.stat_pass   = make_pair("Pass",        1)
        self.stat_fail   = make_pair("Fail",        2)
        self.stat_sent   = make_pair("Last Sent",   3)
        self.stat_recv   = make_pair("Last Recv",   4)
        self.stat_drop   = make_pair("Last Drop",   5)
        self.stat_time   = make_pair("Elapsed",     6)

        parent_layout.addWidget(grp)

    # -------------------------------------------------------------------
    # Log panel
    # -------------------------------------------------------------------
    def _build_log_panel(self, parent_layout):
        grp = QGroupBox("Test Output")
        lay = QVBoxLayout(grp)
        lay.setContentsMargins(4, 8, 4, 4)

        btn_row = QHBoxLayout()
        clear_btn = QPushButton("Clear")
        clear_btn.clicked.connect(lambda: self.log_edit.clear())
        save_btn  = QPushButton("Save log…")
        save_btn.clicked.connect(self._save_log)
        btn_row.addStretch()
        btn_row.addWidget(clear_btn)
        btn_row.addWidget(save_btn)
        lay.addLayout(btn_row)

        self.log_edit = QTextEdit()
        self.log_edit.setReadOnly(True)
        mono = QFont("Monospace", 9)
        mono.setStyleHint(QFont.Monospace)
        self.log_edit.setFont(mono)
        self.log_edit.setLineWrapMode(QTextEdit.NoWrap)
        lay.addWidget(self.log_edit)

        parent_layout.addWidget(grp, 1)

    # -------------------------------------------------------------------
    # Control bar
    # -------------------------------------------------------------------
    def _build_control_bar(self, parent_layout):
        frame = QFrame()
        frame.setFrameShape(QFrame.StyledPanel)
        lay = QVBoxLayout(frame)
        lay.setContentsMargins(6, 4, 6, 4)
        lay.setSpacing(4)

        # Command preview
        cmd_row = QHBoxLayout()
        cmd_row.addWidget(QLabel("Command:"))
        self.cmd_preview = QLineEdit()
        self.cmd_preview.setReadOnly(True)
        self.cmd_preview.setStyleSheet("color: #88ff88; background: #1a1a20;")
        cmd_row.addWidget(self.cmd_preview, 1)
        lay.addLayout(cmd_row)

        # Progress bar + buttons
        btn_row = QHBoxLayout()
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)  # indeterminate
        self.progress.setVisible(False)
        self.progress.setFixedHeight(16)
        btn_row.addWidget(self.progress, 1)

        self.start_btn = QPushButton("▶  Start Test")
        self.start_btn.setObjectName("btnStart")
        self.start_btn.setFixedWidth(160)
        self.start_btn.clicked.connect(self._start_test)
        btn_row.addWidget(self.start_btn)

        self.stop_btn = QPushButton("■  Stop")
        self.stop_btn.setObjectName("btnStop")
        self.stop_btn.setFixedWidth(100)
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self._stop_test)
        btn_row.addWidget(self.stop_btn)

        lay.addLayout(btn_row)
        parent_layout.addWidget(frame)

    # -------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------
    def _browse_ctx(self):
        path, _ = QFileDialog.getOpenFileName(self, "Select pattern file")
        if path:
            self.ctx_edit.setText(path)

    def _save_log(self):
        path, _ = QFileDialog.getSaveFileName(self, "Save log", "serial_test.log",
                                               "Log files (*.log);;All (*)")
        if path:
            with open(path, 'w') as f:
                f.write(self.log_edit.toPlainText())

    def _cts_value(self):
        idx = self.cts_combo.currentIndex()
        return {0: -1, 1: 0, 2: 1}[idx]

    def _build_cmd(self):
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "rptest_run.py")
        cmd = [sys.executable, script]

        def add(flag, val):
            cmd.extend([flag, str(val)])

        if self.bxp_edit.text().strip():
            add('-bxp', self.bxp_edit.text().strip())
        if self.txp_edit.text().strip():
            add('-txp', self.txp_edit.text().strip())
        if self.rxp_edit.text().strip():
            add('-rxp', self.rxp_edit.text().strip())

        add('-dut', self.dut_edit.text().strip() or 'e8')
        add('-aux', self.aux_edit.text().strip() or 'TS')

        add('-bps', int(self.bps_combo.currentText()))
        add('-dbs', int(self.dbs_combo.currentText()))
        add('-par', self.par_combo.currentIndex())
        add('-sbs', self.sbs_combo.currentIndex())
        add('-rts', self.rts_combo.currentIndex())
        add('-cts', self._cts_value())
        add('-dtr', self.dtr_combo.currentIndex())
        add('-xon', self.xon_combo.currentIndex())

        add('-ttr', self.ttr_spin.value())
        add('-btw', self.btw_spin.value())
        add('-rtc', self.rtc_spin.value())
        add('-wtc', self.wtc_spin.value())
        add('-bss', self.bss_spin.value())
        add('-nob', self.nob_spin.value())
        add('-qui', self.qui_spin.value())
        add('-quo', self.quo_spin.value())

        add('-rep', self.rep_spin.value())
        add('-slp', self.slp_spin.value())
        add('-clo', 0 if self.clo_combo.currentIndex() == 1 else 1)

        add('-ver', 1 if self.ver_check.isChecked() else 0)
        add('-dex', 1 if self.dex_check.isChecked() else 0)
        add('-res', 1 if self.res_check.isChecked() else 0)
        add('-flb', 1 if self.flb_check.isChecked() else 0)

        if self.tbp_edit.text().strip():
            add('-tbp', self.tbp_edit.text().strip())
        if self.ctx_edit.text().strip():
            add('-ctx', self.ctx_edit.text().strip())

        return cmd

    def _refresh_command(self):
        try:
            cmd = self._build_cmd()
            self.cmd_preview.setText(' '.join(cmd))
        except Exception:
            pass

    def _colorize(self, line):
        """Return HTML-colored version of a log line."""
        esc = line.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        if 'FAIL' in line or 'ERROR' in line or 'error' in line or 'Dropped' in line:
            return f'<span style="color:#ff6060">{esc}</span>'
        elif 'PASS' in line:
            return f'<span style="color:#60e080">{esc}</span>'
        elif 'VERIFICATION' in line or 'RESULTS' in line or '===' in line:
            return f'<span style="color:#00c8e0">{esc}</span>'
        elif 'WARNING' in line or 'warning' in line:
            return f'<span style="color:#f0b030">{esc}</span>'
        else:
            return f'<span style="color:#cccccc">{esc}</span>'

    def _append_log(self, line):
        self.log_edit.insertHtml(self._colorize(line) + '<br>')
        self.log_edit.moveCursor(QTextCursor.End)

    def _update_stats(self, stats):
        if 'iteration' in stats:
            self.stat_iter.setText(str(stats['iteration']))
        if 'pass' in stats:
            self.stat_pass.setText(str(stats['pass']))
            self.stat_pass.setStyleSheet("color: #60e080; font-weight: bold; font-size: 13px;")
        if 'fail' in stats:
            v = stats['fail']
            self.stat_fail.setText(str(v))
            color = '#ff6060' if v > 0 else '#e0e0e0'
            self.stat_fail.setStyleSheet(f"color: {color}; font-weight: bold; font-size: 13px;")
        if 'last_sent' in stats:
            self.stat_sent.setText(f"{stats['last_sent']:,} B")
        if 'last_recv' in stats:
            self.stat_recv.setText(f"{stats['last_recv']:,} B")
        if 'last_drop' in stats:
            v = stats['last_drop']
            self.stat_drop.setText(f"{v:,} B")
            color = '#ff6060' if v > 0 else '#60e080'
            self.stat_drop.setStyleSheet(f"color: {color}; font-weight: bold; font-size: 13px;")

    def _tick(self):
        if self.run_start is not None:
            elapsed = int(time.time() - self.run_start)
            h, r = divmod(elapsed, 3600)
            m, s = divmod(r, 60)
            self.stat_time.setText(f"{h:02d}:{m:02d}:{s:02d}")

    # -------------------------------------------------------------------
    # Start / Stop
    # -------------------------------------------------------------------
    def _start_test(self):
        if self.runner and self.runner.isRunning():
            return

        cmd = self._build_cmd()
        self.log_edit.clear()
        self._append_log(f"Starting: {' '.join(cmd)}")
        self._append_log("-" * 60)

        self.runner = TestRunner(cmd)
        self.runner.log_line.connect(self._append_log)
        self.runner.stats_update.connect(self._update_stats)
        self.runner.finished.connect(self._on_finished)
        self.runner.start()

        self.run_start = time.time()
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.progress.setVisible(True)
        self.status_bar.showMessage("Test running…")

        # Reset stats
        for w in [self.stat_iter, self.stat_pass, self.stat_fail,
                  self.stat_sent, self.stat_recv, self.stat_drop]:
            w.setText("—")
            w.setStyleSheet("color: #e0e0e0; font-weight: bold; font-size: 13px;")
        self.stat_time.setText("00:00:00")

    def _stop_test(self):
        if self.runner:
            self.runner.stop()
        self.stop_btn.setEnabled(False)
        self.status_bar.showMessage("Stopping…")

    def _on_finished(self, exit_code):
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.progress.setVisible(False)
        self.run_start = None

        if exit_code == 0:
            self.status_bar.showMessage("Test completed: PASS", 10000)
            self._append_log("=" * 60)
            self._append_log("TEST COMPLETED: PASS")
        else:
            self.status_bar.showMessage(f"Test completed: FAIL (exit {exit_code})", 10000)
            self._append_log("=" * 60)
            self._append_log(f"TEST COMPLETED: FAIL (exit code {exit_code})")


# -----------------------------------------------------------------------
# Entry point
# -----------------------------------------------------------------------
def main():
    app = QApplication(sys.argv)
    apply_dark_theme(app)
    win = MainWindow()
    win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
