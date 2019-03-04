import os
import re
import subprocess
import sys
import time
import shutil

from typing import List, Iterable, Dict, Tuple, Callable, Any, Optional

from mypy import defaults
from mypy.test.config import test_temp_dir
import mypy.api as api

import pytest  # type: ignore  # no pytest in typeshed

# Exporting Suite as alias to TestCase for backwards compatibility
# TODO: avoid aliasing - import and subclass TestCase directly
from unittest import TestCase as Suite

from mypy.main import process_options
from mypy.options import Options
from mypy.test.data import DataDrivenTestCase

skip = pytest.mark.skip

# AssertStringArraysEqual displays special line alignment helper messages if
# the first different line has at least this many characters,
MIN_LINE_LENGTH_FOR_ALIGNMENT = 5


def run_mypy(args: List[str]) -> None:
    __tracebackhide__ = True
    outval, errval, status = api.run(args + ['--show-traceback',
                                             '--no-site-packages',
                                             '--no-silence-site-packages'])
    if status != 0:
        sys.stdout.write(outval)
        sys.stderr.write(errval)
        pytest.fail(msg="Sample check failed", pytrace=False)


def assert_string_arrays_equal(expected: List[str], actual: List[str],
                               msg: str) -> None:
    """Assert that two string arrays are equal.

    Display any differences in a human-readable form.
    """

    actual = clean_up(actual)

    if actual != expected:
        num_skip_start = num_skipped_prefix_lines(expected, actual)
        num_skip_end = num_skipped_suffix_lines(expected, actual)

        sys.stderr.write('Expected:\n')

        # If omit some lines at the beginning, indicate it by displaying a line
        # with '...'.
        if num_skip_start > 0:
            sys.stderr.write('  ...\n')

        # Keep track of the first different line.
        first_diff = -1

        # Display only this many first characters of identical lines.
        width = 75

        for i in range(num_skip_start, len(expected) - num_skip_end):
            if i >= len(actual) or expected[i] != actual[i]:
                if first_diff < 0:
                    first_diff = i
                sys.stderr.write('  {:<45} (diff)'.format(expected[i]))
            else:
                e = expected[i]
                sys.stderr.write('  ' + e[:width])
                if len(e) > width:
                    sys.stderr.write('...')
            sys.stderr.write('\n')
        if num_skip_end > 0:
            sys.stderr.write('  ...\n')

        sys.stderr.write('Actual:\n')

        if num_skip_start > 0:
            sys.stderr.write('  ...\n')

        for j in range(num_skip_start, len(actual) - num_skip_end):
            if j >= len(expected) or expected[j] != actual[j]:
                sys.stderr.write('  {:<45} (diff)'.format(actual[j]))
            else:
                a = actual[j]
                sys.stderr.write('  ' + a[:width])
                if len(a) > width:
                    sys.stderr.write('...')
            sys.stderr.write('\n')
        if actual == []:
            sys.stderr.write('  (empty)\n')
        if num_skip_end > 0:
            sys.stderr.write('  ...\n')

        sys.stderr.write('\n')

        if first_diff >= 0 and first_diff < len(actual) and (
                len(expected[first_diff]) >= MIN_LINE_LENGTH_FOR_ALIGNMENT
                or len(actual[first_diff]) >= MIN_LINE_LENGTH_FOR_ALIGNMENT):
            # Display message that helps visualize the differences between two
            # long lines.
            show_align_message(expected[first_diff], actual[first_diff])

        raise AssertionError(msg)


def assert_module_equivalence(name: str,
                              expected: Optional[Iterable[str]], actual: Iterable[str]) -> None:
    if expected is not None:
        expected_normalized = sorted(expected)
        actual_normalized = sorted(set(actual).difference({"__main__"}))
        assert_string_arrays_equal(
            expected_normalized,
            actual_normalized,
            ('Actual modules ({}) do not match expected modules ({}) '
             'for "[{} ...]"').format(
                 ', '.join(actual_normalized),
                 ', '.join(expected_normalized),
                 name))


def assert_target_equivalence(name: str,
                              expected: Optional[List[str]], actual: List[str]) -> None:
    """Compare actual and expected targets (order sensitive)."""
    if expected is not None:
        assert_string_arrays_equal(
            expected,
            actual,
            ('Actual targets ({}) do not match expected targets ({}) '
             'for "[{} ...]"').format(
                 ', '.join(actual),
                 ', '.join(expected),
                 name))


def update_testcase_output(testcase: DataDrivenTestCase, output: List[str]) -> None:
    assert testcase.old_cwd is not None, "test was not properly set up"
    testcase_path = os.path.join(testcase.old_cwd, testcase.file)
    with open(testcase_path, encoding='utf8') as f:
        data_lines = f.read().splitlines()
    test = '\n'.join(data_lines[testcase.line:testcase.lastline])

    mapping = {}  # type: Dict[str, List[str]]
    for old, new in zip(testcase.output, output):
        PREFIX = 'error:'
        ind = old.find(PREFIX)
        if ind != -1 and old[:ind] == new[:ind]:
            old, new = old[ind + len(PREFIX):], new[ind + len(PREFIX):]
        mapping.setdefault(old, []).append(new)

    for old in mapping:
        if test.count(old) == len(mapping[old]):
            betweens = test.split(old)

            # Interleave betweens and mapping[old]
            from itertools import chain
            interleaved = [betweens[0]] + \
                list(chain.from_iterable(zip(mapping[old], betweens[1:])))
            test = ''.join(interleaved)

    data_lines[testcase.line:testcase.lastline] = [test]
    data = '\n'.join(data_lines)
    with open(testcase_path, 'w', encoding='utf8') as f:
        print(data, file=f)


def show_align_message(s1: str, s2: str) -> None:
    """Align s1 and s2 so that the their first difference is highlighted.

    For example, if s1 is 'foobar' and s2 is 'fobar', display the
    following lines:

      E: foobar
      A: fobar
           ^

    If s1 and s2 are long, only display a fragment of the strings around the
    first difference. If s1 is very short, do nothing.
    """

    # Seeing what went wrong is trivial even without alignment if the expected
    # string is very short. In this case do nothing to simplify output.
    if len(s1) < 4:
        return

    maxw = 72  # Maximum number of characters shown

    sys.stderr.write('Alignment of first line difference:\n')

    trunc = False
    while s1[:30] == s2[:30]:
        s1 = s1[10:]
        s2 = s2[10:]
        trunc = True

    if trunc:
        s1 = '...' + s1
        s2 = '...' + s2

    max_len = max(len(s1), len(s2))
    extra = ''
    if max_len > maxw:
        extra = '...'

    # Write a chunk of both lines, aligned.
    sys.stderr.write('  E: {}{}\n'.format(s1[:maxw], extra))
    sys.stderr.write('  A: {}{}\n'.format(s2[:maxw], extra))
    # Write an indicator character under the different columns.
    sys.stderr.write('     ')
    for j in range(min(maxw, max(len(s1), len(s2)))):
        if s1[j:j + 1] != s2[j:j + 1]:
            sys.stderr.write('^')  # Difference
            break
        else:
            sys.stderr.write(' ')  # Equal
    sys.stderr.write('\n')


def clean_up(a: List[str]) -> List[str]:
    """Remove common directory prefix from all strings in a.

    This uses a naive string replace; it seems to work well enough. Also
    remove trailing carriage returns.
    """
    res = []
    for s in a:
        prefix = os.sep
        ss = s
        for p in prefix, prefix.replace(os.sep, '/'):
            if p != '/' and p != '//' and p != '\\' and p != '\\\\':
                ss = ss.replace(p, '')
        # Ignore spaces at end of line.
        ss = re.sub(' +$', '', ss)
        res.append(re.sub('\\r$', '', ss))
    return res


def num_skipped_prefix_lines(a1: List[str], a2: List[str]) -> int:
    num_eq = 0
    while num_eq < min(len(a1), len(a2)) and a1[num_eq] == a2[num_eq]:
        num_eq += 1
    return max(0, num_eq - 4)


def num_skipped_suffix_lines(a1: List[str], a2: List[str]) -> int:
    num_eq = 0
    while (num_eq < min(len(a1), len(a2))
           and a1[-num_eq - 1] == a2[-num_eq - 1]):
        num_eq += 1
    return max(0, num_eq - 4)


def testfile_pyversion(path: str) -> Tuple[int, int]:
    if path.endswith('python2.test'):
        return defaults.PYTHON2_VERSION
    else:
        return defaults.PYTHON3_VERSION


def testcase_pyversion(path: str, testcase_name: str) -> Tuple[int, int]:
    if testcase_name.endswith('python2'):
        return defaults.PYTHON2_VERSION
    else:
        return testfile_pyversion(path)


def normalize_error_messages(messages: List[str]) -> List[str]:
    """Translate an array of error messages to use / as path separator."""

    a = []
    for m in messages:
        a.append(m.replace(os.sep, '/'))
    return a


def retry_on_error(func: Callable[[], Any], max_wait: float = 1.0) -> None:
    """Retry callback with exponential backoff when it raises OSError.

    If the function still generates an error after max_wait seconds, propagate
    the exception.

    This can be effective against random file system operation failures on
    Windows.
    """
    t0 = time.time()
    wait_time = 0.01
    while True:
        try:
            func()
            return
        except OSError:
            wait_time = min(wait_time * 2, t0 + max_wait - time.time())
            if wait_time <= 0.01:
                # Done enough waiting, the error seems persistent.
                raise
            time.sleep(wait_time)

# TODO: assert_true and assert_false are redundant - use plain assert


def assert_true(b: bool, msg: Optional[str] = None) -> None:
    if not b:
        raise AssertionError(msg)


def assert_false(b: bool, msg: Optional[str] = None) -> None:
    if b:
        raise AssertionError(msg)


def good_repr(obj: object) -> str:
    if isinstance(obj, str):
        if obj.count('\n') > 1:
            bits = ["'''\\"]
            for line in obj.split('\n'):
                # force repr to use ' not ", then cut it off
                bits.append(repr('"' + line)[2:-1])
            bits[-1] += "'''"
            return '\n'.join(bits)
    return repr(obj)


def assert_equal(a: object, b: object, fmt: str = '{} != {}') -> None:
    if a != b:
        raise AssertionError(fmt.format(good_repr(a), good_repr(b)))


def typename(t: type) -> str:
    if '.' in str(t):
        return str(t).split('.')[-1].rstrip("'>")
    else:
        return str(t)[8:-2]


def assert_type(typ: type, value: object) -> None:
    if type(value) != typ:
        raise AssertionError('Invalid type {}, expected {}'.format(
            typename(type(value)), typename(typ)))


def parse_options(program_text: str, testcase: DataDrivenTestCase,
                  incremental_step: int) -> Options:
    """Parse comments like '# flags: --foo' in a test case."""
    options = Options()
    flags = re.search('# flags: (.*)$', program_text, flags=re.MULTILINE)
    if incremental_step > 1:
        flags2 = re.search('# flags{}: (.*)$'.format(incremental_step), program_text,
                           flags=re.MULTILINE)
        if flags2:
            flags = flags2

    flag_list = None
    if flags:
        flag_list = flags.group(1).split()
        flag_list.append('--no-site-packages')  # the tests shouldn't need an installed Python
        targets, options = process_options(flag_list, require_targets=False)
        if targets:
            # TODO: support specifying targets via the flags pragma
            raise RuntimeError('Specifying targets via the flags pragma is not supported.')
    else:
        options = Options()
        # TODO: Enable strict optional in test cases by default (requires *many* test case changes)
        options.strict_optional = False

    # Allow custom python version to override testcase_pyversion
    if (not flag_list or
            all(flag not in flag_list for flag in ['--python-version', '-2', '--py2'])):
        options.python_version = testcase_pyversion(testcase.file, testcase.name)

    if testcase.config.getoption('--mypy-verbose'):
        options.verbosity = testcase.config.getoption('--mypy-verbose')

    if os.getenv('NEWSEMANAL'):
        if not flag_list or '--no-new-semantic-analyzer' not in flag_list:
            options.new_semantic_analyzer = True

    return options


def split_lines(*streams: bytes) -> List[str]:
    """Returns a single list of string lines from the byte streams in args."""
    return [
        s
        for stream in streams
        for s in stream.decode('utf8').splitlines()
    ]


def run_command(cmdline: List[str], *, env: Optional[Dict[str, str]] = None,
                timeout: int = 300, cwd: str = test_temp_dir) -> Tuple[int, List[str]]:
    """A poor man's subprocess.run() for 3.4 compatibility."""
    process = subprocess.Popen(
        cmdline,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=cwd,
    )
    try:
        out, err = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        out = err = b''
        process.kill()
    return process.returncode, split_lines(out, err)


def copy_and_fudge_mtime(source_path: str, target_path: str) -> None:
    # In some systems, mtime has a resolution of 1 second which can
    # cause annoying-to-debug issues when a file has the same size
    # after a change. We manually set the mtime to circumvent this.
    # Note that we increment the old file's mtime, which guarentees a
    # different value, rather than incrementing the mtime after the
    # copy, which could leave the mtime unchanged if the old file had
    # a similarly fudged mtime.
    new_time = None
    if os.path.isfile(target_path):
        new_time = os.stat(target_path).st_mtime + 1

    # Use retries to work around potential flakiness on Windows (AppVeyor).
    retry_on_error(lambda: shutil.copy(source_path, target_path))

    if new_time:
        os.utime(target_path, times=(new_time, new_time))
