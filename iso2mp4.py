#!/usr/bin/env python3
"""ISO to MP4 - turn a ripped disc image (DVD / Blu-ray .iso/.img) into a high quality MP4.

Three tabs: Upload -> Estimate -> Convert.  Needs ffmpeg (bundled in the .exe build,
auto-downloaded on Windows if missing) and pycdlib (pure Python, reads the image directly,
no mounting or admin rights needed).
"""
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

APP_NAME = "ISO to MP4"
IMAGE_EXTS = {".iso", ".img", ".bin"}
VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".m2ts", ".ts", ".vob", ".mpg", ".mpeg", ".wmv", ".m4v"}
FFMPEG_URL = ("https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/"
              "ffmpeg-master-latest-win64-gpl.zip")
USER_BIN = Path.home() / ".iso2mp4" / "bin"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
SAMPLE_SECONDS = 20

# label -> (x264 preset, crf)
QUALITY = {
    "Highest (slow, near-lossless)": ("slow", 14),
    "High (balanced)": ("medium", 18),
    "Quick (fast, good)": ("veryfast", 21),
}


class Cancelled(Exception):
    pass


# --------------------------------------------------------------------------- tools

def _exe(name):
    return name + (".exe" if os.name == "nt" else "")


def find_tool(name):
    """Look next to the program, inside the PyInstaller bundle, in ~/.iso2mp4/bin, then PATH."""
    here = Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve().parent
    dirs = [here, USER_BIN]
    if getattr(sys, "_MEIPASS", None):
        dirs.insert(0, Path(sys._MEIPASS))
    for d in dirs:
        p = d / _exe(name)
        if p.exists():
            return str(p)
    return shutil.which(name)


def download_ffmpeg(progress):
    """Windows only: fetch a static ffmpeg build into ~/.iso2mp4/bin. progress(done, total)."""
    USER_BIN.mkdir(parents=True, exist_ok=True)
    zpath = USER_BIN / "ffmpeg.zip"
    with urllib.request.urlopen(FFMPEG_URL, timeout=60) as r, open(zpath, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        while chunk := r.read(1 << 20):
            f.write(chunk)
            done += len(chunk)
            progress(done, total)
    with zipfile.ZipFile(zpath) as z:
        for member in z.namelist():
            base = member.rsplit("/", 1)[-1]
            if member.split("/")[-2:-1] == ["bin"] and base in ("ffmpeg.exe", "ffprobe.exe"):
                with z.open(member) as src, open(USER_BIN / base, "wb") as dst:
                    shutil.copyfileobj(src, dst)
    zpath.unlink(missing_ok=True)


# --------------------------------------------------------------------------- disc scanning

@dataclass
class Candidate:
    label: str
    size: int
    fs: str                      # 'iso_path' | 'udf_path' | 'joliet_path' | 'plain'
    entries: list = field(default_factory=list)   # paths inside the image (or the file itself)


def human_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024


def human_time(sec):
    sec = max(0, int(sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m:02d}m" if h else (f"{m}m {s:02d}s" if m else f"{s}s")


def _list_files(iso, fs):
    """Yield (path, size) for every file using one of pycdlib's filesystem views."""
    for dirpath, _dirs, files in iso.walk(**{fs: "/"}):
        for name in files:
            path = dirpath.rstrip("/") + "/" + name
            rec = iso.get_record(**{fs: path})
            size = rec.get_data_length() if hasattr(rec, "get_data_length") else rec.data_length
            yield path, size


def _candidates_from_files(files, fs):
    dvd, bd, other = {}, [], []
    for path, size in files:
        clean = re.sub(r";\d+$", "", path).upper()
        m = re.search(r"/VIDEO_TS/VTS_(\d+)_(\d+)\.VOB$", clean)
        if m and int(m.group(2)) > 0:                      # _0 is the menu
            dvd.setdefault(int(m.group(1)), []).append((int(m.group(2)), path, size))
        elif re.search(r"/BDMV/STREAM/[^/]+\.M2TS$", clean):
            bd.append((path, size))
        elif os.path.splitext(clean)[1].lower() in VIDEO_EXTS and size > 5 << 20:
            other.append((path, size))
    cands = []
    for n, parts in sorted(dvd.items()):
        parts.sort()
        total = sum(p[2] for p in parts)
        cands.append(Candidate(f"DVD title set {n}  ({human_size(total)})", total, fs, [p[1] for p in parts]))
    for path, size in bd:
        if size > 100 << 20:
            cands.append(Candidate(f"Blu-ray {path.rsplit('/', 1)[-1]}  ({human_size(size)})", size, fs, [path]))
    for path, size in other:
        cands.append(Candidate(f"{path.rsplit('/', 1)[-1]}  ({human_size(size)})", size, fs, [path]))
    cands.sort(key=lambda c: -c.size)                      # biggest (the movie) first
    return cands


def scan_input(path):
    """Return candidate videos for a disc image or plain video file."""
    ext = os.path.splitext(path)[1].lower()
    if ext in VIDEO_EXTS:
        size = os.path.getsize(path)
        return [Candidate(f"{os.path.basename(path)}  ({human_size(size)})", size, "plain", [path])]
    import pycdlib
    iso = pycdlib.PyCdlib()
    iso.open(path)
    try:
        views = ["iso_path"]
        if iso.has_udf():
            views.append("udf_path")
        if iso.has_joliet():
            views.append("joliet_path")
        views = views[1:] + views[:1] if len(views) > 1 else views  # UDF first: Blu-ray is UDF only
        for fs in views:
            try:
                cands = _candidates_from_files(list(_list_files(iso, fs)), fs)
            except Exception:
                continue
            if cands:
                return cands
    finally:
        iso.close()
    raise RuntimeError("No video found in this image. Is it a DVD or Blu-ray rip?")


class _ProgressWriter:
    """File wrapper that reports bytes written and aborts when cancelled."""

    def __init__(self, fp, total, progress, cancel):
        self.fp, self.total, self.progress, self.cancel, self.done = fp, max(total, 1), progress, cancel, 0

    def write(self, data):
        if self.cancel.is_set():
            raise Cancelled()
        self.fp.write(data)
        self.done += len(data)
        self.progress(min(self.done / self.total, 1.0))


def extract(image, cand, dest_dir, progress, cancel):
    """Copy the chosen title out of the image into one file. progress(fraction)."""
    if cand.fs == "plain":
        return cand.entries[0]
    import pycdlib
    free = shutil.disk_usage(dest_dir).free
    if free < cand.size * 1.05:
        raise RuntimeError(f"Not enough free disk space for the temporary copy: "
                           f"need {human_size(cand.size)}, have {human_size(free)}.")
    ext = ".m2ts" if "M2TS" in cand.entries[0].upper() else ".vob"
    out_path = os.path.join(dest_dir, "title" + ext)
    iso = pycdlib.PyCdlib()
    iso.open(image)
    try:
        with open(out_path, "wb") as out:
            writer = _ProgressWriter(out, cand.size, progress, cancel)
            for entry in cand.entries:
                iso.get_file_from_iso_fp(writer, blocksize=1 << 20, **{cand.fs: entry})
    finally:
        iso.close()
    return out_path


# --------------------------------------------------------------------------- ffmpeg

def _run_kwargs():
    return dict(stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                errors="replace", creationflags=NO_WINDOW)


def probe(media):
    ffprobe = find_tool("ffprobe")
    r = subprocess.run([ffprobe, "-v", "error", "-print_format", "json",
                        "-show_format", "-show_streams", media],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                       creationflags=NO_WINDOW)
    if r.returncode != 0:
        raise RuntimeError("Could not read the video:\n" + r.stderr.strip()[-500:])
    info = json.loads(r.stdout)
    video = next((s for s in info["streams"] if s["codec_type"] == "video"), None)
    if not video:
        raise RuntimeError("No video stream found.")
    audio = [s for s in info["streams"] if s["codec_type"] == "audio"]
    duration = float(info["format"].get("duration") or video.get("duration") or 0)
    if duration <= 0:
        raise RuntimeError("Could not determine the video length.")
    sar = video.get("sample_aspect_ratio", "1:1")
    try:
        a, b = (float(x) for x in sar.split(":"))
        sar_val = a / b if b else 1.0
    except ValueError:
        sar_val = 1.0
    return dict(duration=duration, width=int(video["width"]), height=int(video["height"]),
                sar=sar_val, codec=video["codec_name"], audio_tracks=len(audio))


def build_cmd(media, out, quality, meta, start=None, length=None):
    preset, crf = QUALITY[quality]
    vf = ["yadif=mode=0:deint=interlaced"]                 # only touches frames flagged interlaced
    if abs(meta["sar"] - 1) > 0.01:                        # DVD anamorphic -> square pixels
        vf.append(f"scale=w='trunc(iw*{meta['sar']:.6f}/2)*2':h=ih:flags=lanczos,setsar=1")
    cmd = [find_tool("ffmpeg"), "-hide_banner", "-y", "-nostdin"]
    if start is not None:
        cmd += ["-ss", f"{start:.2f}"]
    cmd += ["-i", media]
    if length is not None:
        cmd += ["-t", f"{length:.2f}"]
    cmd += ["-map", "0:v:0", "-map", "0:a?", "-sn", "-dn",
            "-vf", ",".join(vf),
            "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
            "-profile:v", "high", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "320k",
            "-max_muxing_queue_size", "4096", "-movflags", "+faststart",
            "-progress", "pipe:1", "-nostats", out]
    return cmd


def run_ffmpeg(cmd, duration, on_progress, cancel):
    """Run ffmpeg, report (fraction, speed) and return when done. Raises on failure."""
    proc = subprocess.Popen(cmd, **_run_kwargs())
    tail = []
    try:
        for line in proc.stdout:
            if cancel.is_set():
                proc.kill()
                raise Cancelled()
            line = line.strip()
            m = re.match(r"out_time_(?:us|ms)=(\d+)", line)
            if m:
                on_progress(min(int(m.group(1)) / 1e6 / duration, 1.0))
            elif "=" not in line or " " in line.split("=", 1)[0]:
                tail = (tail + [line])[-15:]
        proc.wait()
    finally:
        if proc.poll() is None:
            proc.kill()
    if proc.returncode != 0:
        raise RuntimeError("ffmpeg failed:\n" + "\n".join(tail))


def benchmark(media, meta, quality, workdir, cancel):
    """Encode a short sample with the real settings -> (seconds per video second, bytes per video second)."""
    dur = meta["duration"]
    length = min(SAMPLE_SECONDS, dur)
    start = max(0, dur / 2 - length / 2) if dur > 2 * SAMPLE_SECONDS else 0
    sample = os.path.join(workdir, "sample.mp4")
    t0 = time.monotonic()
    run_ffmpeg(build_cmd(media, sample, quality, meta, start, length), length, lambda f: None, cancel)
    wall = time.monotonic() - t0
    size = os.path.getsize(sample)
    os.remove(sample)
    return wall / length, size / length


def unique_path(path):
    base, ext = os.path.splitext(path)
    n = 1
    while os.path.exists(path):
        path = f"{base} ({n}){ext}"
        n += 1
    return path


# --------------------------------------------------------------------------- GUI

def main():
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    root = tk.Tk()
    root.title(APP_NAME)
    root.geometry("720x520")
    root.minsize(640, 480)
    ttk.Style().configure("Big.TButton", font=("Segoe UI", 12, "bold"), padding=10)
    ttk.Style().configure("Head.TLabel", font=("Segoe UI", 15, "bold"))

    ui = queue.Queue()                    # callables to run on the Tk thread
    cancel = threading.Event()
    state = dict(image=None, cands=[], media=None, meta=None, rate=None, tmp=None, busy=False)

    def pump():
        while True:
            try:
                ui.get_nowait()()
            except queue.Empty:
                break
        root.after(80, pump)

    def post(fn):
        ui.put(fn)

    def work(fn, on_error=None):
        def runner():
            try:
                fn()
            except Cancelled:
                post(lambda: on_error and on_error(None))
            except Exception as e:  # noqa: BLE001 - show every failure to the user
                msg = str(e) or repr(e)
                post(lambda: on_error(msg) if on_error else messagebox.showerror(APP_NAME, msg))
        threading.Thread(target=runner, daemon=True).start()

    nb = ttk.Notebook(root)
    nb.pack(fill="both", expand=True, padx=10, pady=10)
    tabs = []
    for title in ("1 · Upload", "2 · Estimate", "3 · Convert"):
        f = ttk.Frame(nb, padding=18)
        nb.add(f, text=title)
        tabs.append(f)

    def goto(i):
        for j in range(3):
            nb.tab(j, state="normal" if j == i else "disabled")
        nb.select(i)

    def cleanup_tmp():
        if state["tmp"]:
            shutil.rmtree(state["tmp"], ignore_errors=True)
            state["tmp"] = None

    # ---- tab 1: upload
    t1 = tabs[0]
    ttk.Label(t1, text="Choose your ripped disc image", style="Head.TLabel").pack(anchor="w")
    ttk.Label(t1, text="DVD or Blu-ray .iso / .img (or any video file).",
              foreground="#555").pack(anchor="w", pady=(0, 12))
    ttk.Button(t1, text="📂  Upload disc image…", style="Big.TButton",
               command=lambda: pick_image()).pack(anchor="w")
    file_var = tk.StringVar(value="No file chosen")
    ttk.Label(t1, textvariable=file_var, wraplength=640).pack(anchor="w", pady=(8, 14))

    form = ttk.Frame(t1)
    form.pack(fill="x")
    form.columnconfigure(1, weight=1)
    title_var, out_var = tk.StringVar(), tk.StringVar()
    qual_var = tk.StringVar(value=next(iter(QUALITY)))
    ttk.Label(form, text="Movie / title:").grid(row=0, column=0, sticky="w", pady=4)
    title_box = ttk.Combobox(form, textvariable=title_var, state="disabled")
    title_box.grid(row=0, column=1, columnspan=2, sticky="ew", padx=8)
    ttk.Label(form, text="Quality:").grid(row=1, column=0, sticky="w", pady=4)
    ttk.Combobox(form, textvariable=qual_var, values=list(QUALITY), state="readonly"
                 ).grid(row=1, column=1, columnspan=2, sticky="ew", padx=8)
    ttk.Label(form, text="Save MP4 as:").grid(row=2, column=0, sticky="w", pady=4)
    ttk.Entry(form, textvariable=out_var).grid(row=2, column=1, sticky="ew", padx=8)
    ttk.Button(form, text="Browse…", command=lambda: pick_output()).grid(row=2, column=2)
    next_btn = ttk.Button(t1, text="Next ▸  Estimate time", style="Big.TButton",
                          state="disabled", command=lambda: start_estimate())
    next_btn.pack(side="bottom", anchor="e")
    ttk.Label(t1, text="Tip: 'Highest' keeps all detail but takes longest; DVD/Blu-ray copy protection "
                       "is not removed.", foreground="#777", wraplength=640).pack(side="bottom", anchor="w", pady=6)

    def pick_image():
        p = filedialog.askopenfilename(title="Choose disc image", filetypes=[
            ("Disc image or video", "*.iso *.img *.bin *.mp4 *.mkv *.avi *.mov *.m2ts *.ts *.vob *.mpg *.mpeg"),
            ("All files", "*.*")])
        if not p:
            return
        state["image"] = p
        file_var.set(f"{p}\nReading disc…")
        next_btn.config(state="disabled")
        title_box.config(state="disabled")
        result = {}

        def job():
            result["c"] = scan_input(p)
            post(done)

        def done():
            state["cands"] = result["c"]
            file_var.set(f"{p}\n({human_size(os.path.getsize(p))})")
            title_box.config(values=[c.label for c in state["cands"]], state="readonly")
            title_box.current(0)
            out_var.set(unique_path(str(Path(p).with_suffix(".mp4"))))
            next_btn.config(state="normal")

        def err(msg):
            file_var.set(f"{p}\n⚠ could not read this file")
            if msg:
                messagebox.showerror(APP_NAME, msg)
        work(job, err)

    def pick_output():
        p = filedialog.asksaveasfilename(title="Save MP4 as", defaultextension=".mp4",
                                         initialfile=os.path.basename(out_var.get()),
                                         filetypes=[("MP4 video", "*.mp4")])
        if p:
            out_var.set(p)

    # ---- tab 2: estimate
    t2 = tabs[1]
    ttk.Label(t2, text="Estimate", style="Head.TLabel").pack(anchor="w")
    est_status = tk.StringVar()
    ttk.Label(t2, textvariable=est_status, wraplength=640).pack(anchor="w", pady=(8, 4))
    est_bar = ttk.Progressbar(t2, mode="determinate", maximum=100)
    est_bar.pack(fill="x", pady=4)
    est_info = tk.StringVar()
    ttk.Label(t2, textvariable=est_info, font=("Segoe UI", 12), justify="left").pack(anchor="w", pady=14)
    est_btns = ttk.Frame(t2)
    est_btns.pack(side="bottom", fill="x")
    est_back = ttk.Button(est_btns, text="◂ Back", command=lambda: back_to_upload())
    est_back.pack(side="left")
    start_btn = ttk.Button(est_btns, text="Start converting ▸", style="Big.TButton",
                           state="disabled", command=lambda: start_convert())
    start_btn.pack(side="right")

    def back_to_upload():
        cancel.set()
        cleanup_tmp()
        goto(0)

    def ensure_ffmpeg():
        if find_tool("ffmpeg") and find_tool("ffprobe"):
            return True
        if os.name == "nt" and messagebox.askyesno(
                APP_NAME, "ffmpeg (the video engine) was not found.\n\n"
                          "Download it now? (about 150 MB, one time only)"):
            return "download"
        messagebox.showerror(APP_NAME, "ffmpeg is required. Install it and make sure "
                                       "'ffmpeg' and 'ffprobe' are on your PATH.")
        return False

    def start_estimate():
        ok = ensure_ffmpeg()
        if not ok:
            return
        cancel.clear()
        cleanup_tmp()
        state["tmp"] = tempfile.mkdtemp(prefix="iso2mp4_")
        cand = state["cands"][title_box.current()]
        quality = qual_var.get()
        est_info.set("")
        est_bar.config(mode="determinate", value=0)
        start_btn.config(state="disabled")
        goto(1)

        def setp(text, pct=None):
            est_status.set(text)
            if pct is None:
                est_bar.config(mode="indeterminate")
                est_bar.start(12)
            else:
                est_bar.stop()
                est_bar.config(mode="determinate", value=pct)

        def job():
            if ok == "download":
                post(lambda: setp("Downloading ffmpeg (one time)…", 0))
                download_ffmpeg(lambda d, t: post(lambda: setp(
                    f"Downloading ffmpeg… {human_size(d)}", 100 * d / t if t else None)))
            if cand.fs != "plain":
                post(lambda: setp("Reading the movie off the disc image…", 0))
                media = extract(state["image"], cand, state["tmp"],
                                lambda f: post(lambda: setp(
                                    f"Reading the movie off the disc image… {f:.0%}", f * 100)), cancel)
            else:
                media = cand.entries[0]
            post(lambda: setp("Analyzing video…"))
            meta = probe(media)
            post(lambda: setp("Testing encode speed on a short sample…"))
            spv, bpv = benchmark(media, meta, quality, state["tmp"], cancel)
            state.update(media=media, meta=meta, rate=(spv, bpv))
            post(show_estimate)

        def show_estimate():
            meta, (spv, bpv) = state["meta"], state["rate"]
            est_bar.stop()
            est_bar.config(mode="determinate", value=100)
            est_status.set("Ready. Here is what to expect:")
            est_info.set(
                f"Movie length:      {human_time(meta['duration'])}\n"
                f"Source:                 {meta['width']}×{meta['height']} ({meta['codec']}), "
                f"{meta['audio_tracks']} audio track(s)\n"
                f"Estimated time:   about {human_time(spv * meta['duration'])}\n"
                f"Estimated size:    about {human_size(bpv * meta['duration'])}\n\n"
                f"Output: {out_var.get()}")
            start_btn.config(state="normal")

        def err(msg):
            est_bar.stop()
            est_status.set("Stopped." if msg is None else "Something went wrong.")
            cleanup_tmp()
            if msg:
                messagebox.showerror(APP_NAME, msg)
            goto(0)
        work(job, err)

    # ---- tab 3: convert
    t3 = tabs[2]
    ttk.Label(t3, text="Converting", style="Head.TLabel").pack(anchor="w")
    conv_status = tk.StringVar()
    ttk.Label(t3, textvariable=conv_status, wraplength=640).pack(anchor="w", pady=(8, 4))
    conv_bar = ttk.Progressbar(t3, mode="determinate", maximum=100)
    conv_bar.pack(fill="x", pady=4)
    conv_info = tk.StringVar()
    ttk.Label(t3, textvariable=conv_info, font=("Segoe UI", 12), justify="left").pack(anchor="w", pady=14)
    conv_btns = ttk.Frame(t3)
    conv_btns.pack(side="bottom", fill="x")
    cancel_btn = ttk.Button(conv_btns, text="Cancel", command=lambda: cancel.set())
    cancel_btn.pack(side="left")
    folder_btn = ttk.Button(conv_btns, text="Open folder", command=lambda: open_folder())
    again_btn = ttk.Button(conv_btns, text="Convert another", command=lambda: reset())

    def open_folder():
        folder = os.path.dirname(out_var.get())
        if os.name == "nt":
            os.startfile(folder)  # noqa: S606
        else:
            subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", folder])

    def reset():
        folder_btn.pack_forget()
        again_btn.pack_forget()
        file_var.set("No file chosen")
        next_btn.config(state="disabled")
        goto(0)

    def start_convert():
        out = out_var.get()
        final_tmp = out + ".partial.mp4"
        cancel.clear()
        cancel_btn.pack(side="left")
        folder_btn.pack_forget()
        again_btn.pack_forget()
        conv_bar.config(value=0)
        conv_status.set("Converting… keep this window open.")
        conv_info.set("")
        goto(2)
        meta, started = state["meta"], time.monotonic()

        def on_progress(frac):
            elapsed = time.monotonic() - started
            eta = elapsed / frac * (1 - frac) if frac > 0.01 else None
            post(lambda: (conv_bar.config(value=frac * 100), conv_info.set(
                f"{frac:.1%} done\nElapsed: {human_time(elapsed)}\n"
                f"Remaining: {human_time(eta) if eta is not None else 'calculating…'}")))

        def job():
            cmd = build_cmd(state["media"], final_tmp, qual_var.get(), meta)
            run_ffmpeg(cmd, meta["duration"], on_progress, cancel)
            os.replace(final_tmp, out)
            post(done)

        def done():
            cleanup_tmp()
            conv_bar.config(value=100)
            conv_status.set("✅ Finished!")
            conv_info.set(f"Saved to:\n{out}\n\nSize: {human_size(os.path.getsize(out))}\n"
                          f"Took: {human_time(time.monotonic() - started)}")
            cancel_btn.pack_forget()
            folder_btn.pack(side="left")
            again_btn.pack(side="right")

        def err(msg):
            Path(final_tmp).unlink(missing_ok=True)
            cleanup_tmp()
            conv_status.set("Cancelled." if msg is None else "Something went wrong.")
            if msg:
                messagebox.showerror(APP_NAME, msg)
            goto(0)
        work(job, err)

    def on_close():
        if nb.index("current") == 2 and cancel_btn.winfo_ismapped():
            if not messagebox.askyesno(APP_NAME, "Conversion is still running. Quit and discard it?"):
                return
        cancel.set()
        cleanup_tmp()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    goto(0)
    pump()
    root.mainloop()


if __name__ == "__main__":
    main()
