#!/usr/bin/env python
"""A library for tests."""


import codecs
import collections
import cProfile
import datetime
import email
import functools
import itertools
import os
import pdb
import platform
import re
import shutil
import socket
import sys
import tempfile
import time
import traceback
import types
import unittest


import mock
import pkg_resources

import logging
import unittest

from grr import config

from grr.client import actions
from grr.client import client_utils_linux
from grr.client import comms
# pylint: disable=unused-import
from grr.client import local as _
# pylint: enable=unused-import
from grr.client import vfs
from grr.client.client_actions import standard
from grr.client.components.rekall_support import rekall_types as rdf_rekall_types
from grr.client.vfs_handlers import files

from grr.lib import access_control
from grr.lib import action_mocks
from grr.lib import aff4
from grr.lib import artifact
from grr.lib import client_fixture
from grr.lib import client_index
from grr.lib import data_store
from grr.lib import email_alerts
from grr.lib import events
from grr.lib import export
from grr.lib import flags
from grr.lib import flow
from grr.lib import maintenance_utils
from grr.lib import queue_manager
from grr.lib import queues as queue_config
from grr.lib import rdfvalue
from grr.lib import registry
from grr.lib import rekall_profile_server
from grr.lib import server_stubs
from grr.lib import stats
from grr.lib import testing_startup
from grr.lib import utils
from grr.lib import worker
from grr.lib import worker_mocks

from grr.lib.aff4_objects import aff4_grr
from grr.lib.aff4_objects import filestore
from grr.lib.aff4_objects import security
from grr.lib.aff4_objects import standard as aff4_standard
from grr.lib.aff4_objects import users

# pylint: disable=unused-import
from grr.lib.blob_stores import registry_init as _

from grr.lib.data_stores import fake_data_store as _

# Importing administrative to import ClientCrashHandler flow that
# handles ClientCrash events triggered by CrashClientMock.
from grr.lib.flows.general import administrative as _

# Some tests (TestFileFinderFlow.testTreatsGlobsAsPathsWhenMemoryPathTypeIsUsed,
# possibly others, expect the auditing system to be active).
from grr.lib.flows.general import audit as _

from grr.lib.flows.general import ca_enroller
from grr.lib.flows.general import discovery
from grr.lib.flows.general import filesystem as _
# pylint: enable=unused-import

from grr.lib.hunts import results as hunts_results

# pylint: disable=unused-import
from grr.lib.local import plugins as _
# pylint: enable=unused-import

from grr.lib.rdfvalues import client as rdf_client
from grr.lib.rdfvalues import crypto as rdf_crypto
from grr.lib.rdfvalues import flows as rdf_flows
from grr.lib.rdfvalues import paths as rdf_paths
from grr.lib.rdfvalues import protodict as rdf_protodict
from grr.lib.rdfvalues import structs as rdf_structs

from grr.proto import tests_pb2

flags.DEFINE_list(
    "tests",
    None,
    help=("Test module to run. If not specified we run"
          "All modules in the test suite."))

flags.DEFINE_list("labels", ["small"],
                  "A list of test labels to run. (e.g. benchmarks,small).")

flags.DEFINE_string(
    "profile", "", "If set, the test is run using cProfile, storing the "
    "resulting profile data in the given filename. Running multiple tests"
    "will overwrite this file so --tests should be used to limit this to a"
    "single test.")


class Error(Exception):
  """Test base error."""


class ClientActionRunnerArgs(rdf_structs.RDFProtoStruct):
  protobuf = tests_pb2.ClientActionRunnerArgs


class ClientActionRunner(flow.GRRFlow):
  """Just call the specified client action directly.
  """
  args_type = ClientActionRunnerArgs
  action_args = {}

  @flow.StateHandler()
  def Start(self):
    self.CallClient(
        server_stubs.ClientActionStub.classes[self.args.action],
        next_state="End",
        **self.action_args)


class FlowWithOneClientRequest(flow.GRRFlow):
  """Test flow that does one client request in Start() state."""

  @flow.StateHandler()
  def Start(self, unused_message=None):
    self.CallClient(Test, data="test", next_state="End")


class FlowOrderTest(flow.GRRFlow):
  """Tests ordering of inbound messages."""

  def __init__(self, *args, **kwargs):
    self.messages = []
    flow.GRRFlow.__init__(self, *args, **kwargs)

  @flow.StateHandler()
  def Start(self, unused_message=None):
    self.CallClient(Test, data="test", next_state="Incoming")

  @flow.StateHandler(auth_required=True)
  def Incoming(self, responses):
    """Record the message id for testing."""
    self.messages = []

    for _ in responses:
      self.messages.append(responses.message.response_id)


class SendingFlowArgs(rdf_structs.RDFProtoStruct):
  protobuf = tests_pb2.SendingFlowArgs


class SendingFlow(flow.GRRFlow):
  """Tests sending messages to clients."""
  args_type = SendingFlowArgs

  # Flow has to have a category otherwise FullAccessControlManager won't
  # let non-supervisor users to run it at all (it will be considered
  # externally inaccessible).
  category = "/Test/"

  @flow.StateHandler()
  def Start(self, unused_response=None):
    """Just send a few messages."""
    for unused_i in range(0, self.args.message_count):
      self.CallClient(
          standard.ReadBuffer, offset=0, length=100, next_state="Process")


class RaiseOnStart(flow.GRRFlow):
  """A broken flow that raises in the Start method."""

  @flow.StateHandler()
  def Start(self, unused_message=None):
    raise Exception("Broken Start")


class BrokenFlow(flow.GRRFlow):
  """A flow which does things wrongly."""

  @flow.StateHandler()
  def Start(self, unused_response=None):
    """Send a message to an incorrect state."""
    self.CallClient(standard.ReadBuffer, next_state="WrongProcess")


class DummyFlow(flow.GRRFlow):
  """Dummy flow that does nothing."""


class FlowWithOneNestedFlow(flow.GRRFlow):
  """Flow that calls a nested flow."""

  @flow.StateHandler()
  def Start(self, unused_response=None):
    self.CallFlow(DummyFlow.__name__, next_state="Done")

  @flow.StateHandler()
  def Done(self, unused_response=None):
    pass


class DummyFlowWithSingleReply(flow.GRRFlow):
  """Just emits 1 reply."""

  @flow.StateHandler()
  def Start(self, unused_response=None):
    self.CallState(next_state="SendSomething")

  @flow.StateHandler()
  def SendSomething(self, unused_response=None):
    self.SendReply(rdfvalue.RDFString("oh"))


class DummyLogFlow(flow.GRRFlow):
  """Just emit logs."""

  @flow.StateHandler()
  def Start(self, unused_response=None):
    """Log."""
    self.Log("First")
    self.CallFlow(DummyLogFlowChild.__name__, next_state="Done")
    self.Log("Second")

  @flow.StateHandler()
  def Done(self, unused_response=None):
    self.Log("Third")
    self.Log("Fourth")


class DummyLogFlowChild(flow.GRRFlow):
  """Just emit logs."""

  @flow.StateHandler()
  def Start(self, unused_response=None):
    """Log."""
    self.Log("Uno")
    self.CallState(next_state="Done")
    self.Log("Dos")

  @flow.StateHandler()
  def Done(self, unused_response=None):
    self.Log("Tres")
    self.Log("Cuatro")


class WellKnownSessionTest(flow.WellKnownFlow):
  """Tests the well known flow implementation."""
  well_known_session_id = rdfvalue.SessionID(
      queue=rdfvalue.RDFURN("test"), flow_name="TestSessionId")

  messages = []

  def __init__(self, *args, **kwargs):
    flow.WellKnownFlow.__init__(self, *args, **kwargs)

  def ProcessMessage(self, message):
    """Record the message id for testing."""
    self.messages.append(int(message.payload))


class WellKnownSessionTest2(WellKnownSessionTest):
  """Another testing well known flow."""
  well_known_session_id = rdfvalue.SessionID(
      queue=rdfvalue.RDFURN("test"), flow_name="TestSessionId2")


# pylint: disable=g-bad-name
class MockWindowsProcess(object):
  """A mock windows process."""

  pid = 10

  def ppid(self):
    return 1

  def name(self):
    return "cmd"

  def exe(self):
    return "cmd.exe"

  def username(self):
    return "test"

  def cmdline(self):
    return ["c:\\Windows\\cmd.exe", "/?"]

  def create_time(self):
    return 1217061982.375000

  def status(self):
    return "running"

  def cwd(self):
    return "X:\\RECEP\xc3\x87\xc3\x95ES"

  def num_threads(self):
    return 1

  def cpu_times(self):
    cpu_times = collections.namedtuple(
        "CPUTimes", ["user", "system", "children_user", "children_system"])
    return cpu_times(
        user=1.0, system=1.0, children_user=1.0, children_system=1.0)

  def cpu_percent(self):
    return 10.0

  def memory_info(self):
    meminfo = collections.namedtuple("Meminfo", ["rss", "vms"])
    return meminfo(rss=100000, vms=150000)

  def memory_percent(self):
    return 10.0

  def open_files(self):
    return []

  def connections(self):
    return []

  def nice(self):
    return 10

  def as_dict(self, attrs=None):
    dic = {}
    if attrs is None:
      return dic
    for name in attrs:
      if hasattr(self, name):
        attr = getattr(self, name)
        if callable(attr):
          dic[name] = attr()
        else:
          dic[name] = attr
      else:
        dic[name] = None
    return dic


# pylint: enable=g-bad-name


class GRRBaseTest(unittest.TestCase):
  """This is the base class for all GRR tests."""

  __metaclass__ = registry.MetaclassRegistry

  # The type of this test.
  type = "normal"

  def __init__(self, methodName=None):  # pylint: disable=g-bad-name
    """Hack around unittest's stupid constructor.

    We sometimes need to instantiate the test suite without running any tests -
    e.g. to start initialization or setUp() functions. The unittest constructor
    requires to provide a valid method name.

    Args:
      methodName: The test method to run.
    """
    super(GRRBaseTest, self).__init__(methodName=methodName or "__init__")
    self.base_path = config.CONFIG["Test.data_dir"]
    test_user = "test"
    users.GRRUser.SYSTEM_USERS.add(test_user)
    self.token = access_control.ACLToken(
        username=test_user, reason="Running tests")

  def setUp(self):
    super(GRRBaseTest, self).setUp()

    tmpdir = os.environ.get("TEST_TMPDIR") or config.CONFIG["Test.tmpdir"]

    if platform.system() != "Windows":
      # Make a temporary directory for test files.
      self.temp_dir = tempfile.mkdtemp(dir=tmpdir)
    else:
      self.temp_dir = tempfile.mkdtemp()

    config.CONFIG.SetWriteBack(os.path.join(self.temp_dir, "writeback.yaml"))

    logging.info("Starting test: %s.%s", self.__class__.__name__,
                 self._testMethodName)
    self.last_start_time = time.time()

    try:
      # Clear() is much faster than init but only supported for FakeDataStore.
      data_store.DB.Clear()
    except AttributeError:
      self.InitDatastore()

    aff4.FACTORY.Flush()

    # Create a Foreman and Filestores, they are used in many tests.
    aff4_grr.GRRAFF4Init().Run()
    filestore.FileStoreInit().Run()
    hunts_results.ResultQueueInitHook().Run()

    # Stub out the email function
    self.emails_sent = []

    def SendEmailStub(to_user, from_user, subject, message, **unused_kwargs):
      self.emails_sent.append((to_user, from_user, subject, message))

    self.mail_stubber = utils.MultiStubber(
        (email_alerts.EMAIL_ALERTER, "SendEmail", SendEmailStub),
        (email.utils, "make_msgid", lambda: "<message id stub>"))
    self.mail_stubber.Start()

    self.nanny_stubber = utils.Stubber(
        client_utils_linux.NannyController,
        "StartNanny",
        lambda unresponsive_kill_period=None, nanny_logfile=None: True)
    self.nanny_stubber.Start()

  def tearDown(self):
    self.nanny_stubber.Stop()
    self.mail_stubber.Stop()

    logging.info("Completed test: %s.%s (%.4fs)", self.__class__.__name__,
                 self._testMethodName, time.time() - self.last_start_time)

    # This may fail on filesystems which do not support unicode filenames.
    try:
      shutil.rmtree(self.temp_dir, True)
    except UnicodeError:
      pass

  def shortDescription(self):  # pylint: disable=g-bad-name
    doc = self._testMethodDoc or ""
    doc = doc.split("\n")[0].strip()
    # Write the suite and test name so it can be easily copied into the --tests
    # parameter.
    return "\n%s.%s - %s\n" % (self.__class__.__name__, self._testMethodName,
                               doc)

  def _AssertRDFValuesEqual(self, x, y):
    x_has_lsf = hasattr(x, "ListSetFields")
    y_has_lsf = hasattr(y, "ListSetFields")

    if x_has_lsf != y_has_lsf:
      raise AssertionError("%s != %s" % (x, y))

    if not x_has_lsf:
      if isinstance(x, float):
        self.assertAlmostEqual(x, y)
      else:
        self.assertEqual(x, y)
      return

    processed = set()
    for desc, value in x.ListSetFields():
      processed.add(desc.name)
      self._AssertRDFValuesEqual(value, y.Get(desc.name))

    for desc, value in y.ListSetFields():
      if desc.name not in processed:
        self._AssertRDFValuesEqual(value, x.Get(desc.name))

  def assertRDFValuesEqual(self, x, y):
    """Check that two RDFStructs are equal."""
    self._AssertRDFValuesEqual(x, y)

  def assertStatsCounterDelta(self, delta, varname, fields=None):
    return StatsDeltaAssertionContext(self, delta, varname, fields=fields)

  def DoAfterTestCheck(self):
    """May be overriden by subclasses to perform checks after every test."""
    pass

  def run(self, result=None):  # pylint: disable=g-bad-name
    """Run the test case.

    This code is basically the same as the standard library, except that when
    there is an exception, the --debug flag allows us to drop into the raising
    function for interactive inspection of the test failure.

    Args:
      result: The testResult object that we will use.
    """
    if result is None:
      result = self.defaultTestResult()
    result.startTest(self)
    testMethod = getattr(  # pylint: disable=g-bad-name
        self, self._testMethodName)
    try:
      try:
        self.setUp()
      except unittest.SkipTest:
        result.addSkip(self, sys.exc_info())
        result.stopTest(self)
        return
      except:
        # Break into interactive debugger on test failure.
        if flags.FLAGS.debug:
          pdb.post_mortem()

        result.addError(self, sys.exc_info())
        # If the setup step failed we stop the entire test suite
        # immediately. This helps catch errors in the setUp() function.
        raise

      ok = False
      try:
        profile_filename = flags.FLAGS.profile
        if profile_filename:
          cProfile.runctx("testMethod()", globals(), locals(), profile_filename)
        else:
          testMethod()
          # After-test checks are performed only if the test succeeds.
          self.DoAfterTestCheck()

        ok = True
      except self.failureException:
        # Break into interactive debugger on test failure.
        if flags.FLAGS.debug:
          pdb.post_mortem()

        result.addFailure(self, sys.exc_info())
      except KeyboardInterrupt:
        raise
      except unittest.SkipTest:
        result.addSkip(self, sys.exc_info())
      except Exception:  # pylint: disable=broad-except
        # Break into interactive debugger on test failure.
        if flags.FLAGS.debug:
          pdb.post_mortem()

        result.addError(self, sys.exc_info())

      try:
        self.tearDown()
      except KeyboardInterrupt:
        raise
      except Exception:  # pylint: disable=broad-except
        # Break into interactive debugger on test failure.
        if flags.FLAGS.debug:
          pdb.post_mortem()

        result.addError(self, sys.exc_info())
        ok = False

      if ok:
        result.addSuccess(self)
    finally:
      result.stopTest(self)

  def CreateUser(self, username):
    """Creates a user."""
    user = aff4.FACTORY.Create(
        "aff4:/users/%s" % username, users.GRRUser, token=self.token.SetUID())
    user.Flush()
    return user

  def CreateAdminUser(self, username):
    """Creates a user and makes it an admin."""
    with self.CreateUser(username) as user:
      user.SetLabels("admin", owner="GRR")

  def RequestClientApproval(self, client_id, token=None, approver="approver"):
    """Create an approval request to be sent to approver."""
    flow_urn = flow.GRRFlow.StartFlow(
        client_id=client_id,
        flow_name=security.RequestClientApprovalFlow.__name__,
        reason=token.reason,
        subject_urn=rdf_client.ClientURN(client_id),
        approver=approver,
        token=token)
    flow_fd = aff4.FACTORY.Open(
        flow_urn, aff4_type=flow.GRRFlow, token=self.token)
    return flow_fd.state.approval_urn

  def GrantClientApproval(self,
                          client_id,
                          delegate,
                          reason="testing",
                          approver="approver"):
    """Grant an approval from approver to delegate.

    Args:
      client_id: ClientURN
      delegate: username string of the user receiving approval.
      reason: reason for approval request.
      approver: username string of the user granting approval.
    """
    self.CreateAdminUser(approver)

    approver_token = access_control.ACLToken(username=approver)
    flow.GRRFlow.StartFlow(
        client_id=client_id,
        flow_name=security.GrantClientApprovalFlow.__name__,
        reason=reason,
        delegate=delegate,
        subject_urn=rdf_client.ClientURN(client_id),
        token=approver_token)

  def RequestAndGrantClientApproval(self,
                                    client_id,
                                    token=None,
                                    approver="approver"):
    token = token or self.token
    approval_urn = self.RequestClientApproval(
        client_id, token=token, approver=approver)
    self.GrantClientApproval(
        client_id, token.username, reason=token.reason, approver=approver)
    return approval_urn

  def GrantHuntApproval(self, hunt_urn, token=None):
    token = token or self.token

    # Create the approval and approve it.
    flow.GRRFlow.StartFlow(
        flow_name=security.RequestHuntApprovalFlow.__name__,
        subject_urn=rdfvalue.RDFURN(hunt_urn),
        reason=token.reason,
        approver="approver",
        token=token)

    self.CreateAdminUser("approver")

    approver_token = access_control.ACLToken(username="approver")
    flow.GRRFlow.StartFlow(
        flow_name=security.GrantHuntApprovalFlow.__name__,
        subject_urn=rdfvalue.RDFURN(hunt_urn),
        reason=token.reason,
        delegate=token.username,
        token=approver_token)

  def GrantCronJobApproval(self, cron_job_urn, token=None):
    token = token or self.token

    # Create cron job approval and approve it.
    flow.GRRFlow.StartFlow(
        flow_name=security.RequestCronJobApprovalFlow.__name__,
        subject_urn=rdfvalue.RDFURN(cron_job_urn),
        reason=self.token.reason,
        approver="approver",
        token=token)

    self.CreateAdminUser("approver")

    approver_token = access_control.ACLToken(username="approver")
    flow.GRRFlow.StartFlow(
        flow_name=security.GrantCronJobApprovalFlow.__name__,
        subject_urn=rdfvalue.RDFURN(cron_job_urn),
        reason=token.reason,
        delegate=token.username,
        token=approver_token)

  def _SetupClientImpl(self,
                       client_nr,
                       index=None,
                       system=None,
                       os_version=None,
                       arch=None):
    client_id_urn = rdf_client.ClientURN("C.1%015x" % client_nr)

    with aff4.FACTORY.Create(
        client_id_urn, aff4_grr.VFSGRRClient, mode="rw",
        token=self.token) as fd:
      cert = self.ClientCertFromPrivateKey(config.CONFIG["Client.private_key"])
      fd.Set(fd.Schema.CERT, cert)

      info = fd.Schema.CLIENT_INFO()
      info.client_name = "GRR Monitor"
      fd.Set(fd.Schema.CLIENT_INFO, info)
      fd.Set(fd.Schema.PING, rdfvalue.RDFDatetime.Now())
      fd.Set(fd.Schema.HOSTNAME("Host-%x" % client_nr))
      fd.Set(fd.Schema.FQDN("Host-%x.example.com" % client_nr))
      fd.Set(
          fd.Schema.MAC_ADDRESS("aabbccddee%02x\nbbccddeeff%02x" % (client_nr,
                                                                    client_nr)))
      fd.Set(
          fd.Schema.HOST_IPS("192.168.0.%d\n2001:abcd::%x" % (client_nr,
                                                              client_nr)))

      if system:
        fd.Set(fd.Schema.SYSTEM(system))
      if os_version:
        fd.Set(fd.Schema.OS_VERSION(os_version))
      if arch:
        fd.Set(fd.Schema.ARCH(arch))

      kb = rdf_client.KnowledgeBase()
      artifact.SetCoreGRRKnowledgeBaseValues(kb, fd)
      fd.Set(fd.Schema.KNOWLEDGE_BASE, kb)

      hardware_info = fd.Schema.HARDWARE_INFO()
      hardware_info.system_manufacturer = ("System-Manufacturer-%x" % client_nr)
      hardware_info.bios_version = ("Bios-Version-%x" % client_nr)
      fd.Set(fd.Schema.HARDWARE_INFO, hardware_info)

      fd.Flush()

      index.AddClient(fd)

    return client_id_urn

  def SetupClient(self,
                  client_nr,
                  index=None,
                  system=None,
                  os_version=None,
                  arch=None):
    """Prepares a test client mock to be used.

    Args:
      client_nr: int The GRR ID to be used. 0xABCD maps to C.100000000000abcd
                     in canonical representation.
      index: client_index.ClientIndex
      system: string
      os_version: string
      arch: string

    Returns:
      rdf_client.ClientURN
    """
    if index is not None:
      # `with:' is expected to be used in the calling function.
      client_id_urn = self._SetupClientImpl(client_nr, index, system,
                                            os_version, arch)
    else:
      with client_index.CreateClientIndex(token=self.token) as index:
        client_id_urn = self._SetupClientImpl(client_nr, index, system,
                                              os_version, arch)

    return client_id_urn

  def SetupClients(self, nr_clients, system=None, os_version=None, arch=None):
    """Prepares nr_clients test client mocks to be used."""
    with client_index.CreateClientIndex(token=self.token) as index:
      client_ids = [
          self.SetupClient(client_nr, index, system, os_version, arch)
          for client_nr in xrange(nr_clients)
      ]

    return client_ids

  def DeleteClient(self, client_nr):
    """Cleans up a test client mock."""
    client_id = rdf_client.ClientURN("C.1%015x" % client_nr)
    data_store.DB.DeleteSubject(client_id, token=self.token)

  def DeleteClients(self, nr_clients):
    """Cleans up test client mocks. Analogous to .SetupClients(nr_clients) ."""
    for client_nr in xrange(nr_clients):
      self.DeleteClient(client_nr)

  def ClientCertFromPrivateKey(self, private_key):
    communicator = comms.ClientCommunicator(private_key=private_key)
    csr = communicator.GetCSR()
    return rdf_crypto.RDFX509Cert.ClientCertFromCSR(csr)

  def _SendNotification(self,
                        notification_type,
                        subject,
                        message,
                        client_id="aff4:/C.0000000000000001"):
    """Sends a notification to the current user."""
    session_id = flow.GRRFlow.StartFlow(
        client_id=client_id,
        flow_name=discovery.Interrogate.__name__,
        token=self.token)

    with aff4.FACTORY.Open(session_id, mode="rw", token=self.token) as flow_obj:
      flow_obj.Notify(notification_type, subject, message)

  def GenerateToken(self, username, reason):
    return access_control.ACLToken(username=username, reason=reason)


class EmptyActionTest(GRRBaseTest):
  """Test the client Actions."""

  __metaclass__ = registry.MetaclassRegistry

  labels = ["client_action", "small"]

  def RunAction(self, action_cls, arg=None, grr_worker=None):
    if arg is None:
      arg = rdf_flows.GrrMessage()

    self.results = []
    action = self._GetActionInstance(action_cls, grr_worker=grr_worker)

    action.status = rdf_flows.GrrStatus(
        status=rdf_flows.GrrStatus.ReturnedStatus.OK)
    action.Run(arg)

    return self.results

  def ExecuteAction(self, action_cls, arg=None, grr_worker=None):
    message = rdf_flows.GrrMessage(
        name=action_cls.__name__, payload=arg, auth_state="AUTHENTICATED")

    self.results = []
    action = self._GetActionInstance(action_cls, grr_worker=grr_worker)

    action.Execute(message)

    return self.results

  def _GetActionInstance(self, action_cls, grr_worker=None):
    """Run an action and generate responses.

    This basically emulates GRRClientWorker.HandleMessage().

    Args:
       action_cls: The action class to run.
       grr_worker: The GRRClientWorker instance to use. If not provided we make
         a new one.
    Returns:
      A list of response protobufs.
    """

    # A mock SendReply() method to collect replies.
    def MockSendReply(mock_self, reply=None, **kwargs):
      if reply is None:
        reply = mock_self.out_rdfvalues[0](**kwargs)
      self.results.append(reply)

    if grr_worker is None:
      grr_worker = worker_mocks.FakeClientWorker()

    action = action_cls(grr_worker=grr_worker)
    action.SendReply = types.MethodType(MockSendReply, action)

    return action


class FlowTestsBaseclass(GRRBaseTest):
  """The base class for all flow tests."""

  __metaclass__ = registry.MetaclassRegistry

  def setUp(self):
    GRRBaseTest.setUp(self)
    client_ids = self.SetupClients(1)
    self.client_id = client_ids[0]

  def FlowSetup(self, name, client_id_urn=None):
    if client_id_urn is None:
      client_id_urn = self.client_id

    session_id = flow.GRRFlow.StartFlow(
        client_id=client_id_urn, flow_name=name, token=self.token)

    return aff4.FACTORY.Open(session_id, mode="rw", token=self.token)


class ConfigOverrider(object):
  """A context to temporarily change config options."""

  def __init__(self, overrides):
    self._overrides = overrides
    self._saved_values = {}

  def __enter__(self):
    self.Start()

  def Start(self):
    for k, v in self._overrides.iteritems():
      self._saved_values[k] = config.CONFIG.Get(k)
      try:
        config.CONFIG.Set.old_target(k, v)
      except AttributeError:
        config.CONFIG.Set(k, v)

  def __exit__(self, unused_type, unused_value, unused_traceback):
    self.Stop()

  def Stop(self):
    for k, v in self._saved_values.iteritems():
      try:
        config.CONFIG.Set.old_target(k, v)
      except AttributeError:
        config.CONFIG.Set(k, v)


class PreserveConfig(object):

  def __enter__(self):
    self.Start()

  def Start(self):
    self.old_config = config.CONFIG
    config.CONFIG = self.old_config.MakeNewConfig()
    config.CONFIG.initialized = self.old_config.initialized
    config.CONFIG.SetWriteBack(self.old_config.writeback.filename)
    config.CONFIG.raw_data = self.old_config.raw_data.copy()
    config.CONFIG.writeback_data = self.old_config.writeback_data.copy()

  def __exit__(self, unused_type, unused_value, unused_traceback):
    self.Stop()

  def Stop(self):
    config.CONFIG = self.old_config


class VFSOverrider(object):
  """A context to temporarily change VFS handlers."""

  def __init__(self, vfs_type, temp_handler):
    self._vfs_type = vfs_type
    self._temp_handler = temp_handler

  def __enter__(self):
    self.Start()

  def Start(self):
    self._old_handler = vfs.VFS_HANDLERS.get(self._vfs_type)
    vfs.VFS_HANDLERS[self._vfs_type] = self._temp_handler

  def __exit__(self, unused_type, unused_value, unused_traceback):
    self.Stop()

  def Stop(self):
    if self._old_handler:
      vfs.VFS_HANDLERS[self._vfs_type] = self._old_handler
    else:
      del vfs.VFS_HANDLERS[self._vfs_type]


class FakeTime(object):
  """A context manager for faking time."""

  def __init__(self, fake_time, increment=0):
    if isinstance(fake_time, rdfvalue.RDFDatetime):
      self.time = fake_time.AsSecondsFromEpoch()
    else:
      self.time = fake_time
    self.increment = increment

  def __enter__(self):
    self.old_time = time.time

    def Time():
      self.time += self.increment
      return self.time

    time.time = Time

    self.old_strftime = time.strftime

    def Strftime(form, t=time.localtime(Time())):
      return self.old_strftime(form, t)

    time.strftime = Strftime

    return self

  def __exit__(self, unused_type, unused_value, unused_traceback):
    time.time = self.old_time
    time.strftime = self.old_strftime


class FakeDateTimeUTC(object):
  """A context manager for faking time when using datetime.utcnow."""

  def __init__(self, fake_time, increment=0):
    self.time = fake_time
    self.increment = increment

  def __enter__(self):
    self.old_datetime = datetime.datetime

    class FakeDateTime(object):

      def __init__(self, time_val, increment, orig_datetime):
        self.time = time_val
        self.increment = increment
        self.orig_datetime = orig_datetime

      def __call__(self, *args, **kw):
        return self.orig_datetime(*args, **kw)

      def __getattribute__(self, name):
        try:
          return object.__getattribute__(self, name)
        except AttributeError:
          return getattr(self.orig_datetime, name)

      def utcnow(self):  # pylint: disable=invalid-name
        self.time += self.increment
        return self.orig_datetime.utcfromtimestamp(self.time)

    datetime.datetime = FakeDateTime(self.time, self.increment,
                                     self.old_datetime)

  def __exit__(self, unused_type, unused_value, unused_traceback):
    datetime.datetime = self.old_datetime


class Instrument(object):
  """A helper to instrument a function call.

  Stores a copy of all function call args locally for later inspection.
  """

  def __init__(self, module, target_name):
    self.old_target = getattr(module, target_name)

    def Wrapper(*args, **kwargs):
      self.args.append(args)
      self.kwargs.append(kwargs)
      self.call_count += 1
      return self.old_target(*args, **kwargs)

    self.stubber = utils.Stubber(module, target_name, Wrapper)
    self.args = []
    self.kwargs = []
    self.call_count = 0

  def __enter__(self):
    self.stubber.__enter__()
    return self

  def __exit__(self, t, value, tb):
    return self.stubber.__exit__(t, value, tb)


class StatsDeltaAssertionContext(object):
  """A context manager to check the stats variable changes."""

  def __init__(self, test, delta, varname, fields=None):
    self.test = test
    self.varname = varname
    self.fields = fields
    self.delta = delta

  def __enter__(self):
    self.prev_count = stats.STATS.GetMetricValue(
        self.varname, fields=self.fields)
    # Handle the case when we're dealing with distributions.
    if hasattr(self.prev_count, "count"):
      self.prev_count = self.prev_count.count

  def __exit__(self, unused_type, unused_value, unused_traceback):
    new_count = stats.STATS.GetMetricValue(
        varname=self.varname, fields=self.fields)
    if hasattr(new_count, "count"):
      new_count = new_count.count

    self.test.assertEqual(new_count - self.prev_count, self.delta,
                          "%s (fields=%s) expected to change with delta=%d" %
                          (self.varname, self.fields, self.delta))


class AFF4ObjectTest(GRRBaseTest):
  """The base class of all aff4 object tests."""
  __metaclass__ = registry.MetaclassRegistry

  client_id = rdf_client.ClientURN("C." + "B" * 16)


class MicroBenchmarks(GRRBaseTest):
  """This base class created the GRR benchmarks."""
  __metaclass__ = registry.MetaclassRegistry
  labels = ["large"]

  units = "us"

  def setUp(self, extra_fields=None, extra_format=None):
    super(MicroBenchmarks, self).setUp()

    if extra_fields is None:
      extra_fields = []
    if extra_format is None:
      extra_format = []

    base_scratchpad_fields = ["Benchmark", "Time (%s)", "Iterations"]
    scratchpad_fields = base_scratchpad_fields + extra_fields
    # Create format string for displaying benchmark results.
    initial_fmt = ["45", "<20", "<20"] + extra_format
    self.scratchpad_fmt = " ".join([("{%d:%s}" % (ind, x))
                                    for ind, x in enumerate(initial_fmt)])
    # We use this to store temporary benchmark results.
    self.scratchpad = [
        scratchpad_fields, ["-" * len(x) for x in scratchpad_fields]
    ]

  def tearDown(self):
    super(MicroBenchmarks, self).tearDown()
    f = 1
    if self.units == "us":
      f = 1e6
    elif self.units == "ms":
      f = 1e3
    if len(self.scratchpad) > 2:
      print "\nRunning benchmark %s: %s" % (self._testMethodName,
                                            self._testMethodDoc or "")

      for row in self.scratchpad:
        if isinstance(row[1], (int, float)):
          row[1] = "%10.4f" % (row[1] * f)
        elif "%" in row[1]:
          row[1] %= self.units

        print self.scratchpad_fmt.format(*row)
      print

  def AddResult(self, name, time_taken, repetitions, *extra_values):
    logging.info("%s: %s (%s)", name, time_taken, repetitions)
    self.scratchpad.append([name, time_taken, repetitions] + list(extra_values))


class AverageMicroBenchmarks(MicroBenchmarks):
  """A MicroBenchmark subclass for tests that need to compute averages."""

  # Increase this for more accurate timing information.
  REPEATS = 1000
  units = "s"

  def setUp(self):
    super(AverageMicroBenchmarks, self).setUp(["Value"])

  def TimeIt(self, callback, name=None, repetitions=None, pre=None, **kwargs):
    """Runs the callback repetitively and returns the average time."""
    if repetitions is None:
      repetitions = self.REPEATS

    if name is None:
      name = callback.__name__

    if pre is not None:
      pre()

    start = time.time()
    for _ in xrange(repetitions):
      return_value = callback(**kwargs)

    time_taken = (time.time() - start) / repetitions
    self.AddResult(name, time_taken, repetitions, return_value)


class GRRTestLoader(unittest.TestLoader):
  """A test suite loader which searches for tests in all the plugins."""

  # This should be overridden by derived classes. We load all tests extending
  # this class.
  base_class = None

  def __init__(self, labels=None):
    super(GRRTestLoader, self).__init__()
    if labels is None:
      labels = set(flags.FLAGS.labels)

    self.labels = set(labels)

  def getTestCaseNames(self, testCaseClass):
    """Filter the test methods according to the labels they have."""
    result = []
    for test_name in super(GRRTestLoader, self).getTestCaseNames(testCaseClass):
      test_method = getattr(testCaseClass, test_name)
      # If the method is not tagged, it will be labeled "small".
      test_labels = getattr(test_method, "labels", set(["small"]))
      if self.labels and not self.labels.intersection(test_labels):
        continue

      result.append(test_name)

    return result

  def loadTestsFromModule(self, _):
    """Just return all the tests as if they were in the same module."""
    test_cases = [
        self.loadTestsFromTestCase(x) for x in self.base_class.classes.values()
        if issubclass(x, self.base_class)
    ]

    return self.suiteClass(test_cases)

  def loadTestsFromName(self, name, module=None):
    """Load the tests named."""
    parts = name.split(".")
    try:
      test_cases = self.loadTestsFromTestCase(self.base_class.classes[parts[0]])
    except KeyError:
      raise RuntimeError("Unable to find test %r - is it registered?" % name)

    # Specifies the whole test suite.
    if len(parts) == 1:
      return self.suiteClass(test_cases)
    elif len(parts) == 2:
      cls = self.base_class.classes[parts[0]]
      return unittest.TestSuite([cls(parts[1])])


class MockClient(object):
  """Simple emulation of the client.

  This implementation operates directly on the server's queue of client
  messages, bypassing the need to actually send the messages through the comms
  library.
  """

  def __init__(self, client_id, client_mock, token=None):
    if not isinstance(client_id, rdf_client.ClientURN):
      raise RuntimeError("Client id must be an instance of ClientURN")

    if client_mock is None:
      client_mock = action_mocks.InvalidActionMock()

    self.status_message_enforced = getattr(client_mock,
                                           "STATUS_MESSAGE_ENFORCED", True)
    self._mock_task_queue = getattr(client_mock, "mock_task_queue", [])
    self.client_id = client_id
    self.client_mock = client_mock
    self.token = token

    # Well known flows are run on the front end.
    self.well_known_flows = flow.WellKnownFlow.GetAllWellKnownFlows(token=token)
    self.user_cpu_usage = []
    self.system_cpu_usage = []
    self.network_usage = []

  def EnableResourceUsage(self,
                          user_cpu_usage=None,
                          system_cpu_usage=None,
                          network_usage=None):
    if user_cpu_usage:
      self.user_cpu_usage = itertools.cycle(user_cpu_usage)
    if system_cpu_usage:
      self.system_cpu_usage = itertools.cycle(system_cpu_usage)
    if network_usage:
      self.network_usage = itertools.cycle(network_usage)

  def AddResourceUsage(self, status):
    if self.user_cpu_usage or self.system_cpu_usage:
      status.cpu_time_used = rdf_client.CpuSeconds(
          user_cpu_time=self.user_cpu_usage.next(),
          system_cpu_time=self.system_cpu_usage.next())
    if self.network_usage:
      status.network_bytes_sent = self.network_usage.next()

  def PushToStateQueue(self, manager, message, **kw):
    # Assume the client is authorized
    message.auth_state = rdf_flows.GrrMessage.AuthorizationState.AUTHENTICATED

    # Update kw args
    for k, v in kw.items():
      setattr(message, k, v)

    # Handle well known flows
    if message.request_id == 0:

      # Well known flows only accept messages of type MESSAGE.
      if message.type == rdf_flows.GrrMessage.Type.MESSAGE:
        # Assume the message is authenticated and comes from this client.
        message.source = self.client_id

        message.auth_state = "AUTHENTICATED"

        session_id = message.session_id
        if session_id:
          logging.info("Running well known flow: %s", session_id)
          self.well_known_flows[session_id.FlowName()].ProcessMessage(message)

      return

    manager.QueueResponse(message)

  def Next(self):
    # Grab tasks for us from the server's queue.
    with queue_manager.QueueManager(token=self.token) as manager:
      request_tasks = manager.QueryAndOwn(
          self.client_id.Queue(), limit=1, lease_seconds=10000)

      request_tasks.extend(self._mock_task_queue)
      self._mock_task_queue[:] = []  # Clear the referenced list.

      for message in request_tasks:
        status = None
        response_id = 1

        # Collect all responses for this message from the client mock
        try:
          if hasattr(self.client_mock, "HandleMessage"):
            responses = self.client_mock.HandleMessage(message)
          else:
            self.client_mock.message = message
            responses = getattr(self.client_mock, message.name)(message.payload)

          if not responses:
            responses = []

          logging.info("Called client action %s generating %s responses",
                       message.name, len(responses) + 1)

          if self.status_message_enforced:
            status = rdf_flows.GrrStatus()
        except Exception as e:  # pylint: disable=broad-except
          logging.exception("Error %s occurred in client", e)

          # Error occurred.
          responses = []
          if self.status_message_enforced:
            error_message = str(e)
            status = rdf_flows.GrrStatus(
                status=rdf_flows.GrrStatus.ReturnedStatus.GENERIC_ERROR)
            # Invalid action mock is usually expected.
            if error_message != "Invalid Action Mock.":
              status.backtrace = traceback.format_exc()
              status.error_message = error_message

        # Now insert those on the flow state queue
        for response in responses:
          if isinstance(response, rdf_flows.GrrStatus):
            msg_type = rdf_flows.GrrMessage.Type.STATUS
            self.AddResourceUsage(response)
            response = rdf_flows.GrrMessage(
                session_id=message.session_id,
                name=message.name,
                response_id=response_id,
                request_id=message.request_id,
                payload=response,
                type=msg_type)
          elif isinstance(response, rdf_client.Iterator):
            msg_type = rdf_flows.GrrMessage.Type.ITERATOR
            response = rdf_flows.GrrMessage(
                session_id=message.session_id,
                name=message.name,
                response_id=response_id,
                request_id=message.request_id,
                payload=response,
                type=msg_type)
          elif not isinstance(response, rdf_flows.GrrMessage):
            msg_type = rdf_flows.GrrMessage.Type.MESSAGE
            response = rdf_flows.GrrMessage(
                session_id=message.session_id,
                name=message.name,
                response_id=response_id,
                request_id=message.request_id,
                payload=response,
                type=msg_type)

          # Next expected response
          response_id = response.response_id + 1
          self.PushToStateQueue(manager, response)

        # Status may only be None if the client reported itself as crashed.
        if status is not None:
          self.AddResourceUsage(status)
          self.PushToStateQueue(
              manager,
              message,
              response_id=response_id,
              payload=status,
              type=rdf_flows.GrrMessage.Type.STATUS)
        else:
          # Status may be None only if status_message_enforced is False.
          if self.status_message_enforced:
            raise RuntimeError("status message can only be None when "
                               "status_message_enforced is False")

        # Additionally schedule a task for the worker
        manager.QueueNotification(
            session_id=message.session_id, priority=message.priority)

      return len(request_tasks)


class MockThreadPool(object):
  """A mock thread pool which runs all jobs serially."""

  def __init__(self, *_):
    pass

  def AddTask(self, target, args, name="Unnamed task"):
    _ = name
    try:
      target(*args)
      # The real threadpool can not raise from a task. We emulate this here.
    except Exception as e:  # pylint: disable=broad-except
      logging.exception("Thread worker raised %s", e)

  def Join(self):
    pass


class MockWorker(worker.GRRWorker):
  """Mock the worker."""

  # Resource accounting off by default, set these arrays to emulate CPU and
  # network usage.
  USER_CPU = [0]
  SYSTEM_CPU = [0]
  NETWORK_BYTES = [0]

  def __init__(self,
               queues=queue_config.WORKER_LIST,
               check_flow_errors=True,
               token=None):
    self.queues = queues
    self.check_flow_errors = check_flow_errors
    self.token = token

    self.pool = MockThreadPool("MockWorker_pool", 25)

    # Collect all the well known flows.
    self.well_known_flows = flow.WellKnownFlow.GetAllWellKnownFlows(token=token)

    # Simple generators to emulate CPU and network usage
    self.cpu_user = itertools.cycle(self.USER_CPU)
    self.cpu_system = itertools.cycle(self.SYSTEM_CPU)
    self.network_bytes = itertools.cycle(self.NETWORK_BYTES)

  def Simulate(self):
    while self.Next():
      pass

    self.pool.Join()

  def Next(self):
    """Very simple emulator of the worker.

    We wake each flow in turn and run it.

    Returns:
      total number of flows still alive.

    Raises:
      RuntimeError: if the flow terminates with an error.
    """
    with queue_manager.QueueManager(token=self.token) as manager:
      run_sessions = []
      for queue in self.queues:
        notifications_available = manager.GetNotificationsForAllShards(queue)
        # Run all the flows until they are finished

        # Only sample one session at the time to force serialization of flows
        # after each state run.
        for notification in notifications_available[:1]:
          session_id = notification.session_id
          manager.DeleteNotification(session_id, end=notification.timestamp)
          run_sessions.append(session_id)

          # Handle well known flows here.
          flow_name = session_id.FlowName()
          if flow_name in self.well_known_flows:
            well_known_flow = self.well_known_flows[flow_name]
            with well_known_flow:
              responses = well_known_flow.FetchAndRemoveRequestsAndResponses(
                  well_known_flow.well_known_session_id)
            well_known_flow.ProcessResponses(responses, self.pool)
            continue

          with aff4.FACTORY.OpenWithLock(
              session_id, token=self.token, blocking=False) as flow_obj:

            # Run it
            runner = flow_obj.GetRunner()
            cpu_used = runner.context.client_resources.cpu_usage
            user_cpu = self.cpu_user.next()
            system_cpu = self.cpu_system.next()
            network_bytes = self.network_bytes.next()
            cpu_used.user_cpu_time += user_cpu
            cpu_used.system_cpu_time += system_cpu
            runner.context.network_bytes_sent += network_bytes
            runner.ProcessCompletedRequests(notification, self.pool)

            if (self.check_flow_errors and
                isinstance(flow_obj, flow.GRRFlow) and
                runner.context.state == rdf_flows.FlowContext.State.ERROR):
              logging.exception("Flow terminated in state %s with an error: %s",
                                runner.context.current_state,
                                runner.context.backtrace)
              raise RuntimeError(runner.context.backtrace)

    return run_sessions


class Popen(object):
  """A mock object for subprocess.Popen."""

  def __init__(self, run, stdout, stderr, stdin, env=None, cwd=None):
    del env, cwd  # Unused.
    Popen.running_args = run
    Popen.stdout = stdout
    Popen.stderr = stderr
    Popen.stdin = stdin
    Popen.returncode = 0

    try:
      # Store the content of the executable file.
      Popen.binary = open(run[0], "rb").read()
    except IOError:
      Popen.binary = None

  def communicate(self):  # pylint: disable=g-bad-name
    return "stdout here", "stderr here"


class Test(server_stubs.ClientActionStub):
  """A test action which can be used in mocks."""
  in_rdfvalue = rdf_protodict.DataBlob
  out_rdfvalues = [rdf_protodict.DataBlob]


def CheckFlowErrors(total_flows, token=None):
  # Check that all the flows are complete.
  for session_id in total_flows:
    try:
      flow_obj = aff4.FACTORY.Open(
          session_id, aff4_type=flow.GRRFlow, mode="r", token=token)
    except IOError:
      continue

    if flow_obj.context.state != rdf_flows.FlowContext.State.TERMINATED:
      if flags.FLAGS.debug:
        pdb.set_trace()
      raise RuntimeError("Flow %s completed in state %s" %
                         (flow_obj.runner_args.flow_name,
                          flow_obj.context.state))


def TestFlowHelper(flow_urn_or_cls_name,
                   client_mock=None,
                   client_id=None,
                   check_flow_errors=True,
                   token=None,
                   notification_event=None,
                   sync=True,
                   **kwargs):
  """Build a full test harness: client - worker + start flow.

  Args:
    flow_urn_or_cls_name: RDFURN pointing to existing flow (in this case the
                          given flow will be run) or flow class name (in this
                          case flow of the given class will be created and run).
    client_mock: Client mock object.
    client_id: Client id of an emulated client.
    check_flow_errors: If True, TestFlowHelper will raise on errors during flow
                       execution.
    token: Security token.
    notification_event: A well known flow session_id of an event listener. Event
                        will be published once the flow finishes.
    sync: Whether StartFlow call should be synchronous or not.
    **kwargs: Arbitrary args that will be passed to flow.GRRFlow.StartFlow().
  Yields:
    The caller should iterate over the generator to get all the flows
    and subflows executed.
  """
  if client_id or client_mock:
    client_mock = MockClient(client_id, client_mock, token=token)

  worker_mock = MockWorker(check_flow_errors=check_flow_errors, token=token)

  if isinstance(flow_urn_or_cls_name, rdfvalue.RDFURN):
    session_id = flow_urn_or_cls_name
  else:
    # Instantiate the flow:
    session_id = flow.GRRFlow.StartFlow(
        client_id=client_id,
        flow_name=flow_urn_or_cls_name,
        notification_event=notification_event,
        sync=sync,
        token=token,
        **kwargs)

  total_flows = set()
  total_flows.add(session_id)

  # Run the client and worker until nothing changes any more.
  while True:
    if client_mock:
      client_processed = client_mock.Next()
    else:
      client_processed = 0

    flows_run = []
    for flow_run in worker_mock.Next():
      total_flows.add(flow_run)
      flows_run.append(flow_run)

    if client_processed == 0 and not flows_run:
      break

    yield session_id

  # We should check for flow errors:
  if check_flow_errors:
    CheckFlowErrors(total_flows, token=token)


class CrashClientMock(object):

  STATUS_MESSAGE_ENFORCED = False

  def __init__(self, client_id, token):
    self.client_id = client_id
    self.token = token

  def HandleMessage(self, message):

    crash_details = rdf_client.ClientCrash(
        client_id=self.client_id,
        session_id=message.session_id,
        crash_message="Client killed during transaction",
        timestamp=rdfvalue.RDFDatetime.Now())

    msg = rdf_flows.GrrMessage(
        payload=crash_details,
        source=self.client_id,
        auth_state=rdf_flows.GrrMessage.AuthorizationState.AUTHENTICATED)

    self.flow_id = message.session_id
    # This is normally done by the FrontEnd when a CLIENT_KILLED message is
    # received.
    events.Events.PublishEvent("ClientCrash", msg, token=self.token)


class SampleHuntMock(object):

  def __init__(self, failrate=2, data="Hello World!"):
    self.responses = 0
    self.data = data
    self.failrate = failrate
    self.count = 0

  def StatFile(self, args):
    return self._StatFile(args)

  def _StatFile(self, args):
    req = rdf_client.ListDirRequest(args)

    response = rdf_client.StatEntry(
        pathspec=req.pathspec,
        st_mode=33184,
        st_ino=1063090,
        st_dev=64512L,
        st_nlink=1,
        st_uid=139592,
        st_gid=5000,
        st_size=len(self.data),
        st_atime=1336469177,
        st_mtime=1336129892,
        st_ctime=1336129892)

    self.responses += 1
    self.count += 1

    # Create status message to report sample resource usage
    status = rdf_flows.GrrStatus(status=rdf_flows.GrrStatus.ReturnedStatus.OK)
    status.cpu_time_used.user_cpu_time = self.responses
    status.cpu_time_used.system_cpu_time = self.responses * 2
    status.network_bytes_sent = self.responses * 3

    # Every "failrate" client does not have this file.
    if self.count == self.failrate:
      self.count = 0
      return [status]

    return [response, status]

  def TransferBuffer(self, args):
    response = rdf_client.BufferReference(args)

    offset = min(args.offset, len(self.data))
    response.data = self.data[offset:]
    response.length = len(self.data[offset:])
    return [response]


def TestHuntHelperWithMultipleMocks(client_mocks,
                                    check_flow_errors=False,
                                    token=None,
                                    iteration_limit=None):
  """Runs a hunt with a given set of clients mocks.

  Args:
    client_mocks: Dictionary of (client_id->client_mock) pairs. Client mock
        objects are used to handle client actions. Methods names of a client
        mock object correspond to client actions names. For an example of a
        client mock object, see SampleHuntMock.
    check_flow_errors: If True, raises when one of hunt-initiated flows fails.
    token: An instance of access_control.ACLToken security token.
    iteration_limit: If None, hunt will run until it's finished. Otherwise,
        worker_mock.Next() will be called iteration_limit number of tiems.
        Every iteration processes worker's message queue. If new messages
        are sent to the queue during the iteration processing, they will
        be processed on next iteration,
  """

  total_flows = set()

  # Worker always runs with absolute privileges, therefore making the token
  # SetUID().
  token = token.SetUID()

  client_mocks = [
      MockClient(client_id, client_mock, token=token)
      for client_id, client_mock in client_mocks.iteritems()
  ]
  worker_mock = MockWorker(check_flow_errors=check_flow_errors, token=token)

  # Run the clients and worker until nothing changes any more.
  while iteration_limit is None or iteration_limit > 0:
    client_processed = 0

    for client_mock in client_mocks:
      client_processed += client_mock.Next()

    flows_run = []

    for flow_run in worker_mock.Next():
      total_flows.add(flow_run)
      flows_run.append(flow_run)

    if client_processed == 0 and not flows_run:
      break

    if iteration_limit:
      iteration_limit -= 1

  if check_flow_errors:
    CheckFlowErrors(total_flows, token=token)


def TestHuntHelper(client_mock,
                   client_ids,
                   check_flow_errors=False,
                   token=None,
                   iteration_limit=None):
  """Runs a hunt with a given client mock on given clients.

  Args:
    client_mock: Client mock objects are used to handle client actions.
        Methods names of a client mock object correspond to client actions
        names. For an example of a client mock object, see SampleHuntMock.
    client_ids: List of clients ids. Hunt will run on these clients.
        client_mock will be used for every client id.
    check_flow_errors: If True, raises when one of hunt-initiated flows fails.
    token: An instance of access_control.ACLToken security token.
    iteration_limit: If None, hunt will run until it's finished. Otherwise,
        worker_mock.Next() will be called iteration_limit number of tiems.
        Every iteration processes worker's message queue. If new messages
        are sent to the queue during the iteration processing, they will
        be processed on next iteration.
  """
  TestHuntHelperWithMultipleMocks(
      dict([(client_id, client_mock) for client_id in client_ids]),
      check_flow_errors=check_flow_errors,
      iteration_limit=iteration_limit,
      token=token)


# Make the fixture appear to be 1 week old.
FIXTURE_TIME = rdfvalue.RDFDatetime.Now() - rdfvalue.Duration("8d")


def FilterFixture(fixture=None, regex="."):
  """Returns a sub fixture by only returning objects which match the regex."""
  result = []
  regex = re.compile(regex)

  if fixture is None:
    fixture = client_fixture.VFS

  for path, attributes in fixture:
    if regex.match(path):
      result.append((path, attributes))

  return result


def RequiresPackage(package_name):
  """Skip this test if required package isn't present.

  Note this will only work in opensource testing where we actually have
  packages.

  Args:
    package_name: string
  Returns:
    Decorator function
  """

  def Decorator(test_function):

    @functools.wraps(test_function)
    def Wrapper(*args, **kwargs):
      try:
        pkg_resources.get_distribution(package_name)
      except pkg_resources.DistributionNotFound:
        raise unittest.SkipTest(
            "Skipping, package %s not installed" % package_name)
      return test_function(*args, **kwargs)

    return Wrapper

  return Decorator


def SetLabel(*labels):
  """Sets a label on a function so we can run tests with different types."""

  def Decorator(f):
    # If the method is not already tagged, we replace its label (the default
    # label is "small").
    function_labels = getattr(f, "labels", set())
    f.labels = function_labels.union(set(labels))

    return f

  return Decorator


class ClientFixture(object):
  """A tool to create a client fixture.

  This will populate the AFF4 object tree in the data store with a mock client
  filesystem, including various objects. This allows us to test various
  end-to-end aspects (e.g. GUI).
  """

  def __init__(self, client_id, token=None, fixture=None, age=None, **kwargs):
    """Constructor.

    Args:
      client_id: The unique id for the new client.
      token: An instance of access_control.ACLToken security token.
      fixture: An optional fixture to install. If not provided we use
        client_fixture.VFS.
      age: Create the fixture at this timestamp. If None we use FIXTURE_TIME.

      **kwargs: Any other parameters which need to be interpolated by the
        fixture.
    """
    self.args = kwargs
    self.token = token
    self.age = age or FIXTURE_TIME.AsSecondsFromEpoch()
    self.client_id = rdf_client.ClientURN(client_id)
    self.args["client_id"] = self.client_id.Basename()
    self.args["age"] = self.age
    self.CreateClientObject(fixture or client_fixture.VFS)

  def CreateClientObject(self, vfs_fixture):
    """Make a new client object."""

    # First remove the old fixture just in case its still there.
    aff4.FACTORY.Delete(self.client_id, token=self.token)

    # Create the fixture at a fixed time.
    with FakeTime(self.age):
      for path, (aff4_type, attributes) in vfs_fixture:
        path %= self.args

        aff4_object = aff4.FACTORY.Create(
            self.client_id.Add(path), aff4_type, mode="rw", token=self.token)

        for attribute_name, value in attributes.items():
          attribute = aff4.Attribute.PREDICATES[attribute_name]
          if isinstance(value, (str, unicode)):
            # Interpolate the value
            value %= self.args

          # Is this supposed to be an RDFValue array?
          if aff4.issubclass(attribute.attribute_type,
                             rdf_protodict.RDFValueArray):
            rdfvalue_object = attribute()
            for item in value:
              new_object = rdfvalue_object.rdf_type.FromTextFormat(
                  utils.SmartStr(item))
              rdfvalue_object.Append(new_object)

          # It is a text serialized protobuf.
          elif aff4.issubclass(attribute.attribute_type,
                               rdf_structs.RDFProtoStruct):
            # Use the alternate constructor - we always write protobufs in
            # textual form:
            rdfvalue_object = attribute.attribute_type.FromTextFormat(
                utils.SmartStr(value))

          elif aff4.issubclass(attribute.attribute_type, rdfvalue.RDFInteger):
            rdfvalue_object = attribute(int(value))
          else:
            rdfvalue_object = attribute(value)

          # If we don't already have a pathspec, try and get one from the stat.
          if aff4_object.Get(aff4_object.Schema.PATHSPEC) is None:
            # If the attribute was a stat, it has a pathspec nested in it.
            # We should add that pathspec as an attribute.
            if attribute.attribute_type == rdf_client.StatEntry:
              stat_object = attribute.attribute_type.FromTextFormat(
                  utils.SmartStr(value))
              if stat_object.pathspec:
                pathspec_attribute = aff4.Attribute(
                    "aff4:pathspec", rdf_paths.PathSpec,
                    "The pathspec used to retrieve "
                    "this object from the client.", "pathspec")
                aff4_object.AddAttribute(pathspec_attribute,
                                         stat_object.pathspec)

          if attribute in ["aff4:content", "aff4:content"]:
            # For AFF4MemoryStreams we need to call Write() instead of
            # directly setting the contents..
            aff4_object.Write(rdfvalue_object)
          else:
            aff4_object.AddAttribute(attribute, rdfvalue_object)

        # Populate the KB from the client attributes.
        if aff4_type == aff4_grr.VFSGRRClient:
          kb = rdf_client.KnowledgeBase()
          artifact.SetCoreGRRKnowledgeBaseValues(kb, aff4_object)
          aff4_object.Set(aff4_object.Schema.KNOWLEDGE_BASE, kb)

        # Make sure we do not actually close the object here - we only want to
        # sync back its attributes, not run any finalization code.
        aff4_object.Flush()
        if aff4_type == aff4_grr.VFSGRRClient:
          index = client_index.CreateClientIndex(token=self.token)
          index.AddClient(aff4_object)


class ClientVFSHandlerFixtureBase(vfs.VFSHandler):
  """A base class for VFSHandlerFixtures."""

  def ListNames(self):
    for stat in self.ListFiles():
      yield os.path.basename(stat.pathspec.path)

  def IsDirectory(self):
    return bool(self.ListFiles())

  def _FakeDirStat(self):
    # We return some fake data, this makes writing tests easier for some
    # things but we give an error to the tester as it is often not what you
    # want.
    logging.warn("Fake value for %s under %s", self.path, self.prefix)
    return rdf_client.StatEntry(
        pathspec=self.pathspec,
        st_mode=16877,
        st_size=12288,
        st_atime=1319796280,
        st_dev=1)


class ClientVFSHandlerFixture(ClientVFSHandlerFixtureBase):
  """A client side VFS handler for the OS type - returns the fixture."""
  # A class wide cache for fixtures. Key is the prefix, and value is the
  # compiled fixture.
  cache = {}

  paths = None
  supported_pathtype = rdf_paths.PathSpec.PathType.OS

  # Do not auto-register.
  auto_register = False

  # Everything below this prefix is emulated
  prefix = "/fs/os"

  def __init__(self,
               base_fd=None,
               prefix=None,
               pathspec=None,
               progress_callback=None,
               full_pathspec=None):
    super(ClientVFSHandlerFixture, self).__init__(
        base_fd,
        pathspec=pathspec,
        progress_callback=progress_callback,
        full_pathspec=full_pathspec)

    self.prefix = self.prefix or prefix
    self.pathspec.Append(pathspec)
    self.path = self.pathspec.CollapsePath()
    self.paths = self.cache.get(self.prefix)

    self.PopulateCache()

  def PopulateCache(self):
    """Parse the paths from the fixture."""
    if self.paths:
      return

    # The cache is attached to the class so it can be shared by all instance.
    self.paths = self.__class__.cache[self.prefix] = {}
    for path, (vfs_type, attributes) in client_fixture.VFS:
      if not path.startswith(self.prefix):
        continue

      path = utils.NormalizePath(path[len(self.prefix):])
      if path == "/":
        continue

      stat = rdf_client.StatEntry()
      args = {"client_id": "C.1234"}
      attrs = attributes.get("aff4:stat")

      if attrs:
        attrs %= args  # Remove any %% and interpolate client_id.
        stat = rdf_client.StatEntry.FromTextFormat(utils.SmartStr(attrs))

      stat.pathspec = rdf_paths.PathSpec(
          pathtype=self.supported_pathtype, path=path)

      # TODO(user): Once we add tests around not crossing device boundaries,
      # we need to be smarter here, especially for the root entry.
      stat.st_dev = 1
      path = self._NormalizeCaseForPath(path, vfs_type)
      self.paths[path] = (vfs_type, stat)

    self.BuildIntermediateDirectories()

  def _NormalizeCaseForPath(self, path, vfs_type):
    """Handle casing differences for different filesystems."""
    # Special handling for case sensitivity of registry keys.
    # This mimicks the behavior of the operating system.
    if self.supported_pathtype == rdf_paths.PathSpec.PathType.REGISTRY:
      self.path = self.path.replace("\\", "/")
      parts = path.split("/")
      if vfs_type == aff4_grr.VFSFile:
        # If its a file, the last component is a value which is case sensitive.
        lower_parts = [x.lower() for x in parts[0:-1]]
        lower_parts.append(parts[-1])
        path = utils.Join(*lower_parts)
      else:
        path = utils.Join(*[x.lower() for x in parts])
    return path

  def BuildIntermediateDirectories(self):
    """Interpolate intermediate directories based on their children.

    This avoids us having to put in useless intermediate directories to the
    client fixture.
    """
    for dirname, (_, stat) in self.paths.items():
      pathspec = stat.pathspec
      while 1:
        dirname = os.path.dirname(dirname)

        new_pathspec = pathspec.Copy()
        new_pathspec.path = os.path.dirname(pathspec.path)
        pathspec = new_pathspec

        if dirname == "/" or dirname in self.paths:
          break

        self.paths[dirname] = (aff4_standard.VFSDirectory, rdf_client.StatEntry(
            st_mode=16877, st_size=1, st_dev=1, pathspec=new_pathspec))

  def ListFiles(self):
    # First return exact matches
    for k, (_, stat) in self.paths.items():
      dirname = os.path.dirname(k)
      if dirname == self._NormalizeCaseForPath(self.path, None):
        yield stat

  def Read(self, length):
    result = self.paths.get(
        self._NormalizeCaseForPath(self.path, aff4_grr.VFSFile))
    if not result:
      raise IOError("File not found")

    result = result[1]  # We just want the stat.
    data = ""
    if result.HasField("resident"):
      data = result.resident
    elif result.HasField("registry_type"):
      data = utils.SmartStr(result.registry_data.GetValue())

    data = data[self.offset:self.offset + length]

    self.offset += len(data)
    return data

  def Stat(self):
    """Get Stat for self.path."""
    stat_data = self.paths.get(self._NormalizeCaseForPath(self.path, None))
    if (not stat_data and
        self.supported_pathtype == rdf_paths.PathSpec.PathType.REGISTRY):
      # Check in case it is a registry value. Unfortunately our API doesn't let
      # the user specify if they are after a value or a key, so we have to try
      # both.
      stat_data = self.paths.get(
          self._NormalizeCaseForPath(self.path, aff4_grr.VFSFile))
    if stat_data:
      return stat_data[1]  # Strip the vfs_type.
    else:
      return self._FakeDirStat()


class DataAgnosticConverterTestValue(rdf_structs.RDFProtoStruct):
  protobuf = tests_pb2.DataAgnosticConverterTestValue
  rdf_deps = [export.ExportedMetadata, rdfvalue.RDFDatetime, rdfvalue.RDFURN]


class DataAgnosticConverterTestValueWithMetadata(rdf_structs.RDFProtoStruct):
  protobuf = tests_pb2.DataAgnosticConverterTestValueWithMetadata


class FakeRegistryVFSHandler(ClientVFSHandlerFixture):
  """Special client VFS mock that will emulate the registry."""
  prefix = "/registry"
  supported_pathtype = rdf_paths.PathSpec.PathType.REGISTRY


class FakeFullVFSHandler(ClientVFSHandlerFixture):
  """Full client VFS mock."""
  prefix = "/"
  supported_pathtype = rdf_paths.PathSpec.PathType.OS


class FakeTestDataVFSHandler(ClientVFSHandlerFixtureBase):
  """Client VFS mock that looks for files in the test_data directory."""
  prefix = "/fs/os"
  supported_pathtype = rdf_paths.PathSpec.PathType.OS

  def __init__(self,
               base_fd=None,
               prefix=None,
               pathspec=None,
               progress_callback=None,
               full_pathspec=None):
    super(FakeTestDataVFSHandler, self).__init__(
        base_fd,
        pathspec=pathspec,
        progress_callback=progress_callback,
        full_pathspec=full_pathspec)
    # This should not really be done since there might be more information
    # in the pathspec than the path but here in the test is ok.
    if not base_fd:
      self.pathspec = pathspec
    else:
      self.pathspec.last.path = os.path.join(
          self.pathspec.last.path, pathspec.CollapsePath().lstrip("/"))
    self.path = self.pathspec.CollapsePath()

  def _AbsPath(self, filename=None):
    path = self.path
    if filename:
      path = os.path.join(path, filename)
    return os.path.join(config.CONFIG["Test.data_dir"], "VFSFixture",
                        path.lstrip("/"))

  def Read(self, length):
    test_data_path = self._AbsPath()

    if not os.path.exists(test_data_path):
      raise IOError("Could not find %s" % test_data_path)

    data = open(test_data_path, "rb").read()[self.offset:self.offset + length]

    self.offset += len(data)
    return data

  def Stat(self):
    """Get Stat for self.path."""
    test_data_path = self._AbsPath()
    st = os.stat(test_data_path)
    return files.MakeStatResponse(st, self.pathspec)

  def ListFiles(self):
    for f in os.listdir(self._AbsPath()):
      ps = self.pathspec.Copy()
      ps.last.path = os.path.join(ps.last.path, f)
      yield files.MakeStatResponse(os.stat(self._AbsPath(f)), ps)


class GrrTestProgram(unittest.TestProgram):
  """A Unit test program which is compatible with conf based args parsing.

  This program ignores the testLoader passed to it and implements its
  own test loading behavior in case the --tests argument was specified
  when the program is ran. It magically reads from the --tests argument.

  In case no --tests argument was specified, the program uses the test
  loader to load the tests.
  """

  def __init__(self, labels=None, **kw):
    self.labels = labels

    # Set everything up once for all test.
    testing_startup.TestInit()

    self.setUp()
    try:
      super(GrrTestProgram, self).__init__(**kw)
    finally:
      try:
        self.tearDown()
      except Exception as e:  # pylint: disable=broad-except
        logging.exception(e)

  def setUp(self):
    """Any global initialization goes here."""
    # We don't want to send actual email in our tests
    self.smtp_patcher = mock.patch("smtplib.SMTP")
    self.mock_smtp = self.smtp_patcher.start()

    def DisabledSet(*unused_args, **unused_kw):
      raise NotImplementedError(
          "Usage of Set() is disabled, please use a configoverrider in tests.")

    self.config_set_disable = utils.Stubber(config.CONFIG, "Set", DisabledSet)
    self.config_set_disable.Start()

  def tearDown(self):
    """Global teardown code goes here."""
    self.config_set_disable.Stop()
    self.smtp_patcher.stop()

  def parseArgs(self, argv):
    """Delegate arg parsing to the conf subsystem."""
    # Give the same behaviour as regular unittest
    if not flags.FLAGS.tests:
      self.test = self.testLoader.loadTestsFromModule(self.module)
      return

    self.testNames = flags.FLAGS.tests
    self.createTests()


class RemotePDB(pdb.Pdb):
  """A Remote debugger facility.

  Place breakpoints in the code using:
  test_lib.RemotePDB().set_trace()

  Once the debugger is attached all remote break points will use the same
  connection.
  """
  handle = None
  prompt = "RemotePDB>"

  def __init__(self):
    # Use a global socket for remote debugging.
    if RemotePDB.handle is None:
      self.ListenForConnection()

    pdb.Pdb.__init__(
        self, stdin=self.handle, stdout=codecs.getwriter("utf8")(self.handle))

  def ListenForConnection(self):
    """Listens and accepts a single connection."""
    logging.warn("Remote debugger waiting for connection on %s",
                 config.CONFIG["Test.remote_pdb_port"])

    RemotePDB.old_stdout = sys.stdout
    RemotePDB.old_stdin = sys.stdin
    RemotePDB.skt = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    RemotePDB.skt.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    RemotePDB.skt.bind(("127.0.0.1", config.CONFIG["Test.remote_pdb_port"]))
    RemotePDB.skt.listen(1)

    (clientsocket, address) = RemotePDB.skt.accept()
    RemotePDB.handle = clientsocket.makefile("rw", 1)
    logging.warn("Received a connection from %s", address)


class TestRekallRepositoryProfileServer(rekall_profile_server.ProfileServer):
  """This server gets the profiles locally from the test data dir."""

  def __init__(self, *args, **kw):
    super(TestRekallRepositoryProfileServer, self).__init__(*args, **kw)
    self.profiles_served = 0

  def GetProfileByName(self, profile_name, version="v1.0"):
    try:
      profile_data = open(
          os.path.join(config.CONFIG["Test.data_dir"], "profiles", version,
                       profile_name + ".gz"), "rb").read()

      self.profiles_served += 1

      return rdf_rekall_types.RekallProfile(
          name=profile_name, version=version, data=profile_data)
    except IOError:
      return None


class OSSpecificClientTests(EmptyActionTest):
  """OS-specific client action tests.

  We need to temporarily disable the actionplugin class registry to avoid
  registering actions for other OSes.
  """

  def setUp(self):
    super(OSSpecificClientTests, self).setUp()
    self.action_reg_stubber = utils.Stubber(actions.ActionPlugin, "classes", {})
    self.action_reg_stubber.Start()
    self.binary_command_stubber = utils.Stubber(standard.ExecuteBinaryCommand,
                                                "classes", {})
    self.binary_command_stubber.Start()

  def tearDown(self):
    super(OSSpecificClientTests, self).tearDown()
    self.action_reg_stubber.Stop()
    self.binary_command_stubber.Stop()


def WriteComponent(name="grr-rekall",
                   version="0.4",
                   build_system=None,
                   modules=None,
                   token=None,
                   raw_data=""):
  """Create a fake component."""
  components_base = "grr.client.components.rekall_support."
  if modules is None:
    # For tests we load the component from the source tree directly. This is
    # because tests aready include the component compiled in and so do not need
    # to pack and unpack it.
    modules = [components_base + "grr_rekall"]
  result = rdf_client.ClientComponent(raw_data=raw_data)

  if build_system:
    result.build_system = build_system
  else:
    # libc_ver is broken so we need to work around it. It assumes that
    # there is always a sys.executable and just raises if there
    # isn't. For some environments this assumption is not true at all so
    # we just make it return a "sane" value in tests. In the end, this
    # function scans the interpreter binary for some magic regex so it
    # would be best not to use it at all.
    with utils.Stubber(platform, "libc_ver", lambda: ("glibc", "2.3")):
      result.build_system = result.build_system.FromCurrentSystem()

  result.summary.modules = modules
  result.summary.name = name
  result.summary.version = version
  result.summary.cipher = rdf_crypto.SymmetricCipher.Generate("AES128CBC")

  with utils.TempDirectory() as tmp_dir:
    with open(os.path.join(tmp_dir, "component"), "wb") as fd:
      fd.write(result.SerializeToString())

    return maintenance_utils.SignComponent(fd.name, token=token)


def main(argv=None):
  if argv is None:
    argv = sys.argv

  print "Running test %s" % argv[0]
  GrrTestProgram(argv=argv)
