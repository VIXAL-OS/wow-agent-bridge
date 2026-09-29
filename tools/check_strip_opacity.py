"""Offline decoder stress test; optionally composite over a gameplay screenshot.

This does not change game settings or measure a live transfer. Run with:
python -m tools.check_strip_opacity [--background screenshot.png]
"""
import argparse
import json
import random

from PIL import Image, ImageOps

from companion.protocol import decode_image, encode_character, encode_control, encode_prompt, render_frame
from companion.wow import GameWindow, strip_candidates


ALPHAS = (.2, .4, .6, .8, 1.0)
SIZES = ((512, 32), (720, 45), (960, 60))


def sample_frames():
    rng = random.Random(935)
    payload = ''.join(chr(rng.randrange(32, 127)) for _ in range(600))
    return (encode_prompt(payload)[:8] + encode_character(payload)[:8]
            + [encode_control(request=i + 1, remaining_ms=i * 170) for i in range(8)])


def backgrounds(screenshot=None):
    rng = random.Random(356)
    size = (512, 32)
    noise = Image.frombytes('L', size, bytes(rng.randrange(256) for _ in range(512 * 32))).convert('RGB')
    edges = Image.frombytes('L', size, bytes(255 * ((x // 4) % 2) for y in range(32) for x in range(512))).convert('RGB')
    result = {'flat': Image.new('RGB', size, (70, 90, 100)), 'noise': noise, 'cell edges': edges}
    if screenshot:
        with Image.open(screenshot) as source:
            scene = source.convert('RGB')
        window = GameWindow(0, 0, 0, *scene.size)
        for name, (x, y, w, h) in strip_candidates(window):
            result['game ' + name] = scene.crop((x, y, x + w, y + h))
    return result


def measure(screenshot=None):
    rows = []
    for alpha in ALPHAS:
        for name, background in list(backgrounds(screenshot).items()) + [('opposing cells', None)]:
            passed = rejected = wrong = 0
            for frame in sample_frames():
                base = ImageOps.invert(render_frame(frame)) if background is None else background
                rendered = render_frame(frame, alpha=alpha, background=base)
                for size in SIZES:
                    try:
                        decoded = decode_image(rendered.resize(size, Image.Resampling.BILINEAR))
                    except ValueError:
                        rejected += 1
                    else:
                        passed += decoded == frame
                        wrong += decoded != frame
            rows.append(dict(alpha=alpha, background=name, passed=passed, rejected=rejected, wrong=wrong))
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--background')
    args = parser.parse_args()
    rows = measure(args.background)
    print(json.dumps(rows, indent=2))
    return int(any(row['wrong'] or (row['alpha'] >= .6 and row['rejected']) for row in rows))


if __name__ == '__main__':
    raise SystemExit(main())
