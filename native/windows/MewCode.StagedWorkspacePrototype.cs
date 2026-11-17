// Shared staged-workspace protocol. The standard runtime compiles this file
// and dispatches the versioned production envelope from stdin. The historical
// prototype entry and schema remain available for isolated diagnostics.
using System;
using System.Collections.Generic;
using System.IO;
using System.Security.AccessControl;
using System.Security.Principal;
using System.Text;

namespace MewCode.Native.Windows
{
    public static class MewCodeStagedWorkspacePrototype
    {
        // Kept for diagnostic bundles that explicitly select this entry point.
        public static int Main(string[] args) { return MewCodeAppContainerProbe.StagedMain(args); }
    }

    public static partial class MewCodeAppContainerProbe
    {
        internal const string StagedRuntimeSchema = "mewcode-lpac-staged-request-v1";
        private const string StagedPrototypeSchema = "mewcode-lpac-staged-prototype-v1";
        private const int MaxStagedEntries = 12000;

        // Legacy diagnostic entry. Its wrapper did not carry original_workspace
        // and is intentionally accepted only through this separately selected
        // entry point, never through the production RuntimeMain dispatcher.
        internal static int StagedMain(string[] args)
        {
            var receipt = NewReceipt();
            string requestId = null;
            try
            {
                string payload = ReadHostPayload(args);
                var envelope = NewSerializer().Deserialize<StagedRuntimeRequestEnvelope>(payload);
                if (envelope == null || envelope.schema != StagedPrototypeSchema)
                    throw new InvalidDataException("Not a staged prototype request.");
                var validated = ValidateStagedEnvelope(envelope, false);
                requestId = envelope.request.request_id;
                Execute(null, receipt, validated);
            }
            catch (Exception error) { AddError(receipt, Describe(error)); }
            return WriteHostReceipt(receipt, requestId);
        }

        internal static ValidatedRequest ValidateStagedRuntimeRequest(StagedRuntimeRequestEnvelope envelope)
        {
            return ValidateStagedEnvelope(envelope, true);
        }

        private static ProbeReceipt NewReceipt()
        {
            return new ProbeReceipt { stdout = "", stderr = "", error = "",
                capabilities = new string[0], job_cleanup_ok = true,
                profile_cleanup_ok = true, acl_cleanup_ok = true };
        }

        private static string ReadHostPayload(string[] args)
        {
            if (args.Length != 1 || args[0] != "--host-stdin-v1")
                throw new InvalidDataException("Host stdin required.");
            var chars = new char[MaxRequestBytes + 1];
            int count = 0, read;
            using (var input = new StreamReader(Console.OpenStandardInput(), new UTF8Encoding(false, true)))
            {
                while (count < chars.Length && (read = input.Read(chars, count, chars.Length - count)) > 0)
                    count += read;
            }
            if (count == 0 || count > MaxRequestBytes) throw new InvalidDataException("Invalid request size.");
            return new String(chars, 0, count);
        }

        private static int WriteHostReceipt(ProbeReceipt receipt, string requestId)
        {
            Console.OutputEncoding = new UTF8Encoding(false);
            Console.WriteLine(NewSerializer().Serialize(new { schema = "mewcode-lpac-receipt-v1",
                request_id = requestId, backend = "mewcode-windows-lpac", receipt = receipt }));
            return String.IsNullOrEmpty(receipt.error) && !receipt.timed_out && !receipt.cancelled && receipt.exit_code.HasValue
                ? receipt.exit_code.Value : 1;
        }

        private static ValidatedRequest ValidateStagedEnvelope(StagedRuntimeRequestEnvelope envelope,
            bool production)
        {
            if (envelope == null || envelope.request == null)
                throw new InvalidDataException("Invalid staged runtime envelope.");
            if (production)
            {
                Guid envelopeGuid;
                if (envelope.schema != StagedRuntimeSchema ||
                    !Guid.TryParseExact(envelope.request_id, "N", out envelopeGuid) ||
                    !String.Equals(envelope.request_id, envelope.request.request_id, StringComparison.Ordinal) ||
                    envelope.timeout_ms != envelope.request.timeout_ms)
                    throw new InvalidDataException("Invalid staged envelope identity or timeout.");
            }
            else
            {
                if (!String.IsNullOrEmpty(envelope.request_id) &&
                    !String.Equals(envelope.request_id, envelope.request.request_id, StringComparison.Ordinal))
                    throw new InvalidDataException("Invalid staged prototype identity.");
                if (envelope.timeout_ms != 0 && envelope.timeout_ms != envelope.request.timeout_ms)
                    throw new InvalidDataException("Invalid staged prototype timeout.");
            }

            var validated = ValidateRuntimeRequest(envelope.request);
            string workspace = validated.FixtureRoot;
            ValidateStagedWorkspaceRoot(workspace);

            if (production)
            {
                string original = ValidateExistingPath(envelope.original_workspace, "original_workspace", true);
                if (String.Equals(original, Path.GetPathRoot(original), StringComparison.OrdinalIgnoreCase))
                    throw new InvalidDataException("A volume root is not an original workspace.");
                if (PathsOverlap(original, workspace))
                    throw new InvalidDataException("Original and staged workspaces must be disjoint.");
                ValidateNoOriginalWorkspaceGrants(original, validated);
            }

            ValidateStagedTree(workspace);

            // The mutable root ACE is inheritable, so nested ordinary grants
            // are redundant and could conflict with read-only protections.
            // Keep only exact grants outside the staged workspace (tool/runtime
            // inputs), then replace the staged root with its bounded tree grant.
            var retained = new Dictionary<string, PathGrant>(StringComparer.OrdinalIgnoreCase);
            foreach (var pair in validated.Grants)
                if (!IsStrictlyBeneath(pair.Key, workspace)) retained.Add(pair.Key, pair.Value);
            retained[workspace] = new PathGrant { Path = workspace, IsDirectory = true,
                Read = true, Write = true, Execute = true, StagedMutableTree = true };
            AddStagedProtected(envelope.readonly_paths, workspace, retained, true);
            AddStagedProtected(envelope.pinned_containers, workspace, retained, false);
            if (production)
            {
                // Only fresh private runtime subtrees beside this copy. New
                // TEMP/profile files must be reopenable just like workspace
                // outputs; never grant inheritance to a real user profile.
                string runtime = Path.Combine(Path.GetDirectoryName(workspace), "runtime");
                string[] roots = { Path.Combine(runtime, "temp"), Path.Combine(runtime, "profile") };
                string[] names = { "TEMP", "USERPROFILE" };
                for (int i = 0; i < roots.Length; i++)
                {
                    roots[i] = ValidateExistingPath(roots[i], "private runtime", true);
                    string configured;
                    PathGrant grant;
                    if (!validated.Environment.TryGetValue(names[i], out configured) ||
                        !IsSamePath(configured, roots[i]) || !retained.TryGetValue(roots[i], out grant) || !grant.Write)
                        throw new InvalidDataException("Private runtime scope does not match host environment.");
                    ValidateStagedTree(roots[i]);
                    grant.StagedMutableTree = true;
                }
                validated.StagedRuntimeRoots = roots;
            }
            validated.Grants = retained;
            validated.StagedWorkspace = workspace;
            return validated;
        }

        private static void ValidateStagedWorkspaceRoot(string workspace)
        {
            string normalized = TrimSeparators(Path.GetFullPath(workspace));
            if (!String.Equals(Path.GetFileName(normalized), "workspace", StringComparison.OrdinalIgnoreCase))
                throw new InvalidDataException("Staged workspace must be named workspace.");
            string parent = Path.GetDirectoryName(normalized);
            if (String.IsNullOrEmpty(parent))
                throw new InvalidDataException("A private staged workspace is required.");
            parent = ValidateExistingPath(parent, "staging transaction", true);
            string leaf = Path.GetFileName(TrimSeparators(parent));
            const string prefix = "stage-";
            if (leaf == null || !leaf.StartsWith(prefix, StringComparison.OrdinalIgnoreCase) ||
                leaf.Length < prefix.Length + 8 || leaf.Length > prefix.Length + 64 ||
                !IsSafeRandomSuffix(leaf.Substring(prefix.Length)))
                throw new InvalidDataException("Staged workspace must be a direct child of a stage-* directory.");
        }

        private static bool PathsOverlap(string first, string second)
        {
            string left = TrimSeparators(Path.GetFullPath(first));
            string right = TrimSeparators(Path.GetFullPath(second));
            return String.Equals(left, right, StringComparison.OrdinalIgnoreCase) ||
                IsStrictlyBeneath(left, right) || IsStrictlyBeneath(right, left);
        }

        private static void ValidateNoOriginalWorkspaceGrants(string original, ValidatedRequest request)
        {
            foreach (PathGrant grant in request.Grants.Values)
            {
                if (String.Equals(grant.Path, original, StringComparison.OrdinalIgnoreCase) ||
                    IsStrictlyBeneath(grant.Path, original))
                    throw new InvalidDataException("An exact grant targets the original workspace.");
                if (IsStrictlyBeneath(original, grant.Path) &&
                    !IsHarmlessRuntimeAncestorGrant(grant, request))
                    throw new InvalidDataException("An exact grant is an ancestor of the original workspace.");
            }
        }

        private static bool IsHarmlessRuntimeAncestorGrant(PathGrant grant, ValidatedRequest request)
        {
            // Exact grants never inherit. A read-only, non-executable grant on
            // a configured OS/runtime directory can be an ancestor boundary;
            // the original workspace itself and every descendant remain denied.
            if (!grant.IsDirectory || grant.Write || grant.Execute) return false;
            string[] exactNames = { "SystemRoot", "WINDIR", "TEMP", "TMP", "USERPROFILE",
                "APPDATA", "LOCALAPPDATA", "HOME" };
            foreach (string name in exactNames)
            {
                string candidate;
                if (request.Environment.TryGetValue(name, out candidate) && IsSamePath(candidate, grant.Path))
                    return true;
            }
            string path;
            if (request.Environment.TryGetValue("PATH", out path))
                foreach (string candidate in path.Split(';'))
                    if (!String.IsNullOrWhiteSpace(candidate) && IsSamePath(candidate, grant.Path)) return true;
            return false;
        }

        private static bool IsSamePath(string first, string second)
        {
            try { return String.Equals(TrimSeparators(Path.GetFullPath(first)),
                TrimSeparators(Path.GetFullPath(second)), StringComparison.OrdinalIgnoreCase); }
            catch { return false; }
        }

        private static void AddStagedProtected(string[] paths, string workspace,
            Dictionary<string, PathGrant> grants, bool readOnly)
        {
            if (paths == null || paths.Length > MaxStagedEntries)
                throw new InvalidDataException("Invalid staged protection list.");
            foreach (string raw in paths)
            {
                string path = ValidateExistingPath(raw, "staged protection", null);
                if (!IsStrictlyBeneath(path, workspace))
                    throw new InvalidDataException("Protection outside staged workspace.");
                bool directory = (File.GetAttributes(NativeFilesystemPath(path)) & FileAttributes.Directory) != 0;
                if (!readOnly && !directory)
                    throw new InvalidDataException("Pinned container must be a directory.");
                if (grants.ContainsKey(path))
                    throw new InvalidDataException("Duplicate staged protection.");
                grants.Add(path, new PathGrant { Path = path, IsDirectory = directory,
                    Read = true, Execute = !directory, StagedReadOnly = readOnly });
            }
        }

        private static void ValidateStagedTree(string root)
        {
            var pending = new Stack<string>();
            pending.Push(root);
            int count = 0;
            while (pending.Count > 0)
            {
                string path = pending.Pop();
                if (++count > MaxStagedEntries)
                    throw new InvalidDataException("Staged tree exceeds the bound.");
                CheckNoReparseAncestors(path, "staged tree");
                FileAttributes attrs = File.GetAttributes(NativeFilesystemPath(path));
                if ((attrs & FileAttributes.Directory) == 0)
                    RejectHardLinkedFile(path, "staged file");
                else
                    foreach (string child in Directory.EnumerateFileSystemEntries(NativeFilesystemPath(path)))
                    {
                        if (count + pending.Count >= MaxStagedEntries)
                            throw new InvalidDataException("Staged tree exceeds the bound.");
                        pending.Push(PlainFilesystemPath(child));
                    }
            }
        }

        private static void ApplyStagedGrant(PathGrant grant, SecurityIdentifier sid, LaunchState state)
        {
            // Modify, not FullControl: no WRITE_DAC, WRITE_OWNER or
            // FILE_DELETE_CHILD. Read-only leaves additionally deny DELETE.
            FileSystemRights rights = grant.StagedMutableTree ? FileSystemRights.Modify :
                FileSystemRights.Write | FileSystemRights.Delete;
            var inheritance = grant.IsDirectory ? InheritanceFlags.ContainerInherit | InheritanceFlags.ObjectInherit :
                InheritanceFlags.None;
            grant.Rule = new FileSystemAccessRule(sid, rights, inheritance, PropagationFlags.None,
                grant.StagedMutableTree ? AccessControlType.Allow : AccessControlType.Deny);
            state.AppliedGrants.Add(grant);
            if (grant.IsDirectory)
            {
                var security = Directory.GetAccessControl(NativeFilesystemPath(grant.Path));
                security.AddAccessRule(grant.Rule);
                Directory.SetAccessControl(NativeFilesystemPath(grant.Path), security);
            }
            else
            {
                var security = File.GetAccessControl(NativeFilesystemPath(grant.Path));
                security.AddAccessRule(grant.Rule);
                File.SetAccessControl(NativeFilesystemPath(grant.Path), security);
            }
        }
    }
}
