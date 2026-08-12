from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from sys import modules
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase

from util.dispatcher import Dispatcher


class DispatcherTest(TestCase):
	def setUp(self):
		Dispatcher.reset()

	def tearDown(self):
		Dispatcher.reset()
		modules.pop("pbm_demo", None)

	def test_loads_module_with_modern_importlib_api(self):
		with TemporaryDirectory() as temp_dir:
			module_dir = Path(temp_dir, "modules")
			module_dir.mkdir()
			Path(module_dir, "pbm_demo.py").write_text(
				"from util import Mapping\n"
				"def demo(event, bot):\n\tpass\n"
				"mappings = (Mapping(command='demo', function=demo),)\n",
				encoding="utf-8",
			)
			settings = SimpleNamespace(
				botdir=temp_dir,
				debug=False,
				allowmodules=None,
				modules={"pbm_demo"},
				denymodules=set(),
			)

			dispatcher = Dispatcher(settings)

			self.assertIn("pbm_demo", dispatcher.loadedModules)
			self.assertEqual(len(dispatcher._getCommandMappings("demo")), 1)

	def test_all_bundled_modules_import_under_python_3(self):
		module_dir = Path(__file__).resolve().parents[1] / "modules"
		failures = []
		for module_path in sorted(module_dir.glob("*.py")):
			name = module_path.stem
			spec = spec_from_file_location(name, module_path)
			module = module_from_spec(spec)
			modules[name] = module
			try:
				spec.loader.exec_module(module)
			except Exception as exc:
				failures.append("%s: %s" % (name, exc))
			finally:
				modules.pop(name, None)

		self.assertEqual(failures, [])
