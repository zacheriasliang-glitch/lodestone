"""Fresh local teaching records and explicit, separate native-memory imports.

Only the selected runtime's existing LearningMemory and learning lifecycle are
used. Bootstrap examples are authored teaching, not observations of the user.
Imports preserve record bytes but never copy host policy, grants, leases, queues,
launchers or credentials. Nothing here starts a service or a model.
"""
from contextlib import contextmanager, ExitStack
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import uuid

# The worker uses -I, which deliberately excludes the script directory. Load
# this bundled installer helper explicitly; no user workspace joins sys.path.
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
from platform_support import (atomic_write, check_path, durable_move, file_lock,
                              guarded_directory, read_file)


BOOTSTRAP_SCHEMA = "lodestone-desktop-learning-bootstrap/v1"
IMPORT_SCHEMA = "lodestone-desktop-learning-import/v1"
OBSERVER_LABEL = "com.lodestone.desktop-preview.learning"
AUTHOR = "Lodestone Desktop Preview bootstrap teaching"
PURPOSE = ("Use relevant retained corrections with their conditions and limits; "
           "retain the actual outcome and attributed correction for later practice.")
MAX_IMPORT_FILES = 2048
MAX_IMPORT_BYTES = 128 * 1024 * 1024
MAX_RECORD_BYTES = 16 * 1024 * 1024
RECORD_NAME = re.compile(r"[0-9a-f]{64}\.json\Z")
OWNER_NAME = re.compile(r"[a-z][a-z0-9_-]{0,47}\Z")


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _path(value):
    path = Path(value).expanduser().absolute()
    if path.is_symlink():
        raise ValueError("Symlink destinations are not supported: " + str(path))
    if os.name == "nt":
        check_path(path)
    return check_path(path.parent.resolve() / path.name)


def _child(root, *parts):
    path = root
    for part in parts:
        path = path / part
        check_path(path)
    return path


def _read(path, maximum=32768):
    raw = read_file(path, maximum)
    if len(raw) > maximum:
        raise ValueError("Configuration exceeds its byte bound")
    return json.loads(raw)


def _create(path, value):
    """Publish complete metadata without replacing an existing file."""
    _write_new(path, _canonical(value) + b"\n")


def _write_new(path, data):
    path = check_path(path)
    try:
        atomic_write(path, data, replace=False)
    except FileExistsError:
        if read_file(path) != data:
            raise ValueError("Existing learning metadata differs: " + str(path))


@contextmanager
def _exclusive(path):
    with file_lock(path):
        yield


def _invoke(operation, runtime, python, values):
    runtime = _path(runtime)
    interpreter = Path(python).expanduser().absolute()  # Preserve venv symlinks.
    if not interpreter.is_file() or not os.access(interpreter, os.X_OK):
        raise ValueError("A usable local Python interpreter is required")
    if not (runtime / "src/lodestone_machinery/bezoar_learning.py").is_file():
        raise ValueError("Selected release lacks the native learning runtime")
    result = subprocess.run(
        [str(interpreter), "-I", "-B", str(Path(__file__).resolve()),
         "--worker", operation, str(runtime), str(interpreter)],
        input=_canonical(values), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=180, check=False)
    if result.returncode:
        raise ValueError(result.stderr.decode("utf-8", errors="replace").strip()[:2000]
                         or "Native learning setup failed")
    return json.loads(result.stdout)


def initialize(root: Path, runtime: Path, python: Path) -> dict:
    """Create or verify this preview's teaching workspace, without activation.

    An existing valid preview selection is preserved. An unrelated pre-existing
    workspace is refused. The parent installer owns runtime pointers and rollback.
    """
    return _invoke("initialize", runtime, python, {"root": str(_path(root))})


def import_memory(source: Path, destination: Path, runtime: Path, python: Path,
                  *, seed_handles, history_references=False) -> dict:
    """Copy native record bytes into a NEW independent workspace.

    Explicit SEED selection is required. The returned config is optional practice
    input; this function never selects it as the installation's default. No source
    code, host policy, queue or executable is imported, and no run is replayed.
    """
    if (not isinstance(seed_handles, (list, tuple)) or not 1 <= len(seed_handles) <= 16
            or any(type(handle) is not str for handle in seed_handles)
            or len(set(seed_handles)) != len(seed_handles)):
        raise ValueError("Import requires 1..16 explicitly selected distinct SEED handles")
    if type(history_references) is not bool:
        raise ValueError("Reference-history import must be an explicit Boolean choice")
    values = {"source": str(_path(source)), "destination": str(_path(destination)),
              "seed_handles": list(seed_handles)}
    if history_references:
        values["history_references"] = True
    return _invoke("import", runtime, python, values)


def reconcile(root: Path, runtime: Path, python: Path) -> dict:
    """Reconcile abandoned practice once; active work stays BUSY, never replayed."""
    return _invoke("reconcile", runtime, python, {"root": str(_path(root))})


def select_import(root: Path, destination: Path, runtime: Path, python: Path) -> dict:
    """Explicitly select an already-verified import for future local practice.

    Both the preview's learning config and its own Origami policy are updated;
    only memory/seed selection changes. Exact previous bytes are saved, and an
    ordinary write failure restores them. No source policy or grants are adopted.
    """
    return _invoke("select", runtime, python,
                   {"root": str(_path(root)), "destination": str(_path(destination))})


def validate_selection(root: Path, runtime: Path, python: Path) -> dict:
    """Check the selected memory with a specific runtime, without activation."""
    return _invoke("validate-selection", runtime, python, {"root": str(_path(root))})


def _reference_capability():
    """Resolve real APIs from the selected interpreter's runtime, not metadata."""
    from lodestone_machinery.bezoar_learning import LearningMemory
    from lodestone_machinery import history_inventory
    from lodestone_machinery.learning_context import history_window_policy
    if (history_inventory.SCHEMA != "bezoar-learning-history-inventory/v1"
            or not callable(getattr(LearningMemory, "from_active", None))
            or history_window_policy(1) != {
                "schema": "loom-reader-history-window/v1", "keep_latest_runs": 1}):
        raise ValueError("Selected runtime does not support reference-history practice")


def _selection_policy(origami, config):
    """Validate the paired selection; this does not admit non-windowed Origami."""
    from lodestone_machinery.learning_context import read_learning_policy
    if config.get("schema") != "lodestone-learning-active/v2":
        return read_learning_policy(origami)
    _reference_capability()
    policy = _read(_child(origami, "learning-policy.json"))
    if (type(policy) is not dict or set(policy) != {
            "schema", "required", "memory_workspace", "purpose", "seed_handles",
            "history_representation"}
            or policy.get("schema") != "origami-learning-policy/v2"
            or policy.get("required") is not True
            or policy.get("history_representation") != config.get("history_representation")
            or type(policy.get("purpose")) is not str or not policy["purpose"].strip()
            or len(policy["purpose"].encode("utf-8")) > 4096):
        raise ValueError("Invalid paired reference-history selection policy")
    return policy


def _validate_selection(root):
    from lodestone_machinery.yggdrasil_learning import validate_learning_records
    from lodestone_nurarihyon import learning
    root = _path(root)
    config = _read(_child(root, "learning", "ACTIVE.json"))
    memory = _selected(config)
    if not memory.workspace.is_relative_to(root):
        raise ValueError("Selected memory must remain inside this preview")
    policy = _selection_policy(_child(root, "runtime"), config)
    if (policy["memory_workspace"] != config["memory_workspace"]
            or policy["seed_handles"] != config["seed_handles"]):
        raise ValueError("Learning and Origami selections differ")
    with learning.weaving(memory), learning.locked(memory):
        learning._require_one_reader(memory)
        # STOP prevents practice, not read-only compatibility maintenance.
        # The weaving/reader locks and pending-run check still forbid switching
        # a live or unfinished encounter onto a different reader.
        validate_learning_records(memory.store)
        if memory.pending():
            raise ValueError("Pending practice prevents changing the reader runtime")
    return {"status": "COMPATIBLE", "active_schema": config["schema"],
            "memory_workspace": str(memory.workspace), "provider_calls": 0}


def _active(memory, seeds, *, history_references=False):
    result = {"schema": "lodestone-learning-active/v1", "required": True,
              "memory_workspace": str(memory), "seed_handles": seeds,
              "scope": "Explicit local practice; records are evidence and candidate advice, never authority."}
    if history_references:
        result.update(schema="lodestone-learning-active/v2",
                      history_representation="bezoar-learning-history-inventory/v1")
    return result


def _policy(memory, seeds, *, history_references=False):
    result = {"schema": "origami-learning-policy/v1", "required": True,
              "memory_workspace": str(memory), "purpose": PURPOSE, "seed_handles": seeds}
    if history_references:
        # Fail closed in old and currently non-windowed Origami routes. The
        # initial new capability is explicit Loom adapter practice only.
        result.update(schema="origami-learning-policy/v2",
                      history_representation="bezoar-learning-history-inventory/v1")
    return result


def _selected(config):
    from lodestone_machinery.bezoar_learning import LearningMemory
    from lodestone_nurarihyon import learning
    memory_path = _path(config["memory_workspace"])
    if not memory_path.is_dir() or str(memory_path) != config["memory_workspace"]:
        raise ValueError("Selected memory must be an existing resolved absolute directory")
    if callable(getattr(LearningMemory, "from_active", None)):
        memory = LearningMemory.from_active(config)
    else:
        # Compatibility checks also execute against older installed readers.
        # Their original v1 representation is the only admissible fallback.
        if (config.get("schema") != "lodestone-learning-active/v1"
                or config.get("required") is not True or "history_representation" in config):
            raise ValueError("Selected runtime does not support this memory representation")
        marker = memory_path / "learning-history-format.json"
        if marker.exists() or marker.is_symlink():
            raise ValueError("Legacy runtime cannot validate a reference-history workspace marker")
        memory = LearningMemory(memory_path)
    with learning.locked(memory):
        seeds = memory.resolve_seeds(config["seed_handles"])
        memory._pairs(seeds, None)  # Require real linked SEED/NACRE conditions.
    return memory


def _bootstrap(memory):
    from lodestone_nurarihyon import learning
    examples = (
        "Authored example A: an answer asserts that a task succeeded without an observed result. "
        "Correction: distinguish a proposed action from an observed outcome.",
        "Authored example B: advice repeats a previous correction after its conditions changed. "
        "Correction: check current conditions before reusing retained advice.")
    with learning.locked(memory):
        evidence = [memory.evidence({
            "source": "teaching://lodestone-desktop-preview/bootstrap-v1",
            "event_id": "example-" + str(index + 1),
            "observed_at": "2026-11-22T00:00:00Z", "text": text,
            "attribution": "Authored instructional example, dated to the bootstrap edition. "
                           "Not a user event, measured failure, model response or demonstrated learning."})
            for index, text in enumerate(examples)]
        seed = memory.seed({
            "id": "preview-evidence-and-correction", "author": AUTHOR,
            "correction": "Separate observations from proposals; check the conditions of retained advice. "
                          "Preserve actual failures and attributed corrections for the next applicable practice.",
            "conditions": ["An explicit local practice encounter needs evidence-aware advice."],
            "frame_limits": ["Generic authored teaching examples, not the user's history.",
                             "Candidate advice only; no demonstrated semantic learning or weight update.",
                             "A retained record cannot grant permission or require an action."],
            "evidence_handles": evidence})
        nacre = memory.nacre({
            "id": "preview-example-pattern", "author": AUTHOR,
            "pattern": "Two authored examples illustrate losing evidence or conditions when reusing advice; "
                       "this is a teaching pattern, not an observed recurrence in this user's work.",
            "conditions": ["Using the explicitly labelled bootstrap teaching examples."],
            "evidence_handles": evidence, "seed_handles": [seed]})
    return {"seed_handles": [seed], "nacre_handles": [nacre], "evidence_handles": evidence}


def _initialize(root, runtime, python):
    from lodestone_machinery.bezoar_learning import LearningMemory
    root = _path(root)
    learning_dir = _child(root, "learning")
    active_file = _child(root, "learning", "ACTIVE.json")
    plan_file = _child(root, "learning", "bootstrap-plan.json")
    memory_path = _child(root, "learning", "memory")
    origami = _child(root, "runtime")
    policy_file = _child(root, "runtime", "learning-policy.json")
    receipt_file = _child(root, "learning", "BOOTSTRAP.json")
    plan = {"schema": BOOTSTRAP_SCHEMA, "root": str(root),
            "default_memory": str(memory_path), "authority": "NONE", "edition": 1}
    if not plan_file.exists() and (active_file.exists() or receipt_file.exists()
                                  or memory_path.exists() and any(memory_path.iterdir())):
        raise ValueError("Existing learning state is not owned by this preview bootstrap; use explicit import")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with _exclusive(_child(root, ".learning-bootstrap.lock")):
        _create(plan_file, plan)
        if active_file.exists():
            config = _read(active_file)
            selected_path = _path(config["memory_workspace"])
            if not selected_path.is_relative_to(root):
                raise ValueError("Preview selection must stay in its installation data root")
            memory = _selected(config)
            if not receipt_file.is_file():
                # A bootstrap can stop between publishing the config and receipt.
                if selected_path != memory_path:
                    raise ValueError("Missing bootstrap receipt for a non-default selection")
                teaching = _bootstrap(memory)
                _create(receipt_file, {**plan, **teaching, "kind": "AUTHORED_TEACHING",
                                      "provider_calls": 0, "weight_update": False})
        else:
            memory = LearningMemory(memory_path)
            teaching = _bootstrap(memory)
            config = _active(memory_path, teaching["seed_handles"])
            _create(active_file, config)
            _create(receipt_file, {**plan, **teaching, "kind": "AUTHORED_TEACHING",
                                  "provider_calls": 0, "weight_update": False})
        if not policy_file.exists():
            _create(policy_file, _policy(memory.workspace, config["seed_handles"],
                                        history_references=config.get("schema") == "lodestone-learning-active/v2"))
        policy = _selection_policy(origami, config)
        if (policy["memory_workspace"] != config["memory_workspace"]
                or policy["seed_handles"] != config["seed_handles"]):
            raise ValueError("Existing Origami policy differs from the explicit learning selection")
        _selected(config)
    return {"schema": BOOTSTRAP_SCHEMA, "root": str(root),
            "active_config": str(active_file), "memory_workspace": str(memory.workspace),
            "origami_workspace": str(origami), "origami_policy": str(policy_file),
            "bootstrap_receipt": str(receipt_file), "seed_handles": config["seed_handles"],
            "observer_definition": {"Label": OBSERVER_LABEL,
                "ProgramArguments": [str(python), "-B", str(runtime / "tools/nurarihyon/learning-observer"),
                                     "--config", str(active_file), "--status-path", str(learning_dir / "OBSERVER-STATUS.json")],
                "WorkingDirectory": str(runtime), "RunAtLoad": True, "StartInterval": 60,
                "ProcessType": "Background", "LowPriorityIO": True},
            "services_activated": False, "provider_calls": 0, "weight_update": False}


def _record_paths(source):
    organs = _child(source, "organs")
    if not organs.is_dir():
        raise ValueError("Import source must be a native memory workspace containing organs")
    result = []
    for owner in sorted(organs.iterdir()):
        check_path(owner)
        if owner.is_symlink() or not owner.is_dir() or not OWNER_NAME.fullmatch(owner.name):
            raise ValueError("Invalid native organ directory")
        records = _child(source, "organs", owner.name, "records")
        if not records.exists():
            continue
        for path in sorted(records.iterdir()):
            if (path.is_symlink() or not path.is_file() or not RECORD_NAME.fullmatch(path.name)):
                raise ValueError("Only immutable canonical record files can be imported")
            check_path(path)
            result.append(path.relative_to(source))
            if len(result) > MAX_IMPORT_FILES:
                raise ValueError("Import exceeds 2048-record bound")
    if not result:
        raise ValueError("Import source contains no native records")
    return result


def _record_bytes(path):
    raw = read_file(path, MAX_RECORD_BYTES)
    if len(raw) > MAX_RECORD_BYTES:
        raise ValueError("Native record exceeds 16 MiB bound")
    return raw


def _import(source, destination, seed_handles, *, history_references=False):
    from lodestone_machinery.records import RecordStore
    from lodestone_machinery.yggdrasil_learning import validate_learning_records
    if type(history_references) is not bool:
        raise ValueError("Reference-history import must be explicit")
    if history_references:
        _reference_capability()
    source, destination = _path(source), _path(destination)
    if destination.exists():
        raise ValueError("Import destination must be new")
    if source == destination or source in destination.parents or destination in source.parents:
        raise ValueError("Import source and destination must be separate")
    paths = _record_paths(source)
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = Path(tempfile.mkdtemp(prefix=".learning-import-", dir=destination.parent))
    try:
        copied = temporary / "memory"
        manifest, total = {}, 0
        for relative in paths:
            raw = _record_bytes(source / relative)
            total += len(raw)
            if total > MAX_IMPORT_BYTES:
                raise ValueError("Import exceeds 128 MiB bound")
            target = copied / relative
            atomic_write(target, raw, replace=False)
            manifest[str(relative)] = {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
        store = RecordStore(copied)
        catalogue = store.catalog()
        if catalogue["verdict"] != "OK" or len(catalogue["entries"]) != len(paths):
            raise ValueError("Imported record identity or linked evidence is invalid")
        memory = validate_learning_records(store)
        seeds = memory.resolve_seeds(seed_handles)
        memory._pairs(seeds, None)
        if _record_paths(source) != paths:
            raise ValueError("Import source changed during capture; retry a stable snapshot")
        for relative in paths:
            raw = _record_bytes(source / relative)
            if hashlib.sha256(raw).hexdigest() != manifest[str(relative)]["sha256"]:
                raise ValueError("Import source changed during capture; retry a stable snapshot")
        final_memory = destination / "memory"
        if history_references:
            _create(copied / "learning-history-format.json", {
                "schema": "lodestone-learning-workspace-format/v1",
                "history_representation": "bezoar-learning-history-inventory/v1",
                "scope": "EXPLICIT_SEPARATE_IMPORT", "authority": "NONE"})
        _create(temporary / "ACTIVE.json", _active(final_memory, list(seed_handles), history_references=history_references))
        _create(temporary / "runtime/learning-policy.json", _policy(final_memory, list(seed_handles), history_references=history_references))
        receipt = {"schema": IMPORT_SCHEMA, "source": str(source), "destination": str(destination),
                   "captured_at": datetime.now(timezone.utc).isoformat(), "files": manifest,
                   "seed_handles": list(seed_handles), "records": len(paths), "bytes": total,
                   "authority": "NONE", "default_selection_changed": False, "provider_calls": 0,
                   "scope": "Original immutable records only. Attribution and claims are preserved, not endorsed. "
                            "No host configuration, active grants, queue control files, leases or executables imported."}
        if history_references:
            receipt["history_representation"] = "bezoar-learning-history-inventory/v1"
            receipt["compatibility"] = "New isolated ACTIVE/v2 requires a supporting runtime. Rollback restores both old runtime and old ACTIVE/workspace. Origami policy/v2 currently refuses non-windowed use."
        _create(temporary / "IMPORT.json", receipt)
        # Claim the destination atomically; rename alone could replace another
        # writer's empty directory. IMPORT.json is the last published receipt.
        with guarded_directory(destination.parent):
            destination.mkdir(mode=0o700, exist_ok=False)
            with guarded_directory(destination):
                durable_move(temporary / "memory", destination / "memory")
                durable_move(temporary / "runtime", destination / "runtime")
                _write_new(destination / "ACTIVE.json", read_file(temporary / "ACTIVE.json"))
                _write_new(destination / "IMPORT.json", read_file(temporary / "IMPORT.json"))
        return {"schema": IMPORT_SCHEMA, "destination": str(destination),
                "active_config": str(destination / "ACTIVE.json"), "memory_workspace": str(final_memory),
                "origami_workspace": str(destination / "runtime"),
                "origami_policy": str(destination / "runtime/learning-policy.json"),
                "import_receipt": str(destination / "IMPORT.json"), "seed_handles": list(seed_handles),
                "records": len(paths), "default_selection_changed": False, "provider_calls": 0}
    finally:
        if temporary is not None:
            shutil.rmtree(temporary)


def _reconcile(root):
    from lodestone_machinery.bezoar_learning import LearningMemory
    from lodestone_nurarihyon import learning
    root = _path(root)
    if _child(root, "learning", "STOP").exists():
        return {"status": "STOPPED", "provider_calls": 0, "automatic_retry": False}
    config = _read(_child(root, "learning", "ACTIVE.json"))
    if (config.get("schema") not in ("lodestone-learning-active/v1", "lodestone-learning-active/v2")
            or config.get("required") is not True):
        raise ValueError("Required native learning configuration is invalid")
    memory = _path(config["memory_workspace"])
    if not memory.is_dir() or not memory.is_relative_to(root):
        raise ValueError("Reconciliation requires an existing preview memory workspace")
    # Native reconciliation owns its nonblocking lock. Pre-validating selection
    # under a second lock would incorrectly turn active practice into an error.
    if config["schema"] == "lodestone-learning-active/v2":
        _reference_capability()
        policy = _selection_policy(_child(root, "runtime"), config)
        if (policy["memory_workspace"] != config["memory_workspace"]
                or policy["seed_handles"] != config["seed_handles"]):
            raise ValueError("Learning and Origami selections differ")
        return learning.reconcile(LearningMemory.from_active(config))
    return learning.reconcile(memory)


def _replace(path, data, expected):
    if read_file(path) != expected:
        raise ValueError("Learning selection changed concurrently: " + str(path))
    atomic_write(path, data)


def _select(root, destination):
    from lodestone_machinery.bezoar_learning import LearningMemory
    from lodestone_machinery.yggdrasil_learning import validate_learning_records
    from lodestone_nurarihyon import learning
    root, destination = _path(root), _path(destination)
    if destination == root or not destination.is_relative_to(root):
        raise ValueError("Selected import must be inside this preview's data root")
    plan = _read(_child(root, "learning", "bootstrap-plan.json"))
    if plan.get("schema") != BOOTSTRAP_SCHEMA or plan.get("root") != str(root):
        raise ValueError("Selection requires an initialized preview workspace")
    active_file = _child(root, "learning", "ACTIVE.json")
    origami = _child(root, "runtime")
    policy_file = _child(root, "runtime", "learning-policy.json")
    with _exclusive(_child(root, ".learning-bootstrap.lock")):
        receipt = _read(_child(destination, "IMPORT.json"), 1024 * 1024)
        imported = _read(_child(destination, "ACTIVE.json"))
        if imported.get("schema") == "lodestone-learning-active/v2":
            _reference_capability()
            if receipt.get("history_representation") != imported.get("history_representation"):
                raise ValueError("Imported reference-history capability differs from its receipt")
        imported_path = _child(destination, "memory")
        if (receipt.get("schema") != IMPORT_SCHEMA or receipt.get("destination") != str(destination)
                or not imported_path.is_dir()
                or imported.get("memory_workspace") != str(imported_path)
                or imported.get("seed_handles") != receipt.get("seed_handles")):
            raise ValueError("Import receipt and explicit selection differ")
        current = _read(active_file)
        current_path = _path(current["memory_workspace"])
        if not current_path.is_relative_to(root) or not current_path.is_dir():
            raise ValueError("Current selection must be an existing preview memory")
        with ExitStack() as held:
            memories = {p: LearningMemory(p) for p in sorted({current_path, imported_path})}
            for memory in memories.values():
                held.enter_context(learning.weaving(memory))
            for memory in memories.values():
                held.enter_context(learning.locked(memory))
                learning._require_one_reader(memory)
                learning._check_stop(memory)
            _selected(current)
            _selected(imported)
            policy = _selection_policy(origami, current)
            if (policy["memory_workspace"] != current["memory_workspace"]
                    or policy["seed_handles"] != current["seed_handles"]):
                raise ValueError("Current learning and Origami selections differ")
            paths = {str(p): p for p in _record_paths(imported_path)}
            files = receipt.get("files")
            if (type(files) is not dict or len(files) != receipt.get("records")
                    or not 1 <= len(files) <= MAX_IMPORT_FILES or not set(files) <= paths.keys()):
                raise ValueError("Import record inventory differs from its receipt")
            for name, expected in files.items():
                raw = _record_bytes(imported_path / paths[name])
                if ({"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)} != expected):
                    raise ValueError("An imported original record changed")
            store = memories[imported_path].store
            if store.catalog()["verdict"] != "OK":
                raise ValueError("Imported record closure is damaged")
            validate_learning_records(store)
            selection = {"memory_workspace": str(imported_path), "seed_handles": imported["seed_handles"]}
            following_active = {**current, **selection}
            following_policy = {**policy, **selection}
            if imported.get("schema") == "lodestone-learning-active/v2":
                following_active.update(schema="lodestone-learning-active/v2",
                    history_representation=imported["history_representation"])
                following_policy.update(schema="origami-learning-policy/v2",
                    history_representation=imported["history_representation"])
            elif current.get("schema") == "lodestone-learning-active/v2":
                following_active["schema"] = "lodestone-learning-active/v1"
                following_policy["schema"] = "origami-learning-policy/v1"
                following_active.pop("history_representation", None)
                following_policy.pop("history_representation", None)
            if following_active == current and following_policy == policy:
                return {"status": "UNCHANGED", "active_config": str(active_file), **selection,
                        "origami_policy": str(policy_file), "provider_calls": 0, "authority": "NONE"}
            old_active, old_policy = active_file.read_bytes(), policy_file.read_bytes()
            new_active, new_policy = _canonical(following_active) + b"\n", _canonical(following_policy) + b"\n"
            transaction = _child(root, "learning", "selection-history", uuid.uuid4().hex)
            _write_new(transaction / "before-ACTIVE.json", old_active)
            _write_new(transaction / "before-learning-policy.json", old_policy)
            _write_new(transaction / "after-ACTIVE.json", new_active)
            _write_new(transaction / "after-learning-policy.json", new_policy)
            stop_bytes = ("Lodestone preview selection transaction: " + str(transaction) + "\n").encode()
            stops = [memory.workspace.parent / "STOP" for memory in memories.values()
                     if not (memory.workspace.parent / "STOP").exists()
                     and not (memory.workspace.parent / "STOP").is_symlink()]
            _create(transaction / "PREPARED.json", {
                "schema": "lodestone-desktop-learning-selection/v1", "import": str(destination),
                "active_config": str(active_file), "origami_policy": str(policy_file),
                "temporary_stop_files": [str(path) for path in stops],
                "temporary_stop_text": stop_bytes.decode(),
                "interrupted_recovery": "If RESULT.json is absent, keep STOP in place, inspect the exact before/after "
                    "files, and restore both previous selections before removing only this transaction's STOP files.",
                "authority": "NONE", "provider_calls": 0})
            changed = []
            created_stops = []
            try:
                for path in stops:
                    _write_new(path, stop_bytes)
                    created_stops.append(path)
                for path, new, old in ((policy_file, new_policy, old_policy),
                                       (active_file, new_active, old_active)):
                    changed.append((path, old, new))
                    _replace(path, new, old)
                result = {"status": "SELECTED", "active_config": str(active_file), **selection,
                          "origami_policy": str(policy_file), "transaction": str(transaction),
                          "previous_records_preserved": True, "provider_calls": 0, "authority": "NONE"}
                _create(transaction / "RESULT.json", result)
            except BaseException:
                for path, old, new in reversed(changed):
                    actual = path.read_bytes()
                    if actual == new:
                        _replace(path, old, new)
                    elif actual != old:
                        raise ValueError("Concurrent change during selection rollback; STOP retained")
                for path in created_stops:
                    if not path.is_symlink() and path.read_bytes() == stop_bytes:
                        path.unlink()
                raise
            for path in created_stops:
                if not path.is_symlink() and path.read_bytes() == stop_bytes:
                    path.unlink()
            return result


def _main():
    if len(sys.argv) != 5 or sys.argv[1] != "--worker":
        raise ValueError("Use initialize(), import_memory() or reconcile() from the desktop installer")
    _, _, operation, runtime, python = sys.argv
    runtime = _path(runtime)
    sys.path.insert(0, str(runtime / "src"))
    values = json.loads(sys.stdin.buffer.read(32769))
    if operation == "initialize":
        result = _initialize(Path(values["root"]), runtime, Path(python))
    elif operation == "import":
        result = _import(Path(values["source"]), Path(values["destination"]), values["seed_handles"],
                         history_references=values.get("history_references", False))
    elif operation == "reconcile":
        result = _reconcile(Path(values["root"]))
    elif operation == "select":
        result = _select(Path(values["root"]), Path(values["destination"]))
    elif operation == "validate-selection":
        result = _validate_selection(Path(values["root"]))
    else:
        raise ValueError("Unknown bootstrap operation")
    sys.stdout.buffer.write(_canonical(result) + b"\n")


if __name__ == "__main__":
    os.umask(0o077)
    try:
        _main()
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(type(error).__name__ + ": " + str(error), file=sys.stderr)
        raise SystemExit(2)
