import errno
import hashlib
import os
import stat

from typing import Dict, List, Tuple

import _hashlib  # This is only for typing, since hashlib functions return _hashlib.HASH objects


class BuildInformation:
    def __init__(self):
        self.prefix = None
        self.metadata_prefix = None


def make_files_non_writeable(prefix: str) -> None:
    for subdir, _dirs, files in os.walk(prefix):
        for file in files:
            filename = os.path.join(subdir, file)
            new_mode = os.stat(filename).st_mode & ~stat.S_IWUSR & ~stat.S_IWGRP & ~stat.S_IWOTH
            os.chmod(filename, new_mode)


def hash_file(hash_object: _hashlib.HASH, file_name: str) -> None:
    # This adds the content of the file to the provided _hashlib.HASH object.
    # It does not return a hash of the file contents.
    with open(file_name, 'rb') as file:
        while True:
            data = file.read(4096 * 4)
            if not data:
                break
            hash_object.update(data)


def get_file_hash(file_name: str) -> str:
    sha1 = hashlib.sha1()
    hash_file(sha1, file_name)
    return sha1.hexdigest()


def get_output_hashes_and_modes(directory) -> Dict[str, Tuple[str, int]]:
    output_hashes_and_modes = {}
    output_symbolic_links = {}
    for subdir, _dirs, files in os.walk(directory):
        for file in files:
            file_name = os.path.join(subdir, file)
            if os.path.islink(file_name):
                output_symbolic_links[file_name] = os.readlink(file_name)
            else:
                output_hashes_and_modes[file_name] = [get_file_hash(file_name), os.stat(file_name).st_mode]
    return output_hashes_and_modes, output_symbolic_links


def get_builder_env(prefix: str):
    return {
        **os.environ,
        "PREFIX": prefix,
        "PATH": os.pathsep.join([os.path.join(prefix, "bin"),
                                 os.environ["PATH"]]),
    }


def find_code_base_root() -> Tuple[str, str]:
    # Return monorepository_root, code_base_name
    current_dir = os.getcwd()

    while current_dir != "/":
        one_directory_up = os.path.dirname(current_dir)
        if os.path.basename(one_directory_up) == "monorepository":
            return one_directory_up, os.path.basename(current_dir)
        current_dir = os.path.dirname(one_directory_up)
    raise Exception("Could not find monorepository root.")
