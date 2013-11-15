'''
The :class:`TaskBackend` is at the heart of the
:ref:`task queue application <apps-taskqueue>`. It exposes
all the functionalities for running new tasks, scheduling periodic tasks
and retrieving task information. Pulsar ships with two backends, one which uses
pulsar internals and store tasks in the arbiter domain and another which stores
tasks in redis_.


Overview
===============

The backend is created by the :class:`.TaskQueue`
as soon as it starts. It is then passed to all task queue workers
which, in turns, invoke the :class:`TaskBackend.start` method
to start pulling tasks form the distributed task queue.

Implementation
~~~~~~~~~~~~~~~~~
When creating a new :class:`TaskBackend` there are six methods which must
be implemented:

* The :meth:`~TaskBackend.get_task` method, invoked when retrieving
  a :class:`Task` from the backend server.
* The :meth:`~TaskBackend.get_tasks` method, invoked when retrieving
  a group of :class:`Task` from the backend server.
* The :meth:`~TaskBackend.save_task` method, invoked when creating
  or updating a :class:`Task`.
* The :meth:`~TaskBackend.delete_tasks` method, invoked when deleting
  a bunch of :class:`Task`.
* The :meth:`~TaskBackend.flush` method, invoked flushing a backend (remove
  all tasks and clear the task queue).

.. _task-state:

Task states
~~~~~~~~~~~~~

A :class:`Task` can have one of the following :attr:`Task.status` string:

* ``PENDING`` A task waiting to be queued for execution.
* ``QUEUED`` A task queued but not yet executed.
* ``RETRY`` A task is retrying calculation.
* ``STARTED`` task where execution has started.
* ``REVOKED`` the task execution has been revoked. One possible reason could be
  the task has timed out.
* ``UNKNOWN`` task execution is unknown.
* ``FAILURE`` task execution has finished with failure.
* ``SUCCESS`` task execution has finished with success.

.. _task-run-state:

**FULL_RUN_STATES**

The set of states for which a :class:`Task` has run:
``FAILURE`` and ``SUCCESS``

.. _task-ready-state:

**READY_STATES**

The set of states for which a :class:`Task` has finished:
``REVOKED``, ``FAILURE`` and ``SUCCESS``

.. _tasks-pubsub:

Task status broadcasting
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

A :class:`TaskBackend` broadcast :class:`Task` state into three different
channels via the :attr:`~TaskBackend.pubsub` handler.

API
=========

.. _apps-taskqueue-task:

Task
~~~~~~~~~~~~~

.. autoclass:: Task
   :members:
   :member-order: bysource


TaskBackend
~~~~~~~~~~~~~

.. autoclass:: TaskBackend
   :members:
   :member-order: bysource

TaskConsumer
~~~~~~~~~~~~~~~~~~~

.. autoclass:: TaskConsumer
   :members:
   :member-order: bysource

Scheduler Entry
~~~~~~~~~~~~~~~~~~~

.. autoclass:: SchedulerEntry
   :members:
   :member-order: bysource


Local Backend
==================

.. automodule:: pulsar.apps.tasks.backends.local

Redis Backend
==================

.. automodule:: pulsar.apps.tasks.backends.redis


.. _redis: http://redis.io/
'''
import sys
import logging
import time
from datetime import timedelta
from hashlib import sha1

from pulsar import (in_loop, Failure, EventHandler, PulsarException,
                    Deferred, coroutine_return, run_in_loop_thread,
                    in_loop_thread)
from pulsar.utils.pep import itervalues, pickle, to_string
from pulsar.apps.data import create_store, PubSubClient, odm
from pulsar.utils.log import (LocalMixin, local_property, local_method,
                              lazy_string)
from pulsar.utils.security import gen_unique_id

from .models import JobRegistry
from . import states


__all__ = ['Task', 'TaskBackend', 'TaskNotAvailable',
           'nice_task_message', 'LOGGER']

LOGGER = logging.getLogger('pulsar.tasks')
task_backends = {}


if hasattr(timedelta, "total_seconds"):
    timedelta_seconds = lambda delta: max(delta.total_seconds(), 0)
else:   # pragma    nocover
    def timedelta_seconds(delta):
        if delta.days < 0:
            return 0
        return delta.days * 86400 + delta.seconds + (delta.microseconds / 10e5)


def get_time(expiry, start):
    if isinstance(expiry, timedelta):
        return start + expiry
    else:
        return start + expiry


def format_time(dt):
    if isinstance(dt, (float, int)):
        dt = datetime.fromtimestamp(dt)
    return dt.isoformat() if dt else '?'


def nice_task_message(req, smart_time=None):
    smart_time = smart_time or format_time
    status = req['status'].lower()
    user = req.get('user')
    ti = req.get('time_start', req.get('time_executed'))
    name = '%s (%s) ' % (req['name'], req['id'][:8])
    msg = '%s %s at %s' % (name, status, smart_time(ti))
    return '%s by %s' % (msg, user) if user else msg


class TaskNotAvailable(PulsarException):
    MESSAGE = 'Task {0} is not registered. Check your settings.'

    def __init__(self, task_name):
        self.task_name = task_name
        super(TaskNotAvailable, self).__init__(self.MESSAGE.format(task_name))


class TaskTimeout(PulsarException):
    pass


class TaskConsumer(object):
    '''A context manager for consuming tasks.

    Instances of this consumer are created by the :class:`TaskBackend` when
    a task is executed.

    .. attribute:: task_id

        the :attr:`Task.id` being consumed.

    .. attribute:: job

        the :ref:`Job <apps-taskqueue-job>` which generated the :attr:`task`.

    .. attribute:: worker

        the :class:`.Actor` running the task worker.

    .. attribute:: backend

        Access to the :class:`TaskBackend`. This is useful when creating
        tasks from within a :ref:`job callable <job-callable>`.
    '''
    def __init__(self, backend, worker, task_id, job):
        self.backend = backend
        self.worker = worker
        self.job = job
        self.task_id = task_id


class Task(odm.Model):
    id = odm.CharField(primary_key=True)
    lock_id = odm.CharField(unique=True)
    time_queued = odm.FloatField()
    time_started = odm.FloatField()
    time_finished = odm.FloatField()
    expiry = odm.FloatField()
    status = odm.IntegerField()
    kwargs = odm.PickleField()
    result = odm.PickleField()

    def done(self):
        '''Return ``True`` if the :class:`Task` has finshed.

        Its status is one of :ref:`READY_STATES <task-ready-state>`.
        '''
        return self.get('state') in self.READY_STATES

    def info(self):
        state = states.CODES.get(task.get('state'), 'UNKNOWN')
        return 'task.%s(%s)' % (task.get('name'), task.get('id'))

    def lazy_info(self):
        return lazy_string(self.info)

    def load_kwargs(self):
        kwargs = self.get('kwargs')
        return pickle.loads(kwargs) if kwargs else {}


class TaskClient(PubSubClient):

    def __init__(self, be):
        self._be = be

    def __call__(self, channel, message):
        self._be.events.fire_event(channel, message)


class TaskBackend(LocalMixin):
    '''A backend class for running :class:`.Task`.
    A :class:`TaskBackend` is responsible for creating tasks and put them
    into the distributed queue.
    It also schedules the run of periodic tasks if enabled to do so.

    .. attribute:: task_paths

        List of paths where to upload :ref:`jobs <app-taskqueue-job>` which
        are factory of tasks. Passed by the task-queue application
        :ref:`task paths setting <setting-task_paths>`.

    .. attribute:: schedule_periodic

        `True` if this :class:`TaskBackend` can schedule periodic tasks.

        Passed by the task-queue application
        :ref:`schedule-periodic setting <setting-schedule_periodic>`.

    .. attribute:: backlog

        The maximum number of concurrent tasks running on a task-queue
        for an :class:`.Actor`. A number in the order of 5 to 10 is normally
        used. Passed by the task-queue application
        :ref:`concurrent tasks setting <setting-concurrent_tasks>`.

    .. attribute:: max_tasks

        The maximum number of tasks a worker will process before restarting.
        Passed by the task-queue application
        :ref:`max requests setting <setting-max_requests>`.

    .. attribute:: poll_timeout

        The (asynchronous) timeout for polling tasks from the task queue.

        It is always a positive number and it can be specified via the
        backend connection string::

            local://?poll_timeout=3

        There shouldn't be any reason to modify the default value.

        Default: ``2``.

    .. attribute:: processed

        The number of tasks processed (so far) by the worker running this
        backend.
        This value is important in connection with the :attr:`max_tasks`
        attribute.

    '''
    def __init__(self, store_dns, task_paths=None, schedule_periodic=False,
                 backlog=1, max_tasks=0, name=None, poll_timeout=None):
        self._store_dns = store_dns
        self.name = name
        self.task_paths = task_paths
        self.backlog = backlog
        self.max_tasks = max_tasks
        self.poll_timeout = max(poll_timeout or 0, 2)
        self.processed = 0
        self.local.schedule_periodic = schedule_periodic
        self.next_run = time.time()

    @local_property
    def events(self):
        c = self.channel
        return EventHandler(many_times_events=(c('task_queued'),
                                               c('task_started'),
                                               c('task_done')))

    @property
    def _loop(self):
        '''Eventloop running this task backend'''
        return self.store._loop

    @property
    def schedule_periodic(self):
        return self.local.schedule_periodic

    @property
    def logger(self):
        return self.store._loop.logger

    @local_property
    def store(self):
        '''Data store where tasks are queued'''
        return create_store(self._store_dns)

    @local_property
    def concurrent_tasks(self):
        '''Concurrent set of task ids.

        The task with id in this set are currently being executed
        by the task queue worker running this :class:`TaskBackend`..'''
        return set()

    @property
    def num_concurrent_tasks(self):
        '''The number of :attr:`concurrent_tasks`.'''
        return len(self.concurrent_tasks)

    @local_property
    def entries(self):
        return self._setup_schedule()

    @local_property
    def registry(self):
        '''The :class:`.JobRegistry` for this backend.
        '''
        return JobRegistry.load(self.task_paths)

    @local_property
    def callbacks(self):
        return {}

    def channel(self, name):
        return '%s_%s' % (self.name, name)

    @in_loop
    def queue_task(self, jobname, meta_params=None, expiry=None, **kwargs):
        '''Try to queue a new :ref:`Task`.

        This method returns a :class:`.Deferred` which results in the
        :attr:`Task.id` created. If ``jobname`` is not a valid
        :attr:`.Job.name`, a ``TaskNotAvailable`` exception occurs.

        :param jobname: the name of a :class:`.Job`
            registered with the :class:`.TaskQueue` application.
        :param kwargs: optional dictionary used for the key-valued arguments
            in the task callable.
        :param meta_params: Additional parameters to be passed to the
            :class:`Task` constructor (not its callable function).
        :return: a :class:`.Deferred` resulting in a task id on success.
        '''
        pubsub = self.pubsub()
        if jobname in self.registry:
            job = self.registry[jobname]
            task_id, lock_id = self.generate_task_ids(job, kwargs)
            queued = time.time()
            if expiry is not None:
                expiry = get_time(expiry, queued)
            elif job.timeout:
                expiry = get_time(job.timeout, queued)
            kwargs = pickle.dumps(kwargs, protocol=2)
            meta_params = meta_params or {}
            task = Task(id=task_id, lock_id=lock_id, name=job.name,
                        time_queued=queued, expiry=expiry, kwargs=kwargs,
                        status=states.QUEUED)
            if meta_params:
                task.update(meta_params)
            task = yield self.maybe_queue_task(task)
            if task:
                pubsub.publish(self.channel('task_queued'), task['id'])
                if self.entries and job.name in self.entries:
                    self.entries[job.name].next()
                self.logger.debug('%s', lazy_string(task.info))
            else:
                self.logger.debug('%s cannot queue new task', jobname)
        else:
            raise TaskNotAvailable(jobname)

    def bind_event(self, name, handler):
        self.events.bind_event(self.channel(name), handler)

    def wait_for_task(self, task_id, timeout=None):
        '''Asynchronously wait for a task with ``task_id`` to have finished
        its execution.
        '''
        # make sure pubsub is implemented
        self.pubsub()

        def _():
            task = yield self.get_task(task_id)
            if task:
                if task.done():  # task done, simply return it
                    when_done = self.pop_callback(task.id)
                    if when_done:
                        when_done.callback(task)
                    yield task
                else:
                    callbacks = self.callbacks
                    when_done = callbacks.get(task_id)
                    if not when_done:
                        # No deferred, create one
                        callbacks[task_id] = when_done = Deferred()
                    yield when_done

        return run_in_loop_thread(_(), self._loop).set_timeout(timeout)

    ########################################################################
    ##    ABSTRACT METHODS
    ########################################################################
    def maybe_queue_task(self, task):
        '''Actually queue a ``task``.
        '''
        raise NotImplementedError

    def get_task(self, task_id=None, when_done=False):
        '''Asynchronously retrieve a :class:`Task` from a ``task_id``.

        :param task_id: the ``id`` of the task to retrieve.
        :param when_done: if ``True`` return only when the task is in a
            ready state.
        :return: a :class:`Task` or ``None``.
        '''
        raise NotImplementedError

    def get_tasks(self, ids):
        raise NotImplementedError

    def save_task(self, task_id, **params):
        '''Create or update a :class:`Task` with ``task_id`` and key-valued
        parameters ``params``.
        '''
        raise NotImplementedError

    def pubsub(self):
        '''The publish/subscribe handler.
        '''
        raise NotImplementedError

    ########################################################################
    ##    START/CLOSE METHODS FOR TASK WORKERS
    ########################################################################
    def start(self, worker):
        '''invoked by the task queue ``worker`` when it starts.

        Here, the ``worker`` creates its thread pool via
        :meth:`.Actor.create_thread_pool` and register the
        :meth:`may_pool_task` callback in its event loop.'''
        worker.create_thread_pool()
        self.local.task_poller = worker._loop.call_soon(
            self.may_pool_task, worker)
        worker.logger.debug('started polling tasks')

    def close(self, worker):
        '''Close this :class:`TaskBackend`.

        Invoked by the :class:`.Actor` when stopping.
        '''
        if self.local.task_poller:
            self.local.task_poller.cancel()
            worker.logger.debug('stopped polling tasks')

    ########################################################################
    ##    PRIVATE METHODS
    ########################################################################
    def generate_task_ids(self, job, kwargs):
        '''Generate task unique identifiers.

        :parameter job: The :class:`.Job` creating the task.
        :parameter kwargs: dictionary of key-valued parameters passed to the
            :ref:`job callable <job-callable>` method.
        :return: a two-elements tuple containing the unique id and an
            identifier for overlapping tasks if the :attr:`can_overlap`
            results in ``False``.

        Called by the :ref:`TaskBackend <apps-taskqueue-backend>` when
        creating a new task.
        '''
        can_overlap = job.can_overlap
        if hasattr(can_overlap, '__call__'):
            can_overlap = can_overlap(**kwargs)
        tid = gen_unique_id()[:8]
        if can_overlap:
            return tid, None
        else:
            if kwargs:
                kw = ('%s=%s' % (k, kwargs[k]) for k in sorted(kwargs))
                name = '%s %s' % (self.name, ', '.join(kw))
            else:
                name = self.name
            return tid, sha1(name.encode('utf-8')).hexdigest()

    def tick(self, now=None):
        '''Run a tick, that is one iteration of the scheduler. This
method only works when :attr:`schedule_periodic` is ``True`` and
the arbiter context.

Executes all due tasks and calculate the time in seconds to wait before
running a new :meth:`tick`. For testing purposes a :class:`datetime.datetime`
value ``now`` can be passed.'''
        if not self.schedule_periodic:
            return
        remaining_times = []
        try:
            for entry in itervalues(self.entries):
                is_due, next_time_to_run = entry.is_due(now=now)
                if is_due:
                    self.run_job(entry.name)
                if next_time_to_run:
                    remaining_times.append(next_time_to_run)
        except Exception:
            LOGGER.exception('Unhandled error in task backend')
        self.next_run = now or datetime.now()
        if remaining_times:
            self.next_run += timedelta(seconds=min(remaining_times))

    def job_list(self, jobnames=None):
        registry = self.registry
        jobnames = jobnames or registry
        all = []
        for name in jobnames:
            if name not in registry:
                continue
            job = registry[name]
            can_overlap = job.can_overlap
            if hasattr(can_overlap, '__call__'):
                can_overlap = 'maybe'
            d = {'doc': job.__doc__,
                 'doc_syntax': job.doc_syntax,
                 'type': job.type,
                 'can_overlap': can_overlap}
            if self.entries and name in self.entries:
                entry = self.entries[name]
                _, next_time_to_run = self.next_scheduled((name,))
                run_every = 86400*job.run_every.days + job.run_every.seconds
                d.update({'next_run': next_time_to_run,
                          'run_every': run_every,
                          'runs_count': entry.total_run_count})
            all.append((name, d))
        return all

    def next_scheduled(self, jobnames=None):
        if not self.schedule_periodic:
            return
        if jobnames:
            entries = (self.entries.get(name, None) for name in jobnames)
        else:
            entries = itervalues(self.entries)
        next_entry = None
        next_time = None
        for entry in entries:
            if entry is None:
                continue
            is_due, next_time_to_run = entry.is_due()
            if is_due:
                next_time = 0
                next_entry = entry
                break
            elif next_time_to_run is not None:
                if next_time is None or next_time_to_run < next_time:
                    next_time = next_time_to_run
                    next_entry = entry
        if next_entry:
            return (next_entry.name, max(next_time, 0))
        else:
            return (jobnames, None)

    def may_pool_task(self, worker):
        #Called in the ``worker`` event loop.
        #
        # It pools a new task if possible, and add it to the queue of
        # tasks consumed by the ``worker`` CPU-bound thread.'''
        next_time = 0
        if worker.is_running():
            thread_pool = worker.thread_pool
            if not thread_pool:
                worker.logger.warning('No thread pool, cannot poll tasks.')
            elif self.num_concurrent_tasks < self.backlog:
                if self.max_tasks and self.processed >= self.max_tasks:
                    if not self.num_concurrent_tasks:
                        worker.logger.warning(
                            'Processed %s tasks. Restarting.')
                        worker.stop()
                        coroutine_return()
                else:
                    task = yield self.get_task()
                    if task:    # Got a new task
                        self.processed += 1
                        self.concurrent_tasks.add(task['id'])
                        thread_pool.apply(self._execute_task, worker, task)
            else:
                worker.logger.info('%s concurrent requests. Cannot poll.',
                                   self.num_concurrent_tasks)
                next_time = 1
        worker._loop.call_later(next_time, self.may_pool_task, worker)

    def _execute_task(self, worker, task):
        #Asynchronous execution of a Task. This method is called
        #on a separate thread of execution from the worker event loop thread.
        pubsub = self.pubsub()
        task_id = task['id']
        time_ended = time.time()
        try:
            job = self.registry.get(task.get('name'))
            consumer = TaskConsumer(self, worker, task_id, job)
            if not consumer.job:
                raise RuntimeError('%s not in registry %s' %
                                   (task.lazy_info(), self.registry))
            if task['status'] > states.STARTED:
                if task['expiry'] and time_ended > task['expiry']:
                    raise TaskTimeout
                else:
                    worker.logger.info('starting task %s', task.lazy_info())
                    yield self.save_task(task_id, status=states.STARTED,
                                         time_started=time_ended,
                                         worker=worker.aid)
                    pubsub.publish(self.channel('task_started'), task_id)
                    kwargs = task.load_kwargs()
                    result = yield job(consumer, **kwargs)
                    status = states.SUCCESS
            else:
                worker.logger.error('Invalid status for %s', task.lazy_info())
                self.concurrent_tasks.discard(task_id)
                coroutine_return(task_id)
        except TaskTimeout:
            worker.logger.info('%s timed-out', task.lazy_info())
            result = None
            status = states.REVOKED
        except Exception:
            failure = Failure(sys.exc_info())
            failure.log(msg='Failure in %s' % task.info(),
                        log=worker.logger)
            result = str(failure)
            status = states.FAILURE
        #
        time_ended = time.time()
        yield self.save_task(task_id, time_ended=time.time(),
                             status=status, result=result)
        worker.logger.info('Finished task %s', task_id)
        self.concurrent_tasks.discard(task_id)
        pubsub.publish(self.channel('task_done'), task_id)
        coroutine_return(task_id)

    def _setup_schedule(self):
        if not self.local.schedule_periodic:
            return ()
        entries = {}
        for name, task in self.registry.filter_types('periodic'):
            schedule = self._maybe_schedule(task.run_every, task.anchor)
            entries[name] = SchedulerEntry(name, schedule)
        return entries

    def _maybe_schedule(self, s, anchor):
        if not self.local.schedule_periodic:
            return
        if isinstance(s, int):
            s = timedelta(seconds=s)
        if not isinstance(s, timedelta):
            raise ValueError('Schedule %s is not a timedelta' % s)
        return Schedule(s, anchor)

    @in_loop_thread
    def task_done_callback(self, task_id):
        '''Got a task_id from the ``<name>_task_done`` channel.

        Check if a ``callback`` is available in the :attr:`callbacks`
        dictionary. If so fire the callback with the ``task`` instance
        corresponsding to the input ``task_id``.

        If a callback is not available, it must have been fired already.
        '''
        task = yield self.get_task(task_id)
        if task:
            when_done = self.callbacks.pop(task_id, None)
            if when_done:
                when_done.callback(task)


class Schedule(object):

    def __init__(self, run_every=None, anchor=None):
        self.run_every = run_every
        self.anchor = anchor

    def is_due(self, last_run_at, now=None):
        """Returns tuple of two items ``(is_due, next_time_to_run)``,
        where next time to run is in seconds.

        See :meth:`unuk.contrib.tasks.models.PeriodicTask.is_due`
        for more information.
        """
        now = now or datetime.now()
        rem_delta = last_run_at + self.run_every - now
        rem = timedelta_seconds(rem_delta)
        if rem == 0:
            return True, timedelta_seconds(self.run_every)
        return False, rem


class SchedulerEntry(object):
    """A class used as a schedule entry by the :class:`.TaskBackend`."""
    name = None
    '''Task name'''
    schedule = None
    '''The schedule'''
    last_run_at = None
    '''The time and date of when this task was last run.'''
    total_run_count = None
    '''Total number of times this periodic task has been executed by the
    :class:`.TaskBackend`.'''

    def __init__(self, name, schedule, args=(), kwargs={},
                 last_run_at = None, total_run_count=None):
        self.name = name
        self.schedule = schedule
        self.last_run_at = last_run_at or datetime.now()
        self.total_run_count = total_run_count or 0

    def __repr__(self):
        return self.name
    __str__ = __repr__

    @property
    def scheduled_last_run_at(self):
        '''The scheduled last run datetime.

        This is different from :attr:`last_run_at` only when
        :attr:`anchor` is set.
        '''
        last_run_at = self.last_run_at
        anchor = self.anchor
        if last_run_at and anchor:
            run_every = self.run_every
            times = int(timedelta_seconds(last_run_at - anchor)
                        / timedelta_seconds(run_every))
            if times:
                anchor += times*run_every
                while anchor <= last_run_at:
                    anchor += run_every
                while anchor > last_run_at:
                    anchor -= run_every
                self.schedule.anchor = anchor
            return anchor
        else:
            return last_run_at

    @property
    def run_every(self):
        '''tasks run every interval given by this attribute.

        A python :class:`datetime.timedelta` instance.
        '''
        return self.schedule.run_every

    @property
    def anchor(self):
        '''Some periodic :class:`.PeriodicJob` can specify an anchor.'''
        return self.schedule.anchor

    def next(self, now=None):
        """Returns a new instance of the same class, but with
        its date and count fields updated.

        Function called by :class:`.TaskBackend` when this entry
        is due to run.
        """
        now = now or datetime.now()
        self.last_run_at = now or datetime.now()
        self.total_run_count += 1
        return self

    def is_due(self, now=None):
        return self.schedule.is_due(self.scheduled_last_run_at, now=now)


class PulsarTaskBackend(TaskBackend):

    @local_property
    def store_client(self):
        return self.store.client()

    @local_method
    def pubsub(self):
        pubsub = self.store.pubsub()
        pubsub.add_client(TaskClient(self))
        pubsub.subscribe(*tuple(self.events.events))
        self.bind_event('task_done', self.task_done_callback)
        return pubsub

    def maybe_queue_task(self, task):
        free = True
        store = self.store
        c = self.channel
        if task['lock_id']:
            free = yield store.execute('hsetnx', c('locks'),
                                       task['lock_id'], task['id'])
        if free:
            pipe = store.pipeline()
            pipe.hmset(c('task:%s' % task['id']), task)
            pipe.lpush(c('inqueue'), task['id'])
            result = yield pipe.commit()
            coroutine_return(task)
        else:
            coroutine_return()

    def get_task(self, task_id=None, when_done=False):
        store = self.store
        if not task_id:
            inq = self.channel('inqueue')
            ouq = self.channel('outqueue')
            task_id = yield store.execute('brpoplpush', inq, ouq,
                                          self.poll_timeout)
            if not task_id:
                coroutine_return()
        key = self.channel('task:%s' % task_id.decode('utf-8'))
        task = yield store.execute('hgetall', key, factory=self._build_task)
        coroutine_return(task or None)

    def get_tasks(self, ids):
        pipe = self.store.pipeline()
        c = self.channel
        for task_id in ids:
            key = c('task:%s' % to_string(task_id))
            pipe.execute('hgetall', key, factory=self._build_task)
        result = yield pipe.commit()
        coroutine_return(result)

    def save_task(self, task_id, **params):
        client = self.store_client
        return client.hmset(self.channel('task:%s' % task_id), params)

    def _build_task(self, iterable):
        return Task(Task.decode(iterable))


task_backends['pulsar'] = PulsarTaskBackend
task_backends['redis'] = PulsarTaskBackend