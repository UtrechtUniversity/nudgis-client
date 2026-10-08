#!/usr/bin/env python3
'''
Script to list every distinct category string as actually stored on medias.

Categories are plain free-text strings on each media (not a translated enum), so
they are returned in whatever language they were entered. Use this to confirm the
exact stored value before passing it to --skip-category in mass_delete_old_medias.py
(note that matching there is case-insensitive).
'''

import argparse
import os
import sys
from collections import Counter


if __name__ == '__main__':
    sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from ms_client.client import MediaServerClient

    parser = argparse.ArgumentParser(description=__doc__.strip())
    parser.add_argument(
        '--conf',
        help='Path to the configuration file.',
        required=True,
        type=str,
    )

    args = parser.parse_args()
    msc = MediaServerClient(args.conf)

    catalog = msc.get_catalog('flat')
    counts = Counter()
    for key in ('videos', 'lives'):
        for media in catalog.get(key, ()):
            for cat in (media['categories'] or '').strip('\n').split('\n'):
                cat = cat.strip()
                if cat:
                    counts[cat] += 1

    if not counts:
        print('No categories found on any media.')
    else:
        print(f'Found {len(counts)} distinct categories:')
        for cat, count in sorted(counts.items()):
            print(f'  {count:>6}  {cat!r}')
