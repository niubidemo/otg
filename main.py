import sys
import os
import time
import subprocess
import platform
import threading
import re
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
                             QLabel, QPushButton, QProgressBar, QMessageBox, QSpinBox, QGroupBox, QFormLayout)
from PyQt6.QtCore import Qt, QTimer, QThread, pyqtSignal

class AdbWorker(QThread):
    """后台 ADB 管理线程"""
    device_status_signal = pyqtSignal(str, str, bool) # status_text, color, is_connected
    
    def __init__(self):
        super().__init__()
        self.running = True
        self.adb_path = self.find_adb()
        self.device_id = None

    def find_adb(self):
        if getattr(sys, 'frozen', False):
            base_path = sys._MEIPASS
        else:
            base_path = os.getcwd()
            
        system = platform.system()
        adb_name = "adb.exe" if system == "Windows" else "adb"
        
        local_adb = os.path.join(base_path, adb_name)
        if os.path.exists(local_adb):
            return local_adb
        
        tools_adb = os.path.join(base_path, "platform-tools", adb_name)
        if os.path.exists(tools_adb):
            return tools_adb
            
        return adb_name

    def run(self):
        """持续检测设备连接状态"""
        while self.running:
            try:
                cmd = [self.adb_path, "devices"]
                if platform.system() == "Windows":
                     startupinfo = subprocess.STARTUPINFO()
                     startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                     result = subprocess.run(cmd, capture_output=True, text=True, startupinfo=startupinfo)
                else:
                     result = subprocess.run(cmd, capture_output=True, text=True)
                
                output = result.stdout.strip()
                lines = output.split('\n')[1:]
                devices = [line.split()[0] for line in lines if line.strip() and "device" in line]
                
                if devices:
                    # 如果之前没有设备，或者设备ID变了
                    if self.device_id != devices[0]:
                        self.device_id = devices[0]
                        self.device_status_signal.emit(f"✅ 已连接设备: {self.device_id}", "green", True)
                else:
                    if self.device_id is not None:
                        self.device_id = None
                        self.device_status_signal.emit("⚠️ 未检测到设备，请连接手机", "red", False)
            except Exception as e:
                self.device_status_signal.emit(f"❌ ADB 服务异常: {str(e)}", "red", False)
            
            time.sleep(2)

    def stop(self):
        self.running = False
        self.wait()

    def run_cmd(self, args):
        """同步执行ADB命令"""
        if not self.adb_path: return None
        cmd = [self.adb_path]
        if self.device_id:
            cmd.extend(["-s", self.device_id])
        cmd.extend(args)
        try:
            if platform.system() == "Windows":
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                result = subprocess.run(cmd, capture_output=True, text=True, startupinfo=startupinfo)
            else:
                result = subprocess.run(cmd, capture_output=True, text=True)
            return result.stdout.strip()
        except:
            return None

    def get_device_info(self):
        """获取设备型号和Android版本"""
        model = self.run_cmd(["shell", "getprop", "ro.product.model"])
        version = self.run_cmd(["shell", "getprop", "ro.build.version.release"])
        return f"{model} (Android {version})"

    def get_network_type(self):
        """获取当前网络类型 (增强版)"""
        # 1. 尝试直接 Ping 外网，确认是否有网
        ping_res = self.run_cmd(["shell", "ping", "-c", "1", "-w", "1", "223.5.5.5"])
        has_internet = ping_res and "1 received" in ping_res

        # 2. 检查 Wi-Fi 状态 (dumpsys)
        wifi_dump = self.run_cmd(["shell", "dumpsys", "wifi"])
        if wifi_dump and "Wi-Fi is enabled" in wifi_dump:
            # 进一步检查是否连接
            # 不同安卓版本输出不同，检查 wlan0 是否有 IP 更靠谱
            pass

        # 3. 检查网卡 IP (ip -o -4 addr show)
        # 输出示例: 20: wlan0    inet 192.168.1.5/24 ...
        ip_info = self.run_cmd(["shell", "ip", "-o", "-4", "addr", "show", "up"])
        
        net_types = []
        if ip_info:
            if "wlan" in ip_info:
                net_types.append("Wi-Fi")
            if "rmnet" in ip_info or "ccmni" in ip_info:
                net_types.append("移动数据")
            if "eth" in ip_info:
                net_types.append("有线网络")
        
        if not net_types:
             return "无网络连接 (未检测到有效IP)"
        
        status_str = " + ".join(net_types)
        if has_internet:
            return f"{status_str} (互联网正常)"
        else:
            return f"{status_str} (无互联网访问)"

    def get_brand(self):
        """获取手机品牌"""
        return self.run_cmd(["shell", "getprop", "ro.product.brand"])

    def capture_bugreport(self, save_dir):
        """抓取全量日志 (等同于 284 log)"""
        if not os.path.exists(save_dir):
            os.makedirs(save_dir, exist_ok=True)
        
        # adb bugreport 会生成 zip 文件
        filename = f"bugreport_{int(time.time())}"
        full_path = os.path.join(save_dir, filename)
        
        # 注意: bugreport 命令非常耗时 (1-3分钟)
        # 传递给 bugreport 的参数是文件前缀或目录
        self.run_cmd(["bugreport", full_path])
        return full_path + ".zip"

class NetworkMonitor(QThread):
    """测试期间持续监控网络连通性及网速"""
    error_signal = pyqtSignal(str) # 发送错误信息
    speed_signal = pyqtSignal(str) # 发送网速信息

    def __init__(self, adb_worker):
        super().__init__()
        self.adb_worker = adb_worker
        self.running = False
        self.last_rx = 0
        self.last_tx = 0
        self.last_time = 0

    def get_traffic_stats(self):
        """读取 /proc/net/dev 获取总流量"""
        output = self.adb_worker.run_cmd(["shell", "cat", "/proc/net/dev"])
        if not output: return 0, 0
        
        total_rx = 0
        total_tx = 0
        
        # 解析每一行，累加所有网卡的流量 (忽略 lo)
        for line in output.split('\n'):
            if ":" in line:
                parts = line.split(":")
                iface = parts[0].strip()
                if iface == "lo": continue
                
                # 数据部分可能有很多空格，用 split() 自动处理
                data = parts[1].split()
                if len(data) >= 9:
                    try:
                        rx = int(data[0]) # Receive bytes
                        tx = int(data[8]) # Transmit bytes
                        total_rx += rx
                        total_tx += tx
                    except:
                        pass
        return total_rx, total_tx

    def run(self):
        self.running = True
        # 初始化流量基数
        self.last_rx, self.last_tx = self.get_traffic_stats()
        self.last_time = time.time()
        
        while self.running:
            # 1. 连通性检查 (Ping)
            cmd = ["shell", "ping", "-c", "1", "-w", "1", "223.5.5.5"]
            result = self.adb_worker.run_cmd(cmd)
            
            if not result or "1 packets transmitted, 1 received" not in result:
                time.sleep(0.5)
                result_retry = self.adb_worker.run_cmd(cmd)
                if not result_retry or "1 packets transmitted, 1 received" not in result_retry:
                    self.error_signal.emit("网络连接断开！Ping 丢包。")
                    break
            
            # 2. 网速计算
            current_rx, current_tx = self.get_traffic_stats()
            current_time = time.time()
            
            duration = current_time - self.last_time
            if duration >= 1.0:
                # 计算每秒字节数
                rx_speed = (current_rx - self.last_rx) / duration
                tx_speed = (current_tx - self.last_tx) / duration
                
                # 格式化显示 (KB/s 或 MB/s)
                rx_str = self.format_speed(rx_speed)
                tx_str = self.format_speed(tx_speed)
                
                self.speed_signal.emit(f"⬇️ {rx_str}   ⬆️ {tx_str}")
                
                self.last_rx = current_rx
                self.last_tx = current_tx
                self.last_time = current_time
            
            time.sleep(1) 

    def format_speed(self, bytes_per_sec):
        if bytes_per_sec < 1024:
            return f"{bytes_per_sec:.0f} B/s"
        elif bytes_per_sec < 1024 * 1024:
            return f"{bytes_per_sec/1024:.1f} KB/s"
        else:
            return f"{bytes_per_sec/(1024*1024):.2f} MB/s"

    def stop(self):
        self.running = False
        self.wait()

class BugReportThread(QThread):
    finished_signal = pyqtSignal(str, str) # path, error_msg

    def __init__(self, adb_worker, error_msg):
        super().__init__()
        self.adb_worker = adb_worker
        self.error_msg = error_msg

    def run(self):
        try:
            # 1. 获取型号构建目录名
            date_str = time.strftime("%Y-%m-%d")
            model = self.adb_worker.run_cmd(["shell", "getprop", "ro.product.model"])
            if not model: model = "Unknown"
            model = model.strip().replace(" ", "_")
            
            dir_name = f"{date_str}-{model}-NetworkError"
            
            # 2. 确定保存路径 (当前 exe 同级目录)
            if getattr(sys, 'frozen', False):
                base_path = os.path.dirname(sys.executable)
            else:
                base_path = os.getcwd()
                
            save_dir = os.path.join(base_path, dir_name)
            
            # 3. 执行抓取
            self.adb_worker.capture_bugreport(save_dir)
            
            self.finished_signal.emit(save_dir, self.error_msg)
        except Exception as e:
            self.finished_signal.emit("", str(e))

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("自动化网络测试工具 (Pro)")
        self.resize(500, 750) # 稍微加高一点
        self.setStyleSheet("background-color: #f5f5f5;")

        # 核心逻辑变量
        self.test_duration = 30
        self.remaining_time = 30
        self.is_testing = False
        self.has_triggered_bugreport = False # 防止重复触发日志抓取
        
        # UI 组件初始化
        self.setup_ui()

        # 后台线程
        self.adb_thread = AdbWorker()
        self.adb_thread.device_status_signal.connect(self.update_device_status)
        self.adb_thread.start()

        self.net_monitor = NetworkMonitor(self.adb_thread)
        self.net_monitor.error_signal.connect(self.on_net_error)
        self.net_monitor.speed_signal.connect(self.update_net_speed)

        # 定时器
        self.timer = QTimer()
        self.timer.timeout.connect(self.on_timer_tick)
        
        self.swipe_timer = QTimer()
        self.swipe_timer.timeout.connect(self.do_swipe)

    def setup_ui(self):
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)
        main_layout.setSpacing(15)
        main_layout.setContentsMargins(20, 20, 20, 20)

        # 1. 标题
        title = QLabel("网络自动化测试")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet("font-size: 24px; font-weight: bold; color: #333;")
        main_layout.addWidget(title)

        # 2. 设备与网络信息区域 (GroupBox)
        info_group = QGroupBox("当前环境信息")
        info_group.setStyleSheet("QGroupBox { font-weight: bold; border: 1px solid #ccc; border-radius: 5px; margin-top: 10px; } QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 3px; }")
        info_layout = QFormLayout()
        
        self.lbl_device_info = QLabel("等待连接...")
        self.lbl_net_info = QLabel("等待检测...")
        self.lbl_net_speed = QLabel("---") # 网速显示
        
        info_layout.addRow("📱 设备状态:", self.lbl_device_info)
        info_layout.addRow("🌐 网络类型:", self.lbl_net_info)
        info_layout.addRow("🚀 实时网速:", self.lbl_net_speed) # 新增行
        info_group.setLayout(info_layout)
        main_layout.addWidget(info_group)

        # 3. 设置区域
        setting_group = QGroupBox("测试设置")
        setting_layout = QHBoxLayout()
        
        setting_layout.addWidget(QLabel("⏱️ 执行时间:"))
        
        self.spin_min = QSpinBox()
        self.spin_min.setRange(0, 60)
        self.spin_min.setValue(0)
        self.spin_min.setSuffix(" 分")
        setting_layout.addWidget(self.spin_min)
        
        self.spin_sec = QSpinBox()
        self.spin_sec.setRange(0, 59)
        self.spin_sec.setValue(30)
        self.spin_sec.setSuffix(" 秒")
        setting_layout.addWidget(self.spin_sec)
        
        setting_group.setLayout(setting_layout)
        main_layout.addWidget(setting_group)

        # 4. 倒计时显示
        self.lbl_timer = QLabel("00:00:30")
        self.lbl_timer.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_timer.setStyleSheet("font-size: 50px; font-weight: bold; color: #2196F3; font-family: Arial;")
        main_layout.addWidget(self.lbl_timer)

        # 5. 进度条
        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setStyleSheet("""
            QProgressBar {
                border: 1px solid #bbb;
                border-radius: 4px;
                background-color: white;
                height: 20px;
            }
            QProgressBar::chunk {
                background-color: #4caf50;
            }
        """)
        main_layout.addWidget(self.progress)

        # 6. 控制按钮
        btn_layout = QHBoxLayout()
        
        self.btn_start = QPushButton("开始测试")
        self.btn_start.setFixedHeight(45)
        self.btn_start.setStyleSheet("""
            QPushButton { background-color: #2196F3; color: white; font-size: 16px; border-radius: 5px; font-weight: bold; }
            QPushButton:hover { background-color: #1976D2; }
            QPushButton:disabled { background-color: #BDBDBD; }
        """)
        self.btn_start.clicked.connect(self.on_start_clicked)
        
        self.btn_stop = QPushButton("暂停/停止")
        self.btn_stop.setFixedHeight(45)
        self.btn_stop.setStyleSheet("""
            QPushButton { background-color: #f44336; color: white; font-size: 16px; border-radius: 5px; font-weight: bold; }
            QPushButton:hover { background-color: #d32f2f; }
            QPushButton:disabled { background-color: #ef9a9a; }
        """)
        self.btn_stop.clicked.connect(self.stop_test_manual)
        self.btn_stop.setEnabled(False)

        self.btn_restart = QPushButton("重新开始")
        self.btn_restart.setFixedHeight(45)
        self.btn_restart.setStyleSheet("""
            QPushButton { background-color: #FF9800; color: white; font-size: 16px; border-radius: 5px; font-weight: bold; }
            QPushButton:hover { background-color: #F57C00; }
            QPushButton:disabled { background-color: #FFE0B2; }
        """)
        self.btn_restart.clicked.connect(self.restart_test)
        self.btn_restart.setEnabled(False)
        
        btn_layout.addWidget(self.btn_start)
        btn_layout.addWidget(self.btn_stop)
        btn_layout.addWidget(self.btn_restart)
        main_layout.addLayout(btn_layout)
        
        main_layout.addStretch()

        # 连接设置变更信号，重置按钮状态
        self.spin_min.valueChanged.connect(self.reset_start_button)
        self.spin_sec.valueChanged.connect(self.reset_start_button)

    def update_device_status(self, text, color, connected):
        self.lbl_device_info.setText(text)
        self.lbl_device_info.setStyleSheet(f"color: {color}; font-weight: bold;")
        
        if connected and not self.is_testing:
            # 设备连接后，尝试获取更多信息
            threading.Thread(target=self._fetch_details).start()
            self.btn_start.setEnabled(True)
        elif not connected:
            self.lbl_net_info.setText("等待设备...")
            self.btn_start.setEnabled(False)
            self.btn_restart.setEnabled(False)

    def _fetch_details(self):
        """异步获取详细信息"""
        info = self.adb_thread.get_device_info()
        net_type = self.adb_thread.get_network_type()
        # 实际应用中建议使用信号回传更新UI，此处简化处理
        pass

    def reset_start_button(self):
        """当时间设置变更时，重置为开始状态"""
        if not self.is_testing:
            self.btn_start.setText("开始测试")

    def on_start_clicked(self):
        """处理开始/继续点击"""
        if self.btn_start.text() == "继续测试":
            self.resume_test()
        else:
            self.start_new_test()

    def restart_test(self):
        """重新开始测试"""
        self.start_new_test()

    def start_new_test(self):
        self.has_triggered_bugreport = False
        if not self.adb_thread.device_id:
            QMessageBox.warning(self, "错误", "未连接设备！")
            return
            
        # 1. 计算时间
        mins = self.spin_min.value()
        secs = self.spin_sec.value()
        self.test_duration = mins * 60 + secs
        if self.test_duration <= 0:
            QMessageBox.warning(self, "提示", "请设置有效的执行时间")
            return

        # 2. 获取并显示当前环境信息
        self.lbl_device_info.setText("正在读取信息...")
        self.lbl_net_info.setText("正在分析网络...")
        QApplication.processEvents() # 刷新UI
        
        dev_info = self.adb_thread.get_device_info()
        net_type = self.adb_thread.get_network_type()
        
        self.lbl_device_info.setText(dev_info)
        self.lbl_net_info.setText(net_type)
        
        if "无默认路由" in net_type:
            reply = QMessageBox.question(self, "网络警告", f"当前检测网络为: {net_type}\n可能无法正常测试，是否继续？", 
                                       QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if reply == QMessageBox.StandardButton.No:
                return

        # 3. 初始化状态
        self.is_testing = True
        self.remaining_time = self.test_duration
        self.progress.setRange(0, self.test_duration)
        self.progress.setValue(0)
        self.update_timer_display()
        
        self.enable_testing_ui()
        
        # 4. 启动监控和定时器
        self.net_monitor.start()
        self.timer.start(1000)
        self.swipe_timer.start(2000)

    def resume_test(self):
        """继续测试"""
        self.is_testing = True
        self.enable_testing_ui()
        
        self.net_monitor.start()
        self.timer.start(1000)
        self.swipe_timer.start(2000)

    def enable_testing_ui(self):
        """设置测试运行时的UI状态"""
        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.btn_restart.setEnabled(True) # 运行时也可以点重新开始
        self.spin_min.setEnabled(False)
        self.spin_sec.setEnabled(False)

    def on_net_error(self, error_msg):
        """网络异常回调"""
        self.stop_test_internal(is_pause=False)
        
        # 检查是否需要触发日志抓取 (仅针对小米/红米设备)
        if not self.has_triggered_bugreport:
            brand = self.adb_thread.get_brand()
            # 简单判断是否包含 xiaomi 或 redmi (不区分大小写)
            is_xiaomi = brand and ("xiaomi" in brand.lower() or "redmi" in brand.lower())
            
            if is_xiaomi:
                self.has_triggered_bugreport = True
                
                # 弹窗提示 (非模态，但让用户知道在干嘛)
                self.log_dialog = QMessageBox(self)
                self.log_dialog.setWindowTitle("正在抓取日志")
                self.log_dialog.setIcon(QMessageBox.Icon.Information)
                self.log_dialog.setText("⚠️ 检测到网络异常 (红米/小米设备)\n\n正在自动生成全量系统日志 (类似 284 Log)...\n保存位置: 程序同级目录\n\n⏳ 请耐心等待 1-3 分钟，期间请勿断开手机！")
                self.log_dialog.setStandardButtons(QMessageBox.StandardButton.NoButton) # 禁用按钮，强制等待
                self.log_dialog.show()
                
                # 启动抓取线程
                self.bugreport_thread = BugReportThread(self.adb_thread, error_msg)
                self.bugreport_thread.finished_signal.connect(self.on_bugreport_finished)
                self.bugreport_thread.start()
                return # 暂不显示错误弹窗，等日志抓完再显示

        # 如果不是小米设备或已抓取过，直接显示错误
        QMessageBox.critical(self, "测试异常中止", f"❌ 检测到网络故障！\n\n{error_msg}\n\n测试已立即停止。")

    def on_bugreport_finished(self, save_path, error_msg):
        """日志抓取完成回调"""
        if self.log_dialog:
            self.log_dialog.accept() # 关闭进度弹窗
            
        if save_path:
            msg = f"❌ 检测到网络故障！\n\n{error_msg}\n\n✅ 系统日志已保存至:\n{save_path}"
        else:
            msg = f"❌ 检测到网络故障！\n\n{error_msg}\n\n⚠️ 日志抓取失败: {error_msg}" # 这里的 error_msg 可能是异常信息
            
        QMessageBox.critical(self, "测试异常中止", msg)

    def update_net_speed(self, speed_text):
        """更新网速显示"""
        self.lbl_net_speed.setText(speed_text)
        self.lbl_net_speed.setStyleSheet("color: #2196F3; font-weight: bold;")

    def on_timer_tick(self):
        self.remaining_time -= 1
        self.update_timer_display()
        self.progress.setValue(self.test_duration - self.remaining_time)
        
        if self.remaining_time <= 0:
            self.finish_test()

    def update_timer_display(self):
        m, s = divmod(self.remaining_time, 60)
        h, m = divmod(m, 60)
        self.lbl_timer.setText(f"{h:02d}:{m:02d}:{s:02d}")

    def do_swipe(self):
        threading.Thread(target=self._swipe_thread).start()

    def _swipe_thread(self):
        self.adb_thread.run_cmd(["shell", "input", "swipe", "500", "1500", "500", "500", "200"])

    def stop_test_manual(self):
        self.stop_test_internal(is_pause=True)

    def stop_test_internal(self, is_pause=False):
        self.is_testing = False
        self.timer.stop()
        self.swipe_timer.stop()
        self.net_monitor.stop()
        
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.spin_min.setEnabled(True)
        self.spin_sec.setEnabled(True)
        
        if is_pause and self.remaining_time > 0:
            self.btn_start.setText("继续测试")
            self.lbl_timer.setText(f"{self.lbl_timer.text()} (已暂停)")
        else:
            self.btn_start.setText("开始测试")

    def finish_test(self):
        self.stop_test_internal(is_pause=False)
        self.progress.setValue(self.test_duration)
        self.lbl_timer.setText("00:00:00")
        QMessageBox.information(self, "测试完成", "✅ 指定时间的自动化测试已顺利完成。\n期间网络连接保持正常。")

if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
