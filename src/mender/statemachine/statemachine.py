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
import os.path
import sys
import time

from mender.bootstrap import bootstrap
from mender.client import HTTPUnathorized, authorize, deployments
from mender.client import inventory as client_inventory
from mender.config import config
from mender.remoteterminal import remoteterminal
from mender.scripts import artifactinfo, devicetype
from mender.scripts import runner as installscriptrunner
from mender.scripts.aggregator import identity, inventory
from mender.settings import settings
from mender.util import timeutil

log = logging.getLogger(__name__)


class Context:
    """Class for storing the state-machine context"""

    def __init__(self):
        self.private_key = None
        self.config = config.Config({}, {})
        self.identity_data = {}


class StateMachine:
    def run(self, context):
        pass


class State:
    def run(self, context):
        pass


class Init:
    def run(self, context, force_bootstrap=False):
        log.debug("InitState: run()")
        try:
            context.config = config.load(
                local_path=settings.PATHS.local_conf,
                global_path=settings.PATHS.global_conf,
            )
            log.info(f"Loaded configuration: {context.config}")
        except config.NoConfigurationFileError:
            log.error(
                "No configuration files found for the device."
                " Most likely, the device will not be functional."
            )
        identity_data = identity.aggregate(path=settings.PATHS.identity_scripts)
        context.identity_data = identity_data
        private_key = bootstrap.now(
            force_bootstrap=force_bootstrap, private_key_path=settings.PATHS.key
        )
        context.private_key = private_key
        context.inventory_timer = timeutil.IsItTime(
            context.config.InventoryPollIntervalSeconds
        )
        context.update_timer = timeutil.IsItTime(
            context.config.UpdatePollIntervalSeconds
        )
        context.retry_timer = timeutil.IsItTime(context.config.RetryPollIntervalSeconds)
        log.info("Try to load configuration for remote terminal")
        try:
            context.remoteTerminalConfig = config.load(
                local_path=settings.PATHS.local_remote_terminal_conf,
                global_path=settings.PATHS.global_remote_terminal_conf,
            )
            log.info(f"Loaded configuration: {context.remoteTerminalConfig}")
        except config.NoConfigurationFileError:
            log.error(
                "No configuration files for remote terminal found for the device."
                "Most likely, the remote terminal will not be functional."
            )
        log.debug(f"Init set context to: {context}")
        return context


##########################################


def run(force_bootstrap=False):
    while os.path.exists(settings.PATHS.lockfile_path):
        log.info(
            "A deployment is currently in progress, the client will go to sleep for 60 seconds"
        )
        time.sleep(settings.SLEEP_INTERVAL)
    m = Master(force_bootstrap)
    m.run(m.context)


class Master(StateMachine):
    def __init__(self, force_bootstrap=False):
        log.info("Initializing the state-machine")
        context = Context()
        self.context = Init().run(context, force_bootstrap=force_bootstrap)
        self.context.authorized = False
        self.unauthorized_machine = UnauthorizedStateMachine()
        self.authorized_machine = AuthorizedStateMachine()
        log.info("Finished setting up the state-machine")
        self.quit = False

    def run(self, context):
        log.debug(f"Initialized context: {self.context}")
        log.parent.deployment_log_handler.disable()
        while not self.quit:
            self.unauthorized_machine.run(self.context)
            self.authorized_machine.run(self.context)


#
# Hierarchical
#
# i.e., Authorized, and Unauthorized state-machine
#


class Authorize(State):
    def run(self, context):
        if not context.retry_timer.is_it_time():
            return None

        log.info("Authorizing...")
        log.debug(f"Current context: {context}")
        return authorize.request(
            context.config.ServerURL,
            context.config.TenantToken,
            context.identity_data,
            context.private_key,
            context.config.ServerCertificate,
        )


class Idle(State):
    def run(self, context):
        log.info("Idling...")
        timeutil.sleep(context.retry_timer)
        return True


class UnauthorizedStateMachine(StateMachine):
    """Handle Wait, and Authorize attempts"""

    def __init__(self):
        pass

    def run(self, context):
        while True:
            JWT = Authorize().run(context)
            if JWT:
                context.JWT = JWT
                context.authorized = True
                return
            Idle().run(context)


class AuthorizedStateMachine(StateMachine):
    """Handle Inventory update, and update check"""

    def run(self, context):
        while context.authorized:
            try:
                IdleStateMachine().run(context)  # Idle returns when an update is ready
                UpdateStateMachine().run(
                    context
                )  # Update machine runs when idle detects an update
            except HTTPUnathorized:
                context.authorized = False
                return


#
# Second layered machine (below Authorized)
#


class SyncInventory(State):
    def run(self, context):
        if not context.inventory_timer.is_it_time():
            return

        log.info("Syncing the inventory...")
        inventory_data = inventory.aggregate(
            settings.PATHS.inventory_scripts,
            settings.PATHS.device_type,
            settings.PATHS.artifact_info,
        )
        if inventory_data:
            log.debug(f"aggregated inventory data: {inventory_data}")
            if not client_inventory.request(
                context.config.ServerURL,
                context.JWT,
                inventory_data,
                context.config.ServerCertificate,
                method="PUT",
            ):
                log.info("Falling back to to updating the inventory with PATCH")
                # Ignoring the returned error. It will only be logged
                if not client_inventory.request(
                    context.config.ServerURL,
                    context.JWT,
                    inventory_data,
                    context.config.ServerCertificate,
                    method="PATCH",
                ):
                    log.error("Failed to submit the inventory")
                    return None
        else:
            log.info("No inventory data found")
            return None
        log.info("Inventory submitted successfully")


class SyncUpdate(State):
    def run(self, context):
        if not context.update_timer.is_it_time():
            return False

        log.info("Checking for updates...")
        device_type = devicetype.get(settings.PATHS.device_type)
        artifact_name = artifactinfo.get(settings.PATHS.artifact_info)
        deployment = deployments.request(
            context.config.ServerURL,
            context.JWT,
            device_type=device_type,
            artifact_name=artifact_name,
            server_certificate=context.config.ServerCertificate,
        )
        if deployment:
            context.deployment = deployment
            log.parent.deployment_log_handler.enable(reset=True)
            return True
        return False


class IdleStateMachine(AuthorizedStateMachine):
    def __init__(self):
        super().__init__()
        self.sync_inventory = SyncInventory()
        self.sync_update = SyncUpdate()
        self.remote_terminal = remoteterminal.RemoteTerminal()

    def run(self, context):
        while context.authorized:
            self.remote_terminal.run(context)
            self.sync_inventory.run(context)
            if self.sync_update.run(context):
                # Update available
                return
            timeutil.sleep(context.update_timer, context.inventory_timer)


#
# Updating - Run the update state-machine
#


class Download(State):
    def run(self, context):
        log.info("Running the Download state...")
        if deployments.download(
            context.deployment,
            artifact_path=os.path.join(
                settings.PATHS.artifact_download, "artifact.mender"
            ),
            server_certificate=context.config.ServerCertificate,
        ):
            if not deployments.report(
                context.config.ServerURL,
                deployments.STATUS_DOWNLOADING,
                context.deployment.ID,
                context.config.ServerCertificate,
                context.JWT,
                deployment_logger=None,
            ):
                log.error(
                    "Failed to report the deployment status 'downloading' to the Mender server"
                )
            return ArtifactInstall()
        log.warning(
            "The Artifact has not been properly downloaded due to lack of Internet access or a server failure."
        )
        log.warning(
            "The next attempt of the update will happen after the UpdatePollIntervalSeconds config variable."
        )
        return ArtifactFailure()


class ArtifactInstall(State):
    def run(self, context):
        log.info("Running the ArtifactInstall state...")
        ret = installscriptrunner.run_sub_updater(context.deployment.ID)
        if ret == installscriptrunner.INSTALL_SCRIPT_OK:
            log.info(
                "The client has successfully spawned the install-script process. Exiting. Goodbye!"
            )
            sys.exit(0)
            # return ArtifactReboot()
        elif ret in (
            installscriptrunner.INSTALL_SCRIPT_NOT_FOUND_ERROR,
            installscriptrunner.INSTALL_SCRIPT_PERMISSION_ERROR,
        ):
            if not deployments.report(
                context.config.ServerURL,
                deployments.STATUS_FAILURE,
                context.deployment.ID,
                context.config.ServerCertificate,
                context.JWT,
                deployment_logger=log,
            ):
                log.error(
                    "Failed to report the deployment status 'FAILURE' to the Mender server"
                )
        else:
            log.error(
                "The daemon should never reach this point. Something is wrong with the setup of the client."
            )
        sys.exit(1)
        # return ArtifactFailure()


class UnsupportedState(Exception):
    pass


class ArtifactReboot(State):
    def run(self, context):
        log.info("Running the ArtifactReboot state...")
        # return ArtifactCommit()
        raise UnsupportedState("ArtifactReboot is unhandled by the API client")


class ArtifactCommit(State):
    def run(self, context):
        log.info("Running the ArtifactCommit state...")
        # return ArtifactRollback()
        raise UnsupportedState("ArtifactCommit is unhandled by the API client")


class ArtifactRollback(State):
    def run(self, context):
        log.info("Running the ArtifactRollback state...")
        # return ArtifactRollbackReboot()
        raise UnsupportedState("ArtifactRollback is unhandled by the API client")


class ArtifactRollbackReboot(State):
    def run(self, context):
        log.info("Running the ArtifactRollbackReboot state...")
        # return ArtifactFailure()
        raise UnsupportedState("ArtifactRollbackReboot is unhandled by the API client")


class ArtifactFailure(State):
    def run(self, context):
        log.info("Running the ArtifactFailure state...")
        return _UpdateDone()
        # raise UnsupportedState("ArtifactFailure is unhandled by the API client")


class _UpdateDone(State):
    def __str__(self):
        return "done"

    def __eq__(self, other):
        return isinstance(other, _UpdateDone)

    def run(self, context):
        raise Exception(
            "_UpdateDone state should never run. It is simply a placeholder"
        )


class UpdateStateMachine(AuthorizedStateMachine):
    def __init__(self):
        super().__init__()
        self.current_state = Download()

    def run(self, context):
        """Generally it is assumed that download would succeded
         and the state would change to ArtifactInstall and that would do other things
         in external system scripts while this client exits.
         Still in real world scenario the download sometimes is failing due to the poor radio conditions
         and changes the state to ArtifactFailure.
         After returning from ArtifactFailure state we will need to return to IdleState
         in AuthorizedStateMachine to let it go another chance after some time."""
        while self.current_state != _UpdateDone():
            self.current_state = self.current_state.run(context)
            time.sleep(1)
