#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────
# build_docs.sh — Build PDF documentation from Markdown
#
# Usage:
#   ./build_docs.sh              # build both A4 and A5
#   ./build_docs.sh a4           # build A4 only
#   ./build_docs.sh a5           # build A5 only
#
# Output:
#   documents/99_release/a4/*.pdf
#   documents/99_release/a5/*.pdf
#
# Requires: pandoc, weasyprint
#   brew install pandoc
#   pip install weasyprint
# ─────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
DOCS_DIR="$PROJECT_ROOT/sw_artifacts/docs"
RELEASE_DIR="$PROJECT_ROOT/documents/99_release"

# Which formats to build
FORMATS="${1:-a4 a5}"

# Markdown source files to convert
SOURCES="user_manual.md developer_manual.md hacs_publishing_guide.md"

# CSS template for PDF styling (per format)
make_css() {
    local size="$1"
    local font_size margin
    case "$size" in
        a4) font_size="11pt"; margin="25mm" ;;
        a5) font_size="9pt";  margin="15mm" ;;
    esac

    cat <<EOCSS
@page {
    size: $size;
    margin: $margin;
}
body {
    font-family: -apple-system, "Helvetica Neue", Helvetica, Arial, sans-serif;
    font-size: $font_size;
    line-height: 1.5;
    color: #333;
}
h1 { font-size: 1.8em; border-bottom: 2px solid #ddd; padding-bottom: 0.3em; }
h2 { font-size: 1.4em; border-bottom: 1px solid #eee; padding-bottom: 0.2em; }
h3 { font-size: 1.15em; }
code {
    font-family: Menlo, "Courier New", monospace;
    font-size: 0.9em;
    background: #f5f5f5;
    padding: 0.15em 0.3em;
    border-radius: 3px;
}
pre {
    background: #f5f5f5;
    padding: 1em;
    border-radius: 5px;
    overflow-x: auto;
    font-size: 0.85em;
}
pre code { background: none; padding: 0; }
table {
    border-collapse: collapse;
    width: 100%;
    margin: 1em 0;
}
th, td {
    border: 1px solid #ddd;
    padding: 0.5em 0.75em;
    text-align: left;
}
th { background: #f0f0f0; font-weight: 600; }
blockquote {
    border-left: 4px solid #ddd;
    margin: 1em 0;
    padding: 0.5em 1em;
    color: #555;
    background: #fafafa;
}
a { color: #0366d6; text-decoration: none; }
EOCSS
}

echo "=== NeverDry — Documentation Build ==="
echo "Source:  $DOCS_DIR"
echo "Output:  $RELEASE_DIR"
echo ""

for fmt in $FORMATS; do
    outdir="$RELEASE_DIR/$fmt"
    mkdir -p "$outdir"
    echo "── Building $fmt ──"

    # Write temporary CSS
    css_file="$(mktemp /tmp/dryness_doc_XXXXXX.css)"
    make_css "$fmt" > "$css_file"

    for src in $SOURCES; do
        src_path="$DOCS_DIR/$src"
        if [[ ! -f "$src_path" ]]; then
            echo "  SKIP  $src (not found)"
            continue
        fi

        pdf_name="${src%.md}.pdf"
        html_tmp="$(mktemp /tmp/dryness_doc_XXXXXX.html)"
        out_path="$outdir/$pdf_name"

        echo "  $src → $fmt/$pdf_name"

        # Markdown → HTML via pandoc
        pandoc "$src_path" \
            -o "$html_tmp" \
            --standalone \
            --toc \
            --toc-depth=2 \
            --metadata title="NeverDry" \
            --css="$css_file"

        # HTML → PDF via weasyprint
        weasyprint "$html_tmp" "$out_path" \
            --stylesheet "$css_file" \
            2>/dev/null

        rm -f "$html_tmp"
    done

    rm -f "$css_file"
    echo "  Done → $outdir/"
    echo ""
done

echo "=== Build complete ==="
