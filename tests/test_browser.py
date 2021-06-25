# coding=utf-8
# Copyright 2013 The Emscripten Authors.  All rights reserved.
# Emscripten is available under two separate licenses, the MIT license and the
# University of Illinois/NCSA Open Source License.  Both these licenses can be
# found in the LICENSE file.

import argparse
import json
import multiprocessing
import os
import random
import shlex
import shutil
import subprocess
import time
import unittest
import webbrowser
import zlib
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.request import urlopen

from common import BrowserCore, RunnerCore, path_from_root, has_browser, EMTEST_BROWSER, Reporting
from common import create_file, parameterized, ensure_dir, disabled, test_file, WEBIDL_BINDER, EMMAKE
from common import read_file, require_v8
from tools import shared
from tools import system_libs
from tools.shared import EMCC, WINDOWS, FILE_PACKAGER, PIPE
from tools.shared import try_delete


def test_chunked_synchronous_xhr_server(support_byte_ranges, chunkSize, data, checksum, port):
  class ChunkedServerHandler(BaseHTTPRequestHandler):
    def sendheaders(s, extra=[], length=len(data)):
      s.send_response(200)
      s.send_header("Content-Length", str(length))
      s.send_header("Access-Control-Allow-Origin", "http://localhost:%s" % port)
      s.send_header('Cross-Origin-Resource-Policy', 'cross-origin')
      s.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
      s.send_header("Access-Control-Expose-Headers", "Content-Length, Accept-Ranges")
      s.send_header("Content-type", "application/octet-stream")
      if support_byte_ranges:
        s.send_header("Accept-Ranges", "bytes")
      for i in extra:
        s.send_header(i[0], i[1])
      s.end_headers()

    def do_HEAD(s):
      s.sendheaders()

    def do_OPTIONS(s):
      s.sendheaders([("Access-Control-Allow-Headers", "Range")], 0)

    def do_GET(s):
      if s.path == '/':
        s.sendheaders()
      elif not support_byte_ranges:
        s.sendheaders()
        s.wfile.write(data)
      else:
        start, end = s.headers.get("range").split("=")[1].split("-")
        start = int(start)
        end = int(end)
        end = min(len(data) - 1, end)
        length = end - start + 1
        s.sendheaders([], length)
        s.wfile.write(data[start:end + 1])

  # CORS preflight makes OPTIONS requests which we need to account for.
  expectedConns = 22
  httpd = HTTPServer(('localhost', 11111), ChunkedServerHandler)
  for i in range(expectedConns + 1):
    httpd.handle_request()


def shell_with_script(shell_file, output_file, replacement):
  shell = read_file(path_from_root('src', shell_file))
  create_file(output_file, shell.replace('{{{ SCRIPT }}}', replacement))


def is_chrome():
  return EMTEST_BROWSER and 'chrom' in EMTEST_BROWSER.lower()


def no_chrome(note='chrome is not supported'):
  if is_chrome():
    return unittest.skip(note)
  return lambda f: f


def is_firefox():
  return EMTEST_BROWSER and 'firefox' in EMTEST_BROWSER.lower()


def no_firefox(note='firefox is not supported'):
  if is_firefox():
    return unittest.skip(note)
  return lambda f: f


def no_swiftshader(f):
  assert callable(f)

  def decorated(self):
    if is_chrome() and '--use-gl=swiftshader' in EMTEST_BROWSER:
      self.skipTest('not compatible with swiftshader')
    return f(self)

  return decorated


def requires_threads(f):
  assert callable(f)

  def decorated(self, *args, **kwargs):
    if os.environ.get('EMTEST_LACKS_THREAD_SUPPORT'):
      self.skipTest('EMTEST_LACKS_THREAD_SUPPORT is set')
    return f(self, *args, **kwargs)

  return decorated


def requires_asmfs(f):
  assert callable(f)

  def decorated(self, *args, **kwargs):
    # https://github.com/emscripten-core/emscripten/issues/9534
    self.skipTest('ASMFS is looking for a maintainer')
    return f(self, *args, **kwargs)

  return decorated


def also_with_threads(f):
  def decorated(self):
    f(self)
    if not os.environ.get('EMTEST_LACKS_THREAD_SUPPORT'):
      print('(threads)')
      self.emcc_args += ['-pthread']
      f(self)
  return decorated


# Today we only support the wasm backend so any tests that is disabled under the llvm
# backend is always disabled.
# TODO(sbc): Investigate all tests with this decorator and either fix of remove the test.
def no_wasm_backend(note=''):
  assert not callable(note)
  return unittest.skip(note)


requires_graphics_hardware = unittest.skipIf(os.getenv('EMTEST_LACKS_GRAPHICS_HARDWARE'), "This test requires graphics hardware")
requires_sound_hardware = unittest.skipIf(os.getenv('EMTEST_LACKS_SOUND_HARDWARE'), "This test requires sound hardware")
requires_sync_compilation = unittest.skipIf(is_chrome(), "This test requires synchronous compilation, which does not work in Chrome (except for tiny wasms)")
requires_offscreen_canvas = unittest.skipIf(os.getenv('EMTEST_LACKS_OFFSCREEN_CANVAS'), "This test requires a browser with OffscreenCanvas")


class browser(BrowserCore):
  @classmethod
  def setUpClass(cls):
    super().setUpClass()
    cls.browser_timeout = 60
    print()
    print('Running the browser tests. Make sure the browser allows popups from localhost.')
    print()

  def setUp(self):
    super().setUp()
    # avoid various compiler warnings that many browser tests currently generate
    self.emcc_args += [
      '-Wno-pointer-sign',
      '-Wno-int-conversion',
    ]

  def test_sdl1_in_emscripten_nonstrict_mode(self):
    if 'EMCC_STRICT' in os.environ and int(os.environ['EMCC_STRICT']):
      self.skipTest('This test requires being run in non-strict mode (EMCC_STRICT env. variable unset)')
    # TODO: This test is verifying behavior that will be deprecated at some point in the future, remove this test once
    # system JS libraries are no longer automatically linked to anymore.
    self.btest('hello_world_sdl.cpp', reference='htmltest.png')

  def test_sdl1(self):
    self.btest('hello_world_sdl.cpp', reference='htmltest.png', args=['-lSDL', '-lGL'])
    self.btest('hello_world_sdl.cpp', reference='htmltest.png', args=['-s', 'USE_SDL', '-lGL']) # is the default anyhow

  def test_sdl1_es6(self):
    self.btest('hello_world_sdl.cpp', reference='htmltest.png', args=['-s', 'USE_SDL', '-lGL', '-s', 'EXPORT_ES6'])

  # Deliberately named as test_zzz_* to make this test the last one
  # as this test may take the focus away from the main test window
  # by opening a new window and possibly not closing it.
  def test_zzz_html_source_map(self):
    if not has_browser():
      self.skipTest('need a browser')
    cpp_file = 'src.cpp'
    html_file = 'src.html'
    # browsers will try to 'guess' the corresponding original line if a
    # generated line is unmapped, so if we want to make sure that our
    # numbering is correct, we need to provide a couple of 'possible wrong
    # answers'. thus, we add some printf calls so that the cpp file gets
    # multiple mapped lines. in other words, if the program consists of a
    # single 'throw' statement, browsers may just map any thrown exception to
    # that line, because it will be the only mapped line.
    with open(cpp_file, 'w') as f:
      f.write(r'''
      #include <cstdio>

      int main() {
        printf("Starting test\n");
        try {
          throw 42; // line 8
        } catch (int e) { }
        printf("done\n");
        return 0;
      }
      ''')
    # use relative paths when calling emcc, because file:// URIs can only load
    # sourceContent when the maps are relative paths
    try_delete(html_file)
    try_delete(html_file + '.map')
    self.compile_btest(['src.cpp', '-o', 'src.html', '-gsource-map'])
    self.assertExists(html_file)
    self.assertExists('src.wasm.map')
    webbrowser.open_new('file://' + html_file)
    print('''
If manually bisecting:
  Check that you see src.cpp among the page sources.
  Even better, add a breakpoint, e.g. on the printf, then reload, then step
  through and see the print (best to run with EMTEST_SAVE_DIR=1 for the reload).
''')

  def test_emscripten_log(self):
    self.btest_exit(test_file('emscripten_log/emscripten_log.cpp'),
                    args=['--pre-js', path_from_root('src', 'emscripten-source-map.min.js'), '-gsource-map'])

  def test_preload_file(self):
    create_file('somefile.txt', 'load me right before running the code please')
    create_file('.somefile.txt', 'load me right before running the code please')
    create_file('some@file.txt', 'load me right before running the code please')

    absolute_src_path = os.path.abspath('somefile.txt')

    def make_main(path):
      print('make main at', path)
      path = path.replace('\\', '\\\\').replace('"', '\\"') # Escape tricky path name for use inside a C string.
      create_file('main.cpp', r'''
        #include <stdio.h>
        #include <string.h>
        #include <emscripten.h>
        int main() {
          FILE *f = fopen("%s", "r");
          char buf[100];
          fread(buf, 1, 20, f);
          buf[20] = 0;
          fclose(f);
          printf("|%%s|\n", buf);

          int result = !strcmp("load me right before", buf);
          REPORT_RESULT(result);
          return 0;
        }
        ''' % path)

    test_cases = [
      # (source preload-file string, file on target FS to load)
      ("somefile.txt", "somefile.txt"),
      (".somefile.txt@somefile.txt", "somefile.txt"),
      ("./somefile.txt", "somefile.txt"),
      ("somefile.txt@file.txt", "file.txt"),
      ("./somefile.txt@file.txt", "file.txt"),
      ("./somefile.txt@./file.txt", "file.txt"),
      ("somefile.txt@/file.txt", "file.txt"),
      ("somefile.txt@/", "somefile.txt"),
      (absolute_src_path + "@file.txt", "file.txt"),
      (absolute_src_path + "@/file.txt", "file.txt"),
      (absolute_src_path + "@/", "somefile.txt"),
      ("somefile.txt@/directory/file.txt", "/directory/file.txt"),
      ("somefile.txt@/directory/file.txt", "directory/file.txt"),
      (absolute_src_path + "@/directory/file.txt", "directory/file.txt"),
      ("some@@file.txt@other.txt", "other.txt"),
      ("some@@file.txt@some@@otherfile.txt", "some@otherfile.txt")]

    for srcpath, dstpath in test_cases:
      print('Testing', srcpath, dstpath)
      make_main(dstpath)
      self.compile_btest(['main.cpp', '--preload-file', srcpath, '-o', 'page.html'])
      self.run_browser('page.html', 'You should see |load me right before|.', '/report_result?1')
    if WINDOWS:
      # On Windows, the following non-alphanumeric non-control code ASCII characters are supported.
      # The characters <, >, ", |, ?, * are not allowed, because the Windows filesystem doesn't support those.
      tricky_filename = '!#$%&\'()+,-. ;=@[]^_`{}~.txt'
    else:
      # All 7-bit non-alphanumeric non-control code ASCII characters except /, : and \ are allowed.
      tricky_filename = '!#$%&\'()+,-. ;=@[]^_`{}~ "*<>?|.txt'
    create_file(tricky_filename, 'load me right before running the code please')
    make_main(tricky_filename)
    # As an Emscripten-specific feature, the character '@' must be escaped in the form '@@' to not confuse with the 'src@dst' notation.
    self.compile_btest(['main.cpp', '--preload-file', tricky_filename.replace('@', '@@'), '-o', 'page.html'])
    self.run_browser('page.html', 'You should see |load me right before|.', '/report_result?1')

    # By absolute path

    make_main('somefile.txt') # absolute becomes relative
    self.compile_btest(['main.cpp', '--preload-file', absolute_src_path, '-o', 'page.html'])
    self.run_browser('page.html', 'You should see |load me right before|.', '/report_result?1')

    # Test subdirectory handling with asset packaging.
    try_delete('assets')
    ensure_dir('assets/sub/asset1/'.replace('\\', '/'))
    ensure_dir('assets/sub/asset1/.git'.replace('\\', '/')) # Test adding directory that shouldn't exist.
    ensure_dir('assets/sub/asset2/'.replace('\\', '/'))
    create_file('assets/sub/asset1/file1.txt', '''load me right before running the code please''')
    create_file('assets/sub/asset1/.git/shouldnt_be_embedded.txt', '''this file should not get embedded''')
    create_file('assets/sub/asset2/file2.txt', '''load me right before running the code please''')
    absolute_assets_src_path = 'assets'.replace('\\', '/')

    def make_main_two_files(path1, path2, nonexistingpath):
      create_file('main.cpp', r'''
        #include <stdio.h>
        #include <string.h>
        #include <emscripten.h>
        int main() {
          FILE *f = fopen("%s", "r");
          char buf[100];
          fread(buf, 1, 20, f);
          buf[20] = 0;
          fclose(f);
          printf("|%%s|\n", buf);

          int result = !strcmp("load me right before", buf);

          f = fopen("%s", "r");
          if (f == NULL)
            result = 0;
          fclose(f);

          f = fopen("%s", "r");
          if (f != NULL)
            result = 0;

          REPORT_RESULT(result);
          return 0;
        }
      ''' % (path1, path2, nonexistingpath))

    test_cases = [
      # (source directory to embed, file1 on target FS to load, file2 on target FS to load, name of a file that *shouldn't* exist on VFS)
      ("assets", "assets/sub/asset1/file1.txt", "assets/sub/asset2/file2.txt", "assets/sub/asset1/.git/shouldnt_be_embedded.txt"),
      ("assets/", "assets/sub/asset1/file1.txt", "assets/sub/asset2/file2.txt", "assets/sub/asset1/.git/shouldnt_be_embedded.txt"),
      ("assets@/", "/sub/asset1/file1.txt", "/sub/asset2/file2.txt", "/sub/asset1/.git/shouldnt_be_embedded.txt"),
      ("assets/@/", "/sub/asset1/file1.txt", "/sub/asset2/file2.txt", "/sub/asset1/.git/shouldnt_be_embedded.txt"),
      ("assets@./", "/sub/asset1/file1.txt", "/sub/asset2/file2.txt", "/sub/asset1/.git/shouldnt_be_embedded.txt"),
      (absolute_assets_src_path + "@/", "/sub/asset1/file1.txt", "/sub/asset2/file2.txt", "/sub/asset1/.git/shouldnt_be_embedded.txt"),
      (absolute_assets_src_path + "@/assets", "/assets/sub/asset1/file1.txt", "/assets/sub/asset2/file2.txt", "assets/sub/asset1/.git/shouldnt_be_embedded.txt")]

    for test in test_cases:
      (srcpath, dstpath1, dstpath2, nonexistingpath) = test
      make_main_two_files(dstpath1, dstpath2, nonexistingpath)
      print(srcpath)
      self.compile_btest(['main.cpp', '--preload-file', srcpath, '--exclude-file', '*/.*', '-o', 'page.html'])
      self.run_browser('page.html', 'You should see |load me right before|.', '/report_result?1')

    # Should still work with -o subdir/..

    make_main('somefile.txt') # absolute becomes relative
    ensure_dir('dirrey')
    self.compile_btest(['main.cpp', '--preload-file', absolute_src_path, '-o', 'dirrey/page.html'])
    self.run_browser('dirrey/page.html', 'You should see |load me right before|.', '/report_result?1')

    # With FS.preloadFile

    create_file('pre.js', '''
      Module.preRun = function() {
        FS.createPreloadedFile('/', 'someotherfile.txt', 'somefile.txt', true, false); // we need --use-preload-plugins for this.
      };
    ''')
    make_main('someotherfile.txt')
    self.compile_btest(['main.cpp', '--pre-js', 'pre.js', '-o', 'page.html', '--use-preload-plugins'])
    self.run_browser('page.html', 'You should see |load me right before|.', '/report_result?1')

  # Tests that user .html shell files can manually download .data files created with --preload-file cmdline.
  def test_preload_file_with_manual_data_download(self):
    src = test_file('manual_download_data.cpp')

    create_file('file.txt', '''Hello!''')

    self.compile_btest([src, '-o', 'manual_download_data.js', '--preload-file', 'file.txt@/file.txt'])
    shutil.copyfile(test_file('manual_download_data.html'), 'manual_download_data.html')
    self.run_browser('manual_download_data.html', 'Hello!', '/report_result?1')

  # Tests that if the output files have single or double quotes in them, that it will be handled by
  # correctly escaping the names.
  def test_output_file_escaping(self):
    tricky_part = '\'' if WINDOWS else '\' and \"' # On Windows, files/directories may not contain a double quote character. On non-Windowses they can, so test that.

    d = 'dir with ' + tricky_part
    abs_d = os.path.abspath(d)
    ensure_dir(abs_d)
    txt = 'file with ' + tricky_part + '.txt'
    abs_txt = os.path.join(abs_d, txt)
    open(abs_txt, 'w').write('load me right before')

    cpp = os.path.join(d, 'file with ' + tricky_part + '.cpp')
    open(cpp, 'w').write(r'''
      #include <stdio.h>
      #include <string.h>
      #include <emscripten.h>
      int main() {
        FILE *f = fopen("%s", "r");
        char buf[100];
        fread(buf, 1, 20, f);
        buf[20] = 0;
        fclose(f);
        printf("|%%s|\n", buf);
        int result = !strcmp("|load me right before|", buf);
        REPORT_RESULT(result);
        return 0;
      }
    ''' % (txt.replace('\'', '\\\'').replace('\"', '\\"')))

    data_file = os.path.join(abs_d, 'file with ' + tricky_part + '.data')
    data_js_file = os.path.join(abs_d, 'file with ' + tricky_part + '.js')
    self.run_process([FILE_PACKAGER, data_file, '--use-preload-cache', '--indexedDB-name=testdb', '--preload', abs_txt + '@' + txt, '--js-output=' + data_js_file])
    page_file = os.path.join(d, 'file with ' + tricky_part + '.html')
    abs_page_file = os.path.abspath(page_file)
    self.compile_btest([cpp, '--pre-js', data_js_file, '-o', abs_page_file, '-s', 'FORCE_FILESYSTEM'])
    self.run_browser(page_file, '|load me right before|.', '/report_result?0')

  @parameterized({
    '0': (0,),
    '1mb': (1 * 1024 * 1024,),
    '100mb': (100 * 1024 * 1024,),
    '150mb': (150 * 1024 * 1024,),
  })
  def test_preload_caching(self, extra_size):
    create_file('main.cpp', r'''
      #include <stdio.h>
      #include <string.h>
      #include <emscripten.h>

      extern "C" {
        extern int checkPreloadResults();
      }

      int main(int argc, char** argv) {
        FILE *f = fopen("%s", "r");
        char buf[100];
        fread(buf, 1, 20, f);
        buf[20] = 0;
        fclose(f);
        printf("|%%s|\n", buf);

        int result = 0;

        result += !strcmp("load me right before", buf);
        result += checkPreloadResults();

        REPORT_RESULT(result);
        return 0;
      }
    ''' % 'somefile.txt')

    create_file('test.js', '''
      mergeInto(LibraryManager.library, {
        checkPreloadResults: function() {
          var cached = 0;
          var packages = Object.keys(Module['preloadResults']);
          packages.forEach(function(package) {
            var fromCache = Module['preloadResults'][package]['fromCache'];
            if (fromCache)
              ++ cached;
          });
          return cached;
        }
      });
    ''')

    # test caching of various sizes, including sizes higher than 128MB which is
    # chrome's limit on IndexedDB item sizes, see
    # https://cs.chromium.org/chromium/src/content/renderer/indexed_db/webidbdatabase_impl.cc?type=cs&q=%22The+serialized+value+is+too+large%22&sq=package:chromium&g=0&l=177
    # https://cs.chromium.org/chromium/src/out/Debug/gen/third_party/blink/public/mojom/indexeddb/indexeddb.mojom.h?type=cs&sq=package:chromium&g=0&l=60
    if is_chrome() and extra_size >= 100 * 1024 * 1024:
      self.skipTest('chrome bug')
    create_file('somefile.txt', '''load me right before running the code please''' + ('_' * extra_size))
    print('size:', os.path.getsize('somefile.txt'))
    self.compile_btest(['main.cpp', '--use-preload-cache', '--js-library', 'test.js', '--preload-file', 'somefile.txt', '-o', 'page.html', '-s', 'ALLOW_MEMORY_GROWTH'])
    self.run_browser('page.html', 'You should see |load me right before|.', '/report_result?1')
    self.run_browser('page.html', 'You should see |load me right before|.', '/report_result?2')

  def test_preload_caching_indexeddb_name(self):
    create_file('somefile.txt', '''load me right before running the code please''')

    def make_main(path):
      print(path)
      create_file('main.cpp', r'''
        #include <stdio.h>
        #include <string.h>
        #include <emscripten.h>

        extern "C" {
          extern int checkPreloadResults();
        }

        int main(int argc, char** argv) {
          FILE *f = fopen("%s", "r");
          char buf[100];
          fread(buf, 1, 20, f);
          buf[20] = 0;
          fclose(f);
          printf("|%%s|\n", buf);

          int result = 0;

          result += !strcmp("load me right before", buf);
          result += checkPreloadResults();

          REPORT_RESULT(result);
          return 0;
        }
      ''' % path)

    create_file('test.js', '''
      mergeInto(LibraryManager.library, {
        checkPreloadResults: function() {
          var cached = 0;
          var packages = Object.keys(Module['preloadResults']);
          packages.forEach(function(package) {
            var fromCache = Module['preloadResults'][package]['fromCache'];
            if (fromCache)
              ++ cached;
          });
          return cached;
        }
      });
    ''')

    make_main('somefile.txt')
    self.run_process([FILE_PACKAGER, 'somefile.data', '--use-preload-cache', '--indexedDB-name=testdb', '--preload', 'somefile.txt', '--js-output=' + 'somefile.js'])
    self.compile_btest(['main.cpp', '--js-library', 'test.js', '--pre-js', 'somefile.js', '-o', 'page.html', '-s', 'FORCE_FILESYSTEM'])
    self.run_browser('page.html', 'You should see |load me right before|.', '/report_result?1')
    self.run_browser('page.html', 'You should see |load me right before|.', '/report_result?2')

  def test_multifile(self):
    # a few files inside a directory
    ensure_dir('subdirr/moar')
    create_file('subdirr/data1.txt', '1214141516171819')
    create_file('subdirr/moar/data2.txt', '3.14159265358979')
    create_file('main.cpp', r'''
      #include <stdio.h>
      #include <string.h>
      #include <emscripten.h>
      int main() {
        char buf[17];

        FILE *f = fopen("subdirr/data1.txt", "r");
        fread(buf, 1, 16, f);
        buf[16] = 0;
        fclose(f);
        printf("|%s|\n", buf);
        int result = !strcmp("1214141516171819", buf);

        FILE *f2 = fopen("subdirr/moar/data2.txt", "r");
        fread(buf, 1, 16, f2);
        buf[16] = 0;
        fclose(f2);
        printf("|%s|\n", buf);
        result = result && !strcmp("3.14159265358979", buf);

        REPORT_RESULT(result);
        return 0;
      }
    ''')

    # by individual files
    self.compile_btest(['main.cpp', '--preload-file', 'subdirr/data1.txt', '--preload-file', 'subdirr/moar/data2.txt', '-o', 'page.html'])
    self.run_browser('page.html', 'You should see two cool numbers', '/report_result?1')
    os.remove('page.html')

    # by directory, and remove files to make sure
    self.compile_btest(['main.cpp', '--preload-file', 'subdirr', '-o', 'page.html'])
    shutil.rmtree('subdirr')
    self.run_browser('page.html', 'You should see two cool numbers', '/report_result?1')

  def test_custom_file_package_url(self):
    # a few files inside a directory
    ensure_dir('subdirr')
    ensure_dir('cdn')
    create_file(Path('subdirr/data1.txt'), '1214141516171819')
    # change the file package base dir to look in a "cdn". note that normally
    # you would add this in your own custom html file etc., and not by
    # modifying the existing shell in this manner
    default_shell = read_file(path_from_root('src', 'shell.html'))
    create_file('shell.html', default_shell.replace('var Module = {', '''
    var Module = {
      locateFile: function(path, prefix) {
        if (path.endsWith(".wasm")) {
           return prefix + path;
        } else {
           return "cdn/" + path;
        }
      },
    '''))
    create_file('main.cpp', r'''
      #include <stdio.h>
      #include <string.h>
      #include <emscripten.h>
      int main() {
        char buf[17];

        FILE *f = fopen("subdirr/data1.txt", "r");
        fread(buf, 1, 16, f);
        buf[16] = 0;
        fclose(f);
        printf("|%s|\n", buf);
        int result = !strcmp("1214141516171819", buf);

        REPORT_RESULT(result);
        return 0;
      }
    ''')

    self.compile_btest(['main.cpp', '--shell-file', 'shell.html', '--preload-file', 'subdirr/data1.txt', '-o', 'test.html'])
    shutil.move('test.data', Path('cdn/test.data'))
    self.run_browser('test.html', '', '/report_result?1')

  def test_missing_data_throws_error(self):
    def setup(assetLocalization):
      self.clear()
      create_file('data.txt', 'data')
      create_file('main.cpp', r'''
        #include <stdio.h>
        #include <string.h>
        #include <emscripten.h>
        int main() {
          // This code should never be executed in terms of missing required dependency file.
          REPORT_RESULT(0);
          return 0;
        }
      ''')
      create_file('on_window_error_shell.html', r'''
      <html>
          <center><canvas id='canvas' width='256' height='256'></canvas></center>
          <hr><div id='output'></div><hr>
          <script type='text/javascript'>
            window.onerror = function(error) {
              window.onerror = null;
              var result = error.indexOf("test.data") >= 0 ? 1 : 0;
              var xhr = new XMLHttpRequest();
              xhr.open('GET', 'http://localhost:8888/report_result?' + result, true);
              xhr.send();
              setTimeout(function() { window.close() }, 1000);
            }
            var Module = {
              locateFile: function (path, prefix) {if (path.endsWith(".wasm")) {return prefix + path;} else {return "''' + assetLocalization + r'''" + path;}},
              print: (function() {
                var element = document.getElementById('output');
                return function(text) { element.innerHTML += text.replace('\n', '<br>', 'g') + '<br>';};
              })(),
              canvas: document.getElementById('canvas')
            };
          </script>
          {{{ SCRIPT }}}
        </body>
      </html>''')

    def test():
      # test test missing file should run xhr.onload with status different than 200, 304 or 206
      setup("")
      self.compile_btest(['main.cpp', '--shell-file', 'on_window_error_shell.html', '--preload-file', 'data.txt', '-o', 'test.html'])
      shutil.move('test.data', 'missing.data')
      self.run_browser('test.html', '', '/report_result?1')

      # test unknown protocol should go through xhr.onerror
      setup("unknown_protocol://")
      self.compile_btest(['main.cpp', '--shell-file', 'on_window_error_shell.html', '--preload-file', 'data.txt', '-o', 'test.html'])
      self.run_browser('test.html', '', '/report_result?1')

      # test wrong protocol and port
      setup("https://localhost:8800/")
      self.compile_btest(['main.cpp', '--shell-file', 'on_window_error_shell.html', '--preload-file', 'data.txt', '-o', 'test.html'])
      self.run_browser('test.html', '', '/report_result?1')

    test()

    # TODO: CORS, test using a full url for locateFile
    # create_file('shell.html', read_file(path_from_root('src', 'shell.html')).replace('var Module = {', 'var Module = { locateFile: function (path) {return "http:/localhost:8888/cdn/" + path;}, '))
    # test()

  def test_dev_random(self):
    self.btest(Path('filesystem/dev_random.cpp'), expected='0')

  def test_sdl_swsurface(self):
    self.btest('sdl_swsurface.c', args=['-lSDL', '-lGL'], expected='1')

  def test_sdl_surface_lock_opts(self):
    # Test Emscripten-specific extensions to optimize SDL_LockSurface and SDL_UnlockSurface.
    self.btest('hello_world_sdl.cpp', reference='htmltest.png', message='You should see "hello, world!" and a colored cube.', args=['-DTEST_SDL_LOCK_OPTS', '-lSDL', '-lGL'])

  def test_sdl_image(self):
    # load an image file, get pixel data. Also O2 coverage for --preload-file, and memory-init
    shutil.copyfile(test_file('screenshot.jpg'), 'screenshot.jpg')
    src = test_file('sdl_image.c')

    for mem in [0, 1]:
      for dest, dirname, basename in [('screenshot.jpg', '/', 'screenshot.jpg'),
                                      ('screenshot.jpg@/assets/screenshot.jpg', '/assets', 'screenshot.jpg')]:
        self.compile_btest([
          src, '-o', 'page.html', '-O2', '-lSDL', '-lGL', '--memory-init-file', str(mem),
          '--preload-file', dest, '-DSCREENSHOT_DIRNAME="' + dirname + '"', '-DSCREENSHOT_BASENAME="' + basename + '"', '--use-preload-plugins'
        ])
        self.run_browser('page.html', '', '/report_result?600')

  def test_sdl_image_jpeg(self):
    shutil.copyfile(test_file('screenshot.jpg'), 'screenshot.jpeg')
    src = test_file('sdl_image.c')
    self.compile_btest([
      src, '-o', 'page.html', '-lSDL', '-lGL',
      '--preload-file', 'screenshot.jpeg', '-DSCREENSHOT_DIRNAME="/"', '-DSCREENSHOT_BASENAME="screenshot.jpeg"', '--use-preload-plugins'
    ])
    self.run_browser('page.html', '', '/report_result?600')

  def test_sdl_image_prepare(self):
    # load an image file, get pixel data.
    shutil.copyfile(test_file('screenshot.jpg'), 'screenshot.not')
    self.btest('sdl_image_prepare.c', reference='screenshot.jpg', args=['--preload-file', 'screenshot.not', '-lSDL', '-lGL'], also_proxied=True, manually_trigger_reftest=True)

  @parameterized({
    '': ([],),
    # add testing for closure on preloaded files + ENVIRONMENT=web (we must not
    # emit any node.js code here, see
    # https://github.com/emscripten-core/emscripten/issues/14486
    'closure_webonly': (['--closure', '1', '-s', 'ENVIRONMENT=web'],)
  })
  def test_sdl_image_prepare_data(self, args):
    # load an image file, get pixel data.
    shutil.copyfile(test_file('screenshot.jpg'), 'screenshot.not')
    self.btest('sdl_image_prepare_data.c', reference='screenshot.jpg', args=['--preload-file', 'screenshot.not', '-lSDL', '-lGL'] + args, manually_trigger_reftest=True)

  def test_sdl_image_must_prepare(self):
    # load an image file, get pixel data.
    shutil.copyfile(test_file('screenshot.jpg'), 'screenshot.jpg')
    self.btest('sdl_image_must_prepare.c', reference='screenshot.jpg', args=['--preload-file', 'screenshot.jpg', '-lSDL', '-lGL'], manually_trigger_reftest=True)

  def test_sdl_stb_image(self):
    # load an image file, get pixel data.
    shutil.copyfile(test_file('screenshot.jpg'), 'screenshot.not')
    self.btest('sdl_stb_image.c', reference='screenshot.jpg', args=['-s', 'STB_IMAGE', '--preload-file', 'screenshot.not', '-lSDL', '-lGL'])

  def test_sdl_stb_image_bpp(self):
    # load grayscale image without alpha
    self.clear()
    shutil.copyfile(test_file('sdl-stb-bpp1.png'), 'screenshot.not')
    self.btest('sdl_stb_image.c', reference='sdl-stb-bpp1.png', args=['-s', 'STB_IMAGE', '--preload-file', 'screenshot.not', '-lSDL', '-lGL'])

    # load grayscale image with alpha
    self.clear()
    shutil.copyfile(test_file('sdl-stb-bpp2.png'), 'screenshot.not')
    self.btest('sdl_stb_image.c', reference='sdl-stb-bpp2.png', args=['-s', 'STB_IMAGE', '--preload-file', 'screenshot.not', '-lSDL', '-lGL'])

    # load RGB image
    self.clear()
    shutil.copyfile(test_file('sdl-stb-bpp3.png'), 'screenshot.not')
    self.btest('sdl_stb_image.c', reference='sdl-stb-bpp3.png', args=['-s', 'STB_IMAGE', '--preload-file', 'screenshot.not', '-lSDL', '-lGL'])

    # load RGBA image
    self.clear()
    shutil.copyfile(test_file('sdl-stb-bpp4.png'), 'screenshot.not')
    self.btest('sdl_stb_image.c', reference='sdl-stb-bpp4.png', args=['-s', 'STB_IMAGE', '--preload-file', 'screenshot.not', '-lSDL', '-lGL'])

  def test_sdl_stb_image_data(self):
    # load an image file, get pixel data.
    shutil.copyfile(test_file('screenshot.jpg'), 'screenshot.not')
    self.btest('sdl_stb_image_data.c', reference='screenshot.jpg', args=['-s', 'STB_IMAGE', '--preload-file', 'screenshot.not', '-lSDL', '-lGL'])

  def test_sdl_stb_image_cleanup(self):
    shutil.copyfile(test_file('screenshot.jpg'), 'screenshot.not')
    self.btest('sdl_stb_image_cleanup.c', expected='0', args=['-s', 'STB_IMAGE', '--preload-file', 'screenshot.not', '-lSDL', '-lGL', '--memoryprofiler'])

  def test_sdl_canvas(self):
    self.clear()
    self.btest('sdl_canvas.c', expected='1', args=['-s', 'LEGACY_GL_EMULATION', '-lSDL', '-lGL'])
    # some extra coverage
    self.clear()
    self.btest('sdl_canvas.c', expected='1', args=['-s', 'LEGACY_GL_EMULATION', '-O0', '-s', 'SAFE_HEAP', '-lSDL', '-lGL'])
    self.clear()
    self.btest('sdl_canvas.c', expected='1', args=['-s', 'LEGACY_GL_EMULATION', '-O2', '-s', 'SAFE_HEAP', '-lSDL', '-lGL'])

  def post_manual_reftest(self, reference=None):
    self.reftest(test_file(self.reference if reference is None else reference))

    html = read_file('test.html')
    html = html.replace('</body>', '''
<script>
function assert(x, y) { if (!x) throw 'assertion failed ' + y }
%s

var windowClose = window.close;
window.close = function() {
  // wait for rafs to arrive and the screen to update before reftesting
  setTimeout(function() {
    doReftest();
    setTimeout(windowClose, 5000);
  }, 1000);
};
</script>
</body>''' % read_file('reftest.js'))
    create_file('test.html', html)

  def test_sdl_canvas_proxy(self):
    create_file('data.txt', 'datum')
    self.btest('sdl_canvas_proxy.c', reference='sdl_canvas_proxy.png', args=['--proxy-to-worker', '--preload-file', 'data.txt', '-lSDL', '-lGL'], manual_reference=True, post_build=self.post_manual_reftest)

  @requires_graphics_hardware
  def test_glgears_proxy_jstarget(self):
    # test .js target with --proxy-worker; emits 2 js files, client and worker
    self.compile_btest([test_file('hello_world_gles_proxy.c'), '-o', 'test.js', '--proxy-to-worker', '-s', 'GL_TESTING', '-lGL', '-lglut'])
    shell_with_script('shell_minimal.html', 'test.html', '<script src="test.js"></script>')
    self.post_manual_reftest('gears.png')
    self.run_browser('test.html', None, '/report_result?0')

  def test_sdl_canvas_alpha(self):
    # N.B. On Linux with Intel integrated graphics cards, this test needs Firefox 49 or newer.
    # See https://github.com/emscripten-core/emscripten/issues/4069.
    create_file('flag_0.js', '''
      Module['arguments'] = ['-0'];
    ''')

    self.btest('sdl_canvas_alpha.c', args=['-lSDL', '-lGL'], reference='sdl_canvas_alpha.png', reference_slack=12)
    self.btest('sdl_canvas_alpha.c', args=['--pre-js', 'flag_0.js', '-lSDL', '-lGL'], reference='sdl_canvas_alpha_flag_0.png', reference_slack=12)

  def test_sdl_key(self):
    for delay in [0, 1]:
      for defines in [
        [],
        ['-DTEST_EMSCRIPTEN_SDL_SETEVENTHANDLER']
      ]:
        for async_ in [
          [],
          ['-DTEST_SLEEP', '-s', 'ASSERTIONS', '-s', 'SAFE_HEAP', '-s', 'ASYNCIFY']
        ]:
          print(delay, defines, async_)

          create_file('pre.js', '''
            function keydown(c) {
             %s
              var event = new KeyboardEvent("keydown", { 'keyCode': c, 'charCode': c, 'view': window, 'bubbles': true, 'cancelable': true });
              document.dispatchEvent(event);
             %s
            }

            function keyup(c) {
             %s
              var event = new KeyboardEvent("keyup", { 'keyCode': c, 'charCode': c, 'view': window, 'bubbles': true, 'cancelable': true });
              document.dispatchEvent(event);
             %s
            }
          ''' % ('setTimeout(function() {' if delay else '', '}, 1);' if delay else '', 'setTimeout(function() {' if delay else '', '}, 1);' if delay else ''))
          self.compile_btest([test_file('sdl_key.c'), '-o', 'page.html'] + defines + async_ + ['--pre-js', 'pre.js', '-s', 'EXPORTED_FUNCTIONS=_main', '-lSDL', '-lGL'])
          self.run_browser('page.html', '', '/report_result?223092870')

  def test_sdl_key_proxy(self):
    create_file('pre.js', '''
      var Module = {};
      Module.postRun = function() {
        function doOne() {
          Module._one();
          setTimeout(doOne, 1000/60);
        }
        setTimeout(doOne, 1000/60);
      }
    ''')

    def post():
      html = read_file('test.html')
      html = html.replace('</body>', '''
<script>
function keydown(c) {
  var event = new KeyboardEvent("keydown", { 'keyCode': c, 'charCode': c, 'view': window, 'bubbles': true, 'cancelable': true });
  document.dispatchEvent(event);
}

function keyup(c) {
  var event = new KeyboardEvent("keyup", { 'keyCode': c, 'charCode': c, 'view': window, 'bubbles': true, 'cancelable': true });
  document.dispatchEvent(event);
}

keydown(1250);keydown(38);keyup(38);keyup(1250); // alt, up
keydown(1248);keydown(1249);keydown(40);keyup(40);keyup(1249);keyup(1248); // ctrl, shift, down
keydown(37);keyup(37); // left
keydown(39);keyup(39); // right
keydown(65);keyup(65); // a
keydown(66);keyup(66); // b
keydown(100);keyup(100); // trigger the end

</script>
</body>''')
      create_file('test.html', html)

    self.btest('sdl_key_proxy.c', '223092870', args=['--proxy-to-worker', '--pre-js', 'pre.js', '-s', 'EXPORTED_FUNCTIONS=_main,_one', '-lSDL', '-lGL'], manual_reference=True, post_build=post)

  def test_canvas_focus(self):
    self.btest('canvas_focus.c', '1')

  def test_keydown_preventdefault_proxy(self):
    def post():
      html = read_file('test.html')
      html = html.replace('</body>', '''
<script>
function keydown(c) {
  var event = new KeyboardEvent("keydown", { 'keyCode': c, 'charCode': c, 'view': window, 'bubbles': true, 'cancelable': true });
  return document.dispatchEvent(event);
}

function keypress(c) {
  var event = new KeyboardEvent("keypress", { 'keyCode': c, 'charCode': c, 'view': window, 'bubbles': true, 'cancelable': true });
  return document.dispatchEvent(event);
}

function keyup(c) {
  var event = new KeyboardEvent("keyup", { 'keyCode': c, 'charCode': c, 'view': window, 'bubbles': true, 'cancelable': true });
  return document.dispatchEvent(event);
}

function sendKey(c) {
  // Simulate the sending of the keypress event when the
  // prior keydown event is not prevent defaulted.
  if (keydown(c) === false) {
    console.log('keydown prevent defaulted, NOT sending keypress!!!');
  } else {
    keypress(c);
  }
  keyup(c);
}

// Send 'a'. Simulate the sending of the keypress event when the
// prior keydown event is not prevent defaulted.
sendKey(65);

// Send backspace. Keypress should not be sent over as default handling of
// the Keydown event should be prevented.
sendKey(8);

keydown(100);keyup(100); // trigger the end
</script>
</body>''')

      create_file('test.html', html)

    self.btest('keydown_preventdefault_proxy.cpp', '300', args=['--proxy-to-worker', '-s', 'EXPORTED_FUNCTIONS=_main'], manual_reference=True, post_build=post)

  def test_sdl_text(self):
    create_file('pre.js', '''
      Module.postRun = function() {
        function doOne() {
          Module._one();
          setTimeout(doOne, 1000/60);
        }
        setTimeout(doOne, 1000/60);
      }

      function simulateKeyEvent(c) {
        var event = new KeyboardEvent("keypress", { 'keyCode': c, 'charCode': c, 'view': window, 'bubbles': true, 'cancelable': true });
        document.body.dispatchEvent(event);
      }
    ''')

    self.compile_btest([test_file('sdl_text.c'), '-o', 'page.html', '--pre-js', 'pre.js', '-s', 'EXPORTED_FUNCTIONS=_main,_one', '-lSDL', '-lGL'])
    self.run_browser('page.html', '', '/report_result?1')

  def test_sdl_mouse(self):
    create_file('pre.js', '''
      function simulateMouseEvent(x, y, button) {
        var event = document.createEvent("MouseEvents");
        if (button >= 0) {
          var event1 = document.createEvent("MouseEvents");
          event1.initMouseEvent('mousedown', true, true, window,
                     1, Module['canvas'].offsetLeft + x, Module['canvas'].offsetTop + y, Module['canvas'].offsetLeft + x, Module['canvas'].offsetTop + y,
                     0, 0, 0, 0,
                     button, null);
          Module['canvas'].dispatchEvent(event1);
          var event2 = document.createEvent("MouseEvents");
          event2.initMouseEvent('mouseup', true, true, window,
                     1, Module['canvas'].offsetLeft + x, Module['canvas'].offsetTop + y, Module['canvas'].offsetLeft + x, Module['canvas'].offsetTop + y,
                     0, 0, 0, 0,
                     button, null);
          Module['canvas'].dispatchEvent(event2);
        } else {
          var event1 = document.createEvent("MouseEvents");
          event1.initMouseEvent('mousemove', true, true, window,
                     0, Module['canvas'].offsetLeft + x, Module['canvas'].offsetTop + y, Module['canvas'].offsetLeft + x, Module['canvas'].offsetTop + y,
                     0, 0, 0, 0,
                     0, null);
          Module['canvas'].dispatchEvent(event1);
        }
      }
      window['simulateMouseEvent'] = simulateMouseEvent;
    ''')

    self.compile_btest([test_file('sdl_mouse.c'), '-O2', '--minify=0', '-o', 'page.html', '--pre-js', 'pre.js', '-lSDL', '-lGL'])
    self.run_browser('page.html', '', '/report_result?1')

  def test_sdl_mouse_offsets(self):
    create_file('pre.js', '''
      function simulateMouseEvent(x, y, button) {
        var event = document.createEvent("MouseEvents");
        if (button >= 0) {
          var event1 = document.createEvent("MouseEvents");
          event1.initMouseEvent('mousedown', true, true, window,
                     1, x, y, x, y,
                     0, 0, 0, 0,
                     button, null);
          Module['canvas'].dispatchEvent(event1);
          var event2 = document.createEvent("MouseEvents");
          event2.initMouseEvent('mouseup', true, true, window,
                     1, x, y, x, y,
                     0, 0, 0, 0,
                     button, null);
          Module['canvas'].dispatchEvent(event2);
        } else {
          var event1 = document.createEvent("MouseEvents");
          event1.initMouseEvent('mousemove', true, true, window,
                     0, x, y, x, y,
                     0, 0, 0, 0,
                     0, null);
          Module['canvas'].dispatchEvent(event1);
        }
      }
      window['simulateMouseEvent'] = simulateMouseEvent;
    ''')
    create_file('page.html', '''
      <html>
        <head>
          <style type="text/css">
            html, body { margin: 0; padding: 0; }
            #container {
              position: absolute;
              left: 5px; right: 0;
              top: 5px; bottom: 0;
            }
            #canvas {
              position: absolute;
              left: 0; width: 600px;
              top: 0; height: 450px;
            }
            textarea {
              margin-top: 500px;
              margin-left: 5px;
              width: 600px;
            }
          </style>
        </head>
        <body>
          <div id="container">
            <canvas id="canvas"></canvas>
          </div>
          <textarea id="output" rows="8"></textarea>
          <script type="text/javascript">
            var Module = {
              canvas: document.getElementById('canvas'),
              print: (function() {
                var element = document.getElementById('output');
                element.value = ''; // clear browser cache
                return function(text) {
                  if (arguments.length > 1) text = Array.prototype.slice.call(arguments).join(' ');
                  element.value += text + "\\n";
                  element.scrollTop = element.scrollHeight; // focus on bottom
                };
              })()
            };
          </script>
          <script type="text/javascript" src="sdl_mouse.js"></script>
        </body>
      </html>
    ''')

    self.compile_btest([test_file('sdl_mouse.c'), '-DTEST_SDL_MOUSE_OFFSETS', '-O2', '--minify=0', '-o', 'sdl_mouse.js', '--pre-js', 'pre.js', '-lSDL', '-lGL'])
    self.run_browser('page.html', '', '/report_result?1')

  def test_glut_touchevents(self):
    self.btest('glut_touchevents.c', '1', args=['-lglut'])

  def test_glut_wheelevents(self):
    self.btest('glut_wheelevents.c', '1', args=['-lglut'])

  @requires_graphics_hardware
  def test_glut_glutget_no_antialias(self):
    self.btest('glut_glutget.c', '1', args=['-lglut', '-lGL'])
    self.btest('glut_glutget.c', '1', args=['-lglut', '-lGL', '-DDEPTH_ACTIVATED', '-DSTENCIL_ACTIVATED', '-DALPHA_ACTIVATED'])

  # This test supersedes the one above, but it's skipped in the CI because anti-aliasing is not well supported by the Mesa software renderer.
  @requires_graphics_hardware
  def test_glut_glutget(self):
    self.btest('glut_glutget.c', '1', args=['-lglut', '-lGL'])
    self.btest('glut_glutget.c', '1', args=['-lglut', '-lGL', '-DAA_ACTIVATED', '-DDEPTH_ACTIVATED', '-DSTENCIL_ACTIVATED', '-DALPHA_ACTIVATED'])

  def test_sdl_joystick_1(self):
    # Generates events corresponding to the Working Draft of the HTML5 Gamepad API.
    # http://www.w3.org/TR/2012/WD-gamepad-20120529/#gamepad-interface
    create_file('pre.js', '''
      var gamepads = [];
      // Spoof this function.
      navigator['getGamepads'] = function() {
        return gamepads;
      };
      window['addNewGamepad'] = function(id, numAxes, numButtons) {
        var index = gamepads.length;
        gamepads.push({
          axes: new Array(numAxes),
          buttons: new Array(numButtons),
          id: id,
          index: index
        });
        var i;
        for (i = 0; i < numAxes; i++) gamepads[index].axes[i] = 0;
        for (i = 0; i < numButtons; i++) gamepads[index].buttons[i] = 0;
      };
      window['simulateGamepadButtonDown'] = function (index, button) {
        gamepads[index].buttons[button] = 1;
      };
      window['simulateGamepadButtonUp'] = function (index, button) {
        gamepads[index].buttons[button] = 0;
      };
      window['simulateAxisMotion'] = function (index, axis, value) {
        gamepads[index].axes[axis] = value;
      };
    ''')

    self.compile_btest([test_file('sdl_joystick.c'), '-O2', '--minify=0', '-o', 'page.html', '--pre-js', 'pre.js', '-lSDL', '-lGL'])
    self.run_browser('page.html', '', '/report_result?2')

  def test_sdl_joystick_2(self):
    # Generates events corresponding to the Editor's Draft of the HTML5 Gamepad API.
    # https://dvcs.w3.org/hg/gamepad/raw-file/default/gamepad.html#idl-def-Gamepad
    create_file('pre.js', '''
      var gamepads = [];
      // Spoof this function.
      navigator['getGamepads'] = function() {
        return gamepads;
      };
      window['addNewGamepad'] = function(id, numAxes, numButtons) {
        var index = gamepads.length;
        gamepads.push({
          axes: new Array(numAxes),
          buttons: new Array(numButtons),
          id: id,
          index: index
        });
        var i;
        for (i = 0; i < numAxes; i++) gamepads[index].axes[i] = 0;
        // Buttons are objects
        for (i = 0; i < numButtons; i++) gamepads[index].buttons[i] = { pressed: false, value: 0 };
      };
      // FF mutates the original objects.
      window['simulateGamepadButtonDown'] = function (index, button) {
        gamepads[index].buttons[button].pressed = true;
        gamepads[index].buttons[button].value = 1;
      };
      window['simulateGamepadButtonUp'] = function (index, button) {
        gamepads[index].buttons[button].pressed = false;
        gamepads[index].buttons[button].value = 0;
      };
      window['simulateAxisMotion'] = function (index, axis, value) {
        gamepads[index].axes[axis] = value;
      };
    ''')

    self.compile_btest([test_file('sdl_joystick.c'), '-O2', '--minify=0', '-o', 'page.html', '--pre-js', 'pre.js', '-lSDL', '-lGL'])
    self.run_browser('page.html', '', '/report_result?2')

  @requires_graphics_hardware
  def test_glfw_joystick(self):
    # Generates events corresponding to the Editor's Draft of the HTML5 Gamepad API.
    # https://dvcs.w3.org/hg/gamepad/raw-file/default/gamepad.html#idl-def-Gamepad
    create_file('pre.js', '''
      var gamepads = [];
      // Spoof this function.
      navigator['getGamepads'] = function() {
        return gamepads;
      };
      window['addNewGamepad'] = function(id, numAxes, numButtons) {
        var index = gamepads.length;
        var gamepad = {
          axes: new Array(numAxes),
          buttons: new Array(numButtons),
          id: id,
          index: index
        };
        gamepads.push(gamepad)
        var i;
        for (i = 0; i < numAxes; i++) gamepads[index].axes[i] = 0;
        // Buttons are objects
        for (i = 0; i < numButtons; i++) gamepads[index].buttons[i] = { pressed: false, value: 0 };

        // Dispatch event (required for glfw joystick; note not used in SDL test)
        var event = new Event('gamepadconnected');
        event.gamepad = gamepad;
        window.dispatchEvent(event);
      };
      // FF mutates the original objects.
      window['simulateGamepadButtonDown'] = function (index, button) {
        gamepads[index].buttons[button].pressed = true;
        gamepads[index].buttons[button].value = 1;
      };
      window['simulateGamepadButtonUp'] = function (index, button) {
        gamepads[index].buttons[button].pressed = false;
        gamepads[index].buttons[button].value = 0;
      };
      window['simulateAxisMotion'] = function (index, axis, value) {
        gamepads[index].axes[axis] = value;
      };
    ''')

    self.compile_btest([test_file('test_glfw_joystick.c'), '-O2', '--minify=0', '-o', 'page.html', '--pre-js', 'pre.js', '-lGL', '-lglfw3', '-s', 'USE_GLFW=3'])
    self.run_browser('page.html', '', '/report_result?2')

  @requires_graphics_hardware
  def test_webgl_context_attributes(self):
    # Javascript code to check the attributes support we want to test in the WebGL implementation
    # (request the attribute, create a context and check its value afterwards in the context attributes).
    # Tests will succeed when an attribute is not supported.
    create_file('check_webgl_attributes_support.js', '''
      mergeInto(LibraryManager.library, {
        webglAntialiasSupported: function() {
          canvas = document.createElement('canvas');
          context = canvas.getContext('experimental-webgl', {antialias: true});
          attributes = context.getContextAttributes();
          return attributes.antialias;
        },
        webglDepthSupported: function() {
          canvas = document.createElement('canvas');
          context = canvas.getContext('experimental-webgl', {depth: true});
          attributes = context.getContextAttributes();
          return attributes.depth;
        },
        webglStencilSupported: function() {
          canvas = document.createElement('canvas');
          context = canvas.getContext('experimental-webgl', {stencil: true});
          attributes = context.getContextAttributes();
          return attributes.stencil;
        },
        webglAlphaSupported: function() {
          canvas = document.createElement('canvas');
          context = canvas.getContext('experimental-webgl', {alpha: true});
          attributes = context.getContextAttributes();
          return attributes.alpha;
        }
      });
    ''')

    # Copy common code file to temporary directory
    filepath = test_file('test_webgl_context_attributes_common.c')
    temp_filepath = os.path.basename(filepath)
    shutil.copyfile(filepath, temp_filepath)

    # perform tests with attributes activated
    self.btest('test_webgl_context_attributes_glut.c', '1', args=['--js-library', 'check_webgl_attributes_support.js', '-DAA_ACTIVATED', '-DDEPTH_ACTIVATED', '-DSTENCIL_ACTIVATED', '-DALPHA_ACTIVATED', '-lGL', '-lglut', '-lGLEW'])
    self.btest('test_webgl_context_attributes_sdl.c', '1', args=['--js-library', 'check_webgl_attributes_support.js', '-DAA_ACTIVATED', '-DDEPTH_ACTIVATED', '-DSTENCIL_ACTIVATED', '-DALPHA_ACTIVATED', '-lGL', '-lSDL', '-lGLEW'])
    self.btest('test_webgl_context_attributes_sdl2.c', '1', args=['--js-library', 'check_webgl_attributes_support.js', '-DAA_ACTIVATED', '-DDEPTH_ACTIVATED', '-DSTENCIL_ACTIVATED', '-DALPHA_ACTIVATED', '-lGL', '-s', 'USE_SDL=2', '-lGLEW'])
    self.btest('test_webgl_context_attributes_glfw.c', '1', args=['--js-library', 'check_webgl_attributes_support.js', '-DAA_ACTIVATED', '-DDEPTH_ACTIVATED', '-DSTENCIL_ACTIVATED', '-DALPHA_ACTIVATED', '-lGL', '-lglfw', '-lGLEW'])

    # perform tests with attributes desactivated
    self.btest('test_webgl_context_attributes_glut.c', '1', args=['--js-library', 'check_webgl_attributes_support.js', '-lGL', '-lglut', '-lGLEW'])
    self.btest('test_webgl_context_attributes_sdl.c', '1', args=['--js-library', 'check_webgl_attributes_support.js', '-lGL', '-lSDL', '-lGLEW'])
    self.btest('test_webgl_context_attributes_glfw.c', '1', args=['--js-library', 'check_webgl_attributes_support.js', '-lGL', '-lglfw', '-lGLEW'])

  @requires_graphics_hardware
  def test_webgl_no_double_error(self):
    self.btest('webgl_error.cpp', '0')

  @requires_graphics_hardware
  def test_webgl_parallel_shader_compile(self):
    self.btest('webgl_parallel_shader_compile.cpp', '1')

  @requires_graphics_hardware
  def test_webgl_explicit_uniform_location(self):
    self.btest('webgl_explicit_uniform_location.c', '1', args=['-s', 'GL_EXPLICIT_UNIFORM_LOCATION=1', '-s', 'MIN_WEBGL_VERSION=2'])

  @requires_graphics_hardware
  def test_webgl_sampler_layout_binding(self):
    self.btest('webgl_sampler_layout_binding.c', '1', args=['-s', 'GL_EXPLICIT_UNIFORM_BINDING=1'])

  @requires_graphics_hardware
  def test_webgl2_ubo_layout_binding(self):
    self.btest('webgl2_ubo_layout_binding.c', '1', args=['-s', 'GL_EXPLICIT_UNIFORM_BINDING=1', '-s', 'MIN_WEBGL_VERSION=2'])

  # Test that -s GL_PREINITIALIZED_CONTEXT=1 works and allows user to set Module['preinitializedWebGLContext'] to a preinitialized WebGL context.
  @requires_graphics_hardware
  def test_preinitialized_webgl_context(self):
    self.btest('preinitialized_webgl_context.cpp', '5', args=['-s', 'GL_PREINITIALIZED_CONTEXT', '--shell-file', test_file('preinitialized_webgl_context.html')])

  @requires_threads
  def test_emscripten_get_now(self):
    for args in [[], ['-s', 'USE_PTHREADS'], ['-s', 'ENVIRONMENT=web', '-O2', '--closure=1']]:
      self.btest('emscripten_get_now.cpp', '1', args=args)

  def test_write_file_in_environment_web(self):
    self.btest_exit('write_file.c', args=['-s', 'ENVIRONMENT=web', '-Os', '--closure=1'])

  def test_fflush(self):
    self.btest('test_fflush.cpp', '0', args=['-s', 'EXIT_RUNTIME', '--shell-file', test_file('test_fflush.html')], reporting=Reporting.NONE)

  def test_file_db(self):
    secret = str(time.time())
    create_file('moar.txt', secret)
    self.btest('file_db.cpp', '1', args=['--preload-file', 'moar.txt', '-DFIRST'])
    shutil.copyfile('test.html', 'first.html')
    self.btest('file_db.cpp', secret, args=['-s', 'FORCE_FILESYSTEM'])
    shutil.copyfile('test.html', 'second.html')
    create_file('moar.txt', 'aliantha')
    self.btest('file_db.cpp', secret, args=['--preload-file', 'moar.txt']) # even with a file there, we load over it
    shutil.move('test.html', 'third.html')

  def test_fs_idbfs_sync(self):
    for extra in [[], ['-DEXTRA_WORK']]:
      secret = str(time.time())
      self.btest(test_file('fs/test_idbfs_sync.c'), '1', args=['-lidbfs.js', '-DFIRST', '-DSECRET=\"' + secret + '\"', '-s', 'EXPORTED_FUNCTIONS=_main,_test,_success', '-lidbfs.js'])
      self.btest(test_file('fs/test_idbfs_sync.c'), '1', args=['-lidbfs.js', '-DSECRET=\"' + secret + '\"', '-s', 'EXPORTED_FUNCTIONS=_main,_test,_success', '-lidbfs.js'] + extra)

  def test_fs_idbfs_sync_force_exit(self):
    secret = str(time.time())
    self.btest(test_file('fs/test_idbfs_sync.c'), '1', args=['-lidbfs.js', '-DFIRST', '-DSECRET=\"' + secret + '\"', '-s', 'EXPORTED_FUNCTIONS=_main,_test,_success', '-s', 'EXIT_RUNTIME', '-DFORCE_EXIT', '-lidbfs.js'])
    self.btest(test_file('fs/test_idbfs_sync.c'), '1', args=['-lidbfs.js', '-DSECRET=\"' + secret + '\"', '-s', 'EXPORTED_FUNCTIONS=_main,_test,_success', '-s', 'EXIT_RUNTIME', '-DFORCE_EXIT', '-lidbfs.js'])

  def test_fs_idbfs_fsync(self):
    # sync from persisted state into memory before main()
    create_file('pre.js', '''
      Module.preRun = function() {
        addRunDependency('syncfs');

        FS.mkdir('/working1');
        FS.mount(IDBFS, {}, '/working1');
        FS.syncfs(true, function (err) {
          if (err) throw err;
          removeRunDependency('syncfs');
        });
      };
    ''')

    args = ['--pre-js', 'pre.js', '-lidbfs.js', '-s', 'EXIT_RUNTIME', '-s', 'ASYNCIFY']
    secret = str(time.time())
    self.btest(test_file('fs/test_idbfs_fsync.c'), '1', args=args + ['-DFIRST', '-DSECRET=\"' + secret + '\"', '-s', 'EXPORTED_FUNCTIONS=_main,_success', '-lidbfs.js'])
    self.btest(test_file('fs/test_idbfs_fsync.c'), '1', args=args + ['-DSECRET=\"' + secret + '\"', '-s', 'EXPORTED_FUNCTIONS=_main,_success', '-lidbfs.js'])

  def test_fs_memfs_fsync(self):
    args = ['-s', 'ASYNCIFY', '-s', 'EXIT_RUNTIME']
    secret = str(time.time())
    self.btest(test_file('fs/test_memfs_fsync.c'), '1', args=args + ['-DSECRET=\"' + secret + '\"'])

  def test_fs_workerfs_read(self):
    secret = 'a' * 10
    secret2 = 'b' * 10
    create_file('pre.js', '''
      var Module = {};
      Module.preRun = function() {
        var blob = new Blob(['%s']);
        var file = new File(['%s'], 'file.txt');
        FS.mkdir('/work');
        FS.mount(WORKERFS, {
          blobs: [{ name: 'blob.txt', data: blob }],
          files: [file],
        }, '/work');
      };
    ''' % (secret, secret2))
    self.btest(test_file('fs/test_workerfs_read.c'), '1', args=['-lworkerfs.js', '--pre-js', 'pre.js', '-DSECRET=\"' + secret + '\"', '-DSECRET2=\"' + secret2 + '\"', '--proxy-to-worker', '-lworkerfs.js'])

  def test_fs_workerfs_package(self):
    create_file('file1.txt', 'first')
    ensure_dir('sub')
    open(Path('sub/file2.txt'), 'w').write('second')
    self.run_process([FILE_PACKAGER, 'files.data', '--preload', 'file1.txt', Path('sub/file2.txt'), '--separate-metadata', '--js-output=files.js'])
    self.btest(Path('fs/test_workerfs_package.cpp'), '1', args=['-lworkerfs.js', '--proxy-to-worker', '-lworkerfs.js'])

  def test_fs_lz4fs_package(self):
    # generate data
    ensure_dir('subdir')
    create_file('file1.txt', '0123456789' * (1024 * 128))
    open(Path('subdir/file2.txt'), 'w').write('1234567890' * (1024 * 128))
    random_data = bytearray(random.randint(0, 255) for x in range(1024 * 128 * 10 + 1))
    random_data[17] = ord('X')
    open('file3.txt', 'wb').write(random_data)

    # compress in emcc,  -s LZ4=1  tells it to tell the file packager
    print('emcc-normal')
    self.btest(Path('fs/test_lz4fs.cpp'), '2', args=['-s', 'LZ4=1', '--preload-file', 'file1.txt', '--preload-file', 'subdir/file2.txt', '--preload-file', 'file3.txt'])
    assert os.path.getsize('file1.txt') + os.path.getsize(Path('subdir/file2.txt')) + os.path.getsize('file3.txt') == 3 * 1024 * 128 * 10 + 1
    assert os.path.getsize('test.data') < (3 * 1024 * 128 * 10) / 2  # over half is gone
    print('    emcc-opts')
    self.btest(Path('fs/test_lz4fs.cpp'), '2', args=['-s', 'LZ4=1', '--preload-file', 'file1.txt', '--preload-file', 'subdir/file2.txt', '--preload-file', 'file3.txt', '-O2'])

    # compress in the file packager, on the server. the client receives compressed data and can just use it. this is typical usage
    print('normal')
    out = subprocess.check_output([FILE_PACKAGER, 'files.data', '--preload', 'file1.txt', 'subdir/file2.txt', 'file3.txt', '--lz4'])
    open('files.js', 'wb').write(out)
    self.btest(Path('fs/test_lz4fs.cpp'), '2', args=['--pre-js', 'files.js', '-s', 'LZ4=1', '-s', 'FORCE_FILESYSTEM'])
    print('    opts')
    self.btest(Path('fs/test_lz4fs.cpp'), '2', args=['--pre-js', 'files.js', '-s', 'LZ4=1', '-s', 'FORCE_FILESYSTEM', '-O2'])
    print('    modularize')
    self.compile_btest([test_file('fs/test_lz4fs.cpp'), '--pre-js', 'files.js', '-s', 'LZ4=1', '-s', 'FORCE_FILESYSTEM', '-s', 'MODULARIZE=1'])
    create_file('a.html', '''
      <script src="a.out.js"></script>
      <script>
        Module()
      </script>
    ''')
    self.run_browser('a.html', '.', '/report_result?2')

    # load the data into LZ4FS manually at runtime. This means we compress on the client. This is generally not recommended
    print('manual')
    subprocess.check_output([FILE_PACKAGER, 'files.data', '--preload', 'file1.txt', 'subdir/file2.txt', 'file3.txt', '--separate-metadata', '--js-output=files.js'])
    self.btest(Path('fs/test_lz4fs.cpp'), '1', args=['-DLOAD_MANUALLY', '-s', 'LZ4=1', '-s', 'FORCE_FILESYSTEM'])
    print('    opts')
    self.btest(Path('fs/test_lz4fs.cpp'), '1', args=['-DLOAD_MANUALLY', '-s', 'LZ4=1', '-s', 'FORCE_FILESYSTEM', '-O2'])
    print('    opts+closure')
    self.btest(Path('fs/test_lz4fs.cpp'), '1', args=['-DLOAD_MANUALLY', '-s', 'LZ4=1', '-s', 'FORCE_FILESYSTEM', '-O2', '--closure=1', '-g1', '-s', 'CLOSURE_WARNINGS=quiet'])

    '''# non-lz4 for comparison
    try:
      os.mkdir('files')
    except OSError:
      pass
    shutil.copyfile('file1.txt', Path('files/file1.txt'))
    shutil.copyfile('file2.txt', Path('files/file2.txt'))
    shutil.copyfile('file3.txt', Path('files/file3.txt'))
    out = subprocess.check_output([FILE_PACKAGER, 'files.data', '--preload', 'files/file1.txt', 'files/file2.txt', 'files/file3.txt'])
    open('files.js', 'wb').write(out)
    self.btest(Path('fs/test_lz4fs.cpp'), '2', args=['--pre-js', 'files.js'])'''

  def test_separate_metadata_later(self):
    # see issue #6654 - we need to handle separate-metadata both when we run before
    # the main program, and when we are run later

    create_file('data.dat', ' ')
    self.run_process([FILE_PACKAGER, 'more.data', '--preload', 'data.dat', '--separate-metadata', '--js-output=more.js'])
    self.btest(Path('browser/separate_metadata_later.cpp'), '1', args=['-s', 'FORCE_FILESYSTEM'])

  def test_idbstore(self):
    secret = str(time.time())
    for stage in [0, 1, 2, 3, 0, 1, 2, 0, 0, 1, 4, 2, 5]:
      self.clear()
      self.btest(test_file('idbstore.c'), str(stage), args=['-lidbstore.js', '-DSTAGE=' + str(stage), '-DSECRET=\"' + secret + '\"'])

  def test_idbstore_sync(self):
    secret = str(time.time())
    self.clear()
    self.btest(test_file('idbstore_sync.c'), '6', args=['-lidbstore.js', '-DSECRET=\"' + secret + '\"', '--memory-init-file', '1', '-O3', '-g2', '-s', 'ASYNCIFY'])

  def test_idbstore_sync_worker(self):
    secret = str(time.time())
    self.clear()
    self.btest(test_file('idbstore_sync_worker.c'), '6', args=['-lidbstore.js', '-DSECRET=\"' + secret + '\"', '--memory-init-file', '1', '-O3', '-g2', '--proxy-to-worker', '-s', 'INITIAL_MEMORY=80MB', '-s', 'ASYNCIFY'])

  def test_force_exit(self):
    self.btest('force_exit.c', expected='17', args=['-s', 'EXIT_RUNTIME'])

  def test_sdl_pumpevents(self):
    # key events should be detected using SDL_PumpEvents
    create_file('pre.js', '''
      function keydown(c) {
        var event = new KeyboardEvent("keydown", { 'keyCode': c, 'charCode': c, 'view': window, 'bubbles': true, 'cancelable': true });
        document.dispatchEvent(event);
      }
    ''')
    self.btest_exit('sdl_pumpevents.c', assert_returncode=7, args=['--pre-js', 'pre.js', '-lSDL', '-lGL'])

  def test_sdl_canvas_size(self):
    self.btest('sdl_canvas_size.c', expected='1',
               args=['-O2', '--minify=0', '--shell-file',
                     test_file('sdl_canvas_size.html'), '-lSDL', '-lGL'])

  @requires_graphics_hardware
  def test_sdl_gl_read(self):
    # SDL, OpenGL, readPixels
    self.compile_btest([test_file('sdl_gl_read.c'), '-o', 'something.html', '-lSDL', '-lGL'])
    self.run_browser('something.html', '.', '/report_result?1')

  @requires_graphics_hardware
  def test_sdl_gl_mapbuffers(self):
    self.btest('sdl_gl_mapbuffers.c', expected='1', args=['-s', 'FULL_ES3=1', '-lSDL', '-lGL'],
               message='You should see a blue triangle.')

  @requires_graphics_hardware
  def test_sdl_ogl(self):
    shutil.copyfile(test_file('screenshot.png'), 'screenshot.png')
    self.btest('sdl_ogl.c', reference='screenshot-gray-purple.png', reference_slack=1,
               args=['-O2', '--minify=0', '--preload-file', 'screenshot.png', '-s', 'LEGACY_GL_EMULATION', '--use-preload-plugins', '-lSDL', '-lGL'],
               message='You should see an image with gray at the top.')

  @requires_graphics_hardware
  def test_sdl_ogl_regal(self):
    shutil.copyfile(test_file('screenshot.png'), 'screenshot.png')
    self.btest('sdl_ogl.c', reference='screenshot-gray-purple.png', reference_slack=1,
               args=['-O2', '--minify=0', '--preload-file', 'screenshot.png', '-s', 'USE_REGAL', '-DUSE_REGAL', '--use-preload-plugins', '-lSDL', '-lGL'],
               message='You should see an image with gray at the top.')

  @requires_graphics_hardware
  def test_sdl_ogl_defaultmatrixmode(self):
    shutil.copyfile(test_file('screenshot.png'), 'screenshot.png')
    self.btest('sdl_ogl_defaultMatrixMode.c', reference='screenshot-gray-purple.png', reference_slack=1,
               args=['--minify=0', '--preload-file', 'screenshot.png', '-s', 'LEGACY_GL_EMULATION', '--use-preload-plugins', '-lSDL', '-lGL'],
               message='You should see an image with gray at the top.')

  @requires_graphics_hardware
  def test_sdl_ogl_p(self):
    # Immediate mode with pointers
    shutil.copyfile(test_file('screenshot.png'), 'screenshot.png')
    self.btest('sdl_ogl_p.c', reference='screenshot-gray.png', reference_slack=1,
               args=['--preload-file', 'screenshot.png', '-s', 'LEGACY_GL_EMULATION', '--use-preload-plugins', '-lSDL', '-lGL'],
               message='You should see an image with gray at the top.')

  @requires_graphics_hardware
  def test_sdl_ogl_proc_alias(self):
    shutil.copyfile(test_file('screenshot.png'), 'screenshot.png')
    self.btest('sdl_ogl_proc_alias.c', reference='screenshot-gray-purple.png', reference_slack=1,
               args=['-O2', '-g2', '-s', 'INLINING_LIMIT', '--preload-file', 'screenshot.png', '-s', 'LEGACY_GL_EMULATION', '--use-preload-plugins', '-lSDL', '-lGL'])

  @requires_graphics_hardware
  def test_sdl_fog_simple(self):
    shutil.copyfile(test_file('screenshot.png'), 'screenshot.png')
    self.btest('sdl_fog_simple.c', reference='screenshot-fog-simple.png',
               args=['-O2', '--minify=0', '--preload-file', 'screenshot.png', '-s', 'LEGACY_GL_EMULATION', '--use-preload-plugins', '-lSDL', '-lGL'],
               message='You should see an image with fog.')

  @requires_graphics_hardware
  def test_sdl_fog_negative(self):
    shutil.copyfile(test_file('screenshot.png'), 'screenshot.png')
    self.btest('sdl_fog_negative.c', reference='screenshot-fog-negative.png',
               args=['--preload-file', 'screenshot.png', '-s', 'LEGACY_GL_EMULATION', '--use-preload-plugins', '-lSDL', '-lGL'],
               message='You should see an image with fog.')

  @requires_graphics_hardware
  def test_sdl_fog_density(self):
    shutil.copyfile(test_file('screenshot.png'), 'screenshot.png')
    self.btest('sdl_fog_density.c', reference='screenshot-fog-density.png',
               args=['--preload-file', 'screenshot.png', '-s', 'LEGACY_GL_EMULATION', '--use-preload-plugins', '-lSDL', '-lGL'],
               message='You should see an image with fog.')

  @requires_graphics_hardware
  def test_sdl_fog_exp2(self):
    shutil.copyfile(test_file('screenshot.png'), 'screenshot.png')
    self.btest('sdl_fog_exp2.c', reference='screenshot-fog-exp2.png',
               args=['--preload-file', 'screenshot.png', '-s', 'LEGACY_GL_EMULATION', '--use-preload-plugins', '-lSDL', '-lGL'],
               message='You should see an image with fog.')

  @requires_graphics_hardware
  def test_sdl_fog_linear(self):
    shutil.copyfile(test_file('screenshot.png'), 'screenshot.png')
    self.btest('sdl_fog_linear.c', reference='screenshot-fog-linear.png', reference_slack=1,
               args=['--preload-file', 'screenshot.png', '-s', 'LEGACY_GL_EMULATION', '--use-preload-plugins', '-lSDL', '-lGL'],
               message='You should see an image with fog.')

  @requires_graphics_hardware
  def test_glfw(self):
    self.btest('glfw.c', '1', args=['-s', 'LEGACY_GL_EMULATION', '-lglfw', '-lGL'])
    self.btest('glfw.c', '1', args=['-s', 'LEGACY_GL_EMULATION', '-s', 'USE_GLFW=2', '-lglfw', '-lGL'])

  def test_glfw_minimal(self):
    self.btest('glfw_minimal.c', '1', args=['-lglfw', '-lGL'])
    self.btest('glfw_minimal.c', '1', args=['-s', 'USE_GLFW=2', '-lglfw', '-lGL'])

  def test_glfw_time(self):
    self.btest('test_glfw_time.c', '1', args=['-s', 'USE_GLFW=3', '-lglfw', '-lGL'])

  def _test_egl_base(self, *args):
    self.compile_btest([test_file('test_egl.c'), '-O2', '-o', 'page.html', '-lEGL', '-lGL'] + list(args))
    self.run_browser('page.html', '', '/report_result?1')

  @requires_graphics_hardware
  def test_egl(self):
    self._test_egl_base()

  @requires_threads
  @requires_graphics_hardware
  def test_egl_with_proxy_to_pthread(self):
    self._test_egl_base('-s', 'USE_PTHREADS', '-s', 'PROXY_TO_PTHREAD', '-s', 'OFFSCREEN_FRAMEBUFFER')

  def _test_egl_width_height_base(self, *args):
    self.compile_btest([test_file('test_egl_width_height.c'), '-O2', '-o', 'page.html', '-lEGL', '-lGL'] + list(args))
    self.run_browser('page.html', 'Should print "(300, 150)" -- the size of the canvas in pixels', '/report_result?1')

  def test_egl_width_height(self):
    self._test_egl_width_height_base()

  @requires_threads
  def test_egl_width_height_with_proxy_to_pthread(self):
    self._test_egl_width_height_base('-s', 'USE_PTHREADS', '-s', 'PROXY_TO_PTHREAD')

  @requires_graphics_hardware
  def test_egl_createcontext_error(self):
    self.btest('test_egl_createcontext_error.c', '1', args=['-lEGL', '-lGL'])

  def test_worker(self):
    # Test running in a web worker
    create_file('file.dat', 'data for worker')
    html_file = open('main.html', 'w')
    html_file.write('''
      <html>
      <body>
        Worker Test
        <script>
          var worker = new Worker('worker.js');
          worker.onmessage = function(event) {
            var xhr = new XMLHttpRequest();
            xhr.open('GET', 'http://localhost:%s/report_result?' + event.data);
            xhr.send();
            setTimeout(function() { window.close() }, 1000);
          };
        </script>
      </body>
      </html>
    ''' % self.port)
    html_file.close()

    for file_data in [1, 0]:
      cmd = [EMCC, test_file('hello_world_worker.cpp'), '-o', 'worker.js'] + (['--preload-file', 'file.dat'] if file_data else [])
      print(cmd)
      self.run_process(cmd)
      self.assertExists('worker.js')
      self.run_browser('main.html', '', '/report_result?hello from worker, and :' + ('data for w' if file_data else '') + ':')

    self.assertContained('you should not see this text when in a worker!', self.run_js('worker.js')) # code should run standalone too

  @no_firefox('keeps sending OPTIONS requests, and eventually errors')
  def test_chunked_synchronous_xhr(self):
    main = 'chunked_sync_xhr.html'
    worker_filename = "download_and_checksum_worker.js"

    html_file = open(main, 'w')
    html_file.write(r"""
      <!doctype html>
      <html>
      <head><meta charset="utf-8"><title>Chunked XHR</title></head>
      <html>
      <body>
        Chunked XHR Web Worker Test
        <script>
          var worker = new Worker(""" + json.dumps(worker_filename) + r""");
          var buffer = [];
          worker.onmessage = function(event) {
            if (event.data.channel === "stdout") {
              var xhr = new XMLHttpRequest();
              xhr.open('GET', 'http://localhost:%s/report_result?' + event.data.line);
              xhr.send();
              setTimeout(function() { window.close() }, 1000);
            } else {
              if (event.data.trace) event.data.trace.split("\n").map(function(v) { console.error(v); });
              if (event.data.line) {
                console.error(event.data.line);
              } else {
                var v = event.data.char;
                if (v == 10) {
                  var line = buffer.splice(0);
                  console.error(line = line.map(function(charCode){return String.fromCharCode(charCode);}).join(''));
                } else {
                  buffer.push(v);
                }
              }
            }
          };
        </script>
      </body>
      </html>
    """ % self.port)
    html_file.close()

    c_source_filename = "checksummer.c"

    prejs_filename = "worker_prejs.js"
    prejs_file = open(prejs_filename, 'w')
    prejs_file.write(r"""
      if (typeof(Module) === "undefined") Module = {};
      Module["arguments"] = ["/bigfile"];
      Module["preInit"] = function() {
          FS.createLazyFile('/', "bigfile", "http://localhost:11111/bogus_file_path", true, false);
      };
      var doTrace = true;
      Module["print"] = function(s) { self.postMessage({channel: "stdout", line: s}); };
      Module["printErr"] = function(s) { self.postMessage({channel: "stderr", char: s, trace: ((doTrace && s === 10) ? new Error().stack : null)}); doTrace = false; };
    """)
    prejs_file.close()
    # vs. os.path.join(self.get_dir(), filename)
    # vs. test_file('hello_world_gles.c')
    self.compile_btest([test_file(c_source_filename), '-g', '-s', 'SMALL_XHR_CHUNKS', '-o', worker_filename,
                        '--pre-js', prejs_filename])
    chunkSize = 1024
    data = os.urandom(10 * chunkSize + 1) # 10 full chunks and one 1 byte chunk
    checksum = zlib.adler32(data) & 0xffffffff # Python 2 compatibility: force bigint

    server = multiprocessing.Process(target=test_chunked_synchronous_xhr_server, args=(True, chunkSize, data, checksum, self.port))
    server.start()

    # block until the server is actually ready
    for i in range(60):
      try:
        urlopen('http://localhost:11111')
        break
      except Exception as e:
        print('(sleep for server)')
        time.sleep(1)
        if i == 60:
          raise e

    try:
      self.run_browser(main, 'Chunked binary synchronous XHR in Web Workers!', '/report_result?' + str(checksum))
    finally:
      server.terminate()
    # Avoid race condition on cleanup, wait a bit so that processes have released file locks so that test tearDown won't
    # attempt to rmdir() files in use.
    if WINDOWS:
      time.sleep(2)

  @requires_graphics_hardware
  def test_glgears(self, extra_args=[]):
    self.btest('hello_world_gles.c', reference='gears.png', reference_slack=3,
               args=['-DHAVE_BUILTIN_SINCOS', '-lGL', '-lglut'] + extra_args)

  @requires_graphics_hardware
  @requires_threads
  def test_glgears_pthreads(self, extra_args=[]):
    # test that a program that doesn't use pthreads still works with with pthreads enabled
    # (regression test for https://github.com/emscripten-core/emscripten/pull/8059#issuecomment-488105672)
    self.test_glgears(['-s', 'USE_PTHREADS'])

  @requires_graphics_hardware
  def test_glgears_long(self):
    for proxy in [0, 1]:
      print('proxy', proxy)
      self.btest('hello_world_gles.c', expected=list(map(str, range(15, 500))), args=['-DHAVE_BUILTIN_SINCOS', '-DLONGTEST', '-lGL', '-lglut', '-DANIMATE'] + (['--proxy-to-worker'] if proxy else []))

  @requires_graphics_hardware
  def test_glgears_animation(self):
    es2_suffix = ['', '_full', '_full_944']
    for full_es2 in [0, 1, 2]:
      print(full_es2)
      self.compile_btest([test_file('hello_world_gles%s.c' % es2_suffix[full_es2]), '-o', 'something.html',
                          '-DHAVE_BUILTIN_SINCOS', '-s', 'GL_TESTING', '-lGL', '-lglut',
                          '--shell-file', test_file('hello_world_gles_shell.html')] +
                         (['-s', 'FULL_ES2=1'] if full_es2 else []))
      self.run_browser('something.html', 'You should see animating gears.', '/report_gl_result?true')

  @requires_graphics_hardware
  def test_fulles2_sdlproc(self):
    self.btest_exit('full_es2_sdlproc.c', assert_returncode=1, args=['-s', 'GL_TESTING', '-DHAVE_BUILTIN_SINCOS', '-s', 'FULL_ES2', '-lGL', '-lSDL', '-lglut'])

  @requires_graphics_hardware
  def test_glgears_deriv(self):
    self.btest('hello_world_gles_deriv.c', reference='gears.png', reference_slack=2,
               args=['-DHAVE_BUILTIN_SINCOS', '-lGL', '-lglut'],
               message='You should see animating gears.')
    assert 'gl-matrix' not in read_file('test.html'), 'Should not include glMatrix when not needed'

  @requires_graphics_hardware
  def test_glbook(self):
    self.emcc_args.remove('-Werror')
    programs = self.get_library('glbook', [
      Path('Chapter_2/Hello_Triangle', 'CH02_HelloTriangle.o'),
      Path('Chapter_8/Simple_VertexShader', 'CH08_SimpleVertexShader.o'),
      Path('Chapter_9/Simple_Texture2D', 'CH09_SimpleTexture2D.o'),
      Path('Chapter_9/Simple_TextureCubemap', 'CH09_TextureCubemap.o'),
      Path('Chapter_9/TextureWrap', 'CH09_TextureWrap.o'),
      Path('Chapter_10/MultiTexture', 'CH10_MultiTexture.o'),
      Path('Chapter_13/ParticleSystem', 'CH13_ParticleSystem.o'),
    ], configure=None, make=[EMMAKE, 'make'])

    def book_path(*pathelems):
      return test_file('glbook', *pathelems)

    for program in programs:
      print(program)
      basename = os.path.basename(program)
      args = ['-lGL', '-lEGL', '-lX11']
      if basename == 'CH10_MultiTexture.o':
        shutil.copyfile(book_path('Chapter_10', 'MultiTexture', 'basemap.tga'), 'basemap.tga')
        shutil.copyfile(book_path('Chapter_10', 'MultiTexture', 'lightmap.tga'), 'lightmap.tga')
        args += ['--preload-file', 'basemap.tga', '--preload-file', 'lightmap.tga']
      elif basename == 'CH13_ParticleSystem.o':
        shutil.copyfile(book_path('Chapter_13', 'ParticleSystem', 'smoke.tga'), 'smoke.tga')
        args += ['--preload-file', 'smoke.tga', '-O2'] # test optimizations and closure here as well for more coverage

      self.btest(program,
                 reference=book_path(basename.replace('.o', '.png')),
                 args=args)

  @requires_graphics_hardware
  @parameterized({
    'normal': (['-s', 'FULL_ES2=1'],),
    # Enabling FULL_ES3 also enables ES2 automatically
    'full_es3': (['-s', 'FULL_ES3=1'],)
  })
  def test_gles2_emulation(self, args):
    print(args)
    shutil.copyfile(test_file('glbook/Chapter_10/MultiTexture/basemap.tga'), 'basemap.tga')
    shutil.copyfile(test_file('glbook/Chapter_10/MultiTexture/lightmap.tga'), 'lightmap.tga')
    shutil.copyfile(test_file('glbook/Chapter_13/ParticleSystem/smoke.tga'), 'smoke.tga')

    for source, reference in [
      (Path('glbook/Chapter_2', 'Hello_Triangle', 'Hello_Triangle_orig.c'), test_file('glbook/CH02_HelloTriangle.png')),
      # (Path('glbook/Chapter_8', 'Simple_VertexShader', 'Simple_VertexShader_orig.c'), test_file('glbook/CH08_SimpleVertexShader.png')), # XXX needs INT extension in WebGL
      (Path('glbook/Chapter_9', 'TextureWrap', 'TextureWrap_orig.c'), test_file('glbook/CH09_TextureWrap.png')),
      # (Path('glbook/Chapter_9', 'Simple_TextureCubemap', 'Simple_TextureCubemap_orig.c'), test_file('glbook/CH09_TextureCubemap.png')), # XXX needs INT extension in WebGL
      (Path('glbook/Chapter_9', 'Simple_Texture2D', 'Simple_Texture2D_orig.c'), test_file('glbook/CH09_SimpleTexture2D.png')),
      (Path('glbook/Chapter_10', 'MultiTexture', 'MultiTexture_orig.c'), test_file('glbook/CH10_MultiTexture.png')),
      (Path('glbook/Chapter_13', 'ParticleSystem', 'ParticleSystem_orig.c'), test_file('glbook/CH13_ParticleSystem.png')),
    ]:
      print(source)
      self.btest(source,
                 reference=reference,
                 args=['-I' + test_file('glbook/Common'),
                       test_file('glbook/Common/esUtil.c'),
                       test_file('glbook/Common/esShader.c'),
                       test_file('glbook/Common/esShapes.c'),
                       test_file('glbook/Common/esTransform.c'),
                       '-lGL', '-lEGL', '-lX11',
                       '--preload-file', 'basemap.tga', '--preload-file', 'lightmap.tga', '--preload-file', 'smoke.tga'] + args)

  @requires_graphics_hardware
  def test_clientside_vertex_arrays_es3(self):
    self.btest('clientside_vertex_arrays_es3.c', reference='gl_triangle.png', args=['-s', 'FULL_ES3=1', '-s', 'USE_GLFW=3', '-lglfw', '-lGLESv2'])

  def test_emscripten_api(self):
    self.btest_exit('emscripten_api_browser.c', args=['-s', 'EXPORTED_FUNCTIONS=_main,_third', '-lSDL'])

  def test_emscripten_api2(self):
    def setup():
      create_file('script1.js', '''
        Module._set(456);
      ''')
      create_file('file1.txt', 'first')
      create_file('file2.txt', 'second')

    setup()
    self.run_process([FILE_PACKAGER, 'test.data', '--preload', 'file1.txt', 'file2.txt'], stdout=open('script2.js', 'w'))
    self.btest_exit('emscripten_api_browser2.c', args=['-s', 'EXPORTED_FUNCTIONS=_main,_set', '-s', 'FORCE_FILESYSTEM'])

    # check using file packager to another dir
    self.clear()
    setup()
    ensure_dir('sub')
    self.run_process([FILE_PACKAGER, 'sub/test.data', '--preload', 'file1.txt', 'file2.txt'], stdout=open('script2.js', 'w'))
    shutil.copyfile(Path('sub/test.data'), 'test.data')
    self.btest_exit('emscripten_api_browser2.c', args=['-s', 'EXPORTED_FUNCTIONS=_main,_set', '-s', 'FORCE_FILESYSTEM'])

  def test_emscripten_api_infloop(self):
    self.btest_exit('emscripten_api_browser_infloop.cpp', assert_returncode=7)

  def test_emscripten_fs_api(self):
    shutil.copyfile(test_file('screenshot.png'), 'screenshot.png') # preloaded *after* run
    self.btest_exit('emscripten_fs_api_browser.c', assert_returncode=1, args=['-lSDL'])

  def test_emscripten_fs_api2(self):
    self.btest_exit('emscripten_fs_api_browser2.c', assert_returncode=1, args=['-s', "ASSERTIONS=0"])
    self.btest_exit('emscripten_fs_api_browser2.c', assert_returncode=1, args=['-s', "ASSERTIONS=1"])

  @requires_threads
  def test_emscripten_main_loop(self):
    for args in [[], ['-s', 'USE_PTHREADS', '-s', 'PROXY_TO_PTHREAD', '-s', 'EXIT_RUNTIME']]:
      self.btest_exit('emscripten_main_loop.cpp', args=args)

  @requires_threads
  def test_emscripten_main_loop_settimeout(self):
    for args in [
      [],
      # test pthreads + AUTO_JS_LIBRARIES mode as well
      ['-s', 'USE_PTHREADS', '-s', 'PROXY_TO_PTHREAD', '-s', 'AUTO_JS_LIBRARIES=0'],
    ]:
      self.btest_exit('emscripten_main_loop_settimeout.cpp', args=args)

  @requires_threads
  def test_emscripten_main_loop_and_blocker(self):
    for args in [[], ['-s', 'USE_PTHREADS', '-s', 'PROXY_TO_PTHREAD']]:
      self.btest_exit('emscripten_main_loop_and_blocker.cpp', args=args)

  @requires_threads
  def test_emscripten_main_loop_and_blocker_exit(self):
    # Same as above but tests that EXIT_RUNTIME works with emscripten_main_loop.  The
    # app should still stay alive until the loop ends
    self.btest_exit('emscripten_main_loop_and_blocker.cpp')

  @requires_threads
  def test_emscripten_main_loop_setimmediate(self):
    for args in [[], ['--proxy-to-worker'], ['-s', 'USE_PTHREADS', '-s', 'PROXY_TO_PTHREAD']]:
      self.btest_exit('emscripten_main_loop_setimmediate.cpp', args=args)

  def test_fs_after_main(self):
    for args in [[], ['-O1']]:
      self.btest('fs_after_main.cpp', '0', args=args)

  def test_sdl_quit(self):
    self.btest('sdl_quit.c', '1', args=['-lSDL', '-lGL'])

  def test_sdl_resize(self):
    # FIXME(https://github.com/emscripten-core/emscripten/issues/12978)
    self.emcc_args.append('-Wno-deprecated-declarations')
    self.btest('sdl_resize.c', '1', args=['-lSDL', '-lGL'])

  def test_glshaderinfo(self):
    self.btest('glshaderinfo.cpp', '1', args=['-lGL', '-lglut'])

  @requires_graphics_hardware
  def test_glgetattachedshaders(self):
    self.btest('glgetattachedshaders.c', '1', args=['-lGL', '-lEGL'])

  # Covered by dEQP text suite (we can remove it later if we add coverage for that).
  @requires_graphics_hardware
  def test_glframebufferattachmentinfo(self):
    self.btest('glframebufferattachmentinfo.c', '1', args=['-lGLESv2', '-lEGL'])

  @requires_graphics_hardware
  def test_sdlglshader(self):
    self.btest('sdlglshader.c', reference='sdlglshader.png', args=['-O2', '--closure=1', '-s', 'LEGACY_GL_EMULATION', '-lGL', '-lSDL'])

  @requires_graphics_hardware
  def test_sdlglshader2(self):
    self.btest('sdlglshader2.c', expected='1', args=['-s', 'LEGACY_GL_EMULATION', '-lGL', '-lSDL'], also_proxied=True)

  @requires_graphics_hardware
  def test_gl_glteximage(self):
    self.btest('gl_teximage.c', '1', args=['-lGL', '-lSDL'])

  @requires_graphics_hardware
  @requires_threads
  def test_gl_textures(self):
    for args in [[], ['-s', 'USE_PTHREADS', '-s', 'PROXY_TO_PTHREAD', '-s', 'OFFSCREEN_FRAMEBUFFER']]:
      self.btest('gl_textures.cpp', '0', args=['-lGL'] + args)

  @requires_graphics_hardware
  def test_gl_ps(self):
    # pointers and a shader
    shutil.copyfile(test_file('screenshot.png'), 'screenshot.png')
    self.btest('gl_ps.c', reference='gl_ps.png', args=['--preload-file', 'screenshot.png', '-s', 'LEGACY_GL_EMULATION', '-lGL', '-lSDL', '--use-preload-plugins'], reference_slack=1)

  @requires_graphics_hardware
  def test_gl_ps_packed(self):
    # packed data that needs to be strided
    shutil.copyfile(test_file('screenshot.png'), 'screenshot.png')
    self.btest('gl_ps_packed.c', reference='gl_ps.png', args=['--preload-file', 'screenshot.png', '-s', 'LEGACY_GL_EMULATION', '-lGL', '-lSDL', '--use-preload-plugins'], reference_slack=1)

  @requires_graphics_hardware
  def test_gl_ps_strides(self):
    shutil.copyfile(test_file('screenshot.png'), 'screenshot.png')
    self.btest('gl_ps_strides.c', reference='gl_ps_strides.png', args=['--preload-file', 'screenshot.png', '-s', 'LEGACY_GL_EMULATION', '-lGL', '-lSDL', '--use-preload-plugins'])

  @requires_graphics_hardware
  def test_gl_ps_worker(self):
    shutil.copyfile(test_file('screenshot.png'), 'screenshot.png')
    self.btest('gl_ps_worker.c', reference='gl_ps.png', args=['--preload-file', 'screenshot.png', '-s', 'LEGACY_GL_EMULATION', '-lGL', '-lSDL', '--use-preload-plugins'], reference_slack=1, also_proxied=True)

  @requires_graphics_hardware
  def test_gl_renderers(self):
    self.btest('gl_renderers.c', reference='gl_renderers.png', args=['-s', 'GL_UNSAFE_OPTS=0', '-s', 'LEGACY_GL_EMULATION', '-lGL', '-lSDL'])

  @requires_graphics_hardware
  def test_gl_stride(self):
    self.btest('gl_stride.c', reference='gl_stride.png', args=['-s', 'GL_UNSAFE_OPTS=0', '-s', 'LEGACY_GL_EMULATION', '-lGL', '-lSDL'])

  @requires_graphics_hardware
  def test_gl_vertex_buffer_pre(self):
    self.btest('gl_vertex_buffer_pre.c', reference='gl_vertex_buffer_pre.png', args=['-s', 'GL_UNSAFE_OPTS=0', '-s', 'LEGACY_GL_EMULATION', '-lGL', '-lSDL'])

  @requires_graphics_hardware
  def test_gl_vertex_buffer(self):
    self.btest('gl_vertex_buffer.c', reference='gl_vertex_buffer.png', args=['-s', 'GL_UNSAFE_OPTS=0', '-s', 'LEGACY_GL_EMULATION', '-lGL', '-lSDL'], reference_slack=1)

  @requires_graphics_hardware
  def test_gles2_uniform_arrays(self):
    self.btest('gles2_uniform_arrays.cpp', args=['-s', 'GL_ASSERTIONS', '-lGL', '-lSDL'], expected=['1'], also_proxied=True)

  @requires_graphics_hardware
  def test_gles2_conformance(self):
    self.btest('gles2_conformance.cpp', args=['-s', 'GL_ASSERTIONS', '-lGL', '-lSDL'], expected=['1'])

  @requires_graphics_hardware
  def test_matrix_identity(self):
    self.btest('gl_matrix_identity.c', expected=['-1882984448', '460451840', '1588195328', '2411982848'], args=['-s', 'LEGACY_GL_EMULATION', '-lGL', '-lSDL'])

  @requires_graphics_hardware
  @no_swiftshader
  def test_cubegeom_pre(self):
    self.btest(Path('third_party/cubegeom', 'cubegeom_pre.c'), reference=Path('third_party/cubegeom', 'cubegeom_pre.png'), args=['-s', 'LEGACY_GL_EMULATION', '-lGL', '-lSDL'])

  @requires_graphics_hardware
  @no_swiftshader
  def test_cubegeom_pre_regal(self):
    self.btest(Path('third_party/cubegeom', 'cubegeom_pre.c'), reference=Path('third_party/cubegeom', 'cubegeom_pre.png'), args=['-s', 'USE_REGAL', '-DUSE_REGAL', '-lGL', '-lSDL'])

  @requires_graphics_hardware
  @requires_sync_compilation
  def test_cubegeom_pre_relocatable(self):
    self.btest(Path('third_party/cubegeom', 'cubegeom_pre.c'), reference=Path('third_party/cubegeom', 'cubegeom_pre.png'), args=['-s', 'LEGACY_GL_EMULATION', '-lGL', '-lSDL', '-s', 'RELOCATABLE'])

  @requires_graphics_hardware
  @no_swiftshader
  def test_cubegeom_pre2(self):
    self.btest(Path('third_party/cubegeom', 'cubegeom_pre2.c'), reference=Path('third_party/cubegeom', 'cubegeom_pre2.png'), args=['-s', 'GL_DEBUG', '-s', 'LEGACY_GL_EMULATION', '-lGL', '-lSDL']) # some coverage for GL_DEBUG not breaking the build

  @requires_graphics_hardware
  @no_swiftshader
  def test_cubegeom_pre3(self):
    self.btest(Path('third_party/cubegeom', 'cubegeom_pre3.c'), reference=Path('third_party/cubegeom', 'cubegeom_pre2.png'), args=['-s', 'LEGACY_GL_EMULATION', '-lGL', '-lSDL'])

  @parameterized({
    '': ([],),
    'tracing': (['-sTRACE_WEBGL_CALLS'],),
  })
  @requires_graphics_hardware
  def test_cubegeom(self, args):
    # proxy only in the simple, normal case (we can't trace GL calls when
    # proxied)
    self.btest(Path('third_party/cubegeom', 'cubegeom.c'), reference=Path('third_party/cubegeom', 'cubegeom.png'), args=['-O2', '-g', '-s', 'LEGACY_GL_EMULATION', '-lGL', '-lSDL'] + args, also_proxied=not args)

  @requires_graphics_hardware
  def test_cubegeom_regal(self):
    self.btest(Path('third_party/cubegeom', 'cubegeom.c'), reference=Path('third_party/cubegeom', 'cubegeom.png'), args=['-O2', '-g', '-DUSE_REGAL', '-s', 'USE_REGAL', '-lGL', '-lSDL'], also_proxied=True)

  @requires_threads
  @requires_graphics_hardware
  def test_cubegeom_regal_mt(self):
    self.btest(Path('third_party/cubegeom', 'cubegeom.c'), reference=Path('third_party/cubegeom', 'cubegeom.png'), args=['-O2', '-g', '-pthread', '-DUSE_REGAL', '-s', 'USE_PTHREADS', '-s', 'USE_REGAL', '-lGL', '-lSDL'], also_proxied=False)

  @requires_graphics_hardware
  def test_cubegeom_proc(self):
    create_file('side.c', r'''

extern void* SDL_GL_GetProcAddress(const char *);

void *glBindBuffer = 0; // same name as the gl function, to check that the collision does not break us

void *getBindBuffer() {
  if (!glBindBuffer) glBindBuffer = SDL_GL_GetProcAddress("glBindBuffer");
  return glBindBuffer;
}
''')
    # also test -Os in wasm, which uses meta-dce, which should not break legacy gl emulation hacks
    for opts in [[], ['-O1'], ['-Os']]:
      self.btest(Path('third_party/cubegeom', 'cubegeom_proc.c'), reference=Path('third_party/cubegeom', 'cubegeom.png'), args=opts + ['side.c', '-s', 'LEGACY_GL_EMULATION', '-lGL', '-lSDL'])

  @requires_graphics_hardware
  def test_cubegeom_glew(self):
    self.btest(Path('third_party/cubegeom', 'cubegeom_glew.c'), reference=Path('third_party/cubegeom', 'cubegeom.png'), args=['-O2', '--closure=1', '-s', 'LEGACY_GL_EMULATION', '-lGL', '-lGLEW', '-lSDL'])

  @requires_graphics_hardware
  def test_cubegeom_color(self):
    self.btest(Path('third_party/cubegeom', 'cubegeom_color.c'), reference=Path('third_party/cubegeom', 'cubegeom_color.png'), args=['-s', 'LEGACY_GL_EMULATION', '-lGL', '-lSDL'])

  @requires_graphics_hardware
  def test_cubegeom_normal(self):
    self.btest(Path('third_party/cubegeom', 'cubegeom_normal.c'), reference=Path('third_party/cubegeom', 'cubegeom_normal.png'), args=['-s', 'LEGACY_GL_EMULATION', '-lGL', '-lSDL'], also_proxied=True)

  @requires_graphics_hardware
  def test_cubegeom_normal_dap(self): # draw is given a direct pointer to clientside memory, no element array buffer
    self.btest(Path('third_party/cubegeom', 'cubegeom_normal_dap.c'), reference=Path('third_party/cubegeom', 'cubegeom_normal.png'), args=['-s', 'LEGACY_GL_EMULATION', '-lGL', '-lSDL'], also_proxied=True)

  @requires_graphics_hardware
  def test_cubegeom_normal_dap_far(self): # indices do nto start from 0
    self.btest(Path('third_party/cubegeom', 'cubegeom_normal_dap_far.c'), reference=Path('third_party/cubegeom', 'cubegeom_normal.png'), args=['-s', 'LEGACY_GL_EMULATION', '-lGL', '-lSDL'])

  @requires_graphics_hardware
  def test_cubegeom_normal_dap_far_range(self): # glDrawRangeElements
    self.btest(Path('third_party/cubegeom', 'cubegeom_normal_dap_far_range.c'), reference=Path('third_party/cubegeom', 'cubegeom_normal.png'), args=['-s', 'LEGACY_GL_EMULATION', '-lGL', '-lSDL'])

  @requires_graphics_hardware
  def test_cubegeom_normal_dap_far_glda(self): # use glDrawArrays
    self.btest(Path('third_party/cubegeom', 'cubegeom_normal_dap_far_glda.c'), reference=Path('third_party/cubegeom', 'cubegeom_normal_dap_far_glda.png'), args=['-s', 'LEGACY_GL_EMULATION', '-lGL', '-lSDL'])

  @requires_graphics_hardware
  @no_firefox('fails on CI but works locally')
  def test_cubegeom_normal_dap_far_glda_quad(self): # with quad
    self.btest(Path('third_party/cubegeom', 'cubegeom_normal_dap_far_glda_quad.c'), reference=Path('third_party/cubegeom', 'cubegeom_normal_dap_far_glda_quad.png'), args=['-s', 'LEGACY_GL_EMULATION', '-lGL', '-lSDL'])

  @requires_graphics_hardware
  def test_cubegeom_mt(self):
    self.btest(Path('third_party/cubegeom', 'cubegeom_mt.c'), reference=Path('third_party/cubegeom', 'cubegeom_mt.png'), args=['-s', 'LEGACY_GL_EMULATION', '-lGL', '-lSDL']) # multitexture

  @requires_graphics_hardware
  def test_cubegeom_color2(self):
    self.btest(Path('third_party/cubegeom', 'cubegeom_color2.c'), reference=Path('third_party/cubegeom', 'cubegeom_color2.png'), args=['-s', 'LEGACY_GL_EMULATION', '-lGL', '-lSDL'], also_proxied=True)

  @requires_graphics_hardware
  def test_cubegeom_texturematrix(self):
    self.btest(Path('third_party/cubegeom', 'cubegeom_texturematrix.c'), reference=Path('third_party/cubegeom', 'cubegeom_texturematrix.png'), args=['-s', 'LEGACY_GL_EMULATION', '-lGL', '-lSDL'])

  @requires_graphics_hardware
  def test_cubegeom_fog(self):
    self.btest(Path('third_party/cubegeom', 'cubegeom_fog.c'), reference=Path('third_party/cubegeom', 'cubegeom_fog.png'), args=['-s', 'LEGACY_GL_EMULATION', '-lGL', '-lSDL'])

  @requires_graphics_hardware
  @no_swiftshader
  def test_cubegeom_pre_vao(self):
    self.btest(Path('third_party/cubegeom', 'cubegeom_pre_vao.c'), reference=Path('third_party/cubegeom', 'cubegeom_pre_vao.png'), args=['-s', 'LEGACY_GL_EMULATION', '-lGL', '-lSDL'])

  @requires_graphics_hardware
  @no_swiftshader
  def test_cubegeom_pre_vao_regal(self):
    self.btest(Path('third_party/cubegeom', 'cubegeom_pre_vao.c'), reference=Path('third_party/cubegeom', 'cubegeom_pre_vao.png'), args=['-s', 'USE_REGAL', '-DUSE_REGAL', '-lGL', '-lSDL'])

  @requires_graphics_hardware
  @no_swiftshader
  def test_cubegeom_pre2_vao(self):
    self.btest(Path('third_party/cubegeom', 'cubegeom_pre2_vao.c'), reference=Path('third_party/cubegeom', 'cubegeom_pre_vao.png'), args=['-s', 'LEGACY_GL_EMULATION', '-lGL', '-lSDL'])

  @requires_graphics_hardware
  def test_cubegeom_pre2_vao2(self):
    self.btest(Path('third_party/cubegeom', 'cubegeom_pre2_vao2.c'), reference=Path('third_party/cubegeom', 'cubegeom_pre2_vao2.png'), args=['-s', 'LEGACY_GL_EMULATION', '-lGL', '-lSDL'])

  @requires_graphics_hardware
  @no_swiftshader
  def test_cubegeom_pre_vao_es(self):
    self.btest(Path('third_party/cubegeom', 'cubegeom_pre_vao_es.c'), reference=Path('third_party/cubegeom', 'cubegeom_pre_vao.png'), args=['-s', 'FULL_ES2=1', '-lGL', '-lSDL'])

  @requires_graphics_hardware
  def test_cubegeom_u4fv_2(self):
    self.btest(Path('third_party/cubegeom', 'cubegeom_u4fv_2.c'), reference=Path('third_party/cubegeom', 'cubegeom_u4fv_2.png'), args=['-s', 'LEGACY_GL_EMULATION', '-lGL', '-lSDL'])

  @requires_graphics_hardware
  def test_cube_explosion(self):
    self.btest('cube_explosion.c', reference='cube_explosion.png', args=['-s', 'LEGACY_GL_EMULATION', '-lGL', '-lSDL'], also_proxied=True)

  @requires_graphics_hardware
  def test_glgettexenv(self):
    self.btest('glgettexenv.c', args=['-s', 'LEGACY_GL_EMULATION', '-lGL', '-lSDL'], expected=['1'])

  def test_sdl_canvas_blank(self):
    self.btest('sdl_canvas_blank.c', args=['-lSDL', '-lGL'], reference='sdl_canvas_blank.png')

  def test_sdl_canvas_palette(self):
    self.btest('sdl_canvas_palette.c', args=['-lSDL', '-lGL'], reference='sdl_canvas_palette.png')

  def test_sdl_canvas_twice(self):
    self.btest('sdl_canvas_twice.c', args=['-lSDL', '-lGL'], reference='sdl_canvas_twice.png')

  def test_sdl_set_clip_rect(self):
    self.btest('sdl_set_clip_rect.c', args=['-lSDL', '-lGL'], reference='sdl_set_clip_rect.png')

  def test_sdl_maprgba(self):
    self.btest('sdl_maprgba.c', args=['-lSDL', '-lGL'], reference='sdl_maprgba.png', reference_slack=3)

  def test_sdl_create_rgb_surface_from(self):
    self.btest('sdl_create_rgb_surface_from.c', args=['-lSDL', '-lGL'], reference='sdl_create_rgb_surface_from.png')

  def test_sdl_rotozoom(self):
    shutil.copyfile(test_file('screenshot.png'), 'screenshot.png')
    self.btest('sdl_rotozoom.c', reference='sdl_rotozoom.png', args=['--preload-file', 'screenshot.png', '--use-preload-plugins', '-lSDL', '-lGL'], reference_slack=3)

  def test_sdl_gfx_primitives(self):
    self.btest('sdl_gfx_primitives.c', args=['-lSDL', '-lGL'], reference='sdl_gfx_primitives.png', reference_slack=1)

  def test_sdl_canvas_palette_2(self):
    create_file('pre.js', '''
      Module['preRun'].push(function() {
        SDL.defaults.copyOnLock = false;
      });
    ''')

    create_file('args-r.js', '''
      Module['arguments'] = ['-r'];
    ''')

    create_file('args-g.js', '''
      Module['arguments'] = ['-g'];
    ''')

    create_file('args-b.js', '''
      Module['arguments'] = ['-b'];
    ''')

    self.btest('sdl_canvas_palette_2.c', reference='sdl_canvas_palette_r.png', args=['--pre-js', 'pre.js', '--pre-js', 'args-r.js', '-lSDL', '-lGL'])
    self.btest('sdl_canvas_palette_2.c', reference='sdl_canvas_palette_g.png', args=['--pre-js', 'pre.js', '--pre-js', 'args-g.js', '-lSDL', '-lGL'])
    self.btest('sdl_canvas_palette_2.c', reference='sdl_canvas_palette_b.png', args=['--pre-js', 'pre.js', '--pre-js', 'args-b.js', '-lSDL', '-lGL'])

  def test_sdl_ttf_render_text_solid(self):
    self.btest('sdl_ttf_render_text_solid.c', reference='sdl_ttf_render_text_solid.png', args=['-O2', '-s', 'INITIAL_MEMORY=16MB', '-lSDL', '-lGL'])

  def test_sdl_alloctext(self):
    self.btest('sdl_alloctext.c', expected='1', args=['-O2', '-s', 'INITIAL_MEMORY=16MB', '-lSDL', '-lGL'])

  def test_sdl_surface_refcount(self):
    self.btest('sdl_surface_refcount.c', args=['-lSDL'], expected='1')

  def test_sdl_free_screen(self):
    self.btest('sdl_free_screen.cpp', args=['-lSDL', '-lGL'], reference='htmltest.png')

  @requires_graphics_hardware
  def test_glbegin_points(self):
    shutil.copyfile(test_file('screenshot.png'), 'screenshot.png')
    self.btest('glbegin_points.c', reference='glbegin_points.png', args=['--preload-file', 'screenshot.png', '-s', 'LEGACY_GL_EMULATION', '-lGL', '-lSDL', '--use-preload-plugins'])

  @requires_graphics_hardware
  def test_s3tc(self):
    shutil.copyfile(test_file('screenshot.dds'), 'screenshot.dds')
    self.btest('s3tc.c', reference='s3tc.png', args=['--preload-file', 'screenshot.dds', '-s', 'LEGACY_GL_EMULATION', '-lGL', '-lSDL'])

  @requires_graphics_hardware
  def test_s3tc_ffp_only(self):
    shutil.copyfile(test_file('screenshot.dds'), 'screenshot.dds')
    self.btest('s3tc.c', reference='s3tc.png', args=['--preload-file', 'screenshot.dds', '-s', 'LEGACY_GL_EMULATION', '-s', 'GL_FFP_ONLY', '-lGL', '-lSDL'])

  @no_chrome('see #7117')
  @requires_graphics_hardware
  def test_aniso(self):
    shutil.copyfile(test_file('water.dds'), 'water.dds')
    self.btest('aniso.c', reference='aniso.png', reference_slack=2, args=['--preload-file', 'water.dds', '-s', 'LEGACY_GL_EMULATION', '-lGL', '-lSDL', '-Wno-incompatible-pointer-types'])

  @requires_graphics_hardware
  def test_tex_nonbyte(self):
    self.btest('tex_nonbyte.c', reference='tex_nonbyte.png', args=['-s', 'LEGACY_GL_EMULATION', '-lGL', '-lSDL'])

  @requires_graphics_hardware
  def test_float_tex(self):
    self.btest('float_tex.cpp', reference='float_tex.png', args=['-lGL', '-lglut'])

  @requires_graphics_hardware
  def test_subdata(self):
    self.btest('gl_subdata.cpp', reference='float_tex.png', args=['-lGL', '-lglut'])

  @requires_graphics_hardware
  def test_perspective(self):
    self.btest('perspective.c', reference='perspective.png', args=['-s', 'LEGACY_GL_EMULATION', '-lGL', '-lSDL'])

  @requires_graphics_hardware
  def test_glerror(self):
    self.btest('gl_error.c', expected='1', args=['-s', 'LEGACY_GL_EMULATION', '-lGL'])

  def test_openal_error(self):
    for args in [
      [],
      ['-lopenal', '-s', 'STRICT'],
      ['--closure=1']
    ]:
      print(args)
      self.btest('openal_error.c', expected='1', args=args)

  def test_openal_capture_sanity(self):
    self.btest('openal_capture_sanity.c', expected='0')

  def test_runtimelink(self):
    create_file('header.h', r'''
      struct point {
        int x, y;
      };
    ''')

    create_file('supp.c', r'''
      #include <stdio.h>
      #include "header.h"

      extern void mainFunc(int x);
      extern int mainInt;

      void suppFunc(struct point *p) {
        printf("supp: %d,%d\n", p->x, p->y);
        mainFunc(p->x + p->y);
        printf("supp see: %d\n", mainInt);
      }

      int suppInt = 76;
    ''')

    create_file('main.c', r'''
      #include <stdio.h>
      #include <assert.h>
      #include "header.h"

      extern void suppFunc(struct point *p);
      extern int suppInt;

      void mainFunc(int x) {
        printf("main: %d\n", x);
        assert(x == 56);
      }

      int mainInt = 543;

      int main( int argc, const char *argv[] ) {
        struct point p = { 54, 2 };
        suppFunc(&p);
        printf("main see: %d\nok.\n", suppInt);
        assert(suppInt == 76);
        return 0;
      }
    ''')
    self.run_process([EMCC, 'supp.c', '-o', 'supp.wasm', '-s', 'SIDE_MODULE', '-O2'])
    self.btest_exit('main.c', args=['-s', 'MAIN_MODULE=2', '-O2', 'supp.wasm'])

  def test_pre_run_deps(self):
    # Adding a dependency in preRun will delay run
    create_file('pre.js', '''
      Module.preRun = function() {
        addRunDependency();
        out('preRun called, added a dependency...');
        setTimeout(function() {
          Module.okk = 10;
          removeRunDependency()
        }, 2000);
      };
    ''')

    for mem in [0, 1]:
      self.btest('pre_run_deps.cpp', expected='10', args=['--pre-js', 'pre.js', '--memory-init-file', str(mem)])

  @no_wasm_backend('mem init file')
  def test_mem_init(self):
    create_file('pre.js', '''
      function myJSCallback() { // called from main()
        Module._note(1);
      }
      Module.preRun = function() {
        addOnPreMain(function() {
          Module._note(2);
        });
      };
    ''')
    create_file('post.js', '''
      var assert = function(check, text) {
        if (!check) {
          console.log('assert failed: ' + text);
          maybeReportResultToServer(9);
        }
      }
      Module._note(4); // this happens too early! and is overwritten when the mem init arrives
    ''')

    # with assertions, we notice when memory was written to too early
    self.btest('mem_init.cpp', expected='9', args=['-s', 'WASM=0', '--pre-js', 'pre.js', '--post-js', 'post.js', '--memory-init-file', '1'])
    # otherwise, we just overwrite
    self.btest('mem_init.cpp', expected='3', args=['-s', 'WASM=0', '--pre-js', 'pre.js', '--post-js', 'post.js', '--memory-init-file', '1', '-s', 'ASSERTIONS=0'])

  @no_wasm_backend('mem init file')
  def test_mem_init_request(self):
    def test(what, status):
      print(what, status)
      create_file('pre.js', '''
        var xhr = Module.memoryInitializerRequest = new XMLHttpRequest();
        xhr.open('GET', "''' + what + '''", true);
        xhr.responseType = 'arraybuffer';
        xhr.send(null);

        console.warn = function(x) {
          if (x.indexOf('a problem seems to have happened with Module.memoryInitializerRequest') >= 0) {
            var xhr = new XMLHttpRequest();
            xhr.open('GET', 'http://localhost:%s/report_result?0');
            setTimeout(xhr.onload = function() {
              console.log('close!');
              window.close();
            }, 1000);
            xhr.send();
            throw 'halt';
          }
          console.log('WARNING: ' + x);
        };
      ''' % self.port)
      self.btest('mem_init_request.cpp', expected=status, args=['-s', 'WASM=0', '--pre-js', 'pre.js', '--memory-init-file', '1'])

    test('test.html.mem', '1')
    test('nothing.nowhere', '0')

  def test_runtime_misuse(self):
    post_prep = '''
      var expected_ok = false;
      function doCcall(n) {
        ccall('note', 'string', ['number'], [n]);
      }
      var wrapped = cwrap('note', 'string', ['number']); // returns a string to suppress cwrap optimization
      function doCwrapCall(n) {
        var str = wrapped(n);
        out('got ' + str);
        assert(str === 'silly-string');
      }
      function doDirectCall(n) {
        Module['_note'](n);
      }
    '''
    post_test = '''
      var ok = false;
      try {
        doCcall(1);
        ok = true; // should fail and not reach here, runtime is not ready yet so ccall will abort
      } catch(e) {
        out('expected fail 1');
        assert(e.toString().indexOf('assert') >= 0); // assertion, not something else
        ABORT = false; // hackish
      }
      assert(ok === expected_ok);

      ok = false;
      try {
        doCwrapCall(2);
        ok = true; // should fail and not reach here, runtime is not ready yet so cwrap call will abort
      } catch(e) {
        out('expected fail 2');
        assert(e.toString().indexOf('assert') >= 0); // assertion, not something else
        ABORT = false; // hackish
      }
      assert(ok === expected_ok);

      ok = false;
      try {
        doDirectCall(3);
        ok = true; // should fail and not reach here, runtime is not ready yet so any code execution
      } catch(e) {
        out('expected fail 3');
        assert(e.toString().indexOf('assert') >= 0); // assertion, not something else
        ABORT = false; // hackish
      }
      assert(ok === expected_ok);
    '''

    post_hook = r'''
      function myJSCallback() {
        // Run on the next event loop, as code may run in a postRun right after main().
        setTimeout(function() {
          var xhr = new XMLHttpRequest();
          assert(Module.noted);
          xhr.open('GET', 'http://localhost:%s/report_result?' + HEAP32[Module.noted>>2]);
          xhr.send();
          setTimeout(function() { window.close() }, 1000);
        }, 0);
        // called from main, this is an ok time
        doCcall(100);
        doCwrapCall(200);
        doDirectCall(300);
      }
    ''' % self.port

    create_file('pre_runtime.js', r'''
      Module.onRuntimeInitialized = function(){
        myJSCallback();
      };
    ''')

    for filename, extra_args, second_code in [
      ('runtime_misuse.cpp', [], 600),
      ('runtime_misuse_2.cpp', ['--pre-js', 'pre_runtime.js'], 601) # 601, because no main means we *do* run another call after exit()
    ]:
      for mode in [[], ['-s', 'WASM=0']]:
        print('\n', filename, extra_args, mode)

        print('mem init, so async, call too early')
        create_file('post.js', post_prep + post_test + post_hook)
        self.btest(filename, expected='600', args=['--post-js', 'post.js', '--memory-init-file', '1', '-s', 'EXIT_RUNTIME'] + extra_args + mode, reporting=Reporting.NONE)
        print('sync startup, call too late')
        create_file('post.js', post_prep + 'Module.postRun.push(function() { ' + post_test + ' });' + post_hook)
        self.btest(filename, expected=str(second_code), args=['--post-js', 'post.js', '-s', 'EXIT_RUNTIME'] + extra_args + mode, reporting=Reporting.NONE)

        print('sync, runtime still alive, so all good')
        create_file('post.js', post_prep + 'expected_ok = true; Module.postRun.push(function() { ' + post_test + ' });' + post_hook)
        self.btest(filename, expected='606', args=['--post-js', 'post.js'] + extra_args + mode, reporting=Reporting.NONE)

  def test_cwrap_early(self):
    self.btest(Path('browser/cwrap_early.cpp'), args=['-O2', '-s', 'ASSERTIONS', '--pre-js', test_file('browser/cwrap_early.js'), '-s', 'EXPORTED_RUNTIME_METHODS=[cwrap]'], expected='0')

  def test_worker_api(self):
    self.compile_btest([test_file('worker_api_worker.cpp'), '-o', 'worker.js', '-s', 'BUILD_AS_WORKER', '-s', 'EXPORTED_FUNCTIONS=_one'])
    self.btest('worker_api_main.cpp', expected='566')

  def test_worker_api_2(self):
    self.compile_btest([test_file('worker_api_2_worker.cpp'), '-o', 'worker.js', '-s', 'BUILD_AS_WORKER', '-O2', '--minify=0', '-s', 'EXPORTED_FUNCTIONS=_one,_two,_three,_four', '--closure=1'])
    self.btest('worker_api_2_main.cpp', args=['-O2', '--minify=0'], expected='11')

  def test_worker_api_3(self):
    self.compile_btest([test_file('worker_api_3_worker.cpp'), '-o', 'worker.js', '-s', 'BUILD_AS_WORKER', '-s', 'EXPORTED_FUNCTIONS=_one'])
    self.btest('worker_api_3_main.cpp', expected='5')

  def test_worker_api_sleep(self):
    self.compile_btest([test_file('worker_api_worker_sleep.cpp'), '-o', 'worker.js', '-s', 'BUILD_AS_WORKER', '-s', 'EXPORTED_FUNCTIONS=_one', '-s', 'ASYNCIFY'])
    self.btest('worker_api_main.cpp', expected='566')

  def test_emscripten_async_wget2(self):
    self.btest_exit('test_emscripten_async_wget2.cpp')

  def test_emscripten_async_wget2_data(self):
    create_file('hello.txt', 'Hello Emscripten!')
    self.btest('test_emscripten_async_wget2_data.cpp', expected='0')
    time.sleep(10)

  def test_emscripten_async_wget_side_module(self):
    self.run_process([EMCC, test_file('browser_module.c'), '-o', 'lib.wasm', '-O2', '-s', 'SIDE_MODULE'])
    self.btest_exit('browser_main.c', args=['-O2', '-s', 'MAIN_MODULE=2'])

  @parameterized({
    'non-lz4': ([],),
    'lz4': (['-s', 'LZ4'],)
  })
  def test_preload_module(self, args):
    create_file('library.c', r'''
      #include <stdio.h>
      int library_func() {
        return 42;
      }
    ''')
    self.run_process([EMCC, 'library.c', '-s', 'SIDE_MODULE', '-O2', '-o', 'library.so'])
    create_file('main.c', r'''
      #include <dlfcn.h>
      #include <stdio.h>
      #include <emscripten.h>
      int main() {
        int found = EM_ASM_INT(
          return Module['preloadedWasm']['/library.so'] !== undefined;
        );
        if (!found) {
          return 1;
        }
        void *lib_handle = dlopen("/library.so", RTLD_NOW);
        if (!lib_handle) {
          return 2;
        }
        typedef int (*voidfunc)();
        voidfunc x = (voidfunc)dlsym(lib_handle, "library_func");
        if (!x || x() != 42) {
          return 3;
        }
        return 0;
      }
    ''')
    self.btest_exit(
      'main.c',
      args=['-s', 'MAIN_MODULE=2', '--preload-file', '.@/', '-O2', '--use-preload-plugins'] + args)

  def test_mmap_file(self):
    create_file('data.dat', 'data from the file ' + ('.' * 9000))
    self.btest(test_file('mmap_file.c'), expected='1', args=['--preload-file', 'data.dat'])

  # This does not actually verify anything except that --cpuprofiler and --memoryprofiler compiles.
  # Run interactive.test_cpuprofiler_memoryprofiler for interactive testing.
  @requires_graphics_hardware
  def test_cpuprofiler_memoryprofiler(self):
    self.btest('hello_world_gles.c', expected='0', args=['-DLONGTEST=1', '-DTEST_MEMORYPROFILER_ALLOCATIONS_MAP=1', '-O2', '--cpuprofiler', '--memoryprofiler', '-lGL', '-lglut', '-DANIMATE'])

  def test_uuid(self):
    # Run with ./runner browser.test_uuid
    # We run this test in Node/SPIDERMONKEY and browser environments because we try to make use of
    # high quality crypto random number generators such as crypto.getRandomValues or randomBytes (if available).

    # First run tests in Node and/or SPIDERMONKEY using self.run_js. Use closure compiler so we can check that
    # require('crypto').randomBytes and window.crypto.getRandomValues doesn't get minified out.
    self.run_process([EMCC, '-O2', '--closure=1', test_file('uuid/test.c'), '-o', 'test.js', '-luuid'])

    test_js_closure = read_file('test.js')

    # Check that test.js compiled with --closure 1 contains ").randomBytes" and "window.crypto.getRandomValues"
    assert ").randomBytes" in test_js_closure
    assert "window.crypto.getRandomValues" in test_js_closure

    out = self.run_js('test.js')
    print(out)

    # Tidy up files that might have been created by this test.
    try_delete(test_file('uuid/test.js'))
    try_delete(test_file('uuid/test.js.map'))

    # Now run test in browser
    self.btest(test_file('uuid/test.c'), '1', args=['-luuid'])

  @requires_graphics_hardware
  def test_glew(self):
    self.btest(test_file('glew.c'), args=['-lGL', '-lSDL', '-lGLEW'], expected='1')
    self.btest(test_file('glew.c'), args=['-lGL', '-lSDL', '-lGLEW', '-s', 'LEGACY_GL_EMULATION'], expected='1')
    self.btest(test_file('glew.c'), args=['-lGL', '-lSDL', '-lGLEW', '-DGLEW_MX'], expected='1')
    self.btest(test_file('glew.c'), args=['-lGL', '-lSDL', '-lGLEW', '-s', 'LEGACY_GL_EMULATION', '-DGLEW_MX'], expected='1')

  def test_doublestart_bug(self):
    create_file('pre.js', r'''
if (!Module['preRun']) Module['preRun'] = [];
Module["preRun"].push(function () {
  addRunDependency('test_run_dependency');
  removeRunDependency('test_run_dependency');
});
''')

    self.btest('doublestart.c', args=['--pre-js', 'pre.js'], expected='1')

  @parameterized({
    '': ([],),
    'closure': (['-O2', '-g1', '--closure=1', '-s', 'HTML5_SUPPORT_DEFERRING_USER_SENSITIVE_REQUESTS=0'],),
    'pthread': (['-s', 'USE_PTHREADS', '-s', 'PROXY_TO_PTHREAD'],),
    'legacy': (['-s', 'MIN_FIREFOX_VERSION=0', '-s', 'MIN_SAFARI_VERSION=0', '-s', 'MIN_IE_VERSION=0', '-s', 'MIN_EDGE_VERSION=0', '-s', 'MIN_CHROME_VERSION=0'],)
  })
  @requires_threads
  def test_html5_core(self, opts):
    self.btest(test_file('test_html5_core.c'), args=opts, expected='0')

  @requires_threads
  def test_html5_gamepad(self):
    for opts in [[], ['-O2', '-g1', '--closure=1'], ['-s', 'USE_PTHREADS', '-s', 'PROXY_TO_PTHREAD']]:
      print(opts)
      self.btest(test_file('test_gamepad.c'), args=[] + opts, expected='0')

  @requires_graphics_hardware
  def test_html5_webgl_create_context_no_antialias(self):
    for opts in [[], ['-O2', '-g1', '--closure=1'], ['-s', 'FULL_ES2=1']]:
      print(opts)
      self.btest(test_file('webgl_create_context.cpp'), args=opts + ['-DNO_ANTIALIAS', '-lGL'], expected='0')

  # This test supersedes the one above, but it's skipped in the CI because anti-aliasing is not well supported by the Mesa software renderer.
  @requires_threads
  @requires_graphics_hardware
  def test_html5_webgl_create_context(self):
    for opts in [[], ['-O2', '-g1', '--closure=1'], ['-s', 'FULL_ES2=1'], ['-s', 'USE_PTHREADS']]:
      print(opts)
      self.btest(test_file('webgl_create_context.cpp'), args=opts + ['-lGL'], expected='0')

  @requires_graphics_hardware
  # Verify bug https://github.com/emscripten-core/emscripten/issues/4556: creating a WebGL context to Module.canvas without an ID explicitly assigned to it.
  def test_html5_webgl_create_context2(self):
    self.btest(test_file('webgl_create_context2.cpp'), expected='0')

  @requires_graphics_hardware
  # Verify bug https://github.com/emscripten-core/emscripten/issues/4556: creating a WebGL context to Module.canvas without an ID explicitly assigned to it.
  # (this only makes sense in the old deprecated -s DISABLE_DEPRECATED_FIND_EVENT_TARGET_BEHAVIOR=0 mode)
  def test_html5_special_event_targets(self):
    self.btest(test_file('browser/html5_special_event_targets.cpp'), args=['-lGL'], expected='0')

  @requires_graphics_hardware
  def test_html5_webgl_destroy_context(self):
    for opts in [[], ['-O2', '-g1'], ['-s', 'FULL_ES2=1']]:
      print(opts)
      self.btest(test_file('webgl_destroy_context.cpp'), args=opts + ['--shell-file', test_file('webgl_destroy_context_shell.html'), '-lGL'], expected='0')

  @no_chrome('see #7373')
  @requires_graphics_hardware
  def test_webgl_context_params(self):
    if WINDOWS:
      self.skipTest('SKIPPED due to bug https://bugzilla.mozilla.org/show_bug.cgi?id=1310005 - WebGL implementation advertises implementation defined GL_IMPLEMENTATION_COLOR_READ_TYPE/FORMAT pair that it cannot read with')
    self.btest(test_file('webgl_color_buffer_readpixels.cpp'), args=['-lGL'], expected='0')

  # Test for PR#5373 (https://github.com/emscripten-core/emscripten/pull/5373)
  @requires_graphics_hardware
  def test_webgl_shader_source_length(self):
    for opts in [[], ['-s', 'FULL_ES2=1']]:
      print(opts)
      self.btest(test_file('webgl_shader_source_length.cpp'), args=opts + ['-lGL'], expected='0')

  # Tests calling glGetString(GL_UNMASKED_VENDOR_WEBGL).
  @requires_graphics_hardware
  def test_webgl_unmasked_vendor_webgl(self):
    self.btest(test_file('webgl_unmasked_vendor_webgl.c'), args=['-lGL'], expected='0')

  @requires_graphics_hardware
  def test_webgl2(self):
    for opts in [
      ['-s', 'MIN_CHROME_VERSION=0'],
      ['-O2', '-g1', '--closure=1', '-s', 'WORKAROUND_OLD_WEBGL_UNIFORM_UPLOAD_IGNORED_OFFSET_BUG'],
      ['-s', 'FULL_ES2=1'],
    ]:
      print(opts)
      self.btest(test_file('webgl2.cpp'), args=['-s', 'MAX_WEBGL_VERSION=2', '-lGL'] + opts, expected='0')

  # Tests the WebGL 2 glGetBufferSubData() functionality.
  @requires_graphics_hardware
  def test_webgl2_get_buffer_sub_data(self):
    self.btest(test_file('webgl2_get_buffer_sub_data.cpp'), args=['-s', 'MAX_WEBGL_VERSION=2', '-lGL'], expected='0')

  @requires_graphics_hardware
  @requires_threads
  def test_webgl2_pthreads(self):
    # test that a program can be compiled with pthreads and render WebGL2 properly on the main thread
    # (the testcase doesn't even use threads, but is compiled with thread support).
    self.btest(test_file('webgl2.cpp'), args=['-s', 'MAX_WEBGL_VERSION=2', '-lGL', '-s', 'USE_PTHREADS'], expected='0')

  @requires_graphics_hardware
  def test_webgl2_objects(self):
    self.btest(test_file('webgl2_objects.cpp'), args=['-s', 'MAX_WEBGL_VERSION=2', '-lGL'], expected='0')

  @requires_graphics_hardware
  def test_html5_webgl_api(self):
    for mode in [['-s', 'OFFSCREENCANVAS_SUPPORT', '-s', 'USE_PTHREADS', '-s', 'PROXY_TO_PTHREAD'],
                 ['-s', 'OFFSCREEN_FRAMEBUFFER', '-s', 'USE_PTHREADS', '-s', 'PROXY_TO_PTHREAD'],
                 []]:
      if 'OFFSCREENCANVAS_SUPPORT' in mode and os.getenv('EMTEST_LACKS_OFFSCREEN_CANVAS'):
        continue
      self.btest(test_file('html5_webgl.c'), args=['-s', 'MAX_WEBGL_VERSION=2', '-lGL'] + mode, expected='0')

  @requires_graphics_hardware
  def test_webgl2_ubos(self):
    self.btest(test_file('webgl2_ubos.cpp'), args=['-s', 'MAX_WEBGL_VERSION=2', '-lGL'], expected='0')

  @requires_graphics_hardware
  def test_webgl2_garbage_free_entrypoints(self):
    self.btest(test_file('webgl2_garbage_free_entrypoints.cpp'), args=['-s', 'MAX_WEBGL_VERSION=2', '-DTEST_WEBGL2=1'], expected='1')
    self.btest(test_file('webgl2_garbage_free_entrypoints.cpp'), expected='1')

  @requires_graphics_hardware
  def test_webgl2_backwards_compatibility_emulation(self):
    self.btest(test_file('webgl2_backwards_compatibility_emulation.cpp'), args=['-s', 'MAX_WEBGL_VERSION=2', '-s', 'WEBGL2_BACKWARDS_COMPATIBILITY_EMULATION=1'], expected='0')

  @requires_graphics_hardware
  def test_webgl2_runtime_no_context(self):
    # tests that if we support WebGL1 and 2, and WebGL2RenderingContext exists,
    # but context creation fails, that we can then manually try to create a
    # WebGL1 context and succeed.
    self.btest(test_file('test_webgl2_runtime_no_context.cpp'), args=['-s', 'MAX_WEBGL_VERSION=2'], expected='1')

  @requires_graphics_hardware
  def test_webgl2_invalid_teximage2d_type(self):
    self.btest(test_file('webgl2_invalid_teximage2d_type.cpp'), args=['-s', 'MAX_WEBGL_VERSION=2'], expected='0')

  @requires_graphics_hardware
  def test_webgl_with_closure(self):
    self.btest(test_file('webgl_with_closure.cpp'), args=['-O2', '-s', 'MAX_WEBGL_VERSION=2', '--closure=1', '-lGL'], expected='0')

  # Tests that -s GL_ASSERTIONS=1 and glVertexAttribPointer with packed types works
  @requires_graphics_hardware
  def test_webgl2_packed_types(self):
    self.btest(test_file('webgl2_draw_packed_triangle.c'), args=['-lGL', '-s', 'MAX_WEBGL_VERSION=2', '-s', 'GL_ASSERTIONS'], expected='0')

  @requires_graphics_hardware
  def test_webgl2_pbo(self):
    self.btest(test_file('webgl2_pbo.cpp'), args=['-s', 'MAX_WEBGL_VERSION=2', '-lGL'], expected='0')

  @no_firefox('fails on CI likely due to GPU drivers there')
  @requires_graphics_hardware
  def test_webgl2_sokol_mipmap(self):
    self.btest(test_file('third_party/sokol/mipmap-emsc.c'), args=['-s', 'MAX_WEBGL_VERSION=2', '-lGL', '-O1'],
               reference=Path('third_party/sokol', 'mipmap-emsc.png'), reference_slack=2)

  @no_firefox('fails on CI likely due to GPU drivers there')
  @requires_graphics_hardware
  def test_webgl2_sokol_mrt(self):
    self.btest(test_file('third_party/sokol/mrt-emcc.c'), args=['-s', 'MAX_WEBGL_VERSION=2', '-lGL'],
               reference=Path('third_party/sokol', 'mrt-emcc.png'))

  @requires_graphics_hardware
  def test_webgl2_sokol_arraytex(self):
    self.btest(test_file('third_party/sokol/arraytex-emsc.c'), args=['-s', 'MAX_WEBGL_VERSION=2', '-lGL'],
               reference=Path('third_party/sokol', 'arraytex-emsc.png'))

  def test_sdl_touch(self):
    for opts in [[], ['-O2', '-g1', '--closure=1']]:
      print(opts)
      self.btest(test_file('sdl_touch.c'), args=opts + ['-DAUTOMATE_SUCCESS=1', '-lSDL', '-lGL'], expected='0')

  def test_html5_mouse(self):
    for opts in [[], ['-O2', '-g1', '--closure=1']]:
      print(opts)
      self.btest(test_file('test_html5_mouse.c'), args=opts + ['-DAUTOMATE_SUCCESS=1'], expected='0')

  def test_sdl_mousewheel(self):
    for opts in [[], ['-O2', '-g1', '--closure=1']]:
      print(opts)
      self.btest(test_file('test_sdl_mousewheel.c'), args=opts + ['-DAUTOMATE_SUCCESS=1', '-lSDL', '-lGL'], expected='0')

  def test_wget(self):
    create_file('test.txt', 'emscripten')
    self.btest_exit(test_file('test_wget.c'), args=['-s', 'ASYNCIFY'])

  def test_wget_data(self):
    create_file('test.txt', 'emscripten')
    self.btest_exit(test_file('test_wget_data.c'), args=['-O2', '-g2', '-s', 'ASYNCIFY'])

  @parameterized({
    '': ([],),
    'es6': (['-s', 'EXPORT_ES6=1'],),
  })
  def test_locate_file(self, args):
    for wasm in [0, 1]:
      self.clear()
      create_file('src.cpp', r'''
        #include <stdio.h>
        #include <string.h>
        #include <assert.h>
        int main() {
          FILE *f = fopen("data.txt", "r");
          assert(f && "could not open file");
          char buf[100];
          int num = fread(buf, 1, 20, f);
          assert(num == 20 && "could not read 20 bytes");
          buf[20] = 0;
          fclose(f);
          int result = !strcmp("load me right before", buf);
          printf("|%s| : %d\n", buf, result);
          REPORT_RESULT(result);
          return 0;
        }
      ''')
      create_file('data.txt', 'load me right before...')
      create_file('pre.js', 'Module.locateFile = function(x) { return "sub/" + x };')
      self.run_process([FILE_PACKAGER, 'test.data', '--preload', 'data.txt'], stdout=open('data.js', 'w'))
      # put pre.js first, then the file packager data, so locateFile is there for the file loading code
      self.compile_btest(['src.cpp', '-O2', '-g', '--pre-js', 'pre.js', '--pre-js', 'data.js', '-o', 'page.html', '-s', 'FORCE_FILESYSTEM', '-s', 'WASM=' + str(wasm)] + args)
      ensure_dir('sub')
      if wasm:
        shutil.move('page.wasm', Path('sub/page.wasm'))
      else:
        shutil.move('page.html.mem', Path('sub/page.html.mem'))
      shutil.move('test.data', Path('sub/test.data'))
      self.run_browser('page.html', None, '/report_result?1')

      # alternatively, put locateFile in the HTML
      print('in html')

      create_file('shell.html', '''
        <body>
          <script>
            var Module = {
              locateFile: function(x) { return "sub/" + x }
            };
          </script>

          {{{ SCRIPT }}}
        </body>
      ''')

      def in_html(expected):
        self.compile_btest(['src.cpp', '-O2', '-g', '--shell-file', 'shell.html', '--pre-js', 'data.js', '-o', 'page.html', '-s', 'SAFE_HEAP', '-s', 'ASSERTIONS', '-s', 'FORCE_FILESYSTEM', '-s', 'WASM=' + str(wasm)] + args)
        if wasm:
          shutil.move('page.wasm', Path('sub/page.wasm'))
        else:
          shutil.move('page.html.mem', Path('sub/page.html.mem'))
        self.run_browser('page.html', None, '/report_result?' + expected)

      in_html('1')

      # verify that the mem init request succeeded in the latter case
      if not wasm:
        create_file('src.cpp', r'''
          #include <stdio.h>
          #include <emscripten.h>

          int main() {
            int result = EM_ASM_INT({
              return Module['memoryInitializerRequest'].status;
            });
            printf("memory init request: %d\n", result);
            REPORT_RESULT(result);
            return 0;
          }
          ''')

        in_html('200')

  @requires_graphics_hardware
  @parameterized({
    'no_gl': (['-DCLIENT_API=GLFW_NO_API'],),
    'gl_es': (['-DCLIENT_API=GLFW_OPENGL_ES_API'],)
  })
  def test_glfw3(self, args):
    for opts in [[], ['-s', 'LEGACY_GL_EMULATION'], ['-Os', '--closure=1']]:
      print(opts)
      self.btest(test_file('glfw3.c'), args=['-s', 'USE_GLFW=3', '-lglfw', '-lGL'] + args + opts, expected='1')

  @requires_graphics_hardware
  def test_glfw_events(self):
    self.btest(test_file('glfw_events.c'), args=['-s', 'USE_GLFW=2', "-DUSE_GLFW=2", '-lglfw', '-lGL'], expected='1')
    self.btest(test_file('glfw_events.c'), args=['-s', 'USE_GLFW=3', "-DUSE_GLFW=3", '-lglfw', '-lGL'], expected='1')

  @requires_graphics_hardware
  def test_sdl2_image(self):
    # load an image file, get pixel data. Also O2 coverage for --preload-file, and memory-init
    shutil.copyfile(test_file('screenshot.jpg'), 'screenshot.jpg')

    for mem in [0, 1]:
      for dest, dirname, basename in [('screenshot.jpg', '/', 'screenshot.jpg'),
                                      ('screenshot.jpg@/assets/screenshot.jpg', '/assets', 'screenshot.jpg')]:
        self.compile_btest([
          test_file('sdl2_image.c'), '-o', 'page.html', '-O2', '--memory-init-file', str(mem),
          '--preload-file', dest, '-DSCREENSHOT_DIRNAME="' + dirname + '"', '-DSCREENSHOT_BASENAME="' + basename + '"', '-s', 'USE_SDL=2', '-s', 'USE_SDL_IMAGE=2', '--use-preload-plugins'
        ])
        self.run_browser('page.html', '', '/report_result?600')

  @requires_graphics_hardware
  def test_sdl2_image_jpeg(self):
    shutil.copyfile(test_file('screenshot.jpg'), 'screenshot.jpeg')
    self.compile_btest([
      test_file('sdl2_image.c'), '-o', 'page.html',
      '--preload-file', 'screenshot.jpeg', '-DSCREENSHOT_DIRNAME="/"', '-DSCREENSHOT_BASENAME="screenshot.jpeg"', '-s', 'USE_SDL=2', '-s', 'USE_SDL_IMAGE=2', '--use-preload-plugins'
    ])
    self.run_browser('page.html', '', '/report_result?600')

  @requires_graphics_hardware
  def test_sdl2_image_formats(self):
    shutil.copyfile(test_file('screenshot.png'), 'screenshot.png')
    shutil.copyfile(test_file('screenshot.jpg'), 'screenshot.jpg')
    self.btest('sdl2_image.c', expected='512', args=['--preload-file', 'screenshot.png', '-DSCREENSHOT_DIRNAME="/"', '-DSCREENSHOT_BASENAME="screenshot.png"',
                                                     '-DNO_PRELOADED', '-s', 'USE_SDL=2', '-s', 'USE_SDL_IMAGE=2', '-s', 'SDL2_IMAGE_FORMATS=["png"]'])
    self.btest('sdl2_image.c', expected='600', args=['--preload-file', 'screenshot.jpg', '-DSCREENSHOT_DIRNAME="/"', '-DSCREENSHOT_BASENAME="screenshot.jpg"',
                                                     '-DBITSPERPIXEL=24', '-DNO_PRELOADED', '-s', 'USE_SDL=2', '-s', 'USE_SDL_IMAGE=2', '-s', 'SDL2_IMAGE_FORMATS=["jpg"]'])

  def test_sdl2_key(self):
    create_file('pre.js', '''
      Module.postRun = function() {
        function doOne() {
          Module._one();
          setTimeout(doOne, 1000/60);
        }
        setTimeout(doOne, 1000/60);
      }

      function keydown(c) {
        var event = new KeyboardEvent("keydown", { 'keyCode': c, 'charCode': c, 'view': window, 'bubbles': true, 'cancelable': true });
        var prevented = !document.dispatchEvent(event);

        //send keypress if not prevented
        if (!prevented) {
          var event = new KeyboardEvent("keypress", { 'keyCode': c, 'charCode': c, 'view': window, 'bubbles': true, 'cancelable': true });
          document.dispatchEvent(event);
        }
      }

      function keyup(c) {
        var event = new KeyboardEvent("keyup", { 'keyCode': c, 'charCode': c, 'view': window, 'bubbles': true, 'cancelable': true });
        document.dispatchEvent(event);
      }
    ''')

    self.compile_btest([test_file('sdl2_key.c'), '-o', 'page.html', '-s', 'USE_SDL=2', '--pre-js', 'pre.js', '-s', 'EXPORTED_FUNCTIONS=_main,_one'])
    self.run_browser('page.html', '', '/report_result?37182145')

  def test_sdl2_text(self):
    create_file('pre.js', '''
      Module.postRun = function() {
        function doOne() {
          Module._one();
          setTimeout(doOne, 1000/60);
        }
        setTimeout(doOne, 1000/60);
      }

      function simulateKeyEvent(c) {
        var event = new KeyboardEvent("keypress", { 'keyCode': c, 'charCode': c, 'view': window, 'bubbles': true, 'cancelable': true });
        document.body.dispatchEvent(event);
      }
    ''')

    self.compile_btest([test_file('sdl2_text.c'), '-o', 'page.html', '--pre-js', 'pre.js', '-s', 'EXPORTED_FUNCTIONS=_main,_one', '-s', 'USE_SDL=2'])
    self.run_browser('page.html', '', '/report_result?1')

  @requires_graphics_hardware
  def test_sdl2_mouse(self):
    create_file('pre.js', '''
      function simulateMouseEvent(x, y, button) {
        var event = document.createEvent("MouseEvents");
        if (button >= 0) {
          var event1 = document.createEvent("MouseEvents");
          event1.initMouseEvent('mousedown', true, true, window,
                     1, Module['canvas'].offsetLeft + x, Module['canvas'].offsetTop + y, Module['canvas'].offsetLeft + x, Module['canvas'].offsetTop + y,
                     0, 0, 0, 0,
                     button, null);
          Module['canvas'].dispatchEvent(event1);
          var event2 = document.createEvent("MouseEvents");
          event2.initMouseEvent('mouseup', true, true, window,
                     1, Module['canvas'].offsetLeft + x, Module['canvas'].offsetTop + y, Module['canvas'].offsetLeft + x, Module['canvas'].offsetTop + y,
                     0, 0, 0, 0,
                     button, null);
          Module['canvas'].dispatchEvent(event2);
        } else {
          var event1 = document.createEvent("MouseEvents");
          event1.initMouseEvent('mousemove', true, true, window,
                     0, Module['canvas'].offsetLeft + x, Module['canvas'].offsetTop + y, Module['canvas'].offsetLeft + x, Module['canvas'].offsetTop + y,
                     0, 0, 0, 0,
                     0, null);
          Module['canvas'].dispatchEvent(event1);
        }
      }
      window['simulateMouseEvent'] = simulateMouseEvent;
    ''')

    self.compile_btest([test_file('sdl2_mouse.c'), '-O2', '--minify=0', '-o', 'page.html', '--pre-js', 'pre.js', '-s', 'USE_SDL=2'])
    self.run_browser('page.html', '', '/report_result?1')

  @requires_graphics_hardware
  def test_sdl2_mouse_offsets(self):
    create_file('pre.js', '''
      function simulateMouseEvent(x, y, button) {
        var event = document.createEvent("MouseEvents");
        if (button >= 0) {
          var event1 = document.createEvent("MouseEvents");
          event1.initMouseEvent('mousedown', true, true, window,
                     1, x, y, x, y,
                     0, 0, 0, 0,
                     button, null);
          Module['canvas'].dispatchEvent(event1);
          var event2 = document.createEvent("MouseEvents");
          event2.initMouseEvent('mouseup', true, true, window,
                     1, x, y, x, y,
                     0, 0, 0, 0,
                     button, null);
          Module['canvas'].dispatchEvent(event2);
        } else {
          var event1 = document.createEvent("MouseEvents");
          event1.initMouseEvent('mousemove', true, true, window,
                     0, x, y, x, y,
                     0, 0, 0, 0,
                     0, null);
          Module['canvas'].dispatchEvent(event1);
        }
      }
      window['simulateMouseEvent'] = simulateMouseEvent;
    ''')
    create_file('page.html', '''
      <html>
        <head>
          <style type="text/css">
            html, body { margin: 0; padding: 0; }
            #container {
              position: absolute;
              left: 5px; right: 0;
              top: 5px; bottom: 0;
            }
            #canvas {
              position: absolute;
              left: 0; width: 600px;
              top: 0; height: 450px;
            }
            textarea {
              margin-top: 500px;
              margin-left: 5px;
              width: 600px;
            }
          </style>
        </head>
        <body>
          <div id="container">
            <canvas id="canvas"></canvas>
          </div>
          <textarea id="output" rows="8"></textarea>
          <script type="text/javascript">
            var Module = {
              canvas: document.getElementById('canvas'),
              print: (function() {
                var element = document.getElementById('output');
                element.value = ''; // clear browser cache
                return function(text) {
                  if (arguments.length > 1) text = Array.prototype.slice.call(arguments).join(' ');
                  element.value += text + "\\n";
                  element.scrollTop = element.scrollHeight; // focus on bottom
                };
              })()
            };
          </script>
          <script type="text/javascript" src="sdl2_mouse.js"></script>
        </body>
      </html>
    ''')

    self.compile_btest([test_file('sdl2_mouse.c'), '-DTEST_SDL_MOUSE_OFFSETS=1', '-O2', '--minify=0', '-o', 'sdl2_mouse.js', '--pre-js', 'pre.js', '-s', 'USE_SDL=2'])
    self.run_browser('page.html', '', '/report_result?1')

  @requires_threads
  def test_sdl2_threads(self):
      self.btest('sdl2_threads.c', expected='4', args=['-s', 'USE_PTHREADS', '-s', 'USE_SDL=2', '-s', 'PROXY_TO_PTHREAD'])

  @requires_graphics_hardware
  def test_sdl2glshader(self):
    self.btest('sdl2glshader.c', reference='sdlglshader.png', args=['-s', 'USE_SDL=2', '-O2', '--closure=1', '-g1', '-s', 'LEGACY_GL_EMULATION'])
    self.btest('sdl2glshader.c', reference='sdlglshader.png', args=['-s', 'USE_SDL=2', '-O2', '-s', 'LEGACY_GL_EMULATION'], also_proxied=True) # XXX closure fails on proxy

  @requires_graphics_hardware
  def test_sdl2_canvas_blank(self):
    self.btest('sdl2_canvas_blank.c', reference='sdl_canvas_blank.png', args=['-s', 'USE_SDL=2'])

  @requires_graphics_hardware
  def test_sdl2_canvas_palette(self):
    self.btest('sdl2_canvas_palette.c', reference='sdl_canvas_palette.png', args=['-s', 'USE_SDL=2'])

  @requires_graphics_hardware
  def test_sdl2_canvas_twice(self):
    self.btest('sdl2_canvas_twice.c', reference='sdl_canvas_twice.png', args=['-s', 'USE_SDL=2'])

  @requires_graphics_hardware
  def test_sdl2_gfx(self):
    self.btest('sdl2_gfx.cpp', args=['-s', 'USE_SDL=2', '-s', 'USE_SDL_GFX=2'], reference='sdl2_gfx.png', reference_slack=2)

  @requires_graphics_hardware
  def test_sdl2_canvas_palette_2(self):
    create_file('args-r.js', '''
      Module['arguments'] = ['-r'];
    ''')

    create_file('args-g.js', '''
      Module['arguments'] = ['-g'];
    ''')

    create_file('args-b.js', '''
      Module['arguments'] = ['-b'];
    ''')

    self.btest('sdl2_canvas_palette_2.c', reference='sdl_canvas_palette_r.png', args=['-s', 'USE_SDL=2', '--pre-js', 'args-r.js'])
    self.btest('sdl2_canvas_palette_2.c', reference='sdl_canvas_palette_g.png', args=['-s', 'USE_SDL=2', '--pre-js', 'args-g.js'])
    self.btest('sdl2_canvas_palette_2.c', reference='sdl_canvas_palette_b.png', args=['-s', 'USE_SDL=2', '--pre-js', 'args-b.js'])

  def test_sdl2_swsurface(self):
    self.btest('sdl2_swsurface.c', expected='1', args=['-s', 'USE_SDL=2', '-s', 'INITIAL_MEMORY=64MB'])

  @requires_graphics_hardware
  def test_sdl2_image_prepare(self):
    # load an image file, get pixel data.
    shutil.copyfile(test_file('screenshot.jpg'), 'screenshot.not')
    self.btest('sdl2_image_prepare.c', reference='screenshot.jpg', args=['--preload-file', 'screenshot.not', '-s', 'USE_SDL=2', '-s', 'USE_SDL_IMAGE=2'], manually_trigger_reftest=True)

  @requires_graphics_hardware
  def test_sdl2_image_prepare_data(self):
    # load an image file, get pixel data.
    shutil.copyfile(test_file('screenshot.jpg'), 'screenshot.not')
    self.btest('sdl2_image_prepare_data.c', reference='screenshot.jpg', args=['--preload-file', 'screenshot.not', '-s', 'USE_SDL=2', '-s', 'USE_SDL_IMAGE=2'], manually_trigger_reftest=True)

  @requires_graphics_hardware
  def test_sdl2_canvas_proxy(self):
    def post():
      html = read_file('test.html')
      html = html.replace('</body>', '''
<script>
function assert(x, y) { if (!x) throw 'assertion failed ' + y }

%s

var windowClose = window.close;
window.close = function() {
  // wait for rafs to arrive and the screen to update before reftesting
  setTimeout(function() {
    doReftest();
    setTimeout(windowClose, 5000);
  }, 1000);
};
</script>
</body>''' % read_file('reftest.js'))
      create_file('test.html', html)

    create_file('data.txt', 'datum')

    self.btest('sdl2_canvas_proxy.c', reference='sdl2_canvas.png', args=['-s', 'USE_SDL=2', '--proxy-to-worker', '--preload-file', 'data.txt', '-s', 'GL_TESTING'], manual_reference=True, post_build=post)

  def test_sdl2_pumpevents(self):
    # key events should be detected using SDL_PumpEvents
    create_file('pre.js', '''
      function keydown(c) {
        var event = new KeyboardEvent("keydown", { 'keyCode': c, 'charCode': c, 'view': window, 'bubbles': true, 'cancelable': true });
        document.dispatchEvent(event);
      }
    ''')
    self.btest('sdl2_pumpevents.c', expected='7', args=['--pre-js', 'pre.js', '-s', 'USE_SDL=2'])

  def test_sdl2_timer(self):
    self.btest('sdl2_timer.c', expected='5', args=['-s', 'USE_SDL=2'])

  def test_sdl2_canvas_size(self):
    self.btest('sdl2_canvas_size.c', expected='1', args=['-s', 'USE_SDL=2'])

  @requires_graphics_hardware
  def test_sdl2_gl_read(self):
    # SDL, OpenGL, readPixels
    self.compile_btest([test_file('sdl2_gl_read.c'), '-o', 'something.html', '-s', 'USE_SDL=2'])
    self.run_browser('something.html', '.', '/report_result?1')

  @requires_graphics_hardware
  def test_sdl2_glmatrixmode_texture(self):
    self.btest('sdl2_glmatrixmode_texture.c', reference='sdl2_glmatrixmode_texture.png',
               args=['-s', 'LEGACY_GL_EMULATION', '-s', 'USE_SDL=2'],
               message='You should see a (top) red-white and (bottom) white-red image.')

  @requires_graphics_hardware
  def test_sdl2_gldrawelements(self):
    self.btest('sdl2_gldrawelements.c', reference='sdl2_gldrawelements.png',
               args=['-s', 'LEGACY_GL_EMULATION', '-s', 'USE_SDL=2'],
               message='GL drawing modes. Bottom: points, lines, line loop, line strip. Top: triangles, triangle strip, triangle fan, quad.')

  @requires_graphics_hardware
  def test_sdl2_glclipplane_gllighting(self):
    self.btest('sdl2_glclipplane_gllighting.c', reference='sdl2_glclipplane_gllighting.png',
               args=['-s', 'LEGACY_GL_EMULATION', '-s', 'USE_SDL=2'],
               message='glClipPlane and GL_LIGHTING emulation. You should see a torus cut open on one side with lighting from one lightsource applied.')

  @requires_graphics_hardware
  def test_sdl2_fog_simple(self):
    shutil.copyfile(test_file('screenshot.png'), 'screenshot.png')
    self.btest('sdl2_fog_simple.c', reference='screenshot-fog-simple.png',
               args=['-s', 'USE_SDL=2', '-s', 'USE_SDL_IMAGE=2', '-O2', '--minify=0', '--preload-file', 'screenshot.png', '-s', 'LEGACY_GL_EMULATION', '--use-preload-plugins'],
               message='You should see an image with fog.')

  @requires_graphics_hardware
  def test_sdl2_fog_negative(self):
    shutil.copyfile(test_file('screenshot.png'), 'screenshot.png')
    self.btest('sdl2_fog_negative.c', reference='screenshot-fog-negative.png',
               args=['-s', 'USE_SDL=2', '-s', 'USE_SDL_IMAGE=2', '--preload-file', 'screenshot.png', '-s', 'LEGACY_GL_EMULATION', '--use-preload-plugins'],
               message='You should see an image with fog.')

  @requires_graphics_hardware
  def test_sdl2_fog_density(self):
    shutil.copyfile(test_file('screenshot.png'), 'screenshot.png')
    self.btest('sdl2_fog_density.c', reference='screenshot-fog-density.png',
               args=['-s', 'USE_SDL=2', '-s', 'USE_SDL_IMAGE=2', '--preload-file', 'screenshot.png', '-s', 'LEGACY_GL_EMULATION', '--use-preload-plugins'],
               message='You should see an image with fog.')

  @requires_graphics_hardware
  def test_sdl2_fog_exp2(self):
    shutil.copyfile(test_file('screenshot.png'), 'screenshot.png')
    self.btest('sdl2_fog_exp2.c', reference='screenshot-fog-exp2.png',
               args=['-s', 'USE_SDL=2', '-s', 'USE_SDL_IMAGE=2', '--preload-file', 'screenshot.png', '-s', 'LEGACY_GL_EMULATION', '--use-preload-plugins'],
               message='You should see an image with fog.')

  @requires_graphics_hardware
  def test_sdl2_fog_linear(self):
    shutil.copyfile(test_file('screenshot.png'), 'screenshot.png')
    self.btest('sdl2_fog_linear.c', reference='screenshot-fog-linear.png', reference_slack=1,
               args=['-s', 'USE_SDL=2', '-s', 'USE_SDL_IMAGE=2', '--preload-file', 'screenshot.png', '-s', 'LEGACY_GL_EMULATION', '--use-preload-plugins'],
               message='You should see an image with fog.')

  def test_sdl2_unwasteful(self):
    self.btest('sdl2_unwasteful.cpp', expected='1', args=['-s', 'USE_SDL=2', '-O1'])

  def test_sdl2_canvas_write(self):
    self.btest('sdl2_canvas_write.cpp', expected='0', args=['-s', 'USE_SDL=2'])

  @requires_graphics_hardware
  def test_sdl2_gl_frames_swap(self):
    def post_build(*args):
      self.post_manual_reftest(*args)
      html = read_file('test.html')
      html2 = html.replace('''Module['postRun'] = doReftest;''', '') # we don't want the very first frame
      assert html != html2
      create_file('test.html', html2)
    self.btest('sdl2_gl_frames_swap.c', reference='sdl2_gl_frames_swap.png', args=['--proxy-to-worker', '-s', 'GL_TESTING', '-s', 'USE_SDL=2'], manual_reference=True, post_build=post_build)

  @requires_graphics_hardware
  def test_sdl2_ttf(self):
    shutil.copy2(test_file('freetype/LiberationSansBold.ttf'), self.get_dir())
    self.btest('sdl2_ttf.c', reference='sdl2_ttf.png',
               args=['-O2', '-s', 'USE_SDL=2', '-s', 'USE_SDL_TTF=2', '--embed-file', 'LiberationSansBold.ttf'],
               message='You should see colorful "hello" and "world" in the window')

  @requires_graphics_hardware
  def test_sdl2_ttf_rtl(self):
    shutil.copy2(test_file('third_party/notofont/NotoNaskhArabic-Regular.ttf'), self.get_dir())
    self.btest('sdl2_ttf_rtl.c', reference='sdl2_ttf_rtl.png',
               args=['-O2', '-s', 'USE_SDL=2', '-s', 'USE_SDL_TTF=2', '--embed-file', 'NotoNaskhArabic-Regular.ttf'],
               message='You should see colorful "سلام" and "جهان" with shaped Arabic script in the window')

  def test_sdl2_custom_cursor(self):
    shutil.copyfile(test_file('cursor.bmp'), 'cursor.bmp')
    self.btest('sdl2_custom_cursor.c', expected='1', args=['--preload-file', 'cursor.bmp', '-s', 'USE_SDL=2'])

  def test_sdl2_misc(self):
    self.btest_exit('sdl2_misc.c', args=['-s', 'USE_SDL=2'])

  def test_sdl2_misc_main_module(self):
    self.btest_exit('sdl2_misc.c', args=['-s', 'USE_SDL=2', '-s', 'MAIN_MODULE'])

  def test_sdl2_misc_via_object(self):
    self.run_process([EMCC, '-c', test_file('sdl2_misc.c'), '-s', 'USE_SDL=2', '-o', 'test.o'])
    self.compile_btest(['test.o', '-s', 'EXIT_RUNTIME', '-s', 'USE_SDL=2', '-o', 'test.html'])
    self.run_browser('test.html', '...', '/report_result?exit:0')

  @parameterized({
    'dash_s': (['-s', 'USE_SDL=2', '-s', 'USE_SDL_MIXER=2'],),
    'dash_l': (['-lSDL2', '-lSDL2_mixer'],),
  })
  @requires_sound_hardware
  def test_sdl2_mixer_wav(self, flags):
    shutil.copyfile(test_file('sounds/the_entertainer.wav'), 'sound.wav')
    self.btest('sdl2_mixer_wav.c', expected='1', args=['--preload-file', 'sound.wav', '-s', 'INITIAL_MEMORY=33554432'] + flags)

  @parameterized({
    'wav': ([],         '0',            'the_entertainer.wav'),
    'ogg': (['ogg'],    'MIX_INIT_OGG', 'alarmvictory_1.ogg'),
    'mp3': (['mp3'],    'MIX_INIT_MP3', 'pudinha.mp3'),
    'mod': (['mod'],    'MIX_INIT_MOD', 'bleep.xm'),
  })
  @requires_sound_hardware
  def test_sdl2_mixer_music(self, formats, flags, music_name):
    shutil.copyfile(test_file('sounds', music_name), music_name)
    self.btest('sdl2_mixer_music.c', expected='1', args=[
      '--preload-file', music_name,
      '-DSOUND_PATH=' + json.dumps(music_name),
      '-DFLAGS=' + flags,
      '-s', 'USE_SDL=2',
      '-s', 'USE_SDL_MIXER=2',
      '-s', 'SDL2_MIXER_FORMATS=' + json.dumps(formats),
      '-s', 'INITIAL_MEMORY=33554432'
    ])

  @no_wasm_backend('cocos2d needs to be ported')
  @requires_graphics_hardware
  def test_cocos2d_hello(self):
    cocos2d_root = os.path.join(system_libs.Ports.get_build_dir(), 'cocos2d')
    preload_file = os.path.join(cocos2d_root, 'samples', 'HelloCpp', 'Resources') + '@'
    self.btest('cocos2d_hello.cpp', reference='cocos2d_hello.png', reference_slack=1,
               args=['-s', 'USE_COCOS2D=3', '-s', 'ERROR_ON_UNDEFINED_SYMBOLS=0',
                     '--preload-file', preload_file, '--use-preload-plugins',
                     '-Wno-inconsistent-missing-override'],
               message='You should see Cocos2d logo')

  def test_async(self):
    for opts in [0, 1, 2, 3]:
      print(opts)
      self.btest('browser/async.cpp', '1', args=['-O' + str(opts), '-g2', '-s', 'ASYNCIFY'])

  def test_asyncify_tricky_function_sig(self):
    self.btest('browser/test_asyncify_tricky_function_sig.cpp', '85', args=['-s', 'ASYNCIFY_ONLY=[foo(char.const*?.int#),foo2(),main,__original_main]', '-s', 'ASYNCIFY=1'])

  @requires_threads
  def test_async_in_pthread(self):
    self.btest('browser/async.cpp', '1', args=['-s', 'ASYNCIFY', '-s', 'USE_PTHREADS', '-s', 'PROXY_TO_PTHREAD', '-g'])

  def test_async_2(self):
    # Error.stackTraceLimit default to 10 in chrome but this test relies on more
    # than 40 stack frames being reported.
    create_file('pre.js', 'Error.stackTraceLimit = 80;\n')
    self.btest('browser/async_2.cpp', '40', args=['-O3', '--pre-js', 'pre.js', '-s', 'ASYNCIFY'])

  def test_async_virtual(self):
    for opts in [0, 3]:
      print(opts)
      self.btest('browser/async_virtual.cpp', '5', args=['-O' + str(opts), '-profiling', '-s', 'ASYNCIFY'])

  def test_async_virtual_2(self):
    for opts in [0, 3]:
      print(opts)
      self.btest('browser/async_virtual_2.cpp', '1', args=['-O' + str(opts), '-s', 'ASSERTIONS', '-s', 'SAFE_HEAP', '-profiling', '-s', 'ASYNCIFY'])

  # Test async sleeps in the presence of invoke_* calls, which can happen with
  # longjmp or exceptions.
  @parameterized({
    'O0': ([],), # noqa
    'O3': (['-O3'],), # noqa
  })
  def test_async_longjmp(self, args):
    self.btest('browser/async_longjmp.cpp', '2', args=args + ['-s', 'ASYNCIFY'])

  def test_async_mainloop(self):
    for opts in [0, 3]:
      print(opts)
      self.btest('browser/async_mainloop.cpp', '121', args=['-O' + str(opts), '-s', 'ASYNCIFY'])

  @requires_sound_hardware
  def test_sdl_audio_beep_sleep(self):
    self.btest('sdl_audio_beep_sleep.cpp', '1', args=['-Os', '-s', 'ASSERTIONS', '-s', 'DISABLE_EXCEPTION_CATCHING=0', '-profiling', '-s', 'SAFE_HEAP', '-lSDL', '-s', 'ASYNCIFY'], timeout=90)

  def test_mainloop_reschedule(self):
    self.btest('mainloop_reschedule.cpp', '1', args=['-Os', '-s', 'ASYNCIFY'])

  def test_mainloop_infloop(self):
    self.btest('mainloop_infloop.cpp', '1', args=['-s', 'ASYNCIFY'])

  def test_async_iostream(self):
    self.btest('browser/async_iostream.cpp', '1', args=['-s', 'ASYNCIFY'])

  # Test an async return value. The value goes through a custom JS library
  # method that uses asyncify, and therefore it needs to be declared in
  # ASYNCIFY_IMPORTS.
  # To make the test more precise we also use ASYNCIFY_IGNORE_INDIRECT here.
  @parameterized({
    'normal': (['-s', 'ASYNCIFY_IMPORTS=[sync_tunnel, sync_tunnel_bool]'],), # noqa
    'response': (['-s', 'ASYNCIFY_IMPORTS=@filey.txt'],), # noqa
    'nothing': (['-DBAD'],), # noqa
    'empty_list': (['-DBAD', '-s', 'ASYNCIFY_IMPORTS=[]'],), # noqa
    'em_js_bad': (['-DBAD', '-DUSE_EM_JS'],), # noqa
  })
  def test_async_returnvalue(self, args):
    if '@' in str(args):
      create_file('filey.txt', 'sync_tunnel\nsync_tunnel_bool\n')
    self.btest('browser/async_returnvalue.cpp', '0', args=['-s', 'ASYNCIFY', '-s', 'ASYNCIFY_IGNORE_INDIRECT', '--js-library', test_file('browser/async_returnvalue.js')] + args + ['-s', 'ASSERTIONS'])

  def test_async_stack_overflow(self):
    self.btest('browser/async_stack_overflow.cpp', 'abort:RuntimeError: unreachable', args=['-s', 'ASYNCIFY', '-s', 'ASYNCIFY_STACK_SIZE=4'])

  def test_async_bad_list(self):
    self.btest('browser/async_bad_list.cpp', '0', args=['-s', 'ASYNCIFY', '-s', 'ASYNCIFY_ONLY=[waka]', '--profiling'])

  # Tests that when building with -s MINIMAL_RUNTIME=1, the build can use -s MODULARIZE=1 as well.
  def test_minimal_runtime_modularize(self):
    self.compile_btest([test_file('browser_test_hello_world.c'), '-o', 'test.html', '-s', 'MODULARIZE', '-s', 'MINIMAL_RUNTIME'])
    self.run_browser('test.html', None, '/report_result?0')

  @requires_sync_compilation
  def test_modularize(self):
    for opts in [
      [],
      ['-O1'],
      ['-O2', '-profiling'],
      ['-O2'],
      ['-O2', '--closure=1']
    ]:
      for args, code in [
        # defaults
        ([], '''
          let promise = Module();
          if (!promise instanceof Promise) throw new Error('Return value should be a promise');
        '''),
        # use EXPORT_NAME
        (['-s', 'EXPORT_NAME="HelloWorld"'], '''
          if (typeof Module !== "undefined") throw "what?!"; // do not pollute the global scope, we are modularized!
          HelloWorld.noInitialRun = true; // errorneous module capture will load this and cause timeout
          let promise = HelloWorld();
          if (!promise instanceof Promise) throw new Error('Return value should be a promise');
        '''),
        # pass in a Module option (which prevents main(), which we then invoke ourselves)
        (['-s', 'EXPORT_NAME="HelloWorld"'], '''
          HelloWorld({ noInitialRun: true }).then(hello => {
            hello._main();
          });
        '''),
        # Even without a mem init file, everything is async
        (['-s', 'EXPORT_NAME="HelloWorld"', '--memory-init-file', '0'], '''
          HelloWorld({ noInitialRun: true }).then(hello => {
            hello._main();
          });
        '''),
      ]:
        print('test on', opts, args, code)
        # this test is synchronous, so avoid async startup due to wasm features
        self.compile_btest([test_file('browser_test_hello_world.c'), '-s', 'MODULARIZE', '-s', 'SINGLE_FILE'] + args + opts)
        create_file('a.html', '''
          <script src="a.out.js"></script>
          <script>
            %s
          </script>
        ''' % code)
        self.run_browser('a.html', '...', '/report_result?0')

  def test_modularize_network_error(self):
    test_c_path = test_file('browser_test_hello_world.c')
    browser_reporting_js_path = test_file('browser_reporting.js')
    self.compile_btest([test_c_path, '-s', 'MODULARIZE', '-s', 'EXPORT_NAME="createModule"', '--extern-pre-js', browser_reporting_js_path], reporting=Reporting.NONE)
    create_file('a.html', '''
      <script src="a.out.js"></script>
      <script>
        createModule()
          .then(() => {
            reportResultToServer("Module creation succeeded when it should have failed");
          })
          .catch(err => {
            reportResultToServer(err.message.slice(0, 54));
          });
      </script>
    ''')
    print('Deleting a.out.wasm to cause a download error')
    os.remove('a.out.wasm')
    self.run_browser('a.html', '...', '/report_result?abort(both async and sync fetching of the wasm failed)')

  def test_modularize_init_error(self):
    test_cpp_path = test_file('browser/test_modularize_init_error.cpp')
    browser_reporting_js_path = test_file('browser_reporting.js')
    self.compile_btest([test_cpp_path, '-s', 'MODULARIZE', '-s', 'EXPORT_NAME="createModule"', '--extern-pre-js', browser_reporting_js_path], reporting=Reporting.NONE)
    create_file('a.html', '''
      <script src="a.out.js"></script>
      <script>
        if (typeof window === 'object') {
          window.addEventListener('unhandledrejection', function(event) {
            reportResultToServer("Unhandled promise rejection: " + event.reason.message);
          });
        }
        createModule()
          .then(() => {
            reportResultToServer("Module creation succeeded when it should have failed");
          })
          .catch(err => {
            reportResultToServer(err);
          });
      </script>
    ''')
    self.run_browser('a.html', '...', '/report_result?intentional error to test rejection')

  # test illustrating the regression on the modularize feature since commit c5af8f6
  # when compiling with the --preload-file option
  def test_modularize_and_preload_files(self):
    # amount of memory different from the default one that will be allocated for the emscripten heap
    totalMemory = 33554432
    for opts in [[], ['-O1'], ['-O2', '-profiling'], ['-O2'], ['-O2', '--closure=1']]:
      # the main function simply checks that the amount of allocated heap memory is correct
      create_file('test.c', r'''
        #include <stdio.h>
        #include <emscripten.h>
        int main() {
          EM_ASM({
            // use eval here in order for the test with closure compiler enabled to succeed
            var totalMemory = Module['INITIAL_MEMORY'];
            assert(totalMemory === %d, 'bad memory size');
          });
          REPORT_RESULT(0);
          return 0;
        }
      ''' % totalMemory)
      # generate a dummy file
      create_file('dummy_file', 'dummy')
      # compile the code with the modularize feature and the preload-file option enabled
      # no wasm, since this tests customizing total memory at runtime
      self.compile_btest(['test.c', '-s', 'WASM=0', '-s', 'MODULARIZE', '-s', 'EXPORT_NAME="Foo"', '--preload-file', 'dummy_file'] + opts)
      create_file('a.html', '''
        <script src="a.out.js"></script>
        <script>
          // instantiate the Foo module with custom INITIAL_MEMORY value
          var foo = Foo({ INITIAL_MEMORY: %d });
        </script>
      ''' % totalMemory)
      self.run_browser('a.html', '...', '/report_result?0')

  def test_webidl(self):
    # see original in test_core.py
    self.run_process([WEBIDL_BINDER, test_file('webidl/test.idl'), 'glue'])
    self.assertExists('glue.cpp')
    self.assertExists('glue.js')
    for opts in [[], ['-O1'], ['-O2']]:
      print(opts)
      self.btest(Path('webidl/test.cpp'), '1', args=['--post-js', 'glue.js', '-I.', '-DBROWSER'] + opts)

  @requires_sync_compilation
  def test_dynamic_link(self):
    create_file('main.c', r'''
      #include <stdio.h>
      #include <stdlib.h>
      #include <string.h>
      #include <emscripten.h>
      char *side(const char *data);
      int main() {
        char *temp = side("hello through side\n");
        char *ret = (char*)malloc(strlen(temp)+1);
        strcpy(ret, temp);
        temp[1] = 'x';
        EM_ASM({
          Module.realPrint = out;
          out = function(x) {
            if (!Module.printed) Module.printed = x;
            Module.realPrint(x);
          };
        });
        puts(ret);
        EM_ASM({ assert(Module.printed === 'hello through side', ['expected', Module.printed]); });
        return 0;
      }
    ''')
    create_file('side.c', r'''
      #include <stdlib.h>
      #include <string.h>
      char *side(const char *data);
      char *side(const char *data) {
        char *ret = (char*)malloc(strlen(data)+1);
        strcpy(ret, data);
        return ret;
      }
    ''')
    self.run_process([EMCC, 'side.c', '-s', 'SIDE_MODULE', '-O2', '-o', 'side.wasm'])
    self.btest_exit(self.in_dir('main.c'), args=['-s', 'MAIN_MODULE=2', '-O2', 'side.wasm'])

    print('wasm in worker (we can read binary data synchronously there)')

    self.run_process([EMCC, 'side.c', '-s', 'SIDE_MODULE', '-O2', '-o', 'side.wasm'])
    self.btest_exit(self.in_dir('main.c'), args=['-s', 'MAIN_MODULE=2', '-O2', '--proxy-to-worker', 'side.wasm'])

    print('wasm (will auto-preload since no sync binary reading)')

    # same wasm side module works
    self.btest_exit(self.in_dir('main.c'), args=['-s', 'MAIN_MODULE=2', '-O2', '-s', 'EXPORT_ALL', 'side.wasm'])

  def test_dlopen_blocking(self):
    create_file('side.c', 'int foo = 42;\n')
    self.run_process([EMCC, 'side.c', '-o', 'libside.so', '-s', 'SIDE_MODULE', '-s', 'USE_PTHREADS', '-Wno-experimental'])
    # Attempt to use dlopen without preloading the side module should fail on the main thread
    # since the syncronous `readBinary` function does not exist.
    self.btest_exit(test_file('other/test_dlopen_blocking.c'), assert_returncode=1, args=['-s', 'MAIN_MODULE=2'])
    # But with PROXY_TO_PTHEAD it does work, since we can do blocking and sync XHR in a worker.
    self.btest_exit(test_file('other/test_dlopen_blocking.c'), args=['-s', 'MAIN_MODULE=2', '-s', 'PROXY_TO_PTHREAD', '-s', 'USE_PTHREADS', '-Wno-experimental'])

  # verify that dynamic linking works in all kinds of in-browser environments.
  # don't mix different kinds in a single test.
  @parameterized({
    '': ([0],),
    'inworker': ([1],),
  })
  def test_dylink_dso_needed(self, inworker):
    self.emcc_args += ['-O2']
    # --proxy-to-worker only on main
    if inworker:
      self.emcc_args += ['--proxy-to-worker']

    def do_run(src, expected_output, emcc_args=[]):
      # XXX there is no infrastructure (yet ?) to retrieve stdout from browser in tests.
      # -> do the assert about expected output inside browser.
      #
      # we have to put the hook into post.js because in main it is too late
      # (in main we won't be able to catch what static constructors inside
      # linked dynlibs printed), and in pre.js it is too early (out is not yet
      # setup by the shell).
      create_file('post.js', r'''
          Module.realPrint = out;
          out = function(x) {
            if (!Module.printed) Module.printed = "";
            Module.printed += x + '\n'; // out is passed str without last \n
            Module.realPrint(x);
          };
        ''')
      create_file('test_dylink_dso_needed.c', src + r'''
        #include <emscripten/em_asm.h>

        int main() {
          int rtn = test_main();
          EM_ASM({
            var expected = %r;
            assert(Module.printed === expected, ['stdout expected:', expected]);
          });
          return rtn;
        }
      ''' % expected_output)
      self.btest_exit(self.in_dir('test_dylink_dso_needed.c'), args=self.get_emcc_args() + ['--post-js', 'post.js'] + emcc_args)

    self._test_dylink_dso_needed(do_run)

  @requires_graphics_hardware
  @requires_sync_compilation
  def test_dynamic_link_glemu(self):
    create_file('main.c', r'''
      #include <stdio.h>
      #include <string.h>
      #include <assert.h>
      const char *side();
      int main() {
        const char *exts = side();
        puts(side());
        assert(strstr(exts, "GL_EXT_texture_env_combine"));
        REPORT_RESULT(1);
        return 0;
      }
    ''')
    create_file('side.c', r'''
      #include "SDL/SDL.h"
      #include "SDL/SDL_opengl.h"
      const char *side() {
        SDL_Init(SDL_INIT_VIDEO);
        SDL_SetVideoMode(600, 600, 16, SDL_OPENGL);
        return (const char *)glGetString(GL_EXTENSIONS);
      }
    ''')
    self.run_process([EMCC, 'side.c', '-s', 'SIDE_MODULE', '-O2', '-o', 'side.wasm', '-lSDL'])

    self.btest(self.in_dir('main.c'), '1', args=['-s', 'MAIN_MODULE=2', '-O2', '-s', 'LEGACY_GL_EMULATION', '-lSDL', '-lGL', 'side.wasm'])

  def test_dynamic_link_many(self):
    # test asynchronously loading two side modules during startup
    create_file('main.c', r'''
      #include <assert.h>
      int side1();
      int side2();
      int main() {
        assert(side1() == 1);
        assert(side2() == 2);
        return 0;
      }
    ''')
    create_file('side1.c', r'''
      int side1() { return 1; }
    ''')
    create_file('side2.c', r'''
      int side2() { return 2; }
    ''')
    self.run_process([EMCC, 'side1.c', '-s', 'SIDE_MODULE', '-o', 'side1.wasm'])
    self.run_process([EMCC, 'side2.c', '-s', 'SIDE_MODULE', '-o', 'side2.wasm'])
    self.btest_exit(self.in_dir('main.c'), args=['-s', 'MAIN_MODULE=2', 'side1.wasm', 'side2.wasm'])

  def test_dynamic_link_pthread_many(self):
    # Test asynchronously loading two side modules during startup
    # They should always load in the same order
    # Verify that function pointers in the browser's main thread
    # reffer to the same function as in a pthread worker.

    # The main thread function table is populated asynchronously
    # in the browser's main thread. However, it should still be
    # populated in the same order as in a pthread worker to
    # guarantee function pointer interop.
    create_file('main.cpp', r'''
      #include <thread>
      int side1();
      int side2();
      int main() {
        auto side1_ptr = &side1;
        auto side2_ptr = &side2;
        // Don't join the thread since this is running in the
        // browser's main thread.
        std::thread([=]{
          REPORT_RESULT(int(
            side1_ptr == &side1 &&
            side2_ptr == &side2
          ));
        }).detach();
        return 0;
      }
    ''')

    # The browser will try to load side1 first.
    # Use a big payload in side1 so that it takes longer to load than side2
    create_file('side1.cpp', r'''
      char const * payload1 = "''' + str(list(range(1, int(1e5)))) + r'''";
      int side1() { return 1; }
    ''')
    create_file('side2.cpp', r'''
      char const * payload2 = "0";
      int side2() { return 2; }
    ''')
    self.run_process([EMCC, 'side1.cpp', '-Wno-experimental', '-pthread', '-s', 'SIDE_MODULE', '-o', 'side1.wasm'])
    self.run_process([EMCC, 'side2.cpp', '-Wno-experimental', '-pthread', '-s', 'SIDE_MODULE', '-o', 'side2.wasm'])
    self.btest(self.in_dir('main.cpp'), '1',
               args=['-Wno-experimental', '-pthread', '-s', 'MAIN_MODULE=2', 'side1.wasm', 'side2.wasm'])

  def test_memory_growth_during_startup(self):
    create_file('data.dat', 'X' * (30 * 1024 * 1024))
    self.btest('browser_test_hello_world.c', '0', args=['-s', 'ASSERTIONS', '-s', 'ALLOW_MEMORY_GROWTH', '-s', 'INITIAL_MEMORY=16MB', '-s', 'TOTAL_STACK=16384', '--preload-file', 'data.dat'])

  # pthreads tests

  def prep_no_SAB(self):
    create_file('html.html', read_file(path_from_root('src', 'shell_minimal.html')).replace('''<body>''', '''<body>
      <script>
        SharedArrayBuffer = undefined;
        Atomics = undefined;
      </script>
    '''))

  @requires_threads
  def test_pthread_c11_threads(self):
    self.btest(test_file('pthread/test_pthread_c11_threads.c'),
               expected='0',
               args=['-gsource-map', '-std=gnu11', '-xc', '-s', 'USE_PTHREADS', '-s', 'PROXY_TO_PTHREAD', '-s', 'TOTAL_MEMORY=64mb'])

  @requires_threads
  def test_pthread_pool_size_strict(self):
    # Check that it doesn't fail with sufficient number of threads in the pool.
    self.btest(test_file('pthread/test_pthread_c11_threads.c'),
               expected='0',
               args=['-g2', '-xc', '-std=gnu11', '-pthread', '-s', 'PTHREAD_POOL_SIZE=4', '-s', 'PTHREAD_POOL_SIZE_STRICT=2', '-s', 'TOTAL_MEMORY=64mb'])
    # Check that it fails instead of deadlocking on insufficient number of threads in the pool.
    self.btest(test_file('pthread/test_pthread_c11_threads.c'),
               expected='abort:Assertion failed: thrd_create(&t4, thread_main, NULL) == thrd_success',
               args=['-g2', '-xc', '-std=gnu11', '-pthread', '-s', 'PTHREAD_POOL_SIZE=3', '-s', 'PTHREAD_POOL_SIZE_STRICT=2', '-s', 'TOTAL_MEMORY=64mb'])

  @requires_threads
  def test_pthread_in_pthread_pool_size_strict(self):
    # Check that it fails when there's a pthread creating another pthread.
    self.btest(test_file('pthread/test_pthread_create_pthread.cpp'), expected='1', args=['-g2', '-pthread', '-s', 'PTHREAD_POOL_SIZE=2', '-s', 'PTHREAD_POOL_SIZE_STRICT=2'])
    # Check that it fails when there's a pthread creating another pthread.
    self.btest(test_file('pthread/test_pthread_create_pthread.cpp'), expected='-200', args=['-g2', '-pthread', '-s', 'PTHREAD_POOL_SIZE=1', '-s', 'PTHREAD_POOL_SIZE_STRICT=2'])

  # Test that the emscripten_ atomics api functions work.
  @parameterized({
    'normal': ([],),
    'closure': (['--closure=1'],),
  })
  @requires_threads
  def test_pthread_atomics(self, args=[]):
    self.btest(test_file('pthread/test_pthread_atomics.cpp'), expected='0', args=['-s', 'INITIAL_MEMORY=64MB', '-O3', '-s', 'USE_PTHREADS', '-s', 'PTHREAD_POOL_SIZE=8', '-g1'] + args)

  # Test 64-bit atomics.
  @requires_threads
  def test_pthread_64bit_atomics(self):
    self.btest(test_file('pthread/test_pthread_64bit_atomics.cpp'), expected='0', args=['-s', 'INITIAL_MEMORY=64MB', '-O3', '-s', 'USE_PTHREADS', '-s', 'PTHREAD_POOL_SIZE=8'])

  # Test 64-bit C++11 atomics.
  @requires_threads
  def test_pthread_64bit_cxx11_atomics(self):
    for opt in [['-O0'], ['-O3']]:
      for pthreads in [[], ['-s', 'USE_PTHREADS']]:
        self.btest(test_file('pthread/test_pthread_64bit_cxx11_atomics.cpp'), expected='0', args=opt + pthreads)

  # Test c++ std::thread::hardware_concurrency()
  @requires_threads
  def test_pthread_hardware_concurrency(self):
    self.btest(test_file('pthread/test_pthread_hardware_concurrency.cpp'), expected='0', args=['-O2', '-s', 'USE_PTHREADS', '-s', 'PTHREAD_POOL_SIZE="navigator.hardwareConcurrency"'])

  @parameterized({
    'join': ('join',),
    'wait': ('wait',),
  })
  @requires_threads
  def test_pthread_main_thread_blocking(self, name):
    print('Test that we error if not ALLOW_BLOCKING_ON_MAIN_THREAD')
    self.btest(test_file('pthread/main_thread_%s.cpp' % name), expected='abort:Blocking on the main thread is not allowed by default.', args=['-O3', '-s', 'USE_PTHREADS', '-s', 'PTHREAD_POOL_SIZE', '-s', 'ALLOW_BLOCKING_ON_MAIN_THREAD=0'])
    if name == 'join':
      print('Test that by default we just warn about blocking on the main thread.')
      self.btest(test_file('pthread/main_thread_%s.cpp' % name), expected='1', args=['-O3', '-s', 'USE_PTHREADS', '-s', 'PTHREAD_POOL_SIZE'])
      print('Test that tryjoin is fine, even if not ALLOW_BLOCKING_ON_MAIN_THREAD')
      self.btest(test_file('pthread/main_thread_join.cpp'), expected='2', args=['-O3', '-s', 'USE_PTHREADS', '-s', 'PTHREAD_POOL_SIZE', '-g', '-DTRY_JOIN', '-s', 'ALLOW_BLOCKING_ON_MAIN_THREAD=0'])
      print('Test that tryjoin is fine, even if not ALLOW_BLOCKING_ON_MAIN_THREAD, and even without a pool')
      self.btest(test_file('pthread/main_thread_join.cpp'), expected='2', args=['-O3', '-s', 'USE_PTHREADS', '-g', '-DTRY_JOIN', '-s', 'ALLOW_BLOCKING_ON_MAIN_THREAD=0'])
      print('Test that everything works ok when we are on a pthread.')
      self.btest(test_file('pthread/main_thread_%s.cpp' % name), expected='1', args=['-O3', '-s', 'USE_PTHREADS', '-s', 'PTHREAD_POOL_SIZE', '-s', 'PROXY_TO_PTHREAD', '-s', 'ALLOW_BLOCKING_ON_MAIN_THREAD=0'])

  # Test the old GCC atomic __sync_fetch_and_op builtin operations.
  @no_firefox('https://bugzilla.mozilla.org/show_bug.cgi?id=1666568')
  @requires_threads
  def test_pthread_gcc_atomic_fetch_and_op(self):
    for opt in [[], ['-O1'], ['-O2'], ['-O3'], ['-Os']]:
      for debug in [[], ['-g']]:
        args = opt + debug
        print(args)
        self.btest(test_file('pthread/test_pthread_gcc_atomic_fetch_and_op.cpp'), expected='0', args=args + ['-s', 'INITIAL_MEMORY=64MB', '-s', 'USE_PTHREADS', '-s', 'PTHREAD_POOL_SIZE=8'])

  # 64 bit version of the above test.
  @no_firefox('https://bugzilla.mozilla.org/show_bug.cgi?id=1666568')
  @requires_threads
  def test_pthread_gcc_64bit_atomic_fetch_and_op(self):
    self.btest(test_file('pthread/test_pthread_gcc_64bit_atomic_fetch_and_op.cpp'), expected='0', args=['-s', 'INITIAL_MEMORY=64MB', '-O3', '-s', 'USE_PTHREADS', '-s', 'PTHREAD_POOL_SIZE=8'], also_asmjs=True)

  # Test the old GCC atomic __sync_op_and_fetch builtin operations.
  @no_firefox('https://bugzilla.mozilla.org/show_bug.cgi?id=1666568')
  @requires_threads
  def test_pthread_gcc_atomic_op_and_fetch(self):
    self.btest(test_file('pthread/test_pthread_gcc_atomic_op_and_fetch.cpp'), expected='0', args=['-s', 'INITIAL_MEMORY=64MB', '-O3', '-s', 'USE_PTHREADS', '-s', 'PTHREAD_POOL_SIZE=8'], also_asmjs=True)

  # 64 bit version of the above test.
  @no_firefox('https://bugzilla.mozilla.org/show_bug.cgi?id=1666568')
  @requires_threads
  def test_pthread_gcc_64bit_atomic_op_and_fetch(self):
    self.btest(test_file('pthread/test_pthread_gcc_64bit_atomic_op_and_fetch.cpp'), expected='0', args=['-s', 'INITIAL_MEMORY=64MB', '-O3', '-s', 'USE_PTHREADS', '-s', 'PTHREAD_POOL_SIZE=8'], also_asmjs=True)

  # Tests the rest of the remaining GCC atomics after the two above tests.
  @no_firefox('https://bugzilla.mozilla.org/show_bug.cgi?id=1666568')
  @requires_threads
  def test_pthread_gcc_atomics(self):
    self.btest(test_file('pthread/test_pthread_gcc_atomics.cpp'), expected='0', args=['-s', 'INITIAL_MEMORY=64MB', '-O3', '-s', 'USE_PTHREADS', '-s', 'PTHREAD_POOL_SIZE=8'])

  # Test the __sync_lock_test_and_set and __sync_lock_release primitives.
  @requires_threads
  def test_pthread_gcc_spinlock(self):
    for arg in [[], ['-DUSE_EMSCRIPTEN_INTRINSICS']]:
      self.btest(test_file('pthread/test_pthread_gcc_spinlock.cpp'), expected='800', args=['-s', 'INITIAL_MEMORY=64MB', '-O3', '-s', 'USE_PTHREADS', '-s', 'PTHREAD_POOL_SIZE=8'] + arg, also_asmjs=True)

  # Test that basic thread creation works.
  @no_firefox('https://bugzilla.mozilla.org/show_bug.cgi?id=1666568')
  @requires_threads
  def test_pthread_create(self):
    def test(args):
      print(args)
      self.btest(test_file('pthread/test_pthread_create.cpp'),
                 expected='0',
                 args=['-s', 'INITIAL_MEMORY=64MB', '-s', 'USE_PTHREADS', '-s', 'PTHREAD_POOL_SIZE=8'] + args,
                 extra_tries=0) # this should be 100% deterministic
    print() # new line
    test([])
    test(['-O3'])
    # TODO: re-enable minimal runtime once the flakiness is figure out,
    # https://github.com/emscripten-core/emscripten/issues/12368
    # test(['-s', 'MINIMAL_RUNTIME'])

  # Test that preallocating worker threads work.
  @requires_threads
  def test_pthread_preallocates_workers(self):
    self.btest(test_file('pthread/test_pthread_preallocates_workers.cpp'), expected='0', args=['-O3', '-s', '-s', 'USE_PTHREADS', '-s', 'PTHREAD_POOL_SIZE=4', '-s', 'PTHREAD_POOL_DELAY_LOAD'])

  # Test that allocating a lot of threads doesn't regress. This needs to be checked manually!
  @requires_threads
  def test_pthread_large_pthread_allocation(self):
    self.btest(test_file('pthread/test_large_pthread_allocation.cpp'), expected='0', args=['-s', 'INITIAL_MEMORY=128MB', '-O3', '-s', '-s', 'USE_PTHREADS', '-s', 'PTHREAD_POOL_SIZE=50'], message='Check output from test to ensure that a regression in time it takes to allocate the threads has not occurred.')

  # Tests the -s PROXY_TO_PTHREAD=1 option.
  @requires_threads
  def test_pthread_proxy_to_pthread(self):
    self.btest(test_file('pthread/test_pthread_proxy_to_pthread.c'), expected='1', args=['-O3', '-s', 'USE_PTHREADS', '-s', 'PROXY_TO_PTHREAD'])

  # Test that a pthread can spawn another pthread of its own.
  @requires_threads
  def test_pthread_create_pthread(self):
    for modularize in [[], ['-s', 'MODULARIZE', '-s', 'EXPORT_NAME=MyModule', '--shell-file', test_file('shell_that_launches_modularize.html')]]:
      self.btest(test_file('pthread/test_pthread_create_pthread.cpp'), expected='1', args=['-O3', '-s', 'USE_PTHREADS', '-s', 'PTHREAD_POOL_SIZE=2'] + modularize)

  # Test another case of pthreads spawning pthreads, but this time the callers immediately join on the threads they created.
  @requires_threads
  def test_pthread_nested_spawns(self):
    self.btest(test_file('pthread/test_pthread_nested_spawns.cpp'), expected='1', args=['-O3', '-s', 'USE_PTHREADS', '-s', 'PTHREAD_POOL_SIZE=2'])

  # Test that main thread can wait for a pthread to finish via pthread_join().
  @requires_threads
  def test_pthread_join(self):
    self.btest(test_file('pthread/test_pthread_join.cpp'), expected='6765', args=['-O3', '-s', 'USE_PTHREADS', '-s', 'PTHREAD_POOL_SIZE=8'])

  # Test that threads can rejoin the pool once detached and finished
  @requires_threads
  def test_std_thread_detach(self):
    self.btest(test_file('pthread/test_std_thread_detach.cpp'), expected='0', args=['-s', 'USE_PTHREADS'])

  # Test pthread_cancel() operation
  @requires_threads
  def test_pthread_cancel(self):
    self.btest(test_file('pthread/test_pthread_cancel.cpp'), expected='1', args=['-O3', '-s', 'USE_PTHREADS', '-s', 'PTHREAD_POOL_SIZE=8'])

  # Test that pthread_cancel() cancels pthread_cond_wait() operation
  @requires_threads
  def test_pthread_cancel_cond_wait(self):
    self.btest_exit(test_file('pthread/test_pthread_cancel_cond_wait.cpp'), assert_returncode=1, args=['-O3', '-s', 'USE_PTHREADS=1', '-s', 'PTHREAD_POOL_SIZE=8'])

  # Test pthread_kill() operation
  @no_chrome('pthread_kill hangs chrome renderer, and keep subsequent tests from passing')
  @requires_threads
  def test_pthread_kill(self):
    self.btest(test_file('pthread/test_pthread_kill.cpp'), expected='0', args=['-O3', '-s', 'USE_PTHREADS', '-s', 'PTHREAD_POOL_SIZE=8'])

  # Test that pthread cleanup stack (pthread_cleanup_push/_pop) works.
  @requires_threads
  def test_pthread_cleanup(self):
    self.btest_exit(test_file('pthread/test_pthread_cleanup.cpp'), args=['-O3', '-s', 'USE_PTHREADS', '-s', 'PTHREAD_POOL_SIZE=8'])

  # Tests the pthread mutex api.
  @requires_threads
  def test_pthread_mutex(self):
    for arg in [[], ['-DSPINLOCK_TEST']]:
      self.btest(test_file('pthread/test_pthread_mutex.cpp'), expected='50', args=['-s', 'INITIAL_MEMORY=64MB', '-O3', '-s', 'USE_PTHREADS', '-s', 'PTHREAD_POOL_SIZE=8'] + arg)

  @requires_threads
  def test_pthread_attr_getstack(self):
    self.btest(test_file('pthread/test_pthread_attr_getstack.cpp'), expected='0', args=['-s', 'USE_PTHREADS', '-s', 'PTHREAD_POOL_SIZE=2'])

  # Test that memory allocation is thread-safe.
  @requires_threads
  def test_pthread_malloc(self):
    self.btest(test_file('pthread/test_pthread_malloc.cpp'), expected='0', args=['-s', 'INITIAL_MEMORY=64MB', '-O3', '-s', 'USE_PTHREADS', '-s', 'PTHREAD_POOL_SIZE=8'])

  # Stress test pthreads allocating memory that will call to sbrk(), and main thread has to free up the data.
  @requires_threads
  def test_pthread_malloc_free(self):
    self.btest(test_file('pthread/test_pthread_malloc_free.cpp'), expected='0', args=['-s', 'INITIAL_MEMORY=64MB', '-O3', '-s', 'USE_PTHREADS', '-s', 'PTHREAD_POOL_SIZE=8', '-s', 'INITIAL_MEMORY=256MB'])

  # Test that the pthread_barrier API works ok.
  @requires_threads
  def test_pthread_barrier(self):
    self.btest(test_file('pthread/test_pthread_barrier.cpp'), expected='0', args=['-s', 'INITIAL_MEMORY=64MB', '-O3', '-s', 'USE_PTHREADS', '-s', 'PTHREAD_POOL_SIZE=8'])

  # Test the pthread_once() function.
  @requires_threads
  def test_pthread_once(self):
    self.btest(test_file('pthread/test_pthread_once.cpp'), expected='0', args=['-s', 'INITIAL_MEMORY=64MB', '-O3', '-s', 'USE_PTHREADS', '-s', 'PTHREAD_POOL_SIZE=8'])

  # Test against a certain thread exit time handling bug by spawning tons of threads.
  @no_firefox('https://bugzilla.mozilla.org/show_bug.cgi?id=1666568')
  @requires_threads
  def test_pthread_spawns(self):
    self.btest(test_file('pthread/test_pthread_spawns.cpp'), expected='0', args=['-s', 'INITIAL_MEMORY=64MB', '-O3', '-s', 'USE_PTHREADS', '-s', 'PTHREAD_POOL_SIZE=8', '--closure=1', '-s', 'ENVIRONMENT=web,worker'])

  # It is common for code to flip volatile global vars for thread control. This is a bit lax, but nevertheless, test whether that
  # kind of scheme will work with Emscripten as well.
  @requires_threads
  def test_pthread_volatile(self):
    for arg in [[], ['-DUSE_C_VOLATILE']]:
      self.btest(test_file('pthread/test_pthread_volatile.cpp'), expected='1', args=['-s', 'INITIAL_MEMORY=64MB', '-O3', '-s', 'USE_PTHREADS', '-s', 'PTHREAD_POOL_SIZE=8'] + arg)

  # Test thread-specific data (TLS).
  @requires_threads
  def test_pthread_thread_local_storage(self):
    self.btest(test_file('pthread/test_pthread_thread_local_storage.cpp'), expected='0', args=['-s', 'INITIAL_MEMORY=64MB', '-O3', '-s', 'USE_PTHREADS', '-s', 'PTHREAD_POOL_SIZE=8', '-s', 'ASSERTIONS'])

  # Test the pthread condition variable creation and waiting.
  @requires_threads
  def test_pthread_condition_variable(self):
    self.btest(test_file('pthread/test_pthread_condition_variable.cpp'), expected='0', args=['-s', 'INITIAL_MEMORY=64MB', '-O3', '-s', 'USE_PTHREADS', '-s', 'PTHREAD_POOL_SIZE=8'])

  # Test that pthreads are able to do printf.
  @requires_threads
  def test_pthread_printf(self):
    def run(debug):
       self.btest(test_file('pthread/test_pthread_printf.cpp'), expected='0', args=['-s', 'INITIAL_MEMORY=64MB', '-O3', '-s', 'USE_PTHREADS', '-s', 'PTHREAD_POOL_SIZE', '-s', 'LIBRARY_DEBUG=%d' % debug])

    run(debug=True)
    run(debug=False)

  # Test that pthreads are able to do cout. Failed due to https://bugzilla.mozilla.org/show_bug.cgi?id=1154858.
  @requires_threads
  def test_pthread_iostream(self):
    self.btest(test_file('pthread/test_pthread_iostream.cpp'), expected='0', args=['-s', 'INITIAL_MEMORY=64MB', '-O3', '-s', 'USE_PTHREADS', '-s', 'PTHREAD_POOL_SIZE'])

  @requires_threads
  def test_pthread_unistd_io_bigint(self):
    self.btest_exit(test_file('unistd/io.c'), args=['-s', 'USE_PTHREADS', '-s', 'PROXY_TO_PTHREAD', '-s', 'WASM_BIGINT'])

  # Test that the main thread is able to use pthread_set/getspecific.
  @requires_threads
  def test_pthread_setspecific_mainthread(self):
    self.btest_exit(test_file('pthread/test_pthread_setspecific_mainthread.c'), args=['-s', 'INITIAL_MEMORY=64MB', '-O3', '-s', 'USE_PTHREADS'], also_asmjs=True)

  # Test that pthreads have access to filesystem.
  @requires_threads
  def test_pthread_file_io(self):
    self.btest(test_file('pthread/test_pthread_file_io.cpp'), expected='0', args=['-O3', '-s', 'USE_PTHREADS', '-s', 'PTHREAD_POOL_SIZE'])

  # Test that the pthread_create() function operates benignly in the case that threading is not supported.
  @requires_threads
  def test_pthread_supported(self):
    for args in [[], ['-s', 'USE_PTHREADS', '-s', 'PTHREAD_POOL_SIZE=8']]:
      self.btest(test_file('pthread/test_pthread_supported.cpp'), expected='0', args=['-O3'] + args)

  @requires_threads
  def test_pthread_dispatch_after_exit(self):
    self.btest_exit(test_file('pthread/test_pthread_dispatch_after_exit.c'), args=['-s', 'USE_PTHREADS'])

  # Test the operation of Module.pthreadMainPrefixURL variable
  @no_wasm_backend('uses js')
  @requires_threads
  def test_pthread_custom_pthread_main_url(self):
    ensure_dir('cdn')
    create_file('main.cpp', r'''
      #include <stdio.h>
      #include <string.h>
      #include <emscripten/emscripten.h>
      #include <emscripten/threading.h>
      #include <pthread.h>
      int result = 0;
      void *thread_main(void *arg) {
        emscripten_atomic_store_u32(&result, 1);
        pthread_exit(0);
      }

      int main() {
        pthread_t t;
        if (emscripten_has_threading_support()) {
          pthread_create(&t, 0, thread_main, 0);
          pthread_join(t, 0);
        } else {
          result = 1;
        }
        REPORT_RESULT(result);
      }
    ''')

    # Test that it is possible to define "Module.locateFile" string to locate where worker.js will be loaded from.
    create_file('shell.html', read_file(path_from_root('src', 'shell.html')).replace('var Module = {', 'var Module = { locateFile: function (path, prefix) {if (path.endsWith(".wasm")) {return prefix + path;} else {return "cdn/" + path;}}, '))
    self.compile_btest(['main.cpp', '--shell-file', 'shell.html', '-s', 'WASM=0', '-s', 'IN_TEST_HARNESS', '-s', 'USE_PTHREADS', '-s', 'PTHREAD_POOL_SIZE', '-o', 'test.html'])
    shutil.move('test.worker.js', Path('cdn/test.worker.js'))
    shutil.copyfile('test.html.mem', Path('cdn/test.html.mem'))
    self.run_browser('test.html', '', '/report_result?1')

    # Test that it is possible to define "Module.locateFile(foo)" function to locate where worker.js will be loaded from.
    create_file('shell2.html', read_file(path_from_root('src', 'shell.html')).replace('var Module = {', 'var Module = { locateFile: function(filename) { if (filename == "test.worker.js") return "cdn/test.worker.js"; else return filename; }, '))
    self.compile_btest(['main.cpp', '--shell-file', 'shell2.html', '-s', 'WASM=0', '-s', 'IN_TEST_HARNESS', '-s', 'USE_PTHREADS', '-s', 'PTHREAD_POOL_SIZE', '-o', 'test2.html'])
    try_delete('test.worker.js')
    self.run_browser('test2.html', '', '/report_result?1')

  # Test that if the main thread is performing a futex wait while a pthread needs it to do a proxied operation (before that pthread would wake up the main thread), that it's not a deadlock.
  @requires_threads
  def test_pthread_proxying_in_futex_wait(self):
    self.btest(test_file('pthread/test_pthread_proxying_in_futex_wait.cpp'), expected='0', args=['-O3', '-s', 'USE_PTHREADS', '-s', 'PTHREAD_POOL_SIZE'])

  # Test that sbrk() operates properly in multithreaded conditions
  @requires_threads
  def test_pthread_sbrk(self):
    for aborting_malloc in [0, 1]:
      print('aborting malloc=' + str(aborting_malloc))
      # With aborting malloc = 1, test allocating memory in threads
      # With aborting malloc = 0, allocate so much memory in threads that some of the allocations fail.
      self.btest(test_file('pthread/test_pthread_sbrk.cpp'), expected='0', args=['-O3', '-s', 'USE_PTHREADS', '-s', 'PTHREAD_POOL_SIZE=8', '-s', 'ABORTING_MALLOC=' + str(aborting_malloc), '-DABORTING_MALLOC=' + str(aborting_malloc), '-s', 'INITIAL_MEMORY=128MB'])

  # Test that -s ABORTING_MALLOC=0 works in both pthreads and non-pthreads builds. (sbrk fails gracefully)
  @requires_threads
  def test_pthread_gauge_available_memory(self):
    for opts in [[], ['-O2']]:
      for args in [[], ['-s', 'USE_PTHREADS']]:
        self.btest(test_file('gauge_available_memory.cpp'), expected='1', args=['-s', 'ABORTING_MALLOC=0'] + args + opts)

  # Test that the proxying operations of user code from pthreads to main thread work
  @requires_threads
  def test_pthread_run_on_main_thread(self):
    self.btest(test_file('pthread/test_pthread_run_on_main_thread.cpp'), expected='0', args=['-O3', '-s', 'USE_PTHREADS', '-s', 'PTHREAD_POOL_SIZE'])

  # Test how a lot of back-to-back called proxying operations behave.
  @requires_threads
  def test_pthread_run_on_main_thread_flood(self):
    self.btest(test_file('pthread/test_pthread_run_on_main_thread_flood.cpp'), expected='0', args=['-O3', '-s', 'USE_PTHREADS', '-s', 'PTHREAD_POOL_SIZE'])

  # Test that it is possible to asynchronously call a JavaScript function on the main thread.
  @requires_threads
  def test_pthread_call_async(self):
    self.btest(test_file('pthread/call_async.c'), expected='1', args=['-s', 'USE_PTHREADS'])

  # Test that it is possible to synchronously call a JavaScript function on the main thread and get a return value back.
  @requires_threads
  def test_pthread_call_sync_on_main_thread(self):
    self.btest(test_file('pthread/call_sync_on_main_thread.c'), expected='1', args=['-O3', '-s', 'USE_PTHREADS', '-s', 'PROXY_TO_PTHREAD', '-DPROXY_TO_PTHREAD=1', '--js-library', test_file('pthread/call_sync_on_main_thread.js')])
    self.btest(test_file('pthread/call_sync_on_main_thread.c'), expected='1', args=['-O3', '-s', 'USE_PTHREADS', '-DPROXY_TO_PTHREAD=0', '--js-library', test_file('pthread/call_sync_on_main_thread.js')])
    self.btest(test_file('pthread/call_sync_on_main_thread.c'), expected='1', args=['-Oz', '-DPROXY_TO_PTHREAD=0', '--js-library', test_file('pthread/call_sync_on_main_thread.js'), '-s', 'EXPORTED_FUNCTIONS=_main,_malloc'])

  # Test that it is possible to asynchronously call a JavaScript function on the main thread.
  @requires_threads
  def test_pthread_call_async_on_main_thread(self):
    self.btest(test_file('pthread/call_async_on_main_thread.c'), expected='7', args=['-O3', '-s', 'USE_PTHREADS', '-s', 'PROXY_TO_PTHREAD', '-DPROXY_TO_PTHREAD=1', '--js-library', test_file('pthread/call_async_on_main_thread.js')])
    self.btest(test_file('pthread/call_async_on_main_thread.c'), expected='7', args=['-O3', '-s', 'USE_PTHREADS', '-DPROXY_TO_PTHREAD=0', '--js-library', test_file('pthread/call_async_on_main_thread.js')])
    self.btest(test_file('pthread/call_async_on_main_thread.c'), expected='7', args=['-Oz', '-DPROXY_TO_PTHREAD=0', '--js-library', test_file('pthread/call_async_on_main_thread.js')])

  # Tests that spawning a new thread does not cause a reinitialization of the global data section of the application memory area.
  @requires_threads
  def test_pthread_global_data_initialization(self):
    mem_init_modes = [[], ['--memory-init-file', '0'], ['--memory-init-file', '1']]
    for mem_init_mode in mem_init_modes:
      for args in [['-s', 'MODULARIZE', '-s', 'EXPORT_NAME=MyModule', '--shell-file', test_file('shell_that_launches_modularize.html')], ['-O3']]:
        self.btest(test_file('pthread/test_pthread_global_data_initialization.c'), expected='20', args=args + mem_init_mode + ['-s', 'USE_PTHREADS', '-s', 'PROXY_TO_PTHREAD', '-s', 'PTHREAD_POOL_SIZE'])

  @requires_threads
  @requires_sync_compilation
  def test_pthread_global_data_initialization_in_sync_compilation_mode(self):
    mem_init_modes = [[], ['--memory-init-file', '0'], ['--memory-init-file', '1']]
    for mem_init_mode in mem_init_modes:
      args = ['-s', 'WASM_ASYNC_COMPILATION=0']
      self.btest(test_file('pthread/test_pthread_global_data_initialization.c'), expected='20', args=args + mem_init_mode + ['-s', 'USE_PTHREADS', '-s', 'PROXY_TO_PTHREAD', '-s', 'PTHREAD_POOL_SIZE'])

  # Test that emscripten_get_now() reports coherent wallclock times across all pthreads, instead of each pthread independently reporting wallclock times since the launch of that pthread.
  @requires_threads
  def test_pthread_clock_drift(self):
    self.btest(test_file('pthread/test_pthread_clock_drift.cpp'), expected='1', args=['-O3', '-s', 'USE_PTHREADS', '-s', 'PROXY_TO_PTHREAD'])

  @requires_threads
  def test_pthread_utf8_funcs(self):
    self.btest(test_file('pthread/test_pthread_utf8_funcs.cpp'), expected='0', args=['-s', 'USE_PTHREADS', '-s', 'PTHREAD_POOL_SIZE'])

  # Test the emscripten_futex_wake(addr, INT_MAX); functionality to wake all waiters
  @requires_threads
  def test_pthread_wake_all(self):
    self.btest(test_file('pthread/test_futex_wake_all.cpp'), expected='0', args=['-O3', '-s', 'USE_PTHREADS', '-s', 'INITIAL_MEMORY=64MB', '-s', 'NO_EXIT_RUNTIME'], also_asmjs=True)

  # Test that stack base and max correctly bound the stack on pthreads.
  @requires_threads
  def test_pthread_stack_bounds(self):
    self.btest(test_file('pthread/test_pthread_stack_bounds.cpp'), expected='1', args=['-s', 'USE_PTHREADS'])

  # Test that real `thread_local` works.
  @requires_threads
  def test_pthread_tls(self):
    self.btest(test_file('pthread/test_pthread_tls.cpp'), expected='1337', args=['-s', 'PROXY_TO_PTHREAD', '-s', 'USE_PTHREADS'])

  # Test that real `thread_local` works in main thread without PROXY_TO_PTHREAD.
  @requires_threads
  def test_pthread_tls_main(self):
    self.btest(test_file('pthread/test_pthread_tls_main.cpp'), expected='1337', args=['-s', 'USE_PTHREADS'])

  @requires_threads
  def test_pthread_safe_stack(self):
    # Note that as the test runs with PROXY_TO_PTHREAD, we set TOTAL_STACK,
    # and not DEFAULT_PTHREAD_STACK_SIZE, as the pthread for main() gets the
    # same stack size as the main thread normally would.
    self.btest(test_file('core/test_safe_stack.c'), expected='abort:stack overflow', args=['-s', 'USE_PTHREADS', '-s', 'PROXY_TO_PTHREAD', '-s', 'STACK_OVERFLOW_CHECK=2', '-s', 'TOTAL_STACK=64KB'])

  @parameterized({
    'leak': ['test_pthread_lsan_leak', ['-gsource-map']],
    'no_leak': ['test_pthread_lsan_no_leak'],
  })
  @requires_threads
  def test_pthread_lsan(self, name, args=[]):
    self.btest(test_file('pthread', name + '.cpp'), expected='1', args=['-fsanitize=leak', '-s', 'INITIAL_MEMORY=256MB', '-s', 'USE_PTHREADS', '-s', 'PROXY_TO_PTHREAD', '--pre-js', test_file('pthread', name + '.js')] + args)

  @parameterized({
    # Reusing the LSan test files for ASan.
    'leak': ['test_pthread_lsan_leak', ['-gsource-map']],
    'no_leak': ['test_pthread_lsan_no_leak'],
  })
  @requires_threads
  def test_pthread_asan(self, name, args=[]):
    self.btest(test_file('pthread', name + '.cpp'), expected='1', args=['-fsanitize=address', '-s', 'INITIAL_MEMORY=256MB', '-s', 'USE_PTHREADS', '-s', 'PROXY_TO_PTHREAD', '--pre-js', test_file('pthread', name + '.js')] + args)

  @requires_threads
  def test_pthread_asan_use_after_free(self):
    self.btest(test_file('pthread/test_pthread_asan_use_after_free.cpp'), expected='1', args=['-fsanitize=address', '-s', 'INITIAL_MEMORY=256MB', '-s', 'USE_PTHREADS', '-s', 'PROXY_TO_PTHREAD', '--pre-js', test_file('pthread/test_pthread_asan_use_after_free.js')])

  @requires_threads
  def test_pthread_exit_process(self):
    args = ['-s', 'USE_PTHREADS',
            '-s', 'PROXY_TO_PTHREAD',
            '-s', 'PTHREAD_POOL_SIZE=2',
            '-s', 'EXIT_RUNTIME',
            '-DEXIT_RUNTIME',
            '-O0']
    args += ['--pre-js', test_file('core/pthread/test_pthread_exit_runtime.pre.js')]
    self.btest(test_file('core/pthread/test_pthread_exit_runtime.c'), expected='onExit status: 42', args=args)

  @requires_threads
  def test_pthread_no_exit_process(self):
    # Same as above but without EXIT_RUNTIME.  In this case we don't expect onExit to
    # ever be called.
    args = ['-s', 'USE_PTHREADS',
            '-s', 'PROXY_TO_PTHREAD',
            '-s', 'PTHREAD_POOL_SIZE=2',
            '-O0']
    args += ['--pre-js', test_file('core/pthread/test_pthread_exit_runtime.pre.js')]
    self.btest(test_file('core/pthread/test_pthread_exit_runtime.c'), expected='43', args=args)

  # Tests MAIN_THREAD_EM_ASM_INT() function call signatures.
  def test_main_thread_em_asm_signatures(self):
    self.btest_exit(test_file('core/test_em_asm_signatures.cpp'), assert_returncode=121, args=[])

  @requires_threads
  def test_main_thread_em_asm_signatures_pthreads(self):
    self.btest_exit(test_file('core/test_em_asm_signatures.cpp'), assert_returncode=121, args=['-O3', '-s', 'USE_PTHREADS', '-s', 'PROXY_TO_PTHREAD', '-s', 'ASSERTIONS'])

  @requires_threads
  def test_main_thread_async_em_asm(self):
    self.btest_exit(test_file('core/test_main_thread_async_em_asm.cpp'), args=['-O3', '-s', 'USE_PTHREADS', '-s', 'PROXY_TO_PTHREAD', '-s', 'ASSERTIONS'])

  @requires_threads
  def test_main_thread_em_asm_blocking(self):
    create_file('page.html', read_file(test_file('browser/test_em_asm_blocking.html')))

    self.compile_btest([test_file('browser/test_em_asm_blocking.cpp'), '-O2', '-o', 'wasm.js', '-s', 'USE_PTHREADS', '-s', 'PROXY_TO_PTHREAD'])
    self.run_browser('page.html', '', '/report_result?8')

  # Test that it is possible to send a signal via calling alarm(timeout), which in turn calls to the signal handler set by signal(SIGALRM, func);
  def test_sigalrm(self):
    self.btest_exit(test_file('test_sigalrm.c'), args=['-O3'])

  def test_canvas_style_proxy(self):
    self.btest('canvas_style_proxy.c', expected='1', args=['--proxy-to-worker', '--shell-file', test_file('canvas_style_proxy_shell.html'), '--pre-js', test_file('canvas_style_proxy_pre.js')])

  def test_canvas_size_proxy(self):
    self.btest(test_file('canvas_size_proxy.c'), expected='0', args=['--proxy-to-worker'])

  def test_custom_messages_proxy(self):
    self.btest(test_file('custom_messages_proxy.c'), expected='1', args=['--proxy-to-worker', '--shell-file', test_file('custom_messages_proxy_shell.html'), '--post-js', test_file('custom_messages_proxy_postjs.js')])

  def test_vanilla_html_when_proxying(self):
    for opts in [0, 1, 2]:
      print(opts)
      self.compile_btest([test_file('browser_test_hello_world.c'), '-o', 'test.js', '-O' + str(opts), '--proxy-to-worker'])
      create_file('test.html', '<script src="test.js"></script>')
      self.run_browser('test.html', None, '/report_result?0')

  def test_in_flight_memfile_request(self):
    # test the XHR for an asm.js mem init file being in flight already
    for o in [0, 1, 2]:
      print(o)
      opts = ['-O' + str(o), '-s', 'WASM=0']

      print('plain html')
      self.compile_btest([test_file('in_flight_memfile_request.c'), '-o', 'test.js'] + opts)
      create_file('test.html', '<script src="test.js"></script>')
      self.run_browser('test.html', None, '/report_result?0') # never when we provide our own HTML like this.

      print('default html')
      self.btest('in_flight_memfile_request.c', expected='0' if o < 2 else '1', args=opts) # should happen when there is a mem init file (-O2+)

  @requires_sync_compilation
  def test_binaryen_async(self):
    # notice when we use async compilation
    script = '''
    <script>
      // note if we do async compilation
      var real_wasm_instantiate = WebAssembly.instantiate;
      var real_wasm_instantiateStreaming = WebAssembly.instantiateStreaming;
      if (typeof real_wasm_instantiateStreaming === 'function') {
        WebAssembly.instantiateStreaming = function(a, b) {
          Module.sawAsyncCompilation = true;
          return real_wasm_instantiateStreaming(a, b);
        };
      } else {
        WebAssembly.instantiate = function(a, b) {
          Module.sawAsyncCompilation = true;
          return real_wasm_instantiate(a, b);
        };
      }
      // show stderr for the viewer's fun
      err = function(x) {
        out('<<< ' + x + ' >>>');
        console.log(x);
      };
    </script>
    {{{ SCRIPT }}}
'''
    shell_with_script('shell.html', 'shell.html', script)
    common_args = ['--shell-file', 'shell.html']
    for opts, returncode in [
      ([], 1),
      (['-O1'], 1),
      (['-O2'], 1),
      (['-O3'], 1),
      (['-s', 'WASM_ASYNC_COMPILATION'], 1), # force it on
      (['-O1', '-s', 'WASM_ASYNC_COMPILATION=0'], 0), # force it off
    ]:
      print(opts, returncode)
      self.btest_exit('binaryen_async.c', assert_returncode=returncode, args=common_args + opts)
    # Ensure that compilation still works and is async without instantiateStreaming available
    no_streaming = ' <script> WebAssembly.instantiateStreaming = undefined;</script>'
    shell_with_script('shell.html', 'shell.html', no_streaming + script)
    self.btest_exit('binaryen_async.c', assert_returncode=1, args=common_args)

  # Test that implementing Module.instantiateWasm() callback works.
  @parameterized({
    '': ([],),
    'asan': (['-fsanitize=address', '-s', 'INITIAL_MEMORY=128MB'],)
  })
  def test_manual_wasm_instantiate(self, args=[]):
    self.compile_btest([test_file('manual_wasm_instantiate.cpp'), '-o', 'manual_wasm_instantiate.js'] + args)
    shutil.copyfile(test_file('manual_wasm_instantiate.html'), 'manual_wasm_instantiate.html')
    self.run_browser('manual_wasm_instantiate.html', 'wasm instantiation succeeded', '/report_result?1')

  def test_wasm_locate_file(self):
    # Test that it is possible to define "Module.locateFile(foo)" function to locate where worker.js will be loaded from.
    ensure_dir('cdn')
    create_file('shell2.html', read_file(path_from_root('src', 'shell.html')).replace('var Module = {', 'var Module = { locateFile: function(filename) { if (filename == "test.wasm") return "cdn/test.wasm"; else return filename; }, '))
    self.compile_btest([test_file('browser_test_hello_world.c'), '--shell-file', 'shell2.html', '-o', 'test.html'])
    shutil.move('test.wasm', Path('cdn/test.wasm'))
    self.run_browser('test.html', '', '/report_result?0')

  @also_with_threads
  def test_utf8_textdecoder(self):
    self.btest_exit('benchmark_utf8.cpp', 0, args=['--embed-file', test_file('utf8_corpus.txt') + '@/utf8_corpus.txt', '-s', 'EXPORTED_RUNTIME_METHODS=[UTF8ToString]'])

  @also_with_threads
  def test_utf16_textdecoder(self):
    self.btest_exit('benchmark_utf16.cpp', 0, args=['--embed-file', test_file('utf16_corpus.txt') + '@/utf16_corpus.txt', '-s', 'EXPORTED_RUNTIME_METHODS=[UTF16ToString,stringToUTF16,lengthBytesUTF16]'])

  @also_with_threads
  def test_TextDecoder(self):
    self.btest('browser_test_hello_world.c', '0', args=['-s', 'TEXTDECODER=0'])
    just_fallback = os.path.getsize('test.js')
    self.btest('browser_test_hello_world.c', '0')
    td_with_fallback = os.path.getsize('test.js')
    self.btest('browser_test_hello_world.c', '0', args=['-s', 'TEXTDECODER=2'])
    td_without_fallback = os.path.getsize('test.js')
    # pthread TextDecoder support is more complex due to
    # https://github.com/whatwg/encoding/issues/172
    # and therefore the expected code size win there is actually a loss
    if '-pthread' not in self.emcc_args:
      self.assertLess(td_without_fallback, just_fallback)
    else:
      self.assertGreater(td_without_fallback, just_fallback)
    self.assertLess(just_fallback, td_with_fallback)

  def test_small_js_flags(self):
    self.btest('browser_test_hello_world.c', '0', args=['-O3', '--closure=1', '-s', 'INCOMING_MODULE_JS_API=[]', '-s', 'ENVIRONMENT=web'])
    # Check an absolute js code size, with some slack.
    size = os.path.getsize('test.js')
    print('size:', size)
    # Note that this size includes test harness additions (for reporting the result, etc.).
    self.assertLess(abs(size - 5368), 100)

  # Tests that it is possible to initialize and render WebGL content in a pthread by using OffscreenCanvas.
  # -DTEST_CHAINED_WEBGL_CONTEXT_PASSING: Tests that it is possible to transfer WebGL canvas in a chain from main thread -> thread 1 -> thread 2 and then init and render WebGL content there.
  @no_chrome('see https://crbug.com/961765')
  @requires_threads
  @requires_offscreen_canvas
  @requires_graphics_hardware
  def test_webgl_offscreen_canvas_in_pthread(self):
    for args in [[], ['-DTEST_CHAINED_WEBGL_CONTEXT_PASSING']]:
      self.btest('gl_in_pthread.cpp', expected='1', args=args + ['-s', 'USE_PTHREADS', '-s', 'PTHREAD_POOL_SIZE=2', '-s', 'OFFSCREENCANVAS_SUPPORT', '-lGL'])

  # Tests that it is possible to render WebGL content on a <canvas> on the main thread, after it has once been used to render WebGL content in a pthread first
  # -DTEST_MAIN_THREAD_EXPLICIT_COMMIT: Test the same (WebGL on main thread after pthread), but by using explicit .commit() to swap on the main thread instead of implicit "swap when rAF ends" logic
  @requires_threads
  @requires_offscreen_canvas
  @requires_graphics_hardware
  @disabled('This test is disabled because current OffscreenCanvas does not allow transfering it after a rendering context has been created for it.')
  def test_webgl_offscreen_canvas_in_mainthread_after_pthread(self):
    for args in [[], ['-DTEST_MAIN_THREAD_EXPLICIT_COMMIT']]:
      self.btest('gl_in_mainthread_after_pthread.cpp', expected='0', args=args + ['-s', 'USE_PTHREADS', '-s', 'PTHREAD_POOL_SIZE=2', '-s', 'OFFSCREENCANVAS_SUPPORT', '-lGL'])

  @requires_threads
  @requires_offscreen_canvas
  @requires_graphics_hardware
  def test_webgl_offscreen_canvas_only_in_pthread(self):
    self.btest_exit('gl_only_in_pthread.cpp', args=['-s', 'USE_PTHREADS', '-s', 'PTHREAD_POOL_SIZE', '-s', 'OFFSCREENCANVAS_SUPPORT', '-lGL', '-s', 'OFFSCREEN_FRAMEBUFFER'])

  # Tests that rendering from client side memory without default-enabling extensions works.
  @requires_graphics_hardware
  def test_webgl_from_client_side_memory_without_default_enabled_extensions(self):
    self.btest_exit('webgl_draw_triangle.c', args=['-lGL', '-s', 'OFFSCREEN_FRAMEBUFFER', '-DEXPLICIT_SWAP=1', '-DDRAW_FROM_CLIENT_MEMORY=1', '-s', 'FULL_ES2=1'])

  # Tests for WEBGL_multi_draw extension
  # For testing WebGL draft extensions like this, if using chrome as the browser,
  # We might want to append the --enable-webgl-draft-extensions to the EMTEST_BROWSER env arg.
  @requires_graphics_hardware
  def test_webgl_multi_draw(self):
    self.btest('webgl_multi_draw_test.c', reference='webgl_multi_draw.png',
               args=['-lGL', '-s', 'OFFSCREEN_FRAMEBUFFER', '-DMULTI_DRAW_ARRAYS=1', '-DEXPLICIT_SWAP=1'])
    self.btest('webgl_multi_draw_test.c', reference='webgl_multi_draw.png',
               args=['-lGL', '-s', 'OFFSCREEN_FRAMEBUFFER', '-DMULTI_DRAW_ARRAYS_INSTANCED=1', '-DEXPLICIT_SWAP=1'])
    self.btest('webgl_multi_draw_test.c', reference='webgl_multi_draw.png',
               args=['-lGL', '-s', 'OFFSCREEN_FRAMEBUFFER', '-DMULTI_DRAW_ELEMENTS=1', '-DEXPLICIT_SWAP=1'])
    self.btest('webgl_multi_draw_test.c', reference='webgl_multi_draw.png',
               args=['-lGL', '-s', 'OFFSCREEN_FRAMEBUFFER', '-DMULTI_DRAW_ELEMENTS_INSTANCED=1', '-DEXPLICIT_SWAP=1'])

  # Tests for base_vertex/base_instance extension
  # For testing WebGL draft extensions like this, if using chrome as the browser,
  # We might want to append the --enable-webgl-draft-extensions to the EMTEST_BROWSER env arg.
  # If testing on Mac, you also need --use-cmd-decoder=passthrough to get this extension.
  # Also there is a known bug with Mac Intel baseInstance which can fail producing the expected image result.
  @requires_graphics_hardware
  def test_webgl_draw_base_vertex_base_instance(self):
    for multiDraw in [0, 1]:
      for drawElements in [0, 1]:
        self.btest('webgl_draw_base_vertex_base_instance_test.c', reference='webgl_draw_instanced_base_vertex_base_instance.png',
                   args=['-lGL',
                         '-s', 'MAX_WEBGL_VERSION=2',
                         '-s', 'OFFSCREEN_FRAMEBUFFER',
                         '-DMULTI_DRAW=' + str(multiDraw),
                         '-DDRAW_ELEMENTS=' + str(drawElements),
                         '-DEXPLICIT_SWAP=1',
                         '-DWEBGL_CONTEXT_VERSION=2'])

  @requires_graphics_hardware
  def test_webgl_sample_query(self):
    cmd = ['-s', 'MAX_WEBGL_VERSION=2', '-lGL']
    self.btest_exit('webgl_sample_query.cpp', args=cmd)

  @requires_graphics_hardware
  def test_webgl_timer_query(self):
    for args in [
        # EXT query entrypoints on WebGL 1.0
        ['-s', 'MAX_WEBGL_VERSION'],
        # builtin query entrypoints on WebGL 2.0
        ['-s', 'MAX_WEBGL_VERSION=2', '-DTEST_WEBGL2'],
        # EXT query entrypoints on a WebGL 1.0 context while built for WebGL 2.0
        ['-s', 'MAX_WEBGL_VERSION=2'],
      ]:
      cmd = args + ['-lGL']
      self.btest_exit('webgl_timer_query.cpp', args=cmd)

  # Tests that -s OFFSCREEN_FRAMEBUFFER=1 rendering works.
  @requires_graphics_hardware
  def test_webgl_offscreen_framebuffer(self):
    # Tests all the different possible versions of libgl
    for threads in [[], ['-s', 'USE_PTHREADS', '-s', 'PROXY_TO_PTHREAD']]:
      for version in [[], ['-s', 'FULL_ES3'], ['-s', 'FULL_ES3']]:
        args = ['-lGL', '-s', 'OFFSCREEN_FRAMEBUFFER', '-DEXPLICIT_SWAP=1'] + threads + version
        print('with args: %s' % str(args))
        self.btest_exit('webgl_draw_triangle.c', args=args)

  # Tests that VAOs can be used even if WebGL enableExtensionsByDefault is set to 0.
  @requires_graphics_hardware
  def test_webgl_vao_without_automatic_extensions(self):
    self.btest_exit('test_webgl_no_auto_init_extensions.c', args=['-lGL', '-s', 'GL_SUPPORT_AUTOMATIC_ENABLE_EXTENSIONS=0'])

  # Tests that offscreen framebuffer state restoration works
  @requires_graphics_hardware
  def test_webgl_offscreen_framebuffer_state_restoration(self):
    for args in [
        # full state restoration path on WebGL 1.0
        ['-s', 'MAX_WEBGL_VERSION', '-s', 'OFFSCREEN_FRAMEBUFFER_FORBID_VAO_PATH'],
        # VAO path on WebGL 1.0
        ['-s', 'MAX_WEBGL_VERSION'],
        ['-s', 'MAX_WEBGL_VERSION=2', '-DTEST_WEBGL2=0'],
        # VAO path on WebGL 2.0
        ['-s', 'MAX_WEBGL_VERSION=2', '-DTEST_WEBGL2=1', '-DTEST_ANTIALIAS=1', '-DTEST_REQUIRE_VAO=1'],
        # full state restoration path on WebGL 2.0
        ['-s', 'MAX_WEBGL_VERSION=2', '-DTEST_WEBGL2=1', '-DTEST_ANTIALIAS=1', '-s', 'OFFSCREEN_FRAMEBUFFER_FORBID_VAO_PATH'],
        # blitFramebuffer path on WebGL 2.0 (falls back to VAO on Firefox < 67)
        ['-s', 'MAX_WEBGL_VERSION=2', '-DTEST_WEBGL2=1', '-DTEST_ANTIALIAS=0'],
      ]:
      cmd = args + ['-lGL', '-s', 'OFFSCREEN_FRAMEBUFFER', '-DEXPLICIT_SWAP=1']
      self.btest_exit('webgl_offscreen_framebuffer_swap_with_bad_state.c', args=cmd)

  # Tests that -s WORKAROUND_OLD_WEBGL_UNIFORM_UPLOAD_IGNORED_OFFSET_BUG=1 rendering works.
  @requires_graphics_hardware
  def test_webgl_workaround_webgl_uniform_upload_bug(self):
    self.btest_exit('webgl_draw_triangle_with_uniform_color.c',  args=['-lGL', '-s', 'WORKAROUND_OLD_WEBGL_UNIFORM_UPLOAD_IGNORED_OFFSET_BUG'])

  # Tests that using an array of structs in GL uniforms works.
  @requires_graphics_hardware
  def test_webgl_array_of_structs_uniform(self):
    self.btest('webgl_array_of_structs_uniform.c', args=['-lGL', '-s', 'MAX_WEBGL_VERSION=2'], reference='webgl_array_of_structs_uniform.png')

  # Tests that if a WebGL context is created in a pthread on a canvas that has not been transferred to that pthread, WebGL calls are then proxied to the main thread
  # -DTEST_OFFSCREEN_CANVAS=1: Tests that if a WebGL context is created on a pthread that has the canvas transferred to it via using Emscripten's EMSCRIPTEN_PTHREAD_TRANSFERRED_CANVASES="#canvas", then OffscreenCanvas is used
  # -DTEST_OFFSCREEN_CANVAS=2: Tests that if a WebGL context is created on a pthread that has the canvas transferred to it via automatic transferring of Module.canvas when EMSCRIPTEN_PTHREAD_TRANSFERRED_CANVASES is not defined, then OffscreenCanvas is also used
  @requires_threads
  @requires_offscreen_canvas
  @requires_graphics_hardware
  def test_webgl_offscreen_canvas_in_proxied_pthread(self):
    for asyncify in [0, 1]:
      cmd = ['-s', 'USE_PTHREADS', '-s', 'OFFSCREENCANVAS_SUPPORT', '-lGL', '-s', 'GL_DEBUG', '-s', 'PROXY_TO_PTHREAD']
      if asyncify:
        # given the synchronous render loop here, asyncify is needed to see intermediate frames and
        # the gradual color change
        cmd += ['-s', 'ASYNCIFY', '-DASYNCIFY']
      print(str(cmd))
      self.btest('gl_in_proxy_pthread.cpp', expected='1', args=cmd)

  @requires_threads
  @requires_graphics_hardware
  @requires_offscreen_canvas
  def test_webgl_resize_offscreencanvas_from_main_thread(self):
    for args1 in [[], ['-s', 'PROXY_TO_PTHREAD']]:
      for args2 in [[], ['-DTEST_SYNC_BLOCKING_LOOP=1']]:
        for args3 in [[], ['-s', 'OFFSCREENCANVAS_SUPPORT', '-s', 'OFFSCREEN_FRAMEBUFFER']]:
          cmd = args1 + args2 + args3 + ['-s', 'USE_PTHREADS', '-lGL', '-s', 'GL_DEBUG']
          print(str(cmd))
          self.btest('resize_offscreencanvas_from_main_thread.cpp', expected='1', args=cmd)

  @requires_graphics_hardware
  def test_webgl_simple_enable_extensions(self):
    for webgl_version in [1, 2]:
      for simple_enable_extensions in [0, 1]:
        cmd = ['-DWEBGL_CONTEXT_VERSION=' + str(webgl_version),
               '-DWEBGL_SIMPLE_ENABLE_EXTENSION=' + str(simple_enable_extensions),
               '-s', 'MAX_WEBGL_VERSION=2',
               '-s', 'GL_SUPPORT_AUTOMATIC_ENABLE_EXTENSIONS=' + str(simple_enable_extensions),
               '-s', 'GL_SUPPORT_SIMPLE_ENABLE_EXTENSIONS=' + str(simple_enable_extensions)]
        self.btest_exit('webgl2_simple_enable_extensions.c', args=cmd)

  # Tests the feature that shell html page can preallocate the typed array and place it
  # to Module.buffer before loading the script page.
  # In this build mode, the -s INITIAL_MEMORY=xxx option will be ignored.
  # Preallocating the buffer in this was is asm.js only (wasm needs a Memory).
  def test_preallocated_heap(self):
    self.btest_exit('test_preallocated_heap.cpp', args=['-s', 'WASM=0', '-s', 'INITIAL_MEMORY=16MB', '-s', 'ABORTING_MALLOC=0', '--shell-file', test_file('test_preallocated_heap_shell.html')])

  # Tests emscripten_fetch() usage to XHR data directly to memory without persisting results to IndexedDB.
  def test_fetch_to_memory(self):
    # Test error reporting in the negative case when the file URL doesn't exist. (http 404)
    self.btest_exit('fetch/to_memory.cpp',
                    args=['-s', 'FETCH_DEBUG', '-s', 'FETCH', '-DFILE_DOES_NOT_EXIST'],
                    also_asmjs=True)

    # Test the positive case when the file URL exists. (http 200)
    shutil.copyfile(test_file('gears.png'), 'gears.png')
    for arg in [[], ['-s', 'FETCH_SUPPORT_INDEXEDDB=0']]:
      self.btest_exit('fetch/to_memory.cpp',
                      args=['-s', 'FETCH_DEBUG', '-s', 'FETCH'] + arg,
                      also_asmjs=True)

  @parameterized({
    '': ([],),
    'pthread_exit': (['-DDO_PTHREAD_EXIT'],),
  })
  @requires_threads
  def test_fetch_from_thread(self, args):
    shutil.copyfile(test_file('gears.png'), 'gears.png')
    self.btest('fetch/from_thread.cpp',
               expected='42',
               args=args + ['-s', 'USE_PTHREADS', '-s', 'PROXY_TO_PTHREAD', '-s', 'FETCH_DEBUG', '-s', 'FETCH', '-DFILE_DOES_NOT_EXIST'],
               also_asmjs=True)

  def test_fetch_to_indexdb(self):
    shutil.copyfile(test_file('gears.png'), 'gears.png')
    self.btest_exit('fetch/to_indexeddb.cpp',
                    args=['-s', 'FETCH_DEBUG', '-s', 'FETCH'],
                    also_asmjs=True)

  # Tests emscripten_fetch() usage to persist an XHR into IndexedDB and subsequently load up from there.
  def test_fetch_cached_xhr(self):
    shutil.copyfile(test_file('gears.png'), 'gears.png')
    self.btest_exit('fetch/cached_xhr.cpp',
                    args=['-s', 'FETCH_DEBUG', '-s', 'FETCH'],
                    also_asmjs=True)

  # Tests that response headers get set on emscripten_fetch_t values.
  @requires_threads
  def test_fetch_response_headers(self):
    shutil.copyfile(test_file('gears.png'), 'gears.png')
    self.btest_exit('fetch/response_headers.cpp', args=['-s', 'FETCH_DEBUG', '-s', 'FETCH', '-s', 'USE_PTHREADS', '-s', 'PROXY_TO_PTHREAD'], also_asmjs=True)

  # Test emscripten_fetch() usage to stream a XHR in to memory without storing the full file in memory
  def test_fetch_stream_file(self):
    self.skipTest('moz-chunked-arraybuffer was firefox-only and has been removed')
    # Strategy: create a large 128MB file, and compile with a small 16MB Emscripten heap, so that the tested file
    # won't fully fit in the heap. This verifies that streaming works properly.
    s = '12345678'
    for i in range(14):
      s = s[::-1] + s # length of str will be 2^17=128KB
    with open('largefile.txt', 'w') as f:
      for i in range(1024):
        f.write(s)
    self.btest_exit('fetch/stream_file.cpp',
                    args=['-s', 'FETCH_DEBUG', '-s', 'FETCH', '-s', 'INITIAL_MEMORY=536870912'],
                    also_asmjs=True)

  # Tests emscripten_fetch() usage in synchronous mode when used from the main
  # thread proxied to a Worker with -s PROXY_TO_PTHREAD=1 option.
  @requires_threads
  def test_fetch_sync_xhr(self):
    shutil.copyfile(test_file('gears.png'), 'gears.png')
    self.btest_exit('fetch/sync_xhr.cpp', args=['-s', 'FETCH_DEBUG', '-s', 'FETCH', '-s', 'USE_PTHREADS', '-s', 'PROXY_TO_PTHREAD'])

  # Tests emscripten_fetch() usage when user passes none of the main 3 flags (append/replace/no_download).
  # In that case, in append is implicitly understood.
  @requires_threads
  def test_fetch_implicit_append(self):
    shutil.copyfile(test_file('gears.png'), 'gears.png')
    self.btest_exit('fetch/example_synchronous_fetch.cpp', args=['-s', 'FETCH', '-s', 'USE_PTHREADS', '-s', 'PROXY_TO_PTHREAD'])

  # Tests synchronous emscripten_fetch() usage from wasm pthread in fastcomp.
  @requires_threads
  def test_fetch_sync_xhr_in_wasm(self):
    shutil.copyfile(test_file('gears.png'), 'gears.png')
    self.btest_exit('fetch/example_synchronous_fetch.cpp', args=['-s', 'FETCH', '-s', 'USE_PTHREADS', '-s', 'PROXY_TO_PTHREAD'])

  # Tests that the Fetch API works for synchronous XHRs when used with --proxy-to-worker.
  @requires_threads
  def test_fetch_sync_xhr_in_proxy_to_worker(self):
    shutil.copyfile(test_file('gears.png'), 'gears.png')
    self.btest_exit('fetch/sync_xhr.cpp',
                    args=['-s', 'FETCH_DEBUG', '-s', 'FETCH', '--proxy-to-worker'],
                    also_asmjs=True)

  # Tests waiting on EMSCRIPTEN_FETCH_WAITABLE request from a worker thread
  @no_wasm_backend("emscripten_fetch_wait uses an asm.js based web worker")
  @requires_threads
  def test_fetch_sync_fetch_in_main_thread(self):
    shutil.copyfile(test_file('gears.png'), 'gears.png')
    self.btest('fetch/sync_fetch_in_main_thread.cpp', expected='0', args=['-s', 'FETCH_DEBUG', '-s', 'FETCH', '-s', 'WASM=0', '-s', 'USE_PTHREADS', '-s', 'PROXY_TO_PTHREAD'])

  @requires_threads
  @no_wasm_backend("WASM2JS does not yet support pthreads")
  def test_fetch_idb_store(self):
    self.btest('fetch/idb_store.cpp', expected='0', args=['-s', 'USE_PTHREADS', '-s', 'FETCH', '-s', 'WASM=0', '-s', 'PROXY_TO_PTHREAD'])

  @requires_threads
  @no_wasm_backend("WASM2JS does not yet support pthreads")
  def test_fetch_idb_delete(self):
    shutil.copyfile(test_file('gears.png'), 'gears.png')
    self.btest('fetch/idb_delete.cpp', expected='0', args=['-s', 'USE_PTHREADS', '-s', 'FETCH_DEBUG', '-s', 'FETCH', '-s', 'WASM=0', '-s', 'PROXY_TO_PTHREAD'])

  @requires_asmfs
  @requires_threads
  def test_asmfs_hello_file(self):
    # Test basic file loading and the valid character set for files.
    ensure_dir('dirrey')
    shutil.copyfile(test_file('asmfs/hello_file.txt'), Path('dirrey', 'hello file !#$%&\'()+,-.;=@[]^_`{}~ %%.txt'))
    self.btest_exit('asmfs/hello_file.cpp', args=['-s', 'ASMFS', '-s', 'WASM=0', '-s', 'USE_PTHREADS', '-s', 'FETCH_DEBUG', '-s', 'PROXY_TO_PTHREAD'])

  @requires_asmfs
  @requires_threads
  def test_asmfs_read_file_twice(self):
    shutil.copyfile(test_file('asmfs/hello_file.txt'), 'hello_file.txt')
    self.btest_exit('asmfs/read_file_twice.cpp', args=['-s', 'ASMFS', '-s', 'WASM=0', '-s', 'USE_PTHREADS', '-s', 'FETCH_DEBUG', '-s', 'PROXY_TO_PTHREAD'])

  @requires_asmfs
  @requires_threads
  def test_asmfs_fopen_write(self):
    self.btest_exit('asmfs/fopen_write.cpp', args=['-s', 'ASMFS', '-s', 'WASM=0', '-s', 'USE_PTHREADS', '-s', 'FETCH_DEBUG'])

  @requires_asmfs
  @requires_threads
  def test_asmfs_mkdir_create_unlink_rmdir(self):
    self.btest_exit('cstdio/test_remove.cpp', args=['-s', 'ASMFS', '-s', 'WASM=0', '-s', 'USE_PTHREADS', '-s', 'FETCH_DEBUG'])

  @requires_asmfs
  @requires_threads
  def test_asmfs_dirent_test_readdir(self):
    self.btest('dirent/test_readdir.c', expected='0', args=['-s', 'ASMFS', '-s', 'WASM=0', '-s', 'USE_PTHREADS', '-s', 'FETCH_DEBUG'])

  @requires_asmfs
  @requires_threads
  def test_asmfs_dirent_test_readdir_empty(self):
    self.btest('dirent/test_readdir_empty.c', expected='0', args=['-s', 'ASMFS', '-s', 'WASM=0', '-s', 'USE_PTHREADS', '-s', 'FETCH_DEBUG'])

  @requires_asmfs
  @requires_threads
  def test_asmfs_unistd_close(self):
    self.btest_exit(test_file('unistd/close.c'), 0, args=['-s', 'ASMFS', '-s', 'WASM=0', '-s', 'USE_PTHREADS', '-s', 'FETCH_DEBUG'])

  @requires_asmfs
  @requires_threads
  def test_asmfs_unistd_access(self):
    self.btest_exit(test_file('unistd/access.c'), 0, args=['-s', 'ASMFS', '-s', 'WASM=0', '-s', 'USE_PTHREADS', '-s', 'FETCH_DEBUG'])

  @requires_asmfs
  @requires_threads
  def test_asmfs_unistd_unlink(self):
    # TODO: Once symlinks are supported, remove -DNO_SYMLINK=1
    self.btest_exit(test_file('unistd/unlink.c'), 0, args=['-s', 'ASMFS', '-s', 'WASM=0', '-s', 'USE_PTHREADS', '-s', 'FETCH_DEBUG', '-DNO_SYMLINK=1'])

  @requires_asmfs
  @requires_threads
  def test_asmfs_test_fcntl_open(self):
    self.btest('fcntl/test_fcntl_open.c', expected='0', args=['-s', 'ASMFS', '-s', 'WASM=0', '-s', 'USE_PTHREADS', '-s', 'FETCH_DEBUG', '-s', 'PROXY_TO_PTHREAD'])

  @requires_asmfs
  @requires_threads
  def test_asmfs_relative_paths(self):
    self.btest_exit('asmfs/relative_paths.cpp', args=['-s', 'ASMFS', '-s', 'WASM=0', '-s', 'USE_PTHREADS', '-s', 'FETCH_DEBUG'])

  @requires_threads
  def test_pthread_locale(self):
    for args in [
        [],
        ['-s', 'USE_PTHREADS', '-s', 'PTHREAD_POOL_SIZE=2'],
    ]:
      print("Testing with: ", args)
      self.btest_exit('pthread/test_pthread_locale.c', args=args)

  # Tests the Emscripten HTML5 API emscripten_set_canvas_element_size() and
  # emscripten_get_canvas_element_size() functionality in singlethreaded programs.
  def test_emscripten_set_canvas_element_size(self):
    self.btest_exit('emscripten_set_canvas_element_size.c')

  # Test that emscripten_get_device_pixel_ratio() is callable from pthreads (and proxies to main
  # thread to obtain the proper window.devicePixelRatio value).
  @requires_threads
  def test_emscripten_get_device_pixel_ratio(self):
    for args in [[], ['-s', 'USE_PTHREADS', '-s', 'PROXY_TO_PTHREAD']]:
      self.btest_exit('emscripten_get_device_pixel_ratio.c', args=args)

  # Tests that emscripten_run_script() variants of functions work in pthreads.
  @requires_threads
  def test_pthread_run_script(self):
    for args in [[], ['-s', 'USE_PTHREADS', '-s', 'PROXY_TO_PTHREAD']]:
      self.btest_exit(test_file('pthread/test_pthread_run_script.cpp'), args=['-O3'] + args)

  # Tests emscripten_set_canvas_element_size() and OffscreenCanvas functionality in different build configurations.
  @requires_threads
  @requires_graphics_hardware
  def test_emscripten_animate_canvas_element_size(self):
    for args in [
      ['-DTEST_EMSCRIPTEN_SET_MAIN_LOOP=1'],
      ['-DTEST_EMSCRIPTEN_SET_MAIN_LOOP=1', '-s', 'PROXY_TO_PTHREAD', '-s', 'USE_PTHREADS', '-s',   'OFFSCREEN_FRAMEBUFFER=1'],
      ['-DTEST_EMSCRIPTEN_SET_MAIN_LOOP=1', '-s', 'PROXY_TO_PTHREAD', '-s', 'USE_PTHREADS', '-s',   'OFFSCREEN_FRAMEBUFFER=1', '-DTEST_EXPLICIT_CONTEXT_SWAP=1'],
      ['-DTEST_EXPLICIT_CONTEXT_SWAP=1',    '-s', 'PROXY_TO_PTHREAD', '-s', 'USE_PTHREADS', '-s',   'OFFSCREEN_FRAMEBUFFER=1'],
      ['-DTEST_EXPLICIT_CONTEXT_SWAP=1',    '-s', 'PROXY_TO_PTHREAD', '-s', 'USE_PTHREADS', '-s',   'OFFSCREEN_FRAMEBUFFER=1', '-DTEST_MANUALLY_SET_ELEMENT_CSS_SIZE=1'],
      ['-DTEST_EMSCRIPTEN_SET_MAIN_LOOP=1', '-s', 'OFFSCREENCANVAS_SUPPORT'],
    ]:
      cmd = ['-lGL', '-O3', '-g2', '--shell-file', test_file('canvas_animate_resize_shell.html'), '-s', 'GL_DEBUG', '--threadprofiler'] + args
      print(' '.join(cmd))
      self.btest_exit('canvas_animate_resize.cpp', args=cmd)

  # Tests the absolute minimum pthread-enabled application.
  @requires_threads
  def test_pthread_hello_thread(self):
    for opts in [[], ['-O3']]:
      for modularize in [[], ['-s', 'MODULARIZE', '-s', 'EXPORT_NAME=MyModule', '--shell-file', test_file('shell_that_launches_modularize.html')]]:
        self.btest(test_file('pthread/hello_thread.c'), expected='1', args=['-s', 'USE_PTHREADS'] + modularize + opts)

  # Tests that a pthreads build of -s MINIMAL_RUNTIME=1 works well in different build modes
  def test_minimal_runtime_hello_pthread(self):
    for opts in [[], ['-O3']]:
      for modularize in [[], ['-s', 'MODULARIZE', '-s', 'EXPORT_NAME=MyModule']]:
        self.btest(test_file('pthread/hello_thread.c'), expected='1', args=['-s', 'MINIMAL_RUNTIME', '-s', 'USE_PTHREADS'] + modularize + opts)

  # Tests memory growth in pthreads mode, but still on the main thread.
  @requires_threads
  def test_pthread_growth_mainthread(self):
    self.emcc_args.remove('-Werror')

    def run(emcc_args=[]):
      self.btest(test_file('pthread/test_pthread_memory_growth_mainthread.c'), expected='1', args=['-s', 'USE_PTHREADS', '-s', 'PTHREAD_POOL_SIZE=2', '-s', 'ALLOW_MEMORY_GROWTH', '-s', 'INITIAL_MEMORY=32MB', '-s', 'MAXIMUM_MEMORY=256MB'] + emcc_args, also_asmjs=False)

    run()
    run(['-s', 'PROXY_TO_PTHREAD'])

  # Tests memory growth in a pthread.
  @requires_threads
  def test_pthread_growth(self):
    self.emcc_args.remove('-Werror')

    def run(emcc_args=[]):
      self.btest(test_file('pthread/test_pthread_memory_growth.c'), expected='1', args=['-s', 'USE_PTHREADS', '-s', 'PTHREAD_POOL_SIZE=2', '-s', 'ALLOW_MEMORY_GROWTH', '-s', 'INITIAL_MEMORY=32MB', '-s', 'MAXIMUM_MEMORY=256MB', '-g'] + emcc_args, also_asmjs=False)

    run()
    run(['-s', 'ASSERTIONS'])
    run(['-s', 'PROXY_TO_PTHREAD'])

  # Tests that time in a pthread is relative to the main thread, so measurements
  # on different threads are still monotonic, as if checking a single central
  # clock.
  @requires_threads
  def test_pthread_reltime(self):
    self.btest(test_file('pthread/test_pthread_reltime.cpp'), expected='3', args=['-s', 'USE_PTHREADS', '-s', 'PTHREAD_POOL_SIZE'])

  # Tests that it is possible to load the main .js file of the application manually via a Blob URL, and still use pthreads.
  @requires_threads
  def test_load_js_from_blob_with_pthreads(self):
    # TODO: enable this with wasm, currently pthreads/atomics have limitations
    self.compile_btest([test_file('pthread/hello_thread.c'), '-s', 'USE_PTHREADS', '-o', 'hello_thread_with_blob_url.js'])
    shutil.copyfile(test_file('pthread/main_js_as_blob_loader.html'), 'hello_thread_with_blob_url.html')
    self.run_browser('hello_thread_with_blob_url.html', 'hello from thread!', '/report_result?1')

  # Tests that base64 utils work in browser with no native atob function
  def test_base64_atob_fallback(self):
    create_file('test.c', r'''
      #include <stdio.h>
      #include <emscripten.h>
      int main() {
        return 0;
      }
    ''')
    # generate a dummy file
    create_file('dummy_file', 'dummy')
    # compile the code with the modularize feature and the preload-file option enabled
    self.compile_btest(['test.c', '-s', 'EXIT_RUNTIME', '-s', 'MODULARIZE', '-s', 'EXPORT_NAME="Foo"', '--preload-file', 'dummy_file', '-s', 'SINGLE_FILE'])
    create_file('a.html', '''
      <script>
        atob = undefined;
        fetch = undefined;
      </script>
      <script src="a.out.js"></script>
      <script>
        var foo = Foo();
      </script>
    ''')
    self.run_browser('a.html', '...', '/report_result?exit:0')

  # Tests that SINGLE_FILE works as intended in generated HTML (with and without Worker)
  def test_single_file_html(self):
    self.btest('single_file_static_initializer.cpp', '19', args=['-s', 'SINGLE_FILE'], also_proxied=True)
    self.assertExists('test.html')
    self.assertNotExists('test.js')
    self.assertNotExists('test.worker.js')
    self.assertNotExists('test.wasm')
    self.assertNotExists('test.mem')

  # Tests that SINGLE_FILE works as intended in generated HTML with MINIMAL_RUNTIME
  def test_minimal_runtime_single_file_html(self):
    for wasm in [0, 1]:
      for opts in [[], ['-O3']]:
        self.btest('single_file_static_initializer.cpp', '19', args=opts + ['-s', 'MINIMAL_RUNTIME', '-s', 'SINGLE_FILE', '-s', 'WASM=' + str(wasm)])
        self.assertExists('test.html')
        self.assertNotExists('test.js')
        self.assertNotExists('test.wasm')
        self.assertNotExists('test.asm.js')
        self.assertNotExists('test.mem')
        self.assertNotExists('test.js')
        self.assertNotExists('test.worker.js')

  # Tests that SINGLE_FILE works when built with ENVIRONMENT=web and Closure enabled (#7933)
  def test_single_file_in_web_environment_with_closure(self):
    self.btest('minimal_hello.c', '0', args=['-s', 'SINGLE_FILE', '-s', 'ENVIRONMENT=web', '-O2', '--closure=1'])

  # Tests that SINGLE_FILE works as intended with locateFile
  def test_single_file_locate_file(self):
    for wasm_enabled in [True, False]:
      args = [test_file('browser_test_hello_world.c'), '-o', 'test.js', '-s', 'SINGLE_FILE']

      if not wasm_enabled:
        args += ['-s', 'WASM=0']

      self.compile_btest(args)

      create_file('test.html', '''
        <script>
          var Module = {
            locateFile: function (path) {
              if (path.indexOf('data:') === 0) {
                throw new Error('Unexpected data URI.');
              }

              return path;
            }
          };
        </script>
        <script src="test.js"></script>
      ''')

      self.run_browser('test.html', None, '/report_result?0')

  # Tests that SINGLE_FILE works as intended in a Worker in JS output
  def test_single_file_worker_js(self):
    self.compile_btest([test_file('browser_test_hello_world.c'), '-o', 'test.js', '--proxy-to-worker', '-s', 'SINGLE_FILE'])
    create_file('test.html', '<script src="test.js"></script>')
    self.run_browser('test.html', None, '/report_result?0')
    self.assertExists('test.js')
    self.assertNotExists('test.worker.js')

  # Tests that pthreads code works as intended in a Worker. That is, a pthreads-using
  # program can run either on the main thread (normal tests) or when we start it in
  # a Worker in this test (in that case, both the main application thread and the worker threads
  # are all inside Web Workers).
  @requires_threads
  def test_pthreads_started_in_worker(self):
    self.compile_btest([test_file('pthread/test_pthread_atomics.cpp'), '-o', 'test.js', '-s', 'INITIAL_MEMORY=64MB', '-s', 'USE_PTHREADS', '-s', 'PTHREAD_POOL_SIZE=8'])
    create_file('test.html', '''
      <script>
        new Worker('test.js');
      </script>
    ''')
    self.run_browser('test.html', None, '/report_result?0')

  def test_access_file_after_heap_resize(self):
    create_file('test.txt', 'hello from file')
    self.compile_btest([test_file('access_file_after_heap_resize.c'), '-s', 'ALLOW_MEMORY_GROWTH', '--preload-file', 'test.txt', '-o', 'page.html'])
    self.run_browser('page.html', 'hello from file', '/report_result?15')

    # with separate file packager invocation
    self.run_process([FILE_PACKAGER, 'data.data', '--preload', 'test.txt', '--js-output=' + 'data.js'])
    self.compile_btest([test_file('access_file_after_heap_resize.c'), '-s', 'ALLOW_MEMORY_GROWTH', '--pre-js', 'data.js', '-o', 'page.html', '-s', 'FORCE_FILESYSTEM'])
    self.run_browser('page.html', 'hello from file', '/report_result?15')

  def test_unicode_html_shell(self):
    create_file('main.cpp', r'''
      int main() {
        REPORT_RESULT(0);
        return 0;
      }
    ''')
    create_file('shell.html', read_file(path_from_root('src', 'shell.html')).replace('Emscripten-Generated Code', 'Emscripten-Generated Emoji 😅'))
    self.compile_btest(['main.cpp', '--shell-file', 'shell.html', '-o', 'test.html'])
    self.run_browser('test.html', None, '/report_result?0')

  # Tests the functionality of the emscripten_thread_sleep() function.
  @requires_threads
  def test_emscripten_thread_sleep(self):
    self.btest(test_file('pthread/emscripten_thread_sleep.c'), expected='1', args=['-s', 'USE_PTHREADS', '-s', 'EXPORTED_RUNTIME_METHODS=[print]'])

  # Tests that Emscripten-compiled applications can be run from a relative path in browser that is different than the address of the current page
  def test_browser_run_from_different_directory(self):
    self.compile_btest([test_file('browser_test_hello_world.c'), '-o', 'test.html', '-O3'])

    ensure_dir('subdir')
    shutil.move('test.js', Path('subdir/test.js'))
    shutil.move('test.wasm', Path('subdir/test.wasm'))
    src = read_file('test.html')
    # Make sure JS is loaded from subdirectory
    create_file('test-subdir.html', src.replace('test.js', 'subdir/test.js'))
    self.run_browser('test-subdir.html', None, '/report_result?0')

  # Similar to `test_browser_run_from_different_directory`, but asynchronous because of `-s MODULARIZE=1`
  def test_browser_run_from_different_directory_async(self):
    for args, creations in [
      (['-s', 'MODULARIZE'], [
        'Module();',    # documented way for using modularize
        'new Module();' # not documented as working, but we support it
       ]),
    ]:
      print(args)
      # compile the code with the modularize feature and the preload-file option enabled
      self.compile_btest([test_file('browser_test_hello_world.c'), '-o', 'test.js', '-O3'] + args)
      ensure_dir('subdir')
      shutil.move('test.js', Path('subdir/test.js'))
      shutil.move('test.wasm', Path('subdir/test.wasm'))
      for creation in creations:
        print(creation)
        # Make sure JS is loaded from subdirectory
        create_file('test-subdir.html', '''
          <script src="subdir/test.js"></script>
          <script>
            %s
          </script>
        ''' % creation)
        self.run_browser('test-subdir.html', None, '/report_result?0')

  # Similar to `test_browser_run_from_different_directory`, but
  # also also we eval the initial code, so currentScript is not present. That prevents us
  # from finding the file in a subdir, but here we at least check we do not regress compared to the
  # normal case of finding in the current dir.
  def test_browser_modularize_no_current_script(self):
    # test both modularize (and creating an instance) and modularize-instance
    # (which creates by itself)
    for path, args, creation in [
      ([], ['-s', 'MODULARIZE'], 'Module();'),
      (['subdir'], ['-s', 'MODULARIZE'], 'Module();'),
    ]:
      print(path, args, creation)
      filesystem_path = os.path.join('.', *path)
      ensure_dir(filesystem_path)
      # compile the code with the modularize feature and the preload-file option enabled
      self.compile_btest([test_file('browser_test_hello_world.c'), '-o', 'test.js'] + args)
      shutil.move('test.js', Path(filesystem_path, 'test.js'))
      shutil.move('test.wasm', Path(filesystem_path, 'test.wasm'))
      create_file(Path(filesystem_path, 'test.html'), '''
        <script>
          setTimeout(function() {
            var xhr = new XMLHttpRequest();
            xhr.open('GET', 'test.js', false);
            xhr.send(null);
            eval(xhr.responseText);
            %s
          }, 1);
        </script>
      ''' % creation)
      self.run_browser('/'.join(path + ['test.html']), None, '/report_result?0')

  def test_emscripten_request_animation_frame(self):
    self.btest(test_file('emscripten_request_animation_frame.c'), '0')

  def test_emscripten_request_animation_frame_loop(self):
    self.btest(test_file('emscripten_request_animation_frame_loop.c'), '0')

  def test_request_animation_frame(self):
    self.btest('request_animation_frame.cpp', '0', also_proxied=True)

  @requires_threads
  def test_emscripten_set_timeout(self):
    self.btest_exit(test_file('emscripten_set_timeout.c'), args=['-s', 'USE_PTHREADS', '-s', 'PROXY_TO_PTHREAD'])

  @requires_threads
  def test_emscripten_set_timeout_loop(self):
    self.btest_exit(test_file('emscripten_set_timeout_loop.c'), args=['-s', 'USE_PTHREADS', '-s', 'PROXY_TO_PTHREAD'])

  def test_emscripten_set_immediate(self):
    self.btest_exit(test_file('emscripten_set_immediate.c'))

  def test_emscripten_set_immediate_loop(self):
    self.btest_exit(test_file('emscripten_set_immediate_loop.c'))

  @requires_threads
  def test_emscripten_set_interval(self):
    self.btest_exit(test_file('emscripten_set_interval.c'), args=['-s', 'USE_PTHREADS', '-s', 'PROXY_TO_PTHREAD'])

  # Test emscripten_performance_now() and emscripten_date_now()
  @requires_threads
  def test_emscripten_performance_now(self):
    self.btest(test_file('emscripten_performance_now.c'), '0', args=['-s', 'USE_PTHREADS', '-s', 'PROXY_TO_PTHREAD'])

  @requires_threads
  def test_embind_with_pthreads(self):
    self.btest('embind_with_pthreads.cpp', '1', args=['--bind', '-s', 'USE_PTHREADS', '-s', 'PROXY_TO_PTHREAD'])

  def test_embind_with_asyncify(self):
    self.btest('embind_with_asyncify.cpp', '1', args=['--bind', '-s', 'ASYNCIFY'])

  # Test emscripten_console_log(), emscripten_console_warn() and emscripten_console_error()
  def test_emscripten_console_log(self):
    self.btest(test_file('emscripten_console_log.c'), '0', args=['--pre-js', test_file('emscripten_console_log_pre.js')])

  def test_emscripten_throw_number(self):
    self.btest(test_file('emscripten_throw_number.c'), '0', args=['--pre-js', test_file('emscripten_throw_number_pre.js')])

  def test_emscripten_throw_string(self):
    self.btest(test_file('emscripten_throw_string.c'), '0', args=['--pre-js', test_file('emscripten_throw_string_pre.js')])

  # Tests that Closure run in combination with -s ENVIRONMENT=web mode works with a minimal console.log() application
  def test_closure_in_web_only_target_environment_console_log(self):
    self.btest('minimal_hello.c', '0', args=['-s', 'ENVIRONMENT=web', '-O3', '--closure=1'])

  # Tests that Closure run in combination with -s ENVIRONMENT=web mode works with a small WebGL application
  @requires_graphics_hardware
  def test_closure_in_web_only_target_environment_webgl(self):
    self.btest_exit('webgl_draw_triangle.c', args=['-lGL', '-s', 'ENVIRONMENT=web', '-O3', '--closure=1'])

  def test_no_declare_asm_module_exports_asmjs(self):
    for minimal_runtime in [[], ['-s', 'MINIMAL_RUNTIME']]:
      self.btest(test_file('declare_asm_module_exports.cpp'), '1', args=['-s', 'DECLARE_ASM_MODULE_EXPORTS=0', '-s', 'ENVIRONMENT=web', '-O3', '--closure=1', '-s', 'WASM=0'] + minimal_runtime)

  def test_no_declare_asm_module_exports_wasm_minimal_runtime(self):
    self.btest(test_file('declare_asm_module_exports.cpp'), '1', args=['-s', 'DECLARE_ASM_MODULE_EXPORTS=0', '-s', 'ENVIRONMENT=web', '-O3', '--closure=1', '-s', 'MINIMAL_RUNTIME'])

  # Tests that the different code paths in src/shell_minimal_runtime.html all work ok.
  def test_minimal_runtime_loader_shell(self):
    args = ['-s', 'MINIMAL_RUNTIME=2']
    for wasm in [[], ['-s', 'WASM=0', '--memory-init-file', '0'], ['-s', 'WASM=0', '--memory-init-file', '1'], ['-s', 'SINGLE_FILE'], ['-s', 'WASM=0', '-s', 'SINGLE_FILE']]:
      for modularize in [[], ['-s', 'MODULARIZE']]:
        print(str(args + wasm + modularize))
        self.btest('minimal_hello.c', '0', args=args + wasm + modularize)

  # Tests that -s MINIMAL_RUNTIME=1 works well in different build modes
  def test_minimal_runtime_hello_world(self):
    for args in [[], ['-s', 'MINIMAL_RUNTIME_STREAMING_WASM_COMPILATION', '--closure=1'], ['-s', 'MINIMAL_RUNTIME_STREAMING_WASM_INSTANTIATION', '--closure', '1']]:
      self.btest(test_file('small_hello_world.c'), '0', args=args + ['-s', 'MINIMAL_RUNTIME'])

  @requires_threads
  def test_offset_converter(self, *args):
    self.btest_exit(test_file('browser/test_offset_converter.c'), assert_returncode=1, args=['-s', 'USE_OFFSET_CONVERTER', '-gsource-map', '-s', 'PROXY_TO_PTHREAD', '-s', 'USE_PTHREADS'])

  # Tests emscripten_unwind_to_js_event_loop() behavior
  def test_emscripten_unwind_to_js_event_loop(self, *args):
    self.btest_exit(test_file('browser/test_emscripten_unwind_to_js_event_loop.c'))

  def test_wasm2js_fallback(self):
    for args in [[], ['-s', 'MINIMAL_RUNTIME']]:
      self.compile_btest([test_file('small_hello_world.c'), '-s', 'WASM=2', '-o', 'test.html'] + args)

      # First run with WebAssembly support enabled
      # Move the Wasm2js fallback away to test it is not accidentally getting loaded.
      os.rename('test.wasm.js', 'test.wasm.js.unused')
      self.run_browser('test.html', 'hello!', '/report_result?0')
      os.rename('test.wasm.js.unused', 'test.wasm.js')

      # Then disable WebAssembly support in VM, and try again.. Should still work with Wasm2JS fallback.
      html = read_file('test.html')
      html = html.replace('<body>', '<body><script>delete WebAssembly;</script>')
      open('test.html', 'w').write(html)
      os.remove('test.wasm') # Also delete the Wasm file to test that it is not attempted to be loaded.
      self.run_browser('test.html', 'hello!', '/report_result?0')

  def test_wasm2js_fallback_on_wasm_compilation_failure(self):
    for args in [[], ['-s', 'MINIMAL_RUNTIME']]:
      self.compile_btest([test_file('small_hello_world.c'), '-s', 'WASM=2', '-o', 'test.html'] + args)

      # Run without the .wasm.js file present: with Wasm support, the page should still run
      os.rename('test.wasm.js', 'test.wasm.js.unused')
      self.run_browser('test.html', 'hello!', '/report_result?0')

      # Restore the .wasm.js file, then corrupt the .wasm file, that should trigger the Wasm2js fallback to run
      os.rename('test.wasm.js.unused', 'test.wasm.js')
      shutil.copyfile('test.js', 'test.wasm')
      self.run_browser('test.html', 'hello!', '/report_result?0')

  def test_system(self):
    self.btest_exit(test_file('system.c'))

  # Tests that it is possible to hook into/override a symbol defined in a system library.
  @requires_graphics_hardware
  def test_override_system_js_lib_symbol(self):
    # This test verifies it is possible to override a symbol from WebGL library.

    # When WebGL is implicitly linked in, the implicit linking should happen before any user --js-libraries, so that they can adjust
    # the behavior afterwards.
    self.btest_exit(test_file('test_override_system_js_lib_symbol.c'),
                    args=['--js-library', test_file('test_override_system_js_lib_symbol.js')])

    # When WebGL is explicitly linked to in strict mode, the linking order on command line should enable overriding.
    self.btest_exit(test_file('test_override_system_js_lib_symbol.c'),
                    args=['-s', 'AUTO_JS_LIBRARIES=0', '-lwebgl.js', '--js-library', test_file('test_override_system_js_lib_symbol.js')])

  @no_firefox('no 4GB support yet')
  @require_v8
  def test_zzz_zzz_4gb(self):
    # TODO Convert to an actual browser test when it reaches stable.
    #      For now, keep this in browser as this suite runs serially, which
    #      means we don't compete for memory with anything else (and run it
    #      at the very very end, to reduce the risk of it OOM-killing the
    #      browser).

    # test that we can allocate in the 2-4GB range, if we enable growth and
    # set the max appropriately
    self.emcc_args += ['-O2', '-s', 'ALLOW_MEMORY_GROWTH', '-s', 'MAXIMUM_MEMORY=4GB']
    self.do_run_in_out_file_test('browser', 'test_4GB.cpp')

  # Tests that emmalloc supports up to 4GB Wasm heaps.
  @no_firefox('no 4GB support yet')
  def test_zzz_zzz_emmalloc_4gb(self):
    self.btest(test_file('mem_growth.cpp'),
               expected='-65536', # == 4*1024*1024*1024 - 65536 casted to signed
               args=['-s', 'MALLOC=emmalloc', '-s', 'ABORTING_MALLOC=0', '-s', 'ALLOW_MEMORY_GROWTH=1', '-s', 'MAXIMUM_MEMORY=4GB'])

  # Test that it is possible to malloc() a huge 3GB memory block in 4GB mode using emmalloc.
  # Also test emmalloc-memvalidate and emmalloc-memvalidate-verbose build configurations.
  @no_firefox('no 4GB support yet')
  def test_emmalloc_3GB(self):
    def test(args):
      self.btest(test_file('alloc_3gb.cpp'),
                 expected='0',
                 args=['-s', 'MAXIMUM_MEMORY=4GB', '-s', 'ALLOW_MEMORY_GROWTH=1'] + args)

    test(['-s', 'MALLOC=emmalloc'])
    test(['-s', 'MALLOC=emmalloc-debug'])
    test(['-s', 'MALLOC=emmalloc-memvalidate'])
    test(['-s', 'MALLOC=emmalloc-memvalidate-verbose'])

  @no_firefox('no 4GB support yet')
  def test_zzz_zzz_emmalloc_memgrowth(self, *args):
    self.btest(test_file('browser/emmalloc_memgrowth.cpp'), expected='0', args=['-s', 'MALLOC=emmalloc', '-s', 'ALLOW_MEMORY_GROWTH=1', '-s', 'ABORTING_MALLOC=0', '-s', 'ASSERTIONS=2', '-s', 'MINIMAL_RUNTIME=1', '-s', 'MAXIMUM_MEMORY=4GB'])

  @no_firefox('no 4GB support yet')
  @require_v8
  def test_zzz_zzz_2gb_fail(self):
    # TODO Convert to an actual browser test when it reaches stable.
    #      For now, keep this in browser as this suite runs serially, which
    #      means we don't compete for memory with anything else (and run it
    #      at the very very end, to reduce the risk of it OOM-killing the
    #      browser).

    # test that growth doesn't go beyond 2GB without the max being set for that,
    # and that we can catch an allocation failure exception for that
    self.emcc_args += ['-O2', '-s', 'ALLOW_MEMORY_GROWTH', '-s', 'MAXIMUM_MEMORY=2GB']
    self.do_run_in_out_file_test('browser', 'test_2GB_fail.cpp')

  @no_firefox('no 4GB support yet')
  @require_v8
  def test_zzz_zzz_4gb_fail(self):
    # TODO Convert to an actual browser test when it reaches stable.
    #      For now, keep this in browser as this suite runs serially, which
    #      means we don't compete for memory with anything else (and run it
    #      at the very very end, to reduce the risk of it OOM-killing the
    #      browser).

    # test that we properly report an allocation error that would overflow over
    # 4GB.
    self.emcc_args += ['-O2', '-s', 'ALLOW_MEMORY_GROWTH', '-s', 'MAXIMUM_MEMORY=4GB', '-s', 'ABORTING_MALLOC=0']
    self.do_run_in_out_file_test('browser', 'test_4GB_fail.cpp')

  @disabled("only run this manually, to test for race conditions")
  @parameterized({
    'normal': ([],),
    'assertions': (['-s', 'ASSERTIONS'],)
  })
  @requires_threads
  def test_manual_pthread_proxy_hammer(self, args):
    # the specific symptom of the hang that was fixed is that the test hangs
    # at some point, using 0% CPU. often that occured in 0-200 iterations, but
    # you may want to adjust "ITERATIONS".
    self.btest(test_file('pthread/test_pthread_proxy_hammer.cpp'),
               expected='0',
               args=['-s', 'USE_PTHREADS', '-O2', '-s', 'PROXY_TO_PTHREAD',
                     '-DITERATIONS=1024', '-g1'] + args,
               timeout=10000,
               # don't run this with the default extra_tries value, as this is
               # *meant* to notice something random, a race condition.
               extra_tries=0)

  def test_assert_failure(self):
    self.btest(test_file('browser/test_assert_failure.c'), 'abort:Assertion failed: false && "this is a test"')

  def test_full_js_library_strict(self):
    self.btest_exit(test_file('hello_world.c'), args=['-sINCLUDE_FULL_LIBRARY', '-sSTRICT_JS'])


EMRUN = path_from_root('emrun')


class emrun(RunnerCore):
  def test_emrun_info(self):
    if not has_browser():
      self.skipTest('need a browser')
    result = self.run_process([EMRUN, '--system_info', '--browser_info'], stdout=PIPE).stdout
    assert 'CPU' in result
    assert 'Browser' in result
    assert 'Traceback' not in result

    result = self.run_process([EMRUN, '--list_browsers'], stdout=PIPE).stdout
    assert 'Traceback' not in result

  def test_no_browser(self):
    # Test --no_browser mode where we have to take care of launching the browser ourselves
    # and then killing emrun when we are done.
    if not has_browser():
      self.skipTest('need a browser')

    self.run_process([EMCC, test_file('test_emrun.c'), '--emrun', '-o', 'hello_world.html'])
    proc = subprocess.Popen([EMRUN, '--no_browser', '.', '--port=3333'], stdout=PIPE)
    try:
      if EMTEST_BROWSER:
        print('Starting browser')
        browser_cmd = shlex.split(EMTEST_BROWSER)
        browser = subprocess.Popen(browser_cmd + ['http://localhost:3333/hello_world.html'])
        try:
          while True:
            stdout = proc.stdout.read()
            if b'Dumping out file' in stdout:
              break
        finally:
          print('Terminating browser')
          browser.terminate()
          browser.wait()
    finally:
      print('Terminating emrun server')
      proc.terminate()
      proc.wait()

  def test_emrun(self):
    self.run_process([EMCC, test_file('test_emrun.c'), '--emrun', '-o', 'hello_world.html'])
    if not has_browser():
      self.skipTest('need a browser')

    # We cannot run emrun from the temp directory the suite will clean up afterwards, since the
    # browser that is launched will have that directory as startup directory, and the browser will
    # not close as part of the test, pinning down the cwd on Windows and it wouldn't be possible to
    # delete it. Therefore switch away from that directory before launching.

    os.chdir(path_from_root())
    args_base = [EMRUN, '--timeout', '30', '--safe_firefox_profile',
                 '--kill_exit', '--port', '6939', '--verbose',
                 '--log_stdout', self.in_dir('stdout.txt'),
                 '--log_stderr', self.in_dir('stderr.txt')]

    # Verify that trying to pass argument to the page without the `--` separator will
    # generate an actionable error message
    err = self.expect_fail(args_base + ['--foo'])
    self.assertContained('error: unrecognized arguments: --foo', err)
    self.assertContained('remember to add `--` between arguments', err)

    if EMTEST_BROWSER is not None:
      # If EMTEST_BROWSER carried command line arguments to pass to the browser,
      # (e.g. "firefox -profile /path/to/foo") those can't be passed via emrun,
      # so strip them out.
      browser_cmd = shlex.split(EMTEST_BROWSER)
      browser_path = browser_cmd[0]
      args_base += ['--browser', browser_path]
      if len(browser_cmd) > 1:
        browser_args = browser_cmd[1:]
        if 'firefox' in browser_path and ('-profile' in browser_args or '--profile' in browser_args):
          # emrun uses its own -profile, strip it out
          parser = argparse.ArgumentParser(add_help=False) # otherwise it throws with -headless
          parser.add_argument('-profile')
          parser.add_argument('--profile')
          browser_args = parser.parse_known_args(browser_args)[1]
        if browser_args:
          args_base += ['--browser_args', ' ' + ' '.join(browser_args)]

    for args in [
        args_base,
        args_base + ['--private_browsing', '--port', '6941']
    ]:
      args += [self.in_dir('hello_world.html'), '--', '1', '2', '--3']
      print(shared.shlex_join(args))
      proc = self.run_process(args, check=False)
      self.assertEqual(proc.returncode, 100)
      stdout = read_file(self.in_dir('stdout.txt'))
      stderr = read_file(self.in_dir('stderr.txt'))
      self.assertContained('argc: 4', stdout)
      self.assertContained('argv[3]: --3', stdout)
      self.assertContained('hello, world!', stdout)
      self.assertContained('Testing ASCII characters: !"$%&\'()*+,-./:;<=>?@[\\]^_`{|}~', stdout)
      self.assertContained('Testing char sequences: %20%21 &auml;', stdout)
      self.assertContained('hello, error stream!', stderr)
