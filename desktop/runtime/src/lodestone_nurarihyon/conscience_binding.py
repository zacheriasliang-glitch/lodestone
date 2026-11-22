"""Explicit Conscience custody binding for the reconciliation worker.

The binding
is a block of the sealed Nurarihyon host policy, so it is part of every task's
policy identity: task or document bytes cannot supply or change the Conscience
policy, actor or workspace, and no historical host policy is ever loaded as a
fallback (W2). The controller assesses the exact worker command and its typed
effects through this binding before any attempt is charged (W1).

The binding grants nothing. ALLOW only lets the existing admission path
continue unchanged; every other verdict is recorded and held for the host.
No Conscience wake is performed and no warmth is claimed: readiness is the
bound module's own temperature reading taken with ``woken=False``.
"""
import importlib.util
from pathlib import Path
import re

from .common import digest, read_bytes

BINDING_SCHEMA = "nurarihyon-conscience-binding/v1"
ASSESSMENT_SCHEMA = "nurarihyon-custody-assessment/v1"
MODULES = ("conscience.py", "conscience_targets.py")
PIN_LIMIT = 2097152  # Conscience's own 2 MiB read bound.
VERDICTS = ("ALLOW", "DEFER", "NEEDS_HOST", "REFUSE")
REQUIRED = ("_validate_policy", "load_policy", "temperature", "assess", "_owner_of", "_writable", "ROOT", "VERDICTS")
ACTOR = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,95}")
SHA256 = re.compile(r"[0-9a-f]{64}")
READINESS = ("Conscience temperature() over the one/five-minute load average per logical CPU and the "
             "shutil.disk_usage free bytes at the nearest existing ancestor of the selected workspace, "
             "read with woken=False; no waking record was read or written and no context-read evidence is claimed")


class CustodyUnavailable(ValueError):
    """The binding cannot assess at all: a sealed pin drifted, the bound module or
    policy is unusable, or the wiring handed the adapter a command it was not
    written for. A controller-level block, never a task outcome, attempt or lesson."""


def _plain_text_path(value, field):
    if (type(value) is not str or not value or value != str(Path(value))
            or not Path(value).is_absolute() or ".." in Path(value).parts):
        raise ValueError("Conscience binding " + field + " must be a plain absolute path")
    return Path(value)


def _pin(value, field):
    if (type(value) is not dict or set(value) != {"path", "bytes", "sha256"}
            or type(value["bytes"]) is not int or not 0 < value["bytes"] <= PIN_LIMIT
            or type(value["sha256"]) is not str or not SHA256.fullmatch(value["sha256"])):
        raise ValueError("Conscience binding " + field + " must pin path, bytes and sha256")
    return _plain_text_path(value["path"], field + ".path")


def validate_binding(binding):
    """Static shape of the sealed binding block; pure, no file access."""
    if type(binding) is not dict or set(binding) != {"schema", "actor", "workspace", "policy", "modules"}:
        raise ValueError("Conscience binding fields mismatch")
    if binding["schema"] != BINDING_SCHEMA:
        raise ValueError("Unsupported Conscience binding schema")
    if type(binding["actor"]) is not str or not ACTOR.fullmatch(binding["actor"]):
        raise ValueError("Conscience binding actor must be a short plain name")
    workspace = _plain_text_path(binding["workspace"], "workspace")
    policy = _pin(binding["policy"], "policy")
    modules = binding["modules"]
    if type(modules) is not dict or set(modules) != set(MODULES):
        raise ValueError("Conscience binding must pin exactly conscience.py and conscience_targets.py")
    pinned = {name: _pin(modules[name], "modules." + name) for name in MODULES}
    for name, path in pinned.items():
        if path.name != name:
            raise ValueError("Conscience binding module pin must name " + name)
    if pinned["conscience_targets.py"] != pinned["conscience.py"].with_name("conscience_targets.py"):
        raise ValueError("Conscience targets module must be the bound module's sibling")
    if len({policy, *pinned.values()}) != 3 or workspace in (policy, *pinned.values()):
        raise ValueError("Conscience binding paths must be distinct")
    return binding


class ConscienceBinding:
    """Loads the bound Conscience once per controller; every pin is re-verified before each use."""

    def __init__(self, binding, home):
        validate_binding(binding)
        self.binding = binding
        self.home = Path(home)
        self.actor = binding["actor"]
        self.workspace = Path(binding["workspace"])
        self._module = None
        self._policy = None

    def _pins(self):
        return [("policy", self.binding["policy"])] + [(name, self.binding["modules"][name]) for name in MODULES]

    def _verify(self):
        for label, path in (("workspace", self.workspace), *((name, Path(pin["path"])) for name, pin in self._pins())):
            if path == self.home or self.home in path.parents or path in self.home.parents:
                raise CustodyUnavailable("Conscience " + label + " overlaps the controller home")
        for label, pin in self._pins():
            path = Path(pin["path"])
            try:
                aliased = path.resolve() != path
                raw = None if aliased else read_bytes(path, pin["bytes"])
            except (OSError, ValueError) as exc:
                raise CustodyUnavailable("Conscience " + label + " unreadable within its sealed pin: "
                                         + type(exc).__name__) from None
            if aliased:
                raise CustodyUnavailable("Conscience " + label + " path is an alias or symlink")
            if len(raw) != pin["bytes"] or digest(raw) != pin["sha256"]:
                raise CustodyUnavailable("Conscience " + label + " bytes differ from the sealed pin")

    def load(self):
        self._verify()
        if self._module is None:
            pin = self.binding["modules"]["conscience.py"]
            # The bound module is foreign, reviewed code. Any failure to load or
            # to validate its own policy is a block, never a task outcome.
            try:
                spec = importlib.util.spec_from_file_location("_nurarihyon_conscience_" + pin["sha256"][:16], pin["path"])
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
            except Exception as exc:
                raise CustodyUnavailable("Bound Conscience module did not load: " + type(exc).__name__) from None
            if any(not hasattr(module, name) for name in REQUIRED) or tuple(module.VERDICTS) != VERDICTS:
                raise CustodyUnavailable("Bound Conscience module lacks the reviewed interface")
            try:
                policy = module.load_policy(Path(self.binding["policy"]["path"]))
            except Exception as exc:
                raise CustodyUnavailable("Bound Conscience policy rejected by its own validator: "
                                         + type(exc).__name__) from None
            if type(policy) is not dict or policy.get("_pin", {}).get("sha256") != self.binding["policy"]["sha256"]:
                raise CustodyUnavailable("Loaded Conscience policy bytes differ from the sealed pin")
            self._module, self._policy = module, policy
        return self._module, self._policy

    def assess(self, *, stage, parent, raw, task, argv, interpreter, parent_pid):
        """Read-only: the exact worker argv and its typed effects, before the attempt is charged."""
        module, policy = self.load()
        from . import reconciliation
        if task["plan"].get("toolkit") == "bezoar.reconciliation-capture/v1":
            from . import complete_capture as reconciliation
        elif task["plan"].get("toolkit") == "bezoar.reconciliation-observation/v1":
            from . import historical_observation as reconciliation
        from .reconciliation_intent import assess_worker
        try:
            temperature = module.temperature(policy, self.workspace, woken=False)
            packet = assess_worker(module, reconciliation, policy=policy, actor=self.actor, woken=False,
                                   temp=temperature, workspace=self.workspace, home=self.home, stage=stage,
                                   parent=parent, raw=raw, task=task, argv=argv, interpreter=interpreter,
                                   parent_pid=parent_pid)
        except Exception as exc:
            # The adapter refuses any command it was not written for; after the
            # controller's own validation that is a wiring defect, not a plan defect.
            raise CustodyUnavailable("Custody assessment did not complete: " + type(exc).__name__ + ": "
                                     + str(exc)[:300]) from None
        if (type(packet) is not dict or packet.get("verdict") not in VERDICTS or packet.get("authority") != "NONE"
                or packet.get("executed") is not False or packet.get("published") is not False):
            raise CustodyUnavailable("Custody assessment packet is not the reviewed read-only shape")
        return {"schema": ASSESSMENT_SCHEMA, "verdict": packet["verdict"],
                "binding": {"actor": self.actor, "workspace": str(self.workspace),
                            "policy": dict(self.binding["policy"]),
                            "modules": {name: dict(self.binding["modules"][name]) for name in MODULES}},
                "readiness": {"wake_performed": False, "woken": False, "workspace_present": self.workspace.is_dir(),
                              "temperature": temperature, "source": READINESS},
                "assessment": packet, "assessed_before_attempt_charge": True,
                "authority": "NONE", "executed": False, "published": False,
                "scope": "Read-only custody assessment of the exact worker command and its typed effects; not permission, not a sandbox"}
