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

DVD video is de-interlaced only where flagged interlaced, and non-square pixels are corrected to the right aspect ratio. Disc images are read directly (no mounting, no admin rights). A temporary copy of the title is made, so keep free space about equal to the title size.

## Limits

- Copy-protected (still encrypted) discs are not supported – rip/decrypt first.
- Only one title per run (pick another in the *Movie / title* list and run again). Blu-ray playlists that split a movie across several `.m2ts` files are not stitched.
- Subtitles and menus are not included.
