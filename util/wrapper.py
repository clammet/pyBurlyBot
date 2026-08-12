# This seems like a bit of a waste, but it's difficult to implement this in Container
#  because of the reliance on event data.
from twisted.internet import reactor
from twisted.internet.threads import blockingCallFromThread
from twisted.python.failure import Failure

from traceback import format_tb

class BotWrapper:
	def __init__(self, event, botcont):
		self.event = event
		self._botcont = botcont
		
	def __getattr__(self, name):
		if name in self.__dict__: return getattr(self, name)
		return getattr(self._botcont, name)
	
	# I think say should act as like a "reply" sending message back to whatever
	#  send it, be it channel or user
	# TODO: should this prepend event.nick so like "Nick, msg" "Nick: msg"?
	#		saves modules doing it every line. Maybe add a bypass?
	def say(self, msg, **kwargs):
		if self.event.isPM():
			self.sendmsg(self.event.nick, msg, **kwargs)
		else:
			self.sendmsg(self.event.target, msg, **kwargs)
	
	def checkSay(self, msg):
		if self.event.isPM():
			return self.checkSendMsg(self.event.nick, msg)
		else:
			return self.checkSendMsg(self.event.target, msg)
	
	def isadmin(self, module=None, inreactor=False):
		if inreactor:
			return self._isadmin(module)
		else:
			return blockingCallFromThread(reactor, self._isadmin, module)
	
	#_isadmin bypasses the containers get*Option methods so that it
	# only makes 1 call in the reactor and not 2 (in the case of module admin)
	def _isadmin(self, module=None):
		if not self.event.nick: return None
		admins = self._botcont._settings.getOption("admins", inreactor=True)
		if module:
			madmins = None
			try:
				madmins = self._botcont._settings.getOption("admins", module=module, inreactor=True)
			except AttributeError: pass
			if madmins:
				admins.extend(madmins)
		return self.event.nick.lower() in admins
		
	# option getter/setters
	# if channel is None, pass current channel.
	# otherwise duplicated from container
	def getOption(self, opt, channel=None, **kwargs):
		if not self.event.isPM() and channel is None:
			return self._botcont._settings.getOption(opt, channel=self.event.target, **kwargs)
		else:
			return self._botcont._settings.getOption(opt, channel=channel, **kwargs)
	
	def getOptions(self, opts, channel=None, **kwargs):
		if not self.event.isPM() and channel is None:
			return self._botcont._settings.getOptions(opts, channel=self.event.target, **kwargs)
		else:
			return self._botcont._settings.getOptions(opts, channel=channel, **kwargs)
	
	# Default target channel for setOption is to target current channel unless argument of "channel" is False
	def setOption(self, opt, value, channel=None, inreactor=False, **kwargs):
		if not self.event.isPM() and channel is None:
			if inreactor: self._botcont._settings.setOption(opt, value, channel=self.event.target, **kwargs)
			else: blockingCallFromThread(reactor, self._botcont._settings.setOption, opt, value, channel=self.event.target, **kwargs)
		else:
			if inreactor: self._botcont._settings.setOption(opt, value, channel=channel, **kwargs)
			else: blockingCallFromThread(reactor, self._botcont._settings.setOption, opt, value, channel=channel, **kwargs)

	#callback to handle module errors
	def _moduleerr(self, e):
		if isinstance(e, Failure):
			e.cleanFailure()
			e.printTraceback()
			tb = e.getTracebackObject()
			ex = e.value
			if tb:
				# The (hopefully) most 2 important stacks from the traceback.
				# The first 2 are from twisted, the next one is the module stack, probably, and then the next one is whatever the
				# module called.
				self.say("%s: %s. %s" % (type(ex).__name__, ex, "| ".join(format_tb(tb, 5)[-2:]).replace("\n", ". ")))
			else:
				self.say("%s: %s. Don't know where, check log." % (type(ex).__name__, ex))
		else:
			self.say("Error: %s" % str(e))
			print("error:", e)
