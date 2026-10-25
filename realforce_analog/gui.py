"""Calibrate R3SD11 sensor telemetry and send analog input to a vJoy X axis."""

from collections import deque
from dataclasses import asdict
import json
from pathlib import Path
import queue
import statistics
import threading
import time
import tkinter as tk
from tkinter import ttk

from .model import KeyCalibration, SteeringCalibration
from .output import VJoyOutput
from .protocol import SensorReader


SETTINGS = Path(__file__).resolve().parent.parent / "settings.local.json"
BG = "#10151e"
CARD = "#192231"
FG = "#ecf3fc"
MUTED = "#a8b8cc"
ACCENT = "#69e0c5"


def percentile(values, fraction):
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int((len(ordered) - 1) * fraction))]


class Worker:
    """The only owner of the keyboard handle. UI receives data through queues."""

    def __init__(self):
        self.commands = queue.Queue(maxsize=4)
        self.events = queue.Queue(maxsize=64)
        self.live = queue.Queue(maxsize=1)
        self.stop = threading.Event()
        self.shutdown = threading.Event()
        self.thread = threading.Thread(target=self._run, name="sensor-worker", daemon=True)
        self.thread.start()

    def submit(self, name, data=None):
        self.stop.clear()
        self.commands.put_nowait((name, data))

    def event(self, name, data=None):
        # Only bounded status/progress events are emitted; live data has a latest-only queue.
        try:
            self.events.put_nowait((name, data))
        except queue.Full:
            self.stop.set()

    def _run(self):
        while not self.shutdown.is_set():
            try:
                name, data = self.commands.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                if name == "scan":
                    self._scan(data)
                elif name == "preview":
                    self._preview(*data)
            except Exception as error:
                self.event("error", str(error))
            finally:
                self.event("done")

    def _scan(self, phase):
        readings = {index: [] for index in range(128)}
        # Two sweeps avoid choosing a single noisy value as a calibration endpoint.
        with SensorReader(timeout_ms=100) as reader:
            self.event("connected", reader.info)
            for sweep in range(2):
                for index in range(128):
                    if self.stop.is_set() or self.shutdown.is_set():
                        return
                    sample = reader.read(index)
                    if sample.populated:
                        readings[index].append(sample.delta)
                    if index % 16 == 15:
                        self.event("progress", (sweep * 128 + index + 1) / 256)
        result = {index: {"delta": statistics.median(values),
                          "noise": max(values) - min(values)}
                  for index, values in readings.items() if len(values) == 2}
        self.event("scan", (phase, result))

    def _preview(self, calibration, output_enabled, device_id):
        output = None
        output_lock = threading.RLock()
        watchdog_stop = threading.Event()
        last_good = [time.monotonic()]

        def watchdog():
            while not watchdog_stop.wait(0.025):
                if time.monotonic() - last_good[0] > 0.1:
                    self.stop.set()
                    try:
                        with output_lock:
                            if output is not None:
                                output.set_steering(0.0)
                    except Exception:
                        pass  # Main cleanup still releases the device.
                    self.event("error", "Steering stopped: no complete sensor pair for 100 ms.")
                    return

        watch_thread = None
        failure = None
        try:
            if output_enabled:
                output = VJoyOutput(device_id=device_id).open()
            with SensorReader(timeout_ms=50) as reader:
                self.event("connected", reader.info)
                last_good[0] = time.monotonic()
                if output is not None:
                    watch_thread = threading.Thread(target=watchdog, daemon=True,
                                                    name="steering-watchdog")
                    watch_thread.start()
                requests, pairs = deque(maxlen=300), deque(maxlen=150)
                previous = None
                while not self.stop.is_set() and not self.shutdown.is_set():
                    left = reader.read(calibration.left.index)
                    if self.stop.is_set() or self.shutdown.is_set():
                        break
                    right = reader.read(calibration.right.index)
                    now = time.monotonic()
                    requests.extend((left.round_trip_ms, right.round_trip_ms))
                    if previous is not None:
                        pairs.append((now - previous) * 1000)
                    previous = now
                    if not left.populated or not right.populated:
                        raise RuntimeError("A mapped sensor stopped returning populated readings.")
                    steering = calibration.steering(left.delta, right.delta)
                    if output is not None:
                        with output_lock:
                            if self.stop.is_set() or self.shutdown.is_set():
                                break
                            output.set_steering(steering)
                    last_good[0] = now
                    frame = {"left": calibration.left.normalize(left.delta),
                             "right": calibration.right.normalize(right.delta),
                             "left_raw": left.delta, "right_raw": right.delta,
                             "steering": steering,
                             "request_ms": statistics.mean(requests),
                             "pair_ms": statistics.mean(pairs) if pairs else 0.0,
                             "p95_ms": percentile(pairs, 0.95) if pairs else 0.0,
                             "output": output is not None}
                    try:
                        self.live.get_nowait()
                    except queue.Empty:
                        pass
                    self.live.put_nowait(frame)
        except Exception as error:
            failure = error
        finally:
            watchdog_stop.set()
            if watch_thread is not None:
                watch_thread.join(timeout=0.15)
            if output is not None:
                try:
                    with output_lock:
                        output.close()
                except Exception as error:
                    if failure is None:
                        failure = error
            if failure is not None:
                raise failure


class App:
    def __init__(self, root):
        self.root = root
        self.worker = Worker()
        self.busy = False
        self.closing = False
        self.idle = None
        self.calibration = {}
        self.device_id = tk.StringVar(value="1")
        self.status = tk.StringVar(value="Ready. Start by scanning with every key released.")
        self.connection = tk.StringVar(value="R3SD11 · stock telemetry · no firmware changes")
        self.stats = tk.StringVar(value="Request timing appears when preview starts.")
        self.left_info = tk.StringVar(value="Not mapped")
        self.right_info = tk.StringVar(value="Not mapped")
        self._build()
        self._load()
        self._buttons()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.after(40, self._drain)

    def _build(self):
        root = self.root
        root.title("Realforce to vJoy")
        root.configure(bg=BG)
        root.geometry("860x820")
        root.minsize(790, 800)
        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure("TButton", font=("Segoe UI", 10), padding=(14, 9),
                        background="#28384c", foreground=FG, borderwidth=0)
        style.map("TButton", background=[("active", "#35506a"), ("disabled", "#212b39")],
                  foreground=[("disabled", "#6e7c8e")])
        style.configure("Accent.TButton", background="#246d63", foreground=FG)
        style.configure("Horizontal.TProgressbar", troughcolor="#29364a", background=ACCENT,
                        borderwidth=0, thickness=12)
        shell = tk.Frame(root, bg=BG, padx=28, pady=22)
        shell.pack(fill="both", expand=True)
        tk.Label(shell, text="REALFORCE → VJOY", bg=BG, fg=ACCENT,
                 font=("Segoe UI", 10, "bold")).pack(anchor="w")
        tk.Label(shell, text="Key depth to a virtual joystick axis", bg=BG, fg=FG,
                 font=("Segoe UI", 23, "bold")).pack(anchor="w", pady=(3, 5))
        tk.Label(shell, textvariable=self.connection, bg=BG, fg=MUTED,
                 font=("Segoe UI", 10)).pack(anchor="w")

        setup = self.card(shell, "1   Calibrate the Left / Right arrows")
        tk.Label(setup, text="Use the mouse for these buttons. Hold the requested arrow fully down\n"
                            "until its scan finishes; release every other key.",
                 bg=CARD, fg=MUTED, justify="left", font=("Segoe UI", 10)).pack(anchor="w", pady=(0, 10))
        row = tk.Frame(setup, bg=CARD)
        row.pack(fill="x")
        self.idle_button = ttk.Button(row, text="Scan idle", command=lambda: self.scan("idle"))
        self.idle_button.pack(side="left", padx=(0, 10))
        self.left_button = ttk.Button(row, text="Hold ← and map", command=lambda: self.scan("left"))
        self.left_button.pack(side="left", padx=(0, 10))
        self.right_button = ttk.Button(row, text="Hold → and map", command=lambda: self.scan("right"))
        self.right_button.pack(side="left")
        self.progress = ttk.Progressbar(setup, maximum=1, mode="determinate")
        self.progress.pack(fill="x", pady=(14, 8))
        for var in (self.left_info, self.right_info):
            tk.Label(setup, textvariable=var, bg=CARD, fg=MUTED,
                     font=("Segoe UI", 10)).pack(anchor="w")

        live = self.card(shell, "2   Preview calibrated input")
        for name, attribute in (("← Left", "left_bar"), ("→ Right", "right_bar")):
            row = tk.Frame(live, bg=CARD)
            row.pack(fill="x", pady=4)
            tk.Label(row, text=name, width=9, anchor="w", bg=CARD, fg=FG,
                     font=("Segoe UI", 11)).pack(side="left")
            bar = ttk.Progressbar(row, maximum=1)
            bar.pack(side="left", fill="x", expand=True, padx=(6, 12))
            label = tk.Label(row, text="0%", width=5, anchor="e", bg=CARD, fg=FG,
                             font=("Consolas", 11))
            label.pack(side="right")
            setattr(self, attribute, (bar, label))
        self.steering_canvas = tk.Canvas(live, height=50, bg=CARD, highlightthickness=0)
        self.steering_canvas.pack(fill="x", pady=(4, 1))
        self.steering_canvas.bind("<Configure>", lambda _: self._draw_steering(0.0))
        tk.Label(live, textvariable=self.stats, bg=CARD, fg=MUTED,
                 font=("Consolas", 10)).pack(anchor="w")
        tk.Label(live, text="Timing measures USB queries, not physical key-to-screen latency.",
                 bg=CARD, fg=MUTED, font=("Segoe UI", 9)).pack(anchor="w", pady=(6, 0))

        controls = tk.Frame(shell, bg=BG)
        controls.pack(fill="x", pady=(14, 8))
        self.preview_button = ttk.Button(controls, text="Start preview", style="Accent.TButton",
                                         command=lambda: self.preview(False))
        self.preview_button.pack(side="left", padx=(0, 10))
        self.drive_button = ttk.Button(controls, text="Start vJoy output", command=lambda: self.preview(True))
        self.drive_button.pack(side="left", padx=(0, 8))
        tk.Label(controls, text="Device", bg=BG, fg=MUTED,
                 font=("Segoe UI", 9)).pack(side="left", padx=(3, 5))
        self.device_spin = tk.Spinbox(controls, from_=1, to=16, width=3, textvariable=self.device_id,
                                     bg=CARD, fg=FG, buttonbackground=CARD, relief="flat",
                                     font=("Segoe UI", 10))
        self.device_spin.pack(side="left")
        self.stop_button = ttk.Button(controls, text="Stop", command=self.stop)
        self.stop_button.pack(side="right")
        tk.Label(shell, text="Output: Left/Right arrows → vJoy X axis. Bind this axis in your application.\n"
                             "Remove matching digital key bindings to avoid full-strength digital input.",
                 bg=BG, fg=MUTED, font=("Segoe UI", 9), justify="left").pack(anchor="w", pady=(4, 10))
        self.status_label = tk.Label(shell, textvariable=self.status, bg=BG, fg=ACCENT,
                                     wraplength=790, anchor="w", justify="left", font=("Segoe UI", 10))
        self.status_label.pack(fill="x")

    def card(self, parent, title):
        frame = tk.Frame(parent, bg=CARD, padx=18, pady=15)
        frame.pack(fill="x", pady=(17, 0))
        tk.Label(frame, text=title, bg=CARD, fg=FG,
                 font=("Segoe UI", 12, "bold")).pack(anchor="w", pady=(0, 9))
        return frame

    def _buttons(self):
        for button, enabled in ((self.idle_button, not self.busy),
                                (self.left_button, not self.busy and self.idle is not None),
                                (self.right_button, not self.busy and self.idle is not None),
                                (self.preview_button, not self.busy and len(self.calibration) == 2),
                                (self.drive_button, not self.busy and len(self.calibration) == 2),
                                (self.stop_button, self.busy)):
            button.configure(state="normal" if enabled else "disabled")
        self.device_spin.configure(state="disabled" if self.busy else "normal")

    def scan(self, phase):
        self.busy = True
        self.status_label.configure(fg=ACCENT)
        self.progress["value"] = 0
        if phase == "idle":
            self.status.set("Scanning idle. Keep every key released until complete…")
        else:
            self.status.set(f"Scanning {phase.upper()}. Hold that arrow fully down until complete…")
        self._buttons()
        self.worker.submit("scan", phase)

    def preview(self, output_enabled):
        try:
            device = int(self.device_id.get())
            if not 1 <= device <= 16:
                raise ValueError
        except ValueError:
            self.status.set("Choose a vJoy device number from 1 to 16.")
            return
        self.busy = True
        self.status_label.configure(fg=ACCENT)
        self.status.set("vJoy output active. Stop centers and releases the virtual axis."
                        if output_enabled else "Preview active. Press the arrows gently through their travel.")
        self._buttons()
        calibration = SteeringCalibration(self.calibration["left"], self.calibration["right"])
        self.worker.submit("preview", (calibration, output_enabled, device))

    def stop(self):
        self.worker.stop.set()
        self.status.set("Stopping and centering steering…")

    def _mapped(self, phase, result):
        if phase == "idle":
            self.idle = result
            self.calibration.clear()
            self.left_info.set("← Left: not mapped")
            self.right_info.set("→ Right: not mapped")
            self.status.set("Idle captured. Hold LEFT fully down and click ‘Hold ← and map’.")
            return
        candidates = []
        for index in result.keys() & self.idle.keys():
            baseline, held = self.idle[index], result[index]
            difference = held["delta"] - baseline["delta"]
            candidates.append((abs(difference), index, difference,
                               max(baseline["noise"], held["noise"])))
        candidates.sort(reverse=True)
        if not candidates:
            raise ValueError("No populated sensor readings. Repeat the idle scan.")
        magnitude, index, difference, noise = candidates[0]
        runner_up = candidates[1][0] if len(candidates) > 1 else 0
        if magnitude < max(120, 8 * noise) or magnitude < runner_up * 2.5:
            raise ValueError("Could not isolate one fully held key. Release other keys and retry this arrow.")
        other = "right" if phase == "left" else "left"
        if other in self.calibration and self.calibration[other].index == index:
            raise ValueError("Both arrows matched the same sensor. Release the other arrow and retry.")
        self.calibration[phase] = KeyCalibration(index=index, rest=self.idle[index]["delta"],
                                                pressed=result[index]["delta"], noise=noise)
        label = self.left_info if phase == "left" else self.right_info
        label.set(f"{'← Left' if phase == 'left' else '→ Right'}: sensor {index} · "
                  f"range {magnitude:.0f} raw · observed variation {noise:.0f}")
        if len(self.calibration) == 2:
            self._save()
            self.status.set("Both arrows mapped and saved. Release them, then start preview.")
        else:
            self.status.set(f"{phase.title()} mapped. Release it, then hold {other.upper()} and map it.")

    def _save(self):
        contents = {"version": 1, **{key: asdict(value) for key, value in self.calibration.items()}}
        temporary = SETTINGS.with_suffix(".tmp")
        temporary.write_text(json.dumps(contents, indent=2) + "\n", encoding="utf-8")
        temporary.replace(SETTINGS)

    def _load(self):
        if not SETTINGS.exists():
            return
        try:
            saved = json.loads(SETTINGS.read_text(encoding="utf-8"))
            if not isinstance(saved, dict):
                raise ValueError("Calibration must be a JSON object")
            if saved.get("version") != 1:
                raise ValueError("Unsupported calibration version")
            left = KeyCalibration(**saved["left"])
            right = KeyCalibration(**saved["right"])
            SteeringCalibration(left, right)
            self.calibration = {"left": left, "right": right}
            for name, item in self.calibration.items():
                label = self.left_info if name == "left" else self.right_info
                label.set(f"{'← Left' if name == 'left' else '→ Right'}: sensor {item.index} · saved calibration")
            self.status.set("Saved arrows loaded. Start preview, or scan idle to recalibrate.")
        except (OSError, ValueError, TypeError, KeyError) as error:
            self.status.set(f"Saved calibration could not be loaded: {error}")

    def _draw_steering(self, value):
        canvas = self.steering_canvas
        width = max(canvas.winfo_width(), 200)
        center, extent = width / 2, width / 2 - 20
        canvas.delete("all")
        canvas.create_line(20, 23, width - 20, 23, fill="#34445a", width=4)
        canvas.create_line(center, 13, center, 33, fill=MUTED, width=1)
        position = center + value * extent
        canvas.create_line(center, 23, position, 23, fill=ACCENT, width=5)
        canvas.create_oval(position - 6, 17, position + 6, 29, fill=ACCENT, outline="")
        canvas.create_text(center, 44, text=f"Steering {value:+.0%}", fill=FG, font=("Consolas", 10))

    def _drain(self):
        if self.closing:
            return
        for _ in range(64):
            try:
                kind, value = self.worker.events.get_nowait()
            except queue.Empty:
                break
            try:
                if kind == "connected":
                    self.connection.set(f"{value['product_string']} · USB {value['vendor_id']:04X}:"
                                        f"{value['product_id']:04X} · sensor reads only")
                elif kind == "progress":
                    self.progress["value"] = value
                elif kind == "scan":
                    self._mapped(*value)
                elif kind == "error":
                    self.status_label.configure(fg="#ffb69d")
                    self.status.set(value)
                elif kind == "done":
                    self.busy = False
                    self._buttons()
                    self._draw_steering(0)
                    for name in ("left", "right"):
                        bar, label = getattr(self, name + "_bar")
                        bar["value"] = 0
                        label.configure(text="0%")
                    if self.worker.stop.is_set() and self.status.get().startswith("Stopping"):
                        self.status.set("Stopped. Virtual steering centered and released.")
            except Exception as error:
                self.status_label.configure(fg="#ffb69d")
                self.status.set(str(error))
        try:
            frame = self.worker.live.get_nowait()
        except queue.Empty:
            frame = None
        if frame is not None and self.busy:
            for name in ("left", "right"):
                bar, label = getattr(self, name + "_bar")
                bar["value"] = frame[name]
                label.configure(text=f"{frame[name]:.0%}")
            self._draw_steering(frame["steering"])
            interval = frame["pair_ms"]
            hz = 1000 / interval if interval > 0 else 0
            self.stats.set(f"Request {frame['request_ms']:.2f} ms · pair {interval:.2f} ms "
                           f"({hz:.0f} Hz) · p95 {frame['p95_ms']:.2f} ms")
        self.root.after(40, self._drain)

    def close(self):
        if self.closing:
            return
        self.closing = True
        self.worker.stop.set()
        self.worker.shutdown.set()
        self.status.set("Closing and releasing steering…")
        self._await_close()

    def _await_close(self):
        if self.worker.thread.is_alive():
            self.root.after(40, self._await_close)
        else:
            self.root.destroy()


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
