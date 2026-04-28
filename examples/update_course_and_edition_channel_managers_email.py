#!/usr/bin/env python3
# -*- coding: utf-8 -*-
'''
Update managers_emails for course channels AND their edition sub-channels,
based on kanaal_emails.csv.

Channel structure assumed:
  Level 1 (primary): Faculty channels
  Level 2 (sub):     Course channels — title starts with the course code
  Level 3 (sub-sub): Edition channels — children of course channels

For each course channel the first space-separated word of the title is matched
against the CURSUS column in kanaal_emails.csv.  When a match is found, the
email from E_MAIL_ADRES is applied to both the course channel and all of its
edition sub-channels.

Sets managers_emails on each matched channel via channels/edit/.

A report CSV is written with one row per channel processed (course + editions).
Unmatched channels are recorded with an empty old/new email (no API call made).
'''
import argparse
import csv
from datetime import datetime
import os
from pathlib import Path
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed


# Thread-local storage so each worker thread has its own MediaServerClient
# (requests.Session is not safe to share across threads).
_thread_local = threading.local()


def _get_thread_client(conf_path):
    if not hasattr(_thread_local, 'msc'):
        _thread_local.msc = MediaServerClient(conf_path, setup_logging=False)
    return _thread_local.msc


def update_channel(msc, oid, new_email, dry_run):
    '''Apply the email update to a single channel. Returns error string or None.'''
    if dry_run:
        return None
    try:
        msc.api(
            'channels/edit/',
            method='post',
            data={
                'oid': oid,
                'managers_emails': new_email,
            },
        )
    except Exception as e:
        return str(e)
    return None


def _catalog_email(channel):
    '''Return the raw managers_emails string from a catalog channel dict.
    The catalog provides two fields: managers_emails (a resolved list of matched
    user objects) and managers_emails_raw (the plain string as stored via
    channels/edit/). We compare against the raw value to detect actual changes.'''
    return channel.get('managers_emails_raw') or ''


def _process_course_channel(channel, cursus_email, children_of, conf_path, dry_run, server_url, faculty_title):
    '''
    Process one course channel and its edition sub-channels in a worker thread.
    Returns (row, course_updated: bool, course_already_correct: bool,
             editions_updated: int, editions_already_correct: int).
    One CSV row is produced per course channel; edition counts are included as fields.
    Old emails are read directly from the catalog data already in memory.
    '''
    oid = channel['oid']
    title = channel.get('title', '').strip()
    words = title.split()
    course_code = words[0] if words else ''

    if not course_code or course_code not in cursus_email:
        row = {
            'Match': 'no',
            'faculty': faculty_title,
            'Course': title,
            'code': course_code,
            'old email': _catalog_email(channel),
            'new email': '',
            'editions': len(children_of.get(oid, [])),
            'updated': 0,
            'link': f'{server_url}/permalink/{oid}/',
        }
        return row, False, False, 0, 0

    new_email = cursus_email[course_code]
    old_email = _catalog_email(channel)
    if old_email != new_email:
        if not dry_run:
            msc = _get_thread_client(conf_path)
            error = update_channel(msc, oid, new_email, dry_run)
            match_value = 'error' if error else 'yes'
        else:
            match_value = 'yes'
        course_updated = True
        course_already_correct = False
    else:
        match_value = 'correct'
        course_updated = False
        course_already_correct = True

    editions_updated = 0
    editions_already_correct = 0
    for edition in children_of.get(oid, []):
        ed_oid = edition['oid']
        ed_old_email = _catalog_email(edition)
        if ed_old_email != new_email:
            if not dry_run:
                msc = _get_thread_client(conf_path)
                ed_error = update_channel(msc, ed_oid, new_email, dry_run)
            editions_updated += 1
        else:
            editions_already_correct += 1

    row = {
        'Match': match_value,
        'faculty': faculty_title,
        'Course': title,
        'code': course_code,
        'old email': old_email,
        'new email': new_email,
        'editions': editions_updated + editions_already_correct,
        'updated': editions_updated,
        'link': f'{server_url}/permalink/{oid}/',
    }
    return row, course_updated, course_already_correct, editions_updated, editions_already_correct


if __name__ == '__main__':
    sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from ms_client.client import MediaServerClient

    parser = argparse.ArgumentParser(
        description=__doc__.strip(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        '--conf',
        default='acc.json',
        help='Path to the configuration file.',
        type=str,
    )
    parser.add_argument(
        '--csv',
        default='kanaal_emails.csv',
        help='Path to the kanaal_emails CSV file.',
        type=str,
    )
    parser.add_argument(
        '--report',
        default=f'update_manager_email_{datetime.now().strftime("%Y%m%d_%H%M%S")}.csv',
        help='Path to the output report CSV file.',
        type=str,
    )
    parser.add_argument(
        '--apply',
        action='store_true',
        help='Apply changes. Without this flag the script runs as a dry run and makes no API calls.',
    )
    parser.add_argument(
        '--workers',
        default=5,
        type=int,
        help='Number of parallel worker threads for API calls (default: 5).',
    )
    args = parser.parse_args()

    # Append the conf file stem to the report filename, e.g.
    # update_managers_email_with_editions_report_acc.csv
    conf_stem = Path(args.conf).stem
    report_path = Path(args.report)
    args.report = str(report_path.with_stem(report_path.stem + '_' + conf_stem))

    # -------------------------------------------------------------------------
    # Load CSV: build a lookup dict  CURSUS -> E_MAIL_ADRES
    # -------------------------------------------------------------------------
    cursus_email = {}
    for encoding in ('utf-8-sig', 'cp1252', 'latin-1'):
        try:
            with open(args.csv, newline='', encoding=encoding) as f:
                reader = csv.DictReader(f)
                for row in reader:
                    cursus = row.get('CURSUS', '').strip()
                    email = row.get('E_MAIL_ADRES', '').strip()
                    if cursus and email:
                        cursus_email[cursus] = email
            print(f'Read CSV with encoding: {encoding}')
            break
        except UnicodeDecodeError:
            cursus_email = {}
            continue
    else:
        print(f'Error: could not decode {args.csv} with any supported encoding.')
        sys.exit(1)

    print(f'Loaded {len(cursus_email)} CURSUS->email mapping(s) from {args.csv}')
    if not cursus_email:
        print('No usable rows found in CSV. Exiting.')
        sys.exit(1)

    # -------------------------------------------------------------------------
    # Connect and fetch the full channel catalog
    # -------------------------------------------------------------------------
    msc = MediaServerClient(args.conf)
    print(msc.api('/'))

    print('Fetching channel catalog...')
    catalog = msc.get_catalog(fmt='flat')
    all_channels = catalog.get('channels', [])
    print(f'Total channels in catalog: {len(all_channels)}')

    # -------------------------------------------------------------------------
    # Build lookup: parent_oid -> list of child channels
    # -------------------------------------------------------------------------
    children_of = {}
    for ch in all_channels:
        parent = ch.get('parent_oid')
        if parent:
            children_of.setdefault(parent, []).append(ch)

    # -------------------------------------------------------------------------
    # Identify level-1 (faculty) and level-2 (course) channels
    # -------------------------------------------------------------------------
    RECYCLE_BIN_OID = 'c00000000000000trash'
    faculty_channels = {
        ch['oid']: ch for ch in all_channels
        if not ch.get('parent_oid') and ch['oid'] != RECYCLE_BIN_OID
    }
    course_channels = [ch for ch in all_channels if ch.get('parent_oid') in faculty_channels]

    print(f'Faculty channels (level 1): {len(faculty_channels)}')
    print(f'Course channels  (level 2): {len(course_channels)}')
    print()

    # -------------------------------------------------------------------------
    # Process course channels and their edition sub-channels (parallel)
    # -------------------------------------------------------------------------
    # Each future processes one course channel + its edition sub-channels.
    # Unmatched channels generate no API calls at all.
    # -------------------------------------------------------------------------
    report_rows_by_idx = {}   # idx -> rows, to preserve catalog order in report
    faculty_stats = {
        oid: {'title': ch.get('title', oid), 'c_updated': 0, 'c_correct': 0, 'c_unmatched': 0, 'ed_updated': 0, 'ed_correct': 0}
        for oid, ch in faculty_channels.items()
    }
    completed = 0
    total = len(course_channels)
    lock = threading.Lock()

    server_url = msc.conf['SERVER_URL'].rstrip('/')
    print(f'Processing {total} course channels with {args.workers} workers...')

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(
                _process_course_channel,
                channel,
                cursus_email,
                children_of,
                args.conf,
                not args.apply,
                server_url,
                faculty_channels[channel['parent_oid']].get('title', ''),
            ): idx
            for idx, channel in enumerate(course_channels)
        }
        for future in as_completed(futures):
            idx = futures[future]
            row, c_updated, c_correct, ed_updated, ed_correct = future.result()
            faculty_oid = course_channels[idx].get('parent_oid')
            with lock:
                report_rows_by_idx[idx] = row
                s = faculty_stats[faculty_oid]
                if c_updated:
                    s['c_updated'] += 1
                elif c_correct:
                    s['c_correct'] += 1
                else:
                    s['c_unmatched'] += 1
                s['ed_updated'] += ed_updated
                s['ed_correct'] += ed_correct
                completed += 1
                print(f'\r[{completed}/{total}]', end='', flush=True)

    print()  # end progress line

    report_rows = sorted(
        (report_rows_by_idx[idx] for idx in range(total)),
        key=lambda r: (r['faculty'].lower(), r['Course'].lower()),
    )

    # -------------------------------------------------------------------------
    # Write report CSV
    # -------------------------------------------------------------------------
    fieldnames = ['Match', 'faculty', 'Course', 'code', 'editions', 'updated', 'old email', 'new email', 'link']
    with open(args.report, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(report_rows)

    updated_label = 'updated' if args.apply else 'would be updated'
    summary_lines = []
    for s in sorted(faculty_stats.values(), key=lambda x: x['title']):
        summary_lines.append(s['title'])
        summary_lines.append(f"  Courses  — {updated_label}: {s['c_updated']}, already correct: {s['c_correct']}, unmatched: {s['c_unmatched']}")
        summary_lines.append(f"  Editions — {updated_label}: {s['ed_updated']}, already correct: {s['ed_correct']}")
    summary_lines.append(f'\nReport written to: {args.report}')
    if not args.apply:
        summary_lines.append('(dry run — no changes were made)')

    summary_text = '\n'.join(summary_lines)
    print()
    print(summary_text)

    summary_path = f'update_emails_summary_{conf_stem}.txt'
    Path(summary_path).write_text(summary_text + '\n', encoding='utf-8')
    print(f'Summary written to: {summary_path}')
