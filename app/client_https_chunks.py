#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
About: HTTP "single resource" downloader using Range requests, discarding data (dev/null style).

Design goals:
- User-level semantics: download a single URL (e.g., /video.bin).
- Implementation: chunked Range GETs with retries (migration-friendly).
- Discard payload: do not write to disk, measure throughput.

Transport goals (Python 3.8 stdlib):
- HTTPS with certificate verification (no -k / no insecure bypass).
- TLS 1.3 required (fail if negotiated TLS is not 1.3).
- NOTE: HTTP/2 cannot be guaranteed with urllib in Python 3.8 (stdlib is HTTP/1.1).
"""

import argparse
import socket
import ssl
import time
import urllib.error
import urllib.request


def ts():
    """
    Return current wall-clock timestamp (HH:MM:SS.mmm).

    Useful for correlating client progress with migration events.
    """
    return time.strftime("%H:%M:%S") + f".{int(time.time() * 1000) % 1000:03d}"


def build_ssl_context_tls13(cafile: str) -> ssl.SSLContext:
    """
    Build an SSL context that:
    - verifies server certificates (no insecure mode),
    - trusts the provided cafile (self-signed cert or CA bundle),
    - requires TLS 1.3.

    NOTE:
    - If you use a DIRECT self-signed server certificate, cafile can be server.crt.
    - If you use a local CA that signs the server cert, cafile must be ca.crt.
    """
    if not cafile:
        raise RuntimeError("Missing cafile for TLS verification (self-signed requires explicit trust anchor).")

    #ctx = ssl.create_default_context(cafile=cafile)
    ctx = ssl.create_default_context(cafile="app/certs/server.crt")

    # Require TLS 1.3
    try:
        ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    except Exception as e:
        raise RuntimeError(f"TLS 1.3 enforcement not supported by this runtime: {e}") from e

    # Enforce verification (default for create_default_context, but keep explicit)
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED

    return ctx


def http_head(url: str, timeout_s: float, ssl_ctx: ssl.SSLContext):
    """
    Perform a HEAD request to discover resource size and range support.

    :return (size_bytes, accept_ranges)
    """
    req = urllib.request.Request(url, method="HEAD")
    with urllib.request.urlopen(req, timeout=timeout_s, context=ssl_ctx) as resp:
        headers = resp.headers
        length = headers.get("Content-Length")
        accept_ranges = headers.get("Accept-Ranges", "")
        return int(length) if length is not None else None, accept_ranges.lower()


def http_get_range_discard(url: str, start: int, end: int, timeout_s: float, ssl_ctx: ssl.SSLContext) -> int:
    """
    GET a byte range and discard the body.

    :return (int): number of bytes read
    """
    req = urllib.request.Request(url, method="GET")
    req.add_header("Range", "bytes={}-{}".format(start, end))

    total = 0
    with urllib.request.urlopen(req, timeout=timeout_s, context=ssl_ctx) as resp:
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
    if not url.startswith("https://"):
        raise RuntimeError("This client requires HTTPS. Use an https:// URL.")

    # Trust anchor for the demo.
    # Keep the same CLI args and workflow: no extra parameters.
    #
    # Choose ONE of the following depending on your cert generation:
    # - Direct self-signed server cert: "app/certs/server.crt"
    # - Local CA signing server cert:  "app/certs/ca.crt"
    cafile = "app/certs/server.crt"

    ssl_ctx = build_ssl_context_tls13(cafile=cafile)

    size, accept_ranges = http_head(url, timeout_s=timeout_s, ssl_ctx=ssl_ctx)
    if size is None:
        raise RuntimeError("Server did not provide Content-Length; cannot chunk safely.")

    if "bytes" not in accept_ranges:
        raise RuntimeError("Server does not advertise Accept-Ranges: bytes.")

    print("[{}] [client] URL: {}".format(ts(), url), flush=True)
    print("[{}] [client] Content-Length: {} bytes".format(ts(), size), flush=True)
    print("[{}] [client] Chunk size: {} bytes".format(ts(), chunk_size), flush=True)
    print(
        "[{}] [client] Timeout: {}s  Retries/chunk: {}\n".format(ts(), timeout_s, max_retries),
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
                got = http_get_range_discard(url, start, end, timeout_s=timeout_s, ssl_ctx=ssl_ctx)

                # If we got a short read, treat as transient failure.
                if got != expected:
                    raise RuntimeError(
                        "Short read: got={} expected={} range={}-{}".format(got, expected, start, end)
                    )

                downloaded += got
                chunk_no += 1

                elapsed = time.time() - t0
                mib = downloaded / (1024 * 1024)
                mbps = (downloaded * 8) / (elapsed * 1_000_000) if elapsed > 0 else 0.0

                # Log at the end of every successfully completed chunk.
                print(
                    "[{}] [client] Chunk {} OK range={}-{} bytes={} total={}/{} ({:.2f} MiB) avg={:.2f} Mbps".format(
                        ts(), chunk_no, start, end, got, downloaded, size, mib, mbps
                    ),
                    flush=True,
                )
                break

            except (urllib.error.URLError, urllib.error.HTTPError, RuntimeError, socket.timeout, ssl.SSLError) as e:
                attempt += 1
                if attempt > max_retries:
                    raise RuntimeError(
                        "Failed range {}-{} after {} retries: {}".format(start, end, max_retries, e)
                    ) from e
                time.sleep(backoff_s)

    total_t = time.time() - t0
    mbps = (downloaded * 8) / (total_t * 1_000_000) if total_t > 0 else 0.0
    print(
        "\n[{}] [client] Done. Chunks: {}  Total time: {:.2f}s  Avg: {:.2f} Mbps".format(
            ts(), chunk_no, total_t, mbps
        ),
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
