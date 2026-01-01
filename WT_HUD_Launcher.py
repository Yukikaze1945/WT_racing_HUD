import sys
import traceback
import ctypes
import threading
import time
import math
import json
import os
from datetime import datetime

# --- 核心设置 ---
UI_SCALE = 1.5

# --- DPI 感知 ---
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except:
        pass

# --- 库加载 ---
print(f"Step 1: 初始化系统 (Scale: {UI_SCALE}x)...")
try:
    import tkinter as tk
    from tkinter import font, messagebox, simpledialog
    import requests
    import pygame
    import keyboard

    print("运行库加载正常。")
except ImportError as e:
    try:
        import tkinter as tk

        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("启动失败", f"缺少库文件！\n请运行: pip install pygame keyboard requests\n\n错误: {e}")
    except:
        pass
    sys.exit()

CONFIG_FILE = "telemetry_config.json"
HISTORY_FILE = "lap_history.csv"

DEFAULT_CONFIG = {
    "rpm_max": 3000,
    "rpm_threshold_pink": 60,
    "rpm_threshold_blue": 90,
    "rpm_threshold_flash": 96,
    "hud_x": -1, "hud_y": -1,
    "timer_x": -1, "timer_y": -1,
    "best_lap": 0.0,
    "show_best_lap": True,
    "action_hotkey": "space"
}

SERVER_URL = "http://127.0.0.1:8111/indicators"


def s(value):
    return int(value * UI_SCALE)


# --- 手柄读取模块 ---
class GamepadReader:
    def __init__(self):
        self.throttle = 0.0
        self.brake = 0.0
        self.connected = False
        self.running = True

        self.target_btn_index = -1
        self.btn_callback = None
        self.last_btn_state = 0

        self.thread = threading.Thread(target=self.loop, daemon=True)
        self.thread.start()

    def set_trigger(self, btn_index, callback):
        self.target_btn_index = btn_index
        self.btn_callback = callback
        print(f"Gamepad: 正在监听按钮 {btn_index}")

    def clear_trigger(self):
        self.target_btn_index = -1
        self.btn_callback = None

    def loop(self):
        pygame.init()
        pygame.joystick.init()
        while self.running:
            if pygame.joystick.get_count() > 0:
                try:
                    joy = pygame.joystick.Joystick(0)
                    joy.init()
                    self.connected = True

                    while self.running and pygame.joystick.get_count() > 0:
                        pygame.event.pump()

                        if joy.get_numaxes() >= 6:
                            self.brake = (joy.get_axis(4) + 1) / 2
                            self.throttle = (joy.get_axis(5) + 1) / 2

                        if self.target_btn_index >= 0 and self.target_btn_index < joy.get_numbuttons():
                            current_state = joy.get_button(self.target_btn_index)
                            if current_state == 1 and self.last_btn_state == 0:
                                if self.btn_callback:
                                    try:
                                        self.btn_callback(None)
                                    except:
                                        pass
                            self.last_btn_state = current_state

                        time.sleep(0.01)
                except Exception:
                    self.connected = False
            else:
                self.connected = False
                time.sleep(1)


gamepad = GamepadReader()


# --- 计时器窗口 ---
class LapTimerWindow:
    def __init__(self, master_root, config):
        self.config = config
        self.root = tk.Toplevel(master_root)
        self.root.title("Lap Timer")  # OBS 看到的窗口名

        base_w, base_h = 300, 150
        self.width = s(base_w)
        self.height = s(base_h)

        self.start_time = 0.0
        self.final_time = 0.0
        self.is_running = False
        self.just_finished = False
        self.save_feedback_timer = 0

        if self.config["timer_x"] != -1:
            x, y = self.config["timer_x"], self.config["timer_y"]
        else:
            x, y = 100, 100
        self.root.geometry(f"{self.width}x{self.height}+{x}+{y}")

        # --- OBS 核心修复：不使用 overrideredirect，而是手动切边框 ---
        self.root.overrideredirect(False)
        self.root.attributes('-topmost', True)

        self.bg_color = '#000001'
        self.root.configure(bg=self.bg_color)
        self.root.attributes('-transparentcolor', self.bg_color)

        # 绑定 Map 事件：等窗口生成后，立刻切掉边框
        self.root.bind("<Map>", self.remove_border)

        self.canvas = tk.Canvas(self.root, width=self.width, height=self.height,
                                bg=self.bg_color, highlightthickness=0)
        self.canvas.pack()

        self.font_label = font.Font(family="Helvetica", size=-s(14), weight="bold")
        self.font_time_big = font.Font(family="Consolas", size=-s(48), weight="bold")
        self.font_time_small = font.Font(family="Consolas", size=-s(24), weight="bold")
        self.font_feedback = font.Font(family="Helvetica", size=-s(10), weight="bold")

        self.canvas.bind("<Button-1>", self.start_move)
        self.canvas.bind("<B1-Motion>", self.do_move)

        self.setup_hotkey()
        self.update_ui_loop()

    def remove_border(self, event):
        """ 使用 Windows API 强行切除标题栏，但保留 OBS 可见性 """
        try:
            # 获取窗口句柄
            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())

            # 获取当前样式
            GWL_STYLE = -16
            style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_STYLE)

            # 移除标题栏 (WS_CAPTION) 和 调整边框 (WS_THICKFRAME)
            style = style & ~0xC00000
            style = style & ~0x40000

            ctypes.windll.user32.SetWindowLongW(hwnd, GWL_STYLE, style)

            # 强制刷新窗口样式
            SWP_FRAMECHANGED = 0x0020
            SWP_NOMOVE = 0x0002
            SWP_NOSIZE = 0x0001
            SWP_NOZORDER = 0x0004

            ctypes.windll.user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0,
                                              SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_FRAMECHANGED)
        except Exception as e:
            print(f"Timer Window API Error: {e}")

    def setup_hotkey(self):
        raw_key = self.config.get("action_hotkey", "space").lower()
        try:
            keyboard.unhook_all()
        except:
            pass
        gamepad.clear_trigger()

        if raw_key.startswith("btn"):
            try:
                btn_idx = int(raw_key.replace("btn", ""))
                gamepad.set_trigger(btn_idx, self.on_hotkey_press)
                print(f"Timer bound to Gamepad Button {btn_idx}")
            except:
                keyboard.on_press_key("space", self.on_hotkey_press)
        else:
            try:
                keyboard.on_press_key(raw_key, self.on_hotkey_press)
                print(f"Timer bound to Keyboard {raw_key}")
            except:
                pass

    def on_hotkey_press(self, event):
        if not self.is_running:
            self.start_lap()
        else:
            self.finish_lap()

    def start_lap(self):
        self.start_time = time.time()
        self.is_running = True
        self.just_finished = False
        self.save_feedback_timer = 0
        self.final_time = 0.0

    def finish_lap(self):
        if not self.is_running: return
        end_time = time.time()
        self.final_time = end_time - self.start_time
        self.is_running = False
        self.just_finished = True

        current_best = self.config["best_lap"]
        if current_best == 0.0 or self.final_time < current_best:
            self.config["best_lap"] = self.final_time

        self.save_lap_to_file(self.final_time)
        self.save_feedback_timer = 60

    def save_lap_to_file(self, lap_seconds):
        try:
            file_exists = os.path.isfile(HISTORY_FILE)
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            time_str = self.format_time(lap_seconds)
            with open(HISTORY_FILE, "a", encoding="utf-8") as f:
                if not file_exists:
                    f.write("Timestamp,Formatted Time,Seconds\n")
                f.write(f"{timestamp},{time_str},{lap_seconds:.3f}\n")
            print(f"Lap saved: {time_str}")
        except Exception as e:
            print(f"Save failed: {e}")

    def format_time(self, seconds):
        if seconds == 0: return "--:--.---"
        mins = int(seconds // 60)
        secs = int(seconds % 60)
        ms = int((seconds * 1000) % 1000)
        return f"{mins:02}:{secs:02}.{ms:03}"

    def update_ui_loop(self):
        self.canvas.delete("all")
        cx = self.width / 2

        if self.config["show_best_lap"]:
            txt = f"BEST: {self.format_time(self.config['best_lap'])}"
            self.canvas.create_text(cx, s(30), text=txt, fill="#00ffff", font=self.font_time_small)

        if self.is_running:
            display_time = self.format_time(time.time() - self.start_time)
            color, status = "#ffffff", "RUNNING"
        elif self.just_finished:
            display_time = self.format_time(self.final_time)
            if self.config["best_lap"] > 0 and abs(self.final_time - self.config["best_lap"]) < 0.001:
                color, status = "#ffd700", "NEW RECORD"
            else:
                color, status = "#ffffff", "FINAL"
        else:
            display_time = "00:00.000"
            color, status = "#888888", "READY"

        self.canvas.create_text(cx, s(80), text=display_time, fill=color, font=self.font_time_big)

        if self.save_feedback_timer > 0:
            self.save_feedback_timer -= 1
            self.canvas.create_text(cx, s(130), text="DATA SAVED ✓", fill="#55ff55", font=self.font_feedback)
        else:
            self.canvas.create_text(cx, s(130), text=status, fill="#888888", font=self.font_label)

        if self.root.winfo_exists():
            self.root.after(33, self.update_ui_loop)

    def start_move(self, event):
        self.x, self.y = event.x, event.y

    def do_move(self, event):
        new_x = self.root.winfo_x() + event.x - self.x
        new_y = self.root.winfo_y() + event.y - self.y
        self.root.geometry(f"{self.width}x{self.height}+{new_x}+{new_y}")

    def get_pos(self):
        return self.root.winfo_x(), self.root.winfo_y()

    def close(self):
        keyboard.unhook_all()
        gamepad.clear_trigger()
        self.root.destroy()


# --- 主 HUD 窗口 ---
class MainHUDWindow:
    def __init__(self, master_root, config):
        self.config = config
        self.root = tk.Toplevel(master_root)
        self.root.title("Main HUD")  # OBS 看到的窗口名

        base_w, base_h = 600, 300
        self.width = s(base_w)
        self.height = s(base_h)

        if self.config["hud_x"] != -1:
            x, y = self.config["hud_x"], self.config["hud_y"]
        else:
            x = (self.root.winfo_screenwidth() - self.width) // 2
            y = self.root.winfo_screenheight() - self.height - 100

        self.root.geometry(f"{self.width}x{self.height}+{x}+{y}")

        # --- OBS 核心修复 ---
        self.root.overrideredirect(False)
        self.root.attributes('-topmost', True)

        self.bg_color = '#000001'
        self.root.configure(bg=self.bg_color)
        self.root.attributes('-transparentcolor', self.bg_color)

        # 绑定切边框事件
        self.root.bind("<Map>", self.remove_border)

        self.canvas = tk.Canvas(self.root, width=self.width, height=self.height,
                                bg=self.bg_color, highlightthickness=0)
        self.canvas.pack()

        self.font_val = font.Font(family="Impact", size=-s(70))
        self.font_unit = font.Font(family="Helvetica", size=-s(16), weight="bold")
        self.font_gear = font.Font(family="Impact", size=-s(100))
        self.font_rpm = font.Font(family="Consolas", size=-s(16), weight="bold")
        self.font_cc = font.Font(family="Impact", size=-s(32))
        self.font_cc_label = font.Font(family="Helvetica", size=-s(12), weight="bold")

        self.canvas.bind("<Button-1>", self.start_move)
        self.canvas.bind("<B1-Motion>", self.do_move)

        self.update_loop()

    def remove_border(self, event):
        """ 使用 Windows API 强行切除标题栏 """
        try:
            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
            GWL_STYLE = -16
            style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_STYLE)
            style = style & ~0xC00000
            style = style & ~0x40000
            ctypes.windll.user32.SetWindowLongW(hwnd, GWL_STYLE, style)
            SWP_FRAMECHANGED = 0x0020
            SWP_NOMOVE = 0x0002
            SWP_NOSIZE = 0x0001
            SWP_NOZORDER = 0x0004
            ctypes.windll.user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0,
                                              SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_FRAMECHANGED)
        except Exception as e:
            print(f"HUD Window API Error: {e}")

    def get_bar_color(self, rpm_ratio):
        th_flash = self.config.get("rpm_threshold_flash", 96) / 100.0
        th_blue = self.config.get("rpm_threshold_blue", 90) / 100.0
        th_pink = self.config.get("rpm_threshold_pink", 60) / 100.0

        if rpm_ratio > th_flash:
            return "#00ffff" if int(time.time() * 20) % 2 else "#003333"
        if rpm_ratio > th_blue:
            return "#00ffff"
        if rpm_ratio > th_pink:
            return "#ff00ff"
        return "#ff0000"

    def draw_pedals(self):
        b_val = gamepad.brake
        t_val = gamepad.throttle
        bar_h = s(200)
        fill_h = int(b_val * bar_h)

        self.canvas.create_rectangle(s(20), s(50), s(40), s(50) + bar_h, outline="#444", width=2)
        if fill_h > 0:
            self.canvas.create_rectangle(s(22), s(50) + bar_h - fill_h, s(38), s(50) + bar_h, fill="#ff0000",
                                         outline="")

        base_right_x = 560
        self.canvas.create_rectangle(s(base_right_x), s(50), s(base_right_x + 20), s(50) + bar_h, outline="#444",
                                     width=2)

        fill_h_t = int(t_val * bar_h)
        if fill_h_t > 0:
            self.canvas.create_rectangle(s(base_right_x + 2), s(50) + bar_h - fill_h_t, s(base_right_x + 18),
                                         s(50) + bar_h, fill="#ffffff", outline="")

    def update_loop(self):
        try:
            resp = requests.get(SERVER_URL, timeout=0.02)
            data = resp.json()
            if data['valid']:
                rpm = data.get('rpm', 0)
                speed = int(data.get('speed', 0))
                gear = int(data.get('gear', 0))
                neutral = int(data.get('gear_neutral', 1))
                cc = int(data.get('cruise_control', 0))

                rpm_max = float(self.config["rpm_max"])
                if rpm_max <= 0: rpm_max = 3000
                rpm_ratio = min(rpm / rpm_max, 1.0)

                if gear == neutral:
                    g_txt = "N"
                elif gear > neutral:
                    g_txt = str(gear - neutral)
                else:
                    g_txt = f"R{neutral - gear}"

                self.canvas.delete("all")

                bar_col = self.get_bar_color(rpm_ratio)
                segments = 60
                active = int(segments * rpm_ratio)
                base_bar_w = 500
                base_bar_x = 50

                for i in range(segments):
                    prog = i / segments
                    x = s(base_bar_x) + i * (s(base_bar_w) / segments)
                    off_y = math.sin(prog * math.pi) * s(20)
                    y = s(70) - off_y
                    col = bar_col if i < active else "#222222"
                    self.canvas.create_rectangle(x, y, x + (s(base_bar_w) / segments) - 1.5, y + s(25), fill=col,
                                                 outline="")

                self.draw_pedals()

                self.canvas.create_text(s(240), s(150), text=str(speed), font=self.font_val, fill="white", anchor="e")
                self.canvas.create_text(s(250), s(190), text="km/h", font=self.font_unit, fill="#aaa", anchor="w")
                self.canvas.create_line(s(290), s(135), s(290), s(195), fill="white", width=2)
                self.canvas.create_text(s(340), s(150), text=g_txt, font=self.font_gear, fill="white", anchor="w")
                self.canvas.create_text(s(550), s(105), text=f"{int(rpm)} rpm", font=self.font_rpm, fill=bar_col,
                                        anchor="e")

                if cc != 0:
                    cc_txt = str(cc) if cc > 0 else f"R{abs(cc)}"
                    box_x = s(460)
                    box_y = s(145)
                    box_w = s(50)
                    box_h = s(40)
                    self.canvas.create_rectangle(box_x, box_y, box_x + box_w, box_y + box_h, fill="#e60012", outline="")
                    box_cx = box_x + box_w / 2
                    self.canvas.create_text(box_cx, box_y + s(15), text=cc_txt, font=self.font_cc, fill="white")
                    self.canvas.create_text(box_cx, box_y + s(32), text="CC", font=self.font_cc_label, fill="white")

        except:
            pass

        if self.root.winfo_exists():
            self.root.after(16, self.update_loop)

    def start_move(self, event):
        self.x, self.y = event.x, event.y

    def do_move(self, event):
        new_x = self.root.winfo_x() + event.x - self.x
        new_y = self.root.winfo_y() + event.y - self.y
        self.root.geometry(f"{self.width}x{self.height}+{new_x}+{new_y}")

    def get_pos(self):
        return self.root.winfo_x(), self.root.winfo_y()

    def close(self):
        self.root.destroy()


# --- 控制台 ---
class ControlPanel:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("WT TELEMETRY CONTROL")
        self.root.geometry("400x650")

        self.colors = {
            "bg": "#2b2b2b",
            "fg": "#e0e0e0",
            "accent": "#00adb5",
            "panel": "#383838",
            "input": "#484848",
            "btn_launch": "#27ae60",
            "btn_save": "#e67e22"
        }
        self.root.configure(bg=self.colors["bg"])

        self.config = self.load_config()
        self.hud = None
        self.timer = None

        header = tk.Frame(self.root, bg=self.colors["bg"])
        header.pack(pady=20)
        tk.Label(header, text="WAR THUNDER", font=("Impact", 24), fg=self.colors["fg"], bg=self.colors["bg"]).pack()
        tk.Label(header, text="TELEMETRY SYSTEM", font=("Helvetica", 10, "bold"), fg=self.colors["accent"],
                 bg=self.colors["bg"]).pack()

        f_car = self.create_section("车辆参数 / VEHICLE")

        row1 = tk.Frame(f_car, bg=self.colors["panel"])
        row1.pack(fill="x", pady=2)
        tk.Label(row1, text="引擎转速上限 (RPM):", fg=self.colors["fg"], bg=self.colors["panel"]).pack(side=tk.LEFT)
        self.e_rpm = self.create_input(row1, str(self.config["rpm_max"]), 8)
        self.e_rpm.pack(side=tk.RIGHT)

        row_p = tk.Frame(f_car, bg=self.colors["panel"])
        row_p.pack(fill="x", pady=2)
        tk.Label(row_p, text="[粉色] 变色阈值 (%):", fg="#ff00ff", bg=self.colors["panel"]).pack(side=tk.LEFT)
        self.e_pink = self.create_input(row_p, str(self.config.get("rpm_threshold_pink", 60)), 5)
        self.e_pink.pack(side=tk.RIGHT)

        row_b = tk.Frame(f_car, bg=self.colors["panel"])
        row_b.pack(fill="x", pady=2)
        tk.Label(row_b, text="[青色] 换挡提示 (%):", fg="#00ffff", bg=self.colors["panel"]).pack(side=tk.LEFT)
        self.e_blue = self.create_input(row_b, str(self.config.get("rpm_threshold_blue", 90)), 5)
        self.e_blue.pack(side=tk.RIGHT)

        row_f = tk.Frame(f_car, bg=self.colors["panel"])
        row_f.pack(fill="x", pady=2)
        tk.Label(row_f, text="[闪烁] 断油警告 (%):", fg="#ffffff", bg=self.colors["panel"]).pack(side=tk.LEFT)
        self.e_flash = self.create_input(row_f, str(self.config.get("rpm_threshold_flash", 96)), 5)
        self.e_flash.pack(side=tk.RIGHT)

        f_timer = self.create_section("计时器 / TIMER")
        self.v_show_best = tk.BooleanVar(value=self.config["show_best_lap"])
        chk = tk.Checkbutton(f_timer, text="显示历史最快圈 (Show Best)", variable=self.v_show_best,
                             command=self.update_config_live,
                             bg=self.colors["panel"], fg=self.colors["fg"], selectcolor=self.colors["input"],
                             activebackground=self.colors["panel"], activeforeground=self.colors["accent"])
        chk.pack(anchor="w", pady=2)

        row2 = tk.Frame(f_timer, bg=self.colors["panel"])
        row2.pack(fill="x", pady=5)
        tk.Label(row2, text="手动修正记录 (秒):", fg=self.colors["fg"], bg=self.colors["panel"]).pack(side=tk.LEFT)
        self.e_best = self.create_input(row2, str(self.config["best_lap"]), 8)
        self.e_best.pack(side=tk.RIGHT)

        self.btn_key = tk.Button(f_timer, text=f"快捷键: [{self.config['action_hotkey'].upper()}]",
                                 command=self.set_hotkey,
                                 bg=self.colors["input"], fg=self.colors["accent"], relief="flat",
                                 activebackground=self.colors["accent"], activeforeground="#fff")
        self.btn_key.pack(fill="x", pady=5, ipady=3)

        f_ctrl = tk.Frame(self.root, bg=self.colors["bg"])
        f_ctrl.pack(fill="x", padx=20, pady=10)

        tk.Button(f_ctrl, text="▶ 启动 / 重启仪表盘", font=("Helvetica", 11, "bold"),
                  bg=self.colors["btn_launch"], fg="white", relief="flat",
                  command=self.launch_all).pack(fill="x", pady=5, ipady=5)

        tk.Button(f_ctrl, text="💾 保存当前布局", font=("Helvetica", 10),
                  bg=self.colors["btn_save"], fg="white", relief="flat",
                  command=self.save_all).pack(fill="x", pady=5, ipady=3)

        f_info = tk.Frame(self.root, bg=self.colors["bg"])
        f_info.pack(fill="both", expand=True, padx=20, pady=10)

        info_title = tk.Label(f_info, text="OBS 采集设置 (必读)", font=("Arial", 9, "bold"), fg="#e67e22",
                              bg=self.colors["bg"])
        info_title.pack(anchor="w")

        instructions = (
            "1. 启动 HUD，确保任务栏里有 [Main HUD] 窗口。\n"
            "2. 打开 OBS -> 来源 -> 窗口采集。\n"
            "3. 采集方式选择: [Windows 10 (1903 及以上版本)]。\n"
            "4. 务必勾选 [允许透明度] (Allow Transparency)。"
        )
        tk.Label(f_info, text=instructions, font=("Arial", 9), fg="#aaa", bg=self.colors["bg"], justify=tk.LEFT).pack(
            anchor="w", pady=2)

        self.root.protocol("WM_DELETE_WINDOW", self.close_app)
        self.root.mainloop()

    def create_section(self, title):
        frame = tk.LabelFrame(self.root, text=title, font=("Arial", 9, "bold"),
                              bg=self.colors["panel"], fg="#aaa", bd=1, relief="flat")
        frame.pack(fill="x", padx=20, pady=5, ipady=5)
        inner = tk.Frame(frame, bg=self.colors["panel"], padx=10)
        inner.pack(fill="x")
        return inner

    def create_input(self, parent, default_val, width):
        entry = tk.Entry(parent, width=width, bg=self.colors["input"], fg=self.colors["fg"],
                         insertbackground="white", relief="flat", justify="center")
        entry.insert(0, default_val)
        return entry

    def load_config(self):
        cfg = DEFAULT_CONFIG.copy()
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, "r") as f:
                    user_cfg = json.load(f)
                    cfg.update(user_cfg)
            except:
                pass
        return cfg

    def update_config_live(self):
        self.config["show_best_lap"] = self.v_show_best.get()

    def set_hotkey(self):
        key = simpledialog.askstring("设置按键", "请输入按键:\n\n键盘: space, a, enter\n手柄: btn4 (LB), btn0 (A)...")
        if key:
            self.config["action_hotkey"] = key
            self.btn_key.config(text=f"快捷键: [{key.upper()}]")
            if self.timer: self.timer.setup_hotkey()

    def launch_all(self):
        if self.hud: self.hud.close()
        if self.timer: self.timer.close()
        try:
            self.config["rpm_max"] = int(self.e_rpm.get())
            self.config["rpm_threshold_pink"] = int(self.e_pink.get())
            self.config["rpm_threshold_blue"] = int(self.e_blue.get())
            self.config["rpm_threshold_flash"] = int(self.e_flash.get())
            self.config["best_lap"] = float(self.e_best.get())
        except ValueError:
            messagebox.showerror("错误", "请输入有效的数字！")
            return

        self.hud = MainHUDWindow(self.root, self.config)
        self.timer = LapTimerWindow(self.root, self.config)

    def save_all(self):
        if self.hud: self.config["hud_x"], self.config["hud_y"] = self.hud.get_pos()
        if self.timer: self.config["timer_x"], self.config["timer_y"] = self.timer.get_pos()
        try:
            self.config["rpm_max"] = int(self.e_rpm.get())
            self.config["rpm_threshold_pink"] = int(self.e_pink.get())
            self.config["rpm_threshold_blue"] = int(self.e_blue.get())
            self.config["rpm_threshold_flash"] = int(self.e_flash.get())
            self.config["best_lap"] = float(self.e_best.get())
        except ValueError:
            messagebox.showerror("错误", "请输入有效的数字！")
            return

        with open(CONFIG_FILE, "w") as f:
            json.dump(self.config, f, indent=4)
        messagebox.showinfo("System", "Configuration Saved Successfully.")

    def close_app(self):
        if self.hud: self.hud.close()
        if self.timer: self.timer.close()
        self.root.destroy()
        os._exit(0)


if __name__ == "__main__":
    ControlPanel()