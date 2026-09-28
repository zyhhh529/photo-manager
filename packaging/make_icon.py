"""Render the 📷 emoji into packaging/Photoman.icns (run by install.sh; needs PyObjC and iconutil)."""
import subprocess
import sys
import tempfile
from pathlib import Path

from AppKit import (NSBitmapImageRep, NSCalibratedRGBColorSpace, NSColor, NSFont, NSFontAttributeName,
                    NSGraphicsContext, NSPNGFileType, NSString, NSBezierPath, NSMakeRect)

OUT = Path(__file__).with_name("Photoman.icns")


def render(size: int, path: Path) -> None:
    rep = NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
        None, size, size, 8, 4, True, False, NSCalibratedRGBColorSpace, 0, 0)
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.setCurrentContext_(NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep))
    inset = size * 0.1  # macOS icon grid: rounded square with a margin
    NSColor.colorWithCalibratedRed_green_blue_alpha_(0.16, 0.18, 0.22, 1).set()
    NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
        NSMakeRect(inset, inset, size - 2 * inset, size - 2 * inset), size * 0.18, size * 0.18).fill()
    emoji = NSString.stringWithString_("📷")
    attrs = {NSFontAttributeName: NSFont.systemFontOfSize_(size * 0.5)}
    w, h = emoji.sizeWithAttributes_(attrs)
    emoji.drawAtPoint_withAttributes_(((size - w) / 2, (size - h) / 2), attrs)
    NSGraphicsContext.restoreGraphicsState()
    rep.representationUsingType_properties_(NSPNGFileType, {}).writeToFile_atomically_(str(path), True)


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        iconset = Path(tmp) / "Photoman.iconset"
        iconset.mkdir()
        for s in (16, 32, 128, 256, 512):
            render(s, iconset / f"icon_{s}x{s}.png")
            render(s * 2, iconset / f"icon_{s}x{s}@2x.png")
        return subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(OUT)]).returncode


if __name__ == "__main__":
    sys.exit(main())
