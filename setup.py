"""Builds Photoman.app with py2app. install.sh runs: python setup.py py2app -A (alias build: the app runs
this checkout's code with the venv's Python, so edits take effect after restarting the app)."""
from setuptools import setup

setup(
    name="Photoman",
    app=["packaging/Photoman.py"],
    options={"py2app": {
        "iconfile": "packaging/Photoman.icns",
        "plist": {
            "CFBundleName": "Photoman",
            "CFBundleDisplayName": "Photoman",
            "CFBundleIdentifier": "com.photoman.menubar",
            "CFBundleShortVersionString": "0.2",
            "LSUIElement": True,  # menu bar only, no Dock icon
            "NSHumanReadableCopyright": "",
        },
    }},
)
