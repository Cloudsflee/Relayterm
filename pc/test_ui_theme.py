from __future__ import annotations

import unittest
from unittest import mock

from pc.ui_theme import apply_theme, detect_windows_theme, glyph, icon_font


class _Key:
    def __init__(self, value):
        self.value = value

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class _Registry:
    HKEY_CURRENT_USER = "HKCU"

    def __init__(self, value=None, error=None):
        self.value = value
        self.error = error

    def OpenKey(self, _root, _path):
        if self.error:
            raise self.error
        return _Key(self.value)

    def QueryValueEx(self, key, _name):
        return key.value, 4


class _Style:
    def __init__(self):
        self.used = ""
        self.configured = {}

    def theme_names(self):
        return ("vista", "clam")

    def theme_use(self, name):
        self.used = name

    def configure(self, name, **options):
        self.configured[name] = options


class _Root:
    def __init__(self):
        self.options = []

    def option_add(self, *args):
        self.options.append(args)


class ThemeHelperTest(unittest.TestCase):
    def test_detects_light_and_dark_apps_theme(self):
        self.assertEqual("light", detect_windows_theme(_Registry(1)))
        self.assertEqual("dark", detect_windows_theme(_Registry(0)))

    def test_missing_registry_defaults_to_light(self):
        self.assertEqual("light", detect_windows_theme(_Registry(error=OSError("missing"))))

    def test_sv_ttk_is_used_for_requested_theme(self):
        root = _Root()
        style = _Style()
        module = mock.Mock()
        info = apply_theme(root, "dark", sv_ttk_module=module, style_factory=lambda _root: style)
        self.assertEqual("sv-ttk", info.backend)
        self.assertEqual("dark", info.applied)
        module.set_theme.assert_called_once_with("dark")
        self.assertEqual("", style.used)
        self.assertIn("Treeview", style.configured)

    def test_missing_sv_ttk_falls_back_to_vista(self):
        root = _Root()
        style = _Style()
        info = apply_theme(root, "dark", sv_ttk_module=None, style_factory=lambda _root: style)
        self.assertTrue(info.fallback)
        self.assertEqual("native", info.backend)
        self.assertEqual("vista", info.applied)
        self.assertEqual("vista", style.used)

    def test_icon_font_and_glyphs_have_fallbacks(self):
        with mock.patch("pc.ui_theme.tkfont.families", return_value=("Segoe MDL2 Assets",)):
            self.assertEqual(("Segoe MDL2 Assets", 13), icon_font(object(), 13))
        self.assertGreaterEqual(ord(glyph("add")), 0xE000)


if __name__ == "__main__":
    unittest.main()
