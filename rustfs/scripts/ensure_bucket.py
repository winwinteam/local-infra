#!/usr/bin/env python3
"""Ensure an S3 bucket exists on a local RustFS/S3 endpoint using Python stdlib SigV4."""

from __future__ import annotations

import argparse
import datetime
import hashlib
import hmac
import sys
import urllib.error
import urllib.parse
import urllib.request


def _sign(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _sigv4_headers(
    method: str,
    url: str,
    access_key: str,
    secret_key: str,
    region: str = "us-east-1",
    payload: bytes = b"",
) -> dict[str, str]:
    parsed = urllib.parse.urlparse(url)
    host = parsed.netloc
    canonical_uri = parsed.path or "/"
    canonical_querystring = parsed.query

    now = datetime.datetime.now(datetime.UTC)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")
    payload_hash = hashlib.sha256(payload).hexdigest()

    canonical_headers = f"host:{host}\nx-amz-content-sha256:{payload_hash}\nx-amz-date:{amz_date}\n"
    signed_headers = "host;x-amz-content-sha256;x-amz-date"
    canonical_request = (
        f"{method}\n{canonical_uri}\n{canonical_querystring}\n"
        f"{canonical_headers}\n{signed_headers}\n{payload_hash}"
    )

    algorithm = "AWS4-HMAC-SHA256"
    credential_scope = f"{date_stamp}/{region}/s3/aws4_request"
    string_to_sign = (
        f"{algorithm}\n{amz_date}\n{credential_scope}\n"
        f"{hashlib.sha256(canonical_request.encode('utf-8')).hexdigest()}"
    )

    k_date = _sign(("AWS4" + secret_key).encode("utf-8"), date_stamp)
    k_region = _sign(k_date, region)
    k_service = _sign(k_region, "s3")
    k_signing = _sign(k_service, "aws4_request")
    signature = hmac.new(k_signing, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

    authorization = (
        f"{algorithm} Credential={access_key}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    return {
        "Host": host,
        "x-amz-date": amz_date,
        "x-amz-content-sha256": payload_hash,
        "Authorization": authorization,
    }


def ensure_bucket(endpoint: str, bucket: str, access_key: str, secret_key: str) -> None:
    url = f"{endpoint.rstrip('/')}/{bucket}"
    headers = _sigv4_headers("PUT", url, access_key, secret_key)
    req = urllib.request.Request(url, data=b"", headers=headers, method="PUT")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            print(f"Bucket '{bucket}' created (HTTP {resp.status}).")
    except urllib.error.HTTPError as exc:
        if exc.code in (200, 409):
            print(f"Bucket '{bucket}' already exists (HTTP {exc.code}).")
            return
        body = exc.read().decode("utf-8", errors="replace")
        print(f"ERROR creating bucket '{bucket}': HTTP {exc.code}: {body}", file=sys.stderr)
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description="Create an S3 bucket on RustFS if missing.")
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--access-key", required=True)
    parser.add_argument("--secret-key", required=True)
    args = parser.parse_args()
    ensure_bucket(args.endpoint, args.bucket, args.access_key, args.secret_key)


if __name__ == "__main__":
    main()
