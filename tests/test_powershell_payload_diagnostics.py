from pathlib import Path

from scripts.probe_windows_appcontainer import assess_smoke


ROOT = Path(__file__).resolve().parents[1]
PAYLOAD_SOURCE = (ROOT / "native/windows/MewCode.SyntheticPayload.cs").read_text(encoding="utf-8")


def method_body(source, signature, next_signature):
    start = source.index(signature)
    end = source.index(next_signature, start)
    return source[start:end]


def smoke_report():
    common = {
        "exit_code": 0,
        "timed_out": False,
        "error": "",
        "job_cleanup_ok": True,
        "profile_cleanup_ok": True,
        "acl_cleanup_ok": True,
        "appcontainer_verified": True,
        "stdout_truncated": False,
        "stderr_truncated": False,
        "capabilities": [],
    }

    def case(name, lines):
        return {
            "name": name,
            "helper_exit_code": 0,
            "receipt": dict(common, stdout="\n".join(lines)),
        }

    return {
        "cases": [
            case("cmd_smoke", ["CMD_nonce"]),
            case("powershell_boundary", [
                "POWERSHELL_nonce", "ENV_CLEAN", "INPUT_OK", "OUTPUT_OK",
                "OUTSIDE_READ_DENIED", "SECRET_READ_DENIED", "OUTSIDE_WRITE_DENIED",
            ]),
            case("exe_boundary", [
                "EXE_nonce", "ENV_CLEAN", "INPUT_OK", "OUTPUT_OK", "OUTSIDE_READ_DENIED",
                "SECRET_READ_DENIED", "JUNCTION_READ_DENIED", "OUTSIDE_WRITE_DENIED", "NETWORK_DENIED",
            ]),
        ],
        "observed": {
            "network_child_payload_received": False,
            "outside_write_exists": False,
            "output_readable_by_host": True,
            "exe_output_readable_by_host": True,
        },
        "fixture_acl_cleanup_ok": True,
        "host_control": {"junction_target_readable": True},
    }


def test_diagnostic_modes_remain_fixed_and_argumentless():
    assert '(args[0] == "powershell-init" || args[0] == "powershell-drive-init") && args.Length == 1' in PAYLOAD_SOURCE
    assert 'ProbePowerShellInitialization(args[0] == "powershell-drive-init")' in PAYLOAD_SOURCE
    body = method_body(PAYLOAD_SOURCE, "private static int ProbePowerShellInitializationCore", "private static void ProbeDirectSystemCmd")
    fixed_pipeline = body.split('GetMethod("CreatePipeline"', 1)[1].split('runspace, new object[] { "', 1)[1].split('" });', 1)[0]
    markers = (
        "PS_NATIVE_ENGINE_OK",
        "PS_NATIVE_CHILD_BEFORE",
        "& $env:ComSpec /d /c echo PS_NATIVE_CHILD_OK",
        "PS_NATIVE_CHILD_AFTER",
        "PS_NATIVE_LASTEXITCODE_",
    )
    assert all(marker in fixed_pipeline for marker in markers)
    assert [fixed_pipeline.index(marker) for marker in markers] == sorted(fixed_pipeline.index(marker) for marker in markers)
    assert "if (hadErrors || !engineSeen || !childSeen || !beforeSeen || !afterSeen || !childExitZeroSeen)" in body
    assert "Environment.Exit(" not in PAYLOAD_SOURCE


def test_first_chance_trace_is_bounded_thread_safe_and_diagnostic_only():
    wrapper = method_body(PAYLOAD_SOURCE, "private static int ProbePowerShellInitialization(bool", "private sealed class DiagnosticFirstChanceTrace")
    trace = method_body(PAYLOAD_SOURCE, "private sealed class DiagnosticFirstChanceTrace", "private static int ProbePowerShellInitializationCore")

    assert "private const int MaxFirstChanceTypes = 24;" in PAYLOAD_SOURCE
    assert "new DiagnosticFirstChanceTrace()" in wrapper
    assert "finally" in wrapper and "firstChance.Dispose();" in wrapper
    assert "AppDomain.CurrentDomain.FirstChanceException += OnFirstChanceException;" in trace
    assert "AppDomain.CurrentDomain.FirstChanceException -= OnFirstChanceException;" in trace
    assert "lock (gate)" in trace
    assert "seen.Contains(type)" in trace
    assert "types.Count >= MaxFirstChanceTypes" in trace
    assert "DiagnosticTypeToken(args.Exception.GetType())" in trace
    assert "PS_FIRST_CHANCE_TRUNCATED_TRUE" in trace
    assert ".Message" not in trace and ".StackTrace" not in trace


def test_pipeline_error_types_unwrap_psobject_and_follow_inner_exceptions():
    body = method_body(PAYLOAD_SOURCE, "private static void EmitPipelineErrorTypeChain", "private static string DiagnosticValueToken")

    assert "private const int MaxDiagnosticErrorDepth = 8;" in PAYLOAD_SOURCE
    assert '"System.Management.Automation.PSObject"' in body
    assert 'GetProperty("BaseObject")' in body
    assert 'GetProperty("Exception")' in body
    assert "exception.InnerException" in body
    assert "depth < MaxDiagnosticErrorDepth" in body
    assert "PS_PIPELINE_ERROR_RECORD_TYPE_" in body
    assert "PS_PIPELINE_ERROR_CHAIN_" in body
    assert "PS_PIPELINE_ERROR_INSPECT_EXCEPTION_" in body
    assert ".Message" not in body and "error.ToString()" not in body

    pipeline = method_body(PAYLOAD_SOURCE, "private static int ProbePowerShellInitializationCore", "private static void ProbeDirectSystemCmd")
    assert "hadErrors = true;" in pipeline
    assert "EmitPipelineErrorTypeChain(error);" in pipeline
    assert "EmitExceptionTypeChain(\"PS_INIT_PIPELINE_INVOKE_EXCEPTION_CHAIN_\", ex);" in pipeline
    assert "EmitExceptionTypeChain(\"PS_PIPELINE_FAILURE_REASON_CHAIN_\", reason);" in PAYLOAD_SOURCE
    assert "EmitExceptionTypeChain(\"PS_INIT_ERROR_CHAIN_\", ex);" in PAYLOAD_SOURCE


def test_pipeline_diagnostics_are_bounded_and_do_not_print_failure_details():
    for marker in (
        "PS_COMSPEC_PRESENT_FALSE",
        "PS_COMSPEC_PRESENT_TRUE",
        "PS_INIT_PIPELINE_INVOKE_BEGIN",
        "PS_INIT_PIPELINE_INVOKE_RETURNED",
        "PS_INIT_PIPELINE_DISPOSE_BEFORE",
        "PS_INIT_PIPELINE_DISPOSE_AFTER",
        "PS_INIT_RUNSPACE_DISPOSE_BEFORE",
        "PS_INIT_RUNSPACE_DISPOSE_AFTER",
        "PS_PIPELINE_STATE_",
        "PS_PIPELINE_FAILURE_REASON_",
    ):
        assert marker in PAYLOAD_SOURCE

    assert "Console.Error.WriteLine(\"PS_PIPELINE_ERROR_TYPE_\"" in PAYLOAD_SOURCE
    assert '"PS_PIPELINE_ERROR: " + error.ToString()' not in PAYLOAD_SOURCE
    assert 'return Fail("PS_INIT_ERROR", DiagnosticTypeToken(ex.GetType()))' in PAYLOAD_SOURCE


def test_direct_child_is_a_fixed_bounded_diagnostic_comparison():
    body = method_body(PAYLOAD_SOURCE, "private static void ProbeDirectSystemCmd()", "private static bool ContainsExactLine")

    assert "ProbeDirectSystemCmd();" in method_body(
        PAYLOAD_SOURCE, "private static int ProbePowerShellInitialization", "private static int RunBoundary"
    )
    assert 'Path.Combine(Environment.SystemDirectory, "cmd.exe")' in body
    assert 'Arguments = "/d /c echo PS_DIRECT_CHILD_OK"' in body
    assert "UseShellExecute = false" in body
    assert "RedirectStandardOutput = true" in body
    assert "RedirectStandardError = true" in body
    assert "private const int DiagnosticCmdTimeoutMs = 3000;" in PAYLOAD_SOURCE
    assert "child.WaitForExit(DiagnosticCmdTimeoutMs)" in body
    assert "child.Kill();" in body
    for marker in (
        "PS_DIRECT_CHILD_START_EXCEPTION_",
        "PS_DIRECT_CHILD_EXIT_CODE_",
        "PS_DIRECT_CHILD_TIMEOUT",
        "PS_DIRECT_CHILD_STDOUT_CAPTURED_TRUE",
        "PS_DIRECT_CHILD_STDERR_CAPTURED_TRUE",
    ):
        assert marker in body
    assert "PS_DIRECT_CHILD_STDOUT_MARKER_PRESENT_TRUE" in body
    assert "PS_DIRECT_CHILD_STDERR_EMPTY_TRUE" in body


def test_direct_child_success_does_not_replace_original_powershell_child_check():
    body = method_body(PAYLOAD_SOURCE, "private static int ProbePowerShellInitialization", "private static void ProbeDirectSystemCmd")
    assert 'childSeen |= line == "PS_NATIVE_CHILD_OK"' in body
    assert "if (hadErrors || !engineSeen || !childSeen || !beforeSeen || !afterSeen || !childExitZeroSeen)" in body
    assert "PS_DIRECT_CHILD_OK" not in body

    report = smoke_report()
    baseline = assess_smoke(report, "nonce")
    assert baseline["smoke_passed"]
    report["cases"].append({
        "name": "powershell_init_diagnostic",
        "helper_exit_code": 0,
        "receipt": {"stdout": "PS_DIRECT_CHILD_STDOUT_MARKER_PRESENT_TRUE"},
    })
    assert assess_smoke(report, "nonce")["smoke_passed"]

    report["cases"][1]["receipt"]["stdout"] = "\n".join([
        "ENV_CLEAN", "INPUT_OK", "OUTPUT_OK", "OUTSIDE_READ_DENIED",
        "SECRET_READ_DENIED", "OUTSIDE_WRITE_DENIED",
    ])
    assert not assess_smoke(report, "nonce")["smoke_passed"]


def test_native_smoke_stays_non_authorizing_with_or_without_diagnostic_case():
    report = smoke_report()
    assert assess_smoke(report, "nonce")["production_authorized"] is False
    report["cases"].append({
        "name": "powershell_init_diagnostic",
        "helper_exit_code": 0,
        "receipt": {"stdout": "PS_INIT_DONE"},
    })
    verdict = assess_smoke(report, "nonce")
    assert verdict["smoke_passed"]
    assert verdict["production_authorized"] is False
