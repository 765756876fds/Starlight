"""First user trial: managed paths only, no private collection inventory or IO."""

import os
from pathlib import Path
import stat

from .goal_adapter import ReadonlyResources
from .goal_admission import SnapshotError, require_worker


class TrialResources(ReadonlyResources):
    collection_authorized = False

    def __init__(self, controller):
        self.c = controller
        # Lexical only: do not touch the filesystem while assembling the GUI.
        self._paths = tuple(Path(os.path.abspath(p)) for p in
                            (controller.db_path, controller.music_memory.path))
        self.root = self._paths[0].parent

    def inside(self, path):
        require_worker()
        candidate = Path(os.path.abspath(path))
        if (self.root == Path(self.root.anchor) or str(self.root).startswith("\\\\")
                or any(p.parent != self.root for p in self._paths)
                or candidate not in self._paths):
            raise SnapshotError("trial_managed_path_not_authorized")
        # Reject junctions/symlinks on every component, not only the leaf.
        for component in (*reversed(candidate.parents), candidate):
            try:
                info = component.lstat()
            except FileNotFoundError:
                if component == candidate:
                    continue  # A preference DB may not yet exist; never create it.
                raise SnapshotError("trial_managed_parent_missing") from None
            if (stat.S_ISLNK(info.st_mode)
                    or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
                raise SnapshotError("trial_reparse_path_not_authorized")
            if component == candidate and (not stat.S_ISREG(info.st_mode) or info.st_nlink > 1):
                raise SnapshotError("trial_managed_file_not_regular")
        if candidate.resolve() != candidate:
            raise SnapshotError("trial_managed_path_changed")
        return candidate
