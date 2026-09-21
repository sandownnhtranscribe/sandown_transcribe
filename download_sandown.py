#!/usr/bin/env python3
"""Download the latest video from a Sandown Channel 16 CableCast channel.

The channel page (http://173.209.96.235/internetchannel/?channel=1) lists shows
as `<showId>-<slug>` entries whose media is served as fMP4 HLS:

    /store-3/<showId>-<slug>/vod.m3u8   -> master playlist (maps tiers to files)
    /store-3/<showId>-<slug>/<res>.mp4  -> the full fragmented-MP4 file for that tier

Each `<res>.mp4` is a single self-contained file: every HLS segment in the
playlist is just a byte-range into it. So instead of fighting ffmpeg's HLS
demuxer over relative URIs, we fetch the master playlist to resolve which
filename corresponds to the requested resolution and download that one file
directly with curl (which supports resuming interrupted downloads).

Usage:
    python3 download_sandown.py                 # find latest, download 1080p
    python3 download_sandown.py --channel 1     # channel id (default 1)
    python3 download_sandown.py --res 720p      # pick a resolution tier
    python3 download_sandown.py --list          # just show the latest, don't download
    python3 download_sandown.py --out ./videos  # output directory

Download every show whose date falls in an inclusive range:
    python3 download_sandown.py --from 2026-08-01 --to 2026-08-31
Each matching show is downloaded and (unless --no-transcribe) transcribed; the
video is deleted afterwards unless --keep-video is passed.
"""

import argparse
from datetime import date, datetime
import glob
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.request

BASE = "http://173.209.96.235"

# Per-download wall-clock limits (seconds). These prevent the script from
# hanging indefinitely on a stalled transfer: curl's --max-time caps the whole
# operation, and the urllib fallback passes the same timeout to urlopen(). Large
# video files on slow connections may need DOWNLOAD_TIMEOUT raised.
CONNECT_TIMEOUT = 30
DOWNLOAD_TIMEOUT = 3600


def fetch(url: str, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "curl/8"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def find_latest_show(page_html: str):
    """Return (show_id, slug) for the newest show found in a page of HTML.

    CableCast serves shows as `store-3/<id>-<slug>/vod.m3u8` links; the newest
    is simply the one with the highest id on the page.
    """
    matches = list(re.finditer(r"store-3/(?P<id>\d+)-(?P<slug>[^\"'>\s]+?)/vod\.m3u8",
                               page_html, re.IGNORECASE))
    if not matches:
        return None
    best = max(matches, key=lambda m: int(m.group("id")))
    return best.group("id"), best.group("slug")


def parse_shows(page_html: str):
    """Return a list of show dicts parsed from the channel page.

    Each entry has ``show_id``, ``slug``, ``vod_url`` and a parsed ``date``
    (a datetime). The authoritative date comes from CableCast's ISO 8601
    ``"Date"`` field; we fall back to parsing MM-DD-YYYY out of the slug when
    that field is absent. Results are sorted newest-first, matching the page.
    """
    shows = []
    for m in re.finditer(r"store-3/(?P<id>\d+)-(?P<slug>[^\"'>\s]+?)/vod\.m3u8",
                         page_html, re.IGNORECASE):
        show_id = m.group("id")
        slug = m.group("slug")
        # Grab the surrounding JSON blob to find the authoritative "Date".
        ctx_start = max(0, m.start() - 400)
        ctx = page_html[ctx_start:m.end()]
        date_val = None
        dm = re.search(r'"Date"\s*:\s*"([^"]+)"', ctx)
        if dm:
            try:
                date_val = datetime.fromisoformat(dm.group(1))
            except ValueError:
                date_val = None
        if date_val is None:
            sm = re.search(r"(\d{1,2})-(\d{1,2})-(\d{4})", slug)
            if sm:
                month, day, year = sm.groups()
                try:
                    date_val = datetime(int(year), int(month), int(day))
                except ValueError:
                    date_val = None
        if date_val is None:
            continue
        show = {
            "show_id": show_id,
            "slug": slug,
            "vod_url": f"{BASE}/store-3/{show_id}-{slug}/vod.m3u8",
            "date": date_val,
        }
        if show not in shows:  # the page lists each show twice; keep unique ones
            shows.append(show)
    shows.sort(key=lambda s: s["date"], reverse=True)
    return shows


def resolve_channel(channel_id: str):
    """Fetch the channel page and locate its latest show."""
    html = fetch(f"{BASE}/internetchannel/?channel={channel_id}").decode("utf-8", "replace")
    return find_latest_show(html)


def resolve_shows_in_range(channel_id: str, start: date, end: date):
    """Fetch the channel page and return shows whose date is within [start, end].

    The range is inclusive on both ends. Results are sorted newest-first.
    """
    html = fetch(f"{BASE}/internetchannel/?channel={channel_id}").decode("utf-8", "replace")
    shows = parse_shows(html)
    matches = [s for s in shows if start <= s["date"].date() <= end]
    return matches


def _tier_from_master(master: str, res: str):
    """Return the tier filename (e.g. ``1080p.m3u8``) matching `res`.

    Accepts a bare tier name ("1080p" -> "1080p.m3u8") or an exact filename that
    already ends in .m3u8. Returns None if no such tier is listed.
    """
    tiers = {line.strip() for line in master.splitlines()
             if line.strip().endswith(".m3u8") and not line.strip().startswith("#")}
    candidate = res if res.endswith(".m3u8") else f"{res}.m3u8"
    return candidate if candidate in tiers else None


def _media_layout(show_id: str, slug: str, tier: str):
    """Classify how a given tier's media is stored on the server.

    CableCast has shipped two layouts over time; both are handled here:

    * **Combined** -- the playlist references a single self-contained file
      (e.g. ``1080p.mp4``); every segment is just a byte-range into it. This is
      what recent shows use, and for audio too (``1080p_audio.mp4``). A single
      HTTP fetch gets the whole file -- no per-segment downloads.
    * **Segmented** -- the playlist has an ``#EXT-X-MAP`` init segment plus many
      tiny individual ``*.m4s`` files (older shows, e.g. May 2026). There is no
      single combined file to download.

    The two are told apart by the *distinct* media filenames referenced: a
    combined layout points every segment at one repeated filename; a segmented
    layout has many distinct ``*.m4s`` files. (A MAP init segment does **not**
    imply segmented -- combined files embed their init as a byte-range into the
    same file.)

    Returns a dict:

        {"kind": "file", "path": "<name>"}
        {"kind": "segments", "init": "<name>", "segments": ["<name>", ...]}
    """
    show_dir = f"{show_id}-{slug}"
    playlist = fetch(f"{BASE}/store-3/{show_dir}/{tier}").decode("utf-8", "replace")

    # Collect the distinct media filenames referenced by segment lines (ignoring
    # comments and the MAP init). A combined layout references a single file for
    # every segment; a segmented layout has many distinct *.m4s files.
    seg_re = re.compile(r'^(?:\./)?([A-Za-z0-9_./-]+\.(?:m4s|mp4))$')
    refs = []
    for line in playlist.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        m = seg_re.match(s)
        if m:
            refs.append(m.group(1))

    distinct = sorted(set(refs))
    if len(distinct) == 1:
        # Combined file: one self-contained download; every segment is a byte-range.
        return {"kind": "file", "dir": show_dir, "path": distinct[0]}

    # Segmented layout: pull the init (MAP) URI and every segment in order.
    map_m = re.search(r'#EXT-X-MAP:URI="([^"]+)"', playlist)
    if not map_m:
        return None
    init = map_m.group(1).strip()
    if init.startswith("./"):
        init = init[2:]
    return {"kind": "segments", "dir": show_dir,
            "init": init, "segments": refs}


def resolve_resolution(show_id: str, slug: str, res: str):
    """Return the media layout for a requested resolution tier.

    Reads the master playlist, picks the matching tier, and classifies whether
    that tier is stored as a single combined file or as individual segments.
    Returns None if the tier can't be resolved.
    """
    master = fetch(f"{BASE}/store-3/{show_id}-{slug}/vod.m3u8").decode("utf-8", "replace")
    tier = _tier_from_master(master, res)
    if not tier:
        return None
    return _media_layout(show_id, slug, tier)


def resolve_audio_file(show_id: str, slug: str):
    """Return the media layout for a show's shared audio track.

    CableCast serves one shared AAC-in-fMP4 audio track per show (the URI in the
    master playlist's #EXT-X-MEDIA line), regardless of video resolution. We read
    that URI directly so it works even if the tier name doesn't match the file.
    Returns None if no audio track is present.
    """
    master = fetch(f"{BASE}/store-3/{show_id}-{slug}/vod.m3u8").decode("utf-8", "replace")
    m = re.search(r'#EXT-X-MEDIA:[^#]*URI="([^"]+)"', master)
    if not m:
        return None
    uri = m.group(1).strip()
    # Strip a leading "./" so it matches the tier names in the master playlist.
    if uri.startswith("./"):
        uri = uri[2:]
    return _media_layout(show_id, slug, uri)

def download(show_id: str, slug: str, res: str, out_dir: str):
    layout = resolve_resolution(show_id, slug, res)
    if not layout:
        sys.exit(f"Could not resolve resolution '{res}' for {show_id}-{slug}.")

    os.makedirs(out_dir, exist_ok=True)
    safe_slug = slug.replace("/", "_")
    out_file = os.path.join(out_dir, f"{show_id}-{safe_slug}.mp4")

    print(f"Latest show: id={show_id} slug={slug}")
    print(f"Layout     : {layout['kind']}")
    print(f"Output     : {out_file}")

    _download_media(layout, out_file)

    # Audio is a single shared track per show; fetch it and mux into the video.
    audio_layout = resolve_audio_file(show_id, slug)
    if audio_layout:
        audio_tmp = os.path.join(out_dir, f".{show_id}-{safe_slug}-audio.mp4")
        print(f"Audio      : {audio_layout['kind']}")
        _download_media(audio_layout, audio_tmp)

        if shutil.which("ffmpeg"):
            out_tmp = f"/tmp/sandown_mux_{show_id}_{safe_slug}.mp4"
            # Re-encoding streams to standard codecs (H264/AAC) is necessary for robust muxing
            cmd = ["ffmpeg", "-y", "-loglevel", "info",
                   "-i", out_file, "-i", audio_tmp,
                   "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                   "-movflags", "+faststart", out_tmp]
            print("Muxing:", " ".join(cmd))
            proc = subprocess.run(cmd)
            if proc.returncode != 0:
                sys.exit(f"ffmpeg mux failed with exit code {proc.returncode}")
            shutil.move(out_tmp, out_file)
        else:
            sys.exit("ffmpeg not found; cannot combine audio with video.")

        os.remove(audio_tmp)
    else:
        print("No audio track found for this show.")

    size_mb = os.path.getsize(out_file) / (1024 * 1024)
    print(f"Done. Saved {out_file} ({size_mb:.1f} MB)")

    return out_file


def _abbr_from_slug(slug: str) -> str:
    """Derive a short meeting abbreviation from a CableCast show slug.

    Matches the project's existing file conventions (e.g. 'bos' for Board of
    Selectmen, 'zba' for Zoning Board of Adjustment). Falls back to the first
    word of the slug if no known type is present.
    """
    low = slug.lower()
    if "zoning" in low or "adjustment" in low or "zba" in low:
        return "zba"
    if "selectmen" in low or "board of selectmen" in low or "bos" in low:
        return "bos"
    first = re.split(r"[-\s]", slug)[0]
    return re.sub(r"[^a-z0-9]", "", first.lower()) or "meeting"


def derive_slug_info(slug: str):
    """Return (date_str, abbreviation) parsed from a CableCast show slug.

    Slugs embed the meeting date as MM-DD-YYYY (e.g. 'Board-of-Selectmen-8-31-2026-v2');
    we turn that into a YYYYmmdd string for the transcript filename and derive a
    short abbreviation from the meeting type. Returns (None, abbr) if no date is found.
    """
    m = re.search(r"(\d{1,2})-(\d{1,2})-(\d{4})", slug)
    abbr = _abbr_from_slug(slug)
    if not m:
        return None, abbr
    month, day, year = m.groups()
    return f"{year}{int(month):02d}{int(day):02d}", abbr


def transcribe_video(video_path: str, log_dir: str, date_str: str | None,
                     abbr: str, model: str = "turbo", device: str | None = None) -> str:
    """Run whisper on a video file and write "<date>-<abbr>_transcript.log".

    ffmpeg extracts a mono 16 kHz wav; whisper transcribes it to SRT. The .log
    mirrors the project's existing transcripts: whisper's language-detection
    lines followed by the timestamped transcript body. Returns the log path.
    """
    if not shutil.which("ffmpeg"):
        sys.exit("ffmpeg not found; cannot extract audio for transcription.")

    base = f"{date_str}-{abbr}" if date_str else abbr
    out_path = os.path.join(log_dir, f"{base}.log")

    with tempfile.TemporaryDirectory(prefix="sandown_audio_") as tmp:
        audio_path = os.path.join(tmp, f"{base}.wav")
        cmd_ffmpeg = ["ffmpeg", "-y", "-loglevel", "error", "-i", video_path,
                      "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", audio_path]
        if subprocess.run(cmd_ffmpeg).returncode != 0:
            sys.exit("ffmpeg could not extract an audio track from the video.")

        print(f"Transcribing with whisper ({model}) -> {out_path}")
        captured = os.path.join(tmp, "whisper_capture.txt")
        cmd_whisper = ["whisper", audio_path, "--model", model,
                       "--output_dir", log_dir, "--output_format", "srt"]
        if device:
            cmd_whisper += ["--device", device]
        else:
            # Explicitly set to CPU if not specified
            cmd_whisper += ["--device", "cpu"]
        with open(captured, "w") as cap:
            proc = subprocess.run(cmd_whisper, stdout=cap, stderr=subprocess.STDOUT)
        if proc.returncode != 0:
            sys.exit(f"whisper failed with exit code {proc.returncode}")

        srt_path = os.path.join(log_dir, f"{base}.srt")
        if not os.path.exists(srt_path):
            sys.exit("whisper did not produce the expected transcript.")

        header = _extract_header(captured)
        with open(srt_path, encoding="utf-8") as fh:
            srt_text = fh.read().strip("\n")

        with open(out_path, "w", encoding="utf-8") as out:
            if header:
                out.write(header + "\n\n")
            out.write(srt_text + "\n")

        os.remove(srt_path)  # keep only the .log artifact

    return out_path


def _extract_header(captured_path: str) -> str:
    """Return diagnostic lines (e.g. language detection) preceding the first SRT
    timestamp block in whisper's captured output."""
    with open(captured_path, encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    end = 0
    for i, line in enumerate(lines):
        if re.match(r"\s*\[\d+:\d+", line):
            break
        end = i + 1
    return "\n".join(lines[:end]).strip()


def _progress_loop(stop: "threading.Event", out_file: str) -> None:
    """Print a live byte-count for ``out_file`` until ``stop`` is set.

    Runs on a background thread; the caller joins it after the transfer ends so
    the final count line prints cleanly. Prints nothing if the file never grows
    (e.g. an empty response) to avoid misleading output.
    """
    last = os.path.getsize(out_file) if os.path.exists(out_file) else 0
    while not stop.is_set():
        cur = os.path.getsize(out_file) if os.path.exists(out_file) else 0
        if cur != last:
            print(f"  ... {cur / (1024 * 1024):.1f} MB", flush=True)
            last = cur
        stop.wait(1)


def _run_with_progress(cmd: list[str]) -> None:
    """Run a curl command, printing progress until it finishes or exits."""
    out_file = cmd[cmd.index("-o") + 1]
    stop = threading.Event()
    thread = threading.Thread(target=_progress_loop, args=(stop, out_file),
                              daemon=True)
    thread.start()
    proc = subprocess.run(cmd)
    stop.set()
    thread.join()
    if proc.returncode != 0:
        sys.exit(f"download failed with exit code {proc.returncode} ({cmd[-1]})")


def _copyfileobj_with_progress(resp, fh) -> None:
    """Copy from an HTTP response to a file handle, printing progress."""
    stop = threading.Event()
    thread = threading.Thread(target=_progress_loop, args=(stop, fh.name),
                              daemon=True)
    thread.start()
    shutil.copyfileobj(resp, fh)
    stop.set()
    thread.join()


def _download_one(url: str, out_file: str):
    """Download a file with curl (resume-capable) or urllib fallback.

    curl is capped by --max-time so a stalled transfer cannot hang the script;
    on timeout it retries up to DOWNLOAD_TIMEOUT seconds total before giving up.
    A live progress meter prints bytes received every second while the download
    runs.
    """
    if shutil.which("curl"):
        cmd = ["curl", "-L", "--retry", "3", "--max-time", str(DOWNLOAD_TIMEOUT),
               "--connect-timeout", str(CONNECT_TIMEOUT), "-o", out_file, url]
        if os.path.exists(out_file) and os.path.getsize(out_file) > 0:
            cmd = ["curl", "-L", "-C", "-", "--max-time", str(DOWNLOAD_TIMEOUT),
                   "--connect-timeout", str(CONNECT_TIMEOUT),
                   "--retry", "3", "-o", out_file, url]
            print(f"Resuming {os.path.basename(out_file)} "
                  f"({os.path.getsize(out_file)} bytes so far)")
        _run_with_progress(cmd)
    else:
        start = os.path.getsize(out_file) if os.path.exists(out_file) else 0
        req = urllib.request.Request(url, headers={"User-Agent": "curl/8"})
        if start > 0:
            req.add_header("Range", f"bytes={start}-")
        with urllib.request.urlopen(req, timeout=DOWNLOAD_TIMEOUT) as resp:
            mode = "ab" if start > 0 else "wb"
            with open(out_file, mode) as fh:
                _copyfileobj_with_progress(resp, fh)


def _download_media(layout: dict, out_file: str):
    """Download a media layout (combined file or segmented fMP4) to ``out_file``.

    A combined-file layout is one HTTP fetch. A segmented layout downloads the
    init segment then every ``*.m4s`` in order; each file lives under the show's
    own directory on the server, so URLs are prefixed with that directory. The
    result is a single concatenated fMP4 file that ffmpeg can demux.
    """
    kind = layout["kind"]

    if kind == "file":
        # Combined file: a single self-contained download. The file lives under
        # the show's own directory on the server, so prefix it with that dir.
        path = layout["path"]
        return _download_one(f"{BASE}/store-3/{layout['dir']}/{path}", out_file)

    # Segmented: write the init segment, then append each segment in order. The
    # files live under the show's own directory on the server (not at the root
    # of /store-3/), so every URL is prefixed with that directory.
    if os.path.exists(out_file):
        os.remove(out_file)
    with open(out_file, "wb") as fh:
        for name in [layout["init"], *layout["segments"]]:
            url = f"{BASE}/store-3/{layout['dir']}/{name}"
            data = fetch(url)
            if not data:
                sys.exit(f"Empty response downloading {url}")
            fh.write(data)
    print(f"Downloaded {len(layout['segments']) + 1} files "
          f"(init + {len(layout['segments'])} segments)")


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--channel", default="1", help="CableCast channel id (default 1)")
    parser.add_argument("--res", default="1080p",
                        help="Resolution tier: 1080p, 720p, 480p, 360p (default 1080p)")
    parser.add_argument("--out", default="./downloads", help="Output directory")
    parser.add_argument("--list", action="store_true",
                        help="Only print the latest show and exit (no download)")
    parser.add_argument("--log-dir", default=None,
                        help="Directory for the transcript log "
                             "(default: same as --out)")
    parser.add_argument("--model", default="turbo",
                        help="Whisper model name (default: turbo)")
    parser.add_argument("--device", default=None,
                        help="PyTorch device for whisper (cuda/cpu; default: auto)")
    parser.add_argument("--keep-video", action="store_true",
                        help="Keep the downloaded video after transcription")
    parser.add_argument("--no-transcribe", action="store_true",
                        help="Skip whisper transcription and keep the video")
    parser.add_argument("--from", dest="start", default=None, metavar="YYYY-MM-DD",
                        help="Download every show on or after this date "
                             "(inclusive; with --to, restricts to a range)")
    parser.add_argument("--to", dest="end", default=None, metavar="YYYY-MM-DD",
                        help="Download every show on or before this date "
                             "(inclusive; use together with --from for a range)")
    args = parser.parse_args()

    # Date-range mode: download every matching show.
    if args.start or args.end:
        start = date.fromisoformat(args.start) if args.start else date.min
        end = date.fromisoformat(args.end) if args.end else date.max
        shows = resolve_shows_in_range(args.channel, start, end)
        if not shows:
            sys.exit(f"No shows found on channel {args.channel} in the requested range.")
        print(f"{len(shows)} show(s) found on channel {args.channel} "
              f"between {start.isoformat()} and {end.isoformat()}:")
        for s in shows:
            print(f"  - {s['date'].date().isoformat()}  {s['show_id']} - {s['slug']}")
        log_dir = args.log_dir or args.out
        failed = []
        for show in shows:
            try:
                video_path = download(show["show_id"], show["slug"], args.res, args.out)
            except SystemExit as e:
                print(f"Download failed for {show['slug']}: {e}")
                failed.append(show["slug"])
                continue

            date_str, abbr = derive_slug_info(show["slug"])
            if not args.no_transcribe:
                try:
                    transcript = transcribe_video(video_path, log_dir, date_str, abbr,
                                                  model=args.model, device=args.device)
                    print(f"Transcript saved to {transcript}")
                except SystemExit as e:
                    print(f"Transcription failed for {show['slug']}: {e}")
            else:
                print("Skipping transcription (--no-transcribe).")

            if not args.keep_video:
                os.remove(video_path)
                print(f"Removed downloaded video {video_path}.")

        if failed:
            sys.exit(f"{len(failed)} show(s) failed: {', '.join(failed)}")
        return

    # Single-show mode (unchanged behaviour).
    latest = resolve_channel(args.channel)
    if not latest:
        sys.exit(f"No shows found on channel {args.channel}.")
    show_id, slug = latest

    print(f"Latest on channel {args.channel}: {show_id} - {slug}")

    if args.list:
        return

    video_path = download(show_id, slug, args.res, args.out)

    date_str, abbr = derive_slug_info(slug)
    log_dir = args.log_dir or args.out

    if not args.no_transcribe:
        transcript = transcribe_video(video_path, log_dir, date_str, abbr,
                                      model=args.model, device=args.device)
        print(f"Transcript saved to {transcript}")
    else:
        print("Skipping transcription (--no-transcribe).")

    if not args.keep_video:
        os.remove(video_path)
        print(f"Removed downloaded video {video_path}.")


if __name__ == "__main__":
    main()
