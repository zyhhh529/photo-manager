# Entry point for Photoman.app (the app is named after this file)
import sys
from pathlib import Path

# The alias build links this file from the checkout; put the checkout on the path so `photoman` imports
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from photoman.menubar import main  # noqa: E402

main()
