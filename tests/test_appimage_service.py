"""Packaged service boundary tests; no real systemd manager or hardware."""
import tempfile
import unittest
from pathlib import Path
from airpods_hr import service_installer as service
from tests.test_service_installer import FakeSystemctl


class AppImageServiceTests(unittest.TestCase):
    def test_source_unit_migrates_without_force_or_login_enablement(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'airpods-hubd.service'
            path.write_text(service.render_unit(Path('/tmp/deleted/bin/python')))
            manager = FakeSystemctl()
            service.install_service(path, Path('/bundled/python'), systemctl=manager,
                                    appimage=Path('/data/Air Pods/100%/app.AppImage'))
            unit = path.read_text()
            self.assertIn('ExecStart="/data/Air Pods/100%%/app.AppImage" --internal-daemon-service', unit)
            self.assertTrue(service.is_project_owned(unit))
            self.assertEqual(manager.operations, ['daemon-reload'])
            self.assertNotIn('/bin/sh', unit)

    def test_foreign_unit_is_never_replaced(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'airpods-hubd.service'
            contents = '[Service]\nExecStart=/foreign\n'
            path.write_text(contents)
            manager = FakeSystemctl()
            with self.assertRaises(service.ForeignUnitError):
                service.install_service(path, Path('/bundled/python'), systemctl=manager,
                                        appimage=Path('/data/app.AppImage'))
            self.assertEqual(path.read_text(), contents)
            self.assertEqual(manager.operations, [])

    def test_internal_option_is_hidden_and_bad_paths_fail_closed(self):
        parser = service.build_parser()
        self.assertNotIn('--appimage', parser._subparsers._group_actions[0].choices['install'].format_help())
        with self.assertRaises(service.ServiceInstallerError):
            service.install_service(Path('/unused'), Path('/bundled/python'),
                                    systemctl=FakeSystemctl(), appimage=Path('/data/a$b'))
