"""You may replace this with your own version."""

# pylint: disable=line-too-long

import os
import pathlib
import shutil


def upload_artifact(archive_path):
    artifact_directory = os.path.join(pathlib.Path.home(), "monorepo_artifacts")
    os.makedirs(artifact_directory, exist_ok=True)
    destination = os.path.join(artifact_directory, os.path.basename(archive_path))
    shutil.copyfile(archive_path, destination)
