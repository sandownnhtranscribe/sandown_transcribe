# sandown-transcribe

This Python script downloads video content from CableCast (Sandown Channel 16) archives, facilitating both direct media download and automated transcription of local government meeting recordings for the town of Sandown, NH.

## Overview

The `download_sandown.py` script interacts with the internal archive structure at `http://173.209.96.235/store-3/...`. It is designed to:
1.  **Download Videos:** Fetch the highest quality (or specified) video file for a given show or range of shows.
2.  **Audio Muxing:** Download and merge a shared audio track into the main video stream using `ffmpeg`.
3.  **Transcribe Content:** Use OpenAI's Whisper model to transcribe the video, saving the results in a standardized `.log` format mirroring project transcript conventions.

## Usage

The script supports two primary modes of operation: single show download and date range batch processing.

### 1. Single Show Download (Default)
Downloads the latest show found on the specified channel.

```bash
python3 download_sandown.py [OPTIONS]
# Example: Only list the latest show without downloading or transcribing
python3 download_sandown.py --list
# Example: Specify a resolution tier and output directory
python3 download_sandown.py --res 720p --out ./videos
```

### 2. Date Range Batch Download (Advanced)
Downloads all shows whose dates fall within an inclusive range (`--from` to `--to`). This mode is suitable for processing entire months of archives.

```bash
python3 download_sandown.py \
    --from YYYY-MM-DD --to YYYY-MM-DD [OPTIONS]
# Example: Download and transcribe all shows from August 2026, saving to 'august_transcripts'
python3 download_sandown.py --from 2026-08-01 --to 2026-08-31 --out ./august_transcripts
```

### Options

| Option | Alias | Description | Default |
| :--- | :--- | :--- | :--- |
| `--channel` | `-c` | The CableCast channel ID (e.g., 1). | `1` |
| `--res` | N/A | The video resolution tier to download (e.g., `1080p`, `720p`). | `1080p` |
| `--out` | `-o` | The directory where videos and transcripts will be saved. | `./downloads` |
| `--list` | N/A | Only prints the latest show details and exits without downloading or transcribing. | False |
| `--from` | N/A | Start date for range batch processing (YYYY-MM-DD). Must be used with `--to`. | `None` |
| `--to` | N/A | End date for range batch processing (YYYY-MM-DD). Used to define a date range. | `None` |
| `--log-dir`| N/A | Directory specifically for the transcript log files (separate from `--out`). | Same as `--out` |
| `--model` | N/A | The Whisper model to use (`turbo`, etc.). | `turbo` |
| `--device` | N/A | PyTorch device for Whisper (`cuda` or `cpu`). | `None` |
| `--keep-video` | N/A | If set, the downloaded video is retained after transcription. Otherwise, it is deleted. | False |
| `--no-transcribe`| N/A | Skips the entire Whisper transcription process. | False |

## Transcription Output Format

When successful, transcripts are saved as `YYYYmmdd-zboa.log`. This file format preserves a diagnostic header from the Whisper tool, followed by the standard timestamped transcript text.

---
*Note: The script requires `ffmpeg`, `curl`, and `whisper` to be installed and available in your PATH.*
