"""
End-of-turn artifact.
"""
import logging
import os

import sublime

from . import utils

LOG = logging.getLogger("TermMate")

ARTIFACT_REGION_KEY = "chatview_artifact_files"
ARTIFACT_REGION_SCOPE = "region.bluish"
ARTIFACT_REGION_FLAGS = sublime.DRAW_NO_FILL | sublime.DRAW_NO_OUTLINE | sublime.HIDDEN
DIFF_VIEW_PATH_KEY = "chatview_artifact_diff_path"


class FileChangesArtifact:
    """
    Per-session store and renderer for the file changes artifact.

    Records edit diffs during a conversation turn and renders a collapsed,
    clickable raw-text list of changed files in the chat view when the
    conversation stops.

    :param view: the chat view the artifact is rendered into.
    :param window: the Sublime window (used to open diff views).
    :param input_start_key: settings key holding the chat input start position.
    """

    def __init__(self, view, window, input_start_key):
        self.view = view
        self.window = window
        self.input_start_key = input_start_key
        self.file_changes = {}  # abs_path -> {"rel_path": str, "diffs": [], "add": int, "del": int}
        self.pending_changed_files = []  # abs paths changed since last render
        self.file_regions = []  # List of (Region, abs_path, rel_path, diffs_snapshot) for rendered file lines

    @staticmethod
    def _count_diff_lines(diff_text):
        """Return (additions, deletions) from unified diff text."""
        add = del_ = 0
        for line in (diff_text or "").splitlines():
            if line.startswith("+") and not line.startswith("+++"):
                add += 1
            elif line.startswith("-") and not line.startswith("---"):
                del_ += 1
        return add, del_

    def record(self, abs_path, rel_path, diff_text):
        """Record an edit diff so the file is listed in the end-of-turn artifact."""
        entry = self.file_changes.setdefault(
            abs_path, {"rel_path": rel_path, "diffs": [], "add": 0, "del": 0})
        if diff_text:
            entry["diffs"].append(diff_text)
            a, d = self._count_diff_lines(diff_text)
            entry["add"] += a
            entry["del"] += d
        if abs_path not in self.pending_changed_files:
            self.pending_changed_files.append(abs_path)

    def show(self):
        """Append a collapsed raw-text list of files changed this turn.

        Each file name line gets a clickable region that opens a diff view;
        the list is folded by default and expands via the gutter fold arrow.
        """
        if not self.pending_changed_files:
            return
        files = self.pending_changed_files
        self.pending_changed_files = []

        view = self.view
        # term_chat_output_append inserts at input_start - 1
        base = view.settings().get(self.input_start_key, view.size()) - 1

        count = len(files)
        header = "▣ {} file{} changed".format(count, "s" if count != 1 else "")
        text = "\n" + header + "\n"
        new_regions = []
        for abs_path in files:
            entry = self.file_changes.pop(abs_path, {})
            rel = entry.get("rel_path") or abs_path
            add = entry.get("add", 0)
            del_ = entry.get("del", 0)
            stat = "  +{} -{}".format(add, del_) if (add or del_) else ""
            start = base + len(text) + 4
            text += "    " + rel + stat + "\n"
            new_regions.append((sublime.Region(start, start + len(rel)), abs_path, rel, list(entry.get("diffs", []))))

        # Fold from end of header line to end of last file line (excl. trailing \n)
        fold_start = base + 1 + len(header)
        fold_end = base + len(text) - 1

        # Non-blank zero-indent terminator on the line after the gutter fold arrow
        # The char below is (NBSP): renders blank, not indent whitespace.
        text += " "

        view.run_command("term_chat_output_append", {"text": text})

        self.file_regions.extend(new_regions)  # (Region, abs_path, rel_path, diffs_snapshot)
        self._redraw_regions()
        view.fold(sublime.Region(fold_start, fold_end))

    def _redraw_regions(self):
        """Redraw underline regions for all artifact file name lines."""
        if self.file_regions:
            self.view.add_regions(
                ARTIFACT_REGION_KEY,
                [r for r, *_ in self.file_regions],
                ARTIFACT_REGION_SCOPE,
                "",
                ARTIFACT_REGION_FLAGS
            )
        else:
            self.view.erase_regions(ARTIFACT_REGION_KEY)

    def open_diff_at(self, point):
        """If point is on an artifact file name, open its diff view. Returns True if handled."""
        if point is None:
            return False
        # Let clicks on a folded (collapsed) artifact expand it natively
        if hasattr(self.view, "folded_regions"):
            for folded in self.view.folded_regions():
                if folded.contains(point):
                    return False
        for region, abs_path, rel_path, diffs in self.file_regions:
            if region.contains(point):
                self._open_diff(abs_path, rel_path, diffs)
                return True
        return False

    def _open_diff(self, abs_path, rel_path, diffs):
        """Open a read-only diff view for the given turn's diffs."""
        if not diffs:
            sublime.status_message("No recorded changes for this file")
            return
        parts = ["diff a/{0} b/{0}\n--- a/{0}\n+++ b/{0}\n".format(rel_path)]
        for d in diffs:
            parts.append(d if d.endswith("\n") else d + "\n")

        # Close a stale diff view for the same file before reopening
        for v in self.window.views():
            if v.settings().get(DIFF_VIEW_PATH_KEY) == abs_path:
                v.close()
                break

        name = os.path.basename(rel_path) + " (changes)"
        diff_view = utils.show_diff_text(self.window, "".join(parts), name)
        diff_view.settings().set(DIFF_VIEW_PATH_KEY, abs_path)

    def truncate(self, cut_point):
        """Drop artifact file regions that fall inside a truncated tail (rewind)."""
        self.pending_changed_files = []
        self.file_changes = {}
        self.file_regions = [
            (r, p, rel, diffs) for r, p, rel, diffs in self.file_regions if r.end() < cut_point
        ]
        self._redraw_regions()

    def clear(self):
        """Reset all recorded file changes and artifact regions."""
        self.file_changes = {}
        self.pending_changed_files = []
        self.file_regions = []
        self.view.erase_regions(ARTIFACT_REGION_KEY)
