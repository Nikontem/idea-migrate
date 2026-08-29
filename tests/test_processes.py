import unittest

from idea_migrate.processes import running_ides

SAMPLE_PS = """\
/System/Library/CoreServices/loginwindow.app/Contents/MacOS/loginwindow
/Applications/IntelliJ IDEA.app/Contents/MacOS/idea
/Applications/Firefox.app/Contents/MacOS/firefox
/Users/someone/Applications/PyCharm.app/Contents/MacOS/pycharm
/usr/sbin/cfprefsd
"""

TOOLBOX_ONLY = """\
/Applications/JetBrains Toolbox.app/Contents/MacOS/jetbrains-toolbox
/usr/sbin/cfprefsd
"""

NO_IDES = """\
/System/Library/CoreServices/loginwindow.app/Contents/MacOS/loginwindow
/usr/sbin/cfprefsd
"""


class TestRunningIdes(unittest.TestCase):
    def test_detects_running_ides(self):
        self.assertEqual(running_ides(SAMPLE_PS), ["IntelliJ IDEA", "PyCharm"])

    def test_ignores_unrelated_processes(self):
        self.assertEqual(running_ides(NO_IDES), [])

    def test_toolbox_is_not_an_ide(self):
        self.assertEqual(running_ides(TOOLBOX_ONLY), [])

    def test_deduplicates_repeated_processes(self):
        doubled = SAMPLE_PS + "/Applications/IntelliJ IDEA.app/Contents/MacOS/idea\n"
        self.assertEqual(running_ides(doubled), ["IntelliJ IDEA", "PyCharm"])

    def test_detects_toolbox_installed_ide_paths(self):
        toolbox_path = (
            "/Users/someone/Library/Application Support/JetBrains/Toolbox/apps/"
            "goland/GoLand.app/Contents/MacOS/goland\n"
        )
        self.assertEqual(running_ides(toolbox_path), ["GoLand"])

    def test_empty_output_is_safe(self):
        self.assertEqual(running_ides(""), [])


if __name__ == "__main__":
    unittest.main()
