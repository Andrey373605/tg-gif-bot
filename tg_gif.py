#!/usr/bin/env python3
"""Turn images, GIFs and videos into multi-frame GIFs that Telegram saves to "My GIFs".

A single-frame GIF or a picture is sent by Telegram as a photo and never shows up
in saved GIFs. So a static input gets expanded into many frames; one corner pixel
flickers between two near-identical colors so encoders can't merge the frames back
into one (invisible to the eye).

Usage:
    python tg_gif.py picture.png anim.gif clip.webm ...
    python tg_gif.py -d 4 -s 480 cat.jpg -o out/
"""
import argparse
import subprocess
import sys
from pathlib import Path

import imageio_ffmpeg
from PIL import Image, ImageSequence

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()


def open_image(path):
    try:
        im = Image.open(path)
        im.load()
        return im
    except Exception:
        return None  # not an image Pillow can read (e.g. a video), ffmpeg will handle it


def fit(im, max_side):
    im = im.convert("RGB")
    im.thumbnail((max_side, max_side), Image.LANCZOS)
    return im


def nearest_other_index(palette, idx):
    """Palette index whose color is closest to palette[idx], but not idx itself."""
    r, g, b = palette[idx * 3: idx * 3 + 3]
    best, best_d = None, None
    for i in range(len(palette) // 3):
        if i == idx:
            continue
        pr, pg, pb = palette[i * 3: i * 3 + 3]
        d = (pr - r) ** 2 + (pg - g) ** 2 + (pb - b) ** 2
        if best_d is None or d < best_d:
            best, best_d = i, d
    return best


def static_to_frames(im, max_side, n):
    base = fit(im, max_side).quantize(colors=255, dither=Image.FLOYDSTEINBERG)
    palette = base.getpalette()
    orig = base.getpixel((0, 0))
    alt = nearest_other_index(palette, orig)
    frames = []
    for i in range(n):
        f = base.copy()
        f.putpixel((0, 0), alt if i % 2 else orig)
        frames.append(f)
    return frames


def save_gif(frames, dst, duration_ms):
    frames[0].save(
        dst, save_all=True, append_images=frames[1:],
        duration=duration_ms, loop=0, optimize=False, disposal=1,
    )


def video_to_gif(src, dst, max_side, fps):
    vf = (
        f"fps={fps},scale='min({max_side},iw)':'min({max_side},ih)':force_original_aspect_ratio=decrease:flags=lanczos,"
        "split[a][b];[a]palettegen[p];[b][p]paletteuse"
    )
    subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-i", str(src), "-vf", vf, "-loop", "0", str(dst)], check=True)


def convert(src, out_dir, duration, max_side, fps):
    dst = out_dir / (src.stem + "_tg.gif")
    im = open_image(src)
    if im is None:
        kind = "video"
        video_to_gif(src, dst, max_side, fps)
    elif getattr(im, "n_frames", 1) == 1:
        kind = "static -> animated"
        n = max(2, round(duration * fps))
        save_gif(static_to_frames(im, max_side, n), dst, round(1000 / fps))
    else:
        kind = "animated"
        durations = [f.info.get("duration", 100) for f in ImageSequence.Iterator(im)]
        frames = [fit(f, max_side).quantize(colors=256) for f in ImageSequence.Iterator(im)]
        save_gif(frames, dst, durations)

    with Image.open(dst) as out:
        n_out = getattr(out, "n_frames", 1)
    if n_out < 2:
        raise RuntimeError(f"result has only {n_out} frame")
    print(f"{src.name} ({kind}) -> {dst}  [{n_out} frames, {dst.stat().st_size // 1024} KB]")
    return dst


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("inputs", nargs="+", type=Path)
    p.add_argument("-o", "--out", type=Path, help="output folder (default: next to input)")
    p.add_argument("-d", "--duration", type=float, default=2, help="seconds for static images (default 2)")
    p.add_argument("-s", "--size", type=int, default=480, help="max width/height (default 480)")
    p.add_argument("--fps", type=int, default=10)
    args = p.parse_args()

    failed = 0
    for src in args.inputs:
        out_dir = args.out or src.parent
        out_dir.mkdir(parents=True, exist_ok=True)
        try:
            convert(src, out_dir, args.duration, args.size, args.fps)
        except Exception as e:
            failed += 1
            print(f"{src}: failed: {e}", file=sys.stderr)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
