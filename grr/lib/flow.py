#!/usr/bin/env python
"""This file defines the base classes for Flows.

A Flow is a state machine which executes actions on the
client. Messages are transmitted between the flow object and the
client with their responses introduced into a state handler within the
flow.

The flow can send messages to a client, or launch other child flows. While these
messages are processed, the flow can be suspended indefinitely into the data
store. When replies arrive from the client, or a child flow, the flow is woken
up and the responses are sent to one of the flow state methods.

In order for the flow to be suspended and restored, its state is
stored in a protobuf. Rather than storing the entire flow, the
preserved state is well defined and can be found in the flow's "state"
attribute. Note that this means that any parameters assigned to the
flow object itself are not preserved across state executions - only
parameters specifically stored in the state are preserved.

In order to actually run the flow, a FlowRunner is used. The flow runner is
responsible for queuing messages to clients, launching child flows etc. The
runner stores internal flow management information inside the flow's state, in a
variable called "context". This context should only be used by the runner itself
and not manipulated by the flow.

The flow state is a normal dict (even though only types supported by
the ProtoDict class are supported in the state):

self.state.parameter_name = parameter_name

The following defaults parameters exist in the flow's state:

self.args: The flow's protocol buffer args - an instance of
  self.args_type. If the flow was instantiated using keywords only, a new
  instance of the args is created.

self.context: The flow runner's context.

self.runner_args: The flow runners args. This is an instance of
  FlowRunnerArgs() which may be build from keyword args.

"""


import functools
import operator


import logging

from grr.lib import access_control
from grr.lib import aff4
from grr.lib import data_store
from grr.lib import events
from grr.lib import flow_runner
from grr.lib import grr_collections
from grr.lib import multi_type_collection
from grr.lib import queue_manager
from grr.lib import queues
from grr.lib import rdfvalue
from grr.lib import registry
from grr.lib import sequential_collection
from grr.lib import server_stubs
from grr.lib import stats
from grr.lib import type_info
from grr.lib import utils
from grr.lib.rdfvalues import client as rdf_client
from grr.lib.rdfvalues import flows as rdf_flows
from grr.lib.rdfvalues import protodict as rdf_protodict
from grr.lib.rdfvalues import structs as rdf_structs
from grr.proto import jobs_pb2


class FlowResultCollection(sequential_collection.GrrMessageCollection):
  """Sequential FlowResultCollection."""


class FlowError(Exception):
  """Raised when we can not retrieve the flow."""


class Responses(object):
  """An object encapsulating all the responses to a request.

  This object is normally only instantiated from the flow StateHandler
  decorator.
  """

  def __init__(self, request=None, responses=None, auth_required=True):
    self.status = None  # A GrrStatus rdfvalue object.
    self.success = True
    self.request = request
    self._auth_required = auth_required
    if request:
      self.request_data = rdf_protodict.Dict(request.data)
    self._responses = []
    self._dropped_responses = []

    if responses:
      # This may not be needed if we can assume that responses are
      # returned in lexical order from the data_store.
      responses.sort(key=operator.attrgetter("response_id"))

      # The iterator that was returned as part of these responses. This should
      # be passed back to actions that expect an iterator.
      self.iterator = None

      # Filter the responses by authorized states
      for msg in responses:
        # Check if the message is authenticated correctly.
        if msg.auth_state == msg.AuthorizationState.DESYNCHRONIZED or (
            self._auth_required and
            msg.auth_state != msg.AuthorizationState.AUTHENTICATED):
          logging.warning("%s: Messages must be authenticated (Auth state %s)",
                          msg.session_id, msg.auth_state)
          self._dropped_responses.append(msg)
          # Skip this message - it is invalid
          continue

        # Check for iterators
        if msg.type == msg.Type.ITERATOR:
          self.iterator = rdf_client.Iterator(msg.payload)
          continue

        # Look for a status message
        if msg.type == msg.Type.STATUS:
          # Our status is set to the first status message that we see in
          # the responses. We ignore all other messages after that.
          self.status = rdf_flows.GrrStatus(msg.payload)

          # Check this to see if the call succeeded
          self.success = self.status.status == self.status.ReturnedStatus.OK

          # Ignore all other messages
          break

        # Use this message
        self._responses.append(msg)

      if self.status is None:
        # This is a special case of de-synchronized messages.
        if self._dropped_responses:
          logging.error("De-synchronized messages detected:\n" + "\n".join(
              [utils.SmartUnicode(x) for x in self._dropped_responses]))

        if responses:
          self._LogFlowState(responses)

        raise FlowError("No valid Status message.")

    # This is the raw message accessible while going through the iterator
    self.message = None

  def __iter__(self):
    """An iterator which returns all the responses in order."""
    old_response_id = None
    action_registry = server_stubs.ClientActionStub.classes
    expected_response_classes = []
    is_client_request = False
    # This is the client request so this response packet was sent by a client.
    if self.request.HasField("request"):
      is_client_request = True
      client_action_name = self.request.request.name
      if client_action_name not in action_registry:
        raise RuntimeError(
            "Got unknown client action: %s." % client_action_name)
      expected_response_classes = action_registry[
          client_action_name].out_rdfvalues

    for message in self._responses:
      self.message = rdf_flows.GrrMessage(message)

      # Handle retransmissions
      if self.message.response_id == old_response_id:
        continue

      else:
        old_response_id = self.message.response_id

      if self.message.type == self.message.Type.MESSAGE:
        if is_client_request:
          # Let's do some verification for requests that came from clients.
          if not expected_response_classes:
            raise RuntimeError("Client action %s does not specify out_rdfvalue."
                               % client_action_name)
          else:
            args_rdf_name = self.message.args_rdf_name
            if not args_rdf_name:
              raise RuntimeError("Deprecated message format received: "
                                 "args_rdf_name is None.")
            elif args_rdf_name not in [
                x.__name__ for x in expected_response_classes
            ]:
              raise RuntimeError(
                  "Response type was %s but expected %s for %s." %
                  (args_rdf_name, expected_response_classes,
                   client_action_name))

        yield self.message.payload

  def First(self):
    """A convenience method to return the first response."""
    for x in self:
      return x

  def __len__(self):
    return len(self._responses)

  def __nonzero__(self):
    return bool(self._responses)

  def _LogFlowState(self, responses):
    session_id = responses[0].session_id
    token = access_control.ACLToken(username="GRRWorker", reason="Logging")
    token.supervisor = True

    logging.error("No valid Status message.\nState:\n%s\n%s\n%s",
                  data_store.DB.ResolvePrefix(
                      session_id.Add("state"), "flow:", token=token),
                  data_store.DB.ResolvePrefix(
                      session_id.Add(
                          "state/request:%08X" % responses[0].request_id),
                      "flow:",
                      token=token),
                  data_store.DB.ResolvePrefix(
                      queues.FLOWS, "notify:%s" % session_id, token=token))


class FakeResponses(Responses):
  """An object which emulates the responses.

  This is only used internally to call a state method inline.
  """

  def __init__(self, messages, request_data):
    super(FakeResponses, self).__init__()
    self.success = True
    self._responses = messages or []
    self.request_data = request_data
    self.iterator = None

  def __iter__(self):
    return iter(self._responses)


def StateHandler(auth_required=True):
  """A convenience decorator for state methods.

  Args:
    auth_required: Do we require messages to be authenticated? If the
                message is not authenticated we raise.

  Raises:
    RuntimeError: If a next state is not specified.

  Returns:
    A decorator
  """

  def Decorator(f):
    """Initialised Decorator."""

    @functools.wraps(f)
    def Decorated(self, responses=None, request=None, direct_response=None):
      """A decorator that defines allowed follow up states for a method.

      Args:
        self: The self of the wrapped function.

        responses: The responses for this state.

        request: The request sent out originally.

        direct_response: A final responses object that does not need wrapping
                         again. If given, neither request nor responses is used.

      Returns:
        This calls the state and returns the obtained result.
      """
      if "r" in self.mode:
        pending_termination = self.Get(self.Schema.PENDING_TERMINATION)
        if pending_termination:
          self.Error(pending_termination.reason)
          return

      runner = self.GetRunner()

      if direct_response is not None:
        return f(self, direct_response)

      if not isinstance(responses, Responses):
        # Prepare a responses object for the state method to use:
        responses = Responses(
            request=request, responses=responses, auth_required=auth_required)

      if responses.status:
        runner.SaveResourceUsage(request, responses)

      stats.STATS.IncrementCounter("grr_worker_states_run")

      if f.__name__ == "Start":
        stats.STATS.IncrementCounter("flow_starts", fields=[self.Name()])

      # Run the state method (Allow for flexibility in prototypes)
      args = [self, responses]
      res = f(*args[:f.func_code.co_argcount])

      return res

    return Decorated

  return Decorator


# This is an implementation of an AttributedDict taken from
# http://stackoverflow.com/questions/4984647/accessing-dict-keys-like-an-attribute-in-python
# It works very well but there is a small drawback - there is no way
# to assign an attribute to this dict that does not get serialized. Do
# not inherit from this class, there might be interesting side
# effects.
class AttributedDict(dict):

  def __init__(self, *args, **kwargs):
    super(AttributedDict, self).__init__(*args, **kwargs)
    self.__dict__ = self


class PendingFlowTermination(rdf_structs.RDFProtoStruct):
  """Descriptor of a pending flow termination."""
  protobuf = jobs_pb2.PendingFlowTermination


class EmptyFlowArgs(rdf_structs.RDFProtoStruct):
  """Some flows do not take argumentnts."""
  protobuf = jobs_pb2.EmptyMessage


class Behaviour(object):
  """A Behaviour is a property of a flow.

  Behaviours advertise what kind of flow this is. The flow can only advertise
  predefined behaviours.
  """
  # A constant which defines all the allowed behaviours and their descriptions.
  LEXICON = {}

  def __init__(self, *args):
    self.set = set()
    for arg in args:
      if arg not in self.LEXICON:
        raise ValueError("Behaviour %s not known." % arg)

      self.set.add(str(arg))

  def __add__(self, other):
    other = str(other)

    if other not in self.LEXICON:
      raise ValueError("Behaviour %s not known." % other)

    return self.__class__(other, *list(self.set))

  def __sub__(self, other):
    other = str(other)

    result = self.set.copy()
    result.discard(other)

    return self.__class__(*list(result))

  def __iter__(self):
    return iter(self.set)

  def IsSupported(self, other):
    """Ensure the other Behaviour supports all our Behaviours."""
    if not isinstance(other, self.__class__):
      raise TypeError("Must be called on %s" % self.__class__)

    return self.set.issubset(other.set)


class FlowBehaviour(Behaviour):
  # A constant which defines all the allowed behaviours and their descriptions.
  LEXICON = {
      # What GUI mode should this flow appear in?
      "BASIC": ("Include in the simple UI. This flow is designed "
                "for simpler use."),
      "ADVANCED": ("Include in advanced UI. This flow takes "
                   "more experience to use."),
      "DANGEROUS":
          "This flow may be dangerous. Only available for Admins",
      "DEBUG":
          "This flow only appears in debug mode.",

      # Is this a global flow or a client specific flow?
      "Client Flow":
          "This flow works on a client.",
      "Global Flow":
          "Global flow (this is to be deprecated).",

      # OS Support.
      "OSX":
          "This flow works on OSX operating systems.",
      "Windows":
          "This flow works on Windows operating systems.",
      "Linux":
          "This flow works on Linux operating systems.",
  }


RESULTS_SUFFIX = "Results"
RESULTS_PER_TYPE_SUFFIX = "ResultsPerType"
LOGS_SUFFIX = "Logs"


class FlowBase(aff4.AFF4Volume):
  """The base class for Flows and Hunts."""

  # Alternatively we can specify a single semantic protobuf that will be used to
  # provide the args.
  args_type = EmptyFlowArgs

  def Initialize(self):
    # This will be set to the state. Flows and Hunts can store
    # information in the state object which will be serialized between
    # state executions.
    self.state = None

    # This will be populated with an active runner.
    self.runner = None

    self.args = None

  @classmethod
  def FilterArgsFromSemanticProtobuf(cls, protobuf, kwargs):
    """Assign kwargs to the protobuf, and remove them from the kwargs dict."""
    for descriptor in protobuf.type_infos:
      value = kwargs.pop(descriptor.name, None)
      if value is not None:
        setattr(protobuf, descriptor.name, value)

  def CreateRunner(self, **kw):
    """Make a new runner."""
    raise NotImplementedError("Cannot call CreateRunner on the base class.")

  def GetRunner(self):
    # If we already created the runner, just reuse it.
    if self.runner:
      return self.runner

    # Otherwise make a new runner.
    return self.CreateRunner()

  def Flush(self, sync=True):
    """Flushes the flow/hunt and all its requests to the data_store."""
    # Check for Lock expiration first.
    self.CheckLease()
    self.Save()
    self.WriteState()
    self.Load()
    super(FlowBase, self).Flush(sync=sync)
    # Writing the messages queued in the queue_manager of the runner always has
    # to be the last thing that happens or we will have a race condition.
    self.FlushMessages()

  def Close(self, sync=True):
    """Flushes the flow and all its requests to the data_store."""
    # Check for Lock expiration first.
    self.CheckLease()
    self.Save()
    self.WriteState()
    super(FlowBase, self).Close(sync=sync)
    # Writing the messages queued in the queue_manager of the runner always has
    # to be the last thing that happens or we will have a race condition.
    self.FlushMessages()

  def FlushMessages(self):
    """Write all the messages queued in the queue manager."""
    self.GetRunner().FlushMessages()

  def NotifyAboutEnd(self):
    """Send out a final notification about the end of this flow."""
    self.Notify("FlowStatus", self.urn,
                "Flow %s completed" % self.__class__.__name__)

  def Terminate(self, status=None):
    self.NotifyAboutEnd()

  @StateHandler()
  def End(self):
    """Final state.

    This method is called prior to destruction of the flow to give
    the flow a chance to clean up.
    """

  @StateHandler()
  def Start(self, unused_message=None):
    """The first state of the flow."""
    pass

  def Load(self):
    """Loads the flow from storage.

    This hook point is called after retrieval from storage and prior to state
    execution.
    """

  def Save(self):
    """Saves the flow to disk.

    This hook point is called before we get dumped to storage. Note that for
    efficiency we do not generally get serialized on every state transition but
    we may be serialized on any transition.

    If we want to hold something which should only exist while running and not
    in serialized form (e.g. database handle), we can override the Load() and
    Save() methods to remove the object during Save() and recreate it during
    Load().
    """

  @classmethod
  def StartFlow(cls,
                args=None,
                runner_args=None,
                parent_flow=None,
                sync=True,
                token=None,
                **kwargs):
    """The main factory function for Creating and executing a new flow.

    Args:

      args: An arg protocol buffer which is an instance of the required flow's
        args_type class attribute.

      runner_args: an instance of FlowRunnerArgs() protocol buffer which is used
        to initialize the runner for this flow.

      parent_flow: A parent flow or None if this is a top level flow.

      sync: If True, the Start method of this flow will be called
         inline. Otherwise we schedule the starting of this flow on another
         worker.

      token: Security credentials token identifying the user.

      **kwargs: If args or runner_args are not specified, we construct these
        protobufs from these keywords.

    Returns:
      the session id of the flow.

    Raises:
      RuntimeError: Unknown or invalid parameters were provided.
    """
    # Build the runner args from the keywords.
    if runner_args is None:
      runner_args = rdf_flows.FlowRunnerArgs()

    cls.FilterArgsFromSemanticProtobuf(runner_args, kwargs)

    # When asked to run a flow in the future this implied it will run
    # asynchronously.
    if runner_args.start_time:
      sync = False

    # Is the required flow a known flow?
    if runner_args.flow_name not in GRRFlow.classes:
      stats.STATS.IncrementCounter("grr_flow_invalid_flow_count")
      raise RuntimeError("Unable to locate flow %s" % runner_args.flow_name)

    # If no token is specified, raise.
    if not token:
      raise access_control.UnauthorizedAccess("A token must be specified.")

    # For the flow itself we use a supervisor token.
    token = token.SetUID()

    # Extend the expiry time of this token indefinitely. Python on Windows only
    # supports dates up to the year 3000.
    token.expiry = rdfvalue.RDFDatetime.FromHumanReadable("2997-01-01")

    # We create an anonymous AFF4 object first, The runner will then generate
    # the appropriate URN.
    flow_obj = aff4.FACTORY.Create(
        None, aff4.AFF4Object.classes.get(runner_args.flow_name), token=token)

    # Now parse the flow args into the new object from the keywords.
    if args is None:
      args = flow_obj.args_type()

    cls.FilterArgsFromSemanticProtobuf(args, kwargs)

    # Check that the flow args are valid.
    args.Validate()

    # Store the flow args.
    flow_obj.args = args
    flow_obj.runner_args = runner_args

    # At this point we should exhaust all the keyword args. If any are left
    # over, we do not know what to do with them so raise.
    if kwargs:
      raise type_info.UnknownArg("Unknown parameters to StartFlow: %s" % kwargs)

    # Create a flow runner to run this flow with.
    if parent_flow:
      parent_runner = parent_flow.runner
    else:
      parent_runner = None

    runner = flow_obj.CreateRunner(
        parent_runner=parent_runner, runner_args=runner_args)

    logging.info(u"Scheduling %s(%s) on %s", flow_obj.urn,
                 runner_args.flow_name, runner_args.client_id)

    if sync:
      # Just run the first state inline. NOTE: Running synchronously means
      # that this runs on the thread that starts the flow. The advantage is
      # that that Start method can raise any errors immediately.
      flow_obj.Start()
    else:
      # Running Asynchronously: Schedule the start method on another worker.
      runner.CallState(next_state="Start", start_time=runner_args.start_time)

    # The flow does not need to actually remain running.
    if not runner.OutstandingRequests():
      flow_obj.Terminate()

    flow_obj.Close()

    # Publish an audit event, only for top level flows.
    if parent_flow is None:
      events.Events.PublishEvent(
          "Audit",
          events.AuditEvent(
              user=token.username,
              action="RUN_FLOW",
              flow_name=runner_args.flow_name,
              urn=flow_obj.urn,
              client=runner_args.client_id),
          token=token)

    return flow_obj.urn

  @property
  def session_id(self):
    return self.context.session_id

  def Publish(self,
              event_name,
              message=None,
              session_id=None,
              priority=rdf_flows.GrrMessage.Priority.MEDIUM_PRIORITY):
    """Publish a message to an event queue.

    Args:
       event_name: The name of the event to publish to.
       message: An RDFValue instance to publish to the event listeners.
       session_id: The session id to send from, defaults to self.session_id.
       priority: Controls the priority of this message.
    """
    result = message
    logging.debug("Publishing %s to %s",
                  utils.SmartUnicode(message)[:100], event_name)

    # Wrap message in a GrrMessage so it can be queued.
    if not isinstance(message, rdf_flows.GrrMessage):
      result = rdf_flows.GrrMessage(payload=message)

    result.session_id = session_id or self.session_id
    result.auth_state = rdf_flows.GrrMessage.AuthorizationState.AUTHENTICATED
    result.source = self.session_id
    result.priority = priority

    self.runner.Publish(event_name, result)

  def Log(self, format_str, *args):
    """Logs the message using the flow's standard logging.

    Args:
      format_str: Format string
      *args: arguments to the format string
    """
    self.GetRunner().Log(format_str, *args)

  def GetLog(self):
    return self.GetRunner().GetLog()

  # The following methods simply delegate to the runner. They are meant to only
  # be called from within the state handling methods (i.e. a runner
  # should already exist by calling GetRunner() method).
  def CallClient(self,
                 action_cls,
                 request=None,
                 next_state=None,
                 request_data=None,
                 **kwargs):
    return self.runner.CallClient(
        action_cls=action_cls,
        request=request,
        next_state=next_state,
        request_data=request_data,
        **kwargs)

  def CallStateInline(self,
                      messages=None,
                      next_state="",
                      request_data=None,
                      responses=None):
    if responses is None:
      responses = FakeResponses(messages, request_data)

    getattr(self, next_state)(self.runner, direct_response=responses)

  def CallState(self,
                messages=None,
                next_state="",
                request_data=None,
                start_time=None):
    return self.runner.CallState(
        messages=messages,
        next_state=next_state,
        request_data=request_data,
        start_time=start_time)

  def CallFlow(self, flow_name, next_state=None, request_data=None, **kwargs):
    return self.runner.CallFlow(
        flow_name, next_state=next_state, request_data=request_data, **kwargs)


class GRRFlow(FlowBase):
  """A container aff4 object to maintain a flow.

  Flow objects are executed and scheduled by the workers, and extend
  grr.flow.GRRFlow. This object contains the flows object within an AFF4
  container.

  Note: Usually this object can not be created by users using the regular
  aff4.FACTORY.Create() method since it requires elevated permissions. This
  object can instead be created using the flow.GRRFlow.StartFlow() method.

  After creation, access to the flow object can still be obtained through
  the usual aff4.FACTORY.Open() method.

  The GRRFlow object should be extended by flow implementations, adding state
  handling methods (State methods are called with responses and should be
  decorated using the StateHandler() decorator). The mechanics of running the
  flow are separated from the flow itself, using the runner object. Then
  FlowRunner() for the flow can be obtained from the flow.GetRunner(). The
  runner contains all the methods specific to running, scheduling and
  interrogating the flow:


  with aff4.FACTORY.Open(flow_urn, mode="rw") as fd:
    runner = fd.GetRunner()
    runner.ProcessCompletedRequests(messages)

  """

  class SchemaCls(aff4.AFF4Volume.SchemaCls):
    """Attributes specific to GRRFlow."""

    FLOW_STATE_DICT = aff4.Attribute(
        "aff4:flow_state_dict",
        rdf_protodict.AttributedDict,
        "The current state of this flow.",
        "FlowStateDict",
        versioned=False,
        creates_new_object_version=False)

    FLOW_ARGS = aff4.Attribute(
        "aff4:flow_args",
        rdf_protodict.EmbeddedRDFValue,
        "The arguments for this flow.",
        "FlowArgs",
        versioned=False,
        creates_new_object_version=False)

    FLOW_CONTEXT = aff4.Attribute(
        "aff4:flow_context",
        rdf_flows.FlowContext,
        "The metadata for this flow.",
        "FlowContext",
        versioned=False,
        creates_new_object_version=False)

    FLOW_RUNNER_ARGS = aff4.Attribute(
        "aff4:flow_runner_args",
        rdf_flows.FlowRunnerArgs,
        "The runner arguments used for this flow.",
        "FlowRunnerArgs",
        versioned=False,
        creates_new_object_version=False)

    NOTIFICATION = aff4.Attribute("aff4:notification", rdf_flows.Notification,
                                  "Notifications for the flow.")

    CLIENT_CRASH = aff4.Attribute(
        "aff4:client_crash",
        rdf_client.ClientCrash,
        "Client crash details in case of a crash.",
        default=None,
        creates_new_object_version=False)

    PENDING_TERMINATION = aff4.Attribute(
        "aff4:pending_termination",
        PendingFlowTermination,
        "If true, this flow will be "
        "terminated as soon as any of its "
        "states are called.",
        creates_new_object_version=False)

  # This is used to arrange flows into a tree view
  category = ""
  friendly_name = None

  # If this is set, the flow is only displayed in the UI if the user has one of
  # the labels given.
  AUTHORIZED_LABELS = []

  # Behaviors set attributes of this flow. See FlowBehavior() above.
  behaviours = FlowBehaviour("Client Flow", "ADVANCED")

  # If True we let the flow handle its own client crashes. Otherwise the flow
  # is killed when the client crashes.
  handles_crashes = False

  def Initialize(self):
    """The initialization method."""
    super(GRRFlow, self).Initialize()

    if "r" in self.mode:
      state = self.Get(self.Schema.FLOW_STATE_DICT)
      self.context = self.Get(self.Schema.FLOW_CONTEXT)
      self.runner_args = self.Get(self.Schema.FLOW_RUNNER_ARGS)
      args = self.Get(self.Schema.FLOW_ARGS)
      if args:
        self.args = args.payload

      if state:
        self.state = AttributedDict(state.ToDict())
      else:
        self.state = AttributedDict()

      self.Load()

    if self.state is None:
      self.state = AttributedDict()

  def CreateRunner(self, **kw):
    """Make a new runner."""
    self.runner = flow_runner.FlowRunner(self, token=self.token, **kw)
    return self.runner

  @classmethod
  def GetDefaultArgs(cls, token=None):
    """Return a useful default args semantic value.

    This should be extended by flows.

    Args:
      token: The ACL token for the user.

    Returns:
      an instance of cls.args_type pre-populated with useful data
    """
    _ = token
    return cls.args_type()

  def HeartBeat(self):
    if self.locked:
      lease_time = self.transaction.lease_time
      if self.CheckLease() < lease_time / 2:
        logging.debug("%s: Extending Lease", self.session_id)
        self.UpdateLease(lease_time)

        self.runner.HeartBeat()
    else:
      logging.warning("%s is heartbeating while not being locked.", self.urn)

  def _ValidateState(self):
    if self.context is None:
      raise IOError("Trying to write a flow without context: %s." % self.urn)

  def WriteState(self):
    if "w" in self.mode:
      self._ValidateState()
      self.Set(self.Schema.FLOW_ARGS(self.args))
      self.Set(self.Schema.FLOW_CONTEXT(self.context))
      self.Set(self.Schema.FLOW_RUNNER_ARGS(self.runner_args))
      protodict = rdf_protodict.AttributedDict().FromDict(self.state)
      self.Set(self.Schema.FLOW_STATE_DICT(protodict))

  def Status(self, format_str, *args):
    """Flows can call this method to set a status message visible to users."""
    self.GetRunner().Status(format_str, *args)

  def Notify(self, message_type, subject, msg):
    """Send a notification to the originating user.

    Args:
       message_type: The type of the message. This allows the UI to format
         a link to the original object e.g. "ViewObject" or "HostInformation"
       subject: The urn of the AFF4 object of interest in this link.
       msg: A free form textual message.
    """
    self.GetRunner().Notify(message_type, subject, msg)

  def SendReply(self, response):
    return self.runner.SendReply(response)

  def Error(self, backtrace, client_id=None):
    return self.runner.Error(backtrace, client_id=client_id)

  def Terminate(self, status=None):
    super(GRRFlow, self).Terminate(status=status)

    return self.runner.Terminate(status=status)

  @property
  def client_id(self):
    return self.runner_args.client_id

  def Name(self):
    return self.__class__.__name__

  @classmethod
  def MarkForTermination(cls,
                         flow_urn,
                         mutation_pool=None,
                         reason=None,
                         sync=False,
                         token=None):
    """Mark the flow for termination as soon as any of its states are called."""
    # Doing a blind write here using low-level data store API. Accessing
    # the flow via AFF4 is not really possible here, because it forces a state
    # to be written in Close() method.
    if mutation_pool:
      mutation_pool.Set(
          flow_urn,
          cls.SchemaCls.PENDING_TERMINATION.predicate,
          PendingFlowTermination(reason=reason),
          replace=False)
    else:
      data_store.DB.Set(
          flow_urn,
          cls.SchemaCls.PENDING_TERMINATION.predicate,
          PendingFlowTermination(reason=reason),
          replace=False,
          sync=sync,
          token=token)

  @classmethod
  def TerminateFlow(cls,
                    flow_id,
                    reason=None,
                    status=None,
                    token=None,
                    force=False):
    """Terminate a flow.

    Args:
      flow_id: The flow session_id to terminate.
      reason: A reason to log.
      status: Status code used in the generated status message.
      token: The access token to be used for this request.
      force: If True then terminate locked flows hard.

    Raises:
      FlowError: If the flow can not be found.
    """
    if not force:
      flow_obj = aff4.FACTORY.OpenWithLock(
          flow_id, aff4_type=GRRFlow, blocking=True, token=token)
    else:
      flow_obj = aff4.FACTORY.Open(
          flow_id, aff4_type=GRRFlow, mode="rw", token=token)

    if not flow_obj:
      raise FlowError("Could not terminate flow %s" % flow_id)

    with flow_obj:
      runner = flow_obj.GetRunner()
      if not runner.IsRunning():
        return

      if token is None:
        token = access_control.ACLToken()

      if reason is None:
        reason = "Manual termination by console."

      # This calls runner.Terminate to kill the flow
      runner.Error(reason, status=status)

      flow_obj.Log(
          "Terminated by user {0}. Reason: {1}".format(token.username, reason))

      # From now on we run with supervisor access
      super_token = token.SetUID()

      # Also terminate its children
      children_to_kill = aff4.FACTORY.MultiOpen(
          flow_obj.ListChildren(), token=super_token, aff4_type=GRRFlow)

      for child_obj in children_to_kill:
        cls.TerminateFlow(
            child_obj.urn,
            reason="Parent flow terminated.",
            token=super_token,
            force=force)

  @classmethod
  def PrintArgsHelp(cls):
    print cls.GetArgsHelpAsString()

  @classmethod
  def _ClsHelpEpilog(cls):
    return cls.GetArgsHelpAsString()

  @classmethod
  def GetCallingPrototypeAsString(cls):
    """Get a description of the calling prototype for this flow."""
    output = []
    output.append("flow.GRRFlow.StartFlow(client_id=client_id, ")
    output.append("flow_name=\"%s\", " % cls.__name__)
    prototypes = []
    if cls.args_type:
      for type_descriptor in cls.args_type.type_infos:
        if not type_descriptor.hidden:
          prototypes.append("%s=%s" % (type_descriptor.name,
                                       type_descriptor.name))
    output.append(", ".join(prototypes))
    output.append(")")
    return "".join(output)

  @classmethod
  def GetArgs(cls):
    """Get a simplified description of the args for this flow."""
    args = {}
    if cls.args_type:
      for type_descriptor in cls.args_type.type_infos:
        if not type_descriptor.hidden:
          args[type_descriptor.name] = {
              "description": type_descriptor.description,
              "default": type_descriptor.default,
              "type": "",
          }
          if type_descriptor.type:
            args[type_descriptor.name]["type"] = type_descriptor.type.__name__
    return args

  @classmethod
  def GetArgsHelpAsString(cls):
    """Get a string description of the calling prototype for this function."""
    output = ["  Call Spec:", "    %s" % cls.GetCallingPrototypeAsString(), ""]
    arg_list = sorted(cls.GetArgs().items(), key=lambda x: x[0])
    if not arg_list:
      output.append("  Args: None")
    else:
      output.append("  Args:")
      for arg, val in arg_list:
        output.append("    %s" % arg)
        output.append("      description: %s" % val["description"])
        output.append("      type: %s" % val["type"])
        output.append("      default: %s" % val["default"])
        output.append("")
    return "\n".join(output)

  @staticmethod
  def GetFlowRequests(flow_urns, token=None):
    """Returns all outstanding requests for the flows in flow_urns."""
    flow_requests = {}
    flow_request_urns = [flow_urn.Add("state") for flow_urn in flow_urns]

    for flow_urn, values in data_store.DB.MultiResolvePrefix(
        flow_request_urns, "flow:", token=token):
      for subject, serialized, _ in values:
        try:
          if "status" in subject:
            msg = rdf_flows.GrrMessage.FromSerializedString(serialized)
          else:
            msg = rdf_flows.RequestState.FromSerializedString(serialized)
        except Exception as e:  # pylint: disable=broad-except
          logging.warn("Error while parsing: %s", e)
          continue

        flow_requests.setdefault(flow_urn, []).append(msg)
    return flow_requests

  # All the collections flows use.

  # Results collection.
  @property
  def output_urn(self):
    return self.urn.Add(RESULTS_SUFFIX)

  @classmethod
  def ResultCollectionForFID(cls, flow_id, token=None):
    """Returns the ResultCollection for the flow with a given flow_id.

    Args:
      flow_id: The id of the flow, a RDFURN of the form aff4:/flows/F:123456.
      token: A data store token.
    Returns:
      The collection containing the results for the flow identified by the id.
    """
    return sequential_collection.GeneralIndexedCollection(
        flow_id.Add(RESULTS_SUFFIX), token=token)

  def ResultCollection(self):
    return self.ResultCollectionForFID(self.session_id, token=self.token)

  # Results collection per type.
  @property
  def multi_type_output_urn(self):
    return self.urn.Add(RESULTS_PER_TYPE_SUFFIX)

  @classmethod
  def TypedResultCollectionForFID(cls, flow_id, token=None):
    return multi_type_collection.MultiTypeCollection(
        flow_id.Add(RESULTS_PER_TYPE_SUFFIX), token=token)

  def TypedResultCollection(self):
    return self.TypedResultCollectionForFID(self.session_id, token=self.token)

  # Logs collection.
  @property
  def logs_collection_urn(self):
    return self.urn.Add(LOGS_SUFFIX)

  @classmethod
  def LogCollectionForFID(cls, flow_id, token=None):
    return grr_collections.LogCollection(flow_id.Add(LOGS_SUFFIX), token=token)

  def LogCollection(self):
    return self.LogCollectionForFID(self.session_id, token=self.token)


class GRRGlobalFlow(GRRFlow):
  """A flow that acts globally instead of on a specific client.

  Flows that inherit from this will not be shown in the normal Start New Flows
  UI, but will instead be seen in Admin Flows.
  """

  behaviours = GRRFlow.behaviours + "Global Flow" - "Client Flow"


class WellKnownFlow(GRRFlow):
  """A flow with a well known session_id.

  Since clients always need to communicate with a flow, it is
  impossible for them to asynchronously begin communication with the
  server because normally the flow's session ID is randomly
  generated. Sometimes we want the client to communicate with the
  server spontaneously - so it needs a well known session ID.

  This base class defines such flows with a well known
  session_id. Clients can communicate with these flows by themselves
  without prior arrangement.

  Note that necessarily well known flows do not have any state and
  therefore do not need state handlers. In this regard a WellKnownFlow
  is basically an RPC mechanism - if you need to respond with a
  complex sequence of actions you will need to spawn a new flow from
  here.
  """
  # This is the session_id that will be used to register these flows
  well_known_session_id = None

  # Well known flows are not browsable.
  category = None

  @classmethod
  def GetAllWellKnownFlows(cls, token=None):
    """Get instances of all well known flows."""
    well_known_flows = {}
    for cls in GRRFlow.classes.values():
      if aff4.issubclass(cls, WellKnownFlow) and cls.well_known_session_id:
        well_known_flow = cls(cls.well_known_session_id, mode="rw", token=token)
        well_known_flows[cls.well_known_session_id.FlowName()] = well_known_flow

    return well_known_flows

  def _SafeProcessMessage(self, *args, **kwargs):
    try:
      self.ProcessMessage(*args, **kwargs)
    except Exception as e:  # pylint: disable=broad-except
      logging.exception("Error in WellKnownFlow.ProcessMessage: %s", e)
      stats.STATS.IncrementCounter(
          "well_known_flow_errors", fields=[str(self.session_id)])

  def CallState(self, messages=None, next_state=None, delay=0):
    """Well known flows have no states to call."""
    pass

  @property
  def session_id(self):
    return self.well_known_session_id

  def OutstandingRequests(self):
    # Lie about it to prevent us from being destroyed
    return 1

  def FlushMessages(self):
    """Write all the queued messages."""
    # Well known flows do not write anything as they don't issue client calls
    # and don't have states.
    pass

  def FetchAndRemoveRequestsAndResponses(self, session_id):
    """Removes WellKnownFlow messages from the queue and returns them."""
    messages = []
    with queue_manager.WellKnownQueueManager(token=self.token) as manager:
      for response in manager.FetchResponses(session_id):
        messages.append(response)
      manager.DeleteWellKnownFlowResponses(session_id, messages)

    return messages

  def ProcessResponses(self, responses, thread_pool):
    """For WellKnownFlows we receive these messages directly."""
    for response in responses:
      thread_pool.AddTask(
          target=self._SafeProcessMessage,
          args=(response,),
          name=self.__class__.__name__)

  def ProcessMessages(self, msgs):
    for msg in msgs:
      self.ProcessMessage(msg)
      self.HeartBeat()

  def ProcessMessage(self, msg):
    """This is where messages get processed.

    Override in derived classes:

    Args:
       msg: The GrrMessage sent by the client. Note that this
            message is not authenticated.
    """

  def _ValidateState(self):
    # For normal flows it's a bug to write an empty state, here it's ok.
    pass

  def UpdateKillNotification(self):
    # For WellKnownFlows it doesn't make sense to kill them ever.
    pass


def EventHandler(source_restriction=False,
                 auth_required=True,
                 allow_client_access=False):
  """A convenience decorator for Event Handlers.

  Args:

    source_restriction: If this is set to True, each time a message is
      received, its source is passed to the method "CheckSource" of
      the event listener. If that method returns True, processing is
      permitted. Otherwise, the message is rejected.

    auth_required: Do we require messages to be authenticated? If the
                message is not authenticated we raise.

    allow_client_access: If True this event is allowed to handle published
      events from clients.

  Returns:
    A decorator which injects the following keyword args to the handler:

     message: The original raw message RDFValue (useful for checking the
       source).
     event: The decoded RDFValue.

  """

  def Decorator(f):
    """Initialised Decorator."""

    @functools.wraps(f)
    def Decorated(self, msg):
      """A decorator that assists in enforcing EventListener restrictions."""
      if (auth_required and
          msg.auth_state != msg.AuthorizationState.AUTHENTICATED):
        raise RuntimeError("Message from %s not authenticated." % msg.source)

      if (not allow_client_access and msg.source and
          rdf_client.ClientURN.Validate(msg.source)):
        raise RuntimeError("Event does not support clients.")

      if source_restriction:
        source_check_method = getattr(self, "CheckSource")
        if not source_check_method:
          raise RuntimeError("CheckSource method not found.")
        if not source_check_method(msg.source):
          raise RuntimeError("Message source invalid.")

      stats.STATS.IncrementCounter("grr_worker_states_run")
      rdf_msg = rdf_flows.GrrMessage(msg)
      res = f(self, message=rdf_msg, event=rdf_msg.payload)
      return res

    return Decorated

  return Decorator


class EventListener(WellKnownFlow):
  """Base Class for all Event Listeners.

  Event listeners are simply well known flows which extend the EventListener
  class. Registration for an event simply means that the event name is specified
  in the EVENTS constant.

  We will process any messages which are sent to any of the events
  specified. Events are just string names.
  """
  EVENTS = []

  __metaclass__ = registry.EventRegistry

  @EventHandler(auth_required=True)
  def ProcessMessage(self, message=None, event=None):
    """Handler for the event.

    NOTE: The message could arrive from any source, and could be
    unauthenticated. Since the EventListener is just a WellKnownFlow, the
    message could also arrive from a malicious client!

    It is therefore essential to verify the source of the event. This can be a
    flow session id, or an entity such as the FrontEnd, or the Worker.

    Args:
      message: A GrrMessage instance which was sent to the event listener.
      event: The decoded event object.
    """


class FlowInit(registry.InitHook):
  """Sets up flow-related stats."""

  pre = [aff4.AFF4InitHook]

  def RunOnce(self):
    # Counters defined here
    stats.STATS.RegisterCounterMetric("grr_flow_completed_count")
    stats.STATS.RegisterCounterMetric("grr_flow_errors")
    stats.STATS.RegisterCounterMetric("grr_flow_invalid_flow_count")
    stats.STATS.RegisterCounterMetric("grr_request_retransmission_count")
    stats.STATS.RegisterCounterMetric("grr_response_out_of_order")
    stats.STATS.RegisterCounterMetric("grr_unique_clients")
    stats.STATS.RegisterCounterMetric("grr_worker_states_run")
    stats.STATS.RegisterCounterMetric("grr_well_known_flow_requests")

    # Flow-aware counters
    stats.STATS.RegisterCounterMetric("flow_starts", fields=[("flow", str)])
    stats.STATS.RegisterCounterMetric("flow_errors", fields=[("flow", str)])
    stats.STATS.RegisterCounterMetric(
        "flow_completions", fields=[("flow", str)])
    stats.STATS.RegisterCounterMetric(
        "well_known_flow_requests", fields=[("flow", str)])
    stats.STATS.RegisterCounterMetric(
        "well_known_flow_errors", fields=[("flow", str)])
