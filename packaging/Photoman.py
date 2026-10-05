# Entry point for Photoman.app (the app is named after this file)
import locale
import sys
from pathlib import Path

# py2app's embedded Python starts in the "C" locale, so text defaults to ASCII and non-ASCII names
# (Chinese trip names, camera strings) would break. Switch to the user's UTF-8 locale.
for name in ("", "en_US.UTF-8", "C.UTF-8"):
    try:
        locale.setlocale(locale.LC_CTYPE, name)
    except locale.Error:
        continue
    if locale.getpreferredencoding(False).lower().replace("-", "") == "utf8":
        break

# The alias build links this file from the checkout; put the checkout on the path so `photoman` imports
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from photoman.menubar import main  # noqa: E402

main()
