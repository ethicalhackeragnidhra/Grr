#!/usr/bin/env python
"""This is the manager for the various queues."""

import collections
import os
import random
import socket
import time

import logging

from grr import config
from grr.lib import data_store
from grr.lib import rdfvalue
from grr.lib import registry
from grr.lib import stats
from grr.lib import utils
from grr.lib.rdfvalues import client as rdf_client
from grr.lib.rdfvalues import flows as rdf_flows


class Error(Exception):
  """Base class for errors in this module."""


class MoreDataException(Error):
  """Raised when there is more data available."""


def _GetClientIdFromQueue(q):
  """Returns q's client id, if q is a client task queue, otherwise None.

  Args:
    q: rdfvalue.RDFURN

  Returns:
    string or None
  """
  split = q.Split()
  if not split or len(split) < 2:
    return None

  # Normalize to lowercase.
  split = [s.lower() for s in split]

  str_client_id, tasks_marker = split

  if not str_client_id.startswith("c.") or tasks_marker != "tasks":
    return None

  # The "C." prefix should be uppercase.
  str_client_id = "C" + str_client_id[1:]

  return str_client_id


class QueueManager(object):
  """This class manages the representation of the flow within the data store.

  The workflow for client task scheduling is as follows:

  1) Create a bunch of tasks (rdf_flows.GrrMessage()). Tasks must
  be assigned to queues and contain arbitrary values.

  2) Call QueueManager.Schedule(task) to add the tasks to their queues.

  3) In another thread, call QueueManager.QueryAndOwn(queue) to
  obtain a list of tasks leased for a particular time.

  4) If the lease time expires, the tasks automatically become
  available for consumption. When done with the task we can remove it
  from the scheduler using QueueManager.Delete(tasks).

  5) Tasks can be re-leased by calling QueueManager.Schedule(task)
  repeatedly. Each call will extend the lease by the specified amount.

  Important QueueManager's feature is the ability to freeze the timestamp used
  for time-limiting Resolve and Delete queries to the datastore. "with"
  statement should be used to freeze the timestamp, like:
    with queue_manager.QueueManager(token=self.token) as manager:
      ...

  Another option is to use FreezeTimestamp()/UnfreezeTimestamp() methods:
    queue_manager.FreezeTimestamp()
    ...
    queue_manager.UnfreezeTimestamp()
  """

  TASK_PREDICATE_PREFIX = "task:"
  TASK_PREDICATE_TEMPLATE = TASK_PREDICATE_PREFIX + "%s"

  STUCK_PRIORITY = "Flow stuck"

  request_limit = 1000000
  response_limit = 1000000

  notification_shard_counters = {}

  notification_expiry_time = 600

  def __init__(self, store=None, token=None):
    self.token = token
    if store is None:
      store = data_store.DB

    self.data_store = store

    self.request_queue = []
    self.response_queue = []
    self.requests_to_delete = []

    # A queue of client messages to remove. Keys are client ids, values are
    # lists of task ids.
    self.client_messages_to_delete = {}
    self.new_client_messages = []
    self.notifications = {}

    self.prev_frozen_timestamps = []
    self.frozen_timestamp = None

    self.num_notification_shards = config.CONFIG["Worker.queue_shards"]

  def GetNotificationShard(self, queue):
    queue_name = str(queue)
    QueueManager.notification_shard_counters.setdefault(queue_name, 0)
    QueueManager.notification_shard_counters[queue_name] += 1
    notification_shard_index = (QueueManager.notification_shard_counters[
        queue_name] % self.num_notification_shards)
    if notification_shard_index > 0:
      return queue.Add(str(notification_shard_index))
    else:
      return queue

  def GetAllNotificationShards(self, queue):
    result = [queue]
    for i in range(1, self.num_notification_shards):
      result.append(queue.Add(str(i)))
    return result

  def Copy(self):
    """Return a copy of the queue manager.

    Returns:
    Copy of the QueueManager object.
    NOTE: pending writes/deletions are not copied. On the other hand, if the
    original object has a frozen timestamp, a copy will have it as well.
    """
    result = QueueManager(store=self.data_store, token=self.token)
    result.prev_frozen_timestamps = self.prev_frozen_timestamps
    result.frozen_timestamp = self.frozen_timestamp
    return result

  def FreezeTimestamp(self):
    """Freezes the timestamp used for resolve/delete database queries.

    Frozen timestamp is used to consistently limit the datastore resolve and
    delete queries by time range: from 0 to self.frozen_timestamp. This is
    done to avoid possible race conditions, like accidentally deleting
    notifications that were written by another process while we were
    processing requests.
    """
    self.prev_frozen_timestamps.append(self.frozen_timestamp)
    self.frozen_timestamp = rdfvalue.RDFDatetime.Now()

  def UnfreezeTimestamp(self):
    """Unfreezes the timestamp used for resolve/delete database queries."""
    if not self.prev_frozen_timestamps:
      raise RuntimeError("Unbalanced UnfreezeTimestamp call.")
    self.frozen_timestamp = self.prev_frozen_timestamps.pop()

  def __enter__(self):
    """Supports 'with' protocol."""
    self.FreezeTimestamp()
    return self

  def __exit__(self, unused_type, unused_value, unused_traceback):
    """Supports 'with' protocol."""
    # Flush() uses the frozen timestamp so needs to go first.
    self.Flush()
    self.UnfreezeTimestamp()

  def DeQueueClientRequest(self, client_id, task_id):
    """Remove the message from the client queue that this request forms."""
    # Check this request was actually bound for a client.
    if client_id:
      client_id = rdf_client.ClientURN(client_id)

      self.client_messages_to_delete.setdefault(client_id, []).append(task_id)

  def MultiCheckStatus(self, requests):
    """Checks if there is a status message queued for a number of requests."""
    return self.data_store.CheckRequestsForCompletion(
        requests, token=self.token)

  def FetchCompletedRequests(self, session_id, timestamp=None):
    """Fetch all the requests with a status message queued for them."""

    if timestamp is None:
      timestamp = (0, self.frozen_timestamp or rdfvalue.RDFDatetime.Now())

    for request, status in self.data_store.ReadCompletedRequests(
        session_id,
        timestamp=timestamp,
        limit=self.request_limit,
        token=self.token):
      yield request, status

  def FetchCompletedResponses(self, session_id, timestamp=None, limit=10000):
    """Fetch only completed requests and responses up to a limit."""

    if timestamp is None:
      timestamp = (0, self.frozen_timestamp or rdfvalue.RDFDatetime.Now())

    completed_requests = collections.deque(
        self.FetchCompletedRequests(session_id, timestamp=timestamp))

    total_size = 0
    while completed_requests:

      # Size reported in the status messages may be different from actual
      # number of responses read from the database. Example: hunt responses
      # may get deleted from the database and then worker may die before
      # deleting the request. Then status.response_id will be >0, but no
      # responses will be read from the DB.
      projected_total_size = total_size
      request_list = []
      while completed_requests:
        request, status = completed_requests.popleft()

        # Make sure at least one response is fetched.
        request_list.append(request)

        # Quit if there are too many responses.
        projected_total_size += status.response_id
        if projected_total_size > limit:
          break

      for request, responses in self.data_store.ReadResponses(
          request_list, token=self.token):

        yield (request, responses)
        total_size += len(responses)
        if total_size > limit:
          raise MoreDataException()

  def FetchRequestsAndResponses(self, session_id, timestamp=None):
    """Fetches all outstanding requests and responses for this flow.

    We first cache all requests and responses for this flow in memory to
    prevent round trips.

    Args:
      session_id: The session_id to get the requests/responses for.
      timestamp: Tuple (start, end) with a time range. Fetched requests and
                 responses will have timestamp in this range.

    Yields:
      an tuple (request protobufs, list of responses messages) in ascending
      order of request ids.

    Raises:
      MoreDataException: When there is more data available than read by the
                         limited query.
    """
    if timestamp is None:
      timestamp = (0, self.frozen_timestamp or rdfvalue.RDFDatetime.Now())

    num_requests = 0
    for request, responses in self.data_store.ReadRequestsAndResponses(
        session_id,
        timestamp=timestamp,
        request_limit=self.request_limit,
        response_limit=self.response_limit,
        token=self.token):
      yield (request, responses)
      num_requests += 1

    if num_requests >= self.request_limit:
      raise MoreDataException()

  def DeleteRequest(self, request):
    """Deletes the request and all its responses from the flow state queue."""
    self.requests_to_delete.append(request)

    if request and request.HasField("request"):
      self.DeQueueClientRequest(request.client_id, request.request.task_id)

    data_store.DB.DeleteRequest(request, token=self.token)

  def DestroyFlowStates(self, session_id):
    """Deletes all states in this flow and dequeues all client messages."""
    return self.MultiDestroyFlowStates([session_id])

  def MultiDestroyFlowStates(self, session_ids):
    """Deletes all states in multiple flows and dequeues all client messages."""
    deleted_requests = self.data_store.MultiDestroyFlowStates(
        session_ids, request_limit=self.request_limit, token=self.token)

    for request in deleted_requests:
      if request.HasField("request"):
        # Client request dequeueing is cached so we can call it directly.
        self.DeQueueClientRequest(request.client_id, request.request.task_id)

  def Flush(self):
    """Writes the changes in this object to the datastore."""

    self.data_store.StoreRequestsAndResponses(
        new_requests=self.request_queue,
        new_responses=self.response_queue,
        requests_to_delete=self.requests_to_delete,
        token=self.token)

    # We need to make sure that notifications are written after the requests so
    # we flush after writing all requests and only notify afterwards.
    mutation_pool = self.data_store.GetMutationPool(token=self.token)
    with mutation_pool:
      for client_id, messages in self.client_messages_to_delete.iteritems():
        self.Delete(client_id.Queue(), messages, mutation_pool=mutation_pool)

      if self.new_client_messages:
        for timestamp, messages in utils.GroupBy(self.new_client_messages,
                                                 lambda x: x[1]).iteritems():

          self.Schedule(
              [x[0] for x in messages],
              timestamp=timestamp,
              mutation_pool=mutation_pool)

    if self.notifications:
      for notification in self.notifications.itervalues():
        self.NotifyQueue(notification, mutation_pool=mutation_pool)

      mutation_pool.Flush()

    self.request_queue = []
    self.response_queue = []
    self.requests_to_delete = []

    self.client_messages_to_delete = {}
    self.notifications = {}
    self.new_client_messages = []

  def QueueResponse(self, response, timestamp=None):
    """Queues the message on the flow's state."""
    if timestamp is None:
      timestamp = self.frozen_timestamp
    self.response_queue.append((response, timestamp))

  def QueueRequest(self, request_state, timestamp=None):
    if timestamp is None:
      timestamp = self.frozen_timestamp

    self.request_queue.append((request_state, timestamp))

  def QueueClientMessage(self, msg, timestamp=None):
    if timestamp is None:
      timestamp = self.frozen_timestamp

    self.new_client_messages.append((msg, timestamp))

  def QueueNotification(self, notification=None, timestamp=None, **kw):
    """Queues a notification for a flow."""

    if notification is None:
      notification = rdf_flows.GrrNotification(**kw)

    session_id = notification.session_id
    if session_id:
      if timestamp is None:
        timestamp = self.frozen_timestamp

      notification.timestamp = timestamp

      # We must not store more than one notification per session id and
      # timestamp or there will be race conditions. We therefore only keep
      # the one with the highest request number (indicated by last_status).
      # Note that timestamp might be None. In that case we also only want
      # to keep the latest.
      if timestamp is None:
        ts_str = "None"
      else:
        ts_str = int(timestamp)
      key = "%s!%s" % (session_id, ts_str)
      existing = self.notifications.get(key)
      if existing is not None:
        if existing.last_status < notification.last_status:
          self.notifications[key] = notification
      else:
        self.notifications[key] = notification

  def _TaskIdToColumn(self, task_id):
    """Return a predicate representing this task."""
    return self.TASK_PREDICATE_TEMPLATE % ("%08d" % task_id)

  def Delete(self, queue, tasks, mutation_pool=None):
    """Removes the tasks from the queue.

    Note that tasks can already have been removed. It is not an error
    to re-delete an already deleted task.

    Args:
     queue: A queue to clear.
     tasks: A list of tasks to remove. Tasks may be Task() instances
          or integers representing the task_id.
     mutation_pool: An optional MutationPool object to schedule deletions on.
                    If not given, self.data_store is used directly.
    """
    if queue:
      predicates = []
      for task in tasks:
        try:
          task_id = task.task_id
        except AttributeError:
          task_id = int(task)
        predicates.append(self._TaskIdToColumn(task_id))

      if mutation_pool:
        mutation_pool.DeleteAttributes(queue, predicates)
      else:
        self.data_store.DeleteAttributes(
            queue, predicates, token=self.token, sync=False)

  def Schedule(self, tasks, sync=False, timestamp=None, mutation_pool=None):
    """Schedule a set of Task() instances."""
    if timestamp is None:
      timestamp = self.frozen_timestamp

    for queue, queued_tasks in utils.GroupBy(tasks,
                                             lambda x: x.queue).iteritems():
      if queue:

        to_schedule = dict([(self._TaskIdToColumn(task.task_id),
                             [task.SerializeToString()])
                            for task in queued_tasks])

        if mutation_pool:
          mutation_pool.MultiSet(queue, to_schedule, timestamp=timestamp)
        else:
          self.data_store.MultiSet(
              queue,
              to_schedule,
              timestamp=timestamp,
              sync=sync,
              token=self.token)

  def _SortByPriority(self, notifications, queue, output_dict=None):
    """Sort notifications by priority into output_dict."""
    if output_dict is None:
      output_dict = {}

    for notification in notifications:
      priority = notification.priority
      if notification.in_progress:
        priority = self.STUCK_PRIORITY

      output_dict.setdefault(priority, []).append(notification)

    for priority in output_dict:
      stats.STATS.SetGaugeValue(
          "notification_queue_count",
          len(output_dict[priority]),
          fields=[queue.Basename(), str(priority)])
      random.shuffle(output_dict[priority])

    return output_dict

  def GetNotificationsByPriority(self, queue):
    """Retrieves session ids for processing grouped by priority."""
    # Check which sessions have new data.
    # Read all the sessions that have notifications.
    queue_shard = self.GetNotificationShard(queue)
    return self._SortByPriority(
        self._GetUnsortedNotifications(queue_shard).values(), queue)

  def GetNotificationsByPriorityForAllShards(self, queue):
    """Same as GetNotificationsByPriority but for all shards.

    Used by worker_test to cover all shards with a single worker.

    Args:
      queue: usually rdfvalue.RDFURN("aff4:/W")
    Returns:
      dict of notifications objects keyed by priority.
    """
    output_dict = {}
    for queue_shard in self.GetAllNotificationShards(queue):
      self._GetUnsortedNotifications(
          queue_shard, notifications_by_session_id=output_dict)

    return self._SortByPriority(output_dict.values(), queue)

  def GetNotifications(self, queue):
    """Returns all queue notifications sorted by priority."""
    queue_shard = self.GetNotificationShard(queue)
    notifications = self._GetUnsortedNotifications(queue_shard).values()
    notifications.sort(
        key=lambda notification: notification.priority, reverse=True)
    return notifications

  def GetNotificationsForAllShards(self, queue):
    """Returns notifications for all shards of a queue at once.

    Used by test_lib.MockWorker to cover all shards with a single worker.

    Args:
      queue: usually rdfvalue.RDFURN("aff4:/W")
    Returns:
      List of rdf_flows.GrrNotification objects
    """
    notifications_by_session_id = {}
    for queue_shard in self.GetAllNotificationShards(queue):
      self._GetUnsortedNotifications(
          queue_shard, notifications_by_session_id=notifications_by_session_id)

    notifications = notifications_by_session_id.values()
    notifications.sort(
        key=lambda notification: notification.priority, reverse=True)
    return notifications

  def _GetUnsortedNotifications(self,
                                queue_shard,
                                notifications_by_session_id=None):
    """Returns all the available notifications for a queue_shard.

    Args:
      queue_shard: urn of queue shard
      notifications_by_session_id: store notifications in this dict rather than
        creating a new one

    Returns:
      dict of notifications. keys are session ids.
    """
    if notifications_by_session_id is None:
      notifications_by_session_id = {}
    end_time = self.frozen_timestamp or rdfvalue.RDFDatetime.Now()
    for notification in self.data_store.GetNotifications(
        queue_shard, end_time, token=self.token):

      existing = notifications_by_session_id.get(notification.session_id)
      if existing:
        # If we have a notification for this session_id already, we only store
        # the one that was scheduled last.
        if notification.first_queued > existing.first_queued:
          notifications_by_session_id[notification.session_id] = notification
        elif notification.first_queued == existing.first_queued and (
            notification.last_status > existing.last_status):
          # Multiple notifications with the same timestamp should not happen.
          # We can still do the correct thing and use the latest one.
          logging.warn(
              "Notifications with equal first_queued fields detected: %s %s",
              notification, existing)
          notifications_by_session_id[notification.session_id] = notification
      else:
        notifications_by_session_id[notification.session_id] = notification

    return notifications_by_session_id

  def NotifyQueue(self, notification, **kwargs):
    """This signals that there are new messages available in a queue."""
    self._MultiNotifyQueue(notification.session_id.Queue(), [notification],
                           **kwargs)

  def MultiNotifyQueue(self, notifications, mutation_pool=None):
    """This is the same as NotifyQueue but for several session_ids at once.

    Args:
      notifications: A list of notifications.
      mutation_pool: A MutationPool object to schedule Notifications on.

    Raises:
      RuntimeError: An invalid session_id was passed.
    """
    extract_queue = lambda notification: notification.session_id.Queue()
    for queue, notifications in utils.GroupBy(notifications,
                                              extract_queue).iteritems():
      self._MultiNotifyQueue(queue, notifications, mutation_pool=mutation_pool)

  def _MultiNotifyQueue(self, queue, notifications, mutation_pool=None):
    """Does the actual queuing."""
    notification_list = []
    now = rdfvalue.RDFDatetime.Now()
    for notification in notifications:
      if not notification.first_queued:
        notification.first_queued = (self.frozen_timestamp or
                                     rdfvalue.RDFDatetime.Now())
      else:
        diff = now - notification.first_queued
        if diff.seconds >= self.notification_expiry_time:
          # This notification has been around for too long, we drop it.
          logging.debug("Dropping notification: %s", str(notification))
          continue

      notification_list.append(notification)

    mutation_pool.CreateNotifications(
        self.GetNotificationShard(queue), notification_list)

  def DeleteNotification(self, session_id, start=None, end=None):
    self.DeleteNotifications([session_id], start=start, end=end)

  def DeleteNotifications(self, session_ids, start=None, end=None):
    """This deletes the notification when all messages have been processed."""
    if not session_ids:
      return

    for session_id in session_ids:
      if not isinstance(session_id, rdfvalue.SessionID):
        raise RuntimeError(
            "Can only delete notifications for rdfvalue.SessionIDs.")

    if start is None:
      start = 0
    else:
      start = int(start)

    if end is None:
      end = self.frozen_timestamp or rdfvalue.RDFDatetime.Now()

    for queue, ids in utils.GroupBy(
        session_ids, lambda session_id: session_id.Queue()).iteritems():
      queue_shards = self.GetAllNotificationShards(queue)
      self.data_store.DeleteNotifications(
          queue_shards, ids, start, end, token=self.token)

  def Query(self, queue, limit=1, task_id=None):
    """Retrieves tasks from a queue without leasing them.

    This is good for a read only snapshot of the tasks.

    Args:
       queue: The task queue that this task belongs to, usually client.Queue()
              where client is the ClientURN object you want to schedule msgs on.
       limit: Number of values to fetch.
       task_id: If an id is provided we only query for this id.

    Returns:
        A list of Task() objects.
    """
    # This function is usually used for manual testing so we also accept client
    # ids and get the queue from it.
    if isinstance(queue, rdf_client.ClientURN):
      queue = queue.Queue()

    if task_id is None:
      prefix = self.TASK_PREDICATE_PREFIX
    else:
      prefix = utils.SmartStr(task_id)

    all_tasks = []

    for _, serialized, ts in self.data_store.ResolvePrefix(
        queue,
        prefix,
        timestamp=self.data_store.ALL_TIMESTAMPS,
        token=self.token):
      task = rdf_flows.GrrMessage.FromSerializedString(serialized)
      task.eta = ts
      all_tasks.append(task)

    # Sort the tasks in order of priority.
    all_tasks.sort(key=lambda task: task.priority, reverse=True)

    return all_tasks[:limit]

  def DropQueue(self, queue):
    """Deletes a queue - all tasks will be lost."""
    self.data_store.DeleteSubject(queue, token=self.token)

  def QueryAndOwn(self, queue, lease_seconds=10, limit=1):
    """Returns a list of Tasks leased for a certain time.

    Args:
      queue: The queue to query from.
      lease_seconds: The tasks will be leased for this long.
      limit: Number of values to fetch.
    Returns:
        A list of GrrMessage() objects leased.
    """
    user = ""
    if self.token:
      user = self.token.username
    # Do the real work in a transaction
    try:
      lock = self.data_store.LockRetryWrapper(
          queue, lease_time=lease_seconds, token=self.token)
      return self._QueryAndOwn(
          lock.subject, lease_seconds=lease_seconds, limit=limit, user=user)
    except data_store.DBSubjectLockError:
      # This exception just means that we could not obtain the lock on the queue
      # so we just return an empty list, let the worker sleep and come back to
      # fetch more tasks.
      return []
    except data_store.Error as e:
      logging.warning("Datastore exception: %s", e)
      return []

  def _QueryAndOwn(self, subject, lease_seconds=100, limit=1, user=""):
    """Does the real work of self.QueryAndOwn()."""
    tasks = []

    lease = long(lease_seconds * 1e6)

    # Only grab attributes with timestamps in the past.
    delete_attrs = set()
    serialized_tasks_dict = {}
    for predicate, task, timestamp in self.data_store.ResolvePrefix(
        subject,
        self.TASK_PREDICATE_PREFIX,
        timestamp=(0, self.frozen_timestamp or rdfvalue.RDFDatetime.Now()),
        token=self.token):
      task = rdf_flows.GrrMessage.FromSerializedString(task)
      task.eta = timestamp
      task.last_lease = "%s@%s:%d" % (user, socket.gethostname(), os.getpid())
      # Decrement the ttl
      task.task_ttl -= 1
      if task.task_ttl <= 0:
        # Remove the task if ttl is exhausted.
        delete_attrs.add(predicate)
        stats.STATS.IncrementCounter("grr_task_ttl_expired_count")
      else:
        if task.task_ttl != rdf_flows.GrrMessage.max_ttl - 1:
          stats.STATS.IncrementCounter("grr_task_retransmission_count")

        serialized_tasks_dict.setdefault(predicate,
                                         []).append(task.SerializeToString())
        tasks.append(task)
        if len(tasks) >= limit:
          break

    if delete_attrs or serialized_tasks_dict:
      # Update the timestamp on claimed tasks to be in the future and decrement
      # their TTLs, delete tasks with expired ttls.
      self.data_store.MultiSet(
          subject,
          serialized_tasks_dict,
          replace=True,
          timestamp=long(time.time() * 1e6) + lease,
          sync=True,
          to_delete=delete_attrs,
          token=self.token)

    if delete_attrs:
      logging.info("TTL exceeded for %d messages on queue %s",
                   len(delete_attrs), subject)
    return tasks


class WellKnownQueueManager(QueueManager):
  """A flow manager for well known flows."""

  response_limit = 10000

  def FetchRequestsAndResponses(self, *args, **kw):
    raise NotImplementedError("For well known flows use FetchResponses.")

  def DeleteWellKnownFlowResponses(self, session_id, responses):
    """Deletes given responses from the flow state queue."""
    self.data_store.DeleteWellKnownFlowResponses(
        session_id, responses, token=self.token)

  def FetchResponses(self, session_id):
    """Retrieves responses for a well known flow.

    Args:
      session_id: The session_id to get the requests/responses for.

    Yields:
      The retrieved responses.
    """
    timestamp = (0, self.frozen_timestamp or rdfvalue.RDFDatetime.Now())

    for response in self.data_store.FetchResponsesForWellKnownFlow(
        session_id, self.response_limit, timestamp=timestamp, token=self.token):
      yield response


class QueueManagerInit(registry.InitHook):
  """Registers vars used by the QueueManager."""

  def Run(self):
    # Counters used by the QueueManager.
    stats.STATS.RegisterCounterMetric("grr_task_retransmission_count")
    stats.STATS.RegisterCounterMetric("grr_task_ttl_expired_count")
    stats.STATS.RegisterGaugeMetric(
        "notification_queue_count",
        int,
        fields=[("queue_name", str), ("priority", str)])
