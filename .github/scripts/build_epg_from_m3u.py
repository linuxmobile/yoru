#!/usr/bin/env python3
"""Rebuild curated XMLTV files from the checked-in M3U playlists.

Designed for the GitHub Actions workflow in this repository. It downloads
fresh EPG sources, keeps only the tvg-ids present in curated.m3u8 and
curated_no_epg.m3u8, and writes matching XMLTV files with all <channel>
elements before any <programme> element (required by Nuvio).
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

GZIP_MAGIC = b"\x1f\x8b"
ATTR_RE = re.compile(r'([\w-]+)="([^"]*)"')
DATE_RE = re.compile(r"^(\d{8})")
STUB_MARKERS = (
    "Overnight - Live TV",
    "Morning - Live TV",
    "Afternoon - Live TV",
    "Evening - Live TV",
)


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def strip_tag(tag: Any) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def parse_playlist(path: Path) -> list[tuple[str, str, str]]:
    """Return ordered (tvg-id, display-name, logo) triples from an M3U."""
    out: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    pending: tuple[str, str, str] | None = None
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if line.startswith("#EXTINF"):
                attrs = dict(ATTR_RE.findall(line))
                name = line.split(",", 1)[1].strip() if "," in line else attrs.get("tvg-name", "")
                pending = (
                    (attrs.get("tvg-id") or "").strip(),
                    name or attrs.get("tvg-name", "") or attrs.get("tvg-id", ""),
                    (attrs.get("tvg-logo") or "").strip(),
                )
            elif pending and line and not line.startswith("#"):
                cid, name, logo = pending
                if cid and cid not in seen:
                    seen.add(cid)
                    out.append((cid, name, logo))
                pending = None
    return out


def open_auto(path: Path):
    with path.open("rb") as raw:
        magic = raw.read(2)
    return gzip.open(path, "rb") if magic == GZIP_MAGIC else path.open("rb")


def download_source(url: str, destination: Path, timeout: float, retries: int = 3) -> None:
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        tmp = destination.with_name(destination.name + ".part")
        try:
            request = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0 (compatible; yoru-epg-refresh/1.0)",
                    "Accept": "application/xml,text/xml,application/gzip,*/*",
                },
            )
            with urllib.request.urlopen(request, timeout=timeout) as response, tmp.open("wb") as out:
                shutil.copyfileobj(response, out, length=1024 * 1024)
            if tmp.stat().st_size < 32:
                raise OSError("download too small")
            magic = tmp.open("rb").read(2)
            if magic == GZIP_MAGIC:
                with gzip.open(tmp, "rb") as check:
                    if b"<tv" not in check.read(4096):
                        raise OSError("gzip payload is not XMLTV")
            elif b"<tv" not in tmp.open("rb").read(8192):
                raise OSError("payload is not XMLTV")
            tmp.replace(destination)
            return
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            tmp.unlink(missing_ok=True)
            if attempt < retries:
                time.sleep(2 * attempt)
    raise RuntimeError(f"failed to download {url}: {last_error}")


def collect_sources(
    source_urls: list[str],
    timeout: float,
    workers: int,
    work_dir: Path,
) -> list[Path]:
    paths: list[Path] = []

    def fetch(index_url: tuple[int, str]) -> Path | None:
        index, url = index_url
        destination = work_dir / f"source-{index:03d}.xml"
        try:
            download_source(url, destination, timeout)
            return destination
        except Exception as exc:  # noqa: BLE001
            log(f"  skip {url}: {exc}")
            return None

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(fetch, item) for item in enumerate(source_urls)]
        for future in as_completed(futures):
            path = future.result()
            if path is not None:
                paths.append(path)
    return sorted(paths)


def local_sources(directory: Path) -> list[Path]:
    files = [
        p for p in directory.glob("*.xml*")
        if not p.name.startswith("curated") and p.is_file()
    ]
    return sorted(files)


def parse_sources(
    paths: list[Path],
    wanted: set[str],
    max_per_channel: int,
) -> tuple[dict[str, bytes], dict[str, int]]:
    programmes: dict[str, dict[tuple[str, str, str], bytes]] = {cid: {} for cid in wanted}
    counts: dict[str, int] = {}

    for path in paths:
        seen_in_source: set[str] = set()
        current_programme: dict[str, str] | None = None
        current_title = ""
        try:
            with open_auto(path) as fh:
                context = ET.iterparse(fh, events=("start", "end"))
                _event, root = next(context)
                for event, elem in context:
                    tag = strip_tag(elem.tag)
                    if event == "start":
                        if tag == "programme":
                            current_programme = dict(elem.attrib)
                            current_title = ""
                        continue
                    if tag == "channel":
                        cid = (elem.get("id") or "").strip()
                        if cid in wanted:
                            seen_in_source.add(cid)
                        elem.clear()
                        root.clear()
                    elif tag == "title" and current_programme is not None:
                        text = (elem.text or "").strip() or "".join(elem.itertext()).strip()
                        if text:
                            current_title = text
                        elem.clear()
                    elif tag == "programme":
                        if current_programme is not None and current_title:
                            cid = (current_programme.get("channel") or "").strip()
                            if cid in wanted:
                                start = current_programme.get("start") or ""
                                stop = current_programme.get("stop") or ""
                                key = (start, stop, current_title)
                                if len(programmes[cid]) < max_per_channel:
                                    programmes[cid][key] = serialise_programme_bytes(
                                        start, stop, cid, current_title
                                    )
                        current_programme = None
                        current_title = ""
                        elem.clear()
                        root.clear()
        except Exception as exc:  # noqa: BLE001
            log(f"  skip malformed source {path.name}: {exc}")
            continue
        for cid in seen_in_source:
            counts[cid] = counts.get(cid, 0) + 1

    return (
        {cid: b"".join(items.values()) for cid, items in programmes.items()},
        counts,
    )


def serialise_programme_bytes(start: str, stop: str, cid: str, title: str) -> bytes:
    """Serialise a programme with a stable, non-empty <title> element."""
    prog = ET.Element("programme", {"start": start, "stop": stop, "channel": cid})
    node = ET.SubElement(prog, "title", {"lang": "en"})
    node.text = title
    return ET.tostring(prog, encoding="utf-8", short_empty_elements=False)


def stub_programmes(cid: str, name: str, now: datetime) -> list[tuple[str, str, str]]:
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    slots = [
        (0, 6, "Overnight - Live TV"),
        (6, 12, "Morning - Live TV"),
        (12, 18, "Afternoon - Live TV"),
        (18, 24, "Evening - Live TV"),
    ]
    out = []
    for day in range(7):
        day_start = start + timedelta(days=day)
        for begin, end, title in slots:
            out.append((
                (day_start + timedelta(hours=begin)).strftime("%Y%m%d%H%M%S %z"),
                (day_start + timedelta(hours=end)).strftime("%Y%m%d%H%M%S %z"),
                f"{name}: {title}",
            ))
    return out


def write_xmltv(
    path: Path,
    recs: list[tuple[str, str, str]],
    programmes: dict[str, bytes],
    noepg: bool,
    now: datetime,
) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("wb") as out:
        out.write(b'<?xml version="1.0" encoding="UTF-8"?>\n<tv generator-info-name="yoru epg refresh">\n')
        for cid, name, logo in recs:
            channel = ET.Element("channel", {"id": cid})
            dn = ET.SubElement(channel, "display-name")
            dn.text = name or cid
            if logo:
                ET.SubElement(channel, "icon", {"src": logo})
            out.write(ET.tostring(channel, encoding="utf-8", short_empty_elements=False))
        for cid, name, _logo in recs:
            if noepg:
                for start, stop, title in stub_programmes(cid, name, now):
                    prog = ET.Element("programme", {"start": start, "stop": stop, "channel": cid})
                    node = ET.SubElement(prog, "title", {"lang": "en"})
                    node.text = title
                    out.write(ET.tostring(prog, encoding="utf-8", short_empty_elements=False))
                continue
            data = programmes.get(cid, b"")
            if data:
                out.write(data)
            else:
                for start, stop, title in stub_programmes(cid, name, now):
                    prog = ET.Element("programme", {"start": start, "stop": stop, "channel": cid})
                    node = ET.SubElement(prog, "title", {"lang": "en"})
                    node.text = title
                    out.write(ET.tostring(prog, encoding="utf-8", short_empty_elements=False))
        out.write(b"</tv>\n")
    tmp.replace(path)

    gz_tmp = path.with_name(path.name + ".gz.tmp")
    with path.open("rb") as src, gz_tmp.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as dst:
            shutil.copyfileobj(src, dst, length=1024 * 1024)
    gz_tmp.replace(path.with_name(path.name + ".gz"))


def verify(m3u_path: Path, epg_path: Path, require_real: bool = False) -> None:
    text = m3u_path.read_text(encoding="utf-8")
    header = text.splitlines()[0]
    if "https://" not in header:
        raise RuntimeError(f"{m3u_path.name}: EPG header is not absolute")
    ids = set(re.findall(r'tvg-id="([^"]*)"', text))
    channel_ids: set[str] = set()
    titled = 0
    blank = 0
    real = 0
    stubs = 0
    seen_programme = False
    with gzip.open(epg_path, "rb") as fh:
        for _, elem in ET.iterparse(fh, events=("end",)):
            tag = strip_tag(elem.tag)
            if tag == "channel":
                if seen_programme:
                    raise RuntimeError(f"{epg_path.name}: channel found after programme")
                channel_ids.add((elem.get("id") or "").strip())
                elem.clear()
            elif tag == "programme":
                seen_programme = True
                title = elem.findtext("title")
                if title and title.strip():
                    titled += 1
                    clean = title.strip()
                    if any(clean.endswith(marker) for marker in STUB_MARKERS):
                        stubs += 1
                    else:
                        real += 1
                else:
                    blank += 1
                elem.clear()
    missing = ids - channel_ids
    if missing:
        raise RuntimeError(f"{m3u_path.name}: {len(missing)} ids missing in {epg_path.name}")
    if not titled or blank > max(10, titled // 10000):
        raise RuntimeError(f"{epg_path.name}: titled={titled}, blank={blank}")
    if require_real:
        minimum_real = max(10000, titled // 20)
        if real < minimum_real:
            raise RuntimeError(
                f"{epg_path.name}: only {real} real programmes out of {titled}; "
                f"expected at least {minimum_real} (stub titles: {stubs})"
            )
    log(
        f"verified {m3u_path.name}: {len(ids)} channels, {titled} titled programmes, "
        f"{real} real, {stubs} stubs"
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", default=".")
    ap.add_argument("--sources", default=".github/scripts/epg_sources.json")
    ap.add_argument("--local-dir", help="use local EPG files instead of downloading")
    ap.add_argument("--timeout", type=float, default=120.0)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--max-per-channel", type=int, default=400)
    args = ap.parse_args()

    out_dir = Path(args.out_dir).resolve()
    main_m3u = out_dir / "curated.m3u8"
    noepg_m3u = out_dir / "curated_no_epg.m3u8"
    main_recs = parse_playlist(main_m3u)
    noepg_recs = parse_playlist(noepg_m3u)
    log(f"read {len(main_recs)} main and {len(noepg_recs)} no-EPG channels")

    wanted = {cid for cid, _name, _logo in main_recs + noepg_recs}

    with tempfile.TemporaryDirectory(prefix="yoru-epg-") as tmpdir:
        if args.local_dir:
            paths = local_sources(Path(args.local_dir).resolve())
        else:
            config = json.loads(Path(args.sources).read_text(encoding="utf-8"))
            urls = [item["url"] if isinstance(item, dict) else item for item in config["sources"]]
            log(f"downloading {len(urls)} EPG sources")
            paths = collect_sources(urls, args.timeout, args.workers, Path(tmpdir))
        log(f"parsing {len(paths)} EPG sources")
        if not paths:
            raise RuntimeError("no usable EPG sources")
        programmes, counts = parse_sources(paths, wanted, args.max_per_channel)

    now = datetime.now(timezone.utc)
    with_data = sum(1 for cid, _n, _l in main_recs if programmes.get(cid))
    log(f"main channels with current EPG data: {with_data}/{len(main_recs)}")
    log(f"channels found in EPG source channel lists: {len(counts)}/{len(wanted)}")

    write_xmltv(out_dir / "curated_epg.xml", main_recs, programmes, False, now)
    write_xmltv(out_dir / "curated_no_epg_epg.xml", noepg_recs, {}, True, now)

    verify(main_m3u, out_dir / "curated_epg.xml.gz", require_real=True)
    verify(noepg_m3u, out_dir / "curated_no_epg_epg.xml.gz")
    log("EPG rebuild complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
