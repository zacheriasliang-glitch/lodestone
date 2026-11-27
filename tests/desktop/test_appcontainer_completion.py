"""AppContainer completion/recovery contracts; no host ACL or profile mutations."""
import ctypes
from contextlib import ExitStack
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

RUNTIME = Path(__file__).resolve().parents[2] / "desktop/runtime"
sys.path.insert(0, str(RUNTIME / "src"))
from lodestone_machinery import portable_process as native
from lodestone_machinery import windows_appcontainer as fence


class CompletionJournalTests(unittest.TestCase):
    def header(self):
        return {"profile": fence.PREFIX + "fixture", "initial_process_state": "not_started"}

    def test_prelaunch_and_confirmed_runs_admit_cleanup(self):
        self.assertTrue(fence._completion_recorded([self.header()]))
        self.assertTrue(fence._completion_recorded([self.header(), {"process_may_exist": True},
                                                   {"completion_confirmed": True}]))

    def test_later_process_or_uncertainty_revokes_prior_completion(self):
        prefix = [self.header(), {"process_may_exist": True}, {"completion_confirmed": True}]
        for last in ({"process_may_exist": True}, {"completion_unconfirmed": True}):
            self.assertFalse(fence._completion_recorded(prefix + [last]))

    def test_legacy_malformed_or_out_of_order_states_refuse(self):
        for entries in ([], [{"profile": "legacy"}], [None], [self.header(), 1],
                        [self.header(), {"completion_confirmed": True}],
                        [self.header(), {"process_may_exist": "yes"}],
                        [self.header(), {"process_may_exist": True, "completion_confirmed": True}],
                        [self.header(), self.header()]):
            with self.subTest(entries=entries):
                self.assertFalse(fence._completion_recorded(entries))

    def test_sweep_does_not_restore_started_or_legacy_journal(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for suffix, entries in (("started", [self.header(), {"process_may_exist": True}]),
                                    ("legacy", [{"profile": fence.PREFIX + "legacy"}])):
                (root / (fence.PREFIX + suffix + ".journal")).write_text(
                    "".join(json.dumps(entry) + "\n" for entry in entries))
            with patch.object(fence, "_ledger_directory", return_value=root), \
                    patch.object(fence, "_Security"), patch.object(fence, "_undo") as undo, \
                    patch.object(fence, "_delete_profile") as delete, patch("sys.stderr", new=io.StringIO()):
                self.assertEqual(fence._sweep(object()), 2)
                undo.assert_not_called(); delete.assert_not_called()
            self.assertEqual(len(list(root.iterdir())), 2)


class FinishRunTests(unittest.TestCase):
    def fixture(self, temporary, completion, *, process=201, resumed=True, may_exist=True):
        root = Path(temporary)
        events = []
        name = fence.PREFIX + "fixture"
        journal = fence._Journal(root / (name + ".journal"), create=True)
        entries = [{"profile": name, "initial_process_state": "not_started"}]
        if may_exist:
            entries.append({"process_may_exist": True})
        for entry in entries:
            journal.append(entry)
        api = SimpleNamespace(c=SimpleNamespace(get_last_error=lambda: 5),
            kernel=SimpleNamespace(TerminateProcess=Mock(return_value=0),
                WaitForSingleObject=Mock(return_value=0), CloseHandle=Mock(side_effect=lambda h: events.append(("close", h)))),
            advapi=SimpleNamespace(FreeSid=Mock()))
        def undo(*args, **kwargs):
            self.assertTrue(fence._completion_recorded(fence._Journal.entries(journal.path)))
            events.append("undo")
            return []
        self.stack.enter_context(patch.object(fence, "_ledger_directory", return_value=root))
        self.stack.enter_context(patch.object(fence, "_Security", return_value=object()))
        self.undo = self.stack.enter_context(patch.object(fence, "_undo", side_effect=undo))
        self.delete = self.stack.enter_context(patch.object(fence, "_delete_profile", side_effect=lambda *a: events.append("delete") or True))
        return api, events, dict(job=101, process=process, resumed=resumed, completion=completion,
            journal=journal, entries=entries, sid="fixture-sid", name=name, sid_pointer=7,
            process_may_exist=may_exist)

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch("sys.stderr", new=io.StringIO()))

    def test_confirmed_stop_precedes_undo_and_profile_delete(self):
        completion = SimpleNamespace(stop=Mock(return_value={"confirmed_stopped": True}), close=Mock())
        with tempfile.TemporaryDirectory() as temporary:
            api, events, args = self.fixture(temporary, completion)
            completion.stop.side_effect = lambda: events.append("stop") or {"confirmed_stopped": True}
            completion.close.side_effect = lambda: events.append("pins_closed")
            self.assertTrue(fence._finish_run(api, **args))
            self.assertEqual(events, ["stop", "pins_closed", ("close", 201), ("close", 101), "undo", "delete"])
            self.assertFalse(args["journal"].path.exists())

    def test_unknown_completion_keeps_journal_and_skips_acl_profile_cleanup(self):
        completion = SimpleNamespace(stop=Mock(return_value={"confirmed_stopped": False}), close=Mock())
        with tempfile.TemporaryDirectory() as temporary:
            api, events, args = self.fixture(temporary, completion)
            self.assertFalse(fence._finish_run(api, **args))
            self.undo.assert_not_called(); self.delete.assert_not_called()
            self.assertFalse(fence._completion_recorded(fence._Journal.entries(args["journal"].path)))
            completion.close.assert_called_once()

    def test_failed_cleanup_does_not_replace_primary_exception(self):
        completion = SimpleNamespace(stop=Mock(side_effect=OSError("inspection failed")), close=Mock())
        with tempfile.TemporaryDirectory() as temporary:
            api, events, args = self.fixture(temporary, completion)
            with self.assertRaisesRegex(ValueError, "primary setup failure"):
                try:
                    raise ValueError("primary setup failure")
                finally:
                    self.assertFalse(fence._finish_run(api, **args))
            self.undo.assert_not_called(); self.delete.assert_not_called()
            self.assertTrue(args["journal"].path.exists())

    def test_already_terminating_suspended_process_still_waits_exact_handle(self):
        with tempfile.TemporaryDirectory() as temporary:
            api, events, args = self.fixture(temporary, None, resumed=False)
            self.assertTrue(fence._finish_run(api, **args))
            api.kernel.TerminateProcess.assert_called_once_with(201, 1)
            api.kernel.WaitForSingleObject.assert_called_once_with(201, 6000)
            self.assertEqual(args["entries"][-1]["completion"]["termination_winerror"], 5)

    def test_live_or_failed_suspended_wait_never_admits_cleanup(self):
        for state in (258, 0xffffffff):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as temporary:
                api, events, args = self.fixture(temporary, None, resumed=False)
                api.kernel.WaitForSingleObject.return_value = state
                self.assertFalse(fence._finish_run(api, **args))
                self.undo.assert_not_called(); self.delete.assert_not_called()

    def test_create_failed_without_process_records_safe_no_process(self):
        with tempfile.TemporaryDirectory() as temporary:
            api, events, args = self.fixture(temporary, None, process=None, resumed=False)
            self.assertTrue(fence._finish_run(api, **args))
            api.kernel.TerminateProcess.assert_not_called()
            self.assertEqual(args["entries"][-1]["completion"], {"no_process_created": True})


class SuspendedCreateWindowTests(unittest.TestCase):
    def test_successful_create_handle_is_adopted_when_check_raises_before_assignment(self):
        from ctypes import wintypes as w
        with tempfile.TemporaryDirectory() as temporary, ExitStack() as stack:
            root = Path(temporary)
            created, observed = [], []
            def check(value):
                if created:
                    raise OSError("injected after native CreateProcess success")
                if not value:
                    raise OSError("native setup failed")
                return value
            def profile(name, display, description, caps, count, pointer):
                pointer._obj.value = 7
                return 0
            def sid(pointer, output):
                output._obj.value = "S-1-15-2-1"
                return 1
            def attributes(buffer, count, flags, size):
                size._obj.value = 64
                return 1
            def duplicate(source_process, source, target_process, output, access, inherit, flags):
                output._obj.value = 501
                return 1
            def create(*arguments):
                entries = fence._Journal.entries(next(root.glob("*.journal")))
                observed.extend(entries)
                info = arguments[-1]._obj
                info.hProcess, info.hThread, info.dwProcessId = 201, 202, 11
                created.append(True)
                return 1
            kernel = SimpleNamespace(LocalFree=Mock(), CreateJobObjectW=Mock(return_value=101),
                SetInformationJobObject=Mock(return_value=1), GetStdHandle=Mock(return_value=601),
                GetCurrentProcess=Mock(return_value=99), DuplicateHandle=duplicate,
                InitializeProcThreadAttributeList=attributes, UpdateProcThreadAttribute=Mock(return_value=1),
                DeleteProcThreadAttributeList=Mock(), CreateProcessW=create,
                AssignProcessToJobObject=Mock(return_value=1), ResumeThread=Mock(return_value=1),
                TerminateProcess=Mock(return_value=0), WaitForSingleObject=Mock(return_value=0), CloseHandle=Mock())
            api = SimpleNamespace(c=ctypes, w=w, kernel=kernel, check=check,
                userenv=SimpleNamespace(CreateAppContainerProfile=profile),
                advapi=SimpleNamespace(ConvertSidToStringSidW=sid, FreeSid=Mock()))
            stack.enter_context(patch.object(ctypes, "get_last_error", return_value=5, create=True))
            stack.enter_context(patch.dict(sys.modules, {"msvcrt": SimpleNamespace(get_osfhandle=lambda fd: fd)}))
            stack.enter_context(patch.object(fence, "_ledger_directory", return_value=root))
            stack.enter_context(patch.object(fence, "_lexical", side_effect=lambda path: path))
            stack.enter_context(patch.object(fence, "_identity", return_value=[1, 2]))
            stack.enter_context(patch.object(fence, "_Security", return_value=SimpleNamespace(read=lambda p: "D:", write=lambda *a: None)))
            undo = stack.enter_context(patch.object(fence, "_undo", return_value=[]))
            stack.enter_context(patch.object(fence, "_delete_profile", return_value=True))
            tracker = stack.enter_context(patch.object(fence, "_completion_tracker"))
            with self.assertRaisesRegex(OSError, "after native CreateProcess"):
                fence._run_locked(api, ctypes, w, kernel, [sys.executable], root, [])
            self.assertTrue(any(entry.get("process_may_exist") is True for entry in observed))
            kernel.AssignProcessToJobObject.assert_not_called()
            tracker.assert_not_called()
            kernel.WaitForSingleObject.assert_called_once_with(201, 6000)
            kernel.CloseHandle.assert_any_call(201)
            undo.assert_called_once()
            self.assertFalse(list(root.glob("*.journal")))


class BorrowedCompletionTests(unittest.TestCase):
    def api(self):
        class FileTime(ctypes.Structure):
            _fields_ = [("dwLowDateTime", ctypes.c_uint32), ("dwHighDateTime", ctypes.c_uint32)]
        def belongs(process, job, value):
            value._obj.value = 1
            return 1
        def times(process, created, *unused):
            created._obj.dwLowDateTime = 42
            return 1
        return SimpleNamespace(w=SimpleNamespace(BOOL=ctypes.c_int, FILETIME=FileTime),
            check=lambda value: value or (_ for _ in ()).throw(OSError("native refusal")),
            kernel=SimpleNamespace(IsProcessInJob=Mock(side_effect=belongs), GetProcessId=Mock(return_value=11),
                GetProcessTimes=Mock(side_effect=times), CloseHandle=Mock()))

    def test_constructor_validates_root_and_close_never_closes_borrowed_handles(self):
        api = self.api()
        owner = native.WindowsJobCompletion(101, 201, 11, api=api, require_complete_history=True)
        self.assertEqual(owner._lifetime_identities, {(11, 42)})
        owner._member_handles[(12, 43)] = 301
        owner.close()
        api.kernel.CloseHandle.assert_called_once_with(301)

    def test_invalid_job_membership_refuses_before_retaining_handles(self):
        api = self.api()
        api.kernel.IsProcessInJob.side_effect = lambda p, j, v: 1
        with self.assertRaisesRegex(OSError, "not assigned"):
            native.WindowsJobCompletion(101, 201, 11, api=api)
        api.kernel.GetProcessTimes.assert_not_called()
        api.kernel.CloseHandle.assert_not_called()

    def test_strict_lifetime_coverage_refuses_unseen_already_exited_child(self):
        from test_windows_stop_completion import StopCompletionContractTests
        owner = StopCompletionContractTests().owned()
        owner._lifetime_identities = {(2, 20), (3, 30)}
        result = owner.stop()
        self.assertFalse(result["confirmed_stopped"])
        self.assertEqual(result["reason"], "unobserved_lifetime_member")
        self.assertEqual(result["verified_lifetime_member_count"], 2)

    def test_signaled_member_can_be_pruned_without_losing_lifetime_coverage(self):
        from test_windows_stop_completion import StopCompletionContractTests
        owner = StopCompletionContractTests().owned()
        owner._lifetime_identities = {(1, 10), (2, 20), (3, 30)}
        owner._release_completed_members()
        self.assertFalse(owner._member_handles)
        self.assertEqual(len(owner._lifetime_identities), 3)
        result = owner.stop()
        self.assertTrue(result["confirmed_stopped"], result)
        self.assertEqual(result["verified_lifetime_member_count"], 3)

    def test_strict_capture_uses_verified_borrowed_root_even_after_exit(self):
        api = self.api()
        api.kernel.WaitForSingleObject = Mock(return_value=0)
        api.kernel.OpenProcess = Mock(side_effect=AssertionError("must use borrowed root"))
        owner = native.WindowsJobCompletion(101, 201, 11, api=api, require_complete_history=True)
        self.assertTrue(owner._capture_stop_members([11], float("inf")))
        api.kernel.OpenProcess.assert_not_called()
        api.kernel.CloseHandle.assert_not_called()


if __name__ == "__main__":
    unittest.main()
