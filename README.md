# ISO to MP4

Turn a ripped DVD / Blu-ray image (`.iso`, `.img`) into a high quality `.mp4` with three simple tabs:

1. **Upload** – pick your disc image (the biggest title, i.e. the movie, is selected automatically), choose quality and where to save.
2. **Estimate** – the app reads the movie off the image, then encodes a short sample with your real settings to predict the **time and file size**.
3. **Convert** – live progress, elapsed and remaining time. Cancel any time.

## Run it

**Windows exe (easiest):** the *Build Windows exe* GitHub Action produces `ISO-to-MP4.exe` (ffmpeg bundled, nothing to install) – download it from the Actions run's artifacts. Or build locally by double-clicking `build_exe.bat` (needs Python); the exe lands in `dist\`.

**Without building:** double-click `run.bat` (Windows) or run `pip install -r requirements.txt && python3 iso2mp4.py`. On first run the Windows version offers to download ffmpeg itself; on macOS/Linux install ffmpeg yourself (`brew install ffmpeg` / `apt install ffmpeg`). Linux also needs `python3-tk`.

## Quality settings

H.264 (x264) `High` profile, yuv420p, `+faststart`, AAC 320 kbps audio (all audio tracks kept).

| Setting | x264 preset | CRF |
|---|---|---|
| Highest | slow | 14 (visually lossless) |
| High | medium | 18 |
| Quick | veryfast | 21 |

DVD video is de-interlaced only where flagged interlaced, and non-square pixels are corrected to the right aspect ratio.

## Behind the scenes: the pass-through pipeline

Disc images are read directly (no mounting, no admin rights) and the title is **streamed from the image straight into ffmpeg** – no temporary full copy, so no extra disk space is needed. The same pipeline gives you:

- **Several titles at once** – Ctrl/Shift-click titles (or *Select all*); they are queued and saved as `name - title 1.mp4`, `name - title 2.mp4`…
- **Blu-ray playlists** – movies split across several `.m2ts` files are stitched in the correct order using the disc's own playlist (`.mpls`) files.
- **Subtitles** – pick a detected track on the Estimate tab and it is burned into the picture (works for DVD and Blu-ray bitmap subtitles).
- **Estimates without extracting** – speed and size come from a short sample cut from the middle of the title.

## Limits

- Copy-protected (still encrypted) discs are not supported – this tool doesn't bypass disc encryption, so use an image that is already decrypted.
- Menus and extras beyond the listed titles are not included. 

## Test results

Checked on generated disc images (DVD title sets with a bitmap subtitle track, a two-clip Blu-ray playlist, a two-title DVD), driving the real GUI. Pictures are in `docs/test-results/`: converted frames (`frame_*.png`, including burned-in subtitle and the second stitched Blu-ray clip) and GUI screenshots (`gui_*.png`). Estimates came out close to reality (e.g. predicted 15 s / 2.7 MB, actual 20 s / 2.4 MB).
