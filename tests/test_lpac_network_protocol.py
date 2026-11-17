from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read_source(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_network_mode_is_triple_gated_and_wraps_original_staged_bytes():
    runtime = read_source("native/windows/MewCode.AppContainerRuntime.cs")
    staged = read_source("native/windows/MewCode.StagedWorkspacePrototype.cs")

    assert '"--host-stdin-v1"' in runtime
    assert '"--host-network-stdin-v1"' in runtime
    assert '"--provision-network-profile-v1"' in runtime
    assert '"--network-profile-status-v1"' in runtime
    assert '"mewcode-lpac-network-transport-v1"' in runtime
    assert '"mewcode-lpac-network-request-v1"' in runtime
    assert "public bool? runtime_network { get; set; }" in runtime
    assert "validated.RuntimeNetwork != networkProtocol" in runtime
    assert "The network protocol requires the existing staged-workspace envelope." in runtime
    assert "Convert.FromBase64String(transport.request_base64)" in runtime
    assert "requestBytes.Length > MaxRequestBytes" in runtime
    assert "HashRequestBytes(requestBytes, count)" in runtime
    assert "ValidateStagedRuntimeRequest(staged)" in runtime
    assert "RuntimeNetwork = networkRequest" in runtime
    assert '"mewcode-lpac-staged-request-v1"' in staged


def test_network_transport_environment_is_separate_and_loopback_only():
    runtime = read_source("native/windows/MewCode.AppContainerRuntime.cs")

    assert "public Dictionary<string, string> proxy_environment { get; set; }" in runtime
    assert '"HTTP_PROXY", "HTTPS_PROXY", "GRPC_PROXY", "ALL_PROXY", "NO_PROXY"' in runtime
    assert '"@127.0.0.1:60081"' in runtime
    assert 'TryParseLoopbackProxy(value, "http"' in runtime
    assert 'TryParseLoopbackProxy(value, "socks5"' in runtime
    assert 'TryParseLoopbackProxy(value, "socks5h"' in runtime
    assert "candidate.Length > 4096" in runtime
    assert "NO_PROXY must be empty" in runtime
    assert "GIT_CONFIG_PARAMETERS must use the fixed network proxy authentication setting." in runtime
    assert "'http.proxyAuthMethod=basic'" in runtime
    assert "MergeNetworkEnvironment(validated, proxyEnvironment)" in runtime
    # Proxy URLs stay out of diagnostics and receipt serialization.
    assert "fields[\"network\"] = receipt.network" in runtime
    assert "fields[\"HTTP_PROXY\"]" not in runtime


def test_network_capability_and_per_user_profile_are_scoped_and_cleaned():
    probe = read_source("native/windows/MewCode.AppContainerProbe.cs")
    runtime = read_source("native/windows/MewCode.AppContainerRuntime.cs")

    assert 'capabilityNames.Add("internetClient")' in probe
    assert "if (request.RuntimeNetwork)" in probe
    assert "EnsureHostProcessToken();" in runtime
    assert 'GetTokenInformation(token, 29' in probe
    assert "An AppContainer caller cannot invoke the network host protocol." in probe
    assert '"mewcode-network-v1-"' in probe
    assert "SHA256.Create()" in probe
    assert "Encoding.UTF8.GetBytes(normalizedSid)" in probe
    assert "Registry.CurrentUser.OpenSubKey(mappingPath, false)" in probe
    assert 'mapping.GetValue("Moniker"' in probe
    assert "GetAppContainerFolderPath(profileSid" in probe
    assert ('[DllImport("userenv.dll", CharSet = CharSet.Unicode, PreserveSig = true)]\n'
            '        private static extern int GetAppContainerFolderPath(string appContainerSid, out IntPtr path);') in probe
    assert "Directory.Exists(folderPath)" in probe
    assert "CreateNetworkExecutionProfile(validated, state, receipt)" in probe
    assert "DeleteAppContainerProfile(profileName)" in probe
    assert "persistent_profile = false" in probe
    assert "receipt.profile_cleanup_ok = true;" in probe
    assert '"internetClient"' in probe
    status = probe.split("internal static NetworkProfileDetails NetworkProfileStatus()", 1)[1]
    status = status.split("private static DerivedNetworkProfile VerifyExistingNetworkProfile", 1)[0]
    assert "DeriveNetworkProfileSid(profileName)" in status
    assert "CreateAppContainerProfile" not in status
    assert "DeleteAppContainerProfile" not in status


def test_offline_runtime_stays_on_its_original_schema_and_capability_set():
    runtime = read_source("native/windows/MewCode.AppContainerRuntime.cs")
    probe = read_source("native/windows/MewCode.AppContainerProbe.cs")

    assert 'request.schema != "mewcode-lpac-request-v1"' in runtime
    assert '"registryRead"' in probe
    assert '"lpacInstrumentation"' in probe
    assert '"lpacCom"' in probe
    assert "RuntimeNetwork = networkRequest" in runtime
    # The new receipt details are emitted only by network mode, leaving the
    # long-standing offline ProbeReceipt serialization shape alone.
    assert "[ScriptIgnore]" in probe
    assert "if (receipt.network == null) return receipt;" in runtime
