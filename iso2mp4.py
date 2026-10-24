#!/usr/bin/env python3
"""ISO to MP4 - turn a ripped disc image (DVD / Blu-ray .iso/.img) into a high quality MP4.

Three tabs: Upload -> Estimate -> Convert.  Needs ffmpeg (bundled in the .exe build,
auto-downloaded on Windows if missing) and pycdlib (pure Python, reads the image directly,
no mounting or admin rights needed).
"""
import io
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
SAMPLE_MB = 24
HEAD_MB = 32

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
    sizes: list = field(default_factory=list)     # byte size of each entry


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


def parse_mpls(data):
    """Return (clip names in play order, total seconds) from a Blu-ray .mpls playlist."""
    if data[:4] != b"MPLS":
        raise ValueError("not a playlist")
    pos = int.from_bytes(data[8:12], "big") + 10
    count = int.from_bytes(data[pos - 4:pos - 2], "big")
    clips, ticks = [], 0
    for _ in range(count):
        length = int.from_bytes(data[pos:pos + 2], "big")
        clips.append(data[pos + 2:pos + 7].decode("ascii"))
        ticks += int.from_bytes(data[pos + 18:pos + 22], "big") - int.from_bytes(data[pos + 14:pos + 18], "big")
        pos += length + 2
    return clips, ticks / 45000


def _candidates_from_files(iso, files, fs):
    dvd, bd, other, playlists = {}, {}, [], []
    for path, size in files:
        clean = re.sub(r";\d+$", "", path).upper()
        m = re.search(r"/VIDEO_TS/VTS_(\d+)_(\d+)\.VOB$", clean)
        if m and int(m.group(2)) > 0:                      # _0 is the menu
            dvd.setdefault(int(m.group(1)), []).append((int(m.group(2)), path, size))
        elif re.search(r"/BDMV/STREAM/[^/]+\.M2TS$", clean):
            bd[clean] = (path, size)
        elif clean.endswith(".MPLS") and "/BDMV/PLAYLIST/" in clean:
            playlists.append(path)
        elif os.path.splitext(clean)[1].lower() in VIDEO_EXTS and size > 5 << 20:
            other.append((path, size))
    cands = []
    for n, parts in sorted(dvd.items()):
        parts.sort()
        sizes = [p[2] for p in parts]
        cands.append(Candidate(f"DVD title set {n}  ({human_size(sum(sizes))})", sum(sizes), fs,
                               [p[1] for p in parts], sizes))
    seen = set()
    for pl in playlists:                                   # real playlists stitch split movies in order
        try:
            buf = io.BytesIO()
            iso.get_file_from_iso_fp(buf, **{fs: pl})
            clips, seconds = parse_mpls(buf.getvalue())
            items = [bd["/BDMV/STREAM/%s.M2TS" % c.upper()] for c in clips]
        except (ValueError, KeyError, IndexError):
            continue
        if seconds < 180 or tuple(clips) in seen:
            continue
        seen.add(tuple(clips))
        total = sum(i[1] for i in items)
        name = pl.rsplit("/", 1)[-1]
        cands.append(Candidate(f"Blu-ray playlist {name}  ({human_time(seconds)}, {len(clips)} clip(s))",
                               total, fs, [i[0] for i in items], [i[1] for i in items]))
    if not seen:
        for path, size in bd.values():
            if size > 100 << 20:
                cands.append(Candidate(f"Blu-ray {path.rsplit('/', 1)[-1]}  ({human_size(size)})",
                                       size, fs, [path], [size]))
    for path, size in other:
        cands.append(Candidate(f"{path.rsplit('/', 1)[-1]}  ({human_size(size)})", size, fs, [path], [size]))
    cands.sort(key=lambda c: -c.size)                      # biggest (the movie) first
    return cands


def scan_input(path):
    """Return candidate videos for a disc image or plain video file."""
    ext = os.path.splitext(path)[1].lower()
    if ext in VIDEO_EXTS:
        size = os.path.getsize(path)
        return [Candidate(f"{os.path.basename(path)}  ({human_size(size)})", size, "plain", [path], [size])]
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
                cands = _candidates_from_files(iso, list(_list_files(iso, fs)), fs)
            except Exception:
                continue
            if cands:
                return cands
    finally:
        iso.close()
    raise RuntimeError("No video found in this image. Is it a DVD or Blu-ray rip?")


def iter_chunks(image, cand, start=0, length=None, chunk=1 << 20):
    """Stream bytes of a candidate straight out of the image (entries joined end to end)."""
    import pycdlib
    end = cand.size if length is None else min(cand.size, start + length)
    iso = pycdlib.PyCdlib()
    iso.open(image)
    try:
        base = 0
        for entry, size in zip(cand.entries, cand.sizes):
            lo, hi = max(start, base), min(end, base + size)
            if lo < hi:
                with iso.open_file_from_iso(**{cand.fs: entry}) as f:
                    f.seek(lo - base)
                    left = hi - lo
                    while left > 0:
                        data = f.read(min(chunk, left))
                        if not data:
                            break
                        left -= len(data)
                        yield data
            base += size
    finally:
        iso.close()


def dump_range(image, cand, start, length, path):
    """Copy a small slice of the title to a file (used only for probing and speed tests)."""
    with open(path, "wb") as out:
        for data in iter_chunks(image, cand, start, length):
            out.write(data)
    return path


# --------------------------------------------------------------------------- ffmpeg

def probe(media):
    ffprobe = find_tool("ffprobe")
    r = subprocess.run([ffprobe, "-v", "error", "-probesize", "100M", "-analyzeduration", "100M",
                        "-print_format", "json", "-show_format", "-show_streams", media],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                       creationflags=NO_WINDOW)
    if r.returncode != 0:
        raise RuntimeError("Could not read the video:\n" + r.stderr.strip()[-500:])
    info = json.loads(r.stdout)
    video = next((s for s in info["streams"] if s["codec_type"] == "video"), None)
    if not video:
        raise RuntimeError("No video stream found.")
    audio = [s for s in info["streams"] if s["codec_type"] == "audio"]
    subs = [s for s in info["streams"] if s["codec_type"] == "subtitle"]
    duration = float(info["format"].get("duration") or video.get("duration") or 0)
    sar = video.get("sample_aspect_ratio", "1:1")
    try:
        a, b = (float(x) for x in sar.split(":"))
        sar_val = a / b if b else 1.0
    except ValueError:
        sar_val = 1.0
    return dict(duration=duration, width=int(video["width"]), height=int(video["height"]),
                sar=sar_val, codec=video["codec_name"], audio_tracks=len(audio),
                subtitles=[f"{s.get('tags', {}).get('language', '?')} ({s['codec_name']})" for s in subs])


def build_cmd(inp, out, quality, meta, start=None, length=None, sub=None):
    """inp is a path, or 'pipe:0' when the title is streamed straight from the image."""
    preset, crf = QUALITY[quality]
    vf = ["yadif=mode=0:deint=interlaced"]                 # only touches frames flagged interlaced
    if abs(meta["sar"] - 1) > 0.01:                        # DVD anamorphic -> square pixels
        vf.append(f"scale=w='trunc(iw*{meta['sar']:.6f}/2)*2':h=ih:flags=lanczos,setsar=1")
    chain = ",".join(vf)
    cmd = [find_tool("ffmpeg"), "-hide_banner", "-y", "-nostdin",
           "-probesize", "100M", "-analyzeduration", "100M"]
    if start is not None:
        cmd += ["-ss", f"{start:.2f}"]
    cmd += ["-i", inp]
    if length is not None:
        cmd += ["-t", f"{length:.2f}"]
    if sub is None:
        cmd += ["-map", "0:v:0", "-vf", chain]
    else:                                                  # burn the chosen subtitle track into the picture
        cmd += ["-filter_complex", f"[0:v:0][0:s:{sub}]overlay=format=auto[ov];[ov]{chain}[vout]",
                "-map", "[vout]"]
    cmd += ["-map", "0:a?", "-sn", "-dn",
            "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
            "-profile:v", "high", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "320k",
            "-max_muxing_queue_size", "4096", "-movflags", "+faststart",
            "-progress", "pipe:1", "-nostats", out]
    return cmd


def run_ffmpeg(cmd, on_progress, cancel, duration=None, feed=None, feed_total=1):
    """Run ffmpeg and report progress as a 0..1 fraction. With feed (a chunk iterator) the data
    is piped into ffmpeg's stdin and progress is measured in bytes; otherwise by output time."""
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE if feed else None, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, creationflags=NO_WINDOW)   # binary: stdin carries video
    lines = io.TextIOWrapper(proc.stdout, errors="replace")
    tail, feed_err = [], []

    def feeder():
        done = 0
        try:
            for data in feed:
                if cancel.is_set():
                    break
                proc.stdin.write(data)
                done += len(data)
                on_progress(min(done / feed_total, 0.999))
        except (BrokenPipeError, OSError):
            pass                                           # ffmpeg exited; its own error is reported below
        except Exception as e:  # noqa: BLE001
            feed_err.append(e)
            proc.kill()
        finally:
            try:
                proc.stdin.close()
            except OSError:
                pass

    t = threading.Thread(target=feeder, daemon=True) if feed else None
    if t:
        t.start()
    try:
        for line in lines:
            if cancel.is_set():
                proc.kill()
                break
            line = line.strip()
            m = re.match(r"out_time_(?:us|ms)=(\d+)", line)
            if m:
                if duration:
                    on_progress(min(int(m.group(1)) / 1e6 / duration, 1.0))
            elif "=" not in line or " " in line.split("=", 1)[0]:
                tail = (tail + [line])[-15:]
        proc.wait()
    finally:
        if proc.poll() is None:
            proc.kill()
    if cancel.is_set():
        raise Cancelled()
    if feed_err:
        raise feed_err[0]
    if proc.returncode != 0:
        raise RuntimeError("ffmpeg failed:\n" + "\n".join(tail))
    on_progress(1.0)


def benchmark(image, cand, meta, quality, workdir, cancel):
    """Encode a short sample with the real settings.
    Returns seconds-of-video, wall seconds, output bytes and source bytes of the sample."""
    sample = os.path.join(workdir, "sample.mp4")
    if cand.fs == "plain":
        dur = meta["duration"]
        length = min(SAMPLE_SECONDS, dur)
        start = max(0, dur / 2 - length / 2) if dur > 2 * SAMPLE_SECONDS else 0
        t0 = time.monotonic()
        run_ffmpeg(build_cmd(cand.entries[0], sample, quality, meta, start, length), lambda f: None, cancel,
                   duration=length)
        vsec, src = length, length * cand.size / dur
    else:
        want = min(cand.size, SAMPLE_MB << 20)
        start = max(0, (cand.size - want) // 2) // 6144 * 6144   # 6144 = DVD sector x Blu-ray packet
        piece = dump_range(image, cand, start, want, os.path.join(workdir, "sample.bin"))
        t0 = time.monotonic()
        run_ffmpeg(build_cmd(piece, sample, quality, meta), lambda f: None, cancel)
        vsec, src = probe(sample)["duration"], want
        os.remove(piece)
    wall = time.monotonic() - t0
    out = os.path.getsize(sample)
    os.remove(sample)
    if vsec <= 0:
        raise RuntimeError("The speed test produced no video. Is this a playable title?")
    return dict(vsec=vsec, wall=wall, out=out, src=src)


def encode_title(image, cand, out, quality, meta, sub, on_progress, cancel):
    """Convert one title. Disc titles are streamed from the image into ffmpeg, no temporary copy."""
    if cand.fs == "plain":
        cmd = build_cmd(cand.entries[0], out, quality, meta, sub=sub)
        run_ffmpeg(cmd, on_progress, cancel, duration=meta["duration"])
    else:
        cmd = build_cmd("pipe:0", out, quality, meta, sub=sub)
        run_ffmpeg(cmd, on_progress, cancel, feed=iter_chunks(image, cand), feed_total=cand.size)


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
    root.geometry("720x560")
    root.minsize(640, 520)
    ttk.Style().configure("Big.TButton", font=("Segoe UI", 12, "bold"), padding=10)
    ttk.Style().configure("Head.TLabel", font=("Segoe UI", 15, "bold"))

    ui = queue.Queue()                    # callables to run on the Tk thread
    cancel = threading.Event()
    state = dict(image=None, cands=[], jobs=[], meta=None, bench=None, tmp=None)

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
              foreground="#555").pack(anchor="w", pady=(0, 10))
    ttk.Button(t1, text="📂  Upload disc image…", style="Big.TButton",
               command=lambda: pick_image()).pack(anchor="w")
    file_var = tk.StringVar(value="No file chosen")
    ttk.Label(t1, textvariable=file_var, wraplength=640).pack(anchor="w", pady=(8, 10))

    ttk.Label(t1, text="Titles to convert (Ctrl/Shift-click for several):").pack(anchor="w")
    lb_frame = ttk.Frame(t1)
    lb_frame.pack(fill="x")
    title_lb = tk.Listbox(lb_frame, selectmode="extended", height=5, exportselection=False)
    title_lb.pack(side="left", fill="x", expand=True)
    ttk.Button(lb_frame, text="Select all", command=lambda: title_lb.select_set(0, "end")
               ).pack(side="left", padx=8, anchor="n")

    form = ttk.Frame(t1)
    form.pack(fill="x", pady=8)
    form.columnconfigure(1, weight=1)
    out_var = tk.StringVar()
    qual_var = tk.StringVar(value=next(iter(QUALITY)))
    ttk.Label(form, text="Quality:").grid(row=0, column=0, sticky="w", pady=4)
    ttk.Combobox(form, textvariable=qual_var, values=list(QUALITY), state="readonly"
                 ).grid(row=0, column=1, columnspan=2, sticky="ew", padx=8)
    ttk.Label(form, text="Save MP4 as:").grid(row=1, column=0, sticky="w", pady=4)
    ttk.Entry(form, textvariable=out_var).grid(row=1, column=1, sticky="ew", padx=8)
    ttk.Button(form, text="Browse…", command=lambda: pick_output()).grid(row=1, column=2)
    next_btn = ttk.Button(t1, text="Next ▸  Estimate time", style="Big.TButton",
                          state="disabled", command=lambda: start_estimate())
    next_btn.pack(side="bottom", anchor="e")
    ttk.Label(t1, text="Several titles are saved as 'name - title 1.mp4', 'name - title 2.mp4'… "
                       "Copy-protected discs must be decrypted first.",
              foreground="#777", wraplength=640).pack(side="bottom", anchor="w", pady=6)

    def pick_image():
        p = filedialog.askopenfilename(title="Choose disc image", filetypes=[
            ("Disc image or video", "*.iso *.img *.bin *.mp4 *.mkv *.avi *.mov *.m2ts *.ts *.vob *.mpg *.mpeg"),
            ("All files", "*.*")])
        if not p:
            return
        state["image"] = p
        file_var.set(f"{p}\nReading disc…")
        next_btn.config(state="disabled")
        title_lb.delete(0, "end")
        result = {}

        def job():
            result["c"] = scan_input(p)
            post(done)

        def done():
            state["cands"] = result["c"]
            file_var.set(f"{p}\n({human_size(os.path.getsize(p))})")
            for c in state["cands"]:
                title_lb.insert("end", c.label)
            title_lb.select_set(0)
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
    est_grid = ttk.Frame(t2)
    est_grid.pack(anchor="w", pady=10)
    est_vars = {k: tk.StringVar() for k in ("Titles", "Source", "Estimated time", "Estimated size", "Output")}
    for r, (k, v) in enumerate(est_vars.items()):
        ttk.Label(est_grid, text=k + ":", font=("Segoe UI", 12, "bold")).grid(row=r, column=0, sticky="nw", padx=(0, 14))
        ttk.Label(est_grid, textvariable=v, font=("Segoe UI", 12), wraplength=480, justify="left"
                  ).grid(row=r, column=1, sticky="w")
    sub_var = tk.StringVar()
    sub_row = ttk.Frame(t2)
    ttk.Label(sub_row, text="Subtitles (burned in):").pack(side="left")
    sub_box = ttk.Combobox(sub_row, textvariable=sub_var, state="readonly", width=40)
    sub_box.pack(side="left", padx=8)
    est_btns = ttk.Frame(t2)
    est_btns.pack(side="bottom", fill="x")
    ttk.Button(est_btns, text="◂ Back", command=lambda: back_to_upload()).pack(side="left")
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

    def output_paths(n):
        out = out_var.get()
        if n == 1:
            return [out]
        base, ext = os.path.splitext(out)
        return [unique_path(f"{base} - title {i + 1}{ext}") for i in range(n)]

    def start_estimate():
        ok = ensure_ffmpeg()
        if not ok:
            return
        sel = list(title_lb.curselection())
        if not sel:
            messagebox.showinfo(APP_NAME, "Select at least one title.")
            return
        cancel.clear()
        cleanup_tmp()
        state["tmp"] = tempfile.mkdtemp(prefix="iso2mp4_")
        jobs = [state["cands"][i] for i in sel]
        state["jobs"] = jobs
        quality = qual_var.get()
        for v in est_vars.values():
            v.set("")
        sub_row.pack_forget()
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
            main = jobs[0]
            post(lambda: setp("Analyzing video…"))
            if main.fs == "plain":
                meta = probe(main.entries[0])
                if meta["duration"] <= 0:
                    raise RuntimeError("Could not determine the video length.")
            else:
                head = dump_range(state["image"], main, 0, min(main.size, HEAD_MB << 20),
                                  os.path.join(state["tmp"], "head.bin"))
                meta = probe(head)
                os.remove(head)
            post(lambda: setp("Testing encode speed on a short sample…"))
            bench = benchmark(state["image"], main, meta, quality, state["tmp"], cancel)
            state.update(meta=meta, bench=bench)
            post(show_estimate)

        def show_estimate():
            meta, b = state["meta"], state["bench"]
            total = sum(c.size for c in jobs)
            secs = total * b["vsec"] / b["src"]            # video length, all selected titles
            wall = total * b["wall"] / b["src"]
            size = total * b["out"] / b["src"]
            est_bar.stop()
            est_bar.config(mode="determinate", value=100)
            est_status.set("Ready. Here is what to expect:")
            outs = output_paths(len(jobs))
            est_vars["Titles"].set(f"{len(jobs)}  (video length about {human_time(secs)})")
            est_vars["Source"].set(f"{meta['width']}×{meta['height']} ({meta['codec']}), "
                                   f"{meta['audio_tracks']} audio track(s)")
            est_vars["Estimated time"].set(f"about {human_time(wall)}")
            est_vars["Estimated size"].set(f"about {human_size(size)}")
            est_vars["Output"].set(outs[0] + (f"  (+{len(outs) - 1} more)" if len(outs) > 1 else ""))
            choices = ["None"] + [f"Track {i + 1}: {s}" for i, s in enumerate(meta["subtitles"])]
            sub_box.config(values=choices)
            sub_box.current(0)
            sub_row.pack(anchor="w", pady=6)
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
        title_lb.delete(0, "end")
        next_btn.config(state="disabled")
        goto(0)

    def start_convert():
        jobs, meta = state["jobs"], state["meta"]
        outs = output_paths(len(jobs))
        sub = sub_box.current() - 1 if sub_box.current() > 0 else None
        quality = qual_var.get()
        total = sum(c.size for c in jobs)
        cancel.clear()
        cancel_btn.pack(side="left")
        folder_btn.pack_forget()
        again_btn.pack_forget()
        conv_bar.config(value=0)
        conv_info.set("")
        goto(2)
        started = time.monotonic()
        finished = []

        def make_cb(i, before):
            def on_progress(frac):
                overall = (before + frac * jobs[i].size) / total
                elapsed = time.monotonic() - started
                eta = elapsed / overall * (1 - overall) if overall > 0.01 else None
                post(lambda: (conv_bar.config(value=overall * 100), conv_status.set(
                    f"Converting title {i + 1} of {len(jobs)}… keep this window open."), conv_info.set(
                    f"{overall:.1%} done\nElapsed: {human_time(elapsed)}\n"
                    f"Remaining: {human_time(eta) if eta is not None else 'calculating…'}")))
            return on_progress

        def job():
            before = 0
            for i, (cand, out) in enumerate(zip(jobs, outs)):
                part = out + ".partial.mp4"
                try:
                    m = meta if i == 0 else (probe(cand.entries[0]) if cand.fs == "plain" else meta)
                    encode_title(state["image"], cand, part, quality, m, sub, make_cb(i, before), cancel)
                    os.replace(part, out)
                except BaseException:
                    Path(part).unlink(missing_ok=True)
                    raise
                finished.append(out)
                before += cand.size
            post(done)

        def done():
            cleanup_tmp()
            conv_bar.config(value=100)
            conv_status.set("✅ Finished!")
            names = "\n".join(finished)
            conv_info.set(f"Saved:\n{names}\n\nTotal size: "
                          f"{human_size(sum(os.path.getsize(f) for f in finished))}\n"
                          f"Took: {human_time(time.monotonic() - started)}")
            cancel_btn.pack_forget()
            folder_btn.pack(side="left")
            again_btn.pack(side="right")

        def err(msg):
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
