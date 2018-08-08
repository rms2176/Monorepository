"""You may replace this with your own version."""

# pylint: disable=line-too-long

import os
import urllib.parse
import urllib.request
from typing import List

BASE_URL = "http://localhost:8000/"

class HttpFileStager:
    def __init__(self, input_files_dir: str, input_file_names: List[str]) -> None:
        self.input_files_dir = input_files_dir
        self.input_file_names = input_file_names

    def stage(self) -> None:
        for file_name in self.input_file_names:
            url = urllib.parse.urljoin(BASE_URL, file_name)
            destination = os.path.join(self.input_files_dir, file_name)
            urllib.request.urlretrieve(url, destination)
