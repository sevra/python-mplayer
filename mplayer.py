import re, sys, os
from select import select, error
from subprocess import Popen, PIPE
from multiprocessing import Process, Queue, Value, Manager
from collections import namedtuple, deque

import logging
log = logging.getLogger('MPlayer')
if True: log.setLevel(logging.INFO)
else: log.setLevel(logging.NOTSET)

compat = lambda func, arg : func(arg, encoding='utf-8' if sys.version > '3' else '')

PLAYING = 0
PAUSED = 1
STOPPED = 2


class MPlayerNotRunning(Exception): pass


def SelectQueue():
   queue = Queue()
   queue.fileno = lambda : queue._reader.fileno()
   return queue


class IOWorker(Process):
   def __init__(self, stdin, stdout, state, notifier, calls, results):
      self.stdin = stdin
      self.stdout = stdout
      self._state = state
      self.notifier = notifier
      self.calls = calls
      self.results = results
      self.pending = deque()
      self.loading = False
      super(IOWorker, self).__init__()

   @property
   def state(self):
      return self._state.value

   @state.setter
   def state(self, value):
      self._state.value = value
         
   def set_state(self, value):
      if value == self.state: return # make sure redundant events aren't put in the Queue
      log.info('STATE: %s' % ('playing' if value == PLAYING else 'paused' if value == PAUSED else 'stopped'))
      self.state = value

      if value == STOPPED and self.pending:
         # If there is a pending 'get' call and play stops...
         log.info('-------------------- CORNER -------------------')
         self.send_default()

      self.notifier.put(value)

   def convert_result(self, value):
      try:
         return int(value)
      except ValueError:
         try:
            return float(value)
         except ValueError:
            if value == b'(null)':
               return None
            return compat(str, value)

   def parse_result(self, line):
      log.info('MPLAYER: %s' % line)
      value = line.partition(b'=')[-1]
      return self.convert_result(value.strip(b"'\n"))

   def send_result(self, value='blank'):
      log.info('QUEUE: %s' % self.pending)
      default = self.pending.pop()
      if value == 'blank':
         value = default
      log.info('RESPONSE: %s' % value)
      self.results.put(value)

   def send_default(self):
      log.info('SENDING DEFAULT')
      self.send_result()
      
   def run(self):
      while True:
         rfds = [ 
            self.stdout, 
            self.calls 
         ]
      
         try: r, w, e = select(rfds, [], [])
         except (error, IOError): continue
            
         if self.calls in r:
            prefix, cmd, default = self.calls.get()
            call = '%s %s\n' % (prefix, cmd)
            log.info('CALLED: %s' % call.rstrip('\n'))
            self.pending.appendleft(default)
            if cmd.startswith('get'):
               if self.state == STOPPED or self.loading:
                  self.send_default()
               else:
                  self.stdin.write(compat(bytes, call))
            else:
               if cmd.startswith('pause') and self.state in [PAUSED, PLAYING]:
                  self.set_state(PAUSED if self.state == PLAYING else PLAYING)
               elif cmd.startswith('loadfile'):
                  self.loading = True
                  log.info('-------------------- LOADING -------------------')
               self.stdin.write(compat(bytes, call))
               self.send_result(None) # non-get calls always return None
         
         if self.stdout in r:
            line = self.stdout.readline()
            log.info(line)
            if line.startswith(b'Starting play'): # sent when playback has started
               self.set_state(PLAYING)
               self.loading = False
               log.info('-------------------- LOADED -------------------')
            elif line == b'\n': # sent when playback has stopped
               self.set_state(STOPPED)
            elif line.startswith(b'\x1b[A\r\x1b[KPosition'): # sent with calls to 'seek'
               self.stdout.readline() # calls to 'seek' send an extra '\n'
            elif line.startswith(b'ANS'): # sent with 'get_' functions
               self.send_result(self.parse_result(line))


class MPlayer(Popen):
   arg_types = {
      'String':(str, '"%s"'),
      'Float':(float, '%f'),
      'Integer':(int, '%d'),
   }

   @property
   def state(self):
      return self._state.value

   def __init__(self, args=None, path=None):
      self.path = path or 'mplayer' 
      self.args = args or []
      self.get_cmds()
      self.run()
   
   def __getattr__(self, attr):
      if attr in self.cmds:
         return lambda *args, **kargs: self.send_cmd(attr, args, kargs)
      raise AttributeError('MPlayer does not respond to: %s' % attr)

   def run(self):
      Popen.__init__(self, [self.path, '-idle', '-slave', '-quiet']+self.args, stdin=PIPE, stdout=PIPE, stderr=PIPE)
      self.manager = Manager()
      self.defaults = self.manager.dict()
      self._state = Value('i')
      self._state.value = STOPPED
      self.notifier = SelectQueue()
      self.calls = SelectQueue()
      self.results = Queue()
      self.ioworker = IOWorker(self.stdin, self.stdout, self._state, self.notifier, self.calls, self.results)
      self.ioworker.start()

   def kill(self):
      if self.notifier:
         self.notifier.close()
         self.notifier.join_thread()

      if self.results:
         self.results.close()
         self.results.join_thread()

      if self.ioworker:
         self.ioworker.terminate()

      if self.poll() is None:
         self.terminate()

   def restart(self):
      self.kill()
      self.run()

   def get_cmds(self):
      self.cmds = {}
      output = Popen([self.path, '-input', 'cmdlist'], stdout=PIPE)
      for line in output.stdout:
         match = re.search(b'^(\w+)\s+(.*)', line)
         cmd, args = [item.decode('utf-8') for item in match.groups()]
         self.cmds[cmd] = args.split(' ') if args else []

   def process_args(self, cmd, args):
      try:
         for i in range(len(args)):
            arg_type = self.cmds[cmd][i].strip('[]')
            arg_func, arg_mask = self.arg_types[arg_type]
            yield arg_mask % arg_func(args[i])
      except (IndexError, ValueError):
         raise ValueError('%s expects arguments of format %r' % (cmd, ' '.join(str(x) for x in self.cmds[cmd])))

   def send_cmd(self, cmd, args, kargs):
      if self.poll(): raise MPlayerNotRunning('MPlayer instance not running.')
      prefix = kargs.get('prefix', self.defaults.get('prefix', ''))
      default = kargs.get('default', self.defaults.get('default'))
      cmd = '%s %s' % (cmd, ' '.join(arg for arg in self.process_args(cmd, args)))
      self.calls.put([prefix, cmd, default])
      return self.results.get()


class MPlayerAsync(MPlayer):
   pass
