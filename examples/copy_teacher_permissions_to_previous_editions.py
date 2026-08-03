#!/usr/bin/env python3
'''
Copy teacher permissions from source course editions to other editions.

The expected channel hierarchy is:

  Level 1: faculty
  Level 2: course
  Level 3: course edition

A user is considered a teacher when all eight media permissions (access, add,
edit, publish, statistics, moderate, subtitle and delete) are enabled directly
on a source edition. Those eight permissions are then enabled for the same user
on each target edition of that course. Other permissions and users are left
unchanged.

The script starts with a menu for selecting one, several or all faculties. It
runs as a dry run by default; pass --apply to make changes. Edition titles are
compared using natural sorting by default, so for example "2026-10" is newer
than "2026-9". Without --source-edition, that most recent edition is the source
and all older editions are targets. Use --latest-by creation if edition
creation time should decide which edition is the most recent instead.
Alternatively, --source-edition can select all editions whose titles begin
with a specific four-digit year. Their combined teachers are copied to every
edition outside that source year, including newer editions. A CSV report is
written with one row per user/course combination that needed changes. In
dry-run mode it contains the number of editions to be updated; in apply mode it
contains the number successfully updated.
'''
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import csv
from dataclasses import dataclass, field
from datetime import datetime
import logging
import os
from pathlib import Path
import re
import sys
import threading

try:
    from ms_client.client import MediaServerClient
except ModuleNotFoundError:
    sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from ms_client.client import MediaServerClient


logger = logging.getLogger(__name__)

# These are the eight columns labelled Acc, Add, Edit, Pub, Sta, Mod, Sub and
# Del in the media permissions section of a channel.
TEACHER_PERMISSIONS = (
    'can_access_media',
    'can_add_media',
    'can_change_media',
    'can_publish_media',
    'can_see_stats_media',
    'can_moderate_media',
    'can_subtitle_media',
    'can_delete_media',
)

RECYCLE_BIN_OID = 'c00000000000000trash'
REPORT_COUNT_COLUMN = 'number of editions updated/to be updated'
REPORT_FIELDNAMES = ('faculty', 'course', 'user', REPORT_COUNT_COLUMN)
_thread_local = threading.local()


@dataclass
class UserCourseChange:
    faculty: str
    course: str
    course_oid: str
    user: str
    user_id: int | str
    editions: int


@dataclass
class CourseResult:
    faculty_title: str
    course_title: str
    latest_edition_title: str
    previous_editions: int
    source_users: int = 0
    teachers: int = 0
    already_correct: int = 0
    updates_needed: int = 0
    updated: int = 0
    changes: list[UserCourseChange] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _natural_sort_key(value: str) -> tuple:
    '''Return a case-insensitive key which compares digit runs numerically.'''
    return tuple(
        (1, int(part)) if part.isdigit() else (0, part.casefold())
        for part in re.split(r'(\d+)', value or '')
    )


def _parse_source_edition(value: str) -> str:
    if not re.fullmatch(r'[0-9]{4}', value):
        raise argparse.ArgumentTypeError('source edition must be exactly 4 numeric digits')
    return value


def _edition_sort_key(channel: dict, latest_by: str = 'title') -> tuple:
    title_key = _natural_sort_key(channel.get('title') or '')
    creation = channel.get('creation') or channel.get('add_date') or ''
    oid = channel.get('oid') or ''
    if latest_by == 'creation':
        return creation, title_key, oid
    return title_key, creation, oid


def _split_editions(editions: list[dict], latest_by: str = 'title') -> tuple[dict, list[dict]]:
    '''Return the most recent edition followed by all older editions.'''
    if not editions:
        raise ValueError('Cannot find a most recent edition in an empty list.')
    ordered = sorted(editions, key=lambda edition: _edition_sort_key(edition, latest_by))
    return ordered[-1], ordered[:-1]


def _split_source_and_target_editions(
    editions: list[dict],
    latest_by: str = 'title',
    source_edition: str | None = None,
) -> tuple[list[dict], list[dict]]:
    '''Return source editions and the editions which should receive permissions.'''
    if not source_edition:
        latest, previous = _split_editions(editions, latest_by)
        return [latest], previous

    source_pattern = re.compile(rf'^\s*{re.escape(source_edition)}(?![0-9])')
    ordered = sorted(editions, key=lambda edition: _edition_sort_key(edition, latest_by))
    sources = [
        edition for edition in ordered
        if source_pattern.search(edition.get('title') or '')
    ]
    source_oids = {edition['oid'] for edition in sources}
    targets = [edition for edition in ordered if edition['oid'] not in source_oids]
    return sources, targets


def _permission_is_enabled(value) -> bool:
    '''Handle both flat for-content values and nested perms/get values.'''
    if isinstance(value, dict):
        value = value.get('val')
    return value is True


def _has_teacher_permissions(permissions: dict) -> bool:
    return all(_permission_is_enabled(permissions.get(name)) for name in TEACHER_PERMISSIONS)


def _select_faculties(channels: list[dict]) -> set[str]:
    '''Show the faculty menu and return the selected top-level channel oids.'''
    by_oid = {channel['oid']: channel for channel in channels}
    faculties = sorted(
        (
            channel for channel in channels
            if (
                not channel.get('parent_oid')
                or channel.get('parent_oid') not in by_oid
            )
            and channel['oid'] != RECYCLE_BIN_OID
        ),
        key=lambda channel: (channel.get('title') or '').casefold(),
    )

    if not faculties:
        raise ValueError('No top-level faculty channels were found.')

    print('\nAvailable faculties:')
    print('  0. All faculties')
    for index, channel in enumerate(faculties, 1):
        print(f'  {index}. {channel.get("title") or channel["oid"]}')

    selection = input(
        '\nSelect faculties to process (0 for all, or comma/space separated numbers): '
    ).strip()

    if not selection or selection == '0':
        selected = faculties
    else:
        values = [value for value in re.split(r'[\s,]+', selection) if value]
        if not values or any(not value.isdigit() for value in values):
            raise ValueError(f'Invalid faculty selection: {selection!r}.')
        indices = [int(value) for value in values]
        invalid = [index for index in indices if not 1 <= index <= len(faculties)]
        if invalid:
            raise ValueError(f'Invalid faculty selection number(s): {invalid}.')
        selected = [faculties[index - 1] for index in dict.fromkeys(indices)]

    print(f'\nProcessing: {", ".join(channel.get("title") or channel["oid"] for channel in selected)}')
    return {channel['oid'] for channel in selected}


def _get_course_work(
    channels: list[dict],
    faculty_oids: set[str],
    source_edition: str | None = None,
) -> list[tuple[dict, dict, list[dict]]]:
    '''Return selected (faculty, course, editions) tuples with at least two editions.'''
    children_of: dict[str, list[dict]] = {}
    for channel in channels:
        parent_oid = channel.get('parent_oid')
        if parent_oid:
            children_of.setdefault(parent_oid, []).append(channel)

    by_oid = {channel['oid']: channel for channel in channels}
    work = []
    for faculty_oid in faculty_oids:
        faculty = by_oid.get(faculty_oid)
        if faculty is None:
            continue
        courses = children_of.get(faculty_oid, [])
        for course in courses:
            editions = children_of.get(course['oid'], [])
            if len(editions) < 2:
                continue
            if source_edition:
                sources, targets = _split_source_and_target_editions(
                    editions,
                    source_edition=source_edition,
                )
                if not sources or not targets:
                    continue
            work.append((faculty, course, editions))
    return sorted(
        work,
        key=lambda item: (
            (item[0].get('title') or '').casefold(),
            _natural_sort_key(item[1].get('title') or ''),
        ),
    )


def _get_thread_client(conf: str) -> MediaServerClient:
    '''Use one client per worker because requests.Session is not thread-safe.'''
    if getattr(_thread_local, 'conf', None) != conf:
        _thread_local.msc = MediaServerClient(conf, setup_logging=False)
        _thread_local.conf = conf
    return _thread_local.msc


def _user_label(user: dict) -> str:
    return user.get('email') or user.get('username') or f'user id {user.get("id")}'


def _process_course(
    msc: MediaServerClient,
    faculty: dict,
    course: dict,
    editions: list[dict],
    apply: bool = False,
    latest_by: str = 'title',
    source_edition: str | None = None,
) -> CourseResult:
    sources, targets = _split_source_and_target_editions(
        editions,
        latest_by=latest_by,
        source_edition=source_edition,
    )
    if source_edition:
        source_label = f'{source_edition} ({len(sources)} source edition(s))'
    else:
        source_label = sources[0].get('title') or sources[0]['oid']
    result = CourseResult(
        faculty_title=faculty.get('title') or faculty['oid'],
        course_title=course.get('title') or course['oid'],
        latest_edition_title=source_label,
        previous_editions=len(targets),
    )

    if not sources:
        return result

    users_by_id = {}
    teachers_by_id = {}
    for source in sources:
        try:
            response = msc.api(
                'perms/get/for-content/',
                params={'oid': source['oid'], 'users': 'yes'},
            )
        except Exception as exc:
            result.errors.append(
                f'Could not read users from source edition {source["oid"]}: {exc}'
            )
            continue
        for user in response.get('users', []):
            users_by_id[user.get('id')] = user
            if _has_teacher_permissions(user):
                teachers_by_id[user.get('id')] = user

    teachers = list(teachers_by_id.values())
    result.source_users = len(users_by_id)
    result.teachers = len(teachers)

    for teacher in teachers:
        teacher_updates_needed = 0
        teacher_updated = 0
        try:
            response = msc.api(
                'perms/get/',
                params={
                    'type': 'user',
                    'id': teacher['id'],
                    'oid': course['oid'],
                    'recursive': 'no',
                },
            )
        except Exception as exc:
            result.errors.append(
                f'Could not read existing permissions for {_user_label(teacher)}: {exc}'
            )
            continue

        permissions_by_oid = {
            channel['oid']: channel.get('permissions', {})
            for channel in response.get('channels', [])
        }
        for edition in targets:
            current = permissions_by_oid.get(edition['oid'], {})
            if _has_teacher_permissions(current):
                result.already_correct += 1
                continue

            result.updates_needed += 1
            teacher_updates_needed += 1
            if not apply:
                continue

            data = {
                'type': 'user',
                'id': teacher['id'],
                'oid': edition['oid'],
                **{permission: 'True' for permission in TEACHER_PERMISSIONS},
            }
            try:
                msc.api('perms/edit/', method='post', data=data)
            except Exception as exc:
                result.errors.append(
                    f'Could not update {_user_label(teacher)} on edition '
                    f'{edition.get("title") or edition["oid"]} [{edition["oid"]}]: {exc}'
                )
            else:
                result.updated += 1
                teacher_updated += 1

        if teacher_updates_needed:
            result.changes.append(UserCourseChange(
                faculty=result.faculty_title,
                course=result.course_title,
                course_oid=course['oid'],
                user=_user_label(teacher),
                user_id=teacher['id'],
                editions=teacher_updated if apply else teacher_updates_needed,
            ))

    return result


def _process_course_from_conf(
    conf: str,
    faculty: dict,
    course: dict,
    editions: list[dict],
    apply: bool,
    latest_by: str,
    source_edition: str | None,
) -> CourseResult:
    return _process_course(
        _get_thread_client(conf),
        faculty,
        course,
        editions,
        apply=apply,
        latest_by=latest_by,
        source_edition=source_edition,
    )


def _write_report(results: list[CourseResult], report_path: Path) -> int:
    '''Write one aggregate row per unique user/course pair.'''
    rows_by_user_course = {}
    for result in results:
        for change in result.changes:
            key = (change.course_oid, change.user_id)
            if key not in rows_by_user_course:
                rows_by_user_course[key] = {
                    'faculty': change.faculty,
                    'course': change.course,
                    'user': change.user,
                    REPORT_COUNT_COLUMN: 0,
                }
            rows_by_user_course[key][REPORT_COUNT_COLUMN] += change.editions

    rows = sorted(
        rows_by_user_course.values(),
        key=lambda row: (
            row['faculty'].casefold(),
            _natural_sort_key(row['course']),
            row['user'].casefold(),
        ),
    )
    with report_path.open('w', newline='', encoding='utf-8') as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=REPORT_FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)
    logger.info(f'CSV report written to "{report_path}" ({len(rows)} row(s)).')
    return len(rows)


def copy_teacher_permissions(sys_args: list[str]) -> int:
    parser = argparse.ArgumentParser(
        'copy_teacher_permissions_to_previous_editions',
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
        '--apply',
        help='Apply changes. Without this flag the script runs as a dry run.',
        action='store_true',
    )
    parser.add_argument(
        '--report',
        help='Path of the CSV report to write (default: %(default)s).',
        default=Path(
            f'copy_teacher_permissions_report_{datetime.now().strftime("%Y%m%d_%H%M%S")}.csv'
        ),
        type=Path,
    )
    parser.add_argument(
        '--latest-by',
        help='How to identify the most recent edition when --source-edition is '
             'omitted (default: %(default)s).',
        choices=['title', 'creation'],
        default='title',
    )
    parser.add_argument(
        '--source-edition',
        help='Use all editions whose titles begin with this four-digit year as '
             'permission sources, and copy to every edition outside that year.',
        type=_parse_source_edition,
    )
    parser.add_argument(
        '--workers',
        help='Number of parallel API workers (default: %(default)s).',
        default=5,
        type=int,
    )
    parser.add_argument(
        '--log-level',
        help='Log level (default: %(default)s).',
        default='info',
        choices=['critical', 'error', 'warn', 'info', 'debug'],
    )
    args = parser.parse_args(sys_args)

    if args.workers < 1:
        parser.error('--workers must be at least 1.')

    logging.basicConfig(
        format='%(asctime)s %(levelname)s %(message)s',
        level=getattr(logging, args.log_level.upper()),
    )

    msc = MediaServerClient(args.conf, setup_logging=False)
    msc.check_server()
    logger.info('Fetching the channel catalog to list faculties...')
    catalog = msc.get_catalog(fmt='flat')
    channels = catalog.get('channels', [])

    try:
        faculty_oids = _select_faculties(channels)
    except ValueError as exc:
        parser.error(str(exc))

    work = _get_course_work(channels, faculty_oids, source_edition=args.source_edition)
    if not work:
        if args.source_edition:
            logger.info(
                f'No selected courses have both a {args.source_edition} source edition '
                'and another target edition. Nothing to do.'
            )
        else:
            logger.info('No selected courses have at least two editions. Nothing to do.')
        _write_report([], args.report)
        return 0

    if args.source_edition:
        logger.info(
            f'Found {len(work)} course(s) with {args.source_edition} source editions '
            'and at least one edition outside that year.'
        )
    else:
        logger.info(
            f'Found {len(work)} course(s) with at least two editions. '
            f'The most recent edition will be selected by {args.latest_by}.'
        )
    if args.apply:
        answer = input(
            'The script is running in apply mode. Teacher permissions will be copied '
            f'to target editions on {msc.conf["SERVER_URL"]}.\nProceed ? [y / n] '
        )
        if answer.lower() not in ('yes', 'y'):
            return 0
    else:
        logger.info('[Dry run] No permissions will be changed.')

    results = []
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(
                _process_course_from_conf,
                args.conf,
                faculty,
                course,
                editions,
                args.apply,
                args.latest_by,
                args.source_edition,
            ): course
            for faculty, course, editions in work
        }
        for completed, future in enumerate(as_completed(futures), 1):
            course = futures[future]
            try:
                result = future.result()
            except Exception as exc:
                result = CourseResult(
                    faculty_title='',
                    course_title=course.get('title') or course['oid'],
                    latest_edition_title='',
                    previous_editions=0,
                    errors=[f'Unexpected processing error: {exc}'],
                )
            results.append(result)

            action = (
                f'{result.updated}/{result.updates_needed} update(s) applied'
                if args.apply
                else f'{result.updates_needed} update(s) needed'
            )
            message = (
                f'[{completed}/{len(work)}] {result.faculty_title} / {result.course_title}: '
                f'source "{result.latest_edition_title}", {result.teachers} teacher(s), '
                f'{action}, {result.already_correct} already correct'
            )
            if result.teachers or result.errors:
                logger.info(message)
            else:
                logger.debug(message)
            for error in result.errors:
                logger.error(f'{result.course_title}: {error}')

    total_teachers = sum(result.teachers for result in results)
    total_correct = sum(result.already_correct for result in results)
    total_needed = sum(result.updates_needed for result in results)
    total_updated = sum(result.updated for result in results)
    total_errors = sum(len(result.errors) for result in results)

    if args.apply:
        change_summary = f'{total_updated}/{total_needed} permission assignment(s) updated'
    else:
        change_summary = f'{total_needed} permission assignment(s) would be updated'
    logger.info(
        f'Finished: {len(results)} course(s), {total_teachers} teacher assignment(s) '
        f'found in source editions, {change_summary}, {total_correct} already correct, '
        f'{total_errors} error(s).'
    )
    _write_report(results, args.report)
    return 1 if total_errors else 0


if __name__ == '__main__':
    sys.exit(copy_teacher_permissions(sys.argv[1:]))
