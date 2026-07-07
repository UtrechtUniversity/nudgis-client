#!/usr/bin/env python3
'''
Script to remove speakers from recordings (videos and lives) based on a CSV file.

The CSV file must contain at least 2 of the columns "Name", "ID" and "Email"
(header names are case-insensitive); typically "Name" and "Email" are enough,
"ID" is optional. A speaker entry on a recording is removed when at least 2 of
these 3 data points match the CSV row (comparison is case-insensitive).

Optional columns "Replacement Name", "Replacement ID" and "Replacement Email"
can be added. When a replacement name is provided, the removed speaker is
replaced by the replacement speaker at the same position in the speaker list
(so a speaker removed from the top of the list is replaced at the top).
Other speakers on the recording are never touched.

The personal channels root (a top-level channel, see --personal-channels-title)
is always skipped: it is not shown in the faculty menu and its recordings are
never modified.

The script starts with a menu to select one, several or all faculties
(top-level channels). By default it runs in dry-run mode; pass --apply to
actually modify the recordings. In both modes a CSV report is written with one
row per affected recording.
'''
from __future__ import annotations

import argparse
import csv
import logging
import os
import re
import sys
from dataclasses import dataclass
from datetime import date
from itertools import zip_longest
from pathlib import Path
from typing import NamedTuple, Optional

try:
    from ms_client.client import MediaServerClient
except ModuleNotFoundError:
    sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from ms_client.client import MediaServerClient


logger = logging.getLogger(__name__)


# The Recycle bin is a top-level channel: keep it out of the faculty menu and
# never edit media inside it (edits there can fail with 403 errors).
RECYCLE_BIN_OID = 'c00000000000000trash'


class Speaker(NamedTuple):
    email: str
    id: str
    name: str


@dataclass
class RemovalRule:
    name: str
    id: str
    email: str
    replacement: Optional[Speaker] = None

    def matches(self, speaker: Speaker) -> bool:
        '''At least 2 of the 3 data points must match (both sides non-empty).'''
        matches = 0
        if self.name and speaker.name and self.name.lower() == speaker.name.lower():
            matches += 1
        if self.id and speaker.id and self.id.lower() == speaker.id.lower():
            matches += 1
        if self.email and speaker.email and self.email.lower() == speaker.email.lower():
            matches += 1
        return matches >= 2


def _read_csv_rows(csv_path: Path) -> tuple[list[str], list[dict]]:
    '''Read the CSV with encoding fallback (real-world exports are not always UTF-8).'''
    for encoding in ('utf-8-sig', 'cp1252', 'latin-1'):
        try:
            with csv_path.open('r', newline='', encoding=encoding) as csvfile:
                reader = csv.DictReader(csvfile)
                if reader.fieldnames is None:
                    raise ValueError(f'CSV file "{csv_path}" is empty.')
                rows = list(reader)
            logger.debug(f'Read CSV with encoding: {encoding}')
            return reader.fieldnames, rows
        except UnicodeDecodeError:
            continue
    raise ValueError(f'Could not decode "{csv_path}" with any supported encoding.')


def _load_rules(csv_path: Path) -> list[RemovalRule]:
    fieldnames, rows = _read_csv_rows(csv_path)
    # Normalize headers: strip spaces, lowercase, underscores to spaces
    # (tolerates "Name, ID, Email" headers with spaces after the commas).
    fields = {
        (name or '').strip().lower().replace('_', ' '): name
        for name in fieldnames
    }
    # At least 2 of the 3 identification columns are needed (matching requires
    # 2 of 3 data points). Typically "Name" and "Email"; "ID" is optional.
    present = [col for col in ('name', 'id', 'email') if col in fields]
    if len(present) < 2:
        raise ValueError(
            f'CSV file "{csv_path}" must contain at least 2 of the "Name", '
            f'"ID" and "Email" columns. Found columns: {fieldnames}'
        )

    def get(row: dict, key: str) -> str:
        original = fields.get(key)
        if original is None:
            return ''
        return (row.get(original) or '').strip()

    rules = []
    for line_num, row in enumerate(rows, 2):
        name = get(row, 'name')
        spk_id = get(row, 'id')
        email = get(row, 'email')
        provided = sum(1 for value in (name, spk_id, email) if value)
        if provided == 0:
            continue
        if provided < 2:
            logger.warning(
                f'CSV line {line_num} skipped: at least 2 of the 3 data points '
                f'(name, id, email) are required to match reliably '
                f'({name=}, id={spk_id!r}, {email=}).'
            )
            continue
        replacement = None
        replacement_name = get(row, 'replacement name')
        if replacement_name:
            replacement = Speaker(
                email=get(row, 'replacement email'),
                id=get(row, 'replacement id'),
                name=replacement_name,
            )
        rules.append(RemovalRule(name=name, id=spk_id, email=email, replacement=replacement))
    if not rules:
        raise ValueError(f'No usable rows found in CSV file "{csv_path}".')
    return rules


def _build_channel_to_faculty_map(channels: list[dict]) -> dict[str, str]:
    '''Map every channel oid to its top-level (faculty) oid by walking parent_oid.'''
    by_oid = {ch['oid']: ch for ch in channels}
    cache: dict[str, str] = {}

    def find_root(oid: str) -> str:
        if oid in cache:
            return cache[oid]
        ch = by_oid.get(oid)
        if ch is None:
            cache[oid] = oid
            return oid
        parent = ch.get('parent_oid')
        if not parent or parent not in by_oid:
            cache[oid] = oid
            return oid
        root = find_root(parent)
        cache[oid] = root
        return root

    for oid in by_oid:
        find_root(oid)
    return cache


def _parse_media_speakers(media: dict) -> Optional[list[Speaker]]:
    '''Parse the pipe-separated speaker fields. Returns None if unreliable.'''
    emails, ids, names = [], [], []
    if media.get('speaker_email'):
        emails = [value.strip() for value in media['speaker_email'].split('|')]
    if media.get('speaker_id'):
        ids = [value.strip() for value in media['speaker_id'].split('|')]
    if media.get('speaker'):
        names = [value.strip() for value in media['speaker'].split('|')]
    if len({len(lst) for lst in (emails, ids, names) if lst}) > 1:
        return None
    return [
        Speaker(email or '', spk_id or '', name or '')
        for email, spk_id, name in zip_longest(emails, ids, names)
    ]


def _select_faculties(channels: list[dict], personal_channels_title: str) -> Optional[set]:
    '''Show a menu of top-level channels and return the selected oids (None = all).

    The personal channels root is excluded from the menu and from the "all"
    selection: its recordings must never be modified.
    '''
    by_oid = {ch['oid']: ch for ch in channels}
    top_level = [
        ch for ch in channels
        if (not ch.get('parent_oid') or ch['parent_oid'] not in by_oid)
        and ch['oid'] != RECYCLE_BIN_OID
    ]
    personal_roots = [
        ch for ch in top_level
        if (ch.get('title') or '').strip().lower() == personal_channels_title.strip().lower()
    ]
    if personal_roots:
        for ch in personal_roots:
            logger.info(f'Personal channels root "{ch["title"]}" [{ch["oid"]}] will be skipped.')
    else:
        logger.warning(
            f'No top-level channel titled "{personal_channels_title}" was found. '
            'If personal channels exist under a different title, pass it with '
            '--personal-channels-title, otherwise they will NOT be auto-skipped.'
        )
    personal_oids = {ch['oid'] for ch in personal_roots}
    faculties = sorted(
        (ch for ch in top_level if ch['oid'] not in personal_oids),
        key=lambda ch: ch.get('title', ''),
    )

    print('\nAvailable faculties:')
    print('  0. All faculties')
    for i, ch in enumerate(faculties, 1):
        print(f'  {i}. {ch["title"]}')

    selection = input(
        '\nSelect faculties to process (0 for all, or comma/space separated numbers): '
    ).strip()

    if not selection or selection == '0':
        return {ch['oid'] for ch in faculties}
    indices = [int(x) for x in re.split(r'[\s,]+', selection) if x.isdigit()]
    invalid = [x for x in indices if not (1 <= x <= len(faculties))]
    if invalid or not indices:
        print(f'Invalid selection(s): {invalid or selection}')
        sys.exit(1)
    selected_titles = [faculties[i - 1]['title'] for i in indices]
    print(f'\nProcessing: {", ".join(selected_titles)}')
    return {faculties[i - 1]['oid'] for i in indices}


def _remove_speakers(
    msc: MediaServerClient,
    rules: list[RemovalRule],
    faculty_oids: set,
    report_path: Path,
    apply: bool = False,
):
    catalog = msc.get_catalog(fmt='flat')
    channels = {ch['oid']: ch for ch in catalog['channels']}
    channel_to_faculty = _build_channel_to_faculty_map(catalog['channels'])

    report_rows = []
    edit_count = 0
    for key in ('videos', 'lives'):
        for media in catalog.get(key, ()):
            oid = media['oid']
            faculty_oid = channel_to_faculty.get(media.get('parent_oid'))
            if faculty_oid not in faculty_oids:
                continue

            speakers = _parse_media_speakers(media)
            if speakers is None:
                logger.error(
                    f'Media "{oid}" was ignored because its speakers cannot be '
                    f'parsed reliably: speaker={media.get("speaker")!r}, '
                    f'speaker_email={media.get("speaker_email")!r}, '
                    f'speaker_id={media.get("speaker_id")!r}'
                )
                continue

            new_speakers = []
            removed = []
            replacements = []
            for speaker in speakers:
                rule = next((r for r in rules if r.matches(speaker)), None)
                if rule is None:
                    new_speakers.append(speaker)
                    continue
                removed.append(speaker)
                if rule.replacement:
                    replacements.append(rule.replacement)
                    # Replace in place so a speaker removed from the top of the
                    # list is replaced at the top of the list.
                    new_speakers.append(rule.replacement)
            if not removed:
                continue

            # Integrity check: no duplicate emails, ids or names in the result
            # (e.g. the replacement was already a speaker on this recording).
            for attr in ('email', 'id', 'name'):
                values = [getattr(spk, attr).lower() for spk in new_speakers if getattr(spk, attr)]
                if len(values) > len(set(values)):
                    deduped = []
                    seen = set()
                    for spk in new_speakers:
                        value = getattr(spk, attr).lower()
                        if value and value in seen:
                            logger.warning(
                                f'Media "{oid}": duplicate speaker {spk} after '
                                'replacement, keeping only the first occurrence.'
                            )
                            continue
                        if value:
                            seen.add(value)
                        deduped.append(spk)
                    new_speakers = deduped

            media_pp = f'{media["title"]} [{oid}]'
            removed_pp = ', '.join(spk.name or spk.email or spk.id for spk in removed)
            replaced_pp = ', '.join(spk.name for spk in replacements)
            if apply:
                msc.api('medias/edit/', method='post', data={
                    'oid': oid,
                    'speaker': '|'.join(spk.name for spk in new_speakers),
                    'speaker_email': '|'.join(spk.email for spk in new_speakers),
                    'speaker_id': '|'.join(spk.id for spk in new_speakers),
                })
                edit_count += 1
                logger.info(
                    f'{media_pp}: removed [{removed_pp}]'
                    + (f', replaced by [{replaced_pp}]' if replacements else '')
                )
            else:
                logger.info(
                    f'[Dry run] {media_pp}: would remove [{removed_pp}]'
                    + (f', would replace by [{replaced_pp}]' if replacements else '')
                )

            faculty = channels.get(faculty_oid, {})
            parent = channels.get(media.get('parent_oid'), {})
            report_rows.append({
                'oid': oid,
                'title': media['title'],
                'type': key[:-1],
                'faculty': faculty.get('title', ''),
                'channel': parent.get('title', ''),
                'url': f'{msc.conf["SERVER_URL"]}/permalink/{oid}/',
                'removed_speakers': ' | '.join(
                    f'{spk.name} <{spk.email}> ({spk.id})' for spk in removed
                ),
                'replacement_speakers': ' | '.join(
                    f'{spk.name} <{spk.email}> ({spk.id})' for spk in replacements
                ),
                'remaining_speakers': ' | '.join(
                    f'{spk.name} <{spk.email}> ({spk.id})' for spk in new_speakers
                ),
                'status': 'removed' if apply else 'would be removed (dry run)',
            })

    with report_path.open('w', newline='', encoding='utf-8') as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=[
            'oid', 'title', 'type', 'faculty', 'channel', 'url',
            'removed_speakers', 'replacement_speakers', 'remaining_speakers', 'status',
        ])
        writer.writeheader()
        writer.writerows(sorted(report_rows, key=lambda r: (r['faculty'], r['channel'], r['title'])))

    mode_pp = 'Applied' if apply else 'Dry run:'
    logger.info(
        f'{mode_pp} {len(report_rows)} recording(s) affected'
        + (f', {edit_count} updated' if apply else '')
        + f'. Report written to "{report_path}".'
    )


def remove_speakers(sys_args):
    parser = argparse.ArgumentParser(
        'remove_speakers',
        description=__doc__.strip(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        '--conf',
        help='Path to the configuration file (e.g. myconfig.json).',
        required=True,
        type=str,
    )
    parser.add_argument(
        '--csv-file',
        help='Path of the CSV file listing the speakers to remove. At least 2 '
             'of the columns "Name", "ID", "Email" are required (typically '
             '"Name" and "Email"). Optional columns: "Replacement Name", '
             '"Replacement ID", "Replacement Email".',
        required=True,
        type=Path,
    )
    parser.add_argument(
        '--report',
        help='Path of the CSV report to write.',
        default=f'./remove_speakers_report_{date.today().strftime("%Y-%m-%d")}.csv',
        type=Path,
    )
    parser.add_argument(
        '--personal-channels-title',
        help='Title of the top-level channel holding the personal channels. '
             'This channel is excluded from the faculty menu and its '
             'recordings are never modified.',
        default='Personal channels',
        type=str,
    )
    parser.add_argument(
        '--apply',
        help='Whether to apply changes or not. If not set, the script runs in '
             'dry-run mode: it only logs and writes the report.',
        action='store_true',
    )
    parser.add_argument(
        '--log-level',
        help='Log level.',
        default='info',
        choices=['critical', 'error', 'warn', 'info', 'debug'],
    )
    args = parser.parse_args(sys_args)

    logging.basicConfig()
    logger.setLevel(args.log_level.upper())

    rules = _load_rules(args.csv_file)
    logger.info(f'Loaded {len(rules)} speaker removal rule(s) from "{args.csv_file}".')

    msc = MediaServerClient(args.conf)
    msc.conf['TIMEOUT'] = max(600, msc.conf['TIMEOUT'])
    msc.check_server()

    logger.info('Fetching catalog to list faculties...')
    catalog = msc.get_catalog(fmt='flat')
    faculty_oids = _select_faculties(catalog['channels'], args.personal_channels_title)

    if args.apply:
        answer = input(
            'The script is running in apply mode. Speakers will be removed from '
            f'recordings on {msc.conf["SERVER_URL"]}.\nProceed ? [y / n] '
        )
        if answer.lower() not in ['yes', 'y']:
            sys.exit(0)
    else:
        logger.info('[Dry run] The script is running in dry-run mode. No changes will be applied.')

    _remove_speakers(
        msc,
        rules,
        faculty_oids=faculty_oids,
        report_path=args.report,
        apply=args.apply,
    )


if __name__ == '__main__':
    remove_speakers(sys.argv[1:])
