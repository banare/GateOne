# -*- coding: utf-8 -*-
#
#       Copyright 2013 Liftoff Software Corporation
#
# For license information see LICENSE.txt

# Meta
__license__ = "AGPLv3 or Proprietary (see LICENSE.txt)"
__author__ = 'Dan McDougall <daniel.mcdougall@liftoffsoftware.com>'

try:
    from concurrent import futures
except ImportError:
    print("ERROR: You're missing the concurrent.futures module.")
    print("To install it:")
    print('\tsudo pip install futures')
    import sys
    sys.exit(1)
import pickle, signal, os, logging
from functools import wraps
from datetime import timedelta
from itertools import count
from functools import partial
from utils import AutoExpireDict, convert_to_timedelta, get_translation
from tornado.ioloop import IOLoop
from tornado.ioloop import PeriodicCallback as PC

# Localization support
_ = get_translation()

# A global to old memoized results (so multiple instances can share)
MEMO = {}
PID = os.getpid() # So we can tell if we're in the parent process or not
ONE_CALLS = {} # Tracks functions in progress for call_one()

def restart_executor(fn):
    """
    A decorator that ensures the executor is started inside the wrapped instance
    of `AsyncRunner`.
    """
    @wraps(fn)
    def wrapper(self, *args, **kwargs):
        if not self.running:
            self.run()
        self.restart_shutdown_timeout()
        return fn(self, *args, **kwargs)
    return wrapper

def safe_call(function, *args, **kwargs):
    """
    If we're not in the main process, sets the default signal handler
    (`signal.SIG_DFL`) for the ``SIGINT`` signal before calling *function*
    using the given *args* and *kwargs*.  Otherwise *function* will just be
    called and returned normally.

    The point being to prevent loads of unnecessary tracebacks from being
    printed to stdout when the user executes a :kbd:`Ctrl-C` on a running
    gateone.py process.

    ..  note::

        This function is only meant to be used to wrap calls made inside of
        `MultiprocessRunner` instances.
    """
    if os.getpid() != PID:
        signal.signal(signal.SIGINT, signal.SIG_DFL)
    return function(*args, **kwargs)

def done_callback(future, callback):
    """
    Adds the given *callback* via ``future.add_done_callback()`` or
    ``io_loop.add_done_callback()`` depending on whether or not the
    IOLoop is currently running.  This allows `AsyncRunner` instances to be
    debugged in an interactive interpreter without having to start up the
    IOLoop.
    """
    io_loop = IOLoop.current()
    if io_loop._running:
        io_loop.add_future(future, callback)
    else:
        future.add_done_callback(callback)

def append_results(results, function, *args, **kwargs):
    """
    Calls *function* with the given *args* and *kwargs* then appends the result
    to *results* (which must be a list).  If we're not in the main process the
    given *function* will be called using `safe_call`.
    """
    if os.getpid() != PID:
        results.append(safe_call(function, *args, **kwargs))
    else:
        results.append(function(*args, **kwargs))

def callback_when_complete(futures, callback):
    """
    Calls *callback* after all *futures* (list) have completed running.
    """
    counter = count(1)
    io_loop = IOLoop.current()
    results = []
    def add_one(f):
        c = counter.next()
        results.append(f.result())
        if c >= len(futures):
            return callback(results)
    for future in futures:
        io_loop.add_future(future, add_one)

def _cleanup_queue(identifier, future=None):
    """
    Deletes `ONE_CALLS[identifier]` if `ONE_CALLS[identifier]['queue']` is
    empty.
    """
    if identifier in ONE_CALLS:
        if not ONE_CALLS[identifier]['queue']:
            del ONE_CALLS[identifier]

def _call_complete(self, identifier, f=None):
    """
    Used by `AsyncRunner.call_one`; removes the given *identifier* from the
    global `ONE_CALLS` dict if there are no more calls remaining.  Otherwise
    the call count will be decremented by one.
    """
    if identifier in ONE_CALLS and ONE_CALLS[identifier]['queue']:
        if ONE_CALLS[identifier]['future'].done():
            # Submit the next function in the queue
            (function, args,
                kwargs, callback) = ONE_CALLS[identifier]['queue'].popleft()
            if not self.running: # Just in case (it happens, actually)
                self.run()
            self.restart_shutdown_timeout()
            future = self.executor.submit(safe_call, function, *args, **kwargs)
            ONE_CALLS[identifier]['future'] = future
            if callback:
                done_callback(future, lambda f: callback(f.result()))
            completed = partial(_cleanup_queue, identifier)
            done_callback(ONE_CALLS[identifier]['future'], completed)
            # Try again when complete
            call_again = partial(_call_complete, self, identifier)
            done_callback(ONE_CALLS[identifier]['future'], call_again)
    else:
        _cleanup_queue(identifier)

def _cache_result(future, args_string):
    """
    Saves *future.result()* in the `MEMO` dict using *args_string* as the key.
    """
    MEMO[args_string] = future.result()

class AsyncRunner(object):
    """
    A base class to execute functions in an asynchronous manner.  Caches results
    so that future calls to the same functions with the same arguments will
    be returned instantly.

    If no calls are made using the `AsyncRunner` instance after 30 seconds
    (default) it will shut down ``self.executor`` and clear the results
    (memoization) cache to save memory.  The executor will restarted
    automatically on-demand as needed.

    The length of time to wait before shutting down can be specified via the
    *timeout* keyword argument::

        >>> runner = AsyncRunner(timeout="2m")

    The *timeout* value may be specified as a `datetime.timedelta` object or
    a string such as, "2m" or "1h" (will be passed to
    :meth:`utils.convert_to_timedelta`)

    The interval in which the cache is checked for expiration can be controlled
    via the *interval* keyword argument::

        >>> runner = AsyncRunner(interval="30s") # This is the default

    Under most circumstances you won't want to bother changing it but if you do
    it takes the same format as *timeout*.
    """
    def __init__(self, **kwargs):
        self.io_loop = IOLoop.current()
        self.executor = None
        self.shutdown_timeout = None
        self.timeout = kwargs.pop('timeout', None)
        if not self.timeout:
            self.timeout = timedelta(seconds=30)
        if not isinstance(self.timeout, timedelta):
            self.timeout = convert_to_timedelta(self.timeout)
        self.interval = kwargs.pop('interval', None)
        if not self.interval:
            self.interval = "30s"
        global MEMO # Use a global so that instances can share the cache
        if not MEMO:
            MEMO = AutoExpireDict(timeout=self.timeout, interval=self.interval)

    def run(self):
        """
        This method must be overridden by subclasses of `AsyncRunner`.  It must
        start (or re-create) ``self.executor`` when called.
        """
        raise NotImplementedError

    @property
    def running(self):
        """
        This property must be overridden by subclasses of `AsyncRunner`.  It
        must return ``True`` if the executor is running, ``False`` if not.
        """
        raise NotImplementedError

    def shutdown(self, wait=True):
        """
        Calls :meth:`self.executor.shutdown(wait)` and removes and waiting
        timeouts.
        """
        if self.shutdown_timeout:
            self.io_loop.remove_timeout(self.shutdown_timeout)
        if self.running:
            logging.debug(_("Shutting down %s" % repr(self)))
            self.executor.shutdown(wait=wait)

    def restart_shutdown_timeout(self):
        """
        Restarts the shutdown timeout that calls ``self.executor.shutdown()``.
        """
        if self.shutdown_timeout:
            self.io_loop.remove_timeout(self.shutdown_timeout)
        self.shutdown_timeout = self.io_loop.add_timeout(
            self.timeout, self.shutdown)

    def __del__(self):
        """
        Shuts down ``self.executor`` and clears the memoization cache.
        """
        global MEMO
        MEMO = {} # Enables garbage collection of the AutoExpireDict
        self.shutdown()

    @restart_executor
    def call(self, function, *args, **kwargs):
        """
        Executes *function* with *args* and *kwargs*.  Calls are automatically
        memoized and recalled from a cache unless ``memoize=False`` is passed as
        a keyword argument.

        If 'callback' is passed as a keyword argument (*kwargs*) it will be
        called with the result when complete.
        """
        string = ""
        callback = kwargs.pop('callback', None)
        memoize = kwargs.pop('memoize', True)
        if memoize:
            if hasattr(function, '__name__'):
                string = function.__name__
            string += pickle.dumps(args, 0) + pickle.dumps(kwargs, 0)
            if string in MEMO:
                f = futures.Future() # Emulate a completed Future()
                if callback:
                    f.set_result(callback(MEMO[string]))
                else:
                    f.set_result(MEMO[string])
                return f
        future = self.executor.submit(safe_call, function, *args, **kwargs)
        if callback:
            done_callback(future, lambda f: callback(f.result()))
        done_callback(future, lambda f: _cache_result(f, string))
        return future

    @restart_executor
    def call_singleton(self, function, identifier, *args, **kwargs):
        """
        Executes *function* if no other function with the given *identifier*
        is already running.  If a function is currently running with the given
        *identifier* the passed *function* will be called when the first
        function is complete.

        In other words, functions called via this method will be executed in
        sequence with each function being called after the first is complete.

        The function will be passed any given *args* and *kwargs* just like
        :meth:`AsyncRunner.call`.

        If 'callback' is passed as a keyword argument (*kwargs*) it will be
        called with the result when complete.
        """
        callback = kwargs.pop('callback', None)
        if identifier in ONE_CALLS:
            ONE_CALLS[identifier]['queue'].append(
                (function, args, kwargs, callback))
        else:
            from collections import deque
            future = self.executor.submit(safe_call, function, *args, **kwargs)
            ONE_CALLS[identifier] = {
                'future': future,
                'queue': deque()
            }
            if callback:
                done_callback(
                    ONE_CALLS[identifier]['future'],
                    lambda f: callback(f.result()))
            completed = partial(_call_complete, self, identifier)
            done_callback(ONE_CALLS[identifier]['future'], completed)
        return ONE_CALLS[identifier]['future']

    @restart_executor
    def map(self, function, *iterables, **kwargs):
        """
        Calls *function* for every item in *iterables* then calls *callback* (
        if provided as a keyword argument via *kwargs*) with a list containing
        the results when complete.  The results list will be in the order in
        which *iterables* was passed to *function* (not random or based on how
        long they took to complete).

        Any additional *kwargs* will be passed to the *function* with each
        iteration of *iterables*.
        """
        callback = kwargs.pop('callback', None)
        futures = []
        for i in iterables:
            futures.append(self.executor.submit(
                safe_call, function, i, **kwargs))
        if callback:
            callback_when_complete(futures, callback)
        return futures

# The stuff below needs to be converted to use the new self-restarting executor
# style of AsyncRunner.  It's a work-in-progress (I know what to do--just not a
# priority at the moment since these functions aren't used yet).

    #@restart_executor
    #def argchain(self, functions, callback=None):
        #"""
        #Like `AsyncRunner.map` but will pass the result of each function
        #in *functions* as the argument to the next function in the chain.  Calls
        #*callback* when the chain of functions has completed executing.
        #Equivalent to::

            #func_list = [func1, func2, func3]
            #for func in func_list:
                #result = func1()
                #result = func2(result)
                #result = func3(result)
                ## ...and so on
            #return result

        #If a function returns a list or tuple that will be passed as the only
        #argument to the next function in the chain but if that fails with a
        #TypeError an attempt will be made at calling the next function by
        #passing the result as *args.  Equivalent to::

            #def foo(a, b):
                #return (a+1, b+1)

            #def bar(x, y):
                #return (x+10, b+10)

            #def baz(m, n):
                #return (m*10, n*10)

            #result = foo(1, 1) # First func is always executed without args
            #for func in (bar, baz):
                #result = bar(*result)
                #result = baz(*result)
            #return result

        #.. note::

            #Uses the `tornado.concurrent.run_on_executor` decorator to work in
            #a non-blocking fashion.
        #"""
        #result = None
        #for i, function in enumerate(functions):
            #if i == 0:
                #result = function()
            #else:
                #try:
                    #result = function(result)
                #except TypeError:
                    ## Try passing the result as args
                    #result = function(*result)
        #if callback:
            #callback(result)
        #return result

    #@restart_executor
    #def multicall(self, functions, callback=None, counter=count(start=1)):
        #"""
        #Calls every function in *functions* and calls *callback* when all
        #functions are complete.  The *functions* will be called in paralell
        #according to the *max_workers* parameter of this class.
        #"""
        #futures = []
        #results = []
        #def gather_results(f):
            #c = counter.__next__()
            #results.append(f.result())
            #if c == len(functions):
                #if callback:
                    #callback(results)
        #for function in functions:
            #future = self.executor.submit(function)
            #futures.append(future)
            #self.io_loop.add_future(future, gather_results)
        #return futures

class ThreadedRunner(AsyncRunner):
    """
    A class that can be used to execute functions in an asynchronous fashion
    using threads.  Useful for long-running functions that aren't CPU bound.
    """
    def __init__(self, max_workers=10, **kwargs):
        super(ThreadedRunner, self).__init__(**kwargs)
        self.max_workers = max_workers

    def run(self):
        logging.debug(
            _("Starting the ThreadedRunner executor with %s worker threads.")
            % self.max_workers)
        self.executor = futures.ThreadPoolExecutor(max_workers=self.max_workers)

    @property
    def running(self):
        if not self.executor:
            return False
        ibrunning = True
        if self.executor._shutdown_thread:
            ibrunning = False
        return ibrunning

class MultiprocessRunner(AsyncRunner):
    """
    A class that can be used to execute functions in an asynchronous fashion
    using multiple processes.  Useful for long-running functions are mostly CPU
    bound or may use a lot of memory.

    .. warn:: Only works when all objects used by the function(s) are picklable!
    """
    # Enforce singleton on the executor since there's only so many cores
    executor_instance = None
    # Keep track of all our instances so we only shut down once:
    running_instances = set()

    # NOTE: Why is there no default for max_workers below?  Because if it is set
    # to `None` concurrent.futures.ProcessPoolExecutor will automatically use an
    # appropriate number of workers using multiprocessing.cpu_count().
    def __init__(self, max_workers=None, **kwargs):
        super(MultiprocessRunner, self).__init__(**kwargs)
        self.max_workers = max_workers

    def run(self):
        cls = MultiprocessRunner
        started = False
        if self not in cls.running_instances:
            cls.running_instances.add(self)
        if not cls.executor_instance:
            self.executor = futures.ProcessPoolExecutor(
                max_workers=self.max_workers)
            cls.executor_instance = self
            started = True
        elif not cls.executor_instance.running:
            cls.executor_instance.executor = futures.ProcessPoolExecutor(
                max_workers=self.max_workers)
            started = True
        self.executor = cls.executor_instance.executor
        if started:
            workers = self.executor._max_workers # Derived from cpu_count()
            logging.debug(
                _("Starting the MultiprocessRunner executor with %s worker "
                "processes.") % workers)

    @property
    def running(self):
        if not self.executor:
            return False
        ibrunning = True
        if self.executor._shutdown_thread:
            ibrunning = False
        return ibrunning

    def shutdown(self, wait=True):
        """
        An override of `AsyncRunner.shutdown` that is aware of the number of
        running instances so we don't shut down the executor while another
        instance is using it (Remember: This class enforces a singleton
        pattern--only one instance of the executor is allowed).

        .. note::

            The executor will only be shut down when this method is called for
            each instance of `MultiprocessRunner` that exists.
        """
        cls = MultiprocessRunner
        if self.shutdown_timeout:
            self.io_loop.remove_timeout(self.shutdown_timeout)
        if self in cls.running_instances:
            cls.running_instances.remove(self)
        if not cls.running_instances:
            if self.running:
                logging.info("Shutting down the MultiprocessRunner executor.")
                self.executor.shutdown(wait=wait)

class PeriodicCallback(object):
    """
    A wrapper that uses either `tornado.ioloop.PeriodicCallback` or
    `threading.Timer` to call functions at a specified interval depending on
    whether or not there's a running instance of `tornado.ioloop.IOLoop`.

    .. note::

        The purpose of this class is primarily for debugging things in an
        interactive Python interpreter.  It is expected that in production
        you will be using a running `~tornado.ioloop.IOLoop`.
    """
    def __init__(self, callback, callback_time, io_loop=None):
        self.callback = callback
        self.callback_time = callback_time
        self.io_loop = io_loop or IOLoop.current()
        if self.io_loop._running:
            # Use a regular PeriodicCallback
            self._pc = PC(callback, callback_time, io_loop)
        else:
            from threading import Timer
            # NOTE: PeriodicCallback uses ms while Timer uses seconds
            def callback_wrapper():
                "Runs the callback and restarts the Timer so it will run again"
                self.callback()
                if self._running:
                    self._pc = Timer(callback_time / 1000, callback_wrapper)
                    self._pc.start()
            self._pc = Timer(callback_time / 1000, callback_wrapper)
        self._running = False

    def start(self):
        """Starts the timer."""
        self._running = True
        self._pc.start() # Timer() and PeriodicCallback() both use start()

    def stop(self):
        """Stops the timer."""
        self._running = False
        if isinstance(self._pc, PC): # PeriodicCallback()
            self._pc.stop()
        else: # Timer()
            self._pc.cancel()
