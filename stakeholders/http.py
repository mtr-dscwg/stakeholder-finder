"""All network access goes through here, so tests can swap it out in one place.

Wikidata and GLEIF ask callers to identify themselves with a descriptive
User-Agent, so set STAKEHOLDERS_CONTACT to a URL or email you own.
API responses are cached on disk for a week to keep load on them low.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

CONTACT = os.environ.get("STAKEHOLDERS_CONTACT", "https://menturu.dev")
USER_AGENT = f"stakeholder-finder/0.1 ({CONTACT})"
TIMEOUT = 20
MAX_ARTICLE_BYTES = 5_000_000
CACHE_DIR = Path(os.environ.get("STAKEHOLDERS_CACHE", Path.home() / ".cache" / "stakeholders"))
CACHE_TTL = 7 * 24 * 3600


class FetchError(Exception):
    pass


def _session() -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = USER_AGENT
    # Back off and retry on rate limits and server hiccups.
    retry = Retry(total=3, backoff_factor=1.5, status_forcelist=[429, 500, 502, 503, 504],
                  allowed_methods=["GET"], respect_retry_after_header=True)
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.mount("http://", HTTPAdapter(max_retries=retry))
    return s


_SESSION = _session()


def _cache_path(url: str, params: dict | None) -> Path:
    key = url + "?" + json.dumps(params or {}, sort_keys=True)
    return CACHE_DIR / (hashlib.sha1(key.encode()).hexdigest() + ".json")


def get_json(url: str, params: dict | None = None, cache: bool = True) -> dict | None:
    """GET a JSON API. Returns None on 404 (GLEIF uses it for "no parent")."""
    path = _cache_path(url, params)
    if cache and path.exists() and time.time() - path.stat().st_mtime < CACHE_TTL:
        return json.loads(path.read_text())

    try:
        r = _SESSION.get(url, params=params, timeout=TIMEOUT,
                         headers={"Accept": "application/json, application/sparql-results+json"})
    except requests.RequestException as exc:
        raise FetchError(f"{url}: {exc}") from exc
    if r.status_code == 404:
        data = None
    elif not r.ok:
        raise FetchError(f"{url}: HTTP {r.status_code}")
    else:
        try:
            data = r.json()
        except ValueError as exc:
            raise FetchError(f"{url}: response wasn't JSON") from exc

    if cache:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))
    return data


def fetch_article(url: str) -> tuple[str, str]:
    """Download an article page. Returns (html, final_url after redirects)."""
    try:
        r = _SESSION.get(url, timeout=TIMEOUT, stream=True,
                         headers={"Accept": "text/html,application/xhtml+xml"})
        r.raise_for_status()
        body = r.raw.read(MAX_ARTICLE_BYTES + 1, decode_content=True)
    except requests.RequestException as exc:
        raise FetchError(f"Couldn't fetch {url}: {exc}") from exc
    if len(body) > MAX_ARTICLE_BYTES:
        raise FetchError(f"{url} is larger than {MAX_ARTICLE_BYTES // 1_000_000} MB")
    encoding = r.encoding if r.encoding and r.encoding.lower() != "iso-8859-1" else "utf-8"
    return body.decode(encoding, errors="replace"), r.url
