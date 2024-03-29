#!/usr/bin/env python
# -*- mode: python; encoding: utf-8 -*-
"""This modules contains tests for VFS API handlers."""



import StringIO
import zipfile

from grr.gui import api_test_lib

from grr.gui.api_plugins import vfs as vfs_plugin
from grr.lib import access_control
from grr.lib import action_mocks
from grr.lib import aff4
from grr.lib import flags
from grr.lib import flow
from grr.lib import rdfvalue
from grr.lib import test_lib
from grr.lib.aff4_objects import aff4_grr
from grr.lib.aff4_objects import users as aff4_users
from grr.lib.flows.general import discovery
from grr.lib.flows.general import filesystem
from grr.lib.flows.general import transfer
from grr.lib.rdfvalues import client as rdf_client
from grr.lib.rdfvalues import paths as rdf_paths


class VfsTestMixin(object):
  """A helper mixin providing methods to prepare files and flows for testing.
  """

  time_0 = rdfvalue.RDFDatetime(42)
  time_1 = time_0 + rdfvalue.Duration("1d")
  time_2 = time_1 + rdfvalue.Duration("1d")

  def CreateFileVersions(self, client_id, file_path):
    """Add a new version for a file."""

    with test_lib.FakeTime(self.time_1):
      token = access_control.ACLToken(username="test")
      fd = aff4.FACTORY.Create(
          client_id.Add(file_path),
          aff4.AFF4MemoryStream,
          mode="w",
          token=token)
      fd.Write("Hello World")
      fd.Close()

    with test_lib.FakeTime(self.time_2):
      fd = aff4.FACTORY.Create(
          client_id.Add(file_path),
          aff4.AFF4MemoryStream,
          mode="w",
          token=token)
      fd.Write("Goodbye World")
      fd.Close()

  def CreateRecursiveListFlow(self, client_id, token):
    flow_args = filesystem.RecursiveListDirectoryArgs()

    return flow.GRRFlow.StartFlow(
        client_id=client_id,
        flow_name=filesystem.RecursiveListDirectory.__name__,
        args=flow_args,
        token=token)

  def CreateMultiGetFileFlow(self, client_id, file_path, token):
    pathspec = rdf_paths.PathSpec(
        path=file_path, pathtype=rdf_paths.PathSpec.PathType.OS)
    flow_args = transfer.MultiGetFileArgs(pathspecs=[pathspec])

    return flow.GRRFlow.StartFlow(
        client_id=client_id,
        flow_name=transfer.MultiGetFile.__name__,
        args=flow_args,
        token=token)


class ApiGetFileDetailsHandlerTest(api_test_lib.ApiCallHandlerTest,
                                   VfsTestMixin):
  """Test for ApiGetFileDetailsHandler."""

  def setUp(self):
    super(ApiGetFileDetailsHandlerTest, self).setUp()
    self.handler = vfs_plugin.ApiGetFileDetailsHandler()
    self.client_id = self.SetupClients(1)[0]
    self.file_path = "fs/os/c/Downloads/a.txt"
    self.CreateFileVersions(self.client_id, self.file_path)

  def testRaisesOnEmptyPath(self):
    args = vfs_plugin.ApiGetFileDetailsArgs(
        client_id=self.client_id, file_path="")
    with self.assertRaises(ValueError):
      self.handler.Handle(args, token=self.token)

  def testRaisesOnRootPath(self):
    args = vfs_plugin.ApiGetFileDetailsArgs(
        client_id=self.client_id, file_path="/")
    with self.assertRaises(ValueError):
      self.handler.Handle(args, token=self.token)

  def testRaisesIfFirstComponentNotInWhitelist(self):
    args = vfs_plugin.ApiGetFileDetailsArgs(
        client_id=self.client_id, file_path="/analysis")
    with self.assertRaises(ValueError):
      self.handler.Handle(args, token=self.token)

  def testHandlerReturnsNewestVersionByDefault(self):
    # Get file version without specifying a timestamp.
    args = vfs_plugin.ApiGetFileDetailsArgs(
        client_id=self.client_id, file_path=self.file_path)
    result = self.handler.Handle(args, token=self.token)

    # Should return the newest version.
    self.assertEqual(result.file.path, self.file_path)
    self.assertAlmostEqual(
        result.file.age, self.time_2, delta=rdfvalue.Duration("1s"))

  def testHandlerReturnsClosestSpecificVersion(self):
    # Get specific version.
    args = vfs_plugin.ApiGetFileDetailsArgs(
        client_id=self.client_id,
        file_path=self.file_path,
        timestamp=self.time_1)
    result = self.handler.Handle(args, token=self.token)

    # The age of the returned version might have a slight deviation.
    self.assertEqual(result.file.path, self.file_path)
    self.assertAlmostEqual(
        result.file.age, self.time_1, delta=rdfvalue.Duration("1s"))

  def testResultIncludesDetails(self):
    """Checks if the details include certain attributes.

    Instead of using a (fragile) regression test, we enumerate important
    attributes here and make sure they are returned.
    """

    args = vfs_plugin.ApiGetFileDetailsArgs(
        client_id=self.client_id, file_path=self.file_path)
    result = self.handler.Handle(args, token=self.token)

    attributes_by_type = {}
    attributes_by_type["AFF4MemoryStream"] = ["CONTENT"]
    attributes_by_type["AFF4MemoryStreamBase"] = ["SIZE"]
    attributes_by_type["AFF4Object"] = ["LAST", "SUBJECT", "TYPE"]

    details = result.file.details
    for type_name, attrs in attributes_by_type.iteritems():
      type_obj = next(t for t in details.types if t.name == type_name)
      all_attrs = set([a.name for a in type_obj.attributes])
      self.assertTrue(set(attrs).issubset(all_attrs))


class ApiListFilesHandlerTest(api_test_lib.ApiCallHandlerTest, VfsTestMixin):
  """Test for ApiListFilesHandler."""

  def setUp(self):
    super(ApiListFilesHandlerTest, self).setUp()
    self.handler = vfs_plugin.ApiListFilesHandler()
    self.client_id = self.SetupClients(1)[0]
    self.file_path = "fs/os/etc"

  def testDoesNotRaiseIfFirstCompomentIsEmpty(self):
    args = vfs_plugin.ApiListFilesArgs(client_id=self.client_id, file_path="")
    self.handler.Handle(args, token=self.token)

  def testDoesNotRaiseIfPathIsRoot(self):
    args = vfs_plugin.ApiListFilesArgs(client_id=self.client_id, file_path="/")
    self.handler.Handle(args, token=self.token)

  def testRaisesIfFirstComponentIsNotWhitelisted(self):
    args = vfs_plugin.ApiListFilesArgs(
        client_id=self.client_id, file_path="/analysis")
    with self.assertRaises(ValueError):
      self.handler.Handle(args, token=self.token)

  def testHandlerListsFilesAndDirectories(self):
    test_lib.ClientFixture(self.client_id, token=self.token)

    # Fetch all children of a directory.
    args = vfs_plugin.ApiListFilesArgs(
        client_id=self.client_id, file_path=self.file_path)
    result = self.handler.Handle(args, token=self.token)

    self.assertEqual(len(result.items), 4)
    for item in result.items:
      # Check that all files are really in the right directory.
      self.assertIn(self.file_path, item.path)

  def testHandlerFiltersDirectoriesIfFlagIsSet(self):
    test_lib.ClientFixture(self.client_id, token=self.token)

    # Only fetch sub-directories.
    args = vfs_plugin.ApiListFilesArgs(
        client_id=self.client_id,
        file_path=self.file_path,
        directories_only=True)
    result = self.handler.Handle(args, token=self.token)

    self.assertEqual(len(result.items), 1)
    self.assertEqual(result.items[0].is_directory, True)
    self.assertIn(self.file_path, result.items[0].path)


class ApiGetFileTextHandlerTest(api_test_lib.ApiCallHandlerTest, VfsTestMixin):
  """Test for ApiGetFileTextHandler."""

  def setUp(self):
    super(ApiGetFileTextHandlerTest, self).setUp()
    self.handler = vfs_plugin.ApiGetFileTextHandler()
    self.client_id = self.SetupClients(1)[0]
    self.file_path = "fs/os/c/Downloads/a.txt"
    self.CreateFileVersions(self.client_id, self.file_path)

  def testRaisesOnEmptyPath(self):
    args = vfs_plugin.ApiGetFileTextArgs(client_id=self.client_id, file_path="")
    with self.assertRaises(ValueError):
      self.handler.Handle(args, token=self.token)

  def testRaisesOnRootPath(self):
    args = vfs_plugin.ApiGetFileTextArgs(
        client_id=self.client_id, file_path="/")
    with self.assertRaises(ValueError):
      self.handler.Handle(args, token=self.token)

  def testRaisesIfFirstComponentNotInWhitelist(self):
    args = vfs_plugin.ApiGetFileTextArgs(
        client_id=self.client_id, file_path="/analysis")
    with self.assertRaises(ValueError):
      self.handler.Handle(args, token=self.token)

  def testDifferentTimestampsYieldDifferentFileContents(self):
    args = vfs_plugin.ApiGetFileTextArgs(
        client_id=self.client_id,
        file_path=self.file_path,
        encoding=vfs_plugin.ApiGetFileTextArgs.Encoding.UTF_8)

    # Retrieving latest version by not setting a timestamp.
    result = self.handler.Handle(args, token=self.token)

    self.assertEqual(result.content, "Goodbye World")
    self.assertEqual(result.total_size, 13)

    # Change timestamp to get a different file version.
    args.timestamp = self.time_1
    result = self.handler.Handle(args, token=self.token)

    self.assertEqual(result.content, "Hello World")
    self.assertEqual(result.total_size, 11)

  def testEncodingChangesResult(self):
    args = vfs_plugin.ApiGetFileTextArgs(
        client_id=self.client_id,
        file_path=self.file_path,
        encoding=vfs_plugin.ApiGetFileTextArgs.Encoding.UTF_16)

    # Retrieving latest version by not setting a timestamp.
    result = self.handler.Handle(args, token=self.token)

    self.assertNotEqual(result.content, "Goodbye World")
    self.assertEqual(result.total_size, 13)


class ApiGetFileBlobHandlerTest(api_test_lib.ApiCallHandlerTest, VfsTestMixin):

  def setUp(self):
    super(ApiGetFileBlobHandlerTest, self).setUp()
    self.handler = vfs_plugin.ApiGetFileBlobHandler()
    self.client_id = self.SetupClients(1)[0]
    self.file_path = "fs/os/c/Downloads/a.txt"
    self.CreateFileVersions(self.client_id, self.file_path)

  def testRaisesOnEmptyPath(self):
    args = vfs_plugin.ApiGetFileBlobArgs(client_id=self.client_id, file_path="")
    with self.assertRaises(ValueError):
      self.handler.Handle(args, token=self.token)

  def testRaisesOnRootPath(self):
    args = vfs_plugin.ApiGetFileBlobArgs(
        client_id=self.client_id, file_path="/")
    with self.assertRaises(ValueError):
      self.handler.Handle(args, token=self.token)

  def testRaisesIfFirstComponentNotInWhitelist(self):
    args = vfs_plugin.ApiGetFileBlobArgs(
        client_id=self.client_id, file_path="/analysis")
    with self.assertRaises(ValueError):
      self.handler.Handle(args, token=self.token)

  def testNewestFileContentIsReturnedByDefault(self):
    args = vfs_plugin.ApiGetFileBlobArgs(
        client_id=self.client_id, file_path=self.file_path)
    result = self.handler.Handle(args, token=self.token)

    self.assertTrue(hasattr(result, "GenerateContent"))
    self.assertEqual(next(result.GenerateContent()), "Goodbye World")

  def testOffsetAndLengthRestrictResult(self):
    args = vfs_plugin.ApiGetFileBlobArgs(
        client_id=self.client_id, file_path=self.file_path, offset=2, length=3)
    result = self.handler.Handle(args, token=self.token)

    self.assertTrue(hasattr(result, "GenerateContent"))
    self.assertEqual(next(result.GenerateContent()), "odb")

  def testReturnsOlderVersionIfTimestampIsSupplied(self):
    args = vfs_plugin.ApiGetFileBlobArgs(
        client_id=self.client_id,
        file_path=self.file_path,
        timestamp=self.time_1)
    result = self.handler.Handle(args, token=self.token)

    self.assertTrue(hasattr(result, "GenerateContent"))
    self.assertEqual(next(result.GenerateContent()), "Hello World")

  def testLargeFileIsReturnedInMultipleChunks(self):
    chars = ["a", "b", "x"]
    huge_file_path = "fs/os/c/Downloads/huge.txt"

    # Overwrite CHUNK_SIZE in handler for smaller test streams.
    self.handler.CHUNK_SIZE = 5

    # Create a file that requires several chunks to load.
    with aff4.FACTORY.Create(
        self.client_id.Add(huge_file_path),
        aff4.AFF4MemoryStream,
        mode="w",
        token=self.token) as fd:
      for char in chars:
        fd.Write(char * self.handler.CHUNK_SIZE)

    args = vfs_plugin.ApiGetFileBlobArgs(
        client_id=self.client_id, file_path=huge_file_path)
    result = self.handler.Handle(args, token=self.token)

    self.assertTrue(hasattr(result, "GenerateContent"))
    for chunk, char in zip(result.GenerateContent(), chars):
      self.assertEqual(chunk, char * self.handler.CHUNK_SIZE)


class ApiGetFileVersionTimesHandlerTest(api_test_lib.ApiCallHandlerTest,
                                        VfsTestMixin):

  def setUp(self):
    super(ApiGetFileVersionTimesHandlerTest, self).setUp()
    self.client_id = self.SetupClients(1)[0]
    self.handler = vfs_plugin.ApiGetFileVersionTimesHandler()

  def testRaisesOnEmptyPath(self):
    args = vfs_plugin.ApiGetFileVersionTimesArgs(
        client_id=self.client_id, file_path="")
    with self.assertRaises(ValueError):
      self.handler.Handle(args, token=self.token)

  def testRaisesOnRootPath(self):
    args = vfs_plugin.ApiGetFileVersionTimesArgs(
        client_id=self.client_id, file_path="/")
    with self.assertRaises(ValueError):
      self.handler.Handle(args, token=self.token)

  def testRaisesIfFirstComponentNotInWhitelist(self):
    args = vfs_plugin.ApiGetFileVersionTimesArgs(
        client_id=self.client_id, file_path="/analysis")
    with self.assertRaises(ValueError):
      self.handler.Handle(args, token=self.token)


class ApiGetFileDownloadCommandHandlerTest(api_test_lib.ApiCallHandlerTest,
                                           VfsTestMixin):

  def setUp(self):
    super(ApiGetFileDownloadCommandHandlerTest, self).setUp()
    self.client_id = self.SetupClients(1)[0]
    self.handler = vfs_plugin.ApiGetFileDownloadCommandHandler()

  def testRaisesOnEmptyPath(self):
    args = vfs_plugin.ApiGetFileDownloadCommandArgs(
        client_id=self.client_id, file_path="")
    with self.assertRaises(ValueError):
      self.handler.Handle(args, token=self.token)

  def testRaisesOnRootPath(self):
    args = vfs_plugin.ApiGetFileDownloadCommandArgs(
        client_id=self.client_id, file_path="/")
    with self.assertRaises(ValueError):
      self.handler.Handle(args, token=self.token)

  def testRaisesIfFirstComponentNotInWhitelist(self):
    args = vfs_plugin.ApiGetFileDownloadCommandArgs(
        client_id=self.client_id, file_path="/analysis")
    with self.assertRaises(ValueError):
      self.handler.Handle(args, token=self.token)


class ApiCreateVfsRefreshOperationHandlerTest(api_test_lib.ApiCallHandlerTest):
  """Test for ApiCreateVfsRefreshOperationHandler."""

  def setUp(self):
    super(ApiCreateVfsRefreshOperationHandlerTest, self).setUp()
    self.handler = vfs_plugin.ApiCreateVfsRefreshOperationHandler()
    self.client_id = self.SetupClients(1)[0]
    # Choose some directory with pathspec in the ClientFixture.
    self.file_path = "fs/os/Users/Shared"

  def testRaisesOnEmptyPath(self):
    args = vfs_plugin.ApiCreateVfsRefreshOperationArgs(
        client_id=self.client_id, file_path="")
    with self.assertRaises(ValueError):
      self.handler.Handle(args, token=self.token)

  def testRaisesOnRootPath(self):
    args = vfs_plugin.ApiCreateVfsRefreshOperationArgs(
        client_id=self.client_id, file_path="/")
    with self.assertRaises(ValueError):
      self.handler.Handle(args, token=self.token)

  def testRaisesIfFirstComponentNotInWhitelist(self):
    args = vfs_plugin.ApiCreateVfsRefreshOperationArgs(
        client_id=self.client_id, file_path="/analysis")
    with self.assertRaises(ValueError):
      self.handler.Handle(args, token=self.token)

  def testHandlerRefreshStartsListDirectoryFlow(self):
    test_lib.ClientFixture(self.client_id, token=self.token)

    args = vfs_plugin.ApiCreateVfsRefreshOperationArgs(
        client_id=self.client_id, file_path=self.file_path, max_depth=1)
    result = self.handler.Handle(args, token=self.token)

    # Check returned operation_id to references a ListDirectory flow.
    flow_obj = aff4.FACTORY.Open(result.operation_id, token=self.token)
    self.assertEqual(
        flow_obj.Get(flow_obj.Schema.TYPE), filesystem.ListDirectory.__name__)

  def testHandlerRefreshStartsRecursiveListDirectoryFlow(self):
    test_lib.ClientFixture(self.client_id, token=self.token)

    args = vfs_plugin.ApiCreateVfsRefreshOperationArgs(
        client_id=self.client_id, file_path=self.file_path, max_depth=5)
    result = self.handler.Handle(args, token=self.token)

    # Check returned operation_id to references a RecursiveListDirectory flow.
    flow_obj = aff4.FACTORY.Open(result.operation_id, token=self.token)
    self.assertEqual(
        flow_obj.Get(flow_obj.Schema.TYPE),
        filesystem.RecursiveListDirectory.__name__)

  def testNotificationIsSent(self):
    test_lib.ClientFixture(self.client_id, token=self.token)

    args = vfs_plugin.ApiCreateVfsRefreshOperationArgs(
        client_id=self.client_id,
        file_path=self.file_path,
        max_depth=0,
        notify_user=True)
    result = self.handler.Handle(args, token=self.token)

    # Finish flow and check if there are any new notifications.
    flow_urn = rdfvalue.RDFURN(result.operation_id)
    client_mock = action_mocks.ActionMock()
    for _ in test_lib.TestFlowHelper(
        flow_urn,
        client_mock,
        client_id=self.client_id,
        token=self.token,
        check_flow_errors=False):
      pass

    # Get pending notifications and check the newest one.
    user_record = aff4.FACTORY.Open(
        aff4.ROOT_URN.Add("users").Add(self.token.username),
        aff4_type=aff4_users.GRRUser,
        mode="r",
        token=self.token)

    pending_notifications = user_record.Get(
        user_record.Schema.PENDING_NOTIFICATIONS)

    self.assertIn("Recursive Directory Listing complete",
                  pending_notifications[0].message)
    self.assertEqual(pending_notifications[0].source, str(flow_urn))


class ApiGetVfsRefreshOperationStateHandlerTest(api_test_lib.ApiCallHandlerTest,
                                                VfsTestMixin):
  """Test for GetVfsRefreshOperationStateHandler."""

  def setUp(self):
    super(ApiGetVfsRefreshOperationStateHandlerTest, self).setUp()
    self.handler = vfs_plugin.ApiGetVfsRefreshOperationStateHandler()
    self.client_id = self.SetupClients(1)[0]

  def testHandlerReturnsCorrectStateForFlow(self):
    # Create a mock refresh operation.
    self.flow_urn = self.CreateRecursiveListFlow(self.client_id, self.token)

    args = vfs_plugin.ApiGetVfsRefreshOperationStateArgs(
        operation_id=str(self.flow_urn))

    # Flow was started and should be running.
    result = self.handler.Handle(args, token=self.token)
    self.assertEqual(result.state, "RUNNING")

    # Terminate flow.
    with aff4.FACTORY.Open(
        self.flow_urn, aff4_type=flow.GRRFlow, mode="rw",
        token=self.token) as flow_obj:
      flow_obj.GetRunner().Error("Fake error")

    # Recheck status and see if it changed.
    result = self.handler.Handle(args, token=self.token)
    self.assertEqual(result.state, "FINISHED")

  def testHandlerThrowsExceptionOnArbitraryFlowId(self):
    # Create a mock flow.
    self.flow_urn = flow.GRRFlow.StartFlow(
        client_id=self.client_id,
        flow_name=discovery.Interrogate.__name__,
        token=self.token)

    args = vfs_plugin.ApiGetVfsRefreshOperationStateArgs(
        operation_id=str(self.flow_urn))

    # Our mock flow is not a RecursiveListFlow, so an error should be raised.
    with self.assertRaises(vfs_plugin.VfsRefreshOperationNotFoundError):
      self.handler.Handle(args, token=self.token)

  def testHandlerThrowsExceptionOnUnknownFlowId(self):
    # Create args with an operation id not referencing any flow.
    args = vfs_plugin.ApiGetVfsRefreshOperationStateArgs(
        operation_id="F:12345678")

    # Our mock flow can't be read, so an error should be raised.
    with self.assertRaises(vfs_plugin.VfsRefreshOperationNotFoundError):
      self.handler.Handle(args, token=self.token)


class ApiUpdateVfsFileContentHandlerTest(api_test_lib.ApiCallHandlerTest):
  """Test for ApiUpdateVfsFileContentHandler."""

  def setUp(self):
    super(ApiUpdateVfsFileContentHandlerTest, self).setUp()
    self.handler = vfs_plugin.ApiUpdateVfsFileContentHandler()
    self.client_id = self.SetupClients(1)[0]
    self.file_path = "fs/os/c/bin/bash"

  def testRaisesOnEmptyPath(self):
    args = vfs_plugin.ApiUpdateVfsFileContentArgs(
        client_id=self.client_id, file_path="")
    with self.assertRaises(ValueError):
      self.handler.Handle(args, token=self.token)

  def testRaisesOnRootPath(self):
    args = vfs_plugin.ApiUpdateVfsFileContentArgs(
        client_id=self.client_id, file_path="/")
    with self.assertRaises(ValueError):
      self.handler.Handle(args, token=self.token)

  def testRaisesIfFirstComponentNotInWhitelist(self):
    args = vfs_plugin.ApiUpdateVfsFileContentArgs(
        client_id=self.client_id, file_path="/analysis")
    with self.assertRaises(ValueError):
      self.handler.Handle(args, token=self.token)

  def testHandlerStartsFlow(self):
    test_lib.ClientFixture(self.client_id, token=self.token)

    args = vfs_plugin.ApiUpdateVfsFileContentArgs(
        client_id=self.client_id, file_path=self.file_path)
    result = self.handler.Handle(args, token=self.token)

    # Check returned operation_id to references a MultiGetFile flow.
    flow_obj = aff4.FACTORY.Open(result.operation_id, token=self.token)
    self.assertEqual(
        flow_obj.Get(flow_obj.Schema.TYPE), transfer.MultiGetFile.__name__)


class ApiGetVfsFileContentUpdateStateHandlerTest(
    api_test_lib.ApiCallHandlerTest, VfsTestMixin):
  """Test for ApiGetVfsFileContentUpdateStateHandler."""

  def setUp(self):
    super(ApiGetVfsFileContentUpdateStateHandlerTest, self).setUp()
    self.handler = vfs_plugin.ApiGetVfsFileContentUpdateStateHandler()
    self.client_id = self.SetupClients(1)[0]

  def testHandlerReturnsCorrectStateForFlow(self):
    # Create a mock refresh operation.
    self.flow_urn = self.CreateMultiGetFileFlow(
        self.client_id, file_path="fs/os/c/bin/bash", token=self.token)

    args = vfs_plugin.ApiGetVfsFileContentUpdateStateArgs(
        operation_id=str(self.flow_urn))

    # Flow was started and should be running.
    result = self.handler.Handle(args, token=self.token)
    self.assertEqual(result.state, "RUNNING")

    # Terminate flow.
    with aff4.FACTORY.Open(
        self.flow_urn, aff4_type=flow.GRRFlow, mode="rw",
        token=self.token) as flow_obj:
      flow_obj.GetRunner().Error("Fake error")

    # Recheck status and see if it changed.
    result = self.handler.Handle(args, token=self.token)
    self.assertEqual(result.state, "FINISHED")

  def testHandlerRaisesOnArbitraryFlowId(self):
    # Create a mock flow.
    self.flow_urn = flow.GRRFlow.StartFlow(
        client_id=self.client_id,
        flow_name=discovery.Interrogate.__name__,
        token=self.token)

    args = vfs_plugin.ApiGetVfsFileContentUpdateStateArgs(
        operation_id=str(self.flow_urn))

    # Our mock flow is not a MultiGetFile flow, so an error should be raised.
    with self.assertRaises(vfs_plugin.VfsFileContentUpdateNotFoundError):
      self.handler.Handle(args, token=self.token)

  def testHandlerThrowsExceptionOnUnknownFlowId(self):
    # Create args with an operation id not referencing any flow.
    args = vfs_plugin.ApiGetVfsRefreshOperationStateArgs(
        operation_id="F:12345678")

    # Our mock flow can't be read, so an error should be raised.
    with self.assertRaises(vfs_plugin.VfsFileContentUpdateNotFoundError):
      self.handler.Handle(args, token=self.token)


class VfsTimelineTestMixin(object):
  """A helper mixin providing methods to prepare timelines for testing.
  """

  def SetupTestTimeline(self):
    self.client_id = self.SetupClients(1)[0]
    test_lib.ClientFixture(self.client_id, token=self.token)

    # Choose some directory with pathspec in the ClientFixture.
    self.folder_path = "fs/os/Users/中国新闻网新闻中/Shared"
    self.file_path = self.folder_path + "/a.txt"

    file_urn = self.client_id.Add(self.file_path)
    for i in range(0, 5):
      with test_lib.FakeTime(i):
        with aff4.FACTORY.Create(
            file_urn, aff4_grr.VFSAnalysisFile, mode="w",
            token=self.token) as fd:
          stats = rdf_client.StatEntry(
              st_mtime=rdfvalue.RDFDatetimeSeconds().Now())
          fd.Set(fd.Schema.STAT, stats)


class ApiGetVfsTimelineAsCsvHandlerTest(api_test_lib.ApiCallHandlerTest,
                                        VfsTimelineTestMixin):

  def setUp(self):
    super(ApiGetVfsTimelineAsCsvHandlerTest, self).setUp()
    self.handler = vfs_plugin.ApiGetVfsTimelineAsCsvHandler()
    self.SetupTestTimeline()

  def testRaisesOnEmptyPath(self):
    args = vfs_plugin.ApiGetVfsTimelineAsCsvArgs(
        client_id=self.client_id, file_path="")
    with self.assertRaises(ValueError):
      self.handler.Handle(args, token=self.token)

  def testRaisesOnRootPath(self):
    args = vfs_plugin.ApiGetVfsTimelineAsCsvArgs(
        client_id=self.client_id, file_path="/")
    with self.assertRaises(ValueError):
      self.handler.Handle(args, token=self.token)

  def testRaisesIfFirstComponentNotInWhitelist(self):
    args = vfs_plugin.ApiGetVfsTimelineAsCsvArgs(
        client_id=self.client_id, file_path="/analysis")
    with self.assertRaises(ValueError):
      self.handler.Handle(args, token=self.token)

  def testTimelineIsReturnedInChunks(self):
    # Change chunk size to see if the handler behaves correctly.
    self.handler.CHUNK_SIZE = 1

    args = vfs_plugin.ApiGetVfsTimelineAsCsvArgs(
        client_id=self.client_id, file_path=self.folder_path)
    result = self.handler.Handle(args, token=self.token)

    # Check rows returned correctly.
    self.assertTrue(hasattr(result, "GenerateContent"))
    for i in reversed(range(0, 5)):
      with test_lib.FakeTime(i):
        next_chunk = next(result.GenerateContent()).strip()
        timestamp = rdfvalue.RDFDatetime.Now()
        if i == 4:  # The first row includes the column headings.
          self.assertEqual(
              next_chunk, "Timestamp,Datetime,Message,Timestamp_desc\r\n"
              "%d,%s,%s,MODIFICATION" % (timestamp.AsMicroSecondsFromEpoch(),
                                         str(timestamp), self.file_path))
        else:
          self.assertEqual(next_chunk, "%d,%s,%s,MODIFICATION" %
                           (timestamp.AsMicroSecondsFromEpoch(), str(timestamp),
                            self.file_path))

  def testEmptyTimelineIsReturnedOnNonexistantPath(self):
    args = vfs_plugin.ApiGetVfsTimelineAsCsvArgs(
        client_id=self.client_id, file_path="fs/non-existant/file/path")
    result = self.handler.Handle(args, token=self.token)

    self.assertTrue(hasattr(result, "GenerateContent"))
    with self.assertRaises(StopIteration):
      next(result.GenerateContent())


class ApiGetVfsTimelineHandlerTest(api_test_lib.ApiCallHandlerTest,
                                   VfsTimelineTestMixin):

  def setUp(self):
    super(ApiGetVfsTimelineHandlerTest, self).setUp()
    self.handler = vfs_plugin.ApiGetVfsTimelineHandler()
    self.SetupTestTimeline()

  def testRaisesOnEmptyPath(self):
    args = vfs_plugin.ApiGetVfsTimelineArgs(
        client_id=self.client_id, file_path="")
    with self.assertRaises(ValueError):
      self.handler.Handle(args, token=self.token)

  def testRaisesOnRootPath(self):
    args = vfs_plugin.ApiGetVfsTimelineArgs(
        client_id=self.client_id, file_path="/")
    with self.assertRaises(ValueError):
      self.handler.Handle(args, token=self.token)

  def testRaisesIfFirstComponentNotInWhitelist(self):
    args = vfs_plugin.ApiGetVfsTimelineArgs(
        client_id=self.client_id, file_path="/analysis")
    with self.assertRaises(ValueError):
      self.handler.Handle(args, token=self.token)


class ApiGetVfsFilesArchiveHandlerTest(api_test_lib.ApiCallHandlerTest,
                                       VfsTestMixin):
  """Tests for ApiGetVfsFileArchiveHandler."""

  def setUp(self):
    super(ApiGetVfsFilesArchiveHandlerTest, self).setUp()

    self.handler = vfs_plugin.ApiGetVfsFilesArchiveHandler()
    self.client_id = self.SetupClients(1)[0]

    self.CreateFileVersions(self.client_id, "fs/os/c/Downloads/a.txt")

    self.CreateFileVersions(self.client_id, "fs/os/c/b.txt")

  def testGeneratesZipArchiveWhenPathIsNotPassed(self):
    archive_path1 = "vfs_C_1000000000000000/fs/os/c/Downloads/a.txt"
    archive_path2 = "vfs_C_1000000000000000/fs/os/c/b.txt"

    result = self.handler.Handle(
        vfs_plugin.ApiGetVfsFilesArchiveArgs(client_id=self.client_id),
        token=self.token)

    out_fd = StringIO.StringIO()
    for chunk in result.GenerateContent():
      out_fd.write(chunk)

    zip_fd = zipfile.ZipFile(out_fd, "r")
    self.assertEqual(
        set(zip_fd.namelist()), set([archive_path1, archive_path2]))

    for path in [archive_path1, archive_path2]:
      contents = zip_fd.read(path)
      self.assertEqual(contents, "Goodbye World")

  def testFiltersArchivedFilesByPath(self):
    archive_path = ("vfs_C_1000000000000000_fs_os_c_Downloads/"
                    "fs/os/c/Downloads/a.txt")

    result = self.handler.Handle(
        vfs_plugin.ApiGetVfsFilesArchiveArgs(
            client_id=self.client_id, file_path="fs/os/c/Downloads"),
        token=self.token)

    out_fd = StringIO.StringIO()
    for chunk in result.GenerateContent():
      out_fd.write(chunk)

    zip_fd = zipfile.ZipFile(out_fd, "r")
    self.assertEqual(zip_fd.namelist(), [archive_path])

    contents = zip_fd.read(archive_path)
    self.assertEqual(contents, "Goodbye World")

  def testNonExistentPathGeneratesEmptyArchive(self):
    result = self.handler.Handle(
        vfs_plugin.ApiGetVfsFilesArchiveArgs(
            client_id=self.client_id, file_path="fs/os/blah/blah"),
        token=self.token)

    out_fd = StringIO.StringIO()
    for chunk in result.GenerateContent():
      out_fd.write(chunk)

    zip_fd = zipfile.ZipFile(out_fd, "r")
    self.assertEqual(zip_fd.namelist(), [])

  def testInvalidPathTriggersException(self):
    with self.assertRaises(ValueError):
      self.handler.Handle(
          vfs_plugin.ApiGetVfsFilesArchiveArgs(
              client_id=self.client_id, file_path="invalid-prefix/path"),
          token=self.token)


def main(argv):
  test_lib.main(argv)


if __name__ == "__main__":
  flags.StartMain(main)
