"""Write a simple Local Coder icns from a generated bitmap. No extra deps."""
from __future__ import annotations

import math
import struct
import subprocess
import tempfile
from pathlib import Path


def pixel(x: int, y: int, size: int) -> tuple[int, int, int]:
    cx = cy = size / 2
    dx = (x - cx) / size
    dy = (y - cy) / size
    r = math.hypot(dx, dy)
    if r < 0.38:
        return (246, 247, 242)
    if r < 0.46:
        return (46, 92, 74)
    return (31, 61, 51)


def write_ppm(path: Path, size: int = 1024) -> None:
    path.write_bytes(
        f"P6\n{size} {size}\n255\n".encode("ascii")
        + b"".join(struct.pack("BBB", *pixel(x, y, size)) for y in range(size) for x in range(size))
    )


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    dest = root / "AppIcon.icns"
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        ppm = tmp_path / "icon.ppm"
        png = tmp_path / "icon.png"
        iconset = tmp_path / "AppIcon.iconset"
        iconset.mkdir()
        write_ppm(ppm)
        subprocess.run(["sips", "-s", "format", "png", str(ppm), "--out", str(png)], check=True, capture_output=True)
        sizes = [16, 32, 64, 128, 256, 512, 1024]
        for size in sizes:
            subprocess.run(
                ["sips", "-z", str(size), str(size), str(png), "--out", str(iconset / f"icon_{size}x{size}.png")],
                check=True, capture_output=True)
            if size <= 512:
                subprocess.run(
                    ["sips", "-z", str(size * 2), str(size * 2), str(png),
                     "--out", str(iconset / f"icon_{size}x{size}@2x.png")],
                    check=True, capture_output=True)
        subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(dest)], check=True)
    print(dest)


if __name__ == "__main__":
    main()
