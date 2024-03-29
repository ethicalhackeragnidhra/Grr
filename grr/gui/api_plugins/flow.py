#!/usr/bin/env python
"""API handlers for dealing with flows."""

import itertools
import re

from grr import config
from grr.gui import api_call_handler_base

from grr.gui import api_call_handler_utils
from grr.gui.api_plugins import client

from grr.gui.api_plugins import output_plugin as api_output_plugin
from grr.lib import access_control
from grr.lib import aff4
from grr.lib import client_index
from grr.lib import flow
from grr.lib import instant_output_plugin
from grr.lib import output_plugin
from grr.lib import queue_manager
from grr.lib import rdfvalue
from grr.lib import throttle
from grr.lib import utils
from grr.lib.aff4_objects import aff4_grr
from grr.lib.aff4_objects import cronjobs as aff4_cronjobs
from grr.lib.aff4_objects import users as aff4_users
from grr.lib.flows.general import file_finder
from grr.lib.rdfvalues import client as rdf_client
from grr.lib.rdfvalues import file_finder as rdf_file_finder
from grr.lib.rdfvalues import flows as rdf_flows
from grr.lib.rdfvalues import paths as rdf_paths
from grr.lib.rdfvalues import structs as rdf_structs

from grr.proto.api import flow_pb2


class FlowNotFoundError(api_call_handler_base.ResourceNotFoundError):
  """Raised when a flow is not found."""


class RobotGetFilesOperationNotFoundError(
    api_call_handler_base.ResourceNotFoundError):
  """Raises when "get files" operation is not found."""


class ApiFlowId(rdfvalue.RDFString):
  """Class encapsulating flows ids."""

  def __init__(self, initializer=None, age=None):
    super(ApiFlowId, self).__init__(initializer=initializer, age=age)

    # TODO(user): move this to a separate validation method when
    # common RDFValues validation approach is implemented.
    if self._value:
      components = self.Split()
      for component in components:
        try:
          rdfvalue.SessionID.ValidateID(component)
        except ValueError as e:
          raise ValueError("Invalid flow id: %s (%s)" %
                           (utils.SmartStr(self._value), e))

  def _FlowIdToUrn(self, flow_id, client_id):
    return client_id.ToClientURN().Add("flows").Add(flow_id)

  def ResolveCronJobFlowURN(self, cron_job_id):
    """Resolve a URN of a flow with this id belonging to a given cron job."""
    if not self._value:
      raise ValueError("Can't call ResolveCronJobFlowURN on an empty "
                       "client id.")

    return aff4_cronjobs.CRON_MANAGER.CRON_JOBS_PATH.Add(cron_job_id).Add(
        self._value)

  def ResolveClientFlowURN(self, client_id, token=None):
    """Resolve a URN of a flow with this id belonging to a given client.

    Note that this may need a roundtrip to the datastore. Resolving algorithm
    is the following:
    1.  If the flow id doesn't contain slashes (flow is not nested), we just
        append it to the <client id>/flows.
    2.  If the flow id has slashes (flow is nested), we check if the root
        flow pointed to by <client id>/flows/<flow id> is a symlink.
    2a. If it's a symlink, we append the rest of the flow id to the symlink
        target.
    2b. If it's not a symlink, we just append the whole id to
        <client id>/flows (meaning we do the same as in 1).

    Args:
      client_id: Id of a client where this flow is supposed to be found on.
      token: Credentials token.
    Returns:
      RDFURN pointing to a flow identified by this flow id and client id.
    Raises:
      ValueError: if this flow id is not initialized.
    """
    if not self._value:
      raise ValueError("Can't call ResolveClientFlowURN on an empty client id.")

    components = self.Split()
    if len(components) == 1:
      return self._FlowIdToUrn(self._value, client_id)
    else:
      root_urn = self._FlowIdToUrn(components[0], client_id)
      try:
        flow_symlink = aff4.FACTORY.Open(
            root_urn,
            aff4_type=aff4.AFF4Symlink,
            follow_symlinks=False,
            token=token)

        return flow_symlink.Get(flow_symlink.Schema.SYMLINK_TARGET).Add(
            "/".join(components[1:]))
      except aff4.InstantiationError:
        return self._FlowIdToUrn(self._value, client_id)

  def Split(self):
    if not self._value:
      raise ValueError("Can't call Split() on an empty client id.")

    return self._value.split("/")


class ApiFlowDescriptor(rdf_structs.RDFProtoStruct):
  """Descriptor containing information about a flow class."""

  protobuf = flow_pb2.ApiFlowDescriptor

  def GetDefaultArgsClass(self):
    return rdfvalue.RDFValue.classes.get(self.args_type)

  def InitFromFlowClass(self, flow_cls, token=None):
    if not token:
      raise ValueError("token can't be None")

    self.name = flow_cls.__name__
    self.friendly_name = flow_cls.friendly_name
    self.category = flow_cls.category.strip("/")
    self.doc = flow_cls.__doc__
    self.args_type = flow_cls.args_type.__name__
    self.default_args = flow_cls.GetDefaultArgs(token=token)
    self.behaviours = sorted(flow_cls.behaviours)

    return self


class ApiFlow(rdf_structs.RDFProtoStruct):
  """ApiFlow is used when rendering responses.

  ApiFlow is meant to be more lightweight than automatically generated AFF4
  representation. It's also meant to contain only the information needed by
  the UI and and to not expose implementation defails.
  """
  protobuf = flow_pb2.ApiFlow
  rdf_deps = [
      api_call_handler_utils.ApiDataObject,
      "ApiFlow",  # TODO(user): recursive dependency.
      ApiFlowId,
      rdf_flows.FlowContext,
      rdf_flows.FlowRunnerArgs,
      rdfvalue.RDFDatetime,
      rdfvalue.SessionID,
  ]

  def GetArgsClass(self):
    flow_name = self.name
    if not flow_name:
      flow_name = self.runner_args.flow_name

    if flow_name:
      flow_cls = flow.GRRFlow.classes.get(flow_name)
      if flow_cls is None:
        raise ValueError(
            "Flow %s not known by this implementation." % flow_name)

      # The required protobuf for this class is in args_type.
      return flow_cls.args_type

  def InitFromAff4Object(self,
                         flow_obj,
                         flow_id=None,
                         with_state_and_context=False):
    # TODO(user): we should be able to infer flow id from the URN. Currently
    # it's not possible due to an inconsistent way in which we create symlinks
    # and name them.
    self.flow_id = flow_id
    self.urn = flow_obj.urn

    self.name = flow_obj.runner_args.flow_name
    self.started_at = flow_obj.context.create_time
    self.last_active_at = flow_obj.Get(flow_obj.Schema.LAST)
    self.creator = flow_obj.context.creator

    if flow_obj.Get(flow_obj.Schema.CLIENT_CRASH):
      self.state = "CLIENT_CRASHED"
    else:
      self.state = flow_obj.context.state

    try:
      self.args = flow_obj.args
    except ValueError:
      # If args class name has changed, ValueError will be raised. Handling
      # this gracefully - we should still try to display some useful info
      # about the flow.
      pass

    self.runner_args = flow_obj.runner_args

    if with_state_and_context:
      try:
        self.context = flow_obj.context
      except ValueError:
        pass

      flow_state_dict = flow_obj.Get(flow_obj.Schema.FLOW_STATE_DICT)
      if flow_state_dict is not None:
        flow_state_data = flow_state_dict.ToDict()

        if flow_state_data:
          self.state_data = (api_call_handler_utils.ApiDataObject()
                             .InitFromDataObject(flow_state_data))

    return self


class ApiFlowRequest(rdf_structs.RDFProtoStruct):
  protobuf = flow_pb2.ApiFlowRequest
  rdf_deps = [
      rdf_flows.GrrMessage,
      rdf_flows.RequestState,
  ]


class ApiFlowResult(rdf_structs.RDFProtoStruct):
  protobuf = flow_pb2.ApiFlowResult
  rdf_deps = [
      rdfvalue.RDFDatetime,
  ]

  def GetPayloadClass(self):
    return rdfvalue.RDFValue.classes[self.payload_type]

  def InitFromRdfValue(self, value):
    self.payload_type = value.__class__.__name__
    self.payload = value
    self.timestamp = value.age

    return self


class ApiGetFlowArgs(rdf_structs.RDFProtoStruct):
  protobuf = flow_pb2.ApiGetFlowArgs
  rdf_deps = [
      client.ApiClientId,
      ApiFlowId,
  ]


class ApiGetFlowHandler(api_call_handler_base.ApiCallHandler):
  """Renders given flow.

  Only top-level flows can be targeted. Times returned in the response are micro
  seconds since epoch.
  """

  args_type = ApiGetFlowArgs
  result_type = ApiFlow

  def Handle(self, args, token=None):
    flow_urn = args.flow_id.ResolveClientFlowURN(args.client_id, token=token)
    flow_obj = aff4.FACTORY.Open(
        flow_urn, aff4_type=flow.GRRFlow, mode="r", token=token)

    return ApiFlow().InitFromAff4Object(
        flow_obj, flow_id=args.flow_id, with_state_and_context=True)


class ApiListFlowRequestsArgs(rdf_structs.RDFProtoStruct):
  protobuf = flow_pb2.ApiListFlowRequestsArgs
  rdf_deps = [
      client.ApiClientId,
      ApiFlowId,
  ]


class ApiListFlowRequestsResult(rdf_structs.RDFProtoStruct):
  protobuf = flow_pb2.ApiListFlowRequestsResult
  rdf_deps = [
      ApiFlowRequest,
  ]


class ApiListFlowRequestsHandler(api_call_handler_base.ApiCallHandler):
  """Renders list of requests of a given flow."""

  args_type = ApiListFlowRequestsArgs
  result_type = ApiListFlowRequestsResult

  def Handle(self, args, token=None):
    flow_urn = args.flow_id.ResolveClientFlowURN(args.client_id, token=token)

    # Check if this flow really exists.
    try:
      aff4.FACTORY.Open(flow_urn, aff4_type=flow.GRRFlow, mode="r", token=token)
    except aff4.InstantiationError:
      raise FlowNotFoundError()

    result = ApiListFlowRequestsResult()
    manager = queue_manager.QueueManager(token=token)
    requests_responses = manager.FetchRequestsAndResponses(flow_urn)

    stop = None
    if args.count:
      stop = args.offset + args.count

    for request, responses in itertools.islice(requests_responses, args.offset,
                                               stop):
      if request.id == 0:
        continue

      # TODO(user): The request_id field should be an int.
      api_request = ApiFlowRequest(
          request_id=str(request.id), request_state=request)

      if responses:
        api_request.responses = responses

      result.items.append(api_request)

    return result


class ApiListFlowResultsArgs(rdf_structs.RDFProtoStruct):
  protobuf = flow_pb2.ApiListFlowResultsArgs
  rdf_deps = [
      client.ApiClientId,
      ApiFlowId,
  ]


class ApiListFlowResultsResult(rdf_structs.RDFProtoStruct):
  protobuf = flow_pb2.ApiListFlowResultsResult
  rdf_deps = [
      ApiFlowResult,
  ]


class ApiListFlowResultsHandler(api_call_handler_base.ApiCallHandler):
  """Renders results of a given flow."""

  args_type = ApiListFlowResultsArgs
  result_type = ApiListFlowResultsResult

  def Handle(self, args, token=None):
    flow_urn = args.flow_id.ResolveClientFlowURN(args.client_id, token=token)
    output_collection = flow.GRRFlow.ResultCollectionForFID(
        flow_urn, token=token)

    items = api_call_handler_utils.FilterCollection(
        output_collection, args.offset, args.count, args.filter)
    wrapped_items = [ApiFlowResult().InitFromRdfValue(item) for item in items]
    return ApiListFlowResultsResult(
        items=wrapped_items, total_count=len(output_collection))


class ApiListFlowLogsArgs(rdf_structs.RDFProtoStruct):
  protobuf = flow_pb2.ApiListFlowLogsArgs
  rdf_deps = [
      client.ApiClientId,
      ApiFlowId,
  ]


class ApiListFlowLogsResult(rdf_structs.RDFProtoStruct):
  protobuf = flow_pb2.ApiListFlowLogsResult
  rdf_deps = [
      rdf_flows.FlowLog,
  ]


class ApiListFlowLogsHandler(api_call_handler_base.ApiCallHandler):
  """Returns a list of logs for the current client and flow."""

  args_type = ApiListFlowLogsArgs
  result_type = ApiListFlowLogsResult

  def Handle(self, args, token=None):
    flow_urn = args.flow_id.ResolveClientFlowURN(args.client_id, token=token)
    logs_collection = flow.GRRFlow.LogCollectionForFID(flow_urn, token=token)

    result = api_call_handler_utils.FilterCollection(
        logs_collection, args.offset, args.count, args.filter)

    return ApiListFlowLogsResult(items=result, total_count=len(logs_collection))


class ApiGetFlowResultsExportCommandArgs(rdf_structs.RDFProtoStruct):
  protobuf = flow_pb2.ApiGetFlowResultsExportCommandArgs
  rdf_deps = [
      client.ApiClientId,
      ApiFlowId,
  ]


class ApiGetFlowResultsExportCommandResult(rdf_structs.RDFProtoStruct):
  protobuf = flow_pb2.ApiGetFlowResultsExportCommandResult


class ApiGetFlowResultsExportCommandHandler(
    api_call_handler_base.ApiCallHandler):
  """Renders GRR export tool command line that exports flow results."""

  args_type = ApiGetFlowResultsExportCommandArgs
  result_type = ApiGetFlowResultsExportCommandResult

  def Handle(self, args, token=None):
    output_fname = re.sub("[^0-9a-zA-Z]+", "_",
                          "%s_%s" % (utils.SmartStr(args.client_id),
                                     utils.SmartStr(args.flow_id)))
    code_to_execute = ("""grrapi.Client("%s").Flow("%s").GetFilesArchive()."""
                       """WriteToFile("./flow_results_%s.zip")""") % (
                           args.client_id, args.flow_id, output_fname)

    export_command_str = " ".join([
        config.CONFIG["AdminUI.export_command"], "--exec_code",
        utils.ShellQuote(code_to_execute)
    ])

    return ApiGetFlowResultsExportCommandResult(command=export_command_str)


class ApiGetFlowFilesArchiveArgs(rdf_structs.RDFProtoStruct):
  protobuf = flow_pb2.ApiGetFlowFilesArchiveArgs
  rdf_deps = [
      client.ApiClientId,
      ApiFlowId,
  ]


class ApiGetFlowFilesArchiveHandler(api_call_handler_base.ApiCallHandler):
  """Generates archive with all files referenced in flow's results."""

  args_type = ApiGetFlowFilesArchiveArgs

  def __init__(self, path_globs_blacklist=None, path_globs_whitelist=None):
    """Constructor.

    Args:
      path_globs_blacklist: List of paths.GlobExpression values. Blacklist
          will be applied before the whitelist.
      path_globs_whitelist: List of paths.GlobExpression values. Whitelist
          will be applied after the blacklist.

    Raises:
      ValueError: If path_globs_blacklist/whitelist is passed, but
          the other blacklist/whitelist argument is not.

    Note that path_globs_blacklist/whitelist arguments can only be passed
    together. The algorithm of applying the lists is the following:
    1. If the lists are not set, include the file into the archive. Otherwise:
    2. If the file matches the blacklist, skip the file. Otherwise:
    3. If the file does match the whitelist, skip the file.
    """
    super(api_call_handler_base.ApiCallHandler, self).__init__()

    if len(
        [x for x in (path_globs_blacklist, path_globs_whitelist)
         if x is None]) == 1:
      raise ValueError("path_globs_blacklist/path_globs_whitelist have to "
                       "set/unset together.")

    self.path_globs_blacklist = path_globs_blacklist
    self.path_globs_whitelist = path_globs_whitelist

  def _WrapContentGenerator(self, generator, collection, args, token=None):
    user = aff4.FACTORY.Create(
        aff4.ROOT_URN.Add("users").Add(token.username),
        aff4_type=aff4_users.GRRUser,
        mode="rw",
        token=token)
    try:
      for item in generator.Generate(collection, token=token):
        yield item

      user.Notify("ArchiveGenerationFinished", None,
                  "Downloaded archive of flow %s from client %s (archived %d "
                  "out of %d items, archive size is %d)" %
                  (args.flow_id, args.client_id, generator.archived_files,
                   generator.total_files,
                   generator.output_size), self.__class__.__name__)
    except Exception as e:
      user.Notify("Error", None,
                  "Archive generation failed for flow %s on client %s: %s" %
                  (args.flow_id, args.client_id,
                   utils.SmartStr(e)), self.__class__.__name__)
      raise
    finally:
      user.Close()

  def _BuildPredicate(self, client_id, token=None):
    if self.path_globs_whitelist is None:
      return None

    client_obj = aff4.FACTORY.Open(
        client_id.ToClientURN(), aff4_type=aff4_grr.VFSGRRClient, token=token)

    blacklist_regexes = []
    for expression in self.path_globs_blacklist:
      for pattern in expression.Interpolate(client=client_obj):
        blacklist_regexes.append(rdf_paths.GlobExpression(pattern).AsRegEx())

    whitelist_regexes = []
    for expression in self.path_globs_whitelist:
      for pattern in expression.Interpolate(client=client_obj):
        whitelist_regexes.append(rdf_paths.GlobExpression(pattern).AsRegEx())

    def Predicate(fd):
      pathspec = fd.Get(fd.Schema.PATHSPEC)
      path = pathspec.CollapsePath()
      return (not any(r.Match(path) for r in blacklist_regexes) and
              any(r.Match(path) for r in whitelist_regexes))

    return Predicate

  def Handle(self, args, token=None):
    flow_urn = args.flow_id.ResolveClientFlowURN(args.client_id, token=token)
    flow_obj = aff4.FACTORY.Open(
        flow_urn, aff4_type=flow.GRRFlow, mode="r", token=token)

    flow_api_object = ApiFlow().InitFromAff4Object(
        flow_obj, flow_id=args.flow_id)
    description = ("Files downloaded by flow %s (%s) that ran on client %s by "
                   "user %s on %s" %
                   (flow_api_object.name, args.flow_id, args.client_id,
                    flow_api_object.creator, flow_api_object.started_at))

    target_file_prefix = "%s_flow_%s_%s" % (
        args.client_id, flow_obj.runner_args.flow_name,
        flow_urn.Basename().replace(":", "_"))

    collection = flow.GRRFlow.ResultCollectionForFID(flow_urn, token=token)

    if args.archive_format == args.ArchiveFormat.ZIP:
      archive_format = api_call_handler_utils.CollectionArchiveGenerator.ZIP
      file_extension = ".zip"
    elif args.archive_format == args.ArchiveFormat.TAR_GZ:
      archive_format = api_call_handler_utils.CollectionArchiveGenerator.TAR_GZ
      file_extension = ".tar.gz"
    else:
      raise ValueError("Unknown archive format: %s" % args.archive_format)

    generator = api_call_handler_utils.CollectionArchiveGenerator(
        prefix=target_file_prefix,
        description=description,
        archive_format=archive_format,
        predicate=self._BuildPredicate(args.client_id, token=token),
        client_id=args.client_id.ToClientURN())
    content_generator = self._WrapContentGenerator(
        generator, collection, args, token=token)
    return api_call_handler_base.ApiBinaryStream(
        target_file_prefix + file_extension,
        content_generator=content_generator)


class ApiListFlowOutputPluginsArgs(rdf_structs.RDFProtoStruct):
  protobuf = flow_pb2.ApiListFlowOutputPluginsArgs
  rdf_deps = [
      client.ApiClientId,
      ApiFlowId,
  ]


class ApiListFlowOutputPluginsResult(rdf_structs.RDFProtoStruct):
  protobuf = flow_pb2.ApiListFlowOutputPluginsResult
  rdf_deps = [
      api_output_plugin.ApiOutputPlugin,
  ]


class ApiListFlowOutputPluginsHandler(api_call_handler_base.ApiCallHandler):
  """Renders output plugins descriptors and states for a given flow."""

  args_type = ApiListFlowOutputPluginsArgs
  result_type = ApiListFlowOutputPluginsResult

  def Handle(self, args, token=None):
    flow_urn = args.flow_id.ResolveClientFlowURN(args.client_id, token=token)
    flow_obj = aff4.FACTORY.Open(
        flow_urn, aff4_type=flow.GRRFlow, mode="r", token=token)

    output_plugins_states = flow_obj.GetRunner().context.output_plugins_states

    type_indices = {}
    result = []
    for output_plugin_state in output_plugins_states:
      plugin_descriptor = output_plugin_state.plugin_descriptor
      plugin_state = output_plugin_state.plugin_state
      type_index = type_indices.setdefault(plugin_descriptor.plugin_name, 0)
      type_indices[plugin_descriptor.plugin_name] += 1

      # Output plugins states are stored differently for hunts and for flows:
      # as a dictionary for hunts and as a simple list for flows.
      #
      # TODO(user): store output plugins states in the same way for flows
      # and hunts. Until this is done, we can emulate the same interface in
      # the HTTP API.
      api_plugin = api_output_plugin.ApiOutputPlugin(
          id=plugin_descriptor.plugin_name + "_%d" % type_index,
          plugin_descriptor=plugin_descriptor,
          state=plugin_state)
      result.append(api_plugin)

    return ApiListFlowOutputPluginsResult(items=result)


class ApiListFlowOutputPluginLogsHandlerBase(
    api_call_handler_base.ApiCallHandler):
  """Base class used to define log and error messages handlers."""

  __abstract = True  # pylint: disable=g-bad-name

  attribute_name = None

  def Handle(self, args, token=None):
    if not self.attribute_name:
      raise ValueError("attribute_name can't be None")

    flow_urn = args.flow_id.ResolveClientFlowURN(args.client_id, token=token)
    flow_obj = aff4.FACTORY.Open(
        flow_urn, aff4_type=flow.GRRFlow, mode="r", token=token)

    output_plugins_states = flow_obj.GetRunner().context.output_plugins_states

    # Flow output plugins don't use collections to store status/error
    # information. Instead, it's stored in plugin's state. Nevertheless,
    # we emulate collections API here. Having similar API interface allows
    # one to reuse the code when handling hunts and flows output plugins.
    # Good example is the UI code.
    type_indices = {}
    found_state = None

    for output_plugin_state in output_plugins_states:
      plugin_descriptor = output_plugin_state.plugin_descriptor
      plugin_state = output_plugin_state.plugin_state
      type_index = type_indices.setdefault(plugin_descriptor.plugin_name, 0)
      type_indices[plugin_descriptor.plugin_name] += 1

      if args.plugin_id == plugin_descriptor.plugin_name + "_%d" % type_index:
        found_state = plugin_state
        break

    if not found_state:
      raise RuntimeError("Flow %s doesn't have output plugin %s" %
                         (flow_urn, args.plugin_id))

    stop = None
    if args.count:
      stop = args.offset + args.count

    logs_collection = found_state.get(self.attribute_name, [])
    sliced_collection = logs_collection[args.offset:stop]

    return self.result_type(
        total_count=len(logs_collection), items=sliced_collection)


class ApiListFlowOutputPluginLogsArgs(rdf_structs.RDFProtoStruct):
  protobuf = flow_pb2.ApiListFlowOutputPluginLogsArgs
  rdf_deps = [
      client.ApiClientId,
      ApiFlowId,
  ]


class ApiListFlowOutputPluginLogsResult(rdf_structs.RDFProtoStruct):
  protobuf = flow_pb2.ApiListFlowOutputPluginLogsResult
  rdf_deps = [
      output_plugin.OutputPluginBatchProcessingStatus,
  ]


class ApiListFlowOutputPluginLogsHandler(
    ApiListFlowOutputPluginLogsHandlerBase):
  """Renders flow's output plugin's logs."""

  attribute_name = "logs"
  args_type = ApiListFlowOutputPluginLogsArgs
  result_type = ApiListFlowOutputPluginLogsResult


class ApiListFlowOutputPluginErrorsArgs(rdf_structs.RDFProtoStruct):
  protobuf = flow_pb2.ApiListFlowOutputPluginErrorsArgs
  rdf_deps = [
      client.ApiClientId,
      ApiFlowId,
  ]


class ApiListFlowOutputPluginErrorsResult(rdf_structs.RDFProtoStruct):
  protobuf = flow_pb2.ApiListFlowOutputPluginErrorsResult
  rdf_deps = [
      output_plugin.OutputPluginBatchProcessingStatus,
  ]


class ApiListFlowOutputPluginErrorsHandler(
    ApiListFlowOutputPluginLogsHandlerBase):
  """Renders flow's output plugin's errors."""

  attribute_name = "errors"
  args_type = ApiListFlowOutputPluginErrorsArgs
  result_type = ApiListFlowOutputPluginErrorsResult


class ApiListFlowsArgs(rdf_structs.RDFProtoStruct):
  protobuf = flow_pb2.ApiListFlowsArgs
  rdf_deps = [
      client.ApiClientId,
  ]


class ApiListFlowsResult(rdf_structs.RDFProtoStruct):
  protobuf = flow_pb2.ApiListFlowsResult
  rdf_deps = [
      ApiFlow,
  ]


class ApiListFlowsHandler(api_call_handler_base.ApiCallHandler):
  """Lists flows launched on a given client."""

  args_type = ApiListFlowsArgs
  result_type = ApiListFlowsResult

  @staticmethod
  def _GetCreationTime(obj):
    if obj.context:
      return obj.context.create_time
    else:
      return obj.Get(obj.Schema.LAST, 0)

  @staticmethod
  def BuildFlowList(root_urn, count, offset, token=None):
    if not count:
      stop = None
    else:
      stop = offset + count

    root_children_urns = aff4.FACTORY.Open(root_urn, token=token).ListChildren()
    root_children_urns = sorted(
        root_children_urns, key=lambda x: x.age, reverse=True)
    root_children_urns = root_children_urns[offset:stop]

    root_children = aff4.FACTORY.MultiOpen(
        root_children_urns, aff4_type=flow.GRRFlow, token=token)
    root_children = sorted(
        root_children, key=ApiListFlowsHandler._GetCreationTime, reverse=True)

    nested_children_urns = dict(
        aff4.FACTORY.RecursiveMultiListChildren(
            [fd.urn for fd in root_children], token=token))
    nested_children = aff4.FACTORY.MultiOpen(
        set(itertools.chain(*nested_children_urns.values())),
        aff4_type=flow.GRRFlow,
        token=token)
    nested_children_map = dict((x.urn, x) for x in nested_children)

    def BuildList(fds, parent_id=None):
      """Builds list of flows recursively."""
      result = []
      for fd in fds:

        try:
          urn = fd.symlink_urn or fd.urn
          if parent_id:
            flow_id = "%s/%s" % (parent_id, urn.Basename())
          else:
            flow_id = urn.Basename()
          api_flow = ApiFlow().InitFromAff4Object(fd, flow_id=flow_id)
        except AttributeError:
          # If this doesn't work there's no way to recover.
          continue

        try:
          children_urns = nested_children_urns[fd.urn]
        except KeyError:
          children_urns = []

        children = []
        for urn in children_urns:
          try:
            children.append(nested_children_map[urn])
          except KeyError:
            pass

        children = sorted(
            children, key=ApiListFlowsHandler._GetCreationTime, reverse=True)
        try:
          api_flow.nested_flows = BuildList(children, parent_id=flow_id)
        except KeyError:
          pass
        result.append(api_flow)

      return result

    return ApiListFlowsResult(items=BuildList(root_children))

  def Handle(self, args, token=None):
    client_root_urn = args.client_id.ToClientURN().Add("flows")

    return self.BuildFlowList(
        client_root_urn, args.count, args.offset, token=token)


class ApiStartRobotGetFilesOperationArgs(rdf_structs.RDFProtoStruct):
  protobuf = flow_pb2.ApiStartRobotGetFilesOperationArgs
  rdf_deps = [
      rdfvalue.ByteSize,
      rdf_paths.GlobExpression,
  ]


class ApiStartRobotGetFilesOperationResult(rdf_structs.RDFProtoStruct):
  protobuf = flow_pb2.ApiStartRobotGetFilesOperationResult


class ApiStartRobotGetFilesOperationHandler(
    api_call_handler_base.ApiCallHandler):
  """Downloads files from specified machine without requiring approval."""

  args_type = ApiStartRobotGetFilesOperationArgs
  result_type = ApiStartRobotGetFilesOperationResult

  def GetClientTarget(self, args, token=None):
    # Find the right client to target using a hostname search.
    index = client_index.CreateClientIndex(token=token)

    client_list = index.LookupClients([args.hostname])
    if not client_list:
      raise ValueError("No client found matching %s" % args.hostname)

    # If we get more than one, take the one with the most recent poll.
    if len(client_list) > 1:
      return client_index.GetMostRecentClient(client_list, token=token)
    else:
      return client_list[0]

  def Handle(self, args, token=None):
    client_urn = self.GetClientTarget(args, token=token)

    size_condition = rdf_file_finder.FileFinderCondition(
        condition_type=rdf_file_finder.FileFinderCondition.Type.SIZE,
        size=rdf_file_finder.FileFinderSizeCondition(
            max_file_size=args.max_file_size))

    file_finder_args = rdf_file_finder.FileFinderArgs(
        paths=args.paths,
        action=rdf_file_finder.FileFinderAction(action_type=args.action),
        conditions=[size_condition])

    # Check our flow throttling limits, will raise if there are problems.
    throttler = throttle.FlowThrottler(
        daily_req_limit=config.CONFIG.Get("API.DailyFlowRequestLimit"),
        dup_interval=config.CONFIG.Get("API.FlowDuplicateInterval"))
    throttler.EnforceLimits(
        client_urn,
        token.username,
        file_finder.FileFinder.__name__,
        file_finder_args,
        token=token)

    # Limit the whole flow to 200MB so if a glob matches lots of small files we
    # still don't have too much impact.
    runner_args = rdf_flows.FlowRunnerArgs(
        client_id=client_urn,
        flow_name=file_finder.FileFinder.__name__,
        network_bytes_limit=200 * 1000 * 1000)

    flow_id = flow.GRRFlow.StartFlow(
        runner_args=runner_args, token=token, args=file_finder_args)

    return ApiStartRobotGetFilesOperationResult(
        operation_id=utils.SmartUnicode(flow_id))


class ApiGetRobotGetFilesOperationStateArgs(rdf_structs.RDFProtoStruct):
  protobuf = flow_pb2.ApiGetRobotGetFilesOperationStateArgs


class ApiGetRobotGetFilesOperationStateResult(rdf_structs.RDFProtoStruct):
  protobuf = flow_pb2.ApiGetRobotGetFilesOperationStateResult


class ApiGetRobotGetFilesOperationStateHandler(
    api_call_handler_base.ApiCallHandler):
  """Renders summary of a given flow.

  Only top-level flows can be targeted. Times returned in the response are micro
  seconds since epoch.
  """

  args_type = ApiGetRobotGetFilesOperationStateArgs
  result_type = ApiGetRobotGetFilesOperationStateResult

  def Handle(self, args, token=None):
    """Render robot "get files" operation status.

    This handler relies on URN validation and flow type checking to check the
    input parameters to avoid allowing arbitrary reads into the client aff4
    space. This handler filters out only the attributes that are appropriate to
    release without authorization (authentication is still required).

    Args:
      args: ApiGetRobotGetFilesOperationStateArgs object.
      token: access token.
    Returns:
      ApiGetRobotGetFilesOperationStateResult object.
    Raises:
      RobotGetFilesOperationNotFoundError: if operation is not found (i.e.
          if the flow is not found or is not a FileFinder flow).
      ValueError: if operation id is incorrect. It should match the
          aff4:/<client id>/flows/<flow session id> pattern exactly.
    """

    # We deconstruct the operation id and rebuild it as a URN to ensure
    # that it points to the flow on a client.
    urn = rdfvalue.RDFURN(args.operation_id)
    urn_components = urn.Split()

    if len(urn_components) != 3 or urn_components[1] != "flows":
      raise ValueError("Invalid operation id.")

    client_urn = rdf_client.ClientURN(urn_components[0])

    rdfvalue.SessionID.ValidateID(urn_components[2])
    flow_id = rdfvalue.SessionID(urn_components[2])

    # flow_id looks like aff4:/F:ABCDEF12, convert it into a flow urn for
    # the target client.
    flow_urn = client_urn.Add("flows").Add(flow_id.Basename())
    try:
      flow_obj = aff4.FACTORY.Open(
          flow_urn, aff4_type=file_finder.FileFinder, token=token)
    except aff4.InstantiationError:
      raise RobotGetFilesOperationNotFoundError()

    result_collection = flow.GRRFlow.ResultCollectionForFID(
        flow_urn, token=token)
    result_count = len(result_collection)

    api_flow_obj = ApiFlow().InitFromAff4Object(
        flow_obj, flow_id=flow_id.Basename())
    return ApiGetRobotGetFilesOperationStateResult(
        state=api_flow_obj.state, result_count=result_count)


class ApiCreateFlowArgs(rdf_structs.RDFProtoStruct):
  protobuf = flow_pb2.ApiCreateFlowArgs
  rdf_deps = [
      client.ApiClientId,
      ApiFlow,
  ]


class ApiCreateFlowHandler(api_call_handler_base.ApiCallHandler):
  """Starts a flow on a given client with given parameters."""

  args_type = ApiCreateFlowArgs
  result_type = ApiFlow

  def Handle(self, args, token=None):
    flow_name = args.flow.name
    if not flow_name:
      flow_name = args.flow.runner_args.flow_name
    if not flow_name:
      raise RuntimeError("Flow name is not specified.")

    # Clear all fields marked with HIDDEN, except for output_plugins - they are
    # marked HIDDEN, because we have a separate UI for them, not because they
    # shouldn't be shown to the user at all.
    #
    # TODO(user): Refactor the code to remove the HIDDEN label from
    # FlowRunnerArgs.output_plugins.
    args.flow.runner_args.ClearFieldsWithLabel(
        rdf_structs.SemanticDescriptor.Labels.HIDDEN,
        exceptions="output_plugins")

    client_urn = None
    if args.client_id:
      client_urn = args.client_id.ToClientURN()
    flow_id = flow.GRRFlow.StartFlow(
        client_id=client_urn,
        flow_name=flow_name,
        token=token,
        args=args.flow.args,
        runner_args=args.flow.runner_args)

    fd = aff4.FACTORY.Open(flow_id, aff4_type=flow.GRRFlow, token=token)
    return ApiFlow().InitFromAff4Object(fd, flow_id=flow_id.Basename())


class ApiCancelFlowArgs(rdf_structs.RDFProtoStruct):
  protobuf = flow_pb2.ApiCancelFlowArgs
  rdf_deps = [
      client.ApiClientId,
      ApiFlowId,
  ]


class ApiCancelFlowHandler(api_call_handler_base.ApiCallHandler):
  """Cancels given flow on a given client."""

  args_type = ApiCancelFlowArgs

  def Handle(self, args, token=None):
    flow_urn = args.flow_id.ResolveClientFlowURN(args.client_id, token=token)

    flow.GRRFlow.TerminateFlow(
        flow_urn, reason="Cancelled in GUI", token=token, force=True)


class ApiListFlowDescriptorsArgs(rdf_structs.RDFProtoStruct):
  protobuf = flow_pb2.ApiListFlowDescriptorsArgs


class ApiListFlowDescriptorsResult(rdf_structs.RDFProtoStruct):
  protobuf = flow_pb2.ApiListFlowDescriptorsResult
  rdf_deps = [
      ApiFlowDescriptor,
  ]


class ApiListFlowDescriptorsHandler(api_call_handler_base.ApiCallHandler):
  """Renders all available flows descriptors."""

  args_type = ApiListFlowDescriptorsArgs
  result_type = ApiListFlowDescriptorsResult

  client_flow_behavior = flow.FlowBehaviour("Client Flow")
  global_flow_behavior = flow.FlowBehaviour("Global Flow")

  def __init__(self, legacy_security_manager=None):
    super(ApiListFlowDescriptorsHandler, self).__init__()
    self.legacy_security_manager = legacy_security_manager

  def _FlowTypeToBehavior(self, flow_type):
    if flow_type == self.args_type.FlowType.CLIENT:
      return self.client_flow_behavior
    elif flow_type == self.args_type.FlowType.GLOBAL:
      return self.global_flow_behavior
    else:
      raise ValueError("Unexpected flow type: " + str(flow_type))

  def Handle(self, args, token=None):
    """Renders list of descriptors for all the flows."""

    result = []
    for name in sorted(flow.GRRFlow.classes.keys()):
      cls = flow.GRRFlow.classes[name]

      # Flows without a category do not show up in the GUI.
      if not getattr(cls, "category", None):
        continue

      # Only show flows that the user is allowed to start.
      can_be_started_on_client = False
      try:
        if self.legacy_security_manager:
          self.legacy_security_manager.CheckIfCanStartFlow(
              token, name, with_client_id=True)
        can_be_started_on_client = True
      except access_control.UnauthorizedAccess:
        pass

      can_be_started_globally = False
      try:
        if self.legacy_security_manager:
          self.legacy_security_manager.CheckIfCanStartFlow(
              token, name, with_client_id=False)
        can_be_started_globally = True
      except access_control.UnauthorizedAccess:
        pass

      if args.HasField("flow_type"):
        # Skip if there are behaviours that are not supported by the class.
        behavior = self._FlowTypeToBehavior(args.flow_type)
        if not behavior.IsSupported(cls.behaviours):
          continue

        if (args.flow_type == self.args_type.FlowType.CLIENT and
            not can_be_started_on_client):
          continue

        if (args.flow_type == self.args_type.FlowType.GLOBAL and
            not can_be_started_globally):
          continue
      elif not (can_be_started_on_client or can_be_started_globally):
        continue

      result.append(ApiFlowDescriptor().InitFromFlowClass(cls, token=token))

    return ApiListFlowDescriptorsResult(items=result)


class ApiGetExportedFlowResultsArgs(rdf_structs.RDFProtoStruct):
  protobuf = flow_pb2.ApiGetExportedFlowResultsArgs
  rdf_deps = [
      client.ApiClientId,
      ApiFlowId,
  ]


class ApiGetExportedFlowResultsHandler(api_call_handler_base.ApiCallHandler):
  """Exports results of a given flow with an instant output plugin."""

  args_type = ApiGetExportedFlowResultsArgs

  def Handle(self, args, token=None):
    iop_cls = instant_output_plugin.InstantOutputPlugin
    plugin_cls = iop_cls.GetPluginClassByPluginName(args.plugin_name)

    flow_urn = args.flow_id.ResolveClientFlowURN(args.client_id, token=token)

    output_collection = flow.GRRFlow.TypedResultCollectionForFID(
        flow_urn, token=token)

    plugin = plugin_cls(source_urn=flow_urn, token=token)
    content_generator = instant_output_plugin.ApplyPluginToMultiTypeCollection(
        plugin, output_collection, source_urn=args.client_id.ToClientURN())
    return api_call_handler_base.ApiBinaryStream(
        plugin.output_file_name, content_generator=content_generator)
