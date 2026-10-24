"""Build the SVG pack and embed its trusted paths in the existing Mini App bundle."""
import json
from pathlib import Path
from html import escape

ROOT = Path(__file__).resolve().parent
ICONS = {
    'badminton': '<g transform="rotate(30 12 12)"><path d="M9 15 4 4Q12 0 20 4l-5 11M8 3l3 12m1-13v13m4-12-3 12M8 12h8M9 15h6v3a3 3 0 0 1-6 0v-3Z"/></g>',
    'strength': '<path d="M7 12h10M5 8v8m2-10v12M19 8v8m-2-10v12M3 10v4m18-4v4"/>',
    'basketball': '<circle cx="12" cy="12" r="9"/><path d="m5.6 5.6 12.8 12.8M5.6 18.4 18.4 5.6M3 12c5-4 9-4 9-9m0 18c0-5 4-9 9-9"/>',
    'boxing': '<path d="M7 16c-3-2-3-5-2-7l2-4c1-2 4-2 5 0 3-2 6 0 6 3v3l2 1v4l-3 3H8l-1-3Zm1 3v3h9v-3M7 8l2 2m9 1-4 2"/>',
    'cricket': '<path d="m4 19 4-4m-2-2 8-10 4 4-9 9-3-3ZM16 17v5m3-5v5m3-5v5m-7-6h8"/><circle cx="5" cy="5" r="2"/>',
    'crossfit': '<path d="M4 10h16M3 7v6m3-7v8m12-8v8m3-7v6M9 18l3-3 3 3m-3-3v7"/>',
    'running': '<circle cx="16" cy="4" r="2"/><path d="m9 8 4-2 3 5 4 1m-8-3-2 5 5 3-1 5M10 14l-4 5H2M3 8h3m-4 4h3"/>',
    'football': '<circle cx="12" cy="12" r="9"/><path d="m12 7 5 4-2 5H9l-2-5 5-4Zm0-4v4m5 4 4-1m-6 6 2 4m-8-4-2 4m0-9-4-1"/>',
    'frisbee': '<ellipse cx="13" cy="11" rx="9" ry="4" transform="rotate(-22 13 11)"/><path d="M9 11c2-2 5-3 8-2M2 17h5m-3 3h6"/>',
    'functional': '<circle cx="12" cy="4" r="2"/><path d="M4 8h16m-8-2v8m0-3-5 4-3 6m8-10 5 4 3 6"/>',
    'power': '<path d="M6 11v7m12-7v7M4 13v3m16-3v3M6 14h12m-7-4 3-6h-4l-1 6h2l-1 4 5-7h-4"/>',
    'back': '<path d="M7 4 3 9l3 4m11-9 4 5-3 4M8 21l1-7V7m7 14-1-7V7M12 5v14"/><path d="M11 8h2m-2 3h2m-2 3h2m-2 3h2"/>',
    'hiking': '<path d="m2 21 7-12 4 6 3-4 6 10H2Zm4-7 3 2 2-3m5-2V3l5 2-5 2"/>',
    'deadlift': '<path d="M3 18h18M4 15v6m3-7v8m10-8v8m3-7v6M9 15l1-6 5 2 1 4m-6-6 3-3"/><circle cx="15" cy="4" r="2"/>',
    'karate': '<circle cx="12" cy="4" r="2"/><path d="m4 9 6-2 5 2 6-5m-10 4 2 5 7 2m-7-2-4 8m1-9h5M8 9l-2 4"/>',
    'mobility': '<circle cx="12" cy="6" r="2"/><path d="M5 12c0-4 3-8 7-8m7 8c0 4-3 8-7 8M2 10l3 3 3-3m8 4 3-3 3 3M8 16l4-6 4 6"/>',
    'fencing': '<path d="m4 3 14 14m-4 1 4-4m-1 5 4 3M20 3 6 17m0-3 4 4m-3 1-4 3"/>',
    'games': '<path d="M7 8h10c3 0 4 3 5 9 0 3-3 4-5 1l-1-1H8l-1 1c-2 3-5 2-5-1 1-6 2-9 5-9ZM6 11v5m-2-3h4M10 8l1-3h3"/><circle cx="17" cy="12" r=".7"/><circle cx="19" cy="15" r=".7"/>',
    'recharge': '<rect x="3" y="4" width="18" height="13" rx="2"/><path d="M8 21h8m-4-4v4m0-14-3 5h4l-1 3 4-6h-4l1-2"/>',
    'judo': '<path d="m8 3 4 3 4-3 6 7-4 3-2-3v11H8V10l-2 3-4-3 6-7Zm0 0 6 12m2-12-6 12M8 15h8m-4 0-2 6m2-6 3 6"/>',
    'dance': '<circle cx="13" cy="4" r="2"/><path d="m6 4 3 5 6 1 4-5m-8 5-2 5 7 1m-5-6 4 7 4 4M9 15l-4 6"/>',
    'street-dance': '<circle cx="15" cy="5" r="2"/><path d="m6 8 5 1 5 4 5-2M11 9l-2 5 5 3-1 5M9 14l-4 3H2M4 5l2 3"/>',
    'stretching': '<circle cx="13" cy="5" r="2"/><path d="m5 10 7-2 5 3 4-2m-9-1-1 7-8 5m8-5 10 5M2 22h20"/>',
    'strongfit': '<path d="M6 8h12l3 13H3L6 8Zm3 0V6a3 3 0 0 1 6 0v2"/><path d="m10 17 2-5 2 5m-4-2h4"/>',
    'swimming': '<circle cx="16" cy="7" r="2"/><path d="m4 12 5-5 5 4 4 1M2 16c2-2 3 2 5 0s3 2 5 0 3 2 5 0 3 2 5 0M2 20c2-2 3 2 5 0s3 2 5 0 3 2 5 0 3 2 5 0"/>',
    'table-tennis': '<path d="M7 16C-1 11 7 0 14 4c7 4 2 13-4 12l-4 6-3-2 4-4Zm-3-8 12 7"/><circle cx="20" cy="5" r="2"/>',
    'tennis': '<ellipse cx="14" cy="9" rx="6" ry="8" transform="rotate(38 14 9)"/><path d="m9 15-6 7m1-5 3 3m3-14 8 6M8 9l8 6m-5-1 6-8m-9 5 5-7"/>',
    'muay-thai': '<circle cx="14" cy="4" r="2"/><path d="m7 7 3 4 5-3 4 2m-7 0-2 5 7-1 3 4m-10-3-4 7M5 5l2 2m12 3 2-2"/>',
    'volleyball': '<circle cx="12" cy="12" r="9"/><path d="M12 3c-2 3-3 6-2 9l-5 6m5-6 8 1m0 0c1-3 0-6-2-9m-6 8 2 9M3 10l7 2m8 1 2 5"/>',
    'yoga': '<circle cx="12" cy="5" r="2"/><path d="M12 8v6m-6-4 3 3h6l3-3M9 14l-6 5c-2 3 5 3 9 0 4 3 11 3 9 0l-6-5M8 21h8"/>',
    'sport': '<path d="m12 3 9 5v8l-9 5-9-5V8l9-5Zm-4 9 3 3 5-6"/>',
}

# Match specific names before broader sport names; never interpret a title as SVG.
RULES = [
    ('table tennis', 'table-tennis'), ('thai boxing', 'muay-thai'),
    ('badminton', 'badminton'), ('basic strength', 'strength'),
    ('basketball', 'basketball'), ('boxing', 'boxing'), ('cricket', 'cricket'),
    ('crossfit', 'crossfit'), ('running', 'running'), ('football', 'football'),
    ('frisbee', 'frisbee'), ('functional', 'functional'), ('girls power', 'power'),
    ('healthy back', 'back'), ('hike', 'hiking'), ('innotyaga', 'deadlift'),
    ('karate', 'karate'), ('mobility', 'mobility'), ('fencing', 'fencing'),
    ('rage - games', 'games'), ('rage - .etc', 'games'), ('recharge', 'recharge'),
    ('sambo', 'judo'), ('judo', 'judo'), ('social dance', 'dance'),
    ('street dance', 'street-dance'), ('stretching', 'stretching'),
    ('strongfit', 'strongfit'), ('swimming', 'swimming'), ('tennis', 'tennis'),
    ('volleyball', 'volleyball'), ('yoga', 'yoga'),
]
ATTRS = 'viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"'


def build():
    assets = ROOT / 'svg'
    assets.mkdir(exist_ok=True)
    for key, paths in ICONS.items():
        (assets / f'{key}.svg').write_text(f'<svg xmlns="http://www.w3.org/2000/svg" {ATTRS}>{paths}</svg>\n', encoding='utf-8')
    titles = json.loads((ROOT / 'catalogue.json').read_text(encoding='utf-8'))['titles']
    mapping = {title: next((key for word, key in RULES if word in title.lower()), 'sport') for title in titles}
    assert 'sport' not in mapping.values(), 'A known sport is missing its icon'
    (ROOT / 'manifest.json').write_text(json.dumps(mapping, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    script = ('// BEGIN SPORT ICONS (generated by icon-pack/build.py)\n'
              f'const sportPaths = {json.dumps(ICONS)};\n'
              f'const sportRules = {json.dumps(RULES)};\n'
              'function sportIcon(title){\n'
              "  const key=sportRules.find(([word])=>String(title).toLowerCase().includes(word))?.[1]||'sport';\n"
              "  const box=node('div','sport-icon');box.setAttribute('aria-hidden','true');\n"
              "  const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');\n"
              "  svg.setAttribute('viewBox','0 0 24 24');svg.setAttribute('fill','none');\n"
              "  svg.setAttribute('stroke','currentColor');svg.setAttribute('stroke-width','1.5');\n"
              "  svg.setAttribute('stroke-linecap','round');svg.setAttribute('stroke-linejoin','round');\n"
              '  svg.innerHTML=sportPaths[key];box.append(svg);return box;\n}\n'
              '// END SPORT ICONS')
    app = ROOT.parent / 'web' / 'app.js'
    text = app.read_text(encoding='utf-8')
    if '// BEGIN SPORT ICONS' in text:
        start = text.index('// BEGIN SPORT ICONS')
        end = text.index('// END SPORT ICONS', start) + len('// END SPORT ICONS')
        text = text[:start] + script + text[end:]
    else:
        start = text.index('function icon(title)')
        end = text.index('\n', start)
        text = text[:start] + script + text[end:]
    text = text.replace("node('div','sport-icon',icon(event.title))", 'sportIcon(event.title)')
    text = text.replace("node('div','sport-icon',icon(wish.sport))", 'sportIcon(wish.sport)')
    app.write_text(text, encoding='utf-8')
    cards = ''.join(f'<article><svg {ATTRS}>{paths}</svg><span>{escape(key)}</span></article>' for key, paths in ICONS.items())
    (ROOT / 'preview.html').write_text('<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Иконки Inno Sport</title><style>body{font:14px system-ui;background:#f5f7f4;color:#254936;margin:32px}h1{font-size:26px}main{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px}article{display:flex;align-items:center;gap:14px;background:white;border:1px solid #dfe8df;padding:18px;border-radius:16px}svg{width:32px;height:32px;flex-shrink:0}span{font-size:12px}</style><h1>Inno Sport · Иконки</h1><main>' + cards + '</main></html>', encoding='utf-8')
    print(f'Built {len(ICONS)} SVG icons; mapped all {len(mapping)} training names.')


if __name__ == '__main__':
    build()
