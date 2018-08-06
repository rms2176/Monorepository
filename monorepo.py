# We disable this because fstrings are more readable than the older string formatting
# and we don't care about the performance penalty.
# pylint: disable=logging-fstring-interpolation

import errno
import hashlib
import json
import logging
import os
import shutil
import subprocess
import tempfile
from typing import List, Tuple

import argh
import yaml

import helpers
import http_file_stager


MONOREPOSITORY_ROOT, CURRENT_CODE_BASE_NAME = helpers.find_code_base_root()
CODEBASES = {}
BUILD_INFORMATION = helpers.BuildInformation()


def get_codebase(code_base_name):
    if code_base_name not in CODEBASES:
        CODEBASES[code_base_name] = CodeBase(code_base_name)

    return CODEBASES[code_base_name]


class CodeBase:
    def __init__(self, code_base_name):
        self.code_base_name = code_base_name
        self.code_base_root = os.path.join(MONOREPOSITORY_ROOT, code_base_name)
        logging.debug(f"Loading metadata for {code_base_name}...")
        self._load_metadata()
        self._compute_hash()
        self.artifacts_json_file_name = os.path.join(BUILD_INFORMATION.metadata_prefix,
                                                     f"artifacts-{self.code_base_name}-{self.hash.hexdigest()}.json")
        logging.debug(f"Hash for {code_base_name} is {self.hash.hexdigest()}.")

    def _load_metadata(self) -> None:
        with open(os.path.join(self.code_base_root, "metadata.yaml")) as metadata_yaml:
            self.metadata = yaml.load(metadata_yaml)

    def _get_sorted_file_list(self) -> List[str]:
        output = []
        for subdir, _dirs, files in os.walk(self.code_base_root):
            for file in files:
                file_name = os.path.join(subdir, file)
                output.append(file_name)
        return sorted(output)

    def _compute_hash(self) -> None:
        self.hash = hashlib.sha1()
        for dependency in self.metadata.get("dependencies", []):
            depedency_code_base = get_codebase(dependency)
            self.hash.update(depedency_code_base.hash.digest())
        for file_name in self._get_sorted_file_list():
            self.hash.update(file_name.encode("utf-8"))
            self.hash.update(str(os.stat(file_name).st_mode).encode("utf-8"))
            helpers.hash_file(self.hash, file_name)

    def attempt_restore_previous_build(self) -> bool:
        # Return if the restore succeeded.
        # TODO: This can be further optimized by keeping a record of which codebases have been installed in the prefix.
        # But in order to do this, we need to ensure code bases don't overwrite other code base's output.
        logging.debug(f"Checking if {self.artifacts_json_file_name} exists...")
        if os.path.isfile(self.artifacts_json_file_name):
            with open(self.artifacts_json_file_name) as artifacts_json:
                artifacts = json.load(artifacts_json)

            self.output_hashes_and_modes = artifacts["files"]
            cas_path = os.path.join(BUILD_INFORMATION.metadata_prefix, "cas")
            for file_name, hash_and_mode in self.output_hashes_and_modes.items():
                file_hash, file_mode = hash_and_mode
                os.makedirs(os.path.dirname(file_name), exist_ok=True)
                try:
                    os.link(os.path.join(cas_path, file_hash), file_name)
                except OSError as ose:
                    if ose.errno != errno.EEXIST:
                        raise
                os.chmod(file_name, file_mode)
            logging.debug(f"Restored {self.code_base_name} from previous build.")
            return True
        else:
            logging.debug(f"It appears {self.code_base_name} was not previously built.")
            return False

    def _stage_input_files(self, input_files_dir: str) -> None:

        input_file_names = [input_file["name"] for input_file in self.metadata.get("input_files", [])]
        if input_file_names:
            logging.debug("Staging input files...")
            os.makedirs(input_files_dir, exist_ok=True)
            my_http_file_stager = http_file_stager.HttpFileStager(input_files_dir, input_file_names)
            my_http_file_stager.stage()
            # TODO: Validate hashes here.
            logging.debug("Staged input files.")

    def build(self) -> None:
        if self.attempt_restore_previous_build():
            return

        for dependency in self.metadata.get("dependencies", []):
            depedency_code_base = get_codebase(dependency)
            depedency_code_base.build()

        stdout_log_name = os.path.join(BUILD_INFORMATION.metadata_prefix, f"{self.code_base_name}.out")
        stderr_log_name = os.path.join(BUILD_INFORMATION.metadata_prefix, f"{self.code_base_name}.err")
        stdout_log = open(stdout_log_name, 'w')
        stderr_log = open(stderr_log_name, 'w')

        os.chdir(self.code_base_root)

        # In order to avoid modifying the source (which would in turn modify the hash),
        # we build in a temp dir, which is a clone of the source.
        with tempfile.TemporaryDirectory() as temp_dir_parent:
            temp_dir = os.path.join(temp_dir_parent, self.code_base_name)
            shutil.copytree(self.code_base_root, temp_dir)
            self._stage_input_files(os.path.join(temp_dir, "input_files"))

            if os.path.isfile("build"):
                command = ["./build"]
            elif os.path.isfile("Makefile"):
                command = ["make"]
            else:
                raise Exception(f"Unable to determine how to build {self.code_base_name}")

            os.chdir(temp_dir)
            logging.debug(f"Building {self.code_base_name}...")
            logging.debug(f"Standard out log is: {stdout_log_name}")
            logging.debug(f"Standard error log is: {stderr_log_name}")
            try:
                subprocess.run(command, check=True,
                               env=helpers.get_builder_env(BUILD_INFORMATION.prefix),
                               stdout=stdout_log, stderr=stderr_log)
            finally:
                stdout_log.close()
                stderr_log.close()

        helpers.make_files_non_writeable(BUILD_INFORMATION.prefix)
        logging.debug(f"Built {self.code_base_name}.")
        self.output_hashes_and_modes = helpers.get_output_hashes_and_modes(BUILD_INFORMATION.prefix)
        logging.debug("Finished calculating hashes.")
        self._record_build()
        self._populate_cas()

    def _record_build(self) -> None:
        with open(self.artifacts_json_file_name, 'w') as artifacts_json:
            json.dump({
                "code_base": self.code_base_name,
                "hash": self.hash.hexdigest(),
                "files": self.output_hashes_and_modes,
            }, artifacts_json)

    def _populate_cas(self) -> None:
        cas_path = os.path.join(BUILD_INFORMATION.metadata_prefix, "cas")
        logging.debug("Populating content-addressable storage...")
        os.makedirs(cas_path, exist_ok=True)
        existing_blobs = set(os.listdir(cas_path))
        for file_name, hash_and_mode in self.output_hashes_and_modes.items():
            file_hash = hash_and_mode[0]
            if file_hash in existing_blobs:
                continue
            os.link(file_name, os.path.join(cas_path, file_hash))
            existing_blobs.add(file_hash)
        logging.debug("Populated content-addresable storage.")


@argh.arg("--prefix", help="Prefix to output build artifacts to")
@argh.arg("--metadata-prefix", help="Prefix to output build metadata to; the metadata should be on the same file system as the prefix")
@argh.arg("--debug", action="store_true", help="Enable debug logging")
def build(prefix: str = None, metadata_prefix: str = None, debug: bool = False) -> None:
    if debug:
        logging.basicConfig(level=logging.DEBUG)
    else:
        logging.basicConfig()

    logging.debug(f"Mono-repository root is {MONOREPOSITORY_ROOT}.")
    logging.debug(f"Current code base name is {CURRENT_CODE_BASE_NAME}.")

    if prefix is None:
        BUILD_INFORMATION.prefix = os.path.join(MONOREPOSITORY_ROOT, "prefix")
    else:
        BUILD_INFORMATION.prefix = prefix

    if metadata_prefix is None:
        BUILD_INFORMATION.metadata_prefix = os.path.join(MONOREPOSITORY_ROOT, "metadata_prefix")
    else:
        BUILD_INFORMATION.metadata_prefix = metadata_prefix

    os.makedirs(BUILD_INFORMATION.prefix, exist_ok=True)
    os.makedirs(BUILD_INFORMATION.metadata_prefix, exist_ok=True)

    code_base = get_codebase(CURRENT_CODE_BASE_NAME)
    code_base.build()


if __name__ == "__main__":
    argh.dispatch_commands([build])
