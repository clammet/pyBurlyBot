from typing import Any
from contextlib import contextmanager
from importlib import import_module, invalidate_caches
from pathlib import Path
import pickle
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase

from util.dispatcher import Dispatcher
from util.moduleloader import ModuleRegistry


class Addons:
	def __init__(self) -> None:
		self.values: dict[Any, tuple[Any, Any]] = {}

	def _add(self, name: Any, module: Any, value: Any) -> None:
		self.values[name] = (module, value)


@contextmanager
def plugin_package(files: Any) -> Any:
	with TemporaryDirectory() as temp_dir:
		package = "test_plugins"
		package_dir = Path(temp_dir, package)
		package_dir.mkdir()
		Path(package_dir, "__init__.py").write_text("", encoding="utf-8")
		for name, source in files.items():
			Path(package_dir, "%s.py" % name).write_text(source, encoding="utf-8")

		sys.path.insert(0, temp_dir)
		invalidate_caches()
		try:
			yield package, package_dir
		finally:
			for module_name in tuple(sys.modules):
				if module_name == package or module_name.startswith(package + "."):
					sys.modules.pop(module_name, None)
			sys.path.remove(temp_dir)
			invalidate_caches()


def make_settings(module_names: Any, *, allowmodules: Any=None, serverlabel: Any="test-server") -> Any:
	defaults = {}

	def get_option(option: Any, *, module: Any=None, default: Any=None, **kwargs: Any) -> Any:
		defaults[(module, option)] = default
		return default

	settings = SimpleNamespace(
		serverlabel=serverlabel,
		debug=False,
		allowmodules=set() if allowmodules is None else set(allowmodules),
		modules=tuple(module_names),
		denymodules=set(),
		addons=Addons(),
		container=SimpleNamespace(network=serverlabel),
		getOption=get_option,
	)
	settings.defaults = defaults
	return settings


def load_dispatcher(settings: Any, registry: Any) -> Any:
	dispatcher = Dispatcher(settings, registry)
	settings.dispatcher = dispatcher
	dispatcher.reload()
	return dispatcher


class DispatcherTest(TestCase):
	def test_loads_package_module_and_sorts_mappings_once(self) -> None:
		files = {
			"demo": (
				"from util import Mapping\n"
				"def demo(event, bot):\n\tpass\n"
				"mappings = (\n"
				"    Mapping(command='demo', function=demo, priority=20),\n"
				"    Mapping(command='demo', function=demo, priority=5),\n"
				")\n"
			),
		}
		with plugin_package(files) as (package, _):
			registry = ModuleRegistry(package)
			dispatcher = load_dispatcher(make_settings(("demo",)), registry)

			self.assertEqual(dispatcher.get_module("demo").__name__, "%s.demo" % package)
			self.assertEqual(
				[mapping.priority for mapping in dispatcher._getCommandMappings("demo")],
				[5, 20],
			)

	def test_bundled_plugins_do_not_pollute_top_level_names(self) -> None:
		stdlib_modules = {name: import_module(name) for name in ("random", "time", "wikipedia")}
		registry = ModuleRegistry()
		module_dir = Path(__file__).resolve().parents[1] / "pyburlybot_modules"

		for module_path in sorted(module_dir.glob("*.py")):
			if module_path.stem != "__init__":
				registry.import_plugin(module_path.stem)

		self.assertEqual(registry.import_errors, {})
		for name, original in stdlib_modules.items():
			self.assertIs(sys.modules[name], original)
			self.assertEqual(registry.imported[name].__name__, "pyburlybot_modules.%s" % name)

		index_process = registry.imported["logindexsearch"].IndexProcess
		serialized_class = pickle.dumps(index_process)
		registry.reset()
		restored_class = pickle.loads(serialized_class)
		self.assertEqual(restored_class.__module__, "pyburlybot_modules.logindexsearch")
		registry.reset()

	def test_disallowed_requirement_is_not_imported(self) -> None:
		files = {
			"parent": "REQUIRES = ('blocked',)\n",
			"blocked": "raise RuntimeError('must not be imported')\n",
		}
		with plugin_package(files) as (package, _):
			registry = ModuleRegistry(package)
			settings = make_settings(("parent", "blocked"), allowmodules={"parent"})
			dispatcher = load_dispatcher(settings, registry)

			self.assertFalse(dispatcher.is_module_loaded("parent"))
			self.assertNotIn("blocked", registry.imported)
			self.assertNotIn("%s.blocked" % package, sys.modules)
			self.assertIn("not allowed: blocked", registry.activation_errors[settings.serverlabel]["parent"])

	def test_single_string_requirement_is_normalized_and_loaded(self) -> None:
		files = {
			"parent": "REQUIRES = 'dependency'\n",
			"dependency": "VALUE = 42\n",
		}
		with plugin_package(files) as (package, _):
			registry = ModuleRegistry(package)
			dispatcher = load_dispatcher(make_settings(("parent", "dependency")), registry)

			self.assertEqual(set(dispatcher.modules), {"parent", "dependency"})
			self.assertEqual(dispatcher.get_module("dependency").VALUE, 42)

	def test_circular_requirements_report_the_dependency_path(self) -> None:
		files = {
			"first": "REQUIRES = ('second',)\n",
			"second": "REQUIRES = ('first',)\n",
		}
		with plugin_package(files) as (package, _):
			registry = ModuleRegistry(package)
			settings = make_settings(("first", "second"))
			dispatcher = load_dispatcher(settings, registry)

			self.assertEqual(dispatcher.modules, {})
			self.assertIn(
				"first -> second -> first",
				registry.activation_errors[settings.serverlabel]["first"],
			)

	def test_configuration_initialization_and_addons_are_pipeline_stages(self) -> None:
		files = {
			"service": (
				"OPTIONS = {'enabled': (bool, 'Whether enabled', True)}\n"
				"PROVIDES = ('answer',)\n"
				"answer = 42\n"
				"def init(bot):\n\treturn bot.network == 'pipeline-server'\n"
			),
		}
		with plugin_package(files) as (package, _):
			registry = ModuleRegistry(package)
			settings = make_settings(("service",), serverlabel="pipeline-server")
			dispatcher = load_dispatcher(settings, registry)

			self.assertTrue(dispatcher.is_module_loaded("service"))
			self.assertEqual(settings.defaults[("service", "enabled")], True)
			self.assertEqual(settings.addons.values["answer"], ("service", 42))

	def test_activation_is_isolated_per_server(self) -> None:
		with plugin_package({"demo": "VALUE = 42\n"}) as (package, _):
			registry = ModuleRegistry(package)
			first = load_dispatcher(make_settings(("demo",), serverlabel="first"), registry)
			second = load_dispatcher(make_settings((), serverlabel="second"), registry)

			self.assertTrue(first.is_module_loaded("demo"))
			self.assertFalse(second.is_module_loaded("demo"))
			self.assertIn("demo", registry.imported)

	def test_registry_reset_reimports_changed_source(self) -> None:
		with plugin_package({"demo": "VALUE = 1\n"}) as (package, package_dir):
			registry = ModuleRegistry(package)
			settings = make_settings(("demo",))
			first_dispatcher = load_dispatcher(settings, registry)
			first_module = first_dispatcher.get_module("demo")

			Path(package_dir, "demo.py").write_text("VALUE = 200\n", encoding="utf-8")
			registry.reset()
			second_dispatcher = load_dispatcher(settings, registry)
			second_module = second_dispatcher.get_module("demo")

			self.assertIsNot(first_module, second_module)
			self.assertEqual(second_module.VALUE, 200)
