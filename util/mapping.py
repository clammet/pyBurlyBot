def dummyfunc(event, botwrap):
	pass

#TODO: Should this be called "hook"? (with the variable name in modules called "hooks")
#	"hooks" seems kind of "low level" though...
class Mapping:
	def __init__(self, types=None, command=None, regex=None, function=dummyfunc,
				priority=10, override=False, admin=False, hidden=False):
		""" Mapping object to map module functions to IRC events.
		Mapping takes the following arguments:
		type = [list of strings], 
		command = string|[listofcommands], 
		regex = compiledRegExobject,
		function = a defined function should be expecting the following arguments:
			def dummyfunc(event, botwrap):
			command arg can be a list of commands,
		priority = priority for dispatch ordering (Not really useful since module functions are called in
			a thread pool.
		override = If True will override internal bot routines (currently only implemented on sendmsg.)
			If False, internal bot routines will run as well as the event being dispatched. (Default)
		admin = If True, only dispatch when invoked by an admin user
		hidden = If True, do not display in standard command listing, e.g. help
			If False behave normally with regards to listing
		"""
		assert(types is None or isinstance(types, list) or isinstance(types, tuple))
		if not types: self.types = []
		else: self.types = types

		assert(command is None or isinstance(command, (list, tuple, str)))
		if command:
			if isinstance(command, str):
				command = [command]
			if not self.types:
				self.types = ["privmsged"]
		self.command = command
		self.regex = regex
		self.function = function
		self.priority = priority
		self.override = override
		self.admin = admin
		self.hidden = hidden
		
	def __repr__(self):
		return "Mapping(id()=%X, types=%r, command=%r, regex=%r, function=%r, priority=%r, admin=%r)" % \
				(id(self), self.types, self.command, self.regex, self.function, self.priority, self.admin)
