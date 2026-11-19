"""The one owner of artifact-job size, count and time limits.

Every contract that bounds a job input, output or timeout imports these;
no module restates the numbers.
"""

MAX_INPUT_FILES = 32
MAX_INPUT_FILE_BYTES = 512 * 1024**2
MAX_INPUT_TOTAL_BYTES = 1024**3
MAX_OUTPUT_FILES = 32
MAX_OUTPUT_FILE_BYTES = 1024**3
MAX_OUTPUT_TOTAL_BYTES = 2 * 1024**3
MAX_TIMEOUT_SECONDS = 60 * 60
