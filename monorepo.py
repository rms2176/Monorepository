# pylint: disable=fixme
# pylint: disable=line-too-long

# We disable this because fstrings are more readable than the older string formatting
# and we don't care about the performance penalty.
# pylint: disable=logging-fstring-interpolation

import datetime
import errno
import hashlib
import json
import logging
import os
import shutil
import subprocess
import tempfile
from typing import List, Optional

import argh
import yaml

import artifact_uploader
import helpers
import http_file_stager


MONOREPOSITORY_ROOT, CURRENT_CODE_BASE_NAME = helpers.find_code_base_root()
CODEBASES = {}
BUILD_INFORMATION = helpers.BuildInformation()
ORIGINAL_DIRECTORY = os.getcwd()


def get_codebase(code_base_name):
    if code_base_name not in CODEBASES:
        CODEBASES[code_base_name] = CodeBase(code_base_name)

    return CODEBASES[code_base_name]


class CodeBase:
    def __init__(self, code_base_name):
        self.code_base_name = code_base_name
        self.code_base_root = os.path.join(MONOREPOSITORY_ROOT, code_base_name)
        self.output_hashes_and_modes = {}
        self.output_symbolic_links = {}
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
        self.hash.update(BUILD_INFORMATION.prefix.encode("utf-8"))
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
            self.output_symbolic_links = artifacts["symbolic_links"]
            cas_path = os.path.join(BUILD_INFORMATION.metadata_prefix, "cas")
            for file_name, hash_and_mode in self.output_hashes_and_modes.items():
                file_hash, file_mode = hash_and_mode
                cas_name = f"{file_hash}-{file_mode}"
                os.makedirs(os.path.dirname(file_name), exist_ok=True)
                try:
                    os.link(os.path.join(cas_path, cas_name), file_name)
                except OSError as ose:
                    if ose.errno != errno.EEXIST:
                        raise
                os.chmod(file_name, file_mode)

            for symbolic_link_name, symbolic_link_target in self.output_symbolic_links.items():
                os.makedirs(os.path.dirname(symbolic_link_name), exist_ok=True)
                try:
                    os.symlink(symbolic_link_target, symbolic_link_name)
                except OSError as ose:
                    if ose.errno != errno.EEXIST:
                        raise

            logging.debug(f"Restored {self.code_base_name} from previous build.")
            return True

        logging.debug(f"It appears {self.code_base_name} was not previously built.")
        return False

    def _stage_input_files(self, input_files_dir: str) -> None:
        input_file_names = [input_file["name"] for input_file in self.metadata.get("input_files", [])]
        if input_file_names:
            logging.debug("Staging input files...")
            os.makedirs(input_files_dir, exist_ok=True)
            http_file_stager.stage(input_files_dir, input_file_names)
            # TODO: Validate hashes here.
            logging.debug("Staged input files.")

    def build(self, skip_postbuild=False) -> None:
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
        if not skip_postbuild:
            self.output_hashes_and_modes, self.output_symbolic_links = helpers.get_output_hashes_and_modes(BUILD_INFORMATION.prefix)
            logging.debug("Finished calculating hashes.")
            self._record_build()
            self._populate_cas()

    def _record_build(self) -> None:
        with open(self.artifacts_json_file_name, 'w') as artifacts_json:
            json.dump({
                "code_base": self.code_base_name,
                "prefix": BUILD_INFORMATION.prefix,
                "hash": self.hash.hexdigest(),
                "files": self.output_hashes_and_modes,
                "symbolic_links": self.output_symbolic_links,
            }, artifacts_json)

    def _populate_cas(self) -> None:
        cas_path = os.path.join(BUILD_INFORMATION.metadata_prefix, "cas")
        logging.debug("Populating content-addressable storage...")
        os.makedirs(cas_path, exist_ok=True)
        existing_blobs = set(os.listdir(cas_path))
        for file_name, hash_and_mode in self.output_hashes_and_modes.items():
            file_hash, file_mode = hash_and_mode
            cas_name = f"{file_hash}-{file_mode}"
            if cas_name in existing_blobs:
                continue
            os.link(file_name, os.path.join(cas_path, cas_name))
            existing_blobs.add(cas_name)
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
        logging.debug(f"Cleaning up prefix directory {BUILD_INFORMATION.prefix}...")
        if os.path.isdir(BUILD_INFORMATION.prefix):
            shutil.rmtree(BUILD_INFORMATION.prefix)
        logging.debug(f"Finished cleaning up prefix directory.")
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

    # "postbuild" is a special codebase that gets built after all others
    if os.path.isdir(os.path.join(MONOREPOSITORY_ROOT, "postbuild")):
        code_base = get_codebase("postbuild")
        code_base.build(skip_postbuild=True)


@argh.arg("--prefix", help="Prefix to output build artifacts to")
@argh.arg("--metadata-prefix", help="Prefix to output build metadata to; the metadata should be on the same file system as the prefix")
@argh.arg("--debug", action="store_true", help="Enable debug logging")
@argh.arg("--archive-name", help="The name of the pack (defaults to <codebase>-<date>-<codebase-hash>)")
def upload(prefix: str = None, metadata_prefix: str = None, debug: bool = False, archive_name: Optional[str] = None):
    build(prefix, metadata_prefix, debug)

    # Jump back to original directory
    os.chdir(ORIGINAL_DIRECTORY)

    # The build process filled in the prefix to which it actually built
    prefix = BUILD_INFORMATION.prefix

    if archive_name is None:
        printable_date = datetime.datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
        codebase_hash = get_codebase(CURRENT_CODE_BASE_NAME).hash.hexdigest()
        archive_name = f"{CURRENT_CODE_BASE_NAME}-{printable_date}-{codebase_hash}"

    with tempfile.TemporaryDirectory() as temp_dir_parent:
        archive_path = os.path.join(temp_dir_parent, f"{archive_name}.tar.xz")
        logging.debug(f"Archiving {prefix} to {archive_path}...")
        # We shell out to tar because I assume the Python version doesn't support multithreading.
        # I didn't actually check though.
        subprocess.run(["tar", "--create", "--xz", "--file", archive_path, prefix],
                       check=True, env={"XZ_OPT": "--threads=0 -0", **os.environ})
        logging.debug(f"Done archiving. Uploading {archive_name}...")
        artifact_uploader.upload_artifact(archive_path)
        logging.debug(f"Done uploading.")


if __name__ == "__main__":
    argh.dispatch_commands([build, upload])
