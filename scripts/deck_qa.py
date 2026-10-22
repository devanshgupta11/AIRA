"""Geometry QA for a generated .pptx, for environments with no renderer available.

This cannot see the slides, so it checks what is computable from the file: shapes off the canvas,
thin margins, overlapping text, and text too long for the box it sits in. The overflow estimate is
approximate - it assumes an average glyph width - so it reports a ratio and flags only clear cases.

Usage: python deck_qa.py deck.pptx
"""

import sys
from pptx import Presentation
from pptx.util import Emu

SLIDE_W, SLIDE_H = 13.333, 7.5
MARGIN = 0.4           # inches; the guidance is 0.5, 0.4 avoids flagging deliberate full-bleed bars
GLYPH_W = 0.50         # average glyph width as a fraction of font size
LINE_H = 1.22          # line height as a multiple of font size


def inches(v):
    return Emu(v).inches if v is not None else None


def boxes(slide):
    out = []
    for sh in slide.shapes:
        try:
            x, y, w, h = inches(sh.left), inches(sh.top), inches(sh.width), inches(sh.height)
        except Exception:
            continue
        if None in (x, y, w, h):
            continue
        text = sh.text_frame.text if sh.has_text_frame else ""
        size = None
        if sh.has_text_frame:
            for p in sh.text_frame.paragraphs:
                for r in p.runs:
                    if r.font.size:
                        size = max(size or 0, r.font.size.pt)
        out.append({"name": sh.shape_type, "x": x, "y": y, "w": w, "h": h,
                    "text": text, "size": size, "obj": sh})
    return out


def overflow_ratio(b):
    """Estimated fraction of the box the text needs. >1 means it probably does not fit."""
    if not b["text"].strip() or not b["size"]:
        return 0.0
    pt = b["size"]
    char_w = pt * GLYPH_W / 72.0
    line_h = pt * LINE_H / 72.0
    per_line = max(1, int(b["w"] / char_w))
    lines = 0
    for para in b["text"].split("\n"):
        lines += max(1, -(-len(para) // per_line))
    return (lines * line_h) / b["h"] if b["h"] else 0.0


def main(path):
    prs = Presentation(path)
    issues = 0
    for i, slide in enumerate(prs.slides, 1):
        bs = boxes(slide)
        found = []

        for b in bs:
            if b["x"] < -0.01 or b["y"] < -0.01 or b["x"] + b["w"] > SLIDE_W + 0.01 or b["y"] + b["h"] > SLIDE_H + 0.01:
                found.append(f"OFF-CANVAS  x={b['x']:.2f} y={b['y']:.2f} w={b['w']:.2f} h={b['h']:.2f}"
                             f"  '{b['text'][:40]}'")
            elif b["text"].strip() and (b["x"] < MARGIN or b["y"] < MARGIN
                                        or SLIDE_W - (b["x"] + b["w"]) < MARGIN - 0.12
                                        or SLIDE_H - (b["y"] + b["h"]) < MARGIN - 0.12):
                found.append(f"TIGHT MARGIN x={b['x']:.2f} y={b['y']:.2f}  '{b['text'][:40]}'")

            r = overflow_ratio(b)
            if r > 1.15:
                found.append(f"TEXT MAY OVERFLOW ({r:.0%} of box, {b['size']:.0f}pt)  '{b['text'][:55]}'")

        # overlapping text-bearing boxes (ignore a text box sitting on its own backing shape)
        texts = [b for b in bs if b["text"].strip()]
        for a_i in range(len(texts)):
            for b_i in range(a_i + 1, len(texts)):
                a, b = texts[a_i], texts[b_i]
                ox = min(a["x"] + a["w"], b["x"] + b["w"]) - max(a["x"], b["x"])
                oy = min(a["y"] + a["h"], b["y"] + b["h"]) - max(a["y"], b["y"])
                if ox > 0.12 and oy > 0.12:
                    found.append(f"TEXT OVERLAP  '{a['text'][:28]}' x '{b['text'][:28]}'"
                                 f"  ({ox:.2f}in x {oy:.2f}in)")

        if found:
            issues += len(found)
            print(f"\nSlide {i}")
            for f in found:
                print("   " + f)

    print(f"\n{issues} potential issue(s) across {len(prs.slides.__iter__().__self__._sldIdLst)} slides"
          if False else f"\n{issues} potential issue(s)")
    return 1 if issues else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "AIRA-MidSem-Presentation.pptx"))
