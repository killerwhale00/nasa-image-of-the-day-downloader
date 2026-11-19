#!/usr/bin/env python3
"""
NASA Astronomy Picture of the Day (APOD) Downloader
=====================================================

Downloads images (and metadata) from NASA's APOD API:
https://api.nasa.gov/

Features
--------
- Fetch today's picture, a specific date, a date range, or N random pictures
- Choose HD or standard resolution downloads
- Save metadata (title, explanation, copyright) alongside images as .json/.txt
- Skips videos gracefully (APOD occasionally features a video instead of an image)
- Optional filters (fast heuristics, no OCR — require Pillow: pip install Pillow):
    --exclude-text-images   skip diagrams/star-charts/text-heavy graphics
    --exclude-overexposed   skip blown-out/overly bright images (flares, flashes)
    --exclude-incomplete    skip images with missing/blank mosaic sections
    --exclude-panoramas     skip wide rover-panorama-style stitched images
    --min-resolution        skip images below sd (~640x480) or hd (~1280x720);
                             defaults to hd even if this flag isn't passed at all;
                             use --min-resolution none to disable
- Supports your own NASA API key, or falls back to the public DEMO_KEY
  (DEMO_KEY works but is rate-limited to ~30 requests/hour, 50/day)
- Retries automatically on network timeouts/hiccups

Get a free API key (recommended) at: https://api.nasa.gov/

Usage examples
--------------
  # Today's picture, using DEMO_KEY
  python nasa_apod_downloader.py

  # Use your own API key
  python nasa_apod_downloader.py --api-key YOUR_KEY_HERE

  # A specific date
  python nasa_apod_downloader.py --date 2024-12-25

  # A date range
  python nasa_apod_downloader.py --start-date 2024-12-01 --end-date 2024-12-10

  # 5 random APODs
  python nasa_apod_downloader.py --random 5

  # Standard resolution instead of HD, custom output folder, save metadata too
  python nasa_apod_downloader.py --date 2024-12-25 --quality sd -o ./my_apods --save-metadata

  # Interactive mode (no arguments needed, will prompt you for everything)
  python nasa_apod_downloader.py --interactive

  # Skip images that look like diagrams/star-charts/text-heavy graphics
  python nasa_apod_downloader.py --random 10 --exclude-text-images

  # Skip overexposed and incomplete-mosaic images too
  python nasa_apod_downloader.py --random 10 --exclude-overexposed --exclude-incomplete

  # Skip wide stitched panoramas (e.g. Mars rover mosaics)
  python nasa_apod_downloader.py --random 10 --exclude-panoramas

  # Resolution filtering defaults to HD automatically, no flag needed:
  python nasa_apod_downloader.py --random 10

  # Explicitly request SD as the minimum instead
  python nasa_apod_downloader.py --random 10 --min-resolution sd

  # Turn resolution filtering off entirely
  python nasa_apod_downloader.py --random 10 --min-resolution none
"""

import argparse
import io
import json
import os
import sys
import time
import urllib.request
import urllib.parse
import urllib.error
from datetime import date as _date, timedelta as _timedelta

# Optional dependency for --exclude-text-images. The script works fine without
# it; that flag just won't be usable until Pillow is installed:
#   pip install Pillow
try:
    from PIL import Image, ImageFilter, ImageOps
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

APOD_ENDPOINT = "https://api.nasa.gov/planetary/apod"


def _get_pixel_values(img):
    """Return a flat list of pixel values from an image, in a way that's
    compatible with both older and newer Pillow versions. Image.getdata()
    is deprecated starting in Pillow 14 in favor of get_flattened_data(),
    so prefer the new method when it's available.
    """
    if hasattr(img, "get_flattened_data"):
        return list(img.get_flattened_data())
    return list(img.getdata())


def looks_like_text_or_diagram(image_bytes, sample_size=150,
                                color_ratio_threshold=0.05, edge_ratio_threshold=0.15):
    """Fast heuristic (no OCR) to guess whether an image is a diagram/star-chart/
    text-heavy graphic rather than a photograph.

    Idea: photos have lots of unique colors and soft, noisy edges. Diagrams and
    text-heavy images tend to have few unique colors (lots of flat white/black
    space) and a high density of sharp edges (text strokes, chart lines, borders).

    Returns True if the image looks like a diagram/text graphic, False if it
    looks like a photo. Returns False (i.e. "keep it") if Pillow isn't available
    or the image can't be analyzed, so this never silently blocks a download.
    """
    if not PIL_AVAILABLE:
        return False

    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        img_small = img.resize((sample_size, sample_size))

        total_pixels = sample_size * sample_size
        colors = img_small.getcolors(maxcolors=total_pixels)
        num_unique_colors = len(colors) if colors is not None else total_pixels
        color_ratio = num_unique_colors / total_pixels

        gray = ImageOps.grayscale(img_small)
        edges = gray.filter(ImageFilter.FIND_EDGES)
        edge_pixels = sum(1 for p in _get_pixel_values(edges) if p > 50)
        edge_ratio = edge_pixels / total_pixels

        return color_ratio < color_ratio_threshold and edge_ratio > edge_ratio_threshold
    except Exception:
        return False


def looks_overexposed(image_bytes, sample_size=150, bright_threshold=245, bright_frac_threshold=0.35):
    """Fast heuristic to guess whether an image is blown-out/overexposed
    (e.g. a bright flash, lens flare, or sun glare dominating the frame).

    Idea: check what fraction of pixels are near pure white. A normal photo
    might have some bright highlights, but a large majority of near-white
    pixels usually means the frame is blown out.

    Returns False (i.e. "keep it") if Pillow isn't available or analysis fails.
    """
    if not PIL_AVAILABLE:
        return False

    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        img_small = img.resize((sample_size, sample_size))
        gray = ImageOps.grayscale(img_small)
        pixels = _get_pixel_values(gray)
        total = len(pixels)

        bright_frac = sum(1 for p in pixels if p >= bright_threshold) / total
        return bright_frac > bright_frac_threshold
    except Exception:
        return False


def looks_incomplete(image_bytes, sample_size=150, tile_size=15,
                      flat_variance_threshold=4, flat_tile_frac_threshold=0.08):
    """Fast heuristic to guess whether an image has large blank/missing
    sections — e.g. a mosaic image with an unfilled rectangular gap where
    a camera chip's data is missing.

    Idea: divide the image into a grid of small tiles and measure each
    tile's pixel variance. Real photographic content (even a starfield's
    'empty' black sky) has faint noise and scattered stars, so its tiles
    have some variance. A genuinely blank/missing region is perfectly flat
    (variance ~0). If too many tiles are perfectly flat, the image likely
    has incomplete data rather than just being a naturally dark photo.

    Returns False (i.e. "keep it") if Pillow isn't available or analysis fails.
    """
    if not PIL_AVAILABLE:
        return False

    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        img_small = img.resize((sample_size, sample_size))
        gray = ImageOps.grayscale(img_small)
        gray_px = gray.load()

        cols = rows = sample_size // tile_size
        flat_tiles = 0
        total_tiles = cols * rows

        for ty in range(rows):
            for tx in range(cols):
                vals = [
                    gray_px[x, y]
                    for y in range(ty * tile_size, ty * tile_size + tile_size)
                    for x in range(tx * tile_size, tx * tile_size + tile_size)
                ]
                mean = sum(vals) / len(vals)
                variance = sum((v - mean) ** 2 for v in vals) / len(vals)
                if variance < flat_variance_threshold:
                    flat_tiles += 1

        flat_frac = flat_tiles / total_tiles
        return flat_frac > flat_tile_frac_threshold
    except Exception:
        return False


def _fetch_apod_single_request(api_key, date=None, start_date=None, end_date=None, count=None,
                                thumbs=True, max_retries=3, timeout=60):
    """Call the NASA APOD API once and return a list of entry dicts.

    Retries on timeouts/transient network errors before giving up. This
    issues exactly one HTTP request — use fetch_apod() instead, which
    chunks large date ranges / counts to respect NASA's 100-item limit
    per request.
    """
    params = {"api_key": api_key}
    if count:
        params["count"] = count
    elif start_date and end_date:
        params["start_date"] = start_date
        params["end_date"] = end_date
    elif date:
        params["date"] = date
    if thumbs:
        params["thumbs"] = "true"  # gives a thumbnail URL even for video entries

    url = f"{APOD_ENDPOINT}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (APOD-downloader)"})

    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            if isinstance(data, dict):
                return [data]
            return data

        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="ignore")
            print(f"\n[ERROR] NASA API returned HTTP {e.code}.")
            try:
                msg = json.loads(body).get("msg") or json.loads(body).get("error", {}).get("message")
                if msg:
                    print(f"        {msg}")
            except Exception:
                pass
            if e.code == 429:
                print("        You've hit a rate limit. Get your own free API key at https://api.nasa.gov/")
            sys.exit(1)  # HTTP errors won't be fixed by retrying

        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            last_error = e
            reason = getattr(e, "reason", e)
            if attempt < max_retries:
                wait = 2 * attempt
                print(f"[WARN]  Connection issue ({reason}). Retrying in {wait}s "
                      f"(attempt {attempt}/{max_retries})...")
                time.sleep(wait)
            else:
                print(f"\n[ERROR] Could not reach NASA API after {max_retries} attempts: {reason}")
                print("        This is usually a network/firewall/antivirus issue, or a slow connection.")
                print("        Things to try:")
                print("          - Check your internet connection")
                print("          - Temporarily disable VPN/antivirus HTTPS scanning and retry")
                print("          - Try again in a minute (NASA's servers may be briefly slow)")
                sys.exit(1)

    print(f"\n[ERROR] Failed to fetch data from NASA API: {last_error}")
    sys.exit(1)


# NASA's APOD API rejects date ranges or --count values larger than 100
# items in a single request. This wrapper transparently splits larger
# requests into multiple chunked calls and stitches the results together.
MAX_ITEMS_PER_REQUEST = 100


def fetch_apod(api_key, date=None, start_date=None, end_date=None, count=None, thumbs=True,
                max_retries=3, timeout=60):
    """Call the NASA APOD API and return a list of entry dicts.

    Automatically splits requests that would exceed NASA's 100-item-per-request
    limit (large date ranges or --random counts) into multiple chunked calls.
    """
    # --- Random count, possibly > 100 ---
    if count:
        if count <= MAX_ITEMS_PER_REQUEST:
            return _fetch_apod_single_request(api_key, count=count, thumbs=thumbs,
                                               max_retries=max_retries, timeout=timeout)

        results = []
        remaining = count
        chunk_num = 0
        total_chunks = (count + MAX_ITEMS_PER_REQUEST - 1) // MAX_ITEMS_PER_REQUEST
        while remaining > 0:
            chunk_num += 1
            this_count = min(MAX_ITEMS_PER_REQUEST, remaining)
            print(f"[INFO] Requesting random batch {chunk_num}/{total_chunks} ({this_count} images)...")
            results.extend(_fetch_apod_single_request(api_key, count=this_count, thumbs=thumbs,
                                                        max_retries=max_retries, timeout=timeout))
            remaining -= this_count
            if remaining > 0:
                time.sleep(1)  # be polite between chunked requests
        return results

    # --- Date range, possibly spanning > 100 days ---
    if start_date and end_date:
        start = _date.fromisoformat(start_date)
        end = _date.fromisoformat(end_date)
        total_days = (end - start).days + 1

        if total_days <= MAX_ITEMS_PER_REQUEST:
            return _fetch_apod_single_request(api_key, start_date=start_date, end_date=end_date,
                                               thumbs=thumbs, max_retries=max_retries, timeout=timeout)

        results = []
        chunk_start = start
        chunk_num = 0
        total_chunks = (total_days + MAX_ITEMS_PER_REQUEST - 1) // MAX_ITEMS_PER_REQUEST
        while chunk_start <= end:
            chunk_num += 1
            chunk_end = min(chunk_start + _timedelta(days=MAX_ITEMS_PER_REQUEST - 1), end)
            print(f"[INFO] Requesting date chunk {chunk_num}/{total_chunks}: "
                  f"{chunk_start.isoformat()} to {chunk_end.isoformat()}...")
            results.extend(_fetch_apod_single_request(api_key, start_date=chunk_start.isoformat(),
                                                        end_date=chunk_end.isoformat(), thumbs=thumbs,
                                                        max_retries=max_retries, timeout=timeout))
            chunk_start = chunk_end + _timedelta(days=1)
            if chunk_start <= end:
                time.sleep(1)  # be polite between chunked requests
        return results

    # --- Single date or "today" — always just one item, no chunking needed ---
    return _fetch_apod_single_request(api_key, date=date, thumbs=thumbs,
                                       max_retries=max_retries, timeout=timeout)


def pick_url(entry, quality):
    """Pick the best available image URL for an entry given desired quality."""
    media_type = entry.get("media_type", "image")
    if media_type != "image":
        return None  # videos aren't downloadable as images; we'll skip or note them

    if quality == "hd":
        return entry.get("hdurl") or entry.get("url")
    return entry.get("url") or entry.get("hdurl")


# Minimum pixel-count tiers for --min-resolution. Compared against actual
# width*height of the downloaded image (not the --quality flag, which just
# picks which URL to request — the source image can still be smaller than
# expected either way).
RESOLUTION_MIN_PIXELS = {
    "sd": 640 * 480,    # ~307,200 px
    "hd": 1280 * 720,   # ~921,600 px
}


def below_min_resolution(image_bytes, min_level="sd"):
    """Return True if the image's actual dimensions fall below the given
    resolution tier ('sd' or 'hd'). Returns False (i.e. "keep it") if
    Pillow isn't available or the image can't be read, so this never
    silently blocks a download.
    """
    if not PIL_AVAILABLE:
        return False
    try:
        img = Image.open(io.BytesIO(image_bytes))
        width, height = img.size
        min_pixels = RESOLUTION_MIN_PIXELS.get(min_level, RESOLUTION_MIN_PIXELS["sd"])
        return (width * height) < min_pixels
    except Exception:
        return False


def looks_like_panorama(image_bytes, aspect_ratio_threshold=2.5):
    """Return True if the image has an extreme aspect ratio, e.g. a rover
    panorama/mosaic (very wide, often stitched from many frames) rather
    than a normal photo frame.

    Returns False (i.e. "keep it") if Pillow isn't available or the image
    can't be read, so this never silently blocks a download.
    """
    if not PIL_AVAILABLE:
        return False
    try:
        img = Image.open(io.BytesIO(image_bytes))
        width, height = img.size
        ratio = max(width / height, height / width)
        return ratio > aspect_ratio_threshold
    except Exception:
        return False


def safe_filename(entry, url):
    date = entry.get("date", "unknown-date")
    title = entry.get("title", "untitled")
    # sanitize title for filesystem
    title_clean = "".join(c if c.isalnum() or c in " -_" else "" for c in title).strip()
    title_clean = title_clean.replace(" ", "_")[:80]
    ext = os.path.splitext(urllib.parse.urlparse(url).path)[1] or ".jpg"
    return f"{date}_{title_clean}{ext}"


def fetch_bytes(url, max_retries=3, timeout=60):
    """Download a URL into memory, with retries. Returns the raw bytes."""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (APOD-downloader)"})
    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            last_error = e
            if attempt < max_retries:
                time.sleep(2 * attempt)
            else:
                raise last_error


def save_metadata(entry, out_dir, base_name):
    meta_path = os.path.join(out_dir, base_name + ".json")
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(entry, f, indent=2, ensure_ascii=False)

    txt_path = os.path.join(out_dir, base_name + ".txt")
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(f"Title: {entry.get('title', 'N/A')}\n")
        f.write(f"Date: {entry.get('date', 'N/A')}\n")
        if entry.get("copyright"):
            f.write(f"Copyright: {entry.get('copyright')}\n")
        f.write(f"Media type: {entry.get('media_type', 'N/A')}\n\n")
        f.write(f"Explanation:\n{entry.get('explanation', 'N/A')}\n")


def run(args):
    out_dir = args.output
    os.makedirs(out_dir, exist_ok=True)

    res_filter_active = args.min_resolution and args.min_resolution != "none"

    any_filter = (args.exclude_text_images or args.exclude_overexposed
                  or args.exclude_incomplete or args.exclude_panoramas or res_filter_active)
    if any_filter and not PIL_AVAILABLE:
        print("[ERROR] --exclude-text-images / --exclude-overexposed / --exclude-incomplete /")
        print("        --exclude-panoramas / --min-resolution require the 'Pillow' package.")
        print("        Install it with:  pip install Pillow")
        sys.exit(1)

    entries = fetch_apod(
        api_key=args.api_key,
        date=args.date,
        start_date=args.start_date,
        end_date=args.end_date,
        count=args.random,
    )

    print(f"\nFound {len(entries)} entr{'y' if len(entries)==1 else 'ies'}.\n")

    downloaded = 0
    skipped_videos = 0
    skipped_text = 0
    skipped_overexposed = 0
    skipped_incomplete = 0
    skipped_panorama = 0
    skipped_low_res = 0
    failed = 0

    for entry in entries:
        title = entry.get("title", "Untitled")
        date = entry.get("date", "unknown-date")
        media_type = entry.get("media_type", "image")

        if media_type != "image":
            print(f"[SKIP]  {date} - '{title}' is a video, not an image ({entry.get('url')})")
            skipped_videos += 1
            continue

        url = pick_url(entry, args.quality)
        if not url:
            print(f"[SKIP]  {date} - '{title}' has no downloadable image URL")
            failed += 1
            continue

        filename = safe_filename(entry, url)
        dest_path = os.path.join(out_dir, filename)

        if os.path.exists(dest_path) and not args.overwrite:
            print(f"[SKIP]  {date} - '{title}' already downloaded ({filename})")
            continue

        try:
            image_bytes = fetch_bytes(url)

            if args.exclude_text_images and looks_like_text_or_diagram(image_bytes):
                print(f"[SKIP]  {date} - '{title}' looks like a diagram/text image (--exclude-text-images)")
                skipped_text += 1
                continue

            if args.exclude_overexposed and looks_overexposed(image_bytes):
                print(f"[SKIP]  {date} - '{title}' looks overexposed/blown-out (--exclude-overexposed)")
                skipped_overexposed += 1
                continue

            if args.exclude_incomplete and looks_incomplete(image_bytes):
                print(f"[SKIP]  {date} - '{title}' appears to have missing/blank sections (--exclude-incomplete)")
                skipped_incomplete += 1
                continue

            if args.exclude_panoramas and looks_like_panorama(image_bytes):
                print(f"[SKIP]  {date} - '{title}' looks like a panorama (--exclude-panoramas)")
                skipped_panorama += 1
                continue

            if res_filter_active and below_min_resolution(image_bytes, args.min_resolution):
                print(f"[SKIP]  {date} - '{title}' is below {args.min_resolution.upper()} resolution "
                      f"(--min-resolution {args.min_resolution})")
                skipped_low_res += 1
                continue

            with open(dest_path, "wb") as out:
                out.write(image_bytes)
            print(f"[DOWN]  {date} - '{title}'  ->  {filename}")

            downloaded += 1
            if args.save_metadata:
                base_name = os.path.splitext(filename)[0]
                save_metadata(entry, out_dir, base_name)
        except Exception as e:
            print(f"[FAIL]  {date} - '{title}': {e}")
            failed += 1

        # Be polite to the API/CDN when doing bulk downloads
        if len(entries) > 1:
            time.sleep(0.3)

    print("\n--- Summary ---")
    print(f"Downloaded:            {downloaded}")
    print(f"Skipped videos:        {skipped_videos}")
    if args.exclude_text_images:
        print(f"Skipped (text):        {skipped_text}")
    if args.exclude_overexposed:
        print(f"Skipped (overexposed): {skipped_overexposed}")
    if args.exclude_incomplete:
        print(f"Skipped (incomplete):  {skipped_incomplete}")
    if args.exclude_panoramas:
        print(f"Skipped (panorama):    {skipped_panorama}")
    if res_filter_active:
        print(f"Skipped (low-res):     {skipped_low_res}")
    print(f"Failed:                {failed}")
    print(f"Saved to:              {os.path.abspath(out_dir)}")


def interactive_prompt():
    print("=== NASA APOD Downloader (interactive mode) ===\n")

    api_key = input("API key (leave blank to use public DEMO_KEY): ").strip() or "DEMO_KEY"

    print("\nWhat would you like to download?")
    print("  1) Today's picture")
    print("  2) A specific date")
    print("  3) A date range")
    print("  4) N random pictures")
    choice = input("Choose [1-4]: ").strip()

    date = start_date = end_date = None
    random_count = None

    if choice == "2":
        date = input("Date (YYYY-MM-DD): ").strip()
    elif choice == "3":
        start_date = input("Start date (YYYY-MM-DD): ").strip()
        end_date = input("End date (YYYY-MM-DD): ").strip()
    elif choice == "4":
        random_count = int(input("How many random pictures? ").strip())

    quality = input("Quality - hd or sd? [hd]: ").strip().lower() or "hd"
    out_dir = input("Output folder [./nasa_apod]: ").strip() or "./nasa_apod"
    save_meta = (input("Save title/explanation metadata too? [y/N]: ").strip().lower() == "y")

    exclude_text_images = False
    exclude_overexposed = False
    exclude_incomplete = False
    exclude_panoramas = False
    min_resolution = "hd"
    if PIL_AVAILABLE:
        exclude_text_images = (input("Skip images that look like diagrams/text/star-charts? [y/N]: ")
                                .strip().lower() == "y")
        exclude_overexposed = (input("Skip images that look overexposed/blown-out (too bright)? [y/N]: ")
                                .strip().lower() == "y")
        exclude_incomplete = (input("Skip images with missing/blank mosaic sections (incomplete)? [y/N]: ")
                               .strip().lower() == "y")
        exclude_panoramas = (input("Skip images that look like wide panoramas? [y/N]: ")
                              .strip().lower() == "y")
        res_answer = input("Minimum resolution to keep? [hd/sd/none] (default hd): ").strip().lower()
        if res_answer in ("sd",):
            min_resolution = "sd"
        elif res_answer in ("none", "n"):
            min_resolution = "none"
        else:
            min_resolution = "hd"
    else:
        print("(Skipping the image-quality filter options — they require Pillow: pip install Pillow)")

    class Args:
        pass

    args = Args()
    args.api_key = api_key
    args.date = date
    args.start_date = start_date
    args.end_date = end_date
    args.random = random_count
    args.quality = "hd" if quality not in ("hd", "sd") else quality
    args.output = out_dir
    args.save_metadata = save_meta
    args.overwrite = False
    args.exclude_text_images = exclude_text_images
    args.exclude_overexposed = exclude_overexposed
    args.exclude_incomplete = exclude_incomplete
    args.exclude_panoramas = exclude_panoramas
    args.min_resolution = min_resolution
    return args


def main():
    parser = argparse.ArgumentParser(
        description="Download NASA Astronomy Picture of the Day (APOD) images.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--api-key", default=os.environ.get("NASA_API_KEY", "DEMO_KEY"),
                         help="Your NASA API key from https://api.nasa.gov/ (default: DEMO_KEY, or $NASA_API_KEY env var)")
    parser.add_argument("--date", help="Specific date, format YYYY-MM-DD")
    parser.add_argument("--start-date", help="Start date for a range, format YYYY-MM-DD")
    parser.add_argument("--end-date", help="End date for a range, format YYYY-MM-DD")
    parser.add_argument("--random", type=int, metavar="N", help="Download N random APOD entries")
    parser.add_argument("--quality", choices=["hd", "sd"], default="hd",
                         help="Image quality to download (default: hd)")
    parser.add_argument("-o", "--output", default="./nasa_apod", help="Output directory (default: ./nasa_apod)")
    parser.add_argument("--save-metadata", action="store_true",
                         help="Also save a .json and .txt with title/explanation/copyright for each image")
    parser.add_argument("--overwrite", action="store_true", help="Re-download files that already exist")
    parser.add_argument("--exclude-text-images", action="store_true",
                         help="Skip images that look like diagrams/star-charts/text-heavy graphics "
                              "(fast heuristic, no OCR — requires Pillow: pip install Pillow)")
    parser.add_argument("--exclude-overexposed", action="store_true",
                         help="Skip images that look blown-out/overexposed, e.g. bright flashes or "
                              "lens flare dominating the frame (requires Pillow: pip install Pillow)")
    parser.add_argument("--exclude-incomplete", action="store_true",
                         help="Skip images with large missing/blank mosaic sections, e.g. an unfilled "
                              "rectangular gap from a missing camera chip (requires Pillow: pip install Pillow)")
    parser.add_argument("--exclude-panoramas", action="store_true",
                         help="Skip images with an extreme aspect ratio (very wide or very tall), e.g. "
                              "rover panoramas stitched from many frames (requires Pillow: pip install Pillow)")
    parser.add_argument("--min-resolution", nargs="?", const="hd", default="hd", choices=["sd", "hd", "none"],
                         help="Exclude images below a minimum resolution tier based on actual downloaded "
                              "dimensions: sd (~640x480 / ~300K px) or hd (~1280x720 / ~920K px). "
                              "Defaults to 'hd' even if you don't pass this flag at all. "
                              "Pass 'none' to disable resolution filtering entirely. "
                              "Requires Pillow: pip install Pillow")
    parser.add_argument("--interactive", action="store_true", help="Prompt for options instead of using flags")

    args = parser.parse_args()

    if args.interactive or len(sys.argv) == 1:
        args = interactive_prompt()

    if args.api_key == "DEMO_KEY":
        print("[INFO] Using the public DEMO_KEY:")
        print("         - 30 requests/hour per IP address")
        print("         - 50 requests/day per IP address")
        print("       A free registered API key raises this to 1,000 requests/hour (no daily cap).")
        print("       Get one at https://api.nasa.gov/ and pass it with --api-key YOUR_KEY\n")
    else:
        print("[INFO] Using a registered API key: up to 1,000 requests/hour per key (no daily cap).\n")

    run(args)


if __name__ == "__main__":
    main()
