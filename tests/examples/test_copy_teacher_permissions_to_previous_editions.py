import argparse
import csv
from unittest import mock

import pytest

import examples.copy_teacher_permissions_to_previous_editions as script


def _teacher(user_id=42):
    return {
        'id': user_id,
        'email': 'teacher@example.com',
        **{permission: True for permission in script.TEACHER_PERMISSIONS},
    }


def _nested_permissions(enabled=True):
    return {
        permission: {'val': enabled}
        for permission in script.TEACHER_PERMISSIONS
    }


def test_teacher_requires_all_eight_permissions():
    teacher = _teacher()
    assert script._has_teacher_permissions(teacher) is True

    for permission in script.TEACHER_PERMISSIONS:
        incomplete = dict(teacher)
        incomplete[permission] = False
        assert script._has_teacher_permissions(incomplete) is False


def test_latest_edition_uses_natural_title_sorting_and_can_use_creation_time():
    editions = [
        {'oid': 'edition-9', 'title': '2026-9', 'creation': '2026-03-01 00:00:00'},
        {'oid': 'edition-10', 'title': '2026-10', 'creation': '2026-02-01 00:00:00'},
    ]

    latest_by_title, previous_by_title = script._split_editions(editions)
    latest_by_creation, previous_by_creation = script._split_editions(
        editions,
        latest_by='creation',
    )

    assert latest_by_title['oid'] == 'edition-10'
    assert [edition['oid'] for edition in previous_by_title] == ['edition-9']
    assert latest_by_creation['oid'] == 'edition-9'
    assert [edition['oid'] for edition in previous_by_creation] == ['edition-10']


@pytest.mark.parametrize('value', ['0000', '2025', '9999'])
def test_source_edition_accepts_exactly_four_numeric_digits(value):
    assert script._parse_source_edition(value) == value


@pytest.mark.parametrize('value', ['', '202', '20255', '20a5', '２０２５'])
def test_source_edition_rejects_other_values(value):
    with pytest.raises(argparse.ArgumentTypeError):
        script._parse_source_edition(value)


def test_source_edition_targets_every_edition_including_source_year():
    editions = [
        {'oid': 'edition-2024', 'title': '2024-1'},
        {'oid': 'edition-2025-a', 'title': '2025-1'},
        {'oid': 'edition-2025-b', 'title': '2025-2'},
        {'oid': 'edition-2026', 'title': '2026-1'},
        {'oid': 'not-2025', 'title': 'COURSE2025'},
    ]

    sources, targets = script._split_source_and_target_editions(
        editions,
        source_edition='2025',
    )

    assert [edition['oid'] for edition in sources] == ['edition-2025-a', 'edition-2025-b']
    assert {edition['oid'] for edition in targets} == {
        'edition-2024',
        'edition-2025-a',
        'edition-2025-b',
        'edition-2026',
        'not-2025',
    }


def test_course_code_comes_from_year_prefixed_edition_title_with_fallback():
    course = {'oid': 'course', 'title': 'LEVELCODE Course name, with comma'}

    assert script._get_course_code(
        course,
        [{'oid': 'edition', 'title': '2026-1-V EDITIONCODE'}],
    ) == 'EDITIONCODE'
    assert script._get_course_code(
        course,
        [{'oid': 'edition', 'title': 'Edition without a year'}],
    ) == 'LEVELCODE'


@pytest.mark.parametrize('apply', [False, True], ids=['dry-run', 'apply'])
def test_process_course_copies_only_missing_teacher_permissions(apply):
    client = mock.Mock()
    teacher = _teacher()
    non_teacher = _teacher(user_id=99)
    non_teacher['can_delete_media'] = False
    source_response = {'users': [teacher, non_teacher]}
    existing_response = {
        'channels': [
            {
                'oid': 'edition-correct',
                'permissions': _nested_permissions(),
            },
            {
                'oid': 'edition-partial',
                'permissions': {
                    **_nested_permissions(),
                    'can_delete_media': {},
                },
            },
            {
                'oid': 'edition-latest',
                'permissions': _nested_permissions(),
            },
        ],
    }
    client.api.side_effect = [source_response, existing_response, {'success': True}]

    result = script._process_course(
        client,
        faculty={'oid': 'faculty', 'title': 'Faculty'},
        course={'oid': 'course', 'title': 'COURSE Course title'},
        editions=[
            {'oid': 'edition-correct', 'title': '2024-1'},
            {'oid': 'edition-partial', 'title': '2025-1'},
            {'oid': 'edition-latest', 'title': '2026-1'},
        ],
        apply=apply,
    )

    assert result.previous_editions == 2
    assert result.source_users == 2
    assert result.teachers == 1
    assert result.already_correct == 1
    assert result.updates_needed == 1
    assert result.updated == int(apply)
    assert result.errors == []
    assert len(result.changes) == 1
    assert result.changes[0] == script.UserCourseChange(
        faculty='Faculty',
        course='COURSE',
        course_oid='course',
        user='teacher@example.com',
        user_id=42,
        editions=1,
    )

    expected_calls = [
        mock.call(
            'perms/get/for-content/',
            params={'oid': 'edition-latest', 'users': 'yes'},
        ),
        mock.call(
            'perms/get/',
            params={
                'type': 'user',
                'id': 42,
                'oid': 'course',
                'recursive': 'no',
            },
        ),
    ]
    if apply:
        expected_calls.append(mock.call(
            'perms/edit/',
            method='post',
            data={
                'type': 'user',
                'id': 42,
                'oid': 'edition-partial',
                **{permission: 'True' for permission in script.TEACHER_PERMISSIONS},
            },
        ))
    assert client.api.call_args_list == expected_calls


def test_process_course_records_read_error_and_does_not_write():
    client = mock.Mock()
    client.api.side_effect = [
        {'users': [_teacher()]},
        RuntimeError('permission lookup failed'),
    ]

    result = script._process_course(
        client,
        faculty={'oid': 'faculty', 'title': 'Faculty'},
        course={'oid': 'course', 'title': 'Course'},
        editions=[
            {'oid': 'edition-old', 'title': '2025'},
            {'oid': 'edition-new', 'title': '2026'},
        ],
        apply=True,
    )

    assert result.teachers == 1
    assert result.updates_needed == 0
    assert result.updated == 0
    assert len(result.errors) == 1
    assert 'permission lookup failed' in result.errors[0]
    assert client.api.call_count == 2


def test_apply_report_count_includes_only_successful_updates():
    client = mock.Mock()
    client.api.side_effect = [
        {'users': [_teacher()]},
        {'channels': []},
        {'success': True},
        RuntimeError('update failed'),
    ]

    result = script._process_course(
        client,
        faculty={'oid': 'faculty', 'title': 'Faculty'},
        course={'oid': 'course', 'title': 'Course'},
        editions=[
            {'oid': 'edition-2024', 'title': '2024'},
            {'oid': 'edition-2025', 'title': '2025'},
            {'oid': 'edition-2026', 'title': '2026'},
        ],
        apply=True,
    )

    assert result.updates_needed == 2
    assert result.updated == 1
    assert len(result.errors) == 1
    assert len(result.changes) == 1
    assert result.changes[0].editions == 1


def test_source_edition_unions_teachers_and_copies_to_source_year_and_other_years():
    client = mock.Mock()
    teacher_1 = _teacher(user_id=41)
    teacher_1['email'] = 'teacher1@example.com'
    teacher_2 = _teacher(user_id=42)
    teacher_2['email'] = 'teacher2@example.com'
    client.api.side_effect = [
        {'users': [teacher_1]},
        {'users': [teacher_1, teacher_2]},
        {
            'channels': [
                {'oid': 'edition-2024', 'permissions': _nested_permissions()},
                {'oid': 'edition-2025-a', 'permissions': _nested_permissions()},
                {'oid': 'edition-2025-b', 'permissions': _nested_permissions()},
                {'oid': 'edition-2026', 'permissions': {}},
            ],
        },
        {
            'channels': [
                {'oid': 'edition-2025-b', 'permissions': _nested_permissions()},
            ],
        },
    ]

    result = script._process_course(
        client,
        faculty={'oid': 'faculty', 'title': 'Faculty'},
        course={'oid': 'course', 'title': 'Course'},
        editions=[
            {'oid': 'edition-2024', 'title': '2024-1'},
            {'oid': 'edition-2025-a', 'title': '2025-1'},
            {'oid': 'edition-2025-b', 'title': '2025-2'},
            {'oid': 'edition-2026', 'title': '2026-1'},
        ],
        source_edition='2025',
    )

    assert result.latest_edition_title == '2025 (2 source edition(s))'
    assert result.previous_editions == 4
    assert result.source_users == 2
    assert result.teachers == 2
    assert result.already_correct == 4
    assert result.updates_needed == 4
    assert result.updated == 0
    assert [(change.user_id, change.editions) for change in result.changes] == [
        (41, 1),
        (42, 3),
    ]
    assert client.api.call_args_list[:2] == [
        mock.call(
            'perms/get/for-content/',
            params={'oid': 'edition-2025-a', 'users': 'yes'},
        ),
        mock.call(
            'perms/get/for-content/',
            params={'oid': 'edition-2025-b', 'users': 'yes'},
        ),
    ]


def test_apply_updates_another_edition_from_the_explicit_source_year():
    client = mock.Mock()
    teacher = _teacher()
    client.api.side_effect = [
        {'users': [teacher]},
        {'users': []},
        {
            'channels': [
                {'oid': 'semester-1', 'permissions': _nested_permissions()},
                {'oid': 'semester-2', 'permissions': {}},
            ],
        },
        {'success': True},
    ]

    result = script._process_course(
        client,
        faculty={'oid': 'faculty', 'title': 'Faculty'},
        course={'oid': 'course', 'title': 'COURSE Course'},
        editions=[
            {'oid': 'semester-1', 'title': '2026-SEM1-V COURSE'},
            {'oid': 'semester-2', 'title': '2026-SEM2-V COURSE'},
        ],
        apply=True,
        source_edition='2026',
    )

    assert result.already_correct == 1
    assert result.updates_needed == 1
    assert result.updated == 1
    assert client.api.call_args_list[-1] == mock.call(
        'perms/edit/',
        method='post',
        data={
            'type': 'user',
            'id': 42,
            'oid': 'semester-2',
            **{permission: 'True' for permission in script.TEACHER_PERMISSIONS},
        },
    )


def test_write_report_aggregates_unique_user_course_rows(tmp_path):
    result_1 = script.CourseResult('Faculty', 'Course', '2026', 2)
    result_1.changes = [
        script.UserCourseChange('Faculty', 'Course', 'course-1', 'b@example.com', 2, 1),
        script.UserCourseChange('Faculty', 'Course', 'course-1', 'a@example.com', 1, 2),
    ]
    result_2 = script.CourseResult('Faculty', 'Course', '2026', 2)
    result_2.changes = [
        script.UserCourseChange('Faculty', 'Course', 'course-1', 'a@example.com', 1, 3),
    ]
    report_path = tmp_path / 'report.csv'

    row_count = script._write_report([result_1, result_2], report_path)

    with report_path.open(newline='', encoding='utf-8') as csvfile:
        reader = csv.DictReader(csvfile)
        rows = list(reader)
    assert row_count == 2
    assert reader.fieldnames == list(script.REPORT_FIELDNAMES)
    assert rows == [
        {
            'faculty': 'Faculty',
            'course': 'Course',
            'user': 'a@example.com',
            script.REPORT_COUNT_COLUMN: '5',
            script.REPORT_COMMENT_COLUMN: '',
        },
        {
            'faculty': 'Faculty',
            'course': 'Course',
            'user': 'b@example.com',
            script.REPORT_COUNT_COLUMN: '1',
            script.REPORT_COMMENT_COLUMN: '',
        },
    ]


def test_write_report_includes_courses_without_teacher_matches(tmp_path):
    no_match = script.CourseResult(
        'Faculty',
        'Course without teachers',
        '2026',
        2,
        course_oid='course-no-match',
        course_code='NO-MATCH-CODE',
        source_users=3,
    )
    source_error = script.CourseResult(
        'Faculty',
        'Course with source error',
        '2026',
        2,
        course_oid='course-source-error',
        course_code='SOURCE-ERROR-CODE',
        errors=['source lookup failed'],
    )
    report_path = tmp_path / 'report.csv'

    row_count = script._write_report([no_match, source_error], report_path)

    with report_path.open(newline='', encoding='utf-8') as csvfile:
        rows = list(csv.DictReader(csvfile))
    assert row_count == 2
    assert rows == [
        {
            'faculty': 'Faculty',
            'course': 'NO-MATCH-CODE',
            'user': '',
            script.REPORT_COUNT_COLUMN: '0',
            script.REPORT_COMMENT_COLUMN: 'No match',
        },
        {
            'faculty': 'Faculty',
            'course': 'SOURCE-ERROR-CODE',
            'user': '',
            script.REPORT_COUNT_COLUMN: '0',
            script.REPORT_COMMENT_COLUMN: script.SOURCE_READ_ERROR_COMMENT,
        },
    ]


def test_get_course_work_uses_exactly_three_channel_levels():
    channels = [
        {'oid': 'faculty', 'title': 'Faculty'},
        {'oid': 'course', 'parent_oid': 'faculty', 'title': 'Course'},
        {'oid': 'edition-1', 'parent_oid': 'course', 'title': '2025'},
        {'oid': 'edition-2', 'parent_oid': 'course', 'title': '2026'},
        {'oid': 'level-4', 'parent_oid': 'edition-2', 'title': 'Not an edition'},
        {'oid': 'other-faculty', 'title': 'Other Faculty'},
        {'oid': 'other-course', 'parent_oid': 'other-faculty', 'title': 'Other Course'},
        {'oid': 'other-edition-1', 'parent_oid': 'other-course', 'title': '2025'},
        {'oid': 'other-edition-2', 'parent_oid': 'other-course', 'title': '2026'},
    ]

    work = script._get_course_work(channels, {'faculty'})

    assert len(work) == 1
    faculty, course, editions = work[0]
    assert faculty['oid'] == 'faculty'
    assert course['oid'] == 'course'
    assert {edition['oid'] for edition in editions} == {'edition-1', 'edition-2'}
    assert len(script._get_course_work(channels, {'faculty'}, source_edition='2025')) == 1
    assert script._get_course_work(channels, {'faculty'}, source_edition='2024') == []


def test_course_with_only_same_year_editions_is_processed_for_explicit_source_year():
    channels = [
        {'oid': 'faculty', 'title': 'Faculty'},
        {'oid': 'course', 'parent_oid': 'faculty', 'title': 'Course'},
        {'oid': 'semester-1', 'parent_oid': 'course', 'title': '2026-SEM1-V COURSE'},
        {'oid': 'semester-2', 'parent_oid': 'course', 'title': '2026-SEM2-V COURSE'},
    ]

    work = script._get_course_work(channels, {'faculty'}, source_edition='2026')

    assert len(work) == 1
    assert work[0][1]['oid'] == 'course'


def test_faculty_menu_supports_multiple_selections_and_hides_recycle_bin(monkeypatch, capsys):
    channels = [
        {'oid': 'faculty-b', 'title': 'Faculty B'},
        {'oid': script.RECYCLE_BIN_OID, 'title': 'Recycle bin'},
        {'oid': 'faculty-a', 'title': 'Faculty A'},
        {'oid': 'course', 'parent_oid': 'faculty-a', 'title': 'Course'},
    ]
    monkeypatch.setattr('builtins.input', lambda _prompt: '2, 1')

    selected = script._select_faculties(channels)

    assert selected == {'faculty-a', 'faculty-b'}
    output = capsys.readouterr().out
    assert '1. Faculty A' in output
    assert '2. Faculty B' in output
    assert 'Recycle bin' not in output
