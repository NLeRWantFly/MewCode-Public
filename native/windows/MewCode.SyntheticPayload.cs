// Fixed synthetic-only payload for the experimental Windows AppContainer probe.
// This executable is not a production entry point and must not be used with real data.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Reflection;
using System.Runtime.ExceptionServices;
using System.Text;
using System.Threading;
using System.Threading.Tasks;

internal static class MewCodeSyntheticPayload
{
    private const string SyntheticApiKeyName = "MEWCODE_SYNTHETIC_API_KEY";
    private const int NetworkTimeoutMs = 2000;
    private const int ChildSleepMs = 30000;
    private const int DiagnosticCmdTimeoutMs = 3000;
    private const int ThreadStepWaitMs = 2000;
    private const int MaxFirstChanceTypes = 24;
    private const int MaxDiagnosticErrorDepth = 8;

    private static int Main(string[] args)
    {
        Console.OutputEncoding = new UTF8Encoding(false);
        try
        {
            if (args == null || args.Length == 0)
                return Fail("USAGE", "Expected boundary, children, or internal sleep mode.");

            if (String.Equals(args[0], "boundary", StringComparison.Ordinal))
                return RunBoundary(args);
            if (String.Equals(args[0], "network-control", StringComparison.Ordinal))
            {
                int controlPort;
                if (args.Length != 3 || !Int32.TryParse(args[1], out controlPort) ||
                    controlPort < 1 || controlPort > 65535 || !IsSafeNonce(args[2]))
                    return Fail("USAGE", "Expected network-control <port> <nonce>.");
                // Exact same connection implementation, without file operations.
                // The host asserts NETWORK_ALLOWED and its received nonce.
                return ProbeLoopback(controlPort, args[2]) ? 0 : 2;
            }
            if (String.Equals(args[0], "children", StringComparison.Ordinal))
                return RunChildren(args);
            if ((args[0] == "powershell-init" || args[0] == "powershell-drive-init") && args.Length == 1)
                return ProbePowerShellInitialization(args[0] == "powershell-drive-init");
            if (String.Equals(args[0], "dotnet-threadpool", StringComparison.Ordinal) && args.Length == 1)
                return ProbeDotNetThreadPool();
            if (String.Equals(args[0], "sleep", StringComparison.Ordinal))
                return RunSleep(args);

            return Fail("USAGE", "Unknown synthetic payload mode.");
        }
        catch (Exception ex)
        {
            if (args != null && args.Length == 1 && (args[0] == "powershell-init" || args[0] == "powershell-drive-init"))
            {
                EmitExceptionTypeChain("PS_INIT_ERROR_CHAIN_", ex);
                return Fail("PS_INIT_ERROR", DiagnosticTypeToken(ex.GetType()));
            }
            if (args != null && args.Length == 1 && args[0] == "dotnet-threadpool")
            {
                Emit("DOTNET_THREADPOOL_FATAL_EXCEPTION_TYPE_" + DiagnosticTypeToken(ex.GetType()));
                Emit("DOTNET_THREADPOOL_FATAL_EXCEPTION_HR_" + ex.HResult.ToString(CultureInfo.InvariantCulture));
                return 2;
            }
            return Fail("PAYLOAD_ERROR", ex.GetType().Name);
        }
    }

    private sealed class ThreadStepSignal
    {
        // A timed-out callback may signal after the caller moves on. Never dispose
        // these at most four handles; the synthetic process reclaims them on exit.
        internal readonly ManualResetEvent Ready = new ManualResetEvent(false);
        internal readonly object Gate = new object();
        internal bool CallbackCompleted;
        internal bool IsBackground;
        internal ApartmentState Apartment;
        internal string ErrorType;
        internal int ErrorHResult;
    }

    private static int ProbeDotNetThreadPool()
    {
        // Four independent fixed callbacks, each with at most a two-second wait.
        // No PowerShell, files, network, thread-pool settings or user text.
        Emit("DOTNET_THREADPOOL_BEGIN");
        bool direct = ProbeDirectThread("DIRECT", false);
        bool sta = ProbeDirectThread("STA", true);
        bool queued = ProbeQueuedWorkItem();
        bool task = ProbeFixedTask();
        bool allOk = direct && sta && queued && task;
        Emit("DOTNET_THREADPOOL_ALL_OK_" + DiagnosticBoolToken(allOk));
        Emit("DOTNET_THREADPOOL_DONE");
        return allOk ? 0 : 2;
    }

    private static bool ProbeDirectThread(string label, bool sta)
    {
        var signal = new ThreadStepSignal();
        var timer = Stopwatch.StartNew();
        bool accepted = false;
        Exception stepError = null;
        try
        {
            Thread worker = new Thread(RunFixedThreadCallback);
            worker.IsBackground = true;
            if (sta) worker.SetApartmentState(ApartmentState.STA);
            worker.Start(signal);
            accepted = true;
        }
        catch (Exception ex)
        {
            stepError = ex;
        }
        bool signaled = WaitForStep(signal, timer, accepted, ref stepError);
        return EmitThreadStep(label, signal, accepted, signaled, stepError,
            sta ? ApartmentState.STA : ApartmentState.MTA);
    }

    private static bool ProbeQueuedWorkItem()
    {
        var signal = new ThreadStepSignal();
        var timer = Stopwatch.StartNew();
        bool accepted = false;
        Exception stepError = null;
        try
        {
            accepted = ThreadPool.QueueUserWorkItem(RunFixedThreadCallback, signal);
        }
        catch (Exception ex)
        {
            stepError = ex;
        }
        bool signaled = WaitForStep(signal, timer, accepted, ref stepError);
        return EmitThreadStep("THREADPOOL", signal, accepted, signaled, stepError, null);
    }

    private static bool ProbeFixedTask()
    {
        var signal = new ThreadStepSignal();
        var timer = Stopwatch.StartNew();
        bool accepted = false;
        Exception stepError = null;
        try
        {
            Task task = Task.Factory.StartNew(
                delegate { RunFixedThreadCallback(signal); },
                CancellationToken.None, TaskCreationOptions.None, TaskScheduler.Default);
            accepted = task != null;
        }
        catch (Exception ex)
        {
            stepError = ex;
        }
        bool signaled = WaitForStep(signal, timer, accepted, ref stepError);
        return EmitThreadStep("TASK", signal, accepted, signaled, stepError, null);
    }

    private static void RunFixedThreadCallback(object stateObject)
    {
        var signal = (ThreadStepSignal)stateObject;
        try
        {
            bool background = Thread.CurrentThread.IsBackground;
            ApartmentState apartment = Thread.CurrentThread.GetApartmentState();
            lock (signal.Gate)
            {
                signal.IsBackground = background;
                signal.Apartment = apartment;
                signal.CallbackCompleted = true;
            }
        }
        catch (Exception ex)
        {
            lock (signal.Gate)
            {
                signal.ErrorType = DiagnosticTypeToken(ex.GetType());
                signal.ErrorHResult = ex.HResult;
            }
        }
        finally
        {
            try { signal.Ready.Set(); }
            catch (Exception ex)
            {
                lock (signal.Gate)
                {
                    signal.ErrorType = DiagnosticTypeToken(ex.GetType());
                    signal.ErrorHResult = ex.HResult;
                }
            }
        }
    }

    private static bool WaitForStep(ThreadStepSignal signal, Stopwatch timer, bool accepted, ref Exception stepError)
    {
        if (!accepted) return false;
        int remaining = Math.Max(0, ThreadStepWaitMs - (int)Math.Min(timer.ElapsedMilliseconds, ThreadStepWaitMs));
        try { return signal.Ready.WaitOne(remaining); }
        catch (Exception ex)
        {
            stepError = ex;
            return false;
        }
    }

    private static bool EmitThreadStep(string label, ThreadStepSignal signal, bool accepted,
        bool signaled, Exception stepError, ApartmentState? expectedApartment)
    {
        bool completed;
        bool background;
        ApartmentState apartment;
        string errorType;
        int errorHResult;
        lock (signal.Gate)
        {
            completed = signal.CallbackCompleted;
            background = signal.IsBackground;
            apartment = signal.Apartment;
            errorType = signal.ErrorType;
            errorHResult = signal.ErrorHResult;
        }
        if (stepError != null)
        {
            errorType = DiagnosticTypeToken(stepError.GetType());
            errorHResult = stepError.HResult;
        }
        bool apartmentOk = !expectedApartment.HasValue || apartment == expectedApartment.Value;
        bool ok = accepted && signaled && completed && background && apartmentOk && errorType == null;
        string prefix = "DOTNET_" + label + "_";
        Emit(prefix + "ACCEPTED_" + DiagnosticBoolToken(accepted));
        Emit(prefix + "SIGNALED_" + DiagnosticBoolToken(signaled));
        Emit(prefix + "CALLBACK_COMPLETED_" + DiagnosticBoolToken(completed));
        Emit(prefix + "BACKGROUND_" + DiagnosticBoolToken(background));
        Emit(prefix + "APARTMENT_" + SafeDiagnosticToken(apartment.ToString(), 16));
        Emit(prefix + "EXCEPTION_TYPE_" + (errorType ?? "NONE"));
        Emit(prefix + "EXCEPTION_HR_" + errorHResult.ToString(CultureInfo.InvariantCulture));
        Emit(prefix + "OK_" + DiagnosticBoolToken(ok));
        return ok;
    }

    private static string DiagnosticBoolToken(bool value)
    {
        return value ? "TRUE" : "FALSE";
    }

    private static int ProbePowerShellInitialization(bool mountSyntheticDrive)
    {
        var firstChance = new DiagnosticFirstChanceTrace();
        try
        {
            return ProbePowerShellInitializationCore(mountSyntheticDrive);
        }
        finally
        {
            firstChance.Dispose();
        }
    }

    private sealed class DiagnosticFirstChanceTrace : IDisposable
    {
        private readonly object gate = new object();
        private readonly HashSet<string> seen = new HashSet<string>(StringComparer.Ordinal);
        private readonly List<string> types = new List<string>();
        private bool active = true;
        private bool truncated;

        public DiagnosticFirstChanceTrace()
        {
            AppDomain.CurrentDomain.FirstChanceException += OnFirstChanceException;
        }

        private void OnFirstChanceException(object sender, FirstChanceExceptionEventArgs args)
        {
            if (args == null || args.Exception == null) return;
            string type = DiagnosticTypeToken(args.Exception.GetType());
            lock (gate)
            {
                if (!active || seen.Contains(type)) return;
                if (types.Count >= MaxFirstChanceTypes)
                {
                    truncated = true;
                    return;
                }
                seen.Add(type);
                types.Add(type);
            }
        }

        public void Dispose()
        {
            string[] captured;
            bool wasTruncated;
            lock (gate)
            {
                if (!active) return;
                active = false;
                captured = types.ToArray();
                wasTruncated = truncated;
            }
            AppDomain.CurrentDomain.FirstChanceException -= OnFirstChanceException;
            foreach (string type in captured)
                Emit("PS_FIRST_CHANCE_TYPE_" + type);
            Emit(wasTruncated ? "PS_FIRST_CHANCE_TRUNCATED_TRUE" : "PS_FIRST_CHANCE_TRUNCATED_FALSE");
        }
    }

    private static int ProbePowerShellInitializationCore(bool mountSyntheticDrive)
    {
        // Fixed diagnostic sequence, not an alternative script executor.
        // No user text or profiles are accepted. The fixed smoke pipeline only
        // emits literals/language mode and starts the trusted system cmd.exe.
        Emit("PS_INIT_ASSEMBLY_BEGIN");
        Assembly automation = Assembly.Load("System.Management.Automation, Version=3.0.0.0, Culture=neutral, PublicKeyToken=31bf3856ad364e35");
        Emit("PS_INIT_ASSEMBLY_LOADED");
        Type factory = automation.GetType("System.Management.Automation.Runspaces.RunspaceFactory", true);
        Emit("PS_INIT_RUNSPACE_CREATE_BEGIN");
        object runspace = factory.GetMethod("CreateRunspace", Type.EmptyTypes).Invoke(null, null);
        try
        {
            Emit("PS_INIT_RUNSPACE_CREATED");
            runspace.GetType().GetMethod("Open", Type.EmptyTypes).Invoke(runspace, null);
            Emit("PS_INIT_RUNSPACE_OPENED");
            ProbeDirectSystemCmd();
            if (mountSyntheticDrive)
            {
                // A runspace-only logical drive rooted at the existing fixture.
                // No mount point, ACL, privilege or OS drive mapping is changed.
                object proxy = runspace.GetType().GetProperty("SessionStateProxy").GetValue(runspace, null);
                object providers = proxy.GetType().GetProperty("Provider").GetValue(proxy, null);
                object fileProvider = null;
                foreach (object provider in (System.Collections.IEnumerable)providers.GetType().GetMethod("GetAll").Invoke(providers, null))
                {
                    string name = (string)provider.GetType().GetProperty("Name").GetValue(provider, null);
                    Emit("PS_PROVIDER_" + name);
                    if (name == "FileSystem") fileProvider = provider;
                }
                if (fileProvider == null) return Fail("PS_INIT_ERROR", "FileSystem provider missing");
                Type driveType = automation.GetType("System.Management.Automation.PSDriveInfo", true);
                Type credentialType = automation.GetType("System.Management.Automation.PSCredential", true);
                Type providerType = automation.GetType("System.Management.Automation.ProviderInfo", true);
                object drive = driveType.GetConstructor(new Type[] { typeof(string), providerType, typeof(string), typeof(string), credentialType })
                    .Invoke(new object[] { "SyntheticWork", fileProvider, Environment.CurrentDirectory, "synthetic fixture only", null });
                object drives = proxy.GetType().GetProperty("Drive").GetValue(proxy, null);
                drives.GetType().GetMethod("New", new Type[] { driveType, typeof(string) }).Invoke(drives, new object[] { drive, null });
                string root = Path.GetPathRoot(Environment.CurrentDirectory);
                object physicalDrive = driveType.GetConstructor(new Type[] { typeof(string), providerType, typeof(string), typeof(string), credentialType })
                    .Invoke(new object[] { root.Substring(0, 1), fileProvider, root, "logical drive only; unchanged kernel permissions", null });
                drives.GetType().GetMethod("New", new Type[] { driveType, typeof(string) }).Invoke(drives, new object[] { physicalDrive, null });
                Emit("PS_NATIVE_DRIVE_READY");
                object paths = proxy.GetType().GetProperty("Path").GetValue(proxy, null);
                paths.GetType().GetMethod("SetLocation", new Type[] { typeof(string) }).Invoke(paths, new object[] { "SyntheticWork:\\" });
                Emit("PS_SYNTHETIC_DRIVE_READY");
            }
            object pipeline = runspace.GetType().GetMethod("CreatePipeline", new Type[] { typeof(string) }).Invoke(
                runspace, new object[] { "Write-Output 'PS_NATIVE_ENGINE_OK'; Write-Output $ExecutionContext.SessionState.LanguageMode; Write-Output 'PS_NATIVE_CHILD_BEFORE'; & $env:ComSpec /d /c echo PS_NATIVE_CHILD_OK; Write-Output 'PS_NATIVE_CHILD_AFTER'; if ($null -eq $LASTEXITCODE) { Write-Output 'PS_NATIVE_LASTEXITCODE_NULL' } else { Write-Output ('PS_NATIVE_LASTEXITCODE_' + $LASTEXITCODE) }" });
            try
            {
                Emit("PS_INIT_PIPELINE_BEGIN");
                Emit(Environment.GetEnvironmentVariable("COMSPEC") == null
                    ? "PS_COMSPEC_PRESENT_FALSE" : "PS_COMSPEC_PRESENT_TRUE");
                Emit("PS_INIT_PIPELINE_INVOKE_BEGIN");
                System.Collections.IEnumerable output;
                try
                {
                    output = (System.Collections.IEnumerable)pipeline.GetType().GetMethod("Invoke", Type.EmptyTypes).Invoke(pipeline, null);
                    Emit("PS_INIT_PIPELINE_INVOKE_RETURNED");
                }
                catch (Exception ex)
                {
                    Emit("PS_INIT_PIPELINE_INVOKE_EXCEPTION_" + DiagnosticTypeToken(ex.GetType()));
                    EmitExceptionTypeChain("PS_INIT_PIPELINE_INVOKE_EXCEPTION_CHAIN_", ex);
                    throw;
                }
                bool engineSeen = false;
                bool childSeen = false;
                bool beforeSeen = false;
                bool afterSeen = false;
                bool childExitZeroSeen = false;
                foreach (object item in output)
                {
                    string line = item == null ? "" : item.ToString();
                    Emit(line);
                    engineSeen |= line == "PS_NATIVE_ENGINE_OK";
                    childSeen |= line == "PS_NATIVE_CHILD_OK";
                    beforeSeen |= line == "PS_NATIVE_CHILD_BEFORE";
                    afterSeen |= line == "PS_NATIVE_CHILD_AFTER";
                    childExitZeroSeen |= line == "PS_NATIVE_LASTEXITCODE_0";
                }
                object errors = pipeline.GetType().GetProperty("Error").GetValue(pipeline, null);
                bool hadErrors = false;
                foreach (object error in (System.Collections.IEnumerable)errors.GetType().GetMethod("ReadToEnd").Invoke(errors, null))
                {
                    hadErrors = true;
                    EmitPipelineErrorTypeChain(error);
                }
                if (hadErrors || !engineSeen || !childSeen || !beforeSeen || !afterSeen || !childExitZeroSeen)
                    return Fail("PS_PIPELINE_INCOMPLETE", "fixed engine/child markers or error-free pipeline missing");
                Emit("PS_INIT_PIPELINE_DONE");
            }
            finally
            {
                EmitPipelineStateAndFailure(pipeline);
                IDisposable disposablePipeline = pipeline as IDisposable;
                Emit("PS_INIT_PIPELINE_DISPOSE_BEFORE");
                try
                {
                    if (disposablePipeline != null) disposablePipeline.Dispose();
                }
                finally
                {
                    Emit("PS_INIT_PIPELINE_DISPOSE_AFTER");
                }
            }
        }
        finally
        {
            IDisposable disposable = runspace as IDisposable;
            Emit("PS_INIT_RUNSPACE_DISPOSE_BEFORE");
            try
            {
                if (disposable != null) disposable.Dispose();
            }
            finally
            {
                Emit("PS_INIT_RUNSPACE_DISPOSE_AFTER");
            }
        }
        Emit("PS_INIT_DONE");
        return 0;
    }

    private static void ProbeDirectSystemCmd()
    {
        Process child = null;
        try
        {
            var start = new ProcessStartInfo
            {
                FileName = Path.Combine(Environment.SystemDirectory, "cmd.exe"),
                Arguments = "/d /c echo PS_DIRECT_CHILD_OK",
                UseShellExecute = false,
                CreateNoWindow = true,
                WindowStyle = ProcessWindowStyle.Hidden,
                RedirectStandardOutput = true,
                RedirectStandardError = true
            };

            Emit("PS_DIRECT_CHILD_START_ATTEMPT");
            try
            {
                child = Process.Start(start);
            }
            catch (Exception ex)
            {
                Emit("PS_DIRECT_CHILD_START_EXCEPTION_" + DiagnosticTypeToken(ex.GetType()));
                return;
            }

            if (child == null)
            {
                Emit("PS_DIRECT_CHILD_START_RETURNED_NULL");
                return;
            }
            Emit("PS_DIRECT_CHILD_START_RETURNED");

            bool exited = child.WaitForExit(DiagnosticCmdTimeoutMs);
            if (!exited)
            {
                Emit("PS_DIRECT_CHILD_TIMEOUT");
                try
                {
                    child.Kill();
                    Emit("PS_DIRECT_CHILD_KILL_REQUESTED");
                }
                catch (Exception ex)
                {
                    Emit("PS_DIRECT_CHILD_KILL_EXCEPTION_" + DiagnosticTypeToken(ex.GetType()));
                }

                try
                {
                    exited = child.WaitForExit(500);
                    Emit(exited ? "PS_DIRECT_CHILD_EXIT_OBSERVED_AFTER_KILL"
                                : "PS_DIRECT_CHILD_EXIT_NOT_OBSERVED_AFTER_KILL");
                }
                catch (Exception ex)
                {
                    Emit("PS_DIRECT_CHILD_WAIT_EXCEPTION_" + DiagnosticTypeToken(ex.GetType()));
                }
            }
            else
            {
                Emit("PS_DIRECT_CHILD_EXIT_CODE_" + child.ExitCode.ToString(CultureInfo.InvariantCulture));
            }

            if (!exited)
            {
                Emit("PS_DIRECT_CHILD_OUTPUT_CAPTURE_SKIPPED");
                return;
            }

            try
            {
                string stdout = child.StandardOutput.ReadToEnd();
                Emit("PS_DIRECT_CHILD_STDOUT_CAPTURED_TRUE");
                Emit(ContainsExactLine(stdout, "PS_DIRECT_CHILD_OK")
                    ? "PS_DIRECT_CHILD_STDOUT_MARKER_PRESENT_TRUE"
                    : "PS_DIRECT_CHILD_STDOUT_MARKER_PRESENT_FALSE");
            }
            catch (Exception ex)
            {
                Emit("PS_DIRECT_CHILD_STDOUT_CAPTURE_EXCEPTION_" + DiagnosticTypeToken(ex.GetType()));
            }

            try
            {
                string stderr = child.StandardError.ReadToEnd();
                Emit("PS_DIRECT_CHILD_STDERR_CAPTURED_TRUE");
                Emit(String.IsNullOrEmpty(stderr)
                    ? "PS_DIRECT_CHILD_STDERR_EMPTY_TRUE"
                    : "PS_DIRECT_CHILD_STDERR_EMPTY_FALSE");
            }
            catch (Exception ex)
            {
                Emit("PS_DIRECT_CHILD_STDERR_CAPTURE_EXCEPTION_" + DiagnosticTypeToken(ex.GetType()));
            }
        }
        catch (Exception ex)
        {
            Emit("PS_DIRECT_CHILD_DIAGNOSTIC_EXCEPTION_" + DiagnosticTypeToken(ex.GetType()));
        }
        finally
        {
            if (child != null)
            {
                try
                {
                    if (!child.HasExited)
                    {
                        child.Kill();
                        child.WaitForExit(500);
                    }
                }
                catch (Exception ex)
                {
                    Emit("PS_DIRECT_CHILD_CLEANUP_EXCEPTION_" + DiagnosticTypeToken(ex.GetType()));
                }
                child.Dispose();
            }
        }
    }

    private static bool ContainsExactLine(string text, string expected)
    {
        using (StringReader reader = new StringReader(text ?? String.Empty))
        {
            string line;
            while ((line = reader.ReadLine()) != null)
            {
                if (String.Equals(line, expected, StringComparison.Ordinal))
                    return true;
            }
        }
        return false;
    }

    private static void EmitPipelineStateAndFailure(object pipeline)
    {
        try
        {
            PropertyInfo stateInfoProperty = pipeline.GetType().GetProperty("PipelineStateInfo");
            object stateInfo = stateInfoProperty == null ? null : stateInfoProperty.GetValue(pipeline, null);
            if (stateInfo == null)
            {
                Emit("PS_PIPELINE_STATE_UNAVAILABLE");
                Emit("PS_PIPELINE_FAILURE_REASON_UNAVAILABLE");
                return;
            }

            PropertyInfo stateProperty = stateInfo.GetType().GetProperty("State");
            object state = stateProperty == null ? null : stateProperty.GetValue(stateInfo, null);
            Emit("PS_PIPELINE_STATE_" + DiagnosticValueToken(state));

            PropertyInfo reasonProperty = stateInfo.GetType().GetProperty("Reason");
            object reason = reasonProperty == null ? null : reasonProperty.GetValue(stateInfo, null);
            Emit(reason == null
                ? "PS_PIPELINE_FAILURE_REASON_NONE"
                : "PS_PIPELINE_FAILURE_REASON_TYPE_" + DiagnosticTypeToken(reason.GetType()));
            if (reason != null)
                EmitExceptionTypeChain("PS_PIPELINE_FAILURE_REASON_CHAIN_", reason);
        }
        catch (Exception ex)
        {
            Emit("PS_PIPELINE_DIAGNOSTIC_EXCEPTION_" + DiagnosticTypeToken(ex.GetType()));
        }
    }

    private static void EmitPipelineErrorTypeChain(object rawError)
    {
        try
        {
            object record = UnwrapDiagnosticPsObject(rawError);
            Emit("PS_PIPELINE_ERROR_RECORD_TYPE_" +
                DiagnosticTypeToken(record == null ? null : record.GetType()));
            PropertyInfo exceptionProperty = record == null ? null : record.GetType().GetProperty("Exception");
            object errorException = exceptionProperty == null ? null : exceptionProperty.GetValue(record, null);
            errorException = UnwrapDiagnosticPsObject(errorException);
            Console.Error.WriteLine("PS_PIPELINE_ERROR_TYPE_" +
                DiagnosticTypeToken(errorException == null ? null : errorException.GetType()));
            EmitExceptionTypeChain("PS_PIPELINE_ERROR_CHAIN_", errorException);
        }
        catch (Exception ex)
        {
            // Inspection failures remain failures because the caller already set hadErrors.
            Emit("PS_PIPELINE_ERROR_INSPECT_EXCEPTION_" + DiagnosticTypeToken(ex.GetType()));
        }
    }

    private static object UnwrapDiagnosticPsObject(object value)
    {
        for (int depth = 0; value != null && depth < MaxDiagnosticErrorDepth; depth++)
        {
            Type type = value.GetType();
            if (!String.Equals(type.FullName, "System.Management.Automation.PSObject", StringComparison.Ordinal))
                break;
            PropertyInfo baseObjectProperty = type.GetProperty("BaseObject");
            object baseObject = baseObjectProperty == null ? null : baseObjectProperty.GetValue(value, null);
            if (baseObject == null || Object.ReferenceEquals(baseObject, value))
                break;
            value = baseObject;
        }
        return value;
    }

    private static void EmitExceptionTypeChain(string prefix, object value)
    {
        object current = value;
        int depth = 0;
        while (current != null && depth < MaxDiagnosticErrorDepth)
        {
            current = UnwrapDiagnosticPsObject(current);
            Emit(prefix + depth.ToString(CultureInfo.InvariantCulture) + "_" +
                DiagnosticTypeToken(current.GetType()));
            Exception exception = current as Exception;
            current = exception == null ? null : exception.InnerException;
            depth++;
        }
        if (depth == 0) Emit(prefix + "NONE");
        if (current != null) Emit(prefix + "TRUNCATED");
    }

    private static string DiagnosticValueToken(object value)
    {
        return SafeDiagnosticToken(value == null ? null : value.ToString(), 32);
    }

    private static string DiagnosticTypeToken(Type type)
    {
        return SafeDiagnosticToken(type == null ? null : type.Name, 48);
    }

    private static string SafeDiagnosticToken(string value, int maxLength)
    {
        if (String.IsNullOrEmpty(value))
            return "Unknown";

        StringBuilder token = new StringBuilder(Math.Min(value.Length, maxLength));
        for (int i = 0; i < value.Length && token.Length < maxLength; i++)
        {
            char current = value[i];
            if ((current >= 'a' && current <= 'z') || (current >= 'A' && current <= 'Z') ||
                (current >= '0' && current <= '9'))
                token.Append(current);
            else
                token.Append('_');
        }
        return token.Length == 0 ? "Unknown" : token.ToString();
    }

    private static int RunBoundary(string[] args)
    {
        if (args.Length != 5)
            return Fail("USAGE", "Expected boundary <workspace> <outside> <port> <nonce>.");

        string workspace = Path.GetFullPath(args[1]);
        string outside = Path.GetFullPath(args[2]);
        int port;
        if (!Int32.TryParse(args[3], NumberStyles.None, CultureInfo.InvariantCulture, out port) ||
            port < 1 || port > 65535)
            return Fail("USAGE", "Port must be between 1 and 65535.");

        string nonce = args[4];
        if (!IsSafeNonce(nonce))
            return Fail("USAGE", "Nonce must contain only ASCII letters, digits, hyphen, or underscore.");
        // The host established the outside fixture; querying it from inside
        // the boundary may itself be denied. Test the actual file operation.
        if (!Directory.Exists(workspace))
            return Fail("PATH_ERROR", "Synthetic workspace must already exist.");

        Emit("EXE_" + nonce);
        bool success = true;
        if (Environment.GetEnvironmentVariable(SyntheticApiKeyName) == null)
            Emit("ENV_CLEAN");
        else
        {
            Emit("ENV_LEAK");
            success = false;
        }

        string inputPath = Path.Combine(workspace, "input.txt");
        try
        {
            if (String.Equals(File.ReadAllText(inputPath), "SYNTHETIC_INPUT", StringComparison.Ordinal))
                Emit("INPUT_OK");
            else
            {
                Emit("INPUT_INVALID");
                success = false;
            }
        }
        catch (Exception ex)
        {
            Emit("INPUT_UNKNOWN_" + ex.GetType().Name);
            success = false;
        }

        try
        {
            File.WriteAllText(Path.Combine(workspace, "native-output.txt"), "SYNTHETIC_OUTPUT");
            Emit("OUTPUT_OK");
        }
        catch (Exception ex)
        {
            Emit("OUTPUT_UNKNOWN_" + ex.GetType().Name);
            success = false;
        }

        success = ProbeRead(Path.Combine(outside, "ordinary.txt"),
            "OUTSIDE_READ_DENIED", "OUTSIDE_READ_ALLOWED", "OUTSIDE_READ_UNKNOWN") && success;
        success = ProbeRead(Path.Combine(outside, ".env"),
            "SECRET_READ_DENIED", "SECRET_READ_ALLOWED", "SECRET_READ_UNKNOWN") && success;
        success = ProbeRead(Path.Combine(workspace, "junction", "ordinary.txt"),
            "JUNCTION_READ_DENIED", "JUNCTION_READ_ALLOWED", "JUNCTION_READ_UNKNOWN") && success;
        success = ProbeWrite(Path.Combine(outside, "escaped.txt")) && success;
        success = ProbeLoopback(port, nonce) && success;

        return success ? 0 : 2;
    }

    private static bool ProbeRead(string path, string deniedMarker, string allowedMarker, string unknownMarker)
    {
        try
        {
            File.ReadAllText(path);
            Emit(allowedMarker);
            return false;
        }
        catch (UnauthorizedAccessException)
        {
            Emit(deniedMarker);
            return true;
        }
        catch (Exception ex)
        {
            Emit(unknownMarker + "_" + ex.GetType().Name);
            return false;
        }
    }

    private static bool ProbeWrite(string path)
    {
        try
        {
            File.WriteAllText(path, "SYNTHETIC_ONLY");
            Emit("OUTSIDE_WRITE_ALLOWED");
            return false;
        }
        catch (UnauthorizedAccessException)
        {
            Emit("OUTSIDE_WRITE_DENIED");
            return true;
        }
        catch (Exception ex)
        {
            Emit("OUTSIDE_WRITE_UNKNOWN_" + ex.GetType().Name);
            return false;
        }
    }

    private static bool ProbeLoopback(int port, string nonce)
    {
        TcpClient client = null;
        try
        {
            client = new TcpClient();
            IAsyncResult pending = client.BeginConnect(IPAddress.Loopback, port, null, null);
            using (WaitHandle waitHandle = pending.AsyncWaitHandle)
            {
                if (!waitHandle.WaitOne(NetworkTimeoutMs))
                {
                    client.Close();
                    Emit("NETWORK_UNKNOWN_TIMEOUT");
                    return false;
                }
            }

            client.EndConnect(pending);
            byte[] payload = Encoding.ASCII.GetBytes(nonce);
            NetworkStream stream = client.GetStream();
            stream.WriteTimeout = NetworkTimeoutMs;
            stream.Write(payload, 0, payload.Length);
            stream.Flush();
            Emit("NETWORK_ALLOWED");
            return false;
        }
        catch (SocketException ex)
        {
            if (ex.SocketErrorCode == SocketError.AccessDenied)
            {
                Emit("NETWORK_DENIED");
                return true;
            }
            Emit("NETWORK_UNKNOWN_" + ex.SocketErrorCode.ToString());
            return false;
        }
        catch (Exception ex)
        {
            Emit("NETWORK_UNKNOWN_" + ex.GetType().Name);
            return false;
        }
        finally
        {
            if (client != null)
                client.Close();
        }
    }

    private static int RunChildren(string[] args)
    {
        if (args.Length != 2 ||
            (!String.Equals(args[1], "root-exit", StringComparison.Ordinal) &&
             !String.Equals(args[1], "timeout", StringComparison.Ordinal)))
            return Fail("USAGE", "Expected children <root-exit|timeout>.");

        string executable = Assembly.GetExecutingAssembly().Location;
        var start = new ProcessStartInfo
        {
            FileName = executable,
            Arguments = "sleep",
            UseShellExecute = false,
            CreateNoWindow = true,
            WindowStyle = ProcessWindowStyle.Hidden
        };

        using (Process child = Process.Start(start))
        {
            if (child == null)
                return Fail("CHILD_START_FAILED", "Process.Start returned null.");
            Emit("CHILD_PID_" + child.Id.ToString(CultureInfo.InvariantCulture));
        }

        if (String.Equals(args[1], "root-exit", StringComparison.Ordinal))
            return 0;

        Thread.Sleep(ChildSleepMs);
        return 0;
    }

    private static int RunSleep(string[] args)
    {
        if (args.Length != 1)
            return Fail("USAGE", "Internal sleep mode takes no arguments.");
        Thread.Sleep(ChildSleepMs);
        return 0;
    }

    private static bool IsSafeNonce(string value)
    {
        if (String.IsNullOrEmpty(value) || value.Length > 128)
            return false;
        for (int i = 0; i < value.Length; i++)
        {
            char current = value[i];
            if (!((current >= 'a' && current <= 'z') ||
                  (current >= 'A' && current <= 'Z') ||
                  (current >= '0' && current <= '9') || current == '-' || current == '_'))
                return false;
        }
        return true;
    }

    private static void Emit(string line)
    {
        Console.Out.WriteLine(line);
        Console.Out.Flush();
    }

    private static int Fail(string marker, string detail)
    {
        Console.Error.WriteLine(marker + ": " + detail);
        Console.Error.Flush();
        return 2;
    }
}
