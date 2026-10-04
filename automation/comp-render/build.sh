#!/bin/zsh
# Builds comp-render, a command-line renderer for .comp projects, from the app's own sources, so its output matches
# File › Export exactly. Needs only the Swift toolchain (Xcode or the Command Line Tools).
#   automation/comp-render/build.sh            → automation/bin/comp-render
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
SRC="$REPO/Compositor"
OUT="$REPO/automation/bin"
WORK="$REPO/automation/.build/comp-render"
rm -rf "$WORK"; mkdir -p "$WORK/swift" "$WORK/c" "$OUT"

# Every app source except the SwiftUI @main entry point; the app delegate is kept (other views name it) with
# Sparkle swapped for a stub.
find "$SRC" -name '*.swift' ! -name CompositorApp.swift | while read -r file; do
  name="${file#$SRC/}"; name="${name//\//_}"
  sed -e '/^import Sparkle$/d' "$file" > "$WORK/swift/$name"
done
cp "$HERE/main.swift" "$HERE/SparkleStub.swift" "$WORK/swift/"

echo "==> Compiling the C pixel kernels"
for file in "$SRC"/Rendering/*.c; do
  clang -O3 -c "$file" -I "$SRC" -o "$WORK/c/$(basename "${file%.c}").o"
done

echo "==> Compiling Swift (whole module, optimized; this takes a few minutes)"
swiftc -O -wmo -swift-version 5 -target arm64-apple-macosx26.0 \
  -default-isolation MainActor \
  -enable-upcoming-feature MemberImportVisibility \
  -enable-upcoming-feature InferIsolatedConformances \
  -enable-upcoming-feature NonisolatedNonsendingByDefault \
  -enable-upcoming-feature InferSendableFromCaptures \
  -enable-upcoming-feature DisableOutwardActorInference \
  -enable-upcoming-feature GlobalActorIsolatedTypesUsability \
  -import-objc-header "$SRC/Compositor-Bridging-Header.h" -Xcc -I"$SRC" \
  -module-name comp_render \
  "$WORK"/swift/*.swift "$WORK"/c/*.o \
  -o "$OUT/comp-render"
echo "==> Built $OUT/comp-render"
