import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from judge_system.drawings import create_png_preview


class InstallationTests(unittest.TestCase):
    def test_defaults_are_inside_checkout_from_other_working_directory(self):
        env = {k: v for k, v in os.environ.items() if k not in ('DRAWCAD_ROOT', 'VALIDATOR_ROOT')}
        env['PYTHONPATH'] = str(ROOT / 'src')
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable, '-c', '''
from judge_system.reverse_pipeline import DEFAULT_DRAWCAD_ROOT, DEFAULT_VALIDATOR_ROOT
assert (DEFAULT_DRAWCAD_ROOT / "cad_to_svg.py").is_file()
assert (DEFAULT_VALIDATOR_ROOT / "validator.py").is_file()
'''], cwd=directory, env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_missing_rsvg_returns_actionable_error(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch('judge_system.drawings.subprocess.run', side_effect=FileNotFoundError):
                ok, error = create_png_preview(root / 'in.svg', root / 'out.png')
        self.assertFalse(ok)
        self.assertIn('install librsvg', error)


if __name__ == '__main__':
    unittest.main()
