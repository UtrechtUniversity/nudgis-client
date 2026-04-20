#!/usr/bin/env python3
'''
Script to delete empty channels from Nudgis.

To use this script clone MediaServer client, configure it and run this file.

git clone https://github.com/UbiCastTeam/mediaserver-client
cd mediaserver-client
python3 examples/delete_empty_channels.py --conf conf.json --max-add-date YYYY-MM-DD --exclude channel_oid
'''

import argparse
import csv
from datetime import date, datetime
import re
from pathlib import Path
import sys


def empty_channels_iterator(
    channel_info,
    channel_oid_blacklist=(),
    max_date=date.today(),
    min_depth=0,
):
    for channel in channel_info.get('channels', ()):
        channel['path'] = list(channel_info.get('path', [])) + [channel['title']]
        skip_channel = (
            channel['oid'] in channel_oid_blacklist
            or datetime.strptime(channel['add_date'], '%Y-%m-%d %H:%M:%S').date() >= max_date
            or len(channel.get('channels', ())) > 0
            or len(channel.get('videos', ())) > 0
            or len(channel.get('photos', ())) > 0
            or len(channel.get('lives', ())) > 0
            or len(channel['path']) < min_depth
        )
        if not skip_channel:
            yield channel
        if channel['oid'] not in channel_oid_blacklist:
            yield from empty_channels_iterator(
                channel,
                channel_oid_blacklist=channel_oid_blacklist,
                max_date=max_date,
                min_depth=min_depth,
            )


def clean_tree(tree, deleted_oids):
    for channel in list(tree.get('channels', ())):
        if channel['oid'] in deleted_oids:
            tree['channels'].remove(channel)
        else:
            clean_tree(channel, deleted_oids)


def delete_empty_channels(msc, channel_oid_blacklist, max_date, min_depth, apply=False, faculty_oids=None, tree=None, timeout=300):
    if tree is None:
        tree = msc.get_catalog(fmt='tree')
    channel_oid_blacklist = list(channel_oid_blacklist)
    ms_url = msc.conf['SERVER_URL'].rstrip('/') + '/permalink/'
    report_rows = []

    if faculty_oids:
        working_tree = {'channels': [ch for ch in tree.get('channels', []) if ch['oid'] in faculty_oids]}
    else:
        working_tree = tree

    while True:
        empty_channels = list(empty_channels_iterator(
            working_tree,
            channel_oid_blacklist=channel_oid_blacklist,
            max_date=max_date,
            min_depth=min_depth,
        ))
        if not empty_channels:
            break
        empty_by_oid = {ch['oid']: ch for ch in empty_channels}
        if apply:
            response = msc.api(
                'catalog/bulk_delete/',
                method='post',
                data=dict(oids=list(empty_by_oid)),
                timeout=timeout,
            )
            deleted_oids = {
                oid
                for oid, result in response['statuses'].items()
                if result['status'] == 200
            }
            if not deleted_oids:
                break
        else:
            deleted_oids = set(empty_by_oid)

        for oid in deleted_oids:
            path = empty_by_oid[oid].get('path', [])
            report_rows.append({
                'faculty': path[0] if len(path) > 0 else '',
                'course':  path[1] if len(path) > 1 else '',
                'edition': path[2] if len(path) > 2 else '',
                'link':    f'{ms_url}{oid}/',
            })

        clean_tree(working_tree, deleted_oids)

    return report_rows


def main():
    sys.path.append(str(Path(__file__).resolve().parent.parent))
    from ms_client.client import MediaServerClient

    parser = argparse.ArgumentParser(description=__doc__.strip())
    parser.add_argument(
        '--conf',
        dest='configuration',
        help='Path to the configuration file.',
        required=True,
        type=str)

    parser.add_argument(
        '--apply',
        help='Whether to apply changes or not',
        action='store_true',
    )

    parser.add_argument(
        '--exclude',
        action='append',
        dest='exclude_oid',
        help=('Channel oid that should not be deleted. '
              'You can give this parameter multiple times to exclude multiple channels'),
        type=str)

    parser.add_argument(
        '--max-add-date',
        default=datetime.today().strftime('%Y-%m-%d'),
        dest='max_date',
        help=('All channel created prior to the given date will be deleted. '
              'Date format: "YYYY-MM-DD".'),
        type=str)

    parser.add_argument(
        '--min-depth',
        default=0,
        dest='min_depth',
        help=('Minimum path length a channel must have to be eligible for deletion. '
              '1=faculty, 2=course, 3=edition. Default 0 means all levels.'),
        type=int)

    parser.add_argument(
        '--timeout',
        default=300,
        dest='timeout',
        help='Timeout in seconds for bulk_delete API calls. Default: %(default)s.',
        type=int)

    args = parser.parse_args()

    print(f'Configuration path: {args.configuration}')
    print(f'Date limit: {args.max_date}')
    print(f'Blacklist channel: {args.exclude_oid}')
    print(f'Minimum depth: {args.min_depth}')
    print(f'Apply changes: {args.apply}')

    # Check if configuration file exists
    if not args.configuration.startswith('unix:') and not Path(args.configuration).exists():
        print('Invalid path for configuration file.')
        return 1

    # Check date format
    try:
        max_date = datetime.strptime(str(args.max_date), '%Y-%m-%d').date()
    except ValueError:
        print('Incorrect data format, should be "YYYY-MM-DD".')
        return 1

    msc = MediaServerClient(args.configuration)
    msc.check_server()

    # Check channel oid
    if args.exclude_oid:
        for oid_blacklist in args.exclude_oid:
            # Check if channel oid exists
            try:
                msc.api('channels/get/', method='get', params=dict(oid=oid_blacklist))
            except Exception as e:
                print(
                    f'Please enter valid channel oid {oid_blacklist} or check access permissions.'
                    f'Error when trying to get channel was: {e}'
                )
                return 1
    else:
        args.exclude_oid = []

    print('Fetching catalog...')
    tree = msc.get_catalog(fmt='tree')
    faculties = sorted(tree.get('channels', []), key=lambda ch: ch.get('title', ''))

    print('\nAvailable faculties:')
    print('  0. All faculties')
    for i, ch in enumerate(faculties, 1):
        print(f'  {i}. {ch["title"]}')

    selection = input('\nSelect faculties to process (0 for all, or comma/space separated numbers): ').strip()

    if not selection or selection == '0':
        faculty_oids = None
    else:
        indices = [int(x) for x in re.split(r'[\s,]+', selection) if x.isdigit()]
        invalid = [x for x in indices if not (1 <= x <= len(faculties))]
        if invalid:
            print(f'Invalid selection(s): {invalid}')
            return 1
        faculty_oids = {faculties[i - 1]['oid'] for i in indices}
        selected_titles = [faculties[i - 1]['title'] for i in indices]
        print(f'\nProcessing: {", ".join(selected_titles)}')

    report_rows = delete_empty_channels(msc, args.exclude_oid, max_date, args.min_depth, args.apply, faculty_oids=faculty_oids, tree=tree, timeout=args.timeout)

    if report_rows:
        faculty_counts = {}
        for row in report_rows:
            faculty = row['faculty']
            if faculty not in faculty_counts:
                faculty_counts[faculty] = {'course': 0, 'edition': 0, 'other': 0}
            if row['edition']:
                faculty_counts[faculty]['edition'] += 1
            elif row['course']:
                faculty_counts[faculty]['course'] += 1
            else:
                faculty_counts[faculty]['other'] += 1
        label = 'deleted' if args.apply else 'would be deleted'
        summary_lines = []
        for faculty in sorted(faculty_counts):
            counts = faculty_counts[faculty]
            parts = []
            if counts['course']:
                parts.append(f"{counts['course']} course(s)")
            if counts['edition']:
                parts.append(f"{counts['edition']} edition(s)")
            if counts['other']:
                parts.append(f"{counts['other']} other channel(s)")
            summary_lines.append(f'{faculty}:')
            for part in parts:
                summary_lines.append(f'  {part} {label}')

        summary_text = '\n'.join(summary_lines)
        print(summary_text)

        datestamp = datetime.now().strftime('%Y%m%d_%H%M%S')

        summary_path = f'delete_empty_channels_summary_{datestamp}.txt'
        Path(summary_path).write_text(summary_text + '\n', encoding='utf-8')
        print(f'Summary written to: {summary_path}')

        report_path = f'delete_empty_channels_{datestamp}.csv'
        with open(report_path, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=['faculty', 'course', 'edition', 'link'])
            writer.writeheader()
            writer.writerows(report_rows)
        print(f'Report written to: {report_path}')

    return 0


if __name__ == '__main__':
    rc = main()
    sys.exit(rc)
