# NASA APOD Downloader

A command-line tool for downloading images from NASA's [Astronomy Picture of the Day (APOD)](https://apod.nasa.gov/) API — with filters to skip diagrams, overexposed shots, incomplete mosaics, panoramas, and low-resolution images.

## Features

- Download today's picture, a specific date, a date range, or N random pictures
- Choose HD or standard resolution
- Optional image-quality filters (all heuristic-based, no manual review needed):
  - `--exclude-text-images` — skip diagrams/star-charts/text-heavy graphics
  - `--exclude-overexposed` — skip blown-out/overly bright images
  - `--exclude-incomplete` — skip images with missing/blank mosaic sections
  - `--exclude-panoramas` — skip wide, stitched panorama-style images
  - `--min-resolution` — skip images below a resolution tier (defaults to HD)
- Save metadata (title, explanation, copyright) alongside each image
- Automatically chunks large requests to respect NASA's 100-item-per-request limit
- Retries automatically on network timeouts
- Interactive mode — just run it with no arguments and answer the prompts

## Requirements

- Python 3.7+
- [Pillow](https://pypi.org/project/Pillow/) (only needed for the image-quality filters above):
  ```
  pip install Pillow
  ```

## Quick start

```bash
# Clone or download this repo, then run with no arguments for a guided prompt:
python nasa_apod_downloader.py
```

Or use flags directly:

```bash
# Today's picture
python nasa_apod_downloader.py

# A specific date
python nasa_apod_downloader.py --date 2024-12-25

# A date range (automatically chunked if it spans >100 days)
python nasa_apod_downloader.py --start-date 2024-01-01 --end-date 2024-06-01

# 50 random pictures, skipping diagrams and panoramas
python nasa_apod_downloader.py --random 50 --exclude-text-images --exclude-panoramas
```

## Getting a NASA API key

The script works out of the box with NASA's public `DEMO_KEY`, but it's rate-limited:

| Key | Rate limit |
|---|---|
| `DEMO_KEY` (default) | 30 requests/hour, 50/day per IP |
| Free registered key | 1,000 requests/hour, no daily cap |

Get a free key in seconds at **[api.nasa.gov](https://api.nasa.gov/)**, then either pass it directly:

```bash
python nasa_apod_downloader.py --api-key YOUR_KEY_HERE
```

or set it once as an environment variable:

```bash
# macOS/Linux (add to ~/.bashrc or ~/.zshrc to persist)
export NASA_API_KEY="YOUR_KEY_HERE"

# Windows (PowerShell, persists across sessions)
setx NASA_API_KEY "YOUR_KEY_HERE"
```

## All options

| Flag | Description |
|---|---|
| `--api-key KEY` | Your NASA API key (default: `DEMO_KEY` or `$NASA_API_KEY`) |
| `--date YYYY-MM-DD` | A specific date |
| `--start-date` / `--end-date` | A date range |
| `--random N` | N random APOD entries |
| `--quality hd\|sd` | Image quality to request (default: `hd`) |
| `-o`, `--output DIR` | Output directory (default: `./nasa_apod`) |
| `--save-metadata` | Also save title/explanation/copyright as `.json`/`.txt` |
| `--overwrite` | Re-download files that already exist |
| `--exclude-text-images` | Skip diagrams/star-charts/text-heavy graphics |
| `--exclude-overexposed` | Skip blown-out/overly bright images |
| `--exclude-incomplete` | Skip images with missing/blank mosaic sections |
| `--exclude-panoramas` | Skip wide, stitched panorama images |
| `--min-resolution [sd\|hd\|none]` | Minimum resolution to keep (default: `hd`) |
| `--interactive` | Prompt for options instead of using flags |

Run `python nasa_apod_downloader.py --help` for full details at any time.

## Troubleshooting

**"No such file or directory"** — your terminal isn't in the same folder as the script. `cd` into the folder where you saved it, or run it with its full path.

**Connection timeouts** — the script retries automatically. If it still fails after 3 attempts, check your internet connection, or try disabling VPN/antivirus HTTPS scanning, which can interfere with the request.

**Image filters not working** — these require Pillow: `pip install Pillow`.

## License

MIT (or your license of choice).
