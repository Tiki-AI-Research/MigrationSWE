"""Grader regressions: equivalent routes and formerly accepted violations.

Executed in the verifier image, whose pinned parsers are already installed.
These fixtures are evaluator tests, separate from protected candidate tests.
"""
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('native_validation', Path(__file__).with_name('native-validation.py'))
native = importlib.util.module_from_spec(spec)
spec.loader.exec_module(native)


class NativeContractTests(unittest.TestCase):
    def setUp(self):
        self.java = native.Parser(native.Language(native.tree_sitter_java.language()))
        self.cs = native.Parser(native.Language(native.tree_sitter_c_sharp.language()))

    def csharp(self, text):
        tree = self.cs.parse(text.encode())
        self.assertFalse(tree.root_node.has_error, text)
        return tree

    def test_absolute_path_is_rejected_before_normalization(self):
        template = '''class Shot {
          public void releaseCapture(string uri) {
            GUARD
            var root = ApplicationData.Current.LocalFolder.Path;
            var full = Path.GetFullPath(Path.Combine(root, uri));
            var relative = Path.GetRelativePath(root, full);
            if (relative.StartsWith("..") || Path.IsPathRooted(relative)) return;
            File.Delete(full);
          }
        }'''
        self.assertFalse(native.windows_guard(self.csharp(template.replace('GUARD', ''))))
        self.assertTrue(native.windows_guard(self.csharp(template.replace('GUARD',
            'if (string.IsNullOrEmpty(uri) || Path.IsPathRooted(uri)) return;'))))
        self.assertFalse(native.windows_guard(self.csharp(template.replace('GUARD',
            'if (Path.IsPathRooted(uri) && unrelatedFlag) return;'))))
        self.assertFalse(native.windows_guard(self.csharp(template.replace('GUARD',
            'if (!Path.IsPathRooted(uri)) return;'))))

    def test_flat_filename_helper_is_equivalent(self):
        helpers = self.csharp(r'''class Helpers {
          public static bool IsLocal(string value) {
            return !string.IsNullOrEmpty(value) && value != "." && value != ".."
              && value.IndexOfAny(new[] {'/', '\\', ':'}) < 0 && !Path.IsPathRooted(value);
          }
        }''')
        module = self.csharp('''class Shot {
          public void releaseCapture(string uri) {
            if (!Helpers.IsLocal(uri)) return;
            var full = Path.Combine(ApplicationData.Current.LocalFolder.Path, uri);
            File.Delete(full);
          }
        }''')
        self.assertTrue(native.windows_guard(module, helpers))

    def test_size_bound_must_cover_tmpfile(self):
        bound = 'if (stream.Size > int.MaxValue) { throw new InvalidOperationException(); }'
        bound_via_local = '''if (stream.Size > int.MaxValue) {
          var tooLarge = new InvalidOperationException("capture too large");
          tooLarge.Data["passthrough"] = true;
          throw tooLarge;
        }'''
        wrong_local_type = '''if (stream.Size > int.MaxValue) {
          var tooLarge = new ArgumentException("capture too large");
          throw tooLarge;
        }'''
        tmpfile = 'if (result == "tmpfile") { return file.Path; }'
        def tree(body):
            return self.csharp('class Shot { public string Execute(string result) {' + body + ' return "data"; } }')
        self.assertFalse(native.windows_size_guard(tree(tmpfile + bound)))
        self.assertTrue(native.windows_size_guard(tree(bound + tmpfile)))
        self.assertTrue(native.windows_size_guard(tree(bound_via_local + tmpfile)))
        self.assertFalse(native.windows_size_guard(tree(wrong_local_type + tmpfile)))

    def test_tmpfile_result_must_be_releasable(self):
        def tree(value):
            return self.csharp('''class Shot {
              public string Execute(string result) {
                if (result == "tmpfile") { return VALUE; }
                return "data";
              }
            }'''.replace('VALUE', value))
        self.assertTrue(native.windows_tmpfile_releasable(tree('file.Name')))
        self.assertTrue(native.windows_tmpfile_releasable(tree('Path.GetFileName(file.Path)')))
        self.assertFalse(native.windows_tmpfile_releasable(tree('file.Path')))

    def test_error_constant_does_not_map_a_rejection(self):
        methods = '''
          [ReactMethod("captureRef")]
          public void CaptureRef(int tag, JSValue options, IReactPromise<string> promise) {
            Settle(() => captureRef(tag, options), promise);
          }
          [ReactMethod("captureScreen")]
          public void CaptureScreen(JSValue options, IReactPromise<string> promise) {
            Settle(() => captureScreen(options), promise);
          }
          private void Settle(Func<Task<string>> capture, IReactPromise<string> promise) {
            promise.Reject(new ReactError { Code = ViewShot.ErrorUnableToSnapshot });
          }
        '''
        self.assertTrue(native.windows_error_code(self.csharp('class Shot {' + methods + '}')))
        self.assertFalse(native.windows_error_code(self.csharp('class Shot {' + methods + '}'), code_constant=False))
        self.assertFalse(native.windows_error_code(self.csharp('''class Shot {
          const string ErrorUnableToSnapshot = "E_UNABLE_TO_SNAPSHOT";
          [ReactMethod] public Task<string> captureRef(int tag, JSValue options) { throw new Exception(); }
          [ReactMethod] public Task<string> captureScreen(JSValue options) { throw new Exception(); }
        }''')))

    def test_android_capture_requires_guaranteed_dispatch(self):
        companion = self.java.parse(b'''class ViewShot implements UIManagerListener, Runnable {
          public void run() {
            UIManager manager = UIManagerHelper.getUIManager(context, FABRIC);
            manager.addUIManagerEventListener(this);
          }
          public void didDispatchMountItems(UIManager manager) { execute(manager); }
          public void execute(UIManager manager) {
            manager.removeUIManagerEventListener(this);
            View view = manager.resolveView(tag);
            saveToBase64String(view);
          }
        }''')
        immediate = self.java.parse(b'''class Shot { public void captureRef() {
          ViewShot shot = new ViewShot();
          UIManager m = UIManagerHelper.getUIManager(c, FABRIC);
          UiThreadUtil.runOnUiThread(() -> shot.execute(m));
        } }''')
        self.assertTrue(native.android_fabric_route(immediate, companion))
        for listener_only in [
            'ViewShot shot = new ViewShot(); UiThreadUtil.runOnUiThread(shot);',
            'ViewShot shot = new ViewShot(); UIManager m = UIManagerHelper.getUIManager(c, FABRIC); m.addUIManagerEventListener(shot);',
        ]:
            module = self.java.parse(('class Shot { public void captureRef() {' + listener_only + '} }').encode())
            self.assertFalse(native.android_fabric_route(module, companion))
        mixed = self.java.parse(b'''class Shot { public void captureRef() {
          ViewShot shot = new ViewShot();
          UiThreadUtil.runOnUiThread(() -> {
            UIManager m = UIManagerHelper.getUIManager(c, FABRIC);
            if (BuildConfig.IS_NEW_ARCHITECTURE_ENABLED) {
              m.addUIManagerEventListener(shot);
            } else {
              shot.execute(m);
            }
          });
        } }''')
        self.assertFalse(native.android_fabric_route(mixed, companion))
        legacy = self.java.parse(b'''class Shot { public void captureRef() {
          if (BuildConfig.IS_NEW_ARCHITECTURE_ENABLED) {
            UIManager m = UIManagerHelper.getUIManager(c, FABRIC); m.addUIBlock(shot);
          } else { legacy.addUIBlock(shot); }
        } }''')
        self.assertFalse(native.android_fabric_route(legacy, companion))


if __name__ == '__main__':
    unittest.main()
