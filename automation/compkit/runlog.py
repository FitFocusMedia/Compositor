"""Progress and failures for long runs (a show's cull, grade and export), so one bad file never stops a show.

A step prints a line now and then ("grade  1,200/3,000  40%  1.9/s  about 16 min left") to stderr and, given a
folder, the same lines with the time to <folder>/<step>.log; each file that fails goes to <folder>/failures.csv
(time, step, file, error) and the run goes on to the next.
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import sys
import threading
import time
from pathlib import Path


def _duration(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.0f} s"
    if seconds < 5400:
        return f"{seconds / 60:.0f} min"
    return f"{seconds / 3600:.1f} h"


class Progress:
    def __init__(self, step: str, *, folder=None, unit: str = "files", every: float = 10.0, quiet: bool = False):
        self.step, self.unit, self.every, self.quiet = step, unit, every, quiet
        self.folder = Path(folder).expanduser() if folder else None
        if self.folder:
            self.folder.mkdir(parents=True, exist_ok=True)
        self.total = self.done = self.skipped = 0
        self.failures: list[tuple[str, str]] = []
        self.began = self.last = time.monotonic()
        self.lock = threading.Lock()
        self.writing = threading.Lock()

    def start(self, total: int, done: int = 0) -> "Progress":
        """`done` of `total` were finished by an earlier run: they're skipped and don't count toward the speed."""
        with self.lock:
            self.total, self.done, self.skipped = total, done, done
            self.began = self.last = time.monotonic()
        if done:
            self.note(f"{done:,} of {total:,} {self.unit} already done; continuing")
        else:
            self.note(f"{total:,} {self.unit}")
        return self

    def advance(self, count: int = 1) -> None:
        with self.lock:
            self.done += count
            now = time.monotonic()
            if now - self.last < self.every and self.done < self.total:
                return
            self.last = now
            worked, elapsed = self.done - self.skipped, now - self.began
            rate = worked / elapsed if elapsed > 0 else 0
            left = (self.total - self.done) / rate if rate else 0
            line = (f"{self.done:,}/{self.total:,}  {100 * self.done / max(1, self.total):.0f}%  "
                    f"{rate:.1f}/s" + (f"  about {_duration(left)} left" if self.done < self.total else ""))
        self.note(line)

    def fail(self, item, error) -> None:
        message = " ".join(str(error).split())
        with self.lock:
            self.failures.append((str(item), message))
        self.note(f"FAILED {Path(str(item)).name}: {message}")
        if self.folder:
            path = self.folder / "failures.csv"
            new = not path.exists()
            with self.lock, open(path, "a", newline="") as file:
                writer = csv.writer(file)
                if new:
                    writer.writerow(["time", "step", "file", "error"])
                writer.writerow([dt.datetime.now().isoformat(timespec="seconds"), self.step, str(item), message])

    def note(self, text: str) -> None:
        line = f"{self.step}  {text}"
        if not self.quiet:
            print(line, file=sys.stderr, flush=True)
        if self.folder:
            now = dt.datetime.now().isoformat(timespec="seconds")
            with self.writing, open(self.folder / f"{self.step}.log", "a") as file:
                file.write(f"{now}  {line}\n")
            # The latest line of whichever step is running, for the dashboard.
            state = {"step": self.step, "text": text, "done": self.done, "total": self.total,
                     "failed": len(self.failures), "updated": now}
            with self.writing:
                temporary = self.folder / ".progress.json.tmp"
                temporary.write_text(json.dumps(state))
                temporary.replace(self.folder / "progress.json")

    def finish(self, **extra) -> dict:
        """The step's summary, logged: done, failed, how long, and anything in `extra`."""
        elapsed = time.monotonic() - self.began
        summary = {"step": self.step, "total": self.total, "done now": self.done - self.skipped - len(self.failures),
                   "already done": self.skipped, "failed": len(self.failures), "seconds": round(elapsed, 1), **extra}
        tail = f", {len(self.failures)} FAILED (see {self.folder / 'failures.csv'})" if self.failures and self.folder else \
            f", {len(self.failures)} FAILED" if self.failures else ""
        self.note(f"finished in {_duration(elapsed)}: {summary['done now']:,} done now, {self.skipped:,} already done{tail}")
        return summary
