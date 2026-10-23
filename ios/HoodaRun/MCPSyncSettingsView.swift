//  MCPSyncSettingsView.swift — HoodaRun
//
//  Where the user points HoodaRun at the apple-workouts-mcp feed and syncs.
//  Push it from HoodaRun's settings screen:
//
//      NavigationLink("Claude / MCP plans") { MCPSyncSettingsView() }

import SwiftUI
import UniformTypeIdentifiers

struct MCPSyncSettingsView: View {
    @State private var pickingFolder = false
    @State private var feedURL = ""
    @State private var token = ""
    @State private var syncing = false
    @State private var message: String?
    @State private var sourceLabel = Self.describe(MCPSyncSettings.source())
    @State private var useCloudKit = CloudKitPlanStore.isEnabled

    var body: some View {
        Form {
            Section {
                LabeledContent("Source", value: sourceLabel)
                Button {
                    Task { await runSync() }
                } label: {
                    if syncing { ProgressView() } else { Text("Sync now") }
                }
                .disabled(syncing || (MCPSyncSettings.source() == nil && !useCloudKit))
                if let message { Text(message).font(.footnote).foregroundStyle(.secondary) }
            } footer: {
                Text("Plans built with Claude land in Workouts on your Apple Watch. "
                     + "HoodaRun also syncs whenever it opens.")
            }

            Section {
                Toggle("Workouts from yashhooda.ai", isOn: $useCloudKit)
                    .onChange(of: useCloudKit) { _, on in CloudKitPlanStore.isEnabled = on }
            } header: {
                Text("iCloud")
            } footer: {
                Text("Workouts you build at yashhooda.ai while signed in with this Apple ID.")
            }

            Section {
                Button("Choose feed folder…") { pickingFolder = true }
            } header: {
                Text("Local Mac (iCloud Drive)")
            } footer: {
                Text("Pick iCloud Drive › HoodaRun › MCP — the folder the MCP server writes to.")
            }

            Section("Remote server (HTTP)") {
                TextField("https://your-host", text: $feedURL)
                    .textInputAutocapitalization(.never)
                    .keyboardType(.URL)
                    .autocorrectionDisabled()
                SecureField("MCP_AUTH_TOKEN", text: $token)
                Button("Save server") {
                    guard let url = URL(string: feedURL), url.scheme == "https",
                          !token.isEmpty else {
                        message = "Use an https URL and the server's bearer token."
                        return
                    }
                    MCPSyncSettings.saveHTTP(url, token: token)
                    token = ""
                    sourceLabel = Self.describe(MCPSyncSettings.source())
                    message = "Server saved."
                }
            }
        }
        .navigationTitle("Claude plans")
        .fileImporter(isPresented: $pickingFolder, allowedContentTypes: [.folder]) { result in
            switch result {
            case .success(let url):
                do {
                    try MCPSyncSettings.saveFolder(url)
                    sourceLabel = Self.describe(MCPSyncSettings.source())
                    message = "Folder saved."
                } catch {
                    message = error.localizedDescription
                }
            case .failure(let error):
                message = error.localizedDescription
            }
        }
    }

    private func runSync() async {
        syncing = true
        defer { syncing = false }
        do {
            let r = try await MCPPlanSync.shared.sync()
            var parts = ["\(r.scheduled) scheduled", "\(r.removed) removed"]
            if r.pending > 0 { parts.append("\(r.pending) waiting for a free slot") }
            if !r.errors.isEmpty { parts.append("\(r.errors.count) failed: \(r.errors[0])") }
            message = parts.joined(separator: " · ")
        } catch {
            message = error.localizedDescription
        }
    }

    private static func describe(_ s: MCPFeedSource?) -> String {
        switch s {
        case .folder(let url): return url.lastPathComponent
        case .http(let base, _): return base.host() ?? base.absoluteString
        case nil: return "Not set"
        }
    }
}
