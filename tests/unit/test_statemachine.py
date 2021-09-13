# Copyright 2021 Northern.tech AS
#
#    Licensed under the Apache License, Version 2.0 (the "License");
#    you may not use this file except in compliance with the License.
#    You may obtain a copy of the License at
#
#        http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS,
#    WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#    See the License for the specific language governing permissions and
#    limitations under the License.
import logging
import os
import os.path
import sys
import time

import pytest

from mender.client import HTTPUnathorized, deployments
from mender.client import inventory as client_inventory
from mender.config import config
from mender.log import menderlogger
from mender.log.log import DeploymentLogHandler
from mender.scripts import artifactinfo, devicetype
from mender.scripts import runner as installscriptrunner
from mender.scripts.aggregator import identity, inventory
from mender.settings import settings
from mender.statemachine import statemachine
from mender.util import timeutil


@pytest.fixture(name="ctx")
def fixture_ctx():
    settings.PATHS.deployment_log = os.getcwd()
    context = statemachine.Context()
    context.JWT = "foobar"
    context.config = config.Config({}, {})
    context.update_timer = timeutil.IsItTime
    context.deployment_log_handler = DeploymentLogHandler()
    context.deployment = deployments.DeploymentInfo(
        {
            "id": "bugsbunny",
            "artifact": {
                "artifact_name": "release-1",
                "source": {"uri": "https://docker.mender.io",},
            },
        }
    )
    return context


class TestStates:
    @pytest.fixture(autouse=True)
    def setup_mender_logger(self):
        """Set the mender logger and deployment handler"""

        class Args:
            log_file = False
            log_level = "debug"
            no_syslog = False

        menderlogger.setup(Args())

    @pytest.fixture(autouse=True)
    def set_log_level_info(self, caplog):
        """Set the log-level capture to info for all tests"""
        caplog.set_level(logging.DEBUG)

    def test_init(self, monkeypatch, caplog):
        with monkeypatch.context() as m:
            m.setattr(identity, "aggregate", lambda *args, **kwargs: {"foo", "bar"})
            m.setattr(
                settings.PATHS,
                "local_conf",
                os.path.join(os.getcwd(), "tests/unit/data/configs/local_mender.conf"),
            )
            m.setattr(
                settings.PATHS,
                "global_conf",
                os.path.join(os.getcwd(), "tests/unit/data/configs/global_mender.conf"),
            )
            context = statemachine.Context()
            context = statemachine.Init().run(context)
            assert "Loaded configuration" in caplog.text

    def test_init_no_configuration_files(self, monkeypatch, caplog):
        with monkeypatch.context() as m:
            m.setattr(settings.PATHS, "local_conf", "/foobar/")
            m.setattr(settings.PATHS, "global_conf", "/foobar/")
            context = statemachine.Context()
            context = statemachine.Init().run(context)
            assert "No configuration files found for the device" in caplog.text

    def test_idle(self, monkeypatch):
        context = statemachine.Context()
        context.retry_timer = 1
        monkeypatch.setattr(timeutil, "sleep", time.sleep)
        time_1 = time.time()
        statemachine.Idle().run(context)
        time_2 = time.time()
        assert abs(time_2 - time_1) < 2

    def test_sync_inventory(self, ctx, monkeypatch, caplog):
        ctx.inventory_timer = timeutil.IsItTime
        sync_inventory = statemachine.SyncInventory()
        # Not time yet
        with monkeypatch.context() as m:
            m.setattr(ctx.inventory_timer, "is_it_time", lambda *args, **kwargs: False)
            assert not sync_inventory.run(ctx)
        # No inventory data
        with monkeypatch.context() as m:
            m.setattr(ctx.inventory_timer, "is_it_time", lambda *args, **kwargs: True)
            m.setattr(inventory, "aggregate", lambda *args, **kwargs: None)
            m.setattr(client_inventory, "request", lambda *args, **kwargs: None)
            sync_inventory.run(ctx)
            assert "No inventory data found" in caplog.text
        # Inventory data
        with monkeypatch.context() as m:
            ctx.config = config.Config({}, {})
            ctx.JWT = "foobar"
            m.setattr(ctx, "JWT", "foobar")
            m.setattr(ctx.inventory_timer, "is_it_time", lambda *args, **kwargs: True)
            m.setattr(inventory, "aggregate", lambda *args, **kwargs: {"foo", "bar"})
            m.setattr(client_inventory, "request", lambda *args, **kwargs: None)
            sync_inventory.run(ctx)
            assert "aggregated inventory data" in caplog.text

    def test_sync_update(self, monkeypatch, ctx):
        sync_update = statemachine.SyncUpdate()
        with monkeypatch.context() as m:
            m.setattr(ctx.update_timer, "is_it_time", lambda *args, **kwargs: False)
            assert not sync_update.run(ctx)
        # No update available
        with monkeypatch.context() as m:
            m.setattr(devicetype, "get", lambda *args, **kwargs: "qemux86-64")
            m.setattr(ctx.update_timer, "is_it_time", lambda *args, **kwargs: True)
            m.setattr(artifactinfo, "get", lambda *args, **kwargs: "release-0.1")
            m.setattr(deployments, "request", lambda *args, **kwargs: None)
            assert not sync_update.run(ctx)
        # Update available
        with monkeypatch.context() as m:
            m.setattr(devicetype, "get", lambda *args, **kwargs: "qemux86-64")
            m.setattr(ctx.update_timer, "is_it_time", lambda *args, **kwargs: True)
            m.setattr(artifactinfo, "get", lambda *args, **kwargs: "release-0.1")
            m.setattr(deployments, "request", lambda *args, **kwargs: {"foo": "bar"})
            assert sync_update.run(ctx)

    def test_download(self, monkeypatch, ctx):
        download_state = statemachine.Download()
        # Failed download
        with monkeypatch.context() as m:
            m.setattr(deployments, "download", lambda *args, **kwargs: None)
            assert isinstance(download_state.run(ctx), statemachine.ArtifactFailure)
        # Successful download
        with monkeypatch.context() as m:
            m.setattr(
                deployments, "download", lambda *args, **kwargs: {"some": "deployment"}
            )
            assert isinstance(download_state.run(ctx), statemachine.ArtifactInstall)

    def test_install(self, monkeypatch, ctx):
        artifact_install = statemachine.ArtifactInstall()
        with monkeypatch.context() as m:
            m.setattr(
                installscriptrunner, "run_sub_updater", lambda *args, **kwargs: False
            )

            def raise_exception():
                raise Exception()

            m.setattr(sys, "exit", lambda exit_code: raise_exception())
            with pytest.raises(Exception):
                artifact_install.run(ctx)
        with monkeypatch.context() as m:
            m.setattr(
                installscriptrunner, "run_sub_updater", lambda *args, **kwargs: True
            )
            m.setattr(sys, "exit", lambda exit_code: exit_code)
            artifact_install.run(ctx)

    def test_artifact_failure(self, ctx):
        artifact_failure_state = statemachine.ArtifactFailure()
        assert isinstance(artifact_failure_state.run(ctx), statemachine._UpdateDone)

    def test_unsupported_states(self, ctx):
        with pytest.raises(statemachine.UnsupportedState):
            statemachine.ArtifactReboot().run(ctx)
        with pytest.raises(statemachine.UnsupportedState):
            statemachine.ArtifactCommit().run(ctx)
        with pytest.raises(statemachine.UnsupportedState):
            statemachine.ArtifactRollback().run(ctx)
        with pytest.raises(statemachine.UnsupportedState):
            statemachine.ArtifactRollbackReboot().run(ctx)


class TestStateMachines:
    @pytest.fixture(autouse=True)
    def setup_mender_logger(self):
        """Set the mender logger and deployment handler"""

        class Args:
            log_file = False
            log_level = "debug"
            no_syslog = False

        menderlogger.setup(Args())

    def test_master_init(self):
        m = statemachine.Master(force_bootstrap=False)
        assert m.context
        assert not m.context.authorized
        assert isinstance(m.unauthorized_machine, statemachine.UnauthorizedStateMachine)
        assert isinstance(m.authorized_machine, statemachine.AuthorizedStateMachine)

    def test_master(self, ctx):
        master = statemachine.Master(force_bootstrap=False)
        # Do not run the infinite loop
        master.quit = True
        master.run(ctx)

    def test_unathorized(self, ctx, monkeypatch):
        unauthorized_machine = statemachine.UnauthorizedStateMachine()

        class MockAuthorize:
            def run(self, _):
                return "JWTTOKENTEXT"

        with monkeypatch.context() as m:
            m.setattr(statemachine, "Authorize", MockAuthorize)
            unauthorized_machine.run(ctx)
            assert ctx.JWT == "JWTTOKENTEXT"

    def test_authorized(self, ctx, monkeypatch):
        ctx.authorized = True
        ctx.inventory_timer = timeutil.IsItTime

        class MockIdleStateMachine:
            def run(self, _):
                return None

        class MockUpdateStateMachine:
            def run(self, _):
                raise HTTPUnathorized()

        with monkeypatch.context() as m:
            authorized_machine = statemachine.AuthorizedStateMachine()
            m.setattr(statemachine, "IdleStateMachine", MockIdleStateMachine)
            m.setattr(statemachine, "UpdateStateMachine", MockUpdateStateMachine)
            authorized_machine.run(ctx)
            assert not ctx.authorized

    def test_idle(self, ctx):
        class MockSyncInventory:
            def run(self, _):
                return None

        class MockSyncUpdate:
            def run(self, _):
                return True

        class MockRemoteTerminal:
            def run(self, _):
                return True

        idle_machine = statemachine.IdleStateMachine()
        idle_machine.sync_inventory = MockSyncInventory()
        idle_machine.sync_update = MockSyncUpdate()
        idle_machine.remote_terminal = MockRemoteTerminal()
        ctx.authorized = True
        idle_machine.run(ctx)

    def test_update(self, ctx):
        update_machine = statemachine.UpdateStateMachine()
        update_machine.current_state.run = lambda context: statemachine._UpdateDone()
        update_machine.run(ctx)
