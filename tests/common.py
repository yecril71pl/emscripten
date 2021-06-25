# Copyright 2021 The Emscripten Authors.  All rights reserved.
# Emscripten is available under two separate licenses, the MIT license and the
# University of Illinois/NCSA Open Source License.  Both these licenses can be
# found in the LICENSE file.

from enum import Enum
from functools import wraps
from pathlib import Path
from subprocess import PIPE, STDOUT
from urllib.parse import unquote, unquote_plus
from http.server import HTTPServer, SimpleHTTPRequestHandler
import contextlib
import difflib
import hashlib
import logging
import multiprocessing
import os
import shlex
import shutil
import stat
import string
import subprocess
import sys
import tempfile
import time
import webbrowser
import unittest

import clang_native
import jsrun
from jsrun import NON_ZERO
from tools.shared import TEMP_DIR, EMCC, EMXX, DEBUG, EMCONFIGURE, EMCMAKE
from tools.shared import EMSCRIPTEN_TEMP_DIR
from tools.shared import EM_BUILD_VERBOSE
from tools.shared import get_canonical_temp_dir, try_delete, path_from_root
from tools.utils import MACOS, WINDOWS
from tools import shared, line_endings, building, config

logger = logging.getLogger('common')

# User can specify an environment variable EMTEST_BROWSER to force the browser
# test suite to run using another browser command line than the default system
# browser.  Setting '0' as the browser disables running a browser (but we still
# see tests compile)
EMTEST_BROWSER = os.getenv('EMTEST_BROWSER')

EMTEST_DETECT_TEMPFILE_LEAKS = int(os.getenv('EMTEST_DETECT_TEMPFILE_LEAKS', '0'))

# TODO(sbc): Remove this check for the legacy name once its been around for a while.
assert 'EM_SAVE_DIR' not in os.environ, "Please use EMTEST_SAVE_DIR instead of EM_SAVE_DIR"

EMTEST_SAVE_DIR = int(os.getenv('EMTEST_SAVE_DIR', '0'))

# generally js engines are equivalent, testing 1 is enough. set this
# to force testing on all js engines, good to find js engine bugs
EMTEST_ALL_ENGINES = os.getenv('EMTEST_ALL_ENGINES')

EMTEST_SKIP_SLOW = os.getenv('EMTEST_SKIP_SLOW')

EMTEST_LACKS_NATIVE_CLANG = os.getenv('EMTEST_LACKS_NATIVE_CLANG')

EMTEST_VERBOSE = int(os.getenv('EMTEST_VERBOSE', '0')) or shared.DEBUG

TEST_ROOT = path_from_root('tests')

WEBIDL_BINDER = shared.bat_suffix(path_from_root('tools/webidl_binder'))

EMBUILDER = shared.bat_suffix(path_from_root('embuilder'))
EMMAKE = shared.bat_suffix(path_from_root('emmake'))


def delete_contents(pathname):
  for entry in os.listdir(pathname):
    try_delete(os.path.join(pathname, entry))


def test_file(*path_components):
  """Construct a path relative to the emscripten "tests" directory."""
  return str(Path(TEST_ROOT, *path_components))


def read_file(*path_components):
  return Path(*path_components).read_text()


def read_binary(*path_components):
  return Path(*path_components).read_bytes()


# checks if browser testing is enabled
def has_browser():
  return EMTEST_BROWSER != '0'


def compiler_for(filename, force_c=False):
  if shared.suffix(filename) in ('.cc', '.cxx', '.cpp') and not force_c:
    return EMXX
  else:
    return EMCC


# Generic decorator that calls a function named 'condition' on the test class and
# skips the test if that function returns true
def skip_if(func, condition, explanation='', negate=False):
  assert callable(func)
  explanation_str = ' : %s' % explanation if explanation else ''

  @wraps(func)
  def decorated(self, *args, **kwargs):
    choice = self.__getattribute__(condition)()
    if negate:
      choice = not choice
    if choice:
      self.skipTest(condition + explanation_str)
    func(self, *args, **kwargs)

  return decorated


def needs_dylink(func):
  assert callable(func)

  @wraps(func)
  def decorated(self):
    self.check_dylink()
    return func(self)

  return decorated


def is_slow_test(func):
  assert callable(func)

  @wraps(func)
  def decorated(self, *args, **kwargs):
    if EMTEST_SKIP_SLOW:
      return self.skipTest('skipping slow tests')
    return func(self, *args, **kwargs)

  return decorated


def disabled(note=''):
  assert not callable(note)
  return unittest.skip(note)


def no_mac(note=''):
  assert not callable(note)
  if MACOS:
    return unittest.skip(note)
  return lambda f: f


def no_windows(note=''):
  assert not callable(note)
  if WINDOWS:
    return unittest.skip(note)
  return lambda f: f


def requires_native_clang(func):
  assert callable(func)

  def decorated(self, *args, **kwargs):
    if EMTEST_LACKS_NATIVE_CLANG:
      return self.skipTest('native clang tests are disabled')
    return func(self, *args, **kwargs)

  return decorated


def require_node(func):
  assert callable(func)

  def decorated(self, *args, **kwargs):
    self.require_node()
    return func(self, *args, **kwargs)

  return decorated


def require_v8(func):
  assert callable(func)

  def decorated(self, *args, **kwargs):
    self.require_v8()
    return func(self, *args, **kwargs)

  return decorated


def node_pthreads(f):
  def decorated(self):
    self.set_setting('USE_PTHREADS')
    self.emcc_args += ['-Wno-pthreads-mem-growth']
    if self.get_setting('MINIMAL_RUNTIME'):
      self.skipTest('node pthreads not yet supported with MINIMAL_RUNTIME')
    self.js_engines = [config.NODE_JS]
    self.node_args += ['--experimental-wasm-threads', '--experimental-wasm-bulk-memory']
    f(self)
  return decorated


@contextlib.contextmanager
def env_modify(updates):
  """A context manager that updates os.environ."""
  # This could also be done with mock.patch.dict() but taking a dependency
  # on the mock library is probably not worth the benefit.
  old_env = os.environ.copy()
  print("env_modify: " + str(updates))
  # Seting a value to None means clear the environment variable
  clears = [key for key, value in updates.items() if value is None]
  updates = {key: value for key, value in updates.items() if value is not None}
  os.environ.update(updates)
  for key in clears:
    if key in os.environ:
      del os.environ[key]
  try:
    yield
  finally:
    os.environ.clear()
    os.environ.update(old_env)


# Decorator version of env_modify
def with_env_modify(updates):
  def decorated(f):
    def modified(self):
      with env_modify(updates):
        return f(self)
    return modified
  return decorated


def ensure_dir(dirname):
  dirname = Path(dirname)
  dirname.mkdir(parents=True, exist_ok=True)


def limit_size(string, maxbytes=800000 * 20, maxlines=100000, max_line=5000):
  lines = string.splitlines()
  for i, line in enumerate(lines):
    if len(line) > max_line:
      lines[i] = line[:max_line] + '[..]'
  if len(lines) > maxlines:
    lines = lines[0:maxlines // 2] + ['[..]'] + lines[-maxlines // 2:]
  string = '\n'.join(lines) + '\n'
  if len(string) > maxbytes:
    string = string[0:maxbytes // 2] + '\n[..]\n' + string[-maxbytes // 2:]
  return string


def create_file(name, contents, binary=False):
  name = Path(name)
  assert not name.is_absolute()
  if binary:
    name.write_bytes(contents)
  else:
    name.write_text(contents)


def make_executable(name):
  Path(name).chmod(stat.S_IREAD | stat.S_IWRITE | stat.S_IEXEC)


def parameterized(parameters):
  """
  Mark a test as parameterized.

  Usage:
    @parameterized({
      'subtest1': (1, 2, 3),
      'subtest2': (4, 5, 6),
    })
    def test_something(self, a, b, c):
      ... # actual test body

  This is equivalent to defining two tests:

    def test_something_subtest1(self):
      # runs test_something(1, 2, 3)

    def test_something_subtest2(self):
      # runs test_something(4, 5, 6)
  """
  def decorator(func):
    func._parameterize = parameters
    return func
  return decorator


class RunnerMeta(type):
  @classmethod
  def make_test(mcs, name, func, suffix, args):
    """
    This is a helper function to create new test functions for each parameterized form.

    :param name: the original name of the function
    :param func: the original function that we are parameterizing
    :param suffix: the suffix to append to the name of the function for this parameterization
    :param args: the positional arguments to pass to the original function for this parameterization
    :returns: a tuple of (new_function_name, new_function_object)
    """

    # Create the new test function. It calls the original function with the specified args.
    # We use @functools.wraps to copy over all the function attributes.
    @wraps(func)
    def resulting_test(self):
      return func(self, *args)

    # Add suffix to the function name so that it displays correctly.
    if suffix:
      resulting_test.__name__ = f'{name}_{suffix}'
    else:
      resulting_test.__name__ = name

    # On python 3, functions have __qualname__ as well. This is a full dot-separated path to the
    # function.  We add the suffix to it as well.
    resulting_test.__qualname__ = f'{func.__qualname__}_{suffix}'

    return resulting_test.__name__, resulting_test

  def __new__(mcs, name, bases, attrs):
    # This metaclass expands parameterized methods from `attrs` into separate ones in `new_attrs`.
    new_attrs = {}

    for attr_name, value in attrs.items():
      # Check if a member of the new class has _parameterize, the tag inserted by @parameterized.
      if hasattr(value, '_parameterize'):
        # If it does, we extract the parameterization information, build new test functions.
        for suffix, args in value._parameterize.items():
          new_name, func = mcs.make_test(attr_name, value, suffix, args)
          assert new_name not in new_attrs, 'Duplicate attribute name generated when parameterizing %s' % attr_name
          new_attrs[new_name] = func
      else:
        # If not, we just copy it over to new_attrs verbatim.
        assert attr_name not in new_attrs, '%s collided with an attribute from parameterization' % attr_name
        new_attrs[attr_name] = value

    # We invoke type, the default metaclass, to actually create the new class, with new_attrs.
    return type.__new__(mcs, name, bases, new_attrs)


class RunnerCore(unittest.TestCase, metaclass=RunnerMeta):
  # default temporary directory settings. set_temp_dir may be called later to
  # override these
  temp_dir = TEMP_DIR
  canonical_temp_dir = get_canonical_temp_dir(TEMP_DIR)

  # This avoids cluttering the test runner output, which is stderr too, with compiler warnings etc.
  # Change this to None to get stderr reporting, for debugging purposes
  stderr_redirect = STDOUT

  def is_wasm(self):
    return self.get_setting('WASM') != 0

  def check_dylink(self):
    if self.get_setting('ALLOW_MEMORY_GROWTH') == 1 and not self.is_wasm():
      self.skipTest('no dynamic linking with memory growth (without wasm)')
    if not self.is_wasm():
      self.skipTest('no dynamic linking support in wasm2js yet')
    if '-fsanitize=address' in self.emcc_args:
      self.skipTest('no dynamic linking support in ASan yet')
    if '-fsanitize=leak' in self.emcc_args:
      self.skipTest('no dynamic linking support in LSan yet')

  def require_v8(self):
    if not config.V8_ENGINE or config.V8_ENGINE not in config.JS_ENGINES:
      if 'EMTEST_SKIP_V8' in os.environ:
        self.skipTest('test requires v8 and EMTEST_SKIP_V8 is set')
      else:
        self.fail('d8 required to run this test.  Use EMTEST_SKIP_V8 to skip')
    self.js_engines = [config.V8_ENGINE]
    self.emcc_args.append('-sENVIRONMENT=shell')

  def require_node(self):
    if not config.NODE_JS or config.NODE_JS not in config.JS_ENGINES:
      if 'EMTEST_SKIP_NODE' in os.environ:
        self.skipTest('test requires node and EMTEST_SKIP_NODE is set')
      else:
        self.fail('node required to run this test.  Use EMTEST_SKIP_NODE to skip')
    self.js_engines = [config.NODE_JS]

  def uses_memory_init_file(self):
    if self.get_setting('SIDE_MODULE') or (self.is_wasm() and not self.get_setting('WASM2JS')):
      return False
    elif '--memory-init-file' in self.emcc_args:
      return int(self.emcc_args[self.emcc_args.index('--memory-init-file') + 1])
    else:
      # side modules handle memory differently; binaryen puts the memory in the wasm module
      opt_supports = any(opt in self.emcc_args for opt in ('-O2', '-O3', '-Os', '-Oz'))
      return opt_supports

  def set_temp_dir(self, temp_dir):
    self.temp_dir = temp_dir
    self.canonical_temp_dir = get_canonical_temp_dir(self.temp_dir)
    # Explicitly set dedicated temporary directory for parallel tests
    os.environ['EMCC_TEMP_DIR'] = self.temp_dir

  @classmethod
  def setUpClass(cls):
    super().setUpClass()
    print('(checking sanity from test runner)') # do this after we set env stuff
    shared.check_sanity(force=True)

  def setUp(self):
    super().setUp()
    self.settings_mods = {}
    self.emcc_args = ['-Werror']
    self.node_args = []
    self.v8_args = []
    self.env = {}
    self.temp_files_before_run = []
    self.uses_es6 = False
    self.js_engines = config.JS_ENGINES.copy()
    self.wasm_engines = config.WASM_ENGINES.copy()
    self.banned_js_engines = []
    self.use_all_engines = EMTEST_ALL_ENGINES

    if EMTEST_DETECT_TEMPFILE_LEAKS:
      for root, dirnames, filenames in os.walk(self.temp_dir):
        for dirname in dirnames:
          self.temp_files_before_run.append(os.path.normpath(os.path.join(root, dirname)))
        for filename in filenames:
          self.temp_files_before_run.append(os.path.normpath(os.path.join(root, filename)))

    if EMTEST_SAVE_DIR:
      self.working_dir = os.path.join(self.temp_dir, 'emscripten_test')
      if os.path.exists(self.working_dir):
        if EMTEST_SAVE_DIR == 2:
          print('Not clearing existing test directory')
        else:
          print('Clearing existing test directory')
          # Even when EMTEST_SAVE_DIR we still try to start with an empty directoy as many tests
          # expect this.  EMTEST_SAVE_DIR=2 can be used to keep the old contents for the new test
          # run. This can be useful when iterating on a given test with extra files you want to keep
          # around in the output directory.
          delete_contents(self.working_dir)
      else:
        print('Creating new test output directory')
        ensure_dir(self.working_dir)
    else:
      self.working_dir = tempfile.mkdtemp(prefix='emscripten_test_' + self.__class__.__name__ + '_', dir=self.temp_dir)
    os.chdir(self.working_dir)

    if not EMTEST_SAVE_DIR:
      self.has_prev_ll = False
      for temp_file in os.listdir(TEMP_DIR):
        if temp_file.endswith('.ll'):
          self.has_prev_ll = True

  def tearDown(self):
    if not EMTEST_SAVE_DIR:
      # rmtree() fails on Windows if the current working directory is inside the tree.
      os.chdir(os.path.dirname(self.get_dir()))
      try_delete(self.get_dir())

      if EMTEST_DETECT_TEMPFILE_LEAKS and not DEBUG:
        temp_files_after_run = []
        for root, dirnames, filenames in os.walk(self.temp_dir):
          for dirname in dirnames:
            temp_files_after_run.append(os.path.normpath(os.path.join(root, dirname)))
          for filename in filenames:
            temp_files_after_run.append(os.path.normpath(os.path.join(root, filename)))

        # Our leak detection will pick up *any* new temp files in the temp dir.
        # They may not be due to us, but e.g. the browser when running browser
        # tests. Until we figure out a proper solution, ignore some temp file
        # names that we see on our CI infrastructure.
        ignorable_file_prefixes = [
          '/tmp/tmpaddon',
          '/tmp/circleci-no-output-timeout',
          '/tmp/wasmer'
        ]

        left_over_files = set(temp_files_after_run) - set(self.temp_files_before_run)
        left_over_files = [f for f in left_over_files if not any([f.startswith(prefix) for prefix in ignorable_file_prefixes])]
        if len(left_over_files):
          print('ERROR: After running test, there are ' + str(len(left_over_files)) + ' new temporary files/directories left behind:', file=sys.stderr)
          for f in left_over_files:
            print('leaked file: ' + f, file=sys.stderr)
          self.fail('Test leaked ' + str(len(left_over_files)) + ' temporary files!')

  def get_setting(self, key, default=None):
    return self.settings_mods.get(key, default)

  def set_setting(self, key, value=1):
    if value is None:
      self.clear_setting(key)
    self.settings_mods[key] = value

  def has_changed_setting(self, key):
    return key in self.settings_mods

  def clear_setting(self, key):
    self.settings_mods.pop(key, None)

  def serialize_settings(self):
    ret = []
    for key, value in self.settings_mods.items():
      if value == 1:
        ret.append(f'-s{key}')
      elif type(value) == list:
        ret.append(f'-s{key}={",".join(value)}')
      else:
        ret.append(f'-s{key}={value}')
    return ret

  def get_dir(self):
    return self.working_dir

  def in_dir(self, *pathelems):
    return os.path.join(self.get_dir(), *pathelems)

  def add_pre_run(self, code):
    create_file('prerun.js', 'Module.preRun = function() { %s }' % code)
    self.emcc_args += ['--pre-js', 'prerun.js']

  def add_post_run(self, code):
    create_file('postrun.js', 'Module.postRun = function() { %s }' % code)
    self.emcc_args += ['--pre-js', 'postrun.js']

  def add_on_exit(self, code):
    create_file('onexit.js', 'Module.onExit = function() { %s }' % code)
    self.emcc_args += ['--pre-js', 'onexit.js']

  # returns the full list of arguments to pass to emcc
  # param @main_file whether this is the main file of the test. some arguments
  #                  (like --pre-js) do not need to be passed when building
  #                  libraries, for example
  def get_emcc_args(self, main_file=False):
    args = self.serialize_settings() + self.emcc_args
    if not main_file:
      for i, arg in enumerate(args):
        if arg in ('--pre-js', '--post-js'):
          args[i] = None
          args[i + 1] = None
      args = [arg for arg in args if arg is not None]
    return args

  def verify_es5(self, filename):
    es_check = shared.get_npm_cmd('es-check')
    # use --quiet once its available
    # See: https://github.com/dollarshaveclub/es-check/pull/126/
    es_check_env = os.environ.copy()
    es_check_env['PATH'] = os.path.dirname(config.NODE_JS[0]) + os.pathsep + es_check_env['PATH']
    try:
      shared.run_process(es_check + ['es5', os.path.abspath(filename), '--quiet'], stderr=PIPE, env=es_check_env)
    except subprocess.CalledProcessError as e:
      print(e.stderr)
      self.fail('es-check failed to verify ES5 output compliance')

  # Build JavaScript code from source code
  def build(self, filename, libraries=[], includes=[], force_c=False, js_outfile=True, emcc_args=[]):
    suffix = '.js' if js_outfile else '.wasm'
    compiler = [compiler_for(filename, force_c)]
    if compiler[0] == EMCC:
      # TODO(https://github.com/emscripten-core/emscripten/issues/11121)
      # We link with C++ stdlibs, even when linking with emcc for historical reasons.  We can remove
      # this if this issues is fixed.
      compiler.append('-nostdlib++')

    if force_c:
      compiler.append('-xc')

    dirname, basename = os.path.split(filename)
    output = shared.unsuffixed(basename) + suffix
    cmd = compiler + [filename, '-o', output] + self.get_emcc_args(main_file=True) + emcc_args + libraries
    if shared.suffix(filename) not in ('.i', '.ii'):
      # Add the location of the test file to include path.
      cmd += ['-I.']
      cmd += ['-I' + str(include) for include in includes]

    self.run_process(cmd, stderr=self.stderr_redirect if not DEBUG else None)
    self.assertExists(output)
    if js_outfile and not self.uses_es6:
      self.verify_es5(output)

    if js_outfile and self.uses_memory_init_file():
      src = read_file(output)
      # side memory init file, or an empty one in the js
      assert ('/* memory initializer */' not in src) or ('/* memory initializer */ allocate([]' in src)

    return output

  def get_func(self, src, name):
    start = src.index('function ' + name + '(')
    t = start
    n = 0
    while True:
      if src[t] == '{':
        n += 1
      elif src[t] == '}':
        n -= 1
        if n == 0:
          return src[start:t + 1]
      t += 1
      assert t < len(src)

  def count_funcs(self, javascript_file):
    num_funcs = 0
    start_tok = "// EMSCRIPTEN_START_FUNCS"
    end_tok = "// EMSCRIPTEN_END_FUNCS"
    start_off = 0
    end_off = 0

    js = read_file(javascript_file)
    blob = "".join(js.splitlines())

    start_off = blob.find(start_tok) + len(start_tok)
    end_off = blob.find(end_tok)
    asm_chunk = blob[start_off:end_off]
    num_funcs = asm_chunk.count('function ')
    return num_funcs

  def count_wasm_contents(self, wasm_binary, what):
    out = self.run_process([os.path.join(building.get_binaryen_bin(), 'wasm-opt'), wasm_binary, '--metrics'], stdout=PIPE).stdout
    # output is something like
    # [?]        : 125
    for line in out.splitlines():
      if '[' + what + ']' in line:
        ret = line.split(':')[1].strip()
        return int(ret)
    self.fail('Failed to find [%s] in wasm-opt output' % what)

  def get_wasm_text(self, wasm_binary):
    return self.run_process([os.path.join(building.get_binaryen_bin(), 'wasm-dis'), wasm_binary], stdout=PIPE).stdout

  def is_exported_in_wasm(self, name, wasm):
    wat = self.get_wasm_text(wasm)
    return ('(export "%s"' % name) in wat

  def run_js(self, filename, engine=None, args=[], output_nicerizer=None, assert_returncode=0):
    # use files, as PIPE can get too full and hang us
    stdout = self.in_dir('stdout')
    stderr = self.in_dir('stderr')
    error = None
    if not engine:
      engine = self.js_engines[0]
    if engine == config.NODE_JS:
      engine = engine + self.node_args
    if engine == config.V8_ENGINE:
      engine = engine + self.v8_args
    if EMTEST_VERBOSE:
      print(f"Running '{filename}' under '{shared.shlex_join(engine)}'")
    try:
      jsrun.run_js(filename, engine, args,
                   stdout=open(stdout, 'w'),
                   stderr=open(stderr, 'w'),
                   assert_returncode=assert_returncode)
    except subprocess.CalledProcessError as e:
      error = e

    # Make sure that we produced proper line endings to the .js file we are about to run.
    if not filename.endswith('.wasm'):
      self.assertEqual(line_endings.check_line_endings(filename), 0)

    out = read_file(stdout)
    err = read_file(stderr)
    if output_nicerizer:
      ret = output_nicerizer(out, err)
    else:
      ret = out + err
    if error or EMTEST_VERBOSE:
      ret = limit_size(ret)
      print('-- begin program output --')
      print(ret, end='')
      print('-- end program output --')
      if error:
        if assert_returncode == NON_ZERO:
          self.fail('JS subprocess unexpectedly succeeded (%s):  Output:\n%s' % (error.cmd, ret))
        else:
          self.fail('JS subprocess failed (%s): %s.  Output:\n%s' % (error.cmd, error.returncode, ret))

    #  We should pass all strict mode checks
    self.assertNotContained('strict warning:', ret)
    return ret

  def assertExists(self, filename, msg=None):
    if not msg:
      msg = 'Expected file not found: ' + filename
    self.assertTrue(os.path.exists(filename), msg)

  def assertNotExists(self, filename, msg=None):
    if not msg:
      msg = 'Unexpected file exists: ' + filename
    self.assertFalse(os.path.exists(filename), msg)

  # Tests that the given two paths are identical, modulo path delimiters. E.g. "C:/foo" is equal to "C:\foo".
  def assertPathsIdentical(self, path1, path2):
    path1 = path1.replace('\\', '/')
    path2 = path2.replace('\\', '/')
    return self.assertIdentical(path1, path2)

  # Tests that the given two multiline text content are identical, modulo line
  # ending differences (\r\n on Windows, \n on Unix).
  def assertTextDataIdentical(self, text1, text2, msg=None,
                              fromfile='expected', tofile='actual'):
    text1 = text1.replace('\r\n', '\n')
    text2 = text2.replace('\r\n', '\n')
    return self.assertIdentical(text1, text2, msg, fromfile, tofile)

  def assertIdentical(self, values, y, msg=None,
                      fromfile='expected', tofile='actual'):
    if type(values) not in (list, tuple):
      values = [values]
    for x in values:
      if x == y:
        return # success
    diff_lines = difflib.unified_diff(x.splitlines(), y.splitlines(),
                                      fromfile=fromfile, tofile=tofile)
    diff = ''.join([a.rstrip() + '\n' for a in diff_lines])
    if EMTEST_VERBOSE:
      print("Expected to have '%s' == '%s'" % (limit_size(values[0]), limit_size(y)))
    fail_message = 'Unexpected difference:\n' + limit_size(diff)
    if not EMTEST_VERBOSE:
      fail_message += '\nFor full output run with EMTEST_VERBOSE=1.'
    if msg:
      fail_message += '\n' + msg
    self.fail(fail_message)

  def assertTextDataContained(self, text1, text2):
    text1 = text1.replace('\r\n', '\n')
    text2 = text2.replace('\r\n', '\n')
    return self.assertContained(text1, text2)

  def assertContained(self, values, string, additional_info=''):
    if type(values) not in [list, tuple]:
      values = [values]
    if callable(string):
      string = string()

    if not any(v in string for v in values):
      diff = difflib.unified_diff(values[0].split('\n'), string.split('\n'), fromfile='expected', tofile='actual')
      diff = ''.join(a.rstrip() + '\n' for a in diff)
      self.fail("Expected to find '%s' in '%s', diff:\n\n%s\n%s" % (
        limit_size(values[0]), limit_size(string), limit_size(diff),
        additional_info
      ))

  def assertNotContained(self, value, string):
    if callable(value):
      value = value() # lazy loading
    if callable(string):
      string = string()
    if value in string:
      self.fail("Expected to NOT find '%s' in '%s', diff:\n\n%s" % (
        limit_size(value), limit_size(string),
        limit_size(''.join([a.rstrip() + '\n' for a in difflib.unified_diff(value.split('\n'), string.split('\n'), fromfile='expected', tofile='actual')]))
      ))

  def assertContainedIf(self, value, string, condition):
    if condition:
      self.assertContained(value, string)
    else:
      self.assertNotContained(value, string)

  def assertBinaryEqual(self, file1, file2):
    self.assertEqual(os.path.getsize(file1),
                     os.path.getsize(file2))
    self.assertEqual(read_binary(file1),
                     read_binary(file2))

  library_cache = {}

  def get_build_dir(self):
    ret = os.path.join(self.get_dir(), 'building')
    ensure_dir(ret)
    return ret

  def get_library(self, name, generated_libs, configure=['sh', './configure'],
                  configure_args=[], make=['make'], make_args=None,
                  env_init=None, cache_name_extra='', native=False):
    if env_init is None:
      env_init = {}
    if make_args is None:
      make_args = ['-j', str(shared.get_num_cores())]

    build_dir = self.get_build_dir()
    output_dir = self.get_dir()

    emcc_args = self.get_emcc_args()

    hash_input = (str(emcc_args) + ' $ ' + str(env_init)).encode('utf-8')
    cache_name = name + ','.join([opt for opt in emcc_args if len(opt) < 7]) + '_' + hashlib.md5(hash_input).hexdigest() + cache_name_extra

    valid_chars = "_%s%s" % (string.ascii_letters, string.digits)
    cache_name = ''.join([(c if c in valid_chars else '_') for c in cache_name])

    if self.library_cache.get(cache_name):
      print('<load %s from cache> ' % cache_name, file=sys.stderr)
      generated_libs = []
      for basename, contents in self.library_cache[cache_name]:
        bc_file = os.path.join(build_dir, cache_name + '_' + basename)
        with open(bc_file, 'wb') as f:
          f.write(contents)
        generated_libs.append(bc_file)
      return generated_libs

    print(f'<building and saving {cache_name} into cache>', file=sys.stderr)
    if configure is not None:
      # Avoid += so we don't mutate the default arg
      configure = configure + configure_args

    cflags = ' '.join(self.get_emcc_args())
    env_init.setdefault('CFLAGS', cflags)
    env_init.setdefault('CXXFLAGS', cflags)
    return build_library(name, build_dir, output_dir, generated_libs, configure,
                         make, make_args, self.library_cache,
                         cache_name, env_init=env_init, native=native)

  def clear(self):
    delete_contents(self.get_dir())
    if EMSCRIPTEN_TEMP_DIR:
      delete_contents(EMSCRIPTEN_TEMP_DIR)

  def run_process(self, cmd, check=True, **args):
    # Wrapper around shared.run_process.  This is desirable so that the tests
    # can fail (in the unittest sense) rather than error'ing.
    # In the long run it would nice to completely remove the dependency on
    # core emscripten code (shared.py) here.
    try:
      return shared.run_process(cmd, check=check, **args)
    except subprocess.CalledProcessError as e:
      if check and e.returncode != 0:
        self.fail('subprocess exited with non-zero return code(%d): `%s`' %
                  (e.returncode, shared.shlex_join(cmd)))

  def emcc(self, filename, args=[], output_filename=None, **kwargs):
    if output_filename is None:
      output_filename = filename + '.o'
    try_delete(output_filename)
    self.run_process([compiler_for(filename), filename] + args + ['-o', output_filename], **kwargs)

  # Shared test code between main suite and others

  def expect_fail(self, cmd, **args):
    """Run a subprocess and assert that it returns non-zero.

    Return the stderr of the subprocess.
    """
    proc = self.run_process(cmd, check=False, stderr=PIPE, **args)
    self.assertNotEqual(proc.returncode, 0, 'subprocess unexpectedly succeeded. stderr:\n' + proc.stderr)
    # When we check for failure we expect a user-visible error, not a traceback.
    # However, on windows a python traceback can happen randomly sometimes,
    # due to "Access is denied" https://github.com/emscripten-core/emscripten/issues/718
    if not WINDOWS or 'Access is denied' not in proc.stderr:
      self.assertNotContained('Traceback', proc.stderr)
    return proc.stderr

  # excercise dynamic linker.
  #
  # test that linking to shared library B, which is linked to A, loads A as well.
  # main is also linked to C, which is also linked to A. A is loaded/initialized only once.
  #
  #          B
  #   main <   > A
  #          C
  #
  # this test is used by both test_core and test_browser.
  # when run under broswer it excercises how dynamic linker handles concurrency
  # - because B and C are loaded in parallel.
  def _test_dylink_dso_needed(self, do_run):
    create_file('liba.cpp', r'''
        #include <stdio.h>
        #include <emscripten.h>

        static const char *afunc_prev;

        extern "C" {
        EMSCRIPTEN_KEEPALIVE void afunc(const char *s);
        }

        void afunc(const char *s) {
          printf("a: %s (prev: %s)\n", s, afunc_prev);
          afunc_prev = s;
        }

        struct ainit {
          ainit() {
            puts("a: loaded");
          }
        };

        static ainit _;
      ''')

    create_file('libb.c', r'''
        #include <emscripten.h>

        void afunc(const char *s);

        EMSCRIPTEN_KEEPALIVE void bfunc() {
          afunc("b");
        }
      ''')

    create_file('libc.c', r'''
        #include <emscripten.h>

        void afunc(const char *s);

        EMSCRIPTEN_KEEPALIVE void cfunc() {
          afunc("c");
        }
      ''')

    # _test_dylink_dso_needed can be potentially called several times by a test.
    # reset dylink-related options first.
    self.clear_setting('MAIN_MODULE')
    self.clear_setting('SIDE_MODULE')

    # XXX in wasm each lib load currently takes 5MB; default INITIAL_MEMORY=16MB is thus not enough
    self.set_setting('INITIAL_MEMORY', '32mb')

    so = '.wasm' if self.is_wasm() else '.js'

    def ccshared(src, linkto=[]):
      cmdv = [EMCC, src, '-o', shared.unsuffixed(src) + so, '-s', 'SIDE_MODULE'] + self.get_emcc_args()
      cmdv += linkto
      self.run_process(cmdv)

    ccshared('liba.cpp')
    ccshared('libb.c', ['liba' + so])
    ccshared('libc.c', ['liba' + so])

    self.set_setting('MAIN_MODULE')
    extra_args = ['-L.', 'libb' + so, 'libc' + so]
    do_run(r'''
      #ifdef __cplusplus
      extern "C" {
      #endif
      void bfunc();
      void cfunc();
      #ifdef __cplusplus
      }
      #endif

      int test_main() {
        bfunc();
        cfunc();
        return 0;
      }
      ''',
           'a: loaded\na: b (prev: (null))\na: c (prev: b)\n', emcc_args=extra_args)

    for libname in ['liba', 'libb', 'libc']:
      self.emcc_args += ['--embed-file', libname + so]
    do_run(r'''
      #include <assert.h>
      #include <dlfcn.h>
      #include <stddef.h>

      int test_main() {
        void *bdso, *cdso;
        void (*bfunc_ptr)(), (*cfunc_ptr)();

        // FIXME for RTLD_LOCAL binding symbols to loaded lib is not currently working
        bdso = dlopen("libb%(so)s", RTLD_NOW|RTLD_GLOBAL);
        assert(bdso != NULL);
        cdso = dlopen("libc%(so)s", RTLD_NOW|RTLD_GLOBAL);
        assert(cdso != NULL);

        bfunc_ptr = (void (*)())dlsym(bdso, "bfunc");
        assert(bfunc_ptr != NULL);
        cfunc_ptr = (void (*)())dlsym(cdso, "cfunc");
        assert(cfunc_ptr != NULL);

        bfunc_ptr();
        cfunc_ptr();
        return 0;
      }
    ''' % locals(),
           'a: loaded\na: b (prev: (null))\na: c (prev: b)\n')

  def filtered_js_engines(self, js_engines=None):
    if js_engines is None:
      js_engines = self.js_engines
    for engine in js_engines:
      assert engine in config.JS_ENGINES, "js engine does not exist in config.JS_ENGINES"
      assert type(engine) == list
    for engine in self.banned_js_engines:
      assert type(engine) in (list, type(None))
    banned = [b[0] for b in self.banned_js_engines if b]
    return [engine for engine in js_engines if engine and engine[0] not in banned]

  def do_run(self, src, expected_output, force_c=False, **kwargs):
    if 'no_build' in kwargs:
      filename = src
    else:
      if force_c:
        filename = 'src.c'
      else:
        filename = 'src.cpp'
      with open(filename, 'w') as f:
        f.write(src)
    self._build_and_run(filename, expected_output, **kwargs)

  def do_runf(self, filename, expected_output=None, **kwargs):
    self._build_and_run(filename, expected_output, **kwargs)

  ## Just like `do_run` but with filename of expected output
  def do_run_from_file(self, filename, expected_output_filename, **kwargs):
    self._build_and_run(filename, read_file(expected_output_filename), **kwargs)

  def do_run_in_out_file_test(self, *path, **kwargs):
    srcfile = test_file(*path)
    out_suffix = kwargs.pop('out_suffix', '')
    outfile = shared.unsuffixed(srcfile) + out_suffix + '.out'
    expected = read_file(outfile)
    self._build_and_run(srcfile, expected, **kwargs)

  ## Does a complete test - builds, runs, checks output, etc.
  def _build_and_run(self, filename, expected_output, args=[], output_nicerizer=None,
                     no_build=False,
                     js_engines=None, libraries=[],
                     includes=[],
                     assert_returncode=0, assert_identical=False, assert_all=False,
                     check_for_error=True, force_c=False, emcc_args=[]):
    logger.debug(f'_build_and_run: {filename}')

    if no_build:
      js_file = filename
    else:
      self.build(filename, libraries=libraries, includes=includes,
                 force_c=force_c, emcc_args=emcc_args)
      js_file = shared.unsuffixed(os.path.basename(filename)) + '.js'
    self.assertExists(js_file)

    engines = self.filtered_js_engines(js_engines)
    if len(engines) > 1 and not self.use_all_engines:
      engines = engines[:1]
    # In standalone mode, also add wasm vms as we should be able to run there too.
    if self.get_setting('STANDALONE_WASM'):
      # TODO once standalone wasm support is more stable, apply use_all_engines
      # like with js engines, but for now as we bring it up, test in all of them
      if not self.wasm_engines:
        logger.warning('no wasm engine was found to run the standalone part of this test')
      engines += self.wasm_engines
      if self.get_setting('WASM2C') and not EMTEST_LACKS_NATIVE_CLANG:
        # compile the c file to a native executable.
        c = shared.unsuffixed(js_file) + '.wasm.c'
        executable = shared.unsuffixed(js_file) + '.exe'
        cmd = [shared.CLANG_CC, c, '-o', executable] + clang_native.get_clang_native_args()
        self.run_process(cmd, env=clang_native.get_clang_native_env())
        # we can now run the executable directly, without an engine, which
        # we indicate with None as the engine
        engines += [[None]]
    if len(engines) == 0:
      self.skipTest('No JS engine present to run this test with. Check %s and the paths therein.' % config.EM_CONFIG)
    for engine in engines:
      js_output = self.run_js(js_file, engine, args, output_nicerizer=output_nicerizer, assert_returncode=assert_returncode)
      js_output = js_output.replace('\r\n', '\n')
      if expected_output:
        try:
          if assert_identical:
            self.assertIdentical(expected_output, js_output)
          elif assert_all:
            for o in expected_output:
              self.assertContained(o, js_output)
          else:
            self.assertContained(expected_output, js_output)
            if check_for_error:
              self.assertNotContained('ERROR', js_output)
        except Exception:
          print('(test did not pass in JS engine: %s)' % engine)
          raise

  def get_freetype_library(self):
    if '-Werror' in self.emcc_args:
      self.emcc_args.remove('-Werror')
    return self.get_library(os.path.join('third_party', 'freetype'), os.path.join('objs', '.libs', 'libfreetype.a'), configure_args=['--disable-shared', '--without-zlib'])

  def get_poppler_library(self, env_init=None):
    # The fontconfig symbols are all missing from the poppler build
    # e.g. FcConfigSubstitute
    self.set_setting('ERROR_ON_UNDEFINED_SYMBOLS', 0)

    self.emcc_args += [
      '-I' + test_file('third_party/freetype/include'),
      '-I' + test_file('third_party/poppler/include')
    ]

    freetype = self.get_freetype_library()

    # Poppler has some pretty glaring warning.  Suppress them to keep the
    # test output readable.
    if '-Werror' in self.emcc_args:
      self.emcc_args.remove('-Werror')
    self.emcc_args += [
      '-Wno-sentinel',
      '-Wno-logical-not-parentheses',
      '-Wno-unused-private-field',
      '-Wno-tautological-compare',
      '-Wno-unknown-pragmas',
    ]
    env_init = env_init.copy() if env_init else {}
    env_init['FONTCONFIG_CFLAGS'] = ' '
    env_init['FONTCONFIG_LIBS'] = ' '

    poppler = self.get_library(
        os.path.join('third_party', 'poppler'),
        [os.path.join('utils', 'pdftoppm.o'), os.path.join('utils', 'parseargs.o'), os.path.join('poppler', '.libs', 'libpoppler.a')],
        env_init=env_init,
        configure_args=['--disable-libjpeg', '--disable-libpng', '--disable-poppler-qt', '--disable-poppler-qt4', '--disable-cms', '--disable-cairo-output', '--disable-abiword-output', '--disable-shared'])

    return poppler + freetype

  def get_zlib_library(self):
    if WINDOWS:
      return self.get_library(os.path.join('third_party', 'zlib'), os.path.join('libz.a'),
                              configure=['cmake', '.'],
                              make=['cmake', '--build', '.'],
                              make_args=[])
    return self.get_library(os.path.join('third_party', 'zlib'), os.path.join('libz.a'), make_args=['libz.a'])


# Run a server and a web page. When a test runs, we tell the server about it,
# which tells the web page, which then opens a window with the test. Doing
# it this way then allows the page to close() itself when done.
def harness_server_func(in_queue, out_queue, port):
  class TestServerHandler(SimpleHTTPRequestHandler):
    # Request header handler for default do_GET() path in
    # SimpleHTTPRequestHandler.do_GET(self) below.
    def send_head(self):
      if self.path.endswith('.js'):
        path = self.translate_path(self.path)
        try:
          f = open(path, 'rb')
        except IOError:
          self.send_error(404, "File not found: " + path)
          return None
        self.send_response(200)
        self.send_header('Content-type', 'application/javascript')
        self.send_header('Connection', 'close')
        self.end_headers()
        return f
      else:
        return SimpleHTTPRequestHandler.send_head(self)

    # Add COOP, COEP, CORP, and no-caching headers
    def end_headers(self):
      self.send_header('Access-Control-Allow-Origin', '*')
      self.send_header('Cross-Origin-Opener-Policy', 'same-origin')
      self.send_header('Cross-Origin-Embedder-Policy', 'require-corp')
      self.send_header('Cross-Origin-Resource-Policy', 'cross-origin')
      self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
      return SimpleHTTPRequestHandler.end_headers(self)

    def do_GET(self):
      if self.path == '/run_harness':
        if DEBUG:
          print('[server startup]')
        self.send_response(200)
        self.send_header('Content-type', 'text/html')
        self.end_headers()
        self.wfile.write(read_binary(test_file('browser_harness.html')))
      elif 'report_' in self.path:
        # the test is reporting its result. first change dir away from the
        # test dir, as it will be deleted now that the test is finishing, and
        # if we got a ping at that time, we'd return an error
        os.chdir(path_from_root())
        # for debugging, tests may encode the result and their own url (window.location) as result|url
        if '|' in self.path:
          path, url = self.path.split('|', 1)
        else:
          path = self.path
          url = '?'
        if DEBUG:
          print('[server response:', path, url, ']')
        if out_queue.empty():
          out_queue.put(path)
        else:
          # a badly-behaving test may send multiple xhrs with reported results; we just care
          # about the first (if we queued the others, they might be read as responses for
          # later tests, or maybe the test sends more than one in a racy manner).
          # we place 'None' in the queue here so that the outside knows something went wrong
          # (none is not a valid value otherwise; and we need the outside to know because if we
          # raise an error in here, it is just swallowed in python's webserver code - we want
          # the test to actually fail, which a webserver response can't do).
          out_queue.put(None)
          raise Exception('browser harness error, excessive response to server - test must be fixed! "%s"' % self.path)
        self.send_response(200)
        self.send_header('Content-type', 'text/plain')
        self.send_header('Cache-Control', 'no-cache, must-revalidate')
        self.send_header('Connection', 'close')
        self.send_header('Expires', '-1')
        self.end_headers()
        self.wfile.write(b'OK')

      elif 'stdout=' in self.path or 'stderr=' in self.path or 'exception=' in self.path:
        '''
          To get logging to the console from browser tests, add this to
          print/printErr/the exception handler in src/shell.html:

            var xhr = new XMLHttpRequest();
            xhr.open('GET', encodeURI('http://localhost:8888?stdout=' + text));
            xhr.send();
        '''
        print('[client logging:', unquote_plus(self.path), ']')
        self.send_response(200)
        self.send_header('Content-type', 'text/html')
        self.end_headers()
      elif self.path == '/check':
        self.send_response(200)
        self.send_header('Content-type', 'text/html')
        self.end_headers()
        if not in_queue.empty():
          # there is a new test ready to be served
          url, dir = in_queue.get()
          if DEBUG:
            print('[queue command:', url, dir, ']')
          assert in_queue.empty(), 'should not be any blockage - one test runs at a time'
          assert out_queue.empty(), 'the single response from the last test was read'
          # tell the browser to load the test
          self.wfile.write(b'COMMAND:' + url.encode('utf-8'))
          # move us to the right place to serve the files for the new test
          os.chdir(dir)
        else:
          # the browser must keep polling
          self.wfile.write(b'(wait)')
      else:
        # Use SimpleHTTPServer default file serving operation for GET.
        if DEBUG:
          print('[simple HTTP serving:', unquote_plus(self.path), ']')
        SimpleHTTPRequestHandler.do_GET(self)

    def log_request(code=0, size=0):
      # don't log; too noisy
      pass

  # allows streaming compilation to work
  SimpleHTTPRequestHandler.extensions_map['.wasm'] = 'application/wasm'

  httpd = HTTPServer(('localhost', port), TestServerHandler)
  httpd.serve_forever() # test runner will kill us


class Reporting(Enum):
  """When running browser tests we normally automatically include support
  code for reporting results back to the browser.  This enum allows tests
  to decide what type of support code they need/want.
  """
  NONE = 0
  # Include the JS helpers for reporting results
  JS_ONLY = 1
  # Include C/C++ reporting code (REPORT_RESULT mactros) as well as JS helpers
  FULL = 2


class BrowserCore(RunnerCore):
  # note how many tests hang / do not send an output. if many of these
  # happen, likely something is broken and it is best to abort the test
  # suite early, as otherwise we will wait for the timeout on every
  # single test (hundreds of minutes)
  MAX_UNRESPONSIVE_TESTS = 10

  unresponsive_tests = 0

  def __init__(self, *args, **kwargs):
    super().__init__(*args, **kwargs)

  @staticmethod
  def browser_open(url):
    if not EMTEST_BROWSER:
      logger.info('Using default system browser')
      webbrowser.open_new(url)
      return

    browser_args = shlex.split(EMTEST_BROWSER)
    # If the given browser is a scalar, treat it like one of the possible types
    # from https://docs.python.org/2/library/webbrowser.html
    if len(browser_args) == 1:
      try:
        # This throws if the type of browser isn't available
        webbrowser.get(browser_args[0]).open_new(url)
        logger.info('Using Emscripten browser: %s', browser_args[0])
        return
      except webbrowser.Error:
        # Ignore the exception and fallback to the custom command logic
        pass
    # Else assume the given browser is a specific program with additional
    # parameters and delegate to that
    logger.info('Using Emscripten browser: %s', str(browser_args))
    subprocess.Popen(browser_args + [url])

  @classmethod
  def setUpClass(cls):
    super().setUpClass()
    cls.also_asmjs = int(os.getenv('EMTEST_BROWSER_ALSO_ASMJS', '0')) == 1
    cls.port = int(os.getenv('EMTEST_BROWSER_PORT', '8888'))
    if not has_browser():
      return
    cls.browser_timeout = 60
    cls.harness_in_queue = multiprocessing.Queue()
    cls.harness_out_queue = multiprocessing.Queue()
    cls.harness_server = multiprocessing.Process(target=harness_server_func, args=(cls.harness_in_queue, cls.harness_out_queue, cls.port))
    cls.harness_server.start()
    print('[Browser harness server on process %d]' % cls.harness_server.pid)
    cls.browser_open('http://localhost:%s/run_harness' % cls.port)

  @classmethod
  def tearDownClass(cls):
    super().tearDownClass()
    if not has_browser():
      return
    cls.harness_server.terminate()
    print('[Browser harness server terminated]')
    if WINDOWS:
      # On Windows, shutil.rmtree() in tearDown() raises this exception if we do not wait a bit:
      # WindowsError: [Error 32] The process cannot access the file because it is being used by another process.
      time.sleep(0.1)

  def assert_out_queue_empty(self, who):
    if not self.harness_out_queue.empty():
      while not self.harness_out_queue.empty():
        self.harness_out_queue.get()
      raise Exception('excessive responses from %s' % who)

  # @param extra_tries: how many more times to try this test, if it fails. browser tests have
  #                     many more causes of flakiness (in particular, they do not run
  #                     synchronously, so we have a timeout, which can be hit if the VM
  #                     we run on stalls temporarily), so we let each test try more than
  #                     once by default
  def run_browser(self, html_file, message, expectedResult=None, timeout=None, extra_tries=1):
    if not has_browser():
      return
    if BrowserCore.unresponsive_tests >= BrowserCore.MAX_UNRESPONSIVE_TESTS:
      self.skipTest('too many unresponsive tests, skipping browser launch - check your setup!')
    self.assert_out_queue_empty('previous test')
    if DEBUG:
      print('[browser launch:', html_file, ']')
    if expectedResult is not None:
      try:
        self.harness_in_queue.put((
          'http://localhost:%s/%s' % (self.port, html_file),
          self.get_dir()
        ))
        received_output = False
        output = '[no http server activity]'
        start = time.time()
        if timeout is None:
          timeout = self.browser_timeout
        while time.time() - start < timeout:
          if not self.harness_out_queue.empty():
            output = self.harness_out_queue.get()
            received_output = True
            break
          time.sleep(0.1)
        if not received_output:
          BrowserCore.unresponsive_tests += 1
          print('[unresponsive tests: %d]' % BrowserCore.unresponsive_tests)
        if output is None:
          # the browser harness reported an error already, and sent a None to tell
          # us to also fail the test
          raise Exception('failing test due to browser harness error')
        if output.startswith('/report_result?skipped:'):
          self.skipTest(unquote(output[len('/report_result?skipped:'):]).strip())
        else:
          # verify the result, and try again if we should do so
          output = unquote(output)
          try:
            self.assertContained(expectedResult, output)
          except Exception as e:
            if extra_tries > 0:
              print('[test error (see below), automatically retrying]')
              print(e)
              return self.run_browser(html_file, message, expectedResult, timeout, extra_tries - 1)
            else:
              raise e
      finally:
        time.sleep(0.1) # see comment about Windows above
      self.assert_out_queue_empty('this test')
    else:
      webbrowser.open_new(os.path.abspath(html_file))
      print('A web browser window should have opened a page containing the results of a part of this test.')
      print('You need to manually look at the page to see that it works ok: ' + message)
      print('(sleeping for a bit to keep the directory alive for the web browser..)')
      time.sleep(5)
      print('(moving on..)')

  # @manually_trigger If set, we do not assume we should run the reftest when main() is done.
  #                   Instead, call doReftest() in JS yourself at the right time.
  def reftest(self, expected, manually_trigger=False):
    # make sure the pngs used here have no color correction, using e.g.
    #   pngcrush -rem gAMA -rem cHRM -rem iCCP -rem sRGB infile outfile
    basename = os.path.basename(expected)
    shutil.copyfile(expected, os.path.join(self.get_dir(), basename))
    reporting = read_file(test_file('browser_reporting.js'))
    with open('reftest.js', 'w') as out:
      out.write('''
      function doReftest() {
        if (doReftest.done) return;
        doReftest.done = true;
        var img = new Image();
        img.onload = function() {
          assert(img.width == Module.canvas.width, 'Invalid width: ' + Module.canvas.width + ', should be ' + img.width);
          assert(img.height == Module.canvas.height, 'Invalid height: ' + Module.canvas.height + ', should be ' + img.height);

          var canvas = document.createElement('canvas');
          canvas.width = img.width;
          canvas.height = img.height;
          var ctx = canvas.getContext('2d');
          ctx.drawImage(img, 0, 0);
          var expected = ctx.getImageData(0, 0, img.width, img.height).data;

          var actualUrl = Module.canvas.toDataURL();
          var actualImage = new Image();
          actualImage.onload = function() {
            /*
            document.body.appendChild(img); // for comparisons
            var div = document.createElement('div');
            div.innerHTML = '^=expected, v=actual';
            document.body.appendChild(div);
            document.body.appendChild(actualImage); // to grab it for creating the test reference
            */

            var actualCanvas = document.createElement('canvas');
            actualCanvas.width = actualImage.width;
            actualCanvas.height = actualImage.height;
            var actualCtx = actualCanvas.getContext('2d');
            actualCtx.drawImage(actualImage, 0, 0);
            var actual = actualCtx.getImageData(0, 0, actualImage.width, actualImage.height).data;

            var total = 0;
            var width = img.width;
            var height = img.height;
            for (var x = 0; x < width; x++) {
              for (var y = 0; y < height; y++) {
                total += Math.abs(expected[y*width*4 + x*4 + 0] - actual[y*width*4 + x*4 + 0]);
                total += Math.abs(expected[y*width*4 + x*4 + 1] - actual[y*width*4 + x*4 + 1]);
                total += Math.abs(expected[y*width*4 + x*4 + 2] - actual[y*width*4 + x*4 + 2]);
              }
            }
            var wrong = Math.floor(total / (img.width*img.height*3)); // floor, to allow some margin of error for antialiasing
            // If the main JS file is in a worker, or modularize, then we need to supply our own reporting logic.
            if (typeof reportResultToServer === 'undefined') {
              (function() {
                %s
                reportResultToServer(wrong);
              })();
            } else {
              reportResultToServer(wrong);
            }
          };
          actualImage.src = actualUrl;
        }
        img.src = '%s';
      };

      // Automatically trigger the reftest?
      if (!%s) {
        // Yes, automatically

        Module['postRun'] = doReftest;

        if (typeof WebGLClient !== 'undefined') {
          // trigger reftest from RAF as well, needed for workers where there is no pre|postRun on the main thread
          var realRAF = window.requestAnimationFrame;
          window.requestAnimationFrame = /** @suppress{checkTypes} */ (function(func) {
            realRAF(function() {
              func();
              realRAF(doReftest);
            });
          });

          // trigger reftest from canvas render too, for workers not doing GL
          var realWOM = worker.onmessage;
          worker.onmessage = function(event) {
            realWOM(event);
            if (event.data.target === 'canvas' && event.data.op === 'render') {
              realRAF(doReftest);
            }
          };
        }

      } else {
        // Manually trigger the reftest.

        // The user will call it.
        // Add an event loop iteration to ensure rendering, so users don't need to bother.
        var realDoReftest = doReftest;
        doReftest = function() {
          setTimeout(realDoReftest, 1);
        };
      }
''' % (reporting, basename, int(manually_trigger)))

  def compile_btest(self, args, reporting=Reporting.FULL):
    # Inject support code for reporting results. This adds an include a header so testcases can
    # use REPORT_RESULT, and also adds a cpp file to be compiled alongside the testcase, which
    # contains the implementation of REPORT_RESULT (we can't just include that implementation in
    # the header as there may be multiple files being compiled here).
    args += ['-s', 'IN_TEST_HARNESS']
    if reporting != Reporting.NONE:
      # For basic reporting we inject JS helper funtions to report result back to server.
      args += ['-DEMTEST_PORT_NUMBER=%d' % self.port,
               '--pre-js', test_file('browser_reporting.js')]
      if reporting == Reporting.FULL:
        # If C reporting (i.e. REPORT_RESULT macro) is required
        # also compile in report_result.cpp and forice-include report_result.h
        args += ['-I' + TEST_ROOT,
                 '-include', test_file('report_result.h'),
                 test_file('report_result.cpp')]
    self.run_process([EMCC] + self.get_emcc_args() + args)

  def btest_exit(self, filename, assert_returncode=0, *args, **kwargs):
      """Special case of btest that reports its result solely via exiting
      with a give result code.

      In this case we set EXIT_RUNTIME and we don't need to provide the
      REPORT_RESULT macro to the C code.
      """
      self.set_setting('EXIT_RUNTIME')
      kwargs['reporting'] = Reporting.JS_ONLY
      kwargs['expected'] = 'exit:%d' % assert_returncode
      return self.btest(filename, *args, **kwargs)

  def btest(self, filename, expected=None, reference=None,
            reference_slack=0, manual_reference=False, post_build=None,
            args=None, message='.', also_proxied=False,
            url_suffix='', timeout=None, also_asmjs=False,
            manually_trigger_reftest=False, extra_tries=1,
            reporting=Reporting.FULL):
    assert expected or reference, 'a btest must either expect an output, or have a reference image'
    if args is None:
      args = []
    original_args = args.copy()
    if not os.path.exists(filename):
      filename = test_file(filename)
    if reference:
      self.reference = reference
      expected = [str(i) for i in range(0, reference_slack + 1)]
      self.reftest(test_file(reference), manually_trigger=manually_trigger_reftest)
      if not manual_reference:
        args += ['--pre-js', 'reftest.js', '-s', 'GL_TESTING']
    outfile = 'test.html'
    args += [filename, '-o', outfile]
    # print('all args:', args)
    try_delete(outfile)
    self.compile_btest(args, reporting=reporting)
    self.assertExists(outfile)
    if post_build:
      post_build()
    if not isinstance(expected, list):
      expected = [expected]
    self.run_browser(outfile + url_suffix, message, ['/report_result?' + e for e in expected], timeout=timeout, extra_tries=extra_tries)

    # Tests can opt into being run under asmjs as well
    if 'WASM=0' not in original_args and (also_asmjs or self.also_asmjs):
      print('WASM=0')
      self.btest(filename, expected, reference, reference_slack, manual_reference, post_build,
                 original_args + ['-s', 'WASM=0'], message, also_proxied=False, timeout=timeout)

    if also_proxied:
      print('proxied...')
      if reference:
        assert not manual_reference
        manual_reference = True
        assert not post_build
        post_build = self.post_manual_reftest
      # run proxied
      self.btest(filename, expected, reference, reference_slack, manual_reference, post_build,
                 original_args + ['--proxy-to-worker', '-s', 'GL_TESTING'], message, timeout=timeout)


###################################################################################################


def build_library(name,
                  build_dir,
                  output_dir,
                  generated_libs,
                  configure=['sh', './configure'],
                  make=['make'],
                  make_args=[],
                  cache=None,
                  cache_name=None,
                  env_init={},
                  native=False):
  """Build a library and cache the result.  We build the library file
  once and cache it for all our tests. (We cache in memory since the test
  directory is destroyed and recreated for each test. Note that we cache
  separately for different compilers).  This cache is just during the test
  runner. There is a different concept of caching as well, see |Cache|.
  """

  if type(generated_libs) is not list:
    generated_libs = [generated_libs]
  source_dir = test_file(name.replace('_native', ''))

  project_dir = Path(build_dir, name)
  if os.path.exists(project_dir):
    shutil.rmtree(project_dir)
  # Useful in debugging sometimes to comment this out, and two lines above
  shutil.copytree(source_dir, project_dir)

  generated_libs = [os.path.join(project_dir, lib) for lib in generated_libs]

  if native:
    env = clang_native.get_clang_native_env()
  else:
    env = os.environ.copy()
  env.update(env_init)

  if configure:
    if configure[0] == 'cmake':
      configure = [EMCMAKE] + configure
    else:
      configure = [EMCONFIGURE] + configure
    try:
      with open(os.path.join(project_dir, 'configure_out'), 'w') as out:
        with open(os.path.join(project_dir, 'configure_err'), 'w') as err:
          stdout = out if EM_BUILD_VERBOSE < 2 else None
          stderr = err if EM_BUILD_VERBOSE < 1 else None
          shared.run_process(configure, env=env, stdout=stdout, stderr=stderr,
                             cwd=project_dir)
    except subprocess.CalledProcessError:
      print('-- configure stdout --')
      print(read_file(Path(project_dir, 'configure_out')))
      print('-- end configure stdout --')
      print('-- configure stderr --')
      print(read_file(Path(project_dir, 'configure_err')))
      print('-- end configure stderr --')
      raise
    # if we run configure or cmake we don't then need any kind
    # of special env when we run make below
    env = None

  def open_make_out(mode='r'):
    return open(os.path.join(project_dir, 'make.out'), mode)

  def open_make_err(mode='r'):
    return open(os.path.join(project_dir, 'make.err'), mode)

  if EM_BUILD_VERBOSE >= 3:
    make_args += ['VERBOSE=1']

  try:
    with open_make_out('w') as make_out:
      with open_make_err('w') as make_err:
        stdout = make_out if EM_BUILD_VERBOSE < 2 else None
        stderr = make_err if EM_BUILD_VERBOSE < 1 else None
        shared.run_process(make + make_args, stdout=stdout, stderr=stderr, env=env,
                           cwd=project_dir)
  except subprocess.CalledProcessError:
    with open_make_out() as f:
      print('-- make stdout --')
      print(f.read())
      print('-- end make stdout --')
    with open_make_err() as f:
      print('-- make stderr --')
      print(f.read())
      print('-- end stderr --')
    raise

  if cache is not None:
    cache[cache_name] = []
    for f in generated_libs:
      basename = os.path.basename(f)
      cache[cache_name].append((basename, read_binary(f)))

  return generated_libs
