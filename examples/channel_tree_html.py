#!/usr/bin/env python3
# -*- coding: utf-8 -*-
'''
Script to generate an interactive HTML visualisation of the channel tree structure.

Fetches the full catalog and renders a collapsible tree showing channels,
with media counts at each node.

To use this script clone MediaServer client, configure it and run this file.

git clone https://github.com/UbiCastTeam/mediaserver-client
cd mediaserver-client
python3 examples/channel_tree_html.py --conf conf.json
'''

import argparse
import os
import sys
from pathlib import Path


HTML_TEMPLATE = '''\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Channel Tree — {server_url}</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}

    body {{
      font-family: system-ui, -apple-system, sans-serif;
      font-size: 14px;
      background: #f5f6f8;
      color: #1a1a2e;
      padding: 24px;
    }}

    h1 {{
      font-size: 1.4rem;
      font-weight: 600;
      margin-bottom: 6px;
    }}

    .meta {{
      font-size: 0.85rem;
      color: #666;
      margin-bottom: 20px;
    }}

    .controls {{
      margin-bottom: 16px;
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      align-items: center;
    }}

    .controls button {{
      padding: 5px 14px;
      border: 1px solid #ccc;
      border-radius: 6px;
      background: #fff;
      cursor: pointer;
      font-size: 0.85rem;
    }}

    .controls button:hover {{ background: #e8eaf0; }}

    .search-box {{
      padding: 5px 10px;
      border: 1px solid #ccc;
      border-radius: 6px;
      font-size: 0.85rem;
      width: 240px;
    }}

    .tree {{
      list-style: none;
      padding-left: 0;
    }}

    .tree ul {{
      list-style: none;
      padding-left: 22px;
      border-left: 2px solid #dde1ea;
      margin-left: 10px;
    }}

    .tree li {{
      margin: 2px 0;
    }}

    .node-row {{
      display: flex;
      align-items: center;
      gap: 6px;
      padding: 4px 6px;
      border-radius: 6px;
      cursor: default;
      user-select: none;
    }}

    .node-row:hover {{ background: #e8eaf0; }}

    .toggle {{
      width: 18px;
      height: 18px;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      font-size: 10px;
      border: 1px solid #bbb;
      border-radius: 4px;
      cursor: pointer;
      flex-shrink: 0;
      color: #555;
      background: #fff;
      line-height: 1;
    }}

    .toggle:hover {{ background: #d0d4e0; }}

    .toggle-placeholder {{
      width: 18px;
      flex-shrink: 0;
    }}

    .icon {{
      font-size: 15px;
      flex-shrink: 0;
    }}

    .channel-title {{
      font-weight: 500;
      flex: 1;
    }}

    .channel-title a {{
      color: inherit;
      text-decoration: none;
    }}

    .channel-title a:hover {{ text-decoration: underline; }}

    .badges {{
      display: flex;
      gap: 4px;
      flex-shrink: 0;
    }}

    .badge {{
      font-size: 0.72rem;
      padding: 1px 6px;
      border-radius: 10px;
      font-weight: 600;
      white-space: nowrap;
    }}

    .badge-channels  {{ background: #dbeafe; color: #1e40af; }}
    .badge-videos    {{ background: #dcfce7; color: #166534; }}
    .badge-lives     {{ background: #fee2e2; color: #991b1b; }}
    .badge-photos    {{ background: #fef9c3; color: #854d0e; }}

    .icon-empty {{ filter: grayscale(1) opacity(0.45); }}

    .hidden {{ display: none; }}
    .highlight .channel-title {{ background: #fef08a; border-radius: 3px; padding: 0 2px; }}

    .summary-bar {{
      margin-top: 24px;
      padding: 12px 16px;
      background: #fff;
      border: 1px solid #dde1ea;
      border-radius: 8px;
      font-size: 0.85rem;
      color: #555;
      display: flex;
      gap: 20px;
      flex-wrap: wrap;
    }}

    .summary-bar span {{ font-weight: 600; color: #1a1a2e; }}
  </style>
</head>
<body>
  <h1>Channel Tree</h1>
  <p class="meta">Server: <strong>{server_url}</strong> &nbsp;|&nbsp; Generated: <strong>{generated}</strong></p>

  <div class="controls">
    <button onclick="expandAll()">Expand all</button>
    <button onclick="collapseAll()">Collapse all</button>
    <input class="search-box" type="search" placeholder="Filter channels…" oninput="filterTree(this.value)" />
  </div>

  <ul class="tree" id="tree">
{tree_html}
  </ul>

  <div class="summary-bar">
    Total &mdash;
    Channels: <span>{total_channels}</span>
    Videos: <span>{total_videos}</span>
    Live streams: <span>{total_lives}</span>
    Photo groups: <span>{total_photos}</span>
  </div>

  <script>
    function toggle(btn) {{
      const li = btn.closest('li');
      const sub = li.querySelector(':scope > ul');
      if (!sub) return;
      const collapsed = sub.classList.toggle('hidden');
      btn.textContent = collapsed ? '+' : '−';
    }}

    function expandAll() {{
      document.querySelectorAll('.tree ul').forEach(el => el.classList.remove('hidden'));
      document.querySelectorAll('.toggle').forEach(btn => btn.textContent = '−');
    }}

    function collapseAll() {{
      document.querySelectorAll('.tree ul').forEach(el => el.classList.add('hidden'));
      document.querySelectorAll('.toggle').forEach(btn => btn.textContent = '+');
    }}

    function filterTree(query) {{
      const q = query.trim().toLowerCase();
      document.querySelectorAll('li[data-title]').forEach(li => {{
        li.classList.remove('highlight');
      }});
      if (!q) {{
        // Restore default state: all li visible, all sub-lists collapsed
        document.querySelectorAll('li[data-title]').forEach(li => li.classList.remove('hidden'));
        document.querySelectorAll('.tree ul').forEach(el => el.classList.add('hidden'));
        document.querySelectorAll('.toggle').forEach(btn => btn.textContent = '+');
        return;
      }}
      // Hide all, then show matches and their ancestors
      document.querySelectorAll('li[data-title]').forEach(li => li.classList.add('hidden'));
      document.querySelectorAll('.tree ul').forEach(el => el.classList.remove('hidden'));

      document.querySelectorAll('li[data-title]').forEach(li => {{
        const title = li.dataset.title.toLowerCase();
        if (title.includes(q)) {{
          li.classList.remove('hidden');
          li.classList.add('highlight');
          // Show all ancestors
          let parent = li.parentElement;
          while (parent && parent.id !== 'tree') {{
            if (parent.tagName === 'LI') parent.classList.remove('hidden');
            if (parent.tagName === 'UL') parent.classList.remove('hidden');
            parent = parent.parentElement;
          }}
        }}
      }});
    }}
  </script>
</body>
</html>
'''


def count_media(channel):
    '''Recursively count videos, lives, photos_groups in a channel subtree.'''
    videos = len(channel.get('videos', []))
    lives = len(channel.get('lives', []))
    photos = len(channel.get('photos_groups', []))
    channels = len(channel.get('channels', []))
    for sub in channel.get('channels', []):
        sc, sv, sl, sp = count_media(sub)
        channels += sc
        videos += sv
        lives += sl
        photos += sp
    return channels, videos, lives, photos


def render_channel(channel, server_url, depth=0):
    '''Recursively render a channel node as HTML list items.'''
    oid = channel.get('oid', '')
    title = channel.get('title', '(untitled)')
    sub_channels = channel.get('channels', [])
    videos = channel.get('videos', [])
    lives = channel.get('lives', [])
    photos = channel.get('photos_groups', [])

    # Count all descendants for badge display
    total_sub_channels, total_videos, total_lives, total_photos = count_media(channel)

    indent = '  ' * (depth + 2)
    permalink = f'{server_url}/permalink/{oid}/'

    badges = []
    if total_sub_channels:
        badges.append(f'<span class="badge badge-channels" title="Sub-channels">{total_sub_channels} ch</span>')
    if total_videos:
        badges.append(f'<span class="badge badge-videos" title="Videos">{total_videos} vid</span>')
    if total_lives:
        badges.append(f'<span class="badge badge-lives" title="Live streams">{total_lives} live</span>')
    if total_photos:
        badges.append(f'<span class="badge badge-photos" title="Photo groups">{total_photos} photo</span>')

    badges_html = f'<div class="badges">{"".join(badges)}</div>' if badges else ''

    # All sub-channel lists start collapsed so only level-0 channels are visible on load
    icon_class = 'icon' if total_videos else 'icon icon-empty'

    if sub_channels:
        toggle_btn = '<button class="toggle" onclick="toggle(this)" title="Toggle">+</button>'
    else:
        toggle_btn = '<span class="toggle-placeholder"></span>'

    lines = [
        f'{indent}<li data-title="{_escape(title)}" data-oid="{oid}">',
        f'{indent}  <div class="node-row">',
        f'{indent}    {toggle_btn}',
        f'{indent}    <span class="{icon_class}">📁</span>',
        f'{indent}    <span class="channel-title"><a href="{permalink}" target="_blank">{_escape(title)}</a></span>',
        f'{indent}    {badges_html}',
        f'{indent}  </div>',
    ]

    if sub_channels:
        lines.append(f'{indent}  <ul class="hidden">')
        for sub in sub_channels:
            lines.append(render_channel(sub, server_url, depth + 1))
        lines.append(f'{indent}  </ul>')

    lines.append(f'{indent}</li>')
    return '\n'.join(lines)


def _escape(text):
    return (
        text
        .replace('&', '&amp;')
        .replace('<', '&lt;')
        .replace('>', '&gt;')
        .replace('"', '&quot;')
    )


def generate_html(msc, output_path):
    from datetime import datetime

    server_url = msc.conf['SERVER_URL'].rstrip('/')
    print('Fetching catalog (this may take a while for large platforms)...')
    tree = msc.get_catalog(fmt='tree')

    top_channels = tree.get('channels', [])
    print(f'Got {len(top_channels)} top-level channel(s), building tree...')

    tree_lines = []
    for channel in top_channels:
        tree_lines.append(render_channel(channel, server_url, depth=0))

    tree_html = '\n'.join(tree_lines)

    # Totals across entire catalog
    total_channels = 0
    total_videos = 0
    total_lives = 0
    total_photos = 0
    for channel in top_channels:
        sc, sv, sl, sp = count_media(channel)
        total_channels += 1 + sc
        total_videos += sv
        total_lives += sl
        total_photos += sp

    generated = datetime.now().strftime('%Y-%m-%d %H:%M')

    html = HTML_TEMPLATE.format(
        server_url=server_url,
        generated=generated,
        tree_html=tree_html,
        total_channels=total_channels,
        total_videos=total_videos,
        total_lives=total_lives,
        total_photos=total_photos,
    )

    output_path = Path(output_path)
    output_path.write_text(html, encoding='utf-8')
    print(f'HTML tree written to: {output_path}')


if __name__ == '__main__':
    sys.path.append(str(Path(__file__).resolve().parent.parent))
    from ms_client.client import MediaServerClient

    parser = argparse.ArgumentParser(description=__doc__.strip())
    parser.add_argument(
        '--conf',
        dest='configuration',
        help='Path to the configuration file.',
        required=True,
        type=str,
    )
    parser.add_argument(
        '--output',
        dest='output',
        help='Path for the output HTML file (default: channel-tree-<host>.html).',
        default=None,
        type=str,
    )
    args = parser.parse_args()

    if not args.configuration.startswith('unix:') and not Path(args.configuration).exists():
        print('Invalid path for configuration file.')
        sys.exit(1)

    msc = MediaServerClient(args.configuration)
    msc.check_server()

    output = args.output
    if output is None:
        host = msc.conf['SERVER_URL'].split('://')[-1].rstrip('/')
        output = f'channel-tree-{host}.html'

    generate_html(msc, output)
