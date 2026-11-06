"""UI CI success/failure counts and a narrow, payload-free artifact contract."""
import json
import xml.etree.ElementTree as ET

import pytest

from scripts.ci import report_ui_smoke as reports
from scripts.ci import run_ui_smoke as runner


def source_report(path, code=0, outcomes=(None, None), *, timed_out=False):
    path.mkdir(parents=True)
    root = ET.Element('testsuites')
    suite = ET.SubElement(root, 'testsuite', tests=str(len(outcomes)), hostname='PRIVATE_HOST')
    props = ET.SubElement(suite, 'properties')
    ET.SubElement(props, 'property', name='private-config', value='PRIVATE_PAYLOAD')
    for n, outcome in enumerate(outcomes):
        case = ET.SubElement(suite, 'testcase', name=f'test_{n}', classname='fixture')
        ET.SubElement(case, 'system-out').text = 'PRIVATE_PAYLOAD'
        if outcome:
            ET.SubElement(case, outcome, message='PRIVATE_PAYLOAD').text = 'PRIVATE_PAYLOAD'
    ET.ElementTree(root).write(path / 'junit.xml', encoding='utf8')
    (path / 'result.json').write_text(json.dumps(dict(
        returncode=code, process_returncode=code, timed_out=timed_out,
        private_config='PRIVATE_PAYLOAD',
    )), 'utf8')
    (path / 'pytest.log').write_text('PRIVATE_PAYLOAD\n2 passed, 1 warning in 0.03s\n', 'utf8')


@pytest.mark.parametrize('code,outcomes,expected', [
    (0, (None, None), ('passed', 2, 0, 0, 0)),
    (1, (None, 'failure'), ('failed', 1, 1, 0, 0)),
    (1, ('error', 'skipped'), ('failed', 0, 0, 1, 1)),
    (0, ('skipped',), ('passed', 0, 0, 0, 1)),
    (0xC0000374, ('error',), ('failed', 0, 0, 1, 0)),
    (0xC0000005, (None,), ('failed', 1, 0, 0, 0)),
    (0, ('failure',), ('failed', 0, 1, 0, 0)),
])
def test_export_counts_preserve_junit_and_process_outcomes(tmp_path, code, outcomes, expected):
    source, target = tmp_path / 'raw', tmp_path / 'public'
    source_report(source, code, outcomes)
    result = reports.publish_group(source, target, 'theme')
    assert (result['status'], *(result['counts'][key] for key in
            ('passed', 'failed', 'errors', 'skipped'))) == expected
    assert result['returncode'] == code and result['process_returncode'] == code
    assert result['counts']['tests'] == len(outcomes)
    assert result['warnings'] == 1
    assert len(list(ET.parse(target / 'junit.xml').iter('testcase'))) == len(outcomes)
    assert {p.name for p in target.iterdir()} == {'result.json', 'summary.log', 'junit.xml'}
    assert not any('PRIVATE_' in p.read_text('utf8') for p in target.iterdir())
    assert 'PRIVATE_PAYLOAD' in (source / 'pytest.log').read_text('utf8')


def test_timeout_and_native_returncode_remain_visible(tmp_path):
    source_report(tmp_path / 'raw', 124, ('error',), timed_out=True)
    result = reports.publish_group(tmp_path / 'raw', tmp_path / 'public', 'queue')
    assert result['status'] == 'failed' and result['timed_out'] is True
    assert result['returncode'] == 124


@pytest.mark.parametrize('junit', ['<invalid', '<html/>', '<testsuite tests="2"/>'])
def test_invalid_or_incomplete_junit_is_not_success(tmp_path, junit):
    source_report(tmp_path / 'raw')
    (tmp_path / 'raw/junit.xml').write_text(junit, 'utf8')
    result = reports.publish_group(tmp_path / 'raw', tmp_path / 'public', 'queue')
    assert result['status'] == 'report_error'
    assert result['returncode'] == 0 and result['counts'] is None
    assert ET.parse(tmp_path / 'public/junit.xml').getroot().attrib['errors'] == '1'


def test_missing_junit_with_recorded_exit_is_report_error(tmp_path):
    source_report(tmp_path / 'raw')
    (tmp_path / 'raw/junit.xml').unlink()
    result = reports.publish_group(tmp_path / 'raw', tmp_path / 'public', 'queue')
    assert result['status'] == 'report_error'


def test_unrun_groups_are_not_counted_as_passed(tmp_path):
    summary, code = reports.publish_reports(tmp_path / 'missing', tmp_path / 'public')
    assert code == 0 and summary.count('| not_run |') == len(reports.GROUPS)
    for group in reports.GROUPS:
        row = json.loads((tmp_path / 'public' / group / 'result.json').read_text('utf8'))
        assert row['counts'] is None and row['returncode'] is None


def test_harness_without_exit_status_is_reported_not_process_success(tmp_path):
    source_report(tmp_path / 'raw')
    (tmp_path / 'raw/result.json').unlink()
    result = reports.publish_group(tmp_path / 'raw', tmp_path / 'public', 'harness')
    assert result['status'] == 'reported' and result['counts']['passed'] == 2
    assert result['returncode'] is None


def test_cli_always_writes_summary_and_only_allowlisted_reports(tmp_path, monkeypatch):
    source, target = tmp_path / 'raw', tmp_path / 'public'
    for group in reports.GROUPS:
        source_report(source / group, outcomes=(None, 'skipped'))
    (source / 'private.dmp').write_bytes(b'PRIVATE_PAYLOAD')
    (source / 'voice.wav').write_bytes(b'PRIVATE_PAYLOAD')
    destination = tmp_path / 'step-summary.md'
    monkeypatch.setenv('GITHUB_STEP_SUMMARY', str(destination))
    assert reports.main(['--input-dir', str(source), '--output-dir', str(target)]) == 0
    text = destination.read_text('utf8')
    assert text == (target / 'summary.md').read_text('utf8')
    assert '| theme | passed | 1 | 0 | 0 | 1 | 1 | 0 | False |' in text
    assert {p.name for p in target.rglob('*') if p.is_file()} == {
        'summary.md', 'summary.log', 'junit.xml', 'result.json',
    }
    assert not any('PRIVATE_' in p.read_text('utf8') for p in target.rglob('*') if p.is_file())
    assert reports.GROUPS == ('harness', *runner.GROUPS)


def test_cli_retains_failure_summary_and_nonzero_report_error(tmp_path, monkeypatch):
    source_report(tmp_path / 'raw/theme')
    (tmp_path / 'raw/theme/junit.xml').write_text('<bad', 'utf8')
    monkeypatch.setenv('GITHUB_STEP_SUMMARY', str(tmp_path / 'summary.md'))
    assert reports.main(['--input-dir', str(tmp_path / 'raw'),
                         '--output-dir', str(tmp_path / 'public')]) == 1
    assert '| theme | report_error |' in (tmp_path / 'summary.md').read_text('utf8')


@pytest.mark.parametrize('child', [False, True])
def test_cli_rejects_overlapping_directories(tmp_path, child):
    target = tmp_path / 'child' if child else tmp_path
    with pytest.raises(SystemExit) as error:
        reports.main(['--input-dir', str(tmp_path), '--output-dir', str(target)])
    assert error.value.code == 2


def test_existing_export_cannot_be_overwritten(tmp_path):
    (tmp_path / 'summary.md').write_text('first failure', 'utf8')
    with pytest.raises(FileExistsError):
        reports.publish_reports(tmp_path / 'input', tmp_path)
    assert (tmp_path / 'summary.md').read_text('utf8') == 'first failure'


def test_no_warning_summary_is_unknown_instead_of_guessing_zero(tmp_path):
    log = tmp_path / 'pytest.log'
    assert reports.warning_count(log) is None
    log.write_text('process aborted before pytest summary', 'utf8')
    assert reports.warning_count(log) is None
    log.write_text('== 15 passed in 0.12s ==\n', 'utf8')
    assert reports.warning_count(log) == 0
