import sys
import os
from time import time
from multiprocessing import current_process
from multiprocessing.queues import Empty
from threading import current_thread


from pulsar import AlreadyCalledError, AlreadyRegistered,\
                   ActorAlreadyStarted,\
                   logerror, LogSelf, LogginMixin
from pulsar.http import get_httplib
from pulsar.utils.py2py3 import iteritems, itervalues, pickle


from .eventloop import IOLoop
from .proxy import ActorProxy, ActorRequest
from .impl import ActorProcess, ActorThread, ActorMonitorImpl


__all__ = ['is_actor',
           'Actor',
           'ActorRequest',
           'Empty']


EMPTY_TUPLE = ()
EMPTY_DICT = {}


def is_actor(obj):
    return isinstance(obj,Actor)


class HttpMixin(object):
    
    @property
    def http(self):
        return get_httplib(self.cfg)
    
    


class ActorMetaClass(type):
    
    def __new__(cls, name, bases, attrs):
        make = super(ActorMetaClass, cls).__new__
        fprefix = 'actor_'
        attrib  = '{0}functions'.format(fprefix)
        cont = {}
        for key, method in attrs.items():
            if hasattr(method,'__call__') and key.startswith(fprefix):
                meth_name = key[len(fprefix):]
                ack = getattr(method,'ack',True)
                cont[meth_name] = ack
            for base in bases[::-1]:
                if hasattr(base, attrib):
                    rbase = getattr(base,attrib)
                    for key,method in rbase.items():
                        if not key in cont:
                            cont[key] = method
                        
        attrs[attrib] = cont
        return make(cls, name, bases, attrs)

    
ActorBase = ActorMetaClass('BaseActor',(object,),{})


class Actor(ActorBase,LogginMixin,HttpMixin):
    '''A python implementation of the Actor primitive. In computer science,
the Actor model is a mathematical model of concurrent computation that treats
``actors`` as the universal primitives of concurrent digital computation:
in response to a message that it receives, an actor can make local decisions,
create more actors, send more messages, and determine how to respond to
the next message received.

Here is an actor::

    >>> from pulsar import Actor, spawn
    >>> a = spawn(Actor)
    >>> a.is_alive()
    True
'''
    INITIAL = 0X0
    RUN = 0x1
    CLOSE = 0x2
    TERMINATE = 0x3
    status = {0x0:'not started',
              0x1:'started',
              0x2:'closed',
              0x3:'terminated'}
    INBOX_TIMEOUT = 0.02
    DEFAULT_IMPLEMENTATION = 'process'
    MINIMUM_ACTOR_TIMEOUT = 1
    DEFAULT_ACTOR_TIMEOUT = 30
    ACTOR_TIMEOUT_TOLERANCE = 0.2
    _stopping = False
    _ppid = None
    _name = None
    _runner_impl = {'monitor':ActorMonitorImpl,
                    'thread':ActorThread,
                    'process':ActorProcess}
    
    def __init__(self,impl,*args,**kwargs):
        self._impl = impl.impl
        self._aid = impl.aid
        self._inbox = impl.inbox
        self._timeout = impl.timeout
        self._init(impl,*args,**kwargs)
        
    @property
    def proxy(self):
        return ActorProxy(self)
    
    @property
    def aid(self):
        '''Actor unique identifier'''
        return self._aid
    
    @property
    def ppid(self):
        '''Parent process id.'''
        return self._ppid
    
    @property
    def impl(self):
        '''Actor concurrency implementation ("thread", "process" or "greenlet").'''
        return self._impl
    
    @property
    def timeout(self):
        '''Timeout in seconds. If ``0`` the actor has no timeout, otherwise
it will be stopped if it fails to notify itself for a period longer that timeout.'''
        return self._timeout
    
    @property
    def pid(self):
        '''Operative system process ID where the actor is running.'''
        return os.getpid()
    
    @property
    def tid(self):
        '''Operative system process thread name where the actor is running.'''
        return self.current_thread().name
    
    @property
    def name(self):
        'Actor unique name'
        if self._name:
            return self._name
        else:
            return '{0}({1})'.format(self.class_code,self.aid[:8])
    
    @property
    def inbox(self):
        '''Message inbox'''
        return self._inbox
    
    def __reduce__(self):
        raise pickle.PicklingError('{0} - Cannot pickle Actor instances'.format(self))
    
    # HOOKS
    
    def on_start(self):
        '''Callback when the actor starts (after forking).'''
        pass
    
    def on_task(self):
        '''Callback executed at each actor event loop.'''
        pass
    
    def on_stop(self):
        '''Callback executed before stopping the actor.'''
        pass
    
    def on_exit(self):
        '''Called just before the actor is exting.'''
        pass
    
    def on_manage_actor(self, actor):
        pass
    
    def is_alive(self):
        '''``True`` if actor is running.'''
        return self._state == self.RUN
    
    def started(self):
        '''``True`` if actor has started.'''
        return self._state >= self.RUN
    
    def closed(self):
        '''``True`` if actor has exited in an clean fashion.'''
        return self._state == self.CLOSE
    
    def stopped(self):
        '''``True`` if actor has exited.'''
        return self._state >= self.CLOSE
    
    # INITIALIZATION AFTER FORKING
    def _init(self, impl, arbiter = None, monitor = None,
              on_task = None, task_queue = None,
              actor_links = None, name = None):
        self.arbiter = arbiter
        self.monitor = monitor
        self.actor_links = actor_links
        self.loglevel = impl.loglevel
        self._name = name
        self._state = self.INITIAL
        self.log = self.getLogger()
        self._linked_actors = {}
        self.task_queue = task_queue
        self.ioloop = self._get_eventloop(impl)
        self.ioloop.add_loop_task(self)
        if on_task:
            self.on_task = on_task
    
    def start(self):
        if self._state == self.INITIAL:
            if self.isprocess():
                self.configure_logging()
            self.on_start()
            self.log.info('Booting "{0}"'.format(self.name))
            self._state = self.RUN
            self._run()
            return self
    
    def _get_eventloop(self, impl):
        ioimpl = impl.get_ioimpl()
        return IOLoop(impl = ioimpl, logger = LogSelf(self,self.log))
    
    def link(self, actor):
        self._linked_actors[actor.aid] = LinkedActor(actor)
    
    # STOPPING TERMINATIONG AND STARTING
    
    def stop(self):
        # This may be called on a different process domain.
        # In that case there is no ioloop and therefore skip altogether
        if hasattr(self,'ioloop'):
            if self.is_alive() and not self._stopping:
                self._stopping = True
                if not self.on_stop():
                    self._stop_ioloop().add_callback(lambda r : self._stop())
        
    def _stop(self):
        '''Callback after the event loop has stopped.'''
        if self._stopping:
            self.on_exit()
            self._state = self.CLOSE
            self.ioloop.remove_loop_task(self)
            if self.impl != 'monitor':
                self.proxy.on_actor_exit(self.arbiter)
            self._stopping = False
            self._inbox.close()
        
    def terminate(self):
        self.stop()
        
    def shut_down(self):
        '''Called by ``self`` to shut down the arbiter'''
        if self.arbiter:
            self.proxy.stop(self.arbiter)
            
    # LOW LEVEL API
    def _stop_ioloop(self):
        return self.ioloop.stop()
        
    def _run(self):
        '''The run implementation which must be done by a derived class.'''
        try:
            self.ioloop.start()
        except SystemExit:
            raise
        except Exception as e:
            self.log.exception("Exception in worker {0}: {1}".format(self,e))
        finally:
            self.log.info("exiting {0}".format(self))
            self._stop()
    
    def linked_actors(self):
        '''Iterator over linked-actor proxies'''
        return itervalues(self._linked_actors)
    
    @logerror
    def flush(self, closing = False):
        '''Flush one message from the inbox and runs callbacks.
This function should live on a event loop.'''
        inbox = self._inbox
        timeout = self.INBOX_TIMEOUT
        while True:
            request = None
            try:
                request = inbox.get(timeout = timeout)
            except Empty:
                break
            except IOError:
                break
            if request:
                try:
                    actor = self.get_actor(request.aid)
                    if not actor and not closing:
                        self.log.info('Message from an un-linked actor')
                    else:
                        self.handle_request_from_actor(actor,request)
                    if not closing:
                        break
                except Exception as e:
                    #self.handle_request_error(request,e)
                    if self.log:
                        self.log.error('Error while processing worker request: {0}'.format(e),
                                        exc_info=sys.exc_info())
                        
    def get_actor(self, aid):
        if aid == self.aid:
            return self.proxy
        elif aid in self._linked_actors:
            return self._linked_actors[aid]
        elif self.arbiter and aid == self.arbiter.aid:
            return self.arbiter
        elif self.monitor and aid == self.monitor.aid:
            return self.monitor
    
    def handle_request_from_actor(self, caller, request):
        func = getattr(self,'actor_{0}'.format(request.name),None)
        if func:
            ack = getattr(func,'ack',True)
            args = request.msg[0]
            kwargs = request.msg[1]
            result = func(caller, *args, **kwargs)
            if ack:
                #self.log.debug('Sending callback {0}'.format(request.rid))
                self.proxy.callback(caller,request.rid,result)
    
    def __call__(self):
        '''Called in the main eventloop, It flush the inbox queue and notified linked actors'''
        self.flush()
        # If this is not a monitor, we notify to the arbiter we are still alive
        if self.arbiter and self.impl != 'monitor':
            nt = time()
            if hasattr(self,'last_notified'):
                if not self.timeout:
                    tole = self.DEFAULT_ACTOR_TIMEOUT
                else:
                    tole = self.ACTOR_TIMEOUT_TOLERANCE*self.timeout
                if nt - self.last_notified < tole:
                    nt = None
            if nt:
                self.last_notified = nt
                self.proxy.notify(self.arbiter,nt)
        #notify = self.arbiter.notify
        #for actor in self.linked_actors():
        #    actor.notify(self,)
        #   notify(actor.aid,self.aid,time.time())
        if not self._stopping:
            self.on_task()
    
    def current_thread(self):
        '''Return the current thread'''
        return current_thread()
    
    def current_process(self):
        return current_process()
    
    def isprocess(self):
        return self.impl == 'process'
    
    def info(self):
        return {'aid':self.aid,
                'pid':self.pid,
                'ppid':self.ppid,
                'thread':self.current_thread().name,
                'process':self.current_process().name,
                'isprocess':self.isprocess()}
        
    def configure_logging(self):
        if not self.loglevel:
            if self.arbiter:
                self.loglevel = self.arbiter.loglevel
        super(Actor,self).configure_logging()
        
    # BUILT IN ACTOR FUNCTIONS
    
    def actor_callback(self, caller, rid, result):
        #self.log.debug('Received Callaback {0}'.format(rid))
        ActorRequest.actor_callback(rid,result)
    actor_callback.ack = False
    
    def actor_stop(self, caller):
        self.stop()
    actor_stop.ack = False
    
    def actor_notify(self, caller, t):
        '''An actor notified itself'''
        caller.notified = t
    actor_notify.ack = False
    
    def actor_on_actor_exit(self, caller, reason = None):
        self._linked_actors.pop(caller.aid)
    actor_on_actor_exit.ack = False
    
    def actor_info(self, caller):
        '''Get server Info and send it back.'''
        return self.info()
    
    def actor_ping(self, caller):
        return 'pong'


    # CLASS METHODS
    
    @classmethod
    def modify_arbiter_loop(cls, wp):
        '''Called by an instance of :class:`pulsar.WorkerPool`, it modify the 
event loop of the arbiter if required.

:parameter wp: Instance of :class:`pulsar.WorkerPool`
:parameter ioloop: Arbiter event loop
'''
        pass
    
    @classmethod
    def clean_arbiter_loop(cls, wp):
        pass

    @classmethod
    def get_task_queue(cls, monitor):
        return None
