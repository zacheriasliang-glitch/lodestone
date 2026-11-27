"""Exercise GTK's icon decoder and accessibility service on native Linux."""
import subprocess
import sys
import unittest


@unittest.skipUnless(sys.platform == "linux", "Native GTK runtime prerequisites")
class LinuxGuiPrerequisiteTests(unittest.TestCase):
    def test_gtk_can_decode_its_adwaita_svg_resource(self):
        # Import the installed wx build first, so this exercises the same GTK
        # and pixbuf libraries used by the application, including its loader.
        code = r'''
import ctypes as c
from ctypes.util import find_library
import wx
app = wx.App(False)
class GError(c.Structure):
    _fields_ = [('domain', c.c_uint32), ('code', c.c_int), ('message', c.c_char_p)]
pixbuf = c.CDLL(find_library('gdk_pixbuf-2.0'))
pixbuf.gdk_pixbuf_new_from_resource.argtypes = [c.c_char_p, c.POINTER(c.POINTER(GError))]
pixbuf.gdk_pixbuf_new_from_resource.restype = c.c_void_p
error = c.POINTER(GError)()
image = pixbuf.gdk_pixbuf_new_from_resource(
    b'/org/gtk/libgtk/theme/Adwaita/assets/check-symbolic.svg', c.byref(error))
message = error.contents.message.decode('utf-8', 'replace') if error else ''
if error:
    glib = c.CDLL(find_library('glib-2.0'))
    glib.g_error_free.argtypes = [c.POINTER(GError)]
    glib.g_error_free.restype = None
    glib.g_error_free(error)
assert image, 'GTK could not decode its Adwaita SVG: ' + message
gobject = c.CDLL(find_library('gobject-2.0'))
gobject.g_object_unref.argtypes = [c.c_void_p]
gobject.g_object_unref.restype = None
gobject.g_object_unref(image)
print('GTK SVG decoded')
'''
        result = subprocess.run([sys.executable, "-B", "-c", code],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("GTK SVG decoded", result.stdout)

    def test_accessibility_bus_can_activate_in_the_desktop_session(self):
        result = subprocess.run([
            "gdbus", "call", "--session", "--dest", "org.a11y.Bus",
            "--object-path", "/org/a11y/bus", "--method", "org.a11y.Bus.GetAddress",
        ], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("unix:", result.stdout)


if __name__ == "__main__":
    unittest.main()
