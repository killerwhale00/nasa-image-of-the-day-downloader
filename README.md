# NASA APOD Downloader

A command-line tool for downloading images from NASA's [Astronomy Picture of the Day (APOD)](https://apod.nasa.gov/) feed — with filters to skip diagrams, overexposed shots, incomplete mosaics, panoramas, and low-resolution images.

> **Migration note (Nov 2026):** NASA retired the old `api.nasa.gov/planetary/apod` endpoint (the one needing an API key / `DEMO_KEY`). This tool now talks to the new public `science.nasa.gov` APOD Basic feed instead — no API key required. See [What changed](#what-changed-from-the-old-api) below if you used an earlier version of this script.

## Features

- Download today's picture, a specific date, a date range, or N random pictures
- No API key needed — the feed is public
- Save metadata (title, explanation, permalink) alongside each image
- Optional image-quality filters (all heuristic-based, no manual review needed):
  - `--exclude-text-images` — skip diagrams/star-charts/text-heavy graphics
  - `--exclude-overexposed` — skip blown-out/overly bright images
  - `--exclude-incomplete` — skip images with missing/blank mosaic sections
  - `--exclude-panoramas` — skip wide, stitched panorama-style images
  - `--min-resolution` — skip images below a resolution tier (defaults to HD)
- Automatically paginates date ranges to respect NASA's 25-item-per-page cap
- Retries automatically on network timeouts
- Interactive mode — just run it with no arguments and answer the prompts

## Requirements

- Python 3.7+
- [Pillow](https://pypi.org/project/Pillow/) (only needed for the image-quality filters above):
  ```
  pip install Pillow
  ```


## Quick start

## for new users make sure to select the folder the file is in (if it is directly in downloads:
``` cd downloads

```
## Then do
```bash

python nasa_apod_downloader.py
```

Or use flags directly:

```bash
# Today's picture
python nasa_apod_downloader.py

# A specific date
python nasa_apod_downloader.py --date 2024-12-25

# A date range (automatically paginated)
python nasa_apod_downloader.py --start-date 2024-12-01 --end-date 2024-12-10

# 50 random pictures, skipping diagrams and panoramas
python nasa_apod_downloader.py --random 50 --exclude-text-images --exclude-panoramas
```

## All options

| Flag | Description |
|---|---|
| `--date YYYY-MM-DD` | A specific date |
| `--start-date` / `--end-date` | A date range |
| `--random N` | N random APOD entries (samples random dates since APOD began, one request per date) |
| `-o`, `--output DIR` | Output directory (default: `./nasa_apod`) |
| `--save-metadata` | Also save title/explanation/permalink as `.json`/`.txt` |
| `--overwrite` | Re-download files that already exist |
| `--exclude-text-images` | Skip diagrams/star-charts/text-heavy graphics |
| `--exclude-overexposed` | Skip blown-out/overly bright images |
| `--exclude-incomplete` | Skip images with missing/blank mosaic sections |
| `--exclude-panoramas` | Skip wide, stitched panorama images |
| `--min-resolution [sd\|hd\|none]` | Minimum resolution to keep (default: `hd`) |
| `--interactive` | Prompt for options instead of using flags |

Run `python nasa_apod_downloader.py --help` for full details at any time.

## What changed from the old API

NASA's original `api.nasa.gov/planetary/apod` endpoint (the one that needed `DEMO_KEY` or a registered API key) is being retired. This tool now uses the public `science.nasa.gov/wp-json/wp/v2/apod-basic` feed instead:

| | Old API | New feed |
|---|---|---|
| Authentication | API key (`DEMO_KEY` or your own) | None — fully public |
| Rate limit | 30–50/hr (`DEMO_KEY`) or 1,000/hr (registered) | Not published; the tool is polite about request pacing regardless |
| Image resolution | Separate `url` (SD) and `hdurl` (HD) fields | Only one full-size image field (`hdurl`); `--quality` no longer applies |
| Date range size | Chunked in batches of 100 | Paginated in batches of 25 (handled automatically) |
| Random pictures | Built-in `count` parameter | No built-in random endpoint — this tool samples random dates since APOD began (1995-06-16) and fetches each individually, so large `--random` counts take longer (~1 request per date) |
| Metadata | Included `copyright` | No `copyright` field; a `permalink` to the APOD web page is saved instead |

If an APOD entry has no `hdurl` (not all entries do), there's no documented way to get a direct image download for it — the tool reports it honestly as not downloadable rather than guessing.

## Troubleshooting

**"No such file or directory"** — your terminal isn't in the same folder as the script. `cd` into the folder where you saved it, or run it with its full path.

**Connection timeouts** — the script retries automatically. If it still fails after 3 attempts, check your internet connection, or try disabling VPN/antivirus HTTPS scanning, which can interfere with the request.

**Image filters not working** — these require Pillow: `pip install Pillow`.

**`--random` is slow for large counts** — this is expected; the new feed has no batch-random endpoint, so each random date is fetched individually.

## License

MIT 
