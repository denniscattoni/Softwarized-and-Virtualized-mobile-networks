#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
About: HTTP "single resource" downloader using Range requests, discarding data (dev/null style).

Design goals:
- User-level semantics: download a single URL (e.g., /video.bin).
- Implementation: chunked Range GETs with retries (migration-friendly).
- Discard payload: do not write to disk, measure throughput.
"""

import argparse
import time
import urllib.error
import urllib.request


def ts():
    """
    Return current wall-clock timestamp (HH:MM:SS.mmm).

    Useful for correlating client progress with migration events.
    """
    return time.strftime("%H:%M:%S") + f".{int(time.time() * 1000) % 1000:03d}"


def http_head(url: str, timeout_s: float):
    """
    Perform a HEAD request to discover resource size and range support.

    :return (size_bytes, accept_ranges)
    """
    req = urllib.request.Request(url, method="HEAD")
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        headers = resp.headers
        length = headers.get("Content-Length")
        accept_ranges = headers.get("Accept-Ranges", "")
        return int(length) if length is not None else None, accept_ranges.lower()


def http_get_range_discard(url: str, start: int, end: int, timeout_s: float) -> int:
    """
    GET a byte range and discard the body.

    :return (int): number of bytes read
    """
    req = urllib.request.Request(url, method="GET")
    req.add_header("Range", f"bytes={start}-{end}")

    total = 0
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        # Read in small blocks to avoid buffering the whole chunk.
        while True:
            block = resp.read(64 * 1024)  # 64 KiB
            if not block:
                break
            total += len(block)
    return total


def run(url: str, chunk_size: int, timeout_s: float, max_retries: int, backoff_s: float):
    """
    Run the chunked download loop.

    Notes:
    - Download is sequential (one chunk at a time).
    - A log line is printed at the end of each successfully completed chunk.
    - Retries are performed on the same chunk range if an error occurs.
    """
    size, accept_ranges = http_head(url, timeout_s=timeout_s)
    if size is None:
        raise RuntimeError("Server did not provide Content-Length; cannot chunk safely.")

    if "bytes" not in accept_ranges:
        raise RuntimeError("Server does not advertise Accept-Ranges: bytes.")

    print(f"[{ts()}] [client] URL: {url}", flush=True)
    print(f"[{ts()}] [client] Content-Length: {size} bytes", flush=True)
    print(f"[{ts()}] [client] Chunk size: {chunk_size} bytes", flush=True)
    print(
        f"[{ts()}] [client] Timeout: {timeout_s}s  Retries/chunk: {max_retries}\n",
        flush=True,
    )

    downloaded = 0
    chunk_no = 0
    t0 = time.time()

    while downloaded < size:
        start = downloaded
        end = min(downloaded + chunk_size - 1, size - 1)
        expected = end - start + 1

        attempt = 0
        while True:
            try:
                got = http_get_range_discard(url, start, end, timeout_s=timeout_s)

                # If we got a short read, treat as transient failure.
                if got != expected:
                    raise RuntimeError(
                        f"Short read: got={got} expected={expected} range={start}-{end}"
                    )

                downloaded += got
                chunk_no += 1

                elapsed = time.time() - t0
                mib = downloaded / (1024 * 1024)
                mbps = (downloaded * 8) / (elapsed * 1_000_000) if elapsed > 0 else 0.0

                # Log at the end of every successfully completed chunk.
                print(
                    f"[{ts()}] [client] Chunk {chunk_no} OK "
                    f"range={start}-{end} bytes={got} "
                    f"total={downloaded}/{size} ({mib:.2f} MiB) avg={mbps:.2f} Mbps",
                    flush=True,
                )
                break

            except (urllib.error.URLError, urllib.error.HTTPError, RuntimeError) as e:
                attempt += 1
                if attempt > max_retries:
                    raise RuntimeError(
                        f"Failed range {start}-{end} after {max_retries} retries: {e}"
                    ) from e
                time.sleep(backoff_s)

    total_t = time.time() - t0
    mbps = (downloaded * 8) / (total_t * 1_000_000) if total_t > 0 else 0.0
    print(
        f"\n[{ts()}] [client] Done. Chunks: {chunk_no}  Total time: {total_t:.2f}s  Avg: {mbps:.2f} Mbps",
        flush=True,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Chunked HTTP Range downloader (discard payload)."
    )
    parser.add_argument(
        "--url",
        required=True,
        help="Resource URL (e.g., http://10.0.0.100:8080/video.bin)",
    )
    parser.add_argument(
        "--chunk-bytes",
        type=int,
        default=1024 * 1024,
        help="Chunk size in bytes",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=2.0,
        help="Per-request timeout (seconds)",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=100,
        help="Max retries per chunk",
    )
    parser.add_argument(
        "--backoff",
        type=float,
        default=0.3,
        help="Backoff between retries (seconds)",
    )

    args = parser.parse_args()
    run(args.url, args.chunk_bytes, args.timeout, args.retries, args.backoff)
