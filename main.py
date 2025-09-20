import re
import urllib.parse
from typing import List, Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse, JSONResponse, Response
from youtube_transcript_api import (
    YouTubeTranscriptApi,
    TranscriptsDisabled,
    NoTranscriptFound,
    VideoUnavailable,
)

APP_TITLE = "YT Transcript Local API"
APP_VERSION = "1.0"
HOST = "127.0.0.1"
PORT = 17653  # keep in sync with index.html

app = FastAPI(title=APP_TITLE, version=APP_VERSION)

# Wide-open CORS so a GitHub Pages UI can call http://127.0.0.1:17653
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Add Private Network Access header for preflights from HTTPS pages
@app.middleware("http")
async def add_pna_header(request: Request, call_next):
    response: Response = await call_next(request)
    if request.method == "OPTIONS":
        response.headers["Access-Control-Allow-Private-Network"] = "true"
    return response

ID_RE = re.compile(r"^[a-zA-Z0-9_-]{11}$")

def extract_video_id(url_or_id: str) -> str:
    s = (url_or_id or "").strip()
    if not s:
        raise HTTPException(status_code=400, detail="Missing url or videoId")
    if ID_RE.match(s):
        return s
    try:
        u = urllib.parse.urlparse(s)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid URL")
    host = (u.hostname or "").lower().replace("www.", "")
    path = u.path or ""
    qs = urllib.parse.parse_qs(u.query or "")

    # youtu.be/<id>
    if host == "youtu.be":
        segs = [p for p in path.split("/") if p]
        if segs and ID_RE.match(segs[0]):
            return segs[0]

    # youtube.com/watch?v=<id> or /shorts/<id> /embed/<id> /live/<id>
    if host.endswith("youtube.com") or host.endswith("youtube-nocookie.com"):
        if path == "/watch":
            v = (qs.get("v") or [None])[0]
            if v and ID_RE.match(v):
                return v
        segs = [p for p in path.split("/") if p]
        if len(segs) >= 2 and segs[0] in ("shorts", "embed", "live"):
            if ID_RE.match(segs[1]):
                return segs[1]

    raise HTTPException(status_code=400, detail="Could not extract videoId")

def to_plain_text(snippets: List[dict]) -> str:
    lines = []
    for snip in snippets:
        t = (snip.get("text") or "").strip()
        if t:
            lines.append(t)
    return "\\n".join(lines).strip()

@app.get("/api/ping")
def ping():
    return {"ok": True, "service": APP_TITLE, "version": APP_VERSION}

@app.get("/api/transcript")
def get_transcript(
    url: Optional[str] = Query(default=None, description="YouTube share URL"),
    videoId: Optional[str] = Query(default=None, description="11-char video id"),
    lang: str = Query(default="en,en-US,en-GB", description="comma list of langs"),
    prefer_asr: bool = Query(default=False, description="prefer auto if manual missing"),
    format: str = Query(default="txt", description="txt or json"),
    preserve_formatting: bool = Query(default=False, description="keep <i>/<b> if true"),
):
    vid = videoId or (extract_video_id(url) if url else None)
    if not vid:
        raise HTTPException(status_code=400, detail="Provide ?url= or ?videoId=")

    langs = [x.strip() for x in lang.split(",") if x.strip()]
    yta = YouTubeTranscriptApi()

    try:
        tlist = yta.list(vid)  # TranscriptList
        transcript = None

        if not prefer_asr:
            try:
                transcript = tlist.find_manually_created_transcript(langs)
            except Exception:
                pass
        if transcript is None:
            try:
                transcript = tlist.find_generated_transcript(langs)
            except Exception:
                pass
        if transcript is None:
            transcript = tlist.find_transcript(langs)

        fetched = transcript.fetch(preserve_formatting=preserve_formatting)
        # Ensure raw dicts regardless of library minor version
        raw = (
            [dict(s) for s in fetched.to_raw_data()]
            if hasattr(fetched, "to_raw_data")
            else fetched
        )

        if format == "json":
            return JSONResponse(
                {
                    "videoId": vid,
                    "language": getattr(transcript, "language_code", None),
                    "generated": getattr(transcript, "is_generated", None),
                    "snippets": raw,
                },
                headers={"Cache-Control": "public, max-age=86400"},
            )

        text = to_plain_text(raw)
        if not text:
            raise HTTPException(status_code=502, detail="Empty transcript returned")
        return PlainTextResponse(text, headers={"Cache-Control": "public, max-age=86400"})

    except TranscriptsDisabled:
        raise HTTPException(status_code=403, detail="Transcripts disabled for this video")
    except NoTranscriptFound:
        raise HTTPException(status_code=404, detail="No transcript found for requested languages")
    except VideoUnavailable:
        raise HTTPException(status_code=404, detail="Video unavailable")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Unexpected error: {e}")
