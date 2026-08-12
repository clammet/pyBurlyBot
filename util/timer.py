#timer.py

# suggested use would be for an alarm module or somesuch.

from threading import current_thread

from twisted.internet import reactor
from twisted.internet.task import LoopingCall
from twisted.internet.threads import blockingCallFromThread


class TimerExists(Exception):
	pass


class TimerInvalidName(Exception):
	pass


class TimerNotFound(Exception):
	pass


class Timer:
	# reps <= 0 means forever
	def __init__(self, name, interval, f, reps=1, startnow=False, *args, **kwargs):
		self.name = name
		self.f = f
		self.kwargs = kwargs
		self.args = args
		self.reps = reps
		self.interval = interval
		self.lc = LoopingCall(Timers.runTimer, self)
		self.lc.start(interval, startnow)

	def restart(self):
		try:
			self.lc.stop()
		except AssertionError:
			pass
		self.lc.start(self.interval, now=True)


class TimerInfo:
	def __init__(self, timer):
		self.name = timer.name
		self.f = timer.f
		self.kwargs = timer.kwargs
		self.reps = timer.reps
		self.interval = timer.interval


class Timers:
	timers = {}

	@classmethod
	def _addTimer(cls, name, interval, f, reps=1, startnow=False, *args, **kwargs):
		if name in cls.timers:
			raise TimerExists("Timer (%s) already exists." % name)
		else:
			cls.timers[name] = Timer(name, interval, f, reps, startnow, *args, **kwargs)
			return True

	# _timers are for internal use only
	@classmethod
	def addtimer(cls, name, interval, f, reps=1, startnow=False, *args, **kwargs):
		#kinda want to use _ prefix for internal things like DBcommit
		try:
			if name.startswith("_"):
				raise TimerInvalidName("Invalid name (%s)." % name)
		except AttributeError:
			raise TimerInvalidName("Invalid name (%s)." % name)
		else:
			# force interval and rep in to float and int respectivly in case module forgot (I forgot)
			if current_thread().name == 'MainThread':
				return cls._addTimer(name, float(interval), f, int(reps), startnow, *args, **kwargs)
			else:
				return blockingCallFromThread(reactor, cls._addTimer, name, float(interval), f, int(reps), startnow, *args, **kwargs)

	@classmethod
	def _deltimer(cls, name):
		if name in cls.timers:
			#maybe add tryexcept here incase timer already finished
			try: cls.timers[name].lc.stop()
			except AssertionError: pass
			del cls.timers[name]
			return True
		else:
			raise TimerNotFound("Timer (%s) not found." % name)

	@classmethod
	def deltimer(cls, name):
		try:
			if name.startswith("_"):
				raise TimerInvalidName("Invalid name (%s)." % name)
		except AttributeError:
			raise TimerInvalidName("Invalid name (%s)." % name)

		if current_thread().name == 'MainThread':
			return cls._deltimer(name)
		else:
			return blockingCallFromThread(reactor, cls._deltimer, name)

	@classmethod
	def _restarttimer(cls, name):
		if name in cls.timers:
			cls.timers[name].restart()
		else:
			raise TimerNotFound("Timer (%s) not found." % name)


	@classmethod
	def restarttimer(cls, name):
		try:
			if name.startswith("_"):
				raise TimerInvalidName("Invalid name (%s)." % name)
		except AttributeError:
			raise TimerInvalidName("Invalid name (%s)." % name)
		return blockingCallFromThread(reactor, cls._restarttimer, name)

	#run the desired function in a thread but manage the timer in the reactor
	@classmethod
	def runTimer(cls, timerobj):
		reactor.callInThread(timerobj.f, *timerobj.args, **timerobj.kwargs)
		if timerobj.reps > 0:
			timerobj.reps -= 1
			if timerobj.reps == 0:
				cls.timers[timerobj.name].lc.stop()
				del cls.timers[timerobj.name]

	@classmethod
	def _stopall(cls):
		for timername in cls.timers:
			try:
				cls.timers[timername].lc.stop()
			except AssertionError:
				continue

	@classmethod
	def _getTimers(cls):
		d = {}
		for t in cls.timers:
			d[t] = TimerInfo(cls.timers[t])
		return d

	@classmethod
	def getTimers(cls):
		return blockingCallFromThread(reactor, cls._getTimers)

	@classmethod
	def _delPrefix(cls, prefix):
		for timername in list(cls.timers.keys()):
			if timername.startswith(prefix):
				try: cls.timers[timername].lc.stop()
				except AssertionError: pass
				del cls.timers[timername]

	@classmethod
	def delPrefix(cls, prefix):
		return blockingCallFromThread(reactor, cls._delPrefix, prefix)
