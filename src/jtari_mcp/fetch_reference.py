"""Download the Ensembl GRCh38 release 113 gene annotation (GTF) and verify it.

The build uses it for gene symbols and biotypes. It is public, so it is fetched
rather than shipped; the checksum pins the exact file every build must use.

    uv run jtari-fetch-reference            # -> data/reference/Homo_sapiens.GRCh38.113.gtf.gz
"""

import argparse
import hashlib
import shutil
import sys
import urllib.request
from pathlib import Path

GTF_URL = "https://ftp.ensembl.org/pub/release-113/gtf/homo_sapiens/Homo_sapiens.GRCh38.113.gtf.gz"
# sha256 of the file whose BSD `sum` (44004 62639) matches Ensembl's CHECKSUMS file.
GTF_SHA256 = "62f1709b40e083ce9d4cdc64a86b5ffec2c5d5371434bb7095c74dc89079c466"
DEFAULT_DEST = Path("data/reference/Homo_sapiens.GRCh38.113.gtf.gz")


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch(dest: Path = DEFAULT_DEST, source: Path | None = None) -> Path:
    """Put the GTF at `dest` (download, or copy from `source`) and verify its checksum."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and sha256_of(dest) == GTF_SHA256:
        print(f"already present and verified: {dest}", file=sys.stderr)
        return dest
    tmp = dest.with_suffix(".part")
    if source:
        shutil.copyfile(source, tmp)
    else:
        print(f"downloading {GTF_URL}", file=sys.stderr)
        urllib.request.urlretrieve(GTF_URL, tmp)
    got = sha256_of(tmp)
    if got != GTF_SHA256:
        tmp.unlink()
        raise SystemExit(f"checksum mismatch: expected {GTF_SHA256}, got {got}")
    tmp.rename(dest)
    print(f"verified: {dest}", file=sys.stderr)
    return dest


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--dest", type=Path, default=DEFAULT_DEST)
    p.add_argument(
        "--from", dest="source", type=Path, help="copy a local file instead of downloading"
    )
    args = p.parse_args()
    fetch(args.dest, args.source)


if __name__ == "__main__":
    main()
