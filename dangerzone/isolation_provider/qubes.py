import asyncio
import glob
import inspect
import io
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from typing import IO, Callable, Optional

from ..conversion.common import calculate_timeout, running_on_qubes
from ..conversion.errors import exception_from_error_code
from ..conversion.pixels_to_pdf import PixelsToPDF
from ..document import Document
from ..util import (
    Stopwatch,
    get_resource_path,
    get_subprocess_startupinfo,
    get_tmp_dir,
    nonblocking_read,
)
from .base import MAX_CONVERSION_LOG_CHARS, IsolationProvider

log = logging.getLogger(__name__)

CONVERTED_FILE_PATH = (
    # FIXME won't work for parallel conversions (see #454)
    "/tmp/safe-output-compressed.pdf"
)

# The maximum time a qube takes to start up.
STARTUP_TIME_SECONDS = 5 * 60  # 5 minutes


def read_bytes(f: IO[bytes], size: int, timeout: float, exact: bool = True) -> bytes:
    """Read bytes from a file-like object."""
    buf = nonblocking_read(f, size, timeout)
    if exact and len(buf) != size:
        raise ValueError("Did not receive exact number of bytes")
    return buf


def read_int(f: IO[bytes], timeout: float) -> int:
    """Read 2 bytes from a file-like object, and decode them as int."""
    untrusted_int = read_bytes(f, 2, timeout)
    return int.from_bytes(untrusted_int, signed=False)


def read_debug_text(f: IO[bytes], size: int) -> str:
    """Read arbitrarily long text (for debug purposes)"""
    timeout = calculate_timeout(size)
    untrusted_text = read_bytes(f, size, timeout, exact=False)
    return untrusted_text.decode("ascii", errors="replace")


class Qubes(IsolationProvider):
    """Uses a disposable qube for performing the conversion"""

    def install(self) -> bool:
        return True

    def _convert(
        self,
        document: Document,
        ocr_lang: Optional[str] = None,
    ) -> bool:
        success = False

        # FIXME won't work on windows, nor with multi-conversion
        out_dir = Path("/tmp/dangerzone")
        if out_dir.exists():
            shutil.rmtree(out_dir)
        out_dir.mkdir()

        # Reset hard-coded state
        if os.path.exists(CONVERTED_FILE_PATH):
            os.remove(CONVERTED_FILE_PATH)

        percentage = 0.0

        with open(document.input_filename, "rb") as f:
            # TODO handle lack of memory to start qube
            if getattr(sys, "dangerzone_dev", False):
                # Use dz.ConvertDev RPC call instead, if we are in development mode.
                # Basically, the change is that we also transfer the necessary Python
                # code as a zipfile, before sending the doc that the user requested.
                p = subprocess.Popen(
                    ["/usr/bin/qrexec-client-vm", "@dispvm:dz-dvm", "dz.ConvertDev"],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                assert p.stdin is not None

                # Send the dangerzone module first.
                self.teleport_dz_module(p.stdin)

                # Finally, send the document, as in the normal case.
                p.stdin.write(f.read())
                p.stdin.close()
            else:
                p = subprocess.Popen(
                    ["/usr/bin/qrexec-client-vm", "@dispvm:dz-dvm", "dz.Convert"],
                    stdin=f,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                )

            # Get file size (in MiB)
            size = os.path.getsize(document.input_filename) / 1024**2
            timeout = calculate_timeout(size) + STARTUP_TIME_SECONDS

            assert p.stdout is not None
            os.set_blocking(p.stdout.fileno(), False)

            try:
                n_pages = read_int(p.stdout, timeout)
            except ValueError:
                error_code = p.wait()
                # XXX Reconstruct exception from error code
                raise exception_from_error_code(error_code)  # type: ignore [misc]

            if n_pages == 0:
                # FIXME: Fail loudly in that case
                return False
            if ocr_lang:
                percentage_per_page = 50.0 / n_pages
            else:
                percentage_per_page = 100.0 / n_pages

            timeout = calculate_timeout(size, n_pages)
            sw = Stopwatch(timeout)
            sw.start()
            for page in range(1, n_pages + 1):
                # TODO handle too width > MAX_PAGE_WIDTH
                # TODO handle too big height > MAX_PAGE_HEIGHT
                width = read_int(p.stdout, timeout=sw.remaining)
                height = read_int(p.stdout, timeout=sw.remaining)
                untrusted_pixels = read_bytes(
                    p.stdout,
                    width * height * 3,
                    timeout=sw.remaining,
                )  # three color channels

                # Wrapper code
                with open(f"/tmp/dangerzone/page-{page}.width", "w") as f_width:
                    f_width.write(str(width))
                with open(f"/tmp/dangerzone/page-{page}.height", "w") as f_height:
                    f_height.write(str(height))
                with open(f"/tmp/dangerzone/page-{page}.rgb", "wb") as f_rgb:
                    f_rgb.write(untrusted_pixels)

                percentage += percentage_per_page

                text = f"Converting page {page}/{n_pages} to pixels"
                self.print_progress_trusted(document, False, text, percentage)

        # Ensure nothing else is read after all bitmaps are obtained
        p.stdout.close()

        # TODO handle leftover code input
        text = "Converted document to pixels"
        self.print_progress_trusted(document, False, text, percentage)

        if getattr(sys, "dangerzone_dev", False):
            assert p.stderr is not None
            os.set_blocking(p.stderr.fileno(), False)
            untrusted_log = read_debug_text(p.stderr, MAX_CONVERSION_LOG_CHARS)
            p.stderr.close()
            log.info(
                f"Conversion output (doc to pixels)\n{self.sanitize_conversion_str(untrusted_log)}"
            )

        def print_progress_wrapper(error: bool, text: str, percentage: float) -> None:
            self.print_progress_trusted(document, error, text, percentage)

        asyncio.run(
            PixelsToPDF(progress_callback=print_progress_wrapper).convert(ocr_lang)
        )

        percentage = 100.0
        text = "Safe PDF created"
        self.print_progress_trusted(document, False, text, percentage)

        shutil.move(CONVERTED_FILE_PATH, document.output_filename)
        success = True

        return success

    def get_max_parallel_conversions(self) -> int:
        return 1

    def teleport_dz_module(self, wpipe: IO[bytes]) -> None:
        """Send the dangerzone module to another qube, as a zipfile."""
        # Grab the absolute file path of the dangerzone module.
        import dangerzone.conversion as _conv

        _conv_path = Path(inspect.getfile(_conv)).parent
        temp_file = io.BytesIO()

        # Create a Python zipfile that contains all the files of the dangerzone module.
        with zipfile.PyZipFile(temp_file, "w") as z:
            z.mkdir("dangerzone/")
            z.writestr("dangerzone/__init__.py", "")
            z.writepy(str(_conv_path), basename="dangerzone/")

        # Send the following data:
        # 1. The size of the Python zipfile, so that the server can know when to
        #    stop.
        # 2. The Python zipfile itself.
        bufsize_bytes = len(temp_file.getvalue()).to_bytes(4)
        wpipe.write(bufsize_bytes)
        wpipe.write(temp_file.getvalue())


def is_qubes_native_conversion() -> bool:
    """Returns True if the conversion should be run using Qubes OS's diposable
    VMs and False if not."""
    if running_on_qubes():
        if getattr(sys, "dangerzone_dev", False):
            return os.environ.get("QUBES_CONVERSION", "0") == "1"

        # XXX If Dangerzone is installed check if container image was shipped
        # This disambiguates if it is running a Qubes targetted build or not
        # (Qubes-specific builds don't ship the container image)

        compressed_container_path = get_resource_path("container.tar.gz")
        return not os.path.exists(compressed_container_path)
    else:
        return False
