"""Install checksum-pinned Homebrew bottles locally, without installing Homebrew."""
import hashlib
import json
import os
import platform
import tarfile
import tempfile
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "engines" / "katago"


def checksum(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def download(name, package, archive):
    """Resume bounded parallel ranges; verify the complete bottle before extraction."""
    token_url = f"https://ghcr.io/token?service=ghcr.io&scope=repository:homebrew/core/{name}:pull"
    with urllib.request.urlopen(token_url, timeout=60) as response:
        token = json.load(response)["token"]
    headers = {"Authorization": "Bearer " + token}
    token_lock = threading.Lock()
    with urllib.request.urlopen(urllib.request.Request(package["url"], headers=headers, method="HEAD"), timeout=60) as response:
        size = int(response.headers["Content-Length"])
    chunks = archive.parent / (name + ".parts")
    chunks.mkdir(exist_ok=True)
    chunk_size = 4 * 1024 * 1024

    def fetch(index):
        start, end = index * chunk_size, min((index + 1) * chunk_size, size) - 1
        part = chunks / str(index)
        for attempt in range(16):
            offset = part.stat().st_size if part.exists() else 0
            if offset == end - start + 1:
                return part
            if offset > end - start + 1:
                raise RuntimeError(f"Invalid cached download chunk: {part}")
            request = urllib.request.Request(package["url"], headers={**headers, "Range": f"bytes={start + offset}-{end}"})
            try:
                with urllib.request.urlopen(request, timeout=60) as response:
                    if response.status != 206 or response.headers.get("Content-Range") != f"bytes {start + offset}-{end}/{size}":
                        raise RuntimeError("Server did not honor the download range")
                    with part.open("ab") as output:
                        while data := response.read(256 * 1024):
                            output.write(data)
            except urllib.error.HTTPError as error:
                if error.code == 401:
                    with token_lock:
                        with urllib.request.urlopen(token_url, timeout=60) as response:
                            headers["Authorization"] = "Bearer " + json.load(response)["token"]
                elif attempt == 15:
                    raise
            except (OSError, TimeoutError):
                if attempt == 15:
                    raise
            if part.exists() and part.stat().st_size == end - start + 1:
                return part
            time.sleep(min(attempt + 1, 3))
        raise RuntimeError(f"Incomplete download for {name}; rerun installer to resume")

    count = (size + chunk_size - 1) // chunk_size
    print(f"{name}: {size / 1024 / 1024:.1f} MiB, resumable download", flush=True)
    with ThreadPoolExecutor(max_workers=32) as pool:
        futures = [pool.submit(fetch, index) for index in range(count)]
        for done, future in enumerate(as_completed(futures), 1):
            future.result()
            if done % 4 == 0 or done == count:
                print(f"{name}: {done}/{count} parts", flush=True)
    partial = archive.with_suffix(".part")
    with partial.open("wb") as output:
        for index in range(count):
            with (chunks / str(index)).open("rb") as source:
                while data := source.read(1024 * 1024):
                    output.write(data)
    if checksum(partial) != package["sha256"]:
        raise RuntimeError(f"Checksum mismatch for {name}; package was not installed")
    os.replace(partial, archive)


def install():
    if platform.system() != "Darwin" or platform.machine() != "arm64" or int(platform.mac_ver()[0].split(".")[0]) < 26:
        raise SystemExit("This package is for Apple Silicon macOS 26+. See README for other platforms.")
    packages = json.loads((ROOT / "packages.lock.json").read_text())
    downloads = ROOT / "downloads"
    downloads.mkdir(parents=True, exist_ok=True)
    target = ROOT / "packages"
    target.mkdir(exist_ok=True)
    for name, package in packages.items():
        archive = downloads / (name + ".tar.gz")
        if not archive.exists() or checksum(archive) != package["sha256"]:
            print(f"Downloading {name} {package['version']}…", flush=True)
            download(name, package, archive)
        if not (target / name).exists():
            print(f"Extracting {name}…", flush=True)
            with tempfile.TemporaryDirectory(dir=target) as temporary:
                with tarfile.open(archive) as bundle:
                    bundle.extractall(temporary, filter="data")
                os.replace(Path(temporary) / name, target / name)
    print("KataGo and its neural networks are installed in " + str(ROOT), flush=True)


if __name__ == "__main__":
    install()
