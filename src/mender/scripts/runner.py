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
import subprocess

from mender.settings import settings

log = logging.getLogger(__name__)

INSTALL_SCRIPT_OK = 0
INSTALL_SCRIPT_NOT_FOUND_ERROR = 1
INSTALL_SCRIPT_PERMISSION_ERROR = 2


def run_sub_updater(deployment_id: str) -> int:
    """run_sub_updater runs the /usr/share/mender/install script"""
    log.info(f"Running the sub-updater script at {settings.PATHS.install_script}")
    if not os.path.exists(settings.PATHS.install_script):
        log.error(f"No install script found at '{settings.PATHS.install_script}'")
        return INSTALL_SCRIPT_NOT_FOUND_ERROR
    try:
        # Store the deployment ID in the update lockfile
        with open(settings.PATHS.lockfile_path, "w") as f:
            f.write(deployment_id)
        subprocess.Popen(
            [
                f"{settings.PATHS.install_script}",
                settings.PATHS.artifact_download + "/artifact.mender",
            ]
        )
        return INSTALL_SCRIPT_OK
    except PermissionError as e:
        log.error(
            f"The install script '{settings.PATHS.install_script}' has the wrong permissions."
        )
        log.error(f"Error {e}")
    return INSTALL_SCRIPT_PERMISSION_ERROR
