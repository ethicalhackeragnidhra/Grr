#!/usr/bin/env python
"""Tests for debugging flows."""


import os

from grr.client.client_actions import standard
from grr.lib import action_mocks
from grr.lib import flags
from grr.lib import test_lib

from grr.lib.flows import console  # pylint: disable=unused-import

from grr.lib.flows.console import debugging

from grr.lib.rdfvalues import client as rdf_client
from grr.lib.rdfvalues import paths as rdf_paths


class TestDebugFlows(test_lib.FlowTestsBaseclass):

  def testClientAction(self):
    client_mock = action_mocks.ActionMock(standard.ListDirectory)
    pathspec = rdf_paths.PathSpec(
        path=os.path.join(self.base_path, "test_img.dd"),
        pathtype=rdf_paths.PathSpec.PathType.OS)

    request = rdf_client.ListDirRequest(pathspec=pathspec)

    for _ in test_lib.TestFlowHelper(
        debugging.ClientAction.__name__,
        client_mock,
        client_id=self.client_id,
        action=standard.ListDirectory.__name__,
        break_pdb=False,
        action_args=request,
        token=self.token):
      pass


def main(argv):
  # Run the full test suite
  test_lib.GrrTestProgram(argv=argv)


if __name__ == "__main__":
  flags.StartMain(main)
