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
NITTER_BASE = "https://nitter.net"
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

        for a in soup.find_all("a", href=True):
            img = a.find("img")
            if img and "/status/" in a["href"]:
                # This is a video — fetch the nitter page to get the video URL
                status_url = NITTER_BASE + a["href"].replace(NITTER_BASE, "")
                try:
                    resp = requests.get(status_url, timeout=10)
                    status_soup = BeautifulSoup(resp.text, "html.parser")
                    # Look for mp4 source first
                    source = status_soup.find("source", {"type": "video/mp4"})
                    if source:
                        src = source.get("src", "")
                        if src.startswith("/"):
                            src = NITTER_BASE + src
                        video_url = src
                    else:
                        # Fall back to m3u8
                        source = status_soup.find("source", {"type": "application/x-mpegURL"})
                        if source:
                            src = source.get("src", "")
                            if src.startswith("/"):
                                src = NITTER_BASE + src
                            video_url = src
                except Exception as e:
                    print(f"Failed to fetch video URL: {e}")

        for img in soup.find_all("img"):
            parent = img.find_parent("a")
            if parent and "/status/" in parent.get("href", ""):
                continue  # skip video thumbnails
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

def download_video(url):
    """Download video using ffmpeg, returns path to mp4 file or None."""
    try:
        tmp = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
        tmp.close()
        result = subprocess.run([
            "ffmpeg", "-y",
            "-protocol_whitelist", "file,http,https,tcp,tls,crypto",
            "-i", url,
            "-c", "copy",
            "-t", "60",  # max 60 seconds to avoid huge files
            tmp.name
        ], capture_output=True, timeout=120)
        if result.returncode == 0:
            return tmp.name
        else:
            print(f"ffmpeg error: {result.stderr.decode()}")
            return None
    except Exception as e:
        print(f"Failed to download video: {e}")
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

def post_to_bluesky(text, images, video_url):
    try:
        bsky = Client()
        bsky.login(BLUESKY_HANDLE, BLUESKY_PASSWORD)
        facets = parse_facets(text)
        embed = None

        if video_url:
            video_path = download_video(video_url)
            if video_path:
                with open(video_path, "rb") as f:
                    video_data = f.read()
                os.unlink(video_path)
                upload = bsky.upload_blob(video_data)
                embed = models.AppBskyEmbedVideo.Main(
                    video=upload.blob
                )
            else:
                print("Video download failed, skipping video embed")

        elif images:
            image_blobs = []
            for url in images[:4]:
                try:
                    resp = requests.get(url, timeout=10)
                    blob = bsky.upload_blob(resp.content)
                    image_blobs.append(blob.blob)
                except Exception as e:
                    print(f"Failed to upload image {url}: {e}")
            if image_blobs:
                embed = models.AppBskyEmbedImages.Main(
                    images=[
                        models.AppBskyEmbedImages.Image(image=blob, alt="")
                        for blob in image_blobs
                    ]
                )

        bsky.send_post(
            text=text[:300],
            facets=facets if facets else None,
            embed=embed
        )
        print("Posted to Bluesky:", text[:60])
    except Exception as e:
        print(f"Error posting to Bluesky: {e}")

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
