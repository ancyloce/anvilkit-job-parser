"""Build-time only: downloads the weights named by models.lock and verifies them."""

import hashlib
import sys
from pathlib import Path

from huggingface_hub import snapshot_download

lock = [line.split() for line in Path(sys.argv[1]).read_text().splitlines() if line and not line.startswith("#")]
repo, revision, _license = lock[0]
target = Path(sys.argv[2]) / repo.replace("/", "--")
files = {name: digest for digest, name in lock[1:]}
snapshot_download(repo, revision=revision, local_dir=target, allow_patterns=[*files, "README.md"])
for name, digest in files.items():
    actual = hashlib.sha256((target / name).read_bytes()).hexdigest()
    if actual != digest:
        sys.exit(f"{name}: sha256 {actual} is not the locked {digest}")
for extra in target.glob(".cache"):
    import shutil

    shutil.rmtree(extra)
print(f"verified {len(files)} files of {repo}@{revision}")
