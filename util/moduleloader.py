from importlib import import_module, invalidate_caches
from sys import modules as system_modules, stderr
from traceback import format_exc, print_exc


class ModuleRegistry:
	"""Own imported plugins and their per-server activation state."""

	def __init__(self, package="pyburlybot_modules"):
		self.package = package
		self.imported = {}
		self.import_errors = {}
		self.active = {}
		self.activation_errors = {}
		self._activated = set()
		self._unloaded = set()

	def import_plugin(self, name):
		if name in self.imported:
			return self.imported[name]
		if name in self.import_errors:
			return None
		if not isinstance(name, str) or not name.isidentifier():
			self.import_errors[name] = "Invalid module name: %r" % name
			return None

		try:
			module = import_module("%s.%s" % (self.package, name))
		except Exception:
			self.import_errors[name] = format_exc()
			return None

		self.imported[name] = module
		self._unloaded.discard(name)
		return module

	def active_modules(self, server):
		return self.active.setdefault(server, {})

	def clear_server(self, server):
		self.active[server] = {}
		self.activation_errors[server] = {}

	def activate(self, server, name, module):
		self.active_modules(server)[name] = module
		self._activated.add(name)

	def record_activation_error(self, server, name, reason):
		self.activation_errors.setdefault(server, {}).setdefault(name, reason)

	def unload(self):
		for name, module in self.imported.items():
			if name not in self._activated or name in self._unloaded:
				continue
			unload = getattr(module, "unload", None)
			if unload is not None:
				print("UNLOADING (%s)" % name)
				try:
					unload()
				except Exception:
					print("ERROR in unloading %s" % name, file=stderr)
					print_exc()
			self._unloaded.add(name)

	def reset(self):
		"""Unload plugins and remove package children so source is re-imported."""
		self.unload()
		prefix = self.package + "."
		package_module = system_modules.get(self.package)
		for qualified_name in tuple(system_modules):
			if qualified_name.startswith(prefix):
				module = system_modules.pop(qualified_name, None)
				child_name = qualified_name.removeprefix(prefix)
				if package_module is not None and "." not in child_name:
					if getattr(package_module, child_name, None) is module:
						delattr(package_module, child_name)
		invalidate_caches()
		self.imported.clear()
		self.import_errors.clear()
		self.active.clear()
		self.activation_errors.clear()
		self._activated.clear()
		self._unloaded.clear()

	def reload_servers(self, servers):
		self.reset()
		for server in servers:
			server.reload_modules(self)
		self.show_load_errors()

	def show_load_errors(self):
		if self.import_errors:
			print("\nWARNING: MODULE IMPORT(S) FAILED:", file=stderr)
			for module, reason in self.import_errors.items():
				stderr.write("  %s: %s\n" % (module, reason))
			print(file=stderr)

		for server, failures in self.activation_errors.items():
			if not failures:
				continue
			print("\nWARNING: MODULE(S) NOT ACTIVE ON %s:" % server, file=stderr)
			for module, reason in failures.items():
				stderr.write("  %s: %s\n" % (module, reason))
			print(file=stderr)


class ModuleLoadError(Exception):
	pass
