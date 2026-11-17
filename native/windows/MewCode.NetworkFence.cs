// Per-AppContainer outbound policy used with the stable LPAC/internetClient profile.
// This host-only utility never broadens policy beyond one validated AppContainer SID.
using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Diagnostics;
using System.IO;
using System.Net;
using System.Runtime.InteropServices;
using System.Security.Principal;
using System.Security.Cryptography;
using System.Text;
using System.Text.RegularExpressions;
using System.Web.Script.Serialization;

namespace MewCode.Native.Windows
{
    internal sealed class NetworkFenceException : Exception
    {
        public readonly string Code;
        public NetworkFenceException(string code, string detail) : base(detail) { Code = code; }
    }

    internal sealed class FenceKeys
    {
        public readonly Guid Provider;
        public readonly Guid Sublayer;
        public readonly Dictionary<string, Guid> Filters;

        public FenceKeys(string sid)
        {
            Provider = DeriveGuid(sid, "provider");
            Sublayer = DeriveGuid(sid, "sublayer");
            Filters = new Dictionary<string, Guid>(StringComparer.Ordinal)
            {
                { "block_v4", DeriveGuid(sid, "filter:block:v4") },
                { "block_v6", DeriveGuid(sid, "filter:block:v6") },
                { "allow_v4_60080", DeriveGuid(sid, "filter:allow:v4:60080") },
                { "allow_v4_60081", DeriveGuid(sid, "filter:allow:v4:60081") },
                { "allow_v6_60080", DeriveGuid(sid, "filter:allow:v6:60080") },
                { "allow_v6_60081", DeriveGuid(sid, "filter:allow:v6:60081") },
            };
        }

        private static Guid DeriveGuid(string sid, string purpose)
        {
            byte[] digest;
            using (SHA256 sha = SHA256.Create())
                digest = sha.ComputeHash(Encoding.UTF8.GetBytes("MewCode.NetworkFence.v1\n" + purpose + "\n" + sid));
            byte[] value = new byte[16];
            Buffer.BlockCopy(digest, 0, value, 0, value.Length);
            return new Guid(value);
        }
    }

    internal sealed class ParsedOptions
    {
        public string Operation;
        public string Sid;
        public string ReportPath;
    }

    public static class MewCodeNetworkFence
    {
        private const string Schema = "mewcode-network-fence-v1";
        private const uint RpcCAuthnWinnt = 10;
        private const uint FwpEFilterNotFound = 0x80320003;
        private const uint FwpEProviderNotFound = 0x80320005;
        private const uint FwpESublayerNotFound = 0x80320007;
        private const uint ErrorAccessDenied = 5;
        private const uint EAccessDenied = 0x80070005;
        private const uint FwpmProviderFlagPersistent = 0x00000001;
        private const uint FwpmSublayerFlagPersistent = 0x00000001;
        private const uint FwpmFilterFlagPersistent = 0x00000001;
        private const uint FwpActionBlock = 0x00001001;
        private const uint FwpActionPermit = 0x00001002;
        private const uint FwpMatchEqual = 0;
        private const uint FwpUint8 = 1;
        private const uint FwpUint16 = 2;
        private const uint FwpSid = 13;
        private const uint FwpV4AddrMask = 0x100;
        private const uint FwpV6AddrMask = 0x101;
        private const uint FwpUint64 = 4;
        private const byte IpProtocolTcp = 6;
        private const ushort ProxyPortHttpBackend = 60080;
        private const ushort ProxyPortMux = 60081;
        private const uint FilterBlockWeight = 100;
        private const uint FilterPermitWeight = 200;

        // GUIDs copied from Windows SDK 10.0.26100.0, um/fwpmu.h.
        private static readonly Guid LayerAleAuthConnectV4 = new Guid("c38d57d1-05a7-4c33-904f-7fbceee60e82");
        private static readonly Guid LayerAleAuthConnectV6 = new Guid("4a72393b-319f-44bc-84c3-ba54dcb3b6b4");
        private static readonly Guid ConditionAlePackageId = new Guid("71bc78fa-f17c-4997-a602-6abb261f351c");
        private static readonly Guid ConditionIpRemoteAddress = new Guid("b235ae9a-1d64-49b8-a44c-5ff3d9095045");
        private static readonly Guid ConditionIpProtocol = new Guid("3971ef2b-623e-4f9a-8cb1-6e79b806b9a7");
        private static readonly Guid ConditionIpRemotePort = new Guid("c35a604d-d22b-4e1a-91b4-68f674ee674b");

        public static int Main(string[] args)
        {
            Console.OutputEncoding = new UTF8Encoding(false);
            if (args != null && args.Length == 1 && args[0] == "--validate-layouts")
            {
                try
                {
                    Dictionary<string, int> layout = ValidateLayouts();
                    Console.WriteLine(Serialize(new Dictionary<string, object>
                    {
                        { "schema", "mewcode-network-fence-layout-v1" }, { "ok", true }, { "layout", layout }
                    }));
                    return 0;
                }
                catch (Exception error)
                {
                    Console.WriteLine(Serialize(ErrorResult("layout_validation_failed", SafeDetail(error))));
                    return 1;
                }
            }

            ParsedOptions options = null;
            FileStream report = null;
            Dictionary<string, object> result = null;
            try
            {
                ValidateLayouts();
                options = ParseOptions(args);
                string sid = ValidateAppContainerSid(options.Sid);
                FenceKeys keys = new FenceKeys(sid);
                result = BaseResult(options.Operation, sid, keys);
                if (options.Operation == "install")
                {
                    string path = ValidateReportPath(options.ReportPath);
                    report = new FileStream(path, FileMode.CreateNew, FileAccess.ReadWrite, FileShare.Read);
                    result["ok"] = false;
                    result["error_code"] = "installation_in_progress";
                    result["detail"] = "Host setup has not completed.";
                    WriteReport(report, result);
                    Install(sid, keys, result);
                    result["ok"] = true;
                    result["error_code"] = "";
                    result["detail"] = "Per-AppContainer network fence installed.";
                }
                else if (options.Operation == "status")
                {
                    result = ReadStatus(sid, keys);
                }
                else if (options.Operation == "uninstall")
                {
                    Uninstall(sid, keys, result);
                    result["ok"] = true;
                    result["error_code"] = "";
                    result["detail"] = "Only this AppContainer network fence and loopback exemption were removed.";
                }
                else
                {
                    throw new NetworkFenceException("invalid_arguments", "Expected --install, --status, or --uninstall.");
                }
            }
            catch (NetworkFenceException error)
            {
                result = result ?? ErrorResult(error.Code, error.Message);
                result["ok"] = false;
                result["error_code"] = error.Code;
                result["detail"] = error.Message;
                result["requires_administrator"] = error.Code == "administrator_required";
            }
            catch (Exception error)
            {
                result = result ?? ErrorResult("network_fence_runtime_error", SafeDetail(error));
                result["ok"] = false;
                result["error_code"] = "network_fence_runtime_error";
                result["detail"] = SafeDetail(error);
            }
            finally
            {
                if (report != null)
                {
                    try { WriteReport(report, result ?? ErrorResult("network_fence_runtime_error", "No result was produced.")); }
                    catch (Exception)
                    {
                        result = result ?? ErrorResult("report_write_failed", "The report could not be written.");
                        result["ok"] = false;
                        result["error_code"] = "report_write_failed";
                        result["detail"] = "The host setup result could not be persisted to the requested report path.";
                    }
                    finally { report.Dispose(); }
                }
            }
            result = result ?? ErrorResult("network_fence_runtime_error", "No result was produced.");
            Console.WriteLine(Serialize(result));
            return ObjectBool(result, "ok") ? 0 : 1;
        }

        private static ParsedOptions ParseOptions(string[] args)
        {
            if (args == null) args = new string[0];
            if (args.Length == 3 && args[0] == "--status" && args[1] == "--sid")
                return new ParsedOptions { Operation = "status", Sid = args[2] };
            if (args.Length == 3 && args[0] == "--uninstall" && args[1] == "--sid")
                return new ParsedOptions { Operation = "uninstall", Sid = args[2] };
            if (args.Length == 5 && args[0] == "--install" && args[1] == "--sid" && args[3] == "--report")
                return new ParsedOptions { Operation = "install", Sid = args[2], ReportPath = args[4] };
            throw new NetworkFenceException("invalid_arguments",
                "Use --status --sid SID, --uninstall --sid SID, or --install --sid SID --report PATH.");
        }

        private static string ValidateAppContainerSid(string value)
        {
            SecurityIdentifier sid;
            try { sid = new SecurityIdentifier(value ?? ""); }
            catch (Exception) { throw new NetworkFenceException("invalid_appcontainer_sid", "SID is not a valid Windows security identifier."); }
            string canonical = sid.Value;
            string[] parts = canonical.Split('-');
            // SECURITY_APP_PACKAGE_AUTHORITY = S-1-15; BASE_RID = 2;
            // SECURITY_APP_PACKAGE_RID_COUNT is 8 in the Windows SDK. Profile SIDs
            // therefore have exactly eight sub-authorities, beginning with RID 2.
            if (parts.Length != 11 || parts[0] != "S" || parts[1] != "1" || parts[2] != "15" || parts[3] != "2")
                throw new NetworkFenceException("invalid_appcontainer_sid", "Only a full S-1-15-2 AppContainer profile SID is accepted.");
            return canonical;
        }

        private static string ValidateReportPath(string value)
        {
            if (String.IsNullOrWhiteSpace(value) || !Path.IsPathRooted(value))
                throw new NetworkFenceException("invalid_report_path", "--report requires an absolute file path.");
            string path = Path.GetFullPath(value);
            if (!Path.IsPathRooted(path) || String.IsNullOrEmpty(Path.GetDirectoryName(path)))
                throw new NetworkFenceException("invalid_report_path", "The setup report path must be absolute.");
            if (!Directory.Exists(Path.GetDirectoryName(path)))
                throw new NetworkFenceException("report_parent_missing", "The setup report parent directory does not exist.");
            if (Directory.Exists(path) || File.Exists(path))
                throw new NetworkFenceException("report_path_exists", "Refusing to overwrite an existing setup report.");
            return path;
        }

        private static Dictionary<string, object> BaseResult(string operation, string sid, FenceKeys keys)
        {
            var filters = new Dictionary<string, bool>(StringComparer.Ordinal);
            foreach (string name in keys.Filters.Keys) filters[name] = false;
            return new Dictionary<string, object>
            {
                { "schema", Schema }, { "operation", operation }, { "ok", false },
                { "error_code", "" }, { "detail", "" }, { "sid", sid },
                { "provider_key", keys.Provider.ToString("D") },
                { "sublayer_key", keys.Sublayer.ToString("D") },
                { "filter_keys", KeyDictionary(keys) }, { "filters", filters },
                { "provider_present", false }, { "sublayer_present", false },
                { "rules_complete", false }, { "loopback_exempt", false },
                { "installed", false },
                { "loopback_ports", new ushort[] { ProxyPortHttpBackend, ProxyPortMux } },
                { "addresses", new string[] { "127.0.0.1", "::1" } },
                { "protocol", "TCP" }, { "requires_administrator", false }
            };
        }

        private static Dictionary<string, string> KeyDictionary(FenceKeys keys)
        {
            var values = new Dictionary<string, string>(StringComparer.Ordinal);
            foreach (KeyValuePair<string, Guid> pair in keys.Filters) values[pair.Key] = pair.Value.ToString("D");
            return values;
        }

        private static Dictionary<string, object> ErrorResult(string code, string detail)
        {
            return new Dictionary<string, object>
            {
                { "schema", Schema }, { "operation", "" }, { "ok", false },
                { "error_code", code }, { "detail", detail }
            };
        }

        private static Dictionary<string, object> ReadStatus(string sid, FenceKeys keys)
        {
            Dictionary<string, object> result = BaseResult("status", sid, keys);
            IntPtr engine = OpenEngine();
            try
            {
                bool provider = ObjectExists(engine, keys.Provider, FwpmProviderGetByKey0,
                    FwpEProviderNotFound, "FwpmProviderGetByKey0");
                bool sublayer = ObjectExists(engine, keys.Sublayer, FwpmSubLayerGetByKey0,
                    FwpESublayerNotFound, "FwpmSubLayerGetByKey0");
                var filters = (Dictionary<string, bool>)result["filters"];
                foreach (KeyValuePair<string, Guid> pair in keys.Filters)
                    filters[pair.Key] = ObjectExists(engine, pair.Value, FwpmFilterGetByKey0,
                        FwpEFilterNotFound, "FwpmFilterGetByKey0");
                bool complete = provider && sublayer;
                foreach (bool present in filters.Values) complete = complete && present;
                result["provider_present"] = provider;
                result["sublayer_present"] = sublayer;
                result["rules_complete"] = complete;
            }
            finally { FwpmEngineClose0(engine); }
            bool loopback = IsLoopbackExempt(sid);
            result["loopback_exempt"] = loopback;
            result["ok"] = true;
            result["installed"] = ObjectBool(result, "rules_complete") && loopback;
            result["detail"] = ObjectBool(result, "installed")
                ? "Per-AppContainer outbound rules and loopback exemption are present."
                : "The AppContainer network fence is absent or incomplete.";
            return result;
        }

        private static bool ObjectExists(IntPtr engine, Guid key,
            ObjectLookup lookup, uint notFound, string operation)
        {
            IntPtr value;
            uint code = lookup(engine, ref key, out value);
            if (code == 0)
            {
                if (value != IntPtr.Zero) FwpmFreeMemory0(ref value);
                return true;
            }
            if (code == notFound) return false;
            CheckWfp(code, operation);
            return false;
        }

        private delegate uint ObjectLookup(IntPtr engine, ref Guid key, out IntPtr value);

        private static void Install(string sid, FenceKeys keys, Dictionary<string, object> result)
        {
            EnsureAdministrator();
            Dictionary<string, object> before = ReadStatus(sid, keys);
            var priorFilters = (Dictionary<string, bool>)before["filters"];
            bool anyPrior = ObjectBool(before, "provider_present") || ObjectBool(before, "sublayer_present");
            foreach (bool present in priorFilters.Values) anyPrior = anyPrior || present;
            bool priorComplete = ObjectBool(before, "rules_complete");
            if (anyPrior && !priorComplete)
                throw new NetworkFenceException("existing_policy_incomplete",
                    "A partial policy with this AppContainer SID is present; refusing to replace it automatically.");
            bool loopbackBefore = IsLoopbackExempt(sid);
            IntPtr engine = OpenEngine();
            try
            {
                if (!priorComplete) ReplaceOwnedPolicy(engine, sid, keys);
                if (!loopbackBefore)
                {
                    try
                    {
                        RunCheckNetIsolation(LoopbackAction.Add, sid);
                        if (!IsLoopbackExempt(sid))
                            throw new NetworkFenceException("loopback_exemption_unconfirmed",
                                "CheckNetIsolation did not report the requested AppContainer SID exemption.");
                    }
                    catch (Exception error)
                    {
                        Exception rollbackError = null;
                        try
                        {
                            if (IsLoopbackExempt(sid))
                            {
                                RunCheckNetIsolation(LoopbackAction.Remove, sid);
                                if (IsLoopbackExempt(sid))
                                    throw new NetworkFenceException("loopback_rollback_unconfirmed",
                                        "The AppContainer loopback exemption remained after rollback.");
                            }
                        }
                        catch (Exception cleanup) { rollbackError = cleanup; }
                        // Keep the committed outbound block in place if removal of
                        // a possibly-created loopback exemption cannot be confirmed.
                        // Dropping the WFP policy here would leave internetClient open.
                        if (rollbackError == null && !priorComplete)
                        {
                            try { RemoveOwnedPolicyTransaction(engine, keys); }
                            catch (Exception cleanup) { rollbackError = rollbackError ?? cleanup; }
                        }
                        if (rollbackError != null)
                            throw new NetworkFenceException("installation_rollback_failed",
                                "Network fence installation failed and rollback could not be confirmed.");
                        throw error;
                    }
                }
            }
            finally { FwpmEngineClose0(engine); }
            Dictionary<string, object> status = ReadStatus(sid, keys);
            CopyStatus(result, status);
            if (!ObjectBool(result, "installed"))
                throw new NetworkFenceException("installation_unconfirmed", "The installed AppContainer network fence did not pass status verification.");
        }

        private static void ReplaceOwnedPolicy(IntPtr engine, string sid, FenceKeys keys)
        {
            uint begin = FwpmTransactionBegin0(engine, 0);
            CheckWfp(begin, "FwpmTransactionBegin0");
            bool transaction = true;
            try
            {
                DeleteOwnedObjects(engine, keys);
                AddProvider(engine, keys);
                AddSublayer(engine, keys);
                var sidValue = new SecurityIdentifier(sid);
                byte[] sidBytes = new byte[sidValue.BinaryLength];
                sidValue.GetBinaryForm(sidBytes, 0);
                IntPtr sidPointer = Marshal.AllocHGlobal(sidBytes.Length);
                try
                {
                    Marshal.Copy(sidBytes, 0, sidPointer, sidBytes.Length);
                    AddBlockFilter(engine, sidPointer, keys, LayerAleAuthConnectV4, "block_v4");
                    AddBlockFilter(engine, sidPointer, keys, LayerAleAuthConnectV6, "block_v6");
                    AddPermitFilter(engine, sidPointer, keys, true, ProxyPortHttpBackend, "allow_v4_60080");
                    AddPermitFilter(engine, sidPointer, keys, true, ProxyPortMux, "allow_v4_60081");
                    AddPermitFilter(engine, sidPointer, keys, false, ProxyPortHttpBackend, "allow_v6_60080");
                    AddPermitFilter(engine, sidPointer, keys, false, ProxyPortMux, "allow_v6_60081");
                }
                finally { Marshal.FreeHGlobal(sidPointer); }
                CheckWfp(FwpmTransactionCommit0(engine), "FwpmTransactionCommit0");
                transaction = false;
            }
            catch
            {
                if (transaction)
                {
                    uint abort = FwpmTransactionAbort0(engine);
                    if (abort != 0)
                        throw new NetworkFenceException("wfp_transaction_rollback_failed",
                            "WFP transaction failed and its rollback could not be confirmed.");
                }
                throw;
            }
        }

        private static void RemoveOwnedPolicyTransaction(IntPtr engine, FenceKeys keys)
        {
            uint begin = FwpmTransactionBegin0(engine, 0);
            CheckWfp(begin, "FwpmTransactionBegin0");
            bool transaction = true;
            try
            {
                DeleteOwnedObjects(engine, keys);
                CheckWfp(FwpmTransactionCommit0(engine), "FwpmTransactionCommit0");
                transaction = false;
            }
            catch
            {
                if (transaction)
                {
                    uint abort = FwpmTransactionAbort0(engine);
                    if (abort != 0)
                        throw new NetworkFenceException("wfp_transaction_rollback_failed",
                            "WFP removal transaction failed and its rollback could not be confirmed.");
                }
                throw;
            }
        }

        private static void DeleteOwnedObjects(IntPtr engine, FenceKeys keys)
        {
            foreach (Guid filterKey in keys.Filters.Values)
            {
                Guid filter = filterKey;
                uint code = FwpmFilterDeleteByKey0(engine, ref filter);
                if (code != 0 && code != FwpEFilterNotFound) CheckWfp(code, "FwpmFilterDeleteByKey0");
            }
            Guid sublayer = keys.Sublayer;
            uint sublayerCode = FwpmSubLayerDeleteByKey0(engine, ref sublayer);
            if (sublayerCode != 0 && sublayerCode != FwpESublayerNotFound)
                CheckWfp(sublayerCode, "FwpmSubLayerDeleteByKey0");
            Guid provider = keys.Provider;
            uint providerCode = FwpmProviderDeleteByKey0(engine, ref provider);
            if (providerCode != 0 && providerCode != FwpEProviderNotFound)
                CheckWfp(providerCode, "FwpmProviderDeleteByKey0");
        }

        private static void AddProvider(IntPtr engine, FenceKeys keys)
        {
            IntPtr name = Marshal.StringToHGlobalUni("MewCode AppContainer network fence");
            IntPtr description = Marshal.StringToHGlobalUni("Persistent outbound fence for one AppContainer profile SID");
            try
            {
                var provider = new FWPM_PROVIDER0
                {
                    providerKey = keys.Provider,
                    displayData = new FWPM_DISPLAY_DATA0 { name = name, description = description },
                    flags = FwpmProviderFlagPersistent,
                    providerData = new FWP_BYTE_BLOB { size = 0, data = IntPtr.Zero },
                    serviceName = IntPtr.Zero
                };
                CheckWfp(FwpmProviderAdd0(engine, ref provider, IntPtr.Zero), "FwpmProviderAdd0");
            }
            finally { Marshal.FreeHGlobal(name); Marshal.FreeHGlobal(description); }
        }

        private static void AddSublayer(IntPtr engine, FenceKeys keys)
        {
            IntPtr name = Marshal.StringToHGlobalUni("MewCode AppContainer network fence");
            IntPtr description = Marshal.StringToHGlobalUni("Per-profile outbound block and local proxy permits");
            IntPtr providerKey = AllocGuid(keys.Provider);
            try
            {
                var sublayer = new FWPM_SUBLAYER0
                {
                    subLayerKey = keys.Sublayer,
                    displayData = new FWPM_DISPLAY_DATA0 { name = name, description = description },
                    flags = FwpmSublayerFlagPersistent,
                    providerKey = providerKey,
                    providerData = new FWP_BYTE_BLOB { size = 0, data = IntPtr.Zero },
                    weight = UInt16.MaxValue
                };
                CheckWfp(FwpmSubLayerAdd0(engine, ref sublayer, IntPtr.Zero), "FwpmSubLayerAdd0");
            }
            finally
            {
                Marshal.FreeHGlobal(name);
                Marshal.FreeHGlobal(description);
                Marshal.FreeHGlobal(providerKey);
            }
        }

        private static void AddBlockFilter(IntPtr engine, IntPtr sid,
            FenceKeys keys, Guid layer, string filterName)
        {
            var conditions = new List<FWPM_FILTER_CONDITION0>
            {
                SidCondition(sid)
            };
            AddFilter(engine, keys, filterName, layer, FwpActionBlock,
                FilterBlockWeight, conditions);
        }

        private static void AddPermitFilter(IntPtr engine, IntPtr sid,
            FenceKeys keys, bool ipv4, ushort port, string filterName)
        {
            var allocations = new List<IntPtr>();
            try
            {
                var conditions = new List<FWPM_FILTER_CONDITION0> { SidCondition(sid) };
                conditions.Add(UInt8Condition(ConditionIpProtocol, IpProtocolTcp));
                conditions.Add(RemoteAddressCondition(ipv4, allocations));
                conditions.Add(UInt16Condition(ConditionIpRemotePort, port));
                AddFilter(engine, keys, filterName,
                    ipv4 ? LayerAleAuthConnectV4 : LayerAleAuthConnectV6,
                    FwpActionPermit, FilterPermitWeight, conditions, allocations);
            }
            finally
            {
                foreach (IntPtr value in allocations) Marshal.FreeHGlobal(value);
            }
        }

        private static FWPM_FILTER_CONDITION0 SidCondition(IntPtr sid)
        {
            return new FWPM_FILTER_CONDITION0
            {
                fieldKey = ConditionAlePackageId,
                matchType = FwpMatchEqual,
                conditionValue = new FWP_CONDITION_VALUE0 { type = FwpSid, sid = sid }
            };
        }

        private static FWPM_FILTER_CONDITION0 UInt8Condition(Guid key, byte value)
        {
            return new FWPM_FILTER_CONDITION0
            {
                fieldKey = key, matchType = FwpMatchEqual,
                conditionValue = new FWP_CONDITION_VALUE0 { type = FwpUint8, uint8 = value }
            };
        }

        private static FWPM_FILTER_CONDITION0 UInt16Condition(Guid key, ushort value)
        {
            return new FWPM_FILTER_CONDITION0
            {
                fieldKey = key, matchType = FwpMatchEqual,
                conditionValue = new FWP_CONDITION_VALUE0 { type = FwpUint16, uint16 = value }
            };
        }

        private static FWPM_FILTER_CONDITION0 RemoteAddressCondition(bool ipv4, List<IntPtr> allocations)
        {
            if (ipv4)
            {
                IntPtr mask = Marshal.AllocHGlobal(Marshal.SizeOf(typeof(FWP_V4_ADDR_AND_MASK)));
                allocations.Add(mask);
                Marshal.StructureToPtr(new FWP_V4_ADDR_AND_MASK
                {
                    addr = 0x7F000001U, // WFP IPv4 numeric host order, independent of CLR byte order
                    mask = UInt32.MaxValue
                }, mask, false);
                return new FWPM_FILTER_CONDITION0
                {
                    fieldKey = ConditionIpRemoteAddress,
                    matchType = FwpMatchEqual,
                    conditionValue = new FWP_CONDITION_VALUE0 { type = FwpV4AddrMask, v4AddrMask = mask }
                };
            }
            else
            {
                byte[] bytes = IPAddress.IPv6Loopback.GetAddressBytes();
                IntPtr address = Marshal.AllocHGlobal(Marshal.SizeOf(typeof(FWP_V6_ADDR_AND_MASK)));
                allocations.Add(address);
                Marshal.StructureToPtr(new FWP_V6_ADDR_AND_MASK
                {
                    addr = bytes,
                    prefixLength = 128
                }, address, false);
                return new FWPM_FILTER_CONDITION0
                {
                    fieldKey = ConditionIpRemoteAddress,
                    matchType = FwpMatchEqual,
                    conditionValue = new FWP_CONDITION_VALUE0 { type = FwpV6AddrMask, v6AddrMask = address }
                };
            }
        }

        private static void AddFilter(IntPtr engine, FenceKeys keys, string keyName,
            Guid layer, uint action, uint weight, List<FWPM_FILTER_CONDITION0> conditions,
            List<IntPtr> extraAllocations = null)
        {
            var allocations = new List<IntPtr>();
            if (extraAllocations != null) allocations.AddRange(extraAllocations);
            IntPtr conditionMemory = IntPtr.Zero;
            IntPtr providerKey = IntPtr.Zero;
            IntPtr name = IntPtr.Zero;
            IntPtr description = IntPtr.Zero;
            IntPtr weightMemory = IntPtr.Zero;
            try
            {
                int conditionSize = Marshal.SizeOf(typeof(FWPM_FILTER_CONDITION0));
                conditionMemory = Marshal.AllocHGlobal(conditionSize * conditions.Count);
                for (int i = 0; i < conditions.Count; i++)
                    Marshal.StructureToPtr(conditions[i], IntPtr.Add(conditionMemory, conditionSize * i), false);
                providerKey = AllocGuid(keys.Provider);
                name = Marshal.StringToHGlobalUni("MewCode network fence " + keyName);
                description = Marshal.StringToHGlobalUni("Only this AppContainer SID; TCP local proxy endpoint policy");
                weightMemory = Marshal.AllocHGlobal(sizeof(ulong));
                Marshal.WriteInt64(weightMemory, unchecked((long)weight));
                var filter = new FWPM_FILTER0
                {
                    filterKey = keys.Filters[keyName],
                    displayData = new FWPM_DISPLAY_DATA0 { name = name, description = description },
                    flags = FwpmFilterFlagPersistent,
                    providerKey = providerKey,
                    providerData = new FWP_BYTE_BLOB { size = 0, data = IntPtr.Zero },
                    layerKey = layer,
                    subLayerKey = keys.Sublayer,
                    weight = new FWP_VALUE0 { type = FwpUint64, uint64 = weightMemory },
                    numFilterConditions = (uint)conditions.Count,
                    filterCondition = conditionMemory,
                    action = new FWPM_ACTION0 { type = action, value = new FWPM_ACTION_UNION() },
                    context = new FWPM_FILTER_CONTEXT { rawContext = 0 },
                    reserved = IntPtr.Zero,
                    filterId = 0,
                    effectiveWeight = new FWP_VALUE0 { type = 0, uint64 = IntPtr.Zero }
                };
                CheckWfp(FwpmFilterAdd0(engine, ref filter, IntPtr.Zero, IntPtr.Zero), "FwpmFilterAdd0");
            }
            finally
            {
                if (conditionMemory != IntPtr.Zero) Marshal.FreeHGlobal(conditionMemory);
                if (providerKey != IntPtr.Zero) Marshal.FreeHGlobal(providerKey);
                if (name != IntPtr.Zero) Marshal.FreeHGlobal(name);
                if (description != IntPtr.Zero) Marshal.FreeHGlobal(description);
                if (weightMemory != IntPtr.Zero) Marshal.FreeHGlobal(weightMemory);
                // Address condition memory is owned by the permit filter call.
                if (extraAllocations == null)
                    foreach (IntPtr allocation in allocations) Marshal.FreeHGlobal(allocation);
            }
        }

        private static void Uninstall(string sid, FenceKeys keys, Dictionary<string, object> result)
        {
            EnsureAdministrator();
            bool loopbackBefore = IsLoopbackExempt(sid);
            IntPtr engine = OpenEngine();
            try
            {
                try
                {
                    if (loopbackBefore)
                    {
                        RunCheckNetIsolation(LoopbackAction.Remove, sid);
                        if (IsLoopbackExempt(sid))
                            throw new NetworkFenceException("loopback_removal_unconfirmed",
                                "CheckNetIsolation did not confirm removal of this AppContainer SID exemption.");
                    }
                    RemoveOwnedPolicyTransaction(engine, keys);
                }
                catch (Exception error)
                {
                    if (loopbackBefore)
                    {
                        try
                        {
                            if (!IsLoopbackExempt(sid)) RunCheckNetIsolation(LoopbackAction.Add, sid);
                            if (!IsLoopbackExempt(sid))
                                throw new NetworkFenceException("loopback_restore_unconfirmed",
                                    "CheckNetIsolation did not confirm restoration of the prior exemption.");
                        }
                        catch
                        {
                            throw new NetworkFenceException("uninstall_rollback_failed",
                                "WFP removal failed and the prior SID-only loopback exemption could not be restored.");
                        }
                    }
                    throw error;
                }
                result["loopback_exempt"] = false;
                result["rules_complete"] = false;
                result["installed"] = false;
            }
            finally { FwpmEngineClose0(engine); }
        }

        private static bool IsLoopbackExempt(string sid)
        {
            CheckNetResult result = RunCheckNetIsolation(LoopbackAction.List, sid);
            string pattern = @"(?im)^\s*SID:\s*" + Regex.Escape(sid) + @"\s*$";
            return Regex.IsMatch(result.StandardOutput, pattern, RegexOptions.CultureInvariant);
        }

        private enum LoopbackAction { List, Add, Remove }

        private sealed class CheckNetResult
        {
            public string StandardOutput;
            public string StandardError;
        }

        private static CheckNetResult RunCheckNetIsolation(LoopbackAction action, string sid)
        {
            if (Environment.OSVersion.Platform != PlatformID.Win32NT)
                throw new NetworkFenceException("windows_only", "CheckNetIsolation is Windows-only.");
            string system32 = Environment.GetFolderPath(Environment.SpecialFolder.System);
            if (String.IsNullOrEmpty(system32) || !String.Equals(Path.GetFileName(system32), "System32", StringComparison.OrdinalIgnoreCase))
                throw new NetworkFenceException("system_tool_unavailable", "The native System32 directory could not be verified.");
            string executable = Path.Combine(system32, "CheckNetIsolation.exe");
            if (!File.Exists(executable))
                throw new NetworkFenceException("system_tool_unavailable", "System32\\CheckNetIsolation.exe is unavailable.");
            string arguments;
            switch (action)
            {
                case LoopbackAction.List: arguments = "LoopbackExempt -s"; break;
                case LoopbackAction.Add: arguments = "LoopbackExempt -a -p=" + sid; break;
                case LoopbackAction.Remove: arguments = "LoopbackExempt -d -p=" + sid; break;
                default: throw new NetworkFenceException("invalid_operation", "Unknown loopback operation.");
            }
            // This is a direct ProcessStartInfo launch, never cmd.exe or PowerShell.
            // SID is canonicalized by SecurityIdentifier and can contain only digits and hyphens.
            var start = new ProcessStartInfo
            {
                FileName = executable,
                Arguments = arguments,
                UseShellExecute = false,
                CreateNoWindow = true,
                WindowStyle = ProcessWindowStyle.Hidden,
                RedirectStandardOutput = true,
                RedirectStandardError = true
            };
            try
            {
                using (Process process = Process.Start(start))
                {
                    string stdout = process.StandardOutput.ReadToEnd();
                    string stderr = process.StandardError.ReadToEnd();
                    if (!process.WaitForExit(30000))
                    {
                        try { process.Kill(); } catch { }
                        process.WaitForExit(5000);
                        throw new NetworkFenceException("system_tool_timeout", "CheckNetIsolation did not finish within 30 seconds.");
                    }
                    if (process.ExitCode != 0)
                    {
                        if (process.ExitCode == 5 || process.ExitCode == 740)
                            throw new NetworkFenceException("administrator_required", "CheckNetIsolation requires an explicitly elevated host setup call.");
                        throw new NetworkFenceException("loopback_operation_failed", "CheckNetIsolation reported a failure.");
                    }
                    return new CheckNetResult { StandardOutput = stdout, StandardError = stderr };
                }
            }
            catch (NetworkFenceException) { throw; }
            catch (Win32Exception error)
            {
                if (error.NativeErrorCode == 5 || error.NativeErrorCode == 740)
                    throw new NetworkFenceException("administrator_required", "CheckNetIsolation requires an explicitly elevated host setup call.");
                throw new NetworkFenceException("system_tool_start_failed", "System32 CheckNetIsolation could not be started.");
            }
            catch (Exception)
            {
                throw new NetworkFenceException("system_tool_failed", "System32 CheckNetIsolation did not complete.");
            }
        }

        private static void EnsureAdministrator()
        {
            if (Environment.OSVersion.Platform != PlatformID.Win32NT)
                throw new NetworkFenceException("windows_only", "The network fence can only be installed on Windows.");
            try
            {
                using (WindowsIdentity identity = WindowsIdentity.GetCurrent())
                {
                    var principal = new WindowsPrincipal(identity);
                    if (!principal.IsInRole(WindowsBuiltInRole.Administrator))
                        throw new NetworkFenceException("administrator_required",
                            "Run the explicitly requested one-time host setup as an administrator; no elevation was requested automatically.");
                }
            }
            catch (NetworkFenceException) { throw; }
            catch (Exception)
            {
                throw new NetworkFenceException("administrator_status_unavailable",
                    "Administrator status could not be verified; no setup changes were made.");
            }
        }

        private static IntPtr OpenEngine()
        {
            IntPtr engine;
            uint code = FwpmEngineOpen0(null, RpcCAuthnWinnt, IntPtr.Zero, IntPtr.Zero, out engine);
            CheckWfp(code, "FwpmEngineOpen0");
            if (engine == IntPtr.Zero)
                throw new NetworkFenceException("wfp_engine_unavailable", "FwpmEngineOpen0 returned no engine handle.");
            return engine;
        }

        private static void CheckWfp(uint code, string operation)
        {
            if (code == 0) return;
            if (code == ErrorAccessDenied || code == EAccessDenied)
                throw new NetworkFenceException("administrator_required",
                    "" + operation + " requires an explicitly elevated host setup call; no elevation was attempted.");
            throw new NetworkFenceException("wfp_operation_failed",
                operation + " failed with Windows Filtering Platform code 0x" + code.ToString("X8") + ".");
        }

        private static IntPtr AllocGuid(Guid value)
        {
            IntPtr pointer = Marshal.AllocHGlobal(16);
            Marshal.StructureToPtr(value, pointer, false);
            return pointer;
        }

        private static Dictionary<string, int> ValidateLayouts()
        {
            if (IntPtr.Size != 8)
                throw new InvalidOperationException("The WFP marshalling layouts are pinned to x64.");
            var sizes = new Dictionary<string, int>
            {
                { "FWPM_DISPLAY_DATA0", Marshal.SizeOf(typeof(FWPM_DISPLAY_DATA0)) },
                { "FWP_BYTE_BLOB", Marshal.SizeOf(typeof(FWP_BYTE_BLOB)) },
                { "FWPM_PROVIDER0", Marshal.SizeOf(typeof(FWPM_PROVIDER0)) },
                { "FWPM_SUBLAYER0", Marshal.SizeOf(typeof(FWPM_SUBLAYER0)) },
                { "FWP_VALUE0", Marshal.SizeOf(typeof(FWP_VALUE0)) },
                { "FWP_CONDITION_VALUE0", Marshal.SizeOf(typeof(FWP_CONDITION_VALUE0)) },
                { "FWPM_FILTER_CONDITION0", Marshal.SizeOf(typeof(FWPM_FILTER_CONDITION0)) },
                { "FWPM_ACTION0", Marshal.SizeOf(typeof(FWPM_ACTION0)) },
                { "FWPM_FILTER0", Marshal.SizeOf(typeof(FWPM_FILTER0)) },
                { "FWP_V6_ADDR_AND_MASK", Marshal.SizeOf(typeof(FWP_V6_ADDR_AND_MASK)) }
            };
            var expected = new Dictionary<string, int>
            {
                { "FWPM_DISPLAY_DATA0", 16 }, { "FWP_BYTE_BLOB", 16 },
                { "FWPM_PROVIDER0", 64 }, { "FWPM_SUBLAYER0", 72 },
                { "FWP_VALUE0", 16 }, { "FWP_CONDITION_VALUE0", 16 },
                { "FWPM_FILTER_CONDITION0", 40 }, { "FWPM_ACTION0", 20 },
                { "FWPM_FILTER0", 200 }, { "FWP_V6_ADDR_AND_MASK", 17 }
            };
            foreach (KeyValuePair<string, int> pair in expected)
                if (sizes[pair.Key] != pair.Value)
                    throw new InvalidOperationException(pair.Key + " x64 size differs from Windows SDK fwpmtypes.h/fwptypes.h.");
            CheckOffset(typeof(FWPM_PROVIDER0), "providerData", 40);
            CheckOffset(typeof(FWPM_PROVIDER0), "serviceName", 56);
            CheckOffset(typeof(FWPM_SUBLAYER0), "providerKey", 40);
            CheckOffset(typeof(FWPM_SUBLAYER0), "providerData", 48);
            CheckOffset(typeof(FWPM_SUBLAYER0), "weight", 64);
            CheckOffset(typeof(FWP_VALUE0), "uint64", 8);
            CheckOffset(typeof(FWPM_FILTER_CONDITION0), "conditionValue", 24);
            CheckOffset(typeof(FWPM_ACTION0), "value", 4);
            CheckOffset(typeof(FWP_V6_ADDR_AND_MASK), "prefixLength", 16);
            CheckOffset(typeof(FWPM_FILTER0), "providerKey", 40);
            CheckOffset(typeof(FWPM_FILTER0), "providerData", 48);
            CheckOffset(typeof(FWPM_FILTER0), "weight", 96);
            CheckOffset(typeof(FWPM_FILTER0), "filterCondition", 120);
            CheckOffset(typeof(FWPM_FILTER0), "action", 128);
            CheckOffset(typeof(FWPM_FILTER0), "context", 152);
            CheckOffset(typeof(FWPM_FILTER0), "reserved", 168);
            CheckOffset(typeof(FWPM_FILTER0), "filterId", 176);
            CheckOffset(typeof(FWPM_FILTER0), "effectiveWeight", 184);
            return sizes;
        }

        private static void CheckOffset(Type type, string field, int expected)
        {
            int actual = Marshal.OffsetOf(type, field).ToInt32();
            if (actual != expected)
                throw new InvalidOperationException(type.Name + "." + field + " has an unexpected x64 offset.");
        }

        private static void CopyStatus(Dictionary<string, object> destination,
            Dictionary<string, object> source)
        {
            string[] fields = { "filters", "provider_present", "sublayer_present", "rules_complete",
                "loopback_exempt", "installed" };
            foreach (string field in fields)
                if (source.ContainsKey(field)) destination[field] = source[field];
        }

        private static bool ObjectBool(IDictionary<string, object> value, string key)
        {
            return value.ContainsKey(key) && value[key] is bool && (bool)value[key];
        }

        private static string SafeDetail(Exception error)
        {
            return error == null ? "Unexpected network fence failure." :
                "Network fence operation failed (" + error.GetType().Name + ").";
        }

        private static string Serialize(object value)
        {
            return new JavaScriptSerializer().Serialize(value);
        }

        private static void WriteReport(FileStream stream, Dictionary<string, object> result)
        {
            byte[] bytes = new UTF8Encoding(false).GetBytes(Serialize(result));
            if (bytes.Length == 0 || bytes.Length > 65536)
                throw new InvalidDataException("The setup report is outside the fixed size bound.");
            stream.Position = 0;
            stream.SetLength(0);
            stream.Write(bytes, 0, bytes.Length);
            stream.Flush(true);
        }

        [StructLayout(LayoutKind.Sequential, Pack = 8)]
        private struct FWPM_DISPLAY_DATA0 { public IntPtr name; public IntPtr description; }

        [StructLayout(LayoutKind.Sequential, Pack = 8)]
        private struct FWP_BYTE_BLOB { public uint size; public IntPtr data; }

        [StructLayout(LayoutKind.Sequential, Pack = 8)]
        private struct FWPM_PROVIDER0
        {
            public Guid providerKey;
            public FWPM_DISPLAY_DATA0 displayData;
            public uint flags;
            public FWP_BYTE_BLOB providerData;
            public IntPtr serviceName;
        }

        [StructLayout(LayoutKind.Sequential, Pack = 8)]
        private struct FWPM_SUBLAYER0
        {
            public Guid subLayerKey;
            public FWPM_DISPLAY_DATA0 displayData;
            public uint flags;
            public IntPtr providerKey;
            public FWP_BYTE_BLOB providerData;
            public ushort weight;
        }

        [StructLayout(LayoutKind.Explicit, Pack = 8, Size = 16)]
        private struct FWP_VALUE0
        {
            [FieldOffset(0)] public uint type;
            [FieldOffset(8)] public IntPtr uint64;
        }

        [StructLayout(LayoutKind.Explicit, Pack = 8, Size = 16)]
        private struct FWP_CONDITION_VALUE0
        {
            [FieldOffset(0)] public uint type;
            [FieldOffset(8)] public byte uint8;
            [FieldOffset(8)] public ushort uint16;
            [FieldOffset(8)] public IntPtr sid;
            [FieldOffset(8)] public IntPtr v4AddrMask;
            [FieldOffset(8)] public IntPtr v6AddrMask;
        }

        [StructLayout(LayoutKind.Sequential, Pack = 8)]
        private struct FWPM_FILTER_CONDITION0
        {
            public Guid fieldKey;
            public uint matchType;
            public FWP_CONDITION_VALUE0 conditionValue;
        }

        [StructLayout(LayoutKind.Explicit, Pack = 8, Size = 16)]
        private struct FWPM_ACTION_UNION { [FieldOffset(0)] public Guid filterType; }

        [StructLayout(LayoutKind.Sequential, Pack = 8)]
        private struct FWPM_ACTION0
        {
            public uint type;
            public FWPM_ACTION_UNION value;
        }

        [StructLayout(LayoutKind.Sequential, Pack = 8)]
        private struct FWPM_FILTER0
        {
            public Guid filterKey;
            public FWPM_DISPLAY_DATA0 displayData;
            public uint flags;
            public IntPtr providerKey;
            public FWP_BYTE_BLOB providerData;
            public Guid layerKey;
            public Guid subLayerKey;
            public FWP_VALUE0 weight;
            public uint numFilterConditions;
            public IntPtr filterCondition;
            public FWPM_ACTION0 action;
            public FWPM_FILTER_CONTEXT context;
            public IntPtr reserved;
            public ulong filterId;
            public FWP_VALUE0 effectiveWeight;
        }

        [StructLayout(LayoutKind.Explicit, Pack = 8, Size = 16)]
        private struct FWPM_FILTER_CONTEXT
        {
            [FieldOffset(0)] public ulong rawContext;
            [FieldOffset(0)] public Guid providerContextKey;
        }

        [StructLayout(LayoutKind.Sequential, Pack = 8)]
        private struct FWP_V4_ADDR_AND_MASK { public uint addr; public uint mask; }

        [StructLayout(LayoutKind.Sequential, Pack = 8)]
        private struct FWP_V6_ADDR_AND_MASK { [MarshalAs(UnmanagedType.ByValArray, SizeConst = 16)] public byte[] addr; public byte prefixLength; }

        [DllImport("Fwpuclnt.dll", CharSet = CharSet.Unicode, PreserveSig = true)]
        private static extern uint FwpmEngineOpen0(string serverName, uint authnService,
            IntPtr authIdentity, IntPtr session, out IntPtr engineHandle);
        [DllImport("Fwpuclnt.dll", PreserveSig = true)]
        private static extern uint FwpmEngineClose0(IntPtr engineHandle);
        [DllImport("Fwpuclnt.dll", PreserveSig = true)]
        private static extern uint FwpmTransactionBegin0(IntPtr engineHandle, uint flags);
        [DllImport("Fwpuclnt.dll", PreserveSig = true)]
        private static extern uint FwpmTransactionCommit0(IntPtr engineHandle);
        [DllImport("Fwpuclnt.dll", PreserveSig = true)]
        private static extern uint FwpmTransactionAbort0(IntPtr engineHandle);
        [DllImport("Fwpuclnt.dll", PreserveSig = true)]
        private static extern uint FwpmProviderAdd0(IntPtr engineHandle, ref FWPM_PROVIDER0 provider,
            IntPtr securityDescriptor);
        [DllImport("Fwpuclnt.dll", PreserveSig = true)]
        private static extern uint FwpmProviderDeleteByKey0(IntPtr engineHandle, ref Guid key);
        [DllImport("Fwpuclnt.dll", PreserveSig = true)]
        private static extern uint FwpmProviderGetByKey0(IntPtr engineHandle, ref Guid key, out IntPtr provider);
        [DllImport("Fwpuclnt.dll", PreserveSig = true)]
        private static extern uint FwpmSubLayerAdd0(IntPtr engineHandle, ref FWPM_SUBLAYER0 sublayer,
            IntPtr securityDescriptor);
        [DllImport("Fwpuclnt.dll", PreserveSig = true)]
        private static extern uint FwpmSubLayerDeleteByKey0(IntPtr engineHandle, ref Guid key);
        [DllImport("Fwpuclnt.dll", PreserveSig = true)]
        private static extern uint FwpmSubLayerGetByKey0(IntPtr engineHandle, ref Guid key, out IntPtr sublayer);
        [DllImport("Fwpuclnt.dll", PreserveSig = true)]
        private static extern uint FwpmFilterAdd0(IntPtr engineHandle, ref FWPM_FILTER0 filter,
            IntPtr securityDescriptor, IntPtr id);
        [DllImport("Fwpuclnt.dll", PreserveSig = true)]
        private static extern uint FwpmFilterDeleteByKey0(IntPtr engineHandle, ref Guid key);
        [DllImport("Fwpuclnt.dll", PreserveSig = true)]
        private static extern uint FwpmFilterGetByKey0(IntPtr engineHandle, ref Guid key, out IntPtr filter);
        [DllImport("Fwpuclnt.dll", PreserveSig = true)]
        private static extern void FwpmFreeMemory0(ref IntPtr memory);
    }
}
