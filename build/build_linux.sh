#!/usr/bin/env bash
# ── ReDrive Rider — Linux AppImage build script ──────────────────────────
# Builds a standalone AppImage using python-appimage (includes tkinter).
#
# Requires FUSE to run the final AppImage. On systems without FUSE, use:
#   ./ReDriveRider-x86_64.AppImage --appimage-extract-and-run
#
# Run from project root:  bash build/build_linux.sh

set -euo pipefail

PYTHON_VERSION=3.11
APP_NAME=ReDriveRider
ARCH=x86_64

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$SCRIPT_DIR/.."
BUILD_DIR="$ROOT/build/_appimage_work"
CACHE_DIR="$ROOT/build/_appimage_cache"

echo "=== Building $APP_NAME AppImage (Linux $ARCH, Python $PYTHON_VERSION) ==="

cd "$ROOT"
mkdir -p "$BUILD_DIR" "$CACHE_DIR"

# ── Step 1: Download python-appimage base if not cached ──────────────────
PYTHON_APPIMAGE="python${PYTHON_VERSION}-cp${PYTHON_VERSION//./}-manylinux_2_28_${ARCH}.AppImage"
PYTHON_APPIMAGE_URL="https://github.com/niess/python-appimage/releases/download/python${PYTHON_VERSION}/${PYTHON_APPIMAGE}"

if [ ! -f "$CACHE_DIR/$PYTHON_APPIMAGE" ]; then
    echo "--- Downloading python-appimage base..."
    curl -fSL -o "$CACHE_DIR/$PYTHON_APPIMAGE" "$PYTHON_APPIMAGE_URL"
    chmod +x "$CACHE_DIR/$PYTHON_APPIMAGE"
else
    echo "--- Using cached python-appimage base"
fi

# ── Step 2: Extract the base AppImage into AppDir ────────────────────────
rm -rf "$BUILD_DIR/AppDir"
cd "$BUILD_DIR"
"$CACHE_DIR/$PYTHON_APPIMAGE" --appimage-extract
mv squashfs-root AppDir

# ── Step 3: Install dependencies into the bundled Python ─────────────────
echo "--- Installing Python dependencies..."
PYTHON_BIN="$BUILD_DIR/AppDir/usr/bin/python${PYTHON_VERSION}"
"$PYTHON_BIN" -m pip install --no-cache-dir --upgrade pip
"$PYTHON_BIN" -m pip install --no-cache-dir -r "$ROOT/requirements.txt"

# ── Step 4: Copy application source files ────────────────────────────────
echo "--- Copying application source..."
mkdir -p "$BUILD_DIR/AppDir/usr/src"
cp "$ROOT/rider_app.py"    "$BUILD_DIR/AppDir/usr/src/"
cp "$ROOT/rider_client.py" "$BUILD_DIR/AppDir/usr/src/"

# ── Step 5: Create AppRun entry point ────────────────────────────────────
cat > "$BUILD_DIR/AppDir/AppRun" << 'APPRUN'
#!/bin/bash
HERE="$(dirname "$(readlink -f "$0")")"
export PATH="$HERE/usr/bin:$PATH"
export LD_LIBRARY_PATH="$HERE/usr/lib:${LD_LIBRARY_PATH:-}"
export PYTHONPATH="$HERE/usr/src:${PYTHONPATH:-}"
export TCL_LIBRARY="$HERE/usr/share/tcltk/tcl8.6"
export TK_LIBRARY="$HERE/usr/share/tcltk/tk8.6"
exec "$HERE/usr/bin/python3" "$HERE/usr/src/rider_app.py" "$@"
APPRUN
chmod +x "$BUILD_DIR/AppDir/AppRun"

# ── Step 6: Create .desktop file ─────────────────────────────────────────
cat > "$BUILD_DIR/AppDir/$APP_NAME.desktop" << DESKTOP
[Desktop Entry]
Type=Application
Name=$APP_NAME
Exec=rider_app.py
Icon=$APP_NAME
Categories=Utility;
Comment=ReDrive Rider - T-code relay client for ReStim
DESKTOP

# Provide a fallback icon (AppImage requires one)
if [ ! -f "$BUILD_DIR/AppDir/$APP_NAME.png" ]; then
    # Generate a minimal 1x1 PNG if no icon is available
    printf '\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82' \
        > "$BUILD_DIR/AppDir/$APP_NAME.png"
fi

# Symlink .desktop and icon to AppDir root (appimagetool expects them there)
ln -sf "$APP_NAME.desktop" "$BUILD_DIR/AppDir/.desktop" 2>/dev/null || true
ln -sf "$APP_NAME.png" "$BUILD_DIR/AppDir/.DirIcon" 2>/dev/null || true

# ── Step 7: Download appimagetool if not cached ──────────────────────────
APPIMAGETOOL="appimagetool-${ARCH}.AppImage"
APPIMAGETOOL_URL="https://github.com/AppImage/appimagetool/releases/download/continuous/$APPIMAGETOOL"

if [ ! -f "$CACHE_DIR/$APPIMAGETOOL" ]; then
    echo "--- Downloading appimagetool..."
    curl -fSL -o "$CACHE_DIR/$APPIMAGETOOL" "$APPIMAGETOOL_URL"
    chmod +x "$CACHE_DIR/$APPIMAGETOOL"
else
    echo "--- Using cached appimagetool"
fi

# ── Step 8: Build the final AppImage ─────────────────────────────────────
echo "--- Packaging AppImage..."
OUTPUT="$ROOT/${APP_NAME}-${ARCH}.AppImage"
ARCH="$ARCH" "$CACHE_DIR/$APPIMAGETOOL" "$BUILD_DIR/AppDir" "$OUTPUT"

echo "=== Done: $OUTPUT ==="
echo "    Size: $(du -h "$OUTPUT" | cut -f1)"
