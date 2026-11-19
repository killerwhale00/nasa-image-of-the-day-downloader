#!/usr/bin/env python3
"""
NASA Astronomy Picture of the Day (APOD) Downloader
=====================================================

Downloads images (and metadata) from NASA's APOD Basic JSON feed:
https://science.nasa.gov/wp-json/wp/v2/apod-basic

MIGRATION NOTE (Nov 2026): NASA retired the old api.nasa.gov/planetary/apod
endpoint (the one that needed an API key / DEMO_KEY). This version talks to
the new public science.nasa.gov APOD Basic feed instead. Key differences:
  - No API key needed at all - the feed is public.
  - Dates use NASA's legacy YYMMDD code internally (handled for you - you
    still pass normal YYYY-MM-DD dates on the command line).
  - The feed exposes only one image rendition (hdurl), so there's no
    separate HD/SD choice anymore - the --quality flag has been removed.
  - There's no "random picture" endpoint on NASA's side anymore, so
    --random now works by sampling random dates since APOD began
    (1995-06-16) and fetching each one - it's slower than before for
    large counts, since it's one request per date.
  - The feed doesn't include a "copyright" field, so that's no longer
    saved to metadata; a "permalink" to the APOD web page is saved instead.

Features
--------
- Fetch today's picture, a specific date, a date range, or N random pictures
- Save metadata (title, explanation, permalink) alongside images as .json/.txt
- Skips videos/embeds gracefully (APOD occasionally features a video or
  iframe embed instead of a plain image)
- Optional filters (fast heuristics, no OCR - require Pillow: pip install Pillow):
    --exclude-text-images   skip diagrams/star-charts/text-heavy graphics
    --exclude-overexposed   skip blown-out/overly bright images (flares, flashes)
    --exclude-incomplete    skip images with missing/blank mosaic sections
    --exclude-panoramas     skip wide rover-panorama-style stitched images
    --min-resolution        skip images below sd (~640x480) or hd (~1280x720);
                             defaults to hd even if this flag isn't passed at all;
                             use --min-resolution none to disable
- Retries automatically on network timeouts/hiccups
- Automatically paginates date ranges to respect NASA's 25-item-per-page cap

Usage examples
--------------
  # Today's picture
  python nasa_apod_downloader.py

  # A specific date
  python nasa_apod_downloader.py --date 2024-12-25

  # A date range (automatically paginated)
  python nasa_apod_downloader.py --start-date 2024-12-01 --end-date 2024-12-10

  # 5 random APODs (samples random dates since APOD began)
  python nasa_apod_downloader.py --random 5

  # Custom output folder, save metadata too
  python nasa_apod_downloader.py --date 2024-12-25 -o ./my_apods --save-metadata

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
import random
import sys
import time
import urllib.request
import urllib.parse
import urllib.error
from datetime import date as _date, timedelta as _timedelta

# Optional dependency for the image-quality filters below. The script works
# fine without it; those flags just won't be usable until Pillow is installed:
#   pip install Pillow
try:
    from PIL import Image, ImageFilter, ImageOps
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

# NASA's new public APOD Basic JSON feed (replaces api.nasa.gov/planetary/apod).
APOD_BASE = "https://science.nasa.gov/wp-json/wp/v2/apod-basic"

# The first APOD was published on 1995-06-16. Used as the start of the range
# we sample random dates from for --random.
APOD_START_DATE = _date(1995, 6, 16)

# NASA caps per_page at 25 on the list endpoint; date-range requests are
# paginated automatically to work around this.
PER_PAGE_MAX = 25


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


# Minimum pixel-count tiers for --min-resolution. Compared against actual
# width*height of the downloaded image.
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


def _legacy_date_code(d):
    """Convert a datetime.date to NASA's legacy APOD date code (YYMMDD),
    which the science.nasa.gov feed still uses for single-date lookups
    and date_from/date_to filters.
    """
    return d.strftime("%y%m%d")


def _http_get_json(url, max_retries=3, timeout=60, allow_404=False):
    """GET a URL and parse it as JSON, with retries on transient network
    errors. If allow_404 is True, a 404 response returns None instead of
    exiting (used for single-date lookups, where a 404 just means nothing
    was published that day).
    """
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (APOD-downloader)"})

    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))

        except urllib.error.HTTPError as e:
            if e.code == 404 and allow_404:
                return None
            body = e.read().decode("utf-8", errors="ignore")
            print(f"\n[ERROR] NASA's APOD feed returned HTTP {e.code}.")
            try:
                msg = json.loads(body).get("message")
                if msg:
                    print(f"        {msg}")
            except Exception:
                pass
            sys.exit(1)  # HTTP errors (other than an allowed 404) won't be fixed by retrying

        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            last_error = e
            reason = getattr(e, "reason", e)
            if attempt < max_retries:
                wait = 2 * attempt
                print(f"[WARN]  Connection issue ({reason}). Retrying in {wait}s "
                      f"(attempt {attempt}/{max_retries})...")
                time.sleep(wait)
            else:
                print(f"\n[ERROR] Could not reach NASA's APOD feed after {max_retries} attempts: {reason}")
                print("        This is usually a network/firewall/antivirus issue, or a slow connection.")
                print("        Things to try:")
                print("          - Check your internet connection")
                print("          - Temporarily disable VPN/antivirus HTTPS scanning and retry")
                print("          - Try again in a minute (NASA's servers may be briefly slow)")
                sys.exit(1)

    print(f"\n[ERROR] Failed to fetch data from NASA's APOD feed: {last_error}")
    sys.exit(1)


def _normalize_entry(item):
    """Map a raw science.nasa.gov apod-basic JSON item to the internal
    entry dict the rest of this script works with.
    """
    if not item:
        return None
    return {
        "title": item.get("title") or "Untitled",
        "date": item.get("date") or "unknown-date",
        "explanation": item.get("explanation", ""),
        "media_type": item.get("media_type", "image"),
        "hdurl": item.get("hdurl"),
        "permalink": item.get("permalink") or item.get("url"),
    }


def fetch_single_date(d, max_retries=3, timeout=60):
    """Fetch one APOD entry by date. Returns None if nothing was published
    that day (e.g. a gap in the archive, or a date before APOD started).
    """
    legacy = _legacy_date_code(d)
    url = f"{APOD_BASE}/{legacy}"
    data = _http_get_json(url, max_retries=max_retries, timeout=timeout, allow_404=True)
    if isinstance(data, list):
        data = data[0] if data else None
    return _normalize_entry(data)


def fetch_date_range(start_date, end_date, max_retries=3, timeout=60):
    """Fetch all APOD entries between two dates (inclusive), automatically
    paginating through NASA's 25-item-per-page cap.
    """
    legacy_start = _legacy_date_code(start_date)
    legacy_end = _legacy_date_code(end_date)

    results = []
    page = 1
    while True:
        params = {
            "date_from": legacy_start,
            "date_to": legacy_end,
            "page": page,
            "per_page": PER_PAGE_MAX,
        }
        url = f"{APOD_BASE}?{urllib.parse.urlencode(params)}"
        if page > 1:
            print(f"[INFO] Fetching page {page} of date range results...")
        batch = _http_get_json(url, max_retries=max_retries, timeout=timeout)
        if not batch:
            break
        results.extend(_normalize_entry(item) for item in batch)
        if len(batch) < PER_PAGE_MAX:
            break
        page += 1
        time.sleep(0.5)  # be polite between pages
    return results


def fetch_random(count, max_retries=3, timeout=60):
    """Fetch N random APOD entries. NASA's new feed has no built-in random
    picker, so this samples unique random dates since APOD began and fetches
    each individually, skipping any date with nothing published. This means
    one HTTP request per date, so large counts will take a while.
    """
    today = _date.today()
    total_days = (today - APOD_START_DATE).days + 1
    sample_size = min(count, total_days)
    if sample_size < count:
        print(f"[WARN]  Only {total_days} days of APOD archive exist; requesting all of them instead of {count}.")

    offsets = random.sample(range(total_days), sample_size)

    results = []
    for i, offset in enumerate(offsets, 1):
        d = APOD_START_DATE + _timedelta(days=offset)
        entry = fetch_single_date(d, max_retries=max_retries, timeout=timeout)
        if entry:
            results.append(entry)
        if i % 10 == 0 or i == sample_size:
            print(f"[INFO] Checked {i}/{sample_size} random dates ({len(results)} found so far)...")
        time.sleep(0.2)  # be polite between requests
    return results


def fetch_apod(date=None, start_date=None, end_date=None, count=None, max_retries=3, timeout=60):
    """Fetch APOD entries from NASA's science.nasa.gov feed and return a
    list of normalized entry dicts.
    """
    if count:
        return fetch_random(count, max_retries=max_retries, timeout=timeout)

    if start_date and end_date:
        start = _date.fromisoformat(start_date)
        end = _date.fromisoformat(end_date)
        return fetch_date_range(start, end, max_retries=max_retries, timeout=timeout)

    d = _date.fromisoformat(date) if date else _date.today()
    entry = fetch_single_date(d, max_retries=max_retries, timeout=timeout)
    return [entry] if entry else []


def pick_url(entry):
    """Return the downloadable image URL for an entry, or None if it isn't
    a plain image (video/iframe embed) or has no image attached.
    """
    if entry.get("media_type") != "image":
        return None
    return entry.get("hdurl")


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
        if entry.get("permalink"):
            f.write(f"Permalink: {entry.get('permalink')}\n")
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
        date=args.date,
        start_date=args.start_date,
        end_date=args.end_date,
        count=args.random,
    )

    print(f"\nFound {len(entries)} entr{'y' if len(entries)==1 else 'ies'}.\n")

    downloaded = 0
    skipped_non_image = 0
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
            print(f"[SKIP]  {date} - '{title}' is not a downloadable image "
                  f"(media_type: {media_type}) ({entry.get('permalink')})")
            skipped_non_image += 1
            continue

        url = pick_url(entry)
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
    print(f"Skipped (non-image):   {skipped_non_image}")
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
    print("(No API key needed - NASA's APOD feed is public.)\n")

    print("What would you like to download?")
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
    args.date = date
    args.start_date = start_date
    args.end_date = end_date
    args.random = random_count
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
    parser.add_argument("--date", help="Specific date, format YYYY-MM-DD")
    parser.add_argument("--start-date", help="Start date for a range, format YYYY-MM-DD")
    parser.add_argument("--end-date", help="End date for a range, format YYYY-MM-DD")
    parser.add_argument("--random", type=int, metavar="N", help="Download N random APOD entries")
    parser.add_argument("-o", "--output", default="./nasa_apod", help="Output directory (default: ./nasa_apod)")
    parser.add_argument("--save-metadata", action="store_true",
                         help="Also save a .json and .txt with title/explanation/permalink for each image")
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

    run(args)


if __name__ == "__main__":
    main()
