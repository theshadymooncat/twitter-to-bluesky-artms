import os
import json
import re
import subprocess
import tempfile
import requests
import feedparser
from bs4 import BeautifulSoup
from atproto import Client, models

NITTER_RSS = "https://nitter.net/official_artms/rss"
BLUESKY_HANDLE = os.environ["BLUESKY_HANDLE"]
BLUESKY_PASSWORD = os.environ["BLUESKY_PASSWORD"]
STATE_FILE = "seen_ids.json"


def load_seen():
    try:
        return set(json.load(open(STATE_FILE)))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()


def save_seen(seen):
    with open(STATE_FILE, "w") as f:
        json.dump(list(seen), f)


def fetch_media_from_vxtwitter(tweet_path):
    """
    Use the api.vxtwitter.com JSON API to get media URLs for a tweet.
    Returns (video_url, image_urls) where video_url may be None.
    """
    api_url = "https://api.vxtwitter.com" + tweet_path
    try:
        print(f"Fetching media from vxtwitter API: {api_url}")
        resp = requests.get(
            api_url,
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10
        )
        data = resp.json()

        media_urls = data.get("mediaURLs", [])
        video_url = None
        image_urls = []

        for url in media_urls:
            if url.endswith(".mp4") or "video" in url:
                if video_url is None:
                    video_url = url
            else:
                image_urls.append(url)

        # Also check the structured media array if present
        for media in data.get("media_extended", []):
            mtype = media.get("type", "")
            url = media.get("url", "")
            if mtype == "video" and video_url is None:
                video_url = url
            elif mtype == "image" and url not in image_urls:
                image_urls.append(url)

        return video_url, image_urls

    except Exception as e:
        print(f"vxtwitter API fetch failed: {e}")

    return None, []


def fetch_tweets():
    feed = feedparser.parse(NITTER_RSS)
    tweets = []

    for entry in feed.entries[:10]:
        if entry.title.startswith("RT by") or entry.title.startswith("R to"):
            continue

        tweet_id = entry.guid
        text = entry.title

        soup = BeautifulSoup(entry.description, "html.parser")

        images = []
        video_url = None
        tweet_path = None
        has_video = False

        # Detect video using RSS pattern
        for a in soup.find_all("a", href=True):
            if "/status/" in a["href"]:
                href = a["href"].split("#")[0]

                # Normalize to path only
                if href.startswith("http"):
                    # Remove domain (nitter or twitter)
                    tweet_path = "/" + href.split("/", 3)[3]
                else:
                    tweet_path = href
                img = a.find("img")
                if img:
                    src = img.get("src", "")
                    if "amplify_video_thumb" in src:
                        has_video = True
                        break

        if has_video and tweet_path:
            video_url, api_images = fetch_media_from_vxtwitter(tweet_path)
            if api_images and not images:
                images = api_images
            print(f"Video detected → {video_url}")
        elif tweet_path and not images:
            # Try API for images too (catches cases where RSS missed them)
            _, api_images = fetch_media_from_vxtwitter(tweet_path)
            if api_images:
                images = api_images
                print(f"Images from API → {len(images)}")
        else:
            print("No video in tweet")

        # Extract images (skip video thumbnails)
        for img in soup.find_all("img"):
            parent = img.find_parent("a")
            if parent and "/status/" in parent.get("href", ""):
                continue

            src = img.get("src", "")
            src = src.replace("https://nitter.net/pic/", "https://pbs.twimg.com/")
            src = requests.utils.unquote(src)
            images.append(src)

        tweets.append({
            "id": tweet_id,
            "text": text,
            "images": images,
            "video_url": video_url
        })

    print(f"Fetched {len(tweets)} tweets")
    return tweets


BSKY_MAX_DURATION = 180        # 3 minutes in seconds
BSKY_MAX_BYTES    = 100 * 1024 * 1024  # 100 MB
BSKY_MAX_HEIGHT   = 1080


def probe_video(path):
    """Return (width, height, duration_seconds) for a local video file."""
    try:
        result = subprocess.run([
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height",
            "-show_entries", "format=duration",
            "-of", "json",
            path
        ], capture_output=True, timeout=30)
        info = json.loads(result.stdout)
        streams = info.get("streams", [{}])
        fmt = info.get("format", {})
        width    = int(streams[0].get("width",  0)) if streams else 0
        height   = int(streams[0].get("height", 0)) if streams else 0
        duration = float(fmt.get("duration", 0))
        return width, height, duration
    except Exception as e:
        print(f"ffprobe failed: {e}")
        return 0, 0, 0


def transcode_video(src_path):
    """
    Re-encode src_path to meet Bluesky limits:
      - max 1080p (scale down, keep aspect ratio)
      - max 3 minutes
      - target under 100 MB via bitrate cap
    Returns path to the new file (caller must unlink both), or None on failure.
    """
    width, height, duration = probe_video(src_path)
    print(f"Source: {width}x{height}, {duration:.1f}s")

    out = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
    out.close()

    # Scale filter: only downscale when taller than 1080p.
    # -2 keeps aspect ratio and ensures width is divisible by 2 (libx264 req).
    if height > BSKY_MAX_HEIGHT:
        scale_filter = f"scale=-2:{BSKY_MAX_HEIGHT}"
        print(f"Downscaling to {BSKY_MAX_HEIGHT}p")
    else:
        scale_filter = "scale=trunc(iw/2)*2:trunc(ih/2)*2"  # ensure even dims

    trim_duration = min(duration, BSKY_MAX_DURATION) if duration > 0 else BSKY_MAX_DURATION

    # Estimate a safe video bitrate to stay under 100 MB total.
    # Leave ~128 kbps headroom for audio.
    safe_video_kbps = max(500, int((BSKY_MAX_BYTES * 8 / trim_duration) / 1000) - 128)

    cmd = [
        "ffmpeg", "-y",
        "-protocol_whitelist", "file,http,https,tcp,tls,crypto",
        "-i", src_path,
        "-t", str(trim_duration),
        "-vf", scale_filter,
        "-c:v", "libx264",
        "-preset", "fast",
        "-crf", "23",
        "-maxrate", f"{safe_video_kbps}k",
        "-bufsize", f"{safe_video_kbps * 2}k",
        "-c:a", "aac",
        "-b:a", "128k",
        "-movflags", "+faststart",
        out.name
    ]

    result = subprocess.run(cmd, capture_output=True, timeout=300)
    if result.returncode == 0:
        size = os.path.getsize(out.name)
        print(f"Transcoded: {size / 1024 / 1024:.1f} MB")
        return out.name
    else:
        print(f"Transcode error: {result.stderr.decode()}")
        os.unlink(out.name)
        return None


def download_video(url):
    """
    Download video from url. Re-encode if it exceeds Bluesky's limits
    (1080p, 3 min, 100 MB). Returns path to a ready-to-upload .mp4, or None.
    """
    try:
        tmp = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
        tmp.close()

        # Download raw stream first (no re-encode)
        result = subprocess.run([
            "ffmpeg", "-y",
            "-protocol_whitelist", "file,http,https,tcp,tls,crypto",
            "-i", url,
            "-c", "copy",
            tmp.name
        ], capture_output=True, timeout=120)

        if result.returncode != 0:
            print(f"ffmpeg download error: {result.stderr.decode()}")
            os.unlink(tmp.name)
            return None

        # Check whether transcoding is needed
        width, height, duration = probe_video(tmp.name)
        size = os.path.getsize(tmp.name)
        needs_transcode = (
            height > BSKY_MAX_HEIGHT
            or duration > BSKY_MAX_DURATION
            or size > BSKY_MAX_BYTES
        )

        if needs_transcode:
            print(
                f"Exceeds limits ({width}x{height}, {duration:.1f}s, "
                f"{size / 1024 / 1024:.1f} MB) — transcoding…"
            )
            transcoded = transcode_video(tmp.name)
            os.unlink(tmp.name)
            return transcoded
        else:
            print(
                f"Within limits ({width}x{height}, {duration:.1f}s, "
                f"{size / 1024 / 1024:.1f} MB) — using as-is"
            )
            return tmp.name

    except Exception as e:
        print(f"Download failed: {e}")
        return None


def parse_facets(text):
    facets = []

    for match in re.finditer(r'https?://[^\s]+', text):
        start = len(text[:match.start()].encode("utf-8"))
        end = len(text[:match.end()].encode("utf-8"))
        facets.append({
            "index": {"byteStart": start, "byteEnd": end},
            "features": [{"$type": "app.bsky.richtext.facet#link", "uri": match.group()}]
        })

    for match in re.finditer(r'#\w+', text):
        tag = match.group()[1:]
        start = len(text[:match.start()].encode("utf-8"))
        end = len(text[:match.end()].encode("utf-8"))
        facets.append({
            "index": {"byteStart": start, "byteEnd": end},
            "features": [{"$type": "app.bsky.richtext.facet#tag", "tag": tag}]
        })

    return facets


def upload_video_to_bsky(bsky, video_path):
    """
    Upload a video blob directly to the PDS via uploadBlob (simple method).
    Returns (blob, width, height) on success, or (None, 0, 0) on failure.
    """
    try:
        width, height, _ = probe_video(video_path)
        with open(video_path, "rb") as f:
            video_data = f.read()
        upload = bsky.upload_blob(video_data)
        print(f"Uploaded video ({len(video_data) / 1024 / 1024:.1f} MB)")
        return upload.blob, width, height
    except Exception as e:
        print(f"Video upload error: {e}")
        return None, 0, 0
        return None, 0, 0


def post_to_bluesky(text, images, video_url):
    try:
        bsky = Client()
        bsky.login(BLUESKY_HANDLE, BLUESKY_PASSWORD)

        facets = parse_facets(text)
        embed = None

        if video_url:
            video_path = download_video(video_url)
            if video_path:
                blob, width, height = upload_video_to_bsky(bsky, video_path)
                os.unlink(video_path)
                if blob:
                    embed_kwargs = {"video": blob}
                    if width and height:
                        embed_kwargs["aspect_ratio"] = models.AppBskyEmbedDefs.AspectRatio(
                            width=width, height=height
                        )
                    embed = models.AppBskyEmbedVideo.Main(**embed_kwargs)
                else:
                    print("Video upload to Bluesky failed")
            else:
                print("Video download failed")

        elif images:
            image_blobs = []
            for url in images[:4]:
                try:
                    resp = requests.get(url, timeout=10)
                    blob = bsky.upload_blob(resp.content)
                    image_blobs.append(blob.blob)
                except Exception as e:
                    print(f"Image upload failed: {e}")

            if image_blobs:
                embed = models.AppBskyEmbedImages.Main(
                    images=[
                        models.AppBskyEmbedImages.Image(image=blob, alt="")
                        for blob in image_blobs
                    ]
                )

        print(f"Sending post with embed type: {type(embed).__name__ if embed else None}")
        bsky.send_post(
            text=text[:300],
            facets=facets if facets else None,
            embed=embed
        )

        print("Posted:", text[:60])

    except Exception as e:
        print(f"Bluesky error: {e}")


def main():
    seen = load_seen()
    tweets = fetch_tweets()

    for tw in reversed(tweets):
        if tw["id"] in seen:
            continue

        print("Reposting:", tw["text"][:80])
        post_to_bluesky(tw["text"], tw["images"], tw["video_url"])
        seen.add(tw["id"])

    save_seen(seen)


if __name__ == "__main__":
    main()
